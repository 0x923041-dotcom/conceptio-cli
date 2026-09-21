"""Local configuration for the Conceptio CLI.

Stored at ``~/.conceptio/config.json``. Holds the API base, the optional Pro
license key, and user preferences. This tool is fully self-contained and
public-facing — it talks only to the public ``https://www.conceptio.app``
endpoint and stores its own local config.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_DIR = Path.home() / ".conceptio"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_API_BASE = "https://www.conceptio.app"

DEFAULT_CONFIG = {
    "api_base": DEFAULT_API_BASE,
    "license_key": "",
    "api_key": "",
    "bearer_token": "",
    "default_limit": 10,
    "default_citation_format": "bibtex",
}


def load_config() -> Dict[str, Any]:
    """Load config, merging any saved values over the defaults."""
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = DEFAULT_CONFIG.copy()
        if isinstance(data, dict):
            merged.update(data)
        return merged
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(cfg: Dict[str, Any]) -> None:
    """Write the config, kept readable only by its owner.

    The file holds a long-lived credential (``ckey_live_…``), so it is created
    0600 inside a 0700 directory rather than at the process umask — the usual
    0644 leaves the key readable by every account on a shared machine, and no
    amount of hashing server-side makes a copied key stop working. The trailing
    ``chmod`` is not redundant: ``O_CREAT``'s mode applies only to a file it
    actually creates, so a config written by an earlier version of the CLI keeps
    its loose mode until something repairs it.

    Windows has no POSIX mode bits (``os.chmod`` there only toggles the
    read-only attribute), so the modes are applied on POSIX only.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg, indent=2)
    fd = os.open(str(CONFIG_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    if os.name == "posix":
        os.chmod(CONFIG_DIR, 0o700)
        os.chmod(CONFIG_FILE, 0o600)


def set_license_key(key: str) -> None:
    cfg = load_config()
    cfg["license_key"] = key.strip()
    cfg["api_key"] = ""
    # A bearer token outranks both key kinds, so a stale one left behind would
    # keep winning over the key just saved (`auth` would report the key as
    # accepted, then every later command would send the dead token). Nothing in
    # this CLI ever writes a bearer token to disk; it is handed in per-run.
    cfg["bearer_token"] = ""
    save_config(cfg)


def get_license_key() -> str:
    return str(load_config().get("license_key", "") or "")


def set_api_key(key: str) -> None:
    """Save a self-hosted API key (ckey_live_...). Setting one clears any
    stale license key or bearer token so the key just saved is the one every
    later command actually sends."""
    cfg = load_config()
    cfg["api_key"] = key.strip()
    cfg["license_key"] = ""
    cfg["bearer_token"] = ""
    save_config(cfg)


def get_api_key() -> str:
    return str(load_config().get("api_key", "") or "")


def has_credential(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Can this process authenticate? Config file **or** environment.

    One predicate, because there are two callers who must never disagree: the
    CLI's own gate (`cli.require_auth`) and the MCP server's per-call check.
    They did disagree — the gate accepted `CONCEPTIO_API_KEY`, then every tool
    call in the running server answered "Authentication required", because that
    check read only the config file. Env credentials are precisely how a host
    process (an editor, an MCP client, CI) supplies a key without writing to
    `~/.conceptio/config.json`, so the mismatch broke the documented path.

    Pass ``cfg`` when the caller already loaded it, so the file is read once.
    """
    values = load_config() if cfg is None else cfg
    for key in ("api_key", "license_key", "bearer_token"):
        if str(values.get(key) or "").strip():
            return True
    for name in ("CONCEPTIO_API_KEY", "CONCEPTIO_LICENSE_KEY", "CONCEPTIO_BEARER_TOKEN"):
        if os.environ.get(name, "").strip():
            return True
    return False


AUTH_REQUIRED_HINT = (
    "Authentication required — save an API key before searching.\n"
    "  1. Sign in at https://www.conceptio.app\n"
    "  2. Create a key on your profile (Free, Dev, Pro, or Enterprise)\n"
    "  3. Run: conceptio auth ckey_live_..."
)
