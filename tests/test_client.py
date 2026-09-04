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
def test_default_api_base_is_conceptio():
    # The published API base is conceptio.app (rebrand shipped 2026-09-01);
    # the old conceptio-iota.vercel.app host only exists as a 301.
    from conceptio_cli.config import DEFAULT_API_BASE, DEFAULT_CONFIG

    # Machine clients must target www — the apex host 308-redirects to it.
    assert DEFAULT_API_BASE == "https://www.conceptio.app"
    assert DEFAULT_CONFIG["api_base"] == "https://www.conceptio.app"


def test_no_legacy_domain_in_public_strings():
    # No user-facing string should resurrect the pre-rebrand host.
    from conceptio_cli.config import DEFAULT_API_BASE

    assert "conceptio-iota" not in DEFAULT_API_BASE
    assert "conceptio-iota" not in client_mod.UPGRADE_HINT
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
def test_search_sends_clean_params():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"total": 1, "results": []}, request=request)

    client = _make_client(handler)
    client.search("source:nist zero trust", limit=5)
    assert "/api/search" in seen["url"]
    assert seen["params"]["q"] == "zero trust"
    assert seen["params"]["sources"] == "nist"
    assert seen["params"]["limit"] == "5"


def test_search_429_returns_friendly_error():
    client = _make_client(_json_handler({}, status=429))
    data = client.search("anything")
    assert data["error"]
    assert "Upgrade to Pro" in data["error"]


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
def test_get_document_and_citation():
    client = _make_client(
        _json_handler({"id": 42, "title": "Paper", "direct_pdf_url": "https://x/a.pdf"})
    )
    doc = client.get_document(42)
    assert doc["id"] == 42

    client = _make_client(_json_handler({"citation": "@misc{...}"}))
    assert client.get_citation(42, format="bibtex") == "@misc{...}"


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


def test_download_pdf_streams_to_disk(tmp_path):
    pdf_bytes = b"%PDF-1.4 fake content"

    def handler(request):
        return httpx.Response(200, content=pdf_bytes, request=request)

    client = _make_client(handler)
    out = tmp_path / "doc.pdf"
    result = client.download_pdf("https://example.org/doc.pdf", str(out))
    assert result == str(out)
    assert out.read_bytes() == pdf_bytes


def test_download_by_target_resolves_then_downloads(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        path = str(request.url.path)
        if path.startswith("/api/document/"):
            return httpx.Response(200, json={"id": 9, "direct_pdf_url": "https://x/y.pdf"}, request=request)
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
            403, json={"detail": "Free trial exhausted (200 lifetime searches) — upgrade to Pro (2,000 searches/week) for higher limits."},
            request=request,
        )

    client = _make_client(handler)
    with pytest.raises(ConceptioError) as ei:
        client.search("moby dick")
    assert "exhausted" in str(ei.value)
    assert "upgrade to Pro" in str(ei.value)


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
