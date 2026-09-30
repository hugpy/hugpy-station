"""Keeper relay nudge (1.0.125) — pure planning + target order, no I/O of its own.

Operator 2026-09-30: the "📨 N new board pings for keeper" nudge must go to the
keeper's session in SERVE (abstract-claude serve, POST
/api/session/<role>/message — serve runs it now when idle, or queues it for the
turn boundary when busy; it never interrupts a turn). The legacy tmux keeper
pane is a FALLBACK used only when serve is unreachable / unhealthy / has no
live keeper session — never in parallel. Until 1.0.124 the station typed the
line into the tmux pane only.

  plan(pending, open_ids, now, last_sent)  -> which pings to announce, which to drop
  summarize(items, locus)                  -> ONE line: count, distinct count, latest, instruction
  deliver(line, serve, tmux)               -> {"target": serve|tmux|none, "detail"}

Rules: a ping closed since it arrived is dropped at send time and never
counted; at most one nudge per MIN_INTERVAL_S (5 min) summarising everything
pending; pings that say the same thing from several stations (same source +
signature once locus tags, counts and times are stripped) count once.
"""
from __future__ import annotations

import re

MIN_INTERVAL_S = 300

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
        return {"send": False, "items": [], "dropped": [], "wait_s": 0, "why": "board status unknown"}
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


async def deliver(line, serve, tmux):
    """Serve ALWAYS first; tmux ONLY when serve is unreachable/unhealthy.
    ``serve``/``tmux``: async callables line -> (ok, detail)."""
    try:
        ok, detail = await serve(line)
    except Exception as e:                                   # noqa: BLE001 — serve down
        ok, detail = False, "serve error: %s" % str(e)[:160]
    if ok:
        return {"target": "serve", "detail": detail}
    try:
        ok2, d2 = await tmux(line)
    except Exception as e:                                   # noqa: BLE001
        ok2, d2 = False, "tmux error: %s" % str(e)[:160]
    if ok2:
        return {"target": "tmux", "detail": "%s (serve unavailable: %s)" % (d2, detail)}
    return {"target": "none", "detail": "serve: %s; tmux: %s" % (detail, d2)}
