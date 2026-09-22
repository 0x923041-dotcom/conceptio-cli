"""Offline tests for the MCP stdio server (JSON-RPC over fake stdin/stdout)."""

import io
import json
from pathlib import Path

import pytest

import conceptio_cli.mcp_server as mcp_mod
from conceptio_cli.client import ConceptioError
from conceptio_cli.mcp_server import (
    CACHE_SCOPE,
    CACHE_TTL_MS,
    CURRENT_PROTOCOL_VERSION,
    META_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOOLS,
    UNSUPPORTED_PROTOCOL_VERSION_CODE,
    _workspace_output_path,
    run_mcp_server,
)


class FakeMCPClient:
    def __init__(self):
        self.license_key = ""

    def search(self, query, limit=10, category=None, license=None):
        return {"total": 1, "results": [{"id": 1, "title": "Paper", "direct_pdf_url": "https://x/p.pdf"}], "attribution": {"text": "Provided by Conceptio.", "url": "https://www.conceptio.app"}}

    def submit_search_job(self, queries):
        return {"id": "job12345678", "status": "queued", "poll_url": "/api/search/jobs/job12345678"}

    def batch_search(self, queries):
        return {"count": len(queries), "tier": "public",
                "queries": [{"query": q.get("q"), "total": 1, "results": []} for q in queries]}

    def get_document(self, doc_id):
        return {"id": doc_id, "title": "Paper"}

    def get_citation(self, doc_id, format="bibtex"):
        return "@misc{key}"

    def resolve(self, identifier, limit=10):
        return {"query": identifier, "identifier": "RFC 2119", "kind": "rfc", "total": 1,
                "results": [{"id": 304793, "title": "Key words for use in RFCs", "source": "ietf"}]}

    def download_by_target(self, target, out):
        return out

    def send_zotero(self, doc_id):
        return {"ok": True, "connector": "zotero", "doc_id": doc_id, "idempotent": False}

    def send_zotero_all(self, doc_ids):
        return {"ok": True, "connector": "zotero", "total": len(doc_ids), "results": []}

    def authorize_obsidian(self, doc_id):
        return {"doc_id": doc_id, "title": "Paper", "canonical_url": "https://www.conceptio.app/document/1/paper"}

    def send_connector(self, connector, doc_id, vault=""):
        return {"connector": connector, "doc_id": doc_id, "vault": vault}


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(mcp_mod, "ConceptioClient", FakeMCPClient)
    # Deterministic credentials regardless of the real ~/.conceptio file.
    monkeypatch.setattr(mcp_mod, "load_config",
                        lambda: {"api_key": "ckey_live_testkey0123456789abcdef"})


def _run(payload_lines, fake_client):
    fake_client
    stdin = io.StringIO("\n".join(payload_lines) + "\n")
    stdout = io.StringIO()
    import sys as _sys

    old_in, old_out = _sys.stdin, _sys.stdout
    _sys.stdin, _sys.stdout = stdin, stdout
    try:
        run_mcp_server()
    finally:
        _sys.stdin, _sys.stdout = old_in, old_out
    return [json.loads(line) for line in stdout.getvalue().strip().splitlines() if line.strip()]


def test_initialize_returns_server_info(fake_client):
    responses = _run([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2024-11-05"}})], fake_client)
    assert len(responses) == 1
    res = responses[0]
    assert res["id"] == 1
    assert res["result"]["serverInfo"]["name"] == "conceptio-mcp"
    assert res["result"]["protocolVersion"] == "2024-11-05"


def test_initialize_echoes_a_handshake_revision_it_supports(fake_client):
    """The handshake's version rule: a version inside the supported band is
    answered with the SAME version (2025-11-25 Lifecycle — "If the server
    supports the requested protocol version, it MUST respond with the same
    version"). Pinned at the newest handshake revision, so the band cannot
    quietly shrink to the single value we happen to declare."""
    for version in ("2025-03-26", "2025-06-18", "2025-11-25"):
        responses = _run([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": version}})], fake_client)
        assert responses[0]["result"]["protocolVersion"] == version


def test_initialize_never_echoes_a_modern_revision(fake_client):
    """Until 2026-09-22 this echoed whatever was asked for, so a client naming a
    modern revision was TOLD it was speaking that revision and then served
    legacy semantics — the mislabel with no way to detect it. The answer must be
    a version we support; `UnsupportedProtocolVersionError` (`-32022`) is the
    modern contract and would be a louder mislabel here, because a handshake-era
    client has no fall-forward mechanism."""
    for version in ("2026-07-28", "2027-01-01"):
        responses = _run([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": version}})], fake_client)
        result = responses[0]["result"]
        assert result["protocolVersion"] == PROTOCOL_VERSION
        assert result["protocolVersion"] != version
        assert "error" not in responses[0], "a legacy client cannot fall forward"


def test_initialize_answers_a_version_when_none_was_asked_for(fake_client):
    responses = _run([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})],
                     fake_client)
    assert responses[0]["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_tools_list_has_eight_tools(fake_client):
    responses = _run([json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})], fake_client)
    names = [t["name"] for t in responses[0]["result"]["tools"]]
    assert names == [
        "conceptio_search", "conceptio_resolve", "conceptio_download_pdf",
        "conceptio_get_citation", "conceptio_search_batch",
        "conceptio_connectors_send", "conceptio_connectors_send_all",
        "conceptio_get_document",
    ]
    assert len(TOOLS) == 8


def test_tools_call_resolve(fake_client):
    req = {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
           "params": {"name": "conceptio_resolve", "arguments": {"id": "RFC 2119"}}}
    responses = _run([json.dumps(req)], fake_client)
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["kind"] == "rfc"
    assert payload["results"][0]["id"] == 304793


def test_tools_call_search_batch(fake_client):
    req = {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
           "params": {"name": "conceptio_search_batch", "arguments": {"queries": [{"q": "attention"}]}}}
    responses = _run([json.dumps(req)], fake_client)
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["status"] == "queued"
    assert payload["id"] == "job12345678"


def test_tools_call_search_batch_sync(fake_client):
    req = {"jsonrpc": "2.0", "id": 18, "method": "tools/call",
           "params": {"name": "conceptio_search_batch",
                      "arguments": {"queries": [{"q": "attention"}, {"q": "transformers"}], "sync": True}}}
    responses = _run([json.dumps(req)], fake_client)
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["count"] == 2
    assert payload["queries"][0]["query"] == "attention"


def test_tools_call_search_batch_sync_rejects_over_ten(fake_client):
    req = {"jsonrpc": "2.0", "id": 19, "method": "tools/call",
           "params": {"name": "conceptio_search_batch",
                      "arguments": {"queries": [{"q": "x"}] * 11, "sync": True}}}
    responses = _run([json.dumps(req)], fake_client)
    # Tool validation errors surface as JSON-RPC errors (matching
    # test_tool_error_returns_internal_error), never as unbounded polling.
    assert responses[0]["error"]["code"] == -32603
    assert "at most 10" in responses[0]["error"]["message"]


def test_tools_call_connector_send(fake_client):
    req = {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
           "params": {"name": "conceptio_connectors_send", "arguments": {"connector": "zotero", "doc_id": 7}}}
    responses = _run([json.dumps(req)], fake_client)
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["connector"] == "zotero"
    assert payload["doc_id"] == 7


def test_tools_call_connector_bulk(fake_client):
    req = {"jsonrpc": "2.0", "id": 15, "method": "tools/call",
           "params": {"name": "conceptio_connectors_send_all", "arguments": {"doc_ids": [1, 2]}}}
    responses = _run([json.dumps(req)], fake_client)
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["total"] == 2


def test_tools_call_search_preserves_attribution(fake_client):
    req = {"jsonrpc": "2.0", "id": 16, "method": "tools/call",
           "params": {"name": "conceptio_search", "arguments": {"query": "attention", "limit": 5}}}
    responses = _run([json.dumps(req)], fake_client)
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["attribution"]["url"] == "https://www.conceptio.app"


def test_tools_call_search_accepts_license_filter(fake_client):
    req = {"jsonrpc": "2.0", "id": 17, "method": "tools/call",
           "params": {"name": "conceptio_search", "arguments": {"query": "attention", "license": "commercial-ok"}}}
    responses = _run([json.dumps(req)], fake_client)
    assert responses[0]["result"]["content"]


def test_tools_call_citation(fake_client):
    req = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
           "params": {"name": "conceptio_get_citation", "arguments": {"doc_id": 1, "format": "bibtex"}}}
    responses = _run([json.dumps(req)], fake_client)
    assert responses[0]["result"]["content"][0]["text"] == "@misc{key}"


def test_workspace_output_path_stays_below_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    resolved = Path(_workspace_output_path("workspace/p.pdf"))
    assert str(resolved).startswith(str(tmp_path.resolve()))
    with pytest.raises(ConceptioError, match="inside the MCP workspace"):
        _workspace_output_path("../outside.pdf")
    with pytest.raises(ConceptioError, match="inside the MCP workspace"):
        _workspace_output_path(str(tmp_path.parent / "outside.pdf"))


def test_tools_call_download(fake_client):
    req = {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
           "params": {"name": "conceptio_download_pdf", "arguments": {"doc_id_or_url": "1", "output_path": "w/p.pdf"}}}
    responses = _run([json.dumps(req)], fake_client)
    assert responses[0]["result"]["content"][0]["text"].endswith("w\\p.pdf") or responses[0]["result"]["content"][0]["text"].endswith("w/p.pdf")


def test_tools_call_unknown_tool(fake_client):
    req = {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
           "params": {"name": "nope", "arguments": {}}}
    responses = _run([json.dumps(req)], fake_client)
    assert responses[0]["result"]["isError"] is True


def test_ping(fake_client):
    responses = _run([json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"})], fake_client)
    assert responses[0]["result"] == {}


def test_notifications_get_no_response(fake_client):
    responses = _run([json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})], fake_client)
    assert responses == []


def test_unknown_method_returns_error(fake_client):
    responses = _run([json.dumps({"jsonrpc": "2.0", "id": 8, "method": "bogus"})], fake_client)
    assert responses[0]["error"]["code"] == -32601


def test_parse_error(fake_client):
    responses = _run(["this is not json"], fake_client)
    assert responses[0]["error"]["code"] == -32700


def test_tool_error_returns_internal_error(monkeypatch):
    monkeypatch.setattr(mcp_mod, "load_config",
                        lambda: {"api_key": "ckey_live_testkey0123456789abcdef"})
    class BrokenClient(FakeMCPClient):
        def search(self, *a, **kw):
            raise ValueError("bad args")

    monkeypatch.setattr(mcp_mod, "ConceptioClient", BrokenClient)
    req = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
           "params": {"name": "conceptio_search", "arguments": {"query": "x"}}}
    responses = _run([json.dumps(req)], monkeypatch)
    assert responses[0]["error"]["code"] == -32603
    assert "bad args" in responses[0]["error"]["message"]


def _no_credential_anywhere(monkeypatch):
    """Keyless means keyless: an empty config file AND no env credential.

    The environment matters because the server now asks the same question the
    CLI's entry gate asks, and that one has always honoured `CONCEPTIO_API_KEY`.
    """
    monkeypatch.setattr(mcp_mod, "load_config", lambda: {})
    for name in ("CONCEPTIO_API_KEY", "CONCEPTIO_LICENSE_KEY", "CONCEPTIO_BEARER_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_tools_call_keyless_is_auth_error(fake_client, monkeypatch):
    """Without a credential anywhere, every tool call fails closed with setup
    guidance — while initialize/ping still answer (handshake must succeed)."""
    _no_credential_anywhere(monkeypatch)
    req = {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
           "params": {"name": "conceptio_search", "arguments": {"query": "x"}}}
    responses = _run([json.dumps(req)], fake_client)
    result = responses[0]["result"]
    assert result["isError"] is True
    assert "Authentication required" in result["content"][0]["text"]
    assert "conceptio auth" in result["content"][0]["text"]


def test_tools_call_honors_an_env_credential(fake_client, monkeypatch):
    """A host that supplies the key via the environment must be able to work.

    This is the documented host-process path — an editor or MCP client passes
    `CONCEPTIO_API_KEY` instead of writing `~/.conceptio/config.json`. The CLI's
    entry gate accepted it and started the server, and then every tool call was
    refused, because this check read only the config file. Entry and per-call
    now share one predicate (`config.has_credential`).
    """
    monkeypatch.setattr(mcp_mod, "load_config", lambda: {})
    monkeypatch.setenv("CONCEPTIO_API_KEY", "ckey_live_envkey0123456789abcdef")
    req = {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
           "params": {"name": "conceptio_search", "arguments": {"query": "x"}}}
    responses = _run([json.dumps(req)], fake_client)
    result = responses[0]["result"]
    assert "isError" not in result, result
    assert "Paper" in result["content"][0]["text"]


def test_tools_call_honors_an_env_bearer_token(fake_client, monkeypatch):
    """Same rule for the signed-in human session a host passes per run."""
    monkeypatch.setattr(mcp_mod, "load_config", lambda: {})
    monkeypatch.setenv("CONCEPTIO_BEARER_TOKEN", "eyJhbGciOiJub25lIn0.payload.sig")
    req = {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
           "params": {"name": "conceptio_get_document", "arguments": {"doc_id": 1}}}
    responses = _run([json.dumps(req)], fake_client)
    assert "isError" not in responses[0]["result"]


def test_initialize_keyless_still_answers(fake_client, monkeypatch):
    _no_credential_anywhere(monkeypatch)
    responses = _run([json.dumps({"jsonrpc": "2.0", "id": 12, "method": "initialize"})],
                     fake_client)
    assert responses[0]["result"]["serverInfo"]["name"] == "conceptio-mcp"


def test_a_soft_error_payload_is_marked_as_a_tool_error(fake_client, monkeypatch):
    """The client answers what it cannot serve with a payload, not by raising.

    `search("")` returns `{"error": "Empty search query.", "results": []}`, and a
    429 returns its detail the same way, because the CLI prints that payload for
    a human. Wrapped in an ordinary tool result, every MCP host rendered the
    failure as a success — measured live 2026-09-21: a `conceptio_search` call
    with no arguments came back with `isError` absent, so the only signal was a
    key inside the text. The payload still carries the message; the flag is what
    a host acts on.
    """

    class SoftErrorClient(FakeMCPClient):
        def search(self, query, limit=10, category=None, license=None):
            return {"error": "Empty search query.", "results": []}

    monkeypatch.setattr(mcp_mod, "ConceptioClient", SoftErrorClient)
    req = {"jsonrpc": "2.0", "id": 30, "method": "tools/call",
           "params": {"name": "conceptio_search", "arguments": {"query": ""}}}
    responses = _run([json.dumps(req)], fake_client)
    result = responses[0]["result"]
    assert result.get("isError") is True, "a soft error was returned as a successful tool call"
    assert "Empty search query." in result["content"][0]["text"]


def test_a_normal_payload_is_not_marked_as_an_error(fake_client):
    req = {"jsonrpc": "2.0", "id": 31, "method": "tools/call",
           "params": {"name": "conceptio_search", "arguments": {"query": "attention"}}}
    responses = _run([json.dumps(req)], fake_client)
    result = responses[0]["result"]
    assert "isError" not in result, "a successful tool call was flagged as an error"


# ── the modern era (2026-07-28) ───────────────────────────────────────────────
# The gate this closes: a **modern-only** client against our legacy server fails
# non-deterministically — the spec lists *silence* as an outcome. `server/discover`
# is the stdio probe that lets a dual-era client find our era instead of guessing,
# and it is a MUST in the current revision. Eras are selected per request: `_meta`
# means modern, an `initialize` handshake means legacy, and both are served here.


def _modern(method, req_id, params=None, version=CURRENT_PROTOCOL_VERSION):
    """A modern request — version and identity as per-request `_meta`."""
    params = dict(params or {})
    params["_meta"] = {
        META_PROTOCOL_VERSION: version,
        "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})


def test_server_discover_advertises_both_eras(fake_client):
    responses = _run([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})],
                     fake_client)
    result = responses[0]["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == list(SUPPORTED_PROTOCOL_VERSIONS)
    assert result["supportedVersions"][0] == CURRENT_PROTOCOL_VERSION, (
        "the newest supported version must lead, or a client that picks the first entry lands on a legacy revision"
    )
    assert PROTOCOL_VERSION in result["supportedVersions"]
    assert "tools" in result["capabilities"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "conceptio-mcp"
    assert result["ttlMs"] == CACHE_TTL_MS
    assert result["cacheScope"] == CACHE_SCOPE
    assert result.get("instructions")


def test_server_discover_answers_a_bare_probe(fake_client):
    """The probe may arrive with no `_meta` at all — the client does not yet know
    our era. Unanswered, the client reads us as legacy and never speaks modern."""
    responses = _run([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover",
                                  "params": {}})], fake_client)
    assert responses[0]["result"]["resultType"] == "complete"


def test_modern_tools_list_carries_result_type_and_cache_hints(fake_client):
    responses = _run([_modern("tools/list", 2)], fake_client)
    result = responses[0]["result"]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == CACHE_TTL_MS and result["cacheScope"] == CACHE_SCOPE
    assert len(result["tools"]) == 8


def test_modern_tools_call_carries_result_type_and_server_info(fake_client):
    responses = _run([_modern("tools/call", 10,
                              {"name": "conceptio_resolve", "arguments": {"id": "RFC 2119"}})],
                     fake_client)
    result = responses[0]["result"]
    assert result["resultType"] == "complete"
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "conceptio-mcp"
    assert json.loads(result["content"][0]["text"])["kind"] == "rfc"


def test_an_unsupported_modern_version_is_32022(fake_client):
    """The modern contract: list the versions we DO support so the client retries.
    `-32022` is the *modern* reply and is deliberately NOT what `initialize`
    returns — a handshake-era client has no fall-forward mechanism."""
    responses = _run([_modern("tools/list", 4, version="1900-01-01")], fake_client)
    err = responses[0]["error"]
    assert err["code"] == UNSUPPORTED_PROTOCOL_VERSION_CODE
    assert err["data"]["requested"] == "1900-01-01"
    assert err["data"]["supported"] == list(SUPPORTED_PROTOCOL_VERSIONS)
    assert "result" not in responses[0]


def test_a_legacy_version_sent_as_meta_is_still_modern_wire(fake_client):
    """A request carrying `_meta` is the modern mechanism by definition, so a
    handshake revision in that field is not a version we serve over it."""
    responses = _run([_modern("tools/list", 5, version="2025-11-25")], fake_client)
    assert responses[0]["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION_CODE


def test_the_legacy_path_is_unchanged(fake_client):
    """An `initialize` handshake, and any request without `_meta`, is still served
    legacy: no `resultType`, no cache hints. The handshake band predates both, and
    the installed base must not see a wire change."""
    responses = _run([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ], fake_client)
    assert responses[0]["result"]["protocolVersion"] == "2024-11-05"
    assert set(responses[1]["result"]) == {"tools"}, (
        "a legacy result grew a modern field — %s" % sorted(responses[1]["result"])
    )


def test_a_modern_initialize_does_not_shadow_discovery(fake_client):
    """`initialize` on the modern path is not how a modern client opens; it must
    still answer a handshake client without disturbing `server/discover`."""
    responses = _run([_modern("initialize", 6, {"protocolVersion": CURRENT_PROTOCOL_VERSION})],
                     fake_client)
    # A modern `initialize` is era-ambiguous; it is answered by the legacy rule
    # (a version we support), never echoed back as the modern revision.
    assert responses[0]["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_ping_survives_only_on_the_legacy_path(fake_client):
    """`ping` was removed from the core in 2026-07-28; it stays for the legacy
    band, where clients use it as a keepalive."""
    legacy = _run([json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"})], fake_client)
    assert legacy[0]["result"] == {}
    modern = _run([_modern("ping", 8)], fake_client)
    assert modern[0]["error"]["code"] == -32601


def test_the_published_manifest_declares_the_modern_era():
    """A published claim is a test target. `/mcp.json` told every reader
    `2024-11-05` while the code spoke it — the manifest is the only protocol
    statement a client sees before it connects, so it must name the era we
    actually serve. Absent checkout is a skip, not a failure."""
    stack = Path(__file__).resolve().parent.parent.parent
    manifest_path = stack / "Conceptio" / "frontend" / "public" / "mcp.json"
    if not manifest_path.exists():
        pytest.skip("the Conceptio checkout is not present in this workspace")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    transport = manifest.get("transport") or {}
    assert transport.get("protocol") == CURRENT_PROTOCOL_VERSION, (
        "mcp.json declares protocol %r; the server speaks %s"
        % (transport.get("protocol"), CURRENT_PROTOCOL_VERSION)
    )
    assert transport.get("protocols") == list(SUPPORTED_PROTOCOL_VERSIONS), (
        "mcp.json must advertise the whole supported set, not just the newest"
    )
