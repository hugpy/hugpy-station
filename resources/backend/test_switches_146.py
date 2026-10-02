"""1.0.146 — the disabling-switch audit, station side (board t4260, t4250;
operator ruling d4254): a core guarantee never has an off switch, a silent
hold, an attempt cap or an unbounded defer; what remains is a bounded coalesce
or an explicit, owned, visible, expiring operator action.

  1  digest: a busy keeper QUEUES the digest (console submit), state "queued"
  2  reminders: no _REMIND_CAP; the delivery count rides the comment
  3  REMIND_MITIGATOR: live value in the settings payload + a strip row when off
  4  nudge drain "wait" is bounded (15 min) -> requeue + audit + strip counter
  5  "board status unknown" -> comms-nudge-skip + strip counter, never silent
  6  comms-nudge-skip reason + pending count on the strip
  7  hand-off dedupe has a 6 h age floor
  8  a failed serve prompt is re-pended to the locus's pending file, never "failed"
  9  bug-report inflight has a 15 min drain deadline + requeue
 10  frontier-keeper.json / frontier-delegate.json carry by / at / expires
 11  managed-locus standing config (permission mode, cwd, roles) + LOCUS NUANCES
 12  queue action "release" (serve-core 0.1.15), both response shapes tolerated

server.py pieces are AST-extracted and driven with fakes; the standing-config
script runs FOR REAL against a temp state dir. Nothing touches a live serve,
seat, station or the network.
Run:  python3 -m pytest resources/backend/test_switches_146.py
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
import bug_route  # noqa: E402
import directives as DV  # noqa: E402
import keeper_nudge as kg  # noqa: E402
import locus_exec as LX  # noqa: E402
import prompt_send as PS  # noqa: E402
import station_holds as SH  # noqa: E402
import todo_digest as TD  # noqa: E402

SRC = (HERE / "server.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
T0 = 1_790_900_000.0


def run(coro):
    return asyncio.run(coro)


def extract(names, consts=(), **ns):
    nodes = [n for n in TREE.body
             if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names)
             or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in consts for t in n.targets))]
    base = dict(re=re, json=json, time=time, os=os, asyncio=asyncio, logging=logging, Path=Path,
                _knudge=kg, _sh=SH, TD=TD, PS=PS, _rlog=logging.getLogger("t"), _audit_line=lambda *a: None,
                _hold_note=lambda *a, **k: None, _hold_clear=lambda *a, **k: None)
    base.update(ns)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "server.py", "exec"), base)
    missing = set(names) - set(base)
    assert not missing, missing
    return base


class Audit:
    def __init__(self):
        self.lines, self.holds, self.cleared = [], {}, []

    def line(self, action, detail):
        self.lines.append((action, detail))

    def note(self, key, source, identity, detail="", action="", **kw):
        r = self.holds.setdefault(key, {"count": 0})
        r["count"] = r["count"] + 1 if kw.get("inc") else int(kw.get("count") or r["count"] or 1)
        r.update(source=source, identity=identity, detail=detail)
        return r

    def clear(self, key):
        self.cleared.append(key)
        return self.holds.pop(key, None)


# ── station_holds: the visible registry behind the ⚠ strip ───────────────────
def test_holds_count_persist_and_leave_the_strip_when_cleared(tmp_path):
    h = SH.Holds(str(tmp_path / "holds.json"), now=lambda: T0)
    h.note("skip:comms-nudge@hugpy", SH.SOURCE_SKIP, "📨 nudge for hugpy not sent", "3 pending; no serve", count=3)
    h.note("count:requeue", SH.SOURCE_HOLD, "re-pended", inc=True)
    h.note("count:requeue", SH.SOURCE_HOLD, "re-pended", inc=True)
    rows = SH.Holds(str(tmp_path / "holds.json")).rows()          # survives a restart
    byk = {r["key"]: r for r in rows}
    assert byk["skip:comms-nudge@hugpy"]["count"] == 3 and byk["count:requeue"]["count"] == 2
    assert all(r["hold"] and r["active"] and r["source"].startswith("station:") for r in rows)
    assert h.clear("skip:comms-nudge@hugpy") and "skip:comms-nudge@hugpy" not in {r["key"] for r in h.rows()}


# ── 4 + 12: bounded wait, 0.1.15 holds, release ───────────────────────────────
def test_drain_wait_is_bounded_then_requeues():
    w = {"sid": "cs-1", "message_ids": ["m1"], "at": T0}
    q = {"items": [{"id": "m1"}], "busy": True, "paused": False}
    assert kg.drain_action(w, q, "cs-1", T0 + 60) == "wait"
    assert kg.drain_action(w, q, "cs-1", T0 + kg.WAIT_MAX_S - 1) == "wait"
    assert kg.drain_action(w, q, "cs-1", T0 + kg.WAIT_MAX_S) == "requeue"
    assert kg.wait_expired(w, T0 + kg.WAIT_MAX_S) and not kg.wait_expired(w, T0 + 10)
    assert kg.drain_action(w, dict(q, busy=False), "cs-1", T0 + 99999) == "kick"


def test_drain_waits_out_an_owned_expiring_hold_and_never_releases_it():
    """serve-core 0.1.15: paused = an operator's hold {by, until}. The station
    waits for its expiry (bounded by the hold itself) and only then kicks."""
    w = {"sid": "cs-1", "message_ids": ["m1"], "at": T0}
    q = {"items": [{"id": "m1"}, {"id": "other"}], "busy": False, "paused": True,
         "hold": {"by": "operator", "at": T0, "until": T0 + 1800}}
    assert kg.drain_action(w, q, "cs-1", T0 + 1700) == "wait"             # others queued too: still never requeued
    assert kg.drain_action(w, q, "cs-1", T0 + 1800 + 120) == "kick"       # expired but not lifted
    legacy = {"items": [{"id": "m1"}, {"id": "other"}], "busy": False, "paused": True}
    assert kg.drain_action(w, legacy, "cs-1", T0 + 10) == "requeue"       # 0.1.14 hold on someone else's batch


def test_queue_action_ok_accepts_both_release_shapes():
    assert kg.QUEUE_RELEASE == "release"
    assert kg.queue_action_ok(200, {"session_id": "cs-1", "auto": True, "busy": False, "paused": False, "items": []})
    assert kg.queue_action_ok(200, {"session_id": "cs-1", "items": [], "hold": None, "actions": ["add", "release"]})
    assert kg.queue_action_ok(200, {"ok": True, "action": "release"})
    assert not kg.queue_action_ok(400, {"error": "Unknown queue action 'release'"})
    assert not kg.queue_action_ok(200, {"ok": False}) and not kg.queue_action_ok(200, None)


def test_server_release_falls_back_to_retry_on_a_0_1_14_serve():
    calls = []

    async def old(method, path, body):
        calls.append(body["action"])
        if body["action"] == "release":
            return 400, {"error": "Unknown queue action"}
        return 200, {"session_id": "cs-1", "items": [], "busy": False, "paused": False, "auto": True}

    async def new(method, path, body):
        calls.append(body["action"])
        return 200, {"session_id": "cs-1", "items": [], "hold": None, "actions": list(kg.QUEUE_RELEASE)}
    ns = extract({"_serve_queue_release"})
    st, doc = run(ns["_serve_queue_release"](old, "cs-1"))
    assert calls == ["release", "retry"] and kg.queue_action_ok(st, doc)
    calls.clear()
    st, doc = run(ns["_serve_queue_release"](new, "cs-1", by="op"))
    assert calls == ["release"] and kg.queue_action_ok(st, doc)
    assert '"action": "retry"}' not in SRC.replace("{\"session_id\": sid, \"action\": \"retry\"}", "FALLBACK")  \
        or SRC.count('"action": "retry"') == 1      # the only "retry" left is the 0.1.14 fallback


def test_nudge_drain_requeues_after_the_wait_bound_with_audit_and_counter(tmp_path):
    au = Audit()
    posts = []

    async def keeper_session(app, base=None):
        return "cs-1", "roster"

    async def serve_get(app, path, timeout=8, base=None):
        return 200, {"items": [{"id": "m1"}], "busy": True, "auto": True, "paused": False}

    async def serve_post(app, path, body, timeout=8, base=None):
        posts.append(body)
        return 200, {}

    def read_json(p, default):
        try:
            return json.loads(Path(p).read_text())
        except (OSError, ValueError):
            return default
    pend = tmp_path / "pending.json"
    ns = extract({"_nudge_drain", "_keeper_console_head", "_nudge_pending", "_nudge_pending_write", "_serve_queue_release"},
                 consts={"_KEEP"}, _read_json=read_json, _keeper_serve_session=keeper_session,
                 _serve_get=serve_get, _serve_post=serve_post, _NUDGE_PENDING_PATH=pend,
                 _audit_line=au.line, _hold_note=au.note, _hold_clear=au.clear)
    infl = {"sid": "cs-1", "message_ids": ["m1"], "items": [{"id": "r4229", "text": "x"}], "at": time.time() - 120}
    pend.write_text(json.dumps({"pings": [], "last_sent": 5, "inflight": infl}))
    assert run(ns["_nudge_drain"]({}, None, pend)) is True                 # 2 min in: waits, visibly
    assert any(k.startswith("hold:comms-nudge-inflight") for k in au.holds)
    infl["at"] = time.time() - kg.WAIT_MAX_S - 1
    pend.write_text(json.dumps({"pings": [], "last_sent": 5, "inflight": infl}))
    assert run(ns["_nudge_drain"]({}, None, pend)) is False                # bound passed: re-pended
    doc = json.loads(pend.read_text())
    assert doc.get("inflight") is None and [p["id"] for p in doc["pings"]] == ["r4229"] and doc["last_sent"] == 0
    assert any(b.get("action") == "remove" for b in posts)
    req = [d for a, d in au.lines if a == "comms-nudge-requeue"]
    assert req and "waited" in req[0] and "bound" in req[0]
    assert any(k.startswith("count:comms-nudge-requeue") for k in au.holds)
    assert "hold:comms-nudge-inflight@host" in au.cleared


# ── 5 + 6: the skip is on the strip, never silent ─────────────────────────────
def _nudge_ns(tmp, au, ts_down=False, serve_ok=True):
    async def ts_call(app, path, body=None, timeout=8):
        if ts_down:
            raise ConnectionError("toolserver :7004 refused")
        return [{"id": "r1", "status": "open"}, {"id": "r2", "status": "open"}]

    async def nudge_serve(app, line, base=None):
        if serve_ok:
            app["_rt"]["nudge_last_submit"] = {"sid": "cs-1", "message_ids": ["m1"], "queued": False}
            return True, "serve:keeper cs-1 (sent)"
        return False, "serve roster HTTP 503"

    async def drain(app, base=None, path=None):
        return False

    def read_json(p, default):
        try:
            return json.loads(Path(p).read_text())
        except (OSError, ValueError):
            return default
    pend = tmp / "pending.json"
    pend.write_text(json.dumps({"pings": [{"id": "r1", "text": "[ping] a"}, {"id": "r2", "text": "[ping] b"}],
                                "last_sent": 0}))
    async def surface_now(vm=""):                    # 1.0.147: this test models the serve-surface locus
        return "serve", {"ok": True}

    ns = extract({"_nudge_frontier", "_nudge_pending", "_nudge_pending_write", "_is_channel_ping", "_no_tmux"},
                 consts={"_KEEP", "_CHANNEL_REF_RE"}, _ts_call=ts_call, _nudge_serve=nudge_serve,
                 _nudge_tmux=nudge_serve, _nudge_drain=drain, _read_json=read_json, _NUDGE_PENDING_PATH=pend,
                 NUDGE_MIN_INTERVAL=0, _audit_line=au.line, _hold_note=au.note, _hold_clear=au.clear,
                 KEEPER_SURFACE="serve", _keeper_surface_now=surface_now)
    return ns, pend


def test_board_status_unknown_is_audited_and_counted_not_silent(tmp_path):
    au = Audit()
    ns, pend = _nudge_ns(tmp_path, au, ts_down=True)
    app = {"_rt": {}}
    run(ns["_nudge_frontier"](app, "hugpy"))
    skips = [d for a, d in au.lines if a == "comms-nudge-skip"]
    assert skips and "toolserver unreachable" in skips[0] and "2 ping(s) pending" in skips[0]
    row = au.holds["skip:comms-nudge@hugpy"]
    assert row["count"] == 2 and row["source"] == SH.SOURCE_SKIP and "refused" in row["detail"]
    run(ns["_nudge_frontier"](app, "hugpy"))                                  # same reason: audited once
    assert len([1 for a, _d in au.lines if a == "comms-nudge-skip"]) == 1
    assert len(json.loads(pend.read_text())["pings"]) == 2                     # nothing dropped


def test_skip_reason_and_pending_count_reach_the_strip_and_clear_on_send(tmp_path):
    au = Audit()
    ns, pend = _nudge_ns(tmp_path, au, serve_ok=False)
    app = {"_rt": {}}
    run(ns["_nudge_frontier"](app, "keeper"))
    row = au.holds["skip:comms-nudge@keeper"]
    assert row["count"] == 2 and "2 pending" in row["detail"] and "HTTP 503" in row["detail"]
    ns2, _p = _nudge_ns(tmp_path, au, serve_ok=True)
    run(ns2["_nudge_frontier"](app, "keeper"))
    assert "skip:comms-nudge@keeper" in au.cleared and [a for a, _d in au.lines if a == "comms-nudge"]


# ── 1: the digest queues behind a busy keeper (see test_locus_serve_144 for the full loop) ──
def test_decide_has_no_defer_state():
    s = {"last_delivered": T0 - 9999, "last_sig": "old"}
    assert TD.decide(s, 3, T0, 1800, busy=True, sig="new") == "send"
    assert TD.decide(s, 3, T0, 1800, busy=False, sig="new") == "send"
    assert TD.decide(s, 3, T0, 1800, busy=None, sig="new") == "unreachable"
    assert "defer" not in TD.decide.__doc__.split("1.0.146")[0].replace("there is no", "")
    assert "record(\"deferred\"" not in SRC.split("async def _digest_once")[1].split("async def _digest_loop")[0]


# ── 2 + 3: no attempt cap; the mitigator switch is visible ────────────────────
def test_attempt_cap_is_gone_and_the_delivery_count_rides_the_comment():
    body = SRC.split("async def _todo_reminder_cycle")[1].split("async def _todo_reminder_loop")[0]
    assert "n >= _REMIND_CAP" not in SRC and '"reason": "attempt cap"' not in SRC and "_REMIND_CAP = " not in SRC
    assert "withholding reminders: {max(n, nv)}" not in SRC
    assert "delivery #" in body and "no cap" in body and "_REMIND_MIN_GAP" in body


def test_switches_payload_and_strip_row_when_the_mitigator_is_off():
    au = Audit()
    ns = extract({"_station_switches", "_switch_strip_rows"}, REMIND_MITIGATOR=False, FV_TODO_REMIND=True,
                 DIGEST_ON=True, _HANDOFF_REQUEUE_S=21600, _REMIND_MIN_GAP=600, PROMPT_PENDING_TICK=60,
                 _hold_note=au.note, _hold_clear=au.clear)
    sw = ns["_station_switches"]()
    assert sw["remind_mitigator"] == {"on": False, "env": "STATION_REMIND_MITIGATOR", "default": "0",
                                      "effect": sw["remind_mitigator"]["effect"]}
    assert sw["todo_digest"]["on"] and sw["handoff_requeue_s"] == 21600 and sw["nudge_wait_max_s"] == kg.WAIT_MAX_S
    ns["_switch_strip_rows"]()
    assert "switch:remind_mitigator" in au.holds and au.holds["switch:remind_mitigator"]["source"] == SH.SOURCE_SWITCH
    assert "STATION_REMIND_MITIGATOR=0" in au.holds["switch:remind_mitigator"]["detail"]
    assert "switch:todo_digest" in au.cleared and "switch:todo_digest" not in au.holds
    assert '"switches": switches' in SRC.split("async def api_about")[1].split("def _mct_sidecar_dirs")[0]
    assert 'snap["holds"] = _hold_rows()' in SRC and 'snap["config"]["switches"]' in SRC


# ── 7: the hand-off dedupe age floor ──────────────────────────────────────────
def test_handoff_dedupe_requeues_the_same_open_set_after_the_age_floor():
    ns = extract({"_dispatch_is_dup"}, consts={"_HANDOFF_REQUEUE_S"})
    prev = {"sig": "abc", "ts": T0}
    assert ns["_dispatch_is_dup"](prev, "abc", T0 + 3600) == (True, ns["_dispatch_is_dup"](prev, "abc", T0 + 3600)[1])
    assert ns["_dispatch_is_dup"](prev, "abc", T0 + 3600)[1].startswith("same open set queued 60 min ago")
    dup, why = ns["_dispatch_is_dup"](prev, "abc", T0 + 6 * 3600)
    assert not dup and "age floor" in why
    assert ns["_dispatch_is_dup"](prev, "zzz", T0 + 1) == (False, "new open set")
    assert ns["_dispatch_is_dup"](None, "abc", T0) == (False, "new open set")
    assert ns["_HANDOFF_REQUEUE_S"] == 6 * 3600
    flush = SRC.split("async def _flush_dispatch_batch")[1].split("async def _todo_triage")[0]
    assert "_dispatch_is_dup(prev, sig, now_ts)" in flush and "handoff-requeue" in flush


# ── 8: a failed serve prompt is re-pended, retried, delivered ─────────────────
def test_repend_worthy_tells_a_serve_outage_from_a_bad_prompt():
    assert PS.repend_worthy(PS.result("failed", "serve", "serve unreachable: refused"))
    assert PS.repend_worthy(PS.result("failed", "serve", "no abstract-claude serve for locus hugpy"))
    assert PS.repend_worthy(PS.result("failed", "serve", "no serve session to address: no live keeper"))
    assert not PS.repend_worthy(PS.result("failed", "serve", "empty prompt"))
    assert not PS.repend_worthy(PS.result("failed", "serve", "prompt over 200000 chars"))
    assert not PS.repend_worthy(PS.result("failed", "seat", "no live seat"))
    assert PS.result("pending", "serve", "x")["ok"] and not PS.result("failed", "serve", "x")["ok"]


def _pending_ns(tmp, au, serve_ok):
    calls = []

    async def surface_now(vm):
        return "serve", {"ok": serve_ok, "url": "http://127.0.0.1:1", "error": "" if serve_ok else "serve not answering"}

    def caller(app, base):
        async def call(method, path, body=None):
            calls.append((method, path, body))
            if path.startswith("/api/console/queue?id="):
                return 200, {"session_id": "cs-k", "items": [], "busy": False}
            if path == "/api/console/chat":
                return 200, {"message_ids": ["m9"], "session_id": "cs-k", "queued": False}
            return 404, {}
        return call

    async def head(call):
        return "cs-k", "roster"

    def read_json(p, default):
        try:
            return json.loads(Path(p).read_text())
        except (OSError, ValueError):
            return default

    def write_json(p, doc):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(json.dumps(doc))
    ns = extract({"_prompt_pending_path", "_prompt_pending", "_prompt_pending_write", "_prompt_repend",
                  "_prompt_pending_once", "_prompt_pending_vms"},
                 consts={"PROMPT_PENDING_TICK", "PROMPT_PENDING_MAX_FILE_B"},
                 secrets=__import__("secrets"), FV_STATE_HOME=tmp, _is_host_vm=lambda v: not v,
                 _central_locus=lambda v: v, _locus_state_dir=lambda loc: tmp / "loci" / loc,
                 _read_json=read_json, _write_json_atomic=write_json, _keeper_surface_now=surface_now,
                 KEEPER_SURFACE="serve", _serve_caller=caller, _serve_head_of=head,
                 _audit_line=au.line, _hold_note=au.note, _hold_clear=au.clear)
    return ns, calls


def test_repended_prompt_is_retried_until_a_serve_session_takes_it(tmp_path):
    au = Audit()
    ns, calls = _pending_ns(tmp_path, au, serve_ok=False)
    ent = ns["_prompt_repend"]("hugpy", "hugpy", "do the thing", [], "", "auto", "serve not answering: :9125")
    assert ent["id"].startswith("pp-") and ent["attempts"] == 0
    assert (tmp_path / "loci" / "hugpy" / "prompt-pending.json").is_file()
    assert au.holds["pending:prompt@hugpy"]["count"] == 1 and ("prompt-repend", ) and \
        [a for a, _d in au.lines if a == "prompt-repend"]
    res = run(ns["_prompt_pending_once"]({}, "hugpy"))                     # serve still down: stays pending
    assert res == [(ent["id"], "pending", "serve not answering")] and ns["_prompt_pending"]("hugpy")[0]["attempts"] == 1
    ns2, calls2 = _pending_ns(tmp_path, au, serve_ok=True)
    res = run(ns2["_prompt_pending_once"]({}, "hugpy"))                    # serve back: delivered, file empty
    assert res == [(ent["id"], "delivered", "")] and ns2["_prompt_pending"]("hugpy") == []
    assert ("POST", "/api/console/chat", {"session_id": "cs-k", "prompt": "do the thing"}) in calls2
    assert "pending:prompt@hugpy" in au.cleared and [a for a, _d in au.lines if a == "prompt-repend-delivered"]
    assert ns2["_prompt_pending_vms"]() == ["", "hugpy"]


# ── 9: bug-report inflight drain deadline ─────────────────────────────────────
class _BugIO:
    def __init__(self, busy=True):
        self.busy, self.sent, self.cancelled, self.notes, self.n = busy, [], [], {}, 0

    async def board_add(self, e, text, note):
        self.n += 1
        self.notes["f%d" % self.n] = note
        return "f%d" % self.n

    async def board_update(self, e, note):
        self.notes[e["board_id"]] = note

    async def board_done(self, e, note):
        pass

    async def deliver(self, text):
        self.sent.append(text)
        return PS.result("queued", "serve", "", session_id="w1", message_ids=["m%d" % len(self.sent)])

    async def inflight_busy(self, w):
        return self.busy

    async def inflight_cancel(self, w):
        self.cancelled.append(w)


def test_bug_report_inflight_is_requeued_after_the_drain_deadline():
    clock = {"t": T0}
    box = bug_route.Outbox(now=lambda: clock["t"], min_interval=0, inflight_max=900)
    io = _BugIO(busy=True)
    box.offer("a", kind="finding", title="t1", body="b", severity="high", source="x")
    st = run(box.run_pass("", io))
    assert st["state"] == "queued" and len(io.sent) == 1 and box.loci[""]["inflight"]["message_ids"] == ["m1"]
    box.offer("b", kind="finding", title="t2", body="b", severity="high", source="x")
    clock["t"] = T0 + 600
    st = run(box.run_pass("", io))
    assert st["state"] == "deferred" and "10 min of at most 15" in st["reason"] and len(io.sent) == 1
    clock["t"] = T0 + 901
    st = run(box.run_pass("", io))                                          # deadline: cancel + re-send
    assert st["requeued"] and "requeue #1" in st["requeued"] and io.cancelled[0]["message_ids"] == ["m1"]
    assert len(io.sent) == 2 and st["state"] == "queued" and box.loci[""]["inflight"]["message_ids"] == ["m2"]
    assert bug_route.INFLIGHT_MAX_S == 900 and "inflight_cancel" in SRC and "bug-report-requeue" in SRC


def test_bug_report_never_ends_in_failed():
    class Down(_BugIO):
        async def deliver(self, text):
            return PS.result("failed", "serve", "no abstract-claude serve for locus x")
    box = bug_route.Outbox(now=lambda: T0, min_interval=0)
    box.offer("a", kind="finding", title="t1", body="b", severity="high", source="x")
    st = run(box.run_pass("", Down()))
    assert st["state"] == "pending" and box.items["a"]["state"] == "pending" and box.items["a"]["pending"]
    assert "failed" not in json.dumps({k: v.get("state") for k, v in box.items.items()})


# ── 10: toggles carry by / at / expires ───────────────────────────────────────
def test_frontier_keeper_toggle_is_owned_and_expiring(tmp_path):
    ns = extract({"_frontier_doc", "_frontier_enabled", "_toggle_expired"}, FV_STATE_HOME=tmp_path)
    (tmp_path / "frontier-keeper.json").write_text(json.dumps({"enabled": False, "by": "op", "at": T0, "expires": T0 + 600}))
    d = ns["_frontier_doc"](now=T0 + 10)
    assert d == {"enabled": False, "by": "op", "at": T0, "expires": T0 + 600, "expired": False}
    d = ns["_frontier_doc"](now=T0 + 601)
    assert d["enabled"] and d["expired"] and d["by"] == "op"                # the disable expired: enabled again
    (tmp_path / "frontier-keeper.json").write_text(json.dumps({"enabled": False}))     # legacy file: no expiry
    assert not ns["_frontier_enabled"]()
    body = SRC.split("async def term_frontier")[1].split("def _b_enabled")[0]
    assert '"by": _toggle_owner(request)' in body and '"expires"' in body and "frontier-toggle" in body


def test_delegate_toggle_records_meta_and_expires():
    ns = extract({"_delegate_write_doc", "_frontier_delegate_doc", "_frontier_delegate_meta", "_toggle_expired"},
                 consts={"DELEGATE_BACKENDS"}, _cfg_json=lambda cfg, k: cfg.get(k), _read_json=lambda p, d: d,
                 FRONTIER_DELEGATE_FLAG=Path("/nonexistent"))
    doc = ns["_delegate_write_doc"]({"mct": True}, "serve", True, "op", int(T0 + 300))
    assert doc["serve"] and doc["mct"] and doc["meta"]["serve"]["by"] == "op" and doc["meta"]["serve"]["expires"] == int(T0 + 300)
    assert ns["_frontier_delegate_doc"]({"delegate": doc}, now=T0 + 10)["serve"] is True
    assert ns["_frontier_delegate_doc"]({"delegate": doc}, now=T0 + 301)["serve"] is False     # expired: off
    assert ns["_frontier_delegate_doc"]({"delegate": doc}, now=T0 + 301)["mct"] is True        # no expiry: stays
    assert ns["_frontier_delegate_meta"]({"delegate": doc})["serve"]["by"] == "op"
    off = ns["_delegate_write_doc"](doc, "serve", False, "op2", int(T0 + 999))
    assert off["serve"] is False and off["meta"]["serve"]["expires"] == 0 and off["meta"]["serve"]["by"] == "op2"
    assert DV._delegate_doc({"delegate": json.dumps(doc)})["serve"] is True                   # directives.py reads the bools


# ── 11: every locus has a keeper that knows its locus ─────────────────────────
def _local_runner(home, state):
    env = dict(os.environ, HOME=str(home), HUGPY_STATION_STATE=str(state))
    env.pop("AC_ROOT", None)                 # NEVER this process's serve root (the script ignores it too)

    async def run_(argv, stdin=None, timeout=30):
        return await LX.default_runner(argv, stdin, timeout, env=env)
    return run_


def test_standing_config_is_merged_into_the_locus_config_json_idempotently(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    st = tmp_path / "state"; (st / "abstract-claude").mkdir(parents=True)
    (st / "abstract-claude" / "config.json").write_text(json.dumps(
        {"default_model": "x", "permission_mode_by_label": {"keeper": "default"}}))
    add = {"standing_permission_mode": "bypassPermissions", "standing_cwd": "~",
           "permission_mode_by_label": {"keeper": "bypassPermissions", "chat": "bypassPermissions", "worker": "bypassPermissions"}}
    t = LX.Target(LX.KIND_SELF, "", "hugpy")
    d = run(LX.standing_config(t, add, runner=_local_runner(home, st)))
    cfg = json.loads((st / "abstract-claude" / "config.json").read_text())
    assert d["ok"] and sorted(d["added"]) == ["permission_mode_by_label.chat", "permission_mode_by_label.worker",
                                               "standing_cwd", "standing_permission_mode"]
    assert cfg["permission_mode_by_label"] == {"keeper": "default", "chat": "bypassPermissions", "worker": "bypassPermissions"}
    assert cfg["standing_cwd"] == str(home) and cfg["default_model"] == "x"       # operator values kept, ~ = locus HOME
    assert d["home"] == str(home) and isinstance(d["units"], list) and "trees" in d
    d2 = run(LX.standing_config(t, add, runner=_local_runner(home, st)))
    assert d2["added"] == [] and json.loads((st / "abstract-claude" / "config.json").read_text()) == cfg   # idempotent
    fresh = tmp_path / "fresh"; fresh.mkdir()
    d3 = run(LX.standing_config(t, add, runner=_local_runner(home, fresh)))
    assert d3["added"] and json.loads((fresh / "abstract-claude" / "config.json").read_text())["standing_permission_mode"] == "bypassPermissions"


def test_nuances_record_and_keeper_directive_block():
    ns = extract({"_nuances_from"}, consts={"STANDING_ROLES", "STANDING_PERMISSION_MODE", "STANDING_ROLE"},
                 _locus_home=lambda u: "/home/" + u)
    assert ns["_standing_config_keys"]() if "_standing_config_keys" in ns else True
    t = LX.Target(LX.KIND_SSH, "hugpy", "hugpy", ssh_argv=["ssh", "hugpy@h"])
    facts = {"home": "/home/hugpy", "user": "hugpy", "state": "/home/hugpy/hugpy-station", "gate": "",
             "units": ["abstract-claude-serve@station.service"], "trees": ["/srv/hugpy/src"], "sudo_n": False}
    ptr = {"host": "192.168.1.100", "user": "hugpy", "goal": "hugpy locus.\ntrees: /srv/hugpy/src is canonical"}
    nu = ns["_nuances_from"](facts, ptr, {"instance": "station", "port": 9125}, "hugpy", t, "station", 9125)
    assert nu["home"] == "/home/hugpy" and nu["kind"] == "ssh" and nu["serve"]["port"] == 9125
    assert nu["serve"]["permission_mode"] == "bypassPermissions" and nu["tree_rules"] == ["trees: /srv/hugpy/src is canonical"]
    assert nu["registry"] == "toolserver loci/pointers"
    block = DV.nuances_block(nu)
    assert "NOT installed" in block and "`/home/hugpy`" in block and "abstract-claude-serve@station.service" in block
    assert "/srv/hugpy/src" in block and "bypassPermissions" in block and "192.168.1.100" in block
    # the keeper directive carries the block (template part), rendered from station.json
    snap = {"state": "/home/hugpy/hugpy-station", "home": "/home/hugpy", "user": "hugpy", "host": "hs", "remote": True,
            "files": {"station": json.dumps({"locus": "hugpy", "nuances": nu})}}
    text = DV.compose("keeper", snap)
    assert "## LOCUS NUANCES — locus `hugpy`" in text and "/srv/hugpy/src" in text
    assert "nuances-keeper" in DV.PARTS["keeper"] and "nuances-keeper" not in DV.PARTS["worker"]
    bare = DV.compose("keeper", {"files": {}})
    assert "no nuances recorded yet" in bare                                 # never a blank, never invented
    prov = SRC.split("async def api_locus_serve_provision")[1].split("async def _ac_serve_cache_doc")[0]
    assert "_locus_standing_apply(request.app, vm, t" in prov and "api_locus_serve_standing" in SRC
    assert '"/api/locus/serve/standing"' in SRC


def test_locus_standing_apply_writes_station_json_and_renders(tmp_path):
    seen = {}

    async def standing(t, add, runner=None, timeout=30):
        seen["add"] = add
        return {"ok": True, "path": "/h/abstract-claude/config.json", "added": ["standing_cwd"], "present": ["x"],
                "home": "/h", "user": "u", "state": "/h/st", "gate": "/usr/bin/hugpy-gate", "units": [], "trees": [],
                "sudo_n": True}

    async def ts_call(app, path, body=None, timeout=8):
        return {"ssh_hosts": {"hugpy": {"host": "10.0.0.2", "user": "u", "goal": "g"}}}

    async def seat_cfg(t):
        return {"files": {"station": json.dumps({"locus": "hugpy", "port": 8898})}}

    async def write_cfg(t, key, text):
        seen[key] = json.loads(text)
        return "/h/st/directives/station.json"

    async def remote_render(t):
        seen["rendered"] = True
        return True, "ok"
    fake_lx = type("LXf", (), {"standing_config": staticmethod(standing), "write_cfg": staticmethod(write_cfg),
                               "LocusError": LX.LocusError})
    ns = extract({"_locus_standing_apply", "_nuances_from", "_standing_config_keys"},
                 consts={"STANDING_ROLES", "STANDING_PERMISSION_MODE"}, LX=fake_lx, _ts_call=ts_call,
                 _seat_cfg=seat_cfg, _cfg_json=lambda cfg, k: json.loads(cfg["files"][k]),
                 _serve_managed=lambda: {"hugpy": {"instance": "station", "port": 9125}}, _remote_render=remote_render,
                 _locus_home=lambda u: "/home/" + u, _render_session_directives=lambda: [], PORT=8898,
                 _station_locus=lambda: "keeper")
    t = LX.Target(LX.KIND_SSH, "hugpy", "hugpy", ssh_argv=["ssh", "hugpy@h"])
    res = run(ns["_locus_standing_apply"]({}, "hugpy", t))
    assert seen["add"]["permission_mode_by_label"] == {"keeper": "bypassPermissions", "chat": "bypassPermissions",
                                                        "worker": "bypassPermissions"}
    assert seen["add"]["standing_cwd"] == "~" and seen["add"]["standing_permission_mode"] == "bypassPermissions"
    st = seen["station"]
    assert st["locus"] == "hugpy" and st["port"] == 8898                     # existing keys kept
    assert st["nuances"]["gate"] == "/usr/bin/hugpy-gate" and st["nuances"]["hostname"] == "10.0.0.2"
    assert st["nuances"]["serve"]["port"] == 9125 and res["rendered"] and seen["rendered"] and res["config"]["added"] == ["standing_cwd"]


def test_host_render_keeps_nuances_in_station_json():
    body = SRC.split("def _render_session_directives")[1].split("async def api_frontier_handoff")[0]
    assert "keeps `nuances`" in body and "dict(cur if isinstance(cur, dict) else {}, port=PORT" in body


# ── the strip renders holds ───────────────────────────────────────────────────
def test_strip_js_renders_holds():
    js = (HERE / "static" / "fleetview-term.js").read_text(encoding="utf-8")
    assert "j.holds" in js and "station:switch" in js and "station:skip" in js and ".concat(holds)" in js


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
