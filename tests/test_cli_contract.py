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

from pathlib import Path

import pytest

from conceptio_cli.cli import build_parser
from tests.client_contract import CLIENT_CALLS, CLIENT_SOURCES

STACK = Path(__file__).resolve().parent.parent.parent


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


def expect_in_source(text, needle, client, what):
    quoted = ('"%s"' % needle, "'%s'" % needle)
    assert any(q in text for q in quoted), (
        "%s no longer contains the %s %s that tests/client_contract.py records — "
        "update the table if the client's call changed." % (client, what, needle)
    )
