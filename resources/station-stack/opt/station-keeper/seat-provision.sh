#!/usr/bin/env bash
# seat-provision.sh — make a fresh box able to run the station's model seats,
# in ONE ordered, idempotent, best-effort pass (never fatal):
#
#   1. python3 present?      else: apt install python3
#   2. pip present?          else: ensurepip / apt install python3-pip
#   3. venv active/exists?   else: python3 -m venv  (apt python3-venv if needed)
#   4. pip upgrade needed?   else: pip install -U pip wheel
#   5. seat CLIs             abstract-claude + hugpy-agent (venv) → ~/.local/bin
#                            claude (claude.ai/install.sh)
#   6. durable OAuth token   the SAME login the toolserver uses — resolve a
#                            1-year token and store it once so every seat is
#                            authed with NO interactive /login:
#                              • abstract-claude store (~/.config/claude-auth)
#                              • ~/.claude-oauth.env  (keeper-ensure sources it,
#                                so the claude-code keeper inherits the token)
#
# Run as the seat user (NOT root): bash seat-provision.sh
set -u
log(){ printf '  seat-provision: %s\n' "$*"; }

# distro-aware package install (Debian/Ubuntu apt, Fedora/RHEL dnf, Arch pacman).
# best-effort: uses `sudo -n` so it's a no-op when sudo needs a password (the deb
# depends / cloud-init handle the packages on those installs; this is the fallback).
if command -v apt-get >/dev/null 2>&1; then PKG=apt
elif command -v dnf  >/dev/null 2>&1; then PKG=dnf
elif command -v yum  >/dev/null 2>&1; then PKG=yum
elif command -v pacman >/dev/null 2>&1; then PKG=pacman
else PKG=""; fi
pkg_install(){                     # pass DEBIAN names; mapped per distro
  local names="$*"
  case "$PKG" in
    apt) sudo -n apt-get install -y $names >/dev/null 2>&1 ;;
    dnf|yum)
       # map: python3-venv is bundled in Fedora's python3 (no separate pkg)
       names="${names//python3-venv/python3}"
       sudo -n "$PKG" install -y $names >/dev/null 2>&1 ;;
    pacman)
       names="${names//python3-pip/python-pip}"; names="${names//python3-venv/python}"; names="${names//python3/python}"
       sudo -n pacman -S --noconfirm $names >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}
pkg_refresh(){ case "$PKG" in apt) sudo -n apt-get update >/dev/null 2>&1;; esac; }

VENV="${SEAT_VENV:-$HOME/.local/share/station-seats/venv}"
BIN="$HOME/.local/bin"; mkdir -p "$BIN"

# ── 1. python3 present? ──────────────────────────────────────────────────────
if command -v python3 >/dev/null 2>&1; then
  log "python3 present ($(python3 -V 2>&1))"
else
  log "python3 missing — installing"
  pkg_refresh; pkg_install python3 || { log "python3 install failed (need sudo/pkg mgr)"; exit 0; }
fi

# ── 2. pip present? ──────────────────────────────────────────────────────────
if python3 -m pip --version >/dev/null 2>&1; then
  log "pip present"
else
  log "pip missing — bootstrapping"
  python3 -m ensurepip --user >/dev/null 2>&1 || { pkg_install python3-pip || log "pip bootstrap failed"; }
fi

# ── 3. venv active/exists AND has pip? ───────────────────────────────────────
# A venv is only usable if it has pip — a venv created before python3-venv was
# present is a dir with bin/python but NO pip, and reusing it fails every install.
venv_ok() { [ -x "$1/bin/python" ] && "$1/bin/python" -m pip --version >/dev/null 2>&1; }
if [ -n "${VIRTUAL_ENV:-}" ] && venv_ok "$VIRTUAL_ENV"; then
  VENV="$VIRTUAL_ENV"; log "using active venv: $VENV"
elif venv_ok "$VENV"; then
  log "venv exists (with pip): $VENV"
else
  [ -e "$VENV" ] && { log "removing broken/pip-less venv: $VENV"; rm -rf "$VENV"; }
  log "creating venv: $VENV"
  python3 -m venv "$VENV" 2>/dev/null \
    || { pkg_install python3-venv && python3 -m venv "$VENV"; } \
    || { log "venv creation failed (need python3-venv)"; exit 0; }
  # guarantee pip inside it even if the stdlib venv skipped ensurepip
  "$VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
  venv_ok "$VENV" || { log "venv has no pip after create (need python3-venv/ensurepip)"; exit 0; }
fi
PY="$VENV/bin/python"; PIP="$PY -m pip"

# ── 4. pip upgrade needed? ───────────────────────────────────────────────────
cur="$($PY -m pip --version 2>/dev/null | awk '{print $2}')"
lat="$($PY -m pip index versions pip 2>/dev/null | sed -n 's/.*LATEST:[[:space:]]*//p' | head -1)"
if [ -n "$lat" ] && [ "$cur" != "$lat" ]; then
  log "upgrading pip $cur -> $lat"; $PIP install -q -U pip wheel >/dev/null 2>&1 || true
else
  log "pip up-to-date (${cur:-?})"
fi

# ── 5. seat CLIs ─────────────────────────────────────────────────────────────
log "installing seat packages (abstract-claude, hugpy-agent)"
$PIP install -q -U abstract-claude >/dev/null 2>&1 && log "abstract-claude ok" || log "abstract-claude FAILED"
$PIP install -q -U hugpy-agent    >/dev/null 2>&1 && log "hugpy-agent ok"     || log "hugpy-agent (optional) not installed"
for exe in abstract-claude hugpy-agent; do
  [ -x "$VENV/bin/$exe" ] && ln -sfn "$VENV/bin/$exe" "$BIN/$exe" && log "linked $exe -> $BIN"
done
if command -v claude >/dev/null 2>&1 || [ -x "$BIN/claude" ]; then
  log "claude present"
else
  log "installing claude (claude.ai/install.sh)"
  curl -fsSL https://claude.ai/install.sh 2>/dev/null | bash >/dev/null 2>&1 \
    && log "claude ok" || log "claude install failed (try: npm i -g @anthropic-ai/claude-code)"
fi

# ── 6. durable OAuth token (same login method as the toolserver) ─────────────
# Resolve a 1-year token from, in order: explicit env, the abstract-claude
# store, the fleet's claude-oauth.env, then a mounted fleet secret. Whatever we
# find is written to BOTH stores so mct (abstract-claude) AND the claude-code
# keeper (~/.claude-oauth.env) authenticate with no interactive /login.
resolve_token(){
  local t=""
  t="${CLAUDE_CODE_OAUTH_TOKEN:-}"; [ -n "$t" ] && { printf '%s' "$t"; return; }
  t="${HUGPY_STATION_OAUTH:-}";     [ -n "$t" ] && { printf '%s' "$t"; return; }
  for f in "$HOME/.config/claude-auth/env" "$HOME/.claude-oauth.env" \
           /srv/share/secrets/claude-oauth.env /srv/share/scripts/claude-oauth.env; do
    [ -r "$f" ] || continue
    t="$(sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=//p;s/^CLAUDE_OAUTH=//p' "$f" | head -1 | tr -d '"'"'"'')"
    [ -n "$t" ] && { printf '%s' "$t"; return; }
  done
  # LAN fallback: fetch the fleet's durable token from the station endpoint. The
  # gate is LAN(192.168.0.0/16)+WireGuard only, so this only resolves ON the
  # fleet network — a box outside the 192 gets 401/nothing and stays unauthed.
  local base="${HUGPY_STATION_BASE:-https://dev.hugpy.ai/station}"
  local raw=""
  if command -v curl >/dev/null 2>&1; then
    raw="$(curl -fsSL ${HUGPY_STATION_KEY:+-u installer:$HUGPY_STATION_KEY} "$base/claude-oauth.env" 2>/dev/null)"
  elif command -v wget >/dev/null 2>&1; then
    raw="$(wget -qO- ${HUGPY_STATION_KEY:+--user=installer --password=$HUGPY_STATION_KEY} "$base/claude-oauth.env" 2>/dev/null)"
  fi
  t="$(printf '%s' "$raw" | sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=//p;s/^CLAUDE_OAUTH=//p' | head -1 | tr -d '"'"'"'')"
  [ -n "$t" ] && { printf '%s' "$t"; return; }
}
TOK="$(resolve_token)"
if [ -n "$TOK" ]; then
  # abstract-claude store (mct + `abstract-claude launch` injection)
  [ -x "$BIN/abstract-claude" ] && "$BIN/abstract-claude" oauth-set "$TOK" >/dev/null 2>&1 \
    && log "durable token stored (abstract-claude)" || log "abstract-claude oauth-set skipped"
  # keeper env (claude-code keeper sources this; PATH-independent)
  umask 077; printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$TOK" > "$HOME/.claude-oauth.env"
  log "durable token written to ~/.claude-oauth.env (claude-code keeper)"
  # backend canonical path: server.py _export_fleet_oauth_token reads THIS
  # file (FV_STATE_HOME/claude-oauth-token), NOT the env file above — keep
  # them in lockstep so the station backend never runs on a stale token.
  st="${HUGPY_STATION_STATE:-$HOME/.config/hugpy-station}"; mkdir -p "$st"
  printf '%s' "$TOK" > "$st/claude-oauth-token"
  log "durable token written to $st/claude-oauth-token (station backend)"
else
  log "no durable token found — seats need one. Provide it once by any of:"
  log "  • HUGPY_STATION_OAUTH=<sk-ant-oat…> during install, or"
  log "  • run 'abstract-claude oauth-mint' (browser once) on this box, or"
  log "  • drop CLAUDE_CODE_OAUTH_TOKEN=… in ~/.claude-oauth.env"
fi

log "done — relaunch the station; frontier/local seats should be available + authed."
