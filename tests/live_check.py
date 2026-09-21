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
import re
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

sys.path.insert(0, str(REPO))
from tests.client_contract import CLIENT_CALLS, JSON_CALLS, calls_for  # noqa: E402

STUB_PY = Path(os.environ.get("CONCEPTIO_STUB") or (STACK / "conceptio-nvim" / "test" / "stub_api.py"))
STUB_KEY = "ckey_live_local_stub"
VERBOSE = os.environ.get("CONCEPTIO_LIVE_VERBOSE") == "1"

BATCH = [{"q": "zero trust"}, {"q": "transformer"}]


def source_version() -> str:
    """The version *this* checkout declares — the anchor for the `--version` check."""
    try:
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else ""


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

    def with_home(self, home):
        """The same caller with a config dir of its own.

        `auth` writes `~/.conceptio/config.json` and the refusal check needs a
        profile nothing has ever written to, so those two get a home of their
        own instead of sharing the one the rest of the run reads.
        """
        env = dict(self.env)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        return Live(self.command, env, self.cwd)

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
    gave up on. Nothing is smuggled through query text. `connector_mode` dials
    the same way for the connectors, whose failures arrive as a server-issued
    *reason* the CLI maps to a human message.
    """

    def __init__(self, job_mode="done", log_path=None, connector_mode="ok"):
        self.job_mode = job_mode
        self.connector_mode = connector_mode
        self.log_path = log_path
        port = free_port()
        self.base = "http://127.0.0.1:%d" % port
        env = dict(os.environ)
        env["CONCEPTIO_STUB_JOB_MODE"] = job_mode
        env["CONCEPTIO_STUB_CONNECTOR_MODE"] = connector_mode
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
                raise RuntimeError("the stub exited before it served a request (job mode %s, "
                                   "connector mode %s)" % (self.job_mode, self.connector_mode))
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


def define_checks(live, main, running, expired, connectors, work):
    """Build the check list from the stub instances in play (and a temp root)."""
    def mcp_call(caller, base, tool, arguments, timeout=90):
        """One `tools/call`, returning the process whose stdout carries the reply."""
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": tool, "arguments": arguments}}) + "\n"
        proc = caller.run("mcp", stdin_text=request, timeout=timeout, base=base)
        exit_is(proc, 0, "mcp %s" % tool)
        expect(next((l for l in proc.stdout.splitlines() if l.strip().startswith("{")), ""),
               "mcp %s: no JSON-RPC line on stdout: %s" % (tool, proc.stdout.strip()[:300]))
        return proc

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

    # ── the instrument itself ───────────────────────────────────────────────

    @check("--version — the binary under test is this tree, not a stale install")
    def _version():
        proc = live.run("--version", base=main.base)
        exit_is(proc, 0, "--version")
        reported = proc.stdout.strip().split()[-1]
        want = source_version()
        expect(want, "could not read `version` from pyproject.toml")
        expect(
            reported == want,
            "--version reports %s while this tree declares %s, so the binary under test is an "
            "older install and every check below is proving the wrong code. Refresh it "
            "(`pip install -e .`) or point CONCEPTIO_CLI at the binary you meant."
            % (reported, want),
        )

    # ── the read surface the offline suite can only mock ────────────────────

    @check("resolve — the identifier reaches the API verbatim; kind and results render")
    def _resolve():
        mark = main.mark()
        proc = live.run("resolve", "doi:10.1145/3290605.3300333", base=main.base)
        exit_is(proc, 0, "resolve")
        # Assert on the request, not on the reply: the stub answers a canned
        # identifier whatever it is asked (its own contract with the nvim
        # suite), so an echo in stdout would be the stub talking, not the CLI.
        expect("Kind:" in proc.stdout and "result(s):" in proc.stdout,
               "resolve: the typed answer is not rendered: %s" % proc.stdout.strip()[:200])
        line = "".join(main.since(mark, "GET /api/resolve"))
        expect(line, "resolve: the stub saw no GET /api/resolve")
        expect("doi%3A10.1145%2F3290605.3300333" in line,
               "resolve: the identifier was rewritten in transit: %s" % line)
        proc = live.run("resolve", "RFC 2119", "--json", base=main.base)
        exit_is(proc, 0, "resolve --json")
        expect(stdout_json(proc, "resolve --json").get("kind") == "rfc",
               "resolve --json: the API's kind did not survive into the payload")

    @check("search `source:` — stripped from the query, applied as a real filter")
    def _directives():
        mark = main.mark()
        proc = live.run("search", "source:nist zero trust", "--limit", "5", "--json", base=main.base)
        exit_is(proc, 0, "search source:")
        data = stdout_json(proc, "search source:")
        expect(data.get("query") == "zero trust",
               "search source:: the API received %r as the query — the directive is client-side "
               "syntax and the server does not parse it" % data.get("query"))
        line = "".join(main.since(mark, "GET /api/search"))
        expect("sources=nist" in line, "search source:: no source filter reached the API: %s" % line)
        expect("limit=5" in line, "search source:: --limit did not reach the API: %s" % line)

    @check("search filters — --lang/--offset/--license reach the request")
    def _filters():
        mark = main.mark()
        proc = live.run("search", "zero trust", "--lang", "en", "--offset", "20",
                        "--license", "commercial-ok", "--json", base=main.base)
        exit_is(proc, 0, "search filters")
        line = "".join(main.since(mark, "GET /api/search"))
        for needle in ("language=en", "offset=20", "license=commercial-ok"):
            expect(needle in line, "search filters: %s never reached the API: %s" % (needle, line))

    @check("search --markdown — note-ready markdown, not a terminal table")
    def _markdown():
        proc = live.run("search", "zero trust", "--markdown", base=main.base)
        exit_is(proc, 0, "search --markdown")
        expect("## Conceptio results" in proc.stdout,
               "search --markdown: no markdown heading: %s" % proc.stdout.strip()[:200])
        expect("**Zero Trust Architecture**" in proc.stdout and "[source]" in proc.stdout,
               "search --markdown: the entries are not markdown: %s" % proc.stdout.strip()[:300])

    @check("cite — the format rides as a query parameter; stdout is the citation")
    def _cite():
        mark = main.mark()
        proc = live.run("cite", "7288", "--format", "apa", base=main.base)
        exit_is(proc, 0, "cite")
        expect("conceptio7288" in proc.stdout, "cite: no citation on stdout: %r" % proc.stdout[:200])
        line = "".join(main.since(mark, "GET /api/cite/7288"))
        expect("format=apa" in line, "cite: the format never reached the API: %s" % line)

    @check("cite --format bogus — refused locally, before a single request")
    def _cite_bad_format():
        mark = main.mark()
        proc = live.run("cite", "7288", "--format", "vancouver", base=main.base)
        expect(proc.returncode != 0, "cite --format vancouver: accepted, exit 0")
        expect(not main.since(mark, "GET /api/cite"),
               "cite --format bogus: a request was sent anyway")

    @check("info / proof — metadata and the evidence bundle over the real endpoints")
    def _info_proof():
        mark = main.mark()
        proc = live.run("info", "2844", "--json", base=main.base)
        exit_is(proc, 0, "info --json")
        expect(str(stdout_json(proc, "info --json").get("id")) == "2844",
               "info --json: wrong document in the payload")
        expect(main.since(mark, "GET /api/document/2844"), "info: the stub saw no document GET")
        mark = main.mark()
        proc = live.run("proof", "2844", base=main.base)
        exit_is(proc, 0, "proof")
        expect("Proof bundle: document 2844" in proc.stdout,
               "proof: the bundle summary is missing: %s" % proc.stdout.strip()[:200])
        expect("sha256:" in proc.stdout, "proof: no content hash was rendered")
        # The bundle nests the document's identity under `document`, so a summary
        # reading the top level prints `Source —` while the header still looks
        # right. Assert the *rendered row*, not the presence of the header.
        expect("IETF" in proc.stdout,
               "proof: the source label never rendered: %s" % proc.stdout.strip()[:200])
        expect(main.since(mark, "GET /api/document/2844/proof"),
               "proof: the stub saw no proof GET")
        # The passage lives at `passage.snippet`. Both directions are asserted:
        # the human summary has to show the passage the reader asked for, and
        # the JSON bundle has to carry it where the API actually puts it.
        proc = live.run("proof", "2844", "-q", "quantum", base=main.base)
        exit_is(proc, 0, "proof -q")
        expect("Snippet" in proc.stdout and "quantum" in proc.stdout,
               "proof -q: the passage never rendered in the human summary: %s"
               % proc.stdout.strip()[:200])
        proc = live.run("proof", "2844", "-q", "quantum", "--json", base=main.base)
        exit_is(proc, 0, "proof -q --json")
        passage = (stdout_json(proc, "proof -q") or {}).get("passage") or {}
        expect("quantum" in str(passage.get("snippet")),
               "proof -q --json: the passage query did not reach the bundle")

    @check("proof — the evidence hash stays on its label's line at 80 columns")
    def _proof_hash_line():
        # A 71-character hash plus its label overflows a default terminal, and a
        # folded value lands on a second, unindented line: the one line a reader
        # copies out of the bundle arrives split in two.
        proc = live.run("proof", "2844", base=main.base)
        exit_is(proc, 0, "proof (80 cols)")
        line = next((l for l in proc.stdout.splitlines() if "SHA-256" in l), "")
        expect(line, "proof: no SHA-256 row in the bundle")
        expect("sha256:" in line,
               "proof: the hash folded onto its own line: %r" % line.strip())

    @check("a document id below 1 is refused before any request (info, cite, proof)")
    def _bad_doc_id():
        mark = main.mark()
        for command in ("info", "cite", "proof"):
            proc = live.run(command, "0", base=main.base)
            exit_is(proc, 1, "%s 0" % command)
            expect("positive integer" in (proc.stdout + proc.stderr),
                   "%s 0: the message does not say what is wrong: %s"
                   % (command, (proc.stdout + proc.stderr).strip()[:200]))
        expect(not main.since(mark, "/api/document/0") and not main.since(mark, "/api/cite/0"),
               "a document id of 0 still produced a request")

    # ── credentials ─────────────────────────────────────────────────────────

    @check("auth — the key is written to the config file and validated against the API")
    def _auth():
        home = work / "home-auth"
        home.mkdir(exist_ok=True)
        proc = live.with_home(home).run("auth", "ckey_live_written_key", base=main.base, key="")
        exit_is(proc, 0, "auth")
        config = home / ".conceptio" / "config.json"
        expect(config.exists(), "auth: no config file was written under %s" % home)
        saved = json.loads(config.read_text(encoding="utf-8"))
        expect(saved.get("api_key") == "ckey_live_written_key",
               "auth: the key was not stored (api_key=%r)" % saved.get("api_key"))
        expect(saved.get("license_key") == "",
               "auth: saving an API key must clear a stale license key, or two credentials ship")
        expect("Key accepted" in proc.stdout,
               "auth: the live validation never ran: %s" % proc.stdout.strip()[:200])

    @check("a saved config-file credential authenticates with no environment key")
    def _config_credential():
        # Only the env-only path was covered before; this is the path the README
        # documents (sign in once, search from anywhere).
        mark = main.mark()
        proc = live.with_home(work / "home-auth").run("quota", "--json", base=main.base, key="")
        exit_is(proc, 0, "quota (config-file credential)")
        expect(stdout_json(proc, "quota (config-file credential)").get("auth") == "api_key",
               "quota: the saved key was not honored")
        expect(main.since(mark, "GET /api/me"), "quota: no request was made")
        expect("saved in ~/.conceptio/config.json" in proc.stderr,
               "quota: a key read from the config file should be reported as saved: %r"
               % proc.stderr.strip()[:200])

    @check("no credential anywhere — refused, exit 1, nothing leaves the box")
    def _keyless():
        home = work / "home-keyless"
        home.mkdir(exist_ok=True)
        mark = main.mark()
        proc = live.with_home(home).run("search", "zero trust", "--json", base=main.base, key="")
        exit_is(proc, 1, "keyless search")
        expect("Authentication required" in (proc.stdout + proc.stderr),
               "keyless: no guidance was printed: %s" % (proc.stdout + proc.stderr).strip()[:200])
        expect(proc.stdout.strip() == "",
               "keyless --json: the refusal leaked into stdout: %r" % proc.stdout[:200])
        expect(not main.since(mark, "GET /api/search"), "keyless: a request left the box anyway")

    @check("quota names the environment variable when that is where the key came from")
    def _env_origin():
        # The credential here arrives by environment (that is how this whole
        # harness supplies it) and no config file exists — so a sentence about
        # the file is simply false, and CI/editor users read that sentence.
        proc = live.run("quota", base=main.base)
        exit_is(proc, 0, "quota")
        expect("CONCEPTIO_API_KEY environment variable" in proc.stdout,
               "quota does not say where the key came from: %s" % proc.stdout.strip()[:200])
        expect("saved in ~/.conceptio/config.json" not in proc.stdout,
               "quota claims an environment key was saved to a file")

    # ── the download boundary ───────────────────────────────────────────────

    @check("download — a loopback target is refused, and nothing is written")
    def _download_local():
        out = live.cwd / "local.pdf"
        proc = live.run("download", main.base + "/x.pdf", "-o", str(out), base=main.base)
        exit_is(proc, 1, "download loopback")
        expect("local and private-network" in (proc.stdout + proc.stderr),
               "download: a loopback URL was not refused: %s" % (proc.stdout + proc.stderr).strip()[:200])
        expect(not out.exists(), "download: a refused target still produced a file")

    @check("download <id> — a document with no direct link says so instead of writing")
    def _download_no_link():
        out = live.cwd / "nolink.pdf"
        proc = live.run("download", "7288", "-o", str(out), base=main.base)
        exit_is(proc, 1, "download (no direct link)")
        expect("no direct PDF link" in (proc.stdout + proc.stderr),
               "download: the reason is not stated: %s" % (proc.stdout + proc.stderr).strip()[:200])
        expect(not out.exists(), "download: a file appeared for a document with no PDF")

    # ── connector failures: the server's reason picks the message ───────────

    @check("save — the server's refusal reason picks the human message")
    def _connector_reasons():
        ids = live.cwd / "bulk_ids.json"
        ids.write_text(json.dumps([7288, 2844]), encoding="utf-8")
        cases = (
            ("exhausted", ["save", "--to", "zotero", "7288"], "shared connector trial is used up"),
            ("pro", ["save", "--to", "zotero", "--all-saved", "--ids", str(ids)], "Pro plan"),
            ("not_configured", ["save", "--to", "zotero", "7288"], "Configure Zotero"),
        )
        for mode, argv, needle in cases:
            stub = connectors.get(mode)
            expect(stub, "no stub was started for connector mode %r" % mode)
            proc = live.run(*argv, base=stub.base)
            exit_is(proc, 1, "save (%s)" % mode)
            expect(needle in (proc.stdout + proc.stderr),
                   "save (%s): the reason did not become the right message: %s"
                   % (mode, (proc.stdout + proc.stderr).strip()[:200]))

    # ── the rest of the MCP surface ─────────────────────────────────────────

    @check("mcp — every remaining tool answers over the real transport")
    def _mcp_all_tools():
        # Needles avoid quote characters: a tool's answer is a JSON document
        # carried *inside* a JSON string, so its own quotes arrive escaped.
        cases = (
            ("conceptio_resolve", {"id": "RFC 2119"}, "RFC 2119"),
            ("conceptio_get_citation", {"doc_id": 7288, "format": "apa"}, "conceptio7288"),
            ("conceptio_get_document", {"doc_id": 2844}, "Key words for use in RFCs"),
            ("conceptio_connectors_send", {"connector": "zotero", "doc_id": 7288}, "zotero"),
            ("conceptio_connectors_send_all", {"doc_ids": [7288, 2844]}, "total"),
        )
        mark = main.mark()
        for tool, arguments, needle in cases:
            proc = mcp_call(live, main.base, tool, arguments)
            reply = json.loads(proc.stdout.strip().splitlines()[0])
            expect("error" not in reply,
                   "%s: JSON-RPC error %s" % (tool, str(reply.get("error"))[:200]))
            content = json.dumps(reply.get("result", {}))
            expect('"isError": true' not in content, "%s: the call failed: %s" % (tool, content[:200]))
            expect(needle in content, "%s: the answer did not carry %r: %s" % (tool, needle, content[:200]))
        expect(main.since(mark, "GET /api/resolve"), "mcp: no resolve request reached the API")
        expect(main.since(mark, "GET /api/cite/7288"), "mcp: no citation request reached the API")
        expect(main.since(mark, "POST /api/connectors/zotero/send"),
               "mcp: no connector request reached the API")

    @check("mcp — download_pdf keeps its writes inside the workspace")
    def _mcp_download_guard():
        proc = mcp_call(live, main.base, "conceptio_download_pdf",
                        {"doc_id_or_url": "7288", "output_path": "../escape.pdf"})
        reply = json.loads(proc.stdout.strip().splitlines()[0])
        expect("workspace" in json.dumps(reply.get("error", {})),
               "mcp download_pdf: a traversal path was not refused: %s" % json.dumps(reply)[:200])
        expect(not (live.cwd.parent / "escape.pdf").exists(),
               "mcp download_pdf: a file was written outside the workspace")

    @check("mcp — an unknown tool is a tool error, not a crash")
    def _mcp_unknown():
        proc = mcp_call(live, main.base, "definitely_not_a_tool", {})
        reply = json.loads(proc.stdout.strip().splitlines()[0])
        expect(reply.get("result", {}).get("isError") is True,
               "mcp: an unknown tool did not answer as a tool error: %s" % json.dumps(reply)[:200])
        expect(proc.returncode == 0, "mcp: the server exited %s on an unknown tool" % proc.returncode)

    # ── the five clients, driven exactly as they drive it ───────────────────

    def _client_check(client):
        def _run():
            failures = []
            for argv in CLIENT_CALLS[client]:
                proc = live.run(*argv, base=main.base, timeout=90)
                rendered = " ".join(argv)
                if proc.returncode != 0:
                    failures.append("`%s` exited %s: %s" % (rendered, proc.returncode,
                                                            (proc.stdout + proc.stderr).strip()[:160]))
                elif not proc.stdout.strip():
                    failures.append("`%s` printed nothing" % rendered)
                elif argv[0] in JSON_CALLS:
                    try:
                        json.loads(proc.stdout)
                    except ValueError:
                        failures.append("`%s` did not return JSON" % rendered)
            expect(not failures,
                   "the %s client's argv is no longer served by this CLI:\n      %s"
                   % (client, "\n      ".join(failures)))
        return _run

    for _client in sorted(CLIENT_CALLS):
        check("%s — every argv its shipped version sends is still served" % _client)(_client_check(_client))


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
    print("  cli      %s  (this tree declares %s)" % (" ".join(command), source_version() or "?"))
    print("  stub     %s" % STUB_PY)

    stubs = []
    try:
        main_stub = Stub("done", logs / "main.log")
        running_stub = Stub("running", logs / "running.log")
        expired_stub = Stub("expired", logs / "expired.log")
        connector_stubs = {
            mode: Stub("done", logs / ("connector-%s.log" % mode), connector_mode=mode)
            for mode in ("exhausted", "pro", "not_configured")
        }
        stubs = [main_stub, running_stub, expired_stub] + list(connector_stubs.values())
        print("  origins  main=%s running=%s expired=%s"
              % (main_stub.base, running_stub.base, expired_stub.base))
        print("  credit   %s" % " ".join("%s=%s" % (m, s.base) for m, s in sorted(connector_stubs.items())))

        live = Live(command, config_home(home, main_stub.base), tmp)
        define_checks(live, main_stub, running_stub, expired_stub, connector_stubs, tmp)
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
