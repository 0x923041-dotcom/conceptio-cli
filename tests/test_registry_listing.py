"""The official MCP registry listing is four claims that must agree.

Publishing to the registry is a **release-time** act: the registry reads the
PyPI long-description (this README, frozen at upload) for the `mcp-name` marker
and matches it against the `name` in `server.json`, then records the package
version it verified. Nothing on PyPI re-checks `server.json` afterwards, so the
listing can rot silently in exactly the way this repo already guards against
elsewhere (`test_version_literals_agree_with_pyproject`,
`test_packaging.py`). The four places a single name/version lives:

* `README.md` — the `mcp-name:` marker the registry's ownership check greps;
* `server.json` — the registry metadata, whose `name` must equal that marker;
* `pyproject.toml` — the distribution name and the version;
* the entry point — `uvx <identifier> <packageArguments>` assumes an executable
  named after the package, so a `conceptio-search` console script must exist.

Every assertion is offline: it reads the files, no network, no registry call.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
SERVER = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

SCHEMA = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
#: Reverse-DNS namespace for a GitHub-authenticated publisher.
NAME_PATTERN = re.compile(r"^io\.github\.[a-z0-9-]+/[a-z0-9._-]+$")


def _pyproject_value(key: str) -> str:
    match = re.search(rf'(?m)^{key}\s*=\s*"([^"]+)"', PYPROJECT)
    assert match, f"pyproject.toml declares no {key}"
    return match.group(1)


def _readme_marker() -> str:
    match = re.search(r"<!--\s*mcp-name:\s*(\S+)\s*-->", README)
    assert match, "README.md carries no `<!-- mcp-name: ... -->` marker"
    return match.group(1)


def test_the_server_name_matches_the_readme_marker_the_registry_checks():
    marker = _readme_marker()
    assert SERVER["name"] == marker, (
        "README marker %r != server.json name %r — the registry's ownership check "
        "compares exactly these two and would reject the upload" % (marker, SERVER["name"])
    )


def test_the_server_name_is_a_namespaced_reverse_dns_name():
    assert NAME_PATTERN.match(SERVER["name"]), (
        "%r is not io.github.<owner>/<server> — GitHub auth publishes only under "
        "that namespace" % SERVER["name"]
    )
    assert SERVER["name"].count("/") == 1, "the schema requires exactly one slash"


def test_the_listing_versions_agree_with_the_package():
    declared = _pyproject_value("version")
    assert SERVER["version"] == declared, (
        "server.json says %s, pyproject says %s" % (SERVER["version"], declared)
    )
    pkg = SERVER["packages"][0]
    assert pkg["version"] == declared, (
        "the package entry says %s, pyproject says %s" % (pkg["version"], declared)
    )


def test_the_package_identifier_is_the_distribution_name():
    assert SERVER["packages"][0]["identifier"] == _pyproject_value("name"), (
        "the registry points at a different PyPI distribution than this project builds"
    )


def test_the_entry_point_the_uvx_convention_invokes_exists():
    """`uvx <identifier> <args>` runs an executable named after the package.

    Without a `conceptio-search` script the resolver finds nothing to run, so the
    listing would advertise an install that fails on every client.
    """
    identifier = SERVER["packages"][0]["identifier"]
    match = re.search(r'(?m)^%s\s*=\s*"([^"]+)"' % re.escape(identifier), PYPROJECT)
    assert match, (
        "server.json advertises `uvx %s ...` but pyproject declares no `%s` "
        "console script" % (identifier, identifier)
    )
    # Both names must reach the same CLI, so `conceptio-search mcp` == `conceptio mcp`.
    assert match.group(1) == _pyproject_value_target("conceptio"), (
        "the %r script and `conceptio` must share an entry point" % identifier
    )


def _pyproject_value_target(script: str) -> str:
    match = re.search(rf'(?m)^{re.escape(script)}\s*=\s*"([^"]+)"', PYPROJECT)
    assert match, f"pyproject.toml declares no {script} console script"
    return match.group(1)


def test_the_listing_declares_the_stdio_transport_the_server_actually_speaks():
    pkg = SERVER["packages"][0]
    assert pkg["registryType"] == "pypi"
    assert pkg["transport"] == {"type": "stdio"}
    assert pkg["runtimeHint"] == "uvx"
    # `conceptio mcp` is the subcommand that starts the stdio server.
    values = [a.get("value") for a in pkg.get("packageArguments", [])]
    assert "mcp" in values, (
        "the package arguments must invoke the `mcp` subcommand, or the client "
        "launches the plain CLI and the handshake never happens"
    )


def test_the_declared_api_key_is_one_the_auth_gate_accepts():
    import os

    from conceptio_cli.config import has_credential

    pkg = SERVER["packages"][0]
    names = [v["name"] for v in pkg.get("environmentVariables", [])]
    assert names, "no environment variable is declared — an MCP client cannot auth"
    accepted = {"CONCEPTIO_API_KEY", "CONCEPTIO_LICENSE_KEY", "CONCEPTIO_BEARER_TOKEN"}
    assert set(names) <= accepted, (
        "the listing advertises env vars the server ignores: %s" % (set(names) - accepted)
    )
    # An EMPTY config bypasses ~/.conceptio/config.json, so the assertion proves the
    # environment path rather than whatever happens to be saved on the machine
    # running the suite — a green check that only reflects a local file proves nothing.
    empty = {"api_key": "", "license_key": "", "bearer_token": ""}
    saved = {n: os.environ.pop(n, None) for n in accepted}
    try:
        assert not has_credential(empty), "an empty config must not authenticate"
        os.environ[names[0]] = "ckey_live_test"
        assert has_credential(empty), (
            "%s is declared but does not satisfy has_credential()" % names[0]
        )
    finally:
        for n, prior in saved.items():
            if prior is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = prior


def test_the_description_fits_the_schema_limit():
    # ServerDetail.description: maxLength 100.
    assert 1 <= len(SERVER["description"]) <= 100, (
        "description is %d chars; the schema caps it at 100" % len(SERVER["description"])
    )
    assert SERVER["$schema"] == SCHEMA, "server.json pins an unexpected schema version"
