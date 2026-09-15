# Changelog

All notable changes to `conceptio-search`. Version numbers follow the release
tags; the CLI's own `conceptio --version` reports the installed distribution.

## 0.3.1

### Added

- **`quota --json`** — the structured status report (`tier`, `auth`, and the
  credit fields), so a status surface such as an editor extension or a menu-bar
  widget can render the tier itself instead of scraping human text. Human text
  (the credential hint) goes to stderr, as with `info`/`proof --json`. This was
  committed after 0.3.0 was published, which is what 0.3.1 releases.
- **`--timeout SECONDS`** with `--wait` on `search --batch` and `search-job`:
  the polling budget was a hard-coded 300s. A script that must not block that
  long can now say so.
- **`--json` before the subcommand** (`conceptio --json search …`) — accepted as
  well as after it (`conceptio search … --json`). Machine callers reach for the
  first spelling; refusing it only taught them this CLI's grammar.
- **`python -m conceptio_cli`** — the same entry point as the `conceptio`
  console script, for a virtualenv that was never activated or a host process
  that resolves the interpreter rather than the shim.
- A CHANGELOG (this file), and a test asserting the version in `pyproject.toml`
  and the fallback literal in `__init__.py` cannot drift apart.
- **`tests/live_check.py`** — a live harness for the CLI itself, which had
  none: it starts the shared loopback stub (now serving the write surface too —
  batches, search jobs, connectors), drives the real binary with an env-only
  credential, and asserts on exit codes, stdout and the stub's own request log.
  It is what proves the two fixes above end to end rather than mock-adjacent:
  that an unfinished job exits 1, and that an MCP tool call works on
  `CONCEPTIO_API_KEY` alone. Offline suite: `pytest tests/`.

### Fixed

- **A `--wait` that ran out of time reported success.** It returned the last
  poll snapshot, which rendered as `No query results returned by the batch.` and
  exited **0** — a still-running job indistinguishable from a finished one with
  nothing in it. It now raises, names the job and its status, suggests
  `--timeout`, and exits 1. Affected `search --batch … --wait` and
  `search-job … --wait`.
- **The MCP server refused every tool call when the key came from the
  environment.** `conceptio mcp` started (its gate has always honoured
  `CONCEPTIO_API_KEY`), then `_handle_call` read only `~/.conceptio/config.json`
  and answered `Authentication required` to each request. Supplying a key by
  environment is precisely how an editor, an MCP client or CI avoids writing a
  config file — the documented path — and it was broken for the whole session.
  The entry gate and the per-call check now share one predicate
  (`config.has_credential`).
- **`--json` did not always keep stdout clean.** The console's stream was chosen
  at import by scanning `sys.argv`, so a caller passing its own argument list
  (`main(argv)` — a host embedding the CLI, a test, the reporter above) could
  get human text on stdout beside the payload. The stream is now set from the
  parsed arguments on every invocation — and pinned to stderr for the whole
  lifetime of the MCP server, whose stdout is JSON-RPC.
- **A closed pipe printed a traceback.** `conceptio search … | head` now exits
  with the conventional SIGPIPE status (141) and no interpreter noise.

## 0.3.0

- `proof` command (machine-readable evidence bundle, optional passage query) and
  bearer-token credentials.
- Host-process credentials: `CONCEPTIO_API_KEY` / `CONCEPTIO_LICENSE_KEY` /
  `CONCEPTIO_BEARER_TOKEN` take precedence over the config file; exactly one
  credential is ever sent.
- `--json` keeps stdout pure (human text on stderr) across `info`, `proof`,
  `quota` and `search-job`.
- `search --batch` (queue 1–50, `--sync` for 1–10) and `search-job` polling.
- Fail loudly (exit 1) when `search`/`resolve`/batch return an error dict,
  so a rate-limit or quota error is never mistaken for an empty result set.

## 0.1.1

- First published release: `search` (with `source:`/`lang:`/`category:`
  directives), `resolve`, `download`, `cite` (11 formats), `info`, `auth`,
  `quota`, `save`, and the stdio MCP server.
