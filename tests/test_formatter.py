"""Offline tests for the CLI's machine-mode console routing."""

import importlib
import sys

import pytest


def test_console_goes_to_stderr_when_json_requested(monkeypatch):
    """In --json mode human messages must never pollute stdout (JSON goes
    there); errors/hints are routed to stderr so callers can pipe stdout
    straight into a JSON decoder."""
    import conceptio_cli.formatter as formatter

    monkeypatch.setattr(sys, "argv", ["conceptio", "search", "--json"])
    reloaded = importlib.reload(formatter)
    assert reloaded.console.file is sys.stderr


def test_console_stays_on_stdout_without_json(monkeypatch):
    import conceptio_cli.formatter as formatter

    monkeypatch.setattr(sys, "argv", ["conceptio", "search"])
    reloaded = importlib.reload(formatter)
    assert reloaded.console.file is sys.stdout