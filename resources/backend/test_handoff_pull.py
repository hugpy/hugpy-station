"""1.0.146 — the seat handoff is a TOOLSERVER row; /resume = pull.

Pins the receiving-agent contract (operator 2026-10-01):
  * the SessionStart hook pulls the open handoff from the toolserver with the
    seat's own session id (HANDOFF_ID pins a spawned seat's row), injects it
    ONCE per session, and says so explicitly when the pull fails or the
    toolserver predates handoff/pull (legacy list, "could NOT be marked consumed");
  * the tmux launch directive carries only the toolserver's one-paragraph
    pointer (files["handoff"] set by the station, never read from disk), the
    serve kinds never carry it, and no handoff file key exists any more.
Pure: no network, no process, tmp dirs only.
Run:  python3 -m pytest resources/backend/test_handoff_pull.py
"""
import importlib.util
import json
import os
import sys
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import directives as DV          # noqa: E402
import locus_exec as LX          # noqa: E402

_spec = importlib.util.spec_from_file_location("ledger_hook", HERE / "hooks" / "ledger_hook.py")
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

SID = "0f0f0f0f-1111-4222-8333-444444444444"


def _calls(answers):
    log = []

    def _call(path, body, timeout=8):
        log.append((path, dict(body)))
        a = answers.get(path)
        if isinstance(a, Exception):
            raise a
        return a
    return log, _call


def test_hook_pulls_consumes_and_injects_once(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "_HANDOFF_SEEN_DIR", str(tmp_path / "seen"))
    monkeypatch.delenv("HANDOFF_ID", raising=False)
    log, call = _calls({"handoff/pull": {"found": True, "id": "h29", "text": "# Session init prompt (handoff h29 — locus keeper)\nBODY"}})
    monkeypatch.setattr(hook, "_call", call)
    out = hook._handoff_block("keeper", SID, "startup")
    assert out.startswith("# Session init prompt (handoff h29") and "BODY" in out
    assert "h29 is now consumed by this session (0f0f0f0f)" in out and "handoff_pull id=h29" in out
    assert log == [("handoff/pull", {"session_id": SID, "consume": True, "locus": "keeper"})]
    # a serve per-turn resume never re-injects; compact/clear re-pull (idempotent server side)
    assert hook._handoff_block("keeper", SID, "resume") == ""
    assert len(log) == 1
    assert hook._handoff_block("keeper", SID, "compact") != ""
    assert len(log) == 2


def test_hook_pins_the_spawned_seats_row_by_env(monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "_HANDOFF_SEEN_DIR", str(tmp_path / "seen"))
    monkeypatch.setenv("HANDOFF_ID", "h30")
    log, call = _calls({"handoff/pull": {"found": False}})
    monkeypatch.setattr(hook, "_call", call)
    assert hook._handoff_block("keeper", SID, "startup") == ""
    assert log[0][1] == {"session_id": SID, "consume": True, "id": "h30"}


def test_hook_failures_are_explicit_never_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "_HANDOFF_SEEN_DIR", str(tmp_path / "seen"))
    monkeypatch.delenv("HANDOFF_ID", raising=False)
    log, call = _calls({"handoff/pull": RuntimeError("no toolserver configured or advertised on this host")})
    monkeypatch.setattr(hook, "_call", call)
    out = hook._handoff_block("keeper", SID, "startup")
    assert out.startswith("[handoff] pull FAILED") and "handoff_pull locus=keeper" in out
    log, call = _calls({"handoff/pull": urllib.error.HTTPError("u", 503, "down", {}, None)})
    monkeypatch.setattr(hook, "_call", call)
    assert "HTTP 503" in hook._handoff_block("keeper", SID, "startup")


def test_hook_legacy_toolserver_lists_and_says_not_consumed(monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "_HANDOFF_SEEN_DIR", str(tmp_path / "seen"))
    monkeypatch.delenv("HANDOFF_ID", raising=False)
    rows = [{"id": "h28", "locus": "keeper", "mode": "seat", "status": "spun", "created": 10, "brief": "OLD",
             "source_session": "ed209f61-x"},
            {"id": "h29", "locus": "keeper", "mode": "seat", "status": "spun", "created": 20, "brief": "NEWEST",
             "source_session": "1847e9b2-x"},
            {"id": "h7", "locus": "keeper", "mode": "pull", "status": "spun", "created": 30, "brief": "a pull"},
            {"id": "h27", "locus": "hugpy", "mode": "seat", "status": "spun", "created": 40, "brief": "other locus"},
            {"id": "h26", "locus": "keeper", "mode": "seat", "status": "done", "created": 50, "brief": "closed"}]
    log, call = _calls({"handoff/pull": urllib.error.HTTPError("u", 404, "nf", {}, None), "handoff/list": rows})
    monkeypatch.setattr(hook, "_call", call)
    out = hook._handoff_block("keeper", SID, "startup")
    assert "handoff h29 — locus keeper" in out and "NEWEST" in out and "OLD" not in out
    assert "could NOT be marked consumed" in out and "handoff_claim id=h29 status=done" in out
    assert "exchange_list locus=keeper session_id=1847e9b2-x" in out
    assert [p for p, _ in log] == ["handoff/pull", "handoff/list"]
    assert log[1][1] == {"limit": 50}          # no status filter: 'open' (0.0.41) and 'pending' both found


def _snap(**files):
    base = {"guidance": "G", "directive": None, "fs": None, "delegate": None,
            "b_model": json.dumps({"model": "m"}), "serve": json.dumps({"port": 9124}),
            "station": json.dumps({"port": 8898, "locus": "keeper"})}
    base.update(files)
    return {"state": "/srv/vm_mgr/hugpy-station", "home": "/srv/vm_mgr", "user": "vm_mgr", "host": "ae",
            "app": "/x", "remote": False, "locus": "keeper", "files": base}


def test_no_handoff_file_key_anywhere():
    assert "handoff" not in DV.LAYER_FILES and "handoff" not in LX.CFG_FILES
    assert "frontier-handoff.md" not in LX.read_cfg_script()


def test_launch_layer_is_the_pointer_only_on_tmux(tmp_path):
    layer = ("Handoff h29 for locus keeper is open (requested 2026-10-01 19:27 UTC by session 1847e9b2, 1795 chars). "
             "Your SessionStart hook injects it as '# Session init prompt'; if that text is not in your context, "
             "pull it now: handoff_pull locus=keeper id=h29 (this marks it consumed by your session).")
    s = _snap(handoff=layer)
    fx = DV.facts(s)
    cc = DV.compose("tmux", s, fx=fx, backend="claude-code", with_handoff=True)
    assert "# Session init prompt (handoff)\n" + layer in cc and cc.count("handoff_pull locus=keeper id=h29") == 1
    for kind in ("keeper", "chat", "worker"):
        assert "handoff_pull" not in DV.compose(kind, s, fx=fx)
    # read_state never reads a handoff file, even if a legacy one is on disk
    (tmp_path / "frontier-handoff.md").write_text("LEGACY-FILE", encoding="utf-8")
    assert "handoff" not in DV.read_state(tmp_path)["files"]
    assert "LEGACY-FILE" not in DV.compose("tmux", DV.read_state(tmp_path), backend="claude-code", with_handoff=True)
