"""The registry-facing surface: annotations, schema completeness, Dockerfile.

Glama's pipeline clones this repo, builds it (Dockerfile, maintainer-authored
or AI-inferred), introspects `tools/list` in a sandbox, and scores the tool
definitions (TDQS → the quality grade; a working build and releases feed the
maintenance grade). These pins hold the three things a reader-side scanner
judges:

- every tool declares MCP annotation hints (readOnly/destructive/idempotent/
  openWorld), with honesty pinned per tool — read paths claim read-only, state
  changers do not;
- every input property carries a description (completeness);
- the Dockerfile starts the actual stdio server, so an introspection run
  answers `tools/list` instead of exiting.
"""

from pathlib import Path

from conceptio_cli.mcp_server import TOOLS

REPO = Path(__file__).resolve().parent.parent

#: The honesty matrix — a tool that spends credits or writes state must not
#: claim read-only, and the pure read paths must.
ANNOTATIONS = {
    "conceptio_search": (True, True),
    "conceptio_resolve": (True, True),
    "conceptio_download_pdf": (False, True),
    "conceptio_get_citation": (True, True),
    "conceptio_search_batch": (False, False),
    "conceptio_connectors_send": (False, True),
    "conceptio_connectors_send_all": (False, False),
    "conceptio_get_document": (True, True),
    "conceptio_graph_walk": (True, True),
}


def test_every_tool_declares_annotations_matching_the_honesty_matrix():
    names = [t["name"] for t in TOOLS]
    assert sorted(names) == sorted(ANNOTATIONS), "the tool set moved — update the matrix"
    for tool in TOOLS:
        ann = tool.get("annotations")
        assert isinstance(ann, dict), f"{tool['name']} declares no annotations"
        read_only, idempotent = ANNOTATIONS[tool["name"]]
        assert ann.get("readOnlyHint") is read_only, tool["name"]
        assert ann.get("destructiveHint") is False, tool["name"]
        assert ann.get("idempotentHint") is idempotent, tool["name"]
        assert ann.get("openWorldHint") is True, tool["name"]


def test_every_input_property_carries_a_description():
    """Completeness: a schema property without a description is the classic
    TDQS deduction — the model has to guess what the field means."""
    for tool in TOOLS:
        props = tool["inputSchema"].get("properties") or {}
        for prop, schema in props.items():
            assert str(schema.get("description") or "").strip(), (
                f"{tool['name']}.{prop} has no description"
            )


def test_the_dockerfile_starts_the_real_stdio_server():
    """A published build claim is a test target: the image must run the same
    entry point the registry manifest advertises, or an introspection run
    answers nothing and the listing never gets graded."""
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:" in dockerfile, "base image is a python runtime"
    assert '"conceptio-search", "mcp"' in dockerfile, (
        "CMD must start the stdio server exactly as `uvx conceptio-search mcp` does"
    )
