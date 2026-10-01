"""B fix proposals — trigger gate, deterministic context, parse, novelty, cap.

Run:  python3 -m pytest resources/backend/test_b_propose.py
  or: python3 resources/backend/test_b_propose.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import b_propose as bp  # noqa: E402
import keeper_notify as kn  # noqa: E402

T0 = 1_790_700_000.0


def _f(key="f1", sev="medium", sig="bugscan scan failed: could not read journal for <path>", samples=None):
    return {"hash": key, "kind": "traceback", "source": "station.bugscan", "locus": "keeper", "severity": sev,
            "count": 3, "first_seen": T0 - 60, "last_seen": T0, "signature": sig,
            "sample_lines": samples or ["WARNING station.bugscan scan failed"]}


def _book():
    return kn.NotifyBook(now=lambda: T0)


def _gate(b, row, now=T0):
    return bp.gate(b.sig(row["sigkey"]), row, now, sev_rank=kn.sev_rank)


# ── trigger rules ───────────────────────────────────────────────────────────────────────
def test_trigger_new_signature_yes_count_bump_no():
    b = _book()
    ev = b.step([(_f(), "new")], {}, T0)
    assert ev["propose"] and _gate(b, ev["propose"][0]) is None
    ev["propose"][0]["propose_due"] = False                       # attempted
    ev = b.step([(dict(_f(), count=9, last_seen=T0 + 60), "jump")], {}, T0 + 60)
    assert not ev["propose"]


def test_trigger_recurrence_after_resolved_counts_as_new():
    b = _book()
    b.step([(_f(), "new")], {}, T0)["propose"][0]["propose_due"] = False
    b.step([], {"f1": T0}, T0 + 3601)                             # quiet an hour -> resolved
    ev = b.step([(dict(_f(), last_seen=T0 + 5000), "returned")], {}, T0 + 5000)
    assert ev["reopened"] and ev["propose"]


def test_low_severity_never_proposes():
    b = _book()
    b.step([(_f(sev="low"), "new")], {}, T0)
    assert _gate(b, b.rows["f1"]) == "severity below medium"


def test_weekly_cap():
    b = _book()
    b.step([(_f(), "new")], {}, T0)
    s = b.sig(b.rows["f1"]["sigkey"])
    for i in range(3):
        bp.record(s, {"summary": "s", "proposed_fix": "fix %d" % i}, "delivered", now=T0 - 86400 * i, bid="p%d" % i)
    assert _gate(b, b.rows["f1"]).startswith("weekly cap")
    s["proposals"][2]["at"] = T0 - 8 * 86400                      # one aged out of the week
    assert _gate(b, b.rows["f1"]) is None


def test_inert_and_accepted_block_proposals():
    b = _book()
    b.step([(_f(), "new")], {}, T0)
    b.set_disposition("f1", "inert", "noise", now=T0)
    b.rows["f1"]["propose_due"] = True
    assert _gate(b, b.rows["f1"]) == "inert"
    b.set_disposition("f1", "accepted", now=T0)
    b.rows["f1"]["propose_due"] = True
    assert _gate(b, b.rows["f1"]) == "accepted"


# ── context assembly ────────────────────────────────────────────────────────────────────
def test_context_from_a_traceback_frame():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "svc.py"
        src.write_text("\n".join("line %d" % i for i in range(1, 101)))
        f = _f(samples=['Traceback (most recent call last):', '  File "%s", line 60, in run' % src,
                        "KeyError: 'model'"])
        ctx = bp.locate_source(f, [d])
        assert ctx and ctx[0]["how"] == "traceback frame" and ctx[0]["line"] == 60
        assert "   60> line 60" in ctx[0]["snippet"] and "line 35" in ctx[0]["snippet"] and "line 86" not in ctx[0]["snippet"]


def test_context_from_grep_of_the_signature():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.py").write_text("x = 1\n")
        (Path(d) / "b.py").write_text("def f():\n    pass\n    log.warning('could not read journal for %s', p)\n")
        ctx = bp.locate_source(_f(), [d])
        assert ctx and ctx[0]["path"].endswith("b.py") and ctx[0]["line"] == 3 and ctx[0]["how"].startswith("grep")


def test_context_is_capped():
    msgs = bp.build_messages(dict(_f(), sample_lines=["x" * 1000] * 5),
                             [{"path": "p", "line": 1, "how": "grep", "snippet": "y" * 50_000}])
    assert len(msgs[1]["content"]) <= bp.CONTEXT_CHARS


def test_prompt_carries_prior_proposals_dispositions_and_did_not_hold():
    s = {"proposals": [{"id": "p1", "status": "delivered", "proposed_fix": "restart the unit",
                        "commands": ["systemctl --user restart x"], "disposition": "rejected",
                        "reason": "restarts do not fix quota"}],
         "disposition": "rejected", "reason": "restarts do not fix quota", "did_not_hold": "p0"}
    u = bp.build_messages(_f(), [], s)[1]["content"]
    assert "p1 [rejected: restarts do not fix quota]" in u and "did not hold" in u
    assert "materially different" in bp.build_messages(_f(), [], s)[0]["content"]


# ── parse / junk / novelty ──────────────────────────────────────────────────────────────
GOOD = ('{"summary": "journal read fails for peer user", "likely_cause": "no XDG_RUNTIME_DIR", '
        '"proposed_fix": "export XDG_RUNTIME_DIR before journalctl in _journal_argv", '
        '"commands": ["sudo -n -u hugpy XDG_RUNTIME_DIR=/run/user/116 journalctl --user -n 5"], '
        '"files": [{"path": "server.py", "line": 7540}], "confidence": "medium", "needs_privilege": false}')


def test_parse_valid_and_fenced():
    p = bp.parse("```json\n" + GOOD + "\n```")
    assert p["summary"].startswith("journal") and p["files"][0]["line"] == 7540 and p["confidence"] == "medium"


def test_parse_junk_is_none_and_never_blocks():
    assert bp.parse("I think you should restart it.") is None
    assert bp.parse('{"summary": ""}') is None
    assert bp.parse("") is None


def test_no_new_fix():
    assert bp.parse('{"no_new_fix": true, "why": "every cause was covered"}') == {"no_new_fix": True, "why": "every cause was covered"}


def test_duplicate_gate_drops_a_paraphrase_and_identical_commands():
    prior = [dict(bp.parse(GOOD), id="p1", status="delivered")]
    para = bp.parse(GOOD.replace("export XDG_RUNTIME_DIR before journalctl in _journal_argv",
                                 "in _journal_argv, export XDG_RUNTIME_DIR before the journalctl"))
    assert bp.novelty(para, prior) == "duplicate of p1"
    same_cmds = dict(bp.parse(GOOD), proposed_fix="switch the bug scan to ssh for peer loci entirely")
    assert bp.novelty(same_cmds, prior) == "duplicate of p1"
    new = dict(bp.parse(GOOD), proposed_fix="grant vm_mgr group systemd-journal read access instead",
               commands=["sudo usermod -aG systemd-journal vm_mgr"])
    assert bp.novelty(new, prior) is None


def test_delivery_shape():
    p = bp.parse(GOOD)
    text, note = bp.fmt_board(p, {"key": "f1", "sigkey": "s1", "signature": "sig"}, "ae-hugpy")
    assert text.startswith("[B proposal] ") and "```bash\nsudo -n -u hugpy" in note
    assert "- server.py:7540" in note and "origin ae-hugpy" in note and "reject: <reason>" in note
    # BOARD-ITEM-FORMAT.md proposal shape, disposition paragraph last
    for sec in ("PROBLEM: ", "OPTIONS:\nA) ", "\nB) ", "REC: ", "DECISION: pending", "SKETCH:\n```bash"):
        assert sec in note, sec
    assert note.index("SKETCH:") < note.rindex("close with a disposition line")
    s = {}
    e = bp.record(s, p, "delivered", now=T0, bid="p9")
    assert s["disposition"] == "proposed" and e["id"] == "p9" and e["disposition"] == "open"


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
