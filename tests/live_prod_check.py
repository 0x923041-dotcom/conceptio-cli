#!/usr/bin/env python3
"""Production smoke check: the real `conceptio` CLI against the real API.

`live_check.py` is this file's deliberate opposite, and the pair is the point.
That harness forces every network check onto a loopback stub precisely so it can
never reach production; this one reaches production *on purpose*, so its safety
has to come from somewhere else:

  * **The base is asserted before anything runs.** If the child environment could
    be pointed at a stub, this file would report "production verified" from a
    loopback server. It refuses to run unless the base is the shipped default.
  * **The credential is a literal placeholder**, never read from the shell or
    `~/.conceptio/config.json`, and any inherited `CONCEPTIO_*` credential is
    removed from the child environment. A harness that drives production must
    not borrow a real key from whoever happened to run it.
  * **Only commands that cannot spend anything are run:** the reads the API keeps
    open without a credential (`/api/health`, `/api/me`), plus data commands
    whose expected outcome is a refusal. No search ever executes.
  * **A throwaway config dir**, so the real `~/.conceptio/config.json` is never
    read or written.

What that buys: the offline suite and the stub harness both pin the CLI against
a server we control, so neither can notice the *live* API moving underneath it —
a renamed field in `/api/me`, a refusal message that changes wording, an edge
rule that starts rejecting the CLI's transport. Those all ship silently, because
the CLI's own suite stays green while every real user sees the drift.

Usage:  python tests/live_prod_check.py --yes     (skips loudly without --yes)
Env:    CONCEPTIO_CLI  command for the CLI (default: ./.venv/Scripts/conceptio, else PATH)
        CONCEPTIO_PROD_VERBOSE=1 to echo each command's truncated output
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO))

VERBOSE = os.environ.get("CONCEPTIO_PROD_VERBOSE") == "1"
PRODUCTION = "https://www.conceptio.app"

# A key that cannot be valid and cannot spend: the point is the refusal, and a
# placeholder is the only credential this file is allowed to hold.
PLACEHOLDER_KEY = "ckey_live_prod_smoke_placeholder"

CHECKS = []
FAILURES = []


def check(name):
    def register(fn):
        CHECKS.append((name, fn))
        return fn
    return register


def source_version():
    try:
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else ""


def cli_command():
    explicit = os.environ.get("CONCEPTIO_CLI", "").strip()
    if explicit:
        return shlex.split(explicit)
    for candidate in (REPO / ".venv" / "Scripts" / "conceptio.exe", REPO / ".venv" / "bin" / "conceptio"):
        if candidate.exists():
            return [str(candidate)]
    found = shutil.which("conceptio")
    if found:
        return [found]
    return [sys.executable, "-m", "conceptio_cli"]


class Prod:
    """The CLI pointed at production, holding a placeholder and nothing else."""

    def __init__(self, command, home, env=None):
        self.command = command
        self.home = home
        if env is not None:
            self.env = env
            return
        self.env = dict(os.environ)
        # Never inherit a real credential, and never point anywhere but production.
        for name in ("CONCEPTIO_API_KEY", "CONCEPTIO_LICENSE_KEY", "CONCEPTIO_BEARER_TOKEN"):
            self.env.pop(name, None)
        self.env.pop("CONCEPTIO_API_BASE", None)
        self.env["HOME"] = str(home)
        self.env["USERPROFILE"] = str(home)
        self.env["CONCEPTIO_API_KEY"] = PLACEHOLDER_KEY

    def with_home(self, home):
        """The same caller against a config dir nothing has written to.

        `auth` writes `~/.conceptio/config.json`, so its check gets a profile of
        its own rather than sharing the one the rest of the run reads.
        """
        env = dict(self.env)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        return Prod(self.command, home, env)

    def run(self, *args, timeout=60, key=None):
        env = dict(self.env)
        if key is not None:
            env["CONCEPTIO_API_KEY"] = key
            if not key:
                env.pop("CONCEPTIO_API_KEY", None)
        argv = self.command + [str(a) for a in args]
        proc = subprocess.run(
            argv, cwd=str(REPO), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        if VERBOSE:
            sys.stdout.write("    $ %s\n" % " ".join(argv[1:]))
            for label, text in (("out", proc.stdout), ("err", proc.stderr)):
                for line in (text or "").strip().splitlines()[:6]:
                    sys.stdout.write("      %s| %s\n" % (label, line))
        return proc


AUTH_MARKERS = ("Authentication required", "conceptio auth")
QUOTA_MARKERS = ("Rate limit", "credit quota exhausted", "Dev plan (EUR")


def assert_refused_cleanly(proc, label):
    """A refusal must be the CLI's mapped hint: no traceback, no wrong reason."""
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0, "%s should exit non-zero when refused" % label
    assert "Traceback" not in out, "%s leaked a Python traceback:\n%s" % (label, out)
    assert any(marker in out for marker in AUTH_MARKERS), (
        "%s did not report the authentication hint. Output:\n%s" % (label, out)
    )
    assert not any(marker in out for marker in QUOTA_MARKERS), (
        "%s reported a rate-limit/quota problem for an authentication refusal. Output:\n%s"
        % (label, out)
    )


# ── the guard: none of this is a result if it did not reach production ───────

@check("the CLI's shipped default base is production")
def _default_base_is_production(prod):
    from conceptio_cli.config import DEFAULT_API_BASE

    assert DEFAULT_API_BASE == PRODUCTION, (
        "shipped default base is %r, not %r — this file's premise is that running "
        "the CLI unconfigured reaches production" % (DEFAULT_API_BASE, PRODUCTION)
    )
    assert "CONCEPTIO_API_BASE" not in prod.env, "the child must not be re-pointed"


@check("--version matches this checkout (not a stale binary against prod)")
def _version_matches_tree(prod):
    declared = source_version()
    assert declared, "could not read version from pyproject.toml"
    proc = prod.run("--version")
    assert proc.returncode == 0, "--version failed: %s" % proc.stderr
    advertised = (proc.stdout or "").strip()
    assert advertised.endswith(declared), (
        "binary advertises %r but this tree declares %s — the harness would be "
        "testing a different build than the one under review" % (advertised, declared)
    )


# ── the reads the API keeps open without a credential ───────────────────────

@check("quota (keyless) parses the live /api/me payload")
def _quota_keyless(prod):
    proc = prod.run("quota", "--json", key="")
    assert proc.returncode == 0, "quota exited %d: %s" % (proc.returncode, proc.stderr)
    data = json.loads(proc.stdout)
    for field in ("tier", "auth", "email"):
        assert field in data, "live /api/me no longer carries %r — the CLI reads %r" % (field, sorted(data))
    assert data["tier"] == "public" and data["auth"] == "public", (
        "a keyless quota should read the public tier, got %r" % data["tier"]
    )


@check("quota renders for a human without --json")
def _quota_human(prod):
    proc = prod.run("quota", key="")
    assert proc.returncode == 0, proc.stderr
    assert (proc.stdout or "").strip(), "quota printed nothing"


@check("auth warns instead of claiming an unknown key was accepted")
def _auth_does_not_vouch_for_a_rejected_key(prod):
    """The live API answers /api/me with the public tier for an unknown key, so
    'accepted' must not be printable from that response alone."""
    home = prod.home.parent / "home-auth"
    home.mkdir(exist_ok=True)
    proc = prod.with_home(home).run("auth", PLACEHOLDER_KEY, key="")
    assert proc.returncode == 0, "auth exited %d: %s" % (proc.returncode, proc.stderr)
    out = proc.stdout or ""
    assert "Key accepted" not in out, (
        "auth claimed a placeholder key was accepted — /api/me cannot justify that. Output:\n%s" % out
    )
    assert "double-check" in out or "still reports the free tier" in out, (
        "auth did not warn that the key looks rejected. Output:\n%s" % out
    )


# ── the data commands: refused, mapped, and never executed ──────────────────

@check("search refuses locally, without a network attempt")
def _search_keyless_is_local(prod):
    proc = prod.run("search", "attention is all you need", key="")
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0
    assert any(marker in out for marker in AUTH_MARKERS), out
    # A local refusal never mentions an HTTP status; if it does, the gate moved
    # server-side and this check is no longer testing what it claims.
    for status in ("401", "403", "429", "HTTP"):
        assert status not in out, (
            "keyless search reached the network (output mentions %s) — the local "
            "gate is supposed to refuse before any request. Output:\n%s" % (status, out)
        )


@check("search with an unknown key is refused as authentication, not as quota")
def _search_placeholder_is_auth(prod):
    assert_refused_cleanly(prod.run("search", "attention is all you need"), "search")


@check("info/cite/proof are refused the same way (whole data surface, one shape)")
def _other_data_commands(prod):
    assert_refused_cleanly(prod.run("info", "1"), "info")
    assert_refused_cleanly(prod.run("cite", "1", "--format", "bibtex"), "cite")
    assert_refused_cleanly(prod.run("proof", "1"), "proof")


@check("a refused command never leaks the placeholder or a raw server body")
def _no_credential_echo(prod):
    proc = prod.run("search", "attention is all you need")
    out = (proc.stdout or "") + (proc.stderr or "")
    assert PLACEHOLDER_KEY not in out, "the CLI echoed the credential back"
    assert '"detail"' not in out or "conceptio" in out.lower(), (
        "a raw server JSON body reached the user:\n%s" % out
    )


# ── runner ──────────────────────────────────────────────────────────────────

def main():
    if "--yes" not in sys.argv:
        print("production smoke check — SKIPPED, not a result.")
        print("  This touches https://www.conceptio.app for real. Re-run with --yes to run it.")
        print("  It uses a placeholder key and only runs reads the API keeps open, plus")
        print("  commands whose expected outcome is a refusal. No search ever executes.")
        return 0

    command = cli_command()
    tmp = Path(tempfile.mkdtemp(prefix="conceptio-prod-"))
    home = tmp / "home"
    home.mkdir()

    print("Conceptio CLI production smoke check — real CLI, real API, placeholder key")
    print("  cli      %s  (this tree declares %s)" % (" ".join(command), source_version() or "?"))
    print("  base     %s  (the CLI's own default; not overridable here)" % PRODUCTION)
    print("  key      %s  (placeholder — cannot be valid, cannot spend)" % PLACEHOLDER_KEY)
    print("")

    prod = Prod(command, home)
    try:
        for index, (name, fn) in enumerate(CHECKS, 1):
            start = time.monotonic()
            try:
                fn(prod)
                print("  %2d/%d  ok    %s  (%.1fs)" % (index, len(CHECKS), name, time.monotonic() - start))
            except Exception as error:  # noqa: BLE001 — one failing check must not stop the run
                FAILURES.append((name, error))
                print("  %2d/%d  FAIL  %s  (%.1fs)" % (index, len(CHECKS), name, time.monotonic() - start))
                print("        %s" % str(error).replace("\n", "\n        "))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("")
    if FAILURES:
        print("%d of %d production checks failed" % (len(FAILURES), len(CHECKS)))
        return 1
    print("%d production checks passed" % len(CHECKS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
