#!/usr/bin/env python3
"""Live check: the real `conceptio` CLI against a loopback stub.

The offline suite patches `httpx`, so it can prove the client composes a request
and parses a response — and nothing more. Everything the CLI *decides* from a
live exchange is invisible to it: whether a batch envelope renders, whether an
unfinished job is reported as a failure, whether the MCP server honors an
environment credential, whether a connector POST is the request it claims to
be. Those are the behaviors a machine caller depends on, so they get a harness
of their own that spawns the actual CLI and asserts on exit codes, on stdout,
and on the stub's own request log.

Everything stays on loopback: the stub is the shared one
(`conceptio-nvim/test/stub_api.py`), the credential is a placeholder, no
Conceptio account or production credit is touched, and the CLI is pointed at a
throwaway config directory so the real `~/.conceptio/config.json` is never read
or written.

Usage:  python tests/live_check.py
Env:    CONCEPTIO_CLI     command for the CLI (default: ./.venv/Scripts/conceptio, else PATH, else `python -m conceptio_cli`)
        CONCEPTIO_STUB    path to stub_api.py (default: ../conceptio-nvim/test/stub_api.py)
        CONCEPTIO_PYTHON  interpreter used to run the stub (default: this one)
        CONCEPTIO_LIVE_VERBOSE=1 to echo each command's truncated output
"""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STACK = REPO.parent

STUB_PY = Path(os.environ.get("CONCEPTIO_STUB") or (STACK / "conceptio-nvim" / "test" / "stub_api.py"))
STUB_KEY = "ckey_live_local_stub"
VERBOSE = os.environ.get("CONCEPTIO_LIVE_VERBOSE") == "1"

BATCH = [{"q": "zero trust"}, {"q": "transformer"}]


# ── the CLI under test ───────────────────────────────────────────────────────

def cli_command():
    """Resolve the CLI the way a user's shell would: explicit, then install, then source."""
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


def free_port():
    """Ask the OS for a port nothing is listening on, then let go of it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def config_home(home: Path, default_base: str) -> dict:
    """A child environment with a throwaway config dir and an env-only credential.

    `USERPROFILE` is what `Path.home()` reads on Windows and `HOME` on POSIX;
    both are set so neither platform can fall back to the real profile.

    `CONCEPTIO_API_BASE` defaults to the stub, not to production: a check that
    forgets to name a stub then fails against loopback instead of quietly
    calling conceptio.app with a placeholder key. (That is not hypothetical —
    an early version of this file omitted it on the MCP checks and reached the
    real API, where the 401 read exactly like the defect the check was hunting.)
    """
    env = dict(os.environ)
    env.pop("CONCEPTIO_LICENSE_KEY", None)
    env.pop("CONCEPTIO_BEARER_TOKEN", None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["CONCEPTIO_API_KEY"] = STUB_KEY
    env["CONCEPTIO_API_BASE"] = default_base
    return env


class Live:
    def __init__(self, command, env, cwd):
        self.command = command
        self.env = env
        self.cwd = cwd

    def run(self, *args, stdin_text=None, timeout=60, base=None, key=None, extra_env=None):
        env = dict(self.env)
        # Every network-touching check MUST pass `base`. Without it the CLI falls
        # back to the production API base, so a "live" check would quietly make a
        # real request with a placeholder key — the one thing this harness exists
        # to avoid. (It happened: an mcp check without `base` reached
        # conceptio.app and came back 401, which read as a product bug.)
        if base:
            env["CONCEPTIO_API_BASE"] = base
        if key is not None:
            env["CONCEPTIO_API_KEY"] = key
        if extra_env:
            env.update(extra_env)
        argv = self.command + [str(a) for a in args]
        proc = subprocess.run(
            argv, cwd=str(self.cwd), env=env, input=stdin_text,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        if VERBOSE:
            sys.stdout.write("    $ %s\n" % " ".join(argv[1:]))
            for label, text in (("out", proc.stdout), ("err", proc.stderr)):
                for line in (text or "").strip().splitlines()[:6]:
                    sys.stdout.write("      %s| %s\n" % (label, line))
        return proc


# ── the shared stub ─────────────────────────────────────────────────────────

class Stub:
    """One stub process, plus the request log it writes when verbose.

    A job-mode instance is how a *scenario* is dialled: `running` never finishes
    (so a `--wait` budget can be exhausted), `expired` reports a job the server
    gave up on. Nothing is smuggled through query text.
    """

    def __init__(self, job_mode="done", log_path=None):
        self.job_mode = job_mode
        self.log_path = log_path
        port = free_port()
        self.base = "http://127.0.0.1:%d" % port
        env = dict(os.environ)
        env["CONCEPTIO_STUB_JOB_MODE"] = job_mode
        env["CONCEPTIO_STUB_VERBOSE"] = "1" if log_path else "0"
        env["PYTHONUNBUFFERED"] = "1"
        python = os.environ.get("CONCEPTIO_PYTHON") or sys.executable
        # The stub traces on stderr, so stderr *is* the log; its startup line
        # rides along and is ignored by the `since` filter.
        sink = open(log_path, "ab") if log_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(
            [python, str(STUB_PY), str(port)], cwd=str(STUB_PY.parent), env=env,
            stdout=sink, stderr=sink,
        )
        self._await_ready()

    def _await_ready(self, timeout=20.0):
        """Wait for the stub to answer, not merely to have printed something."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("the stub exited before it served a request (job mode %s)" % self.job_mode)
            try:
                urllib.request.urlopen(self.base + "/api/me", timeout=1).read()
                return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("the stub never answered on %s (job mode %s)" % (self.base, self.job_mode))

    def lines(self):
        if not self.log_path or not os.path.exists(self.log_path):
            return []
        with open(self.log_path, "r", encoding="utf-8", errors="replace") as handle:
            return [line for line in handle.read().splitlines() if line.strip()]

    def mark(self):
        """Where the log ends now — so a check reads only its OWN requests.

        The log is shared by every check in the run; without this, a request an
        earlier check made can satisfy a later check's assertion, and "it passed"
        becomes indistinguishable from "someone else passed".
        """
        return len(self.lines())

    def since(self, mark, needle):
        return [line for line in self.lines()[mark:] if needle in line]

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ── checks ──────────────────────────────────────────────────────────────────

FAILURES = []
CHECKS = []


def check(name):
    def register(fn):
        CHECKS.append((name, fn))
        return fn
    return register


class Failed(AssertionError):
    pass


def expect(condition, message):
    if not condition:
        raise Failed(message)


def exit_is(proc, want, what):
    expect(proc.returncode == want,
           "%s: expected exit %s, got %s\n    stdout: %s\n    stderr: %s"
           % (what, want, proc.returncode, proc.stdout.strip()[:400], proc.stderr.strip()[:400]))


def stdout_json(proc, what):
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise Failed("%s: stdout was not JSON\n    stdout: %s\n    stderr: %s"
                     % (what, proc.stdout.strip()[:400], proc.stderr.strip()[:400]))


def run_batch(live, stub, *extra, job_mode=None):
    """`search --batch …` as a caller would type it, with an env-only credential."""
    return live.run("search", "--batch", str(live.cwd / "queries.json"), *extra,
                    base=stub.base, timeout=120)


def define_checks(live, main, running, expired):
    """Build the check list from the three stub instances in play."""
    queries = live.cwd / "queries.json"
    queries.write_text(json.dumps(BATCH), encoding="utf-8")

    @check("search — a plain GET renders the archive's answer")
    def _search():
        mark = main.mark()
        proc = live.run("search", "zero trust", "--json", base=main.base)
        exit_is(proc, 0, "search")
        data = stdout_json(proc, "search")
        expect(data["total"] == 418 and len(data["results"]) == 3,
               "search: unexpected payload %s" % str(data)[:200])
        expect(main.since(mark, "GET /api/search"), "search: the stub saw no GET /api/search")

    @check("quota --json — structured tier on stdout, env credential honored")
    def _quota():
        proc = live.run("quota", "--json", base=main.base)
        exit_is(proc, 0, "quota --json")
        data = stdout_json(proc, "quota")
        expect(data.get("tier") == "dev" and data.get("auth") == "api_key",
               "quota --json: unexpected payload %s" % str(data)[:200])
        expect("Authentication required" not in proc.stdout,
               "quota --json: env credential was refused")

    @check("search --batch --sync — one POST, both queries in the envelope")
    def _sync_batch():
        mark = main.mark()
        proc = run_batch(live, main, "--sync", "--json")
        exit_is(proc, 0, "sync batch")
        data = stdout_json(proc, "sync batch")
        expect(data.get("count") == 2, "sync batch: count=%r" % data.get("count"))
        expect([q["query"] for q in data["queries"]] == ["zero trust", "transformer"],
               "sync batch: queries came back as %s" % [q.get("query") for q in data["queries"]])
        expect(all(len(q["results"]) == 3 for q in data["queries"]),
               "sync batch: a subquery lost its results")
        expect(main.since(mark, "POST /api/search/batch"),
               "sync batch: the stub saw no POST /api/search/batch")

    @check("search --batch — a queued job returns a handle, not a result")
    def _queue():
        mark = main.mark()
        proc = run_batch(live, main, "--json")
        exit_is(proc, 0, "queue")
        data = stdout_json(proc, "queue")
        expect(data.get("id"), "queue: no job id in %s" % str(data)[:200])
        expect(data.get("status") == "queued", "queue: status=%r" % data.get("status"))
        expect(main.since(mark, "POST /api/search/jobs"),
               "queue: the stub saw no POST /api/search/jobs")

    @check("search-job — a hand poll gets the finished envelope")
    def _poll():
        queued = stdout_json(run_batch(live, main, "--json"), "queue")
        mark = main.mark()
        proc = live.run("search-job", queued["id"], "--json", base=main.base)
        exit_is(proc, 0, "search-job")
        data = stdout_json(proc, "search-job")
        expect(data.get("status") == "done", "search-job: status=%r" % data.get("status"))
        expect((data.get("result") or {}).get("count") == 2,
               "search-job: no finished envelope in %s" % str(data)[:200])
        expect(main.since(mark, "GET /api/search/jobs/%s" % queued["id"]),
               "search-job: the stub saw no job poll")

    @check("search --batch --wait — a finished job renders its results")
    def _wait_done():
        proc = run_batch(live, main, "--wait", "--json", "--timeout", "20")
        exit_is(proc, 0, "batch --wait (done)")
        data = stdout_json(proc, "batch --wait (done)")
        expect(data.get("count") == 2, "batch --wait (done): count=%r" % data.get("count"))

    @check("--json before the subcommand — the global alias is accepted")
    def _global_json():
        proc = live.run("--json", "search", "--batch", str(queries), "--sync", base=main.base)
        exit_is(proc, 0, "global --json")
        data = stdout_json(proc, "global --json")
        expect(data.get("count") == 2, "global --json: count=%r" % data.get("count"))

    @check("save --to zotero — the connector POST is the one documented")
    def _zotero():
        mark = main.mark()
        proc = live.run("save", "--to", "zotero", "7288", base=main.base)
        exit_is(proc, 0, "save --to zotero")
        expect(main.since(mark, "POST /api/connectors/zotero/send"),
               "save --to zotero: the stub saw no send")

    @check("save --to obsidian — authorize, then a logged metadata handoff")
    def _obsidian():
        mark = main.mark()
        # The URI is handed to the OS, not printed: on a box with Obsidian
        # installed this command can open the app — and "whatever vault is open"
        # is the developer's real one (here, a git worktree). So the check points
        # BROWSER at an interpreter that ignores the argument: `webbrowser` tries
        # the user's choice first and stops there, so the platform opener is
        # never reached and this check cannot write a note into anyone's vault.
        # The URI's own text is asserted offline instead (handle_save's
        # not-opened branch), where nothing can be launched.
        guard = os.environ.get("CONCEPTIO_LIVE_OPENER") or sys.executable
        proc = live.run("save", "--to", "obsidian", "7288", base=main.base,
                        extra_env={"BROWSER": guard})
        exit_is(proc, 0, "save --to obsidian")
        expect("Obsidian" in proc.stdout,
               "save --to obsidian: nothing names Obsidian: %s" % proc.stdout.strip()[:200])
        expect(main.since(mark, "POST /api/connectors/obsidian/authorize"),
               "save --to obsidian: the stub saw no authorize")
        expect(main.since(mark, "POST /api/connectors/obsidian/log"),
               "save --to obsidian: the handoff was never logged")

    @check("mcp — a tool call works on an environment credential alone")
    def _mcp():
        mark = main.mark()
        request = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "conceptio_search_batch",
                       "arguments": {"queries": BATCH, "sync": True}},
        })
        proc = live.run("mcp", stdin_text=request + "\n", timeout=90, base=main.base)
        exit_is(proc, 0, "mcp tools/call")
        line = next((l for l in proc.stdout.splitlines() if l.strip().startswith("{")), "")
        expect(line, "mcp tools/call: stdout carried no JSON-RPC line: %s" % proc.stdout.strip()[:300])
        reply = json.loads(line)
        expect("error" not in reply, "mcp tools/call: JSON-RPC error %s" % str(reply.get("error"))[:200])
        content = json.dumps(reply.get("result", {}))
        expect("Authentication required" not in content,
               "mcp tools/call: the server refused an env-only credential — the 0.3.1 defect is back")
        expect("zero trust" in content, "mcp tools/call: the batch envelope never arrived")
        expect(main.since(mark, "POST /api/search/batch"),
               "mcp tools/call: the stub saw no batch POST")

    @check("mcp — tools/list advertises the whole surface")
    def _mcp_tools():
        proc = live.run("mcp", stdin_text=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n", timeout=90, base=main.base)
        exit_is(proc, 0, "mcp tools/list")
        names = [t["name"] for t in json.loads(proc.stdout.strip().splitlines()[-1])["result"]["tools"]]
        expect(len(names) == 8, "mcp tools/list: %d tools, README says 8: %s" % (len(names), names))
        expect("conceptio_search_batch" in names, "mcp tools/list: batch tool missing")

    @check("search-job --wait on a RUNNING job — exit 1, never a silent success")
    def _timeout_job():
        proc = live.run("search-job", "job_stub_0001", "--wait", "--timeout", "2", base=running.base)
        exit_is(proc, 1, "search-job --wait (timeout)")
        combined = proc.stdout + proc.stderr
        expect("job_stub_0001" in combined and "running" in combined,
               "search-job --wait (timeout): the error does not name the job and its status: %s"
               % combined.strip()[:300])

    @check("search --batch --wait on a RUNNING job — exit 1, not \"no results\"")
    def _timeout_batch():
        proc = run_batch(live, running, "--wait", "--json", "--timeout", "2")
        exit_is(proc, 1, "batch --wait (timeout)")
        expect("No query results returned by the batch" not in proc.stdout,
               "batch --wait (timeout): a still-running job rendered as an empty result set")
        expect("running" in (proc.stdout + proc.stderr),
               "batch --wait (timeout): the status is not reported")

    @check("search-job --wait on a RUNNING job --json — stdout stays parseable")
    def _timeout_json():
        proc = live.run("search-job", "job_stub_0001", "--wait", "--json", "--timeout", "2",
                        base=running.base)
        exit_is(proc, 1, "search-job --wait --json (timeout)")
        expect("job_stub_0001" in proc.stderr and proc.stdout.strip() == "",
               "search-job --wait --json: the error leaked into stdout: %r / %r"
               % (proc.stdout[:200], proc.stderr[:200]))

    @check("search-job (no --wait) on a RUNNING job — a hand poll still gets its snapshot")
    def _poll_running():
        proc = live.run("search-job", "job_stub_0001", "--json", base=running.base)
        exit_is(proc, 0, "search-job (no --wait)")
        data = stdout_json(proc, "search-job (no --wait)")
        expect(data.get("status") == "running",
               "search-job (no --wait): status=%r — the timeout fix overreached into plain polling"
               % data.get("status"))

    @check("--timeout 0 — rejected before a single poll")
    def _bad_timeout():
        proc = live.run("search-job", "job_stub_0001", "--wait", "--timeout", "0", base=running.base)
        exit_is(proc, 1, "--timeout 0")
        expect("--timeout must be a positive number" in (proc.stdout + proc.stderr),
               "--timeout 0: wrong message: %s" % (proc.stdout + proc.stderr).strip()[:200])

    @check("search --batch --wait on an EXPIRED job — exit 1, says so")
    def _expired():
        proc = run_batch(live, expired, "--wait", "--json", "--timeout", "10")
        exit_is(proc, 1, "batch --wait (expired)")
        expect("expired" in (proc.stdout + proc.stderr).lower(),
               "batch --wait (expired): the tombstone is not reported")

    @check("search --batch on a missing file — a local error, exit 1")
    def _missing():
        proc = live.run("search", "--batch", str(live.cwd / "nope.json"), base=main.base)
        exit_is(proc, 1, "missing batch file")
        expect("Could not read batch JSON" in (proc.stdout + proc.stderr),
               "missing batch file: wrong message")

    @check("search --batch --sync with 11 queries — refused locally, exit 1")
    def _too_many():
        eleven = live.cwd / "eleven.json"
        eleven.write_text(json.dumps([{"q": "q%d" % i} for i in range(11)]), encoding="utf-8")
        proc = live.run("search", "--batch", str(eleven), "--sync", base=main.base)
        exit_is(proc, 1, "sync with 11")
        expect("at most 10" in (proc.stdout + proc.stderr),
               "sync with 11: wrong message: %s" % (proc.stdout + proc.stderr).strip()[:200])


# ── runner ──────────────────────────────────────────────────────────────────

def main():
    if not STUB_PY.exists():
        print("live check: stub not found at %s — set CONCEPTIO_STUB" % STUB_PY)
        return 2
    command = cli_command()
    tmp = Path(tempfile.mkdtemp(prefix="conceptio-live-"))
    home = tmp / "home"
    home.mkdir()
    logs = tmp / "stubs"
    logs.mkdir()

    print("Conceptio CLI live check — real CLI, loopback stub")
    print("  cli      %s" % " ".join(command))
    print("  stub     %s" % STUB_PY)

    stubs = []
    try:
        main_stub = Stub("done", logs / "main.log")
        running_stub = Stub("running", logs / "running.log")
        expired_stub = Stub("expired", logs / "expired.log")
        stubs = [main_stub, running_stub, expired_stub]
        print("  origins  main=%s running=%s expired=%s"
              % (main_stub.base, running_stub.base, expired_stub.base))

        live = Live(command, config_home(home, main_stub.base), tmp)
        define_checks(live, main_stub, running_stub, expired_stub)
        print("")

        for index, (name, fn) in enumerate(CHECKS, 1):
            start = time.monotonic()
            try:
                fn()
                print("  %2d/%d  ok    %s  (%.1fs)" % (index, len(CHECKS), name, time.monotonic() - start))
            except Exception as error:  # noqa: BLE001 — a failing check must not stop the run
                FAILURES.append((name, error))
                print("  %2d/%d  FAIL  %s  (%.1fs)" % (index, len(CHECKS), name, time.monotonic() - start))
                print("        %s" % str(error).replace("\n", "\n        "))
    finally:
        for stub in stubs:
            stub.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    print("")
    if FAILURES:
        print("%d of %d checks failed" % (len(FAILURES), len(CHECKS)))
        return 1
    print("%d checks passed" % len(CHECKS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
