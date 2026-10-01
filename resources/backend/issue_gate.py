"""Issue gate client for hugpy Station (1.0.140) — the toolserver's issue registry
(abstract_toolserver >= 0.0.39, category ``issue``) in front of every keeper ping.

SUPERSEDED the same day (operator 2026-10-01, board t4178 [F2.5]: "the
suppression is too much … a silent killer"): the gate no longer decides whether
anything is sent. server.py observes FAIL-OPEN for history and annotation only;
bug reports go to the locus's worker session (bug_route.py) and every comms
ping is nudged. The text below describes the 1.0.140 design.

Operator 2026-10-01 (serve flood): nothing may ping or nudge the keeper unless
``issue_observe`` says ``page=true`` for that sighting. One sighting of one
problem = one observe; the toolserver fingerprints it (volatile ids stripped),
folds it into the issue's rate/state and decides whether it PAGES (first
sighting, a fix that did not hold, a benign issue that escalated) — everything
else is counted, not sent.

Contract here:

  * PAGING FAILS CLOSED. No decision (toolserver down, bad reply) = no ping; the
    caller logs it and the sighting is re-judged on the next pass.
  * BOARD / HISTORY WRITES FAIL OPEN. ``issue_set`` / ``issue_record_*`` errors
    are logged and swallowed — the station keeps working without them.
  * B sees ONE issue: ``memory(fp)`` is keyed on that fingerprint only.

Pure apart from the injected ``call(path, body) -> result`` coroutine
(server.py hands it ``_ts_call``; the tests hand it a fake).
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("station.issue_gate")

# the fingerprint travels inside the ping (text + note) so the relay knows the
# gate already decided it: "issue_fp=<kind>:<16 hex>"
FP_TAG = "issue_fp="
_FP_RE = re.compile(r"issue_fp=([a-z0-9_.:@-]+:[0-9a-f]{16})\b")

SEV = {"low": 0, "info": 0, "medium": 1, "warn": 1, "warning": 1, "high": 2, "error": 2, "crit": 3, "critical": 3}

# station disposition word -> issue state
DISP_STATE = {"inert": "benign", "accepted": "processed", "closed": "processed", "rejected": "raised",
              "open": "raised", "proposed": "known_processing"}


def sev_num(s):
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return max(0, min(3, int(s)))
    return SEV.get(str(s or "").strip().lower(), 1)


def fp_of(ping):
    """The issue fingerprint a ping carries, or ''."""
    if not isinstance(ping, dict):
        return ""
    for k in ("issue_fp",):
        if ping.get(k):
            return str(ping[k])
    m = _FP_RE.search("%s\n%s" % (ping.get("text") or "", ping.get("note") or ""))
    return m.group(1) if m else ""


def tag(note, fp):
    """Append the fingerprint line to a ping's note (idempotent)."""
    note = str(note or "")
    if not fp or (FP_TAG + fp) in note:
        return note
    return note.rstrip() + "\n\n" + FP_TAG + fp


_PING_HEAD_RE = re.compile(r"^\s*(?:\[(?:ping|msg)\]\s*)*(?:from\s+\S+\s*)?(?:\[[A-Za-z0-9@._-]+\]\s*)?")


_AUTO_RESOLVE_RE = re.compile(r"auto-resolved by the station (?:loop detector|notifier)")


def is_auto_resolve(note):
    """The station closed this board item itself (loop stopped / finding quiet):
    not a keeper disposition — the registry auto-processes on silence."""
    return bool(_AUTO_RESOLVE_RE.search(str(note or "")))


def ping_subject(ping):
    """The first line of a comms ping, its [ping]/[locus] head stripped."""
    text = str((ping or {}).get("text") or "").strip()
    first = text.splitlines()[0] if text else ""
    return _PING_HEAD_RE.sub("", first).strip()[:240]


class Gate:
    """Thin async client. ``call(path, body)`` posts a toolserver tool and
    returns its unwrapped result (raises on error)."""

    def __init__(self, call, locus="keeper"):
        self.call = call
        self.locus = locus or "keeper"

    # ---- the paging decision (fail CLOSED) ------------------------------------------
    async def observe(self, *, source, kind, subject, text="", severity=1, caller="", meta=None,
                      locus=None, n=1):
        """-> the gate's reply dict, or None when there is no decision (never page)."""
        body = {"source": str(source or "station")[:120], "kind": str(kind or "manual")[:60],
                "subject": str(subject or "")[:400], "text": str(text or "")[:600],
                "severity": sev_num(severity), "caller": str(caller or "")[:120],
                "meta": meta or {}, "locus": locus or self.locus, "n": max(1, int(n or 1))}
        try:
            res = await self.call("issue/observe", body)
        except Exception as e:                               # noqa: BLE001 — fail closed
            log.warning("issue gate unreachable (%s %s): %s — not paging", kind, str(subject)[:80], e)
            return None
        if not isinstance(res, dict) or not res.get("fp") or "page" not in res:
            log.warning("issue gate: no decision for %s %s (%r) — not paging", kind, str(subject)[:80],
                        str(res)[:160])
            return None
        return res

    async def b_query_ok(self, fp):
        """True only on an explicit ok; False on an explicit no; None when there
        is no answer (fail closed: no B load without a decision, but the caller
        keeps the row due so it is asked again next pass)."""
        if not fp:
            return False
        try:
            res = await self.call("issue/b_query_ok", {"fp": fp})
        except Exception as e:                               # noqa: BLE001
            log.warning("issue_b_query_ok %s: %s — B not queried", fp, e)
            return None
        if not isinstance(res, dict):
            return None
        return bool(res.get("ok"))

    async def get(self, fp):
        try:
            res = await self.call("issue/get", {"fp": fp, "events": 1})
        except Exception as e:                               # noqa: BLE001
            log.warning("issue_get %s: %s", fp, e)
            return None
        return res if isinstance(res, dict) else None

    # ---- B's memory (read; None = fall back to the local NotifyBook) ------------------
    async def memory(self, fp, proposals=5):
        if not fp:
            return None
        try:
            res = await self.call("issue/memory", {"fp": fp, "proposals": proposals})
        except Exception as e:                               # noqa: BLE001
            log.warning("issue_memory %s: %s — NotifyBook fallback", fp, e)
            return None
        if not isinstance(res, dict) or res.get("fp") != fp:
            return None
        return res

    # ---- history / dispositions (fail OPEN) -----------------------------------------
    async def _write(self, path, body):
        try:
            return await self.call(path, body)
        except Exception as e:                               # noqa: BLE001
            log.warning("%s %s: %s (ignored)", path, body.get("fp"), e)
            return None

    async def set_state(self, fp, state, reason="", by="keeper", board_id=""):
        if not fp:
            return None
        return await self._write("issue/set", {"fp": fp, "state": state, "reason": str(reason or "")[:500],
                                               "by": str(by or "keeper")[:80], "board_id": str(board_id or "")})

    async def disposition(self, fp, disp, reason="", by="keeper", board_id=""):
        """A station disposition word (inert/accepted/rejected/closed/open/proposed)."""
        st = DISP_STATE.get(str(disp or "closed").lower())
        if not st:
            return None
        return await self.set_state(fp, st, reason or disp, by=by, board_id=board_id)

    async def record_b(self, fp, proposal, status="delivered", proposal_id="", model="", verdict=""):
        if not fp:
            return None
        return await self._write("issue/record_b", {"fp": fp, "proposal": proposal or {}, "status": status,
                                                    "proposal_id": str(proposal_id or ""), "model": model,
                                                    "verdict": verdict})

    async def record_action(self, fp, action, by="keeper", note="", proposal_id="", disposition="",
                            board_id="", fix=False):
        if not fp:
            return None
        return await self._write("issue/record_action", {
            "fp": fp, "action": str(action or "")[:1000], "by": by, "note": str(note or "")[:800],
            "proposal_id": str(proposal_id or ""), "disposition": disposition or "",
            "board_id": str(board_id or ""), "fix": bool(fix)})


# ── row gating: one observe per sighting, only page=true rows go on ─────────────────
async def gate_rows(gate, rows, spec):
    """``spec(row) -> dict(observe kwargs) | None`` (None = not a sighting, skip).
    Returns {"page": [(row, res)], "quiet": [(row, res)], "undecided": [row]}.
    Each row gets ``issue_fp`` / ``issue_state`` / ``issue_reason`` when decided."""
    out = {"page": [], "quiet": [], "undecided": []}
    seen = set()
    for row in rows or []:
        if id(row) in seen:
            continue
        seen.add(id(row))
        kw = spec(row)
        if not kw:
            continue
        res = await gate.observe(**kw)
        if res is None:
            out["undecided"].append(row)
            continue
        row["issue_fp"], row["issue_state"] = res["fp"], res.get("state")
        row["issue_reason"] = str(res.get("reason") or "")[:200]
        row["issue_b_query"] = bool(res.get("b_query"))
        (out["page"] if res.get("page") else out["quiet"]).append((row, res))
    return out


def memory_priors(mem):
    """issue_memory proposals in the shape b_propose.novelty() compares."""
    out = []
    for p in (mem or {}).get("proposals") or []:
        out.append({"id": p.get("id"), "status": p.get("status"), "proposed_fix": p.get("fix") or "",
                    "commands": p.get("commands") or [], "disposition": p.get("disposition"),
                    "reason": p.get("reason") or ""})
    return out
