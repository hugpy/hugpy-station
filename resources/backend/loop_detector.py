"""Crash-loop / retry-loop detector for hugpy Station (1.0.118).

Pure logic: every ``observe_*`` method takes rows the station backend already
fetches (systemctl show, central /llm/jobs?live=1, /api/llm/queue, /llm/calls,
/llm/workers) and returns FINDINGS for this cycle; ``finish_cycle`` merges them
into the persistent loop set and reports what changed so the backend can act
(⚠ strip, one ✉ keeper mail per loop, one board todo per loop). Nothing here
touches the network or systemd — server.py owns the fetching and the acting,
so this file is testable over fixture inputs.

A LOOP is one row keyed ``source:identity``::

    {key, source, identity, count, first_seen, last_seen, detail, action,
     severity, active, mailed_at, board_id, cleared_at}

Thresholds (all overridable in the constructor; defaults per the 2026-09-29
operator ask):

* systemd unit: NRestarts grows by >= 3 inside the 10 min window, or the unit is
  seen in activating/auto-restart >= 2 times in the window.
* central job: attempt/retry count >= 3 (attempt field, or retry-stage
  re-entries inside the window), or an active job whose progressed_at is older
  than 5 min.
* (model, error) pair: the same normalized error on the same model >= 3 times
  in 10 min (calls + failed job rows) — the "permanent error treated as
  transient" case.
* calls: the same (caller, model, prompt-hash) >= 3 times in 10 min; rows
  without an extractable prompt fall back to (caller, model) >= 6.
* queue: >= 4 live rows on one model for >= 3 consecutive cycles (calls
  stacking on a single slot).
* worker: agent_boot_at changes >= 2 times in 10 min (re-exec loop).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque

WINDOW_S = 600
DEFAULTS = {
    "window_s": WINDOW_S,
    "unit_restart_threshold": 3,
    "unit_autorestart_threshold": 2,
    "job_retry_threshold": 3,
    "job_stall_s": 300,
    "error_repeat_threshold": 3,
    "call_repeat_threshold": 3,
    "call_nohash_threshold": 6,
    "queue_stack_rows": 4,
    "queue_stack_cycles": 3,
    "worker_reexec_threshold": 2,
    "mail_cooldown_s": 1800,
    "recent_keep_s": 3600,
    # 1.0.125: "stacked" means OVERLAPPING in-flight calls, not repetition
    "call_stack_overlap": 3,
    # sequential repetition is only reported when the caller also burns tokens
    "caller_token_window_s": 3600,
    "caller_token_budget_day": 200_000,
    # sanctioned batch callers: budgeted (token_burn), never loop-alarmed
    "declared_callers": ("tidy-eval", "test-fire", "grade"),
    # an agent run = sequential calls whose message list GROWS (each a strict
    # prefix-extension of the previous) — one run, not a loop, unless it runs away
    "run_step_cap": 50,
}

_ACTIVE_JOB = {"processing", "streaming", "pending", "queued", "dispatched", "loading", "retrying"}
_RETRY_RE = re.compile(r"retry|retrying|not confirmed|re-?dispatch|re-?queue", re.I)
_FIX_RE = re.compile(r"FIX:\s*(.+?)(?:\.\s|$)", re.S)
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_WS_RE = re.compile(r"\s+")


def _h(text, n=12):
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()[:n]


def norm_error(err):
    """Stable fingerprint of an error: message text with numbers collapsed
    (``after 97s`` / ``after 99s`` are one error), first 160 chars."""
    if isinstance(err, dict):
        err = err.get("message") or err.get("error") or err.get("code") or json.dumps(err, sort_keys=True)
    s = _WS_RE.sub(" ", str(err or "")).strip()
    return _NUM_RE.sub("N", s)[:160]


def error_fix(err):
    """The ``FIX: …`` sentence central attaches to permanent errors, if any."""
    if isinstance(err, dict):
        err = err.get("message") or ""
    m = _FIX_RE.search(str(err or ""))
    return m.group(1).strip()[:300] if m else ""


def call_prompt(row):
    """(prompt_text, system_text) of a calls row — the last user message (or
    ``prompt``) and the first system message; '' when the row carries neither."""
    req = row.get("request") if isinstance(row.get("request"), dict) else {}
    msgs = req.get("messages") if isinstance(req.get("messages"), list) else []
    user, system = "", ""
    for m in msgs:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list):      # multimodal parts
            c = " ".join(str(p.get("text") or "") for p in c if isinstance(p, dict))
        c = str(c or "")
        if m.get("role") == "user":
            user = c
        elif m.get("role") == "system" and not system:
            system = c
    if not user:
        user = str(req.get("prompt") or req.get("input") or "")
    return user.strip(), system.strip()


def call_fingerprint(row):
    """Per-message hashes of a calls row's request — (h(role+content), ...)."""
    req = row.get("request") if isinstance(row.get("request"), dict) else {}
    msgs = req.get("messages") if isinstance(req.get("messages"), list) else []
    out = []
    for m in msgs:
        if isinstance(m, dict):
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(str(p.get("text") or "") for p in c if isinstance(p, dict))
            out.append(_h("%s\x00%s" % (m.get("role"), c or ""), 10))
    return tuple(out)


def agent_runs(entries):
    """Split sequential calls into agent runs: a call whose message list is a
    STRICT prefix-extension of the previous call's (a conversation growing turn
    by turn) and that starts after it ended continues the run. -> (runs of >= 2
    steps, loose calls). entries: (ts, start, end, tokens, fingerprint)."""
    runs, cur = [], []
    for e in sorted(entries, key=lambda e: (e[1] or e[0], e[0])):
        if cur:
            prev = cur[-1]
            fp, pf = e[4] or (), prev[4] or ()
            grows = len(fp) > len(pf) > 0 and fp[:len(pf)] == pf
            after = e[1] is None or prev[2] is None or e[1] >= prev[2] - 0.5
            if grows and after:
                cur.append(e)
                continue
            runs.append(cur)
        cur = [e]
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) >= 2], [e for r in runs if len(r) < 2 for e in r]


def job_caller(row):
    """Who submitted a central job (1.0.140, for the gate's self-origin guard)."""
    for k in ("client_task", "client_process", "client_session", "client", "principal", "ua", "caller"):
        v = row.get(k)
        if v:
            return str(v)[:60]
    return ""


def call_caller(row):
    for k in ("client_process", "client_user", "principal", "client_session", "ua", "client", "peer"):
        v = row.get(k)
        if v:
            return str(v)[:60]
    return "unknown"


def _ts(row, *keys):
    for k in keys:
        try:
            v = float(row.get(k) or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            return v
    return 0.0


def _snip(s, n=70):
    s = _WS_RE.sub(" ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


class LoopDetector:
    def __init__(self, state_path=None, now=None, **cfg):
        self.cfg = dict(DEFAULTS, **{k: v for k, v in cfg.items() if v is not None})
        self.now = now or time.time
        self.state_path = state_path
        self.loops = {}               # key -> loop row (active + recently cleared)
        self._units = {}              # unit -> deque[(ts, nrestarts, autorestart)]
        self._caller_tokens = {}      # caller -> deque[(ts, tokens)] (token_burn budget)
        self._jobs = {}               # job id -> {"sig": (attempt, stage), "events": deque[ts], "attempt": int}
        self._errors = {}             # (model, errsig) -> {"ids": set, "events": deque[ts], "sample": str}
        self._calls_seen = deque()    # (ts, id)
        self._calls_ids = set()
        self._call_groups = {}        # (caller, model, phash) -> deque[ts]
        self._queue_streak = {}       # model -> consecutive cycles over threshold
        self._workers = {}            # name -> {"boot": last agent_boot_at, "events": deque[ts]}
        self._pending = []            # findings collected since the last finish_cycle
        if state_path:
            self._load()

    # ---- persistence ---------------------------------------------------------------
    def _load(self):
        try:
            doc = json.loads(open(self.state_path, encoding="utf-8").read())
        except (OSError, ValueError):
            return
        for row in (doc.get("loops") or []):
            if isinstance(row, dict) and row.get("key"):
                self.loops[row["key"]] = row

    def save(self):
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"loops": list(self.loops.values()), "saved": self.now()}, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # ---- helpers ---------------------------------------------------------------------
    def _prune(self, dq, now, ts_of=lambda e: e):
        w = self.cfg["window_s"]
        while dq and now - ts_of(dq[0]) > w:
            dq.popleft()

    def _finding(self, source, identity, count, detail, action, severity="warn", first_seen=None, ids=None,
                 caller="", unit="", session_id=""):
        """1.0.140: ``caller`` / ``unit`` / ``session_id`` say WHO produced the
        loop, so the issue gate's self-origin guard can tell the keeper's own run
        (keeper*, cs-*, abstract-claude-serve*) from a real problem. The local
        key stays as it was; the gate's fingerprint strips job/prompt-hash churn."""
        f = {"source": source, "identity": str(identity)[:200], "count": int(count),
             "detail": str(detail)[:600], "action": str(action)[:600], "severity": severity,
             "key": source + ":" + _h(identity, 16)}
        if first_seen:
            f["first_seen"] = float(first_seen)
        if ids:
            f["ids"] = sorted(set(str(i) for i in ids))[:20]
        if caller:
            f["caller"] = str(caller)[:120]
        if unit:
            f["unit"] = str(unit)[:120]
        if session_id:
            f["session_id"] = str(session_id)[:120]
        self._pending.append(f)
        return f

    # ---- sources ---------------------------------------------------------------------
    def observe_units(self, rows, now=None):
        """rows: [{unit, scope: 'user'|'system', nrestarts, active_state, sub_state, result,
        owner?, uid?}] — ``owner`` marks a PEER locus's user unit on this host (1.0.123):
        it is tracked under ``owner/unit`` so a peer's hugpy-station-web never merges
        with this station's own unit of the same name."""
        now = now or self.now()
        out = []
        for r in rows or []:
            unit = str(r.get("unit") or "").strip()
            if not unit:
                continue
            try:
                n = int(r.get("nrestarts") or 0)
            except (TypeError, ValueError):
                n = 0
            auto = (str(r.get("sub_state") or "") == "auto-restart"
                    or str(r.get("active_state") or "") in ("activating", "deactivating"))
            owner = str(r.get("owner") or "").strip()
            key = "%s/%s" % (owner, unit) if owner else unit
            dq = self._units.setdefault(key, deque())
            dq.append((now, n, auto))
            self._prune(dq, now, lambda e: e[0])
            delta = n - min(e[1] for e in dq)
            autos = sum(1 for e in dq if e[2])
            if delta >= self.cfg["unit_restart_threshold"] or autos >= self.cfg["unit_autorestart_threshold"]:
                scope = str(r.get("scope") or "user")
                sc = "--user " if scope == "user" else ""
                sudo = "" if scope == "user" else "sudo "
                why = ("NRestarts +%d in %dm" % (delta, self.cfg["window_s"] // 60)) if delta else "auto-restart x%d" % autos
                if owner:
                    detail = "user unit %s of user %s (peer locus): %s (now %s/%s, result=%s, NRestarts=%d)" % (
                        unit, owner, why, r.get("active_state") or "?", r.get("sub_state") or "?",
                        r.get("result") or "?", n)
                    rt = "XDG_RUNTIME_DIR=/run/user/%s " % r["uid"] if r.get("uid") not in (None, "") else ""
                    action = ("sudo journalctl --user -M %s@ -u %s -n 50 --no-pager"
                              "   # or, without root: sudo -n -u %s %sjournalctl --user -u %s -n 50 --no-pager"
                              "   # then: sudo -n -u %s %ssystemctl --user stop %s"
                              % (owner, unit, owner, rt, unit, owner, rt, unit))
                else:
                    detail = "%s unit %s: %s (now %s/%s, result=%s, NRestarts=%d)" % (
                        scope, unit, why, r.get("active_state") or "?", r.get("sub_state") or "?",
                        r.get("result") or "?", n)
                    action = ("%sjournalctl %s-u %s -n 80 --no-pager   # then: %ssystemctl %sstop %s"
                              % (sudo, sc, unit, sudo, sc, unit))
                # a unit's restart loop is a real fault even for serve's own unit, so
                # the unit is NOT passed as the self-origin caller (1.0.140)
                out.append(self._finding("systemd", key, max(delta, autos), detail, action,
                                         "crit" if delta >= self.cfg["unit_restart_threshold"] else "warn",
                                         first_seen=dq[0][0], caller="systemd"))
        return out

    def observe_jobs(self, jobs, now=None):
        """jobs: rows of GET /llm/jobs?live=1 (id, status, stage, message, attempt,
        max_attempts, progressed_at, stalled, worker, model_key, error)."""
        now = now or self.now()
        out = []
        for j in jobs or []:
            if not isinstance(j, dict) or not j.get("id"):
                continue
            jid = str(j["id"])
            status = str(j.get("status") or "").lower()
            model = str(j.get("model_key") or j.get("model") or "?")
            worker = str(j.get("worker") or "?")
            stage = str(j.get("stage") or "")
            msg = str(j.get("message") or "")
            err = j.get("error")
            try:
                attempt = int(j.get("attempt") or 0)
            except (TypeError, ValueError):
                attempt = 0
            cancel = "curl -s -X POST http://127.0.0.1:7002/llm/jobs/%s/cancel" % jid
            jcaller = job_caller(j)
            jsess = str(j.get("client_session") or "")[:120]
            if status in ("failed", "expired", "error") and err:
                self._note_error(model, err, "job:" + jid, _ts(j, "ended_ts", "updated", "progressed_at") or now, now)
                continue
            if status and status not in _ACTIVE_JOB:
                continue
            st = self._jobs.setdefault(jid, {"sig": None, "events": deque(), "attempt": 0, "first": now})
            sig = (attempt, stage, _snip(msg, 40))
            retryish = bool(_RETRY_RE.search(stage + " " + msg))
            if sig != st["sig"]:
                if attempt > st["attempt"] or retryish:
                    st["events"].append(now)
                st["sig"], st["attempt"] = sig, max(attempt, st["attempt"])
            self._prune(st["events"], now)
            retries = max(attempt, len(st["events"]))
            fix = error_fix(err) or error_fix(msg)
            if retries >= self.cfg["job_retry_threshold"]:
                detail = "job %s %s on %s: %s x%d (stage=%s%s)%s" % (
                    jid, model, worker, "attempt" if attempt else "retry", retries, stage or "?",
                    ("; " + _snip(msg, 120)) if msg else "",
                    ("; last error: " + _snip(norm_error(err), 160)) if err else "")
                action = cancel + (("   # FIX: " + fix) if fix else "   # permanent error treated as transient — cancel, then fix the cause")
                out.append(self._finding("job", "%s %s@%s" % (jid, model, worker), retries, detail, action,
                                         "crit", first_seen=st["first"], ids=[jid], caller=jcaller,
                                         session_id=jsess))
                continue
            prog = _ts(j, "progressed_at")
            if status in ("processing", "streaming") and prog and now - prog > self.cfg["job_stall_s"]:
                idle = int(now - prog)
                detail = "job %s %s on %s: no progress for %dm%02ds (stage=%s, stalled=%s)" % (
                    jid, model, worker, idle // 60, idle % 60, stage or "?", j.get("stalled"))
                out.append(self._finding("job", "%s %s@%s" % (jid, model, worker), 1, detail,
                                         cancel + "   # stalled job", "warn", first_seen=prog, ids=[jid],
                                         caller=jcaller, session_id=jsess))
        return out

    def _note_error(self, model, err, rid, ts, now):
        sig = norm_error(err)
        if not sig:
            return None
        key = (model, _h(sig, 10))
        st = self._errors.setdefault(key, {"ids": set(), "events": deque(), "sample": "", "first": ts})
        if rid in st["ids"]:
            return None
        st["ids"].add(rid)
        st["events"].append(ts)
        st["sample"] = str(err.get("message") if isinstance(err, dict) else err)[:400]
        st["first"] = min(st["first"], ts)
        return key

    def _error_findings(self, now):
        out = []
        for (model, esig), st in list(self._errors.items()):
            self._prune(st["events"], now)
            if not st["events"]:
                continue
            n = len(st["events"])
            if n >= self.cfg["error_repeat_threshold"]:
                fix = error_fix(st["sample"])
                detail = "%s failed the same way x%d in %dm: %s" % (
                    model, n, self.cfg["window_s"] // 60, _snip(st["sample"], 220))
                action = ("FIX: " + fix) if fix else \
                    "stop callers of %s until the cause is fixed (same error each time — not transient)" % model
                out.append(self._finding("error", "%s %s" % (model, esig), n, detail, action, "crit",
                                         first_seen=min(st["events"])))
        return out

    def _declared(self, caller):
        c = str(caller or "").lower()
        return any(d and str(d).lower() in c for d in self.cfg.get("declared_callers") or ())

    @staticmethod
    def max_overlap(intervals):
        """Peak number of simultaneously in-flight [start, end] intervals."""
        ev = []
        for a, b in intervals:
            if a is None or b is None:
                continue
            ev.append((a, 1))
            ev.append((max(a, b), -1))
        peak = cur = 0
        for _, d in sorted(ev, key=lambda e: (e[0], e[1])):      # an end before a start at the same instant
            cur += d
            peak = max(peak, cur)
        return peak

    def caller_rate(self, caller, now):
        dq = self._caller_tokens.get(caller) or deque()
        while dq and now - dq[0][0] > self.cfg["caller_token_window_s"]:
            dq.popleft()
        tot = sum(t for _, t in dq)
        return int(tot * 86400.0 / self.cfg["caller_token_window_s"]), tot

    def observe_calls(self, calls, now=None):
        """calls: rows of GET /llm/calls?limit=N (id, started/processing_ts,
        ended/processed_ts, model_key, status, error, tokens, request.messages,
        ua/client/…). 1.0.125: "stacked" = >= call_stack_overlap calls of one
        (caller, model, system prompt) IN FLIGHT AT ONCE; sequential repetition is
        "repeated" and reported only when the caller's token rate is over budget;
        declared callers are budgeted (token_burn) and never loop-alarmed."""
        now = now or self.now()
        if not hasattr(self, "_caller_tokens"):
            self._caller_tokens = {}
        self._prune(self._calls_seen, now, lambda e: e[0])
        self._calls_ids = {i for _, i in self._calls_seen}
        rows = [c for c in (calls or []) if isinstance(c, dict) and c.get("id")]
        rows.sort(key=lambda c: _ts(c, "ended_ts", "processed_ts", "ts", "started_ts") or now)
        for c in rows:
            cid = str(c["id"])
            ts = _ts(c, "ended_ts", "processed_ts", "ts", "started_ts") or now
            if cid in self._calls_ids or now - ts > max(self.cfg["window_s"], self.cfg["caller_token_window_s"]):
                continue
            self._calls_ids.add(cid)
            self._calls_seen.append((ts, cid))
            model = str(c.get("model_key") or c.get("model") or "?")
            caller = call_caller(c)
            try:
                tok = int(c.get("total_tokens") or c.get("tokens") or 0)
            except (TypeError, ValueError):
                tok = 0
            self._caller_tokens.setdefault(caller, deque()).append((ts, tok))
            if now - ts > self.cfg["window_s"]:
                continue
            if str(c.get("status") or "") == "failed" and c.get("error"):
                self._note_error(model, c["error"], "call:" + cid, ts, now)
            start = _ts(c, "processing_ts", "started_ts") or None     # 0/absent = unknown: no overlap claim
            end = _ts(c, "processed_ts", "ended_ts") or None
            iv = (start, end if end is not None else (now if start is not None else None))
            user, system = call_prompt(c)
            entry = (ts, iv[0], iv[1], tok, call_fingerprint(c))
            if user:
                gk = ("prompt", caller, model, _h(user), _snip(user, 60))
            else:
                gk = ("nohash", caller, model, "", "")
            self._call_groups.setdefault(gk, deque()).append(entry)
            if system:
                self._call_groups.setdefault(("system", caller, model, _h(system), _snip(system, 60)), deque()).append(entry)
        out, burn_callers = [], set()
        for gk, dq in list(self._call_groups.items()):
            while dq and now - dq[0][0] > self.cfg["window_s"]:
                dq.popleft()
            if not dq:
                self._call_groups.pop(gk, None)
                continue
            kind, caller, model, ph, snip = gk
            n = len(dq)
            if self._declared(caller):
                burn_callers.add(caller)
                continue
            first = min(e[0] for e in dq)
            peak = self.max_overlap([(e[1], e[2]) for e in dq])
            per_day, _tot = self.caller_rate(caller, now)
            over = per_day > self.cfg["caller_token_budget_day"]
            win = self.cfg["window_s"] // 60
            if kind == "system" and peak < self.cfg["call_stack_overlap"]:
                runs, loose = agent_runs(list(dq))
                for run in runs:                          # a growing conversation = ONE agent run
                    rtok = sum(e[3] for e in run)
                    if len(run) > self.cfg["run_step_cap"] or rtok > self.cfg["caller_token_budget_day"]:
                        detail = ("%s agent run on %s ran away: %d sequential steps, %s tokens (step cap %d, "
                                  "budget %s)" % (caller, model, len(run), format(rtok, ","), self.cfg["run_step_cap"],
                                                  format(self.cfg["caller_token_budget_day"], ",")))
                        out.append(self._finding("calls", "%s>%s run %s" % (caller, model, ph), len(run), detail,
                                                 "stop the run (caller %s) and check why it does not converge" % caller,
                                                 "warn", first_seen=run[0][0], caller=caller))
                dq = deque(loose)                          # only calls outside any run count as repetition
                n = len(dq)
                if not dq:
                    continue
            if peak >= self.cfg["call_stack_overlap"] and kind in ("system", "prompt"):
                detail = "%s stacked %d calls IN FLIGHT AT ONCE (peak overlap) on %s with the same %s prompt, %d in %dm: “%s”" % (
                    caller, peak, model, "system" if kind == "system" else "user", n, win, snip)
                out.append(self._finding("calls", "%s>%s %s %s" % (caller, model, kind[:3], ph), peak, detail,
                                         "throttle the caller (one in flight per slot); cancel queued rows via /llm/jobs/<id>/cancel",
                                         "warn", first_seen=first, caller=caller))
                continue
            thresh = self.cfg["call_repeat_threshold"] if kind == "prompt" else self.cfg["call_nohash_threshold"]
            identical = kind == "prompt" and len({e[4] for e in dq}) == 1        # the SAME request, re-sent
            if identical and n >= thresh:
                detail = "identical request from %s to %s repeated x%d in %dm (same messages each time): “%s” (prompt hash %s)" % (
                    caller, model, n, win, snip, ph)
                out.append(self._finding("calls", "%s>%s %s" % (caller, model, ph), n, detail,
                                         "find the timer/probe sending this prompt (caller %s) and stop it; central call ids in /llm/calls" % caller,
                                         "warn", first_seen=first, caller=caller))
                continue
            if n >= thresh and over:
                what = {"prompt": "identical prompt", "system": "same system prompt", "nohash": "calls"}[kind]
                detail = ("%s repeated %s to %s x%d in %dm (sequential, no overlap) and spends ~%s tokens/day "
                          "(budget %s)%s") % (caller, what, model, n, win, format(per_day, ","),
                                              format(self.cfg["caller_token_budget_day"], ","),
                                              (": “%s”" % snip) if snip else "")
                out.append(self._finding("calls", "%s>%s %s %s" % (caller, model, kind[:3], ph or "nohash"), n, detail,
                                         "find the timer/probe sending this (caller %s) and stop or slow it; central call ids in /llm/calls" % caller,
                                         "warn", first_seen=first, caller=caller))
        for caller in burn_callers:
            per_day, tot = self.caller_rate(caller, now)
            if per_day > self.cfg["caller_token_budget_day"]:
                detail = ("declared caller %s spends ~%s tokens/day projected (%s tokens in the last %dm; budget %s/day)"
                          % (caller, format(per_day, ","), format(tot, ","), self.cfg["caller_token_window_s"] // 60,
                             format(self.cfg["caller_token_budget_day"], ",")))
                out.append(self._finding("token_burn", caller, per_day, detail,
                                         "declared batch caller over budget: slow its schedule or raise its budget "
                                         "(STATION_LOOP_CALLER_BUDGET)", "warn", caller=caller))
        out.extend(self._error_findings(now))
        return out

    def observe_queue(self, queue, now=None):
        """queue: GET /api/llm/queue body ({active: [rows], counts})."""
        now = now or self.now()
        rows = (queue or {}).get("active") if isinstance(queue, dict) else queue
        per = {}
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            m = str(r.get("model_key") or r.get("model") or "?")
            per.setdefault(m, []).append(str(r.get("request_id") or r.get("id") or "?"))
        out = []
        for m in list(self._queue_streak):
            if len(per.get(m, [])) < self.cfg["queue_stack_rows"]:
                self._queue_streak.pop(m, None)
        for m, ids in per.items():
            if len(ids) < self.cfg["queue_stack_rows"]:
                continue
            st = self._queue_streak.setdefault(m, {"n": 0, "first": now})
            st["n"] += 1
            if st["n"] >= self.cfg["queue_stack_cycles"]:
                detail = "%d live rows stacked on %s for %d cycles: %s" % (len(ids), m, st["n"], " ".join(ids[:8]))
                action = "for id in %s; do curl -s -X POST http://127.0.0.1:7002/llm/jobs/$id/cancel; done   # then throttle the caller" % " ".join(ids[:8])
                out.append(self._finding("queue", m, len(ids), detail, action, "warn", first_seen=st["first"], ids=ids))
        return out

    def observe_workers(self, workers, now=None):
        """workers: rows of GET /llm/workers (name, agent_boot_at, last_seen, …)."""
        now = now or self.now()
        out = []
        for w in workers or []:
            if not isinstance(w, dict):
                continue
            name = str(w.get("name") or w.get("id") or "").strip()
            boot = _ts(w, "agent_boot_at")
            if not name or not boot:
                continue
            st = self._workers.setdefault(name, {"boot": boot, "events": deque()})
            if st["boot"] != boot:                       # agent_boot_at moved == the agent re-exec'd
                st["boot"] = boot
                st["events"].append(boot if now - boot <= self.cfg["window_s"] else now)
            self._prune(st["events"], now)
            reexecs = len(st["events"])
            if reexecs >= self.cfg["worker_reexec_threshold"]:
                detail = "worker %s re-exec'd x%d in %dm (agent_boot_at moved; last %s)" % (
                    name, reexecs, self.cfg["window_s"] // 60, time.strftime("%H:%M:%S", time.localtime(boot)))
                action = "ssh %s 'journalctl --user -u hugpy-worker -n 100 --no-pager'   # find the crash, then stop the re-exec" % name
                out.append(self._finding("worker", name, reexecs, detail, action, "crit", first_seen=min(st["events"])))
        return out

    # ---- merge ---------------------------------------------------------------------
    def finish_cycle(self, now=None):
        """Merge this cycle's findings into the loop set. Returns
        {"new": [rows], "updated": [rows], "cleared": [rows], "mail": [rows]}
        where ``mail`` is the subset due a keeper mail (new, or re-detected after
        the cooldown)."""
        now = now or self.now()
        seen, events = {}, {"new": [], "updated": [], "cleared": [], "mail": []}
        for f in self._pending:
            k = f["key"]
            if k in seen:
                seen[k]["count"] = max(seen[k]["count"], f["count"])
                continue
            seen[k] = f
        self._pending = []
        for k, f in seen.items():
            row = self.loops.get(k)
            if row is None or not row.get("active"):
                fresh = row is None
                row = dict(row or {}, key=k, source=f["source"], identity=f["identity"],
                           first_seen=f.get("first_seen") or now, active=True, cleared_at=None,
                           board_id=(row or {}).get("board_id") if row and not row.get("cleared_at") else None)
                self.loops[k] = row
                events["new"].append(row)
                if fresh:
                    row.setdefault("mailed_at", 0)
            else:
                events["updated"].append(row)
            row.update(count=f["count"], last_seen=now, detail=f["detail"], action=f["action"],
                       severity=f["severity"])
            if f.get("ids"):
                row["ids"] = f["ids"]
            for k2 in ("caller", "unit", "session_id"):          # 1.0.140: self-origin inputs for the gate
                if f.get(k2):
                    row[k2] = f[k2]
            if now - float(row.get("mailed_at") or 0) >= self.cfg["mail_cooldown_s"]:
                events["mail"].append(row)
        for k, row in list(self.loops.items()):
            if k in seen:
                continue
            if row.get("active"):
                row["active"], row["cleared_at"] = False, now
                events["cleared"].append(row)
            elif now - float(row.get("cleared_at") or 0) > self.cfg["recent_keep_s"]:
                self.loops.pop(k, None)
        self.save()
        return events

    def mark_mailed(self, row, now=None):
        row["mailed_at"] = now or self.now()

    def snapshot(self, now=None):
        now = now or self.now()
        rows = sorted(self.loops.values(), key=lambda r: (not r.get("active"), -(r.get("last_seen") or 0)))
        return {"ts": now, "active": [r for r in rows if r.get("active")],
                "recent": [r for r in rows if not r.get("active")],
                "config": {k: self.cfg[k] for k in DEFAULTS}}


def fmt_mail(row):
    """The one keeper ✉ per loop."""
    hhmm = lambda t: time.strftime("%H:%M", time.localtime(float(t or 0)))  # noqa: E731
    return ("⚠ loop detected [%s] %s ×%d (first %s, last %s)\n%s\nDo:\n```bash\n%s\n```"
            % (row["source"], row["identity"], row.get("count") or 0, hhmm(row.get("first_seen")),
               hhmm(row.get("last_seen")), row.get("detail") or "", row.get("action") or ""))


def fmt_board(row):
    """(text, note) for the keeper-board todo of a loop."""
    text = ("[loop] %s: %s ×%d" % (row["source"], row["identity"], row.get("count") or 0))[:500]
    note = ("%s\n\nfirst %s · last %s · detector key %s\n\n```bash\n%s\n```"
            % (row.get("detail") or "", time.strftime("%Y-%m-%d %H:%M", time.localtime(float(row.get("first_seen") or 0))),
               time.strftime("%H:%M", time.localtime(float(row.get("last_seen") or 0))), row["key"],
               row.get("action") or ""))[:1800]
    # the same close contract as findings (keeper_notify.DISP_MARKER): a decided
    # loop is logged, not re-pinged
    note += ("\n\nclose with a disposition line: `inert: <reason>` lowers this loop's priority (still reported, "
             "board t4178); `accept` = fixed "
             "(it re-opens if it comes back); plain close = handled.")
    return text, note
