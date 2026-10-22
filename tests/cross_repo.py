"""Resolve optional cross-repo check targets without publishing workspace layout.

A public suite must not embed its authors' checkout layout, so cross-repo
targets are referenced by logical name only. ``tests/cross_repo_paths.json``
(gitignored) maps those names to the paths one particular workspace uses —
``tests/cross_repo_paths.example.json`` shows the shape. A name the map does
not carry, or a missing map, means the dependent check skips: a carrier that
is not here cannot be compared.

Map values may be absolute or relative to this repository's root. The map
location itself can be overridden with ``CONCEPTIO_CROSS_PATHS``.
"""

import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAP_FILE = Path(
    os.environ.get("CONCEPTIO_CROSS_PATHS")
    or (Path(__file__).resolve().parent / "cross_repo_paths.json")
)


def resolve(name):
    """Path for the named target, or None when this workspace does not map it."""
    if not MAP_FILE.exists():
        return None
    try:
        mapping = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rel = mapping.get(name)
    if not rel:
        return None
    path = Path(rel)
    return path if path.is_absolute() else (REPO / path)
