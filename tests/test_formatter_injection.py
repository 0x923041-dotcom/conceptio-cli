"""Corpus text is attacker-influenced data, and the terminal is a sink.

Three abuses were measured against the formatter before the guards existed,
each with a corpus-shaped string an adversary can publish:

1. markup injection — ``[bold red]X[/]`` in an author rendered red;
   ``[link=…]`` injected a clickable link into the terminal;
2. crash/DoS — a lone ``[/]`` raised rich's ``MarkupError`` and killed the
   CLI mid-render;
3. terminal-escape passthrough — ``\\x1b]52;c;…`` (OSC-52, a clipboard
   write) reached the terminal byte-for-byte through the search-table Title.

These tests pin the guards in `conceptio_cli.formatter`: every untrusted
string is control-stripped (`sanitize_text`) and, in rich-markup context,
markup-escaped (`literal`), and link targets ride `Style(link=…)` instead of
the markup grammar.

The content assertions run against a NON-terminal console on purpose: rich
emits no ANSI there, so "the tag text is visible in the output" is exactly
"the markup was escaped, not consumed" — no ANSI-sequence guessing.
"""

import io

from rich.console import Console

import conceptio_cli.formatter as formatter


def _capture(fn, *args, **kwargs):
    """Run a formatter function against a plain console, return its output.
    The console is one shared object; restore it afterwards."""
    buf = io.StringIO()
    before = formatter.console
    formatter.console = Console(file=buf, force_terminal=False, width=120)
    try:
        fn(*args, **kwargs)
    finally:
        formatter.console = before
    return buf.getvalue()


def _row(**over):
    r = {"id": "42", "title": "T", "author": "A", "source_label": "S", "year": "2024",
         "direct_pdf_url": None, "url": "https://ok.example/"}
    r.update(over)
    return r


# ── the guards themselves ──────────────────────────────────────────────────


def test_sanitize_text_strips_every_escape_but_keeps_words():
    out = formatter.sanitize_text("T\x1b]52;c;QQ==\x07X\rY\x00Z\nW")
    assert "\x1b" not in out and "\x07" not in out and "\r" not in out and "\x00" not in out
    assert out.replace("\n", "") == "T]52;c;QQ==XYZW"


def test_literal_escapes_the_markup_grammar():
    lit = formatter.literal("[bold red]X[/] and [/]")
    assert "\\[bold red]" in lit and "\\[/]" in lit
    # Byte-deterministic: the same input is the same output.
    assert lit == formatter.literal("[bold red]X[/] and [/]")


def test_link_text_sets_the_target_programmatically_not_via_markup():
    t = formatter.link_text("https://evil.example/] [/link][bold red]PWN")
    assert t.plain == "https://evil.example/] [/link][bold red]PWN"
    assert t.style.link == "https://evil.example/] [/link][bold red]PWN"
    # A non-http target is never a link (no javascript:/obsidian: schemes).
    plain = formatter.link_text("javascript:alert(1)")
    assert getattr(plain.style, "link", None) is None


def test_md_text_neutralises_links_images_and_raw_html():
    out = formatter.md_text("![x](https://evil/p.png) <img src=y> [a](b) `fence`")
    # The structural characters survive as VISIBLE text but every opener is
    # backslash-escaped, so no markdown parser forms a link, image or raw-HTML
    # embed from them.
    assert "!\\[x\\]" in out
    assert "\\<img" in out
    assert "\\`fence\\`" in out


def test_md_url_cannot_break_out_of_its_wrapper():
    # The destination is wrapped by the caller in `<...>`: it must never carry
    # the wrapper's own delimiters.
    assert formatter.md_url("https://e.example/a(b)") == "https://e.example/a(b)"
    assert "<" not in formatter.md_url("https://e.example/x> ][injected")
    assert ">" not in formatter.md_url("https://e.example/x> ][injected")


# ── end to end: the renderers survive hostile corpus rows ─────────────────


def test_a_stray_closing_tag_no_longer_kills_the_cli():
    """The measured DoS: `[/]` in a corpus field raised MarkupError."""
    out = _capture(
        formatter.print_search_results,
        {"total": 1, "query": "q",
         "results": [_row(source_label="[/]", author="[Ed.]", year="[/]")]},
        query="[/]",
    )
    # Rendered as DATA — the tag text is visible instead of being consumed.
    assert "[/]" in out


def test_hostile_corpus_text_expands_no_markup_and_leaks_no_escapes():
    out = _capture(
        formatter.print_search_results,
        {"total": 1, "query": "q",
         "results": [_row(title="T\x1b]52;c;QQ==\x1b\\X",
                          author="[bold red]HACKED[/]",
                          source_label="[link=u]C[/]")]},
        query="[italic]q[/]",
    )
    # The OSC-52 clipboard-write sequence must not reach the terminal.
    assert "\x1b]52" not in out
    # The author's tags are VISIBLE = escaped, not consumed as styling.
    assert "[bold red]HACKED" in out
    # The source's link tag is likewise visible = escaped (kept short so the
    # narrow Source column does not ellipsise the marker), not fired as a link.
    assert "[link=u]C[/]" in out


def test_document_info_survives_hostile_metadata():
    out = _capture(formatter.print_document_info, {
        "id": "1",
        "title": "[bold red]PWN[/]\x1b]0;pwned\x07",
        "author": "[/]",
        "description": "d\x1b]52;c;QQ==\x07esc",
        "url": "https://evil] [/link][bold red]INJECTED",
    })
    assert "\x1b]52" not in out and "\x1b]0;" not in out
    # Title tags visible (escaped), the stray close tag visible too.
    assert "[bold red]PWN" in out and "[/]" in out
    # The URL is printed verbatim as data — no tag grammar fired inside it.
    assert "https://evil] [/link][bold red]INJECTED" in out
    # The description keeps its words, loses its escape (BEL gone, OSC gone).
    assert "d]52;c;QQ==esc" in out


def test_markdown_export_escapes_the_link_text_and_brackets_the_destination():
    out = formatter.to_markdown({
        "total": 1,
        "results": [_row(title="x](https://evil.example) [y",
                         snippet="![track](https://evil.example/p.png)",
                         url="https://ok.example/a(b)")],
    })
    # Every structural character in the title/snippet is backslash-escaped, so
    # no markdown parser forms a link or image from corpus text.
    assert "x\\](https://evil.example) \\[y" in out
    assert "!\\[track\\]" in out
    assert "[source](<https://ok.example/a(b)>)" in out
