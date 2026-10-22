"""Terminal output formatting for the Conceptio CLI (rich)."""

import re
import sys
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.markup import escape as _escape_markup
from rich.style import Style
from rich.table import Table
from rich.text import Text

# ── Corpus text is attacker-influenced data ─────────────────────────────────
# Titles, authors, snippets and URLs come from documents an adversary can
# publish, and they flow into terminal sinks. Three abuses were measured
# against this formatter before the guards below existed:
#   1. markup injection — `[bold red]X[/]` in an author rendered red, and
#      `[link=…]` injected a clickable link into the terminal;
#   2. crash/DoS — a lone `[/]` raised rich's MarkupError and killed the CLI;
#   3. terminal-escape passthrough — `\x1b]52;c;…` (OSC-52, a clipboard
#      write) reached the terminal byte-for-byte through the search table.
# The rule: every untrusted string is CONTROL-STRIPPED (no ESC/OSC, no CR)
# before it is rendered anywhere, and MARKUP-ESCAPED whenever it is
# interpolated into a rich-markup f-string. `literal()` does both; Text
# contexts only need `sanitize_text()` because Text.append treats its input as
# literal (but keeps control codes, hence the strip).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b\u200c\u200d\u2028\u2029\ufeff]")


def sanitize_text(value: Any) -> str:
    """Untrusted text, safe to hand to a terminal: no control codes at all
    (newlines and tabs survive — CR and every escape sequence do not)."""
    return _CONTROL_CHARS.sub("", str(value if value is not None else ""))


def literal(value: Any) -> str:
    """Untrusted text, safe to INTERPOLATE into a rich-markup f-string:
    control-stripped AND markup-escaped, so it renders exactly as data."""
    return _escape_markup(sanitize_text(value))


def link_text(value: Any, color: Optional[str] = None) -> Text:
    """An untrusted URL as a clickable rich Text — the link target is set
    programmatically (`Style(link=…)`), never through the markup grammar, so a
    URL containing `]` cannot close the tag early and inject one. Non-http(s)
    targets render as plain text.

    ``color`` rides the same programmatic Style — a caller colors the link
    without a markup f-string (and per-argument markup is a trap: each string
    argument to ``console.print`` is parsed as its own markup document).
    """
    url = sanitize_text(value).strip()
    style = Style(color=color) if color else Style()
    if url.lower().startswith(("http://", "https://")):
        return Text(url, style=Style(color=color, link=url))
    return Text(url, style=style)


# Markdown export (the `--markdown` surface): corpus text becomes markdown
# STRUCTURE unless escaped — `[`/`]` break or hijack link text, `<img …>` is a
# raw-HTML tracking pixel in most renderers, and backticks fence-break.
_MD_CHARS = re.compile(r"([\\`*_[\]<>])")


def md_text(value: Any) -> str:
    """Untrusted text, safe inside exported markdown: control-stripped and
    with every structural character backslash-escaped (they render as
    themselves, but no longer form links, images, emphasis or raw HTML)."""
    return _MD_CHARS.sub(r"\\\1", sanitize_text(value))


def md_url(value: Any) -> str:
    """Untrusted URL for a markdown link DESTINATION: control-stripped and
    wrapped in `<>` (which tolerates parens/spaces) — `<>` themselves are
    stripped so the wrapper cannot be broken out of."""
    return sanitize_text(value).replace("<", "").replace(">", "").strip()

# In --json mode the CLI is a machine contract: JSON goes to stdout and every
# human message (errors, progress, hints) goes to stderr, so a caller can pipe
# stdout straight into a JSON decoder.
#
# The stream is NOT sniffed from `sys.argv` here. It used to be, and that got
# the question wrong twice: `main(argv)` is the real entry point, so a host that
# embeds the CLI — or a test, or any caller passing its own argument list — was
# judged by an argv it never supplied, and `--json` there left human text on
# stdout next to the payload. `main()` now decides, after parsing, via
# `set_json_mode`.
console = Console(stderr=False)


def set_json_mode(enabled: bool) -> None:
    """Route human output to stderr (machine mode) or stdout (interactive).

    Mutates the shared instance rather than rebinding the name: every module
    imported `console` by value (`from .formatter import console`), so a new
    object would leave the old one printing to the wrong stream.
    """
    console.stderr = bool(enabled)

_HIGHLIGHT_WORDS = re.compile(r"[^\s,+\"'()]+")


def _highlight(text: str, query: Optional[str]) -> Text:
    """Return ``text`` as a rich Text with query words styled gold.

    Control codes are stripped first: ``Text.append`` treats its input as
    literal markup-wise but passes escape bytes through to the terminal."""
    text = sanitize_text(text)
    out = Text()
    words = set()
    if query:
        for w in _HIGHLIGHT_WORDS.findall(sanitize_text(query)):
            if len(w) >= 3 and not w.lower().startswith(("source:", "src:", "lang:", "category:", "cat:")):
                words.add(w.lower())
    if not words:
        out.append(str(text))
        return out
    pattern = re.compile("|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)), re.IGNORECASE)
    pos = 0
    for m in pattern.finditer(str(text)):
        out.append(text[pos : m.start()])
        out.append(m.group(0), style="bold #f5a524")
        pos = m.end()
    out.append(text[pos:])
    return out


def _truncate(text: str, width: int = 72) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= width else text[: width - 3] + "..."


def print_search_results(data: Dict[str, Any], query: Optional[str] = None) -> None:
    """Render a search response as a rich table."""
    if data.get("error"):
        console.print(f"[bold red]{literal(data['error'])}[/]")
        return
    results: List[Dict[str, Any]] = data.get("results") or []
    total = data.get("total", len(results))
    console.print(f"\n[bold]Conceptio -[/] [cyan]{total:,}[/] result{'s' if total != 1 else ''} "
                  f"for [italic]\"{literal(query or data.get('query', ''))}\"[/]")
    if not results:
        console.print("  [dim]Nothing found — try broader keywords, clear source filters, or `conceptio search --help`.[/]")
        return
    table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Year", justify="right", width=5)
    table.add_column("Title", min_width=36, max_width=64)
    table.add_column("Author", max_width=22, overflow="ellipsis")
    table.add_column("Source", max_width=18, overflow="ellipsis")
    table.add_column("PDF", justify="center", width=4)
    for i, r in enumerate(results, start=1):
        title = _highlight(_truncate(r.get("title") or "Untitled", 64), query)
        author = literal(_truncate(r.get("author") or "Unknown", 22))
        source = literal(r.get("source_label") or r.get("source") or "")
        year = literal(r.get("year") or "n.d.")
        pdf = "[bold green][x][/]" if r.get("direct_pdf_url") else "[dim][ ][/]"
        table.add_row(str(i), str(year), title, author, str(source), pdf)
    console.print(table)
    if data.get("offset", 0) + len(results) < total:
        console.print(f"  [dim]More results available — use [bold]--limit[/] or [bold]--offset[/].[/]")
    first_id = literal(results[0].get("id", ""))
    console.print(f"  [dim]Tip: [bold]conceptio download {first_id}[/] saves the PDF, "
                  f"[bold]conceptio cite {first_id}[/] exports a citation.[/]")


def print_document_info(doc: Dict[str, Any]) -> None:
    if not doc or doc.get("error"):
        console.print(f"[bold red]{literal((doc or {}).get('error', 'Document not found.'))}[/]")
        return
    console.print()
    console.print(f"[bold]{literal(doc.get('title') or 'Untitled')}[/]")
    meta = [
        ("ID", doc.get("id")),
        ("Author", doc.get("author") or "Unknown"),
        ("Source", doc.get("source_label") or doc.get("source") or ""),
        ("Category", doc.get("category") or ""),
        ("License", doc.get("license") or ""),
        ("Year", doc.get("year") or "n.d."),
        ("Language", doc.get("language") or "en"),
    ]
    for label, value in meta:
        console.print(f"  [bold cyan]{label}:[/] {literal(value)}")
    if doc.get("url"):
        console.print("  [bold cyan]URL:[/]", link_text(doc["url"]))
    if doc.get("direct_pdf_url"):
        console.print("  [bold cyan]Direct PDF:[/]", link_text(doc["direct_pdf_url"], color="green"))
    if doc.get("description"):
        console.print(f"\n  [bold cyan]Description:[/]\n  {literal(_truncate(doc['description'], 240))}")


def to_markdown(data: Dict[str, Any]) -> str:
    """Render search results as markdown (for --markdown).

    Corpus text is escaped as TEXT (`md_text`) and link targets as
    DESTINATIONS (`md_url`): a title carrying `[`, `<` or backticks must not
    become a link, a raw-HTML embed or a fence break in whatever document the
    user pastes this into.
    """
    if data.get("error"):
        return f"> {md_text(data['error'])}\n"
    lines: List[str] = []
    results: List[Dict[str, Any]] = data.get("results") or []
    lines.append(f"## Conceptio results — {data.get('total', len(results))} found")
    lines.append("")
    for i, r in enumerate(results, start=1):
        title = md_text(r.get("title") or "Untitled").replace("|", "\\|")
        author = md_text(r.get("author") or "Unknown")
        year = md_text(r.get("year") or "n.d.")
        src = md_text(r.get("source_label") or r.get("source") or "")
        lines.append(f"{i}. **{title}** — *{author} ({year})* [{src}]")
        if r.get("snippet"):
            lines.append(f"   > {md_text(_truncate(r.get('snippet') or '', 200))}")
        url = r.get("url") or ""
        pdf = r.get("direct_pdf_url") or ""
        links = []
        if url:
            links.append(f"[source](<{md_url(url)}>)")
        if pdf:
            links.append(f"[PDF](<{md_url(pdf)}>)")
        if links:
            lines.append("   " + " | ".join(links))
        lines.append("")
    return "\n".join(lines)
