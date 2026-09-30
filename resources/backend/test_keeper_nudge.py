"""Keeper relay nudge — serve-first order, batching, closed-item drop, dedupe.

Run:  python3 -m pytest resources/backend/test_keeper_nudge.py
  or: python3 resources/backend/test_keeper_nudge.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keeper_nudge as kg  # noqa: E402

T0 = 1_790_700_000.0


def _p(i, text):
    return {"id": "r%d" % i, "text": text}


LOOP = "[%s] [loop] calls: tidy-eval>Qwen3-Coder-Next-GGUF sys 2e8f019eb1be ×%d"


def test_closed_items_are_dropped_at_send_time_and_not_counted():
    """04:17Z: it announced r1284, closed by the keeper at 04:05Z."""
    pend = [_p(1284, "[ping] build finished"), _p(1290, "[ping] loop on op")]
    pl = kg.plan(pend, open_ids={"r1290"}, now=T0, last_sent=0)
    assert pl["send"] and [p["id"] for p in pl["items"]] == ["r1290"] and pl["dropped"] == ["r1284"]
    line = kg.summarize(pl["items"], "keeper")
    assert line.startswith("📨 1 new board ping for keeper") and "r1284" not in line


def test_unknown_board_status_sends_nothing():
    assert not kg.plan([_p(1, "x")], open_ids=None, now=T0)["send"]


def test_all_closed_sends_nothing():
    pl = kg.plan([_p(1, "x")], open_ids=set(), now=T0)
    assert not pl["send"] and pl["dropped"] == ["r1"]


def test_one_nudge_per_five_minutes():
    pend = [_p(1, "a")]
    assert not kg.plan(pend, {"r1"}, now=T0 + 120, last_sent=T0)["send"]
    assert kg.plan(pend, {"r1"}, now=T0 + 120, last_sent=T0)["wait_s"] == 180
    assert kg.plan(pend, {"r1"}, now=T0 + 301, last_sent=T0)["send"]


def test_seven_station_pings_of_one_loop_are_one_distinct():
    locs = ["keeper", "ae-hugpy", "ae-hugpy-demo", "op", "hs-fresh", "ae-hugpy", "op"]
    pend = [_p(1270 + i, LOOP % (loc, 7 + 3 * i)) for i, loc in enumerate(locs)]
    line = kg.summarize(pend, "keeper")
    assert line.startswith("📨 7 new board pings for keeper (1 distinct) — latest r1276")
    assert len({kg.group_key(p) for p in pend}) == 1
    assert kg.group_key(_p(1, "[op] 🐞 finding [high] op · x.service: disk full")) != kg.group_key(pend[0])


def _run(serve_res, tmux_res):
    calls = []

    async def serve(line):
        calls.append("serve")
        if isinstance(serve_res, Exception):
            raise serve_res
        return serve_res

    async def tmux(line):
        calls.append("tmux")
        return tmux_res
    return asyncio.run(kg.deliver("📨 x", serve, tmux)), calls


def test_serve_ok_goes_to_serve_only():
    res, calls = _run((True, "serve:cs-1 accepted"), (True, "tmux:keeper-claude"))
    assert res == {"target": "serve", "detail": "serve:cs-1 accepted"} and calls == ["serve"]


def test_serve_down_falls_back_to_tmux():
    res, calls = _run(ConnectionRefusedError("127.0.0.1:9124"), (True, "tmux:keeper-claude"))
    assert res["target"] == "tmux" and calls == ["serve", "tmux"] and "9124" in res["detail"]


def test_serve_no_live_keeper_session_falls_back_to_tmux():
    res, calls = _run((False, "no live keeper session in serve"), (True, "tmux:keeper-claude"))
    assert res["target"] == "tmux" and "no live keeper session" in res["detail"]


def test_both_down_keeps_pending():
    res, calls = _run((False, "serve HTTP 502"), (False, "seat busy (keeper-claude)"))
    assert res["target"] == "none" and "HTTP 502" in res["detail"] and "seat busy" in res["detail"]


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   ", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL ", name, "->", repr(e))
    sys.exit(1 if failed else 0)
