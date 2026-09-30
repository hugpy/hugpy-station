"""Loop detector over fixture inputs — no network, no systemd, no station boot.

Run:  python3 -m pytest resources/backend/test_loop_detector.py
  or: python3 resources/backend/test_loop_detector.py      (no pytest needed)
"""
import ast
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loop_detector as ld  # noqa: E402

T0 = 1_790_700_000.0
PEFT_ERR = ("'Llama-3.2-3B-sentiment' failed to become ready on 'computron' after {n}s with no forward "
            "progress. The worker's last reported error is the cause: RuntimeError: PEFT adapter (base "
            "'unsloth/Llama-3.2-3B-Instruct') — the adapter is on disk but its base model is NOT in this "
            "store. FIX: acquire the base model 'unsloth/Llama-3.2-3B-Instruct' into the store. Then retry.")


def _det(**cfg):
    return ld.LoopDetector(now=lambda: T0, **cfg)


def _call(i, ts, prompt, model="Qwen3-Coder-Next-GGUF", ua="hugpy-health/1.0", status="done", error=None, system=None,
          tokens=0, start=None):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    row = {"id": "v1-%04d" % i, "ended_ts": ts, "model_key": model, "ua": ua, "client": "127.0.0.1",
           "status": status, "error": error, "request": {"messages": msgs}, "total_tokens": tokens}
    if start is not None:
        row["started_ts"] = start
    return row


BURN = 20_000        # per call: 3 calls in the hour -> ~1.4M tokens/day, far over the 200k budget


# ---- (a) central retrying a permanent error forever ---------------------------------
def test_job_retry_loop_flags_after_three_retries_and_carries_fix():
    d = _det()
    job = {"id": "j-peft", "status": "processing", "worker": "computron", "model_key": "Llama-3.2-3B-sentiment",
           "stage": "loading", "message": "⏳ retrying Llama-3.2-3B-sentiment on computron; worker load state is not confirmed",
           "attempt": 0, "progressed_at": T0}
    for k in range(3):        # the SAME retry message re-entered each cycle == a retry each time
        d.observe_jobs([dict(job, stage="loading" if k % 2 else "awaiting-capacity")], T0 + 60 * k)
        ev = d.finish_cycle(T0 + 60 * k)
    assert [r["source"] for r in ev["new"]] == ["job"]
    row = ev["new"][0]
    assert row["count"] == 3 and "j-peft" in row["identity"] and "computron" in row["identity"]
    assert "/llm/jobs/j-peft/cancel" in row["action"]
    assert d.snapshot()["active"][0]["key"] == row["key"]

    # the attempt field alone (central counts) also trips it, and the FIX sentence rides along
    d2 = _det()
    d2.observe_jobs([dict(job, attempt=4, error={"message": PEFT_ERR.format(n=99)})], T0)
    ev = d2.finish_cycle(T0)
    assert ev["new"] and ev["new"][0]["count"] == 4
    assert "FIX: acquire the base model 'unsloth/Llama-3.2-3B-Instruct'" in ev["new"][0]["action"]


def test_same_model_error_recurring_three_times_is_one_loop():
    d = _det()
    calls = [_call(i, T0 - 60 * i, "asdf", model="Llama-3.2-3B-sentiment", status="failed",
                   error=PEFT_ERR.format(n=97 + i)) for i in range(3)]
    d.observe_calls(calls, T0)
    ev = d.finish_cycle(T0)
    errs = [r for r in ev["new"] if r["source"] == "error"]
    assert len(errs) == 1 and errs[0]["count"] == 3      # 97s/98s/99s normalize to ONE error
    assert errs[0]["action"].startswith("FIX: acquire the base model")
    # two occurrences are not a loop
    d2 = _det()
    d2.observe_calls(calls[:2], T0)
    assert not d2.finish_cycle(T0)["new"]


# ---- (b) a health probe pinging Coder-Next on a timer --------------------------------
def test_identical_prompt_from_same_caller_three_times_in_window():
    d = _det()
    calls = [_call(i, T0 - 120 * i, "Reply with the single word: ok", tokens=BURN) for i in range(3)]
    d.observe_calls(calls, T0)
    ev = d.finish_cycle(T0)
    assert len(ev["new"]) == 1 and "repeated" in ev["new"][0]["detail"] and "stacked" not in ev["new"][0]["detail"]
    row = ev["new"][0]
    assert row["source"] == "calls" and row["count"] == 3
    assert "Reply with the single word: ok" in row["detail"] and "hugpy-health/1.0" in row["identity"]
    # the same rows on the next cycle are not counted twice
    d.observe_calls(calls, T0 + 60)
    ev = d.finish_cycle(T0 + 60)
    assert ev["updated"] and ev["updated"][0]["count"] == 3 and not ev["new"]
    # outside the 10 min window the loop clears
    d.observe_calls(calls, T0 + 700)
    ev = d.finish_cycle(T0 + 700)
    assert [r["key"] for r in ev["cleared"]] == [row["key"]]
    assert not d.snapshot()["active"] and d.snapshot()["recent"][0]["cleared_at"] == T0 + 700


def test_different_prompts_and_different_callers_do_not_trip():
    d = _det()
    calls = [_call(i, T0 - 60 * i, "Review these logs from the locus home %s" % i) for i in range(5)]
    calls += [_call(10 + i, T0 - 60 * i, "Reply with the single word: ok", ua="probe-%d" % i) for i in range(3)]
    d.observe_calls(calls, T0)
    assert not d.finish_cycle(T0)["new"]


def test_rows_without_a_prompt_fall_back_to_caller_model_count():
    d = _det(call_nohash_threshold=4)
    rows = [{"id": "c%d" % i, "ended_ts": T0 - i, "model_key": "M", "ua": "x", "total_tokens": BURN} for i in range(4)]
    d.observe_calls(rows, T0)
    ev = d.finish_cycle(T0)
    assert ev["new"] and "repeated calls" in ev["new"][0]["detail"] and ev["new"][0]["count"] == 4


# ---- (c) the reducer stacking JSON calls on one slot ---------------------------------
def test_queue_stacking_on_one_model_over_three_cycles():
    d = _det()
    q = {"active": [{"request_id": "r%d" % i, "model_key": "Qwen3-Coder-Next-GGUF", "state": "waiting"} for i in range(5)]}
    for k in range(2):
        d.observe_queue(q, T0 + 60 * k)
        assert not d.finish_cycle(T0 + 60 * k)["new"]       # a burst is not a loop yet
    d.observe_queue(q, T0 + 120)
    ev = d.finish_cycle(T0 + 120)
    assert ev["new"] and ev["new"][0]["source"] == "queue" and ev["new"][0]["count"] == 5
    assert "/llm/jobs/$id/cancel" in ev["new"][0]["action"] and "r0" in ev["new"][0]["action"]
    d.observe_queue({"active": []}, T0 + 180)
    assert d.finish_cycle(T0 + 180)["cleared"]


def test_same_system_prompt_stacked_from_one_caller():
    d = _det()
    sysp = "You maintain the ROLLING STATE of an engineering session. Return ONLY a JSON object"
    calls = [_call(i, T0 - 30 * i, "PREVIOUS STATE: {%d}" % i, ua="serve-reducer", system=sysp,
                   start=T0 - 400) for i in range(6)]                 # all six in flight together
    d.observe_calls(calls, T0)
    ev = d.finish_cycle(T0)
    assert any(r["source"] == "calls" and "stacked 6 calls" in r["detail"] for r in ev["new"])


def test_sequential_same_system_prompt_is_not_stacked_and_quiet_under_budget():
    """Tonight's false alarm: a sanctioned sequential eval run — one call at a time."""
    d = _det()
    sysp = "You tidy a to-do board. Reword ONE board item into one crisp line."
    calls = [_call(i, T0 - 60 * (i + 1), "item %d" % i, ua="eval-runner", system=sysp, tokens=300,
                   start=T0 - 60 * (i + 1) - 30) for i in range(8)]      # 30 s each, never overlapping
    d.observe_calls(calls, T0)
    assert not d.finish_cycle(T0)["new"]


def test_sequential_repetition_over_budget_is_repeated_not_stacked():
    d = _det()
    sysp = "You tidy a to-do board. Reword ONE board item into one crisp line."
    calls = [_call(i, T0 - 60 * (i + 1), "item %d" % i, ua="eval-runner", system=sysp, tokens=BURN,
                   start=T0 - 60 * (i + 1) - 30) for i in range(8)]
    d.observe_calls(calls, T0)
    new = d.finish_cycle(T0)["new"]
    assert new and all("repeated" in r["detail"] and "stacked" not in r["detail"] for r in new)
    assert "tokens/day" in new[0]["detail"]


def test_overlapping_calls_are_stacked():
    d = _det()
    calls = [_call(i, T0 - 5, "q %d" % i, ua="serve-reducer", system="SYS", start=T0 - 100 + i) for i in range(4)]
    d.observe_calls(calls, T0)
    new = d.finish_cycle(T0)["new"]
    assert new and "stacked 4 calls IN FLIGHT AT ONCE" in new[0]["detail"]


def test_declared_caller_is_budgeted_not_loop_alarmed():
    d = _det()
    over = [_call(i, T0 - 5, "q %d" % i, ua="tidy-eval", system="SYS", start=T0 - 100 + i, tokens=BURN) for i in range(6)]
    d.observe_calls(over, T0)
    new = d.finish_cycle(T0)["new"]
    assert [r["source"] for r in new] == ["token_burn"] and "declared caller tidy-eval" in new[0]["detail"]
    d2 = _det()
    d2.observe_calls([_call(i, T0 - 5, "q %d" % i, ua="tidy-eval", system="SYS", start=T0 - 100 + i, tokens=10)
                      for i in range(6)], T0)
    assert not d2.finish_cycle(T0)["new"]                       # stacked but declared and under budget: silent

def _run_steps(n, t_end, ua="hugpy-agent/serve", tokens=900, sysp="You are Hugpy Agent. Use tools."):
    """One agent run: step k resends the whole conversation so far + one more turn."""
    convo, rows = [{"role": "system", "content": sysp}, {"role": "user", "content": "fix the build"}], []
    t = t_end - n * 12
    for k in range(n):
        rows.append({"id": "run-%03d" % k, "started_ts": t, "ended_ts": t + 10, "model_key": "Qwen3-Coder-Next-GGUF",
                     "ua": ua, "status": "done", "total_tokens": tokens, "request": {"messages": list(convo)}})
        convo += [{"role": "assistant", "content": "tool call %d" % k}, {"role": "user", "content": "tool result %d" % k}]
        t += 12
    return rows


def test_growing_eight_step_agent_run_is_not_a_loop():
    """hugpy-agent serve run 93cdd6250593: 8 steps 19:31:30-19:33:08, same system prompt."""
    d = _det()
    d.observe_calls(_run_steps(8, T0), T0)
    assert not d.finish_cycle(T0)["new"]


def test_runaway_agent_run_over_step_cap_is_flagged():
    d = _det(run_step_cap=50, window_s=3600)
    d.observe_calls(_run_steps(60, T0, tokens=100), T0)
    new = d.finish_cycle(T0)["new"]
    assert new and "agent run" in new[0]["detail"] and "60 sequential steps" in new[0]["detail"]


def test_agent_run_over_token_budget_is_flagged():
    d = _det()
    d.observe_calls(_run_steps(8, T0, tokens=30_000), T0)
    new = d.finish_cycle(T0)["new"]
    assert new and "ran away" in new[0]["detail"] and "240,000 tokens" in new[0]["detail"]


def test_identical_non_growing_request_is_a_loop_even_under_budget():
    d = _det()
    d.observe_calls([_call(i, T0 - 60 * i, "Reply with the single word: ok", tokens=5) for i in range(3)], T0)
    new = d.finish_cycle(T0)["new"]
    assert new and "identical request" in new[0]["detail"] and "stacked" not in new[0]["detail"]

# ---- systemd -------------------------------------------------------------------------
def test_unit_restarts_growing_by_three_in_window_flags_crash_loop():
    d = _det()
    for k, n in enumerate((237, 237, 238, 239, 240)):
        d.observe_units([{"unit": "abstract-claude-serve@station.service", "scope": "user", "nrestarts": n,
                          "active_state": "active", "sub_state": "running", "result": "success"}], T0 + 60 * k)
        ev = d.finish_cycle(T0 + 60 * k)
    assert ev["new"] and ev["new"][0]["source"] == "systemd" and ev["new"][0]["severity"] == "crit"
    assert "systemctl --user stop abstract-claude-serve@station.service" in ev["new"][0]["action"]
    assert "NRestarts +3" in ev["new"][0]["detail"]


def test_peer_user_unit_restarts_growing_names_owner_and_keeps_own_unit_separate():
    """1.0.123: tonight's loop — hugpy's OWN hugpy-station-web.service (user unit of
    user hugpy) at 97→123 restarts, while this station's same-named unit is stable."""
    d = _det()
    for k, n in enumerate((97, 99, 101, 103)):
        d.observe_units([
            {"unit": "hugpy-station-web.service", "scope": "user", "owner": "hugpy", "uid": 1002,
             "nrestarts": n, "active_state": "active", "sub_state": "running", "result": "exit-code"},
            {"unit": "hugpy-station-web.service", "scope": "user", "nrestarts": 4,
             "active_state": "active", "sub_state": "running", "result": "success"},
        ], T0 + 60 * k)
        ev = d.finish_cycle(T0 + 60 * k)
        if ev["new"]:
            break
    assert len(ev["new"]) == 1, ev
    f = ev["new"][0]
    assert f["source"] == "systemd" and f["severity"] == "crit"
    assert "hugpy/hugpy-station-web.service" in json.dumps(f)
    assert "of user hugpy" in f["detail"]
    assert "journalctl --user -M hugpy@ -u hugpy-station-web.service -n 50" in f["action"]
    assert "sudo -n -u hugpy XDG_RUNTIME_DIR=/run/user/1002 journalctl --user" in f["action"]
    assert len(d.snapshot()["active"]) == 1          # the keeper's own unit stayed quiet

def test_unit_seeded_with_a_high_nrestarts_but_stable_is_quiet():
    d = _det()
    for k in range(4):
        d.observe_units([{"unit": "hugpy-station-web.service", "scope": "user", "nrestarts": 237,
                          "active_state": "inactive", "sub_state": "dead", "result": "exit-code"}], T0 + 60 * k)
        assert not d.finish_cycle(T0 + 60 * k)["new"]


def test_unit_in_auto_restart_twice_flags_and_system_scope_uses_sudo():
    d = _det()
    for k in range(2):
        d.observe_units([{"unit": "7002_hugpy_api.service", "scope": "system", "nrestarts": 1,
                          "active_state": "activating", "sub_state": "auto-restart", "result": "exit-code"}], T0 + 60 * k)
        ev = d.finish_cycle(T0 + 60 * k)
    assert ev["new"] and ev["new"][0]["action"].startswith("sudo journalctl -u 7002_hugpy_api.service")


# ---- workers -------------------------------------------------------------------------
def test_worker_reexec_twice_in_window_is_a_loop():
    d = _det()
    for k, boot in enumerate((T0 - 900, T0 - 900, T0 - 200, T0 - 200, T0 - 20)):
        d.observe_workers([{"name": "computron", "agent_boot_at": boot, "last_seen": T0 + 60 * k}], T0 + 60 * k)
        ev = d.finish_cycle(T0 + 60 * k)
    assert ev["new"] and ev["new"][0]["source"] == "worker" and ev["new"][0]["count"] == 2
    assert "ssh computron" in ev["new"][0]["action"]


# ---- mail cooldown / persistence / formatting ---------------------------------------
def test_mail_once_per_loop_with_cooldown_and_state_survives_restart():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "loops.json")
        d = ld.LoopDetector(state_path=path, now=lambda: T0)
        calls = [_call(i, T0 - 10 * i, "Reply with the single word: ok", tokens=BURN) for i in range(3)]
        d.observe_calls(calls, T0)
        ev = d.finish_cycle(T0)
        assert len(ev["mail"]) == 1
        d.mark_mailed(ev["mail"][0], T0)
        ev["mail"][0]["board_id"] = "t9999"
        d.save()
        d.observe_calls(calls, T0 + 120)
        assert d.finish_cycle(T0 + 120)["mail"] == []              # inside the 30 min cooldown
        # a fresh detector (station restarted) keeps first_seen, mailed_at, board id
        d2 = ld.LoopDetector(state_path=path, now=lambda: T0 + 200)
        row = d2.snapshot()["active"][0]
        assert row["board_id"] == "t9999" and row["mailed_at"] == T0 and row["first_seen"] == T0 - 20
        doc = json.loads(open(path).read())
        assert doc["loops"][0]["key"] == row["key"]
        # after the cooldown a still-active loop is mailed again
        d2.observe_calls([_call(50 + i, T0 + 1900 - 10 * i, "Reply with the single word: ok", tokens=BURN) for i in range(3)], T0 + 1900)
        assert len(d2.finish_cycle(T0 + 1900)["mail"]) == 1


def test_mail_and_board_text_carry_the_command():
    row = {"key": "calls:abc", "source": "calls", "identity": "hugpy-health>Coder-Next 1a2b", "count": 7,
           "first_seen": T0, "last_seen": T0 + 600, "detail": "identical prompt x7",
           "action": "find the timer and stop it"}
    m = ld.fmt_mail(row)
    assert m.startswith("⚠ loop detected [calls]") and "```bash\nfind the timer and stop it\n```" in m
    text, note = ld.fmt_board(row)
    assert text == "[loop] calls: hugpy-health>Coder-Next 1a2b ×7" and "```bash" in note and "calls:abc" in note


def test_norm_error_and_fix():
    a, b = ld.norm_error(PEFT_ERR.format(n=97)), ld.norm_error({"message": PEFT_ERR.format(n=99)})
    assert a == b and "N" in a
    assert ld.error_fix(PEFT_ERR.format(n=1)).startswith("acquire the base model")
    assert ld.error_fix("plain failure") == ""


# ---- server.py's systemctl block parser, extracted without booting the station ------
def test_server_parse_systemctl_show_blocks():
    src = Path(__file__).with_name("server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_loops_parse_show")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "server.py", "exec"), ns)
    out = ("Id=abstract-claude-serve@station.service\nNRestarts=3\nActiveState=activating\n"
           "SubState=auto-restart\nResult=exit-code\n\nId=hugpy-station-board.service\nNRestarts=0\n"
           "ActiveState=active\nSubState=running\nResult=success\n")
    rows = ns["_loops_parse_show"](out, "user")
    assert [r["unit"] for r in rows] == ["abstract-claude-serve@station.service", "hugpy-station-board.service"]
    assert rows[0]["nrestarts"] == "3" and rows[0]["sub_state"] == "auto-restart" and rows[1]["scope"] == "user"


if __name__ == "__main__":          # plain-python runner when pytest is absent
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
