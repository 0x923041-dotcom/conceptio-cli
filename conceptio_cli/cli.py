"""conceptio — command-line interface for the Conceptio Open Knowledge Archive."""

import argparse
import json
import os
import sys
import webbrowser
from typing import Any, Optional

from . import __version__
from .client import ConceptioClient, ConceptioError, build_obsidian_uri
from .config import AUTH_REQUIRED_HINT, has_credential, load_config, set_api_key, set_license_key
from .formatter import (
    console,
    print_document_info,
    print_search_results,
    set_json_mode,
    to_markdown,
)

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


class SearchJobTimeout(ConceptioError):
    """A queued job was still running when the wait budget ran out.

    Distinct from a job that failed, and distinct from one that expired — it is
    unfinished, not done, and the caller must be able to say so.
    """

    def __init__(self, job_id: str, status: str, waited_s: float):
        super().__init__(
            f"Job {job_id} was still '{status or 'unknown'}' after {int(waited_s)}s — "
            f"it is still running server-side. Poll it again with "
            f"`conceptio search-job {job_id} --wait`, or raise the budget with --timeout."
        )
        self.job_id = job_id
        self.status = status


def _poll_job(
    client: ConceptioClient,
    job_id: str,
    max_wait: float = _JOB_POLL_MAX_S,
    interval: Optional[float] = None,
) -> dict:
    """Poll an asynchronous search job until done/expired/error or ``max_wait``.

    Returns the final snapshot dict (``get_search_job`` shape). Callers decide
    how to render ``result``/``error``.

    A wait that runs out **raises** ``SearchJobTimeout`` instead of returning the
    last snapshot. Returning it made a still-running job indistinguishable from
    a finished one with no results: `search --batch … --wait` printed "No query
    results returned by the batch." and exited **0** after five minutes of
    polling — a failure that reads as success, which is the one outcome a
    machine consumer cannot recover from.
    """
    import time as _time

    if interval is None:
        # Resolved at call time, not bound as a default, so a caller (or a test)
        # that patches the module constant actually changes the cadence.
        interval = _JOB_POLL_INTERVAL_S
    started = _time.monotonic()
    deadline = started + max_wait
    snapshot: dict = {}
    while True:
        snapshot = client.get_search_job(job_id)
        status = snapshot.get("status") or ""
        if snapshot.get("error") or status in ("done", "expired"):
            return snapshot
        console.print(f"[dim]Job {job_id}: {status}…[/]", end="\r")
        _time.sleep(interval if interval and interval > 0 else _JOB_POLL_INTERVAL_S)
        if _time.monotonic() >= deadline:
            raise SearchJobTimeout(job_id, str(status), _time.monotonic() - started)


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


def _wait_budget(value: Optional[float]) -> float:
    """The `--wait` polling budget in seconds, validated.

    `--timeout` exists because 300s is a guess about someone else's job queue:
    a script that must not block that long can say so, and a script that can
    wait longer need not poll by hand.
    """
    if value is None:
        return _JOB_POLL_MAX_S
    if value <= 0:
        raise ConceptioError("--timeout must be a positive number of seconds.")
    return float(value)


def _default_output_name(target: str) -> str:
    target = str(target or "").strip()
    if target.isdigit():
        return f"conceptio_{target}.pdf"
    tail = target.rstrip("/").split("/")[-1] or "conceptio.pdf"
    if not tail.lower().endswith(".pdf"):
        tail += ".pdf"
    return tail


# The server's access levels, rendered with the same short words the editor
# clients use, so one vocabulary reaches a reader across the CLI and the
# extensions.
_ACCESS_LABELS = {
    "public_full_text": "Full text",
    "open_access": "Open access",
    "metadata_only": "Metadata only",
}


def _access_line(access_level: Any, full_text_available: Any) -> str:
    """One line answering `can this caller actually have the text?`

    ``access_level`` is the server's licence verdict for the source;
    ``full_text_available`` is what this caller may receive for it, recomputed
    against that verdict — so a metadata-only source reports ``False`` even when
    its row holds extracted text. When the two disagree the row's own text is
    missing (a scanned PDF) while the licence would have permitted it, which is
    exactly the case a reader would otherwise misread as a licence problem.
    """
    level = str(access_level or "").strip()
    label = _ACCESS_LABELS.get(level, level or "—")
    if full_text_available is False and level in ("public_full_text", "open_access"):
        return f"{label} (nothing extracted for this row)"
    return label


def _revision_line(version: Any) -> str:
    """``version_status`` is ``null`` until a row is revised, and an object once
    it is (``{current_since, superseded_sha256}``). Printing the object raw
    leaks a dict repr into a human summary, so every field is named.
    """
    if isinstance(version, dict):
        since = str(version.get("current_since") or "").strip()
        superseded = str(version.get("superseded_sha256") or "").strip()
        parts = []
        if since:
            parts.append(f"revised {since}")
        if superseded:
            parts.append(f"supersedes {superseded[:16]}"
                         + ("…" if len(superseded) > 16 else ""))
        return " · ".join(parts)
    return str(version or "").strip()


def _print_proof_summary(data: dict, doc_id: int) -> None:
    """Render the proof bundle's key fields for a human reader.

    The bundle is nested: the server keeps the document's identity under
    ``document`` and the matched passage under ``passage``.

      {document: {id, title, author, source, source_label, category, url,
                  source_id},
       retrieved_at, content_hash, license, access_level, publisher,
       authority_score, full_text_available, citation,
       passage: {snippet, context}, version_status, jurisdiction,
       standard_status}

    Reading only the top level (what this did) printed ``Source —`` and dropped
    the passage a ``-q`` proof was fetched for — a summary that looks complete
    and withholds the one thing the reader asked for. The flat spelling stays
    accepted so a bundle from an older server keeps rendering.
    """
    document = data.get("document") if isinstance(data.get("document"), dict) else {}
    passage = data.get("passage") if isinstance(data.get("passage"), dict) else {}

    def text(*candidates: object) -> str:
        """First non-empty candidate, else an em dash."""
        for candidate in candidates:
            if candidate is not None and str(candidate).strip():
                return str(candidate)
        return "—"

    source_label = text(
        document.get("source_label"), data.get("source_label"),
        document.get("source"), data.get("source"),
    )
    license_name = text(data.get("license"), document.get("license"))
    content_hash = text(data.get("content_hash"))
    authority = data.get("authority_score", data.get("authority"))
    retrieved = text(data.get("retrieved_at"), data.get("retrieved"))
    version = _revision_line(data.get("version_status") or data.get("version") or "")
    access = _access_line(data.get("access_level", document.get("access_level")),
                          data.get("full_text_available"))
    snippet = text(passage.get("snippet"), data.get("snippet"), data.get("matched_snippet"))
    context = text(passage.get("context"))

    console.print(f"[bold]Proof bundle:[/] [cyan]document {doc_id}[/]")
    console.print(f"  [dim]Source[/]     {source_label}")
    console.print(f"  [dim]License[/]    {license_name}")
    # The hash is 71 characters; with the label it overflows an 80-column
    # terminal, and a wrapped value loses its indentation — the one line a
    # reader copies to verify the bundle arrives split in two. Let it overflow
    # instead of folding.
    console.print(f"  [dim]SHA-256[/]    {content_hash}", no_wrap=True)
    console.print(f"  [dim]Access[/]     {access}")
    console.print(f"  [dim]Authority[/]  {authority if authority is not None else '—'}")
    console.print(f"  [dim]Retrieved[/]  {retrieved}")
    if version:
        console.print(f"  [dim]Version[/]    {version}")
    if snippet != "—":
        console.print(f"  [dim]Snippet[/]    {snippet}")
    if context != "—":
        console.print(f"  [dim]Context[/]    {context}")


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


def credential_hint(client: ConceptioClient) -> Optional[str]:
    """One sentence naming the effective credential and where it came from.

    Only a prefix and a suffix are ever printed. The location is *measured* from
    the client's resolution order, not assumed: a key supplied by
    `CONCEPTIO_API_KEY` was being reported as "saved in ~/.conceptio/config.json"
    even when no config file existed, which is exactly the documented CI/editor
    path and the one case where the sentence was false.
    """
    credential = client.api_key or client.license_key
    if not credential:
        return None
    preview = f"{credential[:8]}...{credential[-4:]}"
    origin = getattr(client, "credential_origin", "")
    if origin == "environment":
        name = getattr(client, "credential_env_var", "") or "an environment variable"
        return f"{preview} (from the {name} environment variable)"
    if origin == "argument":
        return f"{preview} (supplied by the calling process)"
    return f"{preview} (saved in ~/.conceptio/config.json)"


def handle_quota(client: ConceptioClient, as_json: bool = False) -> int:
    try:
        data = client.quota()
    except ConceptioError as e:
        console.print(f"[bold red][ERR][/] {e}")
        return 1
    if as_json:
        # Machine consumers (editor extensions, status surfaces) render the tier
        # themselves. The credential hint is human text, so it goes to stderr and
        # stdout stays pure JSON — the same contract as `info`/`proof --json`.
        hint = credential_hint(client)
        if hint:
            print(f"Credential: {hint}", file=sys.stderr)
        print(json.dumps(data, indent=2))
        return 0
    tier = data.get("tier") or "public"
    auth_path = data.get("auth") or "public"
    shown = False
    hint = credential_hint(client)
    if hint:
        console.print(f"[bold]Credential:[/] {hint}")
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

    Returns True when an API key, license key or signed-in bearer token is
    configured (config file or the CONCEPTIO_* environment variables), else
    prints how to authenticate and returns False. Deliberately client-side: the
    public API tier still serves browsers; this gate keeps the CLI and MCP
    server behind authentication.

    The predicate is shared with the MCP server (`config.has_credential`) so
    the entry gate and the per-call gate can never disagree again — and the
    config is loaded through this module's own `load_config`, which is the seam
    hosts and tests substitute.
    """
    if has_credential(load_config()):
        return True
    console.print(f"[bold red][ERR][/] {AUTH_REQUIRED_HINT}")
    return False


def handle_auth(key: str) -> int:
    if not key or len(key) < 8:
        console.print("[bold red][ERR][/] A valid key is required — "
                      "license (CONCEPTIO-XXXX-XXXX-XXXX) or API key (ckey_live_...).")
        return 1
    is_api = _is_api_key(key)
    if is_api:
        set_api_key(key)
        console.print(f"[bold green][OK][/] API key saved to ~/.conceptio/config.json")
    else:
        set_license_key(key)
        console.print(f"[bold green][OK][/] License key saved to ~/.conceptio/config.json")
    # An exported variable wins over the file, so a user who just saved a key
    # can be shadowed by one they forgot about — say so instead of leaving them
    # to wonder why `conceptio quota` still reports the old tier. The bearer
    # token is named too: it outranks both key kinds, so it is the variable most
    # able to shadow the key being saved right now.
    shadows = [
        name for name in (
            "CONCEPTIO_BEARER_TOKEN",
            "CONCEPTIO_API_KEY" if is_api else "CONCEPTIO_LICENSE_KEY",
        )
        if os.environ.get(name, "").strip() and os.environ.get(name, "").strip() != key
    ]
    if shadows:
        console.print(f"[yellow]Note:[/] {' and '.join(shadows)} "
                      f"{'is' if len(shadows) == 1 else 'are'} set in this environment and "
                      "take precedence over the saved key for commands you run here.")
    # Validate against the live API so the user knows immediately if it is accepted.
    # The key just supplied is the subject of the test — asking a client that
    # resolves environment-first would validate whatever it found there instead.
    client = ConceptioClient(api_key=key if is_api else "", license_key="" if is_api else key)
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


def build_parser() -> argparse.ArgumentParser:
    """The CLI's grammar, built once and introspectable.

    Separate from `main` so the shipped command surface can be *tested* rather
    than described: the editor extensions are clients of this grammar (they
    exec the binary with an argv list), so a rename or a dropped flag breaks a
    shipped extension silently. `tests/test_cli_contract.py` parses the argv
    each client sends against this parser; `main` only reads from it.
    """
    parser = argparse.ArgumentParser(
        prog="conceptio",
        description="Conceptio — the document retrieval layer for AI agents. Search 1M+ "
                    "open-access documents (papers, standards, textbooks, case law) with "
                    "license-aware access, export citations, and download PDFs. Requires an API key "
                    "(`conceptio auth`) — sign in at https://www.conceptio.app to get one. Also runs "
                    "an MCP server so agents can query the archive directly.",
    )
    parser.add_argument("--version", action="version", version=f"conceptio-cli {__version__}")
    # The same switch is accepted before the subcommand as well as after it.
    # `conceptio --json search …` is how a machine caller naturally writes it,
    # and refusing that spelling taught nothing — it only made the caller learn
    # this CLI's particular grammar. The parsed value is folded into each
    # command's own `json` below, so every handler keeps one flag to read.
    parser.add_argument("--json", dest="json_global", action="store_true",
                        help="Output raw JSON (same as each command's own --json)")
    sub = parser.add_subparsers(dest="command", metavar="command")

    sp = sub.add_parser("search", help="Search the archive (supports source:/lang:/category: directives)")
    sp.add_argument("query", nargs="?", help="Query, e.g. 'attention is all you need' or 'source:nist zero trust'")
    sp.add_argument("--batch", metavar="QUERIES_JSON", help="1–50 query objects from a JSON file (queued by default; --sync runs ≤10 immediately)")
    sp.add_argument("--sync", action="store_true", help="With --batch: run queries through the synchronous batch endpoint (1–10)")
    sp.add_argument("--wait", action="store_true", help="With --batch: block until the queued job completes, then print its results")
    sp.add_argument("--timeout", type=float, default=None, metavar="SECONDS", help="With --wait: give up after this long (default: 300)")
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

    sp = sub.add_parser("quota", help="Show current tier / license status")
    sp.add_argument("--json", action="store_true", help="Output raw JSON (human text goes to stderr)")

    sp = sub.add_parser("save", help="Save one document to Zotero or Obsidian")
    sp.add_argument("--to", dest="destination", required=True, choices=("zotero", "obsidian"), help="Connector destination")
    sp.add_argument("doc_id", nargs="?", type=int, help="Conceptio document id")
    sp.add_argument("--vault", default="", help="Obsidian vault name (sanitized before handoff)")
    sp.add_argument("--all-saved", action="store_true", help="Bulk-save document ids from --ids to Zotero (Pro only)")
    sp.add_argument("--ids", metavar="IDS_JSON", help="JSON list or {\"doc_ids\": [...]} for --all-saved")

    sp = sub.add_parser("search-job", help="Poll an asynchronous search job")
    sp.add_argument("job_id", help="Job id returned by `conceptio search --batch queries.json --json`")
    sp.add_argument("--wait", action="store_true", help="Poll until the job completes, then print its results")
    sp.add_argument("--timeout", type=float, default=None, metavar="SECONDS", help="With --wait: give up after this long (default: 300)")
    sp.add_argument("--json", action="store_true", help="Output raw JSON")

    sub.add_parser("mcp", help="Start the stdio Model Context Protocol server for AI agents")

    return parser


def main(argv: Optional[list] = None) -> int:
    _prepare_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "json_global", False):
        args.json = True
    # Where human output goes is decided from the PARSED arguments, not a sniff
    # of sys.argv at import: `main(argv)` is the real entry point, so a host
    # embedding the CLI passes its own list. `--json` means stdout is a machine
    # contract — errors, hints and progress must not land in it.
    set_json_mode(bool(getattr(args, "json", False)))
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
                        if data.get("error"):
                            console.print(f"[bold red][ERR][/] {data['error']}")
                            return 1
                        _render_search_batch(data, args.json, markdown=args.markdown)
                        return 0
                    data = client.submit_search_job(queries)
                    if data.get("error"):
                        console.print(f"[bold red][ERR][/] {data['error']}")
                        return 1
                    if args.wait:
                        try:
                            final = _poll_job(
                                client, data.get("id", ""), max_wait=_wait_budget(args.timeout)
                            )
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
            if data.get("error"):
                # A rate-limit/quota error dict must fail loudly (exit 1) so
                # machine consumers (editors, agent bridges) never mistake it
                # for an empty result set.
                console.print(f"[bold red][ERR][/] {data['error']}")
                return 1
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
            if data.get("error"):
                console.print(f"[bold red][ERR][/] {data['error']}")
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
            return handle_quota(ConceptioClient(), as_json=args.json)

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
                    data = _poll_job(
                        ConceptioClient(), args.job_id, max_wait=_wait_budget(args.timeout)
                    )
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
            # stdout is JSON-RPC for the whole process lifetime: every human
            # byte goes to stderr, including rich's.
            set_json_mode(True)
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
    except BrokenPipeError:
        # `conceptio search … | head` closes stdout early. That is normal use,
        # not an error: silence the stream so the interpreter's final flush
        # cannot raise again, and exit with the conventional SIGPIPE status.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, sys.stdout.fileno())
            finally:
                os.close(devnull)
        except (OSError, ValueError, AttributeError):
            pass
        return 141
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
        return 130

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
