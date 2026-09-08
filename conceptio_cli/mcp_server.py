"""Model Context Protocol (MCP) server for Conceptio — stdio JSON-RPC.

Zero-dependency hand-rolled implementation of the MCP stdio transport, so the
package works with Claude Desktop, Cursor, Windsurf, Antigravity, OpenCode,
and any other MCP client without pulling in a framework.

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
from .config import AUTH_REQUIRED_HINT, load_config

SERVER_NAME = "conceptio-mcp"
PROTOCOL_VERSION = "2024-11-05"

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
            "('RFC 2119'), a DOI ('doi:10.1145/3290605.3300333'), an arXiv ID ('2604.08499'), a "
            "PubMed ID ('PMID 41961061'), a PubMed Central ID ('PMC10601397'), a NIST/FIPS "
            "designation ('NIST FIPS 199'), a W3C spec shortname ('w3c_digital-credentials'), "
            "or a US legal citation / docket ('410 U.S. 113', '20-5364'). "
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


def _handle_call(client: ConceptioClient, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool call. Returns {content, isError?}."""
    # Defense in depth: the `mcp` entry point already refuses keyless
    # startup, but the config file can change under a running server.
    cfg = load_config()
    if not (str(cfg.get("api_key") or "").strip() or str(cfg.get("license_key") or "").strip()):
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
        return {"content": _text(json.dumps(data, indent=2, ensure_ascii=True))}

    if name == "conceptio_resolve":
        data = client.resolve(args.get("id", ""), limit=args.get("limit", 10))
        return {"content": _text(json.dumps(data, indent=2, ensure_ascii=True))}

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
        return {"content": _text(json.dumps(data, indent=2, ensure_ascii=True))}

    if name == "conceptio_connectors_send":
        connector = str(args.get("connector") or "").strip().lower()
        doc_id = int(args.get("doc_id", 0))
        if connector not in {"zotero", "obsidian"} or doc_id < 1:
            raise ConceptioError("connector must be zotero or obsidian and doc_id must be positive.")
        data = client.send_connector(connector, doc_id, vault=str(args.get("vault") or ""))
        return {"content": _text(json.dumps(data, indent=2, ensure_ascii=True))}

    if name == "conceptio_connectors_send_all":
        connector = str(args.get("connector") or "zotero").strip().lower()
        doc_ids = args.get("doc_ids")
        if connector != "zotero" or not isinstance(doc_ids, list):
            raise ConceptioError("Bulk connector saves require connector=zotero and a doc_ids array.")
        data = client.send_zotero_all(doc_ids)
        return {"content": _text(json.dumps(data, indent=2, ensure_ascii=True))}

    if name == "conceptio_get_document":
        doc = client.get_document(int(args.get("doc_id", 0)))
        return {"content": _text(json.dumps(doc, indent=2, ensure_ascii=True))}

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

            if method == "initialize":
                requested = (req.get("params") or {}).get("protocolVersion")
                result = {
                    "protocolVersion": requested or PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": __version__},
                }
                res = {"jsonrpc": "2.0", "id": req_id, "result": result}
            elif method == "ping":
                res = {"jsonrpc": "2.0", "id": req_id, "result": {}}
            elif method == "tools/list":
                res = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
            elif method == "tools/call":
                params = req.get("params") or {}
                result = _handle_call(client, params.get("name", ""), params.get("arguments") or {})
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
