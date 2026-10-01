"""2026-10-01 (board t4178 [F2.5]): the relay may coalesce or defer, never
silently drop. Lull brain "skip" is off by default; queue overflow coalesces;
a `finding` (bug report, owned by the worker) never becomes a keeper notice."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keeper_relay as R   # noqa: E402


def _patch(name, value):
    old = getattr(R, name)
    setattr(R, name, value)
    return lambda: setattr(R, name, old)


def test_lull_skip_verdict_cannot_withhold_by_default():
    os.environ.pop("KEEPER_RELAY_LULL_SKIP", None)
    snap = [{"id": "t1", "line": "a"}, {"id": "t2", "line": "b"}]
    undo = _patch("_lull_http", lambda cfg, prompt: '{"relay": ["t1"], "skip": ["t2"], "line": "t1 pending"}')
    try:
        relay, skip, line, mode = R._lull_judge({}, snap, "")
        assert relay == ["t1", "t2"] and skip == [] and mode.startswith("ok(skip-off:t2")
        os.environ["KEEPER_RELAY_LULL_SKIP"] = "1"                 # legacy switch
        relay, skip, _l, mode = R._lull_judge({}, snap, "")
        assert relay == ["t1"] and skip == ["t2"] and mode == "ok"
    finally:
        os.environ.pop("KEEPER_RELAY_LULL_SKIP", None)
        undo()


def test_queue_overflow_coalesces_instead_of_dropping():
    audits = []
    undo = _patch("audit", lambda rec: audits.append(rec))
    try:
        state = {"queue": []}
        for i in range(R.MAX_QUEUE + 3):
            R.enqueue(state, {"ts": 1000 + i, "author": "operator", "payload": "line %d" % i,
                              "sha256": "x", "prefix": "line %d" % i})
        head = state["queue"][0]
        assert len(state["queue"]) == R.MAX_QUEUE and head["author"] == "coalesced"
        assert head["n"] == 4 and "line 0" in head["payload"] and "line 3" in head["payload"]
        assert all(a["decision"] == "coalesce" for a in audits) and len(audits) == 4
        assert state["queue"][-1]["payload"] == "line %d" % (R.MAX_QUEUE + 2)
    finally:
        undo()
