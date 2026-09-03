"""conceptio_cli — terminal-first search + MCP server for the Conceptio Open Knowledge Archive."""

# Prefer the installed distribution's metadata so `--version` tracks the released
# package (e.g. 0.2.1). Falls back to a literal for uninstalled source
# checkouts so imports never hard-fail.
try:
    from importlib.metadata import version as _dist_version
    __version__ = _dist_version("conceptio-search")
except Exception:
    __version__ = "0.2.1"
