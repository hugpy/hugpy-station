#!/usr/bin/env python3
"""
board-mirror — reflect this box's ~/todo.json into the central `board` table.

Operator directive 2026-09-01: one central DB holds every locus's board so a
single agent can see the whole fleet, WITHOUT rewriting the todo CLI or moving
truth off the file. This is the watchdog: the file stays authoritative and
untouched (schema todo.v1, atomic writes, the keeper-relay self-suppression
marker — none of it changes); we just watch the file and upsert a projection.

Design guarantees:
  * READ-ONLY on the file. We never write ~/todo.json. Truth stays local; a keeper
    edit never blocks on the DB.
  * FAIL-OPEN. No psycopg, no DB env, DB unreachable, malformed file -> we log once
    and idle. The board keeps working from the file; the mirror just goes stale
    until the DB is back. Nothing on the delivery path depends on this daemon.
  * LOCUS-SCOPED. We only ever touch rows WHERE locus=<this station>, so many
    boxes mirror into one table without stepping on each other.

Schema (created on first successful connect):
    board(locus, item_id, status, type, ts, updated, data JSONB, PK(locus,item_id))
`data` holds the FULL todo.v1 item (arbitrary fields preserved); the flat columns
are just for indexing/filtering. Items with no string id are skipped (nothing to
key on) — counted and logged, never silently dropped.

Env:
    KEEPER_STATION            this box's locus (else hostname) — same as keeper-relay
    TODO_FILE                 board path (else /home/ubuntu/todo.json)
    BOARD_MIRROR_POLL_SECS    mtime poll interval (default 5)
    SOLCATCHER_POSTGRESQL_*   HOST/PORT/USER/PASS/NAME (else ~/.env dotenv)
    BOARD_MIRROR_ENV_FILE     dotenv fallback path (default ~/.env)
"""
import json
import os
import socket
import sys
import time

DEFAULT_FILE = "/home/ubuntu/todo.json"
POLL_SECS = int(os.environ.get("BOARD_MIRROR_POLL_SECS", "5") or 5)
_ENV_KEYS = ("HOST", "PORT", "USER", "PASS", "NAME")


def log(msg):
    sys.stderr.write("[board-mirror] %s\n" % msg)
    sys.stderr.flush()


# ── identity + file ────────────────────────────────────────────────────────────
def resolve_locus():
    """This box's locus — env KEEPER_STATION else hostname, byte-identical to
    keeper_relay._resolve_station() so the mirror and the relay agree on the id."""
    s = os.environ.get("KEEPER_STATION")
    if s and s.strip():
        return s.strip()
    try:
        h = socket.gethostname()
    except OSError:
        h = ""
    return (h or "").strip() or "blackbird"


def todo_path():
    return os.environ.get("TODO_FILE") or DEFAULT_FILE


def read_board(path):
    """(items, None) or (None, reason). Never raises — a half-written/absent file
    is DATA (the CLI may be mid atomic-replace), so we skip the tick and retry."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, type(exc).__name__
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None, "unexpected-shape"
    return data["items"], None


# ── projection ─────────────────────────────────────────────────────────────────
def item_to_row(locus, item, now):
    """One todo.v1 item -> a board row, or None if it has no usable string id."""
    if not isinstance(item, dict):
        return None
    iid = item.get("id")
    if not isinstance(iid, str) or not iid:
        return None
    return {
        "locus": locus,
        "item_id": iid,
        "status": str(item.get("status") or ""),
        "type": str(item.get("type") or ""),
        "ts": int(item["ts"]) if isinstance(item.get("ts"), (int, float)) else now,
        "updated": now,
        "data": item,
    }


def mirror(store, locus, items, now):
    """Reflect the file's items into `store` for this locus: upsert every valid
    item, then prune rows for this locus whose id is no longer on the board.
    Returns a small stats dict. `store` is any object with upsert(row)/prune(locus,
    keep_ids) — a PgStore in prod, a DictStore in tests."""
    rows, skipped = [], 0
    for it in items:
        r = item_to_row(locus, it, now)
        if r is None:
            skipped += 1
        else:
            rows.append(r)
    for r in rows:
        store.upsert(r)
    keep = [r["item_id"] for r in rows]
    pruned = store.prune(locus, keep)
    return {"locus": locus, "upserted": len(rows), "skipped": skipped, "pruned": pruned}


# ── Postgres store (lazy psycopg; fail-open) ───────────────────────────────────
def _db_kwargs():
    """psycopg connect kwargs from SOLCATCHER_POSTGRESQL_* (env, then dotenv), or
    None if unconfigured. Same resolution shape as the toolserver's db.py."""
    def env(name):
        v = os.environ.get("SOLCATCHER_POSTGRESQL_%s" % name)
        return v.strip() if v else v
    vals = {k: env(k) for k in _ENV_KEYS}
    if not vals["HOST"]:
        path = os.environ.get("BOARD_MIRROR_ENV_FILE", os.path.expanduser("~/.env"))
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    for key in _ENV_KEYS:
                        if k == "SOLCATCHER_POSTGRESQL_%s" % key:
                            vals[key] = vals[key] or v.strip().strip('"').strip("'")
        except OSError:
            pass
    if not vals["HOST"] or not vals["NAME"]:
        return None
    return dict(host=vals["HOST"], port=vals["PORT"] or "5432",
                user=vals["USER"], password=vals["PASS"], dbname=vals["NAME"])


class PgStore:
    """psycopg-backed board store. Import + connect are lazy so a box without the
    driver or DB simply never constructs one (fail-open at the call site)."""
    DDL = (
        """CREATE TABLE IF NOT EXISTS board (
               locus    TEXT   NOT NULL,
               item_id  TEXT   NOT NULL,
               status   TEXT   NOT NULL DEFAULT '',
               type     TEXT   NOT NULL DEFAULT '',
               ts       BIGINT NOT NULL DEFAULT 0,
               updated  BIGINT NOT NULL DEFAULT 0,
               data     JSONB  NOT NULL,
               PRIMARY KEY (locus, item_id)
           )""",
        "CREATE INDEX IF NOT EXISTS board_locus_status_idx ON board(locus, status)",
    )

    def __init__(self, conn):
        self.conn = conn

    @classmethod
    def open(cls):
        kw = _db_kwargs()
        if not kw:
            return None
        try:
            import psycopg
        except ImportError:
            return None
        try:
            conn = psycopg.connect(**kw)
        except Exception:
            return None
        store = cls(conn)
        try:
            store._ensure()
        except Exception:
            store.close()
            return None
        return store

    # Name of the index created by the CREATE INDEX statement in DDL above.
    INDEX_NAME = "board_locus_status_idx"

    def _ensure(self):
        """Apply DDL, tolerating a `board` table owned by another role (t308).

        Postgres checks table ownership BEFORE the IF NOT EXISTS short-circuit of
        CREATE INDEX, so a non-owner with full DML grants still gets
        InsufficientPrivilege on an index that is already there. Probe pg_indexes
        first and skip the statement when the index exists; a genuinely missing
        index still raises, which is the honest failure.
        """
        with self.conn.cursor() as cur:
            for stmt in self.DDL:
                if "CREATE INDEX" in stmt:
                    cur.execute(
                        "SELECT 1 FROM pg_indexes WHERE indexname = %s",
                        (self.INDEX_NAME,))
                    if cur.fetchone():
                        continue
                cur.execute(stmt)
        self.conn.commit()

    def upsert(self, row):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO board (locus,item_id,status,type,ts,updated,data)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)"
                " ON CONFLICT (locus,item_id) DO UPDATE SET"
                " status=EXCLUDED.status, type=EXCLUDED.type, ts=EXCLUDED.ts,"
                " updated=EXCLUDED.updated, data=EXCLUDED.data",
                (row["locus"], row["item_id"], row["status"], row["type"],
                 row["ts"], row["updated"], json.dumps(row["data"])))
        self.conn.commit()

    def prune(self, locus, keep_ids):
        with self.conn.cursor() as cur:
            if keep_ids:
                cur.execute("DELETE FROM board WHERE locus=%s AND NOT (item_id = ANY(%s))",
                            (locus, list(keep_ids)))
            else:
                cur.execute("DELETE FROM board WHERE locus=%s", (locus,))
            n = cur.rowcount
        self.conn.commit()
        return n if isinstance(n, int) and n >= 0 else 0

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ── daemon ─────────────────────────────────────────────────────────────────────
def run_once(locus, path, store):
    items, err = read_board(path)
    if err:
        return None, "read:%s" % err
    stats = mirror(store, locus, items, int(time.time()))
    return stats, None


def main():
    locus = resolve_locus()
    path = todo_path()
    log("start: locus=%s file=%s poll=%ds" % (locus, path, POLL_SECS))
    last_mtime = None
    warned_nodb = False
    while True:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        if mtime != last_mtime:
            store = PgStore.open()
            if store is None:
                if not warned_nodb:
                    log("no DB (driver/env/reachability) — idling, file remains truth")
                    warned_nodb = True
            else:
                warned_nodb = False
                try:
                    stats, err = run_once(locus, path, store)
                    if err:
                        log("skip tick: %s" % err)
                    else:
                        last_mtime = mtime
                        log("mirrored %s" % stats)
                except Exception as exc:
                    log("mirror error (%s) — will retry" % type(exc).__name__)
                finally:
                    store.close()
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
