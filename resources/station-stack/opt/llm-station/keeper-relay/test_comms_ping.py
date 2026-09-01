"""
Relay tests for comms.ping (KEEPER-TASK-comms-ping §6, relay half).

Mocks the pane like the §9 tests: pane_state/idle_reason/read_todo are stubbed, so
these drive `_events_detect_board` (detection) and `drain` (delivery gate) directly
and assert the fix's contract:

  * a new `[ping]` item delivers on the INSTANTANEOUS idle gate — NOT quiet-gated:
    a keeper active-seconds-ago (quiet_remaining > 0) still gets the ping, while a
    plain board-event (a done-transition) queued alongside it is HELD;
  * dedup by `evt:<id>:ping` — a second detection pass with the item still present
    fires nothing;
  * exactly one line is injected;
  * a BUSY pane defers to the next idle boundary (10-min quiet hold does NOT apply);
  * the ping stays an `open` request on the board (durable backstop) — the relay
    never writes the board, and delivery re-read keeps an open ping live.

Runnable standalone (`python3 test_comms_ping.py`) or under pytest.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keeper_relay as R   # noqa: E402

NOW = 1_700_000_000.0
PING = {"id": "t1", "type": "request", "status": "open", "by": "ae-agent",
        "text": "the build is done, please look",
        "note": "[ping] from ae-agent (ref board-42)"}
DONE = {"id": "t2", "type": "todo", "status": "done", "text": "some finished task"}


# ── harness ───────────────────────────────────────────────────────────────────
class _Env:
    """Patches the relay's side-effecting seams and captures audits. Restored on
    exit so tests don't leak monkeypatches into each other."""
    def __init__(self, board_items, pane_idle=True):
        self.board_items = board_items
        self.pane_idle = pane_idle
        self.audits = []
        self._saved = {}

    def __enter__(self):
        def patch(name, fn):
            self._saved[name] = getattr(R, name)
            setattr(R, name, fn)
        patch("save_state", lambda *a, **k: None)
        patch("audit", lambda rec: self.audits.append(rec))
        patch("log", lambda *a, **k: None)
        patch("pane_state", lambda cfg: ({}, None))
        patch("idle_reason", lambda st: "" if self.pane_idle else "pane-busy")
        patch("read_todo", lambda path: (list(self.board_items), None))
        # freeze time so quiet math is deterministic
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


def _cfg():
    return {"board_events": True, "live": False, "board_quiet_sec": 600,
            "board_max_hold_sec": 0, "todo_path": "/mock/todo.json",
            "events_marker_path": "/nonexistent/marker.json"}


def _state(**kw):
    # BUSY keeper: last activity is NOW, so quiet_remaining == 600 (> 0).
    s = {"queue": [], "events": {"seen": []}, "todo_fp": {"t2": {"status": "open"}},
         "last_activity_ts": NOW}
    s.update(kw)
    return s


def _authors(queue):
    return [q.get("author") for q in queue]


# ── tests ─────────────────────────────────────────────────────────────────────
def test_detection_enqueues_exempt_ping_and_gated_transition():
    cfg, state = _cfg(), _state()
    with _Env([PING, DONE]):
        R._events_detect_board(cfg, state, [PING, DONE])
    # two board-event notices: the ping (exempt) first, then the done (gated)
    assert len(state["queue"]) == 2, _authors(state["queue"])
    ping_q, done_q = state["queue"]
    assert ping_q["author"] == "board-event" and ping_q.get("exempt_quiet") is True
    assert ping_q["payload"].startswith("[ping]") and "from ae-agent" in ping_q["payload"]
    assert done_q["author"] == "board-event" and not done_q.get("exempt_quiet")
    assert done_q["payload"].startswith("[board]")
    # dedup ring recorded the ping under its stable event id
    assert "evt:t1:ping" in state["events"]["seen"]


def test_ping_delivers_past_quiet_gate_while_transition_is_held():
    cfg, state = _cfg(), _state()
    with _Env([PING, DONE]) as env:
        R._events_detect_board(cfg, state, [PING, DONE])
        assert R._board_quiet_remaining(state, cfg, NOW) > 0   # keeper is busy-recent
        R.drain(cfg, state)   # pane idle, but keeper active-seconds-ago
    # exactly the ping was delivered (dry-run pop); the gated done remains queued
    assert len(state["queue"]) == 1
    assert state["queue"][0]["payload"].startswith("[board]")   # the held done
    dry = [a for a in env.audits if a.get("decision") == "dry-run"]
    assert len(dry) == 1 and dry[0]["prefix"].startswith("[ping]")


def test_ping_dedups_no_refire():
    cfg, state = _cfg(), _state()
    with _Env([PING, DONE]):
        R._events_detect_board(cfg, state, [PING, DONE])   # enqueues ping + done
        R._events_detect_board(cfg, state, [PING, DONE])   # same items, still present
    pings = [q for q in state["queue"] if q.get("exempt_quiet")]
    assert len(pings) == 1, "ping must enqueue at most once (evt:t1:ping dedup)"


def test_busy_pane_defers_to_idle_boundary_not_quiet_hold():
    cfg, state = _cfg(), _state()
    with _Env([PING], pane_idle=False) as env:
        R._events_detect_board(cfg, state, [PING])
        R.drain(cfg, state)                       # pane BUSY -> hold, do not inject
        assert len(state["queue"]) == 1           # still queued
        assert any(a.get("decision") == "hold" and a.get("reason") == "pane-busy"
                   for a in env.audits)
        env.pane_idle = True                      # next idle boundary arrives
        R.drain(cfg, state)                       # keeper still active (quiet>0)
    assert len(state["queue"]) == 0               # delivered on the idle boundary
    dry = [a for a in env.audits if a.get("decision") == "dry-run"]
    assert len(dry) == 1 and dry[0]["prefix"].startswith("[ping]")


def test_ping_stays_open_on_board_durable_backstop():
    # The relay never writes the board; delivery re-read keeps an OPEN ping live.
    cfg, state = _cfg(), _state()
    board = [dict(PING)]
    with _Env(board):
        R._events_detect_board(cfg, state, [PING])
        R.drain(cfg, state)
    assert state["queue"] == []                   # pane took the nudge
    assert board[0]["status"] == "open"           # board item untouched -> backstop


def test_ping_self_written_is_suppressed():
    # A ping the keeper wrote itself (fresh todo-cli marker) is swallowed, not echoed.
    cfg, state = _cfg(), _state()
    saved = R._read_cli_markers
    with _Env([PING]):
        # a marker attributing t1's change to the keeper, within the suppress window
        R._read_cli_markers = lambda path: [{"ts": NOW - 1, "ids": ["t1"], "op": "add"}]
        try:
            R._events_detect_board(cfg, state, [PING])
        finally:
            R._read_cli_markers = saved
    assert all(not q.get("exempt_quiet") for q in state["queue"]), "self-ping must not enqueue"
    assert "evt:t1:ping" in state["events"]["seen"]   # marked seen so it never fires later


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  %s" % fn.__name__)
    print("\n%d passed" % len(fns))


if __name__ == "__main__":
    _run_all()
