"""Durable Station provenance and log intersection ledger.

This is deliberately small and append-oriented.  It keeps the original agent
output/tool payload, the files/items mentioned by it, and findings that later
intersect those items.  The UI can use the same store for live provenance,
without depending on journald or a central API being available.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.RLock()
_DB = None
_PATH_RE = re.compile(r"(?<![\w-])(?:/[^\s'\"`:,;()\[\]{}]+)(?::(\d+))?")
_REL_RE = re.compile(r"(?:^|\s)([\w./-]+\.(?:py|js|jsx|ts|tsx|css|html|json|yaml|yml|toml|sh))(?::(\d+))?")


def db_path():
    state = Path(os.environ.get("HUGPY_STATION_STATE") or
                 Path.home() / ".config" / "hugpy-station")
    state.mkdir(parents=True, exist_ok=True)
    return state / "provenance.sqlite3"


def _connect():
    global _DB
    with _LOCK:
        if _DB is None:
            _DB = sqlite3.connect(str(db_path()), timeout=15, check_same_thread=False)
            _DB.row_factory = sqlite3.Row
            _DB.execute("PRAGMA journal_mode=WAL")
            _DB.execute("PRAGMA busy_timeout=15000")
            _DB.executescript("""
              CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                source TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT,
                session_id TEXT, trace_id TEXT, body TEXT NOT NULL,
                digest TEXT NOT NULL, parent_id INTEGER);
              CREATE TABLE IF NOT EXISTS items(
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
                item TEXT NOT NULL, line INTEGER, operation TEXT,
                digest TEXT, FOREIGN KEY(event_id) REFERENCES events(id));
              CREATE TABLE IF NOT EXISTS findings(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                event_id INTEGER, item_id INTEGER, severity TEXT, kind TEXT,
                signature TEXT, source TEXT, line TEXT, summary TEXT,
                status TEXT DEFAULT 'open', query_sent INTEGER DEFAULT 0,
                FOREIGN KEY(event_id) REFERENCES events(id));
              CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
              CREATE INDEX IF NOT EXISTS items_item ON items(item);
              CREATE INDEX IF NOT EXISTS findings_status ON findings(status);
            """)
            _DB.commit()
            # Existing Station installs get the relationship column without a
            # destructive migration.
            cols = {r[1] for r in _DB.execute("PRAGMA table_info(events)").fetchall()}
            if "parent_id" not in cols:
                _DB.execute("ALTER TABLE events ADD COLUMN parent_id INTEGER")
                _DB.commit()
        return _DB


def _text(body):
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False, default=str)


def items_from(body):
    text = _text(body)
    found = []
    for rx in (_PATH_RE, _REL_RE):
        for m in rx.finditer(text):
            value = m.group(0).strip(".,;)]}")
            line = None
            if m.lastindex and m.group(m.lastindex) and m.group(m.lastindex).isdigit():
                line = int(m.group(m.lastindex))
                value = value.rsplit(":", 1)[0]
            if value not in {x[0] for x in found}:
                found.append((value, line))
    return found[:200]


def record(kind, body, *, source="station", actor="", session_id="", trace_id="", operation="", parent_id=None):
    raw = _text(body)
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
    now = time.time()
    with _LOCK:
        db = _connect()
        cur = db.execute("INSERT INTO events(ts,source,kind,actor,session_id,trace_id,body,digest,parent_id) VALUES(?,?,?,?,?,?,?,?,?)",
                         (now, source, kind, actor, session_id, trace_id, raw, digest, parent_id))
        eid = cur.lastrowid
        for item, line in items_from(raw):
            db.execute("INSERT INTO items(event_id,item,line,operation,digest) VALUES(?,?,?,?,?)",
                       (eid, item, line, operation, hashlib.sha256(item.encode()).hexdigest()))
        db.commit()
    return eid


def record_log(doc):
    """Persist a log record and associate it with recent edits mentioning it."""
    eid = record("log", doc, source=doc.get("name") or "station-log", actor="runtime")
    text = str(doc.get("msg") or "")
    try:
        from log_findings import scan
        rows = scan(text, now=float(doc.get("ts") or time.time()))
    except Exception:
        rows = []
    with _LOCK:
        db = _connect()
        for row in rows:
            sig = str(row.get("signature") or row.get("key") or "")
            hit = db.execute("""SELECT i.id FROM items i JOIN events e ON e.id=i.event_id
                               WHERE e.kind IN ('agent_output','tool_call','edit')
                               AND (? LIKE '%' || i.item || '%' OR i.item LIKE '%' || ? || '%')
                               ORDER BY e.ts DESC LIMIT 1""", (text, text[:120])).fetchone()
            db.execute("""INSERT INTO findings(ts,event_id,item_id,severity,kind,signature,source,line,summary,query_sent)
                        VALUES(?,?,?,?,?,?,?,?,?,0)""",
                       (time.time(), eid, hit[0] if hit else None, row.get("severity"),
                        row.get("kind"), sig, doc.get("name"), text[:240], text[:1000]))
        db.commit()
    return eid


def rows(after=0, limit=300):
    with _LOCK:
        db = _connect()
        ev = [dict(x) for x in db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, min(int(limit), 2000))).fetchall()]
        for row in ev:
            row["body"] = json.loads(row["body"])
            row["items"] = [dict(x) for x in db.execute("SELECT item,line,operation FROM items WHERE event_id=?", (row["id"],)).fetchall()]
        fs = [dict(x) for x in db.execute("SELECT * FROM findings WHERE status='open' ORDER BY id DESC LIMIT 500").fetchall()]
        return {"events": ev, "findings": fs, "cursor": ev[-1]["id"] if ev else after}


def mark_query(finding_id):
    with _LOCK:
        db = _connect()
        db.execute("UPDATE findings SET query_sent=1 WHERE id=?", (int(finding_id),))
        db.commit()
