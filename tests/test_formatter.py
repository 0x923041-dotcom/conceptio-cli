"""Offline tests for the CLI's machine-mode console routing.

In `--json` mode human messages must never pollute stdout (JSON goes there):
errors, hints and progress are routed to stderr so a caller can pipe stdout
straight into a JSON decoder.

The decision moved: it used to be sniffed from `sys.argv` at import time, which
is the wrong instrument — `main(argv)` is the real entry point, so a host that
embeds the CLI (or a test) is judged by an argv it never supplied. `main()`
now calls `set_json_mode()` after parsing, and these tests pin both the routing
and the fact that `sys.argv` no longer has a vote.
"""

import importlib
import sys

import pytest

import conceptio_cli.formatter as formatter


@pytest.fixture(autouse=True)
def _restore_stream():
    """The console is one shared object (every module imported it by value), so
    a test that flips its stream must put it back — otherwise the leak becomes
    the next test's starting state."""
    before = formatter.console.stderr
    yield
    formatter.console.stderr = before


def test_machine_mode_routes_human_output_to_stderr():
    formatter.set_json_mode(True)
    assert formatter.console.file is sys.stderr


def test_interactive_mode_keeps_human_output_on_stdout():
    formatter.set_json_mode(False)
    assert formatter.console.file is sys.stdout


def test_set_json_mode_mutates_the_shared_console():
    """Not a rebind: `from .formatter import console` bound the object, so a new
    Console would leave every other module printing to the old stream."""
    before = formatter.console
    formatter.set_json_mode(True)
    assert formatter.console is before


def test_import_time_argv_does_not_decide_the_stream(monkeypatch):
    """The regression this replaced: `--json` on the process command line used to
    flip the stream at import, which (a) missed the flag when `main(argv)` was
    called with its own list, and (b) fired for a `--json` that argparse would
    never have accepted. The flag is present here and deliberately ignored."""
    monkeypatch.setattr(sys, "argv", ["conceptio", "search", "--json"])
    reloaded = importlib.reload(formatter)
    try:
        assert "--json" in sys.argv
        assert reloaded.console.file is sys.stdout
    finally:
        reloaded.set_json_mode(False)
