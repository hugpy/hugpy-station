"""1.0.138 — the station is LOCUS-SPECIFIC: a dropdown pick is just a different
`locus` key on the central (toolserver) tables. Every locus kind (ssh host, LXD
guest, registered locus) reads AND writes its own central rows; the host keeper
keeps its file failsafe; nothing here needs a station on the locus.

Exercises the REAL server.py functions (AST-extracted — importing the whole
backend has side effects) against an in-memory fake central.
Run:  python3 -m pytest resources/backend/test_locus_central.py
  or: python3 resources/backend/test_locus_central.py
"""
import ast
import asyncio
import contextlib
import getpass
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import operator_scripts as OPS  # noqa: E402

HERE = Path(__file__).resolve().parent
SRC = HERE / "server.py"

FUNCS = {
    # resolution
    "_is_host_vm", "_canon_locus", "_pick_endpoint_locus", "_host_vm_dispatch", "_locus_aliases", "_lxd_ips", "_endpoint", "_endpoints_of",
    "_self_locus_names", "_central_locus", "_known_locus_key", "_locus_name_for",
    "_ssh_hosts_local", "_ssh_hosts", "_ssh_host", "_sidecar_vm", "_retired", "_sidecar_proxy",
    # central board / canvas / comms
    "_central_todo_path", "_encode_comment", "_split_note_comments", "_central_item",
    "_central_todo_state", "_central_todo_get", "_central_todo_post", "_central_todo_route",
    "_central_canvas_path", "_central_canvas_route", "_central_vm_reroute",
    "_central_messages", "_central_todo_brief", "_central_todo_ping", "_todo_brief_summary",
    "_central_prompt", "_loci_ready", "_operator_scripts_sync", "_operator_scripts_attach",
    # the routes
    "mct_todo_get", "mct_todo_post", "mct_todo_ping", "fleet_message_post", "_mail_row",
}
CONSTS = {"_HOST_VMS", "_LEGACY_LOCUS_ALIASES", "HOST_TOKEN", "CENTRAL_TODO_LIMIT", "_CENTRAL_COMMENT_RE", "_CENTRAL_VM_TAILS", "_PING_FROM_RE",
          "MCT_TODO_STATUSES", "MCT_TODO_TYPES", "MCT_TODO_PRIOS", "TODO_TYPES", "TODO_STATUS",
          "_SSH_NAME_RE", "FLEET_MAIL_MAX", "FLEET_MSG_MAX"}

ME = getpass.getuser()


class FakeCentral:
    """The toolserver tools the station calls, over {locus: [rows]}."""

    def __init__(self):
        self.todos = {}       # locus -> [row]
        self.canvas = {}      # (locus, kind) -> doc
        self.pings = {}       # locus -> [ping row]
        self.calls = []       # (path, body)
        self.down = False
        self.n = 0

    def seed(self, locus, *texts):
        for t in texts:
            self.n += 1
            self.todos.setdefault(locus, []).append(
                {"id": f"r{self.n}", "locus": locus, "text": t, "type": "todo", "status": "open",
                 "note": "", "by": "seed", "created": 1000 + self.n, "updated": 1000 + self.n})

    def find(self, tid):
        for loc, rows in self.todos.items():
            for r in rows:
                if r["id"] == tid:
                    return loc, r
        return None, None

    async def __call__(self, app, path, body=None, timeout=8):
        body = dict(body or {})
        self.calls.append((path, body))
        if self.down:
            raise RuntimeError("toolserver down")
        if path == "todo/list":
            return list(reversed(self.todos.get(body["locus"], [])))[: body.get("limit", 500)]
        if path == "todo/add":
            self.n += 1
            row = {"id": f"r{self.n}", "locus": body["locus"], "text": body["text"],
                   "type": body.get("type") or "todo", "status": "open",
                   "note": body.get("note") or "", "by": body.get("by") or "",
                   "priority": body.get("priority"), "created": int(time.time()),
                   "updated": int(time.time())}
            self.todos.setdefault(body["locus"], []).append(row)
            return row
        if path == "todo/update":
            _loc, r = self.find(body["id"])
            if r is None:
                raise RuntimeError("no todo " + body["id"])
            r.update({k: v for k, v in body.items() if k != "id"})
            return r
        if path == "todo/remove":
            loc, r = self.find(body["id"])
            if r is None:
                raise RuntimeError("no todo " + body["id"])
            self.todos[loc].remove(r)
            return {"removed": body["id"]}
        if path == "canvas/get":
            return self.canvas.get((body["locus"], body["kind"])) or {}
        if path == "canvas/put":
            doc = {"state": body["state"], "rev": 1, "updated": 1}
            self.canvas[(body["locus"], body["kind"])] = doc
            return dict(doc, changed=True, notified=False)
        if path == "comms/inbox":
            return {"pings": list(reversed(self.pings.get(body["to"], [])))}
        if path == "comms/ping":
            self.n += 1
            p = {"id": f"r{self.n}", "locus": body["to"], "text": body["text"],
                 "note": f"[ping][msg] from {body.get('from_')}", "by": body.get("from_"),
                 "created": 2000 + self.n, "status": "open"}
            self.pings.setdefault(body["to"], []).append(p)
            return p
        if path == "prompt/submit":
            return {"id": "p1", "path": "/x/" + body["locus"], "board_id": "r0"}
        raise AssertionError("unexpected toolserver call " + path)

    def loci_called(self, path):
        return [b.get("locus") or b.get("to") for p, b in self.calls if p == path]


def load(tmp):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    nodes = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FUNCS:
            nodes.append(n)
        elif isinstance(n, ast.Assign) and any(getattr(t, "id", "") in CONSTS for t in n.targets):
            nodes.append(n)
    tmp = Path(tmp)
    central = FakeCentral()
    st = {"file": {"schema": "todo.v1", "items": []}, "lxd": {}, "audit": [], "mail": []}

    async def known_names():
        return set(st["lxd"]) | set(ns["_ssh_hosts"]())

    async def _known_names_safe():
        return await known_names()

    def _read_json(p, default):
        try:
            return json.loads(Path(p).read_text())
        except (OSError, ValueError):
            return default

    def _mct_todo_read():
        if st["file"] is None:
            return None, "unreadable"
        return json.loads(json.dumps(st["file"])), ""

    ns = dict(
        os=os, re=re, json=json, time=time, getpass=getpass, secrets=secrets, asyncio=asyncio,
        web=web, Path=Path,
        TS_UPSTREAM="http://ts.test", FV_STATE_HOME=tmp,
        LOCUS_ALIASES_PATH=tmp / "locus-aliases.json", SSH_HOSTS_PATH=tmp / "ssh-hosts.json",
        _TS_LOCI={"hosts": {}, "self": "keeper", "lxd": set(), "synced": True},
        _DISCOVER_CACHE={"ts": 0.0, "stations": [], "err": None},
        _ts_call=central, _read_json=_read_json, known_names=known_names,
        OPS=OPS, logging=logging,                      # 1.0.144 operator-scripts on disk
        _known_names_safe=_known_names_safe,
        _keeper_locus=lambda: "keeper", _station_locus=lambda: "keeper",
        _station_id=lambda: "test-station", _local_ips_cached=lambda: {"127.0.0.1", "192.168.1.100"},
        _mct_todo_read=_mct_todo_read, _mct_todo_path=lambda: tmp / "todo.json",
        audit=lambda *a, **k: st["audit"].append(a[1:]), _audit_line=lambda *a: None,
        _keeper_mail_rows=lambda: list(st["mail"]),
        _keeper_mail_write=lambda rows: st.__setitem__("mail", list(rows)),
        _active_session=lambda: "sess", _live_surface_session=lambda *_a: None,
        _surface_key=lambda *a: "k",
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SRC), "exec"), ns)
    missing = FUNCS - set(ns)
    assert not missing, f"server.py lost {missing}"
    return ns, central, st


def fleet(ns, st):
    """The fleet this test station sees: host keeper (self), registered ssh loci,
    local ssh pointers, two LXD guests."""
    ns["_TS_LOCI"]["hosts"].update({
        "hs-fresh-ubuntu": {"host": "10.237.23.104", "user": "ubuntu", "port": 22, "source": "toolserver"},
        "a-brain-coder-next": {"host": "10.99.0.1", "user": "alpha", "port": 22, "source": "toolserver"},
        "jrputkey": {"host": "192.168.1.100", "user": "jrputkey", "port": 22, "source": "toolserver"},
        "twin-a": {"host": "10.9.9.9", "user": "x", "port": 22, "source": "toolserver"},
        "twin-b": {"host": "10.9.9.9", "user": "x", "port": 22, "source": "toolserver"},
    })
    (Path(ns["SSH_HOSTS_PATH"])).write_text(json.dumps({
        "hugpy": {"host": "192.168.1.100", "user": "hugpy", "port": 22},
        "a-brain": {"host": "10.99.0.1", "user": "alpha", "port": 22},
        "ae-self": {"host": "192.168.1.100", "user": ME, "port": 22},
        "twin": {"host": "10.9.9.9", "user": "x", "port": 22},
    }))
    st["lxd"] = {"hs-fresh": ["10.237.23.104"], "solcatcher": []}
    ns["_DISCOVER_CACHE"]["stations"] = [
        {"name": n, "ip": "", "ips": ips} for n, ips in st["lxd"].items()]


def test_resolution_every_locus_kind(tmp_path):
    ns, _c, st = load(tmp_path)
    fleet(ns, st)
    cl = ns["_central_locus"]
    for host in ("", "keeper", "@keeper", "host", None):
        assert cl(host) == "keeper"
    assert cl("hs-fresh-ubuntu") == "hs-fresh-ubuntu"        # registered ssh locus
    assert cl("jrputkey") == "jrputkey"
    assert cl("hugpy") == "hugpy"                            # local ssh, no registered twin
    assert cl("a-brain") == "a-brain-coder-next"             # local ssh pointer at a registered endpoint
    assert cl("hs-fresh") == "hs-fresh-ubuntu"               # LXD guest at a registered endpoint
    assert cl("solcatcher") == "solcatcher"                  # stopped guest: its own name
    assert cl("ae-self") == "keeper"                         # a pointer at THIS user@host
    assert cl("twin") == "twin"                              # ambiguous endpoint: never guess
    assert cl("Bad Name!") == ""
    (tmp_path / "locus-aliases.json").write_text(json.dumps({"solcatcher": "ae-solcatcher"}))
    assert cl("solcatcher") == "ae-solcatcher"               # operator alias wins
    assert ns["_locus_name_for"]("@keeper") == "keeper"
    assert ns["_locus_name_for"]("hs-fresh") == "hs-fresh-ubuntu"

    async def known():
        kk = ns["_known_locus_key"]
        assert await kk("hs-fresh") == "hs-fresh-ubuntu"
        assert await kk("hugpy") == "hugpy"
        assert await kk("typo-locus") == ""                  # unknown: never someone's rows
        assert await kk("@keeper") == "keeper"
    asyncio.run(known())


def _app(ns):
    async def proxy(request):          # api_proxy / bugreport_proxy minus the sidecar
        resp = await ns["_central_vm_reroute"](request)
        return resp if resp is not None else web.json_response({"sidecar": True}, status=299)
    app = web.Application()
    for ht in ("@self", "@keeper"):           # 1.0.141: the host board's reserved tokens
        app.router.add_get(f"/api/vm/{ht}/todo", ns["mct_todo_get"])
        app.router.add_post(f"/api/vm/{ht}/todo", ns["mct_todo_post"])
    app.router.add_get("/api/mct/todo", ns["mct_todo_get"])
    app.router.add_post("/api/mct/todo", ns["mct_todo_post"])
    app.router.add_post("/api/mct/todo/ping", ns["mct_todo_ping"])
    app.router.add_post("/api/fleet/message", ns["fleet_message_post"])
    app.router.add_route("*", "/api/vm/{tail:.*}", proxy)
    return app


@contextlib.asynccontextmanager
async def client(ns):
    async with TestClient(TestServer(_app(ns))) as c:
        yield c


def test_todo_read_write_per_locus_kind(tmp_path):
    ns, central, st = load(tmp_path)
    fleet(ns, st)
    central.seed("hugpy", "hugpy item")
    central.seed("hs-fresh-ubuntu", "guest item 1", "guest item 2")
    central.seed("keeper", "keeper item")

    async def run():
        async with client(ns) as c:
            # ssh host: reads its own rows
            d = await (await c.get("/api/vm/hugpy/todo")).json()
            assert d["ok"] and d["central"] and d["state"]["locus"] == "hugpy"
            assert [i["text"] for i in d["state"]["items"]] == ["hugpy item"]
            # LXD guest: its central key, NOT an lxc file
            d = await (await c.get("/api/vm/hs-fresh/todo")).json()
            assert d["state"]["locus"] == "hs-fresh-ubuntu" and len(d["state"]["items"]) == 2
            # the drawer's write verbs land on THAT locus only
            d = await (await c.post("/api/vm/hs-fresh/todo",
                                    json={"op": "add", "item": {"type": "todo", "text": "new on guest"}})).json()
            assert d["ok"] and [i["text"] for i in d["state"]["items"]][-1] == "new on guest"
            assert central.loci_called("todo/add") == ["hs-fresh-ubuntu"]
            tid = d["state"]["items"][-1]["id"]
            for body in ({"op": "set", "id": tid, "status": "done"},
                         {"op": "edit", "id": tid, "text": "edited on guest", "priority": "high"},
                         {"op": "comment", "id": tid, "text": "a comment"}):
                d = await (await c.post("/api/vm/hs-fresh/todo", json=body)).json()
                assert d["ok"], d
            it = next(i for i in d["state"]["items"] if i["id"] == tid)
            assert it["status"] == "done" and it["text"] == "edited on guest" and it["priority"] == "high"
            assert "a comment" in central.find(tid)[1]["note"]
            assert central.find(tid)[0] == "hs-fresh-ubuntu"
            # local ssh pointer at a registered endpoint writes the registered rows
            await c.post("/api/vm/a-brain/todo", json={"op": "add", "item": {"text": "brain item"}})
            assert [r["text"] for r in central.todos["a-brain-coder-next"]] == ["brain item"]
            # the SPA's ?vm= scoped board (/api/mct/todo) follows the same keys
            d = await (await c.get("/api/mct/todo?vm=hs-fresh")).json()
            assert d["central"] and d["state"]["locus"] == "hs-fresh-ubuntu"
            d = await (await c.post("/api/mct/todo?vm=hugpy", json={"op": "add", "text": "via mct"})).json()
            assert d["ok"] and central.todos["hugpy"][-1]["text"] == "via mct"
            d = await (await c.post("/api/mct/todo?vm=hugpy", json={"op": "remove", "id": central.todos["hugpy"][-1]["id"]})).json()
            assert d["ok"] and [r["text"] for r in central.todos["hugpy"]] == ["hugpy item"]
            # nothing leaked onto the keeper's rows
            assert [r["text"] for r in central.todos["keeper"]] == ["keeper item"]
            # unknown name: 404, no central traffic at all
            n = len(central.calls)
            r = await c.get("/api/vm/typo-locus/todo")
            assert r.status == 404 and len(central.calls) == n
            # a pointer at this very host → the host board (keeps the file failsafe),
            # answered in-process (1.0.141: no 307 — a POST must not need a follow)
            r = await c.get("/api/vm/ae-self/todo", allow_redirects=False)
            d = await r.json()
            assert r.status == 200 and d["ok"] and d.get("locus", "keeper") == "keeper"
            # an LXD guest's machine-side tails still reach the lxc sidecar
            assert (await c.post("/api/vm/hs-fresh/start")).status == 299
    asyncio.run(run())


def test_central_note_never_truncated(tmp_path):
    """BOARD-ITEM-FORMAT.md: an operator item's note is a static reference — a
    console comment or resolve must keep every section and every prior comment
    (it used to cut the note to 2000 chars and drop the comments on resolve)."""
    ns, central, st = load(tmp_path)
    fleet(ns, st)
    long_note = "WHY: x\n" + "\n".join("line %d" % i for i in range(600)) + "\nEXPECT: tail-sentinel"
    central.seed("hugpy", "[root] long item")
    central.todos["hugpy"][-1].update(type="operator", note=long_note)
    tid = central.todos["hugpy"][-1]["id"]

    async def run():
        async with client(ns) as c:
            d = await (await c.get("/api/vm/hugpy/todo")).json()
            assert d["state"]["items"][0]["note"].endswith("tail-sentinel")      # rendered whole
            for body in ({"op": "comment", "id": tid, "text": "first"},
                         {"op": "comment", "id": tid, "text": "second"},
                         {"op": "resolve", "id": tid, "verdict": "accepted"}):
                assert (await (await c.post("/api/vm/hugpy/todo", json=body)).json())["ok"]
            note = central.find(tid)[1]["note"]
            assert note.startswith("[accepted] WHY: x") and "tail-sentinel" in note
            assert "first" in note and "second" in note and len(note) > 2000
    asyncio.run(run())


def test_operator_scripts_materialized_on_keeper_read(tmp_path):
    """1.0.144: a read of THIS locus's board writes every open operator item's
    RUN block to <state>/operator-scripts/<id>.sh and annotates the item with
    its one-liner; another locus's slice gets no files."""
    ns, central, st = load(tmp_path)
    fleet(ns, st)
    run = "WHY: x\nWHO: root\nGATE: none\nDO:\n```bash\nls\n```\nVERIFY:\n```bash\ntrue\n```\nEXPECT: ok\nRUN:\n```bash\n#!/usr/bin/env bash\nset -euo pipefail\ncd /srv\nls\n```"
    central.seed("keeper", "[root] keeper op")
    central.todos["keeper"][-1].update(type="operator", note=run)
    central.seed("hugpy", "[root] hugpy op")
    central.todos["hugpy"][-1].update(type="operator", note=run)
    kid, hid = central.todos["keeper"][-1]["id"], central.todos["hugpy"][-1]["id"]

    async def run_():
        async with client(ns) as c:
            d = await (await c.get("/api/vm/ae-self/todo")).json()
            it = next(i for i in d["state"]["items"] if i["id"] == kid)
            f = tmp_path / "operator-scripts" / f"{kid}.sh"
            assert f.is_file() and it["one_liner"] == f"bash {f}" and "rollback_one_liner" not in it
            assert f.read_text().endswith("cd /srv\nls\n") and it["note"] == run       # stored text untouched
            await (await c.get("/api/vm/hugpy/todo")).json()
            assert not (tmp_path / "operator-scripts" / f"{hid}.sh").exists()
            # a run leaves a sidecar + log: attached ONCE as a RESULT comment, result + flag
            # annotated, the item left open
            rd = tmp_path / "operator-scripts" / "results" / kid
            rd.mkdir(parents=True)
            (rd / "20261001T120000Z.log").write_text("12:00:00 one\n12:00:01 two\n")
            (rd / "20261001T120000Z.json").write_text(json.dumps({
                "item": kid, "kind": "run", "exit": 3, "started": "2026-10-01T12:00:00Z", "ended": "2026-10-01T12:00:01Z",
                "host": "ae", "user": "solcatcher", "cwd": "/home/solcatcher", "log": str(rd / "20261001T120000Z.log")}))
            d = await (await c.get("/api/vm/ae-self/todo")).json()
            it = next(i for i in d["state"]["items"] if i["id"] == kid)
            assert it["status"] == "open" and it["result"]["exit"] == 3 and it["result"]["tail"] == ["12:00:00 one", "12:00:01 two"]
            assert it["result"]["path"] == str(rd / "20261001T120000Z.log") and it["result"]["kind"] == "run"
            assert it["flag"]["status"] == "fail" and it["flag"]["reason"] == "exit 3" and it["flag"]["by"] == "operator-script"
            stored = central.find(kid)[1]["note"]
            assert stored.startswith(run) and "RESULT 2026-10-01T12:00:01Z exit 3 (FAILED)" in stored and "    12:00:01 two" in stored
            await (await c.get("/api/vm/ae-self/todo")).json()
            assert central.find(kid)[1]["note"].count("RESULT ") == 1                 # exactly once
            assert [p for p, _ in central.calls if p == "todo/update"] == ["todo/update"]
            central.find(kid)[1]["status"] = "done"           # closed (by the keeper, through any path)
            await (await c.get("/api/vm/ae-self/todo")).json()
            assert not f.exists() and (tmp_path / "operator-scripts" / f"{kid}.sh.done").is_file()
    asyncio.run(run_())


def test_host_keeper_failsafe(tmp_path):
    ns, central, st = load(tmp_path)
    fleet(ns, st)
    st["file"]["items"] = [{"id": "t1", "type": "todo", "text": "a", "status": "open"},
                           {"id": "t2", "type": "todo", "text": "b", "status": "open"}]

    async def run():
        async with client(ns) as c:
            central.seed("keeper", "only one")                     # central SHORTER than the file
            d = await (await c.get("/api/vm/keeper/todo")).json()
            assert not d.get("central") and len(d["state"]["items"]) == 2
            central.seed("keeper", "two", "three")                 # central caught up / ahead
            d = await (await c.get("/api/vm/keeper/todo")).json()
            assert d["central"] and d["locus"] == "keeper" and len(d["state"]["items"]) == 3
            central.down = True                                    # unreachable → the file
            d = await (await c.get("/api/mct/todo")).json()
            assert not d.get("central") and len(d["state"]["items"]) == 2
            d = await (await c.get("/api/mct/todo?vm=@keeper")).json()
            assert len(d["state"]["items"]) == 2
    asyncio.run(run())


def test_canvas_messages_brief_ping_central(tmp_path):
    ns, central, st = load(tmp_path)
    fleet(ns, st)
    central.seed("hs-fresh-ubuntu", "open one")

    async def run():
        async with client(ns) as c:
            d = await (await c.post("/api/vm/hs-fresh/design",
                                    json={"state": {"screens": []}})).json()
            assert d["ok"] and d["locus"] == "hs-fresh-ubuntu"
            d = await (await c.get("/api/vm/hs-fresh/design")).json()
            assert d["ok"] and d["state"] == {"screens": []}
            # ✉ send to an LXD guest: central comms ping on its key (no lxc)
            d = await (await c.post("/api/fleet/message", json={"to": "hs-fresh", "text": "hello"})).json()
            assert d["ok"] and d["via"] == "board" and d["locus"] == "hs-fresh-ubuntu"
            # ✉ read: the same central inbox, oldest first, sender parsed
            await c.post("/api/fleet/message", json={"to": "hs-fresh", "text": "second"})
            d = await (await c.get("/api/vm/hs-fresh/messages")).json()
            assert [m["text"] for m in d["messages"]] == ["hello", "second"]
            assert d["messages"][0]["from"] == "keeper"
            # ⧉ brief: one ping carrying the open-items snapshot
            d = await (await c.post("/api/vm/hs-fresh/todo/brief", json={"text": "look"})).json()
            assert d["ok"] and d["open"] == 1
            assert "open one" in central.pings["hs-fresh-ubuntu"][-1]["text"]
            # 📨 per-item ping for a ?vm= locus
            tid = central.todos["hs-fresh-ubuntu"][0]["id"]
            d = await (await c.post("/api/mct/todo/ping?vm=hs-fresh", json={"id": tid})).json()
            assert d["ok"] and d["locus"] == "hs-fresh-ubuntu"
            # todo-history has no central journal: an honest 404, not an lxc read
            assert (await c.get("/api/vm/hs-fresh/todo-history")).status == 404
            # to=<pointer at this host> lands in the host keeper inbox
            d = await (await c.post("/api/fleet/message", json={"to": "ae-self", "text": "me"})).json()
            assert d["to"] == "keeper" and st["mail"][-1]["text"] == "me"
            assert (await c.post("/api/fleet/message", json={"to": "nope", "text": "x"})).status == 404
            # nothing reached any other locus
            assert set(central.pings) == {"hs-fresh-ubuntu"}
    asyncio.run(run())


def test_startup_race_waits_for_first_loci_sync(tmp_path):
    """Right after a restart the registered loci are unknown; a resolution must
    wait for the first loci sync instead of filing rows under the bare name."""
    ns, _c, st = load(tmp_path)
    fleet(ns, st)
    hosts = dict(ns["_TS_LOCI"]["hosts"])
    ns["_TS_LOCI"].update(hosts={}, synced=False)

    async def run():
        async def sync_later():
            await asyncio.sleep(0.3)
            ns["_TS_LOCI"].update(hosts=hosts, synced=True)
        asyncio.get_running_loop().create_task(sync_later())
        assert await ns["_known_locus_key"]("hs-fresh") == "hs-fresh-ubuntu"
    asyncio.run(run())


if __name__ == "__main__":
    import inspect
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as d:
                try:
                    fn(Path(d)) if inspect.signature(fn).parameters else fn()
                    print("ok  ", name)
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    print("FAIL", name, repr(e))
    sys.exit(1 if failed else 0)
