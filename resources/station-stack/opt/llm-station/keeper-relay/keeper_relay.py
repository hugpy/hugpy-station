#!/usr/bin/env python3
"""keeper-relay — inject operator Discord messages into the keeper's tmux pane.

The keeper's Claude runs with --dangerously-skip-permissions. Anything this
daemon types into that pane becomes a keeper turn with full authority and no
confirmation prompt. That is the whole risk; every control below exists to
narrow *who* can reach that keyboard and *what shape* the text arrives in.

It also runs a TODO BOARD WATCHER: new items in ~/todo.json (added by the console
or the Discord bridge — anyone other than the keeper itself) are announced with
one injected [board] line, through the SAME idle-gated injection path — so a turn
in flight is never interrupted and the keeper learns of new work within a tick.

And a LULL RELAY: a catch-up digest so nothing pending on the console end goes
unrelayed. Every cycle it deterministically accrues CANDIDATES — new non-keeper
comments, stale-unseen open items, a deploy-ui.failed signal, a rise in bugreport
err+ findings. At a confirmed pane lull (>=3 pending, or the oldest waited past
LULL_MAX_WAIT, and a cooldown has elapsed) ONE background thread asks the brain
which candidates the keeper has not visibly seen in its terminal. The reply is
citation-gated (foreign ids discarded, the summary line rebuilt if it fails
checks) and FAIL-OPEN (brain unreachable/garbage -> relay everything); the one
[lull] line is delivered through the SAME idle-gated enqueue/drain path.

And a SESSION STEWARD (OFF by default): a LULL-TRIGGERED keeper restart with a
written handoff. On a ~2h cadence it ARMS — but only ARMS — a restart when the
session transcript has grown past a threshold (idle sessions skip silently); the
armed restart then FIRES only at a confirmed pane lull (the SAME idle gate above),
never mid-turn, after a 15-min warning the keeper can extend (echo +45m >
~/handoff/hold). The restart rotates ~/handoff/next.md -> last.md and relaunches
the keeper in the same tmux session. The actual restart defaults to DRY-RUN (log +
reset, the pane untouched); live is opt-in ON TOP of the opt-in feature flag.

And an OUTBOUND OPERATOR PINGER (ON by default): the other three features all end
at the KEEPER's keyboard; this one closes the loop back to the OPERATOR. When an
operator-relevant event lands — a new open ⚑ operator item, a new pending ⚖
proposal, a keeper reply to a non-keeper thread, or a console deploy failure — it
POSTs one message to the operator's Discord (<endpoint>/send, the same channel the
relay reads; NO LLM call). It is deliberately conservative (the operator's phone):
silent for ordinary todos/requests/bookmarks, status flips, and keeper non-reply
comments. Detection reuses the board `items` todo_watch already read (no extra
read); pings dedup by stable event id, first run seeds silently, all pings in a
cycle batch into ONE message, and at most one message goes out per cooldown (queued
across it, never dropped). "dry" detects+audits without POSTing; "off" is inert.

And a SESSION-CHANNEL SOURCE (inert unless its key is configured): a SECOND
inbound Discord channel, #keeper-keeper, reached through the dev.hugpy.ai
session API. It is polled on its own ~30s cadence with its own persisted cursor
(first sighting seeds at NOW — no history replay), but everything downstream is
the EXISTING machinery: the SAME classify() fail-closed filter against the SAME
operator allowlist (never a second allowlist), the SAME sanitize(), the SAME
idle-gated enqueue/drain — one delivery pipeline, two sources. The keeper's own
posts echo back in the poll as direction:"out" (author "keeper-keeper");
classify()'s direction=="in" gate drops them, exactly like the primary channel's
out-echoes. Going live is a SEPARATE opt-in (KEEPER_RELAY_SESSION_LIVE=1,
independent of KEEPER_RELAY_LIVE), so either source can inject without the other.
The two sources are also independent of each other's WIRING: the session source
polls even when the station has no STATION_DISCORD_API (offline mode), and a
session fetch fault never blocks the primary intake or the drain.

    keeper-relay                  # dry-run (DEFAULT): decide + log, inject nothing
    keeper-relay --live           # actually type into the pane
    keeper-relay --once           # single cycle, then exit (used by the tests)
    keeper-relay --status         # config (secrets masked), cursor, queue depth

Config (env, else /home/ubuntu/.env):
    KEEPER_STATION          this VM's station identity — the ONE knob the shared
                            copy is keyed off. Default: the hostname (matches
                            keeper-launch.sh). Drives the STATION-derived defaults
                            for the transcript dir, handoff mirror, and deploy
                            signals below; on blackbird -> the historical literals.
    STATION_DISCORD_API     operator comms channel — the relay's ONLY source.
                            (legacy name BLACKBIRD_DISCORD_API still read, with
                            a deprecation log; BLACKBIRD_TODO_API is the todo
                            bridge; NOT this.)
    KEEPER_KEEPER_DISCORD_KEY  session-API key for the #keeper-keeper channel —
                            the SECOND source. Its PRESENCE enables it (endpoint
                            derived from it; the key is never logged/printed).
                            Absent -> the session source is fully inert.
    KEEPER_SESSION_DISCORD_API  full session endpoint override (else derived
                            from the key above). Masked in --status.
    KEEPER_RELAY_SESSION_LIVE  "1" == inject session-channel messages for real.
                            Anything else -> session source stays DRY-RUN, even
                            when KEEPER_RELAY_LIVE=1 (independent gates).
    KEEPER_RELAY_ALLOW      comma-separated authors (default: devinthadude_)
    KEEPER_RELAY_TARGET     tmux target        (default: keeper:0.0)
    KEEPER_RELAY_SOCKET     tmux -L socket     (default: console)
    KEEPER_RELAY_LIVE       "1" == --live
    KEEPER_RELAY_INBOX      fetched-image dir  (default: /home/ubuntu/relay-inbox)
    KEEPER_RELAY_TODO       ~/todo.json path   (default: /home/ubuntu/todo.json)
    KEEPER_RELAY_TODO_NOTIFY  "off" disables the board watcher (default: on)
    KEEPER_RELAY_BOARD_QUIET_SECS  hold [board] notices until the keeper has been
                            INACTIVE this long, in seconds          (default 600)
                            The reserved 'cfg-relay' board bookmark can override
                            this live: its note is JSON and an integer field
                            board_quiet_secs (0..86400) wins over this env; 0
                            turns the gate off. Precedence: bookmark > env > default.
    KEEPER_RELAY_BOARD_MAX_HOLD_SECS  starvation cap: a quiet-held [board] item
                            delivers anyway once queued longer than this  (default
                            3600); 0 = no cap (hold indefinitely). The 'cfg-relay'
                            bookmark's integer board_max_hold_secs (0..86400) wins;
                            same precedence bookmark > env > default.
    KEEPER_RELAY_TODO_HISTORY  event-journal path (default: ~/todo-history.jsonl)
    KEEPER_RELAY_LAST_DEPLOY   console-ui last-deploy.json path (STATION-derived).
    KEEPER_RELAY_DEPLOY_FAILED console-ui deploy-ui.failed path (STATION-derived).
                            A non-existent signals dir == no UI deploy pipeline on
                            this VM -> the deploy candidate/ping is OFF (silent).
    KEEPER_RELAY_LULL       "off" disables the lull relay          (default: on)
    KEEPER_RELAY_LULL_STALE_MIN     open-item stale threshold, min (default: 30)
    KEEPER_RELAY_LULL_MAX_WAIT_MIN  force a digest past this age,  min (default: 10)
    KEEPER_RELAY_LULL_COOLDOWN_MIN  min gap between digests,       min (default: 15)
    HUGPY_API               chat-completions endpoint for the judge brain
    HUGPY_BRAIN             model id for the judge brain
    KEEPER_RELAY_STEWARD    "on" enables the session steward        (default OFF)
    KEEPER_RELAY_STEWARD_RESTART  the restart action: "dry" | "live" (default dry)
    KEEPER_RELAY_STEWARD_CADENCE_MIN     restart cadence floor, min  (default 120)
    KEEPER_RELAY_STEWARD_WARN_LEAD_MIN   warning lead time,     min  (default 3)
    KEEPER_RELAY_STEWARD_THRESHOLD_TOKENS  context tokens that fire the reset —
                            THE BAR since 2026-07-30           (default 400000)
    KEEPER_RELAY_STEWARD_THRESHOLD_BYTES reported only; triggers  (default 300000)
                            nothing since 2026-07-30
    KEEPER_RELAY_STEWARD_FLOOR_BYTES     legacy idle-skip floor      (default 50000)
    KEEPER_RELAY_STEWARD_MAX_EXTEND_MIN  max single extension,  min  (default 60)
    KEEPER_RELAY_STEWARD_TRANSCRIPT_DIR  keeper transcript dir (per-file inventory
                            growth); default STATION-derived (~/.claude/projects/<slug>).
    KEEPER_RELAY_STEWARD_JOBS_DIR   Claude Code jobs dir whose <shortid>/state.json
                            sessionIds mark background-job .jsonl to EXCLUDE from the
                            keeper's growth sum (default ~/.claude/jobs).
    KEEPER_RELAY_HANDOFF_SHARED_DIR  fleet-common handoff mirror base; default
                            STATION-namespaced (/srv/share/fleet/keeper-handoff/
                            <station>) so VMs don't clobber one another.
    KEEPER_RELAY_OPERATOR_PING   outbound operator pinger: on|dry|off  (default on)
    KEEPER_RELAY_OPERATOR_PING_COOLDOWN_MIN  min gap between pings, min (default 5)
State:  ~/.local/state/keeper-relay/state.json   (own cursor — NEVER hugpy-escalate's)
Audit:  ~/.local/state/keeper-relay/audit.jsonl  (append-only, content hashed)
Stop:   systemctl stop keeper-relay  |  touch ~/.keeper-relay.disabled
"""
import argparse
import fcntl
import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

ENV_FILE = "/home/ubuntu/.env"
STATE_DIR = os.path.expanduser("~/.local/state/keeper-relay")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
AUDIT_FILE = os.path.join(STATE_DIR, "audit.jsonl")
DISABLE_FILE = os.path.expanduser("~/.keeper-relay.disabled")

# --- STATION identity: the one per-VM knob the whole fleet is keyed off ---------
# ONE shared copy of this code runs on every keeper VM; per-VM behaviour is driven
# entirely by a single station identity, resolved ONCE at import. env KEEPER_STATION
# wins (the enroll installer / keeper-relay.service seed it); else the hostname —
# the SAME default keeper-launch.sh uses (STATION=${1:-$(hostname)}), so the keeper
# launches with CWD /srv/share/projects/<station> and this resolves to that station.
# On blackbird gethostname()=="blackbird", so EVERY value derived below comes out
# byte-identical to the historical hardcoded literals (backward-compat, tested).
def _resolve_station():
    s = os.environ.get("KEEPER_STATION")
    if s and s.strip():
        return s.strip()
    try:
        h = socket.gethostname()
    except OSError:
        h = ""
    return (h or "").strip() or "blackbird"


def _session_key_env_names(station):
    """Env var names to try for THIS station's #<station>-keeper session key, most
    specific first.

    WHY (2026-07-30, operator: "discord relays everything"). config() hardcoded
    KEEPER_KEEPER_DISCORD_KEY. That name is keeper-specific, so on every OTHER
    station an already-minted key was invisible and the session channel stayed off
    forever — teststation proved it: it holds a real key for the live
    `testation-keeper` channel and still reported `session=off`.

    Derivation follows the de-site-ify pattern the neighbours already use
    (_project_slug, _handoff_shared_dir_for): build the name from the station
    rather than from a literal. On keeper, station=="keeper" derives exactly
    "KEEPER_KEEPER_DISCORD_KEY" — byte-identical to the historical literal, which
    is why keeper's working setup is untouched by this.

    The literal is still returned as a FALLBACK, so a station that copied keeper's
    variable name keeps working; and non-alphanumerics fold to "_" so a hostname
    like "console-vm" yields CONSOLE_VM_KEEPER_DISCORD_KEY.
    """
    s = re.sub(r"[^A-Za-z0-9]", "_", station or "").upper().strip("_")
    names = []
    if s:
        names.append("%s_KEEPER_DISCORD_KEY" % s)
    if "KEEPER_KEEPER_DISCORD_KEY" not in names:
        names.append("KEEPER_KEEPER_DISCORD_KEY")
    return names


def _project_slug(station):
    """Claude Code slugs the keeper's launch CWD to its transcript-dir name: the
    absolute path with the leading slash dropped and every '/' -> '-'. The keeper
    launches with CWD /srv/share/projects/<station> (keeper-launch.sh), so the slug
    is '-srv-share-projects-<station>'. On blackbird == '-srv-share-projects-blackbird'
    (the historical literal), verified byte-identical."""
    cwd = "/srv/share/projects/" + station
    return "-" + cwd.strip("/").replace("/", "-")


def _steward_transcript_dir_for(station):
    """The keeper's ~/.claude/projects/<slug> transcript dir for a station."""
    return "/home/ubuntu/.claude/projects/" + _project_slug(station)


def _handoff_shared_dir_for(station):
    """Fleet-COMMON handoff mirror base, namespaced per station so every VM mirrors
    into its own subdir under ONE predictable place (was a single blackbird dir)."""
    return "/srv/share/fleet/keeper-handoff/" + station


def _deploy_signals_dir_for(station):
    """The station's console-ui deploy-signal directory. Present only on a VM that
    actually runs a UI deploy pipeline (blackbird); absent elsewhere == feature-off."""
    return "/srv/share/projects/" + station + "/console-ui/signals"


STATION = _resolve_station()

def _is_self_authored(who):
    """True when `who` names THIS station's keeper, so its own writes never notify
    it. The self-filter used to test `who == "keeper"` only — but peer-msg stamps
    replies with the STATION-qualified name ("blackbird-keeper", "keeper-keeper"),
    so a keeper's own peer replies never matched and it notified itself about them.
    Observed 2026-07-29: a [lull] fired for comment:keeper-2:14, which was this
    keeper's own reply on an already-closed item.
    Accepts the bare "keeper", "<station>-keeper", and the bare station name; the
    comparison is case-insensitive and whitespace-tolerant because these strings
    arrive from a board file that humans and three different tools all write."""
    if not isinstance(who, str):
        return False
    w = who.strip().lower()
    if not w:
        return False
    station = (STATION or "").strip().lower()
    names = {"keeper"}
    if station:
        names.add(station)
        names.add("%s-keeper" % station)
    # nomenclature ruling 2026-08-27 (docs/NOMENCLATURE.md): keeper names
    # AGENTS, never machines; the host locus is "host" and its keeper signs
    # "host-keeper". The host's legacy station name is literally "keeper", so
    # on that station (or an unnamed one) the explicit forms are self too.
    # On any OTHER station "host-keeper" is the host's keeper -- a peer.
    if station in ("", "keeper", "host"):
        names.update({"host", "host-keeper"})
    return w in names


# --- todo board watcher: notice new ~/todo.json work and inject a one-liner ---
# The console's add box and the Discord bridge write items to this file. We watch
# it so the keeper learns of new work within a tick instead of at the next manual
# board check. Items the keeper WROTE never notify (_is_self_authored on by/via).
# --- HUGPY_HOME: single base dir for hugpy runtime files ---------------------
# Byte-compatible with abstract_hugpy_dev/_platform/paths.py — keep in sync.
# Default ~/.hugpy (state/ config/ logs/ run/); migrates a legacy ~/<f> on first
# resolve so writes land organized instead of strewing $HOME.
def _hugpy_path(sub, name, *legacy):
    base = os.environ.get("HUGPY_HOME") or os.path.join(os.path.expanduser("~"), ".hugpy")
    d = os.path.join(base, sub)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    new = os.path.join(d, name)
    try:
        if not os.path.exists(new):
            for lname in legacy:
                old = os.path.join(os.path.expanduser("~"), lname)
                if os.path.exists(old) and os.path.abspath(old) != os.path.abspath(new):
                    try:
                        os.replace(old, new)
                    except OSError:
                        import shutil
                        shutil.move(old, new)
                    break
    except OSError:
        pass
    return new


TODO_FILE = _hugpy_path("state", "todo.json", "todo.json")   # env KEEPER_RELAY_TODO overrides
TODO_SEEN_CAP = 300                    # persisted seen-id ring; oldest fall off
TODO_SNIPPET = 80                      # per-item chars of text quoted in the line
TODO_NOTIFY_MAX = 300                  # target max chars of the injected line

# --- board quiet gate (operator ask, board t44) ------------------------------
# A [board] to-do notice used to deliver at the FIRST instant the pane looked
# idle — the blink between two keeper turns. Instead hold it until the keeper has
# gone QUIET (no observed activity — no busy marker, no transcript growth) for a
# full window, so the batch lands in a genuine lull. Only author=="board" queue
# items wait; direct operator messages, the lull digest and the steward are
# untouched. The instantaneous idle gate still applies on top (quiet != safe).
#
# The window is operator-tunable at RUNTIME (board t45) without a restart: the
# reserved 'cfg-relay' board bookmark carries a JSON note whose integer field
# board_quiet_secs (0..86400) overrides the env. Precedence bookmark > env >
# default; 0 = gate off (board items deliver on the instantaneous idle gate, like
# pre-t44). The value is re-read from the SAME board read every cycle (no second
# file read) so an edit takes effect within one tick — see _cfg_relay_cycle.
BOARD_QUIET_SECS = 600                 # env KEEPER_RELAY_BOARD_QUIET_SECS overrides
BOARD_QUIET_MAX = 86400                # ceiling (24h) on an operator-set board_quiet_secs
CFG_RELAY_ID = "cfg-relay"             # reserved board bookmark carrying relay settings

# --- cfg-relay mint (t75): make the knob exist on EVERY VM -------------------
# The console's "⏱ push" cadence dropdown only works on a board that already
# HAS the reserved cfg-relay bookmark — the UI refuses to mint reserved ids and
# the console API cannot mint custom ids by design. Since every VM runs this
# daemon from the shared checkout, keeper-relay mints the bookmark ONCE per VM
# if (and only if) it is missing, seeding it with the value the env/default
# layer ALREADY resolved — so minting changes nothing about live behaviour, it
# only makes the truth visible/editable in the UI. After ONE successful mint
# (or on finding the bookmark already present) a persistent state marker is set
# and this VM NEVER mints again: an operator who deletes cfg-relay deliberately
# is not fought. See cfg_relay_mint_cycle.
CFG_RELAY_MINT_TEXT = ("⚙ relay settings — board push cadence "
                       "(operator knob in the ☑ drawer)")
# Board lock: the SAME <board-dir>/.todo.lock advisory flock the todo CLI and
# console-api hold across their load->save — mirrored here (bounded wait, then
# fail-closed: skip the tick, retry next cycle).
BOARD_LOCK_WAIT_SECS = 10.0            # bounded wait for the board lock
BOARD_LOCK_POLL_SECS = 0.2             # poll interval while waiting

# --- board starvation cap (keeper ask; DEFAULT OFF per operator 2026-07-29) ---
# The quiet gate above can hold a [board] item indefinitely if the keeper never
# goes quiet. A cap can bound that: a held board item delivers ANYWAY (on the
# normal instantaneous idle gate) once it has been QUEUED longer than the cap,
# measured from its own enqueue ts. env KEEPER_RELAY_BOARD_MAX_HOLD_SECS overrides;
# the 'cfg-relay' bookmark's integer field board_max_hold_secs (0..86400) wins over
# both (precedence bookmark > env > default). A resolved value of 0 = NO cap: hold
# indefinitely; an OMITTED bookmark field falls back to env.
#
# DEFAULT IS 0 (no cap). Operator directive 2026-07-29: "the check to-do should
# only happen in periods of a lull, not on a timer no matter what." A cap is
# exactly a timer that fires regardless of lull — with the old 3600 default a
# notice queued during a long working stretch force-injected an hour later, and
# on this station that delivered a 65-minute-stale notice about an item already
# finished and deleted. A late notice is cheap; an interrupting one is not.
# Set a non-zero value only if a station demonstrably starves.
BOARD_MAX_HOLD_SECS = 0                # env KEEPER_RELAY_BOARD_MAX_HOLD_SECS overrides
BOARD_MAX_HOLD_MAX = 86400             # ceiling (24h) on an operator-set board_max_hold_secs

# --- peer-message prompt ping (operator ask 2026-07-29) ----------------------
# "Every message sent from one VM to another must ping the receiving VM." A
# peer keeper's `peer-msg send` lands as a board ITEM (stamped by:"<vm>-keeper",
# usually type:"message"); its `peer-msg reply` lands as a COMMENT; the local
# peer-msg poller surfaces remote replies as by:"peer-msg" comments on the
# peer-inbox feed. All three used to ride the quiet-gated [board] / lull paths,
# so with the lull-only delivery directive a busy keeper could go hours without
# hearing a message had ARRIVED at all. A message is CONVERSATION, not queue
# work — the exact class the lull directive kept exempt (direct operator lines,
# the steward). So peer traffic drains as its own author class, exempt from the
# board quiet gate but still behind the instantaneous idle gate: prompt, never
# mid-turn. The ✉ messages tab (console t100) catalogs the same class, so what
# pings and what the tab shows stay one definition.
PEER_AUTHOR = "peer"                   # queue author class: idle-gate only
PEER_MSG_BY = "peer-msg"               # the peer-msg poller's notify identity
PEER_SNIPPET = 80                      # per-message chars quoted in the line
PEER_NOTIFY_MAX = 300                  # target max chars of the injected line


def _is_peer_author(who):
    """True when `who` writes from ANOTHER VM: a peer keeper (peer-msg stamps
    "<vm>-keeper") or the peer-msg poller's own reply notifications. Self names
    are excluded FIRST via _is_self_authored, so "<this-station>-keeper" (and
    "keeper-keeper" on a station literally named keeper) never reads as a peer."""
    if not isinstance(who, str):
        return False
    w = who.strip().lower()
    if not w or _is_self_authored(w):
        return False
    return w == PEER_MSG_BY or w.endswith("-keeper")


def _is_peer_msg_item(it):
    """A board item that is a MESSAGE from another VM: anything a peer keeper
    filed (peer-msg can send --type message|request|todo — all of it is peer
    traffic), or an explicit type:"message" the keeper didn't write itself.
    Mirrors the console's isMsgItem rule (t100) so the prompt-ping class and
    the ✉ messages tab agree on what a message is."""
    if not isinstance(it, dict):
        return False
    if _is_self_authored(it.get("by")) or _is_self_authored(it.get("via")):
        return False
    return it.get("type") == "message" or _is_peer_author(it.get("by"))

# --- board event journal: an append-only, GOING-FORWARD history --------------
# Every board change (add / status / note / text / comment / removal) and every
# console-ui deploy outcome is appended as one JSON line to an append-only
# journal — the history the console's board-history view reads back. It is
# SEPARATE from the notify path above: history records EVERYONE's actions (the
# keeper's own writes included), because the by/via filter exists to suppress
# self-echo PINGS, not to hide history. First run seeds fingerprints silently
# (no birth storm) — history begins the moment this deploys, with NO backfill.
TODO_HISTORY_FILE = _hugpy_path("state", "todo-history.jsonl", "todo-history.jsonl")  # env KEEPER_RELAY_TODO_HISTORY
# STATION-derived (blackbird -> the historical /srv/share/projects/blackbird/... path).
# env KEEPER_RELAY_LAST_DEPLOY overrides (config()). A NON-EXISTENT signals dir means
# this VM has no UI deploy pipeline -> the deploy detectors skip silently (feature-off).
LAST_DEPLOY_FILE = _deploy_signals_dir_for(STATION) + "/last-deploy.json"
HIST_TEXT_MAX = 120       # text/note/to/comment fields, per the fixed UI contract
HIST_REASON_MAX = 200     # deploy-failed reason
HIST_CAP_BYTES = 2 * 1024 * 1024   # rotate to <path>.1 past ~2MB (single rotation)

# --- lull relay: an LLM-judged catch-up digest -------------------------------
# Deterministic detection (every cycle) accrues CANDIDATES — new non-keeper
# comments, stale-unseen open items, a deploy-ui.failed signal, a rise in
# bugreport err+ findings — each with a stable id. At a confirmed pane lull a
# single background thread asks the brain which candidates the keeper has NOT
# yet visibly seen in its terminal; the answer is citation-gated and FAIL-OPEN
# (unreachable/garbage -> relay everything), then delivered through the SAME
# idle-gated enqueue/drain as every other line.
HUGPY_API = "https://dev.hugpy.ai/api/v1/chat/completions"   # env HUGPY_API
HUGPY_BRAIN = "Qwen~Qwen3-Coder-Next-GGUF"                    # env HUGPY_BRAIN
BUGREPORT_FILE = _hugpy_path("logs", "bugreport.json", "bugreport.json")
# STATION-derived (see LAST_DEPLOY_FILE). env KEEPER_RELAY_DEPLOY_FAILED overrides.
DEPLOY_FAILED_FILE = _deploy_signals_dir_for(STATION) + "/deploy-ui.failed"
# "err and above" by the bugreport scanner's own severity ranking (rank <= 3).
LULL_ERR_SEVERITIES = frozenset(
    ["emerg", "alert", "crit", "critical", "err", "error", "fatal"])
LULL_TAIL_LINES = 150     # keeper pane tail (capture-pane -S) handed to the brain
LULL_TAIL_CHARS = 6000    # hard cap on the tail text spliced into the prompt
LULL_HTTP_TIMEOUT = 120   # per-attempt seconds for the ONE chat call (+1 retry)
LULL_SNIPPET = 80         # per-candidate chars of file text quoted in a render
LULL_LINE_MAX = 250       # max chars of the model's summary line (post-sanitize)
LULL_LINE_IDS = 8         # ids listed in a deterministically-composed summary
LULL_DONE_CAP = 500       # persisted delivered-candidate ring; oldest fall off

# --- session steward: a lull-triggered keeper restart with a written handoff ---
# Long keeper sessions drift into uncontrolled context compaction; a deliberate
# restart with a handoff beats an unpredictable squeeze (LOCAL-LLM-OFFLOAD.md §4).
# THE CORE PRINCIPLE — RESTART IN LULLS: the ~2h cadence does NOT force a restart,
# it ARMS one; the armed restart then FIRES only at a confirmed pane lull (the SAME
# idle gate every injection uses — pane idle, not mid-turn). The clock is the
# floor; the lull is the trigger. Never interrupt active work.
#
# Activity metric (board t79/t81 — per-file INVENTORY growth, NOT newest-file):
# Claude Code appends to a per-session transcript (~/.claude/projects/<slug>/*.jsonl),
# but that dir holds MANY concurrent .jsonl files (the interactive keeper session plus
# every background/subagent job — see STEWARD_JOBS_DIR). The old metric measured only
# the NEWEST-mtime file's size against a scalar baseline; with siblings churning, the
# "newest file" flipped identity between polls (baseline for file A, size of file B ->
# growth clamped to 0), and after a LIVE restart the fresh smaller .jsonl read below the
# stale baseline (growth muted for the rest of the day). Observable failure: steward-
# state.json stuck at growth_bytes:0 while the session visibly grew, so the restart offer
# never armed. FIX: at each reset snapshot an INVENTORY {file -> size}; growth = the SUM
# over the current files of REAL appended bytes since that snapshot — existing file ->
# max(0, cur-snap); new-since-reset file -> its full size; shrunk/vanished file -> 0
# (never negative). Robust to the newest-file identity flip and to a live restart opening
# a fresh smaller file. No growth = an idle session -> skip silently, no restart needed.
#
# SAFETY: the WHOLE feature is OFF by default (the operator opts in deliberately),
# and even then the actual restart defaults to DRY-RUN (log + reset, pane untouched).
# STATION-derived from the keeper's launch CWD slug (blackbird -> the historical
# -srv-share-projects-blackbird). env KEEPER_RELAY_STEWARD_TRANSCRIPT_DIR overrides.
STEWARD_TRANSCRIPT_DIR = _steward_transcript_dir_for(STATION)
# --- background-job transcript exclusion (board t79/t81) ----------------------
# The transcript dir holds ONE .jsonl per session, ALL mixed at the top level: the
# interactive keeper session AND every background/daemon job Claude Code spawns
# (each `claude` daemon run under ~/.claude/jobs/<shortid>/ writes its own sibling
# .jsonl into the SAME projects dir). Those job files grow on their own clock and,
# being just as recent, used to steal the "newest-mtime" title from the keeper's
# live transcript every few polls — see the growth-metric rationale block below.
# The reliable discriminator (verified on box 2026-07-22): each daemon job leaves a
# ~/.claude/jobs/<shortid>/state.json carrying the FULL sessionId of the .jsonl it
# writes. So the set of job sessionIds := {state.json.sessionId for each job dir}
# names exactly the sibling files to EXCLUDE from the keeper's growth sum. It is a
# filesystem fact, not a heuristic — a job with no state.json (mid-spawn) simply
# isn't excluded yet (fails toward INCLUDING, never toward silently zeroing growth).
# NOTE: in-process Task/sidechain subagents do NOT get a jobs/ dir and are not
# separately excluded; they are part of whichever session (interactive or job)
# spawned them, so their bytes already land in that session's own .jsonl.
STEWARD_JOBS_DIR = "/home/ubuntu/.claude/jobs"
HANDOFF_DIR = "/home/ubuntu/handoff"
HANDOFF_NEXT = os.path.join(HANDOFF_DIR, "next.md")  # written by the outgoing keeper
HANDOFF_LAST = os.path.join(HANDOFF_DIR, "last.md")  # rotated to on restart
HANDOFF_HOLD = os.path.join(HANDOFF_DIR, "hold")     # the keeper's extension request
HANDOFF_WARNING = os.path.join(HANDOFF_DIR, "WARNING.md")  # imminent-restart notice
# Fleet-COMMON MIRROR of the handoff, so any VM's handoff is readable from ONE
# predictable place. Namespaced per station under a single common base so every VM
# mirrors into its OWN subdir instead of clobbering a shared pair of files. ADDITIVE
# only: ~/handoff/* stays the authoritative source and is untouched; the shared copy
# is best-effort and NEVER gates or alters the restart. STATION-derived; env
# KEEPER_RELAY_HANDOFF_SHARED_DIR overrides (config() -> _steward_mirror_handoff).
HANDOFF_SHARED_DIR = _handoff_shared_dir_for(STATION)
HANDOFF_SHARED_NEXT = os.path.join(HANDOFF_SHARED_DIR, "next.md")
HANDOFF_SHARED_LAST = os.path.join(HANDOFF_SHARED_DIR, "last.md")
# The standard keeper launcher, relaunched in the SAME tmux session on a live
# restart (design §4: graceful /exit, wait, relaunch). Verified present on box.
KEEPER_LAUNCH_CMD = "/opt/station-keeper/keeper-launch.sh"
STEWARD_CADENCE_MIN = 120       # clock FLOOR: don't even consider a restart sooner
# Operator 2026-07-29, the reset contract in full: ONE execution parameter (token
# usage), a 3-MINUTE grace period that tells the keeper to write a handoff, and a
# postponement the keeper may take in 10-MINUTE steps INDEFINITELY. The grace is
# deliberately short because it is not a negotiation — it is "save your work"; the
# indefinite hold is what makes that safe, since a keeper mid-thought can always buy
# another 10 minutes rather than lose the thread.
STEWARD_WARN_LEAD_MIN = 3       # grace period: warning -> fire (operator: 3 minutes)
STEWARD_MAX_EXTEND_MIN = 10     # ONE postponement step; repeatable without limit
STEWARD_EXIT_TIMEOUT = 15.0     # live restart: seconds to wait for the TUI to leave
STEWARD_CLEAR_SETTLE_SEC = 2.0  # let /clear land before the reorient ping is typed
# The post-reset ping. A /clear'd keeper has NO context — not the directive, not the
# handoff, not even the knowledge that it was just reset. This line is the whole
# bridge between the old session and the new one, so it names both files explicitly
# and says to read them BEFORE acting; a fresh keeper that starts guessing is exactly
# the failure the handoff exists to prevent.
STEWARD_REORIENT_TEXT = (
    "[steward] CONTEXT RESET — you are a fresh keeper and this pane's history is "
    "gone. Your directives are ALREADY in your system prompt (the launcher injects "
    "them; do NOT re-read them). Read ONE file before doing anything else: the "
    "outgoing keeper's handoff at ~/handoff/next.md — the only record of what was "
    "in flight when the reset fired. Then check ~/todo.json for open work. Do not "
    "guess at state you can read.")
# Byte bands on INVENTORY growth since the last reset (summed real appended bytes
# across the keeper's transcript files, background jobs excluded — see the metric
# rationale block above):
#   < FLOOR      -> idle session, skip SILENTLY (log nothing) — the idle-session skip
#   < THRESHOLD  -> some activity, but under the bar to warrant a restart yet
#   >= THRESHOLD -> arm (tokens are the trigger; the clock is only the cadence)
# THRESHOLD default 300k bytes ~= a substantial working session's transcript on this
# box (jsonl carries full tool I/O, so bytes accrue fast); tune from the audit trail.
STEWARD_THRESHOLD_BYTES = 300_000
STEWARD_FLOOR_BYTES = 50_000

# --- THE RESET BAR IS TOKENS, NOT BYTES (operator, 2026-07-29/30: "I need for the
#     steward reset to go by tokens, not mb") ---------------------------------
#
# The bytes above are now REPORTED-ONLY. Nothing triggers on them.
#
# WHY. A transcript byte is a terrible proxy for the thing that actually runs out.
# Measured on this box in one day: 1.3 MB -> 164,029 tok (~126 tok/KB), 0.2 MB ->
# 55,675 tok (~278 tok/KB), 0.03 MB -> 15,350 tok (~512 tok/KB). A 4x spread,
# because tool output and file reads bloat the FILE at a completely different rate
# than they bloat CONTEXT. A byte budget therefore fires on a lean session and
# spares a bloated one — the "reset amplifies the problem it was created to solve"
# complaint. _steward_context_tokens has been reading the real figure (what the
# model itself reported consuming: input + cache_read + cache_creation) since
# 2026-07-25, and the console meter has shown it since; only the DECISION was
# still on bytes. This closes that gap.
#
# THE DEFAULT. 400,000 tokens: comfortably inside the 1M window the fleet's keepers
# run, and ~2.5x the ~160k an actively-working keeper session was measured at, so a
# normal session resets after real accumulation rather than eagerly. A station on a
# smaller window sets its own bar — env, or the console's threshold_tokens field.
#
# WHEN TOKENS CANNOT BE READ (no usage record yet, unreadable transcript) there is
# NO budget trigger — deliberately. The old code would have fallen back to bytes,
# and falling back to the metric the operator just rejected is not a safety net, it
# is the defect wearing a hat. An unmeasurable session simply does not reset on
# budget; the state file says so (budget_metric/context_tokens), and the opt-in
# max-age clock remains available for a station that wants a hard cap anyway.
STEWARD_THRESHOLD_TOKENS = 400_000
STEWARD_THRESHOLD_TOKENS_LO = 20_000     # console band: below this is thrash
STEWARD_THRESHOLD_TOKENS_HI = 2_000_000  # above the largest window we run

# --- console control surface: a file-as-interface config the CONSOLE writes and
# keeper-relay reads LIVE each cycle (the station's ~/todo.json / ~/wireframe.json
# pattern). keeper-relay ONLY READS it — it never creates/writes/seeds it, so an
# absent file == pure env/default behaviour (nothing changes).
# Precedence: a present+VALID field here > env var > the built-in constants above.
# FAIL-SAFE (this file can trigger a keeper restart): every failure mode degrades
# to env/default, NEVER to a more restart-prone setting — a malformed config can
# never restart the keeper. See _steward_file_overrides for the exact contract.
STEWARD_CONFIG_FILE = _hugpy_path("config", "steward.json", "steward.json")

# Manual reset request (t89): the console (or the keeper user) drops
# {"schema":"steward-request.v1","op":"reset"} here; the relay consumes it
# at-most-once (read then unlink — the bugreport-request idiom) and pulls the
# steward straight to the FIRE phase: no cadence, no growth, no warn lead —
# the operator IS the warner. The lull gate stays: a mid-turn keeper is never
# yanked, the reset lands at the first quiet moment. Ignored (with an audit)
# when the steward feature is off.
STEWARD_REQUEST_FILE = _hugpy_path("state", "steward-request.json", "steward-request.json")

# t90: one-shot marker the restart drops so keeper-launch.sh boots FRESH (shed
# context) rather than --continue (resume). The launcher archives transcripts +
# reorients from ~/handoff/next.md, then consumes the marker.
STEWARD_RESET_MARKER = "/home/ubuntu/.keeper-reset-fresh"
STEWARD_CONFIG_SCHEMA = "steward.v1"
# console "mode" -> the two existing knobs (steward, steward_restart):
#   off -> steward=False (feature inert)
#   dry -> steward=True,  steward_restart="dry"  (detect+warn+handoff, NO restart)
#   on  -> steward=True,  steward_restart="live" (live restart)
STEWARD_MODES = ("off", "dry", "on")
# timeout_min (console dropdown, default 60) maps onto the cadence FLOOR:
#   steward_cadence_sec = timeout_min * 60  — i.e. how soon after the last reset a
# restart may even be CONSIDERED. Growth still ARMS (threshold bytes unchanged);
# this only schedules the quiet-moment window. Bounded [LO,HI]; out of range or
# wrong type -> dropped, keeping the env/default cadence (>= this, never MORE eager).
STEWARD_TIMEOUT_MIN_LO = 1
STEWARD_TIMEOUT_MIN_HI = 1440
# BUDGET and CLOCK are peers (operator, 2026-07-24): neither may veto the other.
# The cadence is no longer an arm gate; this is the only remaining time floor —
# a hard minimum between resets so a burst can't chain restarts back-to-back.
# CAPPED BY THE CONFIGURED CADENCE: an operator who sets cadence to 0 (or a small
# value) means "arm as soon as the budget says so", and a hardcoded floor must not
# silently override that. The floor only ever SHORTENS to honour the setting.
STEWARD_ANTI_THRASH_SEC = 120


def _anti_thrash_sec(cfg):
    """The effective anti-thrash floor: the default, never longer than the
    configured cadence. cadence 0 -> no floor (immediate arming is intentional)."""
    cadence = cfg.get("steward_cadence_sec")
    if cadence is None:
        return STEWARD_ANTI_THRASH_SEC
    return min(STEWARD_ANTI_THRASH_SEC, float(cadence))
# threshold_bytes (console, default = env/const 300000) overrides the growth-ARM bar
# (steward_threshold_bytes): the inventory growth since reset that ARMS a restart.
# Bounded [LO,HI]; out of range / wrong type / bool -> dropped, keeping env/default.
# LO matches the FLOOR default (50000: never arm below the idle-skip line); HI is
# ~1M tokens at ~4 bytes/token (4_000_000) — a full context, a sane upper stop.
STEWARD_THRESHOLD_BYTES_LO = 50_000
STEWARD_THRESHOLD_BYTES_HI = 4_000_000

# max_age_min (console/steward.json) — the TIME-ARM gate (t89): a session OLDER
# than this since its last reset ARMS regardless of growth, so long-lived idle
# sessions still get reset (each turn on an old session pays for its whole
# accumulated context — capping age caps that cost). 0 = gate OFF (the default:
# absent knob keeps today's growth-only behaviour). Still lull-gated + warned +
# extendable — max-age changes only the ARM condition, never the safety path.
STEWARD_MAX_AGE_MIN_LO = 30
STEWARD_MAX_AGE_MIN_HI = 10_080          # 7 days
# warn_min (console/steward.json) — minutes of warning before the restart fires.
# Floor of 1: below that the keeper cannot finish a handoff write, which makes
# the warning worse than useless (it promises time that does not exist). Ceiling
# of 60: a lead longer than an hour is drift, not warning. 0 bypasses both bounds
# and means "no warning" — the explicit opt-out.
STEWARD_WARN_MIN_LO = 1
STEWARD_WARN_MIN_HI = 60
STEWARD_MAX_AGE_MIN = 0                  # env/const default: off

# --- read-only observability: keeper-relay WRITES this file once per steward cycle
# so a host route/console can serve the steward's current status. It is a pure
# SERIALIZATION of state the cycle already has — writing it changes NOTHING about
# arming/warning/restart/extend, and a write failure is swallowed (never gates the
# cycle). Env-overridable (tests point it at a tmp path). Schema "steward-state.v1".
STEWARD_STATE_FILE = _hugpy_path("state", "steward-state.json", "steward-state.json")
STEWARD_STATE_SCHEMA = "steward-state.v1"

# --- outbound operator pinger: post to the operator's Discord when they're needed -
# The relay only ever READ the operator channel and typed at the KEEPER. This is
# the last side of the loop: an OUTBOUND ping to the operator's phone when an
# OPERATOR-RELEVANT event lands. It makes NO LLM call — it POSTs to the Discord
# bridge's <endpoint>/send (the same channel the relay reads). Deliberately
# conservative (this is the operator's phone): it fires for exactly four events —
# a new open ⚑ operator item, a new pending ⚖ proposal, a keeper reply to a
# non-keeper thread, and a console deploy failure — and stays silent for ordinary
# todos/requests/bookmarks, status flips, and keeper-authored non-reply comments.
# Detection REUSES the board `items` todo_watch already read (NO extra todo.json
# read) plus the deploy-failed mtime the lull/journal already fingerprint. First
# run seeds silently (no storm on deploy); pings dedup by stable event id, batch
# into ONE message per cooldown, and a send failure keeps events for next cycle.
OPPING_SEND_SUFFIX = "/send"   # POST <endpoint>/send {"content": "..."} — Discord bridge
OPPING_HTTP_TIMEOUT = 10       # per-attempt seconds for the ONE POST (+1 retry)
OPPING_TEXT_MAX = 100          # per-event snippet of item/comment text
OPPING_REASON_MAX = 120        # per-event deploy-failed reason
OPPING_MSG_MAX = 1200          # total message budget (server caps ~1500 — stay well under)
OPPING_SEEN_CAP = 500          # persisted delivered/seeded event-id ring; oldest fall off
OPPING_KNOWN_CAP = 1000        # persisted board-id ring for "newly appeared" detection

# --- board EVENT relay: ping the keeper PROMPTLY on decisions + done-transitions -
# The [board] watcher above pings on NEW items (now quiet-gated). But board
# TRANSITIONS — the operator ACCEPTING/DECLINING a proposal, or an item flipping
# to done — are exactly what a keeper is waiting on, so they deliver PROMPTLY:
# subject to the instantaneous idle gate ONLY, exempt from the t44/t45 quiet gate
# and the max-hold cap (a separate queue class, author=="board-event"). Detection
# REUSES the journal's per-item fingerprint map (state["todo_fp"]) as the "before"
# snapshot — the event watcher runs BEFORE journal_cycle advances it, so it reads
# last cycle's status without inventing a second differ. Two event classes:
#   1. proposal decision — a type=proposal item whose note carries an
#      [accepted]/[declined] prefix (PROP_MARK). One event per (id, decision):
#      accepted->declined re-fires (new token); a plain note/text edit does not.
#   2. done-transition — any non-bookmark item whose status changes to done from
#      open/doing (an item that APPEARS already-done never fires — the add watcher
#      owns new items). Type=operator dones are labelled distinctly (⚑).
# Dedup by stable event id in a persisted ring; first run seeds present decisions
# SILENTLY (no storm on deploy). SELF-SUPPRESSION: the keeper's own CLI writes
# (todo-cli) stamp a fresh {ts, ids, op} marker; an event whose id has a fresh
# marker is the keeper's own hand and is dropped (still marked seen, never echoed).
# A console-originated change leaves no marker and fires normally.
PROP_MARK = re.compile(r"^\s*\[(accepted|declined)\]", re.IGNORECASE)
# comms.ping marker (KEEPER-TASK-comms-ping): a NEW board item that is a ping —
# a high-priority `request` whose note is prefixed `[ping]`. Unlike other new
# items (which the quiet-gated add watcher owns), a ping delivers on the §9
# board-event lane: instant idle gate, exempt from the quiet gate / max-hold cap.
PING_MARK = re.compile(r"^\s*\[ping\]\s*", re.IGNORECASE)


def _is_ping(it):
    """A new board item that is a ping (type=request + note prefixed [ping])."""
    return (isinstance(it, dict) and it.get("type") == "request"
            and bool(PING_MARK.match(str(it.get("note") or ""))))


def _ping_fields(it):
    """(from, text, ref) for a ping item — sender from by/from, text is the item
    text, ref/board_ref carried in the note after the [ping] marker if present."""
    note = str(it.get("note") or "")
    body = PING_MARK.sub("", note).strip()
    frm = str(it.get("from") or it.get("by") or "").strip()
    ref = str(it.get("ref") or it.get("board_ref") or "").strip()
    return frm, (str(it.get("text") or body or "").strip()), ref
EVENTS_SEEN_CAP = 800          # persisted fired/seeded event-id ring; oldest fall off
EVENTS_SNIPPET = 80            # per-decision chars of proposal text quoted in the line
EVENTS_NOTIFY_MAX = 300        # target max chars of the injected [board] event line
TODO_CLI_MARKER_FILE = os.path.expanduser(
    "~/.local/state/todo-cli/last-write.json")   # env KEEPER_RELAY_TODO_CLI_MARKER
EVENT_SUPPRESS_WINDOW = 90     # seconds: trust a keeper-CLI marker at most this long

POLL_SECONDS = 15
TIMEOUT = 15

# --- session-channel source: a SECOND inbound channel, same pipeline ---------
# The #keeper-keeper Discord channel via the dev.hugpy.ai session API is a second
# surface the operator can reach the keeper from. It gets its OWN endpoint
# (derived from KEEPER_KEEPER_DISCORD_KEY), its OWN ~30s poll cadence, and its
# OWN persisted cursor (state["session_last_ts"], first sighting seeds at NOW so
# channel history is never replayed) — but everything downstream is the EXISTING
# machinery: classify() against the SAME operator allowlist (cfg["allow"], never
# forked), sanitize(), enqueue(), the idle-gated drain. One delivery pipeline,
# two sources. ECHO TRAP: the keeper's own posts come back in the poll with
# direction:"out" (author "keeper-keeper"); classify()'s direction=="in" gate
# drops them with a logged reason, exactly like the primary channel's
# out-echoes. Injecting for real is a SEPARATE opt-in
# (KEEPER_RELAY_SESSION_LIVE=1, independent of KEEPER_RELAY_LIVE) resolved
# per-item in drain(), so either source can go live without the other.
SESSION_ENDPOINT_BASE = "https://dev.hugpy.ai/api/discord/session"
SESSION_POLL_SECONDS = 30       # the session API's own cadence; >= the 15s cycle

MAX_LEN = 1500
TRUNC_MARK = " …[truncated by keeper-relay]"
MAX_QUEUE = 20
CONFIRM_TIMEOUT = 10.0
LAND_TIMEOUT = 3.0   # measured: the TUI reflects a send-keys -l in ~60ms
POLL_INTERVAL = 0.05

# --- attachment fetch: narrow which bytes may reach the keeper's disk --------
# The operator sends UI-bug screenshots that are load-bearing context. We fetch
# ONLY image attachments, ONLY from the two Discord CDN hosts, ONLY over https,
# ONLY with no redirects, and we name the local file OURSELVES — the remote
# filename is attacker-controlled and never touches the disk or the tag.
INBOX_DIR = "/home/ubuntu/relay-inbox"
ATTACH_MAX = 4                          # at most this many fetched per message
ATTACH_MAX_BYTES = 8 * 1024 * 1024      # hard cap; enforced while reading too
ATTACH_TIMEOUT = 15                     # per-fetch seconds
ATTACH_RETENTION_DAYS = 30
ATTACH_CHUNK = 64 * 1024
ATTACH_HOSTS = frozenset(["cdn.discordapp.com", "media.discordapp.net"])
# declared/served content-type -> (our extension, leading magic bytes).
# WEBP is special-cased in _magic_ok (RIFF....WEBP), so its magic here is unused.
ATTACH_TYPES = {
    "image/png":  ("png",  b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": ("jpg",  b"\xff\xd8\xff"),
    "image/gif":  ("gif",  b"GIF8"),
    "image/webp": ("webp", b"RIFF"),
}

# --- pane-shape constants, per KEEPER FACE -----------------------------------
#
# These describe what an IDLE keeper pane looks like. They were measured against
# a real Claude TUI and, until 2026-07-24, hardcoded to it — which silently broke
# every VM whose face is not claude.
#
# THE BUG THIS FIXES (operator: "hours will go by with nothing happening and
# nothing active and it's not a 'lull'"). On a hugpy-face VM the pane runs
# `opencode`, so idle_reason() hit `st["cmd"] != EXPECT_CMD` and returned
# "pane-not-running-claude(is:opencode)" on a pane that was demonstrably idle —
# not busy, no turn in flight, cursor sitting on the prompt. A lull could
# therefore NEVER be true there, and that one wrong constant cascaded:
#   idle never true -> the steward's warning can never be delivered
#     -> the queued-warning gate in _steward_warned returns early every cycle
#       -> the steward never fires -> no ~/.keeper-reset-fresh marker
#         -> keeper-launch.sh falls through to --continue and re-reads the WHOLE
#            transcript (observed: 1.65 MB spanning 7 relaunches).
# The reset amplified the cost it exists to cut, from one wrong string.
#
# Faces differ in every one of these, so they move together as a profile:
# fixing EXPECT_CMD alone would leave the glyph and busy-marker wrong and the
# pane would read as "cursor-not-on-prompt-line" instead. Measured on
# teststation 2026-07-24 against OpenCode 1.18.5.
KEEPER_FACES = {
    "claude": {
        "cmd": "claude",
        "busy": "esc to interrupt",   # footer text, present only mid-turn
        "glyph": "❯",                 # the input box's leading glyph
        "empty_col": 2,               # cursor_x when the box is empty ("❯ ")
    },
    "opencode": {
        "cmd": "opencode",
        "busy": "esc to interrupt",
        "glyph": "┃",                 # OpenCode boxes the input with a bar
        # OpenCode INDENTS its input box, so the empty-cursor column moves with
        # the layout (measured at 6 in one capture and 43 in another, same idle
        # pane). A fixed column is therefore the wrong model here: offset says
        # "this many columns after the glyph" and survives re-indentation.
        "empty_offset": 3,
    },
}


def _resolve_face(env=None):
    """Which keeper face this VM runs, from KEEPER_FACE. Unset/unknown -> claude,
    the historical default, so a claude VM's behaviour is byte-identical.

    Deliberately PURE: no tmux probe, no I/O. An earlier draft resolved the face
    by asking tmux what was in the pane at import time, which made the module's
    constants depend on live machine state — 49 tests flipped depending on what
    happened to be running. A module whose constants shift under it is not
    testable and not debuggable; the face is configuration, so it comes from
    config."""
    env = env if env is not None else os.environ
    name = (env.get("KEEPER_FACE") or "").strip().lower()
    return KEEPER_FACES.get(name, KEEPER_FACES["claude"])


_FACE = _resolve_face()
BUSY_MARKER = _FACE["busy"]
PROMPT_GLYPH = _FACE["glyph"]
EMPTY_COL = _FACE.get("empty_col", 2)  # offset-model faces do not use this
EXPECT_CMD = _FACE["cmd"]

# Whole ANSI sequences: CSI (ESC[...), OSC (ESC]... BEL/ST) and 2-char escapes.
# Dropping the lone ESC as a control char is enough for SAFETY (the residue is
# inert without it), but it leaves visible "[31m" junk in the keeper's prompt --
# so match and remove the entire sequence instead.
ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]"      # CSI  ESC [ params intermediates final
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC  ESC ] ... BEL | ST
    r"|[@-Z\\-_])")                     # two-character escapes


# ---------------------------------------------------------------- config


def load_env_file(path=ENV_FILE):
    """Parse KEY=VALUE from the 0600 env file. Values never leave this process."""
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _relay_seconds(raw, default):
    """env seconds -> float; any missing/negative/garbage value -> the default."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return v if v >= 0 else float(default)


def _relay_seconds_source(raw):
    """'env' when raw parses to a usable (non-negative) seconds value, else
    'default' — mirrors _relay_seconds's fallback so cmd_status can name which
    of the two supplied the quiet window (the bookmark override, when live, wins
    over both and is resolved later from state)."""
    try:
        return "env" if float(raw) >= 0 else "default"
    except (TypeError, ValueError):
        return "default"


def _deploy_signals_on(path):
    """True when the console-ui deploy-signal directory holding `path` exists. A
    missing directory means this VM runs no UI deploy pipeline -> the deploy
    candidate/ping detectors skip SILENTLY (feature-off). On blackbird the signals
    dir exists, so this is always True and the deploy behaviour is unchanged."""
    try:
        return os.path.isdir(os.path.dirname(path))
    except (OSError, TypeError):
        return False


def config():
    env = load_env_file()

    def get(key, default=None):
        return os.environ.get(key) or env.get(key) or default

    # STATION_DISCORD_API is the station-neutral name (t86 de-site-ify);
    # BLACKBIRD_DISCORD_API is the legacy name every deployed .env still uses —
    # read second, warn, keep working. New provisioning should use STATION_.
    ep = get("STATION_DISCORD_API")
    if not ep:
        ep = get("BLACKBIRD_DISCORD_API")
        if ep:
            log("config: BLACKBIRD_DISCORD_API is deprecated — rename it "
                "STATION_DISCORD_API (same value; old name keeps working)")
    # NO endpoint -> OFFLINE MODE, not a fatal error (2026-07-24).
    #
    # This used to sys.exit here, which conflated "cannot talk to Discord" with
    # "cannot run at all". It cannot: the endpoint is read at exactly two sites,
    # fetch() and _opping_send(), and cycle() runs three subsystems BEFORE the
    # first of them that never touch it —
    #     todo_watch    board notices      (local ~/todo.json)
    #     opping_cycle  detection stage    (send is separately gated below)
    #     steward_cycle transcript hygiene (local inventory -> steward-state.json)
    # steward_cycle in particular writes the file the console's steward drawer
    # renders, even when the feature is OFF. A VM with no Discord wiring was
    # therefore denied a working steward drawer for no reason — and, because the
    # unit restarts on failure, sat in a crash loop instead of doing the local
    # work it was perfectly capable of.
    #
    # Offline mode keeps the process up and the local subsystems running; the two
    # network paths degrade to a logged no-op (see fetch/_opping_send). Nothing
    # about a WIRED relay changes: endpoint present -> byte-identical behaviour.
    if not ep:
        log("config: no STATION_DISCORD_API (or legacy BLACKBIRD_DISCORD_API) "
            "in env or %s — OFFLINE MODE: local subsystems (board watch, "
            "steward) run; Discord fetch/send are no-ops" % ENV_FILE)
    allow = get("KEEPER_RELAY_ALLOW", "devinthadude_")
    # Session-channel source (the SECOND source): EXISTS only when its key (or a
    # full endpoint override) is present; absent -> session_endpoint None and
    # every session code path is inert. The key is spliced into the URL here and
    # NEVER logged/printed — fetch errors report the exception type only (never
    # the URL) and cmd_status masks the endpoint, the same discipline the primary
    # token already gets. Note this is INDEPENDENT of the offline mode above: a
    # station with no STATION_DISCORD_API but a session key still polls the
    # session channel (fetch() resolves its guard per-endpoint, not per-cfg).
    # Station-derived FIRST, keeper's historical literal as fallback — see
    # _session_key_env_names. Records which name won so a station operator can tell
    # "no key" from "key under a name nobody reads", which was the whole defect.
    skey = skey_name = None
    for _n in _session_key_env_names(_resolve_station()):
        _v = get(_n)
        if _v:
            skey, skey_name = _v, _n
            break
    sep = get("KEEPER_SESSION_DISCORD_API") or (
        "%s/%s" % (SESSION_ENDPOINT_BASE, skey) if skey else None)
    if skey_name:
        log("config: session key from %s" % skey_name)
    return {
        # "" (never None) so every existing `cfg["endpoint"]` string op is safe.
        "endpoint": ep.rstrip("/") if ep else "",
        "session_endpoint": sep.rstrip("/") if sep else None,
        # Session live is a SEPARATE opt-in: "1" == inject session messages for
        # real. Deliberately independent of KEEPER_RELAY_LIVE (either source can
        # go live alone); unset -> the session source stays DRY-RUN.
        "session_live": get("KEEPER_RELAY_SESSION_LIVE", "") == "1",
        "allow": frozenset(a.strip() for a in allow.split(",") if a.strip()),
        "socket": get("KEEPER_RELAY_SOCKET", "console"),
        "target": get("KEEPER_RELAY_TARGET", "keeper:0.0"),
        "live": get("KEEPER_RELAY_LIVE", "") == "1",
        "inbox": get("KEEPER_RELAY_INBOX", INBOX_DIR),
        "todo_path": get("KEEPER_RELAY_TODO", TODO_FILE),
        # default on; only the literal "off" disables the board watcher.
        "todo_notify": get("KEEPER_RELAY_TODO_NOTIFY", "on").strip().lower() != "off",
        # board quiet gate: hold [board] notices until the keeper has been inactive
        # this long (seconds). Board class only; everything else drains as today.
        # This is the env-or-default layer; the 'cfg-relay' bookmark can override it
        # live (resolved per cycle in _effective_board_quiet). _source records which
        # of env/default won here so cmd_status can name the effective source.
        "board_quiet_sec": _relay_seconds(
            get("KEEPER_RELAY_BOARD_QUIET_SECS"), BOARD_QUIET_SECS),
        "board_quiet_source": _relay_seconds_source(
            get("KEEPER_RELAY_BOARD_QUIET_SECS")),
        # board starvation cap: force a quiet-held [board] item out once it has been
        # queued longer than this (seconds); 0 = no cap. Bookmark can override live.
        "board_max_hold_sec": _relay_seconds(
            get("KEEPER_RELAY_BOARD_MAX_HOLD_SECS"), BOARD_MAX_HOLD_SECS),
        "board_max_hold_source": _relay_seconds_source(
            get("KEEPER_RELAY_BOARD_MAX_HOLD_SECS")),
        # --- lull relay (default on; only the literal "off" disables it) ---
        "lull": get("KEEPER_RELAY_LULL", "on").strip().lower() != "off",
        "lull_stale_sec": _lull_minutes(get("KEEPER_RELAY_LULL_STALE_MIN"), 30),
        "lull_wait_sec": _lull_minutes(get("KEEPER_RELAY_LULL_MAX_WAIT_MIN"), 10),
        "lull_cooldown_sec": _lull_minutes(get("KEEPER_RELAY_LULL_COOLDOWN_MIN"), 15),
        "hugpy_api": get("HUGPY_API", HUGPY_API),
        "hugpy_brain": get("HUGPY_BRAIN", HUGPY_BRAIN),
        "bugreport_path": BUGREPORT_FILE,
        # console-ui deploy signals: env-overridable, STATION-derived default. A
        # non-existent signals dir == this VM has no UI deploy pipeline (feature-off,
        # see _deploy_signals_on). On blackbird the dir exists -> behaviour unchanged.
        "deploy_failed_path": get("KEEPER_RELAY_DEPLOY_FAILED", DEPLOY_FAILED_FILE),
        # --- board event journal (append-only history; NO enable/disable knob) ---
        "history_path": get("KEEPER_RELAY_TODO_HISTORY", TODO_HISTORY_FILE),
        "last_deploy_path": get("KEEPER_RELAY_LAST_DEPLOY", LAST_DEPLOY_FILE),
        # fleet-common handoff mirror base (STATION-namespaced); env-overridable.
        "handoff_shared_dir": get(
            "KEEPER_RELAY_HANDOFF_SHARED_DIR", HANDOFF_SHARED_DIR),
        # --- session steward. The WHOLE feature is OFF unless the operator opts
        #     in with the literal "on"; when off it is fully inert (ships changing
        #     nothing). Even on, the restart action is DRY-RUN unless _RESTART is
        #     the literal "live". ---
        "steward": get("KEEPER_RELAY_STEWARD", "off").strip().lower() == "on",
        "steward_restart": "live" if get(
            "KEEPER_RELAY_STEWARD_RESTART", "dry").strip().lower() == "live"
            else "dry",
        "steward_cadence_sec": _lull_minutes(
            get("KEEPER_RELAY_STEWARD_CADENCE_MIN"), STEWARD_CADENCE_MIN),
        "steward_warn_lead_sec": _lull_minutes(
            get("KEEPER_RELAY_STEWARD_WARN_LEAD_MIN"), STEWARD_WARN_LEAD_MIN),
        "steward_max_extend_sec": _lull_minutes(
            get("KEEPER_RELAY_STEWARD_MAX_EXTEND_MIN"), STEWARD_MAX_EXTEND_MIN),
        "steward_threshold_bytes": _steward_bytes(
            get("KEEPER_RELAY_STEWARD_THRESHOLD_BYTES"), STEWARD_THRESHOLD_BYTES),
        # The RESET BAR (operator 2026-07-29/30). Reuses _steward_bytes only as a
        # "non-negative int or fall back" parser — the unit is tokens.
        "steward_threshold_tokens": _steward_bytes(
            get("KEEPER_RELAY_STEWARD_THRESHOLD_TOKENS"), STEWARD_THRESHOLD_TOKENS),
        "steward_max_age_sec": _lull_minutes(
            get("KEEPER_RELAY_STEWARD_MAX_AGE_MIN"), STEWARD_MAX_AGE_MIN),
        "steward_floor_bytes": _steward_bytes(
            get("KEEPER_RELAY_STEWARD_FLOOR_BYTES"), STEWARD_FLOOR_BYTES),
        "steward_transcript_dir": get(
            "KEEPER_RELAY_STEWARD_TRANSCRIPT_DIR", STEWARD_TRANSCRIPT_DIR),
        # Claude Code jobs dir: each <shortid>/state.json's sessionId names a
        # background-job .jsonl to EXCLUDE from the keeper's growth sum (t79/t81).
        # Overridable for tests/other layouts; an absent dir == exclude nothing.
        "steward_jobs_dir": get(
            "KEEPER_RELAY_STEWARD_JOBS_DIR", STEWARD_JOBS_DIR),
        # Path to the console-writable live config (~/steward.json). Overridable
        # for tests/other layouts; keeper-relay only READS it, never writes it.
        "steward_config_path": get(
            "KEEPER_RELAY_STEWARD_CONFIG", STEWARD_CONFIG_FILE),
        # Path to the read-only steward status file keeper-relay writes each cycle
        # (~/steward-state.json). Overridable for tests; observability only.
        "steward_state_path": get(
            "KEEPER_RELAY_STEWARD_STATE", STEWARD_STATE_FILE),
        # --- outbound operator pinger. "on" (default) detects+POSTs; "dry" detects
        #     + audits + logs but never POSTs; "off" is fully inert. ---
        "operator_ping": (lambda v: v if v in ("off", "dry") else "on")(
            get("KEEPER_RELAY_OPERATOR_PING", "on").strip().lower()),
        "operator_ping_cooldown_sec": _lull_minutes(
            get("KEEPER_RELAY_OPERATOR_PING_COOLDOWN_MIN"), 5),
        # --- board EVENT relay (default on; only the literal "off" disables it).
        #     Detects proposal decisions + done-transitions and delivers them
        #     promptly (idle gate only). The keeper's own CLI transitions are
        #     suppressed via the todo-cli coordination marker. ---
        "board_events": get(
            "KEEPER_RELAY_BOARD_EVENTS", "on").strip().lower() != "off",
        "events_marker_path": get(
            "KEEPER_RELAY_TODO_CLI_MARKER", TODO_CLI_MARKER_FILE),
    }


# ---------------------------------------------------------------- state


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return None  # signals first run -> caller starts from NOW
    st.setdefault("last_ts", 0.0)
    st.setdefault("queue", [])
    return st


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_FILE)  # atomic: a crash mid-write can't corrupt the cursor


def audit(rec):
    """Append-only JSONL. Never records raw content — hash + short prefix only."""
    rec["audit_ts"] = time.time()
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError as exc:
        log("audit write failed: %s" % exc)


def log(msg):
    sys.stderr.write("[keeper-relay] %s\n" % msg)
    sys.stderr.flush()


def digest(text):
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------- sanitize


def sanitize(raw):
    """Make text safe to hand to `tmux send-keys -l`.

    A newline is the dangerous one: send-keys -l replays it as a RETURN, which
    would submit early and let the remainder run as its own input line. So every
    newline/CR/tab collapses to a space, and every other control/format char is
    removed outright (Cc = C0/C1 controls incl. ESC; Cf = zero-width joiners and
    the bidi overrides used for trojan-source-style display spoofing).

    Returns (text, notes). text == "" means DROP.
    """
    notes = []
    if not isinstance(raw, str):
        return "", ["content-not-a-string"]

    # Normalise first: NFC folds decomposed lookalikes into canonical form so the
    # operator sees what the keeper sees.
    text = unicodedata.normalize("NFC", raw)

    text, n_ansi = ANSI_RE.subn("", text)  # whole sequences, before the char pass
    if n_ansi:
        notes.append("stripped-%d-ansi-sequences" % n_ansi)

    out = []
    stripped_ctl = 0
    for ch in text:
        if ch in ("\n", "\r", "\t", "\v", "\f", " ", " "):
            out.append(" ")  # line breaks -> spaces (never a bare RETURN)
            stripped_ctl += 1
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co"):
            stripped_ctl += 1  # controls, format/bidi, surrogates, private-use
            continue
        out.append(ch)
    text = "".join(out)

    if stripped_ctl:
        notes.append("stripped-%d-control-chars" % stripped_ctl)

    text = " ".join(text.split())  # collapse runs of whitespace, trim ends

    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - len(TRUNC_MARK)] + TRUNC_MARK
        notes.append("truncated-to-%d" % MAX_LEN)

    if not text:
        notes.append("empty-after-sanitize")
    return text, notes


def render(author, text, paths=None, n_skipped=0):
    """Prefix provenance so the keeper can see this text came from the relay.

    Each fetched image contributes an [attachment: <local path>] tag; the path is
    OURS — a fetched-and-verified file under relay-inbox that the keeper can Read.
    Any attachments we skipped (bad host, wrong type, too big, magic mismatch,
    fetch error, over the per-message cap) contribute one aggregate count with no
    reasons — the reasons live in the audit log. Remote strings (the CDN url, the
    sender's filename) NEVER appear here; every character below is our own.
    """
    tag = "[relay:%s] " % author
    for p in (paths or []):
        tag += "[attachment: %s] " % p
    if n_skipped:
        tag += "[+%d attachment(s) skipped] " % n_skipped
    return tag + text


# ---------------------------------------------------------------- filter


def classify(msg, allow):
    """Fail-closed allowlist. Returns (ok, reason).

    Anything not positively proven to be an allowlisted operator message is
    dropped: our own 'out' echoes, other humans (e.g. ClosetShark), bots,
    unknown/missing authors, malformed records.
    """
    if not isinstance(msg, dict):
        return False, "not-an-object"
    if "direction" not in msg or "author" not in msg or "content" not in msg:
        return False, "missing-required-fields"
    direction = msg.get("direction")
    if direction != "in":
        # 'out' is our own post echoing back — injecting it would loop the keeper
        # against itself.
        return False, "direction-not-in(%s)" % (direction,)
    author = msg.get("author")
    if not isinstance(author, str) or not author:
        return False, "author-missing-or-not-a-string"
    if author not in allow:
        return False, "author-not-allowlisted"
    ts = msg.get("ts")
    if not isinstance(ts, (int, float)):
        return False, "ts-missing-or-not-numeric"
    return True, "allowlisted"


# ---------------------------------------------------------------- attachments


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse EVERY redirect. Discord's CDN serves the image bytes directly, so
    a redirect is never legitimate here — and following one would be a chance to
    bounce us off the allowlisted host onto anywhere. Raise instead of follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # req.full_url, not newurl: never surface the redirect target (it, like
        # the CDN url, may carry a signed query string) even into an exception.
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect refused by keeper-relay", headers, fp)


def _magic_ok(ctype, head):
    """Do the leading bytes match what the content type claims to be?"""
    if ctype == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ctype == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if ctype == "image/gif":
        return head.startswith(b"GIF8")
    if ctype == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


def _att_url_ok(url):
    """Return (host, None) for an allowed https CDN url, else (None, reason)."""
    if not isinstance(url, str) or not url:
        return None, "url-missing-or-not-a-string"
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None, "url-unparseable"
    if parts.scheme != "https":
        return None, "scheme-not-https(%s)" % (parts.scheme,)
    host = (parts.hostname or "").lower()
    if host not in ATTACH_HOSTS:
        return None, "host-not-allowlisted"
    return host, None


def _unique_path(dest_dir, base, n, ext):
    """Our local name — never the remote filename. Never overwrite: if the final
    path already exists, add a numeric suffix rather than clobber it."""
    final = os.path.join(dest_dir, "%s-%d.%s" % (base, n, ext))
    if not os.path.exists(final):
        return final
    i = 1
    while True:
        cand = os.path.join(dest_dir, "%s-%d-%d.%s" % (base, n, i, ext))
        if not os.path.exists(cand):
            return cand
        i += 1


def fetch_attachment(att, dest_dir, base, n):
    """Validate + fetch ONE image attachment, fail-closed at every gate.

    Returns (final_path, sha256, nbytes, None) on success, else
    (None, None, None, reason). A failed gate skips THIS attachment only; it is
    never allowed to block the message text. Nothing remote-controlled (the url,
    the sender's filename) ever names the local file or reaches the tag.
    """
    if not isinstance(att, dict):
        return None, None, None, "attachment-not-an-object"

    host, reason = _att_url_ok(att.get("url"))
    if reason:
        return None, None, None, reason

    declared = att.get("content_type")
    if declared not in ATTACH_TYPES:
        return None, None, None, "declared-type-not-allowed(%s)" % (declared,)
    ext, _magic = ATTACH_TYPES[declared]

    size = att.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None, None, None, "size-missing-or-not-int"
    if size > ATTACH_MAX_BYTES:
        # Reject on the declaration BEFORE opening the body — cheap first line.
        return None, None, None, "declared-size-over-cap(%d)" % size

    # Redirects refused via a dedicated opener (see _NoRedirectHandler).
    # The User-Agent is REQUIRED, not cosmetic: Discord's CDN answers 403 to
    # python's default agent (verified live 2026-07-17 — same signed URL, 403
    # bare vs 200 with a UA), so without this header every real fetch fails
    # while every mocked test passes.
    opener = urllib.request.build_opener(_NoRedirectHandler)
    req = urllib.request.Request(att["url"], headers={
        "Accept": "image/*", "User-Agent": "keeper-relay/1.0"})
    try:
        resp = opener.open(req, timeout=ATTACH_TIMEOUT)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return None, None, None, "fetch-error:%s" % type(exc).__name__

    try:
        served = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if served != declared:
            # Both the declared field AND the served header must agree on the
            # SAME allowed type; declared is already known-allowed above.
            return None, None, None, "served-type-mismatch(declared=%s)" % declared

        # Read in chunks and enforce the cap regardless of the declaration —
        # never trust `size`. Abort the moment we cross the cap; bytes discarded.
        buf = bytearray()
        over = False
        while True:
            chunk = resp.read(ATTACH_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > ATTACH_MAX_BYTES:
                over = True
                break
        if over:
            return None, None, None, "stream-exceeded-cap"
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return None, None, None, "read-error:%s" % type(exc).__name__
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if not _magic_ok(declared, bytes(buf[:16])):
        # Content that lied about being an image. Skip and drop the bytes.
        return None, None, None, "magic-mismatch"

    data = bytes(buf)
    final = _unique_path(dest_dir, base, n, ext)
    tmp = final + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, final)  # atomic: a reader never sees a partial file
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None, None, None, "write-error:%s" % type(exc).__name__
    return final, hashlib.sha256(data).hexdigest(), len(data), None


def prune_inbox(dest_dir, now=None):
    """Delete OUR relay-* files older than the retention window. Quiet + fail-
    closed. Never touches a file whose name does not start with 'relay-' — the
    keeper saves its own files in this same directory. Never raises."""
    now = time.time() if now is None else now
    cutoff = now - ATTACH_RETENTION_DAYS * 86400
    try:
        names = os.listdir(dest_dir)
    except OSError:
        return
    for name in names:
        if not name.startswith("relay-"):
            continue
        p = os.path.join(dest_dir, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            continue


def fetch_attachments(cfg, atts, ts, author):
    """Fetch up to ATTACH_MAX image attachments for an ACCEPTED message.

    Returns (local_paths, n_skipped). Self-contained and fail-closed: ANY
    exception in the whole stage degrades to ([], len(atts)) — i.e. the old
    "count them, fetch nothing" behavior — logged + audited, so the message text
    still flows. Only ever called on the accept path, so a dropped/non-allowlisted
    message never triggers a fetch (no fetch-as-side-channel).

    Latency note: a slow CDN can stall this by at most ATTACH_MAX * ATTACH_TIMEOUT
    (4 x 15s = 60s) for one message. Accepted — screenshots are worth the wait.
    """
    if not atts:
        return [], 0
    dest_dir = cfg.get("inbox") or INBOX_DIR
    ts_int = int(ts) if isinstance(ts, (int, float)) else 0
    base = "relay-%s-%d" % (time.strftime("%Y%m%d-%H%M%S", time.gmtime()), ts_int)
    paths = []
    skipped = 0
    try:
        os.makedirs(dest_dir, exist_ok=True)
        for n, att in enumerate(atts):
            if n >= ATTACH_MAX:
                skipped += 1
                audit({"ts": ts, "author": author, "decision": "attach-skip",
                       "reason": "over-max-per-message(%d)" % ATTACH_MAX, "n": n})
                continue
            final, sha, nbytes, reason = fetch_attachment(att, dest_dir, base, n)
            if reason:
                skipped += 1
                audit({"ts": ts, "author": author, "decision": "attach-skip",
                       "reason": reason, "n": n})
                continue
            paths.append(final)
            audit({"ts": ts, "author": author, "decision": "attach-fetch",
                   "reason": "fetched", "bytes": nbytes, "sha256": sha, "n": n})
    except Exception as exc:  # degrade whole stage; text must never be blocked
        log("attachment stage failed: %s" % type(exc).__name__)
        audit({"ts": ts, "author": author, "decision": "attach-skip",
               "reason": "stage-exception:%s" % type(exc).__name__, "n": -1})
        return [], len(atts)
    return paths, skipped


# ---------------------------------------------------------------- tmux


def tmux(cfg, *args, want_out=True):
    """Run tmux via argv. Never a shell string; nothing is ever interpolated."""
    cmd = ["tmux", "-L", cfg["socket"]] + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "tmux-exec-failed:%s" % exc
    if p.returncode != 0:
        return None, "tmux-rc=%d:%s" % (p.returncode, (p.stderr or "").strip()[:120])
    return (p.stdout if want_out else ""), None


def pane_state(cfg):
    """Read the pane. Returns (state_dict, error). Never writes."""
    fmt = "\t".join([
        "#{pane_current_command}", "#{pane_in_mode}", "#{cursor_x}",
        "#{cursor_y}", "#{alternate_on}",
    ])
    out, err = tmux(cfg, "display-message", "-p", "-t", cfg["target"], fmt)
    if err:
        return None, err
    parts = out.rstrip("\n").split("\t")
    if len(parts) != 5:
        return None, "unparseable-display-message"
    try:
        st = {
            "cmd": parts[0],
            "in_mode": int(parts[1] or 0),
            "cursor_x": int(parts[2] or 0),
            "cursor_y": int(parts[3] or 0),
            "alt": int(parts[4] or 0),
        }
    except ValueError:
        return None, "unparseable-display-message-ints"
    cap, err = tmux(cfg, "capture-pane", "-p", "-t", cfg["target"])
    if err:
        return None, err
    st["lines"] = cap.split("\n")
    return st, None


def box_empty(st):
    """True only if the input box holds no typed text.

    cursor_x alone is NOT enough: a long payload wraps, and a wrapped line can
    put the cursor back at column 2 on a continuation row. Continuation rows do
    not carry the "❯", so we require BOTH the empty column AND that the cursor
    is sitting on the prompt row itself.

    (The idle box renders a dim "Try ..." placeholder, so the row is not blank
    even when empty -- which is why this reads the CURSOR, not the row's text.)
    """
    if not (0 <= st["cursor_y"] < len(st["lines"])):
        return False
    row = st["lines"][st["cursor_y"]]
    if not row.lstrip().startswith(PROMPT_GLYPH):
        return False
    if _FACE.get("empty_offset") is not None:
        # Offset model (OpenCode): the box is INDENTED, so the empty column
        # moves with the layout and a fixed EMPTY_COL is meaningless. Measure
        # from the glyph instead. Anything further right than the offset means
        # real typed text sits in the box.
        g = row.find(PROMPT_GLYPH)
        if g < 0:
            return False
        return st["cursor_x"] <= g + _FACE["empty_offset"]
    return st["cursor_x"] == EMPTY_COL


def wait_for(cfg, pred, timeout):
    """Poll the pane until pred(state) holds. Returns (ok, last_error)."""
    deadline = time.time() + timeout
    err = "timeout"
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        st, e = pane_state(cfg)
        if e:
            err = e
            continue
        if pred(st):
            return True, None
    return False, err


def idle_reason(st):
    """Return None if the pane is safe to type into, else why it is not.

    Ambiguity always resolves to 'not idle' -> the message queues instead.
    """
    if st["cmd"] != EXPECT_CMD:
        # The TUI is gone and we're probably at a bash prompt. Typing here would
        # hand an operator's sentence to the SHELL as a command. Hard stop.
        return "pane-not-running-%s(is:%s)" % (EXPECT_CMD, st["cmd"])
    if st["alt"] != 1:
        return "not-on-alternate-screen(tui-absent)"
    if st["in_mode"] != 0:
        # copy-mode: send-keys hits the copy keytable, not the app.
        return "pane-in-copy-mode"
    if any(BUSY_MARKER in ln for ln in st["lines"]):
        return "busy(turn-in-flight)"
    if not (0 <= st["cursor_y"] < len(st["lines"])):
        return "cursor-off-pane"
    if not st["lines"][st["cursor_y"]].lstrip().startswith(PROMPT_GLYPH):
        return "cursor-not-on-prompt-line"
    if not box_empty(st):
        # Someone typed into the box and hasn't hit Enter. Appending would
        # splice our text onto their half-written line and submit both.
        return "input-box-not-empty(cursor_x=%d)" % st["cursor_x"]
    return None


def clear_box(cfg):
    """C-u the input box after a failed inject, to unwedge the relay.

    Only ever called on text WE typed into a box we had just confirmed empty.
    """
    _, err = tmux(cfg, "send-keys", "-t", cfg["target"], "C-u", want_out=False)
    if err:
        log("box cleanup failed: %s" % err)
    return err is None


def inject(cfg, text):
    """Type text and submit it. Returns (confirmed, reason).

    Literal text and RETURN are two separate send-keys calls: -l replays the
    payload verbatim (never as key names), and only our own Enter submits.
    """
    _, err = tmux(cfg, "send-keys", "-t", cfg["target"], "-l", "--", text,
                  want_out=False)
    if err:
        return False, "send-literal-failed:%s" % err

    # The TUI is a separate process and renders asynchronously (~60ms measured),
    # so poll for the literal to land rather than reading once. Reading too early
    # sees an empty box and would strand the text unsent.
    landed, err = wait_for(cfg, lambda st: not box_empty(st), LAND_TIMEOUT)
    if not landed:
        # Nothing visibly landed, so there should be nothing to clean up.
        return False, "literal-did-not-land(%s)" % err

    _, err = tmux(cfg, "send-keys", "-t", cfg["target"], "Enter", want_out=False)
    if err:
        clear_box(cfg)
        return False, "send-enter-failed:%s" % err

    # Confirm consumption: the box must clear again.
    ok, err = wait_for(cfg, box_empty, CONFIRM_TIMEOUT)
    if ok:
        return True, "consumed"
    # Our text is stranded in the box. Left there it would both sit in front of
    # the operator unsent AND wedge the relay forever (the idle check would see a
    # non-empty box every cycle). We typed it into a box we had just confirmed
    # empty, so it is ours to clear.
    clear_box(cfg)
    return False, "unconfirmed-box-did-not-clear(%s),cleared" % err


# ---------------------------------------------------------------- poll


def fetch(cfg, since, endpoint=None):
    """GET new messages. Errors are data; this never raises.

    `endpoint` lets the session source poll ITS channel through this same code
    path (default: the primary endpoint) — one fetch shape, one error
    discipline, two sources.
    """
    # Offline mode (no endpoint configured): report it as an ordinary error token
    # so intake() handles it on its existing path — leave the queue alone, retry
    # next cycle. NOT an exception, and deliberately not a bare "" URL request.
    # The guard is on the RESOLVED endpoint, not on cfg["endpoint"]: a station
    # with no STATION_DISCORD_API can still have a session endpoint, and that
    # source must not be switched off by the primary's absence.
    ep = endpoint or cfg.get("endpoint")
    if not ep:
        return None, "offline-no-endpoint"
    url = "%s/messages?since=%s" % (ep, since)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # The status code is diagnosis gold (401 rotated token vs 5xx upstream
        # blip) and carries no secret; the URL still never leaves this frame.
        return None, "HTTPError:%s" % exc.code
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return None, type(exc).__name__  # never echo the URL: it carries the token
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return None, "unexpected-payload-shape"
    return body["messages"], None


# ---------------------------------------------------------------- todo board


def enqueue(state, item):
    """Append one item to the shared inject queue, evicting + auditing the oldest
    on overflow. Shared by the Discord intake and the board watcher so both get
    the same at-most-once idle-gated drain with a single overflow policy."""
    if len(state["queue"]) >= MAX_QUEUE:
        dropped = state["queue"].pop(0)
        audit({"ts": dropped.get("ts"), "author": dropped.get("author"),
               "decision": "drop", "reason": "queue-overflow(max=%d)" % MAX_QUEUE,
               "queued": False, "sha256": dropped.get("sha256"),
               "prefix": dropped.get("prefix", "")})
    state["queue"].append(item)


def read_todo(path):
    """Load ~/todo.json. Returns (items, None) or (None, reason). Never raises.

    A malformed/half-written/absent file is DATA, not a crash: the console may be
    mid-write. The caller skips the tick on any reason.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, type(exc).__name__
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None, "unexpected-shape"
    return data["items"], None


def _todo_id(it):
    """A usable string id, or None for a record we can't track."""
    if not isinstance(it, dict):
        return None
    iid = it.get("id")
    return iid if isinstance(iid, str) and iid else None


def _cap_seen(ids):
    """Keep the most-recent TODO_SEEN_CAP ids; older ones fall off the front."""
    return ids[-TODO_SEEN_CAP:] if len(ids) > TODO_SEEN_CAP else ids


def render_board(items):
    """One-line board notice — our own text only, every field sanitize()d.

    Format (matches render()'s "our characters only" discipline):
        [board] +<type> <id> "<=80 chars…" · +<type> <id> "…" — check ~/todo.json
    The type/id/text all come from a file anyone can write, so all three go
    through sanitize() (no newline can splice a second command, no ANSI/control
    can reshape the line). Overflows past ~TODO_NOTIFY_MAX chars are summarised
    with a trailing "(+N more)" rather than typed in full.
    """
    prefix = "[board] "
    suffix = " — check ~/todo.json"
    budget = TODO_NOTIFY_MAX - len(prefix) - len(suffix)
    parts = []
    remaining = 0
    for idx, it in enumerate(items):
        iid = sanitize(str(it.get("id") or "?"))[0] or "?"
        typ = sanitize(str(it.get("type") or "item"))[0] or "item"
        snippet = sanitize(it.get("text") or "")[0]
        if len(snippet) > TODO_SNIPPET:
            snippet = snippet[:TODO_SNIPPET] + "…"
        part = '+%s %s "%s"' % (typ, iid, snippet)
        sep = " · " if parts else ""
        # Always keep the first part; stop once another would blow the budget so
        # there is room for the "(+N more)" tail and the suffix.
        if parts and len(prefix) + _joined_len(parts) + len(sep) + len(part) > budget:
            remaining = len(items) - idx
            break
        parts.append(part)
    line = prefix + " · ".join(parts)
    if remaining:
        line += " (+%d more)" % remaining
    return line + suffix


def _joined_len(parts):
    """Length of ' · '.join(parts) without building the string."""
    return sum(len(p) for p in parts) + 3 * max(0, len(parts) - 1)


def render_peer_msgs(entries):
    """One-line [peer-msg] notice — our own text only, every field sanitize()d.

    Format (render_board's "our characters only" discipline, message flavor):
        [peer-msg] ✉ b-3 from blackbird-keeper "text…" · ✉ reply on t97 by
        keeper-keeper "…" — ✉ messages tab / ~/todo.json
    `entries` is [{"id","from","text","reply":bool}] in detection order; a
    reply names the item it landed on instead of claiming to be a new item.
    Overflow past ~PEER_NOTIFY_MAX is summarised with "(+N more)".
    """
    prefix = "[peer-msg] "
    suffix = " — ✉ messages tab / ~/todo.json"
    budget = PEER_NOTIFY_MAX - len(prefix) - len(suffix)
    parts = []
    remaining = 0
    for idx, e in enumerate(entries):
        iid = sanitize(str(e.get("id") or "?"))[0] or "?"
        who = sanitize(str(e.get("from") or "?"))[0] or "?"
        snippet = sanitize(e.get("text") or "")[0]
        if len(snippet) > PEER_SNIPPET:
            snippet = snippet[:PEER_SNIPPET] + "…"
        part = ('✉ reply on %s by %s "%s"' if e.get("reply")
                else '✉ %s from %s "%s"') % (iid, who, snippet)
        sep = " · " if parts else ""
        if parts and len(prefix) + _joined_len(parts) + len(sep) + len(part) > budget:
            remaining = len(entries) - idx
            break
        parts.append(part)
    line = prefix + " · ".join(parts)
    if remaining:
        line += " (+%d more)" % remaining
    return line + suffix


def todo_watch(cfg, state):
    """Notice new ~/todo.json items and enqueue ONE batched [board] notice.

    Reuses the exact same queue + idle gate + inject path as operator messages:
    the notice is deferred (never dropped) while a turn is in flight, and drain()
    types at most one line per tick only into a confirmed-idle pane. First run
    (no persisted todo_seen) seeds every current id and injects NOTHING, so a
    deploy never storms the keeper. Notifies for new ids the keeper didn't
    write: by != "keeper" AND via != "keeper" (`by` = who asked, `via` = who
    wrote; the keeper stamps via:"keeper" on its own board writes so filing a
    request on the operator's behalf doesn't self-echo).
    """
    items, err = read_todo(cfg.get("todo_path") or TODO_FILE)
    if err:
        # Skip silently; log ONCE per consecutive failure streak (the console may
        # be mid-write). Never audit-spam.
        if not state.get("todo_read_failing"):
            log("todo read failed (%s) — skipping until it parses" % err)
            state["todo_read_failing"] = True
            save_state(state)
        return
    if state.get("todo_read_failing"):
        state["todo_read_failing"] = False
        save_state(state)

    # RELAY CONFIG (board t45 + keeper starvation cap): the reserved 'cfg-relay'
    # bookmark can retune the quiet window AND the max-hold cap live. Refresh the
    # overrides from THIS read every cycle (no second file read), regardless of the
    # notify gate below — a settings knob is not a notification. Effect lands within
    # one tick; a bad note/field audits once; the resolved windows persist to state.
    _cfg_relay_cycle(cfg, state, items)

    # BOARD EVENTS (decisions + done-transitions): detect from this SAME read and
    # BEFORE journal_cycle — the event watcher reuses state["todo_fp"] as the
    # pre-change snapshot, so it must run while that map still holds LAST cycle's
    # fingerprints. Own try/except so a fault here can't stall the journal/notify.
    try:
        _events_detect_board(cfg, state, items)
    except Exception as exc:
        log("board-event detect error (%s) — skipping this tick"
            % type(exc).__name__)

    # HISTORY: journal every change from this SINGLE read (no second file read),
    # for EVERYONE's actions — the keeper's own writes included. Deliberately
    # AFTER the event watcher (so todo_fp is still the "before" for it) and BEFORE
    # the notify gate: KEEPER_RELAY_TODO_NOTIFY=off silences the pings, not the
    # history (the by/via filter is for self-echo, not audit).
    journal_cycle(cfg, state, items)

    # OPERATOR PINGER (detection): accrue outbound-ping candidates from this SAME
    # read — BEFORE the todo_notify gate, because that knob silences the KEEPER's
    # board ping, not the OPERATOR's phone ping. Delivery happens in opping_cycle.
    try:
        _opping_detect_board(cfg, state, items)
    except Exception as exc:
        log("operator-ping detect error (%s) — skipping this tick"
            % type(exc).__name__)

    # NOTIFICATIONS: the one-line [board] ping for new non-keeper items.
    if not cfg.get("todo_notify", True):
        return

    seen = state.get("todo_seen")
    if seen is None:
        # First run: adopt everything as already-known, notify about none.
        ids = [iid for iid in (_todo_id(it) for it in items) if iid]
        state["todo_seen"] = _cap_seen(ids)
        save_state(state)
        log("todo watcher: first run — seeded %d ids, no notifications" % len(ids))
        return

    seen_set = set(seen)
    merged = list(seen)
    new_notify = []
    for it in items:
        iid = _todo_id(it)
        if not iid or iid in seen_set:
            continue
        seen_set.add(iid)          # mark EVERY new id seen (keeper items too)…
        merged.append(iid)
        # …but only items the keeper did NOT write notify. Two markers, one per
        # axis: `by` records who ASKED (stays truthful — the operator's requests
        # are by:"user" even when the keeper files them), `via` records who
        # WROTE ("keeper" = the keeper session's own hand, so pinging the keeper
        # about it would be a self-echo — the operator asked for that eliminated
        # 2026-07-17). Console/bridge writes never carry `via`.
        # Self-authorship goes through _is_self_authored so the EXPLICIT
        # signatures ("<station>-keeper", "host-keeper" -- nomenclature
        # 2026-08-27) filter exactly like the legacy bare "keeper".
        if not _is_self_authored(it.get("by")) and not _is_self_authored(it.get("via")):
            new_notify.append(it)
    new_seen = _cap_seen(merged)

    if not new_notify:
        if new_seen != seen:       # keeper-only / no-op ticks still advance seen
            state["todo_seen"] = new_seen
            save_state(state)
        return

    # PEER SPLIT (operator ask 2026-07-29): a message from another VM must ping
    # promptly, so peer message items leave the quiet-gated [board] batch and
    # ride their own PEER_AUTHOR entry (idle gate only — see drain()). Same
    # dedup ring, same one-batch-per-tick shape; only the queue class differs.
    peer_new = [it for it in new_notify if _is_peer_msg_item(it)]
    board_new = [it for it in new_notify if not _is_peer_msg_item(it)]
    state["todo_seen"] = new_seen
    if board_new:
        line = render_board(board_new)
        ids = [it.get("id") for it in board_new]
        # `ids` rides along so the lull relay can see which items already have a
        # [board] notice pending in the queue and never double-carry them.
        enqueue(state, {"ts": time.time(), "author": "board", "payload": line,
                        "sha256": digest(line), "prefix": line[:40], "ids": ids})
        audit({"decision": "todo-notify", "event": "todo-notify", "ids": ids,
               "count": len(ids), "injected": bool(cfg.get("live")),
               "queued": True})
    if peer_new:
        line = render_peer_msgs([{"id": it.get("id"), "from": it.get("by"),
                                  "text": it.get("text")} for it in peer_new])
        ids = [it.get("id") for it in peer_new]
        enqueue(state, {"ts": time.time(), "author": PEER_AUTHOR,
                        "payload": line, "sha256": digest(line),
                        "prefix": line[:40], "ids": ids})
        audit({"decision": "peer-notify", "event": "peer-notify", "ids": ids,
               "count": len(ids), "injected": bool(cfg.get("live")),
               "queued": True})
    save_state(state)


# ---------------------------------------------------------------- event journal
#
# A GOING-FORWARD, append-only board history. Every cycle diffs the board against
# a compact per-item fingerprint map kept in state (id -> status / note-hash /
# text-hash / comment-count / type) and appends one JSON line per change; deploy
# outcomes land here too. Contract (one object per line), consumed by the console:
#   {"ts","event":"added",  "id","type","detail":{"text","by","via"?}}
#   {"ts","event":"status", "id","type","detail":{"from","to"}}
#   {"ts","event":"note",   "id","type","detail":{"to"}}
#   {"ts","event":"text",   "id","type","detail":{"to"}}
#   {"ts","event":"comment","id","type","detail":{"by","text","n"}}
#   {"ts","event":"removed","id","type","detail":{"text"}}
#   {"ts","event":"deploy-ok",     "id":"deploy","type":"deploy",
#        "detail":{"commit","deploy_ts","files"}}
#   {"ts","event":"deploy-failed", "id":"deploy","type":"deploy","detail":{"reason"}}
# FIRST RUN seeds fingerprints silently (no birth storm). NO backfill — the file
# starts empty and grows from the first real change after this deploys.


def _hist_trunc(s, n):
    """Coerce to str and hard-cap at n chars for the journal (per the UI contract).
    An over-long value is cut to n-1 chars plus a one-char ellipsis so len<=n and
    the truncation is visible."""
    s = "" if s is None else str(s)
    s = " ".join(s.split())            # collapse newlines/runs; a journal line is one JSON row
    return s if len(s) <= n else s[:n - 1] + "…"


def _item_fingerprint(it):
    """Compact snapshot used to diff one board item cycle-to-cycle. The hashes
    detect an edit anywhere in the FULL value; the truncated `text` copy lets a
    'removed' line still name the item after it has left the board."""
    if not isinstance(it, dict):
        it = {}
    comments = it.get("comments")
    return {
        "status": it.get("status"),
        "type": it.get("type"),
        "note_hash": digest(it.get("note") or ""),
        "text_hash": digest(it.get("text") or ""),
        "text": _hist_trunc(it.get("text"), HIST_TEXT_MAX),
        "comments": len(comments) if isinstance(comments, list) else 0,
    }


def journal_append(cfg, state, event, iid, typ, detail):
    """Append ONE journal line with a single O_APPEND write. Rotates to <path>.1
    once the file passes ~HIST_CAP_BYTES (single rotation, old .1 overwritten).
    A missing/unwritable path is tolerated with one log per failure streak — it
    NEVER raises, so a broken journal can't take the relay down."""
    path = cfg.get("history_path") or TODO_HISTORY_FILE
    rec = {"ts": time.time(), "event": event, "id": iid, "type": typ,
           "detail": detail}
    line = json.dumps(rec, sort_keys=True) + "\n"
    try:
        # Cap protection BEFORE the write: past the cap, rename the current file
        # to <path>.1 (overwriting any prior .1) so the new O_CREAT starts fresh.
        try:
            if os.path.getsize(path) > HIST_CAP_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass                        # absent file -> nothing to rotate
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        if not state.get("journal_write_failing"):
            log("journal write failed (%s) — %s (history paused until writable)"
                % (type(exc).__name__, path))
            state["journal_write_failing"] = True
        return
    if state.get("journal_write_failing"):
        log("journal write recovered — %s" % path)
        state["journal_write_failing"] = False


def _deploy_ok_mtime(path):
    """int(mtime) of last-deploy.json, or None if there is no such file yet."""
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return None


def _read_json_obj(path):
    """Load a JSON object from path, or None on any absent/half-written/non-object
    file. Data, never a crash."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _deploy_failed_reason(path):
    """Best-effort reason string for a deploy-failed line, truncated per contract.
    The signal may be JSON with a reason/error field, plain text, or empty — all
    three are tolerated."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read(4096)
    except OSError:
        return "deploy-ui failed (signal present, reason unreadable)"
    raw = raw.strip()
    if not raw:
        return "deploy-ui failed (no reason text in signal)"
    try:
        obj = json.loads(raw)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        for k in ("reason", "error", "message", "detail"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                raw = v
                break
    return _hist_trunc(raw, HIST_REASON_MAX)


def _journal_deploy_ok(cfg, state):
    """A fresh last-deploy.json (mtime-fingerprinted) -> one deploy-ok line with
    its commit / deploy_ts / files. The first sighting seeds silently so a deploy
    that predates this feature never fires a spurious event."""
    path = cfg.get("last_deploy_path") or LAST_DEPLOY_FILE
    if not _deploy_signals_on(path):
        return                              # no deploy pipeline on this VM (feature-off)
    mt = _deploy_ok_mtime(path)
    if mt is None:
        return
    if "journal_deploy_ok_mtime" not in state:
        state["journal_deploy_ok_mtime"] = mt      # upgrade/seed: adopt, emit nothing
        return
    if state.get("journal_deploy_ok_mtime") == mt:
        return
    state["journal_deploy_ok_mtime"] = mt
    data = _read_json_obj(path) or {}
    journal_append(cfg, state, "deploy-ok", "deploy", "deploy", {
        "commit": _hist_trunc(data.get("commit"), HIST_TEXT_MAX),
        "deploy_ts": _hist_trunc(data.get("ts"), HIST_TEXT_MAX),
        "files": data.get("files"),
    })


def journal_cycle(cfg, state, items):
    """Diff this cycle's board against the fingerprint map and append one journal
    line per change (added/status/note/text/comment/removed), plus the deploy-ok
    watch. FIRST RUN seeds fingerprints silently and writes nothing. NO by/via
    filter — history is everyone's actions. Reuses `items` from todo_watch's read.
    """
    fp = state.get("todo_fp")
    if fp is None:
        # First run: adopt the current board + deploy state as the baseline and
        # journal NOTHING. History is going-forward only; there is no backfill.
        seed = {}
        for it in items:
            iid = _todo_id(it)
            if iid:
                seed[iid] = _item_fingerprint(it)
        state["todo_fp"] = seed
        state["journal_deploy_ok_mtime"] = _deploy_ok_mtime(
            cfg.get("last_deploy_path") or LAST_DEPLOY_FILE)
        save_state(state)
        log("journal: first run — seeded %d fingerprints, no history written" % len(seed))
        return

    present = set()
    for it in items:
        iid = _todo_id(it)
        if not iid:
            continue
        present.add(iid)
        cur = _item_fingerprint(it)
        old = fp.get(iid)
        if old is None:
            detail = {"text": _hist_trunc(it.get("text"), HIST_TEXT_MAX),
                      "by": _hist_trunc(it.get("by") or "?", HIST_TEXT_MAX)}
            if it.get("via") is not None:
                detail["via"] = _hist_trunc(it.get("via"), HIST_TEXT_MAX)
            journal_append(cfg, state, "added", iid, cur["type"], detail)
            fp[iid] = cur
            continue
        if cur["status"] != old.get("status"):
            journal_append(cfg, state, "status", iid, cur["type"],
                           {"from": old.get("status"), "to": cur["status"]})
        if cur["note_hash"] != old.get("note_hash"):
            journal_append(cfg, state, "note", iid, cur["type"],
                           {"to": _hist_trunc(it.get("note"), HIST_TEXT_MAX)})
        if cur["text_hash"] != old.get("text_hash"):
            journal_append(cfg, state, "text", iid, cur["type"],
                           {"to": _hist_trunc(it.get("text"), HIST_TEXT_MAX)})
        prev_n, cur_n = old.get("comments", 0), cur["comments"]
        if cur_n > prev_n:
            comments = it.get("comments")
            comments = comments if isinstance(comments, list) else []
            for n in range(prev_n, cur_n):
                c = comments[n] if 0 <= n < len(comments) else {}
                c = c if isinstance(c, dict) else {}
                journal_append(cfg, state, "comment", iid, cur["type"], {
                    "by": _hist_trunc(c.get("by") or "?", HIST_TEXT_MAX),
                    "text": _hist_trunc(c.get("text"), HIST_TEXT_MAX),
                    "n": n + 1,
                })
        fp[iid] = cur

    for iid in list(fp):                 # removed = tracked but gone from the board
        if iid not in present:
            old = fp.pop(iid)
            journal_append(cfg, state, "removed", iid, old.get("type"),
                           {"text": _hist_trunc(old.get("text"), HIST_TEXT_MAX)})

    _journal_deploy_ok(cfg, state)
    save_state(state)


# ---------------------------------------------------------------- lull relay
#
# Three strictly-separated layers, mirroring the board watcher's discipline but
# adding a judgment step:
#   1. DETERMINISTIC detection (no LLM, every cycle) — accrue candidates in state.
#   2. LLM JUDGMENT at a lull (background daemon thread, single-flight) — the ONLY
#      network/latency here, and the 15s cycle NEVER waits on it.
#   3. DELIVERY through the existing idle-gated enqueue/drain.
# The worker thread only READS an immutable cfg + snapshot and drops its result
# on a queue; ALL state mutation stays on the main cycle, so state.json has no
# concurrent writer.

_LULL_LOCK = threading.Lock()   # guards the single-flight flag below
_LULL_INFLIGHT = False          # in-memory: is a judgment thread running now?
_LULL_RESULTS = queue.Queue()   # finished judgments, drained by the next cycle


def _lull_minutes(raw, default_min):
    """env minutes -> seconds; any missing/negative/garbage value -> default."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default_min * 60.0
    return v * 60.0 if v >= 0 else default_min * 60.0


# ---- layer 1: deterministic candidate detection ----------------------------


def _lull_deploy_mtime(path):
    """int(mtime) of the deploy-failed signal, or None if there is no failure."""
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return None


def _lull_bugerr_count(path):
    """(count-of-err+-findings, generated_ts) from bugreport.json, or (None, None)
    if it can't be read/parsed. A malformed/half-written file is DATA, not a crash.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    findings = data.get("findings")
    if not isinstance(findings, list):
        return 0, data.get("generated_ts")
    n = 0
    for f in findings:
        if isinstance(f, dict) and str(f.get("severity") or "").strip().lower() \
                in LULL_ERR_SEVERITIES:
            n += 1
    return n, data.get("generated_ts")


def _lull_done_add(done, cid):
    """Mark a candidate delivered (idempotent), capping the ring at LULL_DONE_CAP."""
    if cid in done:
        return
    done.append(cid)
    if len(done) > LULL_DONE_CAP:
        del done[:len(done) - LULL_DONE_CAP]


# ------------------------------------------- delivery-time board re-read (k33)
#
# THE DEFECT. Every board-derived notice is RENDERED AT DETECTION and delivered
# later — a [board] notice waits out the quiet window (and can starve to the
# max-hold cap), a [peer-msg] notice waits for an idle pane, a [lull] digest waits
# for a confirmed lull plus its cooldown. The gap is routinely minutes and can be
# hours. So the line the keeper finally reads describes the board AS IT WAS, and
# five specimens were collected on 2026-07-29 of notices arriving about items the
# keeper had already closed — including a 7-notification self-echo burst from one
# board-hygiene pass. A notice that lies about the board is worse than no notice:
# it costs a re-read to discover there is nothing to do.
#
# THE FIX. Re-read ~/todo.json at DELIVERY time and judge each carried id against
# the CURRENT board, not the one at detection.
#
# THE RULES, in the order they matter:
#   1. UNREADABLE MUST DELIVER. A board we cannot read is not evidence of
#      anything. Half-written file, bad shape, gone — every one of those delivers
#      the notice unchanged and audits WHY. Suppression requires proof.
#   2. SUPPRESS ONLY THE PROVABLY GONE. "done" (status is now done) or "deleted"
#      (the id is absent from a board that read cleanly). An item that merely
#      moved open->doing still pings: the keeper picking something up is not the
#      keeper finishing it.
#   3. THE AUDIT REASON DISTINGUISHES THE CASES. "item is done" and "could not
#      re-read" must never share a reason string — the whole point of the audit
#      trail here is telling a correct suppression from a blind one.
#   4. PARTIAL STALENESS RE-RENDERS. A notice carrying five ids of which two
#      closed delivers the other three, re-rendered from the CURRENT board — so
#      the text is fresh too, not just the id list.
#
# THE board-event ASYMMETRY (deliberate, and the one trap here): a
# author=="board-event" notice REPORTS a transition — "✓ k31 done" exists
# BECAUSE the item is done. Applying the done predicate to it would suppress
# every done-transition notice the relay has. So board-event is re-filtered on
# DELETED ONLY. Its event dicts also cannot be rebuilt from board state (a diff
# is not a snapshot), so it re-renders from the `evs` copy carried on the queue
# entry, and falls back to delivering the original line when that is absent —
# which is the case for entries queued by an older build and still in state.
_DELIVERY_LIVE = "live"
_DELIVERY_DONE = "done"
_DELIVERY_GONE = "deleted"

# Queue classes carrying board ids that a delivery-time re-read can judge.
_REFILTER_AUTHORS = ("board", "board-event", PEER_AUTHOR)


def _delivery_board_index(cfg):
    """Board id -> item for a delivery-time re-read: (by_id, None) on a clean
    read, (None, reason) on anything else. The reason is the caller's audit
    evidence that suppression was NOT attempted."""
    items, err = read_todo(cfg.get("todo_path") or TODO_FILE)
    if err or not isinstance(items, list):
        return None, (err or "unexpected-shape")
    by_id = {}
    for it in items:
        iid = _todo_id(it)
        if iid:
            by_id[iid] = it
    return by_id, None


def _delivery_state_of(by_id, iid, done_is_stale):
    """One id's delivery-time verdict against a board that read cleanly."""
    it = by_id.get(iid)
    if not isinstance(it, dict):
        return _DELIVERY_GONE
    if done_is_stale and it.get("status") == "done":
        return _DELIVERY_DONE
    return _DELIVERY_LIVE


def _delivery_suppress_reason(verdicts):
    """The audit reason for a whole-notice suppression, naming WHICH proof we
    had. Never reused for the unreadable path — see rule 3 above."""
    kinds = set(verdicts.values())
    if kinds == {_DELIVERY_DONE}:
        return "all-items-done"
    if kinds == {_DELIVERY_GONE}:
        return "all-items-deleted"
    return "all-items-done-or-deleted"


def _delivery_rerender(item, by_id, live_ids):
    """Re-render a partially-stale notice for the surviving ids from the CURRENT
    board. Returns the new line, or None when this queue class cannot be
    re-rendered (board-event without its `evs` copy) — the caller then delivers
    the original line rather than a filtered lie."""
    author = item.get("author")
    if author == "board":
        return render_board([by_id[i] for i in live_ids])
    if author == PEER_AUTHOR:
        return render_peer_msgs([{"id": i, "from": by_id[i].get("by"),
                                 "text": by_id[i].get("text")} for i in live_ids])
    if author == "board-event":
        evs = item.get("evs")
        if not isinstance(evs, list):
            return None                  # legacy queue entry: deliver as-queued
        keep = set(live_ids)
        pending = [e for e in evs
                   if isinstance(e, dict) and e.get("id") in keep]
        return render_board_events(pending) if pending else None
    return None


def _queue_refilter_at_delivery(cfg, state, item):
    """Judge ONE queue entry against the board as it is NOW, at delivery time.

    Returns True to deliver (payload possibly re-rendered in place), False to
    suppress the whole notice. Audits every suppression and every re-render with
    a reason that says which of the four rules above applied. Never raises: a
    fault here falls through to delivering the notice unchanged, because the
    fail-safe direction on a notification path is to notify.
    """
    ids = [i for i in (item.get("ids") or []) if isinstance(i, str)]
    if not ids or item.get("author") not in _REFILTER_AUTHORS:
        return True                      # nothing carried -> nothing to re-judge
    by_id, reason = _delivery_board_index(cfg)
    if by_id is None:
        # RULE 1. No proof, so no suppression — and the trail says why, in a
        # reason string that can never be mistaken for "the item was done".
        audit({"decision": "delivery-refilter", "reason": "board-unreadable:%s"
               % reason, "author": item.get("author"), "ids": ids,
               "delivered": True, "suppressed": False, "queued": False})
        return True
    done_is_stale = item.get("author") != "board-event"   # the asymmetry, above
    verdicts = {iid: _delivery_state_of(by_id, iid, done_is_stale) for iid in ids}
    live_ids = [i for i in ids if verdicts[i] == _DELIVERY_LIVE]
    stale = {i: v for i, v in verdicts.items() if v != _DELIVERY_LIVE}
    if not stale:
        return True                      # the common case: deliver, no extra audit
    if not live_ids:
        rec = {"decision": "delivery-suppress",
               "reason": _delivery_suppress_reason(verdicts),
               "author": item.get("author"), "ids": ids, "stale": verdicts,
               "delivered": False, "suppressed": True, "queued": False}
        # RE-ID EVIDENCE (blackbird review of k33, 2026-07-30). "Absent from a
        # board that read cleanly" conflates DELETED with RE-ID'D, and this fleet
        # has precedent: console-api's todo_norm once re-id'd 42 hyphenated ids in
        # a SINGLE op. A rename between enqueue and delivery therefore suppresses
        # as all-items-deleted while the item lives on under a new id — for a
        # author=="board" new-item notice that silences a notice for something
        # that still exists. Severity is low (a keeper reads its board at session
        # start regardless, so the cost is delayed discovery, not a lost item) and
        # the honest fix is not to guess: text can be edited too, so matching
        # snippets would trade a rare wrong suppression for a rare wrong DELIVERY
        # plus a heuristic nobody can reason about.
        #
        # What we owe instead is DIAGNOSABILITY. Carry the line we were about to
        # type whenever anything was judged deleted: it holds the item text as it
        # read at detection, so a re-id is recoverable from the trail alone —
        # grep the audit for the suppressed text, find it live under a new id.
        # Honest-blind beats silent-blind, and the trail is where that distinction
        # has to survive.
        if _DELIVERY_GONE in verdicts.values():
            rec["suppressed_line"] = (item.get("payload") or "")[:200]
        audit(rec)
        log("delivery re-read: suppressed %s notice (%s)"
            % (item.get("author"), _delivery_suppress_reason(verdicts)))
        return False                     # nothing left that is true -> no notice
    line = _delivery_rerender(item, by_id, live_ids)
    if line is None:
        audit({"decision": "delivery-refilter",
               "reason": "stale-ids-kept-unrenderable",
               "author": item.get("author"), "ids": ids, "stale": stale,
               "delivered": True, "suppressed": False, "queued": False})
        return True
    item["payload"] = line
    item["sha256"] = digest(line)
    item["prefix"] = line[:40]
    audit({"decision": "delivery-refilter",
           "reason": "dropped-%d-stale-of-%d" % (len(stale), len(ids)),
           "author": item.get("author"), "ids": ids, "stale": stale,
           "kept": live_ids, "delivered": True, "suppressed": False,
           "queued": False})
    return True


def _queued_board_ids(state):
    """Item ids whose [board] / [peer-msg] notice is still sitting in the queue —
    the lull relay never double-carries something todo_watch already enqueued."""
    out = set()
    for q in state.get("queue", []):
        if isinstance(q, dict) and q.get("author") in ("board", PEER_AUTHOR):
            for iid in (q.get("ids") or []):
                if isinstance(iid, str):
                    out.add(iid)
    return out


def _lull_add(lull, cid, kind, line, first_seen, item=None):
    """Accrue a candidate unless it is already pending or already delivered."""
    if cid in lull["done"] or cid in lull["candidates"]:
        return
    lull["candidates"][cid] = {"kind": kind, "line": line,
                               "first_seen": first_seen, "item": item}


def _as_epoch(v):
    """A comparable epoch float from a ts field, or None if it isn't a number. A
    missing/garbage ts is UNKNOWN, never 0 — the caller must not treat an absent
    timestamp as 'oldest' and suppress on it."""
    if isinstance(v, bool):        # bool is an int subclass; a stray True is not a ts
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


# board t51 — refrain when nothing is actionable. The lull layer used to ping on
# ANY new non-keeper comment; a real 2026-07-18 occurrence surfaced a lone
# operator comment on a DONE item the keeper had ALREADY acted on (a keeper note
# edit POSTDATED the comment) — zero actionable content. We now drop a non-keeper
# comment on a DONE item when keeper activity postdates it. Open/doing items and
# every non-comment candidate class are untouched.
#
# ATTRIBUTION SIGNAL — honest tradeoffs. The relay WRITES ~/todo-history.jsonl but
# never reads it back, and its note/status/text lines carry NO actor (only
# comment/added do) — so the journal could not even attribute the note edit that
# caused this very defect. Rather than build a new attribution store we read the
# fields the relay ALREADY parses: comment.by / comment.ts and the item's own
# last-touch `ts` (the backend restamps it on every update). Two OR'd signals:
#   (1) a keeper comment on the item with a strictly-later ts — a keeper reply
#       in-thread. Comment-adds do NOT bump item.ts, so (2) does not cover this.
#   (2) item.ts strictly later than the comment — the item was MUTATED (status /
#       note / text) after it; on a done item this is the keeper close-out edit.
#   FALSE NEGATIVE (still ping when we could have suppressed): item.ts can lag,
#     and an unknown/missing comment ts never suppresses. Harmless — one extra
#     catch-up ping, and "a decision thread can continue after done" is itself a
#     legitimate reason to keep pinging.
#   FALSE POSITIVE (suppress something unseen): only if a DONE item is mutated
#     after the comment by someone OTHER than the keeper (e.g. the operator
#     re-edits a closed item's text). Rare on done items, bounded to done items
#     with a strictly-later touch; open/doing are never in scope.
def _keeper_acted_after_comment(it, comments, comment_ts):
    """True iff keeper-authored activity on THIS item postdates the non-keeper
    comment stamped `comment_ts`. Returns False when `comment_ts` is unknown —
    never suppress on a missing timestamp (the safe direction is to keep pinging).
    """
    c_ts = _as_epoch(comment_ts)
    if c_ts is None:
        return False
    for c in comments:                   # (1) keeper reply in-thread, strictly later
        if isinstance(c, dict) and _is_self_authored(c.get("by")):
            k_ts = _as_epoch(c.get("ts"))
            if k_ts is not None and k_ts > c_ts:
                return True
    i_ts = _as_epoch(it.get("ts"))       # (2) the item itself edited after the comment
    return i_ts is not None and i_ts > c_ts


def _lull_detect_comments(state, lull, items, now, board_ids):
    """New comments whose `by` != "keeper" become candidates. A per-item count is
    tracked (seeded silently on first run) so only genuinely new comments fire.
    board t51: a non-keeper comment on a DONE item the keeper has since acted on is
    NOT a candidate (still counted -> marked seen -> never re-surfaces; audited
    once, since this comment index is visited exactly once).

    PEER SPLIT (operator ask 2026-07-29): a comment from another VM — a peer
    keeper's `peer-msg reply` on this board, or the local peer-msg poller's
    "[peer] …" reply notification (by:"peer-msg") — is a MESSAGE, not board
    chatter. Those never accrue as lull candidates; they enqueue ONE prompt
    PEER_AUTHOR line right here (idle gate only). Their cids are marked in
    lull.done AT ENQUEUE — the same delivered-at-enqueue semantics _lull_apply
    uses (the queue is durable and at-most-once), and the ring peer-msg's
    prune contract reads as proof of delivery — so nothing double-carries and
    the reply feed still prunes."""
    counts = lull["comment_counts"]
    peer_pending = []                   # [{"cid","id","from","text"}] this tick
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = _todo_id(it)
        comments = it.get("comments")
        if not iid or not isinstance(comments, list):
            continue
        prev = counts.get(iid, 0)
        cur = len(comments)
        if cur <= prev:
            counts[iid] = cur           # a rewrite that shrank the list: resync
            continue
        if iid in board_ids:
            continue                    # item notice still pending -> reconsider later
        done_item = it.get("status") == "done"
        for n in range(prev, cur):
            c = comments[n]
            if not isinstance(c, dict) or _is_self_authored(c.get("by")):
                continue                # the keeper's own comments never notify
            if done_item and _keeper_acted_after_comment(it, comments, c.get("ts")):
                audit({"decision": "lull-suppress",
                       "reason": "comment-on-done-keeper-acted",
                       "id": "comment:%s:%d" % (iid, n), "item": iid,
                       "queued": False})
                continue                # nothing actionable — do not accrue, just seen
            cid = "comment:%s:%d" % (iid, n)
            if _is_peer_author(c.get("by")):
                if cid in lull["done"]:
                    continue            # already delivered (e.g. pre-split digest)
                peer_pending.append({"cid": cid, "id": iid,
                                     "from": c.get("by"), "text": c.get("text")})
                continue
            who = sanitize(str(c.get("by") or "?"))[0] or "?"
            snippet = sanitize(str(c.get("text") or ""))[0][:LULL_SNIPPET]
            _lull_add(lull, cid, "comment",
                      'new comment on %s by %s: "%s"' % (iid, who, snippet),
                      now, item=iid)
        counts[iid] = cur
    if peer_pending:
        line = render_peer_msgs([dict(e, reply=True) for e in peer_pending])
        ids = sorted({e["id"] for e in peer_pending})
        for e in peer_pending:
            _lull_done_add(lull["done"], e["cid"])
        enqueue(state, {"ts": now, "author": PEER_AUTHOR, "payload": line,
                        "sha256": digest(line), "prefix": line[:40], "ids": ids})
        audit({"decision": "peer-notify", "event": "peer-notify", "ids": ids,
               "cids": [e["cid"] for e in peer_pending],
               "count": len(peer_pending), "queued": True})


def _lull_stale_line(it, iid):
    """The one-line rendering of a stale-item candidate. Factored out because the
    delivery re-read (k33) re-renders it from the CURRENT item — a candidate that
    accrued hours ago must not quote text the keeper has since rewritten."""
    typ = sanitize(str(it.get("type") or "item"))[0] or "item"
    snippet = sanitize(str(it.get("text") or ""))[0][:LULL_SNIPPET]
    return 'stale unseen %s %s: "%s"' % (typ, iid, snippet)


def _lull_detect_stale(lull, items, now, stale_sec, board_ids):
    """Open, non-keeper-written items first noticed > stale_sec ago and never yet
    relayed — the "keeper may have missed the original ping" case."""
    first_seen = lull["stale_first_seen"]
    open_now = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = _todo_id(it)
        if not iid or it.get("status") != "open":
            continue
        if _is_self_authored(it.get("by")) or _is_self_authored(it.get("via")):
            continue                    # same by/via filter as the board watcher
        open_now.add(iid)
        fs = first_seen.get(iid)
        if fs is None:
            first_seen[iid] = now        # first noticed now -> not stale yet
            continue
        if (now - fs) <= stale_sec or iid in board_ids:
            continue
        _lull_add(lull, "stale:%s" % iid, "stale", _lull_stale_line(it, iid), now,
                  item=iid)
    for iid in list(first_seen):
        if iid not in open_now:
            del first_seen[iid]          # closed/gone -> re-age if it reopens


def _lull_detect_deploy(lull, path, now):
    """A deploy-ui.failed signal, fingerprinted by mtime so each distinct failure
    fires exactly once."""
    mt = _lull_deploy_mtime(path)
    if mt is None or lull.get("deploy_mtime") == mt:
        return
    lull["deploy_mtime"] = mt
    _lull_add(lull, "deploy-failed:%d" % mt, "deploy",
              "deploy-ui FAILED (signal @ mtime %d)" % mt, now)


def _lull_detect_bugerr(lull, path, now):
    """A RISE in bugreport err+ findings since last seen. The baseline tracks
    decreases too, so a later re-rise fires again."""
    n, gts = _lull_bugerr_count(path)
    if n is None:
        return
    prev = lull.get("bugerr_count", 0)
    if n <= prev:
        lull["bugerr_count"] = n
        return
    lull["bugerr_count"] = n
    _lull_add(lull, "bugerr:%d@%s" % (n, gts), "bugerr",
              "bugreport: %d err+ finding(s) (was %d)" % (n, prev), now)


def _lull_detect(cfg, state):
    """Run every deterministic detector against the current world. No LLM."""
    lull = state["lull"]
    now = time.time()
    board_ids = _queued_board_ids(state)
    items, err = read_todo(cfg.get("todo_path") or TODO_FILE)
    if not err and isinstance(items, list):
        _lull_detect_comments(state, lull, items, now, board_ids)
        _lull_detect_stale(lull, items, now, cfg["lull_stale_sec"], board_ids)
    # Reuse the existing deploy-failed watch: its lull-candidate role is unchanged,
    # but a genuinely-new failure (mtime moved) is ALSO recorded as history.
    dpath = cfg.get("deploy_failed_path") or DEPLOY_FAILED_FILE
    if _deploy_signals_on(dpath):           # skip the deploy candidate on a non-UI VM
        prev_dmt = lull.get("deploy_mtime")
        _lull_detect_deploy(lull, dpath, now)
        new_dmt = lull.get("deploy_mtime")
        if new_dmt is not None and new_dmt != prev_dmt:
            journal_append(cfg, state, "deploy-failed", "deploy", "deploy",
                           {"reason": _deploy_failed_reason(dpath)})
    _lull_detect_bugerr(lull, cfg.get("bugreport_path") or BUGREPORT_FILE, now)


def _lull_seed(cfg, state):
    """First run: adopt the CURRENT world as already-known and relay nothing — the
    same silent seeding the todo watcher does, so enabling this never storms the
    keeper on deploy."""
    items, err = read_todo(cfg.get("todo_path") or TODO_FILE)
    items = items if (not err and isinstance(items, list)) else []
    now = time.time()
    counts, stale_seen = {}, {}
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = _todo_id(it)
        if not iid:
            continue
        comments = it.get("comments")
        if isinstance(comments, list):
            counts[iid] = len(comments)
        if it.get("status") == "open" and it.get("by") != "keeper" \
                and it.get("via") != "keeper":
            stale_seen[iid] = now
    n_err, _gts = _lull_bugerr_count(cfg.get("bugreport_path") or BUGREPORT_FILE)
    state["lull"] = {
        "comment_counts": counts,
        "stale_first_seen": stale_seen,
        "bugerr_count": n_err if n_err is not None else 0,
        "deploy_mtime": _lull_deploy_mtime(
            cfg.get("deploy_failed_path") or DEPLOY_FAILED_FILE),
        "candidates": {},
        "done": [],
        "last_digest_ts": 0.0,
    }
    save_state(state)
    log("lull relay: first run — seeded %d comment-tracked, %d open, bugerr=%d, "
        "no relays" % (len(counts), len(stale_seen), state["lull"]["bugerr_count"]))


# ---- layer 2: LLM judgment at a lull (background, single-flight) ------------


def _lull_prompt(snapshot, tail):
    lines = "\n".join("- %s: %s" % (c["id"], c["line"]) for c in snapshot) \
        or "- (none)"
    tail = (tail or "")[-LULL_TAIL_CHARS:]
    return (
        "You triage catch-up notices for an autonomous agent (\"the keeper\") that "
        "works in the terminal shown below. Each CANDIDATE is a pending notice in "
        "the form  id: one-line summary.\n\n"
        "CANDIDATES:\n%s\n\n"
        "TERMINAL TAIL (most recent lines last):\n%s\n\n"
        "Using the terminal tail, decide which candidates the keeper has NOT yet "
        "visibly seen or actioned — those must be relayed; ones it clearly already "
        "saw or handled should be skipped. Reply with ONLY a JSON object and "
        "nothing else:\n"
        "{\"relay\": [ids to relay], \"skip\": [ids already seen], "
        "\"line\": \"<=200 char summary of the relay set, mentioning the item ids\"}"
        % (lines, tail))


def _lull_http(cfg, prompt):
    """ONE chat completion with ONE retry. Returns the reply content string, or
    None on any failure/timeout after the retry. Never raises; errors are data."""
    url = cfg.get("hugpy_api") or HUGPY_API
    body = json.dumps({
        "model": cfg.get("hugpy_brain") or HUGPY_BRAIN,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    for _attempt in range(2):            # one try + one retry
        try:
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=LULL_HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, str):
                return content
        except (urllib.error.URLError, OSError, ValueError, TimeoutError,
                KeyError, IndexError, TypeError):
            continue
    return None


def _first_brace_object(s):
    """The first balanced {...} substring, ignoring braces inside JSON strings."""
    depth = start = 0
    start = -1
    in_str = esc = False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                return s[start:i + 1]
    return None


def _lull_parse(content):
    """Pull a {relay,skip,line} object out of a model reply, tolerating code fences
    and surrounding prose. Returns a dict, or None if nothing parseable is found."""
    if not isinstance(content, str):
        return None
    tries = [content.strip()]
    fence = re.search(r"```(?:json)?\s*(.+?)```", content, re.DOTALL)
    if fence:
        tries.append(fence.group(1).strip())
    braced = _first_brace_object(content)
    if braced:
        tries.append(braced)
    for cand in tries:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _as_str_list(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _lull_compose_line(ids):
    """Our own deterministic summary — for FAIL-OPEN and whenever the model's line
    fails the citation checks. Every character below is ours."""
    shown = ids[:LULL_LINE_IDS]
    tail = ", ".join(shown)
    if len(ids) > len(shown):
        tail += ", …"
    return "[lull] %d pending: %s — check ~/todo.json" % (len(ids), tail)


def _lull_final_line(raw, relay_ids):
    """The line that will actually be injected. Trust the model's line ONLY if it
    survives sanitize, stays <= LULL_LINE_MAX, and actually cites a relayed id;
    otherwise rebuild it deterministically. Always sanitized, always [lull]-tagged.
    """
    text = sanitize(raw or "")[0]
    if not (text and len(text) <= LULL_LINE_MAX
            and any(rid in text for rid in relay_ids)):
        text = _lull_compose_line(relay_ids)
    if not text.startswith("[lull]"):
        text = "[lull] " + text
    return sanitize(text)[0]


def _lull_judge(cfg, snapshot, tail):
    """Return (relay_ids, skip_ids, final_line, mode). mode is 'ok' when the brain
    answered usefully, 'failopen' when it was unreachable/garbage — in which case
    EVERY candidate is relayed. Nothing is ever silently dropped: a candidate the
    brain neither relays nor skips defaults to relayed."""
    cand_ids = [c["id"] for c in snapshot]
    obj = _lull_parse(_lull_http(cfg, _lull_prompt(snapshot, tail)))
    if not isinstance(obj, dict) or ("relay" not in obj and "skip" not in obj):
        return cand_ids, [], _lull_final_line(None, cand_ids), "failopen"
    cand_set = set(cand_ids)
    # Citation gate: ids outside the candidate set are discarded; a relay claim
    # wins any conflict (never drop something the brain also wanted relayed).
    relay_claim = {i for i in _as_str_list(obj.get("relay")) if i in cand_set}
    skip = [i for i in cand_ids
            if i in {j for j in _as_str_list(obj.get("skip")) if j in cand_set}
            and i not in relay_claim]
    relay = [i for i in cand_ids if i not in skip]
    return relay, skip, _lull_final_line(obj.get("line"), relay), "ok"


def _lull_capture_tail(cfg):
    """The last LULL_TAIL_LINES of the keeper pane, via the existing tmux helper."""
    out, err = tmux(cfg, "capture-pane", "-p", "-t", cfg["target"],
                    "-S", "-%d" % LULL_TAIL_LINES)
    return "" if err else (out or "")


def _lull_spawn(cfg, snapshot, tail):
    """Start the judgment thread iff none is already running. Returns the Thread
    (single-flight winner) or None. The 15s cycle NEVER blocks on it."""
    global _LULL_INFLIGHT
    with _LULL_LOCK:
        if _LULL_INFLIGHT:
            return None
        _LULL_INFLIGHT = True
    t = threading.Thread(target=_lull_worker, args=(cfg, snapshot, tail),
                         daemon=True, name="lull-judge")
    t.start()
    return t


def _lull_worker(cfg, snapshot, tail):
    """Daemon thread body: one judgment, then drop the result on the queue for the
    next cycle to pick up. Any exception degrades to FAIL-OPEN (relay all)."""
    global _LULL_INFLIGHT
    try:
        relay, skip, line, mode = _lull_judge(cfg, snapshot, tail)
    except Exception as exc:             # a judgment bug must never drop candidates
        ids = [c["id"] for c in snapshot]
        relay, skip, line, mode = ids, [], _lull_compose_line(ids), "failopen"
        log("lull worker failed (%s) — failing open" % type(exc).__name__)
    _LULL_RESULTS.put({"relay": relay, "skip": skip, "line": line, "mode": mode})
    with _LULL_LOCK:
        _LULL_INFLIGHT = False


# ---- layer 3: delivery (harvest -> the existing idle-gated queue) -----------


def _lull_apply(cfg, state, res):
    """Fold one finished judgment into state: mark candidates delivered/skipped and
    enqueue the ONE [lull] line through the shared idle-gated queue."""
    lull = state["lull"]
    cands, done = lull["candidates"], lull["done"]
    relayed, skipped = [], []
    for cid in _as_str_list(res.get("skip")):
        cands.pop(cid, None)
        _lull_done_add(done, cid)        # delivered=skipped-by-judge
        skipped.append(cid)
    for cid in _as_str_list(res.get("relay")):
        cands.pop(cid, None)
        _lull_done_add(done, cid)
        relayed.append(cid)
    line = res.get("line") or ""
    if relayed and line:
        enqueue(state, {"ts": time.time(), "author": "lull", "payload": line,
                        "sha256": digest(line), "prefix": line[:40]})
    save_state(state)
    audit({"decision": "lull-relay", "relayed": relayed, "skipped": skipped,
           "llm": res.get("mode", "?"), "count": len(relayed),
           "queued": bool(relayed and line), "injected": bool(cfg.get("live"))})


def _lull_harvest(cfg, state):
    """Apply every judgment result that has landed since the last cycle."""
    while True:
        try:
            res = _LULL_RESULTS.get_nowait()
        except queue.Empty:
            return
        _lull_apply(cfg, state, res)


def _lull_should_trigger(cfg, state, now):
    """Enough pending AND past the cooldown? (Idle + single-flight checked later.)"""
    lull = state["lull"]
    cands = lull["candidates"]
    if not cands:
        return False                    # board t51: an EMPTY set (incl. one emptied
                                        # by suppression) never triggers -> no ping.
                                        # This is the sole gate; no candidate = no
                                        # spawn = no [lull] line, guaranteed here.
    if (now - lull.get("last_digest_ts", 0.0)) < cfg["lull_cooldown_sec"]:
        return False
    if len(cands) >= 3:
        return True
    oldest = min(c["first_seen"] for c in cands.values())
    return (now - oldest) > cfg["lull_wait_sec"]


def _lull_comment_index(cid):
    """The trailing comment index n from a 'comment:<iid>:<n>' candidate id, or
    None if it doesn't parse. Item ids carry no colon, so a right-split is safe."""
    try:
        return int(cid.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return None


def _lull_refilter_at_delivery(cfg, state):
    """board t51 + k33, delivery re-filter. Accrual-time suppression (in
    _lull_detect_comments) can only judge the world AT ACCRUAL — a comment
    legitimately pending then (a decision thread still open on a done item) may be
    handled by the keeper BEFORE the digest fires. So at fire time, re-run the SAME
    predicate against the CURRENT board and drop any candidate the keeper has since
    dealt with. Two classes are re-checked:

      comment — the original t51 case: a non-keeper comment on a DONE item that
        keeper activity now postdates. Predicate unchanged.
      stale   — k33: a "stale unseen item" candidate whose item is now provably
        done, or gone from a board that read cleanly. An item that only moved
        open->doing is KEPT (picked up is not finished), and a kept candidate has
        its line re-rendered from the current item so the digest never quotes text
        the keeper has since rewritten.

    deploy / bugerr classes reference no board item and are untouched. Each drop is
    marked delivered (never resurfaces) and audited once with the lull-suppress
    idiom, with a reason naming WHICH proof applied. Returns the number of state
    changes made (drops PLUS line refreshes) — the caller uses it only to decide
    whether to persist, so a refresh with no drop still gets written down.
    Fail-safe (k33 rule 1): an unreadable board or an unlocatable comment KEEPS the
    candidate and audits a reason that cannot be confused with a real suppression —
    the safe direction on a notification path is to keep pinging."""
    lull = state["lull"]
    cands = lull["candidates"]
    comment_cids = [cid for cid, c in cands.items() if c.get("kind") == "comment"]
    stale_cids = [cid for cid, c in cands.items() if c.get("kind") == "stale"]
    if not comment_cids and not stale_cids:
        return 0
    by_id, reason = _delivery_board_index(cfg)
    if by_id is None:
        # Can't re-read -> deliver as-is, and say so distinguishably.
        audit({"decision": "lull-refilter", "reason": "board-unreadable:%s" % reason,
               "candidates": comment_cids + stale_cids, "delivered": True,
               "suppressed": False, "queued": False})
        return 0
    dropped = 0
    refreshed = 0
    for cid in stale_cids:
        cand = cands.get(cid)
        if cand is None:
            continue
        iid = cand.get("item")
        if not isinstance(iid, str):
            continue                     # no item ref -> nothing provable -> keep
        it = by_id.get(iid)
        if not isinstance(it, dict):
            why = "stale-item-deleted-at-delivery"
        elif it.get("status") == "done":
            why = "stale-item-done-at-delivery"
        else:
            fresh = _lull_stale_line(it, iid)          # kept: quote it as it is NOW
            if fresh != cand.get("line"):
                cand["line"] = fresh
                refreshed += 1
            continue
        cands.pop(cid, None)
        _lull_done_add(lull["done"], cid)
        rec = {"decision": "lull-suppress", "reason": why, "id": cid,
               "item": iid, "queued": False}
        # RE-ID EVIDENCE, the lull twin of the same finding — see the long note in
        # _queue_refilter_at_delivery. A stale candidate's `line` already quotes the
        # item text as it read at accrual, so carrying it makes a rename
        # diagnosable from the trail instead of vanishing as "deleted".
        if why == "stale-item-deleted-at-delivery":
            rec["suppressed_line"] = (cand.get("line") or "")[:200]
        audit(rec)
        dropped += 1
    for cid in comment_cids:
        cand = cands.get(cid)
        if cand is None:
            continue
        it = by_id.get(cand.get("item"))
        if not isinstance(it, dict) or it.get("status") != "done":
            continue                     # gone or no longer done -> keep, still pings
        comments = it.get("comments")
        n = _lull_comment_index(cid)
        if n is None or not isinstance(comments, list) or not 0 <= n < len(comments):
            continue                     # comment vanished/unlocatable -> keep (safe)
        c = comments[n]
        c_ts = c.get("ts") if isinstance(c, dict) else None
        if not _keeper_acted_after_comment(it, comments, c_ts):
            continue                     # still unhandled -> pings as today
        cands.pop(cid, None)
        _lull_done_add(lull["done"], cid)   # delivered: marked seen, never resurfaces
        audit({"decision": "lull-suppress",
               "reason": "comment-on-done-keeper-acted-at-delivery",
               "id": cid, "item": cand.get("item"), "queued": False})
        dropped += 1
    return dropped + refreshed


def lull_cycle(cfg, state):
    """Layer the lull relay onto the poll loop: harvest finished judgments, detect
    fresh candidates, and — at a confirmed lull — spawn ONE background judgment.
    Wrapped so a fault here never blocks the Discord/board drain."""
    if not cfg.get("lull", True):
        return
    try:
        if state.get("lull") is None:
            _lull_seed(cfg, state)
            return                       # the seed cycle relays nothing
        _lull_harvest(cfg, state)
        _lull_detect(cfg, state)
        save_state(state)
        now = time.time()
        if not _lull_should_trigger(cfg, state, now):
            return
        st, err = pane_state(cfg)
        if err or idle_reason(st) is not None:
            return                       # not a real lull yet — wait for idle
        # board t51 delivery re-filter: a candidate accrued while genuinely pending
        # may have been actioned by the keeper since. Re-judge against the current
        # board and drop now-handled comments BEFORE composing the line. Persist the
        # drops (even if the spawn is skipped for single-flight) so they never
        # resurface and are never re-audited.
        if _lull_refilter_at_delivery(cfg, state):
            save_state(state)
        if not state["lull"]["candidates"]:
            return                       # everything handled since accrual — no ping
        snapshot = [{"id": cid, "line": c["line"]}
                    for cid, c in state["lull"]["candidates"].items()]
        tail = _lull_capture_tail(cfg)
        if _lull_spawn(cfg, snapshot, tail) is not None:
            state["lull"]["last_digest_ts"] = now   # cooldown counts from trigger
            save_state(state)
            audit({"decision": "lull-trigger", "count": len(snapshot),
                   "candidates": [c["id"] for c in snapshot], "queued": False})
    except Exception as exc:             # a lull fault must not stop the drain
        log("lull cycle error (%s) — skipping this tick" % type(exc).__name__)
        audit({"decision": "error", "reason": "lull-exception:%s"
               % type(exc).__name__, "queued": False})


# ---------------------------------------------------------------- session steward
#
# A LULL-TRIGGERED keeper restart. Reuses the relay's proven machinery — the idle
# gate (idle_reason), the enqueue/drain queue, tmux(), audit, state — and adds
# nothing that can fire mid-turn. Per-cycle state:
#   state["steward"] = {phase, armed_ts, warned_ts, deadline,
#                       transcript_inventory_at_reset (the {file->size} baseline the
#                       growth metric diffs against — t79/t81), transcript_bytes_at_reset
#                       (its summed size, kept for observability/back-compat), last_reset_ts}
# phase in {"idle","armed","warned"}:
#   idle   : count the clock; at cadence, measure inventory growth and ARM
#   armed  : inject ONE idle-gated [steward] warning, set a WARN_LEAD deadline
#   warned : apply any keeper extension (hold); once past the deadline AND the
#            pane is a confirmed lull, RESTART (dry: log+reset; live: /exit+relaunch)

# The warning is a NOTICE, never a gate. The pre-2026-07-25 machine enqueued this
# line through the idle-gated drain, so it reached the pane only at a lull — and a
# keeper working alongside the operator is never idle, so it never arrived at all.
# The scrap-the-ceremony rewrite then removed the warning entirely, which fixed the
# stall but made every reset silent and mid-work: 48 board items accumulated that
# could never be finished because the keeper kept dying without a chance to wrap up.
# Now: WARN_LEAD before the fire we write ~/handoff/WARNING.md (a file write, which
# cannot stall) and best-effort inject the same line; when the deadline passes the
# restart fires UNCONDITIONALLY — no lull, no hold, no veto.
STEWARD_WARN_TEXT = (
    "[steward] CONTEXT RESET IN ~%dm (token budget reached). WRITE "
    "~/handoff/next.md NOW: what you were mid-way through, what the operator asked "
    "for that is not done yet, and what the next keeper should do FIRST. The reset "
    "is `/clear` — this pane survives but your context does not, and that handoff "
    "is all the next keeper will have. A half-finished task documented well is "
    "worth more than a finished one nobody knows about.\n"
    "NEED LONGER? You may postpone by 10 minutes, as many times as you need: "
    "`echo +10m > ~/handoff/hold`. Use it if you are mid-thought — buying another "
    "10 minutes is always better than losing the thread.")
STEWARD_EXTEND_TEXT = (
    "[steward] extension accepted — restart re-armed for the next lull past the "
    "new deadline. Extend again: echo +45m > ~/handoff/hold")

_STEWARD_HOLD_RE = re.compile(r"\+(\d+)([mh])")


def _steward_bytes(raw, default):
    """env byte-count -> int; any missing/negative/garbage value -> the default."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default


def _steward_file_overrides(path):
    """Read the console-writable ~/steward.json and return a dict of VALIDATED cfg
    overrides to layer on top of the env/default cfg THIS cycle (file > env > const).
    The console writes this file; keeper-relay ONLY READS it (never creates/writes),
    so absence == pure env/default behaviour.

    FAIL-SAFE CONTRACT — this file can trigger a keeper restart, so every failure
    mode degrades to "change nothing" (env/default), never to a MORE restart-prone
    setting, and a malformed file NEVER restarts the keeper:
      * absent / unreadable / not JSON / not an object / wrong-or-missing schema
        -> {} (whole file ignored, log once).
      * a present-but-invalid field is dropped INDIVIDUALLY; other valid fields
        still apply.
    Never raises. Recognised fields (schema "steward.v1"):
      "mode": "off"|"dry"|"on"  -> the two existing knobs (see STEWARD_MODES).
      "timeout_min": int in [LO,HI] -> steward_cadence_sec = timeout_min*60 (the
        cadence FLOOR only; growth still ARMS). Out of range / wrong type / bool
        -> dropped (keep the env/default cadence, which is never MORE eager).
      "threshold_bytes": int in [LO,HI] -> steward_threshold_bytes (the growth-ARM
        bar). Out of range / wrong type / bool -> dropped (keep env/default)."""
    p = path or STEWARD_CONFIG_FILE
    try:
        with open(p, "rb") as fh:
            raw = fh.read(4096)          # this file is tiny — bound the read
    except OSError:
        return {}                        # absent/unreadable -> pure env/default
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log("steward: %s not valid JSON — ignoring (env/default)" % p)
        return {}
    if not isinstance(doc, dict) or doc.get("schema") != STEWARD_CONFIG_SCHEMA:
        log("steward: %s wrong/missing schema — ignoring (env/default)" % p)
        return {}
    out = {}
    # --- mode -> steward (bool) + steward_restart ("dry"/"live") ---
    mode = doc.get("mode")
    if isinstance(mode, str) and mode.strip().lower() in STEWARD_MODES:
        m = mode.strip().lower()
        if m == "off":
            out["steward"] = False
        elif m == "dry":
            out["steward"], out["steward_restart"] = True, "dry"
        else:  # "on"
            out["steward"], out["steward_restart"] = True, "live"
    elif mode is not None:
        log("steward: %s bad mode %r — dropping that field (env/default)" % (p, mode))
    # --- timeout_min -> cadence floor. bool is an int subclass: reject it so a
    #     stray true/false can never read as 1 and shrink the cadence. ---
    tmo = doc.get("timeout_min")
    if isinstance(tmo, int) and not isinstance(tmo, bool) \
            and STEWARD_TIMEOUT_MIN_LO <= tmo <= STEWARD_TIMEOUT_MIN_HI:
        out["steward_cadence_sec"] = float(tmo * 60)
    elif tmo is not None:
        log("steward: %s bad timeout_min %r — dropping that field (env/default)"
            % (p, tmo))
    # --- threshold_bytes -> growth-ARM bar. bool is an int subclass: reject it so a
    #     stray true/false can never read as 1 and drop the arm bar to the floor. ---
    thr = doc.get("threshold_bytes")
    if isinstance(thr, int) and not isinstance(thr, bool) \
            and STEWARD_THRESHOLD_BYTES_LO <= thr <= STEWARD_THRESHOLD_BYTES_HI:
        out["steward_threshold_bytes"] = thr
    elif thr is not None:
        log("steward: %s bad threshold_bytes %r — dropping that field (env/default)"
            % (p, thr))
    # --- threshold_tokens -> THE RESET BAR (operator 2026-07-29/30). The field the
    #     console should offer; threshold_bytes above is now reported-only and
    #     triggers nothing. Same guards: bool rejected as an int subclass, out of
    #     band dropped individually, so a bad value keeps the env/default bar rather
    #     than dropping it to something more restart-prone. ---
    tht = doc.get("threshold_tokens")
    if isinstance(tht, int) and not isinstance(tht, bool) \
            and STEWARD_THRESHOLD_TOKENS_LO <= tht <= STEWARD_THRESHOLD_TOKENS_HI:
        out["steward_threshold_tokens"] = tht
    elif tht is not None:
        log("steward: %s bad threshold_tokens %r — dropping that field (env/default)"
            % (p, tht))
    # --- max_age_min -> time-ARM gate (t89). 0 is VALID and means "explicitly
    #     off" (so the console can disable an env-set gate); otherwise bounded.
    #     bool rejected as an int subclass, same guard as every other field.
    ma = doc.get("max_age_min")
    if isinstance(ma, int) and not isinstance(ma, bool) \
            and (ma == 0 or STEWARD_MAX_AGE_MIN_LO <= ma <= STEWARD_MAX_AGE_MIN_HI):
        out["steward_max_age_sec"] = float(ma * 60)
    elif ma is not None:
        log("steward: %s bad max_age_min %r — dropping that field (env/default)"
            % (p, ma))
    # --- warn_min -> how long BEFORE the restart the warning fires. Until now
    #     this was env-only, so the one knob the operator actually wanted to tune
    #     could not be reached from the console at all (2026-07-25).
    #
    #     Shorter is deliberately better here. A long lead invites drift: the
    #     keeper is told "restart in 15m", keeps working on whatever it was
    #     doing, and either wastes the lead or panic-writes at the end. A short
    #     lead forces the handoff write to happen NOW, which is the entire point
    #     of the warning. 0 is VALID and means "no warning, fire immediately"
    #     (the documented opt-out that _steward_should_restart already honours).
    wm = doc.get("warn_min")
    if isinstance(wm, int) and not isinstance(wm, bool) \
            and (wm == 0 or STEWARD_WARN_MIN_LO <= wm <= STEWARD_WARN_MIN_HI):
        out["steward_warn_lead_sec"] = float(wm * 60)
    elif wm is not None:
        log("steward: %s bad warn_min %r — dropping that field (env/default)"
            % (p, wm))
    # --- lean_context -> Method E 'cache on/off' switch. A bool; when true the
    #     relay runs a TOKEN-BAR context refresh even with mode off (caps
    #     cache_read). Bool only — anything else is dropped. ---
    lc = doc.get("lean_context")
    if isinstance(lc, bool):
        out["steward_lean_context"] = lc
    elif lc is not None:
        log("steward: %s bad lean_context %r — dropping that field" % (p, lc))
    # --- zero_usage -> reversible 'zero the token counters' baseline switch. ---
    zu = doc.get("zero_usage")
    if isinstance(zu, bool):
        out["steward_zero_usage"] = zu
    elif zu is not None:
        log("steward: %s bad zero_usage %r — dropping that field" % (p, zu))
    return out


def _steward_effective_cfg(cfg):
    """A per-cycle COPY of cfg with any valid ~/steward.json overrides layered on
    top (file > env > default). The shared cfg is NEVER mutated. Fail-safe: any
    trouble -> the env/default cfg unchanged (see _steward_file_overrides)."""
    try:
        overrides = _steward_file_overrides(cfg.get("steward_config_path"))
    except Exception as exc:              # defence in depth: the reader never raises
        log("steward: config overlay error (%s) — using env/default"
            % type(exc).__name__)
        return cfg
    if not overrides:
        return cfg
    eff = dict(cfg)
    eff.update(overrides)
    return eff


def _steward_transcript_bytes(cfg):
    """Total bytes of the NEWEST-mtime *.jsonl in the transcript dir = the live
    session's context-so-far. Returns an int (0 if there is no transcript yet), or
    None if the directory can't be read — the caller then skips the cycle rather
    than act on an unknown (never arm/fire on a number we do not have)."""
    d = cfg.get("steward_transcript_dir") or STEWARD_TRANSCRIPT_DIR
    try:
        names = os.listdir(d)
    except OSError:
        return None
    newest, newest_mt = None, -1.0
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        p = os.path.join(d, name)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt > newest_mt:
            newest_mt, newest = mt, p
    if newest is None:
        return 0
    try:
        return os.path.getsize(newest)
    except OSError:
        return None


def _steward_job_session_ids(cfg):
    """The set of background-job session ids to EXCLUDE from the keeper's growth sum
    (board t79/t81). Each Claude Code daemon job leaves ~/.claude/jobs/<shortid>/
    state.json whose "sessionId" IS the basename (minus .jsonl) of the sibling
    transcript that job writes into the shared projects dir. So this set names exactly
    the .jsonl files that belong to background jobs, NOT the interactive keeper session.

    FAIL-SAFE toward INCLUDING (never toward silently zeroing growth): an absent/
    unreadable jobs dir, a job with no/unreadable/garbage state.json, or a missing
    sessionId simply yields no id for that job — the file stays counted. Only a file
    we can POSITIVELY tie to a job is excluded. Never raises."""
    d = cfg.get("steward_jobs_dir") or STEWARD_JOBS_DIR
    ids = set()
    try:
        entries = os.listdir(d)
    except OSError:
        return ids                          # no jobs dir -> exclude nothing
    for name in entries:
        sp = os.path.join(d, name, "state.json")
        try:
            with open(sp, "rb") as fh:
                doc = json.loads(fh.read(65536).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue                         # mid-spawn / not a job dir -> skip (include)
        if not isinstance(doc, dict):
            continue
        sid = doc.get("sessionId") or doc.get("resumeSessionId")
        if isinstance(sid, str) and sid:
            ids.add(sid)
    # NEVER exclude the session the keeper pane is actually running in
    # (2026-07-25). "Background job" and "the interactive keeper" used to be
    # disjoint; they are not guaranteed to be any more, and if the keeper's own
    # session ever acquires a ~/.claude/jobs entry it would match this exclusion
    # and the steward would stop counting the one session that matters.
    #
    # Fail-safe direction is INCLUDE (same posture as the rest of this function):
    # if the keeper session cannot be determined we drop nothing from the
    # exclusion set, leaving the previous behaviour rather than guessing.
    live = _steward_keeper_session_id(cfg)
    if live:
        ids.discard(live)
    return ids


def _steward_session_records():
    """Every ~/.claude/sessions/<pid>.json record whose pid is STILL ALIVE, as
    {sessionId: record}. The daemon leaves records behind when a session exits,
    so an os.kill(pid, 0) liveness probe is what separates live from stale.
    Never raises: any trouble -> {} -> callers change nothing."""
    d = os.path.expanduser("~/.claude/sessions")
    out = {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), "rb") as fh:
                doc = json.loads(fh.read(65536).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        sid, pid = doc.get("sessionId"), doc.get("pid")
        if not (isinstance(sid, str) and sid and isinstance(pid, int)):
            continue
        try:
            os.kill(pid, 0)               # liveness probe only; sends no signal
        except OSError:
            continue                      # stale record -> that session is gone
        out[sid] = doc
    return out


def _steward_keeper_session_id(cfg):
    """Session id of the session running in the KEEPER PANE, or None.

    Identity comes from the pane the relay actually injects into — not from
    "which session looks busy". Several sessions can be busy at once (the pane
    keeper plus background sessions), so status alone picks the wrong one; the
    pane pid is the relay's own definition of "the keeper" and is unambiguous.
    Matches the pane pid, or any of its ancestors, against the live session
    records. Never raises."""
    try:
        out, err = tmux(cfg, "list-panes", "-a", "-F", "#{pane_pid}")
    except Exception:
        return None
    if err or not out:
        return None
    pane_pids = set()
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pane_pids.add(int(line))
    if not pane_pids:
        return None
    recs = _steward_session_records()
    for sid, doc in recs.items():
        pid = doc.get("pid")
        # walk up from the session pid: the pane pid is usually its ancestor
        seen, cur = 0, pid
        while isinstance(cur, int) and cur > 1 and seen < 12:
            if cur in pane_pids:
                return sid
            try:
                with open("/proc/%d/stat" % cur, "rb") as fh:
                    cur = int(fh.read().decode().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                break
            seen += 1
    return None


def _steward_context_tokens(path, tail_bytes=4194304):
    """The keeper's ACTUAL context size in tokens = the last usage record PLUS an
    estimate of any content written AFTER it (pending, not-yet-processed — e.g. a
    big skill or file just loaded). The pending term is what makes a 'must-compact'
    state visible on the meter instead of hiding it behind the last completed turn.
    Returns None when it cannot be determined.

    WHY THIS EXISTS (operator, 2026-07-25: "the files on disk are not what is to
    be watched. its claudes code build up in claude code"). The steward measured
    transcript BYTES and inferred context from them. Those two quantities drift
    badly — measured on this machine the same day:

        1.3 MB -> 164,029 tok   (~126 tok/KB)
        0.2 MB ->  55,675 tok   (~278 tok/KB)
        0.03 MB -> 15,350 tok   (~512 tok/KB)

    A 4x spread, because tool output and file reads bloat the FILE at a very
    different rate than they bloat CONTEXT. So a byte threshold fires on a lean
    session and spares a bloated one — which is exactly the complaint that the
    reset "amplifies the problem it was created to solve".

    The transcript records what the model itself reported consuming, per turn:
    input + cache_read + cache_creation is the context that was actually sent.
    That is the number worth acting on, and it is the only one visible from
    outside Claude Code — compaction and history management happen IN-PROCESS,
    where the relay cannot see or influence them. We can measure, not control.

    Reads only the TAIL (default 512 KB): the last usage record is what matters
    and these files reach megabytes. A partial first line from mid-file seeking
    is expected and simply fails to parse — hence the per-line try/except."""
    try:
        size = os.path.getsize(path)
        start = max(0, size - tail_bytes)
        with open(path, "rb") as fh:
            fh.seek(start)
            raw = fh.read()
    except OSError:
        return None
    last = None
    last_end = None                        # absolute byte offset past the last usage line
    pos = start
    for line in raw.split(b"\n"):
        ln = len(line) + 1                 # +1 for the newline split() removed
        if b'"usage"' in line:             # cheap prefilter; most lines have none
            try:
                doc = json.loads(line.decode("utf-8", "replace"))
                msg = doc.get("message")
                u = msg.get("usage") if isinstance(msg, dict) else None
                # skip SYNTHETIC / zero-usage entries (model "<synthetic>", or a
                # placeholder with all counts 0) — they are not real turns and would
                # anchor last_end at EOF, hiding the pending content behind them.
                if isinstance(u, dict) and any(int(u.get(k) or 0) for k in
                        ("input_tokens", "cache_read_input_tokens",
                         "cache_creation_input_tokens")):
                    last = u
                    last_end = pos + ln
            except ValueError:
                pass                       # truncated first line, or not JSON
        pos += ln
    # PENDING content: bytes written AFTER the last usage record are context that
    # the NEXT turn must send but that has no usage record yet (a big skill/file
    # just loaded). Estimated at ~4 bytes/token — rough, but it is the only signal
    # for not-yet-processed content, and without it the meter hides a compact-due
    # session behind the last completed turn.
    if not last:
        return (size // 4) or None         # no usage record in range -> all pending
    total = 0
    for k in ("input_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens"):
        v = last.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            total += v
    total += max(0, size - (last_end if last_end is not None else size)) // 4
    return total or None


def _steward_stale_transcript_bytes(cfg):
    """Bytes in the inventory belonging to sessions whose process is GONE.

    Reported, never subtracted: the growth math is anchored to a
    transcript_bytes_at_reset baseline recorded when those files WERE counted, so
    dropping them mid-session would read as a huge phantom shrink and could even
    un-arm a legitimately over-budget keeper.

    But it explains a number that otherwise looks broken. Observed 2026-07-25:
    the drawer read 605,123 bytes of which 423,583 (70%) belonged to three dead
    sessions — the largest already `claude rm`ed — leaving only 181,540 that
    could ever move. The operator asked why typing did not change the figure;
    this is most of the answer. Returns 0 if liveness cannot be determined."""
    inv = _steward_transcript_inventory(cfg)
    if not inv:
        return 0
    recs = _steward_session_records()
    if not recs:
        return 0                          # can't tell -> claim nothing is stale
    stale = 0
    for name, size in inv.items():
        if name[:-6] not in recs:         # strip ".jsonl" -> session id
            stale += size
    return stale


def _steward_max_context_tokens(cfg):
    """Context of the LIVE (most recently written) keeper transcript, or None.

    2026-07-27 (operator: "isnt the token reader reading a session in the target
    vm itself?" — yes, and it was reading the WRONG session). This used to take the
    MAX across every transcript in the dir, on the reasoning that each session has
    its own window so the question is "is ANY keeper bloated". In practice the dir
    accumulates FINISHED sessions, and a dead one's high-water mark simply wins
    forever: measured this day it reported 380,010 tokens from a session dead ~10h
    while the session actually running sat at 161,886. The steward then judges a
    live keeper by a number that has nothing to do with it, and the console's meter
    shows a stale figure as if it were current.

    A dead transcript is not a keeper that needs restarting — it is a file. What
    the steward acts on, and what the operator reads on the meter, must be the
    session that is actually accumulating context RIGHT NOW: the one with the
    newest mtime. Ties (a fresh sidecar written the same second) resolve to the
    larger context, so a real reading is never lost to a rounding coincidence.

    mtime, not ctime: ctime here is creation-ish and answers "how old is this
    file", which is what live_transcript_age_sec deliberately reports. "Which
    session is live" is a question about the last WRITE."""
    d = cfg.get("steward_transcript_dir") or STEWARD_TRANSCRIPT_DIR
    inv = _steward_transcript_inventory(cfg)
    if not inv:
        return None
    best = None                    # (mtime, tokens) of the winner so far
    for name in inv:
        p = os.path.join(d, name)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue               # vanished mid-cycle; a missing file is not a reading
        t = _steward_context_tokens(p)
        if t is None:
            continue               # no usage record yet (a just-created transcript)
        if best is None or (mt, t) > best:
            best = (mt, t)
    return best[1] if best else None


def _steward_transcript_inventory(cfg):
    """An INVENTORY {relpath -> size} of the KEEPER'S transcript content in the dir —
    every *.jsonl EXCEPT the background-job siblings (their session ids, see
    _steward_job_session_ids), PLUS each live session's SIDECAR files. This is the
    per-file metric that replaces the old single-newest-file reading: summing appended
    bytes across the inventory is immune to the newest-mtime title flipping between
    concurrent files. Returns the dict (empty if there are no keeper files yet), or None
    if the directory can't be read — the caller then skips the cycle rather than act on
    an unknown (never arm/fire on numbers we do not have). Files that vanish between
    listdir and getsize are simply dropped.

    SIDECARS (2026-07-25, operator: "it needs to reflect every single line"). A session
    does not live only in <sid>.jsonl. Claude Code also writes a sibling DIRECTORY
    <sid>/ holding subagents/*.jsonl and tool-results/*.txt — externalized transcript
    content that is every bit as real as the main file. Counting only the top-level
    *.jsonl measured 4.8% of actual bytes here (477 KB seen of 9.84 MB on disk), so a
    subagent-heavy session looked idle to the steward while it was the most expensive
    kind of session there is.

    ONLY LIVE SIDECARS COUNT. The archive step moves <sid>.jsonl but historically left
    <sid>/ behind, so the dir is littered with sidecars whose parent transcript is long
    gone. Those bytes are DEAD — already shed, already paid for — and counting them
    would add a permanent constant to growth and fire the steward on tick 1. So a
    sidecar is included only when its parent <sid>.jsonl is present in this same listing
    (i.e. the session is still live and un-archived). An orphan contributes nothing.
    Same fail-safe posture as the rest of this function: anything unreadable is skipped,
    never guessed at."""
    d = cfg.get("steward_transcript_dir") or STEWARD_TRANSCRIPT_DIR
    try:
        names = os.listdir(d)
    except OSError:
        return None
    exclude = _steward_job_session_ids(cfg)
    inv = {}
    live_sids = set()
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        sid = name[:-len(".jsonl")]
        if sid in exclude:
            continue                         # a background-job transcript -> not ours
        try:
            inv[name] = os.path.getsize(os.path.join(d, name))
        except OSError:
            continue                         # raced away -> just omit it this poll
        live_sids.add(sid)
    # Sidecar sweep: only for sessions whose .jsonl we just counted (live, ours).
    for sid in live_sids:
        sub = os.path.join(d, sid)
        if not os.path.isdir(sub):
            continue                         # no sidecar for this session -> nothing to add
        for root, _dirs, files in os.walk(sub):
            for f in files:
                p = os.path.join(root, f)
                try:
                    # key by path RELATIVE to the transcript dir so the inventory stays
                    # a flat {name -> size} map that _steward_inventory_growth can diff
                    # across cycles exactly as it does for top-level files.
                    inv[os.path.relpath(p, d)] = os.path.getsize(p)
                except OSError:
                    continue                 # raced away mid-walk -> omit it this poll
    return inv


def _steward_inventory_growth(snapshot, current):
    """Real appended bytes across the inventory since `snapshot` was taken. For each
    file in `current`: existing in the snapshot -> max(0, cur - snap) (only genuine
    growth, a shrink/truncate contributes 0); NEW since the snapshot -> its full
    current size (all of it is post-reset content); a snapshot file that shrank or
    VANISHED contributes 0 by simply not being summed here — never a negative. So the
    sum is monotonic in real activity and can never be dragged below 0, which is exactly
    what defeats the identity-flip and the post-live-restart fresh-file cases. `snapshot`
    and `current` are {name -> size} dicts (a missing/None snapshot counts everything)."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    cur = current if isinstance(current, dict) else {}
    total = 0
    for name, size in cur.items():
        prev = snap.get(name)
        if isinstance(prev, (int, float)):
            total += max(0, int(size) - int(prev))
        else:
            total += int(size)               # not in the snapshot -> new, count in full
    return total


def _steward_growth_since_reset(sd, cur_inv):
    """The single source of the growth number the whole steward keys off: inventory
    growth (see _steward_inventory_growth) between the snapshot taken at the last reset
    (sd["transcript_inventory_at_reset"]) and the current inventory `cur_inv`. A pre-t81
    state dict (no inventory field yet) degrades gracefully to counting the current
    inventory in full — conservative (over-counts once), never a spurious zero."""
    return _steward_inventory_growth(
        sd.get("transcript_inventory_at_reset"), cur_inv)


def _steward_seed(cfg, state, now, cur_inv):
    """First run: snapshot the current transcript INVENTORY as the baseline and start
    the clock, phase idle, and act on NOTHING — the same silent seeding the other
    watchers do, so enabling the steward never fires a spurious restart on tick 1.
    transcript_bytes_at_reset (the summed snapshot size) is retained for observability
    and back-compat; the arming metric is the inventory, not that scalar."""
    state["steward"] = {
        "phase": "idle",
        "armed_ts": None,
        "warned_ts": None,
        "deadline": None,
        "transcript_inventory_at_reset": dict(cur_inv),
        "transcript_bytes_at_reset": sum(cur_inv.values()),
        "last_reset_ts": now,
    }
    save_state(state)
    log("steward: first run — seeded transcript inventory baseline=%d bytes "
        "across %d file(s), phase idle" % (sum(cur_inv.values()), len(cur_inv)))


def _steward_reset(state, now, cur_inv):
    """Restart consumed (dry OR live): new INVENTORY baseline, clock restarts, phase
    idle, arm/warn cleared. In dry mode the transcript identity is unchanged so the new
    snapshot measures growth from here; after a LIVE restart the new session opens a
    fresh (smaller) jsonl — the inventory metric treats it as a NEW file counted from 0
    (never a negative from the vanished old file), so growth reads conservatively LOW
    until real work accrues — safe (never over-eager), never a restart loop."""
    sd = state["steward"]
    sd["phase"] = "idle"
    sd["armed_ts"] = None
    sd["warned_ts"] = None
    sd["deadline"] = None
    sd["transcript_inventory_at_reset"] = dict(cur_inv)
    sd["transcript_bytes_at_reset"] = sum(cur_inv.values())
    sd["last_reset_ts"] = now
    # The warning belonged to the session that just ended. Clear both the pending
    # deadline and the on-disk notice, or the incoming keeper reads WARNING.md and
    # believes it is about to be killed.
    sd.pop("pending_fire_ts", None)
    sd.pop("pending_reason", None)
    _steward_clear_warning()


def _steward_enqueue_warn(state, text, now):
    """Queue ONE [steward] line through the SAME idle-gated enqueue/drain as every
    other injection — deferred (never dropped) while a turn is in flight, typed at
    most once per tick into a confirmed-idle pane. Sanitized like every other line
    (it is our own text, but the discipline stays uniform)."""
    line = sanitize(text)[0]
    enqueue(state, {"ts": now, "author": "steward", "payload": line,
                    "sha256": digest(line), "prefix": line[:40]})


def _steward_write_warning(now, fire_ts, growth, threshold, reason,
                           tokens=None, threshold_tokens=0):
    """Write ~/handoff/WARNING.md — the warning channel that CANNOT stall.

    `tokens`/`threshold_tokens` are the REAL trigger (operator 2026-07-29/30);
    growth/threshold stay in the signature and in the body as the secondary
    transcript figure, clearly labelled as not being the bar. Both are optional so
    the legacy warned-phase call site keeps working.

    The enqueue/drain path is idle-gated by design (nothing may be typed into a busy
    pane), which is exactly why the old warning never arrived on a working keeper.
    A file write has no such gate: the keeper sees it whenever it next looks, and a
    failure here is swallowed — a warning that cannot be written must never block
    the restart it is warning about."""
    left = max(0, int(fire_ts - now))
    body = (
        "# STEWARD RESTART IMMINENT\n\n"
        "**This keeper will be restarted in ~%dm (at %s UTC).**\n\n"
        "It fires UNCONDITIONALLY — mid-turn if necessary. There is no lull to wait\n"
        "for, no hold that defers it, and no second warning.\n\n"
        "## STOP AND DOCUMENT — this is the only task that matters now\n\n"
        "Do not start new work. Do not finish what you were doing unless it takes\n"
        "under a minute. Write `~/handoff/next.md` FIRST; it is rotated to\n"
        "`~/handoff/last.md` on restart and is the ONLY thing the next keeper reads.\n"
        "Anything not written there is lost.\n\n"
        "It must open with what the next keeper should do FIRST, then cover:\n\n"
        "- what you were mid-way through, and how far it got\n"
        "- what the operator asked for that is NOT done yet\n"
        "- anything left in a broken or half-edited state (be explicit)\n"
        "- decisions already made, so they are not re-litigated\n"
        "- what NOT to do — assessments already run, paths already ruled out\n\n"
        "A half-finished task documented well is worth more than a finished one\n"
        "nobody knows about.\n\n"
        "## Why\n\n"
        "- trigger: %s\n"
        "- context: %s tokens against a %s token budget  ← the bar\n"
        "- transcript growth: %d bytes (reported only; bytes trigger nothing)\n"
        "- warned at: %s UTC\n"
    ) % (left // 60, time.strftime("%H:%M:%S", time.gmtime(fire_ts)),
         reason,
         "unreadable" if tokens is None else "{:,}".format(tokens),
         "{:,}".format(threshold_tokens) if threshold_tokens else "off",
         growth,
         time.strftime("%H:%M:%S", time.gmtime(now)))
    try:
        os.makedirs(HANDOFF_DIR, exist_ok=True)
        tmp = HANDOFF_WARNING + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, HANDOFF_WARNING)   # atomic: never a partial read
        return True
    except OSError as exc:
        log("steward: warning write failed (%s) — restart proceeds regardless"
            % type(exc).__name__)
        return False


def _steward_clear_warning():
    """Consume the warning once the restart has fired, so a stale WARNING.md can
    never make the next keeper think it is about to die."""
    try:
        os.remove(HANDOFF_WARNING)
    except OSError:
        pass


def _steward_read_hold():
    """Raw contents of ~/handoff/hold, or None if absent/unreadable. Data, never a
    crash (the keeper may be mid-write). Bounded read — a hold is a tiny token."""
    try:
        with open(HANDOFF_HOLD, encoding="utf-8") as fh:
            return fh.read(256)
    except OSError:
        return None


def _steward_clear_hold():
    """Consume the hold after applying/rejecting it, so each extension is a
    deliberate fresh write. Tolerant of an already-absent file."""
    try:
        os.remove(HANDOFF_HOLD)
    except OSError:
        pass


def _steward_parse_hold(raw, now):
    """Parse a hold request to a target epoch, or None if it is not a FUTURE time.
    Accepts a relative '+Nm'/'+Nh' or a raw epoch (int/float seconds)."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    m = _STEWARD_HOLD_RE.fullmatch(s)
    if m:
        n = int(m.group(1))
        target = now + n * (60 if m.group(2) == "m" else 3600)
        return target if target > now else None   # +0m is not a future extension
    try:
        target = float(s)
    except ValueError:
        return None
    return target if target > now else None


def _steward_rotate_handoff(now, cfg=None):
    """ALWAYS on restart (dry OR live): rotate the handoff. next.md -> last.md
    (atomic) if the outgoing keeper left one; else write a last.md STUB noting no
    handoff was left. Creates ~/handoff/ if missing. Returns whether a real handoff
    was present. Never raises — a broken handoff must not wedge the restart."""
    try:
        os.makedirs(HANDOFF_DIR, exist_ok=True)
    except OSError as exc:
        log("steward: could not create handoff dir %s: %s" % (HANDOFF_DIR, exc))
        return False
    try:
        with open(HANDOFF_NEXT, "rb") as fh:
            data = fh.read()
        present = True
    except OSError:
        data = ("no handoff was left (steward restart at %s)\n"
                % time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now))
                ).encode("utf-8")
        present = False
    tmp = HANDOFF_LAST + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, HANDOFF_LAST)     # atomic: a reader never sees a partial file
    except OSError as exc:
        log("steward: handoff rotation write failed: %s" % type(exc).__name__)
        try:
            os.remove(tmp)
        except OSError:
            pass
    _steward_mirror_handoff(cfg)          # best-effort fleet-common mirror (never gates)
    return present


def _steward_mirror_handoff(cfg=None):
    """ADDITIVE best-effort mirror of the authoritative local handoff
    (~/handoff/{next,last}.md) to the fleet-common shared dir (STATION-namespaced, so
    every VM's handoff is readable from one predictable place without collisions). The
    local files stay authoritative and are untouched. NEVER raises and NEVER
    blocks/alters the restart: a failed mirror (missing share, permissions) is logged
    and swallowed. Each file is copied atomically; a source that isn't present (e.g. no
    next.md was left) is skipped. Base dir: cfg override > STATION-derived default."""
    base = (cfg or {}).get("handoff_shared_dir") or HANDOFF_SHARED_DIR
    shared_next = os.path.join(base, "next.md")
    shared_last = os.path.join(base, "last.md")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError as exc:
        log("steward: shared handoff dir unavailable (%s) — mirror skipped"
            % type(exc).__name__)
        return
    for src, dst in ((HANDOFF_NEXT, shared_next),
                     (HANDOFF_LAST, shared_last)):
        try:
            with open(src, "rb") as fh:
                data = fh.read()
        except OSError:
            continue                      # nothing to mirror from this source -> skip
        tmp = dst + ".tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dst)          # atomic: a reader never sees a partial file
        except OSError as exc:
            log("steward: shared handoff mirror failed for %s (%s)"
                % (dst, type(exc).__name__))
            try:
                os.remove(tmp)
            except OSError:
                pass


def _tmux_gone(reason):
    """True when a tmux error string means the session/server itself vanished.
    On a topology where claude is the session's ROOT process (station-keeper
    launch), /exit ends the session and — if it was the last one — the server;
    every later tmux call then fails with one of these. That is the steward's
    GOAL state (TUI gone), with relaunch owned by the supervising unit."""
    s = str(reason or "")
    return ("no server running" in s) or ("can't find session" in s) \
        or ("can't find window" in s) or ("can't find pane" in s)


def _pane_pid(cfg):
    """The pid running in the keeper pane, or None. tmux already tracks it."""
    out, err = tmux(cfg, "display-message", "-p", "-t", cfg["target"],
                    "#{pane_pid}")
    if err or not out:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _steward_live_restart(cfg):
    """The ONLY pane-mutating steward code. Minimal by design (§4): type a graceful
    '/exit' into the confirmed-idle claude TUI via the SAME send path (inject), wait
    for the TUI to leave, then type the standard launcher into the shell the pane
    falls back to. If the /exit instead ends the whole session (claude as session
    root — the station-keeper topology), that IS success: the supervising unit
    relaunches. Returns (ok, reason). Never raises."""
    try:
        # t90: drop the fresh-reset marker FIRST, so however the session ends the
        # launcher boots a NEW conversation (archives transcripts, reorients from
        # ~/handoff/next.md) instead of --continue reloading everything. A reset
        # exists to SHED context and cut cost; a resumed session shed nothing.
        # Best-effort — a marker-write failure must not block the restart.
        try:
            with open(STEWARD_RESET_MARKER, "w") as fh:
                fh.write("t90 fresh-reset\n")
        except OSError as exc:
            log("steward: fresh-marker write failed (%s) — restart will resume"
                % type(exc).__name__)
        # 1. THE RESET IS `/clear` (operator 2026-07-29). Not a kill, not /exit:
        #    /clear drops the conversation context IN PLACE, which is the entire
        #    point of the reset (shed tokens) without destroying the session, the
        #    tmux pane, or anything the launcher would have to rebuild. It is also
        #    why the grace period matters — the keeper writes its handoff first,
        #    then /clear lands and the next keeper reads that handoff back.
        #
        #    After it lands we PING the fresh keeper to reorient (step 2), because a
        #    cleared session knows nothing: no directive, no handoff, no idea a reset
        #    just happened. A silent /clear would leave a blank keeper staring at an
        #    empty pane.
        #
        # o20 MIRROR ORDERING: re-mirror the handoff NOW, after the grace window
        # has closed. The rotation-time mirror runs at the START of the reset,
        # but the keeper writes its real ~/handoff/next.md DURING the 3-minute
        # grace — so the rotation-time copy is always the pre-grace file and the
        # fleet mirror served stale state (observed 4 days stale, 2026-07-29).
        # Best-effort and never gates, so the second call is safe.
        _steward_mirror_handoff(cfg)
        ok, reason = inject(cfg, "/clear")
        if ok:
            # Give the TUI a moment to process /clear before the reorient ping, so
            # the two do not race into the same input line.
            time.sleep(STEWARD_CLEAR_SETTLE_SEC)
            ping_ok, ping_reason = inject(cfg, STEWARD_REORIENT_TEXT)
            if not ping_ok:
                # The clear DID land; failing to reorient is degraded, not failed —
                # say so precisely rather than reporting a reset that did not happen.
                log("steward: /clear landed but reorient ping failed (%s)"
                    % ping_reason)
                return True, "cleared-reorient-failed:%s" % ping_reason
            return True, "cleared-and-reoriented"
        if _tmux_gone(reason):
            return True, "session-ended-at-clear-supervisor-relaunches"
        # /clear could not be typed (pane not in the TUI, or inject failed). Fall
        # back to the older, heavier paths rather than silently skipping the reset.
        log("steward: /clear inject failed (%s) — falling back to /exit" % reason)
        ok, reason = inject(cfg, "/exit")
        if not ok:
            if _tmux_gone(reason):
                return True, "session-ended-at-exit-supervisor-relaunches"
            return False, "exit-inject-failed:%s" % reason
        # 2. wait for the claude process to actually leave the pane.
        gone, err = wait_for(cfg, lambda st: st["cmd"] != EXPECT_CMD,
                             STEWARD_EXIT_TIMEOUT)
        if not gone:
            if _tmux_gone(err):
                return True, "session-ended-after-exit-supervisor-relaunches"
            return False, "tui-did-not-exit:%s" % err
        # 3. relaunch the standard keeper command in the SAME tmux session. The
        #    pane has fallen back to a shell; send the launcher line + a separate
        #    Enter (literal argv, never interpolated — the two-call shape inject
        #    uses). STATION defaults to the hostname inside the launcher.
        _, err = tmux(cfg, "send-keys", "-t", cfg["target"], "-l", "--",
                      KEEPER_LAUNCH_CMD, want_out=False)
        if err:
            if _tmux_gone(err):
                return True, "session-ended-before-relaunch-supervisor-relaunches"
            return False, "relaunch-literal-failed:%s" % err
        _, err = tmux(cfg, "send-keys", "-t", cfg["target"], "Enter",
                      want_out=False)
        if err:
            if _tmux_gone(err):
                return True, "session-ended-before-relaunch-supervisor-relaunches"
            return False, "relaunch-enter-failed:%s" % err
        return True, "relaunched"
    except Exception as exc:              # a restart bug must never crash the daemon
        return False, "live-restart-exception:%s" % type(exc).__name__


def _steward_restart(cfg, state, now, cur_inv):
    """Execute the armed restart. Reached only once the warned-phase idle gate has
    already passed. The handoff is rotated (in BOTH modes) ONLY once we commit to
    proceeding — a live re-check that finds the lull gone DEFERS with no side
    effects, so it can't consume next.md and then overwrite last.md on a retry."""
    if cfg.get("steward_restart") == "live":
        # --- LIVE (off by default; the operator flips KEEPER_RELAY_STEWARD_RESTART
        #     to "live" only after validating this path). Guard HARD: the pane is
        #     mutated only with BOTH the env AND a freshly-confirmed idle gate. ---
        # NO LULL RE-CHECK (operator, 2026-07-24: "just make it reset on session
        # budget or time"). This was the SECOND idle gate: even with the warned-phase
        # gate removed, this one deferred a live restart whenever the pane was busy —
        # which, for a keeper working alongside the operator, is always. Budget or
        # time arms it; the warning + deadline give the keeper its chance to wrap up;
        # nothing else may veto the fire.
        present = _steward_rotate_handoff(now, cfg)
        ok, why = _steward_live_restart(cfg)
        audit({"decision": "steward-restart", "mode": "live",
               "would_run": KEEPER_LAUNCH_CMD, "handoff": present,
               "fired": bool(ok), "reason": why, "queued": False})
        log("steward: LIVE restart %s (%s)" % ("issued" if ok else "FAILED", why))
        if not ok:
            # A FAILED restart must NOT reset the baseline. It used to, and the
            # effect was that a restart which shed nothing still told the state
            # machine it had succeeded: growth dropped to 0 and the steward went
            # quiet for another whole budget window while the transcript kept
            # growing. Observed live — tui-did-not-exit:timeout, then growth 0.
            # Keep the baseline so the next cycle retries, and drop the fresh-reset
            # marker we optimistically wrote (nothing exited; leaving it armed would
            # make an unrelated relaunch archive transcripts out of nowhere).
            try:
                os.unlink(STEWARD_RESET_MARKER)
            except OSError:
                pass
            save_state(state)
            return
        _steward_reset(state, now, cur_inv)
        save_state(state)
        return

    # DRY-RUN DEFAULT: rotate the handoff, log what WOULD run, reset — WITHOUT ever
    # touching the pane. This lets the full arm->warn->lull->(would-restart)->reset
    # loop be watched safely.
    present = _steward_rotate_handoff(now, cfg)
    log("steward: DRY-RUN would restart (handoff=%s) — %s"
        % (present, KEEPER_LAUNCH_CMD))
    audit({"decision": "steward-restart", "mode": "dry",
           "would_run": KEEPER_LAUNCH_CMD, "handoff": present, "queued": False})
    _steward_reset(state, now, cur_inv)
    save_state(state)


def _steward_should_restart(cfg, state, now, cur):
    """Over budget OR over time -> restart. That is the entire decision.

    Replaces the arm -> warn -> wait-for-lull state machine, which never once
    completed on a working keeper (operator: "the warnings, the lull, everything
    hasn't panned out ... it needs to all be scrapped, and simply work"). Every
    stage was a place to stall, and each one did:
      - the cadence vetoed the byte budget (growth wasn't even measured)
      - the warn phase waited for a lull
      - the live restart re-checked for a lull and deferred again
    A keeper working alongside the operator is never idle, so the machine armed,
    warned, and waited forever at 8x its budget.

    The anti-thrash floor is the ONLY remaining gate: a hard minimum between
    restarts so a burst can't chain them. Capped by the configured cadence, so
    cadence=0 still means "immediately".
    """
    sd = state.get("steward") or {}
    last = sd.get("last_reset_ts")
    if last is None:
        return False
    age = now - last
    # THE BUDGET IS TOKENS (operator, 2026-07-29/30). `tokens` is the LIVE session's
    # context as the model itself reported it; None means unmeasurable, which is NOT
    # zero and NOT a trigger. growth/threshold_bytes are still computed because the
    # audit records and the warning file report them — they decide nothing.
    growth = _steward_growth_since_reset(sd, cur)
    threshold_bytes = int(cfg.get("steward_threshold_bytes") or 0)
    tokens = _steward_max_context_tokens(cfg)
    threshold = int(cfg.get("steward_threshold_tokens") or 0)

    # --- A warning already stands: the ONLY question is whether its deadline has
    #     passed. Nothing may veto the fire here — not the anti-thrash floor (the
    #     warning was issued after it cleared), not a lull, not a hold. This is the
    #     promise the warning makes, and breaking it is what made the old machine
    #     wait forever. ---
    pending = sd.get("pending_fire_ts")
    if pending is not None:
        if now >= pending:
            log("steward: RESTART — warned deadline reached (%s)"
                % sd.get("pending_reason", "unknown"))
            audit({"decision": "steward-trigger",
                   "reason": "warned-deadline:%s" % sd.get("pending_reason", "unknown"),
                   "context_tokens": tokens, "threshold_tokens": threshold,
                   "growth_bytes": growth, "threshold_bytes": threshold_bytes,
                   "queued": False})
            return True
        return False                      # inside the lead — let the keeper wrap up

    if age < _anti_thrash_sec(cfg):
        return False

    # The LEGACY warned phase carries its own deadline and is handled by the warned
    # branch of the cycle. Arming a second, later unconditional fire on top of it
    # would push the restart FURTHER OUT (pending_fire_ts outranks sd["deadline"] in
    # the snapshot) — a warning that defers the thing it warns about, which is the
    # exact stall this rewrite exists to kill. One warning per session, period.
    if sd.get("phase") == "warned":
        return False

    reason = None
    if threshold and tokens is not None and tokens >= threshold:
        reason = "budget"
    else:
        max_age = cfg.get("steward_max_age_sec") or 0
        if max_age and age >= max_age:
            reason = "max-age"
    if reason is None:
        # VISIBILITY for the one new failure mode: a session whose context cannot be
        # measured has no budget trigger at all, so it must not fail SILENTLY the way
        # an under-budget session does. Log once per unmeasurable streak (not every
        # 15s tick), and clear the streak as soon as a reading returns.
        if threshold and tokens is None:
            if not state.get("steward_tokens_unreadable"):
                state["steward_tokens_unreadable"] = True
                save_state(state)
                log("steward: context tokens unreadable — the token budget cannot "
                    "trigger; no reset will fire on budget until a usage record "
                    "appears (max-age cap: %s)"
                    % (("%dm" % ((cfg.get("steward_max_age_sec") or 0) / 60))
                       if cfg.get("steward_max_age_sec") else "off"))
                audit({"decision": "steward-budget-blind",
                       "reason": "context-tokens-unreadable",
                       "threshold_tokens": threshold, "queued": False})
        elif state.get("steward_tokens_unreadable"):
            state["steward_tokens_unreadable"] = False
            save_state(state)
            log("steward: context tokens readable again (%s tok)" % tokens)
        return False
    if state.get("steward_tokens_unreadable"):
        state["steward_tokens_unreadable"] = False
        save_state(state)

    # --- Trigger met: WARN now, fire after the lead. A zero lead keeps the old
    #     fire-immediately behaviour (and is what the tests for an un-warned
    #     restart expect), so the warning is opt-out, not mandatory. ---
    lead = float(cfg.get("steward_warn_lead_sec") or 0)
    if lead <= 0:
        log("steward: RESTART — %s (%s tok >= %s budget; no warning lead configured)"
            % (reason, tokens, threshold))
        audit({"decision": "steward-trigger", "reason": reason,
               "context_tokens": tokens, "threshold_tokens": threshold,
               "growth_bytes": growth, "threshold_bytes": threshold_bytes,
               "queued": False})
        return True

    fire_ts = now + lead
    sd["pending_fire_ts"] = fire_ts
    sd["pending_reason"] = reason
    _steward_write_warning(now, fire_ts, growth, threshold_bytes, reason,
                           tokens=tokens, threshold_tokens=threshold)
    _steward_enqueue_warn(state, STEWARD_WARN_TEXT % (lead // 60), now)
    save_state(state)
    log("steward: WARNED — %s (%s tok >= %s budget); restart fires in %dm "
        "(unconditional)" % (reason, tokens, threshold, lead // 60))
    audit({"decision": "steward-warn", "reason": reason,
           "context_tokens": tokens, "threshold_tokens": threshold,
           "growth_bytes": growth, "threshold_bytes": threshold_bytes,
           "fire_ts": int(fire_ts), "lead_sec": int(lead), "queued": True})
    return False


def _steward_idle(cfg, state, now, cur):
    """BUDGET and CLOCK are PEERS: either one arms on its own, neither vetoes the
    other (operator, 2026-07-24). Previously the cadence was an unconditional early
    return, so growth was not even MEASURED until the clock allowed it — a session
    could blow ~20x past threshold_bytes and still sit unarmed for the full cadence,
    which is the "reset amplifies the problem it was created to solve" defect.

    Two independent arm paths:
      - BUDGET: inventory growth since the last reset >= threshold_bytes
      - CLOCK:  session age >= max_age_sec
    ANTI_THRASH_SEC is the only floor left: a hard minimum between resets so a burst
    cannot fire back-to-back restarts. It is deliberately tiny next to either bar.
    Below FLOOR with no clock trigger is the idle-session skip — silent, nothing
    logged. `cur` is the current transcript inventory ({name -> size}); growth is the
    per-file sum, not a scalar diff, so a newest-file identity flip can't clamp it."""
    sd = state["steward"]
    age = now - sd["last_reset_ts"]
    if age < _anti_thrash_sec(cfg):
        return                            # too soon after the last reset, full stop
    activity = _steward_growth_since_reset(sd, cur)
    # CLOCK path (t89): past max_age, arm regardless of growth — idle-but-old
    # sessions still get reset. No longer gated behind the cadence early return.
    #
    # OPERATOR 2026-07-29: the reset has ONE execution parameter — TOKEN USAGE.
    # The clock path is therefore OFF unless a station explicitly sets max_age
    # (STEWARD_MAX_AGE_MIN already defaults to 0 = off, so this is a no-op on a
    # default fleet and stays available for a station that deliberately wants it).
    # Rationale for keeping the code rather than deleting it: an idle-but-ancient
    # session costs nothing in tokens, which is exactly why age must not fire on its
    # own — but a station running an unattended agent may still want the cap.
    max_age = cfg.get("steward_max_age_sec") or 0
    if max_age and age >= max_age:
        sd["phase"] = "armed"
        sd["armed_ts"] = now
        save_state(state)
        audit({"decision": "steward-arm", "reason": "max-age",
               "activity_bytes": activity, "elapsed_sec": int(age),
               "max_age_sec": int(max_age), "queued": False})
        log("steward: ARMED (max-age: session %dm old >= cap %dm)"
            % (age / 60, max_age / 60))
        return
    # BUDGET path: threshold alone arms it, at any age past the anti-thrash floor.
    if activity < cfg["steward_floor_bytes"]:
        return                            # idle session -> skip SILENTLY (no log)
    if activity < cfg["steward_threshold_bytes"]:
        return                            # some activity, but under the restart bar
    sd["phase"] = "armed"
    sd["armed_ts"] = now
    save_state(state)
    audit({"decision": "steward-arm", "reason": "budget",
           "activity_bytes": activity,
           "elapsed_sec": int(now - sd["last_reset_ts"]), "queued": False})
    log("steward: ARMED (%d bytes of transcript growth since reset)" % activity)


def _steward_armed(cfg, state, now):
    """Inject ONE idle-gated [steward] warning and set the WARN_LEAD deadline. The
    restart will not fire before the deadline, and even then only at a lull."""
    sd = state["steward"]
    _steward_enqueue_warn(state, STEWARD_WARN_TEXT, now)
    sd["phase"] = "warned"
    sd["warned_ts"] = now
    sd["deadline"] = now + cfg["steward_warn_lead_sec"]
    save_state(state)
    audit({"decision": "steward-warn", "deadline": sd["deadline"],
           "warn_lead_sec": int(cfg["steward_warn_lead_sec"]), "queued": True})
    log("steward: WARNED — restart armed for the next lull past the deadline")


def _steward_warned(cfg, state, now, cur):
    """Apply any keeper extension first; then, once past the deadline AND the pane
    is a confirmed lull with no hold pending, RESTART. A busy pane at the deadline
    DEFERS (that is the whole point — never orphan in-flight work)."""
    sd = state["steward"]

    # 1. Extension: a hold file present is always CONSUMED this cycle (deliberate
    #    each time). A valid FUTURE request pushes the deadline, bounded to
    #    <= MAX_EXTEND from now, and re-warns; anything else is rejected+cleared —
    #    so garbage can never defer a restart forever (the bound is the anti-dodge).
    raw = _steward_read_hold()
    if raw is not None:
        _steward_clear_hold()
        target = _steward_parse_hold(raw, now)
        if target is not None:
            bounded = min(target, now + cfg["steward_max_extend_sec"])
            sd["deadline"] = bounded
            sd["warned_ts"] = now
            _steward_enqueue_warn(state, STEWARD_EXTEND_TEXT, now)
            save_state(state)
            audit({"decision": "steward-extend", "deadline": bounded,
                   "requested": target, "bounded_to_max": bounded < target,
                   "max_extend_sec": int(cfg["steward_max_extend_sec"]),
                   "queued": True})
            log("steward: EXTENDED to deadline=%d (capped=%s)"
                % (int(bounded), bounded < target))
        else:
            audit({"decision": "steward-hold-rejected", "queued": False,
                   "reason": "not-a-future-time"})
            log("steward: hold rejected (not a future time) — cleared")
        return                            # applied or rejected -> re-evaluate next tick

    # 2. Fire-at-lull. First: the keeper must actually SEE the warning before any
    #    restart — the whole point of a warning. If our [steward] line is still
    #    queued (the pane was busy through the warn window, or intake errored and
    #    drain never ran), do NOT fire; wait for it to reach the keeper. This also
    #    means a Discord/intake outage can never produce an UNWARNED restart.
    if any(q.get("author") == "steward" for q in state.get("queue", [])):
        return
    # Not past the deadline yet -> wait out the warning lead.
    if now < (sd.get("deadline") or 0):
        return
    # Past the deadline: FIRE. No lull gate (operator, 2026-07-24: "take away the
    # lull — it just needs to fire").
    #
    # WHY THE LULL GATE HAD TO GO: it deadlocked exactly when it was needed most.
    # A keeper working with the operator is never idle, so idle_reason() returned
    # busy(turn-in-flight) on every cycle and the armed restart deferred FOREVER —
    # observed live at ~654 KB of growth against an 80 KB budget, 8x over, still
    # waiting. The gate was meant to protect in-flight work, but the warning +
    # deadline + extension mechanism already does that: the keeper is told, gets
    # WARN_LEAD to finish, and can push the deadline via a hold file. A lull was a
    # fourth guard that could never be satisfied under the one condition that
    # matters — an actively-working keeper burning budget.
    _steward_restart(cfg, state, now, cur)


def _steward_state_snapshot(cfg, state, now, cur):
    """Build the steward-state.v1 status dict from the cycle's ALREADY-KNOWN state:
    a pure read of the effective cfg + state["steward"] + (best-effort) ~/handoff/hold.
    Computes no new behaviour and has NO side effects on the steward machine. `cur` is
    the live transcript INVENTORY this cycle ({name -> size}), or None if the dir
    couldn't be read. The steward-state.v1 schema and its field names are UNCHANGED;
    growth_bytes now carries the per-file inventory growth (t79/t81)."""
    sd = state.get("steward") or {}
    # EFFECTIVE mode (file > env > default is already resolved into cfg by the caller):
    # off when the feature is disabled, else dry/live per steward_restart.
    if not cfg.get("steward"):
        mode = "off"
    elif cfg.get("steward_restart") == "live":
        mode = "on"
    else:
        mode = "dry"
    floor = int(cfg.get("steward_floor_bytes") or 0)
    threshold = int(cfg.get("steward_threshold_bytes") or 0)
    cadence = int(cfg.get("steward_cadence_sec") or 0)
    last_reset = sd.get("last_reset_ts")
    phase = sd.get("phase")
    deadline = sd.get("deadline")
    # growth: inventory bytes accrued since the last reset — the SAME per-file metric
    # _steward_idle arms on (_steward_growth_since_reset), so status and behaviour never
    # disagree. Best-effort: an unmeasurable transcript (cur is None) -> 0.
    if isinstance(cur, dict):
        growth = _steward_growth_since_reset(sd, cur)
    else:
        growth = 0
    age = (now - last_reset) if last_reset is not None else 0
    max_age = cfg.get("steward_max_age_sec") or 0
    past_anti_thrash = (last_reset is not None
                        and age >= _anti_thrash_sec(cfg))
    # armed = a restart is pending. BUDGET and CLOCK are PEERS — either arms alone
    # (exactly the _steward_idle arm test), subject only to the anti-thrash floor.
    # Or the machine has already advanced to armed/warned (dry mode holds growth
    # above threshold until the restart fires).
    armed = bool(phase in ("armed", "warned")
                 or (past_anti_thrash
                     and (growth >= threshold
                          or (max_age and age >= max_age))))
    # A pending_fire_ts IS an active warning: the restart is scheduled and cannot be
    # deferred. Report it as such, or the console shows "not armed" during the very
    # window the keeper is being told to wrap up in.
    pending_fire = sd.get("pending_fire_ts")
    warning_active = (phase == "warned") or (pending_fire is not None)
    if pending_fire is not None:
        armed = True
    # next_eligible_ts: the earliest a reset can arm. The cadence no longer gates
    # arming (budget and clock are peers), so report the ANTI-THRASH floor — the
    # only real bar left. Reporting cadence here would tell the operator a reset
    # was 20m away when the budget path can arm it 2m after the last reset.
    next_eligible = (int(last_reset + _anti_thrash_sec(cfg))
                     if last_reset is not None else None)
    # warn_fire_ts: once warned, sd["deadline"] is the earliest the armed restart fires.
    if pending_fire is not None:
        warn_fire = int(pending_fire)     # the unconditional deadline wins
    else:
        warn_fire = int(deadline) if (warning_active and deadline is not None) else None
    # hold_until_ts: an un-consumed extension request sitting in ~/handoff/hold, parsed
    # to its target epoch (best-effort read; absent/garbage/non-future -> null). Once
    # a warned cycle consumes the hold it is reflected in sd["deadline"]/warn_fire_ts.
    hold_until = None
    try:
        raw = _steward_read_hold()
        if raw is not None:
            t = _steward_parse_hold(raw, now)
            if t is not None:
                hold_until = int(t)
    except Exception:
        hold_until = None
    # --- HONEST fields (2026-07-24) ------------------------------------------
    # Operator: "it just reset the clock, it didn't reset the vm ... i need the
    # ui to be honest."
    #
    # They were right. Every number above is measured from the steward's OWN
    # bookkeeping: session_age_sec is now-last_reset and growth_bytes is growth
    # SINCE last_reset. Both zero when the steward resets its state — including
    # when the keeper process merely restarts without shedding anything. So the
    # panel read "145s old, 113 KB" while 2,065,248 bytes of transcript sat on
    # disk: wrong by 18x, in the reassuring direction.
    #
    # These fields are measured from the FILES THEMSELVES, so they cannot be
    # zeroed by bookkeeping. A restart that archives nothing leaves them
    # unchanged — which is the whole point. They are additive; older drawers
    # ignore unknown keys.
    live_bytes = None
    live_oldest_sec = None
    context_age_sec = None
    try:
        inv = cur if cur is not None else _steward_transcript_inventory(cfg)
        if inv:
            live_bytes = int(sum(inv.values()))
        d = _steward_transcript_dir_for(STATION)
        oldest = None
        for name in (inv or {}):
            try:
                ct = os.path.getctime(os.path.join(d, name))
            except OSError:
                continue
            if oldest is None or ct < oldest:
                oldest = ct
        if oldest is not None:
            live_oldest_sec = int(max(0, now - oldest))
        # Freshness of the figure context_tokens is read FROM: the newest mtime in
        # the inventory is the live session (same selection as
        # _steward_max_context_tokens), so its age is how current that number is.
        newest = None
        for name in (inv or {}):
            try:
                mt = os.path.getmtime(os.path.join(d, name))
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
        if newest is not None:
            context_age_sec = int(max(0, now - newest))
    except Exception:
        pass                              # observability must never break a cycle

    return {
        "schema": STEWARD_STATE_SCHEMA,
        "ts": int(now),
        "mode": mode,
        "armed": armed,
        "growth_bytes": int(growth),
        # What is ACTUALLY on disk right now, independent of any reset.
        "live_transcript_bytes": live_bytes,
        # Age of the OLDEST live transcript — the real "how much history is the
        # keeper still re-reading", which a process restart does not change.
        "live_transcript_age_sec": live_oldest_sec,
        # Seconds since the LIVE session was last WRITTEN — i.e. how fresh
        # context_tokens is. Distinct from live_transcript_age_sec above, which is
        # the OLDEST file's age and stays large while a session is actively going
        # (measured 2026-07-27: oldest 44,513s vs the live session written 3s ago).
        # The console's meter needs THIS one to say "is the token figure current",
        # and conflating the two made it read "stale" permanently. None => unknown,
        # which consumers must treat as "not stale" rather than as a fault.
        "context_tokens_age_sec": context_age_sec,
        "floor_bytes": floor,
        # REPORTED ONLY since 2026-07-30. Kept so existing drawers keep rendering,
        # but it decides nothing — budget_metric below says what actually governs, so
        # a UI can never again present a byte bar as the thing that fires the reset.
        "threshold_bytes": threshold,
        # THE RESET BAR (operator 2026-07-29/30): context tokens vs this budget.
        # Pair it with context_tokens above to draw the meter that matters.
        "threshold_tokens": int(cfg.get("steward_threshold_tokens") or 0),
        "budget_metric": "tokens",
        # True when the token budget is set but context is unmeasurable, i.e. NO
        # budget trigger can fire right now. Surfaced because it is the one way the
        # reset can be silently inert (see _steward_should_restart).
        "budget_blind": bool(cfg.get("steward_threshold_tokens")
                             and _steward_max_context_tokens(cfg) is None),
        "cadence_sec": cadence,
        # t89 time-ARM gate: 0 = off. Additive fields — drawers that predate
        # them ignore unknown keys.
        "max_age_min": int((cfg.get("steward_max_age_sec") or 0) / 60),
        "session_age_sec": (int(now - last_reset)
                            if last_reset is not None else None),
        "last_reset_ts": int(last_reset) if last_reset is not None else None,
        # Identity of the session these numbers describe. A manual-reset request
        # echoes this back so the relay can reject a press minted for a session
        # that has since been replaced (see _steward_consume_reset_request).
        "session_nonce": _steward_session_nonce(state),
        # --- HONEST-NUMBER fields (2026-07-25) -------------------------------
        # growth_bytes counts transcripts from sessions that have since EXITED
        # and can never grow again; on this machine that was 70% of the figure.
        # Reported so the drawer can show what is actually live, without
        # changing the trigger math (see _steward_stale_transcript_bytes).
        "stale_transcript_bytes": _steward_stale_transcript_bytes(cfg),
        # The real context measure. Bytes/token varies ~4x across sessions, so
        # a byte budget cannot predict context; this is what the model itself
        # reported consuming. None when no usage record is readable.
        "context_tokens": _steward_max_context_tokens(cfg),
        "next_eligible_ts": next_eligible,
        "warning_active": bool(warning_active),
        "warn_fire_ts": warn_fire,
        "hold_until_ts": hold_until,
        # while warned, an extend is always honoured (bounded to the max-extend cap),
        # so availability == a warning being active.
        # An extension cannot defer an UNCONDITIONAL pending fire — that warning's
        # whole contract is that nothing vetoes it, so advertising "extend" during
        # that window would be the UI lying about what the button does (cf. 8747825).
        # The legacy warned-phase deadline is still extendable and keeps its button.
        "extend_available": bool(warning_active and pending_fire is None),
        # --- token accounting (operator ask, 2026-07-29) ---------------------
        # Full usage split (output / input / cache_write / cache_read / calls)
        # for the LIVE session and the whole transcript dir, from the model's
        # own usage records. Additive: drawers that predate it ignore it.
        "token_usage": _apply_usage_baseline(cfg, _steward_token_usage(cfg)),
    }


def _apply_usage_baseline(cfg, raw):
    """Reversible per-VM 'zero the token counters' switch. When cfg.steward_zero_usage
    is on, snapshot the current live+total the FIRST time into a baseline file, then
    report every figure as (current - baseline) so the panel reads from 0 and grows.
    Off -> the baseline file is removed and the true totals return. NON-DESTRUCTIVE:
    no transcript is moved or deleted, so it is fully reversible and re-zeroable.
    Observability only: never raises into the cycle."""
    if not isinstance(raw, dict):
        return raw
    path = os.path.expanduser(cfg.get("usage_baseline_path")
                              or "~/.keeper-usage-baseline.json")
    zero = bool(cfg.get("steward_zero_usage"))
    keys = ("output", "input", "cache_write", "cache_read", "calls")
    try:
        base = None
        if os.path.exists(path):
            with open(path) as fh:
                base = json.load(fh)
        if zero and base is None:
            base = {"ts": int(time.time()),
                    "total": dict(raw.get("total") or {}),
                    "live": dict(raw.get("live") or {})}
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(base, fh)
            os.replace(tmp, path)
        elif not zero and base is not None:
            try:
                os.remove(path)
            except OSError:
                pass
            base = None
        if base:
            for scope in ("total", "live"):
                cur = raw.get(scope)
                b = base.get(scope) or {}
                if isinstance(cur, dict):
                    raw[scope] = {k: max(0, int(cur.get(k, 0)) - int(b.get(k, 0)))
                                  for k in keys}
            raw["zeroed_since"] = base.get("ts")
    except Exception:
        pass
    return raw


def _steward_token_usage(cfg):
    """Full token accounting across every *.jsonl in the transcript dir, summed
    from the transcripts' own usage records (the model's numbers, never an
    estimate). INCREMENTAL: a module-level cache remembers how far each file was
    parsed and only newly-written bytes are read each cycle — the per-cycle cost
    is the tail just written, not the whole history. A shrunk/replaced file
    re-parses from zero. Returns the additive steward-state block, or None if
    the dir is unreadable. Observability only: never raises into the cycle."""
    d = cfg.get("steward_transcript_dir") or STEWARD_TRANSCRIPT_DIR
    cache = getattr(_steward_token_usage, "_cache", None)
    if cache is None:
        cache = _steward_token_usage._cache = {}
    try:
        names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
    except OSError:
        return None
    total = [0, 0, 0, 0, 0]            # out, in, cache_write, cache_read, calls
    live, newest_mt = None, -1.0
    for name in names:
        p = os.path.join(d, name)
        try:
            st = os.stat(p)
        except OSError:
            cache.pop(p, None)
            continue
        ent = cache.get(p)
        if ent is None or st.st_size < ent[0]:
            ent = [0, 0, 0, 0, 0, 0]   # parsed-offset, out, in, cw, cr, calls
        if st.st_size > ent[0]:
            try:
                with open(p, "rb") as fh:
                    fh.seek(ent[0])
                    chunk = fh.read()
            except OSError:
                continue
            end = chunk.rfind(b"\n")
            if end >= 0:
                for line in chunk[:end].split(b"\n"):
                    try:
                        u = json.loads(line).get("message", {}).get("usage")
                        if not isinstance(u, dict):
                            continue
                        _in = int(u.get("input_tokens") or 0)
                        _cc = int(u.get("cache_creation_input_tokens") or 0)
                        _cr = int(u.get("cache_read_input_tokens") or 0)
                        if _in == 0 and _cc == 0 and _cr == 0:
                            continue      # synthetic/zero entry — not a real API turn
                        ent[1] += int(u.get("output_tokens") or 0)
                        ent[2] += _in
                        ent[3] += _cc
                        ent[4] += _cr
                        ent[5] += 1
                    except Exception:
                        continue
                ent[0] += end + 1
            cache[p] = ent
        for i in range(5):
            total[i] += ent[i + 1]
        if ent[5] > 0 and st.st_mtime > newest_mt:   # newest transcript with a REAL turn
            newest_mt, live = st.st_mtime, ent
    keys = ("output", "input", "cache_write", "cache_read", "calls")
    return {
        "live": dict(zip(keys, live[1:])) if live else None,
        "total": dict(zip(keys, total)),
        "sessions": len(names),
    }


def _steward_write_state(cfg, state, now, cur):
    """READ-ONLY OBSERVABILITY: atomically write the steward-state.v1 snapshot to
    STEWARD_STATE_FILE so a host route/console can serve the steward's status. This
    only SERIALIZES state the cycle already has — it changes NOTHING about arming,
    warning, restart, extend, or the safety posture. Fully self-guarding: any failure
    (perms, disk, a bad field) is logged at most once per failure streak and swallowed
    — a state write can NEVER raise into, or alter, the steward cycle."""
    path = cfg.get("steward_state_path") or STEWARD_STATE_FILE
    tmp = path + ".tmp"
    try:
        data = json.dumps(_steward_state_snapshot(cfg, state, now, cur)).encode("utf-8")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)             # atomic: a reader never sees a partial file
        _steward_write_state._failed = False
    except Exception as exc:              # observability must never break the steward
        if not getattr(_steward_write_state, "_failed", False):
            log("steward: state write failed (%s) — continuing (observability only)"
                % type(exc).__name__)
            _steward_write_state._failed = True
        try:
            os.remove(tmp)
        except OSError:
            pass


def _steward_session_nonce(state):
    """Identity of the CURRENT keeper session, as published in the steward-state
    snapshot and echoed back by a reset request. `last_reset_ts` is exactly the
    right value: it changes on every reset (so a request cannot outlive the
    session it was minted for) and is stable within one (so repeated presses
    during a session all match). Returns None before the steward is seeded, which
    the caller treats as "cannot verify" rather than "reject"."""
    sd = (state or {}).get("steward")
    if not isinstance(sd, dict):
        return None
    ts = sd.get("last_reset_ts")
    return int(ts) if isinstance(ts, (int, float)) else None


def _steward_request_reset(path=None):
    """Mint a manual-reset request INSIDE the vm, stamped with the live session
    nonce. This is the VM-owned press (operator, 2026-07-25: "this should be an
    internal mechanism per vm") — it needs no host, no lxc, and no console.

    Because the nonce comes from the relay's own on-disk state, a request minted
    here is valid ONLY for the session that is running right now. If the keeper
    resets before the relay consumes it, the nonce no longer matches and the
    request is rejected instead of killing the successor — the exact failure this
    whole change exists to prevent. Returns a process exit code."""
    path = path or STEWARD_REQUEST_FILE
    nonce = _steward_session_nonce(load_state() or {})
    body = {"schema": "steward-request.v1", "op": "reset"}
    if nonce is not None:
        body["session_nonce"] = nonce
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(body, fh)
            fh.flush()
            os.fsync(fh.fileno())         # the relay may read the instant we link
        os.replace(tmp, path)             # atomic: never a partial request
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        sys.stderr.write("could not write %s: %s\n" % (path, exc))
        return 1
    if nonce is None:
        # No seeded state to read: the request is honoured (compat) but cannot be
        # verified, so say so rather than implying a guarantee we did not give.
        print("reset requested (UNVERIFIED — no seeded steward state to stamp)")
    else:
        print("reset requested for session %d — the relay will fire it on its "
              "next cycle, and will ignore it if the keeper resets first" % nonce)
    return 0


def _steward_consume_reset_request(cfg, state):
    """At-most-once manual-reset intake: read then unlink STEWARD_REQUEST_FILE
    (env-overridable via cfg for tests). True only for a well-formed request
    ({"schema":"steward-request.v1","op":"reset"}); malformed bodies are consumed,
    audited, and ignored — a bad file must not re-trigger every cycle. Never
    raises.

    STALENESS GATE (2026-07-25). A request used to carry nothing but a schema and
    an op, so the consumer could not tell a press from one second ago from one
    made hours earlier against a session that no longer exists. It always fired
    at whatever session happened to be running when the relay next looked.

    That is not hypothetical: the relay was down while the operator pressed reset,
    the request sat unconsumed on disk, and the moment the relay came back it
    killed an unrelated session (00:31:56 — "MANUAL reset", pane pid 3377). The
    reset the operator asked for landed on an innocent successor.

    The gate is a nonce match, not a clock comparison. A TTL would be the wrong
    tool: the legacy writer is on the HOST (`lxc file push`) while the consumer
    runs inside the VM, so a wall-clock age compares two independent clocks. The
    nonce sidesteps timebases entirely — `session_nonce` IS the current session's
    last_reset_ts, so a request minted for an earlier session simply does not
    match the live one and is rejected. That covers staleness AND targeting in a
    single equality check.

    VM-OWNED (operator, 2026-07-25: "why does this rely on the host? this should
    be an internal mechanism per vm"). The nonce is minted and verified entirely
    inside the VM — nothing outside has to cooperate for the gate to hold, and
    the correct way to press reset locally is `keeper_relay.py --reset`, which
    stamps the live nonce. Resetting a keeper is the keeper's own business; the
    host belongs in create/destroy/hardware, not in this path.

    COMPAT: a request with NO nonce is accepted, and audited as unverified. The
    host route and hand-written `echo '{"schema":...}' > steward-request.json`
    presses must keep working; the field is additive, exactly like the state
    doc's own additive fields. Tightening this to reject un-nonced requests is a
    one-line change here once every writer is known to stamp them."""
    path = cfg.get("steward_request_path") or STEWARD_REQUEST_FILE
    try:
        with open(path, "rb") as fh:
            raw = fh.read(4096)
    except OSError:
        return False                      # absent (the normal case) or unreadable
    try:
        os.unlink(path)                   # consume before acting => at-most-once
    except OSError:
        pass
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        doc = None
    if (isinstance(doc, dict) and doc.get("schema") == "steward-request.v1"
            and doc.get("op") == "reset"):
        want = _steward_session_nonce(state)
        got = doc.get("session_nonce")
        if got is None:
            # Un-nonced writer (host route / hand-written press): honour it. NOTE
            # the log, not an audit — every caller of this function emits its own
            # steward-manual-reset audit record, so auditing the acceptance here
            # too would double-count one press (caught by the suite: "2 != 1").
            # Rejections below DO audit, because those callers never run.
            log("steward: manual reset accepted UNVERIFIED (no session_nonce) — "
                "a request minted for an older session cannot be told apart")
            return True
        if want is not None and got != want:
            audit({"decision": "steward-manual-reset", "queued": False,
                   "reason": "stale-request-rejected",
                   "request_nonce": got, "session_nonce": want})
            log("steward: STALE manual reset ignored (minted for session %r, "
                "current session is %r) — press reset again" % (got, want))
            return False
        return True
    audit({"decision": "steward-manual-reset", "queued": False,
           "reason": "malformed-request-ignored"})
    log("steward: malformed %s ignored (consumed)" % path)
    return False


def _steward_lean_context_tick(cfg, state):
    """Method E — the 'cache on/off' (lean-context) switch. An independent
    TOKEN-BAR context refresh that runs even when the reset feature (mode) is OFF:
    it caps the keeper's cache_read by refreshing context once it crosses the token
    bar. Token-TRIGGERED only — never a timed/cadence trigger (the operator
    disabled the timed ticker) — so max-age is forced off. Everything else (seed,
    measure, the handoff WARNING + grace, anti-thrash floor, the actual refresh) is
    the SAME tested machinery the reset uses; this only changes what may pull it.

    Called before the OFF gate; fully handles the tick and the caller returns after
    it, so the OFF-gate state write never runs on top of it. No-op unless
    lean_context is set AND the reset feature is off (mode-on already does token-bar
    refreshes, so lean would be redundant there)."""
    try:
        now = time.time()
        cur = _steward_transcript_inventory(cfg)
        if cur is None:
            _steward_write_state(cfg, state, now, None)
            return
        if state.get("steward") is None:
            _steward_seed(cfg, state, now, cur)
            _steward_write_state(cfg, state, now, cur)
            return
        # Token-ONLY view of the cfg: enable the machinery, force max-age off so
        # nothing timed can fire, and give a handoff-write grace (default 3m) so a
        # refresh never yanks context out mid-thought without a next.md.
        lcfg = dict(cfg)
        lcfg["steward"] = True
        # Respect the fleet dry/live flag: Method E switches are OBSERVE-first
        # ("flip it to watch behaviour + cost"), so lean DEFAULTS to dry — it arms,
        # warns, and LOGS the refresh it would do (visible in WARNING.md, the logs,
        # and the meter), executing only when steward_restart is set live. That
        # matches the operator's no-surprise-restart stance (the timed ticker is off).
        lcfg["steward_restart"] = cfg.get("steward_restart") or "dry"
        lcfg["steward_max_age_sec"] = 0
        lcfg["steward_warn_lead_sec"] = float(cfg.get("steward_warn_lead_sec") or 180)
        lcfg["_lean_context"] = True
        # a manual reset (console button) still fires immediately in lean mode
        if _steward_consume_reset_request(lcfg, state):
            audit({"decision": "steward-manual-reset", "queued": False,
                   "reason": "lean-context"})
            log("steward(lean): MANUAL reset — refreshing now")
            _steward_restart(lcfg, state, now, cur)
            _steward_write_state(lcfg, state, now, cur)
            return
        if _steward_should_restart(lcfg, state, now, cur):
            _steward_restart(lcfg, state, now, cur)
        _steward_write_state(lcfg, state, now, cur)
    except Exception as exc:              # a lean tick must not stop the relay
        log("steward lean-context tick error (%s) — skipping this tick"
            % type(exc).__name__)


def steward_cycle(cfg, state):
    """Advance the steward state machine one tick. Fully INERT unless the operator
    has opted the whole feature in (KEEPER_RELAY_STEWARD=on, or mode!=off in the
    console-writable ~/steward.json). Wrapped so a fault here never blocks the
    Discord/board/lull drain."""
    # Layer the console-writable ~/steward.json on top of the env/default cfg LIVE,
    # BEFORE the OFF gate — the console's mode field can enable (or disable) the
    # feature. Fail-safe: any trouble -> the env/default cfg unchanged.
    cfg = _steward_effective_cfg(cfg)
    # Method E lean-context: when the reset feature is OFF but lean_context is ON,
    # a token-bar-only refresh path handles the whole tick and returns.
    if (not cfg.get("steward")) and cfg.get("steward_lean_context"):
        _steward_lean_context_tick(cfg, state)
        return
    if not cfg.get("steward"):
        # OFF -> the steward acts on nothing; still emit a best-effort state file
        # (mode "off" + current inventory growth + ts) so the console can always
        # render status. Read-only: it never seeds/mutates state or the queue.
        # A manual-reset request is consumed-and-ignored here (audited) so it
        # can't lie in wait and fire the moment the feature is enabled.
        if _steward_consume_reset_request(cfg, state):
            audit({"decision": "steward-manual-reset", "queued": False,
                   "reason": "steward-off-ignored"})
            log("steward: manual reset ignored — steward is off")
        _steward_write_state(cfg, state, time.time(),
                             _steward_transcript_inventory(cfg))
        return                            # OFF by default -> this ships changing nothing
    try:
        now = time.time()
        cur = _steward_transcript_inventory(cfg)   # {name -> size}; None if dir unreadable
        if cur is None:
            # Can't measure the transcript -> never act on an unknown. Still emit a
            # best-effort state file (growth falls back to 0) so status stays fresh.
            _steward_write_state(cfg, state, now, None)
            return
        if state.get("steward") is None:
            _steward_seed(cfg, state, now, cur)
            _steward_write_state(cfg, state, now, cur)
            return                        # the seed tick acts on nothing
        # MANUAL RESET: fire NOW, this tick. No phase, no deadline, no warning.
        #
        # The button was never broken — six requests were consumed correctly
        # (18:03, 18:36 x2, 19:33, 20:13, 23:54) and every one parked "firing at
        # the next lull", a lull that an operator actively pressing the button is
        # by definition preventing. It armed a reset that then waited forever.
        if _steward_consume_reset_request(cfg, state):
            audit({"decision": "steward-manual-reset", "queued": False})
            log("steward: MANUAL reset — restarting now")
            _steward_restart(cfg, state, now, cur)
            _steward_write_state(cfg, state, now, cur)
            return
        # AUTOMATIC: over budget or over time -> restart. That is the whole machine
        # (operator, 2026-07-24: "the warnings, the lull, everything hasn't panned
        # out ... it needs to all be scrapped, and simply work").
        #
        # The arm -> warn -> wait-for-lull ceremony is GONE. Every stage of it was a
        # place the reset could stall, and in practice it always did: a keeper
        # working with the operator is never idle, so the machine armed, warned, and
        # then waited forever while the transcript ran 8x past its budget.
        if _steward_should_restart(cfg, state, now, cur):
            _steward_restart(cfg, state, now, cur)
        # Serialize the (post-advance) steward status for the console. Best-effort and
        # internally wrapped -> a state-write failure can never affect the tick above.
        _steward_write_state(cfg, state, now, cur)
    except Exception as exc:              # a steward fault must not stop the relay
        log("steward cycle error (%s) — skipping this tick" % type(exc).__name__)
        audit({"decision": "error", "reason": "steward-exception:%s"
               % type(exc).__name__, "queued": False})


# ---------------------------------------------------------------- operator pinger
#
# Two stages, mirroring the lull relay's separation but with NO LLM step:
#   1. DETECTION accrues candidate events, each with a stable id, into
#      state["opping"]["pending"]. Board events (operator item / proposal / keeper
#      reply) are detected inside todo_watch off the board `items` it ALREADY read
#      (no extra todo.json read); the deploy-failed event is detected in the
#      delivery hook off the signal's mtime (a stat, independent of the board and
#      of the lull feature being on). First run seeds the whole opping state
#      silently — known board ids, per-item comment counts, the deploy fingerprint
#      — so enabling this never storms the operator on deploy.
#   2. DELIVERY (opping_cycle, in the poll loop) batches every pending event into
#      ONE sanitized message, POSTs it to <endpoint>/send, and — on success (or in
#      dry mode) — retires the events and starts the cooldown. A send failure keeps
#      the events pending for the next cycle. Every send audits, never crashes.


def _opping_cap(ids, cap):
    """Keep the most-recent `cap` ids; older ones fall off the front."""
    return ids[-cap:] if len(ids) > cap else ids


def _opping_seen_add(seen, eid):
    """Mark an event delivered (idempotent), capping the ring at OPPING_SEEN_CAP."""
    if eid in seen:
        return
    seen.append(eid)
    if len(seen) > OPPING_SEEN_CAP:
        del seen[:len(seen) - OPPING_SEEN_CAP]


def _opping_id(iid):
    """A sanitized item id safe to splice into a line (the board is anyone-writable)."""
    return sanitize(str(iid))[0] or "?"


def _opping_snippet(text, n):
    """Sanitize file-sourced text and cap at n chars with a visible ellipsis. The
    board/comment text is attacker-writable, so every quoted char is sanitized."""
    s = sanitize("" if text is None else str(text))[0]
    return s if len(s) <= n else s[:n] + "…"


def _opping_add(state, eid, line, now):
    """Accrue one operator-ping event unless it is already pending or delivered.
    The whole line is sanitized (our own text, but the discipline stays uniform —
    no control/ANSI/bidi ever reaches the outbound message)."""
    op = state["opping"]
    if eid in op["pending"] or eid in op["seen"]:
        return
    safe = sanitize(line)[0]
    if not safe:
        return
    op["pending"][eid] = {"line": safe, "first_seen": now}


def _opping_seed(cfg, state, items):
    """First run: adopt the CURRENT world as already-known and ping about none —
    the same silent seeding the other watchers do. Records every present board id,
    each item's comment count, and the deploy-failed fingerprint, so the first real
    change after this enables is the first thing that can ping."""
    known, counts = [], {}
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = _todo_id(it)
        if not iid:
            continue
        known.append(iid)
        comments = it.get("comments")
        counts[iid] = len(comments) if isinstance(comments, list) else 0
    state["opping"] = {
        "seen": [],
        "pending": {},
        "known_ids": _opping_cap(known, OPPING_KNOWN_CAP),
        "comment_counts": counts,
        "deploy_mtime": _lull_deploy_mtime(
            cfg.get("deploy_failed_path") or DEPLOY_FAILED_FILE),
        "last_send_ts": 0.0,
    }
    save_state(state)
    log("operator pinger: first run — seeded %d known ids, deploy fp, no pings"
        % len(known))


def _opping_detect_board(cfg, state, items):
    """Accrue operator-ping candidates from the board `items` todo_watch already
    read (NO extra read). Runs regardless of KEEPER_RELAY_TODO_NOTIFY — the board
    ping is for the KEEPER, this is for the OPERATOR. Detects three events:
      1. a NEWLY-APPEARED open ⚑ operator item (id not previously on the board, so
         a done->open flip of an existing item is NOT a ping — no status-flip spam);
      2. a NEWLY-APPEARED pending ⚖ proposal (type=proposal, status != done);
      3. a NEW comment by "keeper" on an item that ALSO carries a non-keeper comment
         or a query — the keeper REPLYING to the operator (its own soliloquy on a
         keeper-only thread never pings).
    """
    if cfg.get("operator_ping", "on") == "off":
        return
    if state.get("opping") is None:
        _opping_seed(cfg, state, items)
        return
    op = state["opping"]
    now = time.time()
    known = set(op["known_ids"])
    counts = op["comment_counts"]
    newly = []
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = _todo_id(it)
        if not iid:
            continue
        typ = it.get("type")
        status = it.get("status")
        comments = it.get("comments")
        comments = comments if isinstance(comments, list) else []

        # 3. keeper reply — a NEW keeper comment on a thread that also has a
        #    non-keeper voice (a foreign comment or a query field). Track per-item
        #    counts (seeded silently) so only genuinely new comments are examined.
        prev = counts.get(iid, 0)
        cur_n = len(comments)
        if cur_n < prev:
            counts[iid] = cur_n            # list shrank (a rewrite): resync, no fire
        elif cur_n > prev:
            has_foreign = bool(it.get("query")) or any(
                isinstance(c, dict) and c.get("by") != "keeper" for c in comments)
            if has_foreign:
                for n in range(prev, cur_n):
                    c = comments[n] if 0 <= n < len(comments) else None
                    if not isinstance(c, dict) or c.get("by") != "keeper":
                        continue           # only the keeper's own replies ping here
                    _opping_add(state, "kreply:%s:%d" % (iid, n),
                                "💬 keeper replied on %s: %s"
                                % (_opping_id(iid),
                                   _opping_snippet(c.get("text"), OPPING_TEXT_MAX)),
                                now)
            counts[iid] = cur_n

        # 1 & 2: NEWLY-APPEARED items only — an id we have never tracked. A status
        #    flip of an already-known item is deliberately NOT a ping.
        if iid not in known:
            newly.append(iid)
            if typ == "operator" and status == "open":
                _opping_add(state, "op-open:%s" % iid,
                            "⚑ operator task %s: %s"
                            % (_opping_id(iid),
                               _opping_snippet(it.get("text"), OPPING_TEXT_MAX)),
                            now)
            elif typ == "proposal" and status != "done":
                _opping_add(state, "prop:%s" % iid,
                            "⚖ proposal awaits your decision — %s: %s"
                            % (_opping_id(iid),
                               _opping_snippet(it.get("text"), OPPING_TEXT_MAX)),
                            now)
    if newly:
        op["known_ids"] = _opping_cap(op["known_ids"] + newly, OPPING_KNOWN_CAP)
    save_state(state)


def _opping_detect_deploy(cfg, state):
    """A console deploy-ui.failed signal, fingerprinted by mtime so each distinct
    failure pings exactly once. Reuses the lull/journal mtime+reason technique but
    keeps its OWN fingerprint, so the operator ping is independent of the lull
    feature being on."""
    op = state["opping"]
    path = cfg.get("deploy_failed_path") or DEPLOY_FAILED_FILE
    if not _deploy_signals_on(path):
        return                              # no deploy pipeline on this VM (feature-off)
    mt = _lull_deploy_mtime(path)
    if mt is None or op.get("deploy_mtime") == mt:
        return
    op["deploy_mtime"] = mt
    _opping_add(state, "deploy-failed:%d" % mt,
                "⚠ console deploy failed: %s"
                % _opping_snippet(_deploy_failed_reason(path), OPPING_REASON_MAX),
                time.time())


def _opping_compose(events):
    """Batch pending events into ONE message. `events` is [(eid, line)] in delivery
    order; returns (message, sent_ids). Newline-joined, capped at OPPING_MSG_MAX
    with a "+N more" tail — the overflow events are NOT in sent_ids, so they stay
    pending and go out next cooldown (queued across, never dropped)."""
    lines, sent = [], []
    for i, (eid, line) in enumerate(events):
        if lines and len("\n".join(lines + [line])) > OPPING_MSG_MAX:
            lines.append("+%d more pending — check the board" % (len(events) - i))
            break
        lines.append(line)
        sent.append(eid)
    return "\n".join(lines), sent


def _opping_send(cfg, content):
    """POST one message to the operator's Discord via the bridge's <endpoint>/send.
    ONE attempt + ONE retry (the same urllib idiom as the lull HTTP call). Returns
    (ok, http) where http is the status int on success or an error token on failure.
    The session token is in the URL — no auth header. Never raises; errors are data.
    """
    # Offline mode: no endpoint -> nothing to POST to. Return the same (ok, http)
    # shape the caller already handles for a failed send, so the ping stays
    # pending exactly as it would across a network outage. Guarding here rather
    # than at the call site keeps every caller's contract unchanged.
    if not cfg.get("endpoint"):
        log("operator-ping skipped — offline (no endpoint configured)")
        return False, "offline-no-endpoint"
    url = (cfg.get("endpoint") or "").rstrip("/") + OPPING_SEND_SUFFIX
    body = json.dumps({"content": content}).encode("utf-8")
    last = "no-attempt"
    for _attempt in range(2):              # one try + one retry
        try:
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=OPPING_HTTP_TIMEOUT) as resp:
                code = getattr(resp, "status", None) or resp.getcode() or 200
                ack = (resp.read() or b"")[:200].decode("utf-8", "replace")
                log("operator-ping POST ok http=%d ack=%s" % (int(code), ack))
                return True, int(code)
        except urllib.error.HTTPError as exc:
            last = "http-%s" % exc.code     # a 4xx/5xx is a real failure -> keep, retry
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            last = type(exc).__name__
    return False, last


def opping_cycle(cfg, state):
    """Deliver operator pings: detect the deploy-failed signal, then — at most once
    per cooldown — batch every pending event into ONE message and POST it. Wrapped
    so a fault here never blocks the Discord/board/lull drain. Independent of the
    inbound Discord fetch, so a fetch outage never withholds an outbound ping."""
    mode = cfg.get("operator_ping", "on")
    if mode == "off":
        return                              # fully inert
    op = state.get("opping")
    if op is None:
        return                              # not seeded yet (board not read cleanly once)
    try:
        _opping_detect_deploy(cfg, state)
        now = time.time()
        if not op["pending"]:
            save_state(state)               # persist any deploy fp / detection advance
            return
        if (now - op.get("last_send_ts", 0.0)) < cfg["operator_ping_cooldown_sec"]:
            save_state(state)
            return                          # cooldown: queue across it, don't drop
        ordered = sorted(op["pending"].items(),
                         key=lambda kv: (kv[1]["first_seen"], kv[0]))
        message, sent_ids = _opping_compose(
            [(eid, meta["line"]) for eid, meta in ordered])
        if not sent_ids:
            return
        if mode == "dry":
            sent, http = False, "dry"       # detect + audit + log, no POST
        else:
            ok, http = _opping_send(cfg, message)
            sent = bool(ok)
        if mode == "dry" or sent:
            # Delivered (or dry-run): retire the batch, mark seen, start the cooldown.
            for eid in sent_ids:
                op["pending"].pop(eid, None)
                _opping_seen_add(op["seen"], eid)
            op["last_send_ts"] = now
        # A real send failure keeps the events pending and does NOT advance the
        # cooldown, so the next cycle retries them (with any newcomers batched in).
        save_state(state)
        audit({"decision": "operator-ping", "events": sent_ids, "sent": sent,
               "http": http, "mode": mode, "count": len(sent_ids)})
        log("operator-ping %s: %d event(s) http=%s"
            % ("sent" if sent else ("dry" if mode == "dry" else "FAILED"),
               len(sent_ids), http))
    except Exception as exc:                # an opping fault must not stop the relay
        log("operator-ping cycle error (%s) — skipping this tick"
            % type(exc).__name__)
        audit({"decision": "error", "reason": "opping-exception:%s"
               % type(exc).__name__, "queued": False})


# ---------------------------------------------------------------- board events
#
# PROMPT relay of board TRANSITIONS: proposal accepts/declines and done-flips.
# Unlike the [board] NEW-item watcher above (quiet-gated), these deliver on the
# instantaneous idle gate ALONE (author=="board-event" — exempt from the quiet
# gate and the max-hold cap in drain()), because a decision is exactly what the
# keeper is waiting on. Detection reuses the journal's todo_fp fingerprint map as
# the pre-change snapshot (this runs BEFORE journal_cycle advances it), so there
# is no second differ. The keeper's own CLI transitions are suppressed via the
# todo-cli coordination marker; console-originated transitions fire.


def _events_cap(ids):
    """Keep the most-recent EVENTS_SEEN_CAP ids; older ones fall off the front."""
    return ids[-EVENTS_SEEN_CAP:] if len(ids) > EVENTS_SEEN_CAP else ids


def _events_seen_add(seen, eid):
    """Mark an event id fired/seeded (idempotent), capping the ring."""
    if eid in seen:
        return
    seen.append(eid)
    if len(seen) > EVENTS_SEEN_CAP:
        del seen[:len(seen) - EVENTS_SEEN_CAP]


def _event_decision(it):
    """The decision token ('accepted'/'declined') for a proposal whose note carries
    a PROP_MARK prefix, else None. Only type=='proposal' items decide."""
    if not isinstance(it, dict) or it.get("type") != "proposal":
        return None
    m = PROP_MARK.match(str(it.get("note") or ""))
    return m.group(1).lower() if m else None


def _events_seed(state, items):
    """First run (or feature just enabled): adopt every decision ALREADY present as
    seen and fire NOTHING — the same silent seeding the other watchers do, so a
    deploy never storms the keeper with historical accepts/declines. Done-states
    need no seeding: a done-transition requires a prior open/doing fingerprint,
    which the first cycle does not have, so a pre-existing done never fires."""
    seen = []
    for it in items:
        iid = _todo_id(it)
        if not iid:
            continue
        d = _event_decision(it)
        if d:
            seen.append("evt:%s:%s" % (iid, d))
        elif _is_ping(it):
            seen.append("evt:%s:ping" % iid)   # a ping already present at first run
    state["events"] = {"seen": _events_cap(seen)}
    save_state(state)
    log("board events: first run — seeded %d prior decision(s), no events" % len(seen))


def _read_cli_markers(path):
    """The todo-cli coordination marker as a list of {ts, ids, op} records. Absent /
    half-written / wrong-shape -> [] (data, not a crash). The keeper's CLI appends
    one record per transition it makes; the relay reads them to suppress its own
    echo."""
    data = _read_json_obj(path)
    if not data:
        return []
    writes = data.get("writes")
    return writes if isinstance(writes, list) else []


def _cli_suppressed(markers, iid, now):
    """True when a FRESH keeper-CLI marker attributes the just-observed change on
    `iid` to the keeper itself. Fresh = ts predates now (same host/clock, so the
    CLI write precedes this read) and is within EVENT_SUPPRESS_WINDOW. A stale
    marker (older than the window) or NO marker -> not suppressed, so it fires."""
    for m in markers:
        if not isinstance(m, dict):
            continue
        ts = m.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        if ts > now or (now - ts) > EVENT_SUPPRESS_WINDOW:
            continue                          # future/garbage or stale -> fires normally
        ids = m.get("ids")
        if isinstance(ids, list) and iid in ids:
            return True
    return False


def render_board_events(events):
    """One-line [board] event notice — our own text only, every field sanitize()d.

    Format (mirrors render()'s "our characters only" discipline):
        [board] ✔ p8 ACCEPTED "…" · ✕ p9 DECLINED "…" · ✓ t44 done · ⚑ o11 done — check ~/todo.json
    `events` is [{"id","kind","decision"?,"text"?}] in detection order. Decisions
    quote a text snippet; done-transitions do not. Overflow past ~EVENTS_NOTIFY_MAX
    is summarised with a trailing "(+N more)" rather than typed in full.
    """
    # All-pings notice reads as a ping; a mixed/transition notice stays [board].
    prefix = "[ping] " if events and all(e.get("kind") == "ping" for e in events) else "[board] "
    suffix = " — check ~/todo.json"
    budget = EVENTS_NOTIFY_MAX - len(prefix) - len(suffix)
    parts = []
    remaining = 0
    for idx, ev in enumerate(events):
        iid = sanitize(str(ev.get("id") or "?"))[0] or "?"
        kind = ev.get("kind")
        if kind == "ping":
            frm = sanitize(str(ev.get("from") or "?"))[0] or "?"
            snippet = sanitize(ev.get("text") or "")[0]
            if len(snippet) > EVENTS_SNIPPET:
                snippet = snippet[:EVENTS_SNIPPET] + "…"
            ref = sanitize(str(ev.get("ref") or ""))[0]
            part = '📨 from %s: "%s"' % (frm, snippet)
            if ref:
                part += " (ref %s)" % ref
        elif kind == "decision":
            decision = ev.get("decision") or "accepted"
            glyph = "✔" if decision == "accepted" else "✕"
            snippet = sanitize(ev.get("text") or "")[0]
            if len(snippet) > EVENTS_SNIPPET:
                snippet = snippet[:EVENTS_SNIPPET] + "…"
            part = '%s %s %s "%s"' % (glyph, iid, decision.upper(), snippet)
        else:                                 # done-transition
            glyph = "⚑" if ev.get("operator") else "✓"
            part = "%s %s done" % (glyph, iid)
        sep = " · " if parts else ""
        if parts and len(prefix) + _joined_len(parts) + len(sep) + len(part) > budget:
            remaining = len(events) - idx
            break
        parts.append(part)
    line = prefix + " · ".join(parts)
    if remaining:
        line += " (+%d more)" % remaining
    return line + suffix


def _events_detect_board(cfg, state, items):
    """Accrue + enqueue board-EVENT notices from the board `items` todo_watch already
    read (NO extra read). Runs BEFORE journal_cycle so state["todo_fp"] is still last
    cycle's snapshot — the reused "before" for the diff. First run (or todo_fp not yet
    seeded) seeds present decisions silently. Dedup by stable event id; the keeper's
    own CLI transitions (fresh todo-cli marker) are dropped, console ones fire."""
    if not cfg.get("board_events", True):
        return
    fp = state.get("todo_fp")
    ev = state.get("events")
    if ev is None:
        _events_seed(state, items)            # first sighting: adopt, emit nothing
        return
    if fp is None:
        return                                # journal reseeds fp this cycle; resume next
    seen = ev.setdefault("seen", [])
    markers = _read_cli_markers(cfg.get("events_marker_path") or TODO_CLI_MARKER_FILE)
    now = time.time()
    pending = []          # events to deliver
    suppressed = []       # (eid, iid, reason) for the audit trail
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = _todo_id(it)
        if not iid:
            continue
        old = fp.get(iid)
        if old is None:
            # brand-new id -> normally the quiet-gated add watcher owns it. EXCEPTION
            # (KEEPER-TASK-comms-ping): a ping delivers PROMPTLY on this board-event
            # lane (instant idle gate), so a busy keeper sees it in seconds, not after
            # the 10-min quiet hold. Same dedup ring + self-suppression as transitions.
            if _is_ping(it):
                eid = "evt:%s:ping" % iid
                if eid in seen:
                    continue
                if _cli_suppressed(markers, iid, now):
                    _events_seen_add(seen, eid)   # a ping the keeper wrote itself
                    suppressed.append((eid, iid))
                    continue
                _events_seen_add(seen, eid)
                frm, txt, ref = _ping_fields(it)
                pending.append({"id": iid, "kind": "ping", "from": frm,
                                "text": txt, "ref": ref})
            continue                          # non-ping new item -> add watcher owns it
        typ = it.get("type")
        decision = _event_decision(it)
        if decision is not None:
            # class 1: a proposal decision. One event per (id, decision); a proposal
            # that also went done never ALSO emits a done event (one event per
            # decision). Re-decision (accepted->declined) is a new token -> fires.
            eid = "evt:%s:%s" % (iid, decision)
            if eid in seen:
                continue
            if _cli_suppressed(markers, iid, now):
                _events_seen_add(seen, eid)   # keeper's own hand: swallow, never echo
                suppressed.append((eid, iid))
                continue
            _events_seen_add(seen, eid)
            pending.append({"id": iid, "kind": "decision", "decision": decision,
                            "text": it.get("text")})
            continue
        # class 2: a done-transition on a non-bookmark item (open/doing -> done). A
        # bookmark done is bookkeeping; an already-done item has no open/doing
        # predecessor and never fires.
        if typ == "bookmark":
            continue
        if it.get("status") == "done" and old.get("status") in ("open", "doing"):
            eid = "evt:%s:done" % iid
            if eid in seen:
                continue
            if _cli_suppressed(markers, iid, now):
                _events_seen_add(seen, eid)
                suppressed.append((eid, iid))
                continue
            _events_seen_add(seen, eid)
            pending.append({"id": iid, "kind": "done", "operator": typ == "operator"})

    if suppressed:
        audit({"decision": "board-event-suppress", "event": "board-event-suppress",
               "events": [e for e, _ in suppressed],
               "ids": [i for _, i in suppressed], "count": len(suppressed),
               "reason": "keeper-cli-marker", "queued": False})
        log("board events: suppressed %d self-originated transition(s)" % len(suppressed))

    # Enqueue in TWO classes so the drain-time quiet gate can treat them apart. A
    # PING is exempt from the board-quiet window — it delivers on the instantaneous
    # idle gate, past the 10-min hold (KEEPER-TASK-comms-ping §2: "the quiet hold
    # was the failure"). Decisions/done-transitions keep the normal quiet+max-hold
    # gating (operator, 2026-07-29 — board traffic delivers FROM A LULL). Split into
    # separate notices rather than one mixed line so a ping never drags a transition
    # past its gate, and a held transition never keeps a ping waiting. Same
    # board-event author, dedup ring, refilter and rendering for both.
    def _emit(group, exempt):
        if not group:
            return
        line = render_board_events(group)
        ids = [e["id"] for e in group]
        # `evs` carries the event dicts alongside the rendered line so the k33
        # delivery re-read can re-render for the surviving ids. A diff is not a
        # snapshot — an event ("k31 went done") cannot be rebuilt from the board
        # later, so if we don't keep it here, partial staleness can only be
        # delivered whole. Small and bounded (one dict per event in one notice).
        entry = {"ts": now, "author": "board-event", "payload": line,
                 "sha256": digest(line), "prefix": line[:40], "ids": ids,
                 "evs": group}
        if exempt:
            entry["exempt_quiet"] = True   # ping: idle-gated only, past the quiet hold
        enqueue(state, entry)
        audit({"decision": "board-event", "event": "board-event", "ids": ids,
               "kinds": [e["kind"] for e in group], "count": len(ids),
               "exempt_quiet": bool(exempt),
               "injected": bool(cfg.get("live")), "queued": True})

    if pending:
        _emit([e for e in pending if e.get("kind") == "ping"], True)
        _emit([e for e in pending if e.get("kind") != "ping"], False)
    state["events"]["seen"] = _events_cap(seen)
    save_state(state)


# ---------------------------------------------------------------- cycle


def intake(cfg, state):
    """Fetch, classify, enqueue. Advances the cursor over everything seen."""
    # Retention: sweep our own stale relay-* files before anything else. Quiet,
    # fail-closed, and blind to files the keeper saved (non-relay- names).
    try:
        prune_inbox(cfg.get("inbox") or INBOX_DIR)
    except Exception as exc:
        log("inbox prune failed: %s" % type(exc).__name__)

    msgs, err = fetch(cfg, state["last_ts"])
    if err:
        return "fetch-failed:%s" % err

    high = state["last_ts"]
    for m in msgs:
        ts = m.get("ts") if isinstance(m, dict) else None
        if isinstance(ts, (int, float)):
            high = max(high, float(ts))

        ok, reason = classify(m, cfg["allow"])
        author = (m.get("author") if isinstance(m, dict) else None) or "?"
        content = (m.get("content") if isinstance(m, dict) else None) or ""
        if not ok:
            audit({"ts": ts, "author": author, "decision": "drop",
                   "reason": reason, "queued": False,
                   "sha256": digest(content), "prefix": sanitize(content)[0][:40]})
            continue

        text, notes = sanitize(content)
        if not text:
            audit({"ts": ts, "author": author, "decision": "drop",
                   "reason": "sanitized-empty:%s" % ",".join(notes), "queued": False,
                   "sha256": digest(content), "prefix": ""})
            continue

        # Only now — a classified, non-empty-after-sanitize operator message —
        # do we reach out for the images. Dropped messages never got here, so a
        # fetch is never a side channel for a non-allowlisted author.
        atts = m.get("attachments")
        atts = atts if isinstance(atts, list) else []
        paths, n_skipped = fetch_attachments(cfg, atts, ts, author)
        payload = render(author, text, paths, n_skipped)

        enqueue(state, {
            "ts": ts, "author": author, "payload": payload,
            "sha256": digest(content), "prefix": text[:40],
        })
        audit({"ts": ts, "author": author, "decision": "accept",
               "reason": "allowlisted:%s" % (",".join(notes) or "clean"),
               "queued": True, "sha256": digest(content), "prefix": text[:40]})

    if high > state["last_ts"]:
        state["last_ts"] = high
    save_state(state)
    return None


def session_intake(cfg, state):
    """Fetch, classify, enqueue from the #keeper-keeper session channel — the
    SECOND source. Mirrors intake() but with: its own endpoint, its own ~30s
    cadence (the API's documented poll rate; the 15s cycle ticks past it), its
    own persisted cursor (state["session_last_ts"] — first sighting seeds at NOW
    so channel history is never replayed, the same rule main() applies to the
    primary), NO attachment stage (the session API carries text-only message
    objects), and every enqueued item stamped src="session" so drain() gates it
    on KEEPER_RELAY_SESSION_LIVE instead of KEEPER_RELAY_LIVE.

    The allowlist is cfg["allow"] — the SAME operator allowlist as the primary
    channel, never a second one — and classify()'s direction=="in" gate is the
    echo trap: the keeper's own posts come back direction:"out" (author
    "keeper-keeper") and drop with a logged reason. The cursor advances over
    EVERYTHING seen, drops included, so an echo is never re-examined.

    Called inside cycle()'s own try/except so a fault here never blocks the
    primary intake or the drain, and deliberately BEFORE intake() so a primary
    outage cannot stop the session channel from being read.
    """
    if not cfg.get("session_endpoint"):
        return                      # no key configured -> the source is inert

    now = time.time()
    if state.get("session_last_ts") is None:
        # First sighting of the source: cursor starts at NOW — session-channel
        # history is never replayed into the keeper's keyboard.
        state["session_last_ts"] = now
        state["session_last_poll"] = 0.0
        save_state(state)
        log("session source: first run — cursor starts at now, no replay")
        return
    if (now - state.get("session_last_poll", 0.0)) < SESSION_POLL_SECONDS:
        return                      # hold to the session API's ~30s cadence
    state["session_last_poll"] = now

    msgs, err = fetch(cfg, state["session_last_ts"], cfg["session_endpoint"])
    if err:
        save_state(state)           # persist the poll clock even on a bad fetch
        log("session-fetch-failed:%s" % err)   # exception type only, never the URL
        audit({"decision": "error", "reason": "session-fetch-failed:%s" % err,
               "source": "session", "queued": False})
        return

    high = state["session_last_ts"]
    for m in msgs:
        ts = m.get("ts") if isinstance(m, dict) else None
        if isinstance(ts, (int, float)):
            high = max(high, float(ts))

        ok, reason = classify(m, cfg["allow"])   # SAME fail-closed filter
        author = (m.get("author") if isinstance(m, dict) else None) or "?"
        content = (m.get("content") if isinstance(m, dict) else None) or ""
        if not ok:
            audit({"ts": ts, "author": author, "decision": "drop",
                   "reason": reason, "source": "session", "queued": False,
                   "sha256": digest(content), "prefix": sanitize(content)[0][:40]})
            continue

        text, notes = sanitize(content)
        if not text:
            audit({"ts": ts, "author": author, "decision": "drop",
                   "reason": "sanitized-empty:%s" % ",".join(notes),
                   "source": "session", "queued": False,
                   "sha256": digest(content), "prefix": ""})
            continue

        # Provenance names the CHANNEL too, so the keeper can see which surface
        # the operator spoke from — built by the SAME render() the primary uses
        # (no attachments on this source), so the tag format stays in one place.
        # Every character of it is our own; as with the primary tag it is
        # provenance, not a trust boundary.
        payload = render("keeper-keeper:%s" % author, text)
        enqueue(state, {
            "ts": ts, "author": author, "payload": payload, "src": "session",
            "sha256": digest(content), "prefix": text[:40],
        })
        audit({"ts": ts, "author": author, "decision": "accept",
               "reason": "allowlisted:%s" % (",".join(notes) or "clean"),
               "source": "session", "queued": True,
               "sha256": digest(content), "prefix": text[:40]})

    if high > state["session_last_ts"]:
        state["session_last_ts"] = high
    save_state(state)


def activity_cycle(cfg, state):
    """Refresh last_activity_ts whenever the keeper is observed WORKING, so the
    board quiet gate (below) can tell a genuine long lull from the blink between
    two turns. Either signal alone counts as activity:
        (a) the keeper transcript GREW since the previous cycle — reusing the
            steward's newest-*.jsonl reader (the same size the steward measures,
            NOT a second copy of that logic); and/or
        (b) a turn is IN FLIGHT this instant — idle_reason() reports the BUSY_MARKER.
    last_activity_ts and the last-seen transcript size are PERSISTED so a relay
    restart does not reset the clock to 'quiet' and release a held board batch.
    First appearance of the field seeds SILENTLY (treat now as fresh activity, adopt
    the current transcript size as the growth baseline) — like every other watcher's
    first run, a fresh relay never reads as already-quiet on tick one."""
    now = time.time()
    cur = _steward_transcript_bytes(cfg)   # newest *.jsonl bytes; None if unreadable

    if "last_activity_ts" not in state:
        state["last_activity_ts"] = now
        state["activity_bytes"] = cur if cur is not None else 0
        save_state(state)
        return

    changed = False
    active = False

    # (a) transcript growth since last cycle. A None (dir unreadable) is an UNKNOWN,
    #     not a shrink — leave the baseline untouched and infer no activity from it.
    if cur is not None:
        prev = state.get("activity_bytes", 0)
        if cur > prev:
            active = True
        if cur != prev:
            state["activity_bytes"] = cur
            changed = True

    # (b) busy marker: a turn is in flight right now. Read the pane only if the
    #     transcript did not already settle it (one fewer capture-pane per cycle).
    if not active:
        st, err = pane_state(cfg)
        if not err and idle_reason(st) == "busy(turn-in-flight)":
            active = True

    if active:
        state["last_activity_ts"] = now
        changed = True

    if changed:
        save_state(state)


def _cfg_relay_note(items):
    """The reserved 'cfg-relay' bookmark's note, parsed as a JSON object. Returns
    (note_dict, None) on a usable object note; (None, None) when there is NO
    cfg-relay bookmark at all (the common case — a silent fall-back); or
    (None, token) when a PRESENT bookmark's note cannot be used as an object
    (non-JSON / not a JSON object), for the audit-once path."""
    it = None
    for cand in items:
        if isinstance(cand, dict) and cand.get("id") == CFG_RELAY_ID:
            it = cand
            break
    if it is None:
        return None, None                       # no bookmark -> silent fall-back
    try:
        note = json.loads(it.get("note") or "")
    except (TypeError, ValueError):
        return None, "note-not-json"
    if not isinstance(note, dict):
        return None, "note-not-object"
    return note, None


def _cfg_relay_int_field(note, field, lo, hi):
    """Read one optional integer setting in [lo, hi] from a parsed cfg-relay note.
    Returns (value, None) on a usable int; (None, None) when the field is ABSENT
    (a deliberate omission on a multi-setting bookmark — fall back, NOT an error);
    or (None, token) when the field is present but unusable (wrong type / range)."""
    if field not in note:
        return None, None                       # omitted -> silent fall-back
    v = note[field]
    # A JSON integer only. bool is an int subclass in Python (a stray true/false
    # must not read as 1/0); floats/strings are the wrong shape and fall back.
    if isinstance(v, bool) or not isinstance(v, int):
        return None, "%s-not-int(%s)" % (field, type(v).__name__)
    if v < lo or v > hi:
        return None, "%s-out-of-range(%d)" % (field, v)
    return v, None


def _set_state(state, key, val):
    """Assign state[key]=val, returning True only if it actually changed (so the
    caller can batch a single save per cycle)."""
    if state.get(key) != val:
        state[key] = val
        return True
    return False


def _cfg_relay_audit_once(state, slot, bad, field):
    """Audit a bad cfg-relay value ONCE per distinct token — the last-audited token
    is remembered at state[slot], so a steady bad config never re-audits per cycle,
    yet a CHANGED bad value (or a fix-then-rebreak) does. Returns True if state
    changed. Clearing (bad=None) resets the memory silently."""
    if bad == state.get(slot):
        return False
    state[slot] = bad
    if bad is not None:
        audit({"decision": "cfg-relay", "event": "cfg-relay", "field": field,
               "reason": "ignored:%s" % bad, "source": "bookmark"})
    return True


def _cfg_relay_cycle(cfg, state, items):
    """Refresh the 'cfg-relay' bookmark overrides — the board quiet window AND the
    starvation cap — from THIS cycle's board read (no second file read), so an
    operator edit takes effect within one tick with no restart. Each override
    persists in state (…_bookmark: int, or None to fall through to env/default);
    a broken note/field audits ONCE per distinct bad value (tracked per-slot).
    The RESOLVED effective windows are also written back to state as plain ints
    ("board_quiet_secs" / "board_max_hold_secs") so other components — e.g. the
    bugreport activity meter (t46), which reads those exact keys from the state
    file — see the live values with no extra wiring."""
    note, note_bad = _cfg_relay_note(items)
    if note is None:
        quiet_v = quiet_bad = hold_v = hold_bad = None
    else:
        quiet_v, quiet_bad = _cfg_relay_int_field(
            note, "board_quiet_secs", 0, BOARD_QUIET_MAX)
        hold_v, hold_bad = _cfg_relay_int_field(
            note, "board_max_hold_secs", 0, BOARD_MAX_HOLD_MAX)

    changed = False
    changed |= _set_state(state, "board_quiet_bookmark", quiet_v)
    changed |= _set_state(state, "board_max_hold_bookmark", hold_v)
    # audit-once: the note-level failure (affects the whole bookmark), then each
    # field-level failure. When there is no usable note, the field slots clear.
    changed |= _cfg_relay_audit_once(state, "cfg_relay_note_bad", note_bad, "note")
    changed |= _cfg_relay_audit_once(
        state, "board_quiet_bad", quiet_bad, "board_quiet_secs")
    changed |= _cfg_relay_audit_once(
        state, "board_max_hold_bad", hold_bad, "board_max_hold_secs")

    # Persist the RESOLVED effective windows for cross-component reads (t46 meter).
    changed |= _set_state(
        state, "board_quiet_secs", int(_effective_board_quiet(cfg, state)[0]))
    changed |= _set_state(
        state, "board_max_hold_secs", int(_board_max_hold(cfg, state)[0]))

    if changed:
        save_state(state)


def _effective_board_quiet(cfg, state):
    """The quiet-window seconds in force + its SOURCE this cycle, as a (secs, src)
    pair. Precedence: 'cfg-relay' bookmark note > env > built-in default. The
    bookmark value is refreshed from the board read each cycle, so it needs no
    restart and no extra file read to take hold."""
    bm = state.get("board_quiet_bookmark")
    if isinstance(bm, (int, float)) and not isinstance(bm, bool):
        return float(bm), "bookmark"
    return (float(cfg.get("board_quiet_sec", BOARD_QUIET_SECS)),
            cfg.get("board_quiet_source", "default"))


def _board_max_hold(cfg, state):
    """The starvation-cap seconds in force + its SOURCE, as a (secs, src) pair —
    the longest a quiet-held [board] item may wait before it delivers ANYWAY (on
    the instantaneous idle gate). Precedence: 'cfg-relay' bookmark > env > default.
    A resolved value of 0 means NO cap: hold indefinitely (revert to pre-cap
    behaviour), never force a starved item out."""
    bm = state.get("board_max_hold_bookmark")
    if isinstance(bm, (int, float)) and not isinstance(bm, bool):
        return float(bm), "bookmark"
    return (float(cfg.get("board_max_hold_sec", BOARD_MAX_HOLD_SECS)),
            cfg.get("board_max_hold_source", "default"))


def _board_hold_expired(item, cfg, state, now):
    """True once a quiet-held [board] item has STARVED past the max-hold cap and
    must deliver anyway. A cap of 0 (no cap) or an item with no usable enqueue ts
    never forces — the age is measured from the item's own enqueue timestamp."""
    cap, _ = _board_max_hold(cfg, state)
    if cap <= 0:
        return False                            # 0 == no cap: hold indefinitely
    ts = item.get("ts")
    if not isinstance(ts, (int, float)):
        return False
    return (now - ts) >= cap


# ------------------------------------------------- cfg-relay one-shot mint (t75)


def _acquire_board_lock(board_path, timeout=None, poll=None):
    """Take the shared per-board advisory flock (<board-dir>/.todo.lock) — the
    SAME lockfile the todo CLI and console-api hold across their load->save, so
    a mint can never race a console write. Bounded wait (BOARD_LOCK_WAIT_SECS,
    polled every BOARD_LOCK_POLL_SECS), then give up: returns the open fd, or
    None on timeout / any OSError (fail-closed — the caller skips the tick and
    retries next cycle). The caller MUST release+close the fd in a finally."""
    timeout = BOARD_LOCK_WAIT_SECS if timeout is None else timeout
    poll = BOARD_LOCK_POLL_SECS if poll is None else poll
    lock_path = os.path.join(
        os.path.dirname(os.path.abspath(board_path)) or ".", ".todo.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.time() >= deadline:
                try:
                    os.close(fd)
                except OSError:
                    pass
                return None
            time.sleep(poll)


def _cfg_relay_mint_item(cfg):
    """The bookmark to mint — byte-for-byte the shape of blackbird's real one.
    The note carries the CURRENT env-or-default board-quiet value this daemon
    resolved at startup (config()'s board_quiet_sec), so the minted override
    equals what the env already said and live behaviour does not change. Stamped
    by:"keeper" AND via:"keeper" — todo_watch's self-echo filter (and the event
    watcher's, and the operator pinger's) suppresses on either marker, so the
    mint never announces itself as a [board] notice into the keeper pane."""
    return {
        "id": CFG_RELAY_ID,
        "type": "bookmark",
        "text": CFG_RELAY_MINT_TEXT,
        "note": json.dumps(
            {"board_quiet_secs": int(cfg.get("board_quiet_sec", BOARD_QUIET_SECS))}),
        "status": "done",
        "by": "keeper",
        "ts": int(time.time()),
        "via": "keeper",
    }


def _cfg_relay_mint_skip(state, reason):
    """Log a mint skip ONCE per distinct reason (persisted, same idiom as
    _cfg_relay_audit_once): a steady precondition failure — no board yet, a
    console mid-write, a busy lock — never log-spams per cycle, yet a CHANGED
    reason is still visible. The mint itself retries every cycle regardless."""
    if state.get("cfg_relay_mint_skip") == reason:
        return
    state["cfg_relay_mint_skip"] = reason
    save_state(state)
    log("cfg-relay mint: %s — skipping (will retry next cycle)" % reason)


def _cfg_relay_mint_done(state, how):
    """Set the PERMANENT one-shot marker: this VM never mints again — not even
    if the operator later deletes cfg-relay deliberately (we do not fight)."""
    state["cfg_relay_minted"] = True
    state.pop("cfg_relay_mint_skip", None)
    save_state(state)
    log("cfg-relay mint: %s — marker set, never minting again on this VM" % how)


def _cfg_relay_mint(cfg, state):
    """The guarded mint: lock -> read -> check -> append -> atomic write ->
    verify. Every precondition failure skips (logged once) and retries next
    cycle; an already-present cfg-relay sets the marker and touches NOTHING
    (one or many — duplicates are the dup-id guard's business, never ours)."""
    board = cfg.get("todo_path") or TODO_FILE
    if not os.path.exists(board):
        _cfg_relay_mint_skip(state, "board %s absent" % board)
        return
    lock_fd = _acquire_board_lock(board)
    if lock_fd is None:
        _cfg_relay_mint_skip(state, "board lock busy/unavailable")
        return
    tmp = board + ".cfg-relay-tmp"
    wrote_tmp = False
    try:
        # Read + preconditions — ALL under the lock, held across read->write.
        try:
            with open(board, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            _cfg_relay_mint_skip(
                state, "board unreadable (%s)" % type(exc).__name__)
            return
        if (not isinstance(doc, dict) or doc.get("schema") != "todo.v1"
                or not isinstance(doc.get("items"), list)):
            _cfg_relay_mint_skip(state, "board is not a todo.v1 items object")
            return
        if any(isinstance(it, dict) and it.get("id") == CFG_RELAY_ID
               for it in doc["items"]):
            _cfg_relay_mint_done(state, "bookmark already on the board")
            return

        # Mint: append + atomic same-directory replace, the console-api shape
        # (json.dumps(doc, indent=2) + newline).
        doc["items"].append(_cfg_relay_mint_item(cfg))
        wrote_tmp = True
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2) + "\n")
        os.replace(tmp, board)
        wrote_tmp = False

        # Verify-after-write: only a re-read that parses AND contains cfg-relay
        # earns the permanent marker; anything less retries next cycle.
        with open(board, encoding="utf-8") as fh:
            after = json.load(fh)
        if not (isinstance(after, dict)
                and any(isinstance(it, dict) and it.get("id") == CFG_RELAY_ID
                        for it in (after.get("items") or []))):
            _cfg_relay_mint_skip(state, "verify-after-write failed")
            return
        audit({"decision": "cfg-relay-mint", "event": "cfg-relay-mint",
               "board_quiet_secs": int(cfg.get("board_quiet_sec",
                                               BOARD_QUIET_SECS))})
        _cfg_relay_mint_done(state, "minted (board_quiet_secs=%d)"
                             % int(cfg.get("board_quiet_sec", BOARD_QUIET_SECS)))
    finally:
        if wrote_tmp:                   # best effort: never leave a temp behind
            try:
                os.unlink(tmp)
            except OSError:
                pass
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except OSError:
            pass


def cfg_relay_mint_cycle(cfg, state):
    """One-shot 'mint cfg-relay if missing' (board t75) — makes the console's
    "⏱ push" cadence dropdown functional on every VM that runs this daemon.
    Runs at startup and every main-loop cycle until it either succeeds or
    permanently no-ops (state["cfg_relay_minted"]); fail-closed throughout —
    ANY unexpected exception is caught and logged once, the relay's actual
    duties are never affected."""
    if state.get("cfg_relay_minted"):
        return
    try:
        _cfg_relay_mint(cfg, state)
    except Exception as exc:
        _cfg_relay_mint_skip(state, "unexpected %s" % type(exc).__name__)


def _board_quiet_remaining(state, cfg, now):
    """Seconds the board quiet gate still requires before author=='board' items may
    deliver; 0.0 once the keeper has been inactive for the full window. A relay that
    has never recorded activity reads as long-quiet -> 0 (activity_cycle seeds the
    field before drain runs, so in practice this is only the never-seeded edge).
    A window of 0 (bookmark override = gate off) also yields 0 -> board items pass
    on the instantaneous idle gate alone, exactly like pre-t44."""
    quiet, _ = _effective_board_quiet(cfg, state)
    last = state.get("last_activity_ts")
    if last is None:
        return 0.0
    return max(0.0, quiet - (now - last))


# ── attribution-based release (operator 2026-08-31 — REPLACES the time lull) ────
#
# The quiet-window LULL "hasn't worked out", and time periods are out — the
# 2026-07-29 ruling already turned the max-hold cap OFF by default (a cap is a
# timer that fires regardless of lull; it once delivered a 65-min-stale notice).
# So a queued board notice no longer waits on a clock. It releases when the keeper
# is BETWEEN TASKS, judged by the keeper's OWN task/status attribution:
#
#   1. board `doing` status — NOTHING in flight  => between tasks (deterministic);
#   2. a printed `status=done|blocked` line in the just-finished reply           ;
#   3. hugpy-agent (local model) judges an in-flight reply CLEARLY done/blocked —
#      on the AMBIGUOUS path only (something is `doing`, no printed terminal).
#
# The pane is already confirmed idle (turn complete, empty box) by drain() before
# any of this, so "between tasks" is the ADDITIONAL gate that stands in for the
# old lull. Fail-closed throughout: an unreadable board or an unclear/one-word-
# absent verdict HOLDS; the item stays open on the board as the durable backstop,
# and the fallback timer (BOARD_MAX_HOLD_SECS, default 0 = off) is the only time
# escape, enabled per-station only if one demonstrably starves.

# explicit terminal attribution the keeper can print in its reply (best-effort)
STATUS_DONE_RE = re.compile(
    r"\bstatus\s*[=:]\s*(done|blocked|complete|completed|finished)\b", re.I)
ATTRIB_TAIL_LINES = 40          # pane lines handed to the reply scan / agent judge


def _current_inflight(cfg):
    """Board ids the keeper marked `doing` = its in-flight work. One board read;
    an absent/half-written/wrong-shape board returns None so the caller HOLDS
    (never deliver blind), not [] (which would read as 'between tasks')."""
    items, err = read_todo(cfg.get("todo_path") or TODO_FILE)
    if err or not isinstance(items, list):
        return None
    return [it for it in items
            if isinstance(it, dict) and it.get("status") == "doing"]


def _reply_tail(st, n=ATTRIB_TAIL_LINES):
    """The last non-empty pane lines = the keeper's just-finished reply."""
    lines = [ln for ln in (st.get("lines") or []) if ln.strip()]
    return lines[-n:]


def _printed_terminal(st):
    """True when the reply carries an explicit `status=done|blocked` attribution."""
    return any(STATUS_DONE_RE.search(ln) for ln in _reply_tail(st))


def _agent_judge_release(cfg, inflight, st):
    """AMBIGUOUS path: ask hugpy-agent whether the keeper's just-finished reply
    CLEARLY reports the in-flight work done/blocked (safe to hand it a new item).
    Opt-in via KEEPER_RELAY_ATTRIB_AGENT; fully wrapped, timeout-bounded and
    FAIL-CLOSED — any error/timeout/unclear verdict -> (False, reason). hugpy-agent
    models 'done' as a fail-closed final_answer signal, so we ask it to final_answer
    exactly RELEASE or HOLD and release only on a clean RELEASE."""
    if not os.environ.get("KEEPER_RELAY_ATTRIB_AGENT", "").strip():
        return False, "agent-off"
    binary = os.environ.get("KEEPER_RELAY_ATTRIB_AGENT_BIN", "hugpy-agent")
    try:
        timeout = int(os.environ.get("KEEPER_RELAY_ATTRIB_AGENT_TIMEOUT", "45"))
    except ValueError:
        timeout = 45
    tasks = "; ".join(str(it.get("text") or it.get("id") or "?")[:120]
                      for it in inflight[:5])
    reply = "\n".join(_reply_tail(st))[-4000:]
    prompt = (
        "You are a delivery gate for a keeper agent. These tasks are IN FLIGHT "
        "(status=doing): %s\n\nThe keeper's just-finished reply was:\n---\n%s\n---\n\n"
        "Decide whether the keeper has CLEARLY finished or blocked the in-flight "
        "work and is now between tasks (safe to hand it a new board item). Be "
        "strict: if it is still mid-task or ambiguous, HOLD. Call final_answer with "
        "exactly one word: RELEASE or HOLD." % (tasks or "(none named)", reply))
    try:
        proc = subprocess.run([binary, "run", prompt, "--policy", "readonly",
                               "--max-steps", "2", "-q"],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "unavailable:%s" % type(exc).__name__
    try:
        report = json.loads((proc.stdout or "").strip() or "{}")
    except ValueError:
        return False, "unparseable-report"
    if report.get("outcome") != "done":
        return False, "outcome:%s" % report.get("outcome")
    answer = str(report.get("answer") or "").strip().upper()
    return answer.startswith("RELEASE"), "verdict:%s" % (answer[:12] or "empty")


def _board_release_eligible(cfg, state, st, now):
    """Is the keeper at a clean BETWEEN-TASKS boundary right now? (Pane already
    confirmed idle by drain().) Returns (eligible, reason). REPLACES the time lull:
    release is driven by the keeper's task/status attribution, not a clock. The
    hugpy-agent verdict is cached by a fingerprint of the reply so the model is
    consulted at most once per distinct reply, not on every idle poll."""
    inflight = _current_inflight(cfg)
    if inflight is None:
        return False, "board-unreadable"          # can't tell -> hold, never blind
    if not inflight:
        return True, "no-task-in-flight"          # nothing `doing` -> between tasks
    if _printed_terminal(st):
        return True, "printed-status-terminal"
    fp = digest("\n".join(_reply_tail(st)))
    cache = state.get("attrib_judge") or {}
    if cache.get("fp") == fp:                      # same reply -> reuse the verdict
        return bool(cache.get("release")), "agent-cached:%s" % cache.get("reason", "")
    release, reason = _agent_judge_release(cfg, inflight, st)
    state["attrib_judge"] = {"fp": fp, "release": bool(release), "reason": reason}
    save_state(state)
    return bool(release), "agent:%s" % reason


def drain(cfg, state):
    """Inject at most ONE queued message, and only into a confirmed-idle pane."""
    if not state["queue"]:
        return
    st, err = pane_state(cfg)
    if err:
        log("pane read failed, holding queue: %s" % err)
        audit({"decision": "hold", "reason": "pane-read-failed:%s" % err,
               "queued": True, "depth": len(state["queue"])})
        return
    why = idle_reason(st)
    if why:
        head = state["queue"][0]
        audit({"ts": head.get("ts"), "author": head.get("author"),
               "decision": "hold", "reason": why, "queued": True,
               "sha256": head.get("sha256"), "prefix": head.get("prefix", ""),
               "depth": len(state["queue"])})
        return

    # Pane is injectable. Board notices are QUEUE work: they additionally wait for
    # the keeper to be BETWEEN TASKS (attribution gate, operator 2026-08-31 —
    # replaces the old KEEPER_RELAY_BOARD_QUIET_SECS lull). Ineligible board items
    # STAY QUEUED in place (normal queue semantics, never dropped); every other
    # author drains past them. What is EXEMPT from the gate (delivered on the
    # instantaneous idle gate alone):
    #   - direct operator lines, the [lull] digest, the steward, [peer-msg] notices
    #     (author==PEER_AUTHOR) — conversation and safety, not queue;
    #   - a ping (author=="board-event" carrying exempt_quiet) — a poke must land
    #     promptly (KEEPER-TASK-comms-ping §2).
    # FALLBACK TIMER: BOARD_MAX_HOLD_SECS (default 0 = OFF, per the 2026-07-29
    # ruling) is the only time escape — a board item held past it delivers anyway.
    now = time.time()
    idx = None
    cap_fired = False
    hold_reason = None
    _elig = {}

    def _release_verdict():
        # Compute the between-tasks verdict at most ONCE per drain (it is about the
        # keeper, not the item), so a queue of board items never re-invokes the
        # hugpy-agent judge per entry.
        if "v" not in _elig:
            _elig["v"] = _board_release_eligible(cfg, state, st, now)
        return _elig["v"]

    for i, q in enumerate(state["queue"]):
        if q.get("author") in ("board", "board-event") and not q.get("exempt_quiet"):
            eligible, why = _release_verdict()
            if not eligible:
                if not _board_hold_expired(q, cfg, state, now):
                    hold_reason = "attrib-hold:%s" % why    # keeper still mid-task
                    continue
                cap_fired = True                # fallback timer forced it (off by default)
        idx = i
        break
    if idx is None:
        # Everything queued is a board notice held by the attribution gate. Hold —
        # audited every cycle, the SAME idiom as the instantaneous idle hold above.
        head = state["queue"][0]
        audit({"ts": head.get("ts"), "author": head.get("author"),
               "decision": "hold", "reason": hold_reason or "attrib-hold",
               "queued": True, "sha256": head.get("sha256"),
               "prefix": head.get("prefix", ""), "depth": len(state["queue"])})
        return

    item = state["queue"][idx]

    # DELIVERY-TIME BOARD RE-READ (k33). Everything above decided WHETHER the pane
    # can take a line; this decides whether the line is still TRUE. Board-derived
    # notices were rendered at detection, possibly hours ago — re-judge the ids
    # against the board as it is now (full contract at _queue_refilter_at_delivery).
    # A suppression pops the entry and ends the tick without injecting: the pop is
    # the point, and the next eligible item rides the next cycle. Deliberately
    # BEFORE the cap_fired audit below, so a notice we then suppress never leaves a
    # "delivered because it starved" record it didn't earn. Wrapped: a fault on a
    # notification path must fall through to notifying.
    try:
        _deliver = _queue_refilter_at_delivery(cfg, state, item)
    except Exception as exc:
        log("delivery re-read error (%s) — delivering as queued" % type(exc).__name__)
        audit({"decision": "delivery-refilter",
               "reason": "refilter-exception:%s" % type(exc).__name__,
               "author": item.get("author"), "delivered": True,
               "suppressed": False, "queued": False})
        _deliver = True
    if not _deliver:
        state["queue"].pop(idx)
        save_state(state)
        return

    if cap_fired:
        # Honest trail: this board item is delivering because it starved past the
        # cap, NOT because the keeper went quiet. Emitted alongside the normal
        # inject/dry-run audit that follows.
        audit({"ts": item.get("ts"), "author": item.get("author"),
               "decision": "board-max-hold",
               "reason": "board-max-hold-exceeded(%ds)" % int(now - item.get("ts", now)),
               "queued": False, "sha256": item.get("sha256"),
               "prefix": item.get("prefix", ""), "depth": len(state["queue"])})
    # Per-SOURCE live gate: a session-channel item (src=="session") injects only
    # under ITS OWN opt-in (KEEPER_RELAY_SESSION_LIVE); everything else stays
    # under KEEPER_RELAY_LIVE. Independent by design — either source can go live
    # without the other, and each defaults to dry-run. Same pane gate, same
    # board/quiet selection above, same at-most-once pop, same audit shape (plus
    # a "source" field on session items only) either way.
    src = item.get("src")           # "session" for the second channel, else None
    live = cfg.get("session_live", False) if src == "session" else cfg["live"]
    if not live:
        state["queue"].pop(idx)
        save_state(state)
        rec = {"ts": item["ts"], "author": item["author"], "decision": "dry-run",
               "reason": "would-inject(pane-idle)", "queued": False,
               "sha256": item["sha256"], "prefix": item["prefix"]}
        if src:
            rec["source"] = src
        audit(rec)
        log("DRY-RUN would inject from %s (%d chars)"
            % (item["author"], len(item["payload"])))
        return

    # At-most-once: pop BEFORE typing. A duplicated instruction to a
    # bypass-permissions keeper is worse than a lost one the operator can resend.
    state["queue"].pop(idx)
    save_state(state)
    ok, reason = inject(cfg, item["payload"])
    rec = {"ts": item["ts"], "author": item["author"],
           "decision": "inject" if ok else "inject-unconfirmed",
           "reason": reason, "queued": False,
           "sha256": item["sha256"], "prefix": item["prefix"]}
    if src:
        rec["source"] = src
    audit(rec)
    log("%s from %s: %s" % ("injected" if ok else "UNCONFIRMED", item["author"], reason))


def disabled():
    return os.path.exists(DISABLE_FILE)


def cycle(cfg, state):
    if disabled():
        log("kill switch present (%s) — idling" % DISABLE_FILE)
        return
    activity_cycle(cfg, state)  # refresh last_activity_ts BEFORE any drain gate reads it
    # One-shot cfg-relay mint (t75) BEFORE todo_watch, so the same cycle's board
    # read already sees the minted bookmark (marks it seen; via:"keeper"
    # suppresses the self-echo ping). Fail-closed; a no-op once the marker is set.
    cfg_relay_mint_cycle(cfg, state)
    todo_watch(cfg, state)  # board notices are independent of the Discord fetch
    opping_cycle(cfg, state)  # outbound operator pings — independent of the fetch too
    steward_cycle(cfg, state)  # arm/warn/lull-restart — independent of Discord too
    try:
        session_intake(cfg, state)  # second source — inert without its key; a
    except Exception as exc:        # session fault must not stop the primary
        log("session intake error (%s) — skipping this tick" % type(exc).__name__)
        audit({"decision": "error", "reason": "session-exception:%s"
               % type(exc).__name__, "source": "session", "queued": False})
    err = intake(cfg, state)
    if err:
        log(err)
        audit({"decision": "error", "reason": err, "queued": False})
        # Leave the queue alone; retry next cycle. ONE exception (t-session):
        # an OFFLINE station (no STATION_DISCORD_API at all) that DOES have a
        # session endpoint would otherwise never drain — the primary "error" is
        # permanent, not transient, so returning here would queue session items
        # forever. Only that exact case falls through; a real primary fetch
        # failure still holds the queue exactly as before.
        if not (err == "fetch-failed:offline-no-endpoint"
                and cfg.get("session_endpoint")):
            return
    lull_cycle(cfg, state)  # accrue candidates, and at a lull spawn ONE digest
    drain(cfg, state)


# ---------------------------------------------------------------- main


def cmd_status(cfg, state):
    ep = cfg["endpoint"]
    if not ep:
        # Offline mode: say so plainly rather than printing a masked empty token
        # (the old rpartition on "" rendered a confusing "/…").
        print("endpoint : (none — OFFLINE: local subsystems only)")
    else:
        head, _, tok = ep.rpartition("/")
        print("endpoint : %s/%s…%s" % (head, tok[:4], tok[-4:]))
    sep = cfg.get("session_endpoint")
    if sep:                          # masked exactly like the primary — the tail
        shead, _, stok = sep.rpartition("/")   # of the URL IS the session key
        print("session  : %s/%s…%s (%s, cursor %s)" % (
            shead, stok[:4], stok[-4:],
            "LIVE" if cfg.get("session_live") else "dry-run",
            state.get("session_last_ts", "unseeded")))
    else:
        print("session  : OFF (no KEEPER_KEEPER_DISCORD_KEY)")
    print("allowlist: %s" % ", ".join(sorted(cfg["allow"])))
    print("target   : -L %s %s" % (cfg["socket"], cfg["target"]))
    print("inbox    : %s" % (cfg.get("inbox") or INBOX_DIR))
    print("todo     : %s (watch %s)" % (
        cfg.get("todo_path") or TODO_FILE,
        "on" if cfg.get("todo_notify", True) else "OFF"))
    la = state.get("last_activity_ts")
    qsec, qsrc = _effective_board_quiet(cfg, state)
    hsec, hsrc = _board_max_hold(cfg, state)
    cap = ("cap>%ds (%s)" % (int(hsec), hsrc)) if hsec > 0 else "cap OFF (%s)" % hsrc
    print("board gate: quiet>%ds (%s)%s · %s — %s" % (
        int(qsec), qsrc, " GATE OFF" if qsec <= 0 else "", cap,
        ("last activity %ds ago" % int(max(0, time.time() - la)))
        if la else "no activity recorded yet"))
    print("seen ids : %d" % len(state.get("todo_seen") or []))
    print("journal  : %s (%d tracked ids%s)" % (
        cfg.get("history_path") or TODO_HISTORY_FILE,
        len(state.get("todo_fp") or {}),
        ", WRITE-FAILING" if state.get("journal_write_failing") else ""))
    lull = state.get("lull") or {}
    print("lull     : %s (stale>%dm wait>%dm cooldown>%dm)" % (
        "on" if cfg.get("lull", True) else "OFF",
        int(cfg.get("lull_stale_sec", 0) // 60),
        int(cfg.get("lull_wait_sec", 0) // 60),
        int(cfg.get("lull_cooldown_sec", 0) // 60)))
    print("brain    : %s @ %s" % (cfg.get("hugpy_brain"), cfg.get("hugpy_api")))
    print("lull cand: %d pending, %d delivered" % (
        len(lull.get("candidates") or {}), len(lull.get("done") or [])))
    sd = state.get("steward") or {}
    # --status MUST report what the steward will ACTUALLY do, which means applying
    # the ~/steward.json overlay exactly as steward_cycle does. Printing the raw
    # env/default cfg made this line say "OFF ... thr=300000" on a fleet that was
    # configured on/500000 — the operator's own status view contradicting the
    # operator's own config. A status that can be wrong is worse than no status.
    scfg = _steward_effective_cfg(cfg)
    print("steward  : %s (restart=%s cadence>%dm warn>%dm thr=%d floor=%d)" % (
        "on" if scfg.get("steward") else "OFF",
        scfg.get("steward_restart", "dry"),
        int(scfg.get("steward_cadence_sec", 0) // 60),
        int(scfg.get("steward_warn_lead_sec", 0) // 60),
        int(scfg.get("steward_threshold_bytes", 0)),
        int(scfg.get("steward_floor_bytes", 0))))
    print("steward ph: %s%s" % (
        sd.get("phase", "—"),
        (" deadline=%d" % int(sd["deadline"])) if sd.get("deadline") else ""))
    op = state.get("opping") or {}
    print("op-ping  : %s (cooldown>%dm) — %d pending, %d delivered" % (
        cfg.get("operator_ping", "on"),
        int(cfg.get("operator_ping_cooldown_sec", 0) // 60),
        len(op.get("pending") or {}), len(op.get("seen") or [])))
    evs = state.get("events") or {}
    print("board evt: %s (marker %s, window>%ds) — %d seen" % (
        "on" if cfg.get("board_events", True) else "OFF",
        cfg.get("events_marker_path") or TODO_CLI_MARKER_FILE,
        EVENT_SUPPRESS_WINDOW, len(evs.get("seen") or [])))
    print("mode     : %s" % ("LIVE (will inject)" if cfg["live"] else "dry-run"))
    print("cursor   : %s" % state["last_ts"])
    print("queued   : %d" % len(state["queue"]))
    print("kill sw  : %s" % ("PRESENT — relay idle" if disabled() else "absent"))
    st, err = pane_state(cfg)
    print("pane     : %s" % (err if err else (idle_reason(st) or "idle (injectable)")))


def main():
    ap = argparse.ArgumentParser(description="keeper-relay")
    ap.add_argument("--live", action="store_true",
                    help="actually inject (default is dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry-run (default)")
    ap.add_argument("--once", action="store_true", help="one cycle then exit")
    ap.add_argument("--status", action="store_true", help="print status and exit")
    ap.add_argument("--reset", action="store_true",
                    help="request a keeper reset from INSIDE the vm (stamps the "
                         "live session nonce, so it cannot fire at a later session)")
    args = ap.parse_args()

    if args.reset:
        sys.exit(_steward_request_reset())

    cfg = config()
    if args.live and not args.dry_run:
        cfg["live"] = True          # --live is the PRIMARY source's flag only;
        # the session source goes live solely via KEEPER_RELAY_SESSION_LIVE=1.
    if args.dry_run:
        cfg["live"] = False
        cfg["session_live"] = False  # explicit dry-run silences BOTH sources

    # Ensure the fetched-image directory exists up front (exist_ok — the keeper
    # may already use it for its own files). Never fatal if it can't be made.
    try:
        os.makedirs(cfg.get("inbox") or INBOX_DIR, exist_ok=True)
    except OSError as exc:
        log("could not create inbox %s: %s" % (cfg.get("inbox"), exc))

    state = load_state()
    if state is None:
        # First run: start from NOW so we never replay channel history into the
        # keeper's keyboard.
        state = {"last_ts": time.time(), "queue": []}
        save_state(state)
        log("first run — cursor starts at now, history will not be replayed")

    if args.status:
        cmd_status(cfg, state)
        return

    log("starting: mode=%s session=%s target=%s allow=%s"
        % ("LIVE" if cfg["live"] else "dry-run",
           ("LIVE" if cfg.get("session_live") else "dry-run")
           if cfg.get("session_endpoint") else "off",
           cfg["target"], ",".join(sorted(cfg["allow"]))))

    if args.once:
        cycle(cfg, state)
        return

    backoff = POLL_SECONDS
    while True:
        try:
            cycle(cfg, state)
            backoff = POLL_SECONDS
        except Exception as exc:  # a daemon that dies is a daemon that is not there
            log("cycle error (%s), backing off %ds" % (type(exc).__name__, backoff))
            audit({"decision": "error", "reason": "cycle-exception:%s"
                   % type(exc).__name__, "queued": False})
            backoff = min(backoff * 2, 300)
        time.sleep(backoff)


if __name__ == "__main__":
    main()
