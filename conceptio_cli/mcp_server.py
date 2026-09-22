"""Model Context Protocol (MCP) server for Conceptio — stdio JSON-RPC.

Zero-dependency hand-rolled implementation of the MCP stdio transport, so the
package works with Claude Desktop, Cursor, Windsurf, Antigravity, OpenCode,
and any other MCP client without pulling in a framework.

**Dual-era (2026-07-28).** The current revision replaces the `initialize`
handshake with per-request `_meta`, and makes `server/discover` a MUST. This
server implements both: `server/discover` answers with the revisions it speaks,
an `initialize` opens the legacy path, and a request carrying
`io.modelcontextprotocol/protocolVersion` in `_meta` is served statelessly by the
modern path — `resultType: "complete"` on results, cache hints on the two
cacheable lists. The official `mcp` SDK was considered and declined for now (it
requires Python >=3.10 on a package that publishes `>=3.8`, and the modern work
here is small). Rationale and migration order: `Conceptio/Plans/mcp_protocol_era.md`.

Exposed tools:
  - conceptio_search          — keyword search over the open-access archive
  - conceptio_resolve         — resolve an identifier (RFC, DOI, arXiv, PMID, PMCID, NIST, W3C)
  - conceptio_download_pdf    — resolve a doc ID/URL and stream the PDF to disk
  - conceptio_get_citation    — BibTeX/APA/MLA/Chicago citation
  - conceptio_get_document    — full metadata for a document ID
  - conceptio_search_batch     — queue 1–50 searches and return a polling handle
  - conceptio_connectors_send   — save one document to Zotero or Obsidian
  - conceptio_connectors_send_all — Pro-only bulk Zotero save
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .client import ConceptioClient, ConceptioError
from .config import AUTH_REQUIRED_HINT, has_credential, load_config

SERVER_NAME = "conceptio-mcp"

#: The handshake revision this server DECLARES. `/mcp.json` published this alone
#: before the server learned the modern era; it remains the answer the legacy
#: path gives for a requested version outside its band.
PROTOCOL_VERSION = "2024-11-05"

#: The handshake-era revisions this server is compatible with, oldest first. In
#: the 2026-07-28 vocabulary this is a **Legacy** server (it establishes a
#: session with an `initialize` handshake), so the band ends at the last
#: handshake revision, 2025-11-25. The wire subset we speak — `initialize`,
#: `tools/list`, `tools/call`; no resources, prompts, sampling, elicitation,
#: tasks or logging — is unchanged across the band, and a client uses only
#: capabilities that were actually negotiated, so the band is the honest
#: declaration rather than a courtesy.
LEGACY_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

#: The current protocol revision — the **modern** era, where version, identity
#: and capabilities travel as per-request `_meta` instead of an `initialize`
#: handshake (revision 2026-07-28 and later). Read live 2026-09-22.
CURRENT_PROTOCOL_VERSION = "2026-07-28"

#: Modern revisions this server implements. One entry today; a tuple so the next
#: revision is a one-line change.
MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)

#: What `server/discover` advertises, newest first — the modern revision, then the
#: handshake band. This is a **dual-era** server: `initialize` opens the legacy
#: path, a request carrying per-request `_meta` is served statelessly by the
#: modern one, and both are served from this same process (2026-07-28 Versioning
#: -> Backward Compatibility).
SUPPORTED_PROTOCOL_VERSIONS = (
    (CURRENT_PROTOCOL_VERSION,) + tuple(reversed(LEGACY_PROTOCOL_VERSIONS))
)

#: Per-request metadata keys (2026-07-28). On the modern path the version travels
#: on every request, not on a handshake.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

#: `UnsupportedProtocolVersionError` — the modern contract for a version we do not
#: implement. It carries `data.supported` / `data.requested` so the client retries
#: with a mutually supported version instead of guessing.
UNSUPPORTED_PROTOCOL_VERSION_CODE = -32022

#: Cache hints (2026-07-28 Caching). Servers MUST include these on
#: `resultType: "complete"` results of `server/discover` and `tools/list`; both are
#: identical for every caller, so both are `public`. An hour is generous for a list
#: that changes only when the package does.
CACHE_TTL_MS = 3600000
CACHE_SCOPE = "public"

SERVER_INSTRUCTIONS = (
    "Conceptio is an open-access document retrieval layer: search a large live "
    "corpus of books, papers, standards and case law, resolve identifiers, fetch "
    "metadata and citations, and download open-access PDFs. Every search requires "
    "an API key - run `conceptio auth` once first."
)


def negotiate_protocol_version(requested: Optional[Any]) -> str:
    """The revision to answer an `initialize` with (2025-11-25 Lifecycle).

    "If the server supports the requested protocol version, it MUST respond with
    the same version. Otherwise, the server MUST respond with another protocol
    version it supports" — after which the client decides whether to continue.

    So: echo a version inside the band, and answer **our own declared version**
    for anything outside it. Never the requested one when it is unsupported,
    which is what this did until 2026-09-22: a client asking for a modern
    revision (`2026-07-28`) was told `2026-07-28` and then served legacy
    semantics, so nothing in the exchange could tell it which era it was in.

    Deliberately NOT an error: `UnsupportedProtocolVersionError` (`-32022`) is
    the 2026-07-28 contract for a *modern* server, and a handshake-era client
    has no fall-forward mechanism — answering with it would replace one
    mislabelling with a louder one.
    """
    version = str(requested or "").strip()
    if version in LEGACY_PROTOCOL_VERSIONS:
        return version
    return PROTOCOL_VERSION


def _server_info() -> Dict[str, str]:
    return {"name": SERVER_NAME, "version": __version__}


def _server_meta() -> Dict[str, Any]:
    """The `serverInfo` block every modern result carries in its `_meta`."""
    return {META_SERVER_INFO: _server_info()}


def _modernize(result: Dict[str, Any], *, cacheable: bool = False) -> Dict[str, Any]:
    """Mark a result as modern-era: `resultType`, `serverInfo`, cache hints.

    Legacy results carry none of this — the handshake band predates
    `resultType`, and a field a legacy client does not read is a wire change for
    nobody.
    """
    out = dict(result)
    out["resultType"] = "complete"
    out["_meta"] = _server_meta()
    if cacheable:
        out["ttlMs"] = CACHE_TTL_MS
        out["cacheScope"] = CACHE_SCOPE
    return out


def _discover_result() -> Dict[str, Any]:
    """`server/discover` — the MUST, and what a modern client probes with.

    One RPC returning the versions we support, our capabilities and our identity,
    so a client selects a version instead of guessing. On stdio this is also the
    documented backward-compatibility probe: a client that speaks both eras sends
    it first and falls back to `initialize` only on a non-modern error.

    Deliberately not gated on credentials — like `initialize`, discovery is how a
    host decides whether the server is usable at all. A keyless server still
    answers, and refuses each tool call.
    """
    return {
        "resultType": "complete",
        "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {}},
        "_meta": _server_meta(),
        "instructions": SERVER_INSTRUCTIONS,
        "ttlMs": CACHE_TTL_MS,
        "cacheScope": CACHE_SCOPE,
    }


def _unsupported_version(requested: str) -> Dict[str, Any]:
    """The error body for a modern request naming a version we do not implement."""
    return {
        "code": UNSUPPORTED_PROTOCOL_VERSION_CODE,
        "message": "Unsupported protocol version",
        "data": {
            "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
            "requested": requested,
        },
    }


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "conceptio_search",
        "description": (
            "Search the open-access archive — papers, technical standards (NIST, OWASP, CISA), "
            "textbooks, and legal sources (EUR-Lex, HUDOC) with license-aware access. "
            "Requires an API key: run `conceptio auth` once first. Query supports "
            "directives like 'source:nist zero trust'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords or directives (e.g. 'source:nist zero trust')",
                },
                "limit": {"type": "integer", "description": "Number of results (1-20)", "default": 10},
                "category": {
                    "type": "string",
                    "description": "Optional category filter: 'Science & Medicine', 'Economics & Finance', "
                                   "'Computer Science & Tech', 'Social Sciences & Humanities', "
                                   "'Arts & Culture', 'Law & Regulation'",
                },
                "license": {
                    "type": "string",
                    "enum": ["commercial-ok"],
                    "description": "Optional rights filter requiring explicit commercial-use permission.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "conceptio_resolve",
        "description": (
            "Resolve a known identifier straight to its document(s) in the archive: an RFC number "
            "('RFC 2119'), a DOI ('doi:10.1109/access.2020.2986772'), an arXiv ID ('2604.08499'), a "
            "PubMed ID ('PMID 41961061'), a PubMed Central ID ('PMC10601397'), a NIST/FIPS "
            "designation ('NIST FIPS 199'), a W3C spec shortname ('w3c_digital-credentials'), "
            "or a US legal citation / docket ('347 U.S. 483', '20-5364'). "
            "Unrecognized identifiers fall back to a text search. Use this when an agent has a "
            "concrete citation/reference it wants to locate precisely."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "The identifier to resolve (e.g. 'RFC 2119', 'doi:10.x/y', 'PMC10601397')",
                },
                "limit": {"type": "integer", "description": "Max results (1-50)", "default": 10},
            },
            "required": ["id"],
        },
    },
    {
        "name": "conceptio_download_pdf",
        "description": (
            "Download the original open-access PDF of a paper, standard, or book to a local "
            "file path. Accepts a Conceptio document ID (from conceptio_search) or a direct PDF URL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id_or_url": {
                    "type": "string",
                    "description": "Conceptio document ID (digits) or direct PDF URL",
                },
                "output_path": {
                    "type": "string",
                    "description": "Local destination file path (e.g. 'workspace/paper.pdf')",
                },
            },
            "required": ["doc_id_or_url", "output_path"],
        },
    },
    {
        "name": "conceptio_get_citation",
        "description": "Get an academic citation for a document in BibTeX, APA, MLA, or Chicago format.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "integer", "description": "Conceptio document ID"},
                "format": {
                    "type": "string",
                    "enum": ["bibtex", "apa", "mla", "chicago", "ieee", "harvard", "ris", "bluebook", "oscola", "iso690", "ansiz39"],
                    "default": "bibtex",
                },
            },
            "required": ["doc_id"],
        },
    },
    {
        "name": "conceptio_search_batch",
        "description": "Run multiple independent searches in one call. Default: queue 1–50 for bounded background execution and return an opaque job handle (poll it with the public API or `conceptio search-job`). Set sync:true to run 1–10 immediately and return all results in one response — each fresh subquery uses one search credit either way.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "description": "Search objects with q and optional sources, category, language, sort, limit, and offset",
                    "items": {"type": "object"}
                },
                "sync": {
                    "type": "boolean",
                    "default": False,
                    "description": "Run synchronously (1–10 queries, results in one call) instead of queueing a job"
                }
            },
            "required": ["queries"]
        },
    },
    {
        "name": "conceptio_connectors_send",
        "description": "Save one document to Zotero or open a metadata-only Obsidian handoff. Server-side ownership, connector trial, and failure semantics apply.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "connector": {"type": "string", "enum": ["zotero", "obsidian"]},
                "doc_id": {"type": "integer"},
                "vault": {"type": "string", "description": "Optional Obsidian vault name"}
            },
            "required": ["connector", "doc_id"]
        }
    },
    {
        "name": "conceptio_connectors_send_all",
        "description": "Bulk-save selected documents to Zotero. Pro, institutional, or licensed access is required; no free trial path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "connector": {"type": "string", "enum": ["zotero"], "default": "zotero"},
                "doc_ids": {"type": "array", "minItems": 1, "maxItems": 500, "items": {"type": "integer"}}
            },
            "required": ["doc_ids"]
        }
    },
    {
        "name": "conceptio_get_document",
        "description": "Fetch complete metadata (title, author, source, license, description, direct PDF URL) for a document ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"doc_id": {"type": "integer", "description": "Conceptio document ID"}},
            "required": ["doc_id"],
        },
    },
]


def _text(content: str) -> List[Dict[str, str]]:
    return [{"type": "text", "text": content}]


def _with_attribution(data: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve the API's additive free-tier attribution for MCP consumers."""
    return data


def _workspace_output_path(raw: str) -> str:
    """Resolve an MCP output path beneath the MCP process workspace.

    MCP tool arguments are agent-controlled input. Keeping writes below the
    process cwd prevents a prompt-injected document from overwriting arbitrary
    local files such as SSH keys, shell profiles, or project files elsewhere.
    Existing symlink targets are rejected and parent resolution prevents a
    symlinked directory from escaping the workspace.
    """
    value = str(raw or "").strip()
    if not value:
        raise ConceptioError("A local output path is required.")
    workspace = Path.cwd().resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ConceptioError("Output path must stay inside the MCP workspace.") from exc
    if candidate.exists() and candidate.is_symlink():
        raise ConceptioError("Refusing to write through a symbolic link.")
    return str(resolved)


def _payload_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """A tool result, marked as an error when the payload carries one.

    The client answers a request it could not serve with a soft error —
    ``{"error": "Empty search query.", "results": []}``, or a 429's detail —
    rather than raising, because the CLI renders that payload for a human. A
    tool result that says nothing about it therefore told every MCP host the
    call had *succeeded*: an empty query, or a rate-limited one, came back as a
    result the host renders like any other. The MCP convention is ``isError``
    on the result, which is what a host actually reads.
    """
    result: Dict[str, Any] = {"content": _text(json.dumps(data, indent=2, ensure_ascii=True))}
    if isinstance(data, dict) and data.get("error"):
        result["isError"] = True
    return result


def _handle_call(client: ConceptioClient, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool call. Returns {content, isError?}."""
    # Defense in depth: the `mcp` entry point already refuses keyless startup,
    # but the config file can change under a running server. This must ask the
    # SAME question the CLI's gate asks — including the environment, which is
    # how an MCP host supplies a key without touching the config file. It used
    # to read only the config file, so a server started with CONCEPTIO_API_KEY
    # accepted the connection and then refused every single tool call.
    if not has_credential(load_config()):
        return {"content": _text(AUTH_REQUIRED_HINT), "isError": True}
    if name == "conceptio_search":
        search_args = {
            "limit": args.get("limit", 10),
            "category": args.get("category"),
        }
        if args.get("license"):
            search_args["license"] = args["license"]
        data = _with_attribution(client.search(
            args.get("query", ""),
            **search_args,
        ))
        return _payload_result(data)

    if name == "conceptio_resolve":
        data = client.resolve(args.get("id", ""), limit=args.get("limit", 10))
        return _payload_result(data)

    if name == "conceptio_download_pdf":
        target = str(args.get("doc_id_or_url", "")).strip()
        out = str(args.get("output_path", "")).strip()
        if not target or not out:
            raise ConceptioError("Both doc_id_or_url and output_path are required.")
        safe_out = _workspace_output_path(out)
        path = client.download_by_target(target, safe_out)
        return {"content": _text(f"Successfully downloaded PDF to: {path}")}

    if name == "conceptio_get_citation":
        citation = client.get_citation(int(args.get("doc_id", 0)), format=args.get("format", "bibtex"))
        return {"content": _text(citation)}

    if name == "conceptio_search_batch":
        queries = args.get("queries")
        if not isinstance(queries, list) or not 1 <= len(queries) <= 50:
            raise ConceptioError("queries must contain between 1 and 50 search objects.")
        if any(not isinstance(item, dict) or not str(item.get("q") or "").strip() for item in queries):
            raise ConceptioError("Every search object must contain a non-empty q field.")
        if args.get("sync"):
            if len(queries) > 10:
                raise ConceptioError("sync batches support at most 10 queries; drop sync to queue up to 50.")
            data = client.batch_search(queries)
        else:
            data = client.submit_search_job(queries)
        return _payload_result(data)

    if name == "conceptio_connectors_send":
        connector = str(args.get("connector") or "").strip().lower()
        doc_id = int(args.get("doc_id", 0))
        if connector not in {"zotero", "obsidian"} or doc_id < 1:
            raise ConceptioError("connector must be zotero or obsidian and doc_id must be positive.")
        data = client.send_connector(connector, doc_id, vault=str(args.get("vault") or ""))
        return _payload_result(data)

    if name == "conceptio_connectors_send_all":
        connector = str(args.get("connector") or "zotero").strip().lower()
        doc_ids = args.get("doc_ids")
        if connector != "zotero" or not isinstance(doc_ids, list):
            raise ConceptioError("Bulk connector saves require connector=zotero and a doc_ids array.")
        data = client.send_zotero_all(doc_ids)
        return _payload_result(data)

    if name == "conceptio_get_document":
        doc = client.get_document(int(args.get("doc_id", 0)))
        return _payload_result(doc)

    return {"content": _text(f"Unknown tool: {name}"), "isError": True}


def run_mcp_server() -> int:
    """Run the stdio MCP loop until stdin closes. Returns 0 on clean exit."""
    client = ConceptioClient()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id: Optional[Any] = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params") or {}
            meta = params.get("_meta") or {}
            # A request carrying per-request `_meta` is the MODERN era; its
            # absence is the legacy path (opened by an `initialize` handshake).
            modern_version = str(meta.get(META_PROTOCOL_VERSION) or "").strip()

            if modern_version and modern_version not in MODERN_PROTOCOL_VERSIONS:
                # The modern contract: name the versions we DO support so the
                # client retries instead of guessing (2026-07-28 Versioning).
                res = {"jsonrpc": "2.0", "id": req_id,
                       "error": _unsupported_version(modern_version)}
            elif method == "server/discover":
                # MUST be implemented, and served whether or not the probe
                # carried `_meta` — it is how a dual-era client finds our era.
                res = {"jsonrpc": "2.0", "id": req_id, "result": _discover_result()}
            elif method == "initialize":
                requested = params.get("protocolVersion")
                result = {
                    "protocolVersion": negotiate_protocol_version(requested),
                    "capabilities": {"tools": {}},
                    "serverInfo": _server_info(),
                }
                res = {"jsonrpc": "2.0", "id": req_id, "result": result}
            elif method == "ping":
                # Removed from the core in 2026-07-28; it survives only on the
                # legacy path, so a modern request for it is an unknown method.
                if modern_version:
                    res = {"jsonrpc": "2.0", "id": req_id,
                           "error": {"code": -32601, "message": "Method not found"}}
                else:
                    res = {"jsonrpc": "2.0", "id": req_id, "result": {}}
            elif method == "tools/list":
                result = {"tools": TOOLS}
                if modern_version:
                    result = _modernize(result, cacheable=True)
                res = {"jsonrpc": "2.0", "id": req_id, "result": result}
            elif method == "tools/call":
                result = _handle_call(client, params.get("name", ""), params.get("arguments") or {})
                if modern_version:
                    result = _modernize(result)
                res = {"jsonrpc": "2.0", "id": req_id, "result": result}
            elif method.startswith("notifications/"):
                # Notifications carry no id and expect no response.
                continue
            else:
                res = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}
        except json.JSONDecodeError:
            res = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        except (ConceptioError, ValueError, TypeError, KeyError) as e:
            res = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}
        except Exception as e:  # never crash the server
            res = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}

        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(run_mcp_server())
