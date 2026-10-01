"""Bug reports -> the locus's WORKER serve session (operator 2026-10-01).

"Bug reports must not hit the keeper or anyone else. They go ONLY to the WORKER
standing session of the locus." Bug reports = the station's findings scanner
(🐞 bug-scan), the loop detector (crash / call / job loops), issue-gate raises
and, when STATION_B_PROPOSE=1, B's fix proposals. The keeper, the chat session,
B and the tmux seats receive none of them: no comms ping, no ✉ keeper mail, no
reminder-digest entry.

No silent suppression (board t4178 [F2.5]): every report is

  * a board item of type ``finding`` (id fNNN) on the locus's own board slice,
    marked ``[bug-report]`` / source ``station:bug-report`` — the marker the
    keeper's reminder digest EXCLUDES;
  * delivered to the locus's worker serve session through the same per-locus
    serve delivery as the ✍ prompt bar (prompt_send.deliver_serve, role
    ``worker``), with the outcome — delivered / queued / NOT delivered + reason
    — written onto the board item. NOT delivered stays pending and is retried
    on every pass; nothing is dropped.

Flood guards are kept but only COALESCE or DEFER:

  * ONE message per locus per pass carries every pending report (×N counts);
  * one hand-off in flight per locus: while the previous report is still queued
    in the worker session, new reports coalesce behind it (board says so);
  * at most one send per ``min_interval`` per locus (deferred, not dropped);
  * self-origin reports (the serve / keeper's own units, cs-* sessions — the
    serve flood's feedback loop) are coalesced into at most one send per
    ``self_interval``; the board item shows them at once.

Pure logic + injected I/O (``io``: board_add, board_update, board_done,
deliver, inflight_busy) — server.py wires HTTP, the tests hand in fakes.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import time

MARK = "[bug-report]"
BOARD_TYPE = "finding"
SOURCE = "station:bug-report"
ROLE = "worker"
MIN_INTERVAL_S = 120
SELF_INTERVAL_S = 3600
INFLIGHT_MAX_S = 900         # 1.0.146 (t4260): a report queued behind a busy worker turn is waited
                             # for at most this long; then it is pulled out and re-sent (never held)
SELF_DEFAULT = "keeper,keeper:*,keeper/*,cs-*,abstract-claude-serve*,ac-serve*"
MSG_MAX = 12_000
BODY_MAX = 1_500
KEEP_S = 7 * 86400

_LEGACY_BUG_RE = re.compile(
    r"^\s*(?:\[(?:ping|msg)\]\s*)*(?:\[[A-Za-z0-9@._-]+\]\s*)?"
    r"(?:\[finding\]|\[loop\]|\[B proposal\]|🐞 finding|⚠ loop detected|🛠 B proposed)")


def _hm(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t or 0))) if t else "—"


def self_patterns(env=None):
    env = os.environ if env is None else env
    raw = env.get("STATION_BUGREPORT_SELF") or SELF_DEFAULT
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def is_self(*vals, patterns=None):
    pats = self_patterns() if patterns is None else patterns
    for v in vals:
        v = str(v or "").strip().lower()
        if v and any(fnmatch.fnmatchcase(v, p) or fnmatch.fnmatchcase(v.split("@")[0], p) for p in pats):
            return True
    return False


def is_bug_ping(p):
    """A comms ping that is really a bug report (a pre-2026-10-01 station posts
    findings / loops / B proposals to the keeper as [ping] requests): it is
    re-routed to the worker, never mailed or nudged to the keeper."""
    if not isinstance(p, dict):
        return False
    note, text = str(p.get("note") or ""), str(p.get("text") or "")
    ref = str(p.get("ref") or "")
    if MARK in note or MARK in text[:200] or "issue_fp=" in note or "issue_fp=" in text:
        return True
    if re.search(r"\(ref (?:station-findings|station-loops|finding):", note) or \
            ref.startswith(("station-findings:", "station-loops:", "finding:")):
        return True
    return bool(_LEGACY_BUG_RE.match(text))


def ping_origin(p):
    """The originating locus a legacy remote station tagged its bug ping with ('[origin] …')."""
    m = re.match(r"^\s*(?:\[(?:ping|msg)\]\s*)*\[([A-Za-z0-9@._-]+)\]", str((p or {}).get("text") or ""))
    return m.group(1).lower() if m and m.group(1).lower() not in ("finding", "loop") else ""


class Outbox:
    """Pending bug reports per locus (vm '' = this station's own host)."""

    def __init__(self, state_path=None, now=None, min_interval=None, self_interval=None, patterns=None,
                 inflight_max=None):
        self.state_path = state_path
        self.now = now or time.time
        self.min_interval = MIN_INTERVAL_S if min_interval is None else int(min_interval)
        self.self_interval = SELF_INTERVAL_S if self_interval is None else int(self_interval)
        self.inflight_max = INFLIGHT_MAX_S if inflight_max is None else int(inflight_max)
        self.patterns = patterns
        self.items = {}            # key -> entry
        self.loci = {}             # vm -> {inflight, last_sent, last_self_sent}
        self._load()

    # ---- persistence ------------------------------------------------------------------
    def _load(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.items = dict(doc.get("items") or {})
            self.loci = dict(doc.get("loci") or {})
        except (OSError, ValueError):
            pass

    def save(self):
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"items": self.items, "loci": self.loci, "saved": self.now()}, fh)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # ---- intake -----------------------------------------------------------------------
    def offer(self, key, *, vm="", locus="", kind="finding", title="", body="", severity="medium",
              count=1, source="", caller="", annotation="", priority=None, fresh=True):
        """Record one report sighting. Never drops: an entry already pending is
        updated in place (its count carries the coalesced sightings); a delivered
        one becomes pending again with "+N since <last report>" when it is
        ``fresh`` (new / returned / count jumped) or its count grew. A re-offer of
        a delivered report with nothing new only refreshes its metadata."""
        now = self.now()
        e = self.items.get(key)
        if e is not None and not e.get("pending") and not fresh and not e.get("board_closed") \
                and int(count or 0) <= int(e.get("since_count") or 0):
            e.update(annotation=str(annotation or e.get("annotation") or "")[:400])
            return e
        if e is None:
            e = self.items[key] = {"key": key, "first_offer": now, "since_count": 0, "delivered_at": 0,
                                   "board_id": None, "attempts": 0, "state": "pending", "reason": "",
                                   "noted": ""}
        elif e.get("board_closed"):
            e.update(board_id=None, board_closed=False, noted="")   # a closed item: the return is a NEW item
        selfo = is_self(source, caller, patterns=self.patterns)
        e.update(vm=vm or "", locus=locus or "", kind=kind, title=str(title)[:500], body=str(body)[:4000],
                 severity=str(severity or "medium"), count=int(count or 0), source=str(source or ""),
                 self=selfo, annotation=str(annotation or "")[:400], last_offer=now, pending=True,
                 priority=priority or ("low" if selfo else ("high" if str(severity) in ("high", "crit") else "medium")))
        e.pop("cleared", None)
        if e.get("state") in ("delivered", "queued"):
            e.update(state="pending", reason="")
        return e

    def clear(self, key, note):
        """The finding went quiet / the loop stopped: close its board item once the
        report itself has been delivered (an undelivered report still goes out)."""
        e = self.items.get(key)
        if e is not None:
            e["cleared"] = str(note or "")[:2000]

    def board_closed(self, board_id):
        for e in self.items.values():
            if e.get("board_id") and str(e["board_id"]) == str(board_id):
                e["board_closed"] = True

    def pending(self, vm=None):
        return [e for e in self.items.values() if e.get("pending") and (vm is None or e.get("vm", "") == vm)]

    def vms(self):
        return sorted({e.get("vm", "") for e in self.items.values() if e.get("pending") or e.get("cleared")})

    # ---- formatting -------------------------------------------------------------------
    def since_line(self, e):
        grew = max(0, int(e.get("count") or 0) - int(e.get("since_count") or 0))
        if e.get("delivered_at"):
            return "×%d total, +%d since the last report %s" % (int(e.get("count") or 0), grew, _hm(e["delivered_at"]))
        return "×%d (first report; seen since %s)" % (int(e.get("count") or 0), _hm(e.get("first_offer")))

    def board_text(self, e):
        return ("%s %s" % (MARK, e.get("title") or e.get("key")))[:500]

    def board_note(self, e, delivery=""):
        lines = [MARK + " owner: the WORKER session of locus %s (operator 2026-10-01). Not a keeper item."
                 % (e.get("locus") or e.get("vm") or "this station"),
                 "kind %s · severity %s · priority %s · %s" % (e.get("kind"), e.get("severity"), e.get("priority"),
                                                             self.since_line(e))]
        if e.get("self"):
            lines.append("self-origin (%s): coalesced to <=1 worker report per %d min — shown here at once"
                         % (e.get("source"), self.self_interval // 60))
        if e.get("annotation"):
            lines.append("issue: " + e["annotation"])
        lines += ["", e.get("body") or "", "",
                  "close with a disposition line: `inert: <reason>` records it as inert — that LOWERS its priority, "
                  "it is still reported (board t4178); `accept` = fixed (it re-opens if it comes back); plain close = handled."]
        if delivery:
            lines += ["", delivery]
        return "\n".join(lines)

    def compose(self, entries, locus):
        n = len(entries)
        head = ("🐞 %d bug report%s for locus %s — you (the worker session) own bug reports (operator "
                "2026-10-01). Investigate, fix what is in scope, and update/close each board item "
                "(comment the result; `inert: <reason>` lowers its priority)." % (n, "s" if n > 1 else "", locus))
        parts, used, rest = [head], len(head), []
        order = sorted(entries, key=lambda e: ({"high": 0, "medium": 1, "low": 2}.get(e.get("priority"), 1),
                                               -float(e.get("last_offer") or 0)))
        for i, e in enumerate(order, 1):
            block = "\n\n%d. [%s/%s] %s — %s — board %s%s\n%s" % (
                i, e.get("severity"), e.get("priority"), e.get("title"), self.since_line(e),
                e.get("board_id") or "(board write pending)",
                ("\n   issue: " + e["annotation"]) if e.get("annotation") else "",
                (e.get("body") or "")[:BODY_MAX])
            if used + len(block) > MSG_MAX:
                rest.append(e)
                continue
            parts.append(block)
            used += len(block)
        if rest:   # never dropped: listed by title + board id (the board carries the full text)
            parts.append("\n\n…and %d more (full text on the board): %s" % (
                len(rest), "; ".join("%s %s" % (e.get("board_id") or "?", (e.get("title") or "")[:80]) for e in rest)))
        return "".join(parts)

    @staticmethod
    def delivery_line(e, now):
        st = e.get("state")
        if st in ("delivered", "queued"):
            return "delivery: %s → worker session %s at %s%s" % (
                st, e.get("session_id") or "?", _hm(e.get("delivered_at") or now),
                (" (" + e["detail"] + ")") if e.get("detail") else "")
        if st == "pending" and e.get("attempts"):
            return "delivery: NOT delivered yet — %s (attempt %d at %s; re-pended, retried every pass)" % (
                e.get("reason") or "?", int(e.get("attempts") or 0), _hm(now))
        return "delivery: %s — %s" % (st or "pending", e.get("reason") or "waiting for the next pass")

    # ---- one pass for one locus ---------------------------------------------------------
    async def run_pass(self, vm, io, locus_label=""):
        """Board every pending report, then deliver ONE coalesced message to the
        locus's worker unless a flood guard defers it. Returns a summary dict."""
        now = self.now()
        st = {"vm": vm, "boarded": 0, "sent": 0, "state": "", "reason": "", "requeued": ""}
        ents = self.pending(vm)
        for e in ents:
            if not e.get("board_id"):
                try:
                    bid = await io.board_add(e, self.board_text(e), self.board_note(e, self.delivery_line(e, now)))
                except Exception as ex:                    # noqa: BLE001 — retried next pass
                    bid, e["board_error"] = None, str(ex)[:200]
                if bid:
                    e["board_id"], e["noted"] = str(bid), e.get("state")
                    e.pop("board_error", None)
                    st["boarded"] += 1
        loc = self.loci.setdefault(vm or "", {"inflight": None, "last_sent": 0, "last_self_sent": 0})
        due = [e for e in ents if not e.get("self")]
        if any(e.get("self") for e in ents) and now - float(loc.get("last_self_sent") or 0) >= self.self_interval:
            due += [e for e in ents if e.get("self")]
        held = [e for e in ents if e not in due]
        why = ""
        if ents and loc.get("inflight"):
            w = loc["inflight"]
            try:
                busy = await io.inflight_busy(w)
            except Exception:                              # noqa: BLE001 — cannot tell: keep waiting
                busy = True
            waited = now - float(w.get("at") or now)
            if busy and waited >= self.inflight_max:
                # 1.0.146: the drain deadline — pull the stale hand-off out of the
                # worker queue and send a fresh coalesced report now (never an
                # open-ended "coalescing behind")
                cancel = getattr(io, "inflight_cancel", None)
                if cancel is not None:
                    try:
                        await cancel(w)
                    except Exception:                      # noqa: BLE001 — best effort
                        pass
                loc["inflight"] = None
                loc["inflight_requeues"] = int(loc.get("inflight_requeues") or 0) + 1
                st["requeued"] = ("previous report %s in worker session %s waited %d min behind a busy turn — "
                                  "pulled out and re-sent (requeue #%d)"
                                  % (",".join(w.get("message_ids") or []), w.get("sid"), int(waited // 60),
                                     loc["inflight_requeues"]))
                busy = False
            if busy:
                why = ("coalescing behind the previous report (%s) still queued in worker session %s "
                       "(%d min of at most %d)" % (",".join(w.get("message_ids") or []), w.get("sid"),
                                                   int(waited // 60), self.inflight_max // 60))
            else:
                loc["inflight"] = None
        if not why and due and now - float(loc.get("last_sent") or 0) < self.min_interval:
            why = "rate limit: next worker report after %s" % _hm(float(loc["last_sent"]) + self.min_interval)
        for e in held:
            self._note_state(e, "deferred", "self-origin: coalesced into the next self report after %s"
                             % _hm(float(loc.get("last_self_sent") or 0) + self.self_interval))
        if why or not due:
            for e in due:
                self._note_state(e, "deferred", why)
            await self._sync_notes(ents, io, now)
            await self._resolve(vm, io)
            st.update(state="deferred" if due else "idle", reason=why)
            self.save()
            return st
        text = self.compose(due, locus_label or vm or "this station")
        try:
            res = await io.deliver(text)
        except Exception as ex:                            # noqa: BLE001
            res = {"ok": False, "state": "failed", "reason": "deliver error: %s" % ex}
        ok = bool(res.get("ok"))
        for e in due:
            e["attempts"] = int(e.get("attempts") or 0) + 1
            if ok:
                e.update(state=res.get("state") or "delivered", reason="", pending=False, delivered_at=now,
                         since_count=int(e.get("count") or 0), session_id=res.get("session_id") or "",
                         detail=str(res.get("detail") or "")[:160], attempts=0)
            else:
                # never a terminal "failed" (t4260): the report stays pending with
                # the reason and goes out on the next pass that finds a worker
                e.update(state="pending", reason=str(res.get("reason") or "not delivered")[:300])
        if ok:
            loc["last_sent"] = now
            if any(e.get("self") for e in due):
                loc["last_self_sent"] = now
            if res.get("message_ids"):
                loc["inflight"] = {"sid": res.get("session_id"), "message_ids": list(res["message_ids"]), "at": now}
        await self._sync_notes(ents, io, now, force=True)
        await self._resolve(vm, io)
        st.update(sent=len(due) if ok else 0, state=res.get("state") if ok else "pending",
                  reason=res.get("reason") or "")
        self.save()
        return st

    def _note_state(self, e, state, reason):
        e["state"], e["reason"] = state, reason

    async def _sync_notes(self, ents, io, now, force=False):
        """Write the delivery state onto each board item when it changed."""
        for e in ents:
            if not e.get("board_id"):
                continue
            sig = "%s|%s|%s" % (e.get("state"), e.get("reason"), e.get("delivered_at"))
            if not force and e.get("noted") == sig:
                continue
            try:
                await io.board_update(e, self.board_note(e, self.delivery_line(e, now)))
                e["noted"] = sig
            except Exception:                              # noqa: BLE001 — retried next pass
                pass

    async def _resolve(self, vm, io):
        now = self.now()
        for k, e in list(self.items.items()):
            if (e.get("vm", "") == (vm or "") and not e.get("pending") and not e.get("cleared")
                    and now - float(e.get("last_offer") or 0) > KEEP_S):
                self.items.pop(k, None)                    # delivered + silent for a week: history is on the board
                continue
            if e.get("vm", "") != (vm or "") or not e.get("cleared") or e.get("pending"):
                continue
            if e.get("board_id") and not e.get("board_closed"):
                try:
                    await io.board_done(e, e["cleared"])
                except Exception:                          # noqa: BLE001
                    continue
            self.items.pop(k, None)
