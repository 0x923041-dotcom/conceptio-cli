"""Terminal output formatting for the Conceptio CLI (rich)."""

import re
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.table import Table
from rich.text import Text

console = Console()

_HIGHLIGHT_WORDS = re.compile(r"[^\s,+\"'()]+")


def _highlight(text: str, query: Optional[str]) -> Text:
    """Return ``text`` as a rich Text with query words styled gold."""
    out = Text()
    words = set()
    if query:
        for w in _HIGHLIGHT_WORDS.findall(query):
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
        console.print(f"[bold red]{data['error']}[/]")
        return
    results: List[Dict[str, Any]] = data.get("results") or []
    total = data.get("total", len(results))
    console.print(f"\n[bold]Conceptio -[/] [cyan]{total:,}[/] result{'s' if total != 1 else ''} "
                  f"for [italic]\"{query or data.get('query', '')}\"[/]")
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
        author = _truncate(r.get("author") or "Unknown", 22)
        source = r.get("source_label") or r.get("source") or ""
        year = r.get("year") or "n.d."
        pdf = "[bold green][x][/]" if r.get("direct_pdf_url") else "[dim][ ][/]"
        table.add_row(str(i), str(year), title, author, str(source), pdf)
    console.print(table)
    if data.get("offset", 0) + len(results) < total:
        console.print(f"  [dim]More results available — use [bold]--limit[/] or [bold]--offset[/].[/]")
    console.print(f"  [dim]Tip: [bold]conceptio download {results[0].get('id', '')}[/] saves the PDF, "
                  f"[bold]conceptio cite {results[0].get('id', '')}[/] exports a citation.[/]")


def print_document_info(doc: Dict[str, Any]) -> None:
    if not doc or doc.get("error"):
        console.print(f"[bold red]{(doc or {}).get('error', 'Document not found.')}[/]")
        return
    console.print()
    console.print(f"[bold]{doc.get('title') or 'Untitled'}[/]")
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
        console.print(f"  [bold cyan]{label}:[/] {value}")
    if doc.get("url"):
        console.print(f"  [bold cyan]URL:[/] [link={doc['url']}]{doc['url']}[/]")
    if doc.get("direct_pdf_url"):
        console.print(f"  [bold cyan]Direct PDF:[/] [green]{doc['direct_pdf_url']}[/]")
    if doc.get("description"):
        console.print(f"\n  [bold cyan]Description:[/]\n  {_truncate(doc['description'], 240)}")


def to_markdown(data: Dict[str, Any]) -> str:
    """Render search results as markdown (for --markdown)."""
    if data.get("error"):
        return f"> {data['error']}\n"
    lines: List[str] = []
    results: List[Dict[str, Any]] = data.get("results") or []
    lines.append(f"## Conceptio results — {data.get('total', len(results))} found")
    lines.append("")
    for i, r in enumerate(results, start=1):
        title = (r.get("title") or "Untitled").replace("|", "\\|")
        author = r.get("author") or "Unknown"
        year = r.get("year") or "n.d."
        src = r.get("source_label") or r.get("source") or ""
        lines.append(f"{i}. **{title}** — *{author} ({year})* [{src}]")
        if r.get("snippet"):
            lines.append(f"   > {_truncate(r.get('snippet') or '', 200)}")
        url = r.get("url") or ""
        pdf = r.get("direct_pdf_url") or ""
        links = []
        if url:
            links.append(f"[source]({url})")
        if pdf:
            links.append(f"[PDF]({pdf})")
        if links:
            lines.append("   " + " | ".join(links))
        lines.append("")
    return "\n".join(lines)
