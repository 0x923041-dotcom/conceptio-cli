# AGENTS.md — conceptio-cli

CLI and MCP server for the Conceptio open-access archive. Published as PyPI `conceptio-search` and listed in the official MCP registry as `io.github.0x923041-dotcom/conceptio-search` (`server.json` is the registry manifest; published with `mcp-publisher`).

## Rendering rule — corpus text is data, never markup

Every terminal and markdown sink runs through `conceptio_cli/formatter.py` (`sanitize_text`, `literal`, `link_text`, `md_text`, `md_url`) — no raw corpus string reaches rich markup, a link `Style`, or `--markdown` output. Threats: rich markup injection (`[bold red]…[/]`), `rich.errors.MarkupError` crashes, OSC-52 escapes through a search-table Title. A new sink gets a case in `tests/test_formatter_injection.py` before it ships.

## Cross-repo checks — resolve, never hardcode

Tests that compare this repo's published claims against other trees resolve their targets through `tests/cross_repo.py` by logical name; a workspace maps names to paths in `tests/cross_repo_paths.json` (gitignored — see the committed example). An absent mapping is a skip. No test may hardcode a workspace layout. **A skip is silent blindness** — when a target symbol moves inside the service repo (measured: `_CITE_FNS` api.py → docapi.py during the modularization), the carrier goes blind and only a strict carrier test catches it: repoint the logical name in `tests/cross_repo_paths.json` and the committed example in the same change that moves the symbol.

## Release lockstep — one version, several places

`pyproject.toml` · `conceptio_cli/__init__.py` (fallback) · `server.json` · `CHANGELOG.md` — plus the current-release claims in the client docs of the sibling public repos (`conceptio.nvim`, `conceptio-obsidian`), which name this package's version and must move with it (pinned by `tests/test_cli_contract.py` when mapped). Ship: bump all → `pytest -q` → build → `twine check` → upload to PyPI → publish `server.json` to the MCP registry → tag `v<version>` (`git tag -a`) and open a GitHub Release with the CHANGELOG notes — directory listings read tags/releases as maintenance signals → confirm PyPI, the registry and the client docs all state the new version before declaring the release done.

## Tests

`python -m pytest -q`, offline by default; the live harnesses (`tests/live_check.py`, `tests/live_prod_check.py`) are opt-in. Bug fixes get regression tests.

## Disclosed sponsored results — the ads seam

`conceptio_cli/ads.py` attaches ONE labeled `sponsored` data object to successful MCP tool results (Lulu Ads; web app stays no-ads). Fail-open is the contract: no `LULU_ADS_PUBLISHER_ID` + `LULU_ADS_API_KEY` env → inert, zero calls; never on `isError`; never overwrites an existing `sponsored` key; the label is the SDK's and is immutable. The `lulu-ads` dep is marker-gated to `python_version >= "3.10"` — keep the marker when touching dependencies (the `>=3.8` floor is a published claim, pinned by `tests/test_packaging.py`). Pins: `tests/test_mcp_ads.py`.
