"""The credential file's mode, and the one-credential invariant.

`~/.conceptio/config.json` holds a long-lived API key. Two properties matter
beyond "it round-trips": only its owner can read it, and saving one kind of
credential cannot leave an older, higher-precedence one behind to shadow it.
Both are the kind of claim that is easy to assume and cheap to measure.
"""

import json
import os
import stat

import pytest

import conceptio_cli.config as config

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point the module at a throwaway config file, never the real one."""
    directory = tmp_path / ".conceptio"
    monkeypatch.setattr(config, "CONFIG_DIR", directory)
    monkeypatch.setattr(config, "CONFIG_FILE", directory / "config.json")
    return directory / "config.json"


def _written(path):
    return json.loads(path.read_text(encoding="utf-8"))


@posix_only
def test_saved_config_is_readable_only_by_its_owner(isolated_config):
    """The file holds a `ckey_live_…`; at the usual umask it would be 0644 —
    readable by every account on a shared machine, and a copied key keeps
    working because the server only ever sees its hash."""
    config.save_config({"api_key": "ckey_live_secret"})
    mode = stat.S_IMODE(isolated_config.stat().st_mode)
    assert mode == 0o600, f"config file mode is {oct(mode)}, not 0600"
    dir_mode = stat.S_IMODE(isolated_config.parent.stat().st_mode)
    assert dir_mode == 0o700, f"config directory mode is {oct(dir_mode)}, not 0700"


@posix_only
def test_saving_repairs_a_loose_config_left_by_an_older_version(isolated_config):
    """`O_CREAT`'s mode applies only to a file it actually creates, so the loose
    mode an earlier version wrote would survive every later save."""
    isolated_config.parent.mkdir(parents=True)
    isolated_config.write_text("{}", encoding="utf-8")
    os.chmod(isolated_config, 0o644)

    config.save_config({"api_key": "ckey_live_secret"})

    assert stat.S_IMODE(isolated_config.stat().st_mode) == 0o600


def test_saving_an_api_key_clears_a_stale_bearer_token(isolated_config):
    """A bearer token outranks a machine key in the request headers, so one left
    on disk would keep winning over the key the user just saved."""
    isolated_config.parent.mkdir(parents=True)
    isolated_config.write_text(
        json.dumps({"bearer_token": "stale-session", "license_key": "CONCEPTIO-OLD"}),
        encoding="utf-8",
    )

    config.set_api_key("ckey_live_new")

    saved = _written(isolated_config)
    assert saved["api_key"] == "ckey_live_new"
    assert saved["license_key"] == ""
    assert saved["bearer_token"] == ""


def test_saving_a_license_key_clears_a_stale_bearer_token(isolated_config):
    isolated_config.parent.mkdir(parents=True)
    isolated_config.write_text(
        json.dumps({"bearer_token": "stale-session", "api_key": "ckey_live_old"}),
        encoding="utf-8",
    )

    config.set_license_key("CONCEPTIO-NEW")

    saved = _written(isolated_config)
    assert saved["license_key"] == "CONCEPTIO-NEW"
    assert saved["api_key"] == ""
    assert saved["bearer_token"] == ""
