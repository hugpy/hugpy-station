#!/bin/bash
# Launch the station-dedicated abstract_claude console on 127.0.0.1:9124.
# This IS the keeper surface (t-serve, 2026-09-16): the station's frontier pane
# renders it through the same-origin /ac proxy (server.py ac_proxy, env
# STATION_CONSOLE_AC=http://127.0.0.1:9124). The tmux keeper seats are LEGACY.
#
# Base-path contract (d411, 2026-09-17): /ac is serve's OWN base, not a proxy
# rewrite. ac_proxy is a dumb pass-through that rewrites NOTHING (the window.fetch
# shim is deleted); it strips the /ac prefix on the way upstream, and AC_UI_BASE=/ac
# makes serve strip it too when it is present (_strip_base is a no-op when absent),
# so the console works framed under /ac or hit directly on :9124. AC_UI_DIST points
# at webui-station/, the dist baked for /ac (stock is base=/claude) — so every
# asset/API URL the page emits already carries /ac and nothing needs injecting.
# A call the console deliberately aims at the ORIGIN ROOT (keeper-live.js
# stationApi -> the station's /api/b/chat) now reaches the STATION, not serve.
#
# AC_ROOT (2026-09-16): UNIFIED with the station's own abstract-claude root, so
# serve and the station's seats share ONE config.json (fresh_session_mode=dir,
# default_model claude-opus-4-8, quota_fallback_model claude-sonnet-5 -- d412
# 2026-09-17: the fallback was set equal to the default, which made it a no-op)
# and one
# sessions store. Safe because AC_ROOT only holds config.json + template/ —
# sessions (~/.claude-sessions), the usage DB (~/claude_usage.db) and keepalive
# (~/.claude-keepalive) are $HOME-scoped and were ALREADY shared.
# Fallback root kept at ./root-station (+ .bak-20260916) if this must be undone.
set -euo pipefail
export AC_ROOT=/srv/vm_mgr/hugpy-station/abstract-claude
export AC_HOST=127.0.0.1
export AC_PORT=9124
export AC_UI_BASE=/ac
export AC_UI_DIST=/srv/vm_mgr/abstract-claude-serve/webui-station

# Exchange capture: sessions launched by serve must land in the exchanges DB the
# same way tmux seats do. AC_SETTINGS_JSON is the settings.json CONTENT (serve
# json.loads() it — abstract_claude/actions.py _session_settings), so read the
# station's A template at start; it carries env.EXCHANGE_LOCUS/EXCHANGE_INGEST_URL
# and the Stop hook -> /srv/vm_mgr/bin/exchange_capture_hook.py.
A_TEMPLATE=/srv/vm_mgr/hugpy-station/a-settings-template.json
if [ -s "$A_TEMPLATE" ]; then
  export AC_SETTINGS_JSON="$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),separators=(",",":")))' "$A_TEMPLATE")"
fi
export EXCHANGE_LOCUS="${EXCHANGE_LOCUS:-keeper}"
export EXCHANGE_INGEST_URL="${EXCHANGE_INGEST_URL:-http://127.0.0.1:7004}"
export TOOLSERVER_URL="${TOOLSERVER_URL:-${STATION_CONSOLE_TOOLSERVER:-https://toolserver.hugpy.ai}}"
[ -n "${TOOLSERVER_TOKEN:-}" ] || export TOOLSERVER_TOKEN="${STATION_CONSOLE_TOOLSERVER_TOKEN:-}"
export AC_SESSION_LABEL="${AC_SESSION_LABEL:-keeper-serve}"

# Usage/cost persistence (operator ruling 2026-09-16, docs/B-REDUCER-AND-SESSION-LEDGER.md
# Addendum): abstract-claude persists in the toolserver's central Postgres
# (exchanges table, locus=$EXCHANGE_LOCUS), not the per-machine ~/claude_usage.db.
# usage_db.record() -> exchange/record (merges with the Stop hook's row by turn
# uuid); the sidebar/metrics read exchange/sessions|session|usage_report.
# AC_USAGE_LOG_SQLITE=1 would keep the SQLite row in parallel while validating.
export AC_USAGE_BACKEND="${AC_USAGE_BACKEND:-toolserver}"

# t346 (2026-09-16): 'tidy' of prompts queued mid-turn (server._compile_prompt,
# POST /api/session/queue action=compile and /api/compile) uses B = the station's
# local model through the hugpy gateway (HUGPY_URL/HUGPY_API_KEY from station.env,
# the same provider the station's own B chat uses). Model: the station's
# b-model.json choice if the gateway lists it as ready, else the first ready
# *Instruct* chat model (Qwen3-Coder-Next is often not loadable, t101). Unset =
# deterministic join, never a 30 s stall on a model that is not there.
if [ -n "${HUGPY_URL:-}" ] && [ -z "${AC_LOCAL_LLM_URL:-}" ]; then
  _bm="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("model",""))' /srv/vm_mgr/hugpy-station/b-model.json 2>/dev/null || true)"
  _ready="$(curl -s -m 8 -H "Authorization: Bearer ${HUGPY_API_KEY:-}" "${HUGPY_URL%/}/v1/models" 2>/dev/null \
           | python3 -c 'import sys,json;print("\n".join(m.get("id","") for m in json.load(sys.stdin).get("data",[])))' 2>/dev/null || true)"
  _pick=""
  if [ -n "$_bm" ] && printf '%s\n' "$_ready" | grep -qxF "$_bm"; then _pick="$_bm"
  else _pick="$(printf '%s\n' "$_ready" | grep -i 'instruct' | grep -vi 'vl' | head -1)"; fi
  if [ -n "$_pick" ]; then
    export AC_LOCAL_LLM_URL="${HUGPY_URL%/}/v1/chat/completions"
    export AC_LOCAL_LLM_MODEL="$_pick"
    [ -n "${HUGPY_API_KEY:-}" ] && export AC_LOCAL_LLM_KEY="$HUGPY_API_KEY"
    echo "[run-station] queue tidy via B: $AC_LOCAL_LLM_MODEL @ $AC_LOCAL_LLM_URL" >&2
  else
    echo "[run-station] queue tidy: no ready chat model at ${HUGPY_URL} — deterministic join" >&2
  fi
fi

# ── the console dist is DERIVED, never hand-authored (2026-09-19, anti-drift) ──
# AC_UI_DIST above points at a tree baked from the INSTALLED abstract_claude
# package with "/claude/" rewritten to "/ac/". Before 2026-09-19 that bake ran
# only when AC_UI_DIST was UNSET (abstract-claude-serve-run, old line 84) — and
# this script always sets it — so it never re-ran and webui-station/ silently
# became hand-authored state present in no build artifact. Re-derive it on EVERY
# start so a hand edit to it DOES NOT SURVIVE A RESTART; the only place the
# console UI may be edited is the package source (ac-wt/c144
# src/abstract_claude/webui), which flows c144 -> wheel -> .deb -> installed
# package -> here.
#
# station-bake-webui archives the tree before replacing it (nothing is ever
# deleted) and honours the emergency hatch:
#     AC_UI_NO_BAKE=1          or    touch "$AC_UI_DIST/.no-bake"
# Either keeps a live hotfix alive — and BOTH are reported by
# /srv/vm_mgr/bin/station-drift-check.sh, which build-release.sh runs first and
# which refuses to cut a release while the hatch is open. Hotfix freely; you
# just cannot SHIP with one outstanding.
#
# Search order: the installed .deb payload first (station-bake-webui ships from
# 1.0.110), then the station-app source tree so this works before that release.
for _bake in /srv/vm_mgr/hugpy-station/app/current/resources/bin/station-bake-webui \
             /srv/hugpy/src/station-app/resources/bin/station-bake-webui; do
  [ -x "$_bake" ] || continue
  ABSTRACT_CLAUDE_BIN=/srv/vm_mgr/abstract-claude-serve/venv/bin/abstract-claude \
    "$_bake" "$AC_UI_DIST" \
    || echo "[run-station] webui bake failed (non-fatal) — serving whatever is in $AC_UI_DIST" >&2
  break
done

exec /srv/vm_mgr/abstract-claude-serve/venv/bin/abstract-claude serve --host 127.0.0.1 --port 9124
