"""2026-10-01: bug reports go ONLY to the locus's WORKER serve session, and no
report is ever silently dropped (board t4178 [F2.5]).

bug_route.Outbox is pure; the server.py wiring (_BugIO, _report_findings,
_report_loops) is AST-extracted and driven with fakes — nothing reaches the
network, the board or serve.
Run:  python3 -m pytest resources/backend/test_bug_route.py
"""
import ast
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bug_route  # noqa: E402
import issue_gate  # noqa: E402
import keeper_notify  # noqa: E402
import keeper_nudge  # noqa: E402
import loop_detector  # noqa: E402
import prompt_send  # noqa: E402

TREE = ast.parse((HERE / "server.py").read_text(encoding="utf-8"))


def extract(names, **ns):
    nodes = [n for n in TREE.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names]
    base = dict(re=re, json=json, time=time, os=os, asyncio=asyncio, logging=logging,
                _ig=issue_gate, _kn=keeper_notify, _knudge=keeper_nudge, _bugr=bug_route, PS=prompt_send,
                _loopdet=loop_detector, KEEPER_TARGET="keeper", _rlog=logging.getLogger("t"),
                _bslog=logging.getLogger("t"), _audit_line=lambda *a: None, _notify_origin=lambda: "keeper",
                LOCUS_UNCONFIGURED="locus not configured")
    base.update(ns)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "server.py", "exec"), base)
    missing = set(names) - set(base)
    assert not missing, missing
    return base


class Clock:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeIO:
    """The worker serve session + the board, scripted."""

    def __init__(self, ok=True, busy=False):
        self.ok, self.busy = ok, busy
        self.sent, self.boards, self.notes, self.done = [], {}, {}, []
        self.n = 0

    async def board_add(self, e, text, note):
        self.n += 1
        bid = "f%d" % (100 + self.n)
        self.boards[bid] = {"text": text, "note": note, "type": bug_route.BOARD_TYPE}
        return bid

    async def board_update(self, e, note):
        self.notes[e["board_id"]] = note

    async def board_done(self, e, note):
        self.done.append(e["board_id"])

    async def deliver(self, text):
        self.sent.append(text)
        if not self.ok:
            return prompt_send.result("failed", "serve", "no live worker session in serve")
        return prompt_send.result("queued" if self.busy else "delivered", "serve", "", session_id="w1",
                                  message_ids=["m%d" % len(self.sent)])

    async def inflight_busy(self, w):
        return self.busy


def _box(clock, **kw):
    return bug_route.Outbox(now=clock, patterns=bug_route.self_patterns({}), **kw)


def _offer(box, key, count=3, source="hugpy-worker.service", sev="high", **kw):
    return box.offer(key, vm="", locus="keeper", kind="finding:traceback", title="[finding] %s" % key,
                     body="Traceback ... %s" % key, severity=sev, count=count, source=source, **kw)


# ── pure outbox ────────────────────────────────────────────────────────────────────────
def test_reports_go_to_the_worker_as_one_message_and_land_on_the_board_as_findings():
    clock, io = Clock(), FakeIO()
    box = _box(clock)
    _offer(box, "a")
    _offer(box, "b", sev="medium")
    st = asyncio.run(box.run_pass("", io, "keeper"))
    assert st["sent"] == 2 and len(io.sent) == 1                      # coalesced: ONE message
    assert "2 bug reports for locus keeper" in io.sent[0] and "worker session" in io.sent[0]
    assert all(b["type"] == "finding" and b["text"].startswith("[bug-report]") for b in io.boards.values())
    assert all("delivery: delivered → worker session w1" in n for n in io.notes.values())
    assert box.pending() == []


def test_worker_down_is_NOT_delivered_on_the_board_and_retried():
    clock, io = Clock(), FakeIO(ok=False)
    box = _box(clock)
    _offer(box, "a")
    asyncio.run(box.run_pass("", io))
    note = io.notes["f101"]
    assert "NOT delivered yet — no live worker session in serve" in note and box.pending()   # re-pended, not dropped
    assert box.items["a"]["state"] == "pending"            # 1.0.146: never a terminal "failed"
    io.ok = True
    clock.t += 60
    asyncio.run(box.run_pass("", io))
    assert len(io.sent) == 2 and box.pending() == [] and "delivered" in io.notes["f101"]


def test_flood_guard_coalesces_behind_the_inflight_report_never_drops():
    clock, io = Clock(), FakeIO(busy=True)
    box = _box(clock)
    _offer(box, "a", count=3)
    asyncio.run(box.run_pass("", io))                                 # queued in the worker: in flight
    _offer(box, "a", count=9)                                         # the same loop keeps firing
    _offer(box, "b")
    clock.t += 600
    st = asyncio.run(box.run_pass("", io))
    assert st["state"] == "deferred" and len(io.sent) == 1            # one hand-off at a time
    assert "coalescing behind the previous report" in io.notes["f102"]
    io.busy = False                                                   # drained
    clock.t += 60
    asyncio.run(box.run_pass("", io))
    assert len(io.sent) == 2 and "×9 total, +6 since the last report" in io.sent[1]


def test_rate_limit_defers_with_a_visible_reason():
    clock, io = Clock(), FakeIO()
    box = _box(clock, min_interval=300)
    _offer(box, "a")
    asyncio.run(box.run_pass("", io))
    _offer(box, "b")
    clock.t += 30
    st = asyncio.run(box.run_pass("", io))
    assert st["state"] == "deferred" and "rate limit" in io.notes["f102"] and box.pending()
    clock.t += 300
    asyncio.run(box.run_pass("", io))
    assert len(io.sent) == 2 and not box.pending()


def test_self_origin_is_coalesced_not_dropped():
    clock, io = Clock(), FakeIO()
    box = _box(clock, self_interval=3600)
    _offer(box, "s", source="ac-serve-station.service", sev="medium")
    asyncio.run(box.run_pass("", io))
    assert len(io.sent) == 1 and "self-origin" in io.boards["f101"]["note"]   # first one goes out
    _offer(box, "s", count=40, source="ac-serve-station.service", sev="medium", fresh=True)
    clock.t += 300
    asyncio.run(box.run_pass("", io))
    assert len(io.sent) == 1 and "self-origin: coalesced" in io.notes["f101"]  # held, shown on the board
    clock.t += 3600
    asyncio.run(box.run_pass("", io))
    assert len(io.sent) == 2 and "×40 total" in io.sent[1]


def test_redelivery_only_on_news_and_quiet_findings_resolve_after_delivery():
    clock, io = Clock(), FakeIO()
    box = _box(clock, min_interval=0)
    _offer(box, "a")
    asyncio.run(box.run_pass("", io))
    _offer(box, "a", fresh=False)                                     # re-offer, nothing new
    assert box.pending() == []
    box.clear("finding:x", "n/a")                                     # unknown key: ignored
    box.clear("a", "quiet since … — auto-resolved by the station notifier")
    asyncio.run(box.run_pass("", io))
    assert io.done == ["f101"] and "a" not in box.items


def test_legacy_bug_pings_are_recognised():
    assert bug_route.is_bug_ping({"text": "[finding] rate_limit_429 x", "note": "[ping] from keeper"})
    assert bug_route.is_bug_ping({"text": "[hs] [loop] job: v1 ×3", "note": "[ping] from hs"})
    assert bug_route.is_bug_ping({"text": "[aeb] 🐞 finding [high] x", "note": "[ping][msg] from aeb (ref finding:k)"})
    assert bug_route.ping_origin({"text": "[aeb] 🐞 finding [high] x"}) == "aeb"
    assert not bug_route.is_bug_ping({"text": "please review t12", "note": "[ping] from aeb"})
    assert not bug_route.is_bug_ping({"text": "hello", "note": "[ping] from op-keeper (ref ch_7f3a)"})


# ── server wiring ──────────────────────────────────────────────────────────────────────
class Serve:
    """A locus's serve: roster keeper=k1 / worker=w1, console sessions."""

    def __init__(self, worker=True):
        self.calls, self.worker = [], worker

    async def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/api/session/roster":
            roles = [{"role": "keeper", "session_id": "k1"}, {"role": "chat", "session_id": "c1"}]
            if self.worker:
                roles.append({"role": "worker", "session_id": "w1"})
            return 200, {"roles": roles}
        if path == "/api/session/rollover":
            return 200, {"archived": {}}
        if path.startswith("/api/console/queue"):
            return 200, {"session_id": path.split("=")[-1], "items": [], "busy": False}
        if path == "/api/console/chat":
            return 200, {"session_id": body["session_id"], "message_ids": ["m1"], "cursor": 1}
        raise AssertionError(path)


def _io_ns(serve, url="http://127.0.0.1:9100"):
    async def ac_resolve(vm):
        return url, "u", "/ac/", vm

    ns = extract({"_BugIO", "_serve_head_of"}, _ac_resolve=ac_resolve,
                 _serve_caller=lambda app, base: serve, NUDGE_SERVE_ROLE="keeper")
    return ns


def test_bugio_delivers_to_the_worker_session_never_the_keeper():
    serve = Serve()
    io = _io_ns(serve)["_BugIO"]({}, "")
    res = asyncio.run(io.deliver("🐞 1 bug report"))
    assert res["ok"] and res["state"] == "delivered" and res["session_id"] == "w1"
    chats = [b for m, p, b in serve.calls if p == "/api/console/chat"]
    assert chats == [{"session_id": "w1", "prompt": "🐞 1 bug report"}]       # w1, never k1


def test_bugio_without_a_worker_session_says_NOT_delivered():
    res = asyncio.run(_io_ns(Serve(worker=False))["_BugIO"]({}, "").deliver("x"))
    assert not res["ok"] and res["state"] == "failed" and "no live worker session" in res["reason"]
    res = asyncio.run(_io_ns(Serve(), url="")["_BugIO"]({}, "hs").deliver("x"))
    assert not res["ok"] and "no abstract-claude serve for locus hs" in res["reason"]


class FakeTS:
    def __init__(self, down=False, state="benign"):
        self.down, self.state, self.calls = down, state, []

    async def __call__(self, path, body):
        self.calls.append(path)
        if self.down:
            raise ConnectionRefusedError("toolserver down")
        return {"fp": "finding:traceback:0123456789abcdef", "page": False, "state": self.state,
                "annotation": "%s ×4 since … — coalesced" % self.state}


def _findings_ns(box, ts):
    return extract({"_report_findings", "_finding_observe_spec", "_bug_observe"},
                   _bug_outbox=lambda: box, _bug_locus=lambda vm: vm or "keeper",
                   _issue_gate=lambda app: issue_gate.Gate(ts))


def _frow(key, sev="high", source="hugpy-worker.service"):
    return {"key": key, "sigkey": "s" + key, "kind": "traceback", "signature": "KeyError " + key, "count": 3,
            "source": source, "locus": "keeper", "severity": sev, "sample_lines": ["Traceback: boom"],
            "first_seen": 1, "last_seen": 2, "identity": "keeper · %s: KeyError" % source}


def test_findings_still_reported_when_the_gate_and_B_are_down():
    clock = Clock()
    box = _box(clock)
    book = keeper_notify.NotifyBook()
    a, seat = _frow("a"), _frow("c", source="seat:keeper-claude")
    n = asyncio.run(_findings_ns(box, FakeTS(down=True))["_report_findings"]({}, "", book,
                                                                               {"mail": [a, seat], "board": [a]},
                                                                               clock.t))
    assert n == 1 and box.pending()[0]["key"] == "finding:a"           # seat prose: strip only (classification)
    assert a["pending_mail"] is False


def test_inert_finding_still_reported_at_low_priority(monkeypatch):
    monkeypatch.delenv("STATION_NOTIFY_INERT_SILENCES", raising=False)
    clock = Clock()
    box = _box(clock)
    book = keeper_notify.NotifyBook()
    book.sigs["sa"] = {"disposition": "inert", "reason": "client disconnects", "proposals": [], "hist": []}
    a = _frow("a")
    asyncio.run(_findings_ns(box, FakeTS(state="benign"))["_report_findings"]({}, "", book, {"mail": [a]}, clock.t))
    e = box.items["finding:a"]
    assert e["pending"] and e["priority"] == "low"
    assert "inert: client disconnects" in e["annotation"] and "benign ×4" in e["annotation"]
    # the legacy switch restores the silencing (board t4178)
    monkeypatch.setenv("STATION_NOTIFY_INERT_SILENCES", "1")
    assert not book.notify_row(_frow("a"))


def test_decided_loop_is_not_dropped_by_default(monkeypatch):
    monkeypatch.delenv("STATION_NOTIFY_INERT_SILENCES", raising=False)
    book = keeper_notify.NotifyBook()
    row = {"key": "job:1", "identity": "v1 Qwen@ae-worker", "source": "job"}
    book.sigs[keeper_notify.loop_sigkey(row)] = {"disposition": "inert", "reason": "known", "proposals": []}
    ev, quiet = keeper_notify.gate_loops(book, {"new": [row], "mail": [row]})
    assert quiet == [] and ev["new"] == [row] and ev["mail"] == [row]
    monkeypatch.setenv("STATION_NOTIFY_INERT_SILENCES", "1")
    ev, quiet = keeper_notify.gate_loops(book, {"new": [row], "mail": [row]})
    assert quiet == [row] and ev["new"] == []


def test_loops_reported_to_the_worker_outbox():
    clock = Clock()
    box = _box(clock)

    class Det:
        def mark_mailed(self, row, now):
            row["mailed_at"] = now
    row = {"key": "job:1", "identity": "v1 Qwen@ae-worker", "source": "job", "severity": "crit", "count": 4,
           "detail": "no progress", "action": "cancel it", "first_seen": 1, "last_seen": 2,
           "session_id": "cs-keeper1"}
    ns = extract({"_report_loops", "_loop_gate_spec", "_bug_observe"}, _bug_outbox=lambda: box,
                 _bug_locus=lambda vm: "keeper", _issue_gate=lambda app: issue_gate.Gate(FakeTS(down=True)))
    asyncio.run(ns["_report_loops"]({}, Det(), keeper_notify.NotifyBook(), {"new": [row], "mail": [row]},
                                    clock.t, lambda r: "stopped"))
    e = box.items["loop:job:1"]
    assert e["pending"] and e["self"] and e["severity"] == "high"     # cs-* caller: self-origin, coalesced
    assert row["mailed_at"] == clock.t
