"""Durable MCT broker and transcript adapters. No terminal output is rendered.

The adapter uses the existing native seat's input channel and exact open
transcript. SQLite stores original objects, raw events, ordering, and outbox
state. Display events exclude protocol text, system instructions, and reasoning.

Pointer exchange, both directions (mct v2, 2026-09-15):
  operator -> seat   Store.submit  mints prompt/attachment/context objects; the
                     Broker types ONE line into the native seat:
                       MCT context mct://<scope>/<turn>/<ctx>. ...
  seat -> operator   Store.publish (CLI: `mct-push`) stores the reply/artifact
                     as an immutable object bound to the turn and the seat ends
                     its turn with ONE line:
                       MCT response mct://<scope>/<turn>/<obj>
                     The station renders the published object from the ledger;
                     the pointer line itself is archived raw, never displayed.
"""
import argparse
import asyncio
import base64
import calendar
from contextlib import contextmanager
import hashlib
import gzip
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import tempfile
import uuid

NATIVE_SEATS = {"codex": "keeper-codex", "claude-code": "keeper-claude"}
HANDLE = re.compile(r"^mct://([a-f0-9]{16})/([a-f0-9]{32})/([a-f0-9]{32})$")
CONTEXT_IN_TEXT = re.compile(r"MCT context (mct://[a-f0-9]{16}/([a-f0-9]{32})/[a-f0-9]{32})(?=\.|\s|$)")
# The reply-as-pointer convention: the seat's final chat message is exactly
# this one line (the pointer `mct-push` printed). Anything else is a plain reply.
RESPONSE_IN_TEXT = re.compile(r"MCT response (mct://[a-f0-9]{16}/([a-f0-9]{32})/([a-f0-9]{32}))(?=\.|\s|$)")
PUBLISH_KINDS = ("response", "artifact")
# Harness envelopes: content Claude Code / Codex inject into the transcript in
# the USER role that the operator never typed. Classified as kind "harness"
# (never "user"), never minting a turn. The raw record is archived untouched.
HARNESS_ENVELOPES = (
    (re.compile(r"^\s*(\[SYSTEM NOTIFICATION[^\]]*\]\s*)?<task-notification>", re.I), "task-notification"),
    (re.compile(r"^\s*\[SYSTEM NOTIFICATION[^\]]*\]", re.I), "system-notification"),
    (re.compile(r"^\s*<system-reminder>", re.I), "system-reminder"),
    (re.compile(r"^\s*<cross-session-message", re.I), "cross-session"),
    (re.compile(r"^\s*<(local-command-caveat|local-command-stdout|local-command-stderr|command-name|command-message|command-args)>", re.I), "local-command"),
    (re.compile(r"^\s*\[Request interrupted by user", re.I), "interrupt"),
    (re.compile(r"^\s*Your claude\.ai usage limit has reset", re.I), "usage-reset"),
    (re.compile(r"^\s*<(environment_context|user_context|turn_context)>", re.I), "environment"),
)
SYSTEM_REMINDER_BLOCK = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S | re.I)
QUEUED_MESSAGE_WRAPPER = re.compile(r"^\s*The user sent (?:a new|the following) message[^:\n]*:\s*", re.I)
_TAG = lambda name, text: (re.search(r"<%s>(.*?)</%s>" % (name, name), text, re.S) or [None, None])[1]
# Token economy (board t274): harness notices, subagent result blobs and tool
# output are recorded as BOUNDED excerpts. Nothing is lost - the untouched
# provider record always stays in the raw `records` archive, and a trimmed
# detail carries detail["record"] = "<source>:<offset>" as its pointer. Keys
# never change, so the wire schema and the renderer are unaffected.
HARNESS_EXCERPT = int(os.environ.get("MCT_HARNESS_EXCERPT", "600"))
TOOL_EXCERPT = int(os.environ.get("MCT_TOOL_EXCERPT", "2000"))


def bound(value, limit):
    """(value, trimmed) - `value` untouched when it is small, otherwise a
    bounded string excerpt of it. Only oversized bodies are replaced."""
    if value is None or isinstance(value, (int, float, bool)):
        return value, 0
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value, 0
    trimmed = len(text) - limit
    return text[:limit].rstrip() + "\n\u2026 [%d more characters trimmed; the untouched record is in the MCT raw archive]" % trimmed, trimmed


def classify_user_text(text):
    """(kind, text, detail) for a user-role transcript message.

    Rule: a genuine operator message is a user-role message that carries NO
    harness envelope (the operator's own MCT prompts are minted by
    Store.submit and arrive as `MCT context …` pointer lines). Text that
    starts with a harness envelope is kind "harness" with its source; a
    <system-reminder> block appended to a real message is stripped and the
    remainder stays "user"; a queued-message wrapper is unwrapped.
    """
    raw = text if isinstance(text, str) else ""
    stripped = SYSTEM_REMINDER_BLOCK.sub("", raw)
    body = QUEUED_MESSAGE_WRAPPER.sub("", stripped, count=1)
    for pattern, source in HARNESS_ENVELOPES:
        if pattern.search(body):
            # t274: a bounded excerpt, not the whole notice - a completed
            # subagent's <result> blob must not ride into the operator's view.
            shown, trimmed = bound(raw, HARNESS_EXCERPT)
            detail = {"source": source, "text": shown}
            if trimmed:
                detail["trimmed"] = trimmed
            label = "harness: " + source
            if source == "task-notification":
                detail.update({k: (_TAG(t, body) or "").strip() for k, t in (("task_id", "task-id"), ("status", "status"), ("summary", "summary"))})
                label = "subagent " + (detail.get("status") or "finished") + ": " + (detail.get("summary") or detail.get("task_id") or "")
            elif source == "local-command":
                name = (_TAG("command-name", body) or "").strip()
                label = "local command" + (" " + name if name else "")
            elif source == "interrupt":
                label = "request interrupted by the operator"
            elif source == "usage-reset":
                label = "usage limit reset; harness resumed the task"
            return "harness", label.strip()[:200], detail
    if stripped != raw and not body.strip():
        return "harness", "harness: system-reminder", {"source": "system-reminder", "text": raw[:4000]}
    return "user", body if body != raw else raw, {}
OBJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,119}$")
MIME_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]{1,64}/[A-Za-z0-9!#$&^_.+-]{1,120}(;[ -~]{0,80})?$")
TEXT_MIMES = ("application/json", "application/xml", "application/x-yaml", "application/javascript")


def fingerprint(data):
    return hashlib.sha256(data).hexdigest()


def seat_from_env():
    """The native seat this process runs in: $MCT_SEAT, else the enclosing
    tmux session name mapped through NATIVE_SEATS, else ''."""
    seat = os.environ.get("MCT_SEAT", "").strip()
    if seat in NATIVE_SEATS:
        return seat
    if os.environ.get("TMUX"):
        try:
            p = subprocess.run(["tmux", "display-message", "-p", "#S"], capture_output=True, text=True, timeout=3)
            name = p.stdout.strip()
            for provider, session in NATIVE_SEATS.items():
                if name == session:
                    return provider
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / "ledger.sqlite3"
        with self.connect() as c:
            c.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS seats (
                    seat TEXT PRIMARY KEY, source TEXT DEFAULT '', offset INTEGER DEFAULT 0,
                    busy INTEGER DEFAULT 0, current_turn TEXT DEFAULT '', epoch TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY, seat TEXT NOT NULL, request_id TEXT NOT NULL,
                    status TEXT NOT NULL, prompt TEXT NOT NULL, context TEXT DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL, epoch TEXT DEFAULT '',
                    UNIQUE(seat,request_id));
                CREATE TABLE IF NOT EXISTS objects (
                    id TEXT PRIMARY KEY, seat TEXT NOT NULL, turn_id TEXT NOT NULL,
                    kind TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL,
                    sha256 TEXT NOT NULL, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, seat TEXT NOT NULL,
                    turn_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS events_seat ON events(seat,id);
                CREATE TABLE IF NOT EXISTS records (
                    source TEXT NOT NULL, offset INTEGER NOT NULL, body BLOB NOT NULL,
                    PRIMARY KEY(source,offset));
                CREATE TABLE IF NOT EXISTS archive_queue (
                    seat TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 1);
            """)
            if 'response_required' not in {r[1] for r in c.execute('PRAGMA table_info(turns)')}:
                c.execute('ALTER TABLE turns ADD COLUMN response_required INTEGER NOT NULL DEFAULT 0')
            cols = {r[1] for r in c.execute('PRAGMA table_info(objects)')}
            if 'origin' not in cols:
                # who minted the object: '' (broker/transcript import) or 'seat' (mct-push)
                c.execute("ALTER TABLE objects ADD COLUMN origin TEXT NOT NULL DEFAULT ''")
            if 'created' not in cols:
                c.execute("ALTER TABLE objects ADD COLUMN created REAL NOT NULL DEFAULT 0")
            c.execute("CREATE INDEX IF NOT EXISTS objects_turn ON objects(turn_id,kind)")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=FULL")
        try:
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def scope(seat):
        return fingerprint(seat.encode())[:16]

    @staticmethod
    def handle(seat, turn, ident):
        return "mct://%s/%s/%s" % (Store.scope(seat), turn, ident)

    def _object(self, c, seat, turn, kind, body, name="", mime="text/plain", origin=""):
        ident = uuid.uuid4().hex
        c.execute("INSERT INTO objects(id,seat,turn_id,kind,name,mime,sha256,body,origin,created) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (ident, seat, turn, kind, name, mime, fingerprint(body), body, origin, time.time()))
        return self.handle(seat, turn, ident)

    def _event(self, c, seat, turn, kind, text, detail=None):
        c.execute("INSERT INTO events(seat,turn_id,kind,text,detail,created) VALUES (?,?,?,?,?,?)",
                  (seat, turn, kind, text, json.dumps(detail or {}), time.time()))
        c.execute("INSERT INTO archive_queue(seat) VALUES (?) ON CONFLICT(seat) DO UPDATE SET generation=abs(generation)+1", (seat,))

    def submit(self, seat, request_id, text, files=()):
        if seat not in NATIVE_SEATS:
            raise ValueError("Unknown frontier seat")
        if not isinstance(text, str) or (not text.strip() and not files):
            raise ValueError("Write a message or attach a file")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
            raise ValueError("Invalid request identifier")
        decoded = []
        for f in files:
            mime, sep, encoded = str(f.get("dataUrl", "")).partition(";base64,")
            if not sep or not mime.startswith("data:"):
                raise ValueError("Invalid attachment")
            decoded.append((str(f.get("name") or "attachment"), mime[5:], base64.b64decode(encoded, validate=True)))
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT * FROM turns WHERE seat=? AND request_id=?", (seat, request_id)).fetchone()
            if old:
                previous = c.execute("SELECT name,mime,sha256 FROM objects WHERE turn_id=? AND kind='attachment' ORDER BY rowid", (old["id"],)).fetchall()
                if old["prompt"] != text or [tuple(r) for r in previous] != [(n, m, fingerprint(b)) for n, m, b in decoded]:
                    raise ValueError("This request identifier belongs to a different message")
                return dict(old)
            turn = uuid.uuid4().hex
            prompt = self._object(c, seat, turn, "prompt", text.encode(), "prompt.md", "text/markdown")
            attachments = [{"name": name, "mime": mime, "object": self._object(c, seat, turn, "attachment", raw, name, mime),
                            "sha256": fingerprint(raw), "size": len(raw)} for name, mime, raw in decoded]
            context = self._object(c, seat, turn, "context", json.dumps({
                "schema": "mct.context/1", "origin": "operator", "prompt": prompt,
                "attachments": attachments}).encode(), "context.json", "application/json")
            now = time.time()
            c.execute("INSERT INTO turns(id,seat,request_id,status,prompt,context,created,updated) VALUES (?,?,?,'queued',?,?,?,?)",
                      (turn, seat, request_id, text, context, now, now))
            c.execute("INSERT OR IGNORE INTO seats(seat) VALUES (?)", (seat,))
            self._event(c, seat, turn, "user", text, {"attachments": attachments, "request_id": request_id})
            return dict(c.execute("SELECT * FROM turns WHERE id=?", (turn,)).fetchone())

    def pull(self, handle, output=None):
        m = HANDLE.fullmatch(handle)
        if not m:
            raise ValueError("Invalid MCT object handle")
        with self.connect() as c:
            row = c.execute("SELECT * FROM objects WHERE id=? AND turn_id=?", (m[3], m[2])).fetchone()
            if not row or self.scope(row["seat"]) != m[1]:
                raise ValueError("Object is outside this turn")
            raw = bytes(row["body"])
            if fingerprint(raw) != row["sha256"]:
                raise ValueError("Object integrity check failed")
            if row["kind"] == "context":
                manifest = json.loads(raw)
                prompt = self.pull(manifest["prompt"])
                reply = self.reply_path(row['turn_id'])
                push = "mct-push --seat %s --turn %s --file %s" % (row["seat"], row["turn_id"], reply)
                raw = json.dumps({**manifest, "operator_prompt": prompt.decode("utf-8"),
                                  "seat": row["seat"], "turn": row["turn_id"],
                                  "response_file": str(reply),
                                  "response_instructions": "Write your complete answer to response_file, then publish it with `" + push
                                  + "` (prints the mct:// response pointer; `mct-push` also reads stdin). Your final chat message must be exactly one line: MCT response <that pointer>."},
                                 ensure_ascii=False).encode()
            self._event(c, row["seat"], row["turn_id"], "activity", "Read " + (row["name"] or row["kind"]), {"object": handle})
        if output:
            # Explicit destination only; never silently overwrite a model's file.
            with open(output, "xb") as f:
                f.write(raw)
        return raw

    def describe(self, handle):
        """Metadata for a handle (no body): seat, turn, kind, name, mime, size, sha256."""
        m = HANDLE.fullmatch(handle)
        if not m:
            raise ValueError("Invalid MCT object handle")
        with self.connect() as c:
            row = c.execute("SELECT id,seat,turn_id,kind,name,mime,sha256,origin,created,length(body) AS size FROM objects WHERE id=? AND turn_id=?", (m[3], m[2])).fetchone()
        if not row or self.scope(row["seat"]) != m[1]:
            raise ValueError("Object is outside this turn")
        return dict(row)

    def reply_path(self, turn):
        directory = self.root / 'responses' / turn
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory / 'reply.md'

    # ---- outbound front door -------------------------------------------------
    def _resolve_turn(self, c, seat, turn):
        """The turn a publication binds to: an explicit id/handle owned by the
        seat, else the seat's turn in flight, else a freshly minted seat turn."""
        if turn:
            m = HANDLE.fullmatch(turn)
            if m:
                if m[1] != self.scope(seat):
                    raise ValueError("That MCT handle belongs to another seat")
                turn = m[2]
            if not re.fullmatch(r"[a-f0-9]{32}", turn):
                raise ValueError("Invalid MCT turn identifier")
            row = c.execute("SELECT * FROM turns WHERE id=? AND seat=?", (turn, seat)).fetchone()
            if not row:
                raise ValueError("Unknown MCT turn for seat " + seat)
            return dict(row), False
        row = c.execute("SELECT * FROM turns WHERE seat=? AND status IN ('sending','sent','working','uncertain') ORDER BY created LIMIT 1", (seat,)).fetchone()
        if row:
            return dict(row), False
        ident = uuid.uuid4().hex
        now = time.time()
        c.execute("INSERT INTO turns(id,seat,request_id,status,prompt,context,created,updated) VALUES (?,?,?,'working','','',?,?)",
                  (ident, seat, 'seat-' + ident, now, now))
        c.execute("INSERT OR IGNORE INTO seats(seat) VALUES (?)", (seat,))
        return dict(c.execute("SELECT * FROM turns WHERE id=?", (ident,)).fetchone()), True

    def _publish(self, c, turn, kind, raw, name, mime, detail=None, minted=False):
        """Shared by publish() and publish_reply(): immutable object + display
        event + turn completion. Idempotent on identical bodies."""
        seat = turn["seat"]
        # A reply must never be lost: an interrupted/cancelled turn still
        # accepts its answer (recorded + rendered) rather than raising.
        was_cancelled = turn["status"] == "cancelled"
        if kind == "response":
            old = c.execute("SELECT * FROM objects WHERE turn_id=? AND kind='response' AND origin='seat' ORDER BY rowid LIMIT 1", (turn["id"],)).fetchone()
            if old:
                if old["sha256"] == fingerprint(raw):
                    return self.handle(seat, turn["id"], old["id"])
                raise ValueError("This turn already has a different immutable reply; push additional material with --kind artifact")
        else:
            old = c.execute("SELECT * FROM objects WHERE turn_id=? AND kind='artifact' AND origin='seat' AND sha256=? AND name=?",
                            (turn["id"], fingerprint(raw), name)).fetchone()
            if old:
                return self.handle(seat, turn["id"], old["id"])
        obj = self._object(c, seat, turn["id"], kind, raw, name, mime, origin="seat")
        text = None
        if mime.startswith("text/") or mime.split(";")[0] in TEXT_MIMES:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        info = {**(detail or {}), "object": obj, "name": name, "mime": mime, "size": len(raw),
                "sha256": fingerprint(raw), "origin": "seat", "published": True}
        self._event(c, seat, turn["id"], kind, text if text is not None else "Published %s (%d bytes)" % (name, len(raw)), info)
        if (kind == "response" or minted) and turn["status"] != "complete":
            c.execute("UPDATE turns SET status='complete',updated=? WHERE id=?", (time.time(), turn["id"]))
            if was_cancelled:
                msg = "Complete · reply published after interrupt"
            else:
                msg = "Complete · reply published" if kind == "response" else "Complete · artifact published"
            self._event(c, seat, turn["id"], "status", msg)
        return obj

    def publish(self, seat, body, turn=None, kind="response", name="", mime=""):
        """The seat's front door (`mct-push`): store `body` as an immutable
        object of `kind` bound to `turn` (default: the seat's turn in flight,
        else a seat-minted turn) and return its mct:// pointer."""
        if seat not in NATIVE_SEATS:
            raise ValueError("Unknown frontier seat: " + str(seat))
        if kind not in PUBLISH_KINDS:
            raise ValueError("kind must be one of " + "|".join(PUBLISH_KINDS))
        if not isinstance(body, (bytes, bytearray)) or not bytes(body).strip():
            raise ValueError("Empty body")
        raw = bytes(body)
        name = Path(name or ("reply.md" if kind == "response" else "artifact.bin")).name
        if not OBJECT_NAME.match(name):
            raise ValueError("Invalid object name")
        mime = mime or mimetypes.guess_type(name)[0] or ("text/markdown" if kind == "response" else "application/octet-stream")
        if not MIME_TYPE.match(mime):
            raise ValueError("Invalid media type")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row, minted = self._resolve_turn(c, seat, turn)
            return self._publish(c, row, kind, raw, name, mime, minted=minted)

    def publish_reply(self, context, path):
        """Legacy outbound path (`mct-pull <context> --reply-file <response_file>`):
        the same immutable publication as `mct-push`, keyed by the context handle."""
        match = HANDLE.fullmatch(context)
        if not match:
            raise ValueError('Invalid MCT context')
        expected = self.reply_path(match[2])
        if Path(path).absolute() != expected.absolute() or expected.is_symlink():
            raise ValueError('Use the response_file supplied by mct-pull')
        raw = expected.read_bytes()
        if not raw.decode('utf-8').strip():
            raise ValueError('Reply file is empty')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            turn = c.execute('SELECT * FROM turns WHERE id=? AND context=?', (match[2], context)).fetchone()
            if not turn or self.scope(turn['seat']) != match[1]:
                raise ValueError('Unknown MCT turn')
            return self._publish(c, dict(turn), 'response', raw, 'reply.md', 'text/markdown', {'file': str(expected)})

    def published_response(self, c, turn):
        return c.execute("SELECT id FROM objects WHERE turn_id=? AND kind='response' AND origin='seat' LIMIT 1", (turn,)).fetchone()

    def reclassify_harness(self):
        """One-time, idempotent repair of ledgers written before harness
        classification: every displayed "user" event whose text is a harness
        envelope becomes a "harness" event, and the native turn it minted is
        relabelled harness-* (so the turn log does not show it as an operator
        prompt). Raw records and objects are untouched. Returns event ids."""
        fixed = []
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            # Only transcript-imported turns (native-*) can carry harness text; a
            # turn minted by Store.submit IS the operator's, whatever it says.
            for e in c.execute("SELECT e.id,e.seat,e.turn_id,e.text,e.detail FROM events e JOIN turns t ON t.id=e.turn_id "
                               "WHERE e.kind='user' AND t.request_id LIKE 'native-%'").fetchall():
                kind, label, detail = classify_user_text(e["text"])
                if kind != "harness":
                    continue
                old = json.loads(e["detail"] or "{}")
                c.execute("UPDATE events SET kind='harness',text=?,detail=? WHERE id=?",
                          (label, json.dumps({**detail, "reclassified": True, "was": "user", **({"attachments": old["attachments"]} if old.get("attachments") else {})}), e["id"]))
                c.execute("UPDATE turns SET request_id='harness-' || substr(request_id, 8) WHERE id=? AND seat=? AND request_id LIKE 'native-%' AND prompt=?",
                          (e["turn_id"], e["seat"], e["text"]))
                fixed.append(e["id"])
            if fixed:
                for seat in {r[0] for r in c.execute("SELECT DISTINCT seat FROM events WHERE id IN (%s)" % ",".join("?" * len(fixed)), fixed)}:
                    c.execute("INSERT INTO archive_queue(seat) VALUES (?) ON CONFLICT(seat) DO UPDATE SET generation=abs(generation)+1", (seat,))
        return fixed

    def reconcile_delivered(self, seat):
        """Repair acknowledged legacy turns using archived provider evidence.

        Old code missed pointers prefixed by a native input draft. Match only
        canonical USER records and their finished imported turns, never tool
        output or elapsed time. Keep all original rows and append an audit event.
        """
        repaired = []
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            pending = c.execute("SELECT * FROM turns WHERE seat=? AND status IN ('sent','sending','uncertain')", (seat,)).fetchall()
            for turn in pending:
                records = c.execute('SELECT source,offset,body FROM records WHERE instr(body,?)>0', (turn['context'].encode(),)).fetchall()
                for record in records:
                    try:
                        items = normalize(json.loads(record['body']), seat)
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if not any(k == 'user' and any(m[1] == turn['context'] for m in CONTEXT_IN_TEXT.finditer(t)) for k, t, _ in items):
                        continue
                    imported = fingerprint((record['source'] + ':' + str(record['offset'])).encode())[:32]
                    completed = c.execute("SELECT * FROM turns WHERE id=? AND seat=? AND status IN ('complete','cancelled')", (imported, seat)).fetchone()
                    if not completed:
                        continue
                    c.execute('UPDATE turns SET status=?,updated=? WHERE id=?', (completed['status'], time.time(), turn['id']))
                    self._event(c, seat, turn['id'], 'status', 'Delivery reconciled with completed native turn', {'native_turn': imported})
                    repaired.append(turn['id'])
                    break
        return repaired

    def state(self, seat):
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO seats(seat) VALUES (?)", (seat,))
            return dict(c.execute("SELECT * FROM seats WHERE seat=?", (seat,)).fetchone())

    def events(self, seat, after=0, limit=200):
        with self.connect() as c:
            rows = c.execute("SELECT * FROM events WHERE seat=? AND id>? ORDER BY id LIMIT ?", (seat, after, limit)).fetchall()
            turns = c.execute("SELECT id,status FROM turns WHERE seat=? AND status NOT IN ('complete','cancelled','coalesced','captured') ORDER BY created", (seat,)).fetchall()
        return {"events": [{**dict(r), "detail": {} if r['kind'] == 'activity' else json.loads(r["detail"])} for r in rows],
                "turns": [dict(r) for r in turns], "cursor": rows[-1]["id"] if rows else after}

    def turn_log(self, seat=None, limit=12):
        """Operator-facing turn log: recent turns with their pointer events
        (prompt/context/response/artifact objects by handle, never inline
        bodies) — the ledger half of GET /api/mct/live."""
        out = []
        with self.connect() as c:
            seats = [seat] if seat else list(NATIVE_SEATS)
            for s in seats:
                for t in c.execute("SELECT * FROM turns WHERE seat=? AND request_id NOT LIKE 'harness-%' ORDER BY created DESC LIMIT ?", (s, limit)).fetchall():
                    evs = []
                    objs = c.execute("SELECT id,kind,name,mime,sha256,origin,created,length(body) AS size FROM objects WHERE turn_id=? ORDER BY rowid", (t["id"],)).fetchall()
                    for o in objs:
                        typ = {"prompt": "prompt.written", "attachment": "attachment.written", "context": "context.minted",
                               "response": "response.written", "artifact": "artifact.written"}.get(o["kind"], o["kind"] + ".written")
                        actor = "A.frontier" if o["kind"] in ("response", "artifact") else "C.operator" if o["kind"] in ("prompt", "attachment") else "B.broker"
                        evs.append({"type": typ, "actor": actor, "ts": _iso(o["created"] or t["created"]),
                                    "handle": self.handle(s, t["id"], o["id"]), "file": o["name"] or o["kind"],
                                    "mime": o["mime"], "size": o["size"], "sha256": o["sha256"],
                                    "origin": o["origin"] or "broker"})
                    for e in c.execute("SELECT kind,text,created FROM events WHERE turn_id=? AND kind IN ('status','error') ORDER BY id", (t["id"],)).fetchall():
                        evs.append({"type": "turn." + e["kind"], "actor": "B.broker", "ts": _iso(e["created"]), "note": e["text"][:200]})
                    evs.sort(key=lambda e: e["ts"])
                    out.append({"turn_id": t["id"], "state": t["status"], "created_at": _iso(t["created"]),
                                "backend": "mct", "seat": s, "request_id": t["request_id"],
                                "context": t["context"] or None, "events": evs})
        out.sort(key=lambda t: t["created_at"])
        return out[-limit:]

    def delivery_state(self, seat):
        with self.connect() as c:
            waiting = c.execute("SELECT id,status FROM turns WHERE seat=? AND status IN ('sending','sent','uncertain') ORDER BY created LIMIT 1", (seat,)).fetchone()
            count = c.execute("SELECT count(*) FROM turns WHERE seat=? AND status='queued'", (seat,)).fetchone()[0]
            state = c.execute('SELECT busy FROM seats WHERE seat=?', (seat,)).fetchone()
        if waiting:
            return {'state': waiting['status'], 'turn': waiting['id'], 'queued': count,
                    'message': 'Waiting for native delivery acknowledgement' if waiting['status'] != 'uncertain' else 'Delivery uncertain; automatic resend is paused'}
        return {'state': 'working' if state and state['busy'] else 'ready', 'queued': count,
                'message': ('Working' if state and state['busy'] else 'Ready') + (f' · {count} queued' if count else '')}

    def adopt(self, seat, source, epoch):
        with self.connect() as c:
            row = c.execute("SELECT * FROM seats WHERE seat=?", (seat,)).fetchone()
            if row and row["source"] == source:
                if row["epoch"] != epoch:
                    c.execute("UPDATE seats SET epoch=? WHERE seat=?", (epoch, seat))
                return
            if row and row["source"]:
                c.execute("UPDATE turns SET status='interrupted',updated=? WHERE seat=? AND status IN ('sending','sent','working')", (time.time(), seat))
                self._event(c, seat, "", "error", "The native session changed. Unfinished messages require an explicit retry.")
            c.execute("INSERT INTO seats(seat,source,epoch) VALUES (?,?,?) ON CONFLICT(seat) DO UPDATE SET source=excluded.source,epoch=excluded.epoch,offset=0,busy=0,current_turn=''", (seat, source, epoch))

    def ingest(self, seat, source, offset, raw, items, end):
        with self.connect() as c:
            if c.execute("SELECT 1 FROM records WHERE source=? AND offset=?", (source, offset)).fetchone():
                c.execute("UPDATE seats SET offset=? WHERE seat=?", (end, seat))
                return
            c.execute("INSERT INTO records VALUES (?,?,?)", (source, offset, raw))
            c.execute("INSERT INTO archive_queue(seat) VALUES (?) ON CONFLICT(seat) DO UPDATE SET generation=abs(generation)+1", (seat,))
            state = c.execute("SELECT * FROM seats WHERE seat=?", (seat,)).fetchone()
            turn = state["current_turn"] or ""
            for kind, text, detail in items:
                if isinstance(detail, dict) and detail.get("trimmed"):
                    # t274: the excerpt's pointer - the untouched provider
                    # record, archived verbatim in `records`.
                    detail = {**detail, "record": "%s:%d" % (source, offset)}
                if kind == "user":
                    c.execute("UPDATE turns SET status='complete' WHERE seat=? AND status='working' AND request_id LIKE 'native-%'", (seat,))
                    owned = None
                    match = None
                    for match in CONTEXT_IN_TEXT.finditer(text):
                        owned = c.execute("SELECT * FROM turns WHERE id=? AND seat=? AND context=? AND status IN ('sending','sent','working','uncertain','cancelled')", (match[2], seat, match[1])).fetchone()
                        if owned:
                            break
                    if owned:
                        turn = owned["id"]
                        c.execute("UPDATE turns SET status=CASE WHEN status='cancelled' THEN status ELSE 'working' END,updated=? WHERE id=?", (time.time(), turn))
                    else:
                        if match:
                            text = 'Earlier operator prompt (original object unavailable)'
                        turn = fingerprint((source + ":" + str(offset)).encode())[:32]
                        now = time.time()
                        prompt = self._object(c, seat, turn, "prompt", text.encode(), "prompt.md", "text/markdown")
                        detail = dict(detail)
                        imported = detail.pop('_files', [])
                        if imported:
                            detail['attachments'] = []
                            for path in imported:
                                path = Path(path)
                                raw_file = path.read_bytes()
                                mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                                obj = self._object(c, seat, turn, 'attachment', raw_file, path.name, mime)
                                detail['attachments'].append({'name': path.name, 'mime': mime, 'object': obj, 'size': len(raw_file), 'sha256': fingerprint(raw_file)})
                        c.execute("INSERT OR IGNORE INTO turns(id,seat,request_id,status,prompt,context,created,updated) VALUES (?,?,?,'working',?,?,?,?)", (turn, seat, 'native-' + turn, text, prompt, now, now))
                        self._event(c, seat, turn, "user", text, detail)
                    c.execute("UPDATE seats SET busy=1,current_turn=? WHERE seat=?", (turn, seat))
                elif kind == "harness":
                    # Harness-injected user-role content: shown as a system
                    # line under the current turn, never as the operator and
                    # never a turn of its own. The model does react to it, so
                    # the seat counts as busy until its next completion.
                    self._event(c, seat, turn, "harness", text, detail)
                    c.execute("UPDATE seats SET busy=1 WHERE seat=?", (seat,))
                elif kind in ("start", "complete", "cancelled"):
                    busy = kind == "start"
                    c.execute("UPDATE seats SET busy=? WHERE seat=?", (int(busy), seat))
                    if kind != "start" and turn:
                        # Claude's append-only log commonly closes a turn with a
                        # system record; its text messages have no stop_reason.
                        cancelled = c.execute("SELECT 1 FROM turns WHERE id=? AND status='cancelled'", (turn,)).fetchone()
                        policy = c.execute('SELECT response_required FROM turns WHERE id=?', (turn,)).fetchone()
                        requires_file = bool(policy and policy['response_required'])
                        published = self.published_response(c, turn)
                        if kind == "complete" and seat == "claude-code" and not cancelled and not requires_file and not published:
                            last = c.execute("SELECT * FROM events WHERE seat=? AND turn_id=? AND kind IN ('commentary','assistant','activity') ORDER BY id DESC LIMIT 1", (seat, turn)).fetchone()
                            if last and last['kind'] == 'commentary' and not RESPONSE_IN_TEXT.search(last['text']):
                                detail = json.loads(last['detail'])
                                detail['object'] = self._object(c, seat, turn, 'response', last['text'].encode(), 'reply.md', 'text/markdown')
                                self._event(c, seat, turn, 'assistant', last['text'], {**detail, 'replaces': last['id']})
                        missing = kind == 'complete' and requires_file and not cancelled and not c.execute("SELECT 1 FROM objects WHERE turn_id=? AND kind='response'", (turn,)).fetchone()
                        status = 'failed' if missing else 'complete' if kind == 'complete' else 'cancelled'
                        c.execute("UPDATE turns SET status=CASE WHEN status='cancelled' THEN status ELSE ? END,updated=? WHERE id=?", (status, time.time(), turn))
                        self._event(c, seat, turn, 'error' if missing else 'status',
                                    'The model finished without publishing its reply (mct-push). Native output was archived.' if missing else 'Cancelled' if cancelled or kind == 'cancelled' else 'Complete')
                else:
                    row = c.execute("SELECT status FROM turns WHERE id=?", (turn,)).fetchone()
                    if row and row["status"] == "cancelled":
                        continue  # raw provider event remains recorded, never displayed late
                    if kind in ("assistant", "commentary"):
                        pointer = RESPONSE_IN_TEXT.search(text)
                        if pointer and c.execute("SELECT 1 FROM objects WHERE id=? AND turn_id=? AND kind='response'", (pointer[3], pointer[2])).fetchone():
                            # Reply-as-pointer (t249): the published object IS the
                            # reply and is rendered from the ledger as plain text.
                            # A bare acknowledgement is dropped; when the seat wrote
                            # prose around the pointer, only the raw mct:// line is
                            # removed, so no handle is ever shown to the operator.
                            # Both forms stay verbatim in the raw archive.
                            text = RESPONSE_IN_TEXT.sub('', text).strip(' .\r\n\t')
                            if not text:
                                continue
                    if kind == "assistant":
                        policy = c.execute('SELECT response_required FROM turns WHERE id=?', (turn,)).fetchone()
                        if (policy and policy['response_required']) or self.published_response(c, turn):
                            # The published object is the visible reply. Native
                            # pointer acknowledgements stay in the raw archive.
                            continue
                        detail = {**detail, "object": self._object(c, seat, turn, "response", text.encode(), "reply.md", "text/markdown")}
                    self._event(c, seat, turn, kind, text, detail)
            c.execute("UPDATE seats SET offset=? WHERE seat=?", (end, seat))

    def next_turn(self, seat, turn_id=None):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            state = c.execute("SELECT * FROM seats WHERE seat=?", (seat,)).fetchone()
            if not state or state["busy"]:
                return None
            if c.execute("SELECT 1 FROM turns WHERE seat=? AND status IN ('sending','sent','working','uncertain')", (seat,)).fetchone():
                return None
            row = c.execute("SELECT * FROM turns WHERE seat=? AND status='queued' AND (? IS NULL OR id=?) ORDER BY created LIMIT 1", (seat, turn_id, turn_id)).fetchone()
            if row:
                c.execute("UPDATE turns SET status='sending',response_required=1,epoch=?,updated=? WHERE id=?", (state["epoch"], time.time(), row["id"]))
                return dict(row)

    def sent(self, turn, ok):
        with self.connect() as c:
            c.execute("UPDATE turns SET status=?,updated=? WHERE id=?", ("sent" if ok else "uncertain", time.time(), turn))
            row = c.execute("SELECT seat FROM turns WHERE id=?", (turn,)).fetchone()
            self._event(c, row["seat"], turn, "status" if ok else "error",
                        "Sent" if ok else "Delivery could not be confirmed. It will not be resent automatically.")

    def cancel(self, seat, turn):
        with self.connect() as c:
            row = c.execute("SELECT * FROM turns WHERE seat=? AND id=?", (seat, turn)).fetchone()
            if not row or row["status"] in ("complete", "cancelled"):
                return False
            c.execute("UPDATE turns SET status='cancelled',updated=? WHERE id=?", (time.time(), turn))
            self._event(c, seat, turn, "status", "Cancelled")
            return row["status"] in ("sent", "working", "sending", "uncertain")

    def snapshot(self):
        """A consistent, compressed SQLite backup, including untouched raw logs."""
        with self.connect() as c:
            generation = {r['seat']: r['generation'] for r in c.execute('SELECT * FROM archive_queue WHERE generation>0')}
            if not generation:
                return None
            with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                path = Path(tmp) / 'ledger.sqlite3'
                with sqlite3.connect(path) as target:
                    c.backup(target)
                raw = gzip.compress(path.read_bytes(), compresslevel=3)
        return generation, raw

    def archived(self, generation):
        with self.connect() as c:
            for seat, version in generation.items():
                # Retain the generation monotonically: deleting then inserting
                # would let an old acknowledgement erase newer pending changes.
                c.execute('UPDATE archive_queue SET generation=-generation WHERE seat=? AND generation=?', (seat, version))


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts or 0))


def pointer_line(turn):
    """The ONE line the Broker types into the native seat for a queued turn.
    Everything else (prompt, attachments, reply protocol) is pulled on demand."""
    ctx = turn["context"]
    return ("MCT context " + ctx + ". Read it with `mct-pull " + ctx + "` and answer its operator_prompt; "
            "publish the full reply with `mct-push --seat " + turn["seat"] + " --turn " + turn["id"]
            + " --file <response_file>` and end with exactly one line: MCT response <returned pointer>.")


def _trimmed(detail, trimmed):
    """Tag a detail whose body was bounded, so ingest can attach its pointer."""
    if trimmed:
        detail["trimmed"] = trimmed
    return detail


def normalize(row, provider, recover_prompt=lambda text: (text, {})):
    """Canonical provider records only: no PTY parsing or reasoning exposure."""
    result = []
    if provider == "codex":
        p = row.get("payload") or {}
        if row.get("type") == "event_msg":
            event = {"task_started": "start", "task_complete": "complete", "turn_aborted": "cancelled"}.get(p.get("type"))
            return [(event, "", {})] if event else []
        if row.get("type") != "response_item":
            return []
        typ = p.get("type")
        if typ == "message" and p.get("role") in ("user", "assistant"):
            text = "".join(b.get("text", "") for b in p.get("content", []) if b.get("type") in ("input_text", "output_text", "text"))
            if not text or p.get("phase") == "analysis" or text.startswith("<environment_context>"):
                return []
            if p["role"] == "user":
                kind, text, detail = classify_user_text(text)
                if kind == "harness":
                    return [(kind, text, detail)]
                text, detail = recover_prompt(text)
                return [("user", text, detail)]
            return [("assistant" if p.get("phase") in (None, "final_answer", "final") else "commentary", text, {})]
        if typ in ("function_call", "custom_tool_call"):
            shown, trimmed = bound(p.get("arguments", p.get("input", "")), TOOL_EXCERPT)
            return [("activity", "Running " + p.get("name", "tool"), _trimmed({"call_id": p.get("call_id"), "input": shown}, trimmed))]
        if typ in ("function_call_output", "custom_tool_call_output"):
            shown, trimmed = bound(p.get("output", ""), TOOL_EXCERPT)
            return [("activity", "Tool result", _trimmed({"call_id": p.get("call_id"), "output": shown}, trimmed))]
        return []
    typ = row.get("type")
    if typ == "system" and row.get("subtype") in ("turn_duration", "stop_hook_summary"):
        return [("complete", "", {})]
    msg = row.get("message") or {}
    content = msg.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    for block in content:
        bt = block.get("type")
        if bt == "text" and typ in ("user", "assistant"):
            text = block.get("text", "")
            if typ == "user":
                kind, text, detail = classify_user_text(text)
                if kind == "harness":
                    result.append((kind, text, detail))
                    continue
                text, detail = recover_prompt(text)
                result.append(("user", text, detail))
            else:
                result.append(("assistant" if msg.get("stop_reason") == "end_turn" else "commentary", text, {}))
        elif bt == "tool_use":
            shown, trimmed = bound(block.get("input"), TOOL_EXCERPT)
            result.append(("activity", "Running " + block.get("name", "tool"), _trimmed({"call_id": block.get("id"), "input": shown}, trimmed)))
        elif bt == "tool_result":
            shown, trimmed = bound(block.get("content"), TOOL_EXCERPT)
            result.append(("activity", "Tool result", _trimmed({"call_id": block.get("tool_use_id"), "output": shown, "error": block.get("is_error", False)}, trimmed)))
    if typ == "assistant" and msg.get("stop_reason") == "end_turn":
        result.append(("complete", "", {}))
    return result


class NativeAdapter:
    # A seat whose CLAUDE_CONFIG_DIR is unset shares ~/.claude with every other
    # claude session on the host, so its project directory routinely holds
    # several transcripts (board t272). Choose one deterministically instead of
    # refusing to deliver: the pane's own --session-id when the CLI names one,
    # then the transcript already adopted for this seat, then the newest file
    # still being appended to. Ambiguity is logged, never fatal.
    FRESH_S = float(os.environ.get("MCT_PROBE_FRESH_S", "900"))
    SESSION_ARGS = ("--session-id", "--resume", "-r", "--session")

    def __init__(self, provider):
        self.provider = provider
        self.tmux = NATIVE_SEATS[provider]
        self.pinned = None      # (pid, path) kept while it is still live
        self.note = ""          # last line logged, so the 1 Hz loop cannot spam

    def _log(self, message):
        if message == self.note:
            return
        self.note = message
        print("[mct] %s adapter: %s" % (self.provider, message), file=sys.stderr, flush=True)

    @staticmethod
    def _session_start(path):
        """Wall-clock start of a transcript: its first timestamped record."""
        try:
            with open(path, "rb") as f:
                for _ in range(40):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        stamp = json.loads(line).get("timestamp")
                    except ValueError:
                        continue
                    if stamp:
                        return calendar.timegm(time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S"))
        except (OSError, ValueError):
            pass
        return 0

    @classmethod
    def _session_ids(cls, args):
        """Session ids named on the seat process's own command line."""
        found = []
        for i, raw in enumerate(args):
            token = os.fsdecode(raw)
            key, sep, inline = token.partition("=")
            if key not in cls.SESSION_ARGS:
                continue
            value = (inline if sep else (os.fsdecode(args[i + 1]) if i + 1 < len(args) else "")).strip()
            if value:
                found.append(value)
        return found

    def _choose(self, candidates, args, pid, prefer=""):
        """(path, why) for the transcript this pane writes. Never raises."""
        if not candidates:
            return None, "no transcript yet"
        if len(candidates) == 1:
            return candidates[0], "the only transcript in the project"
        named = self._session_ids(args)
        for sid in named:
            for path in candidates:
                if path.stem == sid:
                    return path, "--session-id " + sid
        if named:
            # t321 (2026-09-16): the seat was launched with an explicit session
            # id (station seat launch), so its transcript is THAT file or none
            # yet. Falling through to "newest appended" pinned the adapter to
            # the keeper SERVE chat's transcript (same project dir) and every
            # serve turn was recorded as a native turn of this seat.
            return None, "waiting for transcript " + named[0][:8] + " (named on the seat command line)"
        now = time.time()

        def live(path):
            try:
                return now - path.stat().st_mtime <= self.FRESH_S
            except OSError:
                return False
        # Stay on the transcript already in use while it is still being written:
        # switching source resets the read offset, so it must not flap.
        for path, why in ((self.pinned[1], "pinned") if self.pinned and self.pinned[0] == pid else (None, ""),
                          (Path(prefer) if prefer else None, "already adopted for this seat")):
            if path is not None and path in candidates and live(path):
                return path, why
        # A session that began before this claude process did cannot be its
        # own (the CLI would have had to be given --resume, handled above).
        try:
            started = Path("/proc") / str(pid)
            born = [p for p in candidates if self._session_start(p) >= started.stat().st_mtime - 60]
        except OSError:
            born = []
        candidates = born or candidates
        if len(candidates) == 1:
            return candidates[0], "the only transcript opened since the seat started"
        fresh = [p for p in candidates if live(p)]
        pool = fresh or candidates
        newest = max(sorted(pool, key=lambda p: p.stem), key=lambda p: p.stat().st_mtime)
        return newest, ("newest of %d appended within %ds" % (len(pool), int(self.FRESH_S)) if fresh
                        else "newest of %d (none appended recently)" % len(pool))

    def probe(self, prefer=""):
        try:
            p = subprocess.run(["tmux", "-L", "console", "display-message", "-p", "-t", "=" + self.tmux + ":", "#{pane_pid}"],
                               capture_output=True, text=True, timeout=5)
        except FileNotFoundError:   # 1.0.139: tmux is a Recommends — no tmux, no native seat
            raise RuntimeError("tmux is not installed on this host — the native terminal seat needs it (sudo apt install tmux).")
        if p.returncode or not p.stdout.strip().isdigit():
            raise RuntimeError("Open the selected native terminal once to start or sign in to its session.")
        queue, processes = [int(p.stdout.strip())], []
        while queue and len(processes) < 100:
            pid = queue.pop(0)
            try:
                proc = Path("/proc") / str(pid)
                args = (proc / "cmdline").read_bytes().split(b"\0")
                exe = Path(os.fsdecode(args[0])).name
                if (self.provider == "codex" and exe == "codex") or (self.provider == "claude-code" and (exe == "claude" or any(b"claude" in a and a.endswith((b"cli.js", b"claude")) for a in args[:3]))):
                    processes.append(proc)
                queue.extend(int(x) for x in (proc / "task" / str(pid) / "children").read_text().split())
            except (OSError, ValueError):
                continue
        for proc in processes:
            for fd in (proc / "fd").iterdir():
                try:
                    path = Path(os.readlink(fd))
                    if path.suffix == ".jsonl" and ((self.provider == "codex" and path.name.startswith("rollout-")) or
                                                   (self.provider == "claude-code" and "projects" in path.parts and "subagents" not in path.parts)):
                        return str(path), str(proc.name) + ":" + path.stem
                except OSError:
                    continue
        # Claude opens and closes its transcript for each append, so the fd scan
        # above usually misses it. Resolve the pane's own config dir + cwd
        # project and pick one candidate deterministically (t272).
        if self.provider == "claude-code":
            # t321 (2026-09-16, second pass): a session id named on ANY seat
            # process's command line (the launcher and the claude CLI both carry
            # --session-id since 1.0.93) is binding for the whole pane. Without
            # this, the helper children whose argv merely contains "claude" (the
            # MCP bridge `abstract_claude mcp`, seat-report) reached _choose with
            # no session id and adopted the newest transcript in the project —
            # the keeper SERVE chat's.
            named = []
            for proc in processes:
                try:
                    named += self._session_ids((proc / "cmdline").read_bytes().split(b"\0"))
                except OSError:
                    continue
            if named:
                sid = named[0]
                for proc in processes:
                    try:
                        env = dict(part.split(b"=", 1) for part in (proc / "environ").read_bytes().split(b"\0") if b"=" in part)
                        cfg = Path(os.fsdecode(env.get(b"CLAUDE_CONFIG_DIR", os.fsencode(str(Path.home() / ".claude")))))
                        project = re.sub(r"[^a-zA-Z0-9]", "-", os.readlink(proc / "cwd"))
                        hit = cfg / "projects" / project / (sid + ".jsonl")
                        if hit.is_file():
                            self.pinned = (proc.name, hit)
                            return str(hit), "--session-id " + sid
                    except OSError:
                        continue
                self.pinned = None
                raise RuntimeError("waiting for transcript " + sid[:8] + " (named on the seat command line; nothing adopted)")
            for proc in processes:
                try:
                    args = (proc / "cmdline").read_bytes().split(b"\0")
                    env = dict(part.split(b"=", 1) for part in (proc / "environ").read_bytes().split(b"\0") if b"=" in part)
                    cfg = Path(os.fsdecode(env.get(b"CLAUDE_CONFIG_DIR", os.fsencode(str(Path.home() / ".claude")))))
                    project = re.sub(r"[^a-zA-Z0-9]", "-", os.readlink(proc / "cwd"))
                    directory = cfg / "projects" / project
                    candidates = sorted(directory.glob("*.jsonl"))
                except OSError:
                    continue
                chosen, why = self._choose(candidates, args, proc.name, prefer)
                if chosen is None:
                    continue
                self.pinned = (proc.name, chosen)
                self._log("transcript %s (%s; %d candidate%s in %s)"
                          % (chosen.name, why, len(candidates), "" if len(candidates) == 1 else "s", directory))
                return str(chosen), str(proc.name) + ":" + chosen.stem
        raise RuntimeError("Waiting for the selected native session's transcript. Complete sign-in in its native terminal if needed.")

    async def send(self, pointer):
        target = "=" + self.tmux + ":"
        # Literal tmux input, never a shell command. An Enter is a distinct event.
        for args in (["send-keys", "-t", target, "-l", pointer], ["send-keys", "-t", target, "Enter"]):
            try:
                proc = await asyncio.create_subprocess_exec("tmux", "-L", "console", *args,
                                                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            except FileNotFoundError:   # no tmux: nothing was sent
                return False
            try:
                await asyncio.wait_for(proc.communicate(), 8)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return False
            if proc.returncode:
                return False
            await asyncio.sleep(0.15)
        return True

    async def cancel(self):
        try:
            p = await asyncio.create_subprocess_exec("tmux", "-L", "console", "send-keys", "-t", "=" + self.tmux + ":", "Escape")
        except FileNotFoundError:   # no tmux: no seat to cancel
            return
        await p.wait()


def oauth_token(home):
    """The managed long-lived Claude OAuth token for headless seats.

    Order: the process env (station-web is launched through `bash -lc`, which
    sources ~/.claude-oauth.env), then the fleet source of truth
    `$HUGPY_STATION_STATE/claude-oauth-token`, then ~/.claude-oauth.env. Never
    scraped from a pid's /proc. See resources/bin/claude-oauth-sync."""
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if tok:
        return tok
    for path in (Path(home) / "claude-oauth-token", Path.home() / ".claude-oauth.env"):
        try:
            raw = path.read_text()
        except OSError:
            continue
        m = re.search(r"CLAUDE_CODE_OAUTH_TOKEN=(?:'|\")?([^'\"\r\n]+)", raw)
        tok = (m.group(1) if m else raw).strip()
        if tok:
            return tok
    return ""


class HeadlessAdapter:
    """Drives a headless `claude -p` agent over the API in place of the tmux
    seat. `run(prompt, session_id, on_event)` spawns the CLI with the managed
    OAuth token in env, streams assistant/tool events through `on_event`, and
    returns (response_text, session_id). Same `cancel()` shape as
    NativeAdapter, so the Broker treats the two interchangeably."""
    MODEL = os.environ.get("SEAT_MODEL", "claude-opus-4-8")
    TOOLS = os.environ.get("SEAT_TOOLS", "Bash")

    def __init__(self, provider, token_source):
        self.provider = provider
        self.token_source = token_source
        self._proc = None

    async def run(self, prompt, session_id=None, on_event=None):
        token = self.token_source()
        if not token:
            raise RuntimeError("No managed CLAUDE_CODE_OAUTH_TOKEN for the headless seat")
        env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token}
        args = ["claude", "-p", prompt or "", "--model", self.MODEL,
                "--output-format", "stream-json", "--verbose",
                "--allowedTools", self.TOOLS]
        if session_id:
            args += ["--resume", session_id]
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
        self._proc = proc
        response, new_sid, error = "", session_id, None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("session_id"):
                    new_sid = row["session_id"]
                if row.get("type") == "result":
                    response = row.get("result") or response
                    if row.get("is_error"):
                        error = row.get("result") or "The headless agent reported an error"
                    continue
                if on_event:
                    for kind, text, detail in normalize(row, self.provider):
                        on_event(kind, text, detail)
        finally:
            stderr = (await proc.stderr.read()).decode("utf-8", "replace")
            rc = await proc.wait()
        if error:
            raise RuntimeError(error)
        if rc and not response:
            raise RuntimeError("Headless claude exited %d: %s" % (rc, stderr.strip()[:400]))
        return response, new_sid

    async def cancel(self):
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()


class Broker:
    def __init__(self, state_home, collate=None, capture=None):
        from mct_arbitration import Arbitration
        self.home = Path(state_home)
        self.store = Store(self.home / "mct" / "renderer")
        self.arbitration = Arbitration(self.store)
        self.collate, self.capture = collate, capture
        self.b_state = {}
        self.b_retry = {}
        self.transport = os.environ.get("SEAT_TRANSPORT", "tmux").strip().lower()
        if self.transport == "headless":
            self.adapters = {name: HeadlessAdapter(name, lambda: oauth_token(self.home)) for name in NATIVE_SEATS}
        else:
            self.adapters = {name: NativeAdapter(name) for name in NATIVE_SEATS}
        self.locks = {name: asyncio.Lock() for name in NATIVE_SEATS}
        self.errors = {}
        self.reconciled = set()
        try:
            self.store.reclassify_harness()   # idempotent repair of pre-classification ledgers
        except sqlite3.Error:
            pass

    def maintenance(self, provider):
        if provider != 'codex':
            return ''
        try:
            doc = json.loads((self.store.root / 'codex-restart.json').read_text())
            if doc.get('status') != 'complete':
                return doc.get('message') or 'Codex restart is pending; messages remain saved'
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            return 'Codex restart status is unreadable; delivery paused'
        return ''

    def recover_prompt(self, text):
        # Import existing pointer-bridge turns without showing their internal
        # path. Only recover sources under this station's own prompt inboxes.
        m = re.match(r"Handle the operator prompt at (.+?/prompt\.md)(?:\s|$)", text)
        if not m:
            return text, {}
        path = Path(m[1]).resolve()
        try:
            path.relative_to(self.home.resolve() / "mct")
            if path.parent.parent.name != "prompt-inbox":
                raise ValueError("not an inbox")
            return path.read_text(encoding="utf-8"), {"imported": True, '_files': [str(p) for p in sorted(path.parent.iterdir()) if p.is_file() and p.name != 'prompt.md']}
        except (OSError, ValueError):
            return "Earlier operator prompt (original file unavailable)", {"unavailable": True}

    def read_events(self, provider, source, epoch):
        self.store.adopt(provider, source, epoch)
        offset = self.store.state(provider)["offset"]
        if Path(source).stat().st_size < offset:
            raise RuntimeError("The native transcript shrank; automatic replay is paused.")
        count = 0
        with open(source, "rb") as f:
            f.seek(offset)
            while count < 500:
                start = f.tell()
                raw = f.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    return False
                try:
                    items = normalize(json.loads(raw), provider, self.recover_prompt)
                except (ValueError, TypeError, AttributeError):
                    items = [("error", "An invalid native transcript record was archived.", {})]
                self.store.ingest(provider, source, start, raw, items, f.tell())
                count += 1
            return f.tell() >= Path(source).stat().st_size

    # ---- headless transport (SEAT_TRANSPORT=headless) -----------------------
    def _session_dir(self):
        d = self.store.root / "sessions"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    def _session_load(self, provider):
        try:
            return (self._session_dir() / (provider + ".session")).read_text().strip() or None
        except OSError:
            return None

    def _session_store(self, provider, sid):
        if sid:
            (self._session_dir() / (provider + ".session")).write_text(sid)

    def _headless_prompt(self, turn):
        """The prompt A answers: the (possibly B-compiled) context's operator
        prompt, read straight from the ledger without minting a Read event."""
        with self.store.connect() as c:
            row = c.execute("SELECT body FROM objects WHERE id=?", ((turn.get("context") or "").rsplit("/", 1)[-1],)).fetchone()
            if row:
                try:
                    manifest = json.loads(bytes(row["body"]))
                    prompt = c.execute("SELECT body FROM objects WHERE id=?", (manifest["prompt"].rsplit("/", 1)[-1],)).fetchone()
                    if prompt:
                        return bytes(prompt["body"]).decode("utf-8")
                except (ValueError, KeyError, UnicodeDecodeError):
                    pass
        return turn.get("prompt") or ""

    async def _run_headless(self, provider, turn):
        prompt = await asyncio.to_thread(self._headless_prompt, turn)
        with self.store.connect() as c:
            c.execute("UPDATE turns SET status='working',updated=? WHERE id=? AND status NOT IN ('complete','cancelled')", (time.time(), turn["id"]))
            c.execute("UPDATE seats SET busy=1,current_turn=? WHERE seat=?", (turn["id"], provider))
        session_id = self._session_load(provider)

        def emit(kind, text, detail):
            if kind not in ("commentary", "activity", "error"):
                return  # the terminal assistant text is the published response
            with self.store.connect() as c:
                self.store._event(c, provider, turn["id"], kind, text, detail)

        try:
            response, new_sid = await self.adapters[provider].run(prompt, session_id, on_event=emit)
        except Exception as exc:
            self._session_store(provider, "")  # drop a stale session so the next turn starts fresh
            with self.store.connect() as c:
                c.execute("UPDATE turns SET status='failed',updated=? WHERE id=? AND status NOT IN ('complete','cancelled')", (time.time(), turn["id"]))
                self.store._event(c, provider, turn["id"], "error", "Headless seat error: " + str(exc)[:400])
                c.execute("UPDATE seats SET busy=0,current_turn='' WHERE seat=?", (provider,))
            raise
        self._session_store(provider, new_sid)
        body = (response or "").strip() or "(the headless agent returned no text)"
        with self.store.connect() as c:
            cancelled = c.execute("SELECT 1 FROM turns WHERE id=? AND status='cancelled'", (turn["id"],)).fetchone()
        if not cancelled:
            await asyncio.to_thread(self.store.publish, provider, body.encode("utf-8"), turn["id"])
        with self.store.connect() as c:
            c.execute("UPDATE seats SET busy=0,current_turn='' WHERE seat=?", (provider,))

    async def _tick_headless(self, provider, allow_send=True):
        async with self.locks[provider]:
            try:
                adapter = self.adapters[provider]
                if self.b_retry.get(provider, 0) > time.monotonic():
                    return
                self.errors.pop(provider, None)
                if not (allow_send and not self.maintenance(provider)):
                    return
                for control in self.arbitration.controls(provider):
                    if control['lane'] == 'capture':
                        if self.capture is None:
                            raise RuntimeError('B capture is unavailable; the message is saved')
                        try:
                            reference = await asyncio.to_thread(self.capture, self.arbitration.payload([control])[0])
                        except Exception:
                            self.b_retry[provider] = time.monotonic() + 30
                            raise
                        self.arbitration.captured(control['id'], reference)
                    else:
                        with self.store.connect() as c:
                            active = c.execute("SELECT * FROM turns WHERE seat=? AND status IN ('sending','sent','working','uncertain') ORDER BY created LIMIT 1", (provider,)).fetchone()
                        if active and self.store.cancel(provider, active['id']):
                            await adapter.cancel()
                        self.arbitration.reconcile(control['id'], dict(active) if active else None)
                batch = self.arbitration.batch(provider)
                if not batch:
                    self.b_state[provider] = 'B holds incoming messages for the next turn boundary'
                    return
                if not batch[0]['mediated']:
                    if self.collate is None:
                        raise RuntimeError('B mediation is unavailable; messages are saved, not sent to A')
                    self.b_state[provider] = 'B is compiling the pending operator messages'
                    try:
                        digest = await asyncio.to_thread(self.collate, self.arbitration.payload(batch))
                        if not self.arbitration.commit(provider, batch, digest):
                            self.b_state[provider] = 'B received another message; updating the digest'
                            return
                    except Exception:
                        self.b_retry[provider] = time.monotonic() + 30
                        self.b_state[provider] = 'Waiting for B; operator messages remain saved'
                        raise
                self.b_state[provider] = 'B digest ready for A'
                turn = self.store.next_turn(provider, turn_id=batch[0]['id'])
                if turn:
                    await self._run_headless(provider, turn)
            except Exception as exc:
                self.errors[provider] = str(exc)

    async def tick(self, provider, allow_send=True):
        if self.transport == "headless":
            return await self._tick_headless(provider, allow_send)
        async with self.locks[provider]:
            try:
                adapter = self.adapters[provider]
                # Pass the transcript already adopted for this seat so a shared
                # config directory cannot make the choice flap (t272).
                source, epoch = await asyncio.to_thread(adapter.probe, self.store.state(provider)["source"])
                caught_up = await asyncio.to_thread(self.read_events, provider, source, epoch)
                if caught_up and provider not in self.reconciled:
                    await asyncio.to_thread(self.store.reconcile_delivered, provider)
                    self.reconciled.add(provider)
                if self.b_retry.get(provider, 0) > time.monotonic():
                    return
                self.errors.pop(provider, None)
                if caught_up and allow_send and not self.maintenance(provider):
                    # C's disposition is deterministic; only B compiles wording.
                    for control in self.arbitration.controls(provider):
                        if control['lane'] == 'capture':
                            if self.capture is None:
                                raise RuntimeError('B capture is unavailable; the message is saved')
                            try:
                                reference = await asyncio.to_thread(self.capture, self.arbitration.payload([control])[0])
                            except Exception:
                                self.b_retry[provider] = time.monotonic() + 30
                                raise
                            self.arbitration.captured(control['id'], reference)
                        else:
                            with self.store.connect() as c:
                                active = c.execute("SELECT * FROM turns WHERE seat=? AND status IN ('sending','sent','working','uncertain') ORDER BY created LIMIT 1", (provider,)).fetchone()
                            if active and self.store.cancel(provider, active['id']):
                                await adapter.cancel()
                            self.arbitration.reconcile(control['id'], dict(active) if active else None)
                    batch = self.arbitration.batch(provider)
                    if not batch:
                        self.b_state[provider] = 'B holds incoming messages for the next turn boundary'
                        return
                    if not batch[0]['mediated']:
                        if self.b_retry.get(provider, 0) > time.monotonic():
                            return
                        if self.collate is None:
                            raise RuntimeError('B mediation is unavailable; messages are saved, not sent to A')
                        self.b_state[provider] = 'B is compiling the pending operator messages'
                        try:
                            digest = await asyncio.to_thread(self.collate, self.arbitration.payload(batch))
                            if not self.arbitration.commit(provider, batch, digest):
                                self.b_state[provider] = 'B received another message; updating the digest'
                                return
                        except Exception:
                            self.b_retry[provider] = time.monotonic() + 30
                            self.b_state[provider] = 'Waiting for B; operator messages remain saved'
                            raise
                    self.b_state[provider] = 'B digest ready for A'
                    # Select the prepared root, even when an older append waits behind a digest.
                    turn = self.store.next_turn(provider, turn_id=batch[0]['id'])
                    if turn:
                        try:
                            ok = await adapter.send(pointer_line(turn))
                        except Exception:
                            ok = False
                        self.store.sent(turn["id"], ok)
            except Exception as exc:
                self.errors[provider] = str(exc)

    async def cancel(self, provider, turn):
        async with self.locks[provider]:
            active = self.store.cancel(provider, turn)
            if active:
                await self.adapters[provider].cancel()


def _home():
    # Per-user state dir. Match server.py / seat-provision.sh / abstract_claude:
    # ~/.config/hugpy-station (XDG-aware), NOT ~/hugpy-station. HUGPY_STATION_STATE
    # overrides. (The vm_mgr app base /srv/vm_mgr/hugpy-station is a different thing.)
    return Path(os.environ.get("HUGPY_STATION_STATE") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"),
        "hugpy-station"))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    sub = argv[0] if argv and argv[0] in ("pull", "push", "info") else ""
    if sub:
        argv = argv[1:]
    if sub == "push":
        parser = argparse.ArgumentParser(prog="mct-push", description="Publish a reply/artifact to the MCT ledger; prints its mct:// pointer.")
        parser.add_argument("--seat", default="", help="native seat (codex|claude-code); default: $MCT_SEAT or the enclosing tmux seat")
        parser.add_argument("--turn", default="", help="turn id or any mct:// handle of the turn; default: the seat's turn in flight")
        parser.add_argument("--kind", default="response", choices=PUBLISH_KINDS)
        parser.add_argument("--name", default="", help="object name (reply.md)")
        parser.add_argument("--mime", default="", help="media type (guessed from --name)")
        parser.add_argument("--file", default="", help="read the body from this file instead of stdin")
        args = parser.parse_args(argv)
        seat = args.seat or seat_from_env()
        if not seat:
            parser.error("--seat is required outside a native seat (codex|claude-code)")
        body = Path(args.file).read_bytes() if args.file else sys.stdin.buffer.read()
        store = Store(_home() / "mct" / "renderer")
        print(store.publish(seat, body, args.turn or None, args.kind, args.name, args.mime))
        return
    parser = argparse.ArgumentParser(prog="mct-pull", description="Resolve an immutable object from the MCT ledger.")
    parser.add_argument("handle")
    parser.add_argument("--output")
    parser.add_argument("--reply-file", help="Publish the completed response_file for this context (legacy; prefer mct-push)")
    parser.add_argument("--info", action="store_true", help="print the object's metadata (JSON) instead of its body")
    args = parser.parse_args(argv)
    store = Store(_home() / "mct" / "renderer")
    if args.reply_file:
        print(store.publish_reply(args.handle, args.reply_file))
        return
    if sub == "info" or args.info:
        print(json.dumps(store.describe(args.handle), indent=1))
        return
    raw = store.pull(args.handle, args.output)
    if not args.output:
        sys.stdout.buffer.write(raw + b"\n")


if __name__ == "__main__":
    main()
