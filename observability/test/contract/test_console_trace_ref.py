"""Tests for the console_trace v2 reference (the contract the Station relies on).

    /srv/hugpy/venv/bin/python -m pytest -q station-app/observability/test/contract
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import threading
import time

import pytest
from flask import Flask, jsonify

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def ct(monkeypatch, tmp_path):
    monkeypatch.setenv("HUGPY_CONSOLE_TRACE_DB", str(tmp_path / "trace.sqlite3"))
    for k in ("HUGPY_CONSOLE_TRACE", "HUGPY_CONSOLE_TRACE_RATE", "HUGPY_CONSOLE_TRACE_ROUTES",
              "HUGPY_CONSOLE_TRACE_KEEP", "HUGPY_CONSOLE_TRACE_QUEUE"):
        monkeypatch.delenv(k, raising=False)
    import console_trace_ref
    mod = importlib.reload(console_trace_ref)
    return mod


def _explode():
    raise ValueError("deliberate")


def _middle():
    return _explode()


def _helper(n):
    return list(range(n))


def make_app(ct):
    app = Flask(__name__)
    ct.install_console_trace(app)
    app.register_blueprint(ct.trace_bp, url_prefix="/api")

    @app.get("/work")
    def work():
        return jsonify({"n": len(_helper(5))})

    @app.post("/save")
    def save():
        return jsonify({"ok": True}), 201

    @app.get("/boom")
    def boom():
        return _middle()

    @app.get("/handled")
    def handled():
        try:
            _explode()
        except ValueError:
            pass
        return jsonify({"ok": True})

    return app


def H(tid, **kw):
    h = {"X-Hugpy-Trace": "1", "X-Hugpy-Trace-Id": tid, "X-Hugpy-Trace-Session": "sess-1"}
    h.update(kw)
    return h


def get_trace(client, tid):
    return client.get(f"/api/console/trace?trace_id={tid}").get_json()


def test_per_request_ids_session_and_echo(ct):
    app = make_app(ct)
    c = app.test_client()
    r1 = c.get("/work", headers=H("req-1"))
    r2 = c.get("/work", headers=H("req-2"))
    assert r1.headers["X-Hugpy-Trace-Id"] == "req-1" and r2.headers["X-Hugpy-Trace-Id"] == "req-2"
    assert r1.headers["X-Hugpy-Trace-Server"].startswith("console_trace/2")
    r3 = c.get("/work", headers=H("req-1"))                      # a reused id is never merged
    reused = r3.headers["X-Hugpy-Trace-Id"]
    assert reused != "req-1" and reused.startswith("req-1~")
    assert ct.writer().flush()
    for tid in ("req-1", "req-2", reused):
        d = get_trace(c, tid)
        assert d["schema"] == 2 and d["request"]["id"] == tid and d["request"]["status"] == 200
        assert d["request"]["session_id"] == "sess-1"
        assert d["request"]["view"]["function"] == "work"
        assert d["pending"] is False
    rows = c.get("/api/console/trace?session_id=sess-1").get_json()["requests"]
    assert len(rows) == 3
    assert get_trace(c, reused)["request"]["requested_id"] == "req-1"


def test_untraced_request_costs_nothing(ct, monkeypatch):
    app = make_app(ct)
    made = []
    monkeypatch.setattr(ct, "TraceWriter", lambda *a, **k: made.append(1))
    ct._writer = None
    r = app.test_client().get("/work")
    assert r.status_code == 200 and "X-Hugpy-Trace-Server" not in r.headers
    assert made == [] and sys.gettrace() is None


def test_disabled_installs_nothing(ct, monkeypatch):
    monkeypatch.setenv("HUGPY_CONSOLE_TRACE", "0")
    app = Flask(__name__)
    ct.install_console_trace(app)
    assert not app.before_request_funcs and "console_trace" not in app.extensions


def test_real_exception_capture(ct):
    app = make_app(ct)
    c = app.test_client()
    r = c.get("/boom", headers=H("boom-1"))
    assert r.status_code == 500
    assert ct.writer().flush()
    d = get_trace(c, "boom-1")
    req = d["request"]
    assert req["status"] == 500 and req["error"] == "ValueError: deliberate"
    tb = req["exception"]["traceback"]
    assert req["exception"]["type"] == "ValueError"
    assert [f["function"] for f in tb if f["app"]][-3:] == ["boom", "_middle", "_explode"]
    exc = [s for s in d["spans"] if s["event"] == "exception"]
    assert [s["function"] for s in exc][:3] == ["_explode", "_middle", "boom"]
    unwound = [s["function"] for s in d["spans"] if s["event"] == "return" and (s["error"] or "").startswith("unwound")]
    assert {"_explode", "_middle", "boom"} <= set(unwound)
    assert any(s["function"] == "handle_user_exception" for s in d["spans"] if s["event"] == "call")
    assert sys.gettrace() is None


def test_handled_exception_is_not_an_error(ct):
    app = make_app(ct)
    c = app.test_client()
    assert c.get("/handled", headers=H("h-1")).status_code == 200
    assert ct.writer().flush()
    d = get_trace(c, "h-1")
    assert d["request"]["exception"] is None and d["request"]["error"] is None
    kinds = [(s["event"], s["function"]) for s in d["spans"]]
    assert ("exception", "_explode") in kinds and ("handled", "handled") in kinds
    ret = [s for s in d["spans"] if s["event"] == "return" and s["function"] == "handled"][0]
    assert ret["error"] is None


def test_span_levels_and_module_targeting(ct):
    app = make_app(ct)
    c = app.test_client()
    c.get("/work", headers=H("lv-app"))
    c.get("/work", headers=H("lv-handler", **{"X-Hugpy-Trace-Spans": "handler"}))
    c.get("/work", headers=H("lv-none", **{"X-Hugpy-Trace-Spans": "none"}))
    c.get("/work", headers=H("lv-mod", **{"X-Hugpy-Trace-Modules": "nomatch.*"}))
    assert ct.writer().flush()
    app_fns = {s["function"] for s in get_trace(c, "lv-app")["spans"]}
    assert {"work", "_helper", "dispatch_request", "finalize_request"} <= app_fns
    h_fns = {s["function"] for s in get_trace(c, "lv-handler")["spans"]}
    assert "work" in h_fns and "_helper" not in h_fns and "dispatch_request" in h_fns
    none = get_trace(c, "lv-none")
    assert none["spans"] == [] and none["request"]["spans_level"] == "none" and none["request"]["status"] == 200
    m_fns = {s["function"] for s in get_trace(c, "lv-mod")["spans"]}
    assert "work" not in m_fns and "dispatch_request" in m_fns


def test_twenty_rapid_concurrent_requests_all_written(ct):
    app = make_app(ct)
    ids = [f"burst-{i}" for i in range(20)]

    def go(tid):
        app.test_client().get("/work", headers=H(tid))
    ts = [threading.Thread(target=go, args=(t,)) for t in ids]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert ct.writer().flush()
    c = app.test_client()
    for tid in ids:
        d = get_trace(c, tid)
        assert d["request"]["status"] == 200 and d["request"]["view"]["function"] == "work"
        assert any(s["function"] == "_helper" for s in d["spans"]), tid
    st = c.get("/api/console/trace/stats").get_json()
    assert st["dropped"] == {"queue_full": 0, "write_error": 0}
    assert st["requests_written"] >= 20


def test_locked_db_never_blocks_and_loses_nothing(ct):
    app = make_app(ct)
    c = app.test_client()
    c.get("/work", headers=H("warm"))
    assert ct.writer().flush()
    holder = sqlite3.connect(ct.trace_db_path(), timeout=0)
    holder.execute("BEGIN EXCLUSIVE")
    t0 = time.time()
    for i in range(10):
        assert c.get("/work", headers=H(f"locked-{i}")).status_code == 200
    assert time.time() - t0 < 1.0, "requests waited on the trace store"
    time.sleep(1.5)
    holder.rollback()
    holder.close()
    assert ct.writer().flush(15)
    for i in range(10):
        assert get_trace(c, f"locked-{i}")["request"]["status"] == 200
    st = ct.writer().snapshot()
    assert st["dropped"] == {"queue_full": 0, "write_error": 0}


def test_queue_full_is_counted_not_silent(ct, monkeypatch):
    monkeypatch.setenv("HUGPY_CONSOLE_TRACE_QUEUE", "2")
    ct._writer = None
    w = ct.writer()
    holder = sqlite3.connect(ct.trace_db_path(), timeout=0)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("CREATE TABLE IF NOT EXISTS x(a)")
        holder.execute("BEGIN EXCLUSIVE")
        results = [w.put(("event", (None, time.time(), "k", "{}"))) for _ in range(50)]
    finally:
        holder.rollback()
        holder.close()
    assert results.count(False) > 0
    assert w.snapshot()["dropped"]["queue_full"] == results.count(False)


def test_store_is_bounded(ct, monkeypatch):
    monkeypatch.setenv("HUGPY_CONSOLE_TRACE_KEEP", "5")
    ct._writer = None
    app = make_app(ct)
    c = app.test_client()
    for i in range(120):
        c.get("/work", headers=H(f"b-{i}"))
    assert ct.writer().flush()
    ct.writer()._prune(sqlite3.connect(ct.trace_db_path()))
    con = sqlite3.connect(ct.trace_db_path())
    n = con.execute("SELECT COUNT(*) FROM console_trace_requests").fetchone()[0]
    orphans = con.execute("SELECT COUNT(*) FROM console_trace_spans WHERE trace_id NOT IN "
                          "(SELECT id FROM console_trace_requests)").fetchone()[0]
    assert n <= 5 and orphans == 0


def test_rate_limit_and_route_allowlist(ct, monkeypatch):
    monkeypatch.setenv("HUGPY_CONSOLE_TRACE_RATE", "3")
    app = make_app(ct)
    c = app.test_client()
    heads = [c.get("/work", headers=H(f"r-{i}")).headers.get("X-Hugpy-Trace-Server", "") for i in range(10)]
    assert sum("skipped=rate" in h for h in heads) >= 5
    monkeypatch.setenv("HUGPY_CONSOLE_TRACE_RATE", "0")
    monkeypatch.setenv("HUGPY_CONSOLE_TRACE_ROUTES", "/save")
    assert "skipped=route" in c.get("/work", headers=H("rt-1")).headers["X-Hugpy-Trace-Server"]
    assert "spans=app" in c.post("/save", headers=H("rt-2")).headers["X-Hugpy-Trace-Server"]


def test_existing_tracer_is_never_replaced(ct):
    app = make_app(ct)
    seen = []

    def mine(frame, event, arg):
        seen.append(event)
        return None
    sys.settrace(mine)
    try:
        r = app.test_client().get("/work", headers=H("busy-1"))
        assert sys.gettrace() is mine
    finally:
        sys.settrace(None)
    assert r.headers["X-Hugpy-Trace-Server"].endswith("skipped(tracer-busy)")
