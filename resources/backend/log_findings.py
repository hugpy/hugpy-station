"""Deterministic log findings for hugpy Station (1.0.124) — grep + parse, no model.

Operator 2026-09-29: "it should be that the errors are grepped and parsed; when
found, presented to B." The 🐞 bug scan used to hand the journal window to the
fleet's one Coder-Next slot under a review prompt (~40k tokens every ~11 min
per locus). This module replaces that inference with a pattern table:

    scan(text, now)             -> [finding]          one per (kind, source, signature)
    merge(prev, finding, now)   -> (row, emit_reason)  dedup + "emit when new / count jumps"
    fmt_finding(row)            -> one line for B / the board

A FINDING::

    {kind, severity, source, count, first_seen, last_seen, sample_lines (<=5),
     signature, suggested_action, key}

``kind`` is one of KINDS. ``signature`` is the message with timestamps, pids,
hex ids, uuids, ips, paths and numbers stripped — the dedup key is
sha1(locus|kind|source|signature). ``suggested_action`` is only ever text the error
itself carries (central's "FIX: ..." sentence, a "hint:" clause) — never
invented. Pure logic: no I/O, no network; server.py gathers and stores.
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime

KINDS = ("crash_loop", "rate_limit_429", "http_5xx", "traceback", "oom",
         "refused", "timeout", "other_error")

# ── the pattern table: first match wins, most specific first ─────────────────
# (kind, severity, regex)
PATTERNS = [
    ("oom", "high", re.compile(
        r"out of memory|oom[-_ ]?kill|invoked oom-killer|Killed process \d+|MemoryError"
        r"|CUDA out of memory|result 'oom-kill'", re.I)),
    ("crash_loop", "high", re.compile(
        r"Scheduled restart job|restart counter is at \d+|Start request repeated too quickly"
        r"|Failed with result 'start-limit-hit'|\bcrash[- ]?loop", re.I)),
    # one failed run of a unit; CRASH_LOOP_UNIT_FAILS of them in one window
    # are promoted to a crash_loop finding by scan()
    ("unit_failed", "medium", re.compile(
        r"Failed with result '[\w-]+'|Main process exited, code=(?:exited|killed|dumped), status=(?!0/)"
        r"|Failed to start \S+", re.I)),
    ("rate_limit_429", "high", re.compile(
        r"\b429\b|Too Many Requests|rate[- _]?limit(?:ed|ing)?\b|RateLimitError", re.I)),
    ("http_5xx", "medium", re.compile(
        r"\bHTTP(?:/\d(?:\.\d)?\"?)? 5\d\d\b|\"\s5\d\d\s|\bstatus[=: ]+5\d\d\b"
        r"|\b50[0234] (?:Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout)",
        re.I)),
    ("refused", "medium", re.compile(
        r"Connection refused|ECONNREFUSED|Errno 111\b|could not connect to", re.I)),
    ("timeout", "medium", re.compile(
        r"\btimed out\b|\bTimeoutError\b|ReadTimeout|ConnectTimeout|deadline exceeded|ETIMEDOUT"
        r"|\btimeout (?:after|waiting)\b", re.I)),
    ("traceback", "medium", re.compile(
        r"Traceback \(most recent call last\)|^\s*(?:[\w.]+\.)?[A-Z]\w*(?:Error|Exception)\b(?::|$)")),
    ("other_error", "low", re.compile(
        r"\b(?:ERROR|CRITICAL|FATAL)\b|\bfatal:|\bpermission denied\b|No space left on device"
        r"|No module named|\bfailed\b|\bexception\b", re.I)),
]

# lines that match a pattern but are not a running problem
IGNORE = [
    re.compile(r"DeprecationWarning|UFW BLOCK|\[UFW ", re.I),
    re.compile(r"\berrors?[=:]\s*(?:None|null|0|\[\]|\{\}|\"\"|'')", re.I),
    re.compile(r"\b0 (?:errors?|failed|failures)\b", re.I),
    re.compile(r"\"(?:GET|POST|PUT|DELETE|HEAD) [^\"]*\" [1-4]\d\d\b"),          # access log non-5xx
    re.compile(r"HTTP/1\.[01]\" [1-4]\d\d\b|HTTP/1\.[01] [1-4]\d\d (?:OK|Created|No Content)"),
]

# section headers written by the station's gather step
_SECTION_RE = re.compile(r"^---\s*(journal|station console backend log|seat panes)\b[^-]*?(?:\(([^)]*)\))?\s*---\s*$",
                         re.I)
# 2026-09-29T18:41:24-05:00 host ident[pid]: message   (journalctl -o short-iso)
_JOURNAL_RE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:[.,]\d+)?(?:[+-]\d\d:?\d\d|Z)?)\s+(?P<host>\S+)\s+"
    r"(?P<ident>[^\s\[:]+)(?:\[\d+\])?:\s?(?P<msg>.*)$")
# classic syslog: Sep 29 18:41:24 host ident[pid]: message
_SYSLOG_RE = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s+\d+\s+\d\d:\d\d:\d\d)\s+(?P<host>\S+)\s+"
    r"(?P<ident>[^\s\[:]+)(?:\[\d+\])?:\s?(?P<msg>.*)$")
# station backend ring: HH:MM:SS LEVEL logger message
_RING_RE = re.compile(r"^(?P<ts>\d\d:\d\d:\d\d)\s+(?P<level>[A-Z]+)\s+(?P<name>\S+)\s+(?P<msg>.*)$")
# seat panes: "<session>: line | line | line"
_SEAT_RE = re.compile(r"^(?P<name>[\w.@-]+):\s(?P<msg>.*)$")
_UNIT_RE = re.compile(r"^([\w@.:-]+\.(?:service|socket|timer|scope|mount|slice)):\s")
# a python logging prefix inside a journal message: "2026-09-29 18:41:24,349 - ERROR - ..."
_PYLOG_RE = re.compile(r"^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?(?:\s+[-|]\s+|\s+)")
# systemd's own lines name the unit after the verb: "Failed to start foo.service - …"
_SYSTEMD_UNIT_RE = re.compile(r"\b(?:Failed to start|Started|Stopped|Starting|Stopping)\s+([\w@.:-]+\.(?:service|socket|timer|mount))\b")

_FIX_RE = re.compile(r"\bFIX:\s*(.+?)(?:(?<=[.!])\s|$)", re.S)
_HINT_RE = re.compile(r"\b(?:hint|try|run):\s*(.+?)(?:(?<=[.!])\s|$)", re.I)

# signature normalisation, applied in order
_NORM = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?"), "<ts>"),
    (re.compile(r"\b\d\d:\d\d:\d\d(?:[.,]\d+)?\b"), "<ts>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"(?:(?<=\s)|(?<=^)|(?<=[\"'(=]))(?:~|\.{1,2})?/[\w.@+-]+(?:/[\w.@+-]*)+"), "<path>"),
    (re.compile(r"\b(?:0x)?[0-9a-f]{12,}\b", re.I), "<id>"),
    (re.compile(r"\b[a-z]+-[0-9a-f]{8,}\b", re.I), "<id>"),
    (re.compile(r"\[\d+\]"), ""),
    (re.compile(r"\d+(?:\.\d+)?"), "N"),
    (re.compile(r"\s+"), " "),
]

CRASH_LOOP_UNIT_FAILS = 3        # unit_failed lines for one unit in one window -> crash_loop
MAX_SAMPLES = 5
SAMPLE_CHARS = 240
SIG_CHARS = 160

# emission (see merge): a known finding re-emits when its cumulative count has
# grown by >= JUMP_FACTOR x AND >= JUMP_MIN since the last emission, or when it
# comes back after STALE_S of silence.
JUMP_FACTOR = 2.0
JUMP_MIN = 3
STALE_S = 3600


def signature(msg: str) -> str:
    s = str(msg or "")
    for rx, rep in _NORM:
        s = rx.sub(rep, s)
    return s.strip()[:SIG_CHARS]


def classify(msg: str):
    """(kind, severity) of one message, or None when it is not a problem line."""
    if not msg or not msg.strip():
        return None
    for rx in IGNORE:
        if rx.search(msg):
            return None
    for kind, sev, rx in PATTERNS:
        if rx.search(msg):
            return kind, sev
    return None


def suggested_action(msg: str) -> str:
    m = _FIX_RE.search(msg or "") or _HINT_RE.search(msg or "")
    return m.group(1).strip()[:400] if m else ""


def _key(kind, source, sig, locus=""):
    return hashlib.sha1(f"{locus}|{kind}|{source}|{sig}".encode("utf-8", "replace")).hexdigest()[:12]


def _parse_ts(raw, now):
    if not raw:
        return None
    raw = raw.replace(",", ".")
    try:
        if "T" in raw:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            if re.search(r"[+-]\d{4}$", raw):
                raw = raw[:-2] + ":" + raw[-2:]
            return datetime.fromisoformat(raw).timestamp()
        if re.match(r"^\d\d:\d\d:\d\d$", raw):                    # ring: today, local
            lt = time.localtime(now)
            h, m, s = (int(x) for x in raw.split(":"))
            t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, s, 0, 0, -1))
            return t - 86400 if t > now + 60 else t
        lt = time.localtime(now)                                   # syslog "Sep 29 18:41:24"
        t = time.mktime(time.strptime(f"{lt.tm_year} {raw}", "%Y %b %d %H:%M:%S"))
        return t
    except (ValueError, OverflowError):
        return None


def parse_line(line, section="journal", now=None):
    """(source, ts|None, message) for one raw line; source names the unit /
    logger / seat the line came from."""
    now = now or time.time()
    line = line.rstrip("\n")
    if section == "seat panes":
        m = _SEAT_RE.match(line.strip())
        if m:
            return "seat:" + m.group("name"), None, m.group("msg")
        return "seat", None, line.strip()
    if section == "station console backend log":
        m = _RING_RE.match(line.strip())
        if m:
            return "station:" + m.group("name"), _parse_ts(m.group("ts"), now), \
                m.group("level") + " " + m.group("msg")
        return "station", None, line.strip()
    m = _JOURNAL_RE.match(line) or _SYSLOG_RE.match(line)
    if m:
        msg = m.group("msg")
        ident = m.group("ident")
        u = _UNIT_RE.match(msg) or (_SYSTEMD_UNIT_RE.search(msg) if ident == "systemd" else None)
        source = u.group(1) if u else ident
        return source, _parse_ts(m.group("ts"), now), _PYLOG_RE.sub("", msg)
    return "log", None, line.strip()


def scan(text, now=None, default_section="journal", locus=""):
    """Group every problem line in ``text`` into findings keyed by
    (kind, source, signature). A traceback header is folded into the exception
    line that closes it (same source), so one traceback = one finding keyed on
    its exception; a header with no exception line stands on its own."""
    now = now or time.time()
    section = default_section
    found = {}
    open_tb = {}                                   # source -> (ts, header line)

    def add(kind, sev, source, ts, msg, raw):
        sig = signature(msg)
        if not sig:
            return
        k = _key(kind, source, sig, locus)
        f = found.get(k)
        if f is None:
            f = found[k] = {"key": k, "kind": kind, "severity": sev, "source": source,
                            "locus": locus, "count": 0, "first_seen": None, "last_seen": None,
                            "sample_lines": [], "signature": sig, "suggested_action": "",
                            "_stamps": []}
        f["count"] += 1
        f["_stamps"].append(ts)
        if ts is not None:
            f["first_seen"] = ts if f["first_seen"] is None else min(f["first_seen"], ts)
            f["last_seen"] = ts if f["last_seen"] is None else max(f["last_seen"], ts)
        sample = raw.strip()[:SAMPLE_CHARS]
        if len(f["sample_lines"]) < MAX_SAMPLES and sample not in f["sample_lines"]:
            f["sample_lines"].append(sample)
        if not f["suggested_action"]:
            f["suggested_action"] = suggested_action(msg)

    for raw in (text or "").splitlines():
        h = _SECTION_RE.match(raw.strip())
        if h:
            section = h.group(1).lower()
            continue
        if not raw.strip():
            continue
        if section == "seat panes":                # "name: a | b | c" — one pane, several lines
            src, ts, msg = parse_line(raw, section, now)
            parts = [p for p in msg.split(" | ") if p.strip()]
        else:
            src, ts, msg = parse_line(raw, section, now)
            parts = [msg]
        for part in parts:
            c = classify(part)
            if c is None:
                continue
            kind, sev = c
            if kind == "traceback" and "Traceback (most recent call last)" in part:
                open_tb[src] = (ts, raw)
                continue
            if kind == "traceback":
                open_tb.pop(src, None)
            add(kind, sev, src, ts, part, raw if section != "seat panes" else f"{src}: {part}")
    for src, (ts, raw) in open_tb.items():
        add("traceback", "medium", src, ts, "Traceback (most recent call last)", raw)
    _fold_unit_failures(found, locus)
    out = []
    for f in found.values():
        f["first_seen"] = f["first_seen"] or now
        f["last_seen"] = f["last_seen"] or now
        out.append(f)
    out.sort(key=lambda f: (KINDS.index(f["kind"]), -f["count"]))
    return out


def _fold_unit_failures(found, locus):
    """systemd says one failed run in 2-3 lines ("Main process exited …",
    "Failed with result …", "Failed to start …"). Collapse them per unit into
    ONE finding counted in failed RUNS (distinct 5 s buckets): >=
    CRASH_LOOP_UNIT_FAILS runs in the window is a crash_loop, fewer is an
    other_error "unit failed"."""
    by_src = {}
    for k in [k for k, f in found.items() if f["kind"] == "unit_failed"]:
        by_src.setdefault(found[k]["source"], []).append(found.pop(k))
    for src, fs in by_src.items():
        stamps = [t for f in fs for t in f["_stamps"]]
        runs = sorted({round(t / 5) * 5 for t in stamps if t is not None})
        n = len(runs) + sum(1 for t in stamps if t is None)
        loop = n >= CRASH_LOOP_UNIT_FAILS
        kind, sev = ("crash_loop", "high") if loop else ("other_error", "medium")
        sig = "unit failed repeatedly" if loop else "unit failed"
        samples = []
        for f in fs:
            for x in f["sample_lines"]:
                if x not in samples:
                    samples.append(x)
        firsts = [f["first_seen"] for f in fs if f["first_seen"] is not None]
        lasts = [f["last_seen"] for f in fs if f["last_seen"] is not None]
        k = _key(kind, src, sig, locus)
        found[k] = {"key": k, "kind": kind, "severity": sev, "source": src, "locus": locus,
                    "count": n, "first_seen": min(firsts) if firsts else None,
                    "last_seen": max(lasts) if lasts else None,
                    "sample_lines": samples[:MAX_SAMPLES], "signature": sig,
                    "suggested_action": next((f["suggested_action"] for f in fs
                                              if f["suggested_action"]), ""),
                    "_stamps": [float(r) for r in runs] + [None] * (n - len(runs))}


def merge(prev, f, now=None):
    """Fold one scan finding into its persisted row. Returns (row, reason) where
    reason is 'new' | 'jump' | 'returned' | None (None = known, nothing to say).

    Windows overlap between scans, so the cumulative ``count`` only grows by
    lines stamped AFTER the row's previous ``last_seen``; lines without a
    timestamp (seat panes) can only raise it to the window's own count."""
    now = now or time.time()
    stamps = f.get("_stamps") or []
    if not prev:
        row = {k: v for k, v in f.items() if not k.startswith("_")}
        row.update(count=int(f["count"]), emitted_count=int(f["count"]), emitted_at=now, ts=now)
        return row, "new"
    last = float(prev.get("last_seen") or 0)
    fresh = sum(1 for t in stamps if t is not None and t > last)
    untimed = sum(1 for t in stamps if t is None)
    count = int(prev.get("count") or 0) + fresh
    if untimed:
        count = max(count, untimed)
    row = dict(prev)
    row.update(count=count, last_seen=max(last, float(f.get("last_seen") or 0)), ts=now,
               severity=f["severity"], locus=f.get("locus") or prev.get("locus") or "")
    samples = list(prev.get("sample_lines") or [])
    for s in f.get("sample_lines") or []:
        if s not in samples:
            samples.append(s)
    row["sample_lines"] = samples[-MAX_SAMPLES:]
    if f.get("suggested_action") and not row.get("suggested_action"):
        row["suggested_action"] = f["suggested_action"]
    reason = None
    emitted = int(prev.get("emitted_count") or 0)
    fresh_ts = [t for t in stamps if t is not None and t > last]
    if fresh_ts and last and min(fresh_ts) - last >= STALE_S:
        reason = "returned"
    elif count >= max(emitted * JUMP_FACTOR, emitted + JUMP_MIN):
        reason = "jump"
    if reason:
        row["emitted_count"] = count
        row["emitted_at"] = now
    return row, reason


def subject(row) -> str:
    return f"{row['kind']} · {row['source']}: {row['signature']}"[:200]


def fmt_finding(row, *, samples=2) -> str:
    """One finding as B presents it — deterministic text, no summary model."""
    hhmm = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t or 0)))  # noqa: E731
    head = (f"[{row.get('severity', 'low')}] {row['kind']} · {row.get('locus') or 'this station'} · "
            f"{row['source']} ×{row.get('count', 0)} (first {hhmm(row.get('first_seen'))}, "
            f"last {hhmm(row.get('last_seen'))})")
    lines = [head, f"  signature: {row.get('signature', '')}"]
    for s in (row.get("sample_lines") or [])[:samples]:
        lines.append(f"  > {s[:SAMPLE_CHARS]}")
    if row.get("suggested_action"):
        lines.append(f"  action: {row['suggested_action']}")
    return "\n".join(lines)
