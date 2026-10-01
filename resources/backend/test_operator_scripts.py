"""1.0.144 — operator items' RUN blocks as pre-made files (operator
2026-10-01). Pure: a temp state dir, no network.
Run:  python3 -m pytest resources/backend/test_operator_scripts.py
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import operator_scripts as OPS  # noqa: E402

F = "```"
NOTE = "\n".join([
    "WHY: x", "WHO: root via sudo on ae", "GATE: none covers it",
    "DO:", F + "bash", "sudo systemctl stop a.service", F,
    "VERIFY:", F + "bash", "systemctl is-active a.service", F,
    "EXPECT: inactive",
    "RUN:", F + "bash", "#!/usr/bin/env bash", "set -euo pipefail", "# o5 [root] stop a",
    "cd /srv/x", "sudo systemctl stop a.service",
    'test "$(systemctl is-active a.service)" = inactive', F,
    "ROLLBACK:", F + "bash", "sudo systemctl start a.service", F,
    "ROLLBACK RUN:", F + "bash", "#!/usr/bin/env bash", "set -euo pipefail", "sudo systemctl start a.service", F,
    "[comment operator 2026-10-01 10:00] ok",
])


def item(**kw):
    it = {"id": "o5", "type": "operator", "text": "[root] stop a", "note": NOTE, "status": "open", "ts": 1790000000}
    it.update(kw)
    return it


def test_run_blocks_are_extracted_verbatim():
    b = OPS.run_blocks(NOTE)
    assert b["RUN"].startswith("#!/usr/bin/env bash\nset -euo pipefail\n# o5 [root] stop a\ncd /srv/x\n")
    assert b["ROLLBACK RUN"].endswith("sudo systemctl start a.service")
    assert OPS.run_blocks("WHY: x\nDO:\n```bash\nls\n```") == {}
    assert OPS.run_blocks("RUN:\n```bash\necho 1\n```\nRUN:\n```bash\necho 2\n```")["RUN"] == "echo 1"


def test_materialize_create_update_close_revive(tmp_path):
    st = tmp_path / "state"
    rep = OPS.sync(st, [item(), {"id": "t1", "type": "todo", "text": "x"},
                        {"id": "o6", "type": "operator", "text": "no run", "note": "WHY: y", "status": "open"}], "central:keeper")
    d = st / "operator-scripts"
    files = lambda: sorted(p.name for p in d.iterdir() if p.is_file())              # noqa: E731
    assert files() == ["o5.rollback.sh", "o5.sh"]                                      # o6 has no RUN → no file
    assert sorted(rep["written"]) == sorted([str(d / "o5.sh"), str(d / "o5.rollback.sh")])
    text = (d / "o5.sh").read_text()
    assert text.startswith("#!/usr/bin/env bash\nset -euo pipefail\n# hugpy Station operator script")
    assert text.count("#!/usr/bin/env bash") == 1 and text.count("set -euo pipefail") == 1   # header not doubled
    assert OPS.CAPTURE_BEGIN in text and text.index(OPS.CAPTURE_END) < text.index("cd /srv/x")
    assert "# item:    o5\n# title:   [root] stop a\n# updated: 2026-09-" in text and "# board:   central:keeper" in text
    assert text.endswith("cd /srv/x\nsudo systemctl stop a.service\ntest \"$(systemctl is-active a.service)\" = inactive\n")
    rb = (d / "o5.rollback.sh").read_text()
    assert "# item:    o5  (ROLLBACK)" in rb and rb.endswith("sudo systemctl start a.service\n")
    # permissions: 0755 on the files and the dir (world-readable: no secrets on a board)
    for p in (d, d / "o5.sh", d / "o5.rollback.sh"):
        assert stat.S_IMODE(p.stat().st_mode) == 0o755, p
    # idempotent
    assert OPS.sync(st, [item()], "central:keeper")["written"] == []
    # update: the block changed → rewritten
    rep = OPS.sync(st, [item(note=NOTE.replace("cd /srv/x", "cd /srv/y"))], "central:keeper")
    assert rep["written"] == [str(d / "o5.sh")] and "cd /srv/y" in (d / "o5.sh").read_text()
    # close: renamed to .done, never deleted
    rep = OPS.sync(st, [item(status="done")], "central:keeper")
    assert files() == ["o5.rollback.sh.done", "o5.sh.done"]
    assert len(rep["closed"]) == 2 and OPS.one_liners(st, "o5") == {}
    # reopen: revived
    rep = OPS.sync(st, [item()], "central:keeper")
    assert len(rep["revived"]) == 2 and (d / "o5.sh").is_file() and not (d / "o5.sh.done").exists()


def test_one_liner_annotation(tmp_path):
    st = tmp_path / "state"
    items = [item(), item(id="o6", note="WHY: y", text="no run"), item(id="o7", status="done"), {"id": "t1", "type": "todo", "text": "x"}]
    OPS.sync(st, items, "file:/srv/vm_mgr/.hugpy/state/todo.json")
    OPS.annotate(st, items)
    assert items[0]["one_liner"] == f"bash {st}/operator-scripts/o5.sh"
    assert items[0]["rollback_one_liner"] == f"bash {st}/operator-scripts/o5.rollback.sh"
    assert "one_liner" not in items[1] and "one_liner" not in items[2] and "one_liner" not in items[3]
    assert items[0]["note"] == NOTE                                     # stored text untouched
    # the one-liner runs from any cwd: the file is absolute and carries its own cd
    assert items[0]["one_liner"].split(" ", 1)[1].startswith("/")


def test_disk_error_fails_open(tmp_path):
    ro = tmp_path / "file-not-dir"
    ro.write_text("x")
    rep = OPS.sync(ro / "state", [item()], "x")       # cannot mkdir under a file
    assert rep["written"] == [] and OPS.one_liners(ro / "state", "o5") == {}


# ── result capture: the file wraps itself ────────────────────────────────────
def _materialize(tmp_path, body, rollback=False, status="open"):
    st = tmp_path / "state"
    it = item(note=NOTE.replace("cd /srv/x\nsudo systemctl stop a.service\ntest \"$(systemctl is-active a.service)\" = inactive", body))
    OPS.sync(st, [it], "central:keeper")
    return st, (st / "operator-scripts" / ("o5.rollback.sh" if rollback else "o5.sh"))


def _run(script, home, cwd=None):
    return subprocess.run(["bash", str(script)], cwd=str(cwd or home), capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": str(home)}, timeout=30)


def test_capture_records_output_sidecar_and_exit_code(tmp_path):
    st, script = _materialize(tmp_path, "cd /\necho hello\necho oops >&2\nexit 7")
    home = tmp_path / "home"; home.mkdir()
    r = _run(script, home)
    assert r.returncode == 7                                              # exit code preserved
    rd = st / "operator-scripts" / "results" / "o5"
    logs, sides = sorted(rd.glob("*.log")), sorted(rd.glob("*.json"))
    assert len(logs) == 1 and len(sides) == 1 and logs[0].stem == sides[0].stem
    log = logs[0].read_text()
    assert all(ln[:8].count(":") == 2 and ln[8] == " " for ln in log.rstrip().split("\n"))      # every line stamped
    assert " hello" in log and " oops" in log                                                   # stdout AND stderr
    meta = json.loads(sides[0].read_text())
    assert meta["item"] == "o5" and meta["kind"] == "run" and meta["exit"] == 7
    assert meta["script"] == str(script) and meta["log"] == str(logs[0]) and meta["cwd"] == str(home)
    assert meta["user"] and meta["host"] and meta["started"].endswith("Z") and meta["ended"] >= meta["started"]
    assert len(meta["sha256"]) == 16 and "exit 7" in r.stderr and str(logs[0]) in r.stderr


def test_capture_rollback_kind_and_success(tmp_path):
    st, script = _materialize(tmp_path, "echo rolled", rollback=False)
    st2 = st
    OPS.sync(st2, [item()], "central:keeper")
    rb = st2 / "operator-scripts" / "o5.rollback.sh"
    home = tmp_path / "home"; home.mkdir()
    r = _run(rb, home)
    # ROLLBACK RUN in NOTE calls systemctl: it fails here, but the capture still records it as kind=rollback
    sides = sorted((st2 / "operator-scripts" / "results" / "o5").glob("*.json"))
    meta = json.loads(sides[-1].read_text())
    assert meta["kind"] == "rollback" and meta["exit"] == r.returncode and r.returncode != 0
    # a zero exit is recorded as 0
    st3, ok = _materialize(tmp_path / "ok", "echo fine")
    r = _run(ok, home)
    sides = sorted((st3 / "operator-scripts" / "results" / "o5").glob("*.json"))
    assert r.returncode == 0 and json.loads(sides[-1].read_text())["exit"] == 0


def test_capture_falls_back_to_home_when_results_unwritable(tmp_path):
    st, script = _materialize(tmp_path, "echo fine")
    rd = st / "operator-scripts" / "results"
    os.chmod(rd, 0o555)
    home = tmp_path / "home"; home.mkdir()
    try:
        r = _run(script, home)
    finally:
        os.chmod(rd, 0o2775)
    assert r.returncode == 0 and "results dir not writable" in r.stderr
    fb = home / "operator-results" / "o5"
    assert len(list(fb.glob("*.json"))) == 1 and not list((rd / "o5").glob("*.json")) if (rd / "o5").exists() else True


def test_results_dir_is_group_writable_setgid(tmp_path):
    st = tmp_path / "state"
    OPS.sync(st, [item()], "x")
    assert stat.S_IMODE((st / "operator-scripts" / "results").stat().st_mode) == 0o2775


def test_watcher_attaches_once_flags_and_never_closes(tmp_path):
    st = tmp_path / "state"
    items = [item()]
    OPS.sync(st, items, "x")
    rd = st / "operator-scripts" / "results" / "o5"; rd.mkdir(parents=True)
    (rd / "20261001T100000Z.log").write_text("\n".join("10:00:0%d line%d" % (i, i) for i in range(8)) + "\n")
    (rd / "20261001T100000Z.json").write_text(json.dumps({"item": "o5", "kind": "run", "exit": 0, "started": "s",
                                                           "ended": "2026-10-01T10:00:07Z", "log": str(rd / "20261001T100000Z.log")}))
    (rd / ".20261001T100000Z.rc").write_text("0")            # the wrapper's scratch file is not a sidecar
    (rd / "broken.json").write_text("{not json")
    pend = OPS.pending_results(st, items)
    assert len(pend) == 1 and pend[0]["id"] == "o5" and pend[0]["result"]["exit"] == 0
    assert pend[0]["result"]["tail"] == ["10:00:03 line3", "10:00:04 line4", "10:00:05 line5", "10:00:06 line6", "10:00:07 line7"]
    c = pend[0]["comment"]
    assert c.startswith("[comment operator-script ") and "RESULT 2026-10-01T10:00:07Z exit 0 (completed) · " in c
    assert c.endswith("    10:00:07 line7") and "last 5 lines:" in c
    # not attached yet → the item shows no result; attached → result + flag, still open, text untouched
    OPS.annotate(st, items)
    assert "result" not in items[0]
    OPS.mark_attached(st, pend[0]["sidecar"], "o5")
    assert OPS.pending_results(st, items) == []                       # exactly once
    OPS.annotate(st, items)
    it = items[0]
    assert it["status"] == "open" and it["note"] == NOTE
    assert it["result"] == {"exit": 0, "path": str(rd / "20261001T100000Z.log"), "ts": "2026-10-01T10:00:07Z", "kind": "run",
                            "tail": pend[0]["result"]["tail"], "sidecar": str(rd / "20261001T100000Z.json")}
    assert it["flag"] == {"status": "completed", "reason": "exit 0", "detail": "script run completed · " + str(rd / "20261001T100000Z.log"),
                          "by": "operator-script", "ts": "2026-10-01T10:00:07Z"}
    # a later failing run becomes the latest; the rollback kind is named
    (rd / "20261001T110000Z.log").write_text("11:00:00 boom\n")
    (rd / "20261001T110000Z.json").write_text(json.dumps({"item": "o5", "kind": "rollback", "exit": 2, "ended": "2026-10-01T11:00:00Z",
                                                           "log": str(rd / "20261001T110000Z.log")}))
    p2 = OPS.pending_results(st, items)
    assert len(p2) == 1 and "exit 2 (FAILED · rollback)" in p2[0]["comment"]
    OPS.mark_attached(st, p2[0]["sidecar"], "o5")
    OPS.annotate(st, items)
    assert items[0]["flag"]["status"] == "fail" and items[0]["result"]["kind"] == "rollback" and items[0]["status"] == "open"
