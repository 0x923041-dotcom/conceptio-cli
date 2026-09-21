#!/usr/bin/env python3
"""Authorised check: the real `conceptio` CLI against the real API, *succeeding*.

The other two live harnesses are deliberate opposites of this one, and the three
together are the point:

* `live_check.py` forces every network call onto a loopback stub. It can prove
  the CLI composes a request and parses a reply — and, because the stub is ours,
  it can never notice the deployed API moving underneath.
* `live_prod_check.py` reaches production on purpose, but with a **placeholder**
  credential, so every path it exercises is a *refusal*: it proves the CLI
  degrades cleanly and nothing more.
* this file reaches production with a **real** credential and asserts what the
  README promises when everything works: the JSON shapes a machine caller
  decodes, the 11 citation formats, a PDF that is a whole PDF, the MCP server
  answering a real query, and a `--json` stream that is a single JSON document.

Nothing here runs without `--yes`, and it refuses to report anything at all
unless the credential it resolves is **not** the public tier — a success check
run anonymously would pass by measuring refusals.

The credential is resolved exactly the way the CLI resolves it: `CONCEPTIO_LICENSE_KEY`
/ `CONCEPTIO_API_KEY` from the environment, else the user's own
`~/.conceptio/config.json`. When a credential comes from the environment the run
is confined to a throwaway config directory, so `auth`'s file writes can never
touch the real one; when it comes from the config file, that file is read and the
key is checked for leakage but never printed.

Usage:  python tests/live_account_check.py --yes
Env:    CONCEPTIO_CLI       command for the CLI (default: ./.venv/Scripts/conceptio, else PATH)
        CONCEPTIO_API_KEY / CONCEPTIO_LICENSE_KEY   the credential to use
        CONCEPTIO_ACCOUNT_VERBOSE=1 to echo each command's truncated output
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO))

VERBOSE = os.environ.get("CONCEPTIO_ACCOUNT_VERBOSE") == "1"
PRODUCTION = "https://www.conceptio.app"
PACE_SECONDS = 1.1          # the Pro burst bucket is 1 req/s; a 429 here is our pacing
MAX_PDF_BYTES = 100 * 1024 * 1024
MCP_TOOL_COUNT = 8

# Every command the README documents, and the fields its JSON promises.
SEARCH_RESULT_FIELDS = (
    "id", "title", "author", "source", "source_label", "license", "year",
    "url", "direct_pdf_url", "snippet", "full_text_available",
)
CITE_FORMATS = (
    "bibtex", "apa", "mla", "chicago", "ieee", "harvard",
    "ris", "bluebook", "oscola", "iso690", "ansiz39",
)
# Exactly the examples the README prints, so the front door is what gets tested:
# `kind` must match AND the archive must actually hold the identifier. An example
# that resolves to `total: 0` reads, to the user copying it, exactly like a broken
# command — which is how `doi:10.1145/3290605.3300333` and `410 U.S. 113` shipped.
RESOLVE_CASES = (
    ("RFC 2119", "rfc"),
    ("doi:10.1109/access.2020.2986772", "doi"),
    ("2604.08499", "arxiv"),
    ("PMID 41961061", "pmid"),
    ("PMC10601397", "pmcid"),
    ("NIST FIPS 199", "nist"),
    ("w3c_digital-credentials", "w3c"),
    ("347 U.S. 483", "case"),
    ("20-5364", "case"),
)

CHECKS = []
FAILURES = []
SKIPS = []


def check(name):
    def register(fn):
        CHECKS.append((name, fn))
        return fn
    return register


def source_version() -> str:
    try:
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else ""


def cli_command():
    explicit = os.environ.get("CONCEPTIO_CLI", "").strip()
    if explicit:
        return shlex.split(explicit)
    for candidate in (REPO / ".venv" / "Scripts" / "conceptio.exe", REPO / ".venv" / "bin" / "conceptio"):
        if candidate.exists():
            return [str(candidate)]
    found = shutil.which("conceptio")
    if found:
        return [found]
    return [sys.executable, "-m", "conceptio_cli"]


def mask(value: str) -> str:
    value = str(value or "")
    return f"{value[:12]}…{value[-4:]} ({len(value)} chars)" if len(value) > 20 else "(too short to be a key)"


class Account:
    """The CLI against production, holding a real credential.

    Every invocation is recorded, so the last checks can assert on the whole
    transcript at once: no traceback anywhere, no credential echoed anywhere,
    and — for the runs that asked for JSON — stdout that is exactly one JSON
    document and nothing else.
    """

    def __init__(self, command, home, credential, env_overrides=None):
        self.command = command
        self.home = home
        self.credential = credential
        self.transcript = []          # (argv_without_program, CompletedProcess)
        self.json_runs = []           # (label, argv, stdout)
        self.rate_limited = 0
        self.env = dict(os.environ)
        for name in ("CONCEPTIO_API_KEY", "CONCEPTIO_LICENSE_KEY", "CONCEPTIO_BEARER_TOKEN"):
            self.env.pop(name, None)
        self.env.pop("CONCEPTIO_API_BASE", None)
        for name, value in (env_overrides or {}).items():
            self.env[name] = value
        self.env["HOME"] = str(home)
        self.env["USERPROFILE"] = str(home)

    def run(self, *args, timeout=120, pace=True):
        argv = self.command + [str(a) for a in args]
        if pace:
            time.sleep(PACE_SECONDS)
        proc = subprocess.run(
            argv, cwd=str(REPO), env=self.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        # A 429 is the bucket's answer to our own pacing, not a CLI defect: back
        # off once and record it, so a rate-limited run is visible as such.
        if "Rate limit" in ((proc.stdout or "") + (proc.stderr or "")) and pace:
            self.rate_limited += 1
            time.sleep(3.0)
            proc = subprocess.run(
                argv, cwd=str(REPO), env=self.env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
        self.transcript.append((argv[1:], proc))
        if VERBOSE:
            sys.stdout.write("    $ %s\n" % " ".join(argv[1:]))
            for label, text in (("out", proc.stdout), ("err", proc.stderr)):
                for line in (text or "").strip().splitlines()[:6]:
                    sys.stdout.write("      %s| %s\n" % (label, line))
        return proc

    def json_run(self, label, *args, **kwargs):
        """Run a command asked for JSON, and require stdout to BE that JSON."""
        proc = self.run(*args, **kwargs)
        self.json_runs.append((label, list(args), proc.stdout or ""))
        return proc

    @staticmethod
    def json_of(proc, label):
        out = (proc.stdout or "").strip()
        assert out, "%s printed nothing on stdout (stderr: %s)" % (label, (proc.stderr or "")[:300])
        try:
            return json.loads(out)
        except ValueError as exc:
            raise AssertionError(
                "%s: stdout is not a single JSON document (%s). First 300 chars:\n%s"
                % (label, exc, out[:300])
            )


def combined(proc) -> str:
    return (proc.stdout or "") + (proc.stderr or "")


def assert_clean_failure(proc, label):
    out = combined(proc)
    assert proc.returncode != 0, "%s should exit non-zero" % label
    assert "Traceback" not in out, "%s leaked a Python traceback:\n%s" % (label, out[:600])
    assert out.strip(), "%s exited %d without saying anything" % (label, proc.returncode)


# ── the credential: none of this is a result if it resolved to the public tier ──

@check("the credential is real (not the public tier)")
def _credential_is_real(account):
    proc = account.json_run("quota", "quota", "--json")
    data = Account.json_of(proc, "quota --json")
    tier = str(data.get("tier") or "")
    auth = str(data.get("auth") or "")
    assert tier and tier != "public", (
        "quota reports tier=%r — an anonymous run measures refusals, so every "
        "assertion below would be meaningless. Configure CONCEPTIO_LICENSE_KEY / "
        "CONCEPTIO_API_KEY (or save one with `conceptio auth`) and re-run." % tier
    )
    assert auth and auth != "public", "quota reports auth=%r for a %r tier" % (auth, tier)
    assert auth in ("license", "api_key", "firebase"), (
        "quota reports an unrecognised auth source %r — a status surface reads this" % auth
    )


@check("--version matches this checkout")
def _version_matches_tree(account):
    declared = source_version()
    assert declared, "could not read version from pyproject.toml"
    proc = account.run("--version", pace=False)
    assert proc.returncode == 0, "--version failed: %s" % proc.stderr
    assert (proc.stdout or "").strip().endswith(declared), (
        "binary advertises %r but this tree declares %s" % (proc.stdout, declared)
    )


# ── search: the JSON a machine caller decodes, and the three human renderings ──

@check("search --json is one JSON document with the promised fields")
def _search_json_shape(account):
    proc = account.json_run("search", "search", "zero trust", "--limit", "3", "--json")
    assert proc.returncode == 0, "search failed: %s" % combined(proc)[:400]
    data = Account.json_of(proc, "search --json")
    for field in ("query", "total", "limit", "offset", "results"):
        assert field in data, "search --json lost %r — keys: %s" % (field, sorted(data))
    assert isinstance(data["results"], list) and data["results"], "search returned no results for a common query"
    assert len(data["results"]) <= int(data["limit"]), "more results than the limit asked for"
    for row in data["results"]:
        missing = [f for f in SEARCH_RESULT_FIELDS if f not in row]
        assert not missing, "a result is missing %s — the clients read these: %s" % (missing, sorted(row))
        assert isinstance(row["id"], int) and row["id"] > 0, "result id is not a positive int: %r" % row["id"]
        assert isinstance(row["full_text_available"], bool), (
            "full_text_available is %r, and the clients branch on a bool" % row["full_text_available"]
        )
    assert any(row["direct_pdf_url"] for row in data["results"]), (
        "no result carried direct_pdf_url — `download` and `cite` depend on it"
    )
    assert any("<mark>" in (row.get("snippet") or "") for row in data["results"]), (
        "no snippet carried highlighting — the human renderer decorates those marks"
    )


@check("search renders a table for a human (no --json)")
def _search_human(account):
    proc = account.run("search", "zero trust", "--limit", "3")
    assert proc.returncode == 0, "human search failed: %s" % combined(proc)[:400]
    out = proc.stdout or ""
    assert "zero trust" in out.lower(), "the human table never names the query:\n%s" % out[:400]
    assert "Traceback" not in combined(proc)


@check("search --markdown emits pasteable markdown")
def _search_markdown(account):
    proc = account.run("search", "zero trust", "--limit", "2", "--markdown")
    assert proc.returncode == 0, "markdown search failed: %s" % combined(proc)[:400]
    out = proc.stdout or ""
    # The promise is "for Obsidian/Notion", not "a table": the renderer emits a
    # list with quoted snippets and links. What matters is that it is markdown a
    # note-taking app will render — a link, and emphasis or a heading to hang it on.
    assert "](" in out, "no markdown link in the output:\n%s" % out[:400]
    assert out.lstrip().startswith("#") or "**" in out, (
        "the markdown rendering has neither a heading nor a bold title:\n%s" % out[:400]
    )
    assert "\x1b[" not in out, "the markdown rendering carries ANSI escapes — it is meant to be pasted, not printed"


@check("a source: directive is applied as a real filter")
def _search_directive(account):
    plain = Account.json_of(account.json_run("search", "search", "zero trust", "--limit", "5", "--json"),
                            "search --json")
    filtered = Account.json_of(
        account.json_run("search", "search", "source:nist zero trust", "--limit", "5", "--json"),
        "search source:nist --json")
    assert filtered["query"] == "zero trust", (
        "the directive was left in the query (%r) — it is supposed to be stripped and applied as a filter"
        % filtered["query"]
    )
    results = filtered["results"]
    assert results, "source:nist returned nothing for a query that has NIST results"
    outside = [r["source"] for r in results if "nist" not in str(r.get("source", "")).lower()]
    assert not outside, "source:nist returned rows from %s" % outside
    assert filtered["total"] < plain["total"], (
        "the filtered total (%s) is not below the unfiltered one (%s) — the filter did not apply"
        % (filtered["total"], plain["total"])
    )


@check("--offset paginates")
def _search_offset(account):
    first = Account.json_of(account.json_run("search", "search", "zero trust", "--limit", "5", "--json"),
                            "search page 1")
    second = Account.json_of(
        account.json_run("search", "search", "zero trust", "--limit", "5", "--offset", "5", "--json"),
        "search page 2")
    assert second["offset"] == 5, "offset was not echoed back: %r" % second.get("offset")
    first_ids = [r["id"] for r in first["results"]]
    second_ids = [r["id"] for r in second["results"]]
    assert second_ids, "page 2 is empty for a query with a large total"
    assert not set(first_ids) & set(second_ids), (
        "page 2 repeats page 1 (%s) — offset is not moving the window" % sorted(set(first_ids) & set(second_ids))
    )


@check("a query with no matches is an empty answer, not an error")
def _search_no_results(account):
    proc = account.run("search", "zzqxvbnmkjhgfdsapoiuytrewq", "--limit", "3")
    out = combined(proc)
    assert "Traceback" not in out, "a no-match query produced a traceback:\n%s" % out[:400]
    assert proc.returncode == 0, (
        "a query the archive simply does not contain exited %d — an empty result is not a failure\n%s"
        % (proc.returncode, out[:400])
    )


@check("--json means the same before the subcommand as after it")
def _global_json_equals_local(account):
    after = Account.json_of(account.json_run("search", "search", "zero trust", "--limit", "2", "--json"),
                            "search … --json")
    before = Account.json_of(account.json_run("search", "--json", "search", "zero trust", "--limit", "2"),
                             "--json search …")
    assert before.get("results") and after.get("results"), "one of the two spellings returned nothing"
    assert [r["id"] for r in before["results"]] == [r["id"] for r in after["results"]], (
        "the two spellings returned different results — a machine caller cannot rely on the flag's position"
    )


@check("--limit is bounded, whatever the caller asks for")
def _search_limit_is_bounded(account):
    huge = Account.json_of(account.json_run("search", "search", "zero trust", "--limit", "999", "--json"),
                           "search --limit 999")
    assert huge["results"], "an out-of-range limit returned nothing at all"
    assert len(huge["results"]) <= 100, (
        "--limit 999 returned %d rows — the CLI's own bound is 100, and the API's page is not a bulk dump"
        % len(huge["results"])
    )
    tiny = Account.json_of(account.json_run("search", "search", "zero trust", "--limit", "0", "--json"),
                           "search --limit 0")
    assert tiny["results"], "--limit 0 returned nothing; a bound of 1 is the documented floor"


# ── resolve: every identifier shape the README advertises ─────────────────────

@check("resolve recognises every advertised identifier shape")
def _resolve_kinds(account):
    wrong = []
    for identifier, expected in RESOLVE_CASES:
        proc = account.json_run("resolve", "resolve", identifier, "--json")
        if proc.returncode != 0:
            wrong.append("%s → exit %d (%s)" % (identifier, proc.returncode, combined(proc)[:160]))
            continue
        data = Account.json_of(proc, "resolve %r" % identifier)
        kind = data.get("kind")
        if kind != expected:
            wrong.append("%s → kind=%r, advertised %r" % (identifier, kind, expected))
            continue
        results = data.get("results") or []
        if not results:
            wrong.append("%s → kind=%r but no results" % (identifier, kind))
    assert not wrong, "resolve disagreed with its own documentation:\n  " + "\n  ".join(wrong)


@check("an unrecognised identifier degrades to a search instead of failing")
def _resolve_fallback(account):
    proc = account.json_run("resolve", "resolve", "a phrase that is not an identifier", "--json")
    assert proc.returncode == 0, "the fallback resolve failed: %s" % combined(proc)[:300]
    data = Account.json_of(proc, "resolve fallback")
    assert data.get("kind") is None, "unrecognised text came back as kind=%r" % data.get("kind")


# ── info / proof: one document, two renderings ────────────────────────────────

def _pick_document(account):
    """A document with a PDF and full text — the id every later check reuses."""
    data = Account.json_of(account.json_run("search", "search", "zero trust", "--limit", "5", "--json"),
                           "search (sample)")
    for row in data["results"]:
        if row.get("direct_pdf_url") and row.get("full_text_available"):
            return row
    raise AssertionError("no search result carried both a PDF link and full text — the sample is unusable")


@check("info --json agrees with the row search returned")
def _info_json(account):
    row = account.sample
    proc = account.json_run("info", "info", row["id"], "--json")
    assert proc.returncode == 0, "info failed: %s" % combined(proc)[:300]
    data = Account.json_of(proc, "info --json")
    assert int(data.get("id") or 0) == row["id"], "info returned document %r, asked for %r" % (data.get("id"), row["id"])
    assert data.get("title") == row["title"], (
        "info's title disagrees with search's for the same id — one of the two routes is wrong"
    )
    assert data.get("source") == row["source"], "info and search disagree about the source"


@check("info renders the document for a human")
def _info_human(account):
    row = account.sample
    proc = account.run("info", row["id"])
    assert proc.returncode == 0, "human info failed: %s" % combined(proc)[:300]
    out = proc.stdout or ""
    assert row["title"][:40].lower() in out.lower(), "the human view never prints the title:\n%s" % out[:400]


@check("proof --json is the nested bundle the clients render")
def _proof_shape(account):
    row = account.sample
    proc = account.json_run("proof", "proof", row["id"], "--json")
    assert proc.returncode == 0, "proof failed: %s" % combined(proc)[:300]
    data = Account.json_of(proc, "proof --json")
    document = data.get("document")
    assert isinstance(document, dict), (
        "proof has no nested `document` — the flat spelling was the bug four surfaces shipped against, "
        "and the top-level keys are %s" % sorted(data)
    )
    assert int(document.get("id") or 0) == row["id"], "the bundle is for document %r" % document.get("id")
    for flat in ("document_id", "canonical_url", "source_label"):
        assert flat not in data, "the flat spelling %r is back at the top level" % flat
    citation = data.get("citation")
    assert isinstance(citation, dict), "citation is %r, not a dict of formats" % type(citation).__name__
    for fmt in ("bibtex", "apa", "ris"):
        assert str(citation.get(fmt) or "").strip(), "citation.%s is empty" % fmt
    assert isinstance(data.get("full_text_available"), bool), "full_text_available is not a bool"
    assert str(data.get("access_level") or ""), "access_level is missing — the access gate reads it"
    assert isinstance(data.get("passage"), dict), (
        "passage is %r; without -q it should be an empty dict, not absent" % type(data.get("passage")).__name__
    )


@check("proof -q returns the passage that matched, from a document that has text")
def _proof_passage(account):
    row = account.sample
    assert row.get("full_text_available"), "the sample document has no text to prove"
    proc = account.json_run("proof", "proof", row["id"], "-q", "the", "--json")
    assert proc.returncode == 0, "passage proof failed: %s" % combined(proc)[:300]
    data = Account.json_of(proc, "proof -q")
    passage = data.get("passage") or {}
    snippet = str(passage.get("snippet") or "")
    assert snippet, (
        "no passage came back for a full-text document even though the word asked for is a common one: %r"
        % (passage,)
    )
    assert "the" in snippet.lower(), "the passage does not contain the word asked for: %r" % snippet[:200]


# ── cite: 11 formats, and they must be 11 different citations ─────────────────

@check("cite exports all 11 advertised formats")
def _cite_formats(account):
    doc_id = account.sample["id"]
    outputs = {}
    empty = []
    for fmt in CITE_FORMATS:
        proc = account.run("cite", doc_id, "--format", fmt)
        text = (proc.stdout or "").strip()
        if proc.returncode != 0 or not text:
            empty.append("%s → exit %d, %d bytes" % (fmt, proc.returncode, len(text)))
        outputs[fmt] = text
    assert not empty, "cite produced nothing for: %s" % "; ".join(empty)
    account.citations = outputs
    assert outputs["bibtex"].startswith("@"), "bibtex does not start with @: %r" % outputs["bibtex"][:80]
    ris_lines = [line for line in outputs["ris"].splitlines() if line.strip()]
    assert ris_lines and ris_lines[0].startswith("TY  - "), (
        "the RIS export does not open with TY  - :\n%s" % outputs["ris"][:200]
    )
    assert any(line.startswith("ER  -") for line in ris_lines), (
        "the RIS export has no ER  - terminator:\n%s" % outputs["ris"][:200]
    )
    assert any(line.startswith("PY  - ") for line in ris_lines), (
        "the RIS export dropped the year (no PY  - line) for a dated document:\n%s" % outputs["ris"][:200]
    )


@check("cite and the proof bundle render the same citation for one document")
def _cite_and_proof_agree(account):
    """Two builders render the same document — the pair that drifted.

    `/api/cite/{id}` and the proof bundle (embedded in every search row) each
    build their own citation dict. Nothing compared them, so the bundle's
    citations could be dateless forever while `cite` printed the year: measured
    live 2026-09-21, `cite 155205 --format bibtex` said `year = {2026}` and the
    bundle for 155205 said `n.d.`. A client rendering the bundle shipped the
    wrong year, and the shape checks all stayed green because the shape was
    right. This compares content, not shape.
    """
    if not account.citations:
        SKIPS.append("cite-vs-proof: the cite sweep did not produce citations to compare")
        return
    row = account.sample
    proc = account.json_run("proof", "proof", row["id"], "--json")
    bundle = Account.json_of(proc, "proof --json")
    citation = bundle.get("citation") or {}
    mismatched = []
    for fmt in ("bibtex", "apa", "ris"):
        text = (account.citations.get(fmt) or "").strip()
        bundled = str(citation.get(fmt) or "").strip()
        if text != bundled:
            mismatched.append("%s:\n      cite  %r\n      proof %r" % (fmt, text[:220], bundled[:220]))
    assert not mismatched, (
        "the two citation renderers disagree about the same document:\n    " + "\n    ".join(mismatched)
    )


@check("the 11 formats are really 11 renderings, not one repeated")
def _cite_formats_differ(account):
    outputs = account.citations
    if not outputs:
        SKIPS.append("cite-format-routing: the cite sweep did not produce citations")
        return
    distinct = {text.strip() for text in outputs.values()}
    assert len(distinct) >= 8, (
        "only %d of %d formats produced distinct text — --format is not routing: %s"
        % (len(distinct), len(outputs), sorted(outputs))
    )


# ── download: the promise is a whole PDF, or a clean refusal ─────────────────

@check("download by id writes a complete PDF")
def _download_by_id(account):
    row = account.sample
    target = account.tmp / "by-id.pdf"
    proc = account.run("download", row["id"], "-o", str(target), timeout=180)
    assert proc.returncode == 0, "download failed: %s" % combined(proc)[:300]
    blob = target.read_bytes()
    assert blob[:5] == b"%PDF-", "the file does not start with a PDF header: %r" % blob[:16]
    assert b"%%EOF" in blob[-2048:], (
        "no %%EOF in the last 2 KB — the PDF is truncated, which a length-only check would miss "
        "(%d bytes)" % len(blob)
    )
    assert b"startxref" in blob[-4096:], "no startxref trailer — the file is not a complete PDF"
    assert 1024 < len(blob) < MAX_PDF_BYTES, "implausible PDF size: %d bytes" % len(blob)
    account.pdf_bytes = blob


@check("download by URL produces the same bytes as download by id")
def _download_by_url(account):
    row = account.sample
    target = account.tmp / "by-url.pdf"
    proc = account.run("download", row["direct_pdf_url"], "-o", str(target), timeout=180)
    assert proc.returncode == 0, "download by URL failed: %s" % combined(proc)[:300]
    blob = target.read_bytes()
    assert hashlib.sha256(blob).hexdigest() == hashlib.sha256(account.pdf_bytes).hexdigest(), (
        "the two paths to the same PDF disagree — id resolution and the direct URL are not the same file"
    )


@check("download into an existing path overwrites it deliberately")
def _download_overwrites(account):
    row = account.sample
    target = account.tmp / "by-id.pdf"
    before = target.read_bytes()
    proc = account.run("download", row["id"], "-o", str(target), timeout=180)
    assert proc.returncode == 0, "re-download over an existing file failed: %s" % combined(proc)[:300]
    assert target.read_bytes() == before, "the bytes changed between two downloads of one document"


@check("a document with no direct PDF is refused cleanly, leaving nothing behind")
def _download_without_pdf(account):
    data = Account.json_of(account.json_run("search", "search", "source:eurlex regulation", "--limit", "10", "--json"),
                           "search (pdf-less sample)")
    candidates = [r for r in data["results"] if not r.get("direct_pdf_url")]
    if not candidates:
        SKIPS.append("download-without-PDF: no result in the sample lacked a direct PDF")
        return
    target = account.tmp / "no-pdf.pdf"
    proc = account.run("download", candidates[0]["id"], "-o", str(target), timeout=120)
    assert_clean_failure(proc, "download of a PDF-less document")
    out = combined(proc)
    assert "no direct PDF" in out or "direct PDF link" in out, (
        "the refusal does not say why:\n%s" % out[:300]
    )
    assert not target.exists(), "a refusal left a file at the destination"
    residue = [p.name for p in account.tmp.glob(".conceptio-*.part")]
    assert not residue, "a refusal left a partial download behind: %s" % residue


@check("a local/private download target is refused before any request")
def _download_refuses_private_url(account):
    target = account.tmp / "ssrf.pdf"
    proc = account.run("download", "http://127.0.0.1:2223/anything.pdf", "-o", str(target), timeout=60)
    assert_clean_failure(proc, "download of a loopback URL")
    out = combined(proc)
    assert "private" in out.lower() or "local" in out.lower() or "public" in out.lower(), (
        "the refusal does not name the reason (a private/local destination):\n%s" % out[:300]
    )
    assert not target.exists(), "a refused private URL left a file"


@check("a download target that is neither an id nor a URL is refused cleanly")
def _download_refuses_junk(account):
    proc = account.run("download", "not-an-id-or-url", "-o", str(account.tmp / "junk.pdf"), timeout=60)
    assert_clean_failure(proc, "download of a junk target")


# ── the batch surface and the MCP server ─────────────────────────────────────

@check("search --batch --sync runs several queries in one request")
def _batch_sync(account):
    path = account.tmp / "queries.json"
    path.write_text(json.dumps([{"q": "zero trust"}, {"q": "transformer"}]))
    proc = account.json_run("batch", "search", "--batch", str(path), "--sync", "--json", timeout=180)
    assert proc.returncode == 0, "batch --sync failed: %s" % combined(proc)[:300]
    data = Account.json_of(proc, "batch --sync --json")
    assert int(data.get("count") or 0) == 2, "expected 2 queries in the envelope, got %r" % data.get("count")
    queries = data.get("queries") or []
    assert len(queries) == 2, "the envelope carries %d query blocks" % len(queries)
    for block in queries:
        assert block.get("results"), "query %r came back with no results" % block.get("query")
        assert block.get("query"), "a query block lost its own query string"


@check("a queued batch is pollable to completion with search-job")
def _batch_job(account):
    path = account.tmp / "queries-job.json"
    path.write_text(json.dumps([{"q": "zero trust"}, {"q": "transformer"}]))
    queued = account.json_run("batch-queue", "search", "--batch", str(path), "--json", timeout=180)
    assert queued.returncode == 0, "queueing a batch failed: %s" % combined(queued)[:300]
    data = Account.json_of(queued, "batch (queued)")
    job_id = data.get("job_id") or data.get("id")
    assert job_id, "the queued batch returned no job handle — keys: %s" % sorted(data)
    done = account.json_run("search-job", "search-job", str(job_id), "--wait", "--json", timeout=300)
    assert done.returncode == 0, "search-job --wait failed: %s" % combined(done)[:300]
    payload = Account.json_of(done, "search-job --wait --json")
    assert payload, "search-job returned an empty payload"


@check("the MCP server answers a real query over stdio")
def _mcp_round_trip(account):
    argv = account.command + ["mcp"]
    proc = subprocess.Popen(argv, cwd=str(REPO), env=account.env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "live_account_check", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "conceptio_search", "arguments": {"query": "zero trust", "limit": 2}}},
    ]
    replies = {}
    try:
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and len(replies) < 3:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message.get("id"), int):
                replies[message["id"]] = message
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    assert 1 in replies, "the MCP server never answered initialize"
    assert "result" in replies[1], "initialize answered with an error: %s" % replies[1]
    assert 2 in replies, "tools/list was never answered"
    tools = (replies[2].get("result") or {}).get("tools") or []
    names = [t.get("name") for t in tools]
    assert len(names) == MCP_TOOL_COUNT, (
        "tools/list returned %d tools, the README says %d: %s" % (len(names), MCP_TOOL_COUNT, names)
    )
    assert 3 in replies, "the tools/call for a real search was never answered"
    result = replies[3].get("result") or {}
    assert not result.get("isError"), "a real search through MCP came back as an error: %s" % result
    content = result.get("content") or []
    text = "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    assert text.strip(), "the MCP search returned no content"
    account.mcp_names = names


@check("the MCP server refuses a bad tool call as a JSON-RPC error, not a crash")
def _mcp_bad_call(account):
    argv = account.command + ["mcp"]
    proc = subprocess.Popen(argv, cwd=str(REPO), env=account.env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    try:
        for request in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "live_account_check", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "conceptio_search", "arguments": {}}},
        ):
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
        lines = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line.strip())
            if '"id": 2' in line.replace('"id":2', '"id": 2').replace(" ", "") or '"id":2' in line.replace(" ", ""):
                break
        answers = []
        for line in lines:
            try:
                answers.append(json.loads(line))
            except ValueError:
                continue
        second = [a for a in answers if a.get("id") == 2]
        assert second, "calling a tool with no arguments produced no JSON-RPC answer at all"
        answer = second[0]
        assert "error" in answer or (answer.get("result") or {}).get("isError"), (
            "a tool call missing its required argument was answered as success: %s" % answer
        )
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── the whole transcript, at the end: nothing leaked, nothing crashed ────────

@check("no invocation echoed the credential")
def _no_credential_echo(account):
    if not account.credential:
        SKIPS.append("credential-echo: the key came from a source this harness cannot read")
        return
    leaked = [argv for argv, proc in account.transcript if account.credential in combined(proc)]
    assert not leaked, (
        "the credential appears verbatim in the output of: %s"
        % "; ".join(" ".join(a) for a in leaked)
    )


@check("no invocation leaked a traceback")
def _no_tracebacks(account):
    leaked = [argv for argv, proc in account.transcript if "Traceback (most recent call last)" in combined(proc)]
    assert not leaked, "these invocations printed a Python traceback: %s" % "; ".join(" ".join(a) for a in leaked)


@check("every --json run put one JSON document on stdout and nothing else")
def _json_stdout_is_pure(account):
    dirty = []
    for label, argv, stdout in account.json_runs:
        text = (stdout or "").strip()
        if not text:
            dirty.append("%s: empty stdout" % label)
            continue
        try:
            json.loads(text)
        except ValueError as exc:
            dirty.append("%s: %s" % (label, exc))
    assert not dirty, "JSON-mode stdout is not machine-clean:\n  " + "\n  ".join(dirty)


@check("no invocation reached the API base it was not pointed at")
def _base_is_production(account):
    from conceptio_cli.config import DEFAULT_API_BASE

    assert DEFAULT_API_BASE == PRODUCTION, (
        "the shipped default base is %r, not %r — nothing here tested production"
        % (DEFAULT_API_BASE, PRODUCTION)
    )
    assert "CONCEPTIO_API_BASE" not in account.env, "the child was re-pointed at another base"


# ── runner ──────────────────────────────────────────────────────────────────

def resolve_credential(env_keyed: bool) -> str:
    """The credential string, for the leak check only — never printed."""
    for name in ("CONCEPTIO_LICENSE_KEY", "CONCEPTIO_API_KEY", "CONCEPTIO_BEARER_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    if not env_keyed:
        try:
            from conceptio_cli.config import get_api_key, get_license_key

            return (get_license_key() or get_api_key() or "").strip()
        except Exception:  # noqa: BLE001
            return ""
    return ""


def main():
    if "--yes" not in sys.argv:
        print("authorised live check — SKIPPED, not a result.")
        print("  This drives https://www.conceptio.app with a real credential and spends real reads.")
        print("  Re-run with --yes once CONCEPTIO_LICENSE_KEY / CONCEPTIO_API_KEY is set, or a key")
        print("  is saved in ~/.conceptio/config.json.")
        return 0

    command = cli_command()
    tmp = Path(tempfile.mkdtemp(prefix="conceptio-account-"))
    env_keyed = bool((os.environ.get("CONCEPTIO_LICENSE_KEY") or os.environ.get("CONCEPTIO_API_KEY") or "").strip())
    # An environment credential isolates the run from the user's own config; a
    # config credential has to be able to read that config.
    home = tmp / "home" if env_keyed else Path.home()
    is_temp_home = home != Path.home()
    if is_temp_home:
        home.mkdir(parents=True, exist_ok=True)
    credential = resolve_credential(env_keyed)

    overrides = {}
    if env_keyed:
        for name in ("CONCEPTIO_LICENSE_KEY", "CONCEPTIO_API_KEY", "CONCEPTIO_BEARER_TOKEN"):
            value = (os.environ.get(name) or "").strip()
            if value:
                overrides[name] = value

    account = Account(command, home, credential, overrides)
    account.tmp = tmp
    account.sample = None
    account.citations = {}

    print("Conceptio CLI authorised live check — real CLI, real API, real credential")
    print("  cli         %s  (this tree declares %s)" % (" ".join(command), source_version() or "?"))
    print("  base        %s  (the CLI's own default; not overridable here)" % PRODUCTION)
    print("  credential  %s  (%s)" % (
        mask(credential) if credential else "(resolved by the CLI from its own config)",
        "from the environment" if env_keyed else "from ~/.conceptio/config.json",
    ))
    print("")

    # The sample document is picked once, after the checks that must come first.
    try:
        for index, (name, fn) in enumerate(CHECKS, 1):
            if fn.__name__ in ("_info_json", "_info_human", "_proof_shape", "_proof_passage",
                               "_cite_formats", "_cite_formats_differ", "_cite_and_proof_agree",
                               "_download_by_id",
                               "_download_by_url", "_download_overwrites") and account.sample is None:
                try:
                    account.sample = _pick_document(account)
                except AssertionError as error:
                    FAILURES.append((name, error))
                    print("  %2d/%d  FAIL  %s\n        %s" % (index, len(CHECKS), name, error))
                    continue
            start = time.monotonic()
            try:
                fn(account)
                print("  %2d/%d  ok    %s  (%.1fs)" % (index, len(CHECKS), name, time.monotonic() - start))
            except Exception as error:  # noqa: BLE001 — one failing check must not stop the run
                FAILURES.append((name, error))
                print("  %2d/%d  FAIL  %s  (%.1fs)" % (index, len(CHECKS), name, time.monotonic() - start))
                print("        %s" % str(error).replace("\n", "\n        "))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("")
    if account.rate_limited:
        print("%d call(s) were answered 429 and retried once — that is this harness's pacing "
              "against a 1 req/s bucket, not a CLI finding." % account.rate_limited)
    for note in SKIPS:
        print("SKIPPED  %s" % note)
    if FAILURES:
        print("%d of %d authorised checks failed" % (len(FAILURES), len(CHECKS)))
        return 1
    if SKIPS:
        print("%d authorised checks passed, %d skipped — a skip is not a pass"
              % (len(CHECKS) - len(SKIPS), len(SKIPS)))
        return 0
    print("%d authorised checks passed" % len(CHECKS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
