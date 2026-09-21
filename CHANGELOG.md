# Changelog

All notable changes to `conceptio-search`. Version numbers follow the release
tags; the CLI's own `conceptio --version` reports the installed distribution.

## 0.3.4

### Fixed

- **`proof`'s human summary read a bundle shape no server sends.** The evidence
  bundle nests the document's identity under `document` and the matched passage
  under `passage`, but the summary read the
  top level — so against the real API it printed `Source —` and dropped the
  passage a `proof -q` was fetched for. Both the offline fixture and the shared
  loopback stub served a flat spelling the API never emits, which is why every
  check stayed green. `_print_proof_summary` now reads the nested bundle (the
  flat spelling still renders, so an older server is unaffected) and states the
  retrieval verdict — `access_level` with `full_text_available` — which the
  summary never showed at all. An open-access row whose extraction is empty now
  reads `Full text (nothing extracted for this row)` instead of looking like a
  licence limit.
- **The shared stub served a proof shape the API does not emit.**
  `conceptio-nvim/test/stub_api.py` returns the production nesting, and
  `tests/live_check.py` asserts the *rendered rows* — the source label, the `-q`
  passage in the human summary, and `passage.snippet` in the JSON bundle — rather
  than the header's presence. Negative control: the previous renderer fails that
  check with `proof: the source label never rendered: Proof bundle: document
  2844`.
- **`~/.conceptio/config.json` was written world-readable.** The file holds a
  long-lived `ckey_live_…`; written at the process umask it is 0644, readable by
  every account on a shared machine, and a copied key keeps working because the
  server only ever sees its hash. It is now created `0600` inside a `0700`
  directory on POSIX, and a loose file left by an earlier version is repaired on
  the next save (measured on Linux: `file=644 dir=775` → `file=600 dir=700`).
- **A saved bearer token outranked the key a user had just saved.** `_headers`
  prefers a bearer token, so one left in the config file kept winning after
  `auth` reported the new key as accepted. Saving an API or licence key now
  clears a stored bearer token, and the environment note names
  `CONCEPTIO_BEARER_TOKEN` — the variable most able to shadow the key being
  saved.

## 0.3.3

### Added

- **`tests/live_prod_check.py` — the published CLI against the real API.**
  `live_check.py` pins behaviour against a loopback stub we control, so nothing
  in this repo could notice the *live* API moving underneath it: a renamed field
  in `/api/me`, a refusal message that changes wording, an edge rule that starts
  rejecting the CLI's transport. Those ship silently — the suite stays green
  while every real user sees the drift. The new harness runs the real binary
  against `https://www.conceptio.app` and covers the reads the API keeps open
  without a credential (`/api/me`), the credential paths (the local keyless
  refusal, and an unknown key being refused as *authentication* rather than as
  quota), and every data command's refusal shape (`search`, `info`, `cite`,
  `proof`), asserting on exit codes and on the absence of a traceback or a raw
  server body.

  It is opt-in (`--yes`) because it touches production, and it is safe by
  construction rather than by discipline: it refuses to run unless the base is
  the shipped default (otherwise it would report "production verified" from a
  stub), the credential is a literal placeholder that any inherited
  `CONCEPTIO_*` variable is stripped out in favour of, and no search ever
  executes. Removing the guard or re-pointing the default base turns 6 of its 9
  checks red, which is the only evidence that a check is load-bearing.

### Fixed

- **A 403 with no readable body was reported as a rate limit, and as a plan
  problem.** `_get_json` fell back to `UPGRADE_HINT` ("Rate limit or credit
  quota exhausted … upgrade at …/pricing") whenever a 403 carried no
  API-readable `detail`. A 403 is not a rate limit, and the API *always*
  explains a Dev-gate refusal in its own JSON — so the one case that reached
  this fallback was a refusal that came from somewhere else entirely: an edge
  firewall, a corporate proxy, a captive portal. That user was told to buy a
  plan that no measurement supported. `FORBIDDEN_HINT` now names both causes and
  points at `conceptio quota`, the one tier read that stays open when a request
  is refused. A 403 that *does* carry the API's own detail is still surfaced
  verbatim, which is the Dev-funnel copy.

## 0.3.2

### Added

- **`tests/client_contract.py` — the argv grammar the five clients depend on,
  written down and tested.** The Neovim, VS Code, Obsidian, Raycast and Alfred
  clients all exec this binary with an argument list and read stdout; nothing
  type-checks that list, and no client's suite can see this repo, so a renamed
  subcommand passes both suites and ships broken. Every distinct invocation is
  recorded with the file it came from, `cli.build_parser()` exposes the grammar
  so the offline suite can parse each one, and `live_check.py` runs the same
  rows against a real binary. A client that is not checked out is a *skip*, not
  a pass.
- **`conceptio_cli.cli.build_parser()`** — the grammar, built separately from
  `main()` so it can be introspected. Behaviour is unchanged.
- **`tests/live_check.py` grew from 19 checks to 44.** The harness now covers
  the half of the surface it never touched: `resolve`, `cite` (all formats plus
  a locally-refused one), `info`, `proof` (including `-q`), `search` filters and
  directives as they appear in the request, `--markdown`, the credential paths
  (`auth` writing a config file, a saved key with no environment key, a keyless
  refusal, and where `quota` says the key lives), the download boundary
  (loopback refused, no link reported), the connector failure reasons, all
  eight MCP tools, and each client's own argv. It also asserts that the binary
  it is driving is this working tree — see Fixed.

### Fixed

- **`quota` claimed an environment key was "saved in `~/.conceptio/config.json`".**
  Supplying the credential by `CONCEPTIO_API_KEY` is the documented path for an
  editor, an MCP host or CI — the one case where no config file exists — and
  the sentence was printed anyway. The client now records where the effective
  credential came from (argument, environment, or config file), and the hint
  reports that instead of guessing. A saved key still says "saved".
- **`info` and `cite` accepted a document id below 1.** `proof` refused it while
  `info 0` and `cite 0` asked the API for document 0 and rendered whatever came
  back. One predicate now validates the id in every method that takes one, so
  the CLI and the MCP tools agree.
- **`proof`'s SHA-256 row folded onto a second, unindented line** in a default
  80-column terminal — the one line a reader copies out of the bundle arrived
  split in two. It overflows instead of wrapping.

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
