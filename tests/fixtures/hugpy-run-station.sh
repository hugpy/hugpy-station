#!/bin/bash
# hugpy locus: station-dedicated abstract_claude console on 127.0.0.1:9125.
# Backs the hugpy-station "/ac" pane via the station's same-origin proxy
# (server.py ac_proxy, env STATION_CONSOLE_AC). PARITY launcher (2026-09-17,
# abstract-claude 0.1.44): the same env contract as the keeper's
# /srv/vm_mgr/abstract-claude-serve/run-station.sh, hugpy paths and locus.
#
# Base-path contract: ac_proxy STRIPS the /ac prefix and forwards to this
# upstream at ROOT, so AC_UI_BASE="" (serve at /). The stock dist is baked with
# base=/claude, so AC_UI_DIST points at webui-station/, a copy whose asset URLs
# and API base are rewritten /claude/ -> /ac/ (tar'd from the keeper's copy).
#
# AC_ROOT is the STATION's own abstract-claude root (one config.json, one
# roster.json, one rollover.json, one console.sqlite3 per locus).
# Ports 9111/9112 belong to solcatcher; 9123/9124 are the keeper locus; hugpy owns 9125.
set -euo pipefail

STATE=/srv/hugpy/hugpy-station   # canonical; .config/hugpy-station is a symlink to it

export AC_ROOT="$STATE/abstract-claude"
export AC_HOST=127.0.0.1
export AC_PORT=9125
export AC_UI_BASE=""
export AC_UI_DIST=/srv/hugpy/abstract-claude-serve/webui-station

# Exchange capture: AC_SETTINGS_JSON is the settings.json CONTENT (serve
# json.loads() it - abstract_claude/actions.py _session_settings; a PATH is
# silently ignored, which is what the pre-0.1.44 launcher did). The station's A
# template carries env.EXCHANGE_LOCUS/EXCHANGE_INGEST_URL and the Stop hook.
A_TEMPLATE="$STATE/a-settings-template.json"
if [ -s "$A_TEMPLATE" ]; then
  export AC_SETTINGS_JSON="$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),separators=(",",":")))' "$A_TEMPLATE")"
fi
export EXCHANGE_LOCUS="${EXCHANGE_LOCUS:-hugpy}"
export EXCHANGE_INGEST_URL="${EXCHANGE_INGEST_URL:-http://127.0.0.1:7004}"
export AC_SESSION_LABEL="${AC_SESSION_LABEL:-hugpy-serve}"

# Toolserver creds live in a 0600 file -- never inline the token here.
if [ -r "$STATE/toolserver.env" ]; then
  # shellcheck disable=SC1091
  . "$STATE/toolserver.env"
  export TOOLSERVER_URL="${STATION_CONSOLE_TOOLSERVER:-https://toolserver.hugpy.ai}"
  export TOOLSERVER_OPERATOR_TOKEN="${HUGPY_OPERATOR_TOKEN:-}"
  export TOOLSERVER_TOKEN="${HUGPY_OPERATOR_TOKEN:-}"
fi

# Usage/cost persistence: the toolserver's central Postgres (exchanges table,
# locus=$EXCHANGE_LOCUS), the same stream the keeper console writes.
export AC_USAGE_BACKEND="${AC_USAGE_BACKEND:-toolserver}"

# B (queue tidy, relay stages, Local pane) = the station's local model through the
# hugpy gateway, the same provider the keeper console uses: b-model.json if the
# gateway lists it as ready, else the first ready *Instruct* chat model. Unset =
# deterministic join, never a 30 s stall on a model that is not there.
B_GATEWAY="${AC_B_GATEWAY:-https://dev.hugpy.ai/api}"
if [ -n "${HUGPY_API_KEY:-}" ] && [ -z "${AC_LOCAL_LLM_URL:-}" ]; then
  _bm="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("model",""))' "$STATE/b-model.json" 2>/dev/null || true)"
  _ready="$(curl -s -m 8 -H "Authorization: Bearer ${HUGPY_API_KEY}" "${B_GATEWAY%/}/v1/models" 2>/dev/null \
           | python3 -c 'import sys,json;print("\n".join(m.get("id","") for m in json.load(sys.stdin).get("data",[])))' 2>/dev/null || true)"
  _pick=""
  if [ -n "$_bm" ] && printf '%s\n' "$_ready" | grep -qxF "$_bm"; then _pick="$_bm"
  else _pick="$(printf '%s\n' "$_ready" | grep -i 'instruct' | grep -vi 'vl' | head -1 || true)"; fi
  if [ -n "$_pick" ]; then
    export AC_LOCAL_LLM_URL="${B_GATEWAY%/}/v1/chat/completions"
    export AC_LOCAL_LLM_MODEL="$_pick"
    export AC_LOCAL_LLM_KEY="$HUGPY_API_KEY"
    echo "[run-station] B: $AC_LOCAL_LLM_MODEL @ $AC_LOCAL_LLM_URL" >&2
  else
    echo "[run-station] B: no ready chat model at ${B_GATEWAY} - deterministic join" >&2
  fi
fi

exec /srv/hugpy/abstract-claude-serve/venv/bin/abstract-claude serve \
  --host 127.0.0.1 --port 9125
