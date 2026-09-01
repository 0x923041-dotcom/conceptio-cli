"""Local configuration for the Conceptio CLI.

Stored at ``~/.conceptio/config.json``. Holds the API base, the optional Pro
license key, and user preferences. Never touches any internal Lucida/Insula
infrastructure — this tool is fully self-contained and public-facing.
"""

import json
from pathlib import Path
from typing import Any, Dict

CONFIG_DIR = Path.home() / ".conceptio"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_API_BASE = "https://www.conceptio.app"

DEFAULT_CONFIG = {
    "api_base": DEFAULT_API_BASE,
    "license_key": "",
    "api_key": "",
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
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def set_license_key(key: str) -> None:
    cfg = load_config()
    cfg["license_key"] = key.strip()
    save_config(cfg)


def get_license_key() -> str:
    return str(load_config().get("license_key", "") or "")


def set_api_key(key: str) -> None:
    """Save a self-hosted API key (ckey_live_...). Setting one clears any
    stale license key so a client never sends two credentials at once."""
    cfg = load_config()
    cfg["api_key"] = key.strip()
    cfg["license_key"] = ""
    save_config(cfg)


def get_api_key() -> str:
    return str(load_config().get("api_key", "") or "")
