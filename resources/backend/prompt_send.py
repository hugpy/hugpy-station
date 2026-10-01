"""✍ prompt component delivery (1.0.143) — the composer sends FOR REAL.

Operator 2026-10-01: the bottom ✍ prompt bar resolved to the outdated
file-pointer method (save prompt-inbox/<ts>/prompt.md, then try to TYPE a
"Handle the operator prompt at …" pointer into a PTY that does not exist when
the keeper surface is serve), so most prompts were never sent. Now the prompt
TEXT itself goes straight to the target session and every send reports an
explicit state:

  delivered    serve accepted it and started the turn / the seat took text+Enter
  queued       serve queued it behind a running turn / the seat was busy and
               its TUI holds it for the turn boundary
  unconfirmed  the seat got text+Enter but its input line still shows text
  pending      NOT delivered yet — the station RE-PENDED it to the locus's
               prompt-pending file (1.0.146, t4260) and retries it every
               tick until a serve session takes it; `reason` says why
  failed       NOT delivered and NOT re-pended — only a malformed / empty /
               oversized prompt or a seat whose input line needs the operator
               (`can_force`); `reason` says why; the UI keeps the text

Targets (choose_target): "serve" = the locus's abstract-claude serve console
session (POST /api/console/chat; attachments uploaded first through
/api/session/attach and listed as an "Attachments:" block, the same contract
serve's own composer uses); "seat" = the locus's Terminal (tmux) seat via
`tmux send-keys -l` + a SEPARATE Enter (an Ink TUI reads text+Enter in one
write as a paste and never submits). "auto" follows the locus's keeper
surface. Operator-initiated sends are direct, never gated (1.0.140 rule).

Pure helpers + I/O-injected orchestration only; server.py wires the HTTP and
tmux callables, tests drive stubs.
"""
from __future__ import annotations

import asyncio
import re
import time

MAX_TEXT = 200_000          # serve console_service.submit's cap
SEAT_CHUNK = 2000           # send-keys argv chunk (term_paste's size)
MAX_FILES = 20

_SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# input-line prompt glyphs: claude-code "❯" (older "> " inside a │ box), codex "›"
_PROMPT = re.compile(r"^[\s│|]*([❯›]|>)(\s|$)")


def choose_target(requested, surface):
    """'serve' | 'seat'. auto follows the locus's keeper surface ('serve'|'tmux')."""
    r = str(requested or "auto").strip().lower()
    if r in ("serve", "seat"):
        return r
    return "serve" if str(surface or "").lower() == "serve" else "seat"


OK_STATES = ("delivered", "queued", "unconfirmed", "pending")
# a serve failure the station can retry by itself (re-pended, never "failed")
REPEND_REASONS = ("serve unreachable", "serve not answering", "no abstract-claude serve",
                  "no serve session to address", "serve refused the prompt",
                  "serve relay unreachable", "serve relay HTTP")
# NOT re-pended: "is not a console session" (an explicitly selected session that is
# not on this serve — the composer's choice, not an outage), empty / oversized text,
# an attachment that could not be stored.


def result(state, target, reason="", **kw):
    out = {"ok": state in OK_STATES, "state": state, "target": target, "reason": reason}
    out.update(kw)
    return out


def repend_worthy(res):
    """A failed SERVE result the station should re-pend (the serve head could
    not be resolved / reached) rather than report as final. A bad prompt
    (empty, oversized, attachment not stored) stays final: retrying it changes
    nothing and the UI keeps the text."""
    if not isinstance(res, dict) or res.get("state") != "failed" or res.get("target") != "serve":
        return False
    reason = str(res.get("reason") or "")
    return any(reason.startswith(r) or r in reason for r in REPEND_REASONS)


def attachments_block(items):
    """'' or '\\n\\nAttachments:\\n- name (mime, size): /abs/path' (serve's attachments.describe shape)."""
    lines = []
    for it in items or []:
        if not isinstance(it, dict) or not it.get("path"):
            continue
        meta = ", ".join(x for x in (str(it.get("mime") or ""), _size(it.get("size"))) if x)
        lines.append("- %s%s: %s" % (it.get("name") or "file", " (" + meta + ")" if meta else "", it["path"]))
    return ("\n\nAttachments:\n" + "\n".join(lines)) if lines else ""


def _size(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "kB", "MB"):
        if n < 1024 or unit == "MB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n = n / 1024
    return ""


def split_data_url(data_url):
    """(mime, base64) of a data: URL; ('', '') when it is not one."""
    m = re.match(r"^data:([^;,]*)(?:;[^,]*)?;base64,(.*)$", str(data_url or ""), re.S)
    return (m.group(1), m.group(2)) if m else ("", "")


def clean_files(files):
    return [f for f in (files or []) if isinstance(f, dict) and f.get("dataUrl")][:MAX_FILES]


# ── serve ────────────────────────────────────────────────────────────────────
async def deliver_serve(call, text, files, session_id="", head=None, fallback_head=False):
    """call(method, path, body|None) -> (status, doc); raises on a dead upstream.
    head() -> (sid, why) resolves the keeper session head when no session is
    named — or, with fallback_head (target auto), when the named one is not a
    console session on THIS serve (the console's selection is per browser, not
    per locus). -> result dict (never raises)."""
    sid, why = str(session_id or "").strip(), "selected"
    try:
        async def _head(note=""):
            if head is None:
                return "", "no keeper head resolver"
            h, w = await head()
            return h, (note + str(w or "")) if h else str(w or "?")
        if not sid:
            sid, why = await _head()
            if not sid:
                return result("failed", "serve", "no serve session to address: " + why)
        st, q = await call("GET", "/api/console/queue?id=" + sid, None)
        if (st != 200 or not isinstance(q, dict)) and not sid.startswith("cs-") and why != "selected":
            # 1.0.144: the keeper head is a NATIVE claude id on a serve whose
            # keeper was started by the roster (a freshly provisioned locus):
            # adopt it as a console session — the same call serve's own console
            # makes — so the prompt rides /api/console/chat and the turn still
            # carries the keeper role's directive (role_of follows native ids).
            ast, ad = await call("POST", "/api/console/sessions", {"backend": "claude", "native_id": sid})
            if ast == 200 and isinstance(ad, dict) and ad.get("id"):
                sid, why = str(ad["id"]), why + " (native %s adopted)" % sid[:8]
                st, q = await call("GET", "/api/console/queue?id=" + sid, None)
        if (st != 200 or not isinstance(q, dict)) and fallback_head and why == "selected":
            bad = sid
            sid, why = await _head("selected %s unknown here → keeper head via " % bad)
            if not sid:
                return result("failed", "serve", "selected session %s is not on this serve and %s" % (bad, why))
            st, q = await call("GET", "/api/console/queue?id=" + sid, None)
        if st != 200 or not isinstance(q, dict):
            return result("failed", "serve", "session %s is not a console session on this serve (%s)"
                          % (sid, _err(st, q)), session_id=sid)
        sid = str(q.get("session_id") or sid)
        atts = []
        for f in clean_files(files):
            mime, b64 = split_data_url(f.get("dataUrl"))
            st, d = await call("POST", "/api/session/attach",
                               {"session_id": sid, "name": str(f.get("name") or "file"),
                                "mime": str(f.get("type") or mime or ""), "data_b64": b64})
            if st != 200 or not isinstance(d, dict) or not d.get("path"):
                return result("failed", "serve", "attachment %s not stored: %s"
                              % (f.get("name") or "file", _err(st, d)), session_id=sid)
            atts.append(d)
        prompt = (text or "") + attachments_block(atts)
        if not prompt.strip():
            return result("failed", "serve", "empty prompt", session_id=sid)
        if len(prompt) > MAX_TEXT:
            return result("failed", "serve", "prompt over %d chars" % MAX_TEXT, session_id=sid)
        st, r = await call("POST", "/api/console/chat", {"session_id": sid, "prompt": prompt})
        if st != 200 or not isinstance(r, dict) or not r.get("message_ids"):
            return result("failed", "serve", "serve refused the prompt: " + _err(st, r), session_id=sid)
        live = str(r.get("session_id") or sid)
        state = "queued" if r.get("queued") else "delivered"
        detail = ("queued behind the running turn" if state == "queued" else "turn started")
        if r.get("redirected_from"):
            detail += " (archived %s → live %s)" % (r["redirected_from"], live)
        return result(state, "serve", "", session_id=live, why=why, message_ids=list(r["message_ids"]),
                      cursor=r.get("cursor") or 0, attachments=[a.get("path") for a in atts],
                      detail=detail)
    except Exception as e:                               # noqa: BLE001 — upstream down: say so
        return result("failed", "serve", "serve unreachable: %s" % (str(e) or type(e).__name__),
                      session_id=sid)


def _err(st, doc):
    msg = (doc or {}).get("error") if isinstance(doc, dict) else ""
    return ("HTTP %s" % st) + ((" " + str(msg)[:200]) if msg else "")


def classify_status(ids, queue_doc, events):
    """Where our message_ids are now. queue_doc = GET /api/console/queue (None =
    session gone); events = GET /api/console/events 'events' since the send
    cursor. -> {state, reply?, error?, retry?}
    states: done | failed | held | running | queued | stalled | gone | sent"""
    ids = {str(i) for i in (ids or [])}
    running = False
    for ev in events or []:
        evids = {str(i) for i in (ev.get("message_ids") or [])}
        if not (ids & evids):
            continue
        if ev.get("type") == "done":
            if int(ev.get("rc") or 0) == 0:
                return {"state": "done", "reply": str(ev.get("result") or "")}
            return {"state": "held" if ev.get("held") else "failed", "error": str(ev.get("error") or ""),
                    "retry": bool(ev.get("held"))}
        if ev.get("type") in ("user", "status"):
            running = True
    if queue_doc is None:
        return {"state": "gone", "error": "the session no longer exists"}
    queued = {str(i.get("id")) for i in (queue_doc.get("items") or []) if isinstance(i, dict)}
    if ids & queued:
        hold = queue_doc.get("hold") if isinstance(queue_doc.get("hold"), dict) else None
        if queue_doc.get("paused"):
            if hold:                                 # serve-core 0.1.15: owned + expiring
                return {"state": "held", "retry": True, "hold": hold,
                        "error": "the queue is held by %s until %s — release sends it now"
                                 % (hold.get("by") or "operator",
                                    time.strftime("%H:%M", time.localtime(float(hold.get("until") or 0)))
                                    if hold.get("until") else "released")}
            return {"state": "held", "retry": True,
                    "error": "the session is held (a previous batch failed) — release sends it"}
        if not queue_doc.get("busy"):
            return {"state": "stalled", "retry": True,
                    "error": "queued but nothing is running it — release starts it"}
        return {"state": "queued"}
    return {"state": "running" if running else "sent"}


# ── tmux seat ────────────────────────────────────────────────────────────────
def input_line(pane):
    """(found, rest) — the LAST prompt line of a captured pane and its text."""
    found, rest = False, ""
    for ln in _SGR.sub("", pane or "").splitlines():
        m = _PROMPT.match(ln)
        if m:
            found = True
            rest = ln[m.end():].strip().strip("│|").strip()
    return found, rest


def seat_input_state(pane):
    """busy | empty | dirty | unknown — from the bottom of a captured seat pane."""
    low = _SGR.sub("", pane or "").lower()
    if "esc to interrupt" in low or "esc to cancel" in low:
        return "busy"
    found, rest = input_line(pane)
    if not found:
        return "unknown"
    return "dirty" if rest else "empty"


def seat_parts(text):
    """send-keys -l parts: chunks, bracketed-paste wrapped when multi-line (a
    raw \\n is Enter to a TUI and would submit early)."""
    chunks = [text[i:i + SEAT_CHUNK] for i in range(0, len(text), SEAT_CHUNK)]
    return (["\x1b[200~"] + chunks + ["\x1b[201~"]) if "\n" in text else chunks


async def deliver_seat(tmux, sess, text, force=False, sleep=asyncio.sleep, label=""):
    """tmux(*args) -> (rc, out, err) on the locus's console socket. Types the
    text into seat `sess`, then a SEPARATE Enter. -> result dict (never raises)."""
    where = label or sess
    try:
        if not sess:
            return result("failed", "seat", "no terminal seat for this backend")
        if not text.strip():
            return result("failed", "seat", "empty prompt", seat=where)
        if len(text) > MAX_TEXT:
            return result("failed", "seat", "prompt over %d chars" % MAX_TEXT, seat=where)
        rc, _o, err = await tmux("has-session", "-t", "=" + sess)
        if rc != 0:
            return result("failed", "seat", "no live seat %s (%s)" % (where, (err or "").strip()[:120] or "has-session rc=%d" % rc),
                          seat=where)
        tgt = "=" + sess + ":"
        rc, pane, err = await tmux("capture-pane", "-p", "-t", tgt, "-S", "-8")
        if rc != 0:
            return result("failed", "seat", "capture-pane rc=%d %s" % (rc, (err or "").strip()[:120]), seat=where)
        before = seat_input_state(pane)
        if before == "dirty" and not force:
            _f, rest = input_line(pane)
            return result("failed", "seat", "the seat's input line already holds text (%r) — clear it there, or send anyway"
                          % rest[:60], seat=where, input="dirty", can_force=True)
        for part in seat_parts(text):
            rc, _o, err = await tmux("send-keys", "-t", tgt, "-l", "--", part)
            if rc != 0:
                return result("failed", "seat", "send-keys rc=%d %s" % (rc, (err or "").strip()[:120]), seat=where)
        await sleep(0.5)
        rc, _o, err = await tmux("send-keys", "-t", tgt, "Enter")
        if rc != 0:
            return result("failed", "seat", "text typed but Enter failed (rc=%d %s) — press Enter in the seat"
                          % (rc, (err or "").strip()[:120]), seat=where)
        await sleep(0.8)
        rc, after, _e = await tmux("capture-pane", "-p", "-t", tgt, "-S", "-8")
        _found, rest_after = input_line(after if rc == 0 else "")
        if rc == 0 and rest_after:
            return result("unconfirmed", "seat", "text + Enter sent, but the input line still shows %r" % rest_after[:60],
                          seat=where, input=before)
        if before == "busy":
            return result("queued", "seat", "", seat=where, input=before,
                          detail="seat was busy — its TUI holds the prompt for the turn boundary")
        return result("delivered", "seat", "", seat=where, input=before,
                      detail="typed + Enter" + ("" if before == "empty" else " (input line not recognised)"))
    except Exception as e:                               # noqa: BLE001
        return result("failed", "seat", "seat send error: %s" % (str(e) or type(e).__name__), seat=where)
