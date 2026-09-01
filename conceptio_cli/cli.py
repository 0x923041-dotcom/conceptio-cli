"""conceptio — command-line interface for the Conceptio Open Knowledge Archive."""

import argparse
import json
import sys
from typing import Optional

from . import __version__
from .client import ConceptioClient, ConceptioError
from .config import load_config, set_api_key, set_license_key
from .formatter import console, print_document_info, print_search_results, to_markdown

CITE_FORMATS = ["bibtex", "apa", "mla", "chicago", "ieee", "harvard", "ris", "bluebook", "oscola", "iso690", "ansiz39"]


def _prepare_stdio() -> None:
    """Make stdout/stderr encoding-safe on legacy Windows consoles.

    rich's legacy renderer encodes via the stream's codec; a non-cp1252
    character in a document title (e.g. '\u0107') crashes it with a
    UnicodeEncodeError. UTF-8 with errors='replace' never crashes and renders
    correctly on modern terminals.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _default_output_name(target: str) -> str:
    target = str(target or "").strip()
    if target.isdigit():
        return f"conceptio_{target}.pdf"
    tail = target.rstrip("/").split("/")[-1] or "conceptio.pdf"
    if not tail.lower().endswith(".pdf"):
        tail += ".pdf"
    return tail


def handle_download(client: ConceptioClient, target: str, output: Optional[str]) -> int:
    out = output or _default_output_name(target)
    try:
        resolved = client.resolve_download_url(target)
        console.print(f"[dim]Downloading[/] {resolved}")
        client.download_pdf(resolved, out)
        console.print(f"[bold green][OK][/] Saved {out}")
        return 0
    except ConceptioError as e:
        console.print(f"[bold red][ERR][/] {e}")
        return 1
    except OSError as e:
        console.print(f"[bold red][ERR][/] Could not write {out}: {e}")
        return 1


def handle_quota(client: ConceptioClient) -> int:
    try:
        data = client.quota()
    except ConceptioError as e:
        console.print(f"[bold red][ERR][/] {e}")
        return 1
    tier = data.get("tier") or "public"
    auth_path = data.get("auth") or "public"
    credential = client.api_key or client.license_key
    shown = False
    if credential:
        console.print(f"[bold]Credential:[/] {credential[:8]}...{credential[-4:]} (saved in ~/.conceptio/config.json)")
        shown = True
    if auth_path == "api_key":
        console.print(f"[bold]Auth:[/] [cyan]API key[/] — authenticated agent access.")
    elif auth_path == "license":
        console.print(f"[bold]Auth:[/] [cyan]License key[/]")
    elif auth_path == "firebase":
        console.print(f"[bold]Auth:[/] [cyan]Signed-in account[/]")
    elif shown:
        console.print(f"[yellow]Auth:[/] saved credential not recognized by the API — double-check it.")
    console.print(f"[bold]Tier:[/] [cyan]{tier}[/]")
    if tier == "public":
        console.print("  [dim]Free tier - 50 trial requests included, rate-limited. "
                      "Upgrade at https://conceptio.app or connect a Pro API key.[/]")
    elif tier == "pro":
        console.print("  [green]Pro — higher rate limit. Thank you for supporting the archive![/]")
    elif tier == "institutional":
        console.print("  [green]Institutional — higher rate limit via your institution.[/]")
    return 0


def _is_api_key(key: str) -> bool:
    """API keys are ckey_live_...; everything else is treated as a license key."""
    return key.lower().startswith("ckey_")


def handle_auth(key: str) -> int:
    if not key or len(key) < 8:
        console.print("[bold red][ERR][/] A valid key is required — "
                      "license (CONCEPTIO-XXXX-XXXX-XXXX) or API key (ckey_live_...).")
        return 1
    if _is_api_key(key):
        set_api_key(key)
        console.print(f"[bold green][OK][/] API key saved to ~/.conceptio/config.json")
    else:
        set_license_key(key)
        console.print(f"[bold green][OK][/] License key saved to ~/.conceptio/config.json")
    # Validate against the live API so the user knows immediately if it is accepted.
    client = ConceptioClient()
    try:
        data = client.quota()
        tier = data.get("tier") or "public"
        auth_path = data.get("auth") or "public"
        if tier != "public" or auth_path in ("api_key", "license"):
            console.print(f"[bold green][OK][/] Key accepted - tier: [cyan]{tier}[/]")
        else:
            console.print("[yellow]Key saved but the API still reports the free tier — "
                          "double-check the key (run `conceptio quota` to re-check).[/]")
    except ConceptioError as e:
        console.print(f"[dim]Saved locally; live check failed: {e}[/]")
    return 0


def main(argv: Optional[list] = None) -> int:
    _prepare_stdio()
    parser = argparse.ArgumentParser(
        prog="conceptio",
        description="Conceptio — the document retrieval layer for AI agents. Search 580,000+ "
                    "open-access papers, standards, textbooks and legal documents with "
                    "license-aware access, export citations, and download PDFs. Also runs "
                    "an MCP server so agents can query the archive directly.",
    )
    parser.add_argument("--version", action="version", version=f"conceptio-cli {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    sp = sub.add_parser("search", help="Search the archive (supports source:/lang:/category: directives)")
    sp.add_argument("query", help="Query, e.g. 'attention is all you need' or 'source:nist zero trust'")
    sp.add_argument("-l", "--limit", type=int, default=None, help="Number of results (default: config, 10)")
    sp.add_argument("--offset", type=int, default=0, help="Pagination offset (default: 0)")
    sp.add_argument("-c", "--category", help="Filter by category (e.g. 'Computer Science & Tech')")
    sp.add_argument("--lang", dest="language", help="Filter by language code (e.g. en, it, fr)")
    sp.add_argument("--json", action="store_true", help="Output raw JSON")
    sp.add_argument("--markdown", action="store_true", help="Output markdown (for notes/Obsidian)")

    sp = sub.add_parser(
        "resolve",
        help="Resolve an identifier (RFC 2119, doi:10.xxxx/..., 2604.08499, PMID 41961061, PMC10601397, NIST FIPS 199, w3c_..., US case citation) to a document",
    )
    sp.add_argument("id", help="Identifier to resolve, e.g. 'RFC 2119', 'doi:10.1145/3290605.3300333', or '410 U.S. 113'")
    sp.add_argument("-l", "--limit", type=int, default=10, help="Max results (default: 10)")
    sp.add_argument("--json", action="store_true", help="Output raw JSON")

    sp = sub.add_parser("download", help="Download the direct PDF for a document (by ID or URL)")
    sp.add_argument("target", help="Document ID or direct/canonical URL")
    sp.add_argument("-o", "--output", help="Output PDF path (default: conceptio_<id>.pdf)")

    sp = sub.add_parser("cite", help="Export a citation (11 formats: bibtex/apa/mla/chicago/ieee/harvard/ris/bluebook/oscola/iso690/ansiz39)")
    sp.add_argument("doc_id", type=int, help="Document ID")
    sp.add_argument("-f", "--format", default=None, choices=CITE_FORMATS, help="Citation format (default: config, bibtex)")

    sp = sub.add_parser("info", help="View full metadata for a document")
    sp.add_argument("doc_id", type=int, help="Document ID")

    sp = sub.add_parser("auth", help="Save a Conceptio license key or API key")
    sp.add_argument("key", help="License key (CONCEPTIO-XXXX-XXXX-XXXX) or API key (ckey_live_...)")

    sub.add_parser("quota", help="Show current tier / license status")

    sub.add_parser("mcp", help="Start the stdio Model Context Protocol server for AI agents")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    try:
        if args.command == "search":
            client = ConceptioClient()
            limit = args.limit or int(load_config().get("default_limit", 10))
            data = client.search(
                args.query, limit=limit, offset=args.offset,
                category=args.category, language=args.language,
            )
            if args.json:
                print(json.dumps(data, indent=2))
            elif args.markdown:
                print(to_markdown(data))
            else:
                print_search_results(data, query=args.query)
            return 0

        if args.command == "resolve":
            client = ConceptioClient()
            try:
                data = client.resolve(args.id, limit=args.limit)
            except ConceptioError as e:
                console.print(f"[bold red][ERR][/] {e}")
                return 1
            if args.json:
                print(json.dumps(data, indent=2))
                return 0
            kind = data.get("kind") or "text"
            ident = data.get("identifier") or args.id
            results = data.get("results") or []
            console.print(f"[bold]Identifier:[/] [cyan]{ident}[/]")
            if data.get("kind"):
                console.print(f"[bold]Kind:[/] [magenta]{data['kind']}[/]")
            else:
                console.print("[dim]Unrecognized identifier — falling back to a text search.[/]")
            if not results:
                console.print(f"[yellow]No matching documents in the archive for '{args.id}'.[/]")
                return 0
            console.print(f"[bold]{len(results)} result(s):[/]")
            for i, r in enumerate(results, 1):
                title = r.get("title") or "(untitled)"
                src = r.get("source")
                access = r.get("access_level") or ""
                rid = r.get("id")
                line = f"  {i}. [bold]{title}[/]"
                if rid:
                    line += f"  [dim](id {rid})[/]"
                console.print(line)
                if src:
                    detail = f"     [dim]{src} · {access}[/]"
                    url_hint = r.get("url")
                    if url_hint:
                        detail = f"     [dim]{src} · {access} · {url_hint}[/]"
                    console.print(detail)
            return 0

        if args.command == "download":
            return handle_download(ConceptioClient(), args.target, args.output)

        if args.command == "cite":
            client = ConceptioClient()
            fmt = args.format or load_config().get("default_citation_format", "bibtex")
            try:
                print(client.get_citation(args.doc_id, format=fmt))
            except ConceptioError as e:
                console.print(f"[bold red][ERR][/] {e}")
                return 1
            return 0

        if args.command == "info":
            try:
                print_document_info(ConceptioClient().get_document(args.doc_id))
            except ConceptioError as e:
                console.print(f"[bold red][ERR][/] {e}")
                return 1
            return 0

        if args.command == "auth":
            return handle_auth(args.key)

        if args.command == "quota":
            return handle_quota(ConceptioClient())

        if args.command == "mcp":
            from .mcp_server import run_mcp_server
            return run_mcp_server()

    except ConceptioError as e:
        console.print(f"[bold red][ERR][/] {e}")
        return 1
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
        return 130

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
