"""conceptio — command-line interface for the Conceptio Open Knowledge Archive."""

import argparse
import json
import os
import sys
import webbrowser
from typing import Optional

from . import __version__
from .client import ConceptioClient, ConceptioError, build_obsidian_uri
from .config import AUTH_REQUIRED_HINT, load_config, set_api_key, set_license_key
from .formatter import console, print_document_info, print_search_results, to_markdown

CITE_FORMATS = ["bibtex", "apa", "mla", "chicago", "ieee", "harvard", "ris", "bluebook", "oscola", "iso690", "ansiz39"]

_JOB_POLL_INTERVAL_S = 2.0
_JOB_POLL_MAX_S = 300.0  # hard cap: a stuck job must not hang a terminal


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


def _poll_job(client: ConceptioClient, job_id: str, max_wait: float = _JOB_POLL_MAX_S) -> dict:
    """Poll an asynchronous search job until done/expired/error or max_wait.

    Returns the final snapshot dict (``get_search_job`` shape). Callers decide
    how to render ``result``/``error``.
    """
    import time as _time

    deadline = _time.monotonic() + max_wait
    snapshot: dict = {}
    while True:
        snapshot = client.get_search_job(job_id)
        status = snapshot.get("status") or ""
        if snapshot.get("error") or status in ("done", "expired"):
            return snapshot
        console.print(f"[dim]Job {job_id}: {status}…[/]", end="\r")
        _time.sleep(_JOB_POLL_INTERVAL_S)
        if _time.monotonic() >= deadline:
            return snapshot


def _render_search_batch(data: dict, json_mode: bool, markdown: bool = False) -> None:
    """Render a sync-batch or waited-job envelope ``{count, tier, queries}``."""
    if json_mode:
        print(json.dumps(data, indent=2))
        return
    queries = data.get("queries") or []
    if not queries:
        console.print("[yellow]No query results returned by the batch.[/]")
        return
    for i, item in enumerate(queries, 1):
        q = item.get("query") or item.get("q") or f"query {i}"
        if markdown:
            console.print(f"\n## Query {i}: {q}")
            print(to_markdown(item))
        else:
            console.print(f"\n[bold cyan]── Query {i}:[/] {q}")
            print_search_results(item, query=q)


def _default_output_name(target: str) -> str:
    target = str(target or "").strip()
    if target.isdigit():
        return f"conceptio_{target}.pdf"
    tail = target.rstrip("/").split("/")[-1] or "conceptio.pdf"
    if not tail.lower().endswith(".pdf"):
        tail += ".pdf"
    return tail


def _print_proof_summary(data: dict, doc_id: int) -> None:
    """Render the proof bundle's key fields for a human reader."""
    console.print(f"[bold]Proof bundle:[/] [cyan]document {doc_id}[/]")
    source = data.get("source") or "—"
    source_label = data.get("source_label") or source
    license = data.get("license") or "—"
    content_hash = data.get("content_hash") or "—"
    authority = data.get("authority_score", data.get("authority"))
    retrieved = data.get("retrieved_at") or data.get("retrieved") or "—"
    version = data.get("version_status") or data.get("version") or ""
    console.print(f"  [dim]Source[/]     {source_label}")
    console.print(f"  [dim]License[/]    {license}")
    console.print(f"  [dim]SHA-256[/]    {content_hash}")
    console.print(f"  [dim]Authority[/]  {authority if authority is not None else '—'}")
    console.print(f"  [dim]Retrieved[/]  {retrieved}")
    if version:
        console.print(f"  [dim]Version[/]    {version}")
    snippet = data.get("snippet") or data.get("matched_snippet") or ""
    if snippet:
        console.print(f"  [dim]Snippet[/]    {snippet}")


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


def _connector_error(error: ConceptioError) -> int:
    reason = getattr(error, "reason", "")
    if reason == "zotero_not_configured":
        console.print("[bold red][ERR][/] Configure Zotero in the Conceptio profile before saving.")
    elif reason == "zotero_auth":
        console.print("[bold red][ERR][/] Zotero rejected the saved credentials; reconnect Zotero in the profile.")
    elif reason == "connectors_bulk_pro":
        console.print("[bold red][ERR][/] Bulk connector export is included in the Pro plan.")
    elif reason == "connectors_exhausted":
        console.print("[bold red][ERR][/] The shared connector trial is used up; upgrade to Pro.")
    else:
        console.print(f"[bold red][ERR][/] {error}")
    return 1


def handle_save(client: ConceptioClient, destination: str, doc_id: Optional[int], vault: str = "", all_saved: bool = False) -> int:
    """Save through the public API; entitlement and ownership stay server-side."""
    if all_saved:
        if destination != "zotero":
            console.print("[bold red][ERR][/] --all-saved is currently supported only for Zotero.")
            return 1
        console.print("[bold red][ERR][/] --all-saved requires a JSON file of document ids via --ids.")
        return 1
    if doc_id is None or doc_id < 1:
        console.print("[bold red][ERR][/] A positive document id is required.")
        return 1
    try:
        if destination == "zotero":
            result = client.send_zotero(doc_id)
            status = "Already saved in Zotero." if result.get("idempotent") else "Saved to Zotero."
            console.print(f"[bold green][OK][/] {status}")
        else:
            authorized = client.authorize_obsidian(doc_id)
            uri = build_obsidian_uri(authorized, vault=vault)
            opened = webbrowser.open(uri)
            # A CLI has no reliable callback for a custom URI. Log the handoff
            # after handing it to the OS; the server remains authoritative for
            # entitlement, idempotency, and the trial decrement.
            result = client.log_obsidian(doc_id) if opened else {}
            if opened and result:
                console.print("[bold green][OK][/] Opened metadata in Obsidian.")
            else:
                console.print("[yellow]Obsidian URI generated but was not opened; no save was logged.[/]")
                print(uri)
        return 0
    except ConceptioError as error:
        return _connector_error(error)


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
    trial_remaining = data.get("trial_remaining")
    if tier == "public":
        if isinstance(trial_remaining, int):
            if trial_remaining > 0:
                console.print(f"  [cyan]Free plan: {trial_remaining} of 20 browser credits left[/] — "
                              "your browser and agents share this allowance "
                              "(free keys cannot call the API).")
            else:
                console.print("  [yellow]Free plan: 20-credit allowance used up — "
                              "Conceptio for agents requires the Dev plan "
                              "(EUR 19.99/month, 3,500 credits/month) at conceptio.app.[/]")
        else:
            console.print("  [dim]Free plan — sign in on conceptio.app to mint an API key "
                          "for your agents (all searches share one allowance).[/]")
    elif tier in ("pro", "dev"):
        # Monthly credits are the canonical period; the API still aliases the
        # weekly fields for older clients, so fall back to them defensively.
        limit = data.get("monthly_credit_limit") if data.get("monthly_credit_limit") is not None else data.get("weekly_search_limit")
        used = data.get("monthly_credit_used") if data.get("monthly_credit_used") is not None else data.get("weekly_search_used")
        remaining = data.get("monthly_credit_remaining") if data.get("monthly_credit_remaining") is not None else data.get("weekly_search_remaining")
        reset_at = data.get("monthly_reset_at") or data.get("weekly_reset_at")
        label = "Dev plan" if tier == "dev" else "Pro plan"
        if limit is not None and used is not None:
            console.print(
                f"  [green]{label}: {used} of {limit} monthly credits used[/] "
                f"({remaining} remaining)."
            )
            if reset_at:
                console.print(f"  [dim]Quota resets: {reset_at} (UTC calendar month)[/]")
            console.print("  [dim]Throughput: 60 req/min[/]")
        else:
            console.print(f"  [green]{label} — thank you for supporting the archive![/]")
    elif tier in ("enterprise", "institutional"):
        console.print("  [green]Enterprise — unlimited searches via your organization.[/]")
    return 0


def _is_api_key(key: str) -> bool:
    """API keys are ckey_live_...; everything else is treated as a license key."""
    return key.lower().startswith("ckey_")


def require_auth() -> bool:
    """Refuse keyless usage: every data command needs a credential.

    Returns True when an API key or license key is configured (config file
    or the CONCEPTIO_API_KEY / CONCEPTIO_LICENSE_KEY environment variables),
    else prints how to authenticate and returns False. Deliberately
    client-side: the public API tier still serves browsers; this gate keeps
    the CLI and MCP server behind authentication.
    """
    cfg = load_config()
    if (
        str(cfg.get("api_key") or "").strip()
        or str(cfg.get("license_key") or "").strip()
        or str(cfg.get("bearer_token") or "").strip()
        or os.environ.get("CONCEPTIO_API_KEY", "").strip()
        or os.environ.get("CONCEPTIO_LICENSE_KEY", "").strip()
        or os.environ.get("CONCEPTIO_BEARER_TOKEN", "").strip()
    ):
        return True
    console.print(f"[bold red][ERR][/] {AUTH_REQUIRED_HINT}")
    return False


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
            tr = data.get("trial_remaining")
            if tier == "public" and isinstance(tr, int):
                console.print(f"[dim]Free plan: {tr} of 20 browser credits remaining — free keys "
                              "cannot call the API; agents require the Dev plan "
                              "(https://www.conceptio.app/pricing).[/]")
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
        description="Conceptio — the document retrieval layer for AI agents. Search the "
                    "open-access archive of papers, standards, textbooks and legal documents with "
                    "license-aware access, export citations, and download PDFs. Requires an API key "
                    "(`conceptio auth`) — sign in at https://www.conceptio.app to get one. Also runs "
                    "an MCP server so agents can query the archive directly.",
    )
    parser.add_argument("--version", action="version", version=f"conceptio-cli {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    sp = sub.add_parser("search", help="Search the archive (supports source:/lang:/category: directives)")
    sp.add_argument("query", nargs="?", help="Query, e.g. 'attention is all you need' or 'source:nist zero trust'")
    sp.add_argument("--batch", metavar="QUERIES_JSON", help="1–50 query objects from a JSON file (queued by default; --sync runs ≤10 immediately)")
    sp.add_argument("--sync", action="store_true", help="With --batch: run queries through the synchronous batch endpoint (1–10)")
    sp.add_argument("--wait", action="store_true", help="With --batch: block until the queued job completes, then print its results")
    sp.add_argument("-l", "--limit", type=int, default=None, help="Number of results (default: config, 10)")
    sp.add_argument("--offset", type=int, default=0, help="Pagination offset (default: 0)")
    sp.add_argument("-c", "--category", help="Filter by category (e.g. 'Computer Science & Tech')")
    sp.add_argument("--lang", dest="language", help="Filter by language code (e.g. en, it, fr)")
    sp.add_argument("--license", choices=("commercial-ok",), help="Require an explicit commercial-use license")
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
    sp.add_argument("--json", action="store_true", help="Output raw JSON (human text goes to stderr)")

    sp = sub.add_parser("proof", help="Fetch the machine-readable evidence bundle for a document")
    sp.add_argument("doc_id", type=int, help="Document ID")
    sp.add_argument("-q", "--query", default="", help="Passage-level proof: matched snippet plus surrounding context")
    sp.add_argument("--json", action="store_true", help="Output raw JSON (human text goes to stderr)")

    sp = sub.add_parser("auth", help="Save a Conceptio license key or API key")
    sp.add_argument("key", help="License key (CONCEPTIO-XXXX-XXXX-XXXX) or API key (ckey_live_...)")

    sub.add_parser("quota", help="Show current tier / license status")

    sp = sub.add_parser("save", help="Save one document to Zotero or Obsidian")
    sp.add_argument("--to", dest="destination", required=True, choices=("zotero", "obsidian"), help="Connector destination")
    sp.add_argument("doc_id", nargs="?", type=int, help="Conceptio document id")
    sp.add_argument("--vault", default="", help="Obsidian vault name (sanitized before handoff)")
    sp.add_argument("--all-saved", action="store_true", help="Bulk-save document ids from --ids to Zotero (Pro only)")
    sp.add_argument("--ids", metavar="IDS_JSON", help="JSON list or {\"doc_ids\": [...]} for --all-saved")

    sp = sub.add_parser("search-job", help="Poll an asynchronous search job")
    sp.add_argument("job_id", help="Job id returned by `conceptio search --batch queries.json --json`")
    sp.add_argument("--wait", action="store_true", help="Poll until the job completes, then print its results")
    sp.add_argument("--json", action="store_true", help="Output raw JSON")

    sub.add_parser("mcp", help="Start the stdio Model Context Protocol server for AI agents")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    try:
        if args.command == "search":
            if not require_auth():
                return 1
            client = ConceptioClient()
            if args.batch:
                try:
                    with open(args.batch, "r", encoding="utf-8") as source:
                        queries = json.load(source)
                except (OSError, ValueError) as e:
                    console.print(f"[bold red][ERR][/] Could not read batch JSON: {e}")
                    return 1
                if isinstance(queries, dict):
                    queries = queries.get("queries")
                if not isinstance(queries, list):
                    console.print("[bold red][ERR][/] Batch JSON must be a list or an object with a `queries` list.")
                    return 1
                try:
                    if args.sync:
                        if len(queries) > 10:
                            console.print("[bold red][ERR][/] --sync batches support at most 10 queries; drop --sync to queue up to 50.")
                            return 1
                        data = client.batch_search(queries)
                        _render_search_batch(data, args.json, markdown=args.markdown)
                        return 0
                    data = client.submit_search_job(queries)
                    if data.get("error"):
                        console.print(f"[bold red][ERR][/] {data['error']}")
                        return 1
                    if args.wait:
                        try:
                            final = _poll_job(client, data.get("id", ""))
                        except ConceptioError as e:
                            console.print(f"[bold red][ERR][/] {e}")
                            return 1
                        if final.get("error"):
                            console.print(f"[bold red][ERR][/] {final['error']}")
                            return 1
                        if final.get("status") == "expired":
                            console.print("[bold red][ERR][/] Search job expired before completion — submit it again.")
                            return 1
                        _render_search_batch(final.get("result") or {}, args.json, markdown=args.markdown)
                        return 0
                    print(json.dumps(data, indent=2) if args.json else
                          f"Queued search job {data.get('id', '(unknown)')} — poll with `conceptio search-job {data.get('id', '')}`")
                    return 0
                except ConceptioError as e:
                    console.print(f"[bold red][ERR][/] {e}")
                    return 1
            if not args.query:
                console.print("[bold red][ERR][/] A query or --batch JSON file is required.")
                return 1
            limit = args.limit or int(load_config().get("default_limit", 10))
            search_kwargs = {
                "limit": limit,
                "offset": args.offset,
                "category": args.category,
                "language": args.language,
            }
            if args.license:
                search_kwargs["license"] = args.license
            data = client.search(args.query, **search_kwargs)
            if args.json:
                print(json.dumps(data, indent=2))
            elif args.markdown:
                print(to_markdown(data))
            else:
                print_search_results(data, query=args.query)
            return 0

        if args.command == "resolve":
            if not require_auth():
                return 1
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
            if not require_auth():
                return 1
            return handle_download(ConceptioClient(), args.target, args.output)

        if args.command == "cite":
            if not require_auth():
                return 1
            client = ConceptioClient()
            fmt = args.format or load_config().get("default_citation_format", "bibtex")
            try:
                print(client.get_citation(args.doc_id, format=fmt))
            except ConceptioError as e:
                console.print(f"[bold red][ERR][/] {e}")
                return 1
            return 0

        if args.command == "info":
            if not require_auth():
                return 1
            try:
                doc = ConceptioClient().get_document(args.doc_id)
                if args.json:
                    print(json.dumps(doc, indent=2))
                else:
                    print_document_info(doc)
            except ConceptioError as e:
                console.print(f"[bold red][ERR][/] {e}")
                return 1
            return 0

        if args.command == "proof":
            if not require_auth():
                return 1
            try:
                proof = ConceptioClient().get_proof(args.doc_id, query=args.query or None)
                if args.json:
                    print(json.dumps(proof, indent=2))
                else:
                    _print_proof_summary(proof, args.doc_id)
            except ConceptioError as e:
                console.print(f"[bold red][ERR][/] {e}")
                return 1
            return 0

        if args.command == "auth":
            return handle_auth(args.key)

        if args.command == "quota":
            return handle_quota(ConceptioClient())

        if args.command == "save":
            if not require_auth():
                return 1
            if args.all_saved:
                if not args.ids:
                    console.print("[bold red][ERR][/] --all-saved requires --ids path.")
                    return 1
                try:
                    with open(args.ids, "r", encoding="utf-8") as source:
                        ids_payload = json.load(source)
                    doc_ids = ids_payload.get("doc_ids") if isinstance(ids_payload, dict) else ids_payload
                    if not isinstance(doc_ids, list):
                        raise ValueError("expected a JSON list or an object with doc_ids")
                    result = ConceptioClient().send_zotero_all(doc_ids)
                    console.print(f"[bold green][OK][/] Saved {result.get('total', len(doc_ids))} document(s) to Zotero.")
                    return 0
                except (OSError, ValueError, ConceptioError) as error:
                    if isinstance(error, ConceptioError):
                        return _connector_error(error)
                    console.print(f"[bold red][ERR][/] Could not read --ids JSON: {error}")
                    return 1
            return handle_save(
                ConceptioClient(), args.destination, args.doc_id,
                vault=args.vault, all_saved=False,
            )

        if args.command == "search-job":
            if not require_auth():
                return 1
            try:
                if args.wait:
                    data = _poll_job(ConceptioClient(), args.job_id)
                else:
                    data = ConceptioClient().get_search_job(args.job_id)
            except ConceptioError as e:
                console.print(f"[bold red][ERR][/] {e}")
                return 1
            if args.wait:
                if data.get("error"):
                    console.print(f"[bold red][ERR][/] {data['error']}")
                    return 1
                if data.get("status") == "expired":
                    console.print("[bold red][ERR][/] Search job expired before completion — submit it again.")
                    return 1
                _render_search_batch(data.get("result") or {}, args.json)
                return 0
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                status = data.get("status", "unknown")
                console.print(f"[bold]Job {data.get('id', args.job_id)}:[/] [cyan]{status}[/]")
                if data.get("result") is not None:
                    print(json.dumps(data["result"], indent=2))
                if data.get("error"):
                    console.print(f"[bold red][ERR][/] {data['error']}")
            return 0

        if args.command == "mcp":
            if not require_auth():
                # stdout must stay pure JSON-RPC for the agent host — the
                # guidance goes to stderr instead of the console.
                print(AUTH_REQUIRED_HINT, file=sys.stderr)
                return 1
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
