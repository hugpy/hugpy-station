"""1.0.144 — every locus gets a serve chat + keeper, the station delivers to it,
and the keeper is reminded by a deterministic open-todos DELTA digest.

* locus_exec: seat listing on the target's own socket, the serve provisioner
  shipped on stdin (lxd: linger first), the lxd stdio bridge forward (a REAL
  local relay to a real HTTP server).
* resources/bin/station-serve-provision: run for real against a temp HOME with
  stub systemctl/curl and a fake station payload — every artifact it lays down
  is derived from the shipped files.
* server.py (AST-extracted, toolserver/serve stubbed): the remote-locus comms
  leg (claim -> gate as sender->receiver -> serve nudge -> comms/delivered, never
  tmux) and the digest loop (no B, no cap, idle-gated, recorded on the board).
* todo_digest: delta, size cap, finding exclusion, decide().
* prompt_send: a native keeper id is adopted as a console session.
Nothing here touches a live serve, seat, station or the network.
Run:  python3 -m pytest resources/backend/test_locus_serve_144.py
"""
import ast
import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE.parent
sys.path.insert(0, str(HERE))
import issue_gate  # noqa: E402
import keeper_nudge  # noqa: E402
import locus_exec as LX  # noqa: E402
import prompt_send as PS  # noqa: E402
import todo_digest as TD  # noqa: E402

SSH_ARGV = ["ssh", "-o", "BatchMode=yes", "hugpy@10.0.0.9"]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── locus_exec ───────────────────────────────────────────────────────────────
def test_list_seats_runs_on_the_target_and_parses():
    calls = []

    async def runner(argv, stdin=None, timeout=30):
        calls.append((argv, stdin))
        return 0, "keeper-claude\t1\t1790850175\nkeeper-codex\t0\t1790850218\n", ""
    t = LX.Target(LX.KIND_SSH, "hugpy", "hugpy", ssh_argv=SSH_ARGV)
    ok, seats, err = run(LX.list_seats(t, runner=runner))
    assert ok and err == ""
    assert seats == [{"name": "keeper-claude", "attached": True, "created": 1790850175},
                     {"name": "keeper-codex", "attached": False, "created": 1790850218}]
    argv, stdin = calls[0]
    assert argv == SSH_ARGV + ["bash", "-s"]                       # AS the locus user, script on stdin
    assert b"tmux -L console list-sessions" in stdin
    assert LX.parse_seats("__notmux\n") == []


def test_provision_ships_the_script_on_stdin_and_lxd_gets_linger_first():
    calls = []

    async def runner(argv, stdin=None, timeout=30):
        calls.append((argv, stdin))
        return 0, 'log line\n{"ok": true, "port": 9125, "user": "ubuntu"}\n', "station-serve-provision: step\n"
    t = LX.Target(LX.KIND_LXC, "hs-fresh", "hs-fresh-ubuntu")
    res = run(LX.serve_provision(t, "#!/bin/bash\necho body\n", locus="hs-fresh-ubuntu", port=9130, runner=runner))
    assert res["ok"] and res["port"] == 9125 and "step" in res["log"]
    assert calls[0][0] == ["lxc", "exec", "hs-fresh", "--", "loginctl", "enable-linger", "ubuntu"]
    argv, stdin = calls[1]
    assert argv[:3] == ["lxc", "exec", "hs-fresh"] and argv[-2:] == ["bash", "-s"]
    text = stdin.decode()
    assert "export PROVISION_LOCUS=hs-fresh-ubuntu" in text and "export PROVISION_PORT=9130" in text
    assert text.rstrip().endswith("echo body")
    # ssh: no root step at all
    calls.clear()
    run(LX.serve_provision(LX.Target(LX.KIND_SSH, "hugpy", "hugpy", ssh_argv=SSH_ARGV), "x\n", runner=runner))
    assert len(calls) == 1 and calls[0][0] == SSH_ARGV + ["bash", "-s"]


def test_provision_failure_is_reported_not_raised():
    async def runner(argv, stdin=None, timeout=30):
        return 1, "", "ssh: Permission denied (publickey)"
    res = run(LX.serve_provision(LX.Target(LX.KIND_SSH, "aeb", "aeb", ssh_argv=SSH_ARGV), "x\n", runner=runner))
    assert res["ok"] is False and "Permission denied" in res["error"]


class _Hello(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_bridge_forward_relays_http_to_the_targets_loopback():
    """The lxd path, for real: bridge_argv(self) is the very python relay the
    guest runs; a local listener pipes each connection through it."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Hello)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rport = srv.server_address[1]
    try:
        async def go():
            bf = LX.BridgeForwards()
            t = LX.Target(LX.KIND_LXC, "guest", "guest")
            bf_argv = LX.bridge_argv(t, rport)
            assert bf_argv[:4] == ["lxc", "exec", "guest", "--"] and bf_argv[-3] == "-c"
            # run the SAME relay locally (no lxc here)
            bf._spawn = lambda *argv, **kw: asyncio.create_subprocess_exec(
                *LX.bridge_argv(LX.Target(LX.KIND_SELF), rport), **kw)
            lport = await bf.ensure(t, rport)
            assert await bf.ensure(t, rport) == lport                  # reused
            r, w = await asyncio.open_connection("127.0.0.1", lport)
            w.write(b"GET /api/state HTTP/1.0\r\nHost: x\r\n\r\n")
            await w.drain()
            data = await asyncio.wait_for(r.read(), 10)
            w.close()
            return data
        data = run(go())
        assert b"200" in data.split(b"\r\n", 1)[0] and b'"/api/state"' in data
    finally:
        srv.shutdown()


# ── station-serve-provision, run for real ────────────────────────────────────
def _fake_env(tmp):
    home = tmp / "home"
    (home / ".config").mkdir(parents=True)
    app = tmp / "app"
    for d in ("resources/bin", "resources/systemd", "resources/backend"):
        (app / d).mkdir(parents=True)
    (app / "resources/VERSION").write_text("9.9.9\n")
    tpl = (RES / "systemd" / "abstract-claude-serve@.service").read_text()
    (app / "resources/systemd/abstract-claude-serve@.service").write_text(tpl)
    run_sh = app / "resources/bin/abstract-claude-serve-run"
    run_sh.write_text("#!/bin/bash\n")
    run_sh.chmod(0o755)
    prov = app / "resources/bin/station-provision-venv"     # the shipped provisioner, stubbed: lay a venv
    prov.write_text('#!/bin/bash\nV="$1"; mkdir -p "$V/bin"\n'
                    'printf "#!/bin/bash\\necho abstract-claude 0.1.71\\n" > "$V/bin/abstract-claude"\n'
                    'chmod +x "$V/bin/abstract-claude"\n')
    prov.chmod(0o755)
    stub = tmp / "stub"
    stub.mkdir()
    log = tmp / "systemctl.log"
    (stub / "systemctl").write_text('#!/bin/bash\necho "$*" >> %s\n'
                                    'case "$*" in *is-active*) [ -f %s/active ] ;; *is-failed*) exit 1 ;; *) exit 0 ;; esac\n'
                                    % (log, tmp))
    (stub / "curl").write_text('#!/bin/bash\ntouch %s/active; echo 200\n' % tmp)
    (stub / "python3").write_text('#!/bin/bash\nif [ "$1" = -m ] && [ "$2" = venv ]; then mkdir -p "$3/bin"; '
                                  'printf "#!/bin/bash\\nexit 0\\n" > "$3/bin/pip"; chmod +x "$3/bin/pip"; '
                                  'printf "#!/bin/bash\\nexit 0\\n" > "$3/bin/python"; chmod +x "$3/bin/python"; exit 0; fi\n'
                                  'exec /usr/bin/python3 "$@"\n')
    (stub / "ss").write_text("#!/bin/bash\nexit 0\n")
    for f in stub.iterdir():
        f.chmod(0o755)
    env = {"HOME": str(home), "PATH": "%s:/usr/bin:/bin" % stub, "XDG_RUNTIME_DIR": str(tmp),
           "PROVISION_LOCUS": "hugpy", "PROVISION_APP_ROOT": str(app)}
    return home, app, env, log


def test_provision_script_lays_down_only_derived_artifacts(tmp_path):
    home, app, env, log = _fake_env(tmp_path)
    state = home / ".config" / "hugpy-station"                  # NOT ~/hugpy-station -> drop-in
    script = (RES / "bin" / "station-serve-provision").read_text()
    p = subprocess.run(["bash", "-s"], input=script, env=env, capture_output=True, text=True, timeout=60)
    doc = json.loads(p.stdout.strip().splitlines()[-1])
    assert doc["ok"] is True, (doc, p.stderr)
    assert doc["port"] == 9125 and doc["locus"] == "hugpy" and doc["state"] == str(state)
    unit = home / ".config/systemd/user/abstract-claude-serve@.service"
    assert unit.read_text() == (app / "resources/systemd/abstract-claude-serve@.service").read_text()
    assert os.readlink(home / ".local/bin/abstract-claude-serve-run") == str(app / "resources/bin/abstract-claude-serve-run")
    drop = (home / ".config/systemd/user/abstract-claude-serve@station.service.d/10-station-state.conf").read_text()
    assert "Environment=HUGPY_STATION_STATE=%s" % state in drop and "generated by station-serve-provision" in drop
    senv = (state / "env/serve-station.env").read_text()
    for k in ("ABSTRACT_CLAUDE_BIN=", "AC_PORT=\"9125\"", "EXCHANGE_LOCUS=\"hugpy\"", "AC_UI_BASE=\"/ac\""):
        assert k in senv
    calls = log.read_text()
    assert "enable abstract-claude-serve@station.service" in calls and "start abstract-claude-serve@station.service" in calls
    # second run: healthy -> reported, NOT restarted, operator keys kept
    (state / "env/serve-station.env").write_text(senv.replace('AC_PORT="9125"', 'AC_PORT="9200"'))
    log.write_text("")
    p2 = subprocess.run(["bash", "-s"], input=script, env=env, capture_output=True, text=True, timeout=60)
    doc2 = json.loads(p2.stdout.strip().splitlines()[-1])
    assert doc2["ok"] and doc2["port"] == 9200 and "nothing changed" in " ".join(doc2["steps"])
    assert "restart" not in log.read_text() and "start abstract" not in log.read_text()


def test_provision_script_names_the_blocker_without_a_station_install(tmp_path):
    home, app, env, _log = _fake_env(tmp_path)
    env.pop("PROVISION_APP_ROOT")
    script = (RES / "bin" / "station-serve-provision").read_text().replace("/opt/hugpy-station", str(tmp_path / "none"))
    p = subprocess.run(["bash", "-s"], input=script, env=env, capture_output=True, text=True, timeout=60)
    doc = json.loads(p.stdout.strip().splitlines()[-1])
    assert doc["ok"] is False and "no hugpy-station install" in doc["error"]


# ── server.py: remote comms leg + digest (AST-extracted) ─────────────────────
FUNCS = {"_deliver_pings_locus", "_nudge_frontier", "_no_tmux", "_nudge_pending", "_nudge_pending_write",
         "_pings_seen", "_pings_seen_write", "_gate_pings", "_ping_sender", "_issue_gate", "_nudge_drain",
         "_is_channel_ping", "_nudge_paths", "_locus_state_dir", "_digest_state_path", "_digest_rows",
         "_digest_board_record", "_digest_once"}
CONSTS = {"_CHANNEL_REF_RE", "_KEEP"}


def _load(tmp, board=None, inbox=None, page=True, claim_won=True):
    tree = ast.parse((HERE / "server.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body
             if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FUNCS)
             or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in CONSTS for t in n.targets))]
    tmp = Path(tmp)
    rec = {"serve": [], "observed": [], "claimed": [], "delivered": [], "board": [], "b": 0}
    board = board if board is not None else []

    async def ts_call(app, path, body=None, timeout=8):
        body = body or {}
        if path == "comms/inbox":
            return {"pings": list(inbox or [])}
        if path == "comms/claim":
            rec["claimed"].append(body["id"])
            return {"won": claim_won, "claimed_by": "other@x" if not claim_won else body["by"]}
        if path == "comms/delivered":
            rec["delivered"].append(body["id"])
            return {"ok": True}
        if path == "issue/observe":
            rec["observed"].append(body)
            return {"fp": "comms:%d" % len(rec["observed"]), "page": page, "state": "raised"}
        if path == "todo/list":
            if body.get("type") == "bookmark":
                return [r for r in rec["board"] if r.get("type") == "bookmark"]
            st = body.get("status")
            return [r for r in board if not st or r.get("status") == st]
        if path == "todo/add":
            row = dict(body, id="b%d" % (900 + len(rec["board"])), status="open")
            rec["board"].append(row)
            return row
        if path == "todo/update":
            for r in rec["board"]:
                if r["id"] == body["id"]:
                    r.update(body)
            return {}
        raise AssertionError(path)

    async def nudge_serve(app, line, base=None):
        rec["serve"].append((base, line))
        return True, "serve:keeper cs-1 (sent)"

    def read_json(p, default):
        try:
            return json.loads(Path(p).read_text())
        except (OSError, ValueError):
            return default

    def write_json(p, doc):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(json.dumps(doc))

    async def ac_resolve(vm):
        return "http://127.0.0.1:5555", "u", "/ac/@%s/" % vm, vm

    async def keeper_idle(app, base):
        return "cs-1", False, "roster"

    def todo_keeper_chat(*a, **k):                 # B — must never be consulted
        rec["b"] += 1
        raise ConnectionError("B gateway unreachable")

    async def surface_now(vm=""):                  # 1.0.147: these tests model serve-surface loci
        return "serve", {"ok": True}

    ns = dict(re=re, json=json, time=time, Path=Path, os=os, asyncio=asyncio,
              KEEPER_SURFACE="serve", _keeper_surface_now=surface_now,
              _ts_call=ts_call, _station_locus=lambda: "keeper", _read_json=read_json,
              _write_json_atomic=write_json, FV_STATE_HOME=tmp,
              _NUDGE_PENDING_PATH=tmp / "pending.json", _PINGS_SEEN_PATH=tmp / "seen.json",
              _knudge=keeper_nudge, NUDGE_MIN_INTERVAL=0, _ig=issue_gate, KEEPER_TARGET="keeper",
              _nudge_serve=nudge_serve, _nudge_tmux=lambda *a: (_ for _ in ()).throw(AssertionError("tmux")),
              _audit_line=lambda *a: None, _rlog=logging.getLogger("t"), _station_id=lambda: "vm_mgr@ae",
              _canon_locus=lambda n: (n or "").lower(), _ac_resolve=ac_resolve, _AC_DISCOVERED={},
              _ac_locus_key=lambda v: v, AC_UPSTREAM="http://127.0.0.1:9124", TD=TD,
              DIGEST_CADENCE=1800, DIGEST_TOP_N=5, DIGEST_MAX_CHARS=1500,
              _todo_keeper_chat=todo_keeper_chat, _keeper_busy=keeper_idle,
              _hold_note=lambda *a, **k: None, _hold_clear=lambda *a, **k: None, _sh=None,   # 1.0.146 strip rows
              _serve_queue_release=None)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "server.py", "exec"), ns)
    assert not (FUNCS - set(ns)), FUNCS - set(ns)
    return ns, rec


def test_remote_ping_is_claimed_gated_as_delegation_delivered_and_marked():
    pings = [{"id": "r4165", "text": "[handoff] take over the MoE work", "note": "[ping] from keeper (ref ledger:hugpy/x)"},
             {"id": "r1677", "text": "commit your WorkerRow change", "note": "[ping][msg] from keeper"}]
    with tempfile.TemporaryDirectory() as d:
        ns, rec = _load(d, board=[{"id": "r4165", "status": "open"}, {"id": "r1677", "status": "open"}], inbox=pings)
        run(ns["_deliver_pings_locus"]({"_rt": {}}, {"vm": "hugpy", "locus": "hugpy", "host": False}))
        seen = json.loads((Path(d) / "loci/hugpy/comms-delivered.json").read_text())["ids"]
    assert rec["claimed"] == ["r4165", "r1677"]
    # keeper -> hugpy is a delegation: NOT the keeper's own run (self-origin globs match `keeper`)
    assert [o["caller"] for o in rec["observed"]] == ["keeper->hugpy", "keeper->hugpy"]
    assert all(o["locus"] == "hugpy" for o in rec["observed"])
    assert len(rec["serve"]) == 1 and rec["serve"][0][0] == "http://127.0.0.1:5555"   # the LOCUS's serve
    assert "hugpy" in rec["serve"][0][1]
    assert sorted(rec["delivered"]) == ["r1677", "r4165"] and sorted(seen) == ["r1677", "r4165"]


def test_remote_ping_lost_claim_is_not_nudged():
    pings = [{"id": "r9", "text": "x", "note": "[ping] from op"}]
    with tempfile.TemporaryDirectory() as d:
        ns, rec = _load(d, board=[{"id": "r9", "status": "open"}], inbox=pings, claim_won=False)
        run(ns["_deliver_pings_locus"]({"_rt": {}}, {"vm": "hugpy", "locus": "hugpy", "host": False}))
    assert rec["serve"] == [] and rec["delivered"] == [] and rec["observed"] == []


def _rows(n, now, parent="F3"):
    return [{"id": "t%d" % (100 + i), "type": "todo", "status": "open", "priority": ("high", "medium", "low")[i % 3],
             "text": "[%s.%d] item number %d %s" % (parent, i, i, "x" * 60), "created": now - 3600 * (i + 1),
             "updated": now - 60, "note": "Acceptance: it works %d" % i} for i in range(n)]


def test_digest_is_delivered_with_B_unreachable_and_recorded_on_the_board():
    now = time.time()
    with tempfile.TemporaryDirectory() as d:
        ns, rec = _load(d, board=_rows(60, now))
        sent = []

        async def send(text, base):
            sent.append((base, text))
            return True, "serve:keeper cs-1 (sent)"
        st = run(ns["_digest_once"]({"_rt": {}}, {"vm": "", "locus": "keeper", "host": True}, now=now, send=send))
    assert rec["b"] == 0                                   # B never consulted
    assert st["last_state"] == "delivered" and st["open"] == 60 and len(sent) == 1
    assert sent[0][0] == "http://127.0.0.1:9124"
    text = sent[0][1]
    assert len(text) <= 1500 and "OPEN-TODOS DIGEST" in text and "todo_list locus=keeper status=open" in text
    row = rec["board"][0]
    assert row["type"] == "bookmark" and row["text"].startswith("[digest]") and "delivered" in row["text"]


def test_digest_repeats_without_a_cap_and_queues_behind_a_busy_keeper():
    """No 3-strike cutoff: an unchanged open board is re-sent after the 2 h
    floor, every time; a busy keeper does NOT defer it (1.0.146): it is sent
    (queued behind the turn); only an unreadable keeper is "unreachable"."""
    s = {}
    t0 = 1_000_000.0
    for k in range(10):                                    # ten cycles, never capped
        now = t0 + k * (TD.UNCHANGED_FLOOR_S + 1)
        assert TD.decide(s, 5, now, 1800, busy=False, sig="same") == "send"
        s = {"last_delivered": now, "last_sig": "same"}
    assert TD.decide(s, 5, now + 1801, 1800, busy=False, sig="same") == "skip"      # unchanged, < 2 h
    assert TD.decide(s, 5, now + 1801, 1800, busy=False, sig="changed") == "send"   # changed, > cadence
    assert TD.decide(s, 5, now + 600, 1800, busy=False, sig="changed") == "wait"    # inside the cadence
    assert TD.decide(s, 5, now + TD.UNCHANGED_FLOOR_S + 1, 1800, busy=True, sig="same") == "send"
    assert TD.decide(s, 5, now + TD.UNCHANGED_FLOOR_S + 1, 1800, busy=None, sig="same") == "unreachable"
    assert TD.decide(s, 0, now + 99999, 1800, busy=False) == "idle"


def test_digest_QUEUES_behind_a_busy_keeper_and_records_not_delivered_without_serve():
    """1.0.146 (t4260 / d4254): a busy keeper no longer "defers" the digest for
    an unbounded number of ticks — it goes through the nudge path (console
    submit) and QUEUES behind the turn; state "queued" + the message id."""
    now = time.time()
    with tempfile.TemporaryDirectory() as d:
        ns, rec = _load(d, board=_rows(3, now))

        async def busy(app, base):
            return "cs-1", True, "roster"
        ns["_keeper_busy"] = busy
        app = {"_rt": {}}
        sent = []

        async def send(text, base):
            sent.append(text)
            app["_rt"]["nudge_last_submit"] = {"sid": "cs-1", "message_ids": ["m-77"], "queued": True}
            return True, "serve:keeper cs-1 via roster (queued)"
        st = run(ns["_digest_once"](app, {"vm": "", "locus": "keeper", "host": True}, now=now, send=send))
        assert len(sent) == 1 and st["last_state"] == "queued" and "deferred" not in json.dumps(st)
        assert st["queue_message_ids"] == ["m-77"] and st["queued_behind"] == "cs-1" and "m-77" in st["last_detail"]
        assert any("queued" in r["text"] for r in rec["board"])

        async def noserve(vm):
            return "", "", "", vm
        ns["_ac_resolve"] = noserve
        st2 = run(ns["_digest_once"]({"_rt": {}}, {"vm": "hugpy", "locus": "hugpy", "host": False}, now=now))
        assert st2["last_state"] == "NOT delivered" and "no reachable serve" in st2["last_detail"]
        assert any("NOT delivered" in r["text"] for r in rec["board"])


def test_delta_digest_shows_changes_caps_size_and_excludes_findings():
    now = time.time()
    rows = _rows(60, now) + [
        {"id": "t900", "type": "finding", "status": "open", "priority": "high", "text": "rate_limit_429 x"},
        {"id": "t901", "type": "todo", "status": "open", "priority": "high", "text": "[finding] rate_limit_429"},
        {"id": "o902", "type": "operator", "status": "open", "priority": "high", "text": "[prompt] 1 open items"},
        {"id": "d903", "type": "direction", "status": "open", "priority": "high", "text": "standing rule"}]
    text, n = TD.build(rows, "keeper", now)
    assert n == 60 and "t900" not in text and "t901" not in text and "o902" not in text and "d903" not in text
    assert len(text) <= TD.DEFAULT_MAX_CHARS and "first digest" in text
    snap = TD.snapshot(rows)
    rows2 = [r for r in rows if r["id"] != "t100"] + [
        {"id": "t999", "type": "todo", "status": "open", "priority": "high", "text": "[F6.1] new thing", "created": now}]
    rows2[0] = dict(rows2[0], updated=now, note="commented by keeper")
    t2, _ = TD.build(rows2, "keeper", now, prev_snap=snap)
    assert "+ t999" in t2 and "✓ t100 closed" in t2 and "✎ %s acted on" % rows2[0]["id"] in t2
    assert "→ Acceptance:" in t2
    tiny, _ = TD.build(rows2, "keeper", now, prev_snap=snap, max_chars=500)
    assert len(tiny) <= 500 and "todo_list locus=keeper status=open" in tiny and tiny.startswith("⏱ OPEN-TODOS")
    assert TD.signature(rows) != TD.signature(rows2) and TD.signature(rows) == TD.signature(list(rows))


def test_reminder_mitigator_is_off_by_default():
    src = (HERE / "server.py").read_text(encoding="utf-8")
    assert 'STATION_REMIND_MITIGATOR", "0"' in src
    assert "if not FV_TODO_REMIND or not REMIND_MITIGATOR:" in src


def test_locus_console_referer_grounds_b_calls():
    tree = ast.parse((HERE / "server.py").read_text(encoding="utf-8"))
    ns = {"re": re}
    for n in tree.body:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in ("_AC_FRAME_REF_RE", "_AC_FRAME_STATION_APIS"):
            exec(compile(ast.Module(body=[n], type_ignores=[]), "server.py", "exec"), ns)
    m = ns["_AC_FRAME_REF_RE"].search("https://station.hugpy.ai/ac/@hugpy/?x=1")
    assert m and m.group(1) == "hugpy"
    assert not ns["_AC_FRAME_REF_RE"].search("https://station.hugpy.ai/ac/")
    assert "/api/b/chat".startswith(ns["_AC_FRAME_STATION_APIS"])


def test_drain_compares_the_console_head_not_the_native_id(tmp_path):
    """Regression (seen live on hugpy): the roster names the NATIVE keeper id,
    the nudge went to its console session cs-…; the drain read the mismatch as
    a rollover and re-queued the same nudge every tick. The head is resolved
    to its console session first, so a busy keeper just waits."""
    tree = ast.parse((HERE / "server.py").read_text(encoding="utf-8"))
    want = {"_nudge_drain", "_keeper_console_head", "_nudge_pending", "_nudge_pending_write"}
    nodes = [n for n in tree.body if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in want)
             or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_KEEP")]
    posts = []

    async def keeper_session(app, base=None):
        return "9322699d-native", "roster"

    async def serve_get(app, path, timeout=8, base=None):
        return 200, {"items": [{"id": "m1"}], "busy": True, "auto": True, "paused": False}

    async def serve_post(app, path, body, timeout=8, base=None):
        posts.append(path)
        if path == "/api/console/sessions":
            return 200, {"id": "cs-0bb3", "native_id": body["native_id"]}
        return 200, {}

    def read_json(p, default):
        try:
            return json.loads(Path(p).read_text())
        except (OSError, ValueError):
            return default
    pend = tmp_path / "pending.json"
    ns = dict(json=json, time=time, Path=Path, _read_json=read_json, _knudge=keeper_nudge,
              _keeper_serve_session=keeper_session, _serve_get=serve_get, _serve_post=serve_post,
              _NUDGE_PENDING_PATH=pend, _audit_line=lambda *a: None, _rlog=logging.getLogger("t"),
              _hold_note=lambda *a, **k: None, _hold_clear=lambda *a, **k: None, _sh=None, _serve_queue_release=None)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "server.py", "exec"), ns)
    infl = {"sid": "cs-0bb3", "message_ids": ["m1"], "items": [{"id": "r4229"}], "at": time.time() - 60}
    pend.write_text(json.dumps({"pings": [], "last_sent": 5, "inflight": infl}))
    assert run(ns["_nudge_drain"]({}, "http://127.0.0.1:5555", pend)) is True        # waits, no requeue
    assert "/api/console/queue" not in posts
    assert json.loads(pend.read_text())["inflight"]["sid"] == "cs-0bb3"


# ── prompt_send: a native keeper id is adopted ───────────────────────────────
def test_prompt_send_adopts_a_native_keeper_session():
    calls = []

    async def call(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith("/api/console/queue?id=9322"):
            return 404, {"error": "Unknown session"}
        if path == "/api/console/sessions":
            return 200, {"id": "cs-abc", "native_id": body["native_id"]}
        if path.startswith("/api/console/queue?id=cs-abc"):
            return 200, {"session_id": "cs-abc", "items": [], "busy": False}
        if path == "/api/console/chat":
            return 200, {"session_id": "cs-abc", "message_ids": ["m1"]}
        raise AssertionError(path)

    async def head():
        return "9322699d-native", "roster"
    res = run(PS.deliver_serve(call, "hello", [], head=head))
    assert res["state"] == "delivered" and res["session_id"] == "cs-abc" and "adopted" in res["why"]
    assert ("POST", "/api/console/sessions", {"backend": "claude", "native_id": "9322699d-native"}) in calls


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
