"""conceptio_cli — terminal-first search + MCP server for the Conceptio Open Knowledge Archive."""

# Prefer the installed distribution's metadata so `--version` tracks the
# released package. Falls back to a literal for uninstalled source checkouts so
# imports never hard-fail.
#
# The literal is a second copy of `pyproject.toml`'s version, and two copies of
# a number drift: `tests/test_cli.py::test_version_literals_agree_with_pyproject`
# fails if they disagree, so a release that bumps one and forgets the other
# cannot ship.
try:
    from importlib.metadata import version as _dist_version
    __version__ = _dist_version("conceptio-search")
except Exception:
    __version__ = "0.3.7"
