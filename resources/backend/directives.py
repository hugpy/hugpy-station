"""Per-session directives + init briefs (1.0.143 — operator 2026-10-01).

ONE source of record for the text every agent session on a locus is handed:

    resources/backend/directive-templates/*.md   (shipped templates)
  + live facts of the locus, resolved at generation time (user, hostname,
    base directories, ports, B's model, the toolserver)
  + the operator's layers, read from the locus's own station state dir
    (🧭 operator guidance, per-session operator overlays, the tmux one-shot
    handoff, the fs / delegation switches)
  = one DISTINCT directive per (locus x session kind).

Session kinds: the four STANDING serve sessions of abstract-serve-core's roster
(keeper / chat / worker / local) — the norm — plus `tmux`, the terminal seats
(keeper-claude, keeper-codex, keeper-mct): a FALLBACK and an extra inference
arm, never "the keeper".

The generated part is fenced by BEGIN/END markers so any composition can strip
a stray copy mechanically (the directive-doubling guard): an operator text that
carries a generated block, or a pre-1.0.143 full "Frontier directive" copy, is
never composed twice.

Live files are DERIVED, never authored: `render` writes
<state>/directives/rendered/<kind>.md (+ index.json) on every serve start
(abstract-claude-serve-run) and on every station save; the serve reads them per
turn ($AC_SESSION_DIRECTIVES_DIR). Operator text lives ONLY in the operator
layers below, which this module reads and never writes.

CLI:  python3 directives.py render [--state DIR] [--out DIR] [--locus NAME] [--print KIND]
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = HERE / "directive-templates"
GENERATOR_VERSION = 1

SESSIONS = ("keeper", "chat", "worker", "local")       # abstract-serve-core roster.ROLES
KINDS = SESSIONS + ("tmux",)
TITLES = {"keeper": "KEEPER session", "chat": "CHAT session", "worker": "WORKER session",
          "local": "LOCAL session (B)", "tmux": "TMUX SEAT (fallback / inference arm)"}

# Which template parts each kind is built from, in order. The token-economy
# rules apply only where the session is METERED and has someone to delegate to
# (keeper, chat, the tmux seats) — not to the worker (it IS the delegate) and
# not to B (its tokens are free).
PARTS = {
    # 1.0.146 (t4250): the keeper carries a LOCUS NUANCES block — home, gate,
    # units, trees of ITS locus (directives/station.json `nuances`, written by
    # the station when it provisions / re-applies the locus's standing roles)
    "keeper": ("charge-keeper", "nuances-keeper", "core", "toolkit", "toolkit-keeper", "economy", "continuity-keeper"),
    "chat": ("charge-chat", "core", "toolkit", "economy"),
    "worker": ("charge-worker", "core", "toolkit"),
    "local": ("charge-local", "core", "toolkit-local"),
    "tmux": ("charge-tmux", "core", "toolkit", "economy", "continuity-tmux"),
}
# The station toolkit (operator 2026-10-01: "the directives must highlight the
# parts of the station that support DELEGATION"): one shared section + a keeper
# block, opened by a per-kind emphasis line. Every tool name it mentions is
# checked against the toolserver's tool list (test_directives_toolkit.py;
# live: check_directive_tools.py).
TOOLKIT_FOCUS = {
    "keeper": ("As keeper you ROUTE: search to B, bulk work to the worker or a subagent, "
               "a peer locus's work to its keeper. You keep the board and the ledger true."),
    "chat": ("As chat you hand work over: a task becomes a board item or `session_message "
             "{to:\"keeper\"}`. You read the board but you do not close it."),
    "worker": ("As worker you ARE the delegate: do the task, report the result with its evidence, "
               "and take every bug report for this locus (🧭 d4188)."),
    "local": "",
    "tmux": ("As a tmux seat you use the same toolkit when the operator runs keeper work through you "
             "or a session hands you a task."),
}
# The 🧭 operator guidance (lay of the land, station upkeep & release) is the
# KEEPER's charge text; the tmux seats carry it too because they are where
# keeper work runs when serve is down. chat / worker / B never get it.
GUIDANCE_KINDS = ("keeper", "tmux")
# Delegation switch (board t7): the tmux seats keep their per-backend ON/OFF
# section; the frontier serve sessions (keeper, chat) honour the "serve" key
# and only carry the section when it is ON.
DELEGATE_SERVE_KINDS = ("keeper", "chat")

# Operator layers, relative to the station state dir — the same keys as
# locus_exec.CFG_FILES, so a remote locus's snapshot composes identically.
OVERLAY_FILES = {k: f"directives/{k}.operator.md" for k in SESSIONS}
OVERLAY_FILES["tmux"] = "frontier-directive.md"         # the pre-1.0.143 steward file
LAYER_FILES = {
    "directive": "frontier-directive.md",
    # 1.0.146: no "handoff" file — the layer is the toolserver's launch pointer
    # (handoff/station → layer), set on the snapshot by the station at launch.
    "fs": "frontier-fs.json",
    "delegate": "frontier-delegate.json",
    "b_model": "b-model.json",
    "guidance": "mct/repl/operator-guidance.user.md",
    "serve": "ac-serve-station.json",
    "station": "directives/station.json",
    **{"dir_" + k: OVERLAY_FILES[k] for k in SESSIONS},
}
RENDERED_DIR = "directives/rendered"

BEGIN_RE = re.compile(r"<!-- hugpy-station directive [^>]*-->")
END = "<!-- /hugpy-station directive -->"
LEGACY_HEADER = "# Frontier directive (hugpy Station)"
COMPOSER_HEADERS = ("# Operator guidance\n", "# Session init prompt (handoff — this launch only)\n",
                    "# Session init prompt (handoff)\n")

FS_DENY_TOOLS = ["Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Grep", "Glob", "LS"]
DELEGATE_ONLY_TEXT = (
    "# Delegation switch: DELEGATE-ONLY (operator)\n"
    "You are the ROUTER for this seat, not the worker. Do not execute tasks yourself: "
    "for every task that needs more than a sentence of judgement, launch a subagent "
    "(the Agent tool) with a complete brief and let IT do the reading, editing, "
    "running and testing; keep your own turns to routing, brief-writing, and a short "
    "summary of what came back. Several independent tasks → several subagents in one "
    "message, in parallel. Direct questions that need no tool use you answer yourself. "
    "This keeps your context (the operator's spend) small and leaves you free to take "
    "new queries while work is in flight.")
DELEGATE_OFF_TEXT = ("# Delegation switch: OFF\nHandle tasks yourself; delegate when the "
                     "token economy above says so.")
FS_MEDIATED_TEXT = ("# Filesystem switch: MEDIATED\nYour direct file tools ("
                    + ", ".join(FS_DENY_TOOLS) + ") are denied by the operator for "
                    "this seat. Route every file read/search/edit through the local "
                    "agent (B) or a local shell; do not retry the denied tools.")
FS_DIRECT_TEXT = ("# Filesystem switch: DIRECT\nYour file tools are open. Prefer "
                  "delegating search and bulk reads to B anyway (token economy).")

_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
_OVERLAY_HDR = re.compile(r"^# Operator directive — [^\n]*\n", re.M)


# ── templates ────────────────────────────────────────────────────────────────
def template(name: str) -> str:
    return (TEMPLATE_DIR / f"{name}.md").read_text(encoding="utf-8")


def fill(text: str, facts: dict) -> str:
    """{{name}} -> facts[name]. An unknown placeholder is an error: a template
    must never ship a blank where a fact belongs."""
    def sub(m):
        k = m.group(1)
        if k not in facts:
            raise KeyError(f"directive template placeholder {{{{{k}}}}} has no fact")
        return str(facts[k])
    return _PLACEHOLDER.sub(sub, text)


def templates_digest() -> str:
    h = hashlib.sha256()
    for p in sorted(TEMPLATE_DIR.glob("*.md")):
        h.update(p.name.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()[:12]


# ── the doubling guard ───────────────────────────────────────────────────────
def strip_generated(text: str) -> str:
    """Remove every generated directive block, every composer header and every
    switch section from operator text, so a directive is composed EXACTLY once
    however often a composed file is fed back in as operator text."""
    t = text or ""
    while True:
        m = BEGIN_RE.search(t)
        if not m:
            break
        end = t.find(END, m.end())
        t = t[:m.start()] + (t[end + len(END):] if end >= 0 else "")
    t = t.replace(END, "")
    for hdr in COMPOSER_HEADERS:
        t = t.replace(hdr, "")
    t = _OVERLAY_HDR.sub("", t)
    for marker in (DELEGATE_ONLY_TEXT, DELEGATE_OFF_TEXT, FS_MEDIATED_TEXT, FS_DIRECT_TEXT):
        t = t.replace(marker, "")
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t + ("\n" if t else "")


def is_legacy_full(text: str) -> bool:
    """A pre-1.0.143 full "Frontier directive" (the old shipped text or an
    operator-edited copy of it). It is superseded by the templates — composing
    it next to them would double the nomenclature/economy rules — so it is
    preserved on disk, reported, and never composed."""
    return (text or "").lstrip().startswith(LEGACY_HEADER)


def overlay_text(text: str) -> str:
    """The composable part of an operator overlay ('' for a legacy full copy)."""
    if not (text or "").strip() or is_legacy_full(text):
        return ""
    return strip_generated(text)


# ── live facts ───────────────────────────────────────────────────────────────
def _json(text, default=None):
    try:
        v = json.loads(text) if text else default
    except ValueError:
        return default
    return v if v is not None else default


def facts(snap: dict | None = None, *, locus: str = "", kind: str = "", env=None,
          station_port: str = "") -> dict:
    """The live facts of a locus. `snap` = a seat-config snapshot (locus_exec
    read_cfg shape: {state, home, user, host, app, files}); without one this
    machine is resolved here and now (user, hostname, env)."""
    env = os.environ if env is None else env
    snap = snap or {}
    files = snap.get("files") or {}
    remote = bool(snap.get("remote"))
    home = str(snap.get("home") or os.path.expanduser("~"))
    state = str(snap.get("state") or env.get("HUGPY_STATION_STATE")
                or os.path.join(home, ".config", "hugpy-station"))
    app = str(snap.get("app") or "") if remote else str(HERE)   # this station's own app on the host
    station = _json(files.get("station"), {}) or {}
    serve = _json(files.get("serve"), {}) or {}
    bdoc = _json(files.get("b_model"), {}) or {}
    bm = bdoc.get("model") if isinstance(bdoc, dict) and isinstance(bdoc.get("model"), str) else ""
    ts_url = "" if remote else (env.get("HUGPY_TOOLSERVER_URL") or env.get("TOOLSERVER_URL")
                                or env.get("STATION_CONSOLE_TOOLSERVER") or "")
    tok = (env.get("TOOLSERVER_TOKEN") or env.get("STATION_CONSOLE_TOOLSERVER_TOKEN")
           or env.get("HUGPY_OPERATOR_TOKEN") or "")
    loc = (locus or snap.get("locus") or station.get("locus") or env.get("EXCHANGE_LOCUS")
           or env.get("STATION_LOCUS") or "")
    if loc in ("ae-vm-mgr", "ae_vm_mgr"):
        loc = "keeper"                                   # documented legacy alias
    nudge = str(env.get("STATION_NUDGE_TMUX") or "0").strip().lower() in ("1", "true", "on", "yes")
    return {
        "nuances": nuances_block(station.get("nuances"), home=home, state=state),
        "locus": loc or "(unconfigured — set STATION_LOCUS)",
        "kind": kind or snap.get("kind") or ("ssh" if remote else "host"),
        "user": str(snap.get("user") or ("(unknown)" if remote else getpass.getuser())),
        "hostname": str(snap.get("host") or ("(unknown)" if remote else socket.gethostname())),
        "facts_source": "on the locus by its station" if remote else "live at generation time",
        "home": home,
        "state": state,
        "app": app or "(the station backend dir on that locus)",
        "docs": (app + "/static/docs") if app else "(the station docs dir on that locus)",
        "ws": state + "/mct/repl",
        "local_ws": state + "/local-keeper",
        "board_file": (env.get("HUGPY_HOME") if not remote and env.get("HUGPY_HOME") else home + "/.hugpy")
                      + "/state/todo.json",
        "ac_root": (env.get("AC_ROOT") if not remote and env.get("AC_ROOT") else state + "/abstract-claude"),
        "station_port": str(station_port or ("" if remote else env.get("PORT", "")) or station.get("port") or "<station port>"),
        "serve_port": str(("" if remote else env.get("AC_PORT", "")) or serve.get("port") or "9124"),
        "toolserver": (f"`{ts_url}`" if ts_url else
                       "the toolserver configured on that locus" if remote else
                       "discovered on this host (abstract-toolserver discovery)"),
        "toolserver_token": ("unknown here" if remote else "set" if tok else "NOT set"),
        "b_model": f"`{bm}`" if bm else "the hugpy package default model",
        "tmux_nudges": ("ON (STATION_NUDGE_TMUX=1): only when the keeper serve session is unreachable "
                        "does the station type a 📨 nudge into the keeper-claude pane" if nudge else
                        "OFF (STATION_NUDGE_TMUX=0): the station types no pings, nudges or reminders "
                        "into a tmux seat"),
    }


NUANCE_KEYS = ("home", "user", "hostname", "kind", "state", "gate", "sudo_n", "units", "trees", "serve",
               "goal", "tree_rules", "registry", "updated")


def nuances_block(doc, home="", state="") -> str:
    """The LOCUS NUANCES table of the keeper directive from station.json
    `nuances` (written by the station: locus facts read ON the locus + the
    loci registry pointer). Without a record the block says so — never a
    blank, never an invented fact."""
    if not isinstance(doc, dict) or not doc:
        return ("| | |\n|---|---|\n| home | `%s` |\n| station state | `%s` |\n"
                "| registry | _no nuances recorded yet — the station writes them when it provisions this "
                "locus's serve (`POST /api/locus/serve/standing?vm=<locus>`)_ |" % (home or "~", state or "?"))
    rows = [("home", "`%s`" % (doc.get("home") or home or "~")),
            ("user@host", "`%s@%s` (locus kind `%s`)" % (doc.get("user") or "?", doc.get("hostname") or "?",
                                                           doc.get("kind") or "?")),
            ("station state", "`%s`" % (doc.get("state") or state or "?"))]
    gate = doc.get("gate")
    rows.append(("gate (sudo bottleneck)", ("`%s` on this locus%s" % (gate, " · passwordless sudo rules present"
                                                                   if doc.get("sudo_n") else ""))
                 if gate else "NOT installed — no hugpy-gate here; root actions go through the operator "
                              "or the keeper locus's gate (never assume sudo)"))
    units = doc.get("units") or []
    rows.append(("user units", ", ".join("`%s`" % u for u in units[:12]) + (" …" if len(units) > 12 else "")
                 if units else "none of abstract-claude-serve@* / hugpy-station* / hugpy-gate* are present"))
    sv = doc.get("serve") or {}
    if isinstance(sv, dict) and sv:
        rows.append(("serve", "instance `%s` · port %s · unit `%s` · standing roles keeper/chat/worker run with "
                              "`%s` in `%s`" % (sv.get("instance") or "station", sv.get("port") or "?",
                                               sv.get("unit") or "abstract-claude-serve@station.service",
                                               sv.get("permission_mode") or "bypassPermissions",
                                               sv.get("cwd") or doc.get("home") or "~")))
    trees = doc.get("trees") or []
    rows.append(("source trees", ", ".join("`%s`" % t for t in trees) if trees else "none found (no ~/src, /srv/pyit/dev, /srv/hugpy/src)"))
    rules = doc.get("tree_rules") or []
    if rules:
        rows.append(("tree rules", " · ".join(str(r) for r in rules)))
    if doc.get("goal"):
        rows.append(("registry goal", str(doc["goal"]).replace("|", "¦")[:600]))
    rows.append(("recorded", "%s from %s" % (doc.get("updated") or "?", doc.get("registry") or "the station")))
    return "| | |\n|---|---|\n" + "\n".join("| %s | %s |" % (k, v) for k, v in rows)


# ── composition ──────────────────────────────────────────────────────────────
def _delegate_doc(files: dict) -> dict:
    doc = _json(files.get("delegate"), {}) or {}
    return {k: bool(doc.get(k)) for k in ("mct", "claude-code", "serve")} if isinstance(doc, dict) else {}


def _fs_mediated(files: dict) -> bool:
    doc = _json(files.get("fs"), None)
    return bool(doc.get("mediated")) if isinstance(doc, dict) and "mediated" in doc else False


def signature(kind: str, locus: str) -> str:
    """Board/comms `by`: <locus>-<session kind> (NOMENCLATURE: a keeper signs
    <locus>-keeper); the keeper locus's own keeper keeps its standing sign-off
    `host-keeper` rather than "keeper-keeper"."""
    return "host-keeper" if (kind, locus) == ("keeper", "keeper") else f"{locus}-{kind}"


def generated(kind: str, fx: dict) -> str:
    """The fenced, template-only part of a kind's directive."""
    if kind not in KINDS:
        raise ValueError(f"unknown session kind {kind!r} (kinds: {', '.join(KINDS)})")
    f = dict(fx, session=kind, title=TITLES[kind], signature=signature(kind, fx.get("locus", "")),
             toolkit_focus=TOOLKIT_FOCUS[kind])
    body = "\n\n".join(fill(template(p), f).strip() for p in PARTS[kind])
    begin = (f"<!-- hugpy-station directive {kind}@{f['locus']} · generator v{GENERATOR_VERSION} · "
             f"templates {templates_digest()} · derived — edit the template or the operator overlay, "
             f"never this text -->")
    return begin + "\n" + body + "\n" + END


def compose(kind: str, snap: dict | None = None, *, fx: dict | None = None, backend: str = "",
            with_handoff: bool = False) -> str:
    """The full directive for one session kind on one locus.

    snap   = the locus's seat-config snapshot (files: directive, guidance,
             handoff, fs, delegate, b_model, dir_<session>, ...); None = no layers.
    fx     = precomputed facts (default: facts(snap)).
    backend (tmux only) = claude-code | mct | codex — picks the switch sections:
             claude-code gets fs + delegate, mct gets delegate, codex neither.
    with_handoff (tmux only) = append the session init prompt layer: since
             1.0.146 the toolserver's one-paragraph POINTER to the open handoff
             row (files["handoff"], set by the station from handoff/station —
             never a file); the body is injected once by the seat's
             SessionStart hook (handoff/pull).
    """
    snap = snap or {}
    files = snap.get("files") or {}
    fx = fx or facts(snap)
    parts = [generated(kind, fx)]
    g = ""
    if kind in GUIDANCE_KINDS:
        g = strip_generated(_strip_legacy(files.get("guidance") or "", files.get("directive") or ""))
        if g.strip():
            parts.append("# Operator guidance\n" + g.strip())
    ov = overlay_text(files.get("directive" if kind == "tmux" else "dir_" + kind) or "")
    if g.strip() and g.strip() in ov:                  # a verbatim copy of another layer: once
        ov = strip_generated(ov.replace(g.strip(), ""))
    if ov.strip():
        parts.append(f"# Operator directive — {TITLES[kind]}\n" + ov.strip())
    dl = _delegate_doc(files)
    if kind == "tmux":
        if backend == "claude-code":
            parts.append(FS_MEDIATED_TEXT if _fs_mediated(files) else FS_DIRECT_TEXT)
        if backend in ("claude-code", "mct"):
            parts.append(DELEGATE_ONLY_TEXT if dl.get(backend) else DELEGATE_OFF_TEXT)
        h = (files.get("handoff") or "").strip() if with_handoff else ""
        if h:
            parts.append("# Session init prompt (handoff)\n" + h)
    elif kind in DELEGATE_SERVE_KINDS and dl.get("serve"):
        parts.append(DELEGATE_ONLY_TEXT)
    return "\n\n".join(parts) + "\n"


def _strip_legacy(text: str, legacy: str) -> str:
    """Drop a verbatim copy of the steward's frontier-directive.md (legacy full
    directive or tmux overlay) that rode into the guidance text (the pre-1.0.65
    doubling) — it is composed on its own, once; the operator's words around
    it stay."""
    if legacy and legacy.strip() and legacy.strip() in (text or ""):
        text = text.replace(legacy.strip(), "")
    return text


def legacy_report(snap: dict | None) -> dict:
    """What the steward shows about the pre-1.0.143 frontier-directive.md."""
    t = ((snap or {}).get("files") or {}).get("directive") or ""
    if not t.strip():
        return {"present": False}
    full = is_legacy_full(t)
    return {"present": True, "chars": len(t), "legacy_full": full, "composed": not full,
            "note": ("pre-1.0.143 full frontier directive — superseded by the per-session templates, "
                     "preserved on disk and NOT composed; clear it (steward → directive → default) or "
                     "rewrite it as a short tmux-seat overlay" if full else
                     "composed as the operator overlay of the tmux seats")}


def render_all(snap: dict | None = None, fx: dict | None = None) -> dict:
    """{kind: text} — every kind for one locus. The tmux text is the
    claude-code seat's (no one-shot handoff: that rides only a real launch)."""
    fx = fx or facts(snap)
    return {k: compose(k, snap, fx=fx, backend="claude-code" if k == "tmux" else "") for k in KINDS}


# ── the locus's own files (host side) ────────────────────────────────────────
def read_state(state: Path | str, overrides: dict | None = None) -> dict:
    """A snapshot of THIS machine's layers in read_cfg shape. overrides =
    {key: absolute path} (the station passes its active guidance file)."""
    state = Path(state)
    out = {"state": str(state), "home": os.path.expanduser("~"), "files": {}}
    for key, rel in LAYER_FILES.items():
        p = Path((overrides or {}).get(key) or (state / rel))
        try:
            out["files"][key] = p.read_text(encoding="utf-8")
        except OSError:
            out["files"][key] = None
    return out


def write_rendered(out_dir: Path | str, texts: dict, fx: dict | None = None) -> list:
    """Write <out>/<kind>.md (+ index.json) atomically; a file whose text is
    unchanged is not rewritten (its bytes — and the serve's prompt cache — stay)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    changed, index = [], {"generator": GENERATOR_VERSION, "templates": templates_digest(), "kinds": {}}
    for kind, text in texts.items():
        p = out / f"{kind}.md"
        index["kinds"][kind] = {"chars": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()[:16]}
        try:
            if p.read_text(encoding="utf-8") == text:
                continue
        except OSError:
            pass
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
        changed.append(str(p))
    if fx:
        index["facts"] = {k: fx[k] for k in ("locus", "user", "hostname", "state", "station_port", "serve_port")}
    tmp = out / "index.json.tmp"
    tmp.write_text(json.dumps(index, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, out / "index.json")
    return changed


def render_state(state: Path | str, out_dir: Path | str | None = None, *, locus: str = "",
                 overrides: dict | None = None, station_port: str = "", env=None) -> dict:
    snap = read_state(state, overrides)
    fx = facts(snap, locus=locus, env=env, station_port=station_port)
    texts = render_all(snap, fx)
    changed = write_rendered(out_dir or Path(state) / RENDERED_DIR, texts, fx)
    return {"texts": texts, "facts": fx, "changed": changed,
            "out": str(out_dir or Path(state) / RENDERED_DIR)}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="directives.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="render every session kind for this locus")
    r.add_argument("--state", default=os.environ.get("HUGPY_STATION_STATE")
                   or os.path.join(os.path.expanduser("~"), ".config", "hugpy-station"))
    r.add_argument("--out", default="")
    r.add_argument("--locus", default="")
    r.add_argument("--print", dest="show", default="", choices=("",) + KINDS)
    a = ap.parse_args(argv)
    res = render_state(a.state, a.out or None, locus=a.locus)
    if a.show:
        sys.stdout.write(res["texts"][a.show])
    else:
        print(f"directives: {len(res['texts'])} kinds for {res['facts']['locus']} -> {res['out']}"
              f" ({len(res['changed'])} changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
