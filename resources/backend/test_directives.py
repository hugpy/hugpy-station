"""1.0.143 — per-(locus x session) directives from ONE source of record.

directives.py composes the shipped templates (directive-templates/) with the
locus's live facts and the operator layers. These tests pin the contract the
operator asked for (2026-10-01): a DISTINCT directive per standing serve
session per locus, the locus's user/hostname filled live, the tmux seats
framed as the fallback / inference arm (never "the keeper"), the token-economy
rules only where relevant, and no directive doubling however a composed text
is fed back in. Pure: no network, no process, tmp dirs only.
Run:  python3 -m pytest resources/backend/test_directives.py
"""
import getpass
import json
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import directives as DV  # noqa: E402

GUIDANCE = "## Lay of the land — test locus\nOPERATOR-GUIDANCE-SENTINEL\n"
LEGACY = DV.LEGACY_HEADER + "\n\nYou are the FRONTIER model (A).\nLEGACY-SENTINEL\n"


def snap(locus="keeper", user="vm_mgr", host="ae", remote=True, **files):
    base = {"guidance": GUIDANCE, "directive": None, "handoff": None, "fs": None,
            "delegate": None, "b_model": json.dumps({"model": "Qwen3-Coder-Next"}),
            "serve": json.dumps({"port": 9124}), "station": json.dumps({"port": 8898, "locus": locus})}
    base.update(files)
    return {"state": f"/srv/{user}/hugpy-station", "home": f"/srv/{user}", "user": user, "host": host,
            "app": f"/srv/{user}/hugpy-station/app/current/resources/backend",
            "remote": remote, "locus": locus, "files": base}


def render(s):
    return DV.render_all(s, DV.facts(s))


def test_distinct_per_locus_and_session():
    a, b = render(snap("keeper", "vm_mgr", "ae")), render(snap("hugpy", "hugpy", "ae"))
    texts = list(a.values()) + list(b.values())
    assert len(set(texts)) == len(DV.KINDS) * 2                 # 10 distinct directives
    for locus, out in (("keeper", a), ("hugpy", b)):
        for kind, t in out.items():
            assert t.startswith(f"<!-- hugpy-station directive {kind}@{locus} ")
            assert DV.TITLES[kind] in t and "{{" not in t


def test_locus_user_and_hostname_filled():
    out = render(snap("keeper", "vm_mgr", "ae"))
    for kind in DV.KINDS:
        assert "`vm_mgr@ae`" in out[kind], kind
    assert "`/srv/vm_mgr/hugpy-station`" in out["keeper"]
    assert "http://127.0.0.1:8898" in out["keeper"] and "127.0.0.1:9124" in out["keeper"]
    assert "Qwen3-Coder-Next" in out["keeper"] and "Qwen3-Coder-Next" in out["local"]
    assert "Sign every board / comms write `host-keeper`" in out["keeper"]
    assert "`keeper-worker`" in out["worker"] and "`hugpy-keeper`" in render(snap("hugpy"))["keeper"]
    # the host is resolved live when no snapshot names it
    fx = DV.facts({"files": {}}, locus="op", env={})
    assert fx["user"] == getpass.getuser() and fx["hostname"] == socket.gethostname()
    # a remote snapshot never borrows this machine's identity
    rfx = DV.facts({"remote": True, "files": {}}, locus="far")
    assert rfx["user"] == "(unknown)" and rfx["hostname"] == "(unknown)"


def test_keeper_brief_carries_its_charge():
    k = render(snap())["keeper"]
    for feature in ("☑ todo tab", "◳ canvas tab", "canvas_put", "prompt_done", "B — the local keeper",
                    "/api/b/chat", "The toolserver", "ledger_put", "issue_list", "exchange_list",
                    "station state", "board file", "Station upkeep & release",
                    "timestamp · tokens · duration · model", "Comms doctrine"):
        assert feature in k, feature
    assert "# Operator guidance\n## Lay of the land" in k


def test_session_briefs_are_scoped():
    out = render(snap())
    assert "conversational" in out["chat"] and "do not write the task ledger" in out["chat"]
    assert "task execution" in out["worker"] and "never widen scope" in out["worker"]
    assert "B, the local keeper" in out["local"]
    # keeper-only features stay out of the others
    for kind in ("chat", "worker", "local", "tmux"):
        assert "Station upkeep & release" not in out[kind], kind
        assert "canvas_put" not in out[kind], kind
    # token economy only where the session is metered and can delegate
    for kind in ("keeper", "chat", "tmux"):
        assert "## Token economy" in out[kind], kind
    for kind in ("worker", "local"):
        assert "## Token economy" not in out[kind], kind
    # the operator guidance is keeper-charge text: keeper + tmux only
    for kind in ("chat", "worker", "local"):
        assert "OPERATOR-GUIDANCE-SENTINEL" not in out[kind], kind


def test_tmux_seat_is_fallback_not_keeper():
    t = render(snap())["tmux"]
    assert "FALLBACK" in t and "inference arm" in t
    assert "You are NOT the keeper" in t
    assert "STATION_NUDGE_TMUX=0" in t and "no pings, nudges or reminders" in t
    assert "You are the **keeper**" not in t
    on = DV.facts(snap(), env={"STATION_NUDGE_TMUX": "1"})
    assert "STATION_NUDGE_TMUX=1" in DV.compose("tmux", snap(), fx=on)


def test_no_doubling():
    s = snap(directive=LEGACY)
    first = DV.compose("keeper", s)
    # feed the whole composed text back in as the guidance (the 1.0.41 migration
    # seeded the user file from a composed one) AND as the keeper overlay
    again = DV.compose("keeper", snap(directive=LEGACY, guidance=first, dir_keeper=first))
    for t in (first, again):
        assert len(DV.BEGIN_RE.findall(t)) == 1 and t.count(DV.END) == 1
        assert t.count("## Shared core") == 1
        assert "LEGACY-SENTINEL" not in t                     # legacy full copy: never composed
    assert again.count("OPERATOR-GUIDANCE-SENTINEL") == 1
    assert "# Operator directive" not in again                # the overlay held nothing new
    assert again == first                                     # idempotent
    # the tmux seat: legacy full file not composed; a plain overlay composed once
    tm = DV.compose("tmux", snap(directive=LEGACY), backend="claude-code")
    assert "LEGACY-SENTINEL" not in tm and len(DV.BEGIN_RE.findall(tm)) == 1
    ov = DV.compose("tmux", snap(directive="TMUX-OVERLAY", guidance=GUIDANCE + "TMUX-OVERLAY\n"),
                    backend="claude-code")
    assert ov.count("TMUX-OVERLAY") == 1
    assert DV.legacy_report(snap(directive=LEGACY))["composed"] is False
    assert DV.strip_generated(first).count("<!-- hugpy-station directive") == 0


def test_overlays_and_switches():
    s = snap(dir_worker="WORKER-OVERLAY", handoff="HANDOFF-SENTINEL",
             fs=json.dumps({"mediated": True}),
             delegate=json.dumps({"claude-code": True, "mct": False, "serve": True}))
    out = render(s)
    assert "WORKER-OVERLAY" in out["worker"]
    assert all("WORKER-OVERLAY" not in out[k] for k in DV.KINDS if k != "worker")
    assert DV.DELEGATE_ONLY_TEXT in out["keeper"] and DV.DELEGATE_ONLY_TEXT in out["chat"]
    assert DV.DELEGATE_ONLY_TEXT not in out["worker"] and DV.DELEGATE_ONLY_TEXT not in out["local"]
    cc = DV.compose("tmux", s, backend="claude-code", with_handoff=True)
    assert DV.FS_MEDIATED_TEXT in cc and DV.DELEGATE_ONLY_TEXT in cc and "HANDOFF-SENTINEL" in cc
    mct = DV.compose("tmux", s, backend="mct", with_handoff=True)
    assert DV.FS_MEDIATED_TEXT not in mct and DV.DELEGATE_OFF_TEXT in mct
    codex = DV.compose("tmux", s, backend="codex")
    assert "Delegation switch" not in codex and "Filesystem switch" not in codex
    # the one-shot handoff never rides a serve session's per-turn directive
    assert all("HANDOFF-SENTINEL" not in t for t in out.values())


def test_render_state_writes_derived_files(tmp_path):
    st = tmp_path / "state"
    (st / "mct" / "repl").mkdir(parents=True)
    (st / "mct" / "repl" / "operator-guidance.user.md").write_text(GUIDANCE)
    (st / "directives").mkdir()
    (st / "directives" / "chat.operator.md").write_text("CHAT-OVERLAY")
    r = DV.render_state(st, locus="keeper", station_port="8898", env={})
    out = st / DV.RENDERED_DIR
    assert sorted(p.name for p in out.glob("*.md")) == sorted(k + ".md" for k in DV.KINDS)
    assert "CHAT-OVERLAY" in (out / "chat.md").read_text()
    assert "OPERATOR-GUIDANCE-SENTINEL" in (out / "keeper.md").read_text()
    idx = json.loads((out / "index.json").read_text())
    assert idx["facts"]["locus"] == "keeper" and set(idx["kinds"]) == set(DV.KINDS)
    assert len(r["changed"]) == len(DV.KINDS)
    again = DV.render_state(st, locus="keeper", station_port="8898", env={})
    assert again["changed"] == []                             # unchanged text is not rewritten


def test_layer_files_match_locus_exec():
    import locus_exec as LX
    for k, rel in DV.LAYER_FILES.items():
        assert LX.CFG_FILES.get(k) == rel, k
