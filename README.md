# conceptio-search

**Search 1M+ open-access documents — papers, standards, textbooks, and case law — right from your terminal or your AI agent.**

`conceptio-search` is a CLI and [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for the [Conceptio Open Knowledge Archive](https://conceptio.app) — 1M+ open-access documents from 500+ living sources, indexed daily. Every source is open access or public domain. The CLI authenticates with an API key: sign in once, save the key, search from anywhere.

- **For humans** — search, export citations in 11 formats (BibTeX, APA, MLA, Chicago, IEEE, Harvard, RIS, Bluebook, OSCOLA, ISO 690, ANSI Z39), and download PDFs to disk with one command.
- **For AI agents** — a stdio MCP server with eight tools, so Claude, Cursor, Windsurf, OpenCode, or any MCP client can search, queue batch searches, resolve identifiers (RFC, DOI, arXiv, PMID, PMCID, NIST/FIPS, W3C, US case citation), save PDFs into your workspace, and hand a document to Zotero or Obsidian.
- **100% self-contained** — talks only to the public HTTPS API. No internal infrastructure; your key lives in `~/.conceptio/config.json`.

---

## Install

```bash
pip install conceptio-search
```

Requires Python 3.8+. (Checked, not just claimed: the suite parses every source
file at the declared floor's grammar, so the promise cannot rot silently.)

The console script is `conceptio`; `python -m conceptio_cli` is the same entry
point for a virtualenv that was never activated.

---

## Authenticate (one time)

The CLI needs an API key before it can search:

```bash
conceptio auth ckey_live_...
```

Get the key by signing in at [conceptio.app](https://conceptio.app) and
creating one on your profile (Free, Dev, Pro, or Enterprise). The key is stored in
`~/.conceptio/config.json` and sent as `X-Api-Key` with every request.
`conceptio quota` reports your tier at any time. Every command below
assumes this step is done — without a key the CLI refuses to run and tells
you exactly this.

---

## Quick start

```bash
# Search — supports source:/lang:/category: directives
conceptio search "attention is all you need" --limit 5
conceptio search "source:nist zero trust" --license commercial-ok
conceptio search "source:eurlex AI act" --json
conceptio search "meditations marcus aurelius" --markdown   # for Obsidian/Notion
conceptio search "diffusion models" --offset 20            # paginate past the first page
# Multi-query programmatic search — two modes:
conceptio search --batch queries.json --json                # queue 1–50 searches, print the job handle
conceptio search --batch queries.json --wait --json         # queue, then block until done and print results
conceptio search --batch queries.json --wait --timeout 60   # …but never block longer than a minute
conceptio search --batch queries.json --sync --json         # run 1–10 immediately, no queue (one request)
conceptio search-job <job-id> --json                        # poll once; repeat on your own cadence
conceptio search-job <job-id> --wait --json                 # poll until done, then print results

# Resolve a known identifier straight to its document(s)
conceptio resolve "RFC 2119"
conceptio resolve "doi:10.1145/3290605.3300333"
conceptio resolve "2604.08499"                              # arXiv
conceptio resolve "PMID 41961061"                           # PubMed
conceptio resolve "PMC10601397"                             # PubMed Central
conceptio resolve "NIST FIPS 199"
conceptio resolve "410 U.S. 113"                            # US case citation (Supreme Court)
conceptio resolve "20-5364"                                 # federal docket
conceptio resolve "RFC 2119" --json

# Download the original PDF
conceptio download 297465 -o paper.pdf          # by document ID
conceptio download "https://arxiv.org/pdf/2604.21816v1.pdf"   # by URL

# Citations — 11 formats: bibtex, apa, mla, chicago, ieee, harvard,
# ris, bluebook, oscola, iso690, ansiz39
conceptio cite 7288 --format bibtex
conceptio cite 7288 --format apa
conceptio cite 7288 --format iso690

# Document metadata
conceptio info 2844
conceptio proof 2844                # machine-readable evidence bundle
conceptio proof 2844 -q "quantum"   # passage-level proof for a phrase

# License key (Pro) or API key + quota
conceptio auth CONCEPTIO-XXXX-XXXX-XXXX   # Pro license (one-time, account-bound)
conceptio auth ckey_live_...              # API key (agent/machine credential)
conceptio quota
conceptio quota --json                    # {tier, auth, credits…} for a status surface

# The flag may come before the subcommand, the way machine callers write it
conceptio --json search "zero trust"

# Version
conceptio --version

# AI agent server
conceptio mcp
```

### Example

```text
$ conceptio search "source:nist zero trust" --limit 3

Conceptio - 3 results for "source:nist zero trust"
  #  Year  Title                                       Author                  Source           PDF
  1  n.d.  NIST SP 1800-35: Implementing a Zero Trust  Scott Rose (NIST); …    NIST             [x]
  2  n.d.  NIST SP 800-207: Zero Trust Architecture     Scott Rose (NIST); …    NIST             [x]
  3  n.d.  NIST SP 800-207A: A Zero Trust Architecture Ramaswamy Chandramo…   NIST             [x]

  Tip: conceptio download 2844 saves the PDF, conceptio cite 2844 exports a citation.
```

Query words are highlighted in gold in the title column. The `PDF` column shows
`[x]` when a direct PDF link is available; `[ ]` rows still surface the source
URL via `conceptio info <id>`.

### Search directives

The CLI understands the same directives as the Conceptio web app — they are stripped
from the query and applied as real filters:

| Directive | Example | Effect |
|-----------|---------|--------|
| `source:` / `src:` | `source:nist` | Restrict to one or more sources (`source:nist source:owasp`) |
| `lang:` / `language:` | `lang:it` | Restrict to a language (ISO code) |
| `category:` / `cat:` | `cat:"Law & Regulation"` | Restrict to a category tab |
| `--license commercial-ok` | `--license commercial-ok` | Keep only sources whose catalog metadata explicitly permits commercial use; this is a conservative filter, not a redistribution grant. |

Directives can be combined freely: `conceptio search "cat:\"Computer Science & Tech\" source:nist lattice cryptography"`.

### Resolve

`conceptio resolve` recognises several identifier shapes and returns a typed
response (`kind: rfc | doi | arxiv | pmid | pmcid | nist | w3c | case` plus
fallback `null` for plain text). Unrecognised identifiers degrade to a regular
search so the command never fails silently.

---

## Model Context Protocol (MCP)

The `conceptio mcp` command starts a stdio JSON-RPC MCP server. It is dependency-free
(no MCP SDK required) and works with any MCP client.

### Tools

| Tool | Description |
|------|-------------|
| `conceptio_search` | Search the archive (`query`, optional `limit` 1–20, optional `category`). Returns structured results with titles, authors, years, source, snippet, and `direct_pdf_url` when available. |
| `conceptio_search_batch` | Run multiple searches in one call — queue 1–50 as a background job and return an opaque handle (`sync` omitted/false), or set `sync: true` to run 1–10 immediately and return all results in one response. |
| `conceptio_resolve` | Resolve a known identifier — RFC (`RFC 2119`), DOI (`doi:10.1145/3290605.3300333`), arXiv (`2604.08499`), PubMed ID (`PMID 41961061`), PubMed Central ID (`PMC10601397`), NIST/FIPS designation (`NIST FIPS 199`), W3C spec shortname (`w3c_digital-credentials`), or US legal citation / docket (`410 U.S. 113`, `20-5364`) — straight to its document(s). Unrecognised identifiers fall back to a text search. |
| `conceptio_download_pdf` | Download the original open-access PDF for a Conceptio document ID or a direct PDF URL to a local path. |
| `conceptio_get_citation` | Get a citation for a document ID in any of 11 formats (BibTeX, APA, MLA, Chicago, IEEE, Harvard, RIS, Bluebook, OSCOLA, ISO 690, ANSI Z39). |
| `conceptio_get_document` | Full metadata (title, author, source, category, license, year, language, URL, direct PDF URL, description) for a document ID. |
| `conceptio_connectors_send` | Save one document to Zotero, or open a metadata-only Obsidian handoff. Server-side ownership, connector trial and failure semantics apply. |
| `conceptio_connectors_send_all` | Bulk-save up to 500 document ids to Zotero. Requires Pro, institutional or licensed access — there is no free-trial path. |

### Claude Desktop

Add to `claude_desktop_config.json`:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "conceptio": {
      "command": "conceptio",
      "args": ["mcp"]
    }
  }
}
```

### Cursor

`Settings → Cursor Settings → MCP → Add new MCP server`:

```json
{
  "mcpServers": {
    "conceptio": {
      "command": "conceptio",
      "args": ["mcp"]
    }
  }
}
```

### OpenCode / Windsurf / Antigravity

The same JSON shape works in any MCP-aware client — point it at `conceptio mcp`
and the eight tools above are exposed automatically. An MCP host may supply the
credential by environment (`CONCEPTIO_API_KEY`, or `CONCEPTIO_BEARER_TOKEN` for a
signed-in session) instead of writing `~/.conceptio/config.json`.

After adding, restart the client and you can ask, for example:

> *"Find the NIST post-quantum encryption standard and download the PDF into my workspace."*

### Manual smoke test

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | conceptio mcp
```

> **Windows PowerShell quirk:** pwsh pipes text to native commands as UTF-16,
> which the server (UTF-8 JSON-RPC) answers with a `Parse error`. Real MCP
> hosts speak UTF-8 and are unaffected — this only bites hand-rolled
> `echo ... | conceptio mcp` probes from pwsh. Save the request as UTF-8
> first instead:
>
> ```powershell
> '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | Out-File -Encoding utf8 req.jsonl
> cmd /c "conceptio mcp < req.jsonl"
> ```

---

## Fair use

Every source in Conceptio is open access or public domain. The CLI sends
your API key with every request and is rate-limited per tier (see the
[rate limits](https://conceptio.app/docs#rate-limits)); the browser keeps a
separate free trial for casual searching without an account.

Add a credential once with `conceptio auth <key>` — it is stored in
`~/.conceptio/config.json` and sent with every request. `conceptio auth`
accepts either a **self-hosted API key** (`ckey_live_...`, the
agent/machine credential issued from your account — store it plainly, it is
hashed server-side and shown only once) or a **Pro license key**
(`CONCEPTIO-XXXX-XXXX-XXXX`, one-time / account-bound). API keys are
`X-Api-Key`; license keys are `X-License-Key`; exactly one credential is sent.

Without a saved key, every data command exits before touching the network:

```text
Authentication required — save an API key before searching.
```

`conceptio quota` reports your current tier (`public` / `pro` /
`enterprise`) and which auth the server honored (`api_key` / `license` /
`firebase` / `public`); `conceptio quota --json` returns the same report as
the structured object a status surface can render, with the human credential
hint on stderr so stdout stays parseable.

---

## Configuration

State lives in `~/.conceptio/config.json`:

```json
{
  "api_base": "https://www.conceptio.app",
  "license_key": "",
  "api_key": "",
  "default_limit": 10,
  "default_citation_format": "bibtex"
}
```

Override the API base (for staging, self-hosted mirrors, or a local proxy) by
editing `api_base`. Public API origins must use HTTPS; only loopback HTTP
origins such as `http://127.0.0.1:8000` are allowed for local development.
The client refuses cross-origin API redirects so credentials cannot be sent to
an unexpected host. PDF downloads reject loopback/private-network targets,
limit responses to 100 MiB, and write atomically. The MCP server additionally
keeps `output_path` beneath its current workspace and rejects traversal or
symlink escapes. Set `default_limit` to change the search page size; set
`default_citation_format` to any of the 11 supported format names.

Host processes (editors, CI, agent runtimes) can supply credentials without
writing to the config file — environment variables win over saved values:
`CONCEPTIO_API_KEY`, `CONCEPTIO_LICENSE_KEY`, and `CONCEPTIO_API_BASE`. A
signed-in human session can also be passed per-run as
`CONCEPTIO_BEARER_TOKEN` (the same short-lived bearer token the web app uses);
it takes precedence over a machine key, and exactly one credential is ever
sent.

---

## Development

```bash
pip install -e ".[test]"     # or: pip install -e . && pip install pytest
pytest tests/                      # offline tests (mocked HTTP, no network)
python tests/live_check.py         # live: the real CLI against a loopback stub
python tests/live_prod_check.py --yes   # smoke: the real CLI against the real API
```

The offline suite patches `httpx` with a `MockTransport` and stubs
`ConceptioClient` where it composes other services. No test in it makes a real
network call — which is also its ceiling: it can prove the client composes a
request and parses a response, but nothing about what the CLI *decides* from a
live exchange.

`tests/live_check.py` covers that gap. It starts the shared loopback stub
(`conceptio-nvim/test/stub_api.py`, dialled into `done`/`running`/`expired` job
modes and `exhausted`/`pro`/`not_configured` connector modes), points the real
binary at it with `CONCEPTIO_API_BASE` and an environment credential, and asserts
on exit codes, on stdout and on the stub's request log. It covers the whole
surface rather than a sample of it — search (directives, filters, markdown,
batch, jobs), resolve, cite, info, proof, download's boundary, the credential
paths, the connector failure reasons, all eight MCP tools, and the exact argv
each editor client sends (see below). It also refuses to run quietly against the
wrong binary: if `conceptio --version` does not match the version this tree
declares, every check would be proving an older install, so it fails and says so.

Everything stays on loopback: no account, no key, no production credit, and the
CLI is given a throwaway config directory so your real
`~/.conceptio/config.json` is never read or written. `CONCEPTIO_CLI` selects
the binary, `CONCEPTIO_STUB` the stub, `CONCEPTIO_LIVE_VERBOSE=1` echoes each
command's output. It exits nonzero and prints every failing check with the
command's own output.

`tests/live_prod_check.py` is that harness's deliberate opposite, and closing
the pair is the point. Everything above is pinned against a server we control,
so none of it can see the *live* API moving — a renamed field in `/api/me`, a
refusal that changes wording, an edge rule that starts rejecting the CLI's
transport. Those ship silently, because this repo's own suite stays green while
every real user sees the drift.

It is opt-in (`--yes`) and safe by construction rather than by discipline: it
refuses to run unless the base is the shipped default, the credential is a
placeholder that any inherited `CONCEPTIO_*` variable is stripped out in favour
of, and it only runs reads the API keeps open without a credential plus commands
whose expected outcome is a refusal — no search ever executes. `rm -f` the
guard and it stops being able to tell production from a stub; re-point the
default base and 6 of its 9 checks go red, which is how you know they bite.

### The client contract

The CLI is also a dependency of five editor clients — the Neovim plugin, the
VS Code extension, the Obsidian plugin, the Raycast extension and the Alfred
workflow. They exec this binary with an argv list and read stdout; nothing
couples them but that grammar. `tests/client_contract.py` records every
invocation they make, with the file it came from:

- `tests/test_cli_contract.py` parses each row against `cli.build_parser()`, and
  while a client's checkout sits next to this repo, asserts its sources still
  contain the subcommand and flags the row claims.
- `tests/live_check.py` runs the same rows against a real binary on the stub.

If you change a subcommand or a flag, one of the two will fail and name the
client. Add a row when a client gains a call.

The README is a client too. `tests/test_readme_contract.py` extracts every
invocation from the code blocks above and parses it against the same grammar,
so a renamed flag cannot leave the documentation teaching a command that exits
with a usage error. Shell constructs (a pipe, a redirect) are skipped by an
explicit rule rather than by exception, and the test fails if it stops finding
the examples — an extractor that quietly matches nothing proves nothing.

---

## License

MIT — see [LICENSE](LICENSE).

Part of the [Conceptio Open Knowledge Archive](https://conceptio.app) — 500+
living sources: arXiv, NIST, OWASP, CISA, PubMed/PMC, MIT OpenCourseWare,
EUR-Lex, Project Gutenberg, and more.
