"""The CLI surface every shipped client depends on — one table, two checks.

`conceptio-search` is not one product, it is a product with clients: the
Neovim plugin and the Obsidian plugin both shell out to *this* binary with an
argv list (`vim.system`, `execFile`) and read stdout. Nothing else
couples them — no client touches the REST API — so the argv grammar is the
entire interface between them, and it is the seam where a rename on one side
breaks a shipped extension on the other, silently and months later.

The table below is transcribed from each client's own source, one entry per
distinct invocation, with the file it came from. Two tests hold it:

  * `test_cli_contract.py` (offline) — every argv here must be accepted by the
    CLI's own parser (`cli.build_parser`), and while a client's checkout is
    present the subcommand and flags must still appear in its sources. That
    direction catches a CLI-side rename and a client-side removal.
  * `live_check.py` — every argv here is *run* against the loopback stub with a
    real binary and must exit 0 with output. That direction catches a change
    the parser accepts but the runtime refuses.

Neither is a substitute for the other, and neither can see a client changing an
argument's *order* — so when you add a call to a client, add its row here.
"""

# Where each client keeps the code that builds the argv — logical names that
# resolve through tests/cross_repo.py (a workspace maps them in
# tests/cross_repo_paths.json; see the committed example). Used by the offline
# test to confirm the table still describes the clients whose sources are
# mapped into this workspace.
CLIENT_SOURCES = {
    "nvim": ["client-nvim-api"],
    "obsidian": ["client-obsidian-cli", "client-obsidian-commands"],
}

CLIENT_CALLS = {
    "nvim": [
        # api.lua:86 — search plus the optional directive filters
        ["search", "zero trust", "--json", "-l", "20"],
        ["search", "zero trust", "--json", "-l", "20", "--lang", "en"],
        ["search", "zero trust", "--json", "-l", "20", "--license", "commercial-ok"],
        ["resolve", "RFC 2119", "--json", "-l", "20"],
        ["cite", "7288", "-f", "apa"],
        ["info", "7288", "--json"],
        ["quota"],
    ],
    "obsidian": [
        # cli.ts:209-248 — note the quota pair: `--json` first, plain as the
        # documented fallback for a CLI that predates it.
        ["search", "zero trust", "--json", "-l", "10"],
        ["search", "zero trust", "--json", "-l", "10", "--license", "commercial-ok"],
        ["resolve", "RFC 2119", "--json", "-l", "10"],
        ["cite", "7288", "-f", "apa"],
        ["proof", "7288", "--json"],
        ["info", "7288", "--json"],
        ["quota", "--json"],
        ["quota"],
    ],
}

# `--json` output is decoded by every client here; `cite` is read as raw
# text; `quota` has a human form and a structured one.
JSON_CALLS = {"search", "resolve", "info", "proof"}
RAW_TEXT_CALLS = {"cite"}


def calls_for(clients=None):
    """Flatten the table to ``[(client, argv), ...]``, optionally filtered."""
    rows = []
    for client, calls in CLIENT_CALLS.items():
        if clients and client not in clients:
            continue
        for argv in calls:
            rows.append((client, argv))
    return rows
