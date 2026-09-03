# conceptio-search

**Search the open-access archive — papers, standards, textbooks, and legal documents — right from your terminal or your AI agent.**

`conceptio-search` is a CLI and [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for the [Conceptio Open Knowledge Archive](https://conceptio.app). Every source in the archive is open access or public domain. The CLI authenticates with an API key: sign in once, save the key, search from anywhere.

- **For humans** — search, export citations in 11 formats (BibTeX, APA, MLA, Chicago, IEEE, Harvard, RIS, Bluebook, OSCOLA, ISO 690, ANSI Z39), and download PDFs to disk with one command.
- **For AI agents** — a stdio MCP server with five tools, so Claude, Cursor, Windsurf, OpenCode, or any MCP client can search, resolve identifiers (RFC, DOI, arXiv, PMID, PMCID, NIST/FIPS, W3C, US case citation), and save PDFs into your workspace.
- **100% self-contained** — talks only to the public HTTPS API. No internal infrastructure; your key lives in `~/.conceptio/config.json`.

---

## Install

```bash
pip install conceptio-search
```

Or from source:

```bash
git clone https://github.com/0x923041-dotcom/conceptio-cli.git
cd conceptio-cli
pip install .
```

Requires Python 3.8+.

---

## Authenticate (one time)

The CLI needs an API key before it can search:

```bash
conceptio auth ckey_live_...
```

Get the key by signing in at [conceptio.app](https://conceptio.app) and
creating one on your profile (Pro or Institutional). The key is stored in
`~/.conceptio/config.json` and sent as `X-Api-Key` with every request.
`conceptio quota` reports your tier at any time. Every command below
assumes this step is done — without a key the CLI refuses to run and tells
you exactly this.

---

## Quick start

```bash
# Search — supports source:/lang:/category: directives
conceptio search "attention is all you need" --limit 5
conceptio search "source:nist zero trust" --category "Computer Science & Tech"
conceptio search "source:eurlex AI act" --json
conceptio search "meditations marcus aurelius" --markdown   # for Obsidian/Notion
conceptio search "diffusion models" --offset 20            # paginate past the first page

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

# License key (Pro) or API key + quota
conceptio auth CONCEPTIO-XXXX-XXXX-XXXX   # Pro license (one-time, account-bound)
conceptio auth ckey_live_...              # API key (agent/machine credential)
conceptio quota

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
| `conceptio_resolve` | Resolve a known identifier — RFC (`RFC 2119`), DOI (`doi:10.1145/3290605.3300333`), arXiv (`2604.08499`), PubMed ID (`PMID 41961061`), PubMed Central ID (`PMC10601397`), NIST/FIPS designation (`NIST FIPS 199`), W3C spec shortname (`w3c_digital-credentials`), or US legal citation / docket (`410 U.S. 113`, `20-5364`) — straight to its document(s). Unrecognised identifiers fall back to a text search. |
| `conceptio_download_pdf` | Download the original open-access PDF for a Conceptio document ID or a direct PDF URL to a local path. |
| `conceptio_get_citation` | Get a citation for a document ID in any of 11 formats (BibTeX, APA, MLA, Chicago, IEEE, Harvard, RIS, Bluebook, OSCOLA, ISO 690, ANSI Z39). |
| `conceptio_get_document` | Full metadata (title, author, source, category, license, year, language, URL, direct PDF URL, description) for a document ID. |

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
and the five tools above are exposed automatically.

After adding, restart the client and you can ask, for example:

> *"Find the NIST post-quantum encryption standard and download the PDF into my workspace."*

### Manual smoke test

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | conceptio mcp
```

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
`institutional`) and which auth the server honored (`api_key` / `license` /
`firebase` / `public`).

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
editing `api_base`. Set `default_limit` to change the search page size; set
`default_citation_format` to any of the 11 supported format names.

---

## Development

```bash
pip install -e ".[test]"     # or: pip install -e . && pip install pytest
pytest tests/                # 60 offline tests (mocked HTTP, no network)
```

The test suite is fully offline — `httpx` is patched with a `MockTransport`
and `ConceptioClient` is stubbed where it composes other services. No test makes a
real network call.

---

## License

MIT — see [LICENSE](LICENSE).

Part of the [Conceptio Open Knowledge Archive](https://conceptio.app) — 500+
living sources: arXiv, NIST, OWASP, CISA, PubMed/PMC, MIT OpenCourseWare,
EUR-Lex, Project Gutenberg, and more.
