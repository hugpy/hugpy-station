"""t4262: /api/locus/keeper — the locus's serve roster Keeper role on the
steward › sessions card. Exercises the REAL route code lifted from server.py
(ast, like test_serve_proxy) against a fake serve (aiohttp TestServer); the
locus resolver, serve resolver and tmux-seat summary are stubbed."""
import ast
import asyncio
import json
import re
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

NAMES = {"_ac_call", "_ac_err_text", "_locus_keeper_base", "_locus_keeper_seat", "_locus_keeper_doc", "api_locus_keeper"}

ROSTER = {"roles": [
    {"role": "keeper", "label": "Keeper", "backend": "claude", "model": "claude-fable-5-1",
     "session_id": "cs-keeper1", "live_session_id": "cs-keeper1", "pending_model": None,
     "default_model": "", "model_by": "operator-ui", "model_at": 1790884651},
    {"role": "chat", "label": "Chat", "backend": "claude", "model": "claude-opus-4-6",
     "session_id": "cs-chat1", "live_session_id": "cs-chat1", "pending_model": None},
], "defaults": {"keeper": ""}}
SESSIONS = {"sessions": [
    {"id": "cs-chat1", "backend": "claude", "model": "claude-opus-4-6", "busy": False, "updated": 1.0},
    {"id": "cs-keeper1", "backend": "claude", "native_id": "ffa61f62-native", "model": "claude-fable-5-1",
     "label": "Keeper (fresh)", "busy": True, "updated": 1790886152.6, "created": 1790883793.6, "cwd": "/srv/vm_mgr",
     "actual_model": "claude-fable-5-1",
     "rollover": {"gauge": {"tokens": 57711, "window": 294000, "pct": 19.6,
                            "source": "claude:usage(input+cache_read+cache_creation)", "model": "claude-fable-5-1"},
                  "state": "ok"}},
]}
MODELS = {"models": [
    {"backend": "claude", "model": "", "label": "Claude · default"},
    {"backend": "claude", "model": "claude-fable-5-1", "label": "Claude · Claude Fable 5.1"},
    {"backend": "claude", "model": "claude-haiku-4-5", "label": "Claude · claude-haiku-4-5"},
    {"backend": "gpt", "model": "gpt-5.6-luna", "label": "GPT · gpt-5.6-luna"},
]}


def _ns(serve_url, resolve_err=""):
    source = Path(__file__).parents[1] / "resources/backend/server.py"
    tree = ast.parse(source.read_text())
    audits = []

    async def _ac_resolve(vm):
        return (serve_url if not resolve_err else ""), "unit", "/ac/@%s/" % (vm or ""), (vm or "")

    async def _resolve_vm_name(v):
        v = (v or "").strip()
        if not v or v in ("@keeper", "@self"):
            return ""
        return v if v in ("hugpy", "hs-fresh") else None

    async def _rolling_state(app, vm):
        return {"ok": True, "locus": vm or "keeper", "seat_session": "keeper-claude", "busy": False,
                "idle_relaunch": {"minutes": 30, "would": False}}

    async def _ac_serve_cache_doc(base=None):
        return {"ok": True, "requests_total": 1234,
                "totals": {"total_tokens": 4443744, "cost_usd": 12.5}}

    ns = dict(re=re, json=json, web=web, aiohttp=aiohttp, asyncio=asyncio, AC_UPSTREAM=serve_url,
              _AC_DISCOVERED={"hugpy": {"error": "ssh forward refused"}},
              _ac_resolve=_ac_resolve, _resolve_vm_name=_resolve_vm_name, _rolling_state=_rolling_state,
              _ac_serve_cache_doc=_ac_serve_cache_doc, _station_locus=lambda: "keeper",
              _tmux_session_for=lambda s, b: "keeper-claude",
              audit=lambda req, action, detail="", ok=True: audits.append((action, detail, ok)))
    body = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in NAMES]
    assert {n.name for n in body} == NAMES
    exec(compile(ast.Module(body=body, type_ignores=[]), str(source), "exec"), ns)
    ns["_audits"] = audits
    return ns


def _run(fn):
    asyncio.run(fn())


def _fake_serve(seen, roster=ROSTER, fail=None):
    async def handler(req):
        rec = {"method": req.method, "path": req.path}
        if req.method == "POST":
            rec["body"] = await req.json()
        seen.append(rec)
        if fail and req.path == fail[0] and req.method == fail[1]:
            return web.Response(status=fail[2], text=fail[3], content_type=fail[4] if len(fail) > 4 else "text/plain")
        if req.path == "/api/session/roster":
            if req.method == "POST":
                b = rec["body"]
                if b["action"] == "set_model":
                    return web.json_response({"ok": True, "role": "keeper", "model": "claude-fable-5-1",
                                              "pending_model": b["model"], "staged": True,
                                              "session_id": "cs-keeper1", **roster})
                if b["action"] == "set_provider":
                    return web.json_response({"ok": True, "session_id": "cs-keeper1", "previous_session_id": "cs-keeper1", **roster})
            return web.json_response(roster)
        if req.path == "/api/console/sessions":
            return web.json_response(SESSIONS)
        if req.path == "/api/console/models":
            return web.json_response(MODELS)
        if req.path == "/api/usage/report":
            return web.json_response({"totals": {}, "recent": []})
        return web.json_response({"error": "nope"}, status=404)
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


async def _station(ns):
    app = web.Application()
    app.router.add_get("/api/locus/keeper", ns["api_locus_keeper"])
    app.router.add_post("/api/locus/keeper", ns["api_locus_keeper"])
    return app


def test_get_composes_keeper_from_roster_sessions_models():
    async def go():
        seen = []
        async with TestServer(_fake_serve(seen)) as remote:
            ns = _ns(str(remote.make_url("")).rstrip("/"))
            async with TestClient(TestServer(await _station(ns))) as c:
                r = await c.get("/api/locus/keeper?vm=hugpy")
                assert r.status == 200
                d = await r.json()
        assert d["ok"] is True and d["locus"] == "hugpy" and d["vm"] == "hugpy"
        assert d["serve"]["path"] == "/ac/@hugpy/" and d["serve"]["url"].startswith("http://")
        kp = d["keeper"]
        assert kp["session_id"] == "cs-keeper1" and kp["native_id"] == "ffa61f62-native"
        assert kp["backend"] == "claude" and kp["model"] == "claude-fable-5-1" and kp["pending_model"] == ""
        assert kp["label"] == "Keeper (fresh)" and kp["busy"] is True and kp["last_turn_ts"] == 1790886152.6
        assert kp["gauge"] == {"tokens": 57711, "window": 294000, "pct": 19.6,
                               "source": "claude:usage(input+cache_read+cache_creation)", "model": "claude-fable-5-1"}
        # the FULL catalog, nothing filtered to defaults, every entry tagged with its source
        assert [m["model"] for m in d["models"]] == ["", "claude-fable-5-1", "claude-haiku-4-5", "gpt-5.6-luna"]
        assert {m["source"] for m in d["models"]} == {"serve"} and d["models"][1]["label"] == "Claude · Claude Fable 5.1"
        # the cumulative relayed total is a SEPARATE number, never the gauge
        assert d["relayed"]["tokens"] == 4443744 and d["relayed"]["runs"] == 1234
        assert d["seat"]["emergency"] is True and d["seat"]["surface"] == "tmux" and d["seat"]["session"] == "keeper-claude"
        assert d["errors"] == []
        assert [s["path"] for s in seen][:3] == ["/api/session/roster", "/api/console/sessions", "/api/console/models"]
    _run(go)


def test_get_pending_model_and_host_locus():
    roster = json.loads(json.dumps(ROSTER))
    roster["roles"][0]["pending_model"] = "claude-opus-5"

    async def go():
        seen = []
        async with TestServer(_fake_serve(seen, roster=roster)) as remote:
            ns = _ns(str(remote.make_url("")).rstrip("/"))
            async with TestClient(TestServer(await _station(ns))) as c:
                d = await (await c.get("/api/locus/keeper?vm=@keeper")).json()
        assert d["ok"] and d["vm"] == "@self" and d["locus"] == "keeper"
        assert d["keeper"]["model"] == "claude-fable-5-1" and d["keeper"]["pending_model"] == "claude-opus-5"
    _run(go)


def test_post_forwards_set_model_and_returns_roster_status():
    async def go():
        seen = []
        async with TestServer(_fake_serve(seen)) as remote:
            ns = _ns(str(remote.make_url("")).rstrip("/"))
            async with TestClient(TestServer(await _station(ns))) as c:
                r = await c.post("/api/locus/keeper", json={"vm": "hugpy", "action": "set_model",
                                                            "model": "claude-opus-5", "by": "op"})
                assert r.status == 200
                d = await r.json()
        posts = [s for s in seen if s["method"] == "POST"]
        assert posts == [{"method": "POST", "path": "/api/session/roster",
                          "body": {"action": "set_model", "role": "keeper", "model": "claude-opus-5", "by": "op"}}]
        assert d["ok"] is True and d["staged"] is True and d["pending_model"] == "claude-opus-5"
        assert d["model"] == "claude-fable-5-1" and d["session_id"] == "cs-keeper1" and d["steps"] == ["set_model:claude-opus-5"]
        assert d["roster"]["roles"][0]["role"] == "keeper"
        assert ns["_audits"][0][0] == "keeper-model"
    _run(go)


def test_post_backend_change_sets_provider_first():
    async def go():
        seen = []
        async with TestServer(_fake_serve(seen)) as remote:
            ns = _ns(str(remote.make_url("")).rstrip("/"))
            async with TestClient(TestServer(await _station(ns))) as c:
                r = await c.post("/api/locus/keeper", json={"vm": "hugpy", "action": "set_model",
                                                            "model": "gpt-5.6-luna", "backend": "gpt"})
                d = await r.json()
                assert r.status == 200 and d["steps"] == ["set_provider:gpt", "set_model:gpt-5.6-luna"]
                seen.clear()
                # same backend as the roster's → no set_provider
                r = await c.post("/api/locus/keeper", json={"vm": "hugpy", "model": "claude-opus-5", "backend": "claude"})
                assert r.status == 200
        actions = [s["body"]["action"] for s in seen if s["method"] == "POST"]
        assert actions == ["set_model"]
    _run(go)


def test_errors_pass_the_upstream_text_through():
    async def go():
        # 1) the serve rejects set_model (400 with its own words)
        seen = []
        fail = ("/api/session/roster", "POST", 400, json.dumps({"ok": False, "error": "bad model id"}), "application/json")
        async with TestServer(_fake_serve(seen, fail=fail)) as remote:
            ns = _ns(str(remote.make_url("")).rstrip("/"))
            async with TestClient(TestServer(await _station(ns))) as c:
                r = await c.post("/api/locus/keeper", json={"vm": "hugpy", "model": "claude-opus-5"})
                assert r.status == 502
                d = await r.json()
                assert d["ok"] is False and d["error"] == "serve set_model: HTTP 400: bad model id" and d["step"] == "set_model"
                # 2) the roster GET fails with a plain-text 500 → the card gets the text, plus the seat
                r = await c.get("/api/locus/keeper?vm=hugpy")
        fail2 = ("/api/session/roster", "GET", 500, "roster.json unreadable")
        async with TestServer(_fake_serve(seen, fail=fail2)) as remote:
            ns = _ns(str(remote.make_url("")).rstrip("/"))
            async with TestClient(TestServer(await _station(ns))) as c:
                d = await (await c.get("/api/locus/keeper?vm=hugpy")).json()
                assert d["ok"] is False and d["error"] == "serve roster: HTTP 500: roster.json unreadable"
                assert d["keeper"] is None and d["seat"]["emergency"] is True
                # 3) one sub-call failing (models) is reported in errors, the keeper still renders
                fail3 = ("/api/console/models", "GET", 503, "models offline")
        async with TestServer(_fake_serve(seen, fail=fail3)) as remote:
            ns = _ns(str(remote.make_url("")).rstrip("/"))
            async with TestClient(TestServer(await _station(ns))) as c:
                d = await (await c.get("/api/locus/keeper?vm=hugpy")).json()
                assert d["ok"] is True and d["models"] == [] and d["errors"] == ["serve models: HTTP 503: models offline"]
        # 4) no serve reachable on the locus → the discovery error, 502 on POST, {ok:false} on GET
        ns = _ns("http://127.0.0.1:1", resolve_err="x")
        async with TestClient(TestServer(await _station(ns))) as c:
            d = await (await c.get("/api/locus/keeper?vm=hugpy")).json()
            assert d["ok"] is False and d["error"] == "no reachable serve on hugpy: ssh forward refused"
            r = await c.post("/api/locus/keeper", json={"vm": "hugpy", "model": "claude-opus-5"})
            assert r.status == 502 and (await r.json())["error"].startswith("no reachable serve on hugpy")
            # 5) unknown locus / bad input are refused before any upstream call
            r = await c.get("/api/locus/keeper?vm=nope")
            assert r.status == 404 and (await r.json()) == {"ok": False, "error": "unknown locus nope"}
            r = await c.post("/api/locus/keeper", json={"vm": "hugpy", "model": "bad id;rm"})
            assert r.status == 400 and (await r.json())["error"] == "bad model id"
            r = await c.post("/api/locus/keeper", json={"vm": "hugpy", "action": "archive"})
            assert r.status == 400
        # 6) a dead serve (connection refused) → the exception text, not a silent None
        ns = _ns("http://127.0.0.1:1")
        async with TestClient(TestServer(await _station(ns))) as c:
            d = await (await c.get("/api/locus/keeper?vm=hugpy")).json()
            assert d["ok"] is False and d["error"].startswith("serve roster: ClientConnectorError")
    _run(go)
