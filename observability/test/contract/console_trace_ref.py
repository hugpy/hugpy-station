"""console_trace v2 — REFERENCE implementation of the server half of the
Station observability contract (observability/README.md ("Server contract")).

UNAPPLIED reference: hugpy central owns its console_trace_routes.py. This file
is what the Station's contract stub (stub_server.py) runs, and is offered to
the hugpy keeper as a drop-in for py/services/hugpy_server/src/hugpy_server/
app/routes/console_trace_routes.py (same blueprint name, same
install_console_trace(app) entry point). It also runs without hugpy installed
(the hugpy imports fall back to plain Flask + an env path).

What changed against the live v1 module (2026-10-01):
  * writes go through ONE bounded queue drained by ONE writer thread per process,
    batched in a transaction with a long busy timeout and retries. A request never
    waits on SQLite. Anything that still fails is COUNTED (stats endpoint), not
    silently dropped. The store is bounded (keep newest N requests).
  * per-request ids: X-Hugpy-Trace-Id is one request; X-Hugpy-Trace-Session groups
    them. An id this process already used is suffixed, and the stored id is echoed
    in the X-Hugpy-Trace-Id response header.
  * real exception capture: sys.settrace (per thread, line events off) delivers
    'exception' events per frame; Flask's got_request_exception adds the full
    traceback of an unhandled raise to the request row.
  * targeted, cheap spans: X-Hugpy-Trace-Spans none|handler|app|all (default app)
    and X-Hugpy-Trace-Modules (module-name globs). Server-side env caps: allowed
    routes, rate limit, span cap.
  * the view is recorded directly (request.url_rule endpoint -> view function),
    not inferred from the span tree.
"""
from __future__ import annotations

import fnmatch
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from collections import OrderedDict

from flask import Response, g, jsonify, request, stream_with_context

try:  # inside hugpy
    from abstract_flask import get_bp
    trace_bp, logger = get_bp("console_trace_bp", __name__)
except Exception:  # noqa: BLE001 — standalone (contract stub / tests)
    import logging
    from flask import Blueprint
    trace_bp = Blueprint("console_trace_bp", __name__)
    logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SERVER_TAG = "console_trace/2"

H_ON = "X-Hugpy-Trace"
H_ID = "X-Hugpy-Trace-Id"
H_SESSION = "X-Hugpy-Trace-Session"
H_SPANS = "X-Hugpy-Trace-Spans"
H_MODULES = "X-Hugpy-Trace-Modules"
H_SERVER = "X-Hugpy-Trace-Server"

SPAN_LEVELS = ("none", "handler", "app", "all")
_ID_RE = re.compile(r"[^A-Za-z0-9._:~-]")
_LIB_RE = re.compile(r"/(site|dist)-packages/|/lib/python\d|^<frozen|^<string>")
_FLASK_APP_RE = re.compile(r"/flask/(app|sansio/app)\.py$")
# Flask frames kept at every span level: the dispatch and output path anchors.
FLASK_ANCHORS = frozenset({"dispatch_request", "make_response", "finalize_request", "process_response",
                           "handle_user_exception", "handle_exception", "handle_http_exception"})
_TRACE_PATH_PREFIXES = ("/console/trace", "/api/console/trace")
_SELF = os.path.abspath(__file__)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def enabled():
    return (os.environ.get("HUGPY_CONSOLE_TRACE", "1").strip().lower() not in ("0", "false", "off", "no"))


def trace_db_path():
    """The trace store: never the fleet/comms database itself."""
    env = (os.environ.get("HUGPY_CONSOLE_TRACE_DB") or "").strip()
    if env:
        return env
    try:
        from hugpy_control.shared import default_db_path
        base = os.path.dirname(default_db_path()) or "."
    except Exception:  # noqa: BLE001
        base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, "console_trace.sqlite3")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS console_trace_requests (
  id TEXT PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
  method TEXT NOT NULL, path TEXT NOT NULL, status INTEGER,
  duration_ms REAL, browser TEXT, error TEXT,
  session_id TEXT, requested_id TEXT, endpoint TEXT, view TEXT,
  exception TEXT, spans_level TEXT, span_count INTEGER, spans_truncated INTEGER,
  pid INTEGER, schema INTEGER
);
CREATE TABLE IF NOT EXISTS console_trace_spans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL,
  seq INTEGER NOT NULL, event TEXT NOT NULL, depth INTEGER NOT NULL,
  function TEXT NOT NULL, file TEXT, line INTEGER, at REAL NOT NULL,
  duration_ms REAL, error TEXT, module TEXT
);
CREATE TABLE IF NOT EXISTS console_trace_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, at REAL NOT NULL,
  kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_console_trace_spans_trace ON console_trace_spans(trace_id, seq);
CREATE INDEX IF NOT EXISTS ix_console_trace_requests_started ON console_trace_requests(started_at);
CREATE INDEX IF NOT EXISTS ix_console_trace_requests_session ON console_trace_requests(session_id, started_at);
"""
# v1 stores gain the v2 columns without a destructive migration.
_V2_COLUMNS = {"console_trace_requests": [("session_id", "TEXT"), ("requested_id", "TEXT"), ("endpoint", "TEXT"),
                                          ("view", "TEXT"), ("exception", "TEXT"), ("spans_level", "TEXT"),
                                          ("span_count", "INTEGER"), ("spans_truncated", "INTEGER"),
                                          ("pid", "INTEGER"), ("schema", "INTEGER")],
               "console_trace_spans": [("module", "TEXT")]}


def _connect(path, timeout):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return con


def _ensure_schema(con):
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    # the index on session_id needs the column first on a v1 store
    for table, cols in _V2_COLUMNS.items():
        try:
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            have = set()
        if have:
            for name, typ in cols:
                if name not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    con.executescript(_SCHEMA)
    con.commit()


# ───────────────────────────────────────────────────────────── the writer ──
class TraceWriter:
    """One per process: a bounded queue and a single daemon thread that batches
    writes. put() never blocks. Every item is written or counted as dropped."""

    def __init__(self, path, *, maxsize=None, keep=None, batch=200, retries=4, busy_timeout=5.0):
        self.path = path
        self.pid = os.getpid()
        self.q = queue.Queue(maxsize=maxsize or _env_int("HUGPY_CONSOLE_TRACE_QUEUE", 4000))
        self.keep = keep or _env_int("HUGPY_CONSOLE_TRACE_KEEP", 2000)
        self.batch = batch
        self.retries = retries
        self.busy_timeout = busy_timeout
        self.stats = {"enqueued": 0, "written": 0, "requests_written": 0, "spans_written": 0, "batches": 0,
                      "retries": 0, "pruned_requests": 0,
                      "dropped": {"queue_full": 0, "write_error": 0}, "last_error": None}
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._thread = threading.Thread(target=self._run, name="console-trace-writer", daemon=True)
        self._thread.start()

    def put(self, item):
        try:
            self.q.put_nowait(item)
            self._idle.clear()
            with self._lock:
                self.stats["enqueued"] += 1
            return True
        except queue.Full:
            with self._lock:
                self.stats["dropped"]["queue_full"] += 1
            return False

    def flush(self, timeout=10.0):
        """Wait until everything queued so far is written (tests, shutdown)."""
        end = time.time() + timeout
        while time.time() < end:
            if self.q.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    def snapshot(self):
        with self._lock:
            s = json.loads(json.dumps(self.stats))
        s.update({"queued": self.q.qsize(), "queue_max": self.q.maxsize, "keep": self.keep,
                  "pid": self.pid, "db": self.path})
        try:
            s["db_bytes"] = os.path.getsize(self.path)
        except OSError:
            s["db_bytes"] = None
        return s

    # — thread side —
    def _run(self):
        con = None
        since_prune = 0
        while True:
            items = [self.q.get()]
            while len(items) < self.batch:
                try:
                    items.append(self.q.get_nowait())
                except queue.Empty:
                    break
            try:
                if con is None:
                    con = _connect(self.path, self.busy_timeout)
                    _ensure_schema(con)
                self._write(con, items)
                since_prune += len(items)
                if since_prune >= 200:
                    since_prune = 0
                    self._prune(con)
            except Exception as exc:  # noqa: BLE001 — counted, never raised
                with self._lock:
                    self.stats["dropped"]["write_error"] += len(items)
                    self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("console trace: %d item(s) not written: %s", len(items), exc)
                try:
                    if con is not None:
                        con.close()
                except Exception:  # noqa: BLE001
                    pass
                con = None
            finally:
                for _ in items:
                    self.q.task_done()

    def _write(self, con, items):
        delay = 0.05
        for attempt in range(self.retries + 1):
            try:
                n_req = n_spans = 0
                with con:  # one transaction per batch
                    for kind, data in items:
                        if kind == "start":
                            con.execute("INSERT OR IGNORE INTO console_trace_requests "
                                        "(id,started_at,method,path,browser,session_id,requested_id,pid,schema) "
                                        "VALUES (:id,:started_at,:method,:path,:browser,:session_id,:requested_id,:pid,2)",
                                        data)
                        elif kind == "finish":
                            row, spans, events = data
                            con.execute("INSERT OR REPLACE INTO console_trace_requests "
                                        "(id,started_at,finished_at,method,path,status,duration_ms,browser,error,"
                                        "session_id,requested_id,endpoint,view,exception,spans_level,span_count,"
                                        "spans_truncated,pid,schema) VALUES (:id,:started_at,:finished_at,:method,"
                                        ":path,:status,:duration_ms,:browser,:error,:session_id,:requested_id,"
                                        ":endpoint,:view,:exception,:spans_level,:span_count,:spans_truncated,:pid,2)",
                                        row)
                            con.execute("DELETE FROM console_trace_spans WHERE trace_id=?", (row["id"],))
                            con.executemany("INSERT INTO console_trace_spans (trace_id,seq,event,depth,function,file,"
                                            "line,at,duration_ms,error,module) VALUES (?,?,?,?,?,?,?,?,?,?,?)", spans)
                            con.executemany("INSERT INTO console_trace_events(trace_id,at,kind,payload) "
                                            "VALUES (?,?,?,?)", events)
                            n_req += 1
                            n_spans += len(spans)
                        elif kind == "event":
                            con.execute("INSERT INTO console_trace_events(trace_id,at,kind,payload) VALUES (?,?,?,?)",
                                        data)
                with self._lock:
                    self.stats["written"] += len(items)
                    self.stats["requests_written"] += n_req
                    self.stats["spans_written"] += n_spans
                    self.stats["batches"] += 1
                return
            except sqlite3.OperationalError as exc:
                if attempt >= self.retries or "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                with self._lock:
                    self.stats["retries"] += 1
                time.sleep(delay)
                delay = min(delay * 2, 1.0)

    def _prune(self, con):
        """Bounded store: keep the newest `keep` requests (and their spans)."""
        cut = con.execute("SELECT started_at FROM console_trace_requests ORDER BY started_at DESC "
                          "LIMIT 1 OFFSET ?", (self.keep,)).fetchone()
        if not cut:
            return
        with con:
            old = [r[0] for r in con.execute("SELECT id FROM console_trace_requests WHERE started_at<=?", (cut[0],))]
            con.executemany("DELETE FROM console_trace_spans WHERE trace_id=?", [(i,) for i in old])
            con.execute("DELETE FROM console_trace_requests WHERE started_at<=?", (cut[0],))
            con.execute("DELETE FROM console_trace_events WHERE id <= (SELECT MAX(id) FROM console_trace_events) - ?",
                        (self.keep * 5,))
        with self._lock:
            self.stats["pruned_requests"] += len(old)


_writer = None
_writer_lock = threading.Lock()


def writer():
    """The process's writer (re-created after a fork: gunicorn workers)."""
    global _writer
    w = _writer
    if w is None or w.pid != os.getpid() or w.path != trace_db_path():
        with _writer_lock:
            w = _writer
            if w is None or w.pid != os.getpid() or w.path != trace_db_path():
                _writer = w = TraceWriter(trace_db_path())
    return w


def _read_db():
    con = _connect(trace_db_path(), 5.0)
    _ensure_schema(con)
    return con


# ─────────────────────────────────────────────────────────── the tracer ──
class _RateLimit:
    def __init__(self):
        self.lock = threading.Lock()
        self.tokens = None
        self.last = time.monotonic()

    def allow(self):
        rate = _env_int("HUGPY_CONSOLE_TRACE_RATE", 50)
        if rate <= 0:
            return True
        with self.lock:
            now = time.monotonic()
            if self.tokens is None:
                self.tokens = float(rate)
            self.tokens = min(float(rate), self.tokens + (now - self.last) * rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


_rate = _RateLimit()
_recent_ids = OrderedDict()
_recent_lock = threading.Lock()


def _unique_id(raw):
    tid = _ID_RE.sub("", (raw or "").strip())[:96] or uuid.uuid4().hex
    with _recent_lock:
        if tid in _recent_ids:
            tid = f"{tid[:84]}~{uuid.uuid4().hex[:8]}"
        _recent_ids[tid] = True
        while len(_recent_ids) > 5000:
            _recent_ids.popitem(last=False)
    return tid


def _clean(value, limit=4096):
    return str(value or "")[:limit]


def _is_app_file(file):
    return bool(file) and not _LIB_RE.search(file) and os.path.abspath(file) != _SELF


class _Tracer:
    """Per-request, per-thread sys.settrace tracer. Line events are switched off
    on every traced frame, so a traced frame costs a call/return/exception
    callback and an untraced frame costs one global callback."""

    def __init__(self, level, modules, view_code, max_spans):
        self.level = level
        self.modules = modules
        self.view_code = view_code
        self.max_spans = max_spans
        self.spans = []
        self.stack = []        # (frame id, start time)
        self.truncated = 0
        self.raised = {}       # frame id -> last error text (for 'return' after an exception)

    def _wanted(self, frame):
        code = frame.f_code
        file = code.co_filename
        if self.level == "all":
            return os.path.abspath(file) != _SELF
        if code.co_name in FLASK_ANCHORS and _FLASK_APP_RE.search(file or ""):
            return True
        if self.level == "handler":
            return code is self.view_code
        if not _is_app_file(file):
            return False
        if self.modules:
            mod = frame.f_globals.get("__name__", "")
            return any(fnmatch.fnmatchcase(mod, m) for m in self.modules)
        return True

    def _add(self, event, frame, depth, **extra):
        if len(self.spans) >= self.max_spans:
            self.truncated += 1
            return
        code = frame.f_code
        self.spans.append({"event": event, "depth": depth, "function": code.co_name, "file": code.co_filename,
                           "line": frame.f_lineno, "module": frame.f_globals.get("__name__", ""),
                           "at": time.time(), **extra})

    def global_trace(self, frame, event, arg):
        if event != "call" or not self._wanted(frame):
            return None
        frame.f_trace_lines = False
        self._add("call", frame, len(self.stack))
        self.stack.append((id(frame), time.perf_counter()))
        return self.local_trace

    def local_trace(self, frame, event, arg):
        if event == "exception":
            exc_type, exc, _tb = arg
            err = f"{getattr(exc_type, '__name__', exc_type)}: {_clean(exc, 500)}"
            self.raised[id(frame)] = err
            self._add("exception", frame, max(0, len(self.stack) - 1), error=err)
            # a 'line' event after this means the frame HANDLED it; switch line
            # events on just until we know (off again below)
            frame.f_trace_lines = True
        elif event == "line":
            frame.f_trace_lines = False
            if self.raised.pop(id(frame), None) is not None and self.spans and len(self.spans) < self.max_spans:
                self.spans.append({"event": "handled", "depth": max(0, len(self.stack) - 1),
                                   "function": frame.f_code.co_name, "file": frame.f_code.co_filename,
                                   "line": frame.f_lineno, "module": frame.f_globals.get("__name__", ""),
                                   "at": time.time()})
        elif event == "return":
            started = None
            if self.stack and self.stack[-1][0] == id(frame):
                _, started = self.stack.pop()
            err = self.raised.pop(id(frame), None)
            extra = {"duration_ms": (time.perf_counter() - started) * 1000 if started else None}
            if err is not None:      # exception event, no handler line since: it unwound this frame
                extra["error"] = "unwound: " + err
            self._add("return", frame, len(self.stack), **extra)
        return self.local_trace


def _view_info(app):
    try:
        ep = request.endpoint
        fn = app.view_functions.get(ep) if ep else None
        fn = getattr(fn, "__wrapped__", fn)
        code = getattr(fn, "__code__", None)
        if not code:
            return ep, None, None
        return ep, {"function": code.co_name, "module": getattr(fn, "__module__", ""),
                    "file": code.co_filename, "line": code.co_firstlineno}, code
    except Exception:  # noqa: BLE001
        return None, None, None


def _route_allowed(path):
    globs = [x.strip() for x in (os.environ.get("HUGPY_CONSOLE_TRACE_ROUTES") or "").split(",") if x.strip()]
    return not globs or any(fnmatch.fnmatchcase(path, x) for x in globs)


def install_console_trace(app):
    """Install request hooks once. With HUGPY_CONSOLE_TRACE=0 nothing is installed."""
    if app.extensions.get("console_trace") or not enabled():
        return
    max_spans = _env_int("HUGPY_CONSOLE_TRACE_MAX_SPANS", 5000)

    @app.before_request
    def _trace_before():
        if request.headers.get(H_ON) != "1" or request.path.startswith(_TRACE_PATH_PREFIXES):
            return
        if not _route_allowed(request.path):
            g._ct_skip = "route"
            return
        if not _rate.allow():
            g._ct_skip = "rate"
            return
        requested = _clean(request.headers.get(H_ID), 96)
        tid = _unique_id(requested)
        level = (request.headers.get(H_SPANS) or "app").strip().lower()
        level = level if level in SPAN_LEVELS else "app"
        modules = [m.strip() for m in (request.headers.get(H_MODULES) or "").split(",") if m.strip()][:20]
        endpoint, view, view_code = _view_info(app)
        state = {"id": tid, "requested": requested or None, "started": time.time(), "t0": time.perf_counter(),
                 "session": _clean(request.headers.get(H_SESSION), 96) or None, "level": level,
                 "endpoint": endpoint, "view": view, "exception": None, "status": None, "tracer": None,
                 "prev_trace": None}
        g._ct = state
        writer().put(("start", {"id": tid, "started_at": state["started"], "method": request.method,
                                "path": request.path, "browser": _clean(request.headers.get("User-Agent"), 512),
                                "session_id": state["session"], "requested_id": state["requested"],
                                "pid": os.getpid()}))
        if level != "none":
            prev = sys.gettrace()
            if prev is not None:      # a debugger/coverage owns the tracer: never fight it
                state["level"] = f"{level}:skipped(tracer-busy)"
            else:
                tr = _Tracer(level, modules, view_code, max_spans)
                state["tracer"] = tr
                sys.settrace(tr.global_trace)

    @app.after_request
    def _trace_after(response):
        state = getattr(g, "_ct", None)
        skip = getattr(g, "_ct_skip", None)
        if state is not None:
            state["status"] = response.status_code
            response.headers[H_ID] = state["id"]
            response.headers[H_SERVER] = f"{SERVER_TAG}; spans={state['level']}"
        elif skip:
            response.headers[H_SERVER] = f"{SERVER_TAG}; skipped={skip}"
        return response

    def _on_exception(sender, exception, **_kw):
        state = getattr(g, "_ct", None)
        if state is None or exception is None:
            return
        tb = traceback.extract_tb(exception.__traceback__)
        state["exception"] = {"type": type(exception).__name__, "message": _clean(exception, 2000),
                              "traceback": [{"function": f.name, "file": f.filename, "line": f.lineno,
                                             "code": _clean(f.line, 200), "app": _is_app_file(f.filename)}
                                            for f in tb][-40:]}

    try:
        from flask import got_request_exception
        got_request_exception.connect(_on_exception, app, weak=False)
    except Exception:  # noqa: BLE001 — no blinker: teardown still records the error text
        pass

    @app.teardown_request
    def _trace_teardown(exc):
        state = getattr(g, "_ct", None)
        if state is None:
            return
        g._ct = None
        tr = state["tracer"]
        if tr is not None and sys.gettrace() == tr.global_trace:
            sys.settrace(None)
        if exc is not None and state["exception"] is None:
            _on_exception(None, exc)
        finished = time.time()
        duration = (time.perf_counter() - state["t0"]) * 1000
        spans = tr.spans if tr else []
        status = state["status"] if state["status"] is not None else (500 if exc is not None else None)
        exc_doc = state["exception"]
        row = {"id": state["id"], "started_at": state["started"], "finished_at": finished,
               "method": request.method, "path": request.path, "status": status, "duration_ms": duration,
               "browser": _clean(request.headers.get("User-Agent"), 512),
               "error": (f"{exc_doc['type']}: {exc_doc['message']}" if exc_doc else None),
               "session_id": state["session"], "requested_id": state["requested"],
               "endpoint": state["endpoint"], "view": json.dumps(state["view"]) if state["view"] else None,
               "exception": json.dumps(exc_doc) if exc_doc else None, "spans_level": state["level"],
               "span_count": len(spans), "spans_truncated": tr.truncated if tr else 0, "pid": os.getpid()}
        span_rows = [(state["id"], n, s["event"], s["depth"], s["function"], s["file"], s["line"], s["at"],
                      s.get("duration_ms"), s.get("error"), s.get("module")) for n, s in enumerate(spans)]
        meta = {"method": request.method, "path": request.path, "status": status, "duration_ms": duration,
                "spans": len(spans), "session_id": state["session"], "error": row["error"]}
        writer().put(("finish", (row, span_rows, [(state["id"], finished, "flask.response", json.dumps(meta))])))

    app.extensions["console_trace"] = True


# ───────────────────────────────────────────────────────────── read side ──
def _request_doc(r):
    d = dict(r)
    for k in ("view", "exception"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


@trace_bp.route("/console/trace/events", methods=["POST"])
def trace_event():
    body = request.get_json(silent=True) or {}
    kind = _clean(body.get("kind"), 64) or "browser"
    payload = body.get("payload") if isinstance(body, dict) else {}
    raw = json.dumps(payload if isinstance(payload, dict) else {"value": str(payload)}, default=str)
    queued = writer().put(("event", (_clean(body.get("trace_id"), 96) or None, time.time(), kind, raw[:4096])))
    return jsonify({"ok": True, "queued": queued})


@trace_bp.route("/console/trace", methods=["GET"])
def trace_list():
    trace_id = request.args.get("trace_id")
    session_id = request.args.get("session_id")
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except ValueError:
        limit = 100
    con = _read_db()
    try:
        if trace_id:
            req = con.execute("SELECT * FROM console_trace_requests WHERE id=?", (trace_id,)).fetchone()
            spans = con.execute("SELECT seq,event,depth,function,module,file,line,at,duration_ms,error "
                                "FROM console_trace_spans WHERE trace_id=? ORDER BY seq", (trace_id,)).fetchall()
            doc = _request_doc(req) if req else None
            return jsonify({"schema": SCHEMA_VERSION, "request": doc, "spans": [dict(x) for x in spans],
                            "pending": bool(doc and doc.get("finished_at") is None)})
        if session_id:
            rows = con.execute("SELECT * FROM console_trace_requests WHERE session_id=? ORDER BY started_at DESC "
                               "LIMIT ?", (session_id, limit)).fetchall()
        else:
            rows = con.execute("SELECT * FROM console_trace_requests ORDER BY started_at DESC LIMIT ?",
                               (limit,)).fetchall()
        return jsonify({"schema": SCHEMA_VERSION, "requests": [_request_doc(x) for x in rows]})
    finally:
        con.close()


@trace_bp.route("/console/trace/stats", methods=["GET"])
def trace_stats():
    """This process's writer counters (gunicorn: one writer per worker)."""
    return jsonify({"schema": SCHEMA_VERSION, "enabled": enabled(), **writer().snapshot()})


@trace_bp.route("/console/trace/stream", methods=["GET"])
def trace_stream():
    since = int(request.args.get("since", 0) or 0)

    def generate():
        cursor = since
        deadline = time.time() + 3600
        while time.time() < deadline:
            con = _read_db()
            rows = con.execute("SELECT id,trace_id,at,kind,payload FROM console_trace_events WHERE id>? "
                               "ORDER BY id LIMIT 100", (cursor,)).fetchall()
            con.close()
            if rows:
                for r in rows:
                    cursor = r[0]
                    yield "data: " + json.dumps({"id": r[0], "trace_id": r[1], "at": r[2], "kind": r[3],
                                                 "payload": json.loads(r[4])}, default=str) + "\n\n"
            else:
                yield ": keepalive\n\n"
            time.sleep(0.5)
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
