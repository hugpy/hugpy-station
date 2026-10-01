"""1.0.140: the station behind the toolserver issue gate (abstract_toolserver 0.0.39).

Every toolserver call is MOCKED (a fake ``call`` / ``_ts_call``); nothing here
reaches the network, the board or serve. The server.py functions are the REAL
ones, AST-extracted (as test_channel_ping_nudge.py does).

Run:  python3 -m pytest resources/backend/test_issue_gate.py
"""
import ast
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import b_propose  # noqa: E402
import issue_gate  # noqa: E402
import keeper_notify  # noqa: E402
import keeper_nudge  # noqa: E402
import loop_detector  # noqa: E402

TREE = ast.parse((HERE / "server.py").read_text(encoding="utf-8"))


def extract(funcs, consts=(), **ns):
    nodes = [n for n in TREE.body
             if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in funcs)
             or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in consts for t in n.targets))]
    base = dict(re=re, json=json, time=time, os=os, asyncio=asyncio, Path=Path, logging=logging,
                _ig=issue_gate, _kn=keeper_notify, _knudge=keeper_nudge, _bprop=b_propose,
                KEEPER_TARGET="keeper", _rlog=logging.getLogger("t"), _bslog=logging.getLogger("t"),
                _todo_hist=lambda *a, **k: None, _audit_line=lambda *a: AUDIT.append(a),
                _notify_origin=lambda: "keeper")
    base.update(ns)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "server.py", "exec"), base)
    missing = set(funcs) - set(base)
    assert not missing, missing
    return base


AUDIT = []


class FakeTS:
    """The toolserver's issue category, scripted. ``pages``: fp-or-subject ->
    page bool (default True); ``down``: every call raises (unreachable)."""

    def __init__(self, page=True, down=False, state="raised", b_ok=True):
        self.calls, self.page, self.down, self.state, self.b_ok = [], page, down, state, b_ok

    async def __call__(self, path, body):
        self.calls.append((path, dict(body)))
        if self.down:
            raise ConnectionRefusedError("toolserver :7004 down")
        if path == "issue/observe":
            page = self.page(body) if callable(self.page) else self.page
            fp = "%s:%016x" % (body["kind"], abs(hash(body["subject"])) % (1 << 64))
            return {"fp": fp, "page": page, "state": "raised", "b_query": page,
                    "reason": "first sighting" if page else "already raised"}
        if path == "issue/get":
            return {"fp": body["fp"], "state": self.state}
        if path == "issue/b_query_ok":
            return {"fp": body["fp"], "ok": self.b_ok}
        if path == "issue/memory":
            return {"fp": body["fp"], "brief": "ISSUE %s raised x3; last fix DID NOT HOLD" % body["fp"],
                    "proposals": [{"id": "r900", "status": "delivered", "fix": "restart the unit",
                                   "commands": ["systemctl --user restart x"], "disposition": "rejected",
                                   "reason": "did nothing"}],
                    "last_fix": {"held": False, "recurrences_since": 4}}
        return {"ok": True}

    def of(self, path):
        return [b for p, b in self.calls if p == path]


def gate_for(ts):
    return lambda app: issue_gate.Gate(ts, locus="keeper")


# ── issue_gate.py ───────────────────────────────────────────────────────────────────
def test_observe_fails_closed_and_writes_fail_open():
    g = issue_gate.Gate(FakeTS(down=True))
    assert asyncio.run(g.observe(source="s", kind="loop:job", subject="x")) is None      # no decision
    assert asyncio.run(g.b_query_ok("loop:job:0123456789abcdef")) is None
    assert asyncio.run(g.memory("loop:job:0123456789abcdef")) is None
    assert asyncio.run(g.set_state("f", "processed")) is None                            # swallowed
    assert asyncio.run(g.record_b("f", {"summary": "x"})) is None


def test_fp_tag_round_trip():
    fp = "finding:rate_limit_429:0123456789abcdef"
    note = issue_gate.tag("close with a disposition line", fp)
    assert issue_gate.fp_of({"text": "[finding] x", "note": note}) == fp
    assert issue_gate.tag(note, fp) == note                                              # idempotent
    assert issue_gate.fp_of({"text": "plain ping"}) == ""


def test_gate_rows_partitions():
    ts = FakeTS(page=lambda b: b["subject"] == "a")
    rows = [{"k": "a"}, {"k": "b"}, {"k": "skip"}]
    res = asyncio.run(issue_gate.gate_rows(issue_gate.Gate(ts), rows,
                                           lambda r: None if r["k"] == "skip" else
                                           {"source": "t", "kind": "manual", "subject": r["k"]}))
    assert [r["k"] for r, _ in res["page"]] == ["a"] and [r["k"] for r, _ in res["quiet"]] == ["b"]
    assert rows[0]["issue_fp"] and len(ts.of("issue/observe")) == 2


# ── findings (2026-10-01: no gate on delivery; bug reports -> the worker) ──────────
def _frow(key, sig, count, source="hugpy-worker.service", sev="high"):
    return {"key": key, "sigkey": "s" + key, "kind": "traceback", "signature": sig, "count": count,
            "source": source, "locus": "keeper", "severity": sev, "sample_lines": ["Traceback: boom"],
            "propose_due": True}


# ── loops ───────────────────────────────────────────────────────────────────────────
def test_loop_detector_threads_caller_and_session():
    det = loop_detector.LoopDetector()
    det.observe_jobs([{"id": "v1-abc123", "status": "processing", "model_key": "Qwen3-Coder-Next",
                       "worker": "ae-worker", "progressed_at": time.time() - 900,
                       "client_task": "hugpy-agent run:9f8e7d", "client_session": "cs-cd165265"}])
    row = det.finish_cycle()["new"][0]
    assert row["caller"] == "hugpy-agent run:9f8e7d" and row["session_id"] == "cs-cd165265"


# ── B: query gate, per-fp memory, record_b ─────────────────────────────────────────
def test_b_prompt_uses_issue_memory_not_notifybook():
    sig = {"proposals": [{"id": "r1", "status": "delivered", "proposed_fix": "OTHER ISSUE FIX"}],
           "did_not_hold": "r1"}
    mem = asyncio.run(issue_gate.Gate(FakeTS()).memory("finding:x:0123456789abcdef"))
    user = b_propose.build_messages({"kind": "traceback", "signature": "KeyError"}, [], sig, memory=mem)[1]["content"]
    assert "ISSUE MEMORY (this issue only — fp finding:x:0123456789abcdef)" in user
    assert "restart the unit" in user and "did nothing" in user and "did NOT hold" in user
    assert "OTHER ISSUE FIX" not in user                                       # nothing from the file book
    # fallback: no memory (toolserver down) -> the NotifyBook record is read through
    fb = b_propose.build_messages({"kind": "t", "signature": "s"}, [], sig)[1]["content"]
    assert "OTHER ISSUE FIX" in fb


def _propose_ns(ts, b_text):
    sent = {"b_calls": [], "delivered": []}

    def b_call(msgs):
        sent["b_calls"].append(msgs)
        return b_text, 100, None

    async def deliver(app, book, sink, row, sk, p, entry=None):
        sent["delivered"].append(p)
        return "r1000"

    async def loops_get(app, path):
        return {"counts": {"waiting": 0}}

    ns = extract({"_propose_run"}, _issue_gate=gate_for(ts), _notify_sink=lambda app: None,
                 _propose_undelivered=lambda book: [], _loops_get=loops_get,
                 _propose_roots=lambda row: [], _b_propose_call=b_call, _meter_tokens=lambda *a: None,
                 _propose_deliver=deliver, B_PROPOSE_PER_SCAN=2, _PROPOSE_BUSY=False)
    return ns, sent


def test_b_not_queried_when_gate_says_no():
    ts = FakeTS(b_ok=False)
    ns, sent = _propose_ns(ts, "{}")
    book = keeper_notify.NotifyBook()
    row = dict(_frow("a", "KeyError", 2), issue_fp="finding:traceback:0123456789abcdef")
    asyncio.run(ns["_propose_run"]({}, book, [row]))
    assert sent["b_calls"] == [] and row["propose_due"] is False


def test_b_not_queried_without_a_gate_answer_but_stays_due():
    ns, sent = _propose_ns(FakeTS(down=True), "{}")
    row = dict(_frow("a", "KeyError", 2), issue_fp="finding:traceback:0123456789abcdef")
    asyncio.run(ns["_propose_run"]({}, keeper_notify.NotifyBook(), [row]))
    assert sent["b_calls"] == [] and row["propose_due"] is True


def test_b_queried_with_memory_and_duplicate_recorded_to_the_issue():
    ts = FakeTS(b_ok=True)
    dup = json.dumps({"summary": "s", "proposed_fix": "restart the unit", "commands": []})
    ns, sent = _propose_ns(ts, dup)
    row = dict(_frow("a", "KeyError", 2), issue_fp="finding:traceback:0123456789abcdef")
    asyncio.run(ns["_propose_run"]({}, keeper_notify.NotifyBook(), [row]))
    assert len(sent["b_calls"]) == 1 and "ISSUE MEMORY" in sent["b_calls"][0][1]["content"]
    assert ts.of("issue/memory")[0]["fp"] == row["issue_fp"]
    rb = ts.of("issue/record_b")
    assert rb and rb[0]["status"] == "duplicate" and sent["delivered"] == []   # novelty vs issue priors


# ── dispositions → issue_set / issue_record_action ─────────────────────────────────
def test_dispositions_forwarded_once():
    ts = FakeTS()

    class Sink:
        async def closed(self, ids):
            return {"r10": "handled it", "r11": "x\ninert: noisy client disconnects", "r12": "reject: wrong cause"}

    ns = extract({"_notify_poll_dispositions", "_book_issue_fp"}, _issue_gate=gate_for(ts))
    book = keeper_notify.NotifyBook()
    book.rows = {"f1": {"key": "f1", "sigkey": "s1", "board_id": "r10", "active": True, "issue_fp": "finding:a:1"},
                 "f2": {"key": "f2", "sigkey": "s2", "board_id": "r11", "active": True, "issue_fp": "finding:b:2"}}
    book.sigs = {"s3": {"disposition": "proposed", "issue_fp": "finding:c:3",
                        "proposals": [{"id": "r12", "status": "delivered", "disposition": "open"}]}}
    asyncio.run(ns["_notify_poll_dispositions"]({}, book, Sink()))
    sets = {b["fp"]: b["state"] for b in ts.of("issue/set")}
    assert sets == {"finding:a:1": "processed", "finding:b:2": "benign", "finding:c:3": "raised"}
    ra = ts.of("issue/record_action")
    assert ra[0]["fp"] == "finding:c:3" and ra[0]["disposition"] == "rejected" and ra[0]["proposal_id"] == "r12"
    n = len(ts.calls)
    asyncio.run(ns["_notify_poll_dispositions"]({}, book, Sink()))             # same closes again
    assert len(ts.calls) == n                                                  # not re-forwarded


def test_loop_close_forwarded_but_station_auto_resolve_is_not():
    ts = FakeTS()

    class Sink:
        async def closed(self, ids):
            return {"r20": "x\naccept", "r21": "loop stopped 2026-10-01 03:00 — auto-resolved by the station "
                                               "loop detector (it saw no recurrence for one cycle)"}

    class Det:
        loops = {"a": {"key": "a", "identity": "w", "board_id": "r20", "issue_fp": "loop:worker:1"},
                 "b": {"key": "b", "identity": "q", "board_id": "r21", "issue_fp": "loop:queue:2"}}

        def save(self):
            pass
    ns = extract({"_loops_read_dispositions"})
    asyncio.run(ns["_loops_read_dispositions"](keeper_notify.NotifyBook(), Det(), Sink(), issue_gate.Gate(ts)))
    assert [(b["fp"], b["state"]) for b in ts.of("issue/set")] == [("loop:worker:1", "processed")]
    assert ts.of("issue/record_action")[0]["fix"] is True


# ── comms relay: every request ping nudges (2026-10-01, t4178) ───────────────────
def test_every_request_ping_nudges_no_gate():
    """The gate is consulted for annotation only (1.0.144 signature: async, app +
    pings): page=false and a down toolserver both still nudge; [msg] is filed, not nudged."""
    tagged = {"id": "r1", "text": "x", "note": "[ping] from keeper\n\nissue_fp=finding:x:0123456789abcdef"}
    plain = {"id": "r2", "text": "[ping] from aeb [aeb] something broke", "note": "[ping] from aeb"}
    msg = {"id": "r3", "text": "hi", "note": "[ping][msg] from op"}
    for ts in (FakeTS(page=False), FakeTS(down=True)):
        ns = extract({"_gate_pings", "_ping_sender", "_is_channel_ping"}, consts={"_CHANNEL_REF_RE"},
                     _issue_gate=gate_for(ts), _canon_locus=lambda n: (n or "").lower())
        out = asyncio.run(ns["_gate_pings"]({"_rt": {}}, [tagged, plain, msg], locus="keeper"))
        assert [q["id"] for q in out] == ["r1", "r2"]
        assert out[0]["issue_fp"] == "finding:x:0123456789abcdef"           # note tag kept, gate not asked
        assert [b["caller"] for b in ts.of("issue/observe")] == ["aeb->keeper"]   # sender->receiver
        assert ("issue_fp" in out[1]) is (not ts.down)                       # annotated when it answers
    assert [q["id"] for q in asyncio.run(ns["_gate_pings"]({"_rt": {}}, [msg], include_msg=True))] == ["r3"]


# ── delivery: fresh session + drain ────────────────────────────────────────────────
def _drain_ns(tmp, queue, head="cs-a"):
    posts = []

    async def serve_get(app, path, timeout=8):
        return (200, queue) if queue is not None else (400, {"error": "Unknown session"})

    async def serve_post(app, path, body, timeout=8):
        posts.append((path, body))
        return 200, {}

    async def keeper_session(app):
        return head, "roster"

    def read_json(p, default):
        try:
            return json.loads(Path(p).read_text())
        except (OSError, ValueError):
            return default
    ns = extract({"_nudge_drain", "_keeper_console_head", "_nudge_pending", "_nudge_pending_write",
                  "_serve_queue_release"}, {"_KEEP"},
                 _serve_get=serve_get, _serve_post=serve_post, _keeper_serve_session=keeper_session,
                 _read_json=read_json, _NUDGE_PENDING_PATH=Path(tmp) / "pending.json",
                 _hold_note=lambda *a, **k: None, _hold_clear=lambda *a, **k: None, _sh=None)   # 1.0.146
    return ns, posts


def test_auto_off_strand_is_kicked_and_rollover_requeues():
    infl = {"sid": "cs-a", "message_ids": ["m1"], "items": [{"id": "r7", "text": "x"}], "at": 1}
    with tempfile.TemporaryDirectory() as d:
        q = {"items": [{"id": "m1"}], "busy": False, "auto": False, "paused": False}
        ns, posts = _drain_ns(d, q)
        ns["_NUDGE_PENDING_PATH"].write_text(json.dumps({"pings": [], "last_sent": 5, "inflight": infl}))
        assert asyncio.run(ns["_nudge_drain"]({})) is True
        # 1.0.146: the kick is serve-core 0.1.15's "release" (0.1.14 fallback to "retry" only on HTTP 400)
        assert posts == [("/api/console/queue", {"session_id": "cs-a", "action": "release", "by": "station-nudge"})]
    with tempfile.TemporaryDirectory() as d:                                   # rolled over to cs-b
        ns, posts = _drain_ns(d, {"items": [{"id": "m1"}], "busy": True, "auto": True}, head="cs-b")
        ns["_NUDGE_PENDING_PATH"].write_text(json.dumps({"pings": [], "last_sent": 5, "inflight": infl}))
        assert asyncio.run(ns["_nudge_drain"]({})) is False
        doc = json.loads(ns["_NUDGE_PENDING_PATH"].read_text())
        assert [p["id"] for p in doc["pings"]] == ["r7"] and "inflight" not in doc and doc["last_sent"] == 0
        assert posts == [("/api/console/queue", {"session_id": "cs-a", "action": "remove", "id": "m1"})]
    with tempfile.TemporaryDirectory() as d:                                   # drained
        ns, posts = _drain_ns(d, {"items": [], "busy": True, "auto": False})
        ns["_NUDGE_PENDING_PATH"].write_text(json.dumps({"pings": [], "last_sent": 5, "inflight": infl}))
        assert asyncio.run(ns["_nudge_drain"]({})) is False and posts == []
        assert "inflight" not in json.loads(ns["_NUDGE_PENDING_PATH"].read_text())


# ── STATION_BUGSCAN=0 ──────────────────────────────────────────────────────────────
def test_bugscan_env_off_wins_over_persisted_state():
    state = {"loci": {"": {"on": True, "interval": 600, "last": 0, "note": ""}}}
    ns = extract({"_bugscan_on", "_bugscan_locus_state", "_bugscan_run"}, {"BUGSCAN_OFF_NOTE"},
                 BUGSCAN_ON=False, BUGSCAN_INTERVAL=600, _bugscan_state=lambda: state, _BUGSCAN_BUSY={})
    row = ns["_bugscan_locus_state"]("")
    assert row["on"] is True and ns["_bugscan_on"](row) is False               # persisted kept, not effective
    res = asyncio.run(ns["_bugscan_run"]({}, ""))
    assert res == {"ok": False, "note": "disabled by STATION_BUGSCAN=0"}
    ns["BUGSCAN_ON"] = True
    assert ns["_bugscan_on"](row) is True


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
