"""B proposes fixes to the keeper (1.0.124) — pure logic, no I/O beyond reading
source files it is pointed at.

Operator 2026-09-29: "B should propose fixes to the keeper based on the logs it
receives" — and "it would need to differ to past propositions, rather than
constantly suggesting a fix to a thing that is already determined as inert".

  gate(sig, row, now)        -> None | reason-not-to-propose (inert, accepted, weekly cap, severity)
  locate_source(f, roots)    -> [{path, line, snippet, how}]  traceback frame, else grep — no model
  build_messages(f, ctx, sig) -> chat messages (every prior proposal + its disposition in context)
  parse(text)                -> proposal dict | {"no_new_fix": True, "why"} | None (junk)
  novelty(p, priors)         -> None | "duplicate of <id>"  (token Jaccard >= 0.7 on the fix, or same commands)
  fmt_board(p, f, origin)    -> (text, note) for the keeper-board type:proposal item
"""
from __future__ import annotations

import json
import os
import re
import time

MAX_PER_WEEK = 3
WEEK_S = 7 * 86400
CONTEXT_CHARS = 24_000          # ~6k tokens of source + finding
SNIPPET_RADIUS = 25
JACCARD_DUP = 0.7
MAX_TOKENS = 600
TASK = "b_propose_fix"

SYSTEM = (
    "You are B, the local keeper of a hugpy Station. A deterministic log scanner found a recurring "
    "problem. Using ONLY the finding and the source excerpts given, propose ONE concrete fix for the "
    "keeper to review. Answer with a single JSON object and nothing else:\n"
    '{"summary": str, "likely_cause": str, "proposed_fix": str, "commands": [str], '
    '"files": [{"path": str, "line": int}], "confidence": "low"|"medium"|"high", "needs_privilege": bool}\n'
    "Every prior proposal for this issue is listed with the keeper's disposition. Your fix MUST be "
    "materially different from every prior one (a different cause or a different change — not a "
    "rewording). If you cannot offer one, answer exactly {\"no_new_fix\": true, \"why\": str}. Never "
    "invent file paths or commands the excerpts do not support; say so in likely_cause instead."
)

_TB_RE = re.compile(r'File "([^"]+)", line (\d+)')
_PLACEHOLDER_RE = re.compile(r"<(?:uuid|ts|ip|path|id)>|\bN\b")
_SRC_EXT = (".py", ".sh", ".js", ".ts", ".mjs")
_SKIP_DIRS = {"node_modules", "__pycache__", ".git", "dist", "vendor", "site-packages", ".venv", "venv"}
_STOP = set("a an the to of in on for and or with from by is are be it this that as at run use then "
            "if not no set make ensure check".split())


def gate(sig, row, now=None, min_rank=1, sev_rank=None):
    """Why NOT to propose now (None = go). ``sig``: the NotifyBook sig record."""
    now = now or time.time()
    if not row.get("propose_due"):
        return "not due"
    rank = sev_rank(row.get("severity")) if sev_rank else 1
    if rank < min_rank:
        return "severity below medium"
    disp = (sig or {}).get("disposition") or "open"
    if disp == "inert":
        return "inert"
    if disp == "accepted":
        return "accepted"
    recent = [p for p in (sig or {}).get("proposals") or []          # every B call counts, bar gateway errors
              if p.get("status") != "error" and now - float(p.get("at") or 0) < WEEK_S]
    if len(recent) >= MAX_PER_WEEK:
        return "weekly cap (%d in 7 days)" % MAX_PER_WEEK
    return None


# ── deterministic context ──────────────────────────────────────────────────────────────
def _snippet(path, line, radius=SNIPPET_RADIUS):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    line = max(1, min(int(line), len(lines) or 1))
    lo, hi = max(1, line - radius), min(len(lines), line + radius)
    return "\n".join("%5d%s %s" % (i, ">" if i == line else " ", lines[i - 1]) for i in range(lo, hi + 1))


def _walk(roots, limit=6000):
    n = 0
    for root in roots or []:
        if not root:
            continue
        if os.path.isfile(root):
            n += 1
            yield root
            continue
        for d, subdirs, files in os.walk(root):
            subdirs[:] = [s for s in subdirs if s not in _SKIP_DIRS and not s.startswith(".")]
            for f in files:
                if f.startswith("test_"):
                    continue
                if f.endswith(_SRC_EXT) or "." not in f:
                    n += 1
                    if n > limit:
                        return
                    yield os.path.join(d, f)


def _chunks(signature):
    """Literal pieces of a normalised signature, longest first — what the
    source code most likely contains verbatim (a log/raise format string)."""
    out = set()
    for piece in _PLACEHOLDER_RE.split(signature or ""):
        for clause in re.split(r":\s|['\"]|\s[-—|]\s", piece):
            clause = clause.strip(" :;,.'\"()[]{}-=")
            if len(clause) >= 8:
                out.add(clause)
            words = clause.split()
            for n in (5, 4, 3):                      # word n-grams: a format string's literal stretch
                for i in range(0, max(0, len(words) - n + 1)):
                    g = " ".join(words[i:i + n])
                    if len(g) >= 12:
                        out.add(g)
    return sorted(out, key=len, reverse=True)


def locate_source(finding, roots, max_chars=CONTEXT_CHARS):
    """Implicated source WITHOUT a model: the deepest traceback frame that
    exists on disk, else a grep of the signature's literal chunks across the
    roots. Returns [{path, line, snippet, how}] within ``max_chars``."""
    out, used = [], 0
    text = "\n".join(list(finding.get("sample_lines") or []) + [finding.get("signature") or ""])
    frames = [(p, int(n)) for p, n in _TB_RE.findall(text)]
    for path, line in reversed(frames):
        if not os.path.isfile(path):
            base = os.path.basename(path)
            path = next((f for f in _walk(roots) if os.path.basename(f) == base), None)
            if not path:
                continue
        snip = _snippet(path, line)
        if snip and used + len(snip) <= max_chars:
            out.append({"path": path, "line": line, "snippet": snip, "how": "traceback frame"})
            used += len(snip)
        if len(out) >= 2:
            break
    if out:
        return out
    chunks = _chunks(finding.get("signature") or "")[:12]
    best = None                                   # (chunk rank, path, line) — one pass over the files
    for path in _walk(roots) if chunks else ():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = fh.read(2_000_000)
        except OSError:
            continue
        for rank, chunk in enumerate(chunks[: (best[0] if best else len(chunks))]):
            pos = body.find(chunk)
            if pos >= 0:
                best = (rank, path, body.count("\n", 0, pos) + 1, chunk)
                break
        if best and best[0] == 0:
            break
    if best:
        snip = _snippet(best[1], best[2])
        if snip and len(snip) <= max_chars:
            out.append({"path": best[1], "line": best[2], "snippet": snip, "how": "grep %r" % best[3][:60]})
    return out
    return out


# ── prompt ─────────────────────────────────────────────────────────────────────────────
def memory_block(mem):
    """1.0.140: B's history for THIS issue only — the toolserver's
    issue_memory(fp) (brief + its proposals with the keeper's dispositions).
    Nothing about any other issue is ever put in the prompt."""
    lines = ["", "ISSUE MEMORY (this issue only — fp %s)" % mem.get("fp")]
    brief = str(mem.get("brief") or "").strip()
    if brief:
        lines.append(brief[:2500])
    props = [p for p in mem.get("proposals") or [] if p.get("status") in ("delivered", "duplicate", None)]
    if props:
        lines.append("PRIOR PROPOSALS for this issue (yours must differ materially from every one):")
        for p in props:
            lines.append("- %s [%s%s] fix: %s | commands: %s" % (
                p.get("id") or "?", p.get("disposition") or "open",
                (": " + p["reason"]) if p.get("reason") else "", (p.get("fix") or "")[:300],
                "; ".join(p.get("commands") or [])[:300]))
    last_fix = mem.get("last_fix") or {}
    if last_fix and last_fix.get("held") is False:
        lines.append("NOTE: the last fix did NOT hold — it recurred %s time(s) since."
                     % last_fix.get("recurrences_since"))
    return lines


def build_messages(finding, ctx, sig=None, max_chars=CONTEXT_CHARS, memory=None):
    """``memory`` (issue_memory of this finding's fp) replaces the NotifyBook
    ``sig`` prior-proposal block; ``sig`` is only the read-through fallback when
    the toolserver could not be reached."""
    sig = {} if memory else (sig or {})
    f = finding
    lines = ["FINDING", "kind: %s  severity: %s  locus: %s  source: %s" % (
        f.get("kind"), f.get("severity"), f.get("locus"), f.get("source")),
        "count: %s  first: %s  last: %s" % (f.get("count"), time.strftime("%Y-%m-%d %H:%M", time.localtime(float(f.get("first_seen") or 0))),
                                           time.strftime("%Y-%m-%d %H:%M", time.localtime(float(f.get("last_seen") or 0)))),
        "signature: %s" % f.get("signature")]
    if f.get("suggested_action"):
        lines.append("suggested_action (from the log itself): %s" % f["suggested_action"])
    lines.append("samples:")
    lines += ["  > " + s[:300] for s in (f.get("sample_lines") or [])[:5]]
    if sig.get("did_not_hold"):
        lines.append("\nNOTE: previous accepted fix %s did not hold — the finding recurred after it was accepted."
                     % sig["did_not_hold"])
    priors = [p for p in sig.get("proposals") or [] if p.get("status") in ("delivered", "duplicate")]
    if priors:
        lines.append("\nPRIOR PROPOSALS for this signature (yours must differ materially from every one):")
        for p in priors:
            lines.append("- %s [%s%s] fix: %s | commands: %s" % (
                p.get("id") or "?", p.get("disposition") or "open",
                (": " + p["reason"]) if p.get("reason") else "", (p.get("proposed_fix") or "")[:400],
                "; ".join(p.get("commands") or [])[:300]))
    if sig.get("disposition") == "rejected" and sig.get("reason"):
        lines.append("keeper's last rejection: %s" % sig["reason"])
    if memory:
        lines += memory_block(memory)
    body = "\n".join(lines)
    src = []
    for c in ctx or []:
        src.append("--- %s:%s (%s) ---\n%s" % (c["path"], c["line"], c["how"], c["snippet"]))
    srctext = "\n\n".join(src) if src else "(no implicated source located)"
    user = (body + "\n\nSOURCE EXCERPTS\n" + srctext)[:max_chars]
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


# ── parse + validate ───────────────────────────────────────────────────────────────────
def _first_json(text):
    s = str(text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        return json.loads(s)
    except ValueError:
        pass
    start = s.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except ValueError:
                        break
        start = s.find("{", start + 1)
    return None


def parse(text):
    """-> validated proposal | {"no_new_fix": True, "why"} | None when junk."""
    d = _first_json(text)
    if not isinstance(d, dict):
        return None
    if d.get("no_new_fix") is True:
        return {"no_new_fix": True, "why": str(d.get("why") or "")[:600]}
    summary, fix = str(d.get("summary") or "").strip(), str(d.get("proposed_fix") or "").strip()
    if not summary or not fix:
        return None
    cmds = d.get("commands") or []
    if isinstance(cmds, str):
        cmds = [cmds]
    files = []
    for f in d.get("files") or []:
        if isinstance(f, dict) and f.get("path"):
            try:
                files.append({"path": str(f["path"])[:300], "line": int(f.get("line") or 0)})
            except (TypeError, ValueError):
                files.append({"path": str(f["path"])[:300], "line": 0})
    conf = str(d.get("confidence") or "low").lower()
    return {"summary": summary[:300], "likely_cause": str(d.get("likely_cause") or "").strip()[:1200],
            "proposed_fix": fix[:2000], "commands": [str(c).strip()[:400] for c in cmds if str(c).strip()][:8],
            "files": files[:8], "confidence": conf if conf in ("low", "medium", "high") else "low",
            "needs_privilege": bool(d.get("needs_privilege"))}


# ── novelty gate ───────────────────────────────────────────────────────────────────────
def _toks(s):
    return {w for w in re.findall(r"[a-z0-9_./-]+", str(s or "").lower()) if w not in _STOP and len(w) > 1}


def _cmdset(cmds):
    return {re.sub(r"\s+", " ", str(c).strip().lower()) for c in cmds or [] if str(c).strip()}


def jaccard(a, b):
    a, b = _toks(a), _toks(b)
    return len(a & b) / len(a | b) if (a or b) else 1.0


def novelty(p, priors):
    """None when ``p`` is new; else 'duplicate of <id>'."""
    for q in priors or []:
        if q.get("status") not in ("delivered", "duplicate"):
            continue
        if jaccard(p.get("proposed_fix"), q.get("proposed_fix")) >= JACCARD_DUP:
            return "duplicate of %s" % (q.get("id") or q.get("dup_of") or "?")
        cs = _cmdset(p.get("commands"))
        if cs and cs == _cmdset(q.get("commands")):
            return "duplicate of %s" % (q.get("id") or q.get("dup_of") or "?")
    return None


# ── delivery shape ─────────────────────────────────────────────────────────────────────
def fmt_board(p, finding, origin):
    """(text, note) of B's fix proposal in the BOARD-ITEM-FORMAT.md proposal
    shape (PROBLEM / OPTIONS / REC / DECISION / SKETCH) — the toolserver refuses
    a proposal without it. The disposition paragraph stays LAST: the keeper's
    close line after it is what parse_disposition reads."""
    text = ("[B proposal] " + p["summary"])[:500]
    cmds = "\n".join(p.get("commands") or []) or "# (no commands proposed)"
    files = "\n".join("- %s:%s" % (f["path"], f["line"]) for f in p.get("files") or []) or "- (none named)"
    conf = "%s confidence%s" % (p.get("confidence"), ", needs privilege" if p.get("needs_privilege") else "")
    note = ("PROBLEM: %s (%s)\nfinding %s · signature %s · origin %s\n`%s`\n\n"
            "OPTIONS:\nA) apply B's fix: %s — + addresses the likely cause / − B's diagnosis, not yet verified\n"
            "B) reject or mark inert — + no change / − the finding keeps recurring\n\n"
            "REC: A once the keeper has verified the cause (%s).\n\n"
            "DECISION: pending (keeper: disposition line below)\n\n"
            "SKETCH:\n```bash\n%s\n```\n\nfiles:\n%s\n\n"
            "close with a disposition line: `accept`, `reject: <reason>`, or `inert: <reason>` — B's next "
            "proposal for this signature must differ from this one; inert lowers its priority (still reported, t4178)."
            ) % (p.get("likely_cause") or "—", conf, finding.get("key"), finding.get("sigkey"), origin,
                 finding.get("signature") or "", p.get("proposed_fix"), conf, cmds, files)
    return text, note[:4000]


def fmt_mail(p, finding, bid):
    return ("🛠 B proposed a fix (%s) for %s ×%s\n%s\n\ncause: %s\nfix: %s\n```bash\n%s\n```"
            % (bid or "board", finding.get("identity"), finding.get("count"), p["summary"],
               (p.get("likely_cause") or "—")[:400], p["proposed_fix"][:600], "\n".join(p.get("commands") or [])))


def record(sig, p, status, now=None, bid=None, dup_of=None, why=None):
    """Append one attempt to the signature's proposal history."""
    now = now or time.time()
    entry = {"at": now, "status": status, "id": bid,
             "summary": (p or {}).get("summary"), "proposed_fix": (p or {}).get("proposed_fix"),
             "commands": (p or {}).get("commands") or [], "disposition": "open" if status == "delivered" else None,
             "reason": ""}
    if dup_of:
        entry["dup_of"] = dup_of
    if why:
        entry["why"] = why
    sig.setdefault("proposals", []).append(entry)
    sig["proposals"] = sig["proposals"][-30:]
    if status == "delivered":
        sig["disposition"] = "proposed"
    return entry
