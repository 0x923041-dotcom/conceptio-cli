"""Packaging claims, tested instead of asserted.

`pyproject.toml` tells a stranger what this package needs. A README line like
"Requires Python 3.8+" is a promise about every source file, and the cheapest
way to keep it honest is to parse the files at that version's grammar — no
interpreter of that version required.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "conceptio_cli"


def _declared_floor() -> tuple:
    """The `requires-python` floor from pyproject, as a (major, minor) pair."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^requires-python\s*=\s*"([^"]+)"', pyproject)
    assert match, "pyproject.toml declares no requires-python"
    spec = match.group(1)
    numbers = re.search(r"(\d+)(?:\.(\d+))?", spec)
    assert numbers, f"could not read a version out of requires-python = {spec!r}"
    return (int(numbers.group(1)), int(numbers.group(2) or 0))


def test_sources_parse_at_the_python_we_claim_to_support():
    floor = _declared_floor()
    files = sorted(PACKAGE.glob("*.py"))
    assert files, "no source files found — the layout moved"
    incompatible = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        try:
            ast.parse(source, filename=str(path), feature_version=floor)
        except SyntaxError as exc:
            incompatible.append(f"{path.name}:{exc.lineno}: {exc.msg}")
    assert not incompatible, (
        f"these files do not parse under the declared floor {floor[0]}.{floor[1]}: "
        + "; ".join(incompatible)
    )
