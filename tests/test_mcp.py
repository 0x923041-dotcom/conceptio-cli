"""Offline tests for the MCP stdio server (JSON-RPC over fake stdin/stdout)."""

import io
import json
from pathlib import Path

import pytest

import conceptio_cli.mcp_server as mcp_mod
from conceptio_cli.client import ConceptioError
from conceptio_cli.mcp_server import TOOLS, _workspace_output_path, run_mcp_server


class FakeMCPClient:
    def __init__(self):
        self.license_key = ""

    def search(self, query, limit=10, category=None):
        return {"total": 1, "results": [{"id": 1, "title": "Paper", "direct_pdf_url": "https://x/p.pdf"}], "attribution": {"text": "Provided by Conceptio.", "url": "https://www.conceptio.app"}}

    def submit_search_job(self, queries):
        return {"id": "job12345678", "status": "queued", "poll_url": "/api/search/jobs/job12345678"}

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


def test_tools_call_search(fake_client):
    req = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
           "params": {"name": "conceptio_search", "arguments": {"query": "attention", "limit": 5}}}
    responses = _run([json.dumps(req)], fake_client)
    result = responses[0]["result"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["results"][0]["title"] == "Paper"


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


def test_tools_call_keyless_is_auth_error(fake_client, monkeypatch):
    """Without a stored credential every tool call fails closed with setup
    guidance — while initialize/ping still answer (handshake must succeed)."""
    monkeypatch.setattr(mcp_mod, "load_config", lambda: {})
    req = {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
           "params": {"name": "conceptio_search", "arguments": {"query": "x"}}}
    responses = _run([json.dumps(req)], fake_client)
    result = responses[0]["result"]
    assert result["isError"] is True
    assert "Authentication required" in result["content"][0]["text"]
    assert "conceptio auth" in result["content"][0]["text"]


def test_initialize_keyless_still_answers(fake_client, monkeypatch):
    monkeypatch.setattr(mcp_mod, "load_config", lambda: {})
    responses = _run([json.dumps({"jsonrpc": "2.0", "id": 12, "method": "initialize"})],
                     fake_client)
    assert responses[0]["result"]["serverInfo"]["name"] == "conceptio-mcp"
