"""Open-todos digest (1.0.144, board [F3.8]) — pure logic, no I/O.

Operator 2026-10-01: "make sure the keeper is pinged on the remaining todos
until they are all done" — and "take B out of it for now". Until 1.0.143 the
reminder cycle only BUFFERED per-item reminders into the serve reminders panel
(prompt/submit), every item went through B's verdict (which could withhold
it), and a 3-strike cap silenced open work. Nothing reached the keeper session.

Now the reminder is deterministic: board state -> ONE digest per locus ->
delivered INTO that locus's keeper serve session (server.py), repeated on a
cadence for as long as anything stays open. No B, no attempt cap:

  digest_rows(rows)            the items the keeper is pinged about
  build(rows, locus, now, prev_snap=…) -> (text, n) — a size-capped DELTA digest
  decide(state, n, now, cadence, busy, sig) -> send | wait | skip | unreachable | idle

Excluded on purpose: directions (standing rules, not work), bookmarks and
proposals, '[prompt] …' board mirrors (the reminder cycle's own batches — the
self-loop guard), and FINDINGS (bug reports / log-scan detections go ONLY to
the locus's WORKER session, operator 2026-10-01) — see excluded_from_keeper_digest.
A future reminder MITIGATOR (board [F2.5]) may annotate or prioritise this
digest; it must never suppress it.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

DIGEST_TYPES = ("todo", "request", "operator")
OPEN_STATUSES = ("open", "doing")
PRIO_RANK = {"high": 0, "medium": 1, "low": 2}
DEFAULT_CADENCE_S = 1800
MIN_CADENCE_S = 300          # flood floor: never more often than this, whatever the env says
TEXT_MAX = 120
DEFAULT_TOP_N = 5
DEFAULT_MAX_CHARS = 1500
UNCHANGED_FLOOR_S = 7200     # an unchanged board is re-sent after 2 h — never silent forever
_PARENT_RE = re.compile(r"^\s*\[(F\d+)(?:\.(\d+))?\]\s*")
_ID_NUM = re.compile(r"(\d+)")

# The finding marker is set by the findings re-route (branch st-nosupp): type
# "finding", or a "[finding] …" text prefix. ONE predicate so the marker can be
# adjusted at merge.
FINDING_TYPES = ("finding",)
FINDING_PREFIXES = ("[finding]",)


def excluded_from_keeper_digest(it):
    """True for a row the KEEPER digest must never carry (findings: worker-only)."""
    typ = str((it or {}).get("type") or "").strip().lower()
    text = str((it or {}).get("text") or "").lstrip().lower()
    return typ in FINDING_TYPES or text.startswith(FINDING_PREFIXES)


def digest_rows(rows):
    """Open/doing todo|request|operator rows, minus findings and [prompt] mirrors."""
    out = []
    for it in rows or []:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        if str(it.get("type") or "todo").lower() not in DIGEST_TYPES:
            continue
        if str(it.get("status") or "open").lower() not in OPEN_STATUSES:
            continue
        if excluded_from_keeper_digest(it):
            continue
        text = str(it.get("text") or "")
        if text.startswith("[prompt]") or str(it.get("source") or "") == "prompt.submit":
            continue
        if text.startswith("[digest]") or str(it.get("source") or "") == "station.digest":
            continue
        if str(it.get("id")) == "cfg-relay":
            continue
        out.append(it)
    return out


def parent_of(it):
    """'F3' for '[F3.8] …' or '[F3] …', '' otherwise."""
    m = _PARENT_RE.match(str((it or {}).get("text") or ""))
    return m.group(1) if m else ""


def is_parent_row(it):
    m = _PARENT_RE.match(str((it or {}).get("text") or ""))
    return bool(m) and m.group(2) is None


def _id_num(it):
    m = _ID_NUM.search(str(it.get("id") or ""))
    return int(m.group(1)) if m else 0



def snapshot(rows):
    """{id: {status, updated, note_hash, text, priority}} of the digest set —
    what the NEXT digest diffs against."""
    out = {}
    for it in digest_rows(rows):
        note = str(it.get("note") or "")
        out[str(it["id"])] = {"status": str(it.get("status") or "open"),
                              "updated": int(it.get("updated") or it.get("ts") or 0),
                              "note_hash": hashlib.sha1(note.encode("utf-8", "replace")).hexdigest()[:10],
                              "text": _one(it.get("text"), 90), "priority": str(it.get("priority") or "medium")}
    return out


def signature(rows):
    """Identity of the open set INCLUDING edits (status, updated, note) — equal
    signatures = nothing changed since the last digest."""
    snap = snapshot(rows)
    return hashlib.sha1(json.dumps(snap, sort_keys=True).encode()).hexdigest()[:16]


def delta(prev_snap, rows):
    """-> {added: [rows], closed: [(id, text)], acted: [rows]} vs the snapshot the
    last DELIVERED digest carried. acted = the item was updated / re-noted /
    commented since (the keeper's ack on a listed id)."""
    prev = prev_snap or {}
    cur_rows = {str(it["id"]): it for it in digest_rows(rows)}
    cur = snapshot(rows)
    added = [cur_rows[i] for i in cur if i not in prev]
    closed = [(i, prev[i].get("text") or "") for i in prev if i not in cur]
    acted = [cur_rows[i] for i in cur if i in prev and (
        cur[i]["updated"] != prev[i].get("updated") or cur[i]["note_hash"] != prev[i].get("note_hash")
        or cur[i]["status"] != prev[i].get("status"))]
    return {"added": added, "closed": closed, "acted": acted}


_NEXT_RE = re.compile(r"^\s*(?:[-*]\s*)?(acceptance|accept|next(?: step)?|done when|fix)\s*[:=]\s*(.+)$", re.I | re.M)


def next_step(it):
    """The acceptance / next-step line of an item's note, '' when it has none."""
    m = _NEXT_RE.search(str(it.get("note") or ""))
    return _one(m.group(1).capitalize() + ": " + m.group(2), 110) if m else ""


def _one(text, n):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def _age_s(now, it):
    t = int(it.get("created") or it.get("ts") or 0)
    return max(0, int(now - t)) if t else 0


def _fmt_age(s):
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def build(rows, locus, now=None, cadence_s=DEFAULT_CADENCE_S, prev_snap=None,
          top_n=DEFAULT_TOP_N, max_chars=DEFAULT_MAX_CHARS):
    """-> (text, count). A DELTA digest (keeper 2026-10-01: a full 60-item list
    every 30 min wastes the receiving context and trains it to skim):
      header   open count by parent (F1..Fn, other) and priority + oldest age
      changed  added / closed / acted-on since the last delivered digest
      top N    open items by priority then age, + acceptance / next step
      pull     how to get the full list (todo_list locus=… status=open)
    Hard-capped at max_chars."""
    now = time.time() if now is None else now
    items = [it for it in digest_rows(rows) if not is_parent_row(it)]
    parents = [it for it in digest_rows(rows) if is_parent_row(it)]
    if not items and not parents:
        return "", 0
    allr = items + parents
    n = len(allr)
    byp = {}
    for it in allr:
        byp[parent_of(it) or "other"] = byp.get(parent_of(it) or "other", 0) + 1

    def pkey(p):
        m = re.match(r"F(\d+)$", p)
        return (0, int(m.group(1))) if m else (1, 0)

    pr = {k: sum(1 for it in allr if str(it.get("priority") or "medium") == k) for k in ("high", "medium", "low")}
    oldest = max(allr, key=lambda it: _age_s(now, it))
    head = ("⏱ OPEN-TODOS DIGEST · %s · %s · %d open (%s) · high %d / med %d / low %d · oldest %s (%s)"
            % (locus, time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), n,
               " ".join("%s %d" % (p, byp[p]) for p in sorted(byp, key=pkey)),
               pr["high"], pr["medium"], pr["low"], _fmt_age(_age_s(now, oldest)), oldest.get("id")))
    lines = [head]
    if prev_snap is None:
        lines.append("Changed: first digest (baseline).")
    else:
        d = delta(prev_snap, rows)
        ch = ([("+ %s %s" % (it.get("id"), _one(it.get("text"), 80))) for it in d["added"]]
              + [("✓ %s closed — %s" % (i, _one(t, 70))) for i, t in d["closed"]]
              + [("✎ %s acted on — %s" % (it.get("id"), _one(it.get("text"), 70))) for it in d["acted"]])
        if ch:
            lines.append("Changed since last digest (+added ✓closed ✎acted-on):")
            lines.extend(ch[:12])
            if len(ch) > 12:
                lines.append("… +%d more changes" % (len(ch) - 12))
        else:
            lines.append("Changed since last digest: nothing.")
    top = sorted(items or parents, key=lambda it: (PRIO_RANK.get(str(it.get("priority")), 1),
                                                    -_age_s(now, it), _id_num(it)))[:max(1, int(top_n))]
    lines.append("Top %d (priority, then oldest):" % len(top))
    for it in top:
        ln = "- %s [%s%s] %s (%s)" % (it.get("id"), str(it.get("priority") or "medium"),
                                       " doing" if str(it.get("status")) == "doing" else "",
                                       _one(it.get("text"), 100), _fmt_age(_age_s(now, it)))
        nx = next_step(it)
        lines.append(ln + (("\n    → " + nx) if nx else ""))
    tail = ("Full list: todo_list locus=%s status=open · close with todo_done; comment blockers on the item. "
            "Repeats every %d min while anything is open." % (locus, max(1, int(cadence_s // 60))))
    return _cap(lines, tail, max_chars), n


def _cap(lines, tail, max_chars):
    """Header + body + pull line, body lines dropped from the end (with a cut
    marker) until the text fits max_chars. The header and pull line always stay."""
    max_chars = max(400, int(max_chars))
    head, body = lines[0], list(lines[1:])

    def join(b):
        return "\n".join([head] + b + [tail]) + "\n"
    if len(join(body)) <= max_chars:
        return join(body)
    cut = "… (cut to fit %d chars)" % max_chars
    while body and len(join(body + [cut])) > max_chars:
        body.pop()
    text = join(body + [cut])
    return text if len(text) <= max_chars else text[:max_chars - 2] + "…\n"


def cadence(env_value):
    try:
        v = int(env_value)
    except (TypeError, ValueError):
        return DEFAULT_CADENCE_S
    return max(MIN_CADENCE_S, v)


def decide(state, n_open, now, cadence_s, busy, sig="", unchanged_floor_s=None):
    """What the digest loop does for one locus this tick.
      idle         nothing open — no digest (an item that closes drops out by itself)
      wait         delivered less than one cadence ago
      skip         NOTHING changed since the last delivered digest and that was under
                   the unchanged floor (2 h) ago — recorded on the board, never silent
      unreachable  due, but the keeper session cannot be read (serve down / no head):
                   recorded as NOT delivered, retried next tick
      send         due — idle (runs now) OR mid-turn (submitted through the console
                   queue, it goes out at the turn boundary: state "queued")
    ``state`` = {last_delivered, last_sig}. No attempt cap: an unchanged board
    is re-sent after the floor, so the digest repeats until everything is done.
    1.0.146 (t4260 / d4254): there is no "defer" any more — a busy keeper does not
    hold the digest for an unbounded number of ticks; it QUEUES behind the turn."""
    floor = UNCHANGED_FLOOR_S if unchanged_floor_s is None else unchanged_floor_s
    if n_open <= 0:
        return "idle"
    last = float((state or {}).get("last_delivered") or 0)
    if last and now - last < cadence_s:
        return "wait"
    if last and sig and sig == (state or {}).get("last_sig") and now - last < floor:
        return "skip"
    if busy is None:
        return "unreachable"
    return "send"
