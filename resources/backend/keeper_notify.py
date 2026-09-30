"""Keeper notifier for hugpy Station (1.0.124) — ONE notifier, two sources.

Operator 2026-09-29: "it was a waste because it was never brought to the
attention of the keeper; it should be." Findings (log_findings.py) and loops
(loop_detector.py) reach the keeper through the SAME surfaces:

  * the ⚠ strip row (GET /api/loops, ``findings`` list);
  * ONE ✉ keeper mail per signature (30 min cooldown; re-mailed on the
    detector's own new / count-doubling / returned emissions);
  * ONE keeper-board todo carrying the exact command, auto-resolved when the
    finding has been quiet for an hour.

A station whose locus is not the keeper delivers to the KEEPER (RemoteSink:
board rows on the keeper's slice via the toolserver, mail as a comms ping
kind=message to the keeper), tagged with the originating locus. An unreachable
toolserver is swallowed — nothing is marked delivered, so the next scan retries.

Per-signature DISPOSITION (operator: "differ to past propositions, rather than
constantly suggesting a fix to a thing that is already determined as inert"):
open | proposed | accepted | rejected(reason) | inert(reason, by, at). ``inert``
silences notifications and proposals for the signature (still counted, still
listed greyed) until a MATERIAL change: rate >= 10x the rate when it was marked,
a new source/unit/locus, or a higher severity class — the reopened
notification says why. The disposition key is (kind, normalised signature), so
the same error from a new unit or locus is recognisably "the same thing, new
place".

Also here: TokenMeter — a station's own model-calling tasks are metered and a
task above budget (default 200k tokens/day) becomes a ``token_burn`` finding.

Pure logic + injectable I/O: no aiohttp, no toolserver import — server.py
hands the sinks their post/mail callables, the tests hand them fakes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

SEV_RANK = {"low": 0, "medium": 1, "warn": 1, "high": 2, "crit": 3}

DEFAULTS = {
    "mail_cooldown_s": 1800,       # one ✉ per signature per 30 min
    "quiet_s": 3600,               # a finding quiet this long is resolved (board item closed)
    "min_severity": "medium",      # below: strip + findings.json only
    "inert_rate_factor": 10.0,     # an inert signature re-opens at >= 10x its marked rate
    "rate_window_s": 3600,
    "keep_cleared_s": 86400,
    # sources whose findings are strip-only: seat-pane tails are terminal PROSE
    # (an agent discussing "rate limits" is not a 429) — pushing them would spam
    # the keeper with its own conversation. STATION_NOTIFY_SEAT_PANES=1 opts in.
    "strip_only_prefixes": ("seat:",),
}

DISPOSITIONS = ("open", "proposed", "accepted", "rejected", "inert")


def sig_key(kind, signature):
    """The disposition key: the SAME error regardless of where it shows up."""
    return hashlib.sha1(f"{kind}|{signature}".encode("utf-8", "replace")).hexdigest()[:12]


def sev_rank(s):
    return SEV_RANK.get(str(s or "low").lower(), 0)


def _hhmm(t):
    return time.strftime("%H:%M", time.localtime(float(t or 0)))


def _ymdhm(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t or 0)))


def default_action(f):
    """The finding's own FIX:/hint: text, else a deterministic inspect command."""
    if f.get("suggested_action"):
        return f["suggested_action"]
    src = str(f.get("source") or "")
    if re.match(r"^[\w@.:-]+\.(service|socket|timer)$", src):
        return "journalctl --user -u %s -n 80 --no-pager" % src
    return "open the 🐞 bug-scan panel for %s (samples in the board note)" % (f.get("locus") or "this station")


class NotifyBook:
    """Findings → keeper notifications, with per-signature dispositions."""

    def __init__(self, state_path=None, now=None, **cfg):
        self.state_path = state_path
        self.now = now or time.time
        self.cfg = dict(DEFAULTS)
        self.cfg.update({k: v for k, v in cfg.items() if k in DEFAULTS})
        self.rows = {}       # finding key -> notify row
        self.sigs = {}       # sig key -> {disposition, reason, by, at, baseline, proposals, hist, ...}
        self._load()

    # ---- persistence ------------------------------------------------------------------
    def _load(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.rows = dict(doc.get("rows") or {})
            self.sigs = dict(doc.get("sigs") or {})
        except (OSError, ValueError):
            pass

    def save(self):
        if not self.state_path:
            return
        tmp = self.state_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"rows": self.rows, "sigs": self.sigs, "saved": self.now()}, fh)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # ---- dispositions -----------------------------------------------------------------
    def sig(self, skey):
        return self.sigs.setdefault(skey, {"disposition": "open", "proposals": [], "hist": []})

    def resolve_sig(self, ref):
        """A finding key or a sig key (or a unique prefix of either) -> sig key."""
        ref = str(ref or "").strip()
        if ref in self.sigs:
            return ref
        if ref in self.rows:
            return self.rows[ref]["sigkey"]
        hits = {r["sigkey"] for k, r in self.rows.items() if k.startswith(ref) or r["sigkey"].startswith(ref)}
        hits |= {k for k in self.sigs if k.startswith(ref)}
        return hits.pop() if len(hits) == 1 and ref else None

    def rate(self, skey, now=None):
        """Occurrences per hour of a signature over the rate window (from the
        per-scan (ts, total count) history; >= 10 min of span to say anything)."""
        now = now or self.now()
        hist = [h for h in self.sig(skey).get("hist") or [] if now - h[0] <= self.cfg["rate_window_s"]]
        if len(hist) < 2:
            return 0.0
        span = max(hist[-1][0] - hist[0][0], 600.0)
        return max(0.0, (hist[-1][1] - hist[0][1]) * 3600.0 / span)

    def _baseline(self, skey, now):
        rows = [r for r in self.rows.values() if r["sigkey"] == skey]
        return {"rate": round(self.rate(skey, now), 3),
                "sources": sorted({r.get("source") or "" for r in rows}),
                "loci": sorted({r.get("locus") or "" for r in rows}),
                "sev": max((sev_rank(r.get("severity")) for r in rows), default=0)}

    def set_disposition(self, ref, disposition, reason="", by="", now=None, proposal_id=None):
        now = now or self.now()
        skey = self.resolve_sig(ref)
        disposition = str(disposition or "").strip().lower()
        if skey is None or disposition not in DISPOSITIONS:
            return None
        s = self.sig(skey)
        s.update(disposition=disposition, reason=str(reason or "")[:500], by=str(by or "")[:40], at=now)
        if disposition == "inert":
            s["baseline"] = self._baseline(skey, now)
            s.pop("reopen_reason", None)
        if disposition == "accepted":
            s["accepted_at"] = now
            s["accepted_id"] = proposal_id or s.get("accepted_id") or ""
        if proposal_id:
            for p in s.get("proposals") or []:
                if p.get("id") == proposal_id:
                    p["disposition"], p["reason"] = disposition, s["reason"]
        for r in self.rows.values():
            if r["sigkey"] == skey:
                r["propose_due"] = False if disposition in ("inert", "accepted") else r.get("propose_due", False)
                if disposition == "inert":
                    r["pending_mail"] = False
        self.save()
        return s

    def _material_change(self, skey, now):
        s = self.sig(skey)
        base = s.get("baseline") or {}
        rows = [r for r in self.rows.values() if r["sigkey"] == skey and r.get("active")]
        for r in rows:
            if (r.get("source") or "") not in (base.get("sources") or []):
                return "new source %s (inert covered %s)" % (r.get("source"), ", ".join(base.get("sources") or []) or "none")
            if (r.get("locus") or "") not in (base.get("loci") or []):
                return "new locus %s (inert covered %s)" % (r.get("locus"), ", ".join(base.get("loci") or []) or "none")
            if sev_rank(r.get("severity")) > int(base.get("sev") or 0):
                return "severity rose to %s" % r.get("severity")
        rate, was = self.rate(skey, now), float(base.get("rate") or 0.0)
        floor = max(was, 1.0)
        if rate >= self.cfg["inert_rate_factor"] * floor:
            return "rate %.0f/h >= %.0fx the %.1f/h when marked inert" % (rate, self.cfg["inert_rate_factor"], was)
        return None

    # ---- the scan step ----------------------------------------------------------------
    def notify_row(self, r):
        src = str(r.get("source") or "")
        return (sev_rank(r.get("severity")) >= sev_rank(self.cfg["min_severity"])
                and not any(src.startswith(p) for p in self.cfg["strip_only_prefixes"] or ())
                and self.sig(r["sigkey"]).get("disposition") != "inert")

    def step(self, emitted, live, now=None):
        """Fold one scan. ``emitted``: [(finding, reason)] for EVERY finding the
        scan saw (reason 'new'|'jump'|'returned'|None); ``live``: {finding key:
        last_seen} of every persisted finding. Returns
        {new, updated, cleared, reopened, mail, board, propose} (lists of rows)."""
        now = now or self.now()
        ev = {"new": [], "updated": [], "cleared": [], "reopened": [], "mail": [], "board": [], "propose": []}
        touched = set()
        for f, reason in emitted or []:
            k = f.get("hash") or f.get("key")
            if not k:
                continue
            skey = sig_key(f.get("kind"), f.get("signature"))
            row = self.rows.get(k)
            fresh = row is None
            reactivated = bool(row) and not row.get("active")
            if fresh:
                row = self.rows[k] = {"key": k, "sigkey": skey, "board_id": None, "mailed_at": 0,
                                      "first_seen": f.get("first_seen") or now}
            prev_count = int(row.get("count") or 0)
            row.update(kind=f.get("kind"), source=f.get("source"), locus=f.get("locus") or "",
                       severity=f.get("severity") or "low", signature=f.get("signature") or "",
                       count=int(f.get("count") or 0), last_seen=float(f.get("last_seen") or now),
                       sample_lines=list(f.get("sample_lines") or [])[:5],
                       action=default_action(f),
                       identity="%s · %s: %s" % (f.get("locus") or "this station", f.get("source"),
                                                 (f.get("signature") or "")[:90]))
            touched.add(k)
            s = self.sig(skey)
            if fresh or reactivated:
                row.update(active=True, cleared_at=None, pending_mail=True,
                           board_id=None if reactivated else row.get("board_id"))
                if s.get("disposition") not in ("inert", "accepted"):
                    row["propose_due"] = True
                (ev["new"] if fresh else ev["reopened"]).append(row)
            else:
                ev["updated"].append(row)
                if reason in ("jump", "returned"):
                    row["pending_mail"] = True
            # accepted fix did not hold: new occurrences after the accept
            if (s.get("disposition") == "accepted" and s.get("accepted_at")
                    and row["last_seen"] > float(s["accepted_at"]) and (reason or row["count"] > prev_count)):
                s.update(disposition="open", reopen_reason="recurred after accepted fix %s — it did not hold"
                         % (s.get("accepted_id") or "?"), did_not_hold=s.get("accepted_id") or "?")
                row.update(pending_mail=True, propose_due=True)
                if row not in ev["reopened"]:
                    ev["reopened"].append(row)
        # per-signature totals -> rate history
        totals = {}
        for r in self.rows.values():
            if r.get("active") or r["key"] in touched:
                totals[r["sigkey"]] = totals.get(r["sigkey"], 0) + int(r.get("count") or 0)
        for skey, tot in totals.items():
            h = self.sig(skey).setdefault("hist", [])
            h.append([now, tot])
            self.sig(skey)["hist"] = [x for x in h if now - x[0] <= 2 * self.cfg["rate_window_s"]][-200:]
        # inert signatures: re-open only on a material change
        for skey, s in self.sigs.items():
            if s.get("disposition") != "inert":
                continue
            why = self._material_change(skey, now)
            if why:
                s.update(disposition="open", reopen_reason="re-opened from inert: " + why, reopened_at=now)
                for r in self.rows.values():
                    if r["sigkey"] == skey and r.get("active"):
                        r.update(pending_mail=True, propose_due=True, board_id=None)
                        if r not in ev["reopened"]:
                            ev["reopened"].append(r)
        # quiet for an hour -> resolved
        for k, r in list(self.rows.items()):
            ls = max(float(r.get("last_seen") or 0), float((live or {}).get(k) or 0))
            r["last_seen"] = ls
            if r.get("active") and now - ls >= self.cfg["quiet_s"]:
                r.update(active=False, cleared_at=now, pending_mail=False, propose_due=False)
                ev["cleared"].append(r)
            elif not r.get("active") and now - float(r.get("cleared_at") or now) > self.cfg["keep_cleared_s"]:
                self.rows.pop(k, None)
        for r in self.rows.values():
            if not r.get("active") or not self.notify_row(r):
                continue
            if r.get("pending_mail") and now - float(r.get("mailed_at") or 0) >= self.cfg["mail_cooldown_s"]:
                ev["mail"].append(r)
            if not r.get("board_id"):
                ev["board"].append(r)
            if r.get("propose_due"):
                ev["propose"].append(r)
        self.save()
        return ev

    def strip_rows(self, now=None):
        """Rows for the ⚠ strip: active findings (inert ones flagged, greyed)."""
        out = []
        for r in sorted(self.rows.values(), key=lambda r: (-sev_rank(r.get("severity")), -(r.get("last_seen") or 0))):
            if not r.get("active"):
                continue
            s = self.sig(r["sigkey"])
            props = [p for p in s.get("proposals") or [] if p.get("status") == "delivered"]
            out.append({"key": r["key"], "sigkey": r["sigkey"], "source": "finding:%s" % r.get("kind"),
                        "identity": r.get("identity"), "count": r.get("count"), "severity": r.get("severity"),
                        "first_seen": r.get("first_seen"), "last_seen": r.get("last_seen"), "active": True,
                        "board_id": r.get("board_id"), "action": r.get("action"),
                        "detail": "\n".join(r.get("sample_lines") or [])[:600],
                        "disposition": s.get("disposition"), "disposition_reason": s.get("reason") or "",
                        "inert": s.get("disposition") == "inert", "notify": self.notify_row(r),
                        "proposal_id": props[-1]["id"] if props else None,
                        "reopen_reason": s.get("reopen_reason") or ""})
        return out


# ── formatting ─────────────────────────────────────────────────────────────────────────
def fmt_mail(row, sig=None):
    why = (sig or {}).get("reopen_reason") or ""
    return ("🐞 finding [%s] %s ×%d (first %s, last %s)%s\n%s\nDo:\n```bash\n%s\n```"
            % (row.get("severity"), row.get("identity"), int(row.get("count") or 0), _hhmm(row.get("first_seen")),
               _hhmm(row.get("last_seen")), ("\nwhy now: " + why) if why else "",
               "\n".join("> " + s[:240] for s in (row.get("sample_lines") or [])[:3]), row.get("action") or ""))


def fmt_board(row, sig=None):
    why = (sig or {}).get("reopen_reason") or ""
    text = ("[finding] %s %s ×%d" % (row.get("kind"), row.get("identity"), int(row.get("count") or 0)))[:500]
    note = ("%s%s\n\nfirst %s · last %s · finding %s · signature %s\n\n```\n%s\n```\n\n```bash\n%s\n```\n\n"
            "close with a disposition line: `inert: <reason>` silences this signature until it materially "
            "changes; plain close = handled.") % (
        ("why now: " + why + "\n\n") if why else "", row.get("signature") or "",
        _ymdhm(row.get("first_seen")), _hhmm(row.get("last_seen")), row.get("key"), row.get("sigkey"),
        "\n".join(s[:240] for s in (row.get("sample_lines") or [])[:5]), row.get("action") or "")
    return text, note[:4000]


def fmt_resolve(row):
    return ("quiet since %s — auto-resolved by the station notifier (no occurrence for %d min); it re-opens "
            "if the finding returns." % (_ymdhm(row.get("last_seen")), DEFAULTS["quiet_s"] // 60))


DISP_MARKER = "close with a disposition line"
_DISP_RE = re.compile(r"(?im)^\s*(?:[-*>]\s*)?(inert|reject(?:ed)?|accept(?:ed)?)\s*(?:[:\-—]\s*(.*))?$")


def parse_disposition(note):
    """The keeper's close comment -> (disposition, reason) or (None, ''). The
    LAST disposition line wins ("inert: <reason>", "reject: <reason>", "accept")."""
    note = str(note or "")
    marker = note.rfind(DISP_MARKER)
    if marker >= 0:                     # only the keeper's text AFTER our own note counts
        nl = note.find("\n", marker)
        note = note[nl + 1:] if nl >= 0 else ""
    last = None
    for m in _DISP_RE.finditer(note):
        last = m
    if not last:
        return None, ""
    word = last.group(1).lower()
    disp = "inert" if word.startswith("inert") else "rejected" if word.startswith("reject") else "accepted"
    return disp, (last.group(2) or "").strip()[:500]


# ── fan-out: one notifier for loops and findings ───────────────────────────────────────
async def fanout(ev, sink, *, fmt_mail, fmt_board, fmt_resolve, now=None, by="station",
                 board=True, mail=True, priority_of=None):
    """Deliver an event set through a sink. Rows are only marked (mailed_at,
    board_id) when the sink call succeeds, so an unreachable keeper is retried
    on the next pass. Returns {"mailed": n, "posted": n, "resolved": n, "errors": n}."""
    now = now or time.time()
    st = {"mailed": 0, "posted": 0, "resolved": 0, "errors": 0}
    pinged = set()
    if board:
        rows = ev.get("board")
        if rows is None:
            rows = list(ev.get("new") or []) + list(ev.get("updated") or [])
        for row in rows:
            if row.get("board_id"):
                continue
            text, note = fmt_board(row)
            try:
                bid = await sink.board(row, text, note, by=by, type="todo",
                                       priority=(priority_of(row) if priority_of else "high"))
            except Exception:                                   # noqa: BLE001
                st["errors"] += 1
                continue
            if bid:
                row["board_id"] = bid
                st["posted"] += 1
                if getattr(sink, "board_is_mail", False):      # the [ping] IS the first ✉ (relay delivers it)
                    row["mailed_at"], row["pending_mail"] = now, False
                    pinged.add(id(row))
    if mail:
        for row in ev.get("mail") or []:
            if id(row) in pinged:
                continue
            try:
                await sink.mail(row, fmt_mail(row), by=by)
                row["mailed_at"], row["pending_mail"] = now, False
                st["mailed"] += 1
            except Exception:                                   # noqa: BLE001
                st["errors"] += 1
    for row in ev.get("cleared") or []:
        if not row.get("board_id"):
            continue
        try:
            await sink.resolve(row, fmt_resolve(row))
            st["resolved"] += 1
        except Exception:                                       # noqa: BLE001
            st["errors"] += 1
    return st


class LocalSink:
    """This station IS the keeper: ✉ into its own keeper mail, todos on its board."""

    remote = False

    def __init__(self, post, write_mail, keeper_locus="keeper", origin="keeper", station=""):
        self.post, self.write_mail = post, write_mail
        self.keeper_locus, self.origin, self.station = keeper_locus, origin, station

    def _by(self, by):
        return str(by or "station")[:24]

    async def mail(self, row, text, by="station"):
        self.write_mail(self._by(by), text)

    board_is_mail = True

    async def board(self, row, text, note, by="station", type="todo", priority="high"):
        """A finding / loop -> ONE `[ping]` request on the keeper's board (comms
        ping kind=request): the keeper relay nudges the keeper's serve session
        and files it in ✉. Other types (B's proposals) are plain board rows."""
        if type == "todo":
            ref = "%s:%s" % (str(by).split("@")[0], row.get("key") or "")
            res = await self.post("comms/ping", {"to": self.keeper_locus, "from_": self._from(),
                                                 "kind": "request", "text": self._tag(text) + "\n\n" + note,
                                                 "ref": ref})
            bid = str((res or {}).get("id") or "") if isinstance(res, dict) else ""
            if bid:
                # the action lives in the NOTE too (after the [ping] marker the relay
                # keys on), so a triage edit of the text never loses the command
                try:
                    await self.post("todo/update", {"id": bid, "note": "[ping] from %s (ref %s)\n\n%s"
                                                     % (self._from(), ref, note)})
                except Exception:                           # noqa: BLE001 — the text still carries it
                    pass
        else:
            res = await self.post("todo/add", {"text": self._tag(text)[:500], "type": type, "priority": priority,
                                               "note": note, "by": self._by(by), "locus": self.keeper_locus,
                                               "source": "station:" + (self.station or self.origin)})
        if isinstance(res, dict) and res.get("error"):
            raise RuntimeError(res["error"])
        return str((res or {}).get("id") or "") if isinstance(res, dict) else ""

    def _tag(self, text):
        return text

    def _from(self):
        return re.sub(r"[^a-z0-9-]", "-", str(self.origin or "station").lower())[:40] or "station"

    async def resolve(self, row, note):
        await self.post("todo/update", {"id": row["board_id"], "status": "done", "note": note,
                                        "locus": self.keeper_locus})

    async def closed(self, ids):
        """{id: note} for the given board ids that are closed (status done)."""
        res = await self.post("todo/list", {"status": "done", "locus": self.keeper_locus, "limit": 500})
        want = set(ids)
        return {str(r.get("id")): r.get("note") or "" for r in (res or []) if isinstance(r, dict)
                and str(r.get("id")) in want}


class RemoteSink(LocalSink):
    """A station whose locus is NOT the keeper: everything lands on the KEEPER —
    board rows on the keeper's slice, ✉ as a comms ping kind=message — tagged
    with the originating locus (dedup key already carries the locus)."""

    remote = True

    def _by(self, by):
        base = str(by or "station").split("@")[0].replace("station-", "")
        return ("%s@%s" % (base, self.origin))[:24]

    async def mail(self, row, text, by="station"):
        res = await self.post("comms/ping", {"to": self.keeper_locus, "from_": self.origin, "kind": "message",
                                             "text": "[%s] %s" % (self.origin, text),
                                             "ref": "finding:%s" % (row.get("key") or "")})
        if isinstance(res, dict) and res.get("error"):
            raise RuntimeError(res["error"])

    def _tag(self, text):
        return "[%s] %s" % (self.origin, text)


# ── token burn ─────────────────────────────────────────────────────────────────────────
class TokenMeter:
    """Model tokens spent by this station's own tasks. A task whose projected
    daily spend exceeds its budget is a ``token_burn`` finding (so a costly
    periodic task surfaces even when it 'works')."""

    def __init__(self, state_path=None, now=None, budget=200_000, window_s=86400, budgets=None):
        self.state_path, self.now = state_path, now or time.time
        self.budget, self.window_s = int(budget), int(window_s)
        self.budgets = dict(budgets or {})
        self.events = {}       # task -> [[ts, tokens]]
        try:
            with open(state_path, encoding="utf-8") as fh:
                self.events = dict(json.load(fh).get("events") or {})
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        if not self.state_path:
            return
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"events": self.events}, fh)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def add(self, task, tokens, ts=None):
        ts = ts or self.now()
        ev = self.events.setdefault(str(task), [])
        ev.append([ts, int(tokens or 0)])
        self.events[str(task)] = [e for e in ev if ts - e[0] <= self.window_s][-5000:]
        self.save()

    def rates(self, now=None):
        now = now or self.now()
        out = {}
        for task, ev in self.events.items():
            ev = [e for e in ev if now - e[0] <= self.window_s]
            if not ev:
                continue
            tot = sum(e[1] for e in ev)
            span = min(self.window_s, max(now - ev[0][0], 3600.0))
            out[task] = {"tokens": tot, "calls": len(ev), "per_day": int(tot * 86400.0 / span),
                         "since": ev[0][0], "budget": int(self.budgets.get(task, self.budget))}
        return out

    def findings(self, now=None, locus=""):
        """log_findings-shaped rows (kind token_burn) for tasks over budget."""
        now = now or self.now()
        out = []
        for task, r in self.rates(now).items():
            if r["per_day"] <= r["budget"]:
                continue
            sig = "station task %s spends model tokens above its daily budget" % task
            out.append({"key": hashlib.sha1(f"{locus}|token_burn|{task}|{sig}".encode()).hexdigest()[:12],
                        "kind": "token_burn", "source": task, "locus": locus,
                        "severity": "high" if r["per_day"] >= 5 * r["budget"] else "medium",
                        "count": 1, "first_seen": r["since"], "last_seen": now, "signature": sig,
                        "sample_lines": ["~%s tokens/day projected (%s tokens over %d calls since %s; budget %s/day)"
                                         % (format(r["per_day"], ","), format(r["tokens"], ","), r["calls"],
                                            _ymdhm(r["since"]), format(r["budget"], ","))],
                        "suggested_action": ("cut the %s task's model calls (interval / prompt size), or raise "
                                             "STATION_TOKEN_BUDGET_%s if the spend is intended"
                                             % (task, re.sub(r"\W", "_", task).upper())),
                        "_stamps": [now]})
        return out
