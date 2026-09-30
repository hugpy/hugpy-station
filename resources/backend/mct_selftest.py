#!/usr/bin/env python3
"""MCT v2 self-test (stdlib only). Against a TEMP ledger it exercises the full
bidirectional pointer exchange through BOTH the Store API and the CLI front
doors (mct-pull / mct-push wrappers or the gateway subcommands):

  operator submit -> pointer line -> seat pull (scope + sha verified)
  seat push (response) -> pointer -> operator pull-response -> turn complete
  artifact push, idempotent re-push, wrong-scope / tampered-body rejection,
  reply-as-pointer acknowledgement collapsed by ingest, seat-minted turn,
  deterministic transcript choice (t272), a reply to a cancelled turn (t275),
  plain-text reply pointers (t249) and token-lean bounded details (t274).

Run:  python3 mct_selftest.py          (exit 0 = all assertions hold)
"""
import calendar
import hashlib
import json
import os
import time
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from mct_gateway import HANDLE, RESPONSE_IN_TEXT, NATIVE_SEATS, Store, pointer_line  # noqa: E402

BIN = HERE.parent / "bin"
GATEWAY = HERE / "mct_gateway.py"
checks = []


def ok(cond, what):
    checks.append((bool(cond), what))
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        raise AssertionError(what)


def cli(env, *args, stdin=b""):
    """Prefer the shipped wrappers (what the seat runs); fall back to the gateway."""
    name = args[0]
    wrapper = BIN / name
    argv = [str(wrapper)] + list(args[1:]) if wrapper.exists() else [sys.executable, str(GATEWAY), name.split("-")[1]] + list(args[1:])
    p = subprocess.run(argv, input=stdin, capture_output=True, env=env, timeout=30)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip(), p.stderr.decode("utf-8", "replace").strip()


def main():
    with tempfile.TemporaryDirectory(prefix="mct-selftest-") as tmp:
        home = Path(tmp) / "state"
        env = {**os.environ, "HUGPY_STATION_STATE": str(home), "MCT_SEAT": ""}
        env.pop("TMUX", None)
        store = Store(home / "mct" / "renderer")
        seat = "claude-code"
        print("1. operator -> seat (submit, dispatch, pull)")
        text = "Please summarise the deploy plan.\n\nInline text never reaches the seat: only a pointer does. ✓"
        attachment = bytes(range(256)) * 4
        import base64
        files = [{"name": "plan.bin", "dataUrl": "data:application/octet-stream;base64," + base64.b64encode(attachment).decode()}]
        turn = store.submit(seat, "selftest_request_0001", text, files)
        ok(turn["status"] == "queued", "submit -> queued turn " + turn["id"])
        ok(store.submit(seat, "selftest_request_0001", text, files)["id"] == turn["id"], "submit is idempotent on request_id")
        ok(HANDLE.fullmatch(turn["context"]) and turn["context"].split("/")[2] == Store.scope(seat), "context pointer carries the seat scope")
        store.adopt(seat, "fixture.jsonl", "epoch1")
        nxt = store.next_turn(seat)
        ok(nxt and nxt["id"] == turn["id"], "broker picks the queued turn")
        store.sent(turn["id"], True)
        line = pointer_line(nxt)
        ok("\n" not in line and text not in line and turn["context"] in line and "mct-push" in line, "pointer line is ONE line, carries no prompt text, names mct-push")
        rc, out, err = cli(env, "mct-pull", turn["context"])
        ok(rc == 0, "mct-pull resolves the context via the CLI (%s)" % (err or "ok"))
        manifest = json.loads(out)
        ok(manifest["schema"] == "mct.context/1" and manifest["operator_prompt"] == text, "manifest schema + inlined operator_prompt exact")
        ok(manifest["seat"] == seat and manifest["turn"] == turn["id"], "manifest names seat + turn for mct-push")
        rc, out, _ = cli(env, "mct-pull", manifest["attachments"][0]["object"])
        ok(rc == 0 and hashlib.sha256(attachment).hexdigest() == manifest["attachments"][0]["sha256"], "attachment pulled; sha256 matches manifest")
        bad = turn["context"].replace(Store.scope(seat), Store.scope("codex"))
        rc, _, err = cli(env, "mct-pull", bad)
        ok(rc != 0 and "outside" in err, "wrong-scope handle is refused")
        # the seat reads the pointer (transcript ingest): turn -> working
        raw = json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": line}]}}).encode() + b"\n"
        store.ingest(seat, "fixture.jsonl", 0, raw, [("user", line, {})], len(raw))
        with store.connect() as c:
            ok(c.execute("SELECT status FROM turns WHERE id=?", (turn["id"],)).fetchone()[0] == "working", "seat acknowledgement -> turn working")

        print("2. seat -> operator (mct-push response)")
        reply = "## Deploy plan\n\n1. stage\n2. gate\n3. restart \U0001f331\n"
        rc, ptr, err = cli(env, "mct-push", "--seat", seat, "--turn", turn["id"], "--name", "reply.md", stdin=reply.encode())
        ok(rc == 0 and HANDLE.fullmatch(ptr), "mct-push prints an mct:// pointer (%s)" % (err or ptr))
        ok(ptr.split("/")[2] == Store.scope(seat) and ptr.split("/")[3] == turn["id"], "response pointer bound to the seat scope + the current turn")
        rc, ptr2, _ = cli(env, "mct-push", "--seat", seat, "--turn", turn["context"], stdin=reply.encode())
        ok(rc == 0 and ptr2 == ptr, "re-push of the identical reply is idempotent (turn given as a handle)")
        rc, _, err = cli(env, "mct-push", "--seat", seat, "--turn", turn["id"], stdin=b"a different reply")
        ok(rc != 0 and "different immutable reply" in err, "a conflicting second reply is refused")
        rc, _, err = cli(env, "mct-push", "--seat", "codex", "--turn", turn["id"], stdin=b"x")
        ok(rc != 0, "another seat cannot publish into this turn")
        rc, out, _ = cli(env, "mct-pull", ptr)
        ok(rc == 0 and out == reply.strip(), "operator pull-response returns the exact reply")
        meta = store.describe(ptr)
        ok(meta["sha256"] == hashlib.sha256(reply.encode()).hexdigest() and meta["origin"] == "seat" and meta["kind"] == "response", "response object: sha256 + origin=seat + kind=response")
        with store.connect() as c:
            ok(c.execute("SELECT status FROM turns WHERE id=?", (turn["id"],)).fetchone()[0] == "complete", "turn marked complete on response push")
        evs = store.events(seat)["events"]
        resp = [e for e in evs if e["kind"] == "response"]
        ok(len(resp) == 1 and resp[0]["text"] == reply and resp[0]["detail"]["object"] == ptr, "ONE 'response' event with the reply text + pointer for the live view")
        # reply-as-pointer acknowledgement in the transcript is archived, not displayed
        ack = "MCT response " + ptr
        ok(RESPONSE_IN_TEXT.search(ack), "ack line matches the reply-as-pointer convention")
        raw2 = json.dumps({"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": ack}]}}).encode() + b"\n"
        store.ingest(seat, "fixture.jsonl", len(raw), raw2, [("assistant", ack, {}), ("complete", "", {})], len(raw) + len(raw2))
        evs = store.events(seat)["events"]
        ok(not any(e["kind"] == "assistant" for e in evs), "pointer acknowledgement is not rendered as a second reply")
        ok(sum(e["kind"] == "response" for e in evs) == 1, "still exactly one visible reply")
        with store.connect() as c:
            ok(c.execute("SELECT status FROM turns WHERE id=?", (turn["id"],)).fetchone()[0] == "complete", "transcript completion keeps the turn complete (not failed)")
            ok(c.execute("SELECT count(*) FROM records").fetchone()[0] == 2, "raw transcript records archived losslessly")
        log = store.turn_log(seat)
        types = [e["type"] for e in log[-1]["events"]]
        ok("prompt.written" in types and "response.written" in types and all("handle" in e for e in log[-1]["events"] if e["type"].endswith(".written")), "turn_log exposes pointer events for /api/mct/live")

        print("3. artifacts, tampering, seat-minted turn")
        rc, art, err = cli(env, "mct-push", "--seat", seat, "--turn", turn["id"], "--kind", "artifact", "--name", "trace.json", stdin=b'{"ok":true}')
        ok(rc == 0 and HANDLE.fullmatch(art) and store.describe(art)["mime"] == "application/json", "artifact push -> pointer, mime guessed from name")
        ok(cli(env, "mct-push", "--seat", seat, "--turn", turn["id"], "--kind", "artifact", "--name", "trace.json", stdin=b'{"ok":true}')[1] == art, "identical artifact re-push is idempotent")
        with store.connect() as c:
            c.execute("UPDATE objects SET body=? WHERE id=?", (b"tampered", art.rsplit("/", 1)[1]))
        rc, _, err = cli(env, "mct-pull", art)
        ok(rc != 0 and "integrity" in err, "tampered body fails the sha256 check on pull")
        rc, minted, err = cli(env, "mct-push", "--seat", "codex", stdin=b"unsolicited note from the seat")
        ok(rc == 0 and HANDLE.fullmatch(minted), "push without a turn mints a seat turn (%s)" % (err or "ok"))
        with store.connect() as c:
            row = c.execute("SELECT * FROM turns WHERE id=?", (minted.split("/")[3],)).fetchone()
            ok(row and row["request_id"].startswith("seat-") and row["status"] == "complete", "seat-minted turn is complete and labelled seat-*")
        rc, _, err = cli(env, "mct-push", "--turn", turn["id"], stdin=b"x")
        ok(rc != 0 and "--seat" in err, "outside a seat, --seat is required")
        rc, named, err = cli(env, "mct-push", "--seat", seat, "--turn", turn["id"], "--kind", "artifact", "--name", "../etc/passwd", stdin=b"x")
        ok(rc == 0 and store.describe(named)["name"] == "passwd", "object names are basenamed (no path traversal)")
        ok(all(NATIVE_SEATS[s] for s in ("codex", "claude-code")), "native seats table intact")

        print("4. harness-injected user-role content is never the operator")
        from mct_gateway import classify_user_text, normalize
        notice = "<task-notification>\n<task-id>a58cb1e30e9458b90</task-id>\n<status>completed</status>\n<summary>Agent \"Verify B\" finished</summary>\n<result>…</result>\n</task-notification>"
        k, label, d = classify_user_text(notice)
        ok(k == "harness" and d["source"] == "task-notification" and d["task_id"] == "a58cb1e30e9458b90" and label.startswith("subagent completed"), "task-notification -> harness (task_id + summary parsed)")
        ok(classify_user_text("<local-command-caveat>Caveat: …</local-command-caveat>")[0] == "harness", "local-command caveat -> harness")
        ok(classify_user_text("[Request interrupted by user]")[0] == "harness", "interrupt marker -> harness")
        ok(classify_user_text("fix the login\n<system-reminder>\nnoise\n</system-reminder>") == ("user", "fix the login\n", {}), "system-reminder appended to a real message is stripped; message stays user")
        ok(classify_user_text("MCT context " + turn["context"] + ". Read it.")[0] == "user", "operator pointer line stays user")
        items = normalize({"type": "user", "message": {"content": [{"type": "text", "text": notice}]}}, "claude-code")
        ok(items and items[0][0] == "harness", "normalize(claude-code) emits kind=harness")
        raw3 = json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": notice}]}}).encode() + b"\n"
        with store.connect() as c:
            turns_before = c.execute("SELECT count(*) FROM turns").fetchone()[0]
        store.ingest(seat, "fixture.jsonl", 10 ** 6, raw3, items, 10 ** 6 + len(raw3))
        with store.connect() as c:
            turns_after = c.execute("SELECT count(*) FROM turns").fetchone()[0]
            hv = c.execute("SELECT kind,text FROM events WHERE kind='harness' ORDER BY id DESC LIMIT 1").fetchone()
        ok(turns_after == turns_before and hv and hv["text"].startswith("subagent"), "ingest: harness event recorded, NO turn minted, not a user event")
        # pre-classification ledgers are repaired idempotently, only for transcript-imported turns
        with store.connect() as c:
            c.execute("INSERT INTO turns(id,seat,request_id,status,prompt,context,created,updated) VALUES ('f'*32,?, 'native-' || ('f'*32),'complete',?,'',1,1)", (seat, notice))
            c.execute("INSERT INTO events(seat,turn_id,kind,text,detail,created) VALUES (?,'f'*32,'user',?,'{}',1)", (seat, notice))
        fixed = store.reclassify_harness()
        ok(len(fixed) == 1 and store.reclassify_harness() == [], "reclassify_harness repairs the legacy event once (idempotent)")
        with store.connect() as c:
            ok(c.execute("SELECT request_id FROM turns WHERE id='f'*32").fetchone()[0].startswith("harness-"), "its native turn is relabelled harness-*")
            ok(c.execute("SELECT kind FROM events WHERE turn_id=? AND kind='user'", (turn["id"],)).fetchone() is not None, "the submit-minted operator prompt stays user")
        print("5. deterministic transcript choice, cancelled-turn replies, token-lean details")
        from mct_gateway import NativeAdapter, bound, HARNESS_EXCERPT, TOOL_EXCERPT

        # --- t272: one seat's project directory holds several transcripts ---
        project = Path(tmp) / "cfg" / "projects" / "-srv-x"
        project.mkdir(parents=True)
        for stem, age in (("aaaa1111", 3600), ("bbbb2222", 5), ("cccc3333", 30)):
            f = project / (stem + ".jsonl")
            f.write_text(json.dumps({"type": "mode"}) + "\n"
                         + json.dumps({"type": "user", "timestamp": "2026-09-16T09:00:00.000Z"}) + "\n")
            os.utime(f, (time.time() - age, time.time() - age))
        cands = sorted(project.glob("*.jsonl"))
        adapter = NativeAdapter("claude-code")
        ok(NativeAdapter._session_ids([b"claude", b"--session-id", b"bbbb2222"]) == ["bbbb2222"]
           and NativeAdapter._session_ids([b"claude", b"--resume=cccc3333"]) == ["cccc3333"],
           "probe reads --session-id / --resume=… off the pane's own cmdline")
        ok(adapter._choose(cands, [b"claude", b"--session-id", b"aaaa1111"], "1")[0].stem == "aaaa1111",
           "a session id named by the seat process wins over mtime")
        chosen, why = adapter._choose(cands, [b"claude"], "1")
        ok(chosen.stem == "bbbb2222" and "newest" in why,
           "no session id: the newest transcript still being appended to (%s)" % why)
        ok(adapter._choose(cands[:1], [b"claude"], "1")[1] == "the only transcript in the project",
           "the single-candidate fast path is kept")
        adapter.pinned = ("1", cands[2])
        ok(adapter._choose(cands, [b"claude"], "1")[1] == "pinned",
           "the chosen transcript is pinned, so the seat's read offset cannot flap")
        ok(adapter._choose(cands, [b"claude"], "2", str(cands[2]))[0] == cands[2],
           "the source already adopted for this seat is preferred over a newer stranger")
        ok(adapter._choose([], [b"claude"], "1") == (None, "no transcript yet"),
           "several candidates never raise; only an empty directory has no choice")
        ok(NativeAdapter._session_start(cands[0]) == calendar.timegm((2026, 9, 16, 9, 0, 0, 0, 0, 0)),
           "a session's start is read from its first timestamped record")

        # --- t275: a reply to a cancelled/interrupted turn is kept, not refused ---
        late = store.submit(seat, "selftest_cancelled_0001", "answer me even if I interrupt")
        store.cancel(seat, late["id"])
        with store.connect() as c:
            ok(c.execute("SELECT status FROM turns WHERE id=?", (late["id"],)).fetchone()[0] == "cancelled", "turn cancelled")
        rc, late_ptr, err = cli(env, "mct-push", "--seat", seat, "--turn", late["id"], stdin=b"late but complete answer")
        ok(rc == 0 and HANDLE.fullmatch(late_ptr), "a reply to a cancelled turn is still published (%s)" % (err or late_ptr))
        with store.connect() as c:
            state = c.execute("SELECT status FROM turns WHERE id=?", (late["id"],)).fetchone()[0]
            note = c.execute("SELECT text FROM events WHERE turn_id=? AND kind='status' ORDER BY id DESC LIMIT 1", (late["id"],)).fetchone()[0]
        ok(state == "complete" and "after interrupt" in note, "the interrupted turn completes with an audit note (%s)" % note)

        # --- t249: the operator never sees a raw mct:// handle or a JSON blob ---
        ok(store.describe(ptr)["mime"].startswith("text/") and store.pull(ptr) == reply.encode("utf-8"),
           "the published reply is text/* and pulls back byte-exact (rendered as plain text, not JSON)")
        mixed = "Done — the detail is in the file. MCT response " + late_ptr + " (sha verified)"
        raw4 = json.dumps({"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": mixed}]}}).encode() + b"\n"
        store.ingest(seat, "fixture.jsonl", 2 * 10 ** 6, raw4, [("assistant", mixed, {})], 2 * 10 ** 6 + len(raw4))
        leaked = [e for e in store.events(seat, limit=1000)["events"] if "mct://" in (e["text"] or "")]
        ok(not leaked, "no displayed event carries a raw mct:// pointer in its text")

        # --- t274: harness notices and tool dumps are bounded excerpts ---
        blob = "x" * 50000
        shown, trimmed = bound(blob, TOOL_EXCERPT)
        ok(trimmed == 50000 - TOOL_EXCERPT and len(shown) < TOOL_EXCERPT + 200 and "trimmed" in shown,
           "bound() excerpts an oversized body and says how much was trimmed")
        ok(bound("short", TOOL_EXCERPT) == ("short", 0) and bound({"a": 1}, TOOL_EXCERPT) == ({"a": 1}, 0),
           "a small body keeps its exact value AND type (wire schema unchanged)")
        items = normalize({"type": "assistant", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": blob}]}}, "claude-code")
        ok(items[0][0] == "activity" and set(items[0][2]) >= {"call_id", "output", "error"}
           and len(items[0][2]["output"]) < TOOL_EXCERPT + 200 and items[0][2]["trimmed"],
           "tool-result dumps are bounded; the detail's keys are unchanged")
        big = ("<task-notification>\n<task-id>abc</task-id>\n<status>completed</status>\n<summary>agent done</summary>\n"
               "<result>" + "y" * 40000 + "</result>\n</task-notification>")
        k, label, d = classify_user_text(big)
        ok(k == "harness" and d["summary"] == "agent done" and len(d["text"]) < HARNESS_EXCERPT + 200 and d["trimmed"] > 0,
           "a subagent's result blob never rides into the operator's view (summary kept, body excerpted)")
        rawn = json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": big}]}}).encode() + b"\n"
        store.ingest(seat, "fixture.jsonl", 3 * 10 ** 6, rawn, normalize(json.loads(rawn), "claude-code"), 3 * 10 ** 6 + len(rawn))
        with store.connect() as c:
            detail = json.loads(c.execute("SELECT detail FROM events WHERE kind='harness' ORDER BY id DESC LIMIT 1").fetchone()[0])
            archived = c.execute("SELECT length(body) FROM records WHERE source=? AND offset=?", ("fixture.jsonl", 3 * 10 ** 6)).fetchone()[0]
        ok(detail.get("record") == "fixture.jsonl:3000000" and archived > 40000,
           "the excerpt points at the record, which stays archived losslessly")
    print("\n%d/%d checks passed" % (sum(1 for c in checks if c[0]), len(checks)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print("\nSELF-TEST FAILED: " + str(exc))
        sys.exit(1)
