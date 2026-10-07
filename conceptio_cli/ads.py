"""Disclosed sponsored results on the MCP surface — Lulu Ads, fail-open.

One clearly labeled ``sponsored`` data object rides a successful tool result
when (and only when) a contextually matched ad exists. Policy: the web app
keeps the no-ads promise; the MCP server may serve ONE Sponsored result,
never ranked, never an instruction to the model — data only, the host model
keeps full judgment.

This module mirrors the guarantees the SDK's own ``LuluAdsMiddleware``
documents, by hand, because this server is a hand-rolled stdio loop rather
than a FastMCP instance:

- fail-open: no credentials, no SDK, no network, any exception — the result
  comes back byte-for-byte as the tool returned it (an ads failure may never
  delay or break the tool call it rides alongside);
- never on error paths (``isError`` results are untouched);
- never overwrites an already-present ``sponsored`` key;
- the ``label`` is the SDK's own (always the literal ``Sponsored``) — this
  layer never renames, hides or strips it;
- the field is structured data inside the result payload, never prompt text;
- terminal hosts (clientInfo in the SDK's KNOWN_CLI_CLIENTS set) additionally
  get the bordered plain-text card as a separate content item, plus the
  ``cli_card_delivered`` beacon when the slot carries an ``imp_url``.

Credentials come from the environment only (``LULU_ADS_PUBLISHER_ID`` +
``LULU_ADS_API_KEY``) — never code, never config. Absent either, the
integration is inert: zero network calls, zero fields. The SDK dependency is
marker-gated in ``pyproject.toml`` (``python_version >= "3.10"``) so the
package keeps its ``>=3.8`` floor with the same zero-extra-dep contract on
older interpreters, where this module's import guard leaves it inert too.
"""

import asyncio
import json
import os
import threading
from typing import Any, Dict, Optional

try:
    from lulu_ads import LuluAds
    from lulu_ads import cli_card as _cli_card
except Exception:  # pragma: no cover - SDK absent (py<3.10 install, no dep)
    LuluAds = None  # type: ignore[assignment]
    _cli_card = None  # type: ignore[assignment]

#: Process-wide client — constructed once so the SDK's connection keepalive
#: does its job across tool calls. Single-threaded stdio loop, no lock needed.
_client = None  # type: Optional[Any]


def _has_creds() -> bool:
    """Both halves, mirroring the SDK's own inert check (`LuluAds._is_inert`)."""
    return bool(os.environ.get("LULU_ADS_PUBLISHER_ID")) and bool(
        os.environ.get("LULU_ADS_API_KEY")
    )


def _get_client():
    """The shared SDK client, or None when this integration is inert."""
    global _client
    if LuluAds is None or not _has_creds():
        return None
    if _client is None:
        _client = LuluAds()
    return _client


def _fire_beacon(client, imp_url: str) -> None:
    """Fire-and-forget ``cli_card_delivered`` beacon (daemon thread).

    The SDK method is async; the stdio loop must never await an ad. Swallows
    everything — a dead beacon changes nothing about the response already on
    its way out.
    """

    def _run() -> None:
        try:
            asyncio.run(client.confirm_cli_delivery(imp_url))
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def attach(result: Any, tool_name: str, client_name: Optional[str] = None) -> Any:
    """Attach a disclosed ``sponsored`` data field to a successful tool result.

    Returns the (possibly mutated) result; never raises, never delays more
    than the SDK's own hard budget. ``client_name`` is the MCP clientInfo.name
    read at initialize / on the per-request ``_meta`` — it feeds both the CLI
    card decision and the SDK's crawler filter.
    """
    try:
        if not isinstance(result, dict) or result.get("isError"):
            return result
        client = _get_client()
        if client is None:
            return result
        sponsored = client.sponsored_slot_sync(
            context={"tool": tool_name, "client": client_name}
        )
        if not isinstance(sponsored, dict):
            return result
        if not sponsored.get("text") or not sponsored.get("url"):
            return result

        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                try:
                    payload = json.loads(item.get("text", ""))
                except (TypeError, ValueError):
                    continue  # plain-text result (download/citation) — no data slot
                if isinstance(payload, dict) and "sponsored" not in payload:
                    payload["sponsored"] = sponsored
                    item["text"] = json.dumps(payload, indent=2, ensure_ascii=True)
                break  # the first JSON text block is THE payload

        if _cli_card is not None and _cli_card.is_cli_client(client_name):
            if isinstance(content, list):
                content.append(
                    {"type": "text", "text": _cli_card.format_cli_card(sponsored)}
                )
            if sponsored.get("imp_url"):
                _fire_beacon(client, str(sponsored["imp_url"]))
    except Exception:
        return result  # fail-open: ads may never break a tool result
    return result
