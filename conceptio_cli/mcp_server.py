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
"""

import json
import sys
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


def _handle_call(client: ConceptioClient, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool call. Returns {content, isError?}."""
    # Defense in depth: the `mcp` entry point already refuses keyless
    # startup, but the config file can change under a running server.
    cfg = load_config()
    if not (str(cfg.get("api_key") or "").strip() or str(cfg.get("license_key") or "").strip()):
        return {"content": _text(AUTH_REQUIRED_HINT), "isError": True}
    if name == "conceptio_search":
        data = client.search(
            args.get("query", ""),
            limit=args.get("limit", 10),
            category=args.get("category"),
        )
        return {"content": _text(json.dumps(data, indent=2, ensure_ascii=True))}

    if name == "conceptio_resolve":
        data = client.resolve(args.get("id", ""), limit=args.get("limit", 10))
        return {"content": _text(json.dumps(data, indent=2, ensure_ascii=True))}

    if name == "conceptio_download_pdf":
        target = str(args.get("doc_id_or_url", "")).strip()
        out = str(args.get("output_path", "")).strip()
        if not target or not out:
            raise ConceptioError("Both doc_id_or_url and output_path are required.")
        path = client.download_by_target(target, out)
        return {"content": _text(f"Successfully downloaded PDF to: {path}")}

    if name == "conceptio_get_citation":
        citation = client.get_citation(int(args.get("doc_id", 0)), format=args.get("format", "bibtex"))
        return {"content": _text(citation)}

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
