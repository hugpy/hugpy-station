#!/usr/bin/env python3
"""Claude Code hook — the session LEDGER (docs/B-REDUCER-AND-SESSION-LEDGER.md,
Phase 1). One script, dispatched on hook_event_name from the stdin payload:

  SessionStart  (startup|resume|clear|compact) → print the locus's ACTIVE ledger
                so the (fresh / resumed / post-compaction) session starts with
                STATE, plus a two-line contract on keeping it current.
  PreCompact    → deterministic checkpoint: append an auto "World state" block
                (files/commands/turns since the ledger's last update, from the
                exchanges DB) and bump the ledger BEFORE compaction loses it.
  SessionEnd    → the same checkpoint, noted as session end.

Ledger tools: toolserver ledger/get|put (also reachable by the agent as MCP
tools ledger_get / ledger_put). Locus: $EXCHANGE_LOCUS (the console injects it)
else the unix user. Task: $LEDGER_TASK else the locus's most recently updated
active ledger.

Design notes (same contract as exchange_capture_hook.py):
  - best-effort and SILENT: never fail or slow a turn; exit 0 always;
  - SessionStart output is bounded (LEDGER_MAX_CHARS, default 12000);
  - PreCompact/SessionEnd do their network work in a forked child.
"""
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request

MAX_CHARS = int(os.environ.get("LEDGER_MAX_CHARS", "12000") or 12000)


def _discovered_toolserver():
    """The toolserver advertised on this host (abstract-toolserver discovery):
    the package when importable, else its CLI; '' when neither finds one."""
    try:
        from abstract_toolserver import discovery
        return ((discovery.find_endpoint() or {}).get("url") or "").rstrip("/")
    except ImportError:
        pass
    except Exception:
        return ""
    import shutil
    import subprocess
    for cli in (shutil.which("abstract-toolserver"),
                os.path.expanduser("~/.local/share/station-seats/venv/bin/abstract-toolserver")):
        if cli and os.path.exists(cli):
            try:
                r = subprocess.run([cli, "endpoint"], capture_output=True, text=True, timeout=10)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip().splitlines()[0].rstrip("/")
            except Exception:
                pass
            break
    return ""


def _url_tok():
    url = (os.environ.get("EXCHANGE_INGEST_URL") or os.environ.get("HUGPY_TOOLSERVER_URL")
           or os.environ.get("TOOLSERVER_URL") or "").rstrip("/")
    tok = os.environ.get("TOOLSERVER_TOKEN") or os.environ.get("TOOLSERVER_OPERATOR_TOKEN") or ""
    try:
        d = json.load(open(os.path.expanduser("~/.claude.json")))
        env = ((d.get("mcpServers") or {}).get("toolserver") or {}).get("env") or {}
        url = url or (env.get("TOOLSERVER_URL") or "").rstrip("/")
        tok = tok or env.get("TOOLSERVER_TOKEN", "")
    except Exception:
        pass
    if not tok:
        try:
            for line in open(os.path.expanduser("~/.config/hugpy/operator.env")):
                if "=" in line and not line.startswith("#") and "TOKEN" in line.split("=", 1)[0]:
                    tok = line.split("=", 1)[1].strip().strip('"'); break
        except Exception:
            pass
    # 2026-09-30: no hardcoded URL — configured, else the locally advertised toolserver
    return (url or _discovered_toolserver()), tok


def _call(path, body, timeout=8):
    url, tok = _url_tok()
    if not url:
        raise RuntimeError("no toolserver configured or advertised on this host")
    req = urllib.request.Request(url + "/" + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Operator-Token": tok})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        doc = json.loads(r.read().decode("utf-8"))
    if isinstance(doc, dict) and doc.get("error"):
        raise RuntimeError(doc["error"])
    return doc.get("result", doc) if isinstance(doc, dict) else doc


def _locus():
    return (os.environ.get("EXCHANGE_LOCUS") or os.environ.get("HUGPY_LOCUS") or getpass.getuser()).strip().lower()


def _ledger(locus):
    task = os.environ.get("LEDGER_TASK", "").strip()
    body = {"locus": locus}
    if task:
        body["task"] = task
    try:
        return _call("ledger/get", body)
    except Exception:
        return None


_SEEN_DIR = os.path.expanduser("~/.claude-console/ledger-seen")


def _seen_rev(sid):
    try:
        return int(open(os.path.join(_SEEN_DIR, sid)).read().strip() or 0)
    except Exception:
        return None


def _mark_seen(sid, rev):
    try:
        os.makedirs(_SEEN_DIR, mode=0o700, exist_ok=True)
        with open(os.path.join(_SEEN_DIR, sid), "w") as f:
            f.write(str(rev))
    except Exception:
        pass


_PENDING_DIR = os.path.expanduser("~/.claude-console/pending-seen")


def _pending_counts(locus):
    """(open reminders, open inbox pings) for this locus — COUNTS ONLY."""
    n_p = n_i = 0
    try:
        rows = _call("prompt/list", {"locus": locus, "status": "open", "limit": 50}, timeout=4)
        n_p = len(rows or []) if isinstance(rows, list) else 0
    except Exception:
        pass
    try:
        doc = _call("comms/inbox", {"to": locus}, timeout=4)
        n_i = len(doc.get("pings") or []) if isinstance(doc, dict) else 0
    except Exception:
        pass
    return n_p, n_i


def _pending_line(locus, sid):
    """Defect 2 (operator ruling 2026-09-16): reminders and inbox messages reach
    the operator through the console's /api/reminders panel, NEVER by pasting
    their bodies into the prompt input or the model's context. The model gets at
    most this ONE line, and only when the counts changed since this session last
    saw them; it pulls a body with prompt_get / comms_inbox if it needs one."""
    n_p, n_i = _pending_counts(locus)
    state = f"{n_p}:{n_i}"
    if sid:
        f = os.path.join(_PENDING_DIR, sid)
        try:
            if open(f).read().strip() == state:
                return ""
        except Exception:
            pass
        try:
            os.makedirs(_PENDING_DIR, mode=0o700, exist_ok=True)
            with open(f, "w") as fh:
                fh.write(state)
        except Exception:
            pass
    if not (n_p or n_i):
        return ""
    return (f"[pending] {n_p + n_i} pending reminders/inbox items "
            f"({n_p} reminder(s), {n_i} inbox) for locus '{locus}'; "
            "call prompt_list / comms_inbox to read them. Bodies are NOT injected here.")


_HANDOFF_SEEN_DIR = os.path.expanduser("~/.claude-console/handoff-seen")


def _handoff_pull(locus, sid):
    """1.0.146 — the /resume PULL (operator 2026-10-01: the toolserver houses the
    handoffs; no file). Pull the open handoff for this seat from the toolserver
    (HANDOFF_ID env = the exact row a spawned seat was launched for, else the
    newest open row on the locus) and mark it consumed by THIS session id. Text
    or '' — every failure mode says what it is, nothing is swallowed."""
    hid = os.environ.get("HANDOFF_ID", "").strip()
    body = {"session_id": sid, "consume": True}
    if hid:
        body["id"] = hid
    else:
        body["locus"] = locus
    try:
        doc = _call("handoff/pull", body, timeout=12)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return _handoff_pull_legacy(locus, hid)
        return (f"[handoff] pull FAILED (HTTP {e.code}) — a handoff may be waiting: run "
                f"handoff_pull locus={locus}" + (f" id={hid}" if hid else "") + " before working.")
    except Exception as e:                                   # noqa: BLE001
        return (f"[handoff] pull FAILED ({str(e)[:140]}) — a handoff may be waiting: run "
                f"handoff_pull locus={locus}" + (f" id={hid}" if hid else "") + " before working.")
    if not isinstance(doc, dict) or not doc.get("found"):
        return ""
    return (str(doc.get("text") or "").rstrip() + "\n"
            f"[handoff] {doc.get('id')} is now consumed by this session ({sid[:8] or '?'}); "
            f"re-pull any time: handoff_pull id={doc.get('id')}.")


def _handoff_pull_legacy(locus, hid):
    """A toolserver before 0.0.41 has no handoff/pull: read the newest open seat
    row via handoff/list and SAY that it could not be marked consumed."""
    try:
        rows = [r for r in (_call("handoff/list", {"limit": 50}, timeout=8) or [])
                if isinstance(r, dict) and r.get("mode", "seat") == "seat"
                and r.get("status") in ("pending", "open", "spun", "claimed")]
    except Exception as e:                                   # noqa: BLE001
        return f"[handoff] toolserver < 0.0.41 and handoff/list failed ({str(e)[:120]}) — run handoff_list yourself."
    rows = [r for r in rows if (hid and r.get("id") == hid) or (not hid and r.get("locus") == locus)]
    if not rows:
        return ""
    r = max(rows, key=lambda r: int(r.get("created") or 0))
    return (f"# Session init prompt (handoff {r.get('id')} — locus {r.get('locus')})\n"
            f"{str(r.get('brief') or '').rstrip()}\n"
            f"[handoff] toolserver < 0.0.41: this row could NOT be marked consumed (no handoff/pull). "
            f"When its work is finished: handoff_claim id={r.get('id')} status=done. "
            f"Previous seat: exchange_list locus={r.get('locus')} session_id={r.get('source_session') or '?'} full=true.")


def _handoff_block(locus, sid, src):
    """Inject the handoff ONCE per session (serve resumes fire SessionStart per
    turn): startup/clear/compact always pull (consume is idempotent for the same
    session); resume only until this session has seen one."""
    f = os.path.join(_HANDOFF_SEEN_DIR, sid) if sid else ""
    if src == "resume" and f and os.path.exists(f):
        return ""
    text = _handoff_pull(locus, sid)
    if f:
        try:
            os.makedirs(_HANDOFF_SEEN_DIR, mode=0o700, exist_ok=True)
            with open(f, "w") as fh:
                fh.write("1" if text else "0")
        except Exception:                                    # noqa: BLE001
            pass
    return text


def session_start(payload):
    locus = _locus()
    led = _ledger(locus)
    src = payload.get("source") or ""
    sid = payload.get("session_id") or ""
    handoff = _handoff_block(locus, sid, src)
    if handoff:
        print(handoff + "\n")
    # Token discipline: under serve every console turn is a fresh `claude -p
    # --resume` process, so SessionStart(source=resume) fires PER TURN. Inject
    # the ledger on resume only when its rev changed since this session last saw
    # it (state file per session); startup/clear/compact always inject.
    if src == "resume" and sid and ((led and _seen_rev(sid) == int(led.get("rev") or 0))
                                    or (not led and _seen_rev(sid) == 0)):
        # ledger unchanged: still worth ONE line if the pending counts moved.
        line = _pending_line(locus, sid)
        if line:
            print(line)
        return
    pending = _pending_line(locus, sid)
    if not led:
        if sid:
            _mark_seen(sid, 0)
        if pending:
            print(pending)
        print(f"[ledger] no active ledger for locus '{locus}'. When you start a task, create one: "
              f"ledger_put locus={locus} task=<slug> doc=<markdown with the headings from ledger_template>; "
              f"update it at every decision point (rulings, decisions+why, verified/unverified state, in-flight step).")
        return
    doc = led.get("doc") or ""
    if len(doc) > MAX_CHARS:
        doc = doc[:MAX_CHARS] + f"\n…[ledger truncated at {MAX_CHARS} chars; ledger_get locus={locus} task={led['task']} for the rest]"
    if sid:
        _mark_seen(sid, int(led.get("rev") or 0))
    why = {"compact": "context was just compacted", "resume": "session resumed (ledger changed since last seen)",
           "clear": "context cleared", "startup": "new session"}.get(src, src or "session start")
    print(f"[ledger] {why} — this is the ACTIVE ledger for locus '{locus}', task '{led['task']}' "
          f"(rev {led.get('rev')}, updated {time.strftime('%Y-%m-%d %H:%M', time.gmtime(int(led.get('updated') or 0)))} UTC). "
          f"Treat it as the state of record; keep it current with ledger_put (same locus/task, whole document) "
          f"at every decision point.\n")
    if pending:
        print(pending + "\n")
    print(doc)


def _checkpoint(payload, label):
    locus = _locus()
    led = _ledger(locus)
    if not led:
        return
    sid = payload.get("session_id") or ""
    since = int(led.get("updated") or 0)
    files, cmds, turns, n = set(), [], [], 0
    try:
        rows = _call("exchange/list", {"locus": locus, "since": since, "limit": 200, "full": False}, timeout=15)
        for r in rows or []:
            if sid and r.get("session_id") and r["session_id"] != sid:
                continue
            n += 1
            p = r.get("pointers") or {}
            if isinstance(p, str):
                try:
                    p = json.loads(p)
                except Exception:
                    p = {}
            files.update(p.get("files") or [])
            cmds.extend(p.get("commands") or [])
            if r.get("turn"):
                turns.append(r["turn"][:8])
    except Exception:
        pass
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime()) + " UTC"
    block = [f"\n### auto checkpoint ({label}, {stamp}, session {sid[:8] or '?'})",
             f"- turns since last ledger update: {n}" + (f" ({turns[0]}…{turns[-1]})" if turns else "")]
    if files:
        block.append("- files touched: " + ", ".join(sorted(files)[:25]) + (" …" if len(files) > 25 else ""))
    if cmds:
        block.append(f"- commands run: {len(cmds)} (last: {cmds[-1][:120]})")
    block.append("- VERIFY the items above before building on them; this block was written by the hook, not by the agent.")
    doc = (led.get("doc") or "").rstrip("\n")
    marker = "## In-flight step"
    add = "\n".join(block) + "\n"
    # replace an earlier auto block for THIS session (one per session, updated in place)
    head_tag = f"### auto checkpoint (" ; sid_tag = f"session {sid[:8] or '?'})"
    lines, out, skipping = doc.split("\n"), [], False
    for ln in lines:
        if ln.startswith(head_tag) and ln.rstrip().endswith(sid_tag):
            skipping = True; continue
        if skipping and (ln.startswith("## ") or ln.startswith("### ")):
            skipping = False
        if not skipping:
            out.append(ln)
    doc = "\n".join(out).rstrip("\n")
    if marker in doc:                       # keep the auto block just above In-flight step
        i = doc.index(marker)
        doc = doc[:i].rstrip("\n") + "\n" + add + "\n" + doc[i:]
    else:
        doc = doc + "\n" + add
    try:
        _call("ledger/put", {"locus": locus, "task": led["task"], "doc": doc, "by": "ledger-hook",
                             "note": f"auto checkpoint: {label}", "session_id": sid}, timeout=15)
    except Exception:
        pass


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    ev = payload.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
    try:
        if ev == "SessionStart":
            session_start(payload)
        elif ev == "SessionEnd" and os.environ.get("AC_SESSION_LABEL"):
            # a serve console turn ending is not the session ending (each turn is
            # its own `claude -p` process); the checkpoint would fire every turn.
            return 0
        elif ev in ("PreCompact", "SessionEnd"):
            try:
                if os.fork() > 0:
                    return 0
            except OSError:
                pass
            _checkpoint(payload, "pre-compact" if ev == "PreCompact" else "session end")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
