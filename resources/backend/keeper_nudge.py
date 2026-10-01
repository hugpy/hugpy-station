"""Keeper relay nudge (1.0.125) — pure planning + target order, no I/O of its own.

Operator 2026-09-30: the "📨 N new board pings for keeper" nudge must go to the
keeper's session in SERVE (abstract-claude serve, POST
/api/session/<role>/message — serve runs it now when idle, or queues it for the
turn boundary when busy; it never interrupts a turn). The legacy tmux keeper
pane is a FALLBACK used only when serve is unreachable / unhealthy / has no
live keeper session — never in parallel. Until 1.0.124 the station typed the
line into the tmux pane only.

1.0.140: the tmux fallback is OFF unless STATION_NUDGE_TMUX=1 (a serve failure
leaves the item pending + logged); the target is the keeper SERVE SESSION,
resolved fresh per send (roster role -> rollover chain head), and a nudge
queued behind a turn on an auto=off session is kicked once the session is idle
(drain_action) — nothing is left queued that nothing drains.

  plan(pending, open_ids, now, last_sent)  -> which pings to announce, which to drop
  summarize(items, locus)                  -> ONE line: count, distinct count, latest, instruction
  deliver(line, serve, tmux)               -> {"target": serve|tmux|none, "detail"}
  resolve_keeper_session(roster, rollover) -> (sid, why)  the live keeper session head
  drain_action(watch, queue, head, now)    -> done|wait|kick|requeue
  queue_action_ok(status, doc)             -> did POST /api/console/queue succeed (0.1.14 / 0.1.15 shapes)

Rules: a ping closed since it arrived is dropped at send time and never
counted; at most one nudge per MIN_INTERVAL_S (5 min) summarising everything
pending; pings that say the same thing from several stations (same source +
signature once locus tags, counts and times are stripped) count once.

1.0.146 (board t4260, operator ruling d4254): a "wait" behind a busy turn is
BOUNDED (WAIT_MAX_S, 15 min) — past it the hand-off is re-pended ("requeue")
with an audit line and a strip counter, never an open-ended wait. A serve-core
0.1.15 hold ({by, until}) is an owned, expiring operator action: it is waited
out, never released by the station. The queue action the station sends to
start a stalled queue is "release" (0.1.15; 0.1.14 knew it as "retry").
"""
from __future__ import annotations

import os
import re

MIN_INTERVAL_S = 300
WAIT_MAX_S = 900             # 1.0.146: a hand-off waits behind a busy turn at most this long
QUEUE_RELEASE = "release"    # serve-core 0.1.15 action name ("retry" = 0.1.14 alias, one release)

_TAG_RE = re.compile(r"^\s*(?:\[(?:ping|msg)\]\s*)*(?:\[[a-z0-9@._-]+\]\s*)?", re.I)
_NORM = [
    (re.compile(r"×\s*\d+|x\d+\b"), "×N"),
    (re.compile(r"\b\d\d:\d\d(?::\d\d)?\b"), "<t>"),
    (re.compile(r"\b\d{4}-\d\d-\d\d\b"), "<d>"),
    (re.compile(r"\d+(?:[.,]\d+)*"), "N"),
    (re.compile(r"\s+"), " "),
]


def group_key(p):
    """(source, signature) of a ping, stripped of the originating-locus tag,
    counts and times — so seven stations reporting one loop are one group."""
    first = str((p or {}).get("text") or "").strip().splitlines()[0] if (p or {}).get("text") else ""
    s = _TAG_RE.sub("", first)
    for rx, rep in _NORM:
        s = rx.sub(rep, s)
    return s.strip().lower()[:160]


def plan(pending, open_ids, now, last_sent=0.0, min_interval=MIN_INTERVAL_S):
    """-> {"send": bool, "items": [open pings], "dropped": [ids closed since], "wait_s": n}.
    ``open_ids`` is the board's open set read AT SEND TIME (None = unknown:
    nothing is sent, so a closed item can never be announced)."""
    if open_ids is None:
        # 1.0.146: NOT a silent no-send — the caller emits comms-nudge-skip and a
        # strip counter ("toolserver unreachable, N pending"); the items stay pending
        return {"send": False, "items": list(pending or []), "dropped": [], "wait_s": 0,
                "why": "board status unknown"}
    open_ids = {str(i) for i in open_ids}
    items = [p for p in pending or [] if str(p.get("id")) in open_ids]
    dropped = [str(p.get("id")) for p in pending or [] if str(p.get("id")) not in open_ids]
    if not items:
        return {"send": False, "items": [], "dropped": dropped, "wait_s": 0, "why": "nothing open"}
    wait = max(0, int(min_interval - (now - float(last_sent or 0))))
    if wait > 0:
        return {"send": False, "items": items, "dropped": dropped, "wait_s": wait, "why": "batch window"}
    return {"send": True, "items": items, "dropped": dropped, "wait_s": 0, "why": ""}


def summarize(items, locus):
    n = len(items)
    groups = []
    for p in items:
        k = group_key(p)
        if k not in groups:
            groups.append(k)
    latest = items[-1]
    distinct = "" if len(groups) >= n else " (%d distinct)" % len(groups)
    text = str(latest.get("text") or "").strip().splitlines()[0] if latest.get("text") else ""
    return ("📨 %d new board ping%s for %s%s — latest %s: %s … read them with comms_inbox to=%s and answer on the board"
            % (n, "s" if n > 1 else "", locus, distinct, latest.get("id"), text[:140], locus))


def tmux_enabled(env=None):
    """STATION_NUDGE_TMUX (1.0.140, default 0): the tmux keeper pane is reached
    ONLY when this is explicitly 1. Off = a serve failure leaves the item
    pending on the board and is logged — never typed into a terminal."""
    env = os.environ if env is None else env
    return str(env.get("STATION_NUDGE_TMUX") or "0").strip().lower() in ("1", "true", "on", "yes")


async def deliver(line, serve, tmux, allow_tmux=None):
    """Serve ALWAYS first; tmux ONLY when serve failed AND STATION_NUDGE_TMUX=1.
    ``serve``/``tmux``: async callables line -> (ok, detail)."""
    if allow_tmux is None:
        allow_tmux = tmux_enabled()
    try:
        ok, detail = await serve(line)
    except Exception as e:                                   # noqa: BLE001 — serve down
        ok, detail = False, "serve error: %s" % str(e)[:160]
    if ok:
        return {"target": "serve", "detail": detail}
    if not allow_tmux:
        return {"target": "none", "detail": "serve: %s; tmux fallback off (STATION_NUDGE_TMUX=0) — left pending"
                % detail}
    try:
        ok2, d2 = await tmux(line)
    except Exception as e:                                   # noqa: BLE001
        ok2, d2 = False, "tmux error: %s" % str(e)[:160]
    if ok2:
        return {"target": "tmux", "detail": "%s (serve unavailable: %s)" % (d2, detail)}
    return {"target": "none", "detail": "serve: %s; tmux: %s" % (detail, d2)}


# ── 1.0.140: the keeper SERVE SESSION, resolved fresh on every send ─────────────────
MAX_HOPS = 20


def resolve_keeper_session(roster, rollover=None, role="keeper"):
    """-> (session_id, why). The roster's role binding, then serve's rollover
    chain (rollover.json ``archived[old].successor``) followed to its live head:
    the binding drifts across rollover / restart / wipe, so never cache it."""
    ent = next((e for e in (roster or {}).get("roles") or [] if isinstance(e, dict)
                and str(e.get("role") or "").lower() == role), None)
    sid = str((ent or {}).get("session_id") or "")
    if not sid:
        return "", "no live %s session in serve" % role
    archived = (rollover or {}).get("archived") or {}
    seen, why = {sid}, "roster"
    for _ in range(MAX_HOPS):
        nxt = str((archived.get(sid) or {}).get("successor") or "")
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        sid, why = nxt, "rollover head"
    return sid, why


def drain_action(watch, queue, head_sid, now, held_grace_s=120, wait_max_s=WAIT_MAX_S):
    """What to do about a nudge already handed to serve (``watch`` = {sid,
    message_ids, at}). ``queue`` = GET /api/console/queue of watch.sid (None =
    the session is gone). -> one of
      "done"    our messages left the queue (running / done / failed): drained
      "wait"    still queued behind a busy turn (serve drains it at the boundary)
                or behind an OWNED, EXPIRING hold (serve-core 0.1.15 {by, until}) —
                BOUNDED: past wait_max_s (15 min) it becomes "requeue"
      "kick"    queued and nothing will start it (idle, or auto=off after the turn,
                or a vacuous hold on OUR messages only): POST queue action=release
      "requeue" the session is not the keeper head any more (rollover / wipe), is
                gone, holds other held originals, or the bounded wait ran out: pull
                ours out, re-pend the pings
    """
    mids = {str(m) for m in (watch or {}).get("message_ids") or []}
    if queue is None:
        return "requeue"
    queued = {str(i.get("id")) for i in queue.get("items") or [] if isinstance(i, dict)}
    ours = mids & queued
    if not ours:
        return "done"
    if head_sid and str(watch.get("sid")) != str(head_sid):
        return "requeue"
    waited = now - float((watch or {}).get("at") or now)
    others = queued - mids
    hold = queue.get("hold") if isinstance(queue.get("hold"), dict) else None
    if queue.get("paused"):
        if hold:
            # 0.1.15: an operator's hold — owned and expiring. Wait it out (it
            # lifts itself at `until`); never release someone else's hold.
            until = float(hold.get("until") or 0)
            if until:                                # bounded by the hold's own expiry
                return "kick" if now >= until + held_grace_s else "wait"
            return "wait" if waited < wait_max_s else "requeue"
        # 0.1.14: a hold protects someone else's failed batch: never re-dispatch it
        return "requeue" if others else "kick"
    if queue.get("busy"):
        # mid-turn: serve drains it at the boundary — but not forever (d4254)
        return "wait" if waited < wait_max_s else "requeue"
    return "kick"


def wait_expired(watch, now, wait_max_s=WAIT_MAX_S):
    """True when a watched hand-off has been waiting longer than the bound."""
    return (now - float((watch or {}).get("at") or now)) >= wait_max_s


def queue_action_ok(status, doc):
    """Did POST /api/console/queue succeed? serve-core 0.1.14 answers with the
    queue document ({session_id, items, busy, paused, auto}); 0.1.15 answers
    with the queue document too (plus hold / actions / will_send) or, on an
    error, {error}. Both: HTTP 200 and no error key."""
    if int(status or 0) != 200 or not isinstance(doc, dict):
        return False
    if doc.get("error"):
        return False
    if "ok" in doc and not doc.get("ok"):
        return False
    return True
