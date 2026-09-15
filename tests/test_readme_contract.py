"""Every command the README documents must still be one this binary accepts.

The README is the front door, and its examples are the only place most users
copy an invocation from. Nothing checked them: a renamed flag or a dropped
subcommand would leave the docs teaching a command that exits 2, and the
offline suite (which calls `main()` directly) would stay green — the same drift
class as the client contract in `test_cli_contract.py`, one audience further out.

The rules are deliberately explicit rather than a skip list:

* an example is a line inside a fenced block that invokes `conceptio` (with or
  without a `$` prompt), tokenized with `shlex` so quoting cannot fool it;
* a trailing `#` comment is stripped before tokenizing;
* a line containing a **shell** construct — a pipe, a redirect, a binary
  operator, an assignment or an explicit interpreter — is not an argv list and
  is skipped, and every skip must name one of those markers, so nothing is
  skipped for an unstated reason;
* `--version` exits 0 through argparse's own action, which is a pass.

At least `MIN_EXAMPLES` lines must survive, so an extractor that quietly stops
matching the file fails instead of passing vacuously.
"""

import re
import shlex
from pathlib import Path

import pytest

from conceptio_cli.cli import build_parser

README = Path(__file__).resolve().parent.parent / "README.md"
MIN_EXAMPLES = 25

SHELL_MARKERS = ("|", ">", "<", "&&", "||", ";", "$(", "cmd /c", "powershell", "export ")


def fenced_blocks(text):
    """Yield (info_string, [lines]) for every fenced code block."""
    block, info = None, ""
    for line in text.splitlines():
        if line.startswith("```"):
            if block is None:
                block, info = [], line[3:].strip()
                continue
            yield info, block
            block = None
            continue
        if block is not None:
            block.append(line)


def readme_invocations():
    """Return (checked, skipped) — the argv lists the README documents."""
    text = README.read_text(encoding="utf-8")
    checked, skipped = [], []
    for _info, lines in fenced_blocks(text):
        for raw in lines:
            line = raw.strip()
            if line.startswith("$"):
                line = line[1:].strip()
            if not line.startswith("conceptio"):
                continue
            line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
            # A `<placeholder>` is a value, not a redirect — collapse it before
            # the shell test, or `search-job <job-id> --json` gets skipped as if
            # it were a pipeline (`< req.jsonl` has no closing bracket and still
            # counts as one, which is the distinction that matters).
            probe = re.sub(r"<[^<>\s]*>", "PLACEHOLDER", line)
            if any(marker in probe for marker in SHELL_MARKERS):
                skipped.append((line, [m for m in SHELL_MARKERS if m in probe]))
                continue
            # Drop the program name: `build_parser()` describes what comes
            # *after* `conceptio`, so leaving it in makes the parser read the
            # binary's own name as the subcommand.
            checked.append(shlex.split(probe)[1:])
    return checked, skipped


def test_the_readme_extractor_still_finds_the_examples():
    checked, skipped = readme_invocations()
    assert len(checked) >= MIN_EXAMPLES, (
        "only %d invocation(s) found in README.md (expected >= %d) — the extractor "
        "stopped matching the document, which is not the same as the document being right"
        % (len(checked), MIN_EXAMPLES)
    )
    for line, markers in skipped:
        assert markers, "skipped a line without naming a reason: %r" % line


def test_every_command_the_readme_documents_is_accepted_by_the_cli():
    checked, _ = readme_invocations()
    parser = build_parser()
    failures = []
    for argv in checked:
        try:
            parser.parse_args(list(argv))
        except SystemExit as exc:
            if exc.code == 0:
                continue  # argparse's --version action
            failures.append("`conceptio %s` (exit %s)" % (" ".join(argv), exc.code))
    assert not failures, (
        "the README documents commands this binary refuses — a user copying one gets a "
        "usage error:\n      " + "\n      ".join(failures)
    )


@pytest.mark.parametrize("subcommand", ["search", "resolve", "download", "cite", "info",
                                        "proof", "auth", "quota", "save", "search-job", "mcp"])
def test_every_subcommand_the_readme_names_still_exists(subcommand):
    """The grammar's own help table is a promise the README leans on."""
    parser = build_parser()
    choices = next(a for a in parser._actions if a.dest == "command").choices
    assert subcommand in choices, "%s is gone from the CLI" % subcommand
