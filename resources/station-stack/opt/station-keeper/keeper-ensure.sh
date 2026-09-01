#!/usr/bin/env bash
# station-keeper: ensure the persistent keeper tmux session exists. Idempotent —
# run at boot and periodically by the timer. Creates the frontier keeper's
# session on the 'console' tmux socket (the same socket/session the web
# console attaches to, so a boot-started keeper and a console-started one are
# interchangeable). Exits 0 even when it can't start (no claude/tmux) so the
# timer never enters a fail loop.
#
# EVERY backend has its OWN explicitly named session (operator, 2026-08-22) —
# this table MUST match BACKEND_TMUX_SESSION in the console's server.py:
#   frontier: mct → keeper-mct   claude-code → keeper-claude   clawd-code → keeper-clawd
#   local:    opencode → keeper-local-opencode   qwen-code → keeper-local-qwen
# The legacy shared name 'keeper' is never created any more: with one name,
# `new-session -A` handed whichever backend arrived first to every caller.
# KEEPER_BACKEND (mct|claude) selects which frontier backend this timer keeps.
set -u
STATION="${1:-$(hostname)}"
SOCK=console
KEEPER_BACKEND="${KEEPER_BACKEND:-mct}"
case "$KEEPER_BACKEND" in
  mct)    SESS=keeper-mct ;;
  claude) SESS=keeper-claude ;;
  *) echo "station-keeper: unknown KEEPER_BACKEND=$KEEPER_BACKEND (mct|claude)"; exit 0 ;;
esac
export TERM="${TERM:-xterm-256color}"
# Fleet Claude OAuth token (claude-oauth-sync): the timer's environment has no
# login profile, so source it here — the tmux server inherits it and every
# keeper started in it authenticates without ~/.claude/.credentials.json.
[ -r "$HOME/.claude-oauth.env" ] && . "$HOME/.claude-oauth.env"

command -v tmux >/dev/null 2>&1 || { echo "station-keeper: tmux missing; cannot persist keeper"; exit 0; }
if tmux -L "$SOCK" has-session -t "=$SESS" 2>/dev/null; then exit 0; fi   # already running

# Frontier keeper = mct (operator, 2026-08-21): the console's frontier surface
# attaches to this same session, so ONE process serves timer and console.
# Claude Code (keeper-launch.sh) remains the fallback where mct is absent.
export PATH="$HOME/.local/bin:$PATH"
if [ "$KEEPER_BACKEND" = mct ] && command -v abstract-claude >/dev/null 2>&1; then
  # mct backend = abstract-claude mct (2026-08-31: REPLACES the retired
  # `hugpy-agent mct` broker — the source of the stale-broker keeper seats).
  # Same workspace the console's host mct seat + /api/mct/status use
  # (FV_STATE_HOME/mct2/repl), so timer- and console-started keepers share
  # one session + status.
  WS="${XDG_CONFIG_HOME:-$HOME/.config}/hugpy-station/mct2/repl"
  mkdir -p "$WS" 2>/dev/null || true
  LAUNCH="exec abstract-claude mct $(printf %q "$WS")"
else
  # claude-code backend (explicit, or mct/abstract-claude absent): its OWN
  # session, never 'keeper-mct'
  [ "$KEEPER_BACKEND" = mct ] && SESS=keeper-claude
  c="$HOME/.local/bin/claude"
  [ -x "$c" ] || command -v claude >/dev/null 2>&1 || { echo "station-keeper: neither abstract-claude nor claude found; keeper not started"; exit 0; }
  LAUNCH="exec /opt/station-keeper/keeper-launch.sh $(printf %q "$STATION")"
fi

# Same global tmux options the console uses, so the TUI renders cleanly on attach.
tmux -L "$SOCK" \
  set -g status off \; set -g prefix None \; set -g prefix2 None \; \
  set -g escape-time 0 \; set -g mouse off \; set -g destroy-unattached off \; \
  set -g window-size latest \; setw -g aggressive-resize on \; \
  new-session -d -s "$SESS" "$LAUNCH"
echo "station-keeper: $SESS session started for $STATION"
