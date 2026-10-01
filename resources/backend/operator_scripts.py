"""Operator scripts on disk + their results (operator 2026-10-01: "the RUN
script should be a PRE-MADE FILE on disk, prepared for a single command from
the host, with proper directory control" and "the results should be explicitly
pointed to rather than a log search later").

Every open `operator` board item that carries a `RUN:` block (BOARD-ITEM-FORMAT.md
§1) is materialized on this locus as

    <state>/operator-scripts/<id>.sh              the RUN block
    <state>/operator-scripts/<id>.rollback.sh     the ROLLBACK RUN block

0755, world-readable (no secrets live in a board item). The operator runs it as
ONE command from ~ on the host: `bash <state>/operator-scripts/<id>.sh`. The
script carries its own absolute `cd`s, so the file never depends on the cwd.

Each file wraps itself (a derived prelude, see `prelude()`): on the first run
it re-executes itself under a capture pipe so its FULL stdout+stderr, every
line stamped, lands in a dedicated results dir, with a sidecar:

    <state>/operator-scripts/results/<id>/<ts>.log
    <state>/operator-scripts/results/<id>/<ts>.json
        {item, kind: run|rollback, script, sha256, started, ended, exit,
         host, user, cwd, log}

results/ is setgid group-writable (the operator's account shares the station
user's group); if it is not writable the script falls back to
~/operator-results/<id>/ and says so on stderr. The exit code is preserved.

The station reconciles on every board read (`sync`): files written / renamed
`.done` on close (never deleted) / revived on reopen; new sidecars are
ATTACHED to their item exactly once (`pending_results` → the caller posts the
comment → `mark_attached`), and `annotate` adds to the item JSON — never to
the stored text —

    one_liner / rollback_one_liner   "bash <path>"
    result   {exit, path, ts, kind, tail: [≤5 lines], sidecar}   (the latest)
    flag     {status: completed|fail, reason, detail, by: operator-script, ts}

The item is never auto-closed: the operator or the keeper closes it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

DIR = "operator-scripts"
RESULTS = "results"
MODE = 0o755
RESULTS_MODE = 0o2775            # setgid: the operator's account shares the station user's group
ATTACHED = ".attached.json"
TAIL = 5
_SECTION = re.compile(r"^(RUN|ROLLBACK RUN):\s*$", re.M)
_FENCE = re.compile(r"^\s{0,3}```")
SCRIPT_HEAD = ("#!/usr/bin/env bash", "set -euo pipefail")
CAPTURE_BEGIN = "# --- hugpy Station result capture (derived; do not edit) ---"
CAPTURE_END = "# --- end result capture ---"


def scripts_dir(state) -> Path:
    return Path(state) / DIR


def results_dir(state, item_id: str | None = None) -> Path:
    d = scripts_dir(state) / RESULTS
    return d / item_id if item_id else d


# ── the RUN blocks of a note ─────────────────────────────────────────────────
def run_blocks(note: str) -> dict:
    """{'RUN': script_text, 'ROLLBACK RUN': script_text} — the first fenced
    block under each header, fences stripped, verbatim otherwise."""
    out = {}
    lines = (note or "").split("\n")
    i = 0
    while i < len(lines):
        m = _SECTION.match(lines[i])
        if not m:
            i += 1
            continue
        name, j = m.group(1), i + 1
        while j < len(lines) and not _FENCE.match(lines[j]):
            if lines[j].strip():
                break
            j += 1
        if j < len(lines) and _FENCE.match(lines[j]):
            k = j + 1
            while k < len(lines) and not _FENCE.match(lines[k]):
                k += 1
            if k < len(lines) and name not in out:
                out[name] = "\n".join(lines[j + 1:k])
            i = k + 1
        else:
            i = j
    return out


# ── the file ─────────────────────────────────────────────────────────────────
def prelude(item_id: str, kind: str, results: Path, sha: str) -> list:
    """The self-wrapping capture, as bash. First run: re-exec under a capture
    pipe (every line stamped, tee'd to <ts>.log), then write the sidecar and
    exit with the script's own code. Wrapped run (__OPS_WRAPPED set): fall
    through to the script body. `set +e` only inside the wrapper branch — the
    body keeps `set -euo pipefail`."""
    return [
        CAPTURE_BEGIN,
        'if [ -z "${__OPS_WRAPPED:-}" ]; then',
        "  set +e",
        f"  __OPS_ID={item_id}; __OPS_KIND={kind}; __OPS_SHA={sha}",
        f'  __OPS_DIR="{results}"',
        '  if ! mkdir -p "$__OPS_DIR" 2>/dev/null || [ ! -w "$__OPS_DIR" ]; then',
        '    __OPS_DIR="$HOME/operator-results/$__OPS_ID"; mkdir -p "$__OPS_DIR"',
        '    echo "operator-script $__OPS_ID: results dir not writable; recording to $__OPS_DIR" >&2',
        "  fi",
        '  __OPS_TS="$(date -u +%Y%m%dT%H%M%SZ)"; __OPS_LOG="$__OPS_DIR/$__OPS_TS.log"; __OPS_RC="$__OPS_DIR/.$__OPS_TS.rc"',
        '  __OPS_START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; __OPS_CWD="$PWD"',
        '  { __OPS_WRAPPED=1 bash "$0" "$@" 2>&1; echo $? > "$__OPS_RC"; } '
        "| while IFS= read -r __l; do printf '%s %s\\n' \"$(date -u +%H:%M:%S)\" \"$__l\"; done | tee -a \"$__OPS_LOG\"",
        '  __OPS_EXIT="$(cat "$__OPS_RC" 2>/dev/null || echo 1)"; rm -f "$__OPS_RC"',
        "  printf '{\"item\":\"%s\",\"kind\":\"%s\",\"script\":\"%s\",\"sha256\":\"%s\",\"started\":\"%s\",\"ended\":\"%s\","
        "\"exit\":%s,\"host\":\"%s\",\"user\":\"%s\",\"cwd\":\"%s\",\"log\":\"%s\"}\\n' \\",
        '    "$__OPS_ID" "$__OPS_KIND" "$0" "$__OPS_SHA" "$__OPS_START" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
        '"$__OPS_EXIT" "$(hostname -s)" "$(id -un)" "$__OPS_CWD" "$__OPS_LOG" > "$__OPS_DIR/$__OPS_TS.json"',
        '  echo "operator-script $__OPS_ID ($__OPS_KIND): exit $__OPS_EXIT · $__OPS_LOG" >&2',
        '  exit "$__OPS_EXIT"',
        "fi",
        CAPTURE_END,
        "",
    ]


def render(item: dict, body: str, source: str, state=None, rollback: bool = False) -> str:
    """The file text: the script header, the item's provenance as comments, the
    capture prelude, then the RUN block verbatim. A block that already starts
    with the shebang / `set -euo pipefail` has them moved into the header (never
    doubled); the header always carries both."""
    lines = body.split("\n")
    if lines and lines[0].strip() == SCRIPT_HEAD[0]:
        lines = lines[1:]
    lines = [ln for i, ln in enumerate(lines) if not (i < 3 and ln.strip() == SCRIPT_HEAD[1])]
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(item.get("ts") or item.get("updated") or 0) or time.time()))
    iid = str(item.get("id"))
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    head = [SCRIPT_HEAD[0], SCRIPT_HEAD[1]] + [
        "# hugpy Station operator script — DERIVED from the board; edit the item, not this file.",
        "# item:    %s%s" % (iid, "  (ROLLBACK)" if rollback else ""),
        "# title:   %s" % str(item.get("text") or "").strip(),
        "# updated: %s" % ts,
        "# board:   %s" % source,
        "# run as the operator from ~ on the host: bash <this file>  (output + exit code are recorded under results/%s/)" % iid,
        ""]
    cap = prelude(iid, "rollback" if rollback else "run", results_dir(state, iid) if state is not None else Path(RESULTS) / iid, sha)
    return "\n".join(head + cap + lines).rstrip("\n") + "\n"


def paths(state, item_id: str) -> dict:
    d = scripts_dir(state)
    return {"RUN": d / f"{item_id}.sh", "ROLLBACK RUN": d / f"{item_id}.rollback.sh"}


def one_liners(state, item_id: str) -> dict:
    """{'one_liner', 'rollback_one_liner'} for the files that EXIST (live, not .done)."""
    p = paths(state, item_id)
    out = {}
    if p["RUN"].is_file():
        out["one_liner"] = f"bash {p['RUN']}"
    if p["ROLLBACK RUN"].is_file():
        out["rollback_one_liner"] = f"bash {p['ROLLBACK RUN']}"
    return out


def _write(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text and stat.S_IMODE(path.stat().st_mode) == MODE:
            return False
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, MODE)
    os.replace(tmp, path)
    return True


def ensure_results_dir(state) -> Path:
    """results/: setgid + group-writable so the operator's account (in the
    station user's group) can write; a default ACL for the group when setfacl
    exists. Best effort — the scripts fall back to ~ when it is not writable."""
    d = results_dir(state)
    d.mkdir(parents=True, exist_ok=True)
    try:
        if stat.S_IMODE(d.stat().st_mode) != RESULTS_MODE:
            os.chmod(d, RESULTS_MODE)
    except OSError:
        pass
    marker = d / ".acl-set"
    if not marker.exists():
        try:
            import grp
            g = grp.getgrgid(os.getgid()).gr_name
            subprocess.run(["setfacl", "-m", f"d:g:{g}:rwx", "-m", f"g:{g}:rwx", str(d)],
                           check=False, capture_output=True, timeout=5)
        except Exception:                                     # noqa: BLE001 — no setfacl: setgid alone
            pass
        try:
            marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
        except OSError:
            pass
    return d


def sync(state, items, source: str = "board") -> dict:
    """Reconcile the scripts dir with these board items. Open/doing operator
    items with a RUN block get their files (re)written; done items' files are
    renamed to `.done`; a reopened item's `.done` files are revived (and
    rewritten if the block changed). Items absent from `items` are untouched.
    -> {written: [...], closed: [...], revived: [...], dir}"""
    d = scripts_dir(state)
    rep = {"written": [], "closed": [], "revived": [], "dir": str(d)}
    try:
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, MODE)
        ensure_results_dir(state)
    except OSError:
        return rep
    for it in items or []:
        if not isinstance(it, dict) or it.get("type") != "operator" or not it.get("id"):
            continue
        iid = str(it["id"])
        blocks = run_blocks(it.get("note") or "")
        for name, path in paths(state, iid).items():
            done = path.with_name(path.name + ".done")
            if it.get("status") == "done":
                if path.is_file():
                    os.replace(path, done)
                    rep["closed"].append(str(path))
                continue
            body = blocks.get(name)
            if body is None:
                continue
            if done.is_file() and not path.is_file():
                os.replace(done, path)
                rep["revived"].append(str(path))
            if _write(path, render(it, body, source, state=state, rollback=(name != "RUN"))):
                rep["written"].append(str(path))
    return rep


# ── results: sidecars → the board item ───────────────────────────────────────
def _registry_path(state) -> Path:
    return results_dir(state) / ATTACHED


def _registry(state) -> dict:
    try:
        return json.loads(_registry_path(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_registry(state, reg: dict):
    p = _registry_path(state)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(reg, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _read_result(sidecar: Path) -> dict | None:
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or "exit" not in meta:
        return None
    log = Path(str(meta.get("log") or sidecar.with_suffix(".log")))
    try:
        tail = log.read_text(encoding="utf-8", errors="replace").rstrip("\n").split("\n")[-TAIL:]
    except OSError:
        tail = []
    try:
        ex = int(meta.get("exit"))
    except (TypeError, ValueError):
        ex = 1
    return {"exit": ex, "path": str(log), "ts": str(meta.get("ended") or meta.get("started") or ""),
            "kind": str(meta.get("kind") or "run"), "tail": [t for t in tail if t.strip()],
            "sidecar": str(sidecar), "host": meta.get("host"), "user": meta.get("user")}


def _sidecars(state, item_id: str) -> list:
    d = results_dir(state, item_id)
    try:
        return sorted(p for p in d.iterdir() if p.suffix == ".json" and not p.name.startswith("."))
    except OSError:
        return []


def pending_results(state, items) -> list:
    """New sidecars (not yet attached) for these items, oldest first:
    [{id, sidecar, result, comment}]. The caller posts `comment` on the
    item and then calls mark_attached — so a failed post is retried at the
    next read and a posted one is never repeated."""
    reg = _registry(state)
    out = []
    for it in items or []:
        if not isinstance(it, dict) or it.get("type") != "operator" or not it.get("id"):
            continue
        iid = str(it["id"])
        for sc in _sidecars(state, iid):
            if str(sc) in reg:
                continue
            r = _read_result(sc)
            if r is None:
                continue
            out.append({"id": iid, "sidecar": str(sc), "result": r, "comment": result_comment(r)})
    return out


def result_comment(r: dict) -> str:
    verdict = "completed" if r["exit"] == 0 else "FAILED"
    head = "[comment operator-script %s] RESULT %s exit %d (%s%s) · %s" % (
        time.strftime("%Y-%m-%d %H:%M"), r.get("ts") or "?", r["exit"], verdict,
        " · rollback" if r.get("kind") == "rollback" else "", r["path"])
    tail = r.get("tail") or []
    return head + ("\nlast %d lines:\n" % len(tail) + "\n".join("    " + t for t in tail) if tail else "")


def mark_attached(state, sidecar: str, item_id: str):
    reg = _registry(state)
    reg[str(sidecar)] = {"item": item_id, "attached": int(time.time())}
    try:
        _save_registry(state, reg)
    except OSError:
        pass


def latest_result(state, item_id: str) -> dict | None:
    """The newest ATTACHED result of an item (what the renderer shows)."""
    reg = _registry(state)
    for sc in reversed(_sidecars(state, item_id)):
        if str(sc) in reg:
            return _read_result(sc)
    return None


def annotate(state, items) -> list:
    """Add one_liner / rollback_one_liner, and the latest result + flag, to each
    open operator item. Returns the same list (items mutated in place). The
    stored text is never touched and the status never changes."""
    for it in items or []:
        if not (isinstance(it, dict) and it.get("type") == "operator" and it.get("status") != "done" and it.get("id")):
            continue
        iid = str(it["id"])
        it.update(one_liners(state, iid))
        r = latest_result(state, iid)
        if r is not None:
            it["result"] = {k: r[k] for k in ("exit", "path", "ts", "kind", "tail", "sidecar")}
            ok = r["exit"] == 0
            it["flag"] = {"status": "completed" if ok else "fail",
                          "reason": "exit %d" % r["exit"],
                          "detail": ("%s run %s · %s" % ("rollback" if r["kind"] == "rollback" else "script",
                                                        "completed" if ok else "failed", r["path"])),
                          "by": "operator-script", "ts": r["ts"]}
    return items
