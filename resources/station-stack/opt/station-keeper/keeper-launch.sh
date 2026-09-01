#!/usr/bin/env bash
# station-keeper: the keeper claude launcher. Run inside the tmux session.
#
# v2 (2026-07-01 overhaul §3E — keepers booted confused):
#   - ONE standing directive, with defined precedence: the project's
#     .keeper-directive.md wins; /opt/llm-station/directive.md is only the
#     fallback when no project directive exists. (The old script appended
#     BOTH; on hugpy they contradicted each other — a stale pre-rename doc
#     stacked under the current one.)
#   - launch in the project tree, not /: CWD orients every relative path the
#     keeper reads or writes. Isolated VMs (no /srv/share) fall back to $HOME.
#   - a generated orientation preamble: today's date, the station, the CWD,
#     and WHICH directive was loaded — facts the model cannot otherwise know
#     at boot, injected fresh each launch instead of trusted from memory.
set -u
STATION="${1:-$(hostname)}"
c="$HOME/.local/bin/claude"; [ -x "$c" ] || c=claude

# Single-session enforcement (operator, 2026-07-31): the keeper is ONE persistent
# foreground session, not a fan-out of daemon-managed background/agent-view jobs.
# CLAUDE_CODE_DISABLE_AGENT_VIEW=1 disables the Claude Code supervisor daemon, its
# warm bg-spare pool, and persistent background job-sessions, so every turn runs in
# this one tmux-pane session. Enforces the _global.md single-session doctrine at the
# harness level. Verified 2026-07-31: keeper boots clean and synchronous subagent
# dispatch (Task tool / keeper-spawn claude -p) is unaffected (runs inline).
export CLAUDE_CODE_DISABLE_AGENT_VIEW=1

PROJ="/srv/share/projects/$STATION"
DIRECTIVE=""
for d in "$PROJ/.keeper-directive.md" /opt/llm-station/directive.md; do
  if [ -s "$d" ]; then DIRECTIVE="$d"; break; fi
done

# Global layer (2026-07-04): fleet-wide keeper role (warden + dispatcher),
# loaded BENEATH the station/project directive so specific refines general.
# The precedence chain global -> station was documented but never injected;
# this makes it real at runtime. Absent on isolated VMs (no shared plane) — fine.
GLOBAL=""
for g in /srv/share/scripts/directives/_global.md /opt/llm-station/_global.md; do
  if [ -s "$g" ]; then GLOBAL="$g"; break; fi   # isolated VMs get a copied-in fallback
done

SOP=""
for s in /srv/share/scripts/directives/TODO-BOARD-SOP.md /opt/llm-station/TODO-BOARD-SOP.md; do
  if [ -s "$s" ]; then SOP="$s"; break; fi
done

# Router mode (steward drawer "🛰 router (lean launch)"): the console writes
# router_mode into ~/steward.json; ON drops the per-launch directive appends
# (global/directive/SOP, ~2.2k tokens riding EVERY turn) in favour of a one-time
# primer in the first user turn that tells the keeper to read them itself.
ROUTER_MODE=0
if [ -s "$HOME/steward.json" ]; then
  ROUTER_MODE="$(python3 -c 'import json,os,sys
try: sys.stdout.write("1" if json.load(open(os.path.expanduser("~/steward.json"))).get("router_mode") else "0")
except Exception: sys.stdout.write("0")' 2>/dev/null || echo 0)"
fi

# Work from the project tree when it exists (isolated VMs: $HOME).
WORKDIR="$HOME"
[ -d "$PROJ" ] && WORKDIR="$PROJ"
cd "$WORKDIR" 2>/dev/null || cd "$HOME" || true

preamble="ORIENTATION (generated at launch — trust this over memory):
- date: $(date '+%Y-%m-%d %H:%M %Z')
- station: $STATION (LXD guest; hostname $(hostname))
- working directory: $WORKDIR
- global directive loaded: ${GLOBAL:-none found}
- standing directive loaded: ${DIRECTIVE:-none found}
- project share: $([ -d "$PROJ" ] && echo "$PROJ" || echo "not present on this VM — do not reference /srv/share paths")
- your tmux session: 'keeper-claude' (or legacy 'keeper') on socket 'console' (the web console attaches here)
- router mode: $([ "$ROUTER_MODE" = 1 ] && echo "ON (directives NOT appended — read them per the primer)" || echo off)
State drifts between launches — verify services/files before acting on them."

args=(--append-system-prompt "$preamble")
if [ "$ROUTER_MODE" != 1 ]; then
  [ -n "$GLOBAL" ] && args+=(--append-system-prompt "$(cat "$GLOBAL")")
  [ -n "$DIRECTIVE" ] && args+=(--append-system-prompt "$(cat "$DIRECTIVE")")
  [ -n "$SOP" ] && args+=(--append-system-prompt "$(cat "$SOP")")
fi

# One-time primer replacing the appends when router mode is ON: point at the
# directive files (read once, now) and the finder/local-keeper routing, instead
# of re-shipping their full text on every turn.
PRIMER=""
if [ "$ROUTER_MODE" = 1 ]; then
  PRIMER="ROUTER-MODE PRIMER (one-time — your standing directives were NOT appended \
to the system prompt this launch to keep per-turn context lean): read these now, \
once, and follow them for the whole session: \
${GLOBAL:+global directive: $GLOBAL; }${DIRECTIVE:+standing directive: $DIRECTIVE; }\
${SOP:+todo-board SOP: $SOP; }prefer finder (search) and local-keeper requests over \
raw filesystem sweeps. Re-read a directive file only if told it changed.

"
fi

charge="You are the KEEPER of the LXD station '$STATION' — its long-lived warden \
and dispatcher. You watch over the VM and its effects: its health, services, \
project files, and the consequences of changes made inside it. For substantive \
work you assess, assign an implementer agent (subagent) with a scoped brief, \
verify its results, and report — you implement directly only for trivial fixes \
or emergencies. Investigate before acting, prefer reversible steps, and report \
what you observe."
exec "$c" --dangerously-skip-permissions "${args[@]}" "$PRIMER$charge"
