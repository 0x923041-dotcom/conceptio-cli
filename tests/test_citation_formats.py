"""The 11 citation formats are enumerated in nine places, and nothing compared them.

`conceptio cite -f <format>` accepts one list; every other surface carries its
own copy of it, because a client repo cannot import this one and the backend
cannot import a client. So the list is duplicated by necessity — a settings
dropdown, a webview `<select>`, the MCP tool schema, the server's own validation
regex — and a 12th format added in one place is a one-place change that silently
does not reach the other eight. The user-visible shape of that bug: a format the
API renders perfectly, missing from the list a person can pick.

This file is the comparison. `canonical()` is this repo's own `CITE_FORMATS`
(what `--format` validates against); every other carrier must agree with it.

**The invariant is the SET, not the order** — measured, not assumed. Raycast's
settings dropdown opens with `apa` while its `types.ts` array opens with
`bibtex`, and the website's `CITE_FORMATS` opens with `apa` while this CLI's list
opens with `bibtex`. Those are presentation choices in different places (a
default-first dropdown, a documented order in `--help`), so pinning the sequence
would fail on a legitimate edit; pinning the membership is what actually breaks
a user. Membership is therefore asserted, and order differences are reported as
information, never as a failure.

Two rules carried from the checks next door, because they are what make this one
worth having:

* **Absence is a skip, a carrier that is present but unparseable is a failure.**
  A checkout that is not there cannot be compared — but a file that embeds one of
  these lists and yields nothing means the *extractor* went blind, which is a
  broken instrument rather than a correct document, and passing quietly there is
  how a parity check rots into a no-op.
* **The extractors are themselves tested** (`test_extractors_*`), against literal
  fixtures, including one whose only job is to prove a mismatch is detected. An
  extractor that returns `[]` for everything would otherwise satisfy every
  equality assertion in the file.
"""

import json
import re
from pathlib import Path

import pytest

from conceptio_cli.cli import CITE_FORMATS

STACK = Path(__file__).resolve().parent.parent.parent
REPO = Path(__file__).resolve().parent.parent


# ── extractors ────────────────────────────────────────────────────────────────
# Each returns a list of format names, or [] when it could not find its subject.
# A list is the only shape a carrier may produce — never a set — so the
# comparison can report a difference a reader can act on.

_QUOTED = re.compile(r"""["']([A-Za-z0-9_-]+)["']""")


def extract_identifier_array(text, identifier):
    """`<identifier> … = [ "a", "b" ]` as `['a', 'b']`.

    The `=` is load-bearing: `export const CITATION_FORMATS: CitationFormat[] = [`
    contains a `[]` in the *type annotation*, so taking the first bracket after
    the name would read an empty array out of a file that is not empty.
    """
    start = text.find(identifier)
    if start == -1:
        return []
    eq = text.find("=", start)
    if eq == -1:
        return []
    opening = text.find("[", eq)
    closing = text.find("]", opening)
    if opening == -1 or closing == -1:
        return []
    return _QUOTED.findall(text[opening + 1:closing])


def extract_json_enum_in_source(text):
    """The `"enum": [ ... ]` of a JSON-shaped literal embedded in source.

    Used for the MCP server's tool schema, which is a Python dict — `json.loads`
    cannot read the file, so the one flat `enum` list is located textually.
    """
    matches = re.findall(r'"enum"\s*:\s*\[([^\]]*)\]', text)
    for body in matches:
        found = _QUOTED.findall(body)
        if "bibtex" in found:
            return found
    return []


def extract_dict_keys(text, identifier):
    """The keys of `<identifier> = { "a": …, "b": … }` (one line by convention)."""
    start = text.find(identifier)
    if start == -1:
        return []
    opening = text.find("{", start)
    closing = text.find("}", opening)
    if opening == -1 or closing == -1:
        return []
    return re.findall(r'"([A-Za-z0-9_-]+)"\s*:', text[opening + 1:closing])


def extract_regex_alternation(text, prefix):
    """`prefix="^(a|b|c)$"` as `['a', 'b', 'c']`.

    The server validates `format` with a single FastAPI `pattern=`; that regex is
    the authority a request actually hits, so it is compared like any other copy.
    """
    start = text.find(prefix)
    if start == -1:
        return []
    body_start = text.find('"^(', start)
    if body_start == -1:
        return []
    body_end = text.find(')$"', body_start)
    if body_end == -1:
        return []
    return [part for part in text[body_start + 3:body_end].split("|") if part]


def extract_prose_formats(text, marker):
    """A comma-separated run of formats in documentation, after `marker`.

    The Neovim plugin holds no list in code — it delegates — but its README and
    its `:help` page both enumerate the formats for the reader, so those are
    user-facing claims like any dropdown. Deliberately narrow: the run ends at
    the first sentence stop, `(default)` and backticks are stripped, and only
    lowercase alphanumeric tokens survive, so a directive name or a `<doc-id>`
    beside the list cannot be mistaken for a format.
    """
    start = text.find(marker)
    if start == -1:
        return []
    body = text[start + len(marker):]
    stop = re.search(r"\.(?:\s|$)", body)
    if stop:
        body = body[:stop.start()]
    body = re.sub(r"\([^)]*\)", "", body)
    return [
        token.strip().strip("`").strip()
        for token in body.split(",")
        if re.fullmatch(r"[a-z][a-z0-9]*", token.strip().strip("`").strip())
    ]


def _string_lists(node, out):
    """Collect every list of strings in a decoded JSON document."""
    if isinstance(node, list):
        if node and all(isinstance(item, str) for item in node):
            out.append(list(node))
        else:
            for item in node:
                _string_lists(item, out)
    elif isinstance(node, dict):
        for value in node.values():
            _string_lists(value, out)
    return out


def extract_json_enum_containing(text):
    """The one string-list in a JSON document that names `bibtex`.

    Exactly one, or nothing: two candidate lists means the extractor cannot tell
    the citation list from something that merely mentions a format, and reporting
    either one would be a guess wearing a measurement's clothes.
    """
    candidates = [lst for lst in _string_lists(json.loads(text), []) if "bibtex" in lst]
    return candidates[0] if len(candidates) == 1 else []


def extract_json_dropdown_values(text, preference):
    """A Raycast preference dropdown's `data[].value` list."""
    for pref in json.loads(text).get("preferences", []):
        if pref.get("name") != preference:
            continue
        return [entry.get("value") for entry in pref.get("data", [])]
    return []


def extract_package_enum(text, key):
    """`contributes.configuration.properties.<key>.enum` from a package.json."""
    properties = (
        json.loads(text)
        .get("contributes", {})
        .get("configuration", {})
        .get("properties", {})
    )
    return list(properties.get(key, {}).get("enum", []))


EXTRACTORS = {
    "identifier_array": extract_identifier_array,
    "prose_formats": extract_prose_formats,
    "json_enum_in_source": extract_json_enum_in_source,
    "dict_keys": extract_dict_keys,
    "regex_alternation": extract_regex_alternation,
    "json_enum_containing": extract_json_enum_containing,
    "json_dropdown_values": extract_json_dropdown_values,
    "package_enum": extract_package_enum,
}


# ── the carriers ──────────────────────────────────────────────────────────────
# (label, path relative to the stack root, extractor, extra arguments)
#
# The first row is the canonical list itself, read from source so the check
# cannot pass by comparing a list to itself in memory. A carrier whose path is
# absent is a skip; a carrier whose path is present but yields nothing is a
# failure.

CARRIERS = (
    ("this CLI's --format list", "conceptio-cli/conceptio_cli/cli.py",
     "identifier_array", ("CITE_FORMATS",)),
    ("MCP server tool schema", "conceptio-cli/conceptio_cli/mcp_server.py",
     "json_enum_in_source", ()),
    ("server validation regex", "Conceptio/conceptio/api.py",
     "regex_alternation", ("pattern=",)),
    ("server citation renderers", "Conceptio/conceptio/api.py",
     "dict_keys", ("_CITE_FNS",)),
    ("the web app's picker", "Conceptio/frontend/src/shared/utils/config.js",
     "identifier_array", ("CITE_FORMATS",)),
    ("the published mcp.json", "Conceptio/frontend/public/mcp.json",
     "json_enum_containing", ()),
    ("Neovim README (delegates, but states them)", "conceptio-nvim/README.md",
     "prose_formats", ("Formats:",)),
    ("Neovim :help page", "conceptio-nvim/doc/conceptio.txt",
     "prose_formats", ("Formats:",)),
    ("Obsidian plugin", "conceptio-obsidian/src/types.ts",
     "identifier_array", ("CITATION_FORMATS",)),
    ("Raycast extension", "conceptio-raycast/src/lib/types.ts",
     "identifier_array", ("CITATION_FORMATS",)),
    ("Raycast settings dropdown", "conceptio-raycast/package.json",
     "json_dropdown_values", ("defaultCitationFormat",)),
    ("VS Code webview", "conceptio-vscode/src/webview/main.ts",
     "identifier_array", ("CITATION_FORMATS",)),
    ("VS Code settings enum", "conceptio-vscode/package.json",
     "package_enum", ("conceptio.citationFormat",)),
)

# A checkout that is not here cannot be compared. Below this many *present*
# carriers the comparison is not covering the surface it claims to, which is a
# failure of this check rather than a fact about the code — the same rule the
# README contract uses.
MIN_CARRIERS = 4


def canonical():
    """This repo's own list, read from source rather than imported as a literal."""
    found = extract_identifier_array(
        (REPO / "conceptio_cli" / "cli.py").read_text(encoding="utf-8"), "CITE_FORMATS"
    )
    assert found == list(CITE_FORMATS), (
        "read %r out of cli.py but the module says %r — the extractor or the "
        "module-level list moved." % (found, list(CITE_FORMATS))
    )
    return found


def load_carriers():
    """Return `(read, absent, blind)` — carriers to compare, to skip, to fail on."""
    read, absent, blind = [], [], []
    for label, rel, kind, extra in CARRIERS:
        path = STACK / rel
        if not path.exists():
            absent.append(label)
            continue
        try:
            found = EXTRACTORS[kind](path.read_text(encoding="utf-8", errors="replace"), *extra)
        except (ValueError, json.JSONDecodeError) as exc:
            blind.append("%s (%s: %s)" % (label, kind, exc))
            continue
        if not found:
            blind.append("%s (%s found nothing in %s)" % (label, kind, rel))
            continue
        read.append((label, rel, found))
    return read, absent, blind


def compare(expected, found):
    """Human-readable differences between two format lists (membership only)."""
    problems = []
    missing = [name for name in expected if name not in found]
    extra = [name for name in found if name not in expected]
    if missing:
        problems.append("missing %s" % ", ".join(missing))
    if extra:
        problems.append("lists %s, which the CLI does not accept" % ", ".join(extra))
    if len(found) != len(set(found)):
        problems.append("repeats a format")
    return problems


# ── the checks ────────────────────────────────────────────────────────────────

def test_every_carrier_agrees_with_this_repo():
    """Membership, across every surface that enumerates the formats."""
    expected = canonical()
    read, absent, blind = load_carriers()

    assert not blind, (
        "these carriers are checked out but could not be read — a file that "
        "enumerates citation formats yielded nothing, so the extractor went "
        "blind rather than the document being empty:\n      " + "\n      ".join(blind)
    )
    if not read:
        pytest.skip("no carrier checkouts beside this repo")

    mismatches = []
    for label, rel, found in read:
        problems = compare(expected, found)
        if problems:
            mismatches.append("%s (%s): %s" % (label, rel, "; ".join(problems)))
    assert not mismatches, (
        "the citation format list is not the same everywhere. One place accepted "
        "formats the others do not, so a user of that surface cannot pick it:\n"
        "      canonical (%d): %s\n      " % (len(expected), ", ".join(expected))
        + "\n      ".join(mismatches)
    )


def test_the_comparison_is_covering_the_surfaces_it_claims():
    """Guards against a green run that quietly compared three files."""
    read, absent, blind = load_carriers()
    if not read:
        pytest.skip("no carrier checkouts beside this repo")
    assert len(read) >= MIN_CARRIERS, (
        "only %d carrier(s) were readable (%s) — below the %d this check needs to "
        "mean anything. Absent: %s."
        % (len(read), ", ".join(label for label, _rel, _found in read),
           MIN_CARRIERS, ", ".join(absent) or "none")
    )


def test_the_canonical_list_is_not_truncated():
    """A short canonical list would make every carrier 'agree' by being equally wrong."""
    expected = canonical()
    assert len(set(expected)) == len(expected), "the CLI's own list repeats a format"
    assert len(expected) >= 5, (
        "this repo's --format list has %d entries — that is not the citation "
        "vocabulary, it is a parser failure." % len(expected)
    )


# ── the instrument itself ─────────────────────────────────────────────────────
# Every extractor is exercised against a literal fixture. Without these, an
# extractor that always returned [] would pass the equality checks above on the
# strength of a blindness guard alone, and one that returned the same wrong thing
# everywhere would pass them outright.

FIXTURES = {
    "identifier_array": ("export const CITATION_FORMATS: X[] = [\n  \"bibtex\", \"apa\",\n];",
                         ["bibtex", "apa"]),
    "json_enum_in_source": ('{"enum": ["bibtex", "apa"], "default": "bibtex"}', ["bibtex", "apa"]),
    "prose_formats": ("Formats: `bibtex`, `apa` (default), `mla`. Next sentence here.",
                      ["bibtex", "apa", "mla"]),
    "dict_keys": ('_CITE_FNS = {"bibtex": _cite_bibtex, "apa": _cite_apa}', ["bibtex", "apa"]),
    "regex_alternation": ('pattern="^(bibtex|apa)$"', ["bibtex", "apa"]),
    "json_enum_containing": ('{"a": [{"enum": ["bibtex", "apa"]}]}', ["bibtex", "apa"]),
    "json_dropdown_values": ('{"preferences": [{"name": "f", "data": '
                             '[{"title": "A", "value": "apa"}, {"title": "B", "value": "mla"}]}]}',
                             ["apa", "mla"]),
    "package_enum": ('{"contributes": {"configuration": {"properties": '
                     '{"conceptio.citationFormat": {"enum": ["apa", "bibtex"]}}}}}',
                     ["apa", "bibtex"]),
}


@pytest.mark.parametrize("kind", sorted(FIXTURES))
def test_every_extractor_reads_a_fixture_it_was_given(kind):
    text, expected = FIXTURES[kind]
    if kind == "identifier_array":
        found = extract_identifier_array(text, "CITATION_FORMATS")
    elif kind == "prose_formats":
        found = extract_prose_formats(text, "Formats:")
    elif kind == "dict_keys":
        found = extract_dict_keys(text, "_CITE_FNS")
    elif kind == "regex_alternation":
        found = extract_regex_alternation(text, "pattern=")
    elif kind == "json_dropdown_values":
        found = extract_json_dropdown_values(text, "f")
    elif kind == "package_enum":
        found = extract_package_enum(text, "conceptio.citationFormat")
    else:
        found = EXTRACTORS[kind](text)
    assert found == expected, "%s read %r out of %r" % (kind, found, text)


def test_a_fixture_missing_a_format_is_reported_as_a_mismatch():
    """The positive control for `compare`: it must bite on the bug it exists for."""
    expected = ["bibtex", "apa", "mla"]
    assert compare(expected, list(expected)) == []
    assert "mla" in " ".join(compare(expected, ["bibtex", "apa"]))
    assert "oscola" in " ".join(compare(expected, ["bibtex", "apa", "mla", "oscola"]))
    assert "repeats" in " ".join(compare(expected, ["bibtex", "bibtex", "apa", "mla"]))


def test_an_extractor_that_finds_nothing_is_a_blind_carrier_not_a_quiet_pass():
    """The blindness guard, inverted: an unreadable carrier must land in `blind`."""
    assert extract_identifier_array("no such identifier here", "CITE_FORMATS") == []
    assert extract_regex_alternation("pattern=\"plain\"", "pattern=") == []
    assert extract_json_enum_in_source('{"enum": ["apa"]}') == []
    assert extract_dict_keys("nothing = []", "_CITE_FNS") == []
