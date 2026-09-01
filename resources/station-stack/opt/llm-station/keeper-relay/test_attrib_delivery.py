"""
Attribution-based board delivery (operator 2026-08-31 — replaces the time lull).

A queued board notice no longer waits on a clock. It releases when the keeper is
BETWEEN TASKS, judged by the keeper's own task/status attribution:
  1. board `doing` status  — nothing in flight  => release (deterministic);
  2. a printed `status=done|blocked` line in the reply => release (even mid-task);
  3. hugpy-agent judges an in-flight reply CLEARLY done => release (ambiguous path);
and otherwise HOLDS (fail-closed). The fallback timer (BOARD_MAX_HOLD_SECS) is off
by default; the board item stays open as the durable backstop. Pings are exempt.

These drive `drain` directly with the pane/board mocked. Runnable standalone
(`python3 test_attrib_delivery.py`) or under pytest.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keeper_relay as R   # noqa: E402

NOW = 1_700_000_000.0
MID = {"id": "t5", "type": "todo", "status": "doing", "text": "refactor the widget"}
NEW = {"id": "t9", "type": "todo", "status": "open", "text": "new request please"}


def _board_notice(ts=NOW, ids=("t9",)):
    """A queue entry shaped like todo_watch's new-item [board] notice."""
    line = "[board] ✎ t9 new request please — check ~/todo.json"
    return {"ts": ts, "author": "board", "payload": line, "sha256": R.digest(line),
            "prefix": line[:40], "ids": list(ids)}


class _Env:
    """Patch the pane/board/side-effect seams; capture audits. `board` and
    `pane_lines` are mutable so a test can change keeper state between drains."""
    def __init__(self, board, pane_lines=None, pane_idle=True, agent=None):
        self.board = board                 # None -> read_todo returns an error
        self.pane_lines = pane_lines or ["❯ "]
        self.pane_idle = pane_idle
        self.agent = agent                 # None -> leave real _agent_judge_release
        self.audits = []
        self._saved = {}

    def __enter__(self):
        def patch(name, fn):
            self._saved[name] = getattr(R, name)
            setattr(R, name, fn)
        patch("save_state", lambda *a, **k: None)
        patch("audit", lambda rec: self.audits.append(rec))
        patch("log", lambda *a, **k: None)
        patch("pane_state", lambda cfg: ({"lines": list(self.pane_lines)}, None))
        patch("idle_reason", lambda st: "" if self.pane_idle else "busy(turn-in-flight)")
        patch("read_todo", lambda path:
              (list(self.board), None) if self.board is not None else (None, "absent"))
        if self.agent is not None:
            patch("_agent_judge_release", self.agent)
        self._saved["_time"] = R.time.time
        R.time.time = lambda: NOW
        return self

    def __exit__(self, *exc):
        for name, val in self._saved.items():
            if name == "_time":
                R.time.time = val
            else:
                setattr(R, name, val)
        return False


def _cfg(**kw):
    c = {"board_events": True, "live": False, "board_max_hold_sec": 0,
         "todo_path": "/mock/todo.json", "target": "pane"}
    c.update(kw)
    return c


def _state(queue=None, **kw):
    s = {"queue": queue if queue is not None else [_board_notice()], "events": {"seen": []}}
    s.update(kw)
    return s


def _delivered(audits):
    return [a for a in audits if a.get("decision") == "dry-run"]


def _held(audits):
    return [a for a in audits if a.get("decision") == "hold"]


# ── tests ─────────────────────────────────────────────────────────────────────
def test_held_while_a_task_is_in_flight():
    cfg, state = _cfg(), _state()
    with _Env([MID, NEW]) as env:                 # t5 is `doing` -> mid-task
        R.drain(cfg, state)
    assert len(state["queue"]) == 1               # not delivered
    holds = _held(env.audits)
    assert holds and holds[0]["reason"].startswith("attrib-hold")
    assert "agent-off" in holds[0]["reason"]      # agent not enabled -> stays held


def test_released_when_between_tasks():
    cfg, state = _cfg(), _state()
    with _Env([NEW]) as env:                       # nothing `doing`
        R.drain(cfg, state)
    assert state["queue"] == []                    # delivered on the idle boundary
    dl = _delivered(env.audits)
    assert len(dl) == 1 and dl[0]["prefix"].startswith("[board]")


def test_printed_status_line_releases_even_mid_task():
    cfg, state = _cfg(), _state()
    lines = ["... working ...", "status=done  (t5 refactor complete)", "❯ "]
    with _Env([MID, NEW], pane_lines=lines) as env:
        R.drain(cfg, state)
    assert state["queue"] == []
    assert len(_delivered(env.audits)) == 1


def test_agent_release_verdict_delivers():
    cfg, state = _cfg(), _state()
    calls = {"n": 0}

    def agent(cfg_, inflight, st):
        calls["n"] += 1
        return True, "verdict:RELEASE"
    with _Env([MID, NEW], agent=agent):
        os.environ["KEEPER_RELAY_ATTRIB_AGENT"] = "1"
        try:
            R.drain(cfg, state)
        finally:
            os.environ.pop("KEEPER_RELAY_ATTRIB_AGENT", None)
    assert state["queue"] == []                    # agent said RELEASE
    assert calls["n"] == 1


def test_agent_hold_verdict_holds_and_caches():
    cfg, state = _cfg(), _state()
    calls = {"n": 0}

    def agent(cfg_, inflight, st):
        calls["n"] += 1
        return False, "verdict:HOLD"
    with _Env([MID, NEW], agent=agent):
        R.drain(cfg, state)                        # 1st drain: consult agent -> HOLD
        R.drain(cfg, state)                        # 2nd drain: same reply -> use cache
    assert len(state["queue"]) == 1                # still held
    assert calls["n"] == 1, "agent must be consulted at most once per reply"
    assert state.get("attrib_judge", {}).get("release") is False


def test_fallback_timer_off_by_default_then_on():
    old = _board_notice(ts=NOW - 100_000)          # queued long ago
    # default: max-hold 0 -> never force, even an ancient item stays held mid-task
    cfg, state = _cfg(board_max_hold_sec=0), _state(queue=[old])
    with _Env([MID, NEW]):
        R.drain(cfg, state)
    assert len(state["queue"]) == 1                # no time escape by default
    # enable a cap -> the starved item force-delivers on the idle gate
    cfg2, state2 = _cfg(board_max_hold_sec=60), _state(queue=[_board_notice(ts=NOW - 100_000)])
    with _Env([MID, NEW]) as env:
        R.drain(cfg2, state2)
    assert state2["queue"] == []
    assert any(a.get("decision") == "board-max-hold" for a in env.audits)


def test_unreadable_board_holds_fail_closed():
    cfg, state = _cfg(), _state()
    with _Env(None) as env:                         # read_todo errors
        R.drain(cfg, state)
    assert len(state["queue"]) == 1
    assert any("board-unreadable" in a.get("reason", "") for a in _held(env.audits))


def test_ping_bypasses_gate_even_mid_task():
    ping = {"ts": NOW, "author": "board-event", "payload": "[ping] 📨 from ae: \"look\"",
            "sha256": R.digest("p"), "prefix": "[ping]", "ids": ["t1"], "exempt_quiet": True,
            "evs": [{"id": "t1", "kind": "ping", "from": "ae", "text": "look", "ref": ""}]}
    cfg, state = _cfg(), _state(queue=[ping])
    board = [MID, {"id": "t1", "type": "request", "status": "open", "note": "[ping] from ae"}]
    with _Env(board) as env:                        # t5 doing -> keeper mid-task
        R.drain(cfg, state)
    assert state["queue"] == []                     # ping delivered despite mid-task
    assert len(_delivered(env.audits)) == 1


def test_agent_wrapper_fail_closed_when_binary_missing():
    # the real _agent_judge_release must fail closed if hugpy-agent can't run
    def boom(*a, **k):
        raise OSError("no such binary")
    saved = R.subprocess.run
    os.environ["KEEPER_RELAY_ATTRIB_AGENT"] = "1"
    R.subprocess.run = boom
    try:
        rel, reason = R._agent_judge_release(_cfg(), [MID], {"lines": ["❯ "]})
    finally:
        R.subprocess.run = saved
        os.environ.pop("KEEPER_RELAY_ATTRIB_AGENT", None)
    assert rel is False and reason.startswith("unavailable")


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  %s" % fn.__name__)
    print("\n%d passed" % len(fns))


if __name__ == "__main__":
    _run_all()
