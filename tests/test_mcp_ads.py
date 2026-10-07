"""The disclosed sponsored layer — fail-open, offline, data-only.

Three invariants this file exists to hold:

1. **Fail-open is absolute.** No credentials, no SDK, no match, or an
   exploding ad client — the tool result comes back byte-for-byte unchanged,
   and without credentials the SDK client is never even constructed (zero
   network, zero calls).
2. **Ads are data, never a directive, never on an error.** The field lands
   inside the result payload as JSON with the SDK's immutable
   ``label: "Sponsored"``; ``isError`` results are never touched; an existing
   ``sponsored`` key is never overwritten.
3. **The terminal sees a card, not raw JSON only.** A clientInfo.name in the
   SDK's KNOWN_CLI_CLIENTS set gets the bordered text card as its own content
   item plus the delivery beacon; unknown clients get the plain field and no
   card (never a false positive that mis-formats a rich UI).
"""

import json

import pytest

import conceptio_cli.ads as ads_mod
import conceptio_cli.mcp_server as mcp_mod
from tests.test_mcp import FakeMCPClient, _run

SLOT = {"label": "Sponsored", "text": "Ship your MCP with Lulu.", "url": "https://ads.getlulu.dev/c/t"}


class FakeAdsClient:
    """Stand-in for `lulu_ads.LuluAds` — never touches the network."""

    def __init__(self, slot=SLOT, raise_on_slot=False):
        self.slot = slot
        self.raise_on_slot = raise_on_slot
        self.contexts = []

    def sponsored_slot_sync(self, context=None, timeout_ms=None, enabled=True):
        if self.raise_on_slot:
            raise RuntimeError("ads exploded")
        self.contexts.append(context)
        return self.slot


def _result(payload=None, text=None, is_error=False):
    if text is None:
        payload = {"results": []} if payload is None else payload
        text = json.dumps(payload, indent=2, ensure_ascii=True)
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


@pytest.fixture(autouse=True)
def _no_lulu_env(monkeypatch):
    """Deterministic regardless of the host's real publisher credentials."""
    monkeypatch.delenv("LULU_ADS_PUBLISHER_ID", raising=False)
    monkeypatch.delenv("LULU_ADS_API_KEY", raising=False)


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(mcp_mod, "ConceptioClient", FakeMCPClient)
    monkeypatch.setattr(mcp_mod, "load_config",
                        lambda: {"api_key": "ckey_live_testkey0123456789abcdef"})


# --- fail-open --------------------------------------------------------------


def test_missing_credentials_never_even_build_the_client(monkeypatch):
    """Absence of creds must not construct an SDK client — that is the
    zero-network half of fail-open, before any request shape exists."""
    class Boom:
        def __init__(self):
            raise AssertionError("an SDK client was built without credentials")

    monkeypatch.setattr(ads_mod, "LuluAds", Boom)
    result = _result({"results": [{"id": 1}]})
    out = ads_mod.attach(result, "conceptio_search", "claude-code")
    assert out is result
    assert "sponsored" not in json.loads(result["content"][0]["text"])
    assert len(result["content"]) == 1


def test_no_match_leaves_the_result_exactly_as_it_was(monkeypatch):
    fake = FakeAdsClient(slot=None)
    monkeypatch.setattr(ads_mod, "_get_client", lambda: fake)
    result = _result({"results": [{"id": 1}]})
    before = json.dumps(result, sort_keys=True)
    out = ads_mod.attach(result, "conceptio_search", "claude-code")
    assert json.dumps(out, sort_keys=True) == before


def test_an_exploding_ad_client_never_breaks_the_tool(monkeypatch):
    fake = FakeAdsClient(raise_on_slot=True)
    monkeypatch.setattr(ads_mod, "_get_client", lambda: fake)
    result = _result({"results": [{"id": 1}]})
    before = json.dumps(result, sort_keys=True)
    out = ads_mod.attach(result, "conceptio_search", None)
    assert json.dumps(out, sort_keys=True) == before


def test_garbage_in_garbage_out_is_still_never_an_exception(monkeypatch):
    monkeypatch.setattr(ads_mod, "_get_client", lambda: FakeAdsClient())
    assert ads_mod.attach("not a result", "conceptio_search") == "not a result"
    assert ads_mod.attach(None, "conceptio_search") is None
    weird = {"content": "not a list"}
    assert ads_mod.attach(weird, "conceptio_search") == weird



# --- the data field ---------------------------------------------------------


def test_sponsored_rides_the_payload_as_data_with_the_immutable_label(monkeypatch):
    fake = FakeAdsClient()
    monkeypatch.setattr(ads_mod, "_get_client", lambda: fake)
    result = _result({"results": [{"id": 1, "title": "Paper"}]})
    ads_mod.attach(result, "conceptio_search", "claude-desktop")
    payload = json.loads(result["content"][0]["text"])
    assert payload["sponsored"] == SLOT  # passed through untouched
    assert payload["sponsored"]["label"] == "Sponsored"
    assert payload["results"] == [{"id": 1, "title": "Paper"}]  # tool data intact
    assert len(result["content"]) == 1  # rich host: field only, no card
    assert fake.contexts[-1] == {"tool": "conceptio_search", "client": "claude-desktop"}


def test_error_results_are_never_touched(monkeypatch):
    """The red line: no ads on error paths — the fetch must not even happen."""
    fake = FakeAdsClient()
    monkeypatch.setattr(ads_mod, "_get_client", lambda: fake)
    result = _result({"error": "quota exceeded", "results": []}, is_error=True)
    out = ads_mod.attach(result, "conceptio_search", "claude-code")
    assert out is result
    assert "sponsored" not in out["content"][0]["text"]
    assert len(out["content"]) == 1
    assert fake.contexts == []


def test_an_existing_sponsored_key_is_never_overwritten(monkeypatch):
    fake = FakeAdsClient()
    monkeypatch.setattr(ads_mod, "_get_client", lambda: fake)
    existing = {"label": "Sponsored", "text": "already here", "url": "https://x/1"}
    result = _result({"results": [], "sponsored": existing})
    ads_mod.attach(result, "conceptio_search", None)
    payload = json.loads(result["content"][0]["text"])
    assert payload["sponsored"] == existing


def test_plain_text_results_gain_no_data_slot(monkeypatch):
    """download/citation answers are prose, not JSON — there is no data slot
    to inject into, and prose must never be rewritten to make one."""
    fake = FakeAdsClient()
    monkeypatch.setattr(ads_mod, "_get_client", lambda: fake)
    result = _result(text="Successfully downloaded PDF to: /tmp/x.pdf")
    out = ads_mod.attach(result, "conceptio_download_pdf", "claude-desktop")
    assert out["content"][0]["text"] == "Successfully downloaded PDF to: /tmp/x.pdf"
    assert len(out["content"]) == 1



# --- the terminal card ------------------------------------------------------


def test_known_cli_client_gets_the_bordered_card_and_the_beacon(monkeypatch):
    beacons = []
    monkeypatch.setattr(ads_mod, "_fire_beacon", lambda client, url: beacons.append(url))
    fake = FakeAdsClient(slot={**SLOT, "imp_url": "https://ads.getlulu.dev/i/abc"})
    monkeypatch.setattr(ads_mod, "_get_client", lambda: fake)
    result = _result(text="@misc{key}")
    ads_mod.attach(result, "conceptio_get_citation", "claude-code")
    assert result["content"][0]["text"] == "@misc{key}"  # tool output untouched
    assert len(result["content"]) == 2
    card = result["content"][1]["text"]
    assert "Sponsored" in card and "via Lulu Ads" in card
    assert beacons == ["https://ads.getlulu.dev/i/abc"]


def test_unknown_client_gets_neither_card_nor_rewrite(monkeypatch):
    fake = FakeAdsClient()
    monkeypatch.setattr(ads_mod, "_get_client", lambda: fake)
    result = _result(text="@misc{key}")
    out = ads_mod.attach(result, "conceptio_get_citation", "mystery-host")
    assert len(out["content"]) == 1
    assert out["content"][0]["text"] == "@misc{key}"


# --- wired into the stdio loop ----------------------------------------------


def _line(payload):
    return json.dumps(payload)


def test_legacy_handshake_clientinfo_reaches_the_layer(fake_client, monkeypatch):
    """initialize carries clientInfo once; the tools/call answer must still
    get the card — the handshake name is the legacy path's fallback."""
    fake = FakeAdsClient()
    monkeypatch.setattr(ads_mod, "_get_client", lambda: fake)
    responses = _run([
        _line({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2024-11-05",
                          "clientInfo": {"name": "claude-code", "version": "1"}}}),
        _line({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "conceptio_get_document", "arguments": {"doc_id": 1}}}),
    ], fake_client)
    result = responses[1]["result"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["sponsored"]["label"] == "Sponsored"
    assert "via Lulu Ads" in result["content"][1]["text"]



def test_modern_meta_clientinfo_reaches_the_layer(fake_client, monkeypatch):
    """The modern era has no handshake — clientInfo rides the per-request
    `_meta`, and it must be read there or every modern host loses the card."""
    fake = FakeAdsClient()
    monkeypatch.setattr(ads_mod, "_get_client", lambda: fake)
    from conceptio_cli.mcp_server import META_CLIENT_INFO, META_PROTOCOL_VERSION
    responses = _run([
        _line({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "conceptio_get_document", "arguments": {"doc_id": 1},
                          "_meta": {META_PROTOCOL_VERSION: "2026-07-28",
                                    META_CLIENT_INFO: {"name": "claude-code"}}}}),
    ], fake_client)
    result = responses[0]["result"]
    assert result.get("resultType") == "complete"  # attach runs before modernize
    assert "via Lulu Ads" in result["content"][1]["text"]


def test_without_credentials_the_loop_serves_pristine_results(fake_client, monkeypatch):
    """End-to-end fail-open: real `_get_client`, no env, full loop."""
    class Boom:
        def __init__(self):
            raise AssertionError("an SDK client was built without credentials")

    monkeypatch.setattr(ads_mod, "LuluAds", Boom)
    responses = _run([
        _line({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "conceptio_get_document", "arguments": {"doc_id": 1}}}),
    ], fake_client)
    result = responses[0]["result"]
    assert len(result["content"]) == 1
    payload = json.loads(result["content"][0]["text"])
    assert "sponsored" not in payload
    assert payload == {"id": 1, "title": "Paper"}


def test_error_answers_from_the_loop_carry_no_ad(fake_client, monkeypatch):
    """Credential-gate refusals (isError) must bypass the ads layer."""
    fake = FakeAdsClient()
    monkeypatch.setattr(ads_mod, "_get_client", lambda: fake)
    monkeypatch.setattr(mcp_mod, "load_config", lambda: {})
    responses = _run([
        _line({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "conceptio_search", "arguments": {"query": "x"}}}),
    ], fake_client)
    result = responses[0]["result"]
    assert result["isError"] is True
    assert len(result["content"]) == 1
    assert "sponsored" not in result["content"][0]["text"]
    assert fake.contexts == []

