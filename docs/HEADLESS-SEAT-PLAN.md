# Headless seat rewrite — kill tmux, drive Claude Code over the API

## Goal
Replace the interactive tmux seat + MCT pointer-exchange + broker send-keys/transcript-scrape
with a **headless Claude Code agent** driven over the API, rendered in a fluid `/media`-style
web UI. Keep the existing ledger (`turns`/`objects`/`events`/`arbitration`) and the B
coalescing. Delete tmux, `send-keys`, probe/transcript-scrape, busy-gating.

## PROVEN foundation (verified 2026-09-16, do not re-litigate)
- `claude -p "<prompt>" --model claude-opus-4-8 --output-format json < /dev/null` works headless
  and returns structured JSON (`result`, `session_id`, `usage`, `total_cost_usd`, `is_error`).
- **Auth**: export `CLAUDE_CODE_OAUTH_TOKEN` (the managed token). A fresh `claude -p` WITHOUT it
  fails `OAuth session expired`; WITH it, returns `HEADLESS_OK`. The live seat carries it in env.
  TODO for durability: source it from the managed origin (abstract-claude / toolserver
  `claude_oauth_*`), NOT by scraping a pid's /proc/environ.
- **Tools work headless**: `--allowedTools "Bash"` ran the agent loop and executed the command.
- **Session continuity**: `--resume <session_id>` / `--session-id <uuid>` carries context across turns.
- Ledger: `/srv/vm_mgr/hugpy-station/mct/renderer/ledger.sqlite3`; `Store` in
  `resources/backend/mct_gateway.py` already does `submit`/`publish`/events.

## Architecture
- **HeadlessAdapter** (new, replaces `NativeAdapter` in `mct_gateway.py`): `run(prompt, session_id)`
  spawns `claude -p --output-format stream-json --resume <sid> --allowedTools <set>` with
  `CLAUDE_CODE_OAUTH_TOKEN` in env, streams assistant/tool events, returns `(response_text, new_sid)`.
- **Broker** (`mct_gateway.py` tick + `mct_http.py` follow): on a committed/coalesced turn, call
  `adapter.run`, stream events into the ledger as they arrive, then `_publish` the final response.
  No probe, no busy-gating, no tmux. Arbitration/coalesce stays as-is.
- **Web UI**: a fluid `/media`-style page that renders the ledger conversation and streams new
  events (SSE/websocket over the ledger `events`), replacing the xterm/tmux-pane attach in
  `fleetview-term.js`.
- **Session store**: persist the seat's `session_id` (e.g. in `seats` or a small file) for continuity.

## Phased + flag-gated (nothing breaks mid-flight)
- Flag `SEAT_TRANSPORT = headless | tmux` (default `tmux` until headless verified).
- **P1** HeadlessAdapter + broker wiring behind the flag. VERIFY: a submitted turn drives a headless
  agent that can run a tool, and the streamed response lands in the ledger + renders. No tmux touched.
- **P2** Fluid web UI streaming from the ledger (the `/media`-style surface).
- **P3** Default `SEAT_TRANSPORT=headless`; remove tmux/`NativeAdapter`/send-keys/probe. Ship in the .deb.

## Verification (definition of done)
Operator types in the web UI → headless agent runs (uses tools when needed) → response streams
back into the UI → `pgrep tmux` shows nothing involved in the exchange.

## Safety
- The station-app release is UNCOMMITTED (HEAD 1.0.85). Do NOT git reset/checkout/clean.
- Build alongside; keep tmux working until headless is proven. Don't touch the live deployed copy
  until cutover. Verify against the running keeper station (:8898, `--user hugpy-station-web`).
