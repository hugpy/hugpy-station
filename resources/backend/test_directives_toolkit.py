"""1.0.144 — the STATION TOOLKIT section of the rendered directives (operator
2026-10-01: highlight what supports delegation, how to use it, and keep the
facts verified). Pins: every toolkit entry is in the keeper render, the
per-kind sizes stay compact, every tool name a directive mentions exists in the
toolserver's tool list (a mocked list here; check_directive_tools.py checks the
live one), and every doc it points to ships in static/docs.
Run:  python3 -m pytest resources/backend/test_directives_toolkit.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_directive_tools as CT  # noqa: E402
import directives as DV  # noqa: E402
from test_directives import render, snap  # noqa: E402

ENTRIES = (  # entry -> the call shape it must carry
    ("Board = the worklist", "todo_batch {locus:"), ("Board", "todo_list {status, type, locus:"),
    ("🧭 directions", "DERIVE:"), ("⚑ operator", "BOARD-ITEM-FORMAT.md"),
    ("Conversation or action", 'comms_ping {to, kind:"message"'), ("request", 'kind:"request"'),
    ("keeper session", "/api/prompt/send"), ("NOT delivered", "queued"),
    ("Bug reports", "WORKER session only"), ("tmux", "Never type into another account's tmux"),
    ("B (local, free)", "/api/b/chat"), ("NOT a gate", "d4187"),
    ("Subagents", "git worktree under"), ("Toolserver categories", "issue_observe"),
    ("exchange", "exchange_list {locus}"), ("fs", "fs_search"), ("db", "db_query"),
    ("vm", "vm_hosts_overview"), ("metrics", "metrics_*"), ("Docs, by task", "STATION-FEATURES-PER-LOCUS.md"),
    ("Ledger", "ledger_put {locus:"), ("REPLACES the whole document", "ledger_get"),
    ("Release and landing", "push.sh status | stage | release"), ("drift gate", "b4195"),
    ("hugpy locus", "d4185"), ("Privilege", "sudo hugpy-gate list"), ("confirm tier", "--confirm"),
)


def _section(text, title):
    i = text.index(title)
    j = text.find("\n## ", i + 1)
    return text[i:j if j > 0 else len(text)]


def test_keeper_toolkit_has_every_entry_with_its_call():
    k = render(snap())["keeper"]
    for what, call in ENTRIES:
        assert what in k and call in k, (what, call)
    assert "## Station toolkit" in k and "### Keeper toolkit" in k and "As keeper you ROUTE" in k
    assert "### Board items" in k and "{{docs}}" not in k


def test_toolkit_is_compact_and_scoped():
    out = render(snap())
    tk = {kind: _section(t, "## Station toolkit") for kind, t in out.items()}
    assert len(tk["keeper"].splitlines()) <= 60, len(tk["keeper"].splitlines())
    for kind in ("chat", "worker", "local", "tmux"):
        assert len(tk[kind].splitlines()) < len(tk["keeper"].splitlines()), kind
        assert "### Keeper toolkit" not in out[kind] and "ledger_put" not in out[kind], kind
    assert "As worker you ARE the delegate" in out["worker"] and "As chat you hand work over" in out["chat"]
    assert "NOT a gate" in out["local"] and "todo_batch" not in out["local"]
    for kind in DV.KINDS:                                     # the board-items rules reach everyone
        assert "BOARD-ITEM-FORMAT.md" in out[kind], kind


def test_every_named_tool_exists_in_the_toolserver_list():
    known = CT.snapshot_tools()
    for kind, text in render(snap()).items():
        assert not CT.missing(text, known), (kind, CT.missing(text, known))
    k = render(snap())["keeper"]
    named = CT.mentioned(k, known | CT.BRIDGE_EXTRAS)
    assert {"todo_batch", "ledger_put", "comms_ping", "issue_observe", "db_query"} <= named


def test_missing_tool_is_caught_with_a_mocked_list():
    mock = {"todo_list", "todo_batch", "comms_ping", "ts_list"}
    text = "use `todo_batch` then `todo_teleport {x}` and `comms_ping`; categories `issue_*`; `prompt_send`"
    assert CT.missing(text, mock | {"prompt_list"}) == {"todo_teleport", "prompt_send"}
    assert "issue_" not in " ".join(CT.mentioned(text, mock | {"issue_list"}))
    assert CT.missing("session_message {to}", mock | {"session_list"}) == set()   # bridge extra


def test_named_docs_ship():
    k = render(snap())["keeper"]
    k = _section(k, "### Board items") + _section(k, "## Station toolkit")
    docs = set(re.findall(r"([A-Z][A-Z0-9-]+\.md)", k))
    assert "BOARD-ITEM-FORMAT.md" in docs
    for d in docs:
        assert (HERE / "static" / "docs" / d).is_file(), d
