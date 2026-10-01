"""1.0.143 ✍ prompt — the composer delivers DIRECTLY and says what happened.

Pure helpers (prompt_send.py) plus the REAL server.py routes (AST-extracted —
importing the whole backend has side effects) driven against a STUB serve
(an aiohttp app speaking /api/console/*, /api/session/attach|roster|rollover)
and a stub tmux. Nothing here touches a live serve, seat or station.
Run:  python3 -m pytest resources/backend/test_prompt_send.py
  or: python3 resources/backend/test_prompt_send.py
"""
import ast
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import keeper_nudge  # noqa: E402
import prompt_send as PS  # noqa: E402

SRC = HERE / "server.py"
FUNCS = {"_serve_caller", "_serve_head_of", "_prompt_store_local", "api_prompt_send", "api_prompt_status",
         "_serve_queue_release", "_prompt_repend", "_prompt_pending", "_prompt_pending_write", "_prompt_pending_path"}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_choose_target_follows_surface_unless_explicit():
    assert PS.choose_target("auto", "serve") == "serve"
    assert PS.choose_target("", "tmux") == "seat"
    assert PS.choose_target("seat", "serve") == "seat"
    assert PS.choose_target("serve", "tmux") == "serve"


def test_attachments_block_lists_paths():
    blk = PS.attachments_block([{"path": "/r/a/x.png", "name": "x.png", "mime": "image/png", "size": 2048}])
    assert blk == "\n\nAttachments:\n- x.png (image/png, 2.0 kB): /r/a/x.png"
    assert PS.attachments_block([]) == ""


def test_seat_input_state():
    assert PS.seat_input_state("hello\n❯ \n  ? for shortcuts") == "empty"
    assert PS.seat_input_state("│ > half typed │") == "dirty"
    assert PS.seat_input_state("✻ Working… (esc to interrupt)\n❯ ") == "busy"
    assert PS.seat_input_state("$ ls\nfoo") == "unknown"
    assert PS.seat_input_state("> earlier prompt\nreply\n\x1b[2m›\x1b[0m ") == "empty"   # SGR stripped, last line wins


def test_seat_parts_brackets_multiline_only():
    assert PS.seat_parts("one line") == ["one line"]
    assert PS.seat_parts("a\nb") == ["\x1b[200~", "a\nb", "\x1b[201~"]
    assert len(PS.seat_parts("x" * 4500)) == 3


def test_classify_status():
    ids = ["m1"]
    assert PS.classify_status(ids, {"items": [{"id": "m1"}], "busy": True}, [])["state"] == "queued"
    st = PS.classify_status(ids, {"items": [{"id": "m1"}], "busy": False}, [])
    assert st["state"] == "stalled" and st["retry"]
    assert PS.classify_status(ids, {"items": [{"id": "m1"}], "busy": False, "paused": True}, [])["state"] == "held"
    assert PS.classify_status(ids, {"items": []}, [{"type": "user", "message_ids": ["m1"]}])["state"] == "running"
    done = PS.classify_status(ids, {"items": []}, [{"type": "done", "rc": 0, "result": "hi", "message_ids": ["m0", "m1"]}])
    assert done == {"state": "done", "reply": "hi"}
    assert PS.classify_status(ids, {"items": []}, [{"type": "done", "rc": 1, "error": "x", "message_ids": ["m1"]}])["state"] == "failed"
    assert PS.classify_status(ids, None, [])["state"] == "gone"


# ── stub tmux ────────────────────────────────────────────────────────────────
class FakeTmux:
    def __init__(self, pane="❯ ", live=True, clears=True):
        self.pane, self.live, self.clears, self.sent = pane, live, clears, []

    async def __call__(self, *a):
        if a[0] == "has-session":
            return (0, "", "") if self.live else (1, "", "can't find session")
        if a[0] == "capture-pane":
            return 0, self.pane, ""
        if a[0] == "send-keys":
            self.sent.append(a[3:])
            if a[-1] == "Enter" and self.clears:
                self.pane = self.pane.replace("❯ x", "❯ ")
            return 0, "", ""
        return 1, "", "?"


async def _nosleep(_s):
    return None


def test_seat_delivered_types_text_then_separate_enter():
    t = FakeTmux()
    r = run(PS.deliver_seat(t, "keeper-claude", "hello\nworld", sleep=_nosleep))
    assert r["ok"] and r["state"] == "delivered"
    assert t.sent[0][-1] == "\x1b[200~" and t.sent[-1] == ("Enter",)   # bracketed text, Enter alone


def test_seat_dirty_input_refuses_unless_forced():
    r = run(PS.deliver_seat(FakeTmux(pane="❯ x"), "keeper-claude", "hi", sleep=_nosleep))
    assert not r["ok"] and r["state"] == "failed" and r["can_force"] and "input line" in r["reason"]
    r = run(PS.deliver_seat(FakeTmux(pane="❯ x"), "keeper-claude", "hi", force=True, sleep=_nosleep))
    assert r["ok"]


def test_seat_busy_is_queued_and_missing_seat_fails_loudly():
    r = run(PS.deliver_seat(FakeTmux(pane="(esc to interrupt)\n❯ "), "keeper-claude", "hi", sleep=_nosleep))
    assert r["state"] == "queued"
    r = run(PS.deliver_seat(FakeTmux(live=False), "keeper-claude", "hi", sleep=_nosleep))
    assert r["state"] == "failed" and "no live seat" in r["reason"]


def test_seat_unconfirmed_when_input_keeps_text():
    t = FakeTmux(pane="❯ ", clears=False)

    async def tm(*a):
        out = await t(*a)
        if a[0] == "send-keys" and a[-1] == "Enter":
            t.pane = "❯ hi"                       # Enter did not submit
        return out
    r = run(PS.deliver_seat(tm, "keeper-claude", "hi", sleep=_nosleep))
    assert r["state"] == "unconfirmed" and r["ok"]


# ── stub serve + the real routes ────────────────────────────────────────────
class StubServe:
    def __init__(self):
        self.sessions = {"cs-head": {"busy": False}, "cs-sel": {"busy": True}}
        self.msgs, self.attached, self.down_chat = [], [], False

    def app(self):
        a = web.Application()
        a.router.add_get("/api/console/queue", self.queue)
        a.router.add_post("/api/console/queue", self.queue_post)
        a.router.add_get("/api/console/events", self.events)
        a.router.add_post("/api/console/chat", self.chat)
        a.router.add_post("/api/session/attach", self.attach)
        a.router.add_get("/api/session/roster", lambda r: web.json_response(
            {"roles": [{"role": "keeper", "session_id": "cs-old"}]}))
        a.router.add_get("/api/session/rollover", lambda r: web.json_response(
            {"archived": {"cs-old": {"successor": "cs-head"}}}))
        return a

    async def queue(self, r):
        sid = r.query.get("id")
        if sid not in self.sessions:
            return web.json_response({"error": "Unknown session"}, status=400)
        items = [{"id": m["id"]} for m in self.msgs if m["sid"] == sid and m["queued"]]
        return web.json_response({"session_id": sid, "busy": self.sessions[sid]["busy"], "paused": False, "items": items})

    async def queue_post(self, r):
        b = await r.json()
        return web.json_response({"ok": True, "action": b.get("action")})

    async def events(self, r):
        sid = r.query.get("id")
        ev = [{"seq": i + 1, "type": "done", "rc": 0, "result": "reply to " + m["text"][:5], "message_ids": [m["id"]]}
              for i, m in enumerate(self.msgs) if m["sid"] == sid and not m["queued"]]
        return web.json_response({"events": [e for e in ev if e["seq"] > int(r.query.get("since") or 0)]})

    async def chat(self, r):
        b = await r.json()
        if self.down_chat:
            return web.json_response({"error": "Session queue is full"}, status=400)
        sid = b["session_id"]
        busy = self.sessions[sid]["busy"]
        mid = "m%d" % (len(self.msgs) + 1)
        self.msgs.append({"id": mid, "sid": sid, "text": b["prompt"], "queued": busy})
        return web.json_response({"session_id": sid, "message_ids": [mid], "cursor": 0, "queued": busy})

    async def attach(self, r):
        b = await r.json()
        p = "/ac-root/attachments/%s/%s" % (b["session_id"], b["name"])
        self.attached.append(b)
        return web.json_response({"ok": True, "path": p, "name": b["name"], "mime": b["mime"], "size": 3})


def _read_json(p, default):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def _write_json(p, doc):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(doc))


def _load(ns_extra):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FUNCS]
    assert {n.name for n in nodes} == FUNCS
    ns = {"asyncio": asyncio, "aiohttp": aiohttp, "web": web, "json": json, "os": os, "re": re, "time": time,
          "Path": Path, "PS": PS, "AC_UPSTREAM": "http://127.0.0.1:1", "NUDGE_SERVE_ROLE": "keeper",
          "KEEPER_SURFACE": "serve", "_knudge": keeper_nudge, "audit": lambda *a, **k: None,
          "_station_id": lambda: "st", "_active_session": lambda: "repl", "_central_locus": lambda vm: vm,
          # 1.0.146: re-pend + holds + release (test-local state dir, strip rows ignored)
          "secrets": __import__("secrets"), "FV_STATE_HOME": Path(tempfile.mkdtemp()), "PROMPT_PENDING_TICK": 60,
          "PROMPT_PENDING_MAX_FILE_B": 2_000_000, "_is_host_vm": lambda v: not v,
          "_locus_state_dir": lambda loc: Path(tempfile.gettempdir()) / "pp-test" / loc,
          "_read_json": _read_json, "_write_json_atomic": _write_json, "_audit_line": lambda *a: None,
          "_hold_note": lambda *a, **k: None, "_hold_clear": lambda *a, **k: None, "_sh": None,
          "_toggle_owner": lambda r: "op"}
    ns.update(ns_extra)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SRC), "exec"), ns)
    return ns


async def _client(ns):
    app = web.Application()
    app.router.add_post("/api/prompt/send", ns["api_prompt_send"])
    app.router.add_get("/api/prompt/status", ns["api_prompt_status"])
    app.router.add_post("/api/prompt/status", ns["api_prompt_status"])

    async def _close(a):
        if a.get("proxy_sess") is not None:
            await a["proxy_sess"].close()
    app.on_cleanup.append(_close)
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


async def _live_frontier():
    return "keeper-claude"


def _serve_ns(serve_url, surface="serve", tmux=None, ws=None):
    async def sidecar_vm(request):
        return request.query.get("vm", "")

    async def surface_now(vm):
        return surface, {"ok": True, "url": serve_url, "path": "/ac/"}

    async def ac_resolve(vm):
        return serve_url, "u", "/ac/", ""
    return _load({"_sidecar_vm": sidecar_vm, "_keeper_surface_now": surface_now, "_ac_resolve": ac_resolve,
                  "_tmux_session_for": lambda s, b: {"codex": "keeper-codex", "claude-code": "keeper-claude"}.get(b, ""),
                  "_live_frontier_session": _live_frontier,
                  "_tmux_on": (lambda vm, *a: tmux(*a)) if tmux else None,
                  "_active_ws": lambda: Path(ws or tempfile.mkdtemp())})


def test_route_serve_delivered_queued_and_reply_status():
    async def go():
        stub = StubServe()
        srv = TestServer(stub.app()); await srv.start_server()
        ns = _serve_ns(str(srv.make_url("")).rstrip("/"))
        c = await _client(ns)
        try:
            # auto + no session -> keeper head (roster cs-old -> rollover head cs-head), idle -> delivered
            r = await (await c.post("/api/prompt/send", json={"text": "hello there", "target": "auto"})).json()
            assert r["ok"] and r["state"] == "delivered" and r["session_id"] == "cs-head" and r["why"] == "rollover head"
            st = await (await c.get("/api/prompt/status?session_id=cs-head&ids=" + r["message_ids"][0])).json()
            assert st["state"] == "done" and st["reply"].startswith("reply to hello") and st["cursor"] == 1
            # a busy selected session -> queued, with the image uploaded through /api/session/attach first
            f = {"name": "clip.png", "type": "image/png", "dataUrl": "data:image/png;base64,iVBO"}
            r = await (await c.post("/api/prompt/send", json={"text": "see", "files": [f], "target": "serve",
                                                              "session_id": "cs-sel"})).json()
            assert r["state"] == "queued" and r["attachments"] == ["/ac-root/attachments/cs-sel/clip.png"]
            assert stub.attached[0]["data_b64"] == "iVBO" and "Attachments:\n- clip.png" in stub.msgs[-1]["text"]
            st = await (await c.get("/api/prompt/status?session_id=cs-sel&ids=" + r["message_ids"][0])).json()
            assert st["state"] == "queued"
            # explicit serve + unknown session: NOT delivered, with the reason (no silent fallback)
            r = await (await c.post("/api/prompt/send", json={"text": "x", "target": "serve", "session_id": "cs-nope"})).json()
            assert not r["ok"] and r["state"] == "failed" and "not a console session" in r["reason"]
            # auto + an unknown selection falls back to the head and says so
            r = await (await c.post("/api/prompt/send", json={"text": "x", "target": "auto", "session_id": "cs-nope"})).json()
            assert r["ok"] and r["session_id"] == "cs-head" and "unknown here" in r["why"]
            # serve refuses (a full queue is transient) -> 1.0.146: re-pended with serve's error, retried
            stub.down_chat = True
            r = await (await c.post("/api/prompt/send", json={"text": "x", "target": "serve", "session_id": "cs-head"})).json()
            assert r["state"] == "pending" and "queue is full" in r["reason"] and r["pending_id"]
            # release forwarding (1.0.146: serve-core 0.1.15 action; "retry" from an older UI is sent as release)
            r = await (await c.post("/api/prompt/status", json={"session_id": "cs-sel", "action": "retry"})).json()
            assert r["ok"] and r["result"]["action"] == "release" and r["action"] == "release"
            r = await (await c.post("/api/prompt/status", json={"session_id": "cs-sel", "action": "release"})).json()
            assert r["ok"] and r["result"]["action"] == "release"
            # empty prompt is a 400 with a reason
            resp = await c.post("/api/prompt/send", json={"text": "  "})
            assert resp.status == 400 and (await resp.json())["reason"] == "empty prompt"
        finally:
            await c.close(); await srv.close()
    run(go())


def test_route_serve_down_is_RE_PENDED_not_failed():
    """1.0.146 (t4260 / d4254): a serve the station cannot reach does not end the
    prompt in "failed" — it is re-pended to the locus's prompt-pending file with
    the reason and retried every tick; the reply says so (state "pending")."""
    async def go():
        ns = _serve_ns("http://127.0.0.1:9")            # nothing listens on :9
        c = await _client(ns)
        try:
            r = await (await c.post("/api/prompt/send", json={"text": "x", "target": "serve", "session_id": "cs-a"})).json()
            assert r["ok"] and r["state"] == "pending" and "unreachable" in r["reason"] and r["pending_id"].startswith("pp-")
            pend = ns["_prompt_pending"]("")
            assert [e["id"] for e in pend] == [r["pending_id"]] and pend[0]["text"] == "x" and pend[0]["session_id"] == "cs-a"
            assert "unreachable" in pend[0]["reason"] and pend[0]["attempts"] == 0
        finally:
            await c.close()
    run(go())


def test_route_seat_stores_host_attachments_and_types_into_the_backend_seat():
    async def go():
        t = FakeTmux()
        ws = tempfile.mkdtemp()
        ns = _serve_ns("http://127.0.0.1:9", surface="tmux", tmux=t, ws=ws)
        c = await _client(ns)
        try:
            f = {"name": "a b.txt", "type": "text/plain", "dataUrl": "data:text/plain;base64,aGk="}
            r = await (await c.post("/api/prompt/send", json={"text": "look", "files": [f], "seat_backend": "codex"})).json()
            assert r["ok"] and r["target"] == "seat" and r["seat"] == "keeper-codex@host"
            typed = "".join(p[-1] for p in t.sent if p[-1] not in ("Enter", "\x1b[200~", "\x1b[201~"))
            m = re.search(r"- a_b\.txt \(text/plain, 2 B\): (\S+)", typed)
            assert typed.startswith("look") and m and Path(m.group(1)).read_text() == "hi"
        finally:
            await c.close()
    run(go())


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); print("ok", k)
