"""Offline tests for ConceptioClient + directive parsing (mocked HTTP transport)."""

import json

import httpx
import pytest

import conceptio_cli.client as client_mod
from conceptio_cli import __version__
from conceptio_cli.client import ConceptioClient, ConceptioError, parse_query_directives


def test_version_and_user_agent():
    # version resolves via installed metadata (or the literal fallback) and is never empty
    assert isinstance(__version__, str) and __version__
    assert len(__version__.split(".")) >= 2
    # USER_AGENT embeds the same version it advertises
    assert client_mod.USER_AGENT == f"conceptio-cli/{__version__}"


_CURRENT_HANDLER = {"fn": None}


@pytest.fixture(autouse=True)
def _mock_transport(monkeypatch):
    """Patch httpx.Client.__init__ exactly once, dispatching to the current handler."""
    real_init = httpx.Client.__init__

    def patched_init(self, *a, **kw):
        if _CURRENT_HANDLER["fn"] is not None:
            kw["transport"] = httpx.MockTransport(_CURRENT_HANDLER["fn"])
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    yield
    _CURRENT_HANDLER["fn"] = None


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch):
    """Never read/write the real ~/.conceptio config during tests."""
    cfg = {
        "api_base": "https://conceptio.test",
        "license_key": "",
        "api_key": "",
        "default_limit": 10,
        "default_citation_format": "bibtex",
    }
    monkeypatch.setattr(client_mod, "load_config", lambda: cfg.copy())


def _make_client(handler, license_key="", api_key=""):
    _CURRENT_HANDLER["fn"] = handler
    return ConceptioClient(license_key=license_key, api_key=api_key)


def _json_handler(payload, status=200):
    def handler(request):
        return httpx.Response(status, json=payload, request=request)

    return handler


# ── rebrand / default endpoint ───────────────────────────────────────────────
def test_api_base_rejects_plaintext_public_origin():
    with pytest.raises(ConceptioError, match="require HTTPS"):
        ConceptioClient(api_base="http://api.example.com")


def test_api_base_allows_loopback_development_origin():
    client = ConceptioClient(api_base="http://127.0.0.1:8000")
    assert client.api_base == "http://127.0.0.1:8000"


def test_api_base_rejects_embedded_credentials():
    with pytest.raises(ConceptioError, match="embedded credentials"):
        ConceptioClient(api_base="https://user:pass@example.com")


def test_default_api_base_is_conceptio():
    # Machine clients must target www — the apex host redirects to it.
    from conceptio_cli.config import DEFAULT_API_BASE, DEFAULT_CONFIG

    assert DEFAULT_API_BASE == "https://www.conceptio.app"
    assert DEFAULT_CONFIG["api_base"] == "https://www.conceptio.app"


def test_no_legacy_domain_in_public_strings():
    # No user-facing string may point at anything but Conceptio-owned hosts.
    import re

    from conceptio_cli.config import DEFAULT_API_BASE

    candidates = [DEFAULT_API_BASE, client_mod.UPGRADE_HINT]
    hosts = set()
    for text in candidates:
        hosts.update(re.findall(r"https?://([^/\s)'\"]+)", text))
    assert hosts, "expected at least one URL in public strings"
    assert all(h.lower().rstrip(".").endswith("conceptio.app") for h in hosts)
    assert "conceptio.app" in client_mod.UPGRADE_HINT


# ── directive parsing ─────────────────────────────────────────────────────────
def test_parse_directives_source_and_lang():
    parsed = parse_query_directives("source:nist zero trust maturity model lang:en")
    assert parsed["query"] == "zero trust maturity model"
    assert parsed["sources"] == ["nist"]
    assert parsed["language"] == "en"


def test_parse_directives_category_and_multiple_sources():
    parsed = parse_query_directives('cat:"Law & Regulation" source:eurlex source:hudoc AI act')
    assert parsed["query"] == "AI act"
    assert parsed["sources"] == ["eurlex", "hudoc"]
    assert parsed["category"] == "Law & Regulation"


def test_parse_directives_no_directives():
    parsed = parse_query_directives("attention is all you need")
    assert parsed["query"] == "attention is all you need"
    assert parsed["sources"] == []
    assert parsed["category"] == ""


# ── search ────────────────────────────────────────────────────────────────────
def test_search_preserves_additive_free_attribution():
    client = _make_client(_json_handler({
        "total": 0,
        "results": [],
        "attribution": {"text": "Provided by Conceptio.", "url": "https://www.conceptio.app"},
    }), api_key="ckey_live_abcdef0123456789abcdef0123456789")
    data = client.search("hello")
    assert data["attribution"] == {"text": "Provided by Conceptio.", "url": "https://www.conceptio.app"}


def test_search_sends_clean_params():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"total": 1, "results": []}, request=request)

    client = _make_client(handler)
    client.search("source:nist zero trust", limit=5, license="commercial-ok")
    assert "/api/search" in seen["url"]
    assert seen["params"]["q"] == "zero trust"
    assert seen["params"]["sources"] == "nist"
    assert seen["params"]["license"] == "commercial-ok"
    assert seen["params"]["limit"] == "5"


def test_search_429_returns_friendly_error():
    client = _make_client(_json_handler({}, status=429))
    data = client.search("anything")
    assert data["error"]
    assert "Dev plan" in data["error"]


def test_search_500_retries_then_raises():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, json={"detail": "boom"}, request=request)

    client = _make_client(handler)
    with pytest.raises(ConceptioError):
        client.search("query")
    assert len(calls) == 2  # one retry


def test_search_transport_failure_raises_conceptio_error():
    def handler(request):
        raise httpx.ConnectError("no route")

    client = _make_client(handler)
    with pytest.raises(ConceptioError):
        client.search("query")


def test_search_sends_license_header():
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"total": 0, "results": []}, request=request)

    client = _make_client(handler, license_key="CONCEPTIO-TEST-1234")
    client.search("hello")
    assert seen["headers"].get("x-license-key") == "CONCEPTIO-TEST-1234"


def test_search_sends_api_key_header():
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"total": 0, "results": []}, request=request)

    client = _make_client(handler, api_key="ckey_live_abcdef0123456789abcdef0123456789")
    client.search("hello")
    assert seen["headers"].get("x-api-key") == "ckey_live_abcdef0123456789abcdef0123456789"
    assert "x-license-key" not in seen["headers"]


def test_noarg_constructor_reads_api_key_from_config(_isolate_config, monkeypatch):
    """The MCP path constructs ConceptioClient() with no args — it must pick
    up an api_key stored in config (conceptio auth ckey_...) automatically,
    or MCP tools silently run unauthenticated."""
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"total": 0, "results": []}, request=request)

    cfg = {
        "api_base": "https://conceptio.test",
        "license_key": "",
        "api_key": "ckey_live_abcdef0123456789abcdef0123456789",
        "default_limit": 10,
        "default_citation_format": "bibtex",
    }
    monkeypatch.setattr(client_mod, "load_config", lambda: cfg.copy())
    _CURRENT_HANDLER["fn"] = handler
    client = ConceptioClient()
    client.search("hello")
    assert seen["headers"].get("x-api-key") == "ckey_live_abcdef0123456789abcdef0123456789"
    assert "x-license-key" not in seen["headers"]


def test_bearer_token_wins_over_machine_keys():
    """A signed-in human session (bearer) is exactly-one-credential — it
    wins over any machine key, and no key header is ever sent alongside it."""
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"total": 0, "results": []}, request=request)

    _CURRENT_HANDLER["fn"] = handler
    client = ConceptioClient(
        api_key="ckey_live_abcdef0123456789abcdef0123456789",
        license_key="CONCEPTIO-TEST-1234",
        bearer_token="idtoken-abc",
    )
    client.search("hello")
    assert seen["headers"].get("authorization") == "Bearer idtoken-abc"
    assert "x-api-key" not in seen["headers"]
    assert "x-license-key" not in seen["headers"]


def test_bearer_token_resolves_from_env(monkeypatch):
    for name in ("CONCEPTIO_API_KEY", "CONCEPTIO_LICENSE_KEY", "CONCEPTIO_BEARER_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONCEPTIO_BEARER_TOKEN", "idtoken-env")
    client = ConceptioClient()
    assert client.bearer_token == "idtoken-env"
    assert client.api_key == ""


def test_proof_fetches_bundle():
    bundle = {
        "doc_id": 42,
        "content_hash": "ab" * 32,
        "source": "nist",
        "license": "Open Access",
        "retrieved_at": "2026-09-10T00:00:00Z",
    }
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json=bundle, request=request)

    client = _make_client(handler, api_key="ckey_live_abcdef0123456789abcdef0123456789")
    data = client.get_proof(42)
    assert seen["path"] == "/api/document/42/proof"
    assert data["content_hash"] == "ab" * 32


def test_proof_passes_passage_query():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"doc_id": 42, "snippet": "matched"}, request=request)

    client = _make_client(handler, api_key="ckey_live_abcdef0123456789abcdef0123456789")
    client.get_proof(42, query="quantum")
    assert seen["params"].get("q") == "quantum"


def test_proof_raises_on_error_dict():
    detail = "Your free API key cannot use programmatic endpoints"
    client = _make_client(_json_handler({"detail": detail}, status=403))
    with pytest.raises(ConceptioError) as ei:
        client.get_proof(42)
    assert detail in str(ei.value)


def test_api_key_wins_over_license_key():
    """A client configured with both sends exactly one credential — the API
    key (belt-and-suspenders: set_api_key clears the license key, this pins
    the fallback for hand-edited configs)."""
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"total": 0, "results": []}, request=request)

    client = _make_client(handler, license_key="CONCEPTIO-TEST-1234", api_key="ckey_live_abcdef0123456789abcdef0123456789")
    client.search("hello")
    assert seen["headers"].get("x-api-key") == "ckey_live_abcdef0123456789abcdef0123456789"
    assert "x-license-key" not in seen["headers"]


# ── document / citation ───────────────────────────────────────────────────────
def test_submit_and_poll_search_job():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.content))
        if request.url.path.endswith("/jobs"):
            return httpx.Response(202, json={"id": "job12345678", "status": "queued"}, request=request)
        return httpx.Response(200, json={"id": "job12345678", "status": "done", "result": {"count": 1}}, request=request)

    client = _make_client(handler, api_key="ckey_live_abcdef0123456789abcdef0123456789")
    created = client.submit_search_job([{"q": "zero trust", "limit": 5}])
    assert created["status"] == "queued"
    assert client.get_search_job("job12345678")["status"] == "done"
    assert seen[0][0] == "POST"
    assert seen[1][1].endswith("/jobs/job12345678")
    assert b"ckey_live" not in seen[0][2]


def test_expired_search_job_is_a_friendly_error():
    def handler(request):
        return httpx.Response(410, json={"detail": "Search job expired"}, request=request)

    client = _make_client(handler, api_key="ckey_live_abcdef0123456789abcdef0123456789")
    with pytest.raises(ConceptioError, match="expired"):
        client.get_search_job("job12345678")


def test_tombstoned_document_uses_server_message():
    """A 410 on a document fetch carries the server's tombstone message
    ("document removed"), not the job-expired default."""
    def handler(request):
        return httpx.Response(410, json={
            "detail": {
                "error": "document_removed",
                "message": "This document was removed from the archive.",
                "tombstone": {"doc_id": 42, "reason": "pruned"},
            }
        }, request=request)

    client = _make_client(handler, api_key="ckey_live_abcdef0123456789abcdef0123456789")
    with pytest.raises(ConceptioError, match="removed from the archive"):
        client.get_document(42)


def test_submit_search_job_rejects_more_than_fifty():
    client = ConceptioClient(api_base="https://conceptio.test")
    with pytest.raises(ConceptioError, match="between 1 and 50"):
        client.submit_search_job([{"q": "x"}] * 51)


def test_batch_search_posts_sync_batch_payload():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"count": 1, "tier": "public", "queries": [{"total": 1, "results": []}]},
            request=request,
        )

    client = _make_client(handler, api_key="ckey_live_abcdef0123456789abcdef0123456789")
    data = client.batch_search([{"q": "zero trust", "limit": 5}])
    assert seen["path"] == "/api/search/batch"
    assert seen["payload"] == {"queries": [{"q": "zero trust", "limit": 5}]}
    assert data["count"] == 1
    assert data["tier"] == "public"


def test_batch_search_rejects_more_than_ten():
    client = ConceptioClient(api_base="https://conceptio.test")
    with pytest.raises(ConceptioError, match="between 1 and 10"):
        client.batch_search([{"q": "x"}] * 11)


def test_batch_search_rejects_non_list():
    client = ConceptioClient(api_base="https://conceptio.test")
    with pytest.raises(ConceptioError, match="between 1 and 10"):
        client.batch_search({"queries": [{"q": "x"}]})


def test_env_vars_resolve_credentials(monkeypatch):
    for name in ("CONCEPTIO_API_KEY", "CONCEPTIO_LICENSE_KEY", "CONCEPTIO_API_BASE", "CONCEPTIO_BEARER_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    client = ConceptioClient()
    assert client.api_key == ""
    assert client.license_key == ""
    assert client.bearer_token == ""
    # No env, no explicit arg → the config file's api_base (fixture-isolated).
    assert client.api_base == "https://conceptio.test"

    monkeypatch.setenv("CONCEPTIO_API_KEY", "ckey_live_env")
    monkeypatch.setenv("CONCEPTIO_LICENSE_KEY", "CONCEPTIO-ENV-KEY")
    monkeypatch.setenv("CONCEPTIO_API_BASE", "https://env.example")
    monkeypatch.setenv("CONCEPTIO_BEARER_TOKEN", "idtoken-env")
    client = ConceptioClient()
    assert client.api_key == "ckey_live_env"
    assert client.license_key == "CONCEPTIO-ENV-KEY"
    assert client.bearer_token == "idtoken-env"
    assert client.api_base == "https://env.example"


def test_get_search_job_rejects_untrusted_id():
    client = ConceptioClient(api_base="https://conceptio.test")
    with pytest.raises(ConceptioError, match="invalid format"):
        client.get_search_job("../../secrets")


def test_get_document_and_citation():
    client = _make_client(
        _json_handler({"id": 42, "title": "Paper", "direct_pdf_url": "https://x/a.pdf"})
    )
    doc = client.get_document(42)
    assert doc["id"] == 42

    client = _make_client(_json_handler({"citation": "@misc{...}"}))
    assert client.get_citation(42, format="bibtex") == "@misc{...}"


def test_get_citation_raises_on_error_dict(monkeypatch):
    # A 429/403 surfaces the server's own detail as an exception so machine
    # callers never swallow it into an empty string (the old behaviour left
    # `conceptio cite` printing a blank line on a rate-limited request).
    detail = "Rate limit: 1 request/second on the Dev tier"
    client = _make_client(_json_handler({"detail": detail}, status=429))
    with pytest.raises(ConceptioError) as ei:
        client.get_citation(42)
    assert detail in str(ei.value)


def test_get_document_raises_on_error_dict(monkeypatch):
    detail = "Your free API key cannot use programmatic endpoints"
    client = _make_client(_json_handler({"detail": detail}, status=403))
    with pytest.raises(ConceptioError) as ei:
        client.get_document(42)
    assert detail in str(ei.value)


# ── download resolution + streaming ───────────────────────────────────────────
def test_resolve_download_url_by_id():
    client = _make_client(_json_handler({"id": 7, "direct_pdf_url": "https://g.org/files/7/7-pdf.pdf"}))
    assert client.resolve_download_url("7") == "https://g.org/files/7/7-pdf.pdf"


def test_resolve_download_url_by_id_without_pdf_raises():
    client = _make_client(_json_handler({"id": 7}))
    with pytest.raises(ConceptioError):
        client.resolve_download_url("7")


def test_resolve_download_url_passthrough():
    client = _make_client(_json_handler({}))
    assert client.resolve_download_url("https://example.org/x.pdf") == "https://example.org/x.pdf"


def test_resolve_download_url_garbage_raises():
    client = _make_client(_json_handler({}))
    with pytest.raises(ConceptioError):
        client.resolve_download_url("not-an-id-not-a-url")


def test_download_pdf_does_not_send_credentials_to_external_host(tmp_path):
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, content=b"%PDF", request=request)

    client = _make_client(handler, api_key="ckey_live_secret0123456789abcdef")
    out = client.download_pdf("https://source.example/paper.pdf", str(tmp_path / "paper.pdf"))
    assert out == str(tmp_path / "paper.pdf")
    assert "x-api-key" not in seen["headers"]
    assert "x-license-key" not in seen["headers"]


def test_download_redirect_drops_credentials_on_cross_origin_redirect(tmp_path):
    seen = []

    def handler(request):
        seen.append((str(request.url), dict(request.headers)))
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "https://source.example/paper.pdf"}, request=request)
        return httpx.Response(200, content=b"%PDF", request=request)

    client = _make_client(handler, api_key="ckey_live_secret0123456789abcdef")
    out = client.download_pdf("https://conceptio.test/files/paper.pdf", str(tmp_path / "paper.pdf"))
    assert (tmp_path / "paper.pdf").read_bytes() == b"%PDF"
    assert "x-api-key" in seen[0][1]
    assert "x-api-key" not in seen[1][1]


def test_download_rejects_loopback_target(tmp_path):
    client = _make_client(lambda request: httpx.Response(200, content=b"%PDF", request=request))
    with pytest.raises(ConceptioError, match="local and private"):
        client.download_pdf("http://127.0.0.1:8000/secrets", str(tmp_path / "paper.pdf"))


def test_download_pdf_streams_to_disk(tmp_path):
    pdf_bytes = b"%PDF-1.4 fake content"

    def handler(request):
        return httpx.Response(200, content=pdf_bytes, request=request)

    client = _make_client(handler)
    out = tmp_path / "doc.pdf"
    result = client.download_pdf("https://example.org/doc.pdf", str(out))
    assert result == str(out)
    assert out.read_bytes() == pdf_bytes


def test_download_rejects_oversized_content_length(tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-length": str(client_mod._MAX_DOWNLOAD_BYTES + 1)},
            content=b"%PDF",
            request=request,
        )

    client = _make_client(handler)
    with pytest.raises(ConceptioError, match="100 MiB"):
        client.download_pdf("https://example.org/doc.pdf", str(tmp_path / "doc.pdf"))
    assert not (tmp_path / "doc.pdf").exists()


def test_api_client_rejects_cross_origin_redirect_without_retry():
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(302, headers={"Location": "https://attacker.example/api/me"}, request=request)

    client = _make_client(handler, api_key="ckey_live_secret0123456789abcdef")
    with pytest.raises(ConceptioError, match="cross-origin redirect"):
        client.quota()
    assert len(seen) == 1


def test_download_by_target_resolves_then_downloads(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        path = str(request.url.path)
        if path.startswith("/api/document/"):
            return httpx.Response(200, json={"id": 9, "direct_pdf_url": "https://example.org/y.pdf"}, request=request)
        calls.append(path)
        return httpx.Response(200, content=b"%PDF", request=request)

    client = _make_client(handler)
    out = tmp_path / "y.pdf"
    client.download_by_target("9", str(out))
    assert calls == ["/y.pdf"]
    assert out.read_bytes() == b"%PDF"


# ── auth/trial status handling (401 → auth hint, 403 → upgrade, 2026-09-03) ──
def test_401_maps_to_auth_hint_loudly():
    from conceptio_cli.config import AUTH_REQUIRED_HINT

    def handler(request):
        assert request.headers.get("user-agent", "").startswith("conceptio-cli/")
        return httpx.Response(401, json={"detail": "key required"}, request=request)

    client = _make_client(handler)
    with pytest.raises(ConceptioError) as ei:
        client.search("moby dick")
    assert "conceptio auth" in str(ei.value)
    assert str(ei.value) == AUTH_REQUIRED_HINT


def test_403_trial_exhausted_surfaces_server_detail():
    def handler(request):
        return httpx.Response(
            403, json={"detail": "Your free API key cannot use programmatic endpoints — Conceptio for agents requires the Dev plan (EUR 19.99/month, 3,500 credits/month)."},
            request=request,
        )

    client = _make_client(handler)
    with pytest.raises(ConceptioError) as ei:
        client.search("moby dick")
    assert "programmatic endpoints" in str(ei.value)
    assert "Dev plan" in str(ei.value)


def test_429_still_returns_error_dict():
    client = _make_client(_json_handler({"detail": "slow down"}, status=429))
    data = client.search("moby dick")
    assert "error" in data and data["results"] == []


def test_free_key_quota_reports_trial_remaining():
    """/api/me for a free-tier key carries the account's shared allowance; the
    client passes it through so `conceptio quota` can display it."""
    client = _make_client(_json_handler({
        "tier": "public", "email": "", "auth": "api_key", "trial_remaining": 33,
    }), api_key="ckey_live_1234567890abcdef")
    data = client.quota()
    assert data["auth"] == "api_key"
    assert data["tier"] == "public"
    assert data["trial_remaining"] == 33


def test_free_key_search_response_carries_remaining():
    client = _make_client(_json_handler({
        "query": "moby dick", "total": 1, "results": [], "tier": "public", "trial_remaining": 31,
    }))
    data = client.search("moby dick")
    assert data["trial_remaining"] == 31
