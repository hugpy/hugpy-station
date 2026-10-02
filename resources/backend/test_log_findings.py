"""Deterministic log findings (the 🐞 bug scan without a model) — fixtures only.

Run:  python3 -m pytest resources/backend/test_log_findings.py
  or: python3 resources/backend/test_log_findings.py      (no pytest needed)
"""
import ast
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import log_findings as lf  # noqa: E402

NOW = 1_790_730_000.0
SERVER = Path(__file__).with_name("server.py")


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t))


def _j(t, ident, msg, host="ae"):
    return f"{_iso(t)} {host} {ident}[1234]: {msg}"


# ---- the pattern table ----------------------------------------------------------------
def test_pattern_table_kinds():
    cases = {
        "oom": "Out of memory: Killed process 4242 (python3) total-vm:1234kB",
        "crash_loop": "hugpy-station-web.service: Scheduled restart job, restart counter is at 118.",
        "rate_limit_429": 'HTTP Request: POST https://api.anthropic.com/v1/messages "HTTP/1.1 429 Too Many Requests"',
        "http_5xx": "events relay: events/stream HTTP 502 (retry in 2s)",
        "refused": "ping delivery: [Errno 111] Connection refused",
        "timeout": "todo/list: ReadTimeout after 15s",
        "traceback": "RuntimeError: PEFT adapter base is not in this store",
        "other_error": "ERROR - worker registration failed for ae-worker",
    }
    for kind, msg in cases.items():
        got = lf.classify(msg)
        assert got and got[0] == kind, (kind, msg, got)
    assert set(cases) == set(lf.KINDS)


def test_ignore_list_drops_noise():
    for msg in ("/opt/hugpy-station/resources/backend/server.py:6228: DeprecationWarning: Changing state",
                "[UFW BLOCK] IN=vpn0 OUT= SRC=192.168.7.60",
                'HTTP Request: GET http://192.168.1.100:9200/ops/activity "HTTP/1.1 200 OK"',
                "sync done: 0 errors, 12 rows", "job finished error=None"):
        assert lf.classify(msg) is None, msg


def test_bare_429_is_not_a_rate_limit_and_sweeps_are_ignored():
    """1.0.152: serve's rollover sweep #429 filed two HIGH rate_limit_429 findings. A bare
    429 needs HTTP status context; the sweep bookkeeping lines are ignored outright."""
    for msg in ("[rollover] sweep #429: mode=auto thr=12 candidates=3 evaluated=3 due=0 promotable=0 skipped=2",
                "[rollover] sweep #429 seen: 68c57bfb=12/idle429s 76a65777=4/idle9s",
                "job 429 finished in 3.1s", "listening on port 4290 and 429"):
        got = lf.classify(msg)
        assert not got or got[0] != "rate_limit_429", (msg, got)
    for msg in ('"HTTP/1.1 429 Too Many Requests"', "call failed: HTTP 429 Too Many Requests",
                "status=429 from upstream", "stream error: 429 rate limit exceeded",
                "anthropic.RateLimitError: overloaded", 'POST /v1/messages" 429 12'):
        got = lf.classify(msg)
        assert got and got[0] == "rate_limit_429", (msg, got)


def test_signature_strips_volatile_parts():
    a = lf.signature("2026-09-29 18:41:24,349 worker 3a1f9c2e-1111-2222-3333-444455556666 at "
                     "/srv/hugpy/x/y.py line 12 from 192.168.1.100:7002 after 97s")
    b = lf.signature("2026-09-30 01:02:03,000 worker 9b1f9c2e-aaaa-bbbb-cccc-ddddeeeeffff at "
                     "/opt/other/z.py line 480 from 10.0.0.1:80 after 99s")
    assert a == b
    assert "<uuid>" in a and "<path>" in a and "<ip>" in a and "N" in a


def test_fix_sentence_becomes_suggested_action():
    msg = ("RuntimeError: base model missing. FIX: acquire the base model 'unsloth/x' into the store. "
           "Then retry.")
    assert lf.suggested_action(msg) == "acquire the base model 'unsloth/x' into the store."
    assert lf.suggested_action("ERROR something broke") == ""


# ---- scan -----------------------------------------------------------------------------
def test_scan_groups_by_signature_and_counts():
    text = "\n".join([
        "--- journal (hugpy@ae, since 20 min ago) ---",
        _j(NOW - 300, "hugpy", "2026-09-29 18:41:24,349 - ERROR - call v1-aaaa1111bbbb2222 failed: HTTP 429 Too Many Requests"),
        _j(NOW - 200, "hugpy", "2026-09-29 18:43:24,349 - ERROR - call v1-cccc3333dddd4444 failed: HTTP 429 Too Many Requests"),
        _j(NOW - 100, "hugpy", "2026-09-29 18:44:24,349 - ERROR - call v1-eeee5555ffff6666 failed: HTTP 429 Too Many Requests"),
        _j(NOW - 90, "sshd", "Connection closed by 192.168.1.113 port 57992"),
    ])
    fs = lf.scan(text, now=NOW, locus="ae-hugpy")
    assert len(fs) == 1
    f = fs[0]
    assert f["kind"] == "rate_limit_429" and f["source"] == "hugpy" and f["count"] == 3
    assert f["first_seen"] == NOW - 300 and f["last_seen"] == NOW - 100
    assert len(f["sample_lines"]) == 3 and f["locus"] == "ae-hugpy"
    assert "v1-" not in f["signature"]


def test_scan_sections_ring_and_seat_panes():
    text = "\n".join([
        "--- station console backend log (warnings+) ---",
        "13:58:17 WARNING station.events events relay: events/stream HTTP 502 (retry in 2s)",
        "13:58:19 WARNING station.events events relay: events/stream HTTP 502 (retry in 4s)",
        "--- seat panes (last error-ish lines) ---",
        "keeper-codex: stream error: 429 rate limit exceeded | all good here",
    ])
    fs = {f["kind"]: f for f in lf.scan(text, now=NOW)}
    assert fs["http_5xx"]["source"] == "station:station.events" and fs["http_5xx"]["count"] == 2
    assert fs["rate_limit_429"]["source"] == "seat:keeper-codex"


def test_traceback_folds_into_exception_line():
    text = "\n".join([
        _j(NOW - 50, "run.sh", "Traceback (most recent call last):"),
        _j(NOW - 50, "run.sh", '  File "/opt/x/server.py", line 10, in main'),
        _j(NOW - 50, "run.sh", "KeyError: 'loci'"),
        _j(NOW - 40, "other", "Traceback (most recent call last):"),
    ])
    fs = lf.scan(text, now=NOW)
    tb = [f for f in fs if f["kind"] == "traceback"]
    assert len(tb) == 2
    by_src = {f["source"]: f for f in tb}
    assert by_src["run.sh"]["signature"].startswith("KeyError")
    assert by_src["other"]["signature"].startswith("Traceback")


def test_unit_failures_fold_per_unit_and_promote_to_crash_loop():
    lines = []
    for k in range(4):               # 4 failed runs, 3 systemd lines each
        t = NOW - 400 + 60 * k
        lines += [_j(t, "systemd", "foo.service: Main process exited, code=exited, status=1/FAILURE"),
                  _j(t, "systemd", "foo.service: Failed with result 'exit-code'."),
                  _j(t, "systemd", "Failed to start foo.service - Foo.")]
    lines += [_j(NOW - 30, "systemd", "bar.service: Failed with result 'exit-code'.")]
    fs = {f["source"]: f for f in lf.scan("\n".join(lines), now=NOW)}
    assert fs["foo.service"]["kind"] == "crash_loop" and fs["foo.service"]["count"] == 4
    assert fs["bar.service"]["kind"] == "other_error" and fs["bar.service"]["count"] == 1
    assert len(fs) == 2


def test_systemd_restart_counter_is_crash_loop():
    fs = lf.scan(_j(NOW, "systemd", "hugpy-station-web.service: Scheduled restart job, restart counter is at 120."),
                 now=NOW)
    assert fs[0]["kind"] == "crash_loop" and fs[0]["source"] == "hugpy-station-web.service"
    assert fs[0]["severity"] == "high"


# ---- merge / emission ---------------------------------------------------------------------
def _scan429(times):
    return lf.scan("\n".join(_j(t, "hugpy", "ERROR upstream 429 Too Many Requests") for t in times),
                   now=max(times), locus="x")[0]


def test_new_then_overlap_does_not_reemit():
    f1 = _scan429([NOW - 300, NOW - 200])
    row, why = lf.merge({}, f1, NOW)
    assert why == "new" and row["count"] == 2 and row["emitted_count"] == 2
    f2 = _scan429([NOW - 300, NOW - 200, NOW + 10])      # overlapping window: one fresh line
    row2, why2 = lf.merge(row, f2, NOW + 600)
    assert why2 is None and row2["count"] == 3


def test_count_jump_reemits():
    row, _ = lf.merge({}, _scan429([NOW - 10, NOW - 5]), NOW)
    row, why = lf.merge(row, _scan429([NOW + i for i in range(1, 6)]), NOW + 600)
    assert why == "jump" and row["count"] == 7 and row["emitted_count"] == 7


def test_returned_after_silence_reemits():
    row, _ = lf.merge({}, _scan429([NOW]), NOW)
    row, why = lf.merge(row, _scan429([NOW + lf.STALE_S + 60]), NOW + lf.STALE_S + 60)
    assert why == "returned"


def test_untimed_lines_do_not_inflate():
    text = "--- seat panes ---\ns1: ERROR boom"
    row, _ = lf.merge({}, lf.scan(text, now=NOW)[0], NOW)
    for k in range(5):
        row, why = lf.merge(row, lf.scan(text, now=NOW + 600 * k)[0], NOW + 600 * k)
        assert why is None
    assert row["count"] == 1


def test_fmt_finding_is_deterministic_text():
    f = _scan429([NOW])
    row, _ = lf.merge({}, f, NOW)
    out = lf.fmt_finding(row)
    assert out.startswith("[high] rate_limit_429 · x · hugpy ×1")
    assert "signature:" in out and "> " in out


# ---- server wiring -------------------------------------------------------------------------
def _server_fn(name):
    tree = ast.parse(SERVER.read_text())
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(name)


def test_bugscan_run_never_calls_a_model():
    src = ast.unparse(_server_fn("_bugscan_run"))
    assert "_b_answer" not in src and "run_in_executor" not in src and "Gateway" not in src
    assert "_logf.scan" in src and "_logf.merge" in src and "_b_findings_publish" in src
    assert "_BUGSCAN_PROMPT" not in SERVER.read_text()


def test_b_findings_ask_and_reply_are_lookups():
    tree = ast.parse(SERVER.read_text())
    want = ("_b_meta_line", "_b_findings_ask", "_b_findings_reply")
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in want)
            or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in (
                "_B_FINDINGS_ASK_RE", "_B_LOOKUP_MODEL", "B_FINDINGS_MAX_AGE") for t in n.targets))]
    ns = {"re": re, "time": time, "os": __import__("os"), "_logf": lf}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVER), "exec"), ns)
    for q in ("what's broken?", "any errors?", "are there crash loops", "any 429s today", "what is failing"):
        assert ns["_b_findings_ask"](q), q
    assert not ns["_b_findings_ask"]("summarize the notes for a handoff")
    row, _ = lf.merge({}, _scan429([NOW]), NOW)
    reply = ns["_b_findings_reply"]([row], time.monotonic())
    assert "rate_limit_429" in reply
    assert re.search(r"· tokens 0 · \d+\.\d{3}s · lookup\)$", reply)
    assert "No running problems" in ns["_b_findings_reply"]([], time.monotonic())
    ans = ast.unparse(_server_fn("_b_answer"))
    assert ans.index("_b_findings_ask") < ans.index("_b_answer_in_vm")   # before any model path


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
