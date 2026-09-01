"""Offline tests for the MCP stdio server (JSON-RPC over fake stdin/stdout)."""

import io
import json

import pytest

import conceptio_cli.mcp_server as mcp_mod
from conceptio_cli.mcp_server import TOOLS, run_mcp_server


class FakeMCPClient:
    def __init__(self):
        self.license_key = ""

    def search(self, query, limit=10, category=None):
        return {"total": 1, "results": [{"id": 1, "title": "Paper", "direct_pdf_url": "https://x/p.pdf"}]}

    def get_document(self, doc_id):
        return {"id": doc_id, "title": "Paper"}

    def get_citation(self, doc_id, format="bibtex"):
        return "@misc{key}"

    def resolve(self, identifier, limit=10):
        return {"query": identifier, "identifier": "RFC 2119", "kind": "rfc", "total": 1,
                "results": [{"id": 304793, "title": "Key words for use in RFCs", "source": "ietf"}]}

    def download_by_target(self, target, out):
        return out


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(mcp_mod, "ConceptioClient", FakeMCPClient)


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


def test_tools_list_has_five_tools(fake_client):
    responses = _run([json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})], fake_client)
    names = [t["name"] for t in responses[0]["result"]["tools"]]
    assert names == ["conceptio_search", "conceptio_resolve", "conceptio_download_pdf", "conceptio_get_citation", "conceptio_get_document"]
    assert len(TOOLS) == 5


def test_tools_call_resolve(fake_client):
    req = {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
           "params": {"name": "conceptio_resolve", "arguments": {"id": "RFC 2119"}}}
    responses = _run([json.dumps(req)], fake_client)
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["kind"] == "rfc"
    assert payload["results"][0]["id"] == 304793


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


def test_tools_call_download(fake_client):
    req = {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
           "params": {"name": "conceptio_download_pdf", "arguments": {"doc_id_or_url": "1", "output_path": "w/p.pdf"}}}
    responses = _run([json.dumps(req)], fake_client)
    assert "w/p.pdf" in responses[0]["result"]["content"][0]["text"]


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
    class BrokenClient(FakeMCPClient):
        def search(self, *a, **kw):
            raise ValueError("bad args")

    monkeypatch.setattr(mcp_mod, "ConceptioClient", BrokenClient)
    req = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
           "params": {"name": "conceptio_search", "arguments": {"query": "x"}}}
    responses = _run([json.dumps(req)], monkeypatch)
    assert responses[0]["error"]["code"] == -32603
    assert "bad args" in responses[0]["error"]["message"]
