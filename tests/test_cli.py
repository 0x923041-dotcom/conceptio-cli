"""Offline CLI tests (stubbed ConceptioClient, no network)."""

import json

import pytest

import conceptio_cli.cli as cli_mod
from conceptio_cli.cli import main


class FakeClient:
    """Minimal stand-in for ConceptioClient used by cli.main()."""

    def __init__(self, *a, **kw):
        self.license_key = kw.get("license_key", "") or ""
        self.api_key = kw.get("api_key", "") or ""

    def search(self, query, limit=10, offset=0, category=None, language=None, sources=None):
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
    monkeypatch.setattr(
        cli_mod, "load_config",
        lambda: {"default_limit": 10, "default_citation_format": "bibtex",
                 "api_key": "", "license_key": ""},
    )


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower()


def test_search_json(capsys):
    assert main(["search", "attention", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total"] == 1
    assert data["results"][0]["direct_pdf_url"].endswith(".pdf")


def test_search_markdown(capsys):
    assert main(["search", "attention", "--markdown"]) == 0
    assert "## Conceptio results" in capsys.readouterr().out


def test_search_table(capsys):
    assert main(["search", "attention"]) == 0
    out = capsys.readouterr().out
    assert "Attention Is All You Need" in out
    assert "arXiv" in out


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


def test_cite(capsys):
    assert main(["cite", "1", "--format", "bibtex"]) == 0
    assert "@misc" in capsys.readouterr().out


def test_info(capsys):
    assert main(["info", "1"]) == 0
    assert "Paper" in capsys.readouterr().out


def test_quota(capsys):
    assert main(["quota"]) == 0
    assert "public" in capsys.readouterr().out.lower()


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
