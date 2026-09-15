"""Offline CLI tests (stubbed ConceptioClient, no network)."""

import json
from pathlib import Path

import pytest

import conceptio_cli.cli as cli_mod
from conceptio_cli.cli import main


class FakeClient:
    """Minimal stand-in for ConceptioClient used by cli.main()."""

    def __init__(self, *a, **kw):
        self.license_key = kw.get("license_key", "") or ""
        self.api_key = kw.get("api_key", "") or ""

    def search(self, query, limit=10, offset=0, category=None, language=None, sources=None, license=None):
        return {
            "query": query,
            "total": 1,
            "results": [
                {
                    "id": 1,
                    "title": "Attention Is All You Need",
                    "author": "Vaswani et al.",
                    "source": "arxiv_cs",
                    "source_label": "arXiv CS",
                    "year": "",
                    "license": "Open Access",
                    "snippet": "we propose a new architecture",
                    "url": "https://arxiv.org/abs/1706.03762",
                    "direct_pdf_url": "https://arxiv.org/pdf/1706.03762.pdf",
                }
            ],
        }

    def get_document(self, doc_id):
        return {"id": doc_id, "title": "Paper", "direct_pdf_url": "https://x/a.pdf"}

    def get_proof(self, doc_id, query=None):
        return {
            "doc_id": doc_id,
            "content_hash": "ab" * 32,
            "source": "nist",
            "source_label": "NIST",
            "license": "Open Access",
            "authority_score": 0.9,
            "retrieved_at": "2026-09-10T00:00:00Z",
            "snippet": "matched passage" if query else None,
        }

    def get_citation(self, doc_id, format="bibtex"):
        return f"@misc{{key, title = Paper, year = n.d.}}"

    def quota(self):
        return {"tier": "public", "auth": "public"}

    def resolve(self, identifier, limit=10):
        return {
            "query": identifier,
            "identifier": "RFC 2119",
            "kind": "rfc",
            "total": 1,
            "results": [
                {
                    "id": 304793,
                    "title": "Key words for use in RFCs",
                    "source": "ietf",
                    "access_level": "open_access",
                    "url": "https://www.rfc-editor.org/rfc/rfc2119.html",
                }
            ],
        }

    def resolve_text(self, identifier, limit=10):
        # Unrecognized identifier -> kind is null (text fallback).
        return {
            "query": identifier,
            "identifier": identifier,
            "kind": None,
            "total": 0,
            "results": [],
        }

    def resolve_download_url(self, target):
        return "https://x/a.pdf"

    def download_pdf(self, url, out):
        return out

    def download_by_target(self, target, out):
        return out

    def submit_search_job(self, queries):
        return {"id": "job12345678", "status": "queued", "poll_url": "/api/search/jobs/job12345678"}

    def get_search_job(self, job_id):
        return {
            "id": job_id, "status": "done",
            "result": {"count": 1, "tier": "public", "queries": [self.search("attention")]},
        }

    def batch_search(self, queries):
        return {"count": len(queries), "tier": "public",
                "queries": [self.search(q.get("q") or "") for q in queries]}

    def send_zotero(self, doc_id):
        return {"idempotent": False, "doc_id": doc_id}

    def send_zotero_all(self, doc_ids):
        return {"total": len(doc_ids), "results": []}

    def authorize_obsidian(self, doc_id):
        return {"doc_id": doc_id, "title": "Paper", "canonical_url": "https://www.conceptio.app/document/1/paper"}

    def log_obsidian(self, doc_id):
        return {"ok": True}


@pytest.fixture(autouse=True)
def _stub_client(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_mod, "ConceptioClient", FakeClient)
    monkeypatch.setattr(cli_mod, "set_license_key", lambda k: None)
    monkeypatch.setattr(cli_mod, "set_api_key", lambda k: None)
    monkeypatch.setattr(
        cli_mod, "load_config",
        lambda: {"default_limit": 10, "default_citation_format": "bibtex",
                 "api_key": "ckey_live_testkey0123456789abcdef"},
    )


def _keyless(monkeypatch):
    """No credential anywhere — config file AND environment.

    Clearing the environment matters after the gate and the MCP server started
    sharing one predicate: an ambient `CONCEPTIO_API_KEY` (a developer shell, a
    CI runner) would otherwise satisfy the gate and turn these tests into
    assertions about the host, not the code.
    """
    monkeypatch.setattr(
        cli_mod, "load_config",
        lambda: {"default_limit": 10, "default_citation_format": "bibtex",
                 "api_key": "", "license_key": ""},
    )
    for name in ("CONCEPTIO_API_KEY", "CONCEPTIO_LICENSE_KEY", "CONCEPTIO_BEARER_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_require_auth_honors_env_api_key(monkeypatch):
    # CONCEPTIO_API_KEY must satisfy the gate without touching the config
    # file (editors/CI supply credentials via env).
    _keyless(monkeypatch)
    monkeypatch.setenv("CONCEPTIO_API_KEY", "ckey_live_envkey0123456789abcdef")
    assert cli_mod.require_auth() is True


def test_require_auth_refuses_without_any_credential(monkeypatch):
    _keyless(monkeypatch)
    monkeypatch.delenv("CONCEPTIO_API_KEY", raising=False)
    monkeypatch.delenv("CONCEPTIO_LICENSE_KEY", raising=False)
    assert cli_mod.require_auth() is False


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower()


def test_search_json(capsys):
    assert main(["search", "attention", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total"] == 1
    assert data["results"][0]["direct_pdf_url"].endswith(".pdf")


def test_search_commercial_license_flag(capsys):
    assert main(["search", "attention", "--license", "commercial-ok", "--json"]) == 0
    assert "Attention Is All You Need" in capsys.readouterr().out


def test_search_table(capsys):
    assert main(["search", "attention"]) == 0
    out = capsys.readouterr().out
    assert "Attention Is All You Need" in out
    assert "arXiv" in out


def test_search_batch_sync_json(capsys, tmp_path):
    queries = tmp_path / "q.json"
    queries.write_text(json.dumps([{"q": "attention"}, {"q": "transformers"}]), encoding="utf-8")
    assert main(["search", "--batch", str(queries), "--sync", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 2
    assert data["queries"][0]["total"] == 1


def test_search_batch_sync_renders_each_query(capsys, tmp_path):
    queries = tmp_path / "q.json"
    queries.write_text(json.dumps([{"q": "attention"}, {"q": "transformers"}]), encoding="utf-8")
    assert main(["search", "--batch", str(queries), "--sync"]) == 0
    out = capsys.readouterr().out
    assert "Query 1" in out and "Query 2" in out
    assert out.count("Attention Is All You Need") == 2


def test_search_batch_sync_rejects_over_ten(capsys, tmp_path):
    queries = tmp_path / "q.json"
    queries.write_text(json.dumps([{"q": "x"}] * 11), encoding="utf-8")
    assert main(["search", "--batch", str(queries), "--sync"]) == 1
    assert "at most 10" in capsys.readouterr().out


def test_search_batch_queues_and_waits(capsys, tmp_path):
    queries = tmp_path / "q.json"
    queries.write_text(json.dumps([{"q": "attention"}]), encoding="utf-8")
    assert main(["search", "--batch", str(queries), "--wait", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 1
    assert data["queries"][0]["total"] == 1


def test_search_batch_queues_prints_handle_without_wait(capsys, tmp_path):
    queries = tmp_path / "q.json"
    queries.write_text(json.dumps([{"q": "attention"}]), encoding="utf-8")
    assert main(["search", "--batch", str(queries), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "queued"
    assert data["id"] == "job12345678"


def test_resolve_rfc(capsys):
    assert main(["resolve", "RFC 2119"]) == 0
    out = capsys.readouterr().out
    assert "RFC 2119" in out
    assert "rfc" in out
    assert "Key words for use in RFCs" in out
    assert "304793" in out


def test_resolve_json(capsys):
    assert main(["resolve", "RFC 2119", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "rfc"
    assert data["results"][0]["id"] == 304793


def test_resolve_text_fallback(capsys, monkeypatch):
    # kind null -> the CLI notes the fallback and reports no matches.
    fake = FakeClient()
    monkeypatch.setattr(fake, "resolve", fake.resolve_text)
    monkeypatch.setattr(cli_mod, "ConceptioClient", lambda *a, **k: fake)
    assert main(["resolve", "some plain text"]) == 0
    out = capsys.readouterr().out
    assert "Unrecognized identifier" in out
    assert "No matching documents" in out


def test_search_job_wait_renders_result(capsys):
    assert main(["search-job", "job12345678", "--wait", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 1


def test_cite(capsys):
    assert main(["cite", "1", "--format", "bibtex"]) == 0
    assert "@misc" in capsys.readouterr().out


def test_info(capsys):
    assert main(["info", "1"]) == 0
    assert "Paper" in capsys.readouterr().out


def test_proof_json(capsys):
    assert main(["proof", "1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["doc_id"] == 1
    assert data["content_hash"] == "ab" * 32


def test_proof_human_summary(capsys):
    assert main(["proof", "1"]) == 0
    out = capsys.readouterr().out
    assert "Proof bundle" in out
    assert "NIST" in out
    assert "Open Access" in out


def test_proof_passes_passage_query(capsys):
    assert main(["proof", "1", "-q", "quantum"]) == 0
    assert "matched passage" in capsys.readouterr().out


def test_proof_raises_on_error_body(capsys, monkeypatch):
    class FailingClient(FakeClient):
        def get_proof(self, doc_id, query=None):
            raise cli_mod.ConceptioError("Rate limit: 1 request/second on the Dev tier")

    monkeypatch.setattr(cli_mod, "ConceptioClient", FailingClient)
    assert main(["proof", "1", "--json"]) == 1
    # In machine mode the failure goes to stderr and stdout stays empty: a
    # caller decodes stdout, so an error printed there either corrupts the
    # parse or, worse, parses as data.
    captured = capsys.readouterr()
    assert "Rate limit" in captured.err
    assert captured.out == ""


def test_info_json(capsys):
    # Machine mode: `info --json` must emit pure JSON on stdout (editors and
    # other host processes decode it directly), reserving human text for the
    # non-json path.
    assert main(["info", "1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == 1
    assert data["title"] == "Paper"


def test_search_error_dict_exits_nonzero(capsys, monkeypatch):
    """A rate-limit/quota error dict must fail loudly — machine consumers
    (editors, agent bridges) read the exit code, not just the payload."""
    class QuotaClient(FakeClient):
        def search(self, query, limit=10, offset=0, category=None, language=None, sources=None, license=None):
            return {"error": "Rate limit: 1 request/second on the Dev tier", "results": []}

    monkeypatch.setattr(cli_mod, "ConceptioClient", QuotaClient)
    assert main(["search", "attention", "--json"]) == 1
    captured = capsys.readouterr()
    assert "Rate limit" in captured.err
    assert captured.out == ""


def test_resolve_error_dict_exits_nonzero(capsys, monkeypatch):
    class QuotaClient(FakeClient):
        def resolve(self, identifier, limit=10):
            return {"error": "Quota exhausted", "results": []}

    monkeypatch.setattr(cli_mod, "ConceptioClient", QuotaClient)
    assert main(["resolve", "RFC 2119"]) == 1
    assert "Quota exhausted" in capsys.readouterr().out


def test_sync_batch_error_dict_exits_nonzero(capsys, tmp_path, monkeypatch):
    class QuotaClient(FakeClient):
        def batch_search(self, queries):
            return {"error": "Rate limit exceeded"}

    monkeypatch.setattr(cli_mod, "ConceptioClient", QuotaClient)
    queries = tmp_path / "q.json"
    queries.write_text(json.dumps([{"q": "attention"}]), encoding="utf-8")
    assert main(["search", "--batch", str(queries), "--sync", "--json"]) == 1
    assert "Rate limit exceeded" in capsys.readouterr().err


def test_quota(capsys):
    assert main(["quota"]) == 0
    assert "public" in capsys.readouterr().out.lower()


def test_quota_json_prints_only_json(capsys, monkeypatch):
    """`quota --json` is a machine surface: stdout must parse, nothing else."""

    class DevClient(FakeClient):
        def quota(self):
            return {
                "tier": "dev",
                "auth": "api_key",
                "monthly_credit_limit": 3500,
                "monthly_credit_used": 42,
                "monthly_credit_remaining": 3458,
                "monthly_reset_at": "2026-10-01T00:00:00Z",
            }

    monkeypatch.setattr(cli_mod, "ConceptioClient", DevClient)
    assert main(["quota", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["tier"] == "dev"
    assert payload["monthly_credit_remaining"] == 3458
    # The human rendering must not leak into the machine read.
    assert "Tier:" not in captured.out


def test_quota_json_keeps_the_credential_hint_on_stderr(capsys, monkeypatch):
    class DevClient(FakeClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **{**kw, "api_key": "ckey_live_abcdef0123456789"})

        def quota(self):
            return {"tier": "dev", "auth": "api_key"}

    monkeypatch.setattr(cli_mod, "ConceptioClient", DevClient)
    assert main(["quota", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["tier"] == "dev"
    assert "Credential:" in captured.err
    assert "Credential:" not in captured.out


def test_quota_json_reports_a_failure_loudly(capsys, monkeypatch):
    class BrokenClient(FakeClient):
        def quota(self):
            raise cli_mod.ConceptioError("API unreachable")

    monkeypatch.setattr(cli_mod, "ConceptioClient", BrokenClient)
    assert main(["quota", "--json"]) == 1
    assert "API unreachable" in capsys.readouterr().err


def test_quota_shows_api_key_identity(capsys, monkeypatch):
    class ProClient(FakeClient):
        def quota(self):
            return {"tier": "pro", "auth": "api_key"}

    monkeypatch.setattr(cli_mod, "ConceptioClient", ProClient)
    assert main(["quota"]) == 0
    out = capsys.readouterr().out
    assert "API key" in out
    assert "pro" in out.lower()


def test_auth_saves_api_key_when_ckey_prefix(capsys, monkeypatch):
    saved = {}
    monkeypatch.setattr(cli_mod, "set_api_key", lambda k: saved.update({"api": k}))
    monkeypatch.setattr(cli_mod, "set_license_key", lambda k: saved.update({"license": k}))

    class ProClient(FakeClient):
        def quota(self):
            return {"tier": "pro", "auth": "api_key"}

    monkeypatch.setattr(cli_mod, "ConceptioClient", ProClient)
    assert main(["auth", "ckey_live_abcdef0123456789abcdef0123456789"]) == 0
    assert saved.get("api") == "ckey_live_abcdef0123456789abcdef0123456789"
    assert "license" not in saved


def test_auth_saves_license_key_when_conceptio_prefix(capsys, monkeypatch):
    saved = {}
    monkeypatch.setattr(cli_mod, "set_api_key", lambda k: saved.update({"api": k}))
    monkeypatch.setattr(cli_mod, "set_license_key", lambda k: saved.update({"license": k}))

    class ProClient(FakeClient):
        def quota(self):
            return {"tier": "pro", "auth": "license"}

    monkeypatch.setattr(cli_mod, "ConceptioClient", ProClient)
    assert main(["auth", "CONCEPTIO-AAAA-BBBB-CCCC"]) == 0
    assert saved.get("license") == "CONCEPTIO-AAAA-BBBB-CCCC"
    assert "api" not in saved


def test_auth_rejects_short_key(capsys):
    assert main(["auth", "short"]) == 1
    assert "valid key" in capsys.readouterr().out.lower()


def test_download(capsys, tmp_path):
    out = tmp_path / "a.pdf"
    assert main(["download", "1", "-o", str(out)]) == 0
    assert "Saved" in capsys.readouterr().out


def test_save_zotero(capsys):
    assert main(["save", "--to", "zotero", "1"]) == 0
    assert "Saved to Zotero" in capsys.readouterr().out


def test_save_obsidian_with_vault(capsys, monkeypatch):
    monkeypatch.setattr(cli_mod.webbrowser, "open", lambda uri: True)
    assert main(["save", "--to", "obsidian", "1", "--vault", "Research"]) == 0
    assert "Obsidian" in capsys.readouterr().out


def test_save_obsidian_unopened_prints_the_uri(capsys, monkeypatch):
    """When the OS declines the URI, the URI *is* the deliverable.

    A CLI cannot verify a custom-URI handoff, so the only honest report is the
    one it can show: the exact metadata document it built. This is also the only
    test of that branch — and it is the reason the live check never needs to
    assert the URI (where asserting it would mean opening it).
    """
    monkeypatch.setattr(cli_mod.webbrowser, "open", lambda uri: False)
    assert main(["save", "--to", "obsidian", "1", "--vault", "Research"]) == 0
    out = capsys.readouterr().out
    assert "no save was logged" in out
    assert "obsidian://new?" in out
    assert "vault=Research" in out
    assert "file=Paper" in out


def test_save_all_requires_ids(capsys):
    assert main(["save", "--to", "zotero", "--all-saved"]) == 1
    assert "--ids" in capsys.readouterr().out


def test_save_all_zotero(capsys, tmp_path):
    ids = tmp_path / "ids.json"
    ids.write_text(json.dumps({"doc_ids": [1, 2]}), encoding="utf-8")
    assert main(["save", "--to", "zotero", "--all-saved", "--ids", str(ids)]) == 0
    assert "Saved 2" in capsys.readouterr().out


def test_unknown_command_exits_with_usage(capsys):
    # argparse rejects unknown subcommands with exit code 2 + usage on stderr.
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2
    assert "usage:" in (capsys.readouterr().err or "").lower()

def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    out = capsys.readouterr().out
    assert exc.value.code == 0
    assert "conceptio-cli" in out
    # the version must resolve to something real, not an empty/undefined string
    ver = out.split("conceptio-cli", 1)[-1].strip()
    assert ver and all(part.isdigit() for part in ver.split(".")[:3])


@pytest.mark.parametrize("argv", [
    ["search", "attention"],
    ["resolve", "RFC 2119"],
    ["download", "1", "-o", "a.pdf"],
    ["cite", "1"],
    ["info", "1"],
    ["proof", "1"],
])
def test_data_commands_require_auth(capsys, monkeypatch, argv):
    """Keyless runs refuse before any network call, with setup guidance."""
    _keyless(monkeypatch)
    assert main(argv) == 1
    out = capsys.readouterr().out
    assert "Authentication required" in out
    assert "conceptio auth" in out


def test_mcp_refuses_keyless_on_stderr(capsys, monkeypatch):
    _keyless(monkeypatch)
    assert main(["mcp"]) == 1
    err = capsys.readouterr().err
    assert "Authentication required" in err


def test_quota_reports_an_environment_key_as_an_environment_key(capsys, monkeypatch):
    """Supplying the key by environment is the documented editor/CI path — the
    one case where no config file exists, so a sentence about the file is wrong."""
    class DevClient(FakeClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **{**kw, "api_key": "ckey_live_abcdef0123456789"})
            self.credential_origin = "environment"
            self.credential_env_var = "CONCEPTIO_API_KEY"

        def quota(self):
            return {"tier": "dev", "auth": "api_key"}

    monkeypatch.setattr(cli_mod, "ConceptioClient", DevClient)
    assert main(["quota"]) == 0
    out = capsys.readouterr().out
    assert "CONCEPTIO_API_KEY environment variable" in out
    assert "saved in ~/.conceptio/config.json" not in out


def test_quota_still_reports_a_saved_key_as_saved(capsys, monkeypatch):
    class DevClient(FakeClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **{**kw, "api_key": "ckey_live_abcdef0123456789"})
            self.credential_origin = "config file"

        def quota(self):
            return {"tier": "dev", "auth": "api_key"}

    monkeypatch.setattr(cli_mod, "ConceptioClient", DevClient)
    assert main(["quota"]) == 0
    out = capsys.readouterr().out
    assert "saved in ~/.conceptio/config.json" in out
    assert "environment variable" not in out


def test_auth_validates_the_key_it_was_handed(capsys, monkeypatch):
    """The key just supplied is the subject of the test. Asking a client that
    resolves environment-first would validate whatever it found there instead."""
    seen = {}

    class RecordingClient(FakeClient):
        def __init__(self, *a, **kw):
            seen["api_key"] = kw.get("api_key")
            super().__init__(*a, **kw)

        def quota(self):
            return {"tier": "dev", "auth": "api_key"}

    monkeypatch.setattr(cli_mod, "ConceptioClient", RecordingClient)
    monkeypatch.setattr(cli_mod, "set_api_key", lambda key: seen.setdefault("saved", key))
    monkeypatch.setenv("CONCEPTIO_API_KEY", "ckey_live_environment_one")
    assert main(["auth", "ckey_live_the_new_key"]) == 0
    assert seen["saved"] == "ckey_live_the_new_key"
    assert seen["api_key"] == "ckey_live_the_new_key"
    # ...and the environment shadowing that key is stated, not left to surprise.
    assert "CONCEPTIO_API_KEY is set" in capsys.readouterr().out


def test_proof_hash_stays_on_the_label_line(capsys, monkeypatch):
    """A 71-character hash plus its label overflows an 80-column terminal; a
    folded value lands on a second, unindented line and the one line a reader
    copies out of the bundle arrives split in two."""
    class HashClient(FakeClient):
        def get_proof(self, doc_id, query=None):
            return {"doc_id": doc_id, "content_hash": "sha256:" + "ab" * 32,
                    "source_label": "NIST", "license": "Open Access"}

    monkeypatch.setattr(cli_mod, "ConceptioClient", HashClient)
    assert main(["proof", "1"]) == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if "SHA-256" in l]
    assert lines, "the bundle printed no SHA-256 row"
    assert "sha256:" in lines[0], "the hash folded onto its own line: %r" % lines[0]


def test_license_key_satisfies_gate(capsys, monkeypatch):
    monkeypatch.setattr(
        cli_mod, "load_config",
        lambda: {"default_limit": 10, "default_citation_format": "bibtex",
                 "api_key": "", "license_key": "CONCEPTIO-AAAA-BBBB-CCCC"},
    )
    assert main(["search", "attention", "--json"]) == 0


def test_auth_and_quota_stay_keyless(capsys, monkeypatch):
    """auth/quota/help never gate — they are the recovery path."""
    _keyless(monkeypatch)
    assert main(["quota"]) == 0
    assert main([]) == 0


# ── --wait: an unfinished job is a failure, not an empty result ──────────────

class StuckJobClient(FakeClient):
    """A job that never leaves the queue — the shape of a slow server, not a
    broken one."""

    def get_search_job(self, job_id):
        return {"id": job_id, "status": "queued"}


def _fast_polling(monkeypatch):
    # The cadence is a module constant resolved at call time (see _poll_job), so
    # this keeps the test about the exit code rather than about sleeping.
    monkeypatch.setattr(cli_mod, "_JOB_POLL_INTERVAL_S", 0.01)


def test_batch_wait_timeout_fails_instead_of_reporting_no_results(capsys, tmp_path, monkeypatch):
    """A wait that runs out must exit non-zero and name the job.

    It used to return the last snapshot, which rendered as "No query results
    returned by the batch." with exit 0 — a still-running job reported as a
    successful empty search, which is the one thing a machine caller cannot
    detect.
    """
    _fast_polling(monkeypatch)
    monkeypatch.setattr(cli_mod, "ConceptioClient", StuckJobClient)
    queries = tmp_path / "q.json"
    queries.write_text(json.dumps([{"q": "attention"}]), encoding="utf-8")

    assert main(["search", "--batch", str(queries), "--wait", "--timeout", "0.05"]) == 1
    out = capsys.readouterr().out
    assert "still 'queued'" in out
    assert "conceptio search-job job12345678 --wait" in out
    assert "No query results" not in out


def test_search_job_wait_timeout_fails_loudly(capsys, monkeypatch):
    _fast_polling(monkeypatch)
    monkeypatch.setattr(cli_mod, "ConceptioClient", StuckJobClient)
    assert main(["search-job", "job12345678", "--wait", "--timeout", "0.05"]) == 1
    assert "still 'queued'" in capsys.readouterr().out


def test_wait_timeout_rejects_a_non_positive_budget(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "ConceptioClient", StuckJobClient)
    queries = tmp_path / "q.json"
    queries.write_text(json.dumps([{"q": "attention"}]), encoding="utf-8")
    assert main(["search", "--batch", str(queries), "--wait", "--timeout", "0"]) == 1
    assert "positive number" in capsys.readouterr().out


# ── machine mode: decided by the parsed arguments, not by sys.argv ───────────

def test_json_flag_passed_directly_still_purifies_stdout(capsys, tmp_path):
    """`main(argv)` is the real entry point, so IT decides where text goes.

    The stream used to be sniffed from `sys.argv` at import, so a caller that
    passed its own argument list — a host embedding the CLI, or a test — got
    human text on stdout beside the payload the flag promised.
    """
    missing = tmp_path / "nope.json"
    assert main(["--json", "search", "--batch", str(missing)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "", f"stdout must carry only JSON, got: {captured.out!r}"
    assert "Could not read batch JSON" in captured.err


def test_json_mode_does_not_leak_into_the_next_invocation(capsys):
    """The console is one shared object: setting it per call is what keeps a
    host (or a test) from inheriting the previous run's stream."""
    assert main(["--json", "resolve", "RFC 2119"]) == 0
    capsys.readouterr()
    assert main(["search", "attention"]) == 0
    out = capsys.readouterr().out
    assert "Attention Is All You Need" in out  # human table back on stdout


def test_broken_pipe_exits_cleanly(capsys, monkeypatch):
    """`conceptio search … | head` closes stdout early; that is normal use."""
    def boom(*a, **kw):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(cli_mod.console, "print", boom)
    assert main(["search", "attention"]) == 141


# ── source-of-truth parity ──────────────────────────────────────────────────

def test_version_literals_agree_with_pyproject():
    """Two places carry the version: `pyproject.toml` and the fallback literal
    in `__init__.py` (used when the package is imported from an uninstalled
    checkout). A release that bumps one and not the other ships a lie —
    `conceptio --version` would answer from whichever won."""
    import inspect
    import re as _re

    import conceptio_cli

    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    declared = _re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
    assert declared, "pyproject.toml has no version"

    source = inspect.getsource(conceptio_cli)
    fallback = _re.search(r'__version__\s*=\s*"([^"]+)"', source)
    assert fallback, "__init__.py has no fallback version literal"
    assert fallback.group(1) == declared.group(1), (
        f"pyproject says {declared.group(1)}, the fallback literal says {fallback.group(1)}"
    )


def test_python_m_entry_point_works():
    """`python -m conceptio_cli` — for a venv that was never activated, or a
    host process that resolves the interpreter rather than the console script."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "conceptio_cli", "--version"],
        cwd=str(root), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "conceptio-cli" in (proc.stdout + proc.stderr)
