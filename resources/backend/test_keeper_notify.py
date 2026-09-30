"""Keeper notifier + dispositions + token meter — fixtures only, no network.

Run:  python3 -m pytest resources/backend/test_keeper_notify.py
  or: python3 resources/backend/test_keeper_notify.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keeper_notify as kn  # noqa: E402

T0 = 1_790_700_000.0


def _f(key="f1", kind="rate_limit_429", source="7002_hugpy_api.service", locus="keeper", sev="high",
       count=3, last=T0, sig="upstream returned N Too Many Requests for model <id>", action=""):
    return {"hash": key, "kind": kind, "source": source, "locus": locus, "severity": sev, "count": count,
            "first_seen": T0 - 600, "last_seen": last, "signature": sig,
            "sample_lines": ["HTTP 429 Too Many Requests"], "suggested_action": action}


def _book(**cfg):
    return kn.NotifyBook(state_path=None, now=lambda: T0, **cfg)


class FakeSink:
    def __init__(self, fail=False):
        self.mails, self.boards, self.resolved, self.fail = [], [], [], fail

    async def mail(self, row, text, by="station"):
        if self.fail:
            raise ConnectionError("unreachable")
        self.mails.append((row["key"], text, by))

    async def board(self, row, text, note, by="station", type="todo", priority="high"):
        if self.fail:
            raise ConnectionError("unreachable")
        self.boards.append((row["key"], text, note, by, type))
        return "t%d" % len(self.boards)

    async def resolve(self, row, note):
        self.resolved.append((row["board_id"], note))


def _fan(ev, sink, now=T0):
    return asyncio.run(kn.fanout(ev, sink, fmt_mail=kn.fmt_mail, fmt_board=kn.fmt_board,
                                 fmt_resolve=kn.fmt_resolve, now=now, by="station-findings"))


# ── notifier fan-out ────────────────────────────────────────────────────────────────────
def test_new_medium_plus_finding_fans_out_to_mail_and_board_once():
    b, s = _book(), FakeSink()
    ev = b.step([(_f(), "new")], {}, T0)
    st = _fan(ev, s)
    assert st["mailed"] == 1 and st["posted"] == 1
    assert "journalctl" in s.boards[0][2] or "```bash" in s.boards[0][2]
    ev2 = b.step([(_f(count=4, last=T0 + 60), None)], {}, T0 + 60)     # known, no emission
    st2 = _fan(ev2, s, T0 + 60)
    assert st2["mailed"] == 0 and st2["posted"] == 0 and len(s.boards) == 1


def test_low_severity_is_strip_only():
    b, s = _book(), FakeSink()
    ev = b.step([(_f(sev="low", kind="other_error"), "new")], {}, T0)
    _fan(ev, s)
    assert not s.mails and not s.boards
    assert b.strip_rows(T0)[0]["notify"] is False


def test_jump_remails_after_cooldown_only():
    b, s = _book(), FakeSink()
    _fan(b.step([(_f(), "new")], {}, T0), s)
    _fan(b.step([(_f(count=9, last=T0 + 300), "jump")], {}, T0 + 300), s, T0 + 300)
    assert len(s.mails) == 1                                            # inside the 30 min cooldown
    _fan(b.step([(_f(count=9, last=T0 + 1900), None)], {}, T0 + 1900), s, T0 + 1900)
    assert len(s.mails) == 2                                            # pending jump delivered after cooldown


def test_quiet_for_an_hour_resolves_the_board_item():
    b, s = _book(), FakeSink()
    _fan(b.step([(_f(), "new")], {}, T0), s)
    ev = b.step([], {"f1": T0}, T0 + 3601)
    _fan(ev, s, T0 + 3601)
    assert s.resolved and s.resolved[0][0] == "t1" and not b.strip_rows(T0 + 3601)


def test_unreachable_keeper_marks_nothing_and_retries_next_scan():
    b = _book()
    ev = b.step([(_f(), "new")], {}, T0)
    st = _fan(ev, FakeSink(fail=True))
    assert st["errors"] == 2 and not b.rows["f1"].get("board_id") and b.rows["f1"]["pending_mail"]
    ok = FakeSink()
    _fan(b.step([], {}, T0 + 600), ok, T0 + 600)
    assert len(ok.mails) == 1 and len(ok.boards) == 1


def test_loop_detector_events_use_the_same_fanout():
    import loop_detector as ld
    det = ld.LoopDetector(now=lambda: T0)
    for k, n in enumerate((10, 11, 12, 13)):
        det.observe_units([{"unit": "x.service", "scope": "user", "nrestarts": n,
                            "active_state": "active", "sub_state": "running"}], T0 + 60 * k)
        ev = det.finish_cycle(T0 + 60 * k)
    s = FakeSink()
    asyncio.run(kn.fanout(ev, s, fmt_mail=ld.fmt_mail, fmt_board=ld.fmt_board, fmt_resolve=lambda r: "stopped",
                          now=T0, by="station-loops"))
    assert s.mails and s.boards and s.boards[0][1].startswith("[loop]")


def test_seat_pane_prose_is_strip_only_by_default():
    b, s = _book(), FakeSink()
    ev = b.step([(_f(source="seat:keeper-claude"), "new")], {}, T0)
    _fan(ev, s)
    assert not s.mails and not s.boards and not ev["propose"]
    b2 = kn.NotifyBook(now=lambda: T0, strip_only_prefixes=())
    assert b2.step([(_f(source="seat:keeper-claude"), "new")], {}, T0)["mail"]

# ── remote delivery ─────────────────────────────────────────────────────────────────────
class FakeToolserver:
    def __init__(self, down=False):
        self.calls, self.down = [], down

    async def __call__(self, path, body):
        if self.down:
            raise OSError("toolserver unreachable")
        self.calls.append((path, body))
        if path == "todo/add":
            return {"id": "t%d" % len(self.calls), "locus": body["locus"]}
        if path == "comms/ping":
            return {"id": "r%d" % len(self.calls), "to": body["to"]}
        return {}


def test_remote_station_delivers_to_the_keeper_tagged_with_its_locus():
    ts = FakeToolserver()
    sink = kn.RemoteSink(ts, write_mail=None, keeper_locus="keeper", origin="ae-hugpy", station="hugpy@ae")
    b = _book()
    ev = b.step([(_f(locus="ae-hugpy"), "new")], {}, T0)
    st = asyncio.run(kn.fanout(ev, sink, fmt_mail=kn.fmt_mail, fmt_board=kn.fmt_board,
                               fmt_resolve=kn.fmt_resolve, now=T0, by="station-findings"))
    assert st == {"mailed": 0, "posted": 1, "resolved": 0, "errors": 0}          # the [ping] is the first ✉
    pings = [b_ for p, b_ in ts.calls if p == "comms/ping"]
    assert b.rows["f1"]["board_id"] == "r1"
    assert len(pings) == 1 and not [p for p, _ in ts.calls if p == "todo/add"]
    assert pings[0]["to"] == "keeper" and pings[0]["kind"] == "request" and pings[0]["from_"] == "ae-hugpy"
    assert pings[0]["text"].startswith("[ae-hugpy] [finding]") and "```bash" in pings[0]["text"]
    assert b.rows["f1"]["board_id"] == "r1"
    upd = [b_ for p, b_ in ts.calls if p == "todo/update"][0]                   # action kept in the NOTE
    assert upd["id"] == "r1" and upd["note"].startswith("[ping] from ae-hugpy (ref ") and "```bash" in upd["note"]
    # a later count-doubling re-mail (after the cooldown) is a kind=message ping, not a new board item
    ev = b.step([(dict(_f(locus="ae-hugpy"), count=9, last_seen=T0 + 1900), "jump")], {}, T0 + 1900)
    asyncio.run(kn.fanout(ev, sink, fmt_mail=kn.fmt_mail, fmt_board=kn.fmt_board, fmt_resolve=kn.fmt_resolve,
                          now=T0 + 1900, by="station-findings"))
    assert ts.calls[-1][0] == "comms/ping" and ts.calls[-1][1]["kind"] == "message"
    # B's proposals stay plain board rows (type proposal, no ping)
    bid = asyncio.run(sink.board(b.rows["f1"], "[B proposal] x", "note", by="B@ae-hugpy", type="proposal"))
    assert ts.calls[-1][0] == "todo/add" and ts.calls[-1][1]["type"] == "proposal" and ts.calls[-1][1]["by"] == "B@ae-hugpy" and bid


def test_remote_unreachable_degrades_silently():
    sink = kn.RemoteSink(FakeToolserver(down=True), None, "keeper", "op")
    b = _book()
    st = asyncio.run(kn.fanout(b.step([(_f(locus="op"), "new")], {}, T0), sink, fmt_mail=kn.fmt_mail,
                               fmt_board=kn.fmt_board, fmt_resolve=kn.fmt_resolve, now=T0))
    assert st["errors"] == 2 and st["posted"] == 0          # ping and mail both failed; nothing marked


# ── dispositions ────────────────────────────────────────────────────────────────────────
def test_parse_disposition_close_comment():
    assert kn.parse_disposition("done.\n\ninert: expected during nightly backup") == ("inert", "expected during nightly backup")
    assert kn.parse_disposition("reject: wrong unit, the 429s come from central") == ("rejected", "wrong unit, the 429s come from central")
    assert kn.parse_disposition("looks good\naccept")[0] == "accepted"
    assert kn.parse_disposition("plain close") == (None, "")
    ours = kn.fmt_board({"key": "f", "sigkey": "s", "sample_lines": ["Rejected: upstream said no"],
                         "identity": "x", "count": 1})[1]
    assert kn.parse_disposition(ours) == (None, "")                      # a log sample never counts
    assert kn.parse_disposition(ours + "\ninert: benign retry") == ("inert", "benign retry")


def test_inert_suppresses_notifications_and_proposals_but_keeps_counting():
    b, s = _book(), FakeSink()
    _fan(b.step([(_f(), "new")], {}, T0), s)
    b.set_disposition("f1", "inert", "known upstream quota", by="keeper", now=T0 + 10)
    ev = b.step([(_f(count=5, last=T0 + 2000), "jump")], {}, T0 + 2000)
    _fan(ev, s, T0 + 2000)
    assert len(s.mails) == 1 and not ev["propose"]
    row = b.strip_rows(T0 + 2000)[0]
    assert row["inert"] and row["count"] == 5 and row["disposition_reason"] == "known upstream quota"


def _inert_then(b, f_after, t):
    b.step([(_f(), "new")], {}, T0)
    b.set_disposition("f1", "inert", "noise", now=T0 + 5)
    return b.step([(f_after, "jump")], {}, t)


def test_inert_reopens_on_new_source():
    b = _book()
    ev = _inert_then(b, _f(key="f2", source="7004_hugpy_toolserver.service", last=T0 + 60), T0 + 60)
    assert ev["reopened"] and "new source" in b.sig(b.rows["f2"]["sigkey"])["reopen_reason"]
    assert ev["mail"] and ev["propose"]
    assert "why now: re-opened from inert: new source" in kn.fmt_mail(ev["mail"][0], b.sig(ev["mail"][0]["sigkey"]))


def test_inert_reopens_on_new_locus():
    b = _book()
    _inert_then(b, _f(key="f3", locus="op", last=T0 + 60), T0 + 60)
    assert "new locus op" in b.sig(b.rows["f3"]["sigkey"])["reopen_reason"]


def test_inert_reopens_on_severity_class():
    b = _book()
    b.step([(_f(sev="medium"), "new")], {}, T0)
    b.set_disposition("f1", "inert", "noise", now=T0 + 5)
    ev = b.step([(_f(sev="high", count=4, last=T0 + 60), None)], {}, T0 + 60)
    assert ev["reopened"] and "severity rose to high" in b.sig(b.rows["f1"]["sigkey"])["reopen_reason"]


def test_inert_reopens_on_tenfold_rate_only():
    b = _book()
    b.step([(_f(count=3), "new")], {}, T0)
    b.step([(_f(count=6, last=T0 + 1800), None)], {}, T0 + 1800)                # ~6/h
    b.set_disposition("f1", "inert", "noise", now=T0 + 1800)
    ev = b.step([(_f(count=20, last=T0 + 3000), None)], {}, T0 + 3000)          # below 10x
    assert not ev["reopened"]
    ev = b.step([(_f(count=200, last=T0 + 3500), None)], {}, T0 + 3500)         # far above 10x
    assert ev["reopened"] and "rate" in b.sig(b.rows["f1"]["sigkey"])["reopen_reason"]


def test_accepted_blocks_proposals_until_recurrence_then_marks_did_not_hold():
    b = _book()
    b.step([(_f(), "new")], {}, T0)
    b.set_disposition("f1", "accepted", "fixed quota", now=T0 + 10, proposal_id="p7")
    ev = b.step([(_f(count=3, last=T0), None)], {}, T0 + 20)
    assert not ev["propose"]
    ev = b.step([(_f(count=5, last=T0 + 900), "jump")], {}, T0 + 900)
    s = b.sig(b.rows["f1"]["sigkey"])
    assert ev["propose"] and s["did_not_hold"] == "p7" and "did not hold" in s["reopen_reason"]


# ── token burn ──────────────────────────────────────────────────────────────────────────
def test_token_burn_threshold():
    with tempfile.TemporaryDirectory() as d:
        m = kn.TokenMeter(state_path=d + "/m.json", now=lambda: T0, budget=200_000)
        for i in range(10):                                   # 150k in 2 h -> 1.8M/day projected
            m.add("bugscan_review", 15_000, T0 - 7200 + i * 700)
        m.add("b_chat", 2_000, T0 - 3600)                     # 48k/day -> under budget
        fs = m.findings(T0, locus="keeper")
        assert [f["source"] for f in fs] == ["bugscan_review"]
        f = fs[0]
        assert f["kind"] == "token_burn" and f["severity"] == "high" and "tokens/day" in f["sample_lines"][0]
        assert "STATION_TOKEN_BUDGET_BUGSCAN_REVIEW" in f["suggested_action"]
        m2 = kn.TokenMeter(state_path=d + "/m.json", now=lambda: T0, budget=200_000)
        assert m2.findings(T0, "keeper")[0]["key"] == f["key"]     # persisted + stable dedup key


def test_token_burn_under_budget_is_quiet():
    m = kn.TokenMeter(now=lambda: T0, budget=200_000)
    m.add("b_propose_fix", 600, T0 - 100)
    assert m.findings(T0) == []


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
