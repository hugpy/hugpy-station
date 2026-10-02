"""1.0.138 channel push: a comms ping whose ref is a channel id (`ch_…`, stored by
comms_ping in the note as "(ref ch_…)") skips the NUDGE_MIN_INTERVAL batch window
and reaches the keeper at once through _nudge_frontier — serve first, tmux only
as the fallback. Ordinary board pings still wait out the window.

Exercises the REAL server.py functions (AST-extracted) + keeper_nudge.py, with
the toolserver, serve and tmux stubbed.
Run:  python3 -m pytest resources/backend/test_channel_ping_nudge.py
  or: python3 resources/backend/test_channel_ping_nudge.py
"""
import ast
import asyncio
import json
import logging
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import keeper_nudge  # noqa: E402
import issue_gate  # noqa: E402
import bug_route  # noqa: E402

FUNCS = {"_deliver_pings", "_nudge_frontier", "_is_channel_ping", "_nudge_pending",
         "_nudge_pending_write", "_pings_seen", "_pings_seen_write", "_mail_row",
         "_gate_pings", "_ping_sender", "_nudge_drain", "_issue_gate"}
CONSTS = {"_CHANNEL_REF_RE", "FLEET_MSG_MAX", "_KEEP"}


def load(tmp, inbox, open_ids, serve_ok=True, page=True, gate_down=False, surface="serve"):
    """``page`` / ``gate_down`` script the toolserver issue gate: since 2026-10-01
    (d4187 / t4178) it is consulted for ANNOTATION only — a page=false answer or
    an unreachable toolserver must never withhold a ping (sent["gate"] records
    every observation so a test can check what the gate saw)."""
    tree = ast.parse((HERE / "server.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body
             if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FUNCS)
             or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in CONSTS for t in n.targets))]
    tmp = Path(tmp)
    sent = {"serve": [], "tmux": [], "closed": [], "gate": []}

    async def ts_call(app, path, body=None, timeout=8):
        if path == "comms/inbox":
            return {"pings": list(inbox)}
        if path == "todo/list":
            return [{"id": i} for i in open_ids]
        if path == "todo/done":
            sent["closed"].append(body["id"])
            return {}
        if path == "issue/observe":                   # annotation only: never withholds
            sent["gate"].append(dict(body))
            if gate_down:
                raise ConnectionRefusedError("toolserver :7004 down")
            return {"fp": "%s:%016x" % (body["kind"], abs(hash(body["subject"])) % (1 << 64)),
                    "page": page, "state": "raised", "reason": "first sighting" if page else "already raised"}
        raise AssertionError(path)

    async def nudge_serve(app, line):
        sent["serve"].append(line)
        return (serve_ok, "queued" if serve_ok else "serve down")

    async def nudge_tmux(app, line):
        sent["tmux"].append(line)
        return (True, "typed")

    def read_json(p, default):
        try:
            return json.loads(Path(p).read_text())
        except (OSError, ValueError):
            return default

    async def reroute(app, bugs):
        sent.setdefault("rerouted", []).extend(b["id"] for b in bugs)

    async def surface_now(vm=""):                  # 1.0.147: the locus's keeper surface ("tmux" default live)
        return surface, {"ok": True}

    import secrets
    ns = dict(re=re, json=json, time=time, secrets=secrets, Path=Path,
              KEEPER_SURFACE=surface, _keeper_surface_now=surface_now,
              _ts_call=ts_call, _station_locus=lambda: "keeper", _read_json=read_json,
              _canon_locus=lambda n: (n or "").lower(),
              _NUDGE_PENDING_PATH=tmp / "pending.json", _PINGS_SEEN_PATH=tmp / "seen.json",
              _keeper_mail_rows=lambda: [],
              _keeper_mail_write=lambda rows: sent.setdefault("mail", []).extend(rows),
              _bugr=bug_route, _reroute_bug_pings=reroute,
              _knudge=keeper_nudge, NUDGE_MIN_INTERVAL=300, _ig=issue_gate, KEEPER_TARGET="keeper",
              _nudge_serve=nudge_serve, _nudge_tmux=nudge_tmux,
              _audit_line=lambda *a: None, _rlog=logging.getLogger("t"),
              _hold_note=lambda *a, **k: None, _hold_clear=lambda *a, **k: None, _sh=None)   # 1.0.146 strip rows
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "server.py", "exec"), ns)
    assert not (FUNCS - set(ns)), FUNCS - set(ns)
    return ns, sent


def _run(ns, last_sent):
    ns["_NUDGE_PENDING_PATH"].write_text(json.dumps({"pings": [], "last_sent": last_sent}))
    app = {"_rt": {}}
    asyncio.run(ns["_deliver_pings"](app))
    return json.loads(ns["_NUDGE_PENDING_PATH"].read_text())


def test_is_channel_ping():
    with tempfile.TemporaryDirectory() as d:
        ns, _ = load(d, [], [])
    ch = ns["_is_channel_ping"]
    assert ch({"note": "[ping] from op-keeper (ref ch_7f3a)"})
    assert ch({"ref": "ch_abc"})
    assert not ch({"note": "[ping][msg] from keeper (ref hugpy live tree react/ui WorkerRow.jsx)"})
    assert not ch({"note": "[ping] from keeper"})
    assert not ch(None)


def test_board_ping_waits_out_the_window():
    ping = {"id": "r1", "text": "board thing", "note": "[ping] from aeb"}
    with tempfile.TemporaryDirectory() as d:
        ns, sent = load(d, [ping], ["r1"])
        doc = _run(ns, last_sent=time.time() - 10)          # inside the 300 s window
    assert sent["serve"] == [] and sent["tmux"] == []
    assert [p["id"] for p in doc["pings"]] == ["r1"]       # still pending for later


def test_channel_ping_bypasses_the_window_and_goes_serve_first():
    ping = {"id": "r2", "text": "hello over the channel", "note": "[ping] from op-keeper (ref ch_7f3a)"}
    with tempfile.TemporaryDirectory() as d:
        ns, sent = load(d, [ping], ["r2"])
        doc = _run(ns, last_sent=time.time() - 10)          # same window: pushed anyway
    assert len(sent["serve"]) == 1 and "r2" in sent["serve"][0]
    assert sent["tmux"] == [] and sent["closed"] == []      # request kind: stays open on the board
    assert doc["pings"] == [] and doc["last_sent"] > time.time() - 5


def test_channel_ping_never_falls_back_to_tmux_by_default(monkeypatch=None):
    """1.0.140: STATION_NUDGE_TMUX unset/0 — serve down = left pending, logged."""
    import os
    os.environ.pop("STATION_NUDGE_TMUX", None)
    ping = {"id": "r3", "text": "ping", "note": "[ping] from op-keeper (ref ch_x1)"}
    with tempfile.TemporaryDirectory() as d:
        ns, sent = load(d, [ping], ["r3"], serve_ok=False)
        doc = _run(ns, last_sent=time.time())
    assert len(sent["serve"]) == 1 and sent["tmux"] == []
    assert [p["id"] for p in doc["pings"]] == ["r3"]       # still pending, on the board


def test_channel_ping_falls_back_to_tmux_only_when_flag_is_1():
    import os
    os.environ["STATION_NUDGE_TMUX"] = "1"
    try:
        ping = {"id": "r3", "text": "ping", "note": "[ping] from op-keeper (ref ch_x1)"}
        with tempfile.TemporaryDirectory() as d:
            ns, sent = load(d, [ping], ["r3"], serve_ok=False)
            _run(ns, last_sent=time.time())
        assert len(sent["serve"]) == 1 and len(sent["tmux"]) == 1
    finally:
        os.environ.pop("STATION_NUDGE_TMUX", None)


def test_every_request_ping_nudges_without_the_gate():
    """2026-10-01 (t4178): the issue gate annotates but never withholds — a peer/
    channel ping always reaches the keeper, whether the gate says page=false or
    the toolserver is down (1.0.144: the gate is still OBSERVED, for the record)."""
    pings = [{"id": "r5", "text": "hello", "note": "[ping] from op-keeper (ref ch_7f3a)"},
             {"id": "r6", "text": "status?", "note": "[ping] from aeb"}]
    for kw in ({"page": False}, {"gate_down": True}):
        with tempfile.TemporaryDirectory() as d:
            ns, sent = load(d, pings, ["r5", "r6"], **kw)
            doc = _run(ns, last_sent=0)
        assert len(sent["serve"]) == 1 and "2 new board pings" in sent["serve"][0], kw
        assert doc["pings"] == [], kw
        assert [(g["kind"], g["source"], g["caller"]) for g in sent["gate"]] == \
            [("channel", "channel:ch_7f3a", "op-keeper->keeper"), ("comms", "comms:aeb", "aeb->keeper")], kw


def test_gate_annotates_and_names_the_receiver_for_another_locus():
    """1.0.144: a ping relayed to ANOTHER locus is observed as `sender->receiver`
    (a keeper -> hugpy delegation is not that locus's own run); the gate's fp
    rides along on the pending row, and a note-tagged issue_fp is kept as is."""
    tagged = {"id": "r10", "text": "x", "note": "[ping] from aeb\n\nissue_fp=finding:x:0123456789abcdef"}
    plain = {"id": "r11", "text": "deploy please", "note": "[ping] from keeper"}
    msg = {"id": "r12", "text": "hi", "note": "[ping][msg] from op"}
    with tempfile.TemporaryDirectory() as d:
        ns, sent = load(d, [], [])
        out = asyncio.run(ns["_gate_pings"]({"_rt": {}}, [tagged, plain, msg], locus="hugpy"))
    assert [q["id"] for q in out] == ["r10", "r11"]
    assert out[0]["issue_fp"] == "finding:x:0123456789abcdef"
    assert out[1]["issue_fp"].startswith("comms:")
    assert [(g["caller"], g["locus"]) for g in sent["gate"]] == [("keeper->hugpy", "hugpy")]


def test_bug_report_ping_goes_to_the_worker_never_the_keeper():
    """An older station's finding [ping] is re-routed: no keeper mail, no keeper nudge."""
    bug = {"id": "r7", "text": "[finding] rate_limit_429 keeper · ac-serve-station: 429 ×3",
           "note": "[ping] from keeper (ref station-findings:abc)\n\nissue_fp=finding:rate_limit_429:0123456789abcdef"}
    loop = {"id": "r8", "text": "[hs-fresh] [loop] job: v1-77 ×4", "note": "[ping] from hs-fresh"}
    real = {"id": "r9", "text": "please look at t12", "note": "[ping] from aeb"}
    with tempfile.TemporaryDirectory() as d:
        ns, sent = load(d, [bug, loop, real], ["r7", "r8", "r9"])
        _run(ns, last_sent=0)
    assert sent["rerouted"] == ["r7", "r8"]
    assert [r["text"].split("]")[0] for r in sent["mail"]] == ["[board r9"]      # only the real ping is mailed
    assert len(sent["serve"]) == 1 and "r9" in sent["serve"][0] and "finding" not in sent["serve"][0]


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if failed else 0)
