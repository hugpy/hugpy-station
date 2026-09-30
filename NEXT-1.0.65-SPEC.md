# hugpy-station 1.0.65 — operator spec (2026-09-02)

Operator's words, itemised. Each item is a requirement, not a suggestion.

1. **Backend switch = buttons, not a dropdown.** `mct` and `claude-code` (the frontier
   backends) are two buttons side by side; the chosen / currently displayed one is
   highlighted. No dropdown.

2. **"host" button (top bar)** → takes the user to the HOST's frontier session AND sets the
   locus dropdown to the host.

3. **The button next to it** shows the PREVIOUSLY chosen locus → clicking it takes the user to
   THAT locus's frontier session AND sets the locus dropdown to it.

4. **The locus dropdown means "this console is now that locus's console."** Choosing a locus
   re-grounds every surface (frontier, local, shell, files, steward, sessions) in that locus.

5. **Settings → model → "custom"**: today it does nothing and reverts to the previous item.
   It must accept a user-typed model id; validity is verified when the user moves to another
   selection or saves the setting (reject with a message, keep the field editable).

6. **Frontier handoff = a per-session START/INIT prompt** that rides along with the frontier
   directive at seat launch. Once it has been handed off (consumed by a launched seat) the
   field is cleared — it must not stay populated.

7. **Access tab**: the frontier options are mostly greyed out — make them live (or remove
   the dead ones); nothing greyed without a reason shown.

8. **Session tab → frontier keeper model selections must literally change the model** as they
   are selected (the running seat picks the new model up: relaunch/`--model` on next start
   and, where the backend supports it, live).

Ground truth for terms: NOMENCLATURE.md (locus / seat / backend / model). Ship as 1.0.65 via
the shelf pipeline (`build-release.sh` → `release.sh promote`).

## Implementation notes (2026-09-02, commits 4ff14bf + beacb29)
1. `fleetview-term.js`: `#fv-backend` is a button group; `setBackend()` + real `window.__fvBackend`.
2/3. top bar: ⌂ host also `setActive(null)`; new `↩ <prev>` button from `__fvVm.prev()` (event `fv-vm`).
4. "console of" label + retitled dropdown; grounding unchanged (every surface already re-grounds).
5. `ModelDropdown`: custom = text field kept, verified on blur/Enter (`MODEL_ID` regex), ↺ list.
6. `frontier-handoff.md` = session init prompt: folded into `_claude_seat_system_prompt` and
   `_compose_guidance`; consumed (`rm` in the claude prelude / `_frontier_handoff_consume` after the
   mct render). UI renamed "🚀 session init prompt", empties once consumed.
7. `FrontierFsSwitch`: greyed state names its reason + ↻ retry. Root cause on ae was the stale
   `STATION_CONSOLE_MODEL_VM=hugpy` pin (removed from the site drop-in); `console-api` hugpy URL
   default → the host's central (`HUGPY_URL`/`HUGPY_BASE` also accepted from toolserver.env).
8. `/api/frontier/models` POST types `/model <id>` into the live tmux seat (paced: Escape, C-u,
   type, settle, Enter, confirm) — verified on ae's keeper-claude ("Set model to …").

## 1.0.66 follow-up (operator: "the model dropdown still doesn't allow custom")
- Cause: the custom path used `window.prompt`, a no-op in Electron. Now an inline field (apply /
  cancel / Enter / Esc) on both the frontier pickers and the B picker; the ⚙ settings picker already
  had the inline field from 1.0.65.
- The model list is LIVE: toolserver `claude/models` = `GET https://api.anthropic.com/v1/models`
  with the fleet OAuth token (Bearer + `anthropic-beta: oauth-2025-04-20`), cached 10 min; the
  station's `/api/frontier/models` serves it (cached 10 min per process) → one call per page load;
  static fallback when the toolserver is down (`choices_source` says which).

## 1.0.67 (operator: "a cache countdown in the steward window")
- `GET /api/frontier/cache?vm=` — parses the seat's transcript (`<CLAUDE_CONFIG_DIR>/projects/*/*.jsonl`,
  newest): last assistant row's `usage` gives cache_read + cache_creation (= cached prefix) and the TTL
  split (`cache_creation.ephemeral_5m/1h_input_tokens`; Claude Code used the 1 h TTL on ae's keeper);
  busy = last row is a pending tool_use or an unanswered user turn. Exact, not estimated.
- steward → session: "⏳ prompt cache" card — state busy/warm/lapsed, mm:ss countdown (client tick),
  cached tokens, last request age, requests/hour, cold-rebuild vs warm-turn $ estimate.
- Directive: "## Session init prompt (handoff)" replaces the old handoff-letter paragraph; the seat is
  told to write its successor's init prompt when a burst of work ends.
