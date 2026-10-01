"""Station holds — every coalesce / defer / skip the station performs, VISIBLE
(1.0.146, board t4260; operator ruling d4254).

A core guarantee (prompts go out at idle, pings and digests are delivered,
standing sessions can act, the keeper is reachable) never has an off switch, a
silent hold, an attempt cap or an unbounded defer. What remains is a bounded
coalesce or a visible, owned, expiring operator action. This registry is where
every such state is RECORDED so the ⚠ strip (GET /api/loops → `holds`) shows
it with a count, instead of an audit.log line nobody reads:

  note(key, source, identity, detail, action, count=None, inc=False)
        record / refresh one hold row (count: absolute, or inc=True to +1)
  clear(key)        the condition ended (the row leaves the strip)
  rows(now)         strip rows, the loop-detector row shape (source · identity
                    ×count · first/last · action · detail)

Persisted to a JSON file so a restart does not blank the strip. Pure: the
caller injects the clock; no I/O beyond the state file.
"""
from __future__ import annotations

import json
import os
import time

SOURCE_SWITCH = "station:switch"        # a code default that inerts a loop (visible, settable)
SOURCE_HOLD = "station:hold"            # a bounded coalesce / wait in progress
SOURCE_SKIP = "station:skip"            # a delivery the station could not make (pending, retried)
SOURCE_PENDING = "station:pending"      # work re-pended for a later pass (prompts, pings)


class Holds:
    def __init__(self, state_path=None, now=None):
        self.state_path = state_path
        self.now = now or time.time
        self.rows_by_key = {}
        self._load()

    def _load(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.rows_by_key = {str(k): v for k, v in (doc.get("holds") or {}).items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self.rows_by_key = {}

    def save(self):
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"holds": self.rows_by_key, "saved": self.now()}, fh)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def note(self, key, source, identity, detail="", action="", count=None, inc=False, severity="warn", **extra):
        """Record or refresh one visible hold. Returns the row."""
        now = self.now()
        r = self.rows_by_key.get(key)
        if r is None:
            r = self.rows_by_key[key] = {"key": key, "first_seen": now, "count": 0}
        if inc:
            r["count"] = int(r.get("count") or 0) + 1
        elif count is not None:
            r["count"] = int(count)
        elif not r.get("count"):
            r["count"] = 1
        r.update(source=str(source), identity=str(identity)[:160], detail=str(detail)[:600],
                 action=str(action)[:400], severity=severity, last_seen=now, active=True)
        for k, v in extra.items():
            r[k] = v
        self.save()
        return r

    def clear(self, key):
        """The condition ended: the row leaves the strip (returns the old row)."""
        r = self.rows_by_key.pop(key, None)
        if r is not None:
            self.save()
        return r

    def clear_prefix(self, prefix):
        gone = [k for k in self.rows_by_key if k.startswith(prefix)]
        for k in gone:
            self.rows_by_key.pop(k, None)
        if gone:
            self.save()
        return gone

    def get(self, key):
        return self.rows_by_key.get(key)

    def rows(self, now=None):
        """Strip rows, newest first — the loop-detector row shape so the
        fleetview strip renders them with the same code."""
        out = []
        for r in sorted(self.rows_by_key.values(), key=lambda r: -float(r.get("last_seen") or 0)):
            out.append(dict(r, active=True, hold=True))
        return out


def fmt_age(seconds):
    s = max(0, int(seconds or 0))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)
