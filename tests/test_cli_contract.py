"""The argv grammar the five shipped clients depend on, held to the parser.

A client is a separate process that execs this binary with a list of arguments.
Nothing type-checks that list, and no client's suite can see this repo — so a
renamed subcommand or a dropped flag passes both suites and ships broken. These
two tests close that gap from the CLI's side: the table in
`tests/client_contract.py` is parsed by the real `argparse` grammar, and, while
a client's checkout sits next to this repo, its sources must still contain the
subcommand and flags the table claims it sends.

What is *not* covered here: argument order, and values. `live_check.py` runs the
same table end to end against a real binary.
"""

import re
from pathlib import Path

import pytest

from conceptio_cli.cli import build_parser
from tests.client_contract import CLIENT_CALLS, CLIENT_SOURCES

STACK = Path(__file__).resolve().parent.parent.parent
REPO = Path(__file__).resolve().parent.parent

# Every client tells the user which CLI release is current, in prose. Nothing
# checks prose: when 0.3.3 shipped, four of the five still said "The current
# release is 0.3.2", and every one of their own suites stayed green — a client
# cannot see this repo's version. This repo can see all of them, so the claim is
# checked here, next to the argv contract it belongs to.
CLIENT_READMES = {
    "neovim": STACK / "conceptio-nvim" / "README.md",
    "vscode": STACK / "conceptio-vscode" / "README.md",
    "raycast": STACK / "conceptio-raycast" / "README.md",
    "alfred": STACK / "conceptio-alfred" / "README.md",
    "obsidian": STACK / "conceptio-obsidian" / "README.md",
}

# Deliberately narrow: it matches the sentence the clients actually use, so it
# cannot pick up an unrelated version number (a minimum CLI version, a Neovim
# requirement) and report it as a stale release.
RELEASE_CLAIM = re.compile(r"current release is\s+\**\s*(\d+\.\d+\.\d+)", re.IGNORECASE)


def source_version():
    match = re.search(
        r'(?m)^version\s*=\s*"([^"]+)"',
        (REPO / "pyproject.toml").read_text(encoding="utf-8"),
    )
    assert match, "could not read version from pyproject.toml"
    return match.group(1)


def test_every_client_argv_is_accepted_by_the_cli_parser():
    """The contract in one direction: this binary still speaks these argv."""
    parser = build_parser()
    for client, calls in CLIENT_CALLS.items():
        for argv in calls:
            try:
                parser.parse_args(list(argv))
            except SystemExit as exc:  # argparse errors exit(2) after printing usage
                raise AssertionError(
                    "%s sends `conceptio %s` and the CLI's parser refused it (exit %s). "
                    "Either the CLI's grammar moved or the client's call is stale."
                    % (client, " ".join(argv), exc.code)
                )


def test_the_table_still_matches_the_clients_that_are_checked_out():
    """The contract in the other direction: the clients still send these argv.

    Absence is a skip, not a pass — a client that is not checked out cannot be
    compared, and a green tick there would mean nothing.
    """
    checked, missing = [], []
    for client, calls in CLIENT_CALLS.items():
        files = [STACK / rel for rel in CLIENT_SOURCES.get(client, [])]
        present = [f for f in files if f.exists()]
        if not present:
            missing.append(client)
            continue
        text = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in present)
        for argv in calls:
            expect_in_source(text, argv[0], client, "subcommand")
            for token in argv:
                if token.startswith("--") or token in ("-l", "-f"):
                    expect_in_source(text, token, client, "flag")
        checked.append(client)

    if not checked:
        pytest.skip("no client checkouts next to this repo (%s)" % ", ".join(sorted(missing)))


def test_client_readmes_name_this_release():
    """A client README that names a release has to name *this* one.

    Absence of a checkout is a skip, never a pass. A README that stopped naming
    a release is allowed — that is a documentation decision, not drift — but if
    no README names one at all, the extractor has gone blind rather than the
    documents having become correct, so that fails instead of passing quietly.
    """
    declared = source_version()
    claims, stale, missing = [], [], []
    for client, path in sorted(CLIENT_READMES.items()):
        if not path.exists():
            missing.append(client)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for found in RELEASE_CLAIM.findall(text):
            claims.append((client, found))
            if found != declared:
                stale.append((client, found))

    assert not stale, (
        "%s, but this repo is %s. A user reads the client's README and installs "
        "what it names, so the two have to agree: either the READMEs are stale "
        "or this version moved without them."
        % (", ".join("%s says %s" % (c, v) for c, v in sorted(stale)), declared)
    )
    if not claims:
        pytest.skip("no client checkouts next to this repo (%s)" % ", ".join(sorted(missing)))
    assert len(claims) >= 3, (
        "only %d client README(s) state a current release — the phrase moved or "
        "was dropped, so this check is no longer guarding anything." % len(claims)
    )


def expect_in_source(text, needle, client, what):
    quoted = ('"%s"' % needle, "'%s'" % needle)
    assert any(q in text for q in quoted), (
        "%s no longer contains the %s %s that tests/client_contract.py records — "
        "update the table if the client's call changed." % (client, what, needle)
    )
