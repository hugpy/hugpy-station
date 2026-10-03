# hugpy Station — changelog

Versions before 1.0.91 are recorded in git history and in README.md; this file
starts at the release that introduced it.

## 1.0.156 — 2026-10-02

- Seat pins: hugpy-agent 0.1.103 + abstract-toolserver 0.0.54 — the versions that read ~/.hugpy/.env, so seats follow ⚙ Settings.
- firstrun: HUGPY_BASE stored as a bare host (trailing /api stripped); HUGPY_API_KEY copied into ~/.hugpy/.env when absent (agent.env keeps its copy).

* 

## 1.0.155 — 2026-10-02

- OSC 52 clipboard: every Station terminal accepts OSC 52 copies (write-only; `?` reads refused, 1 MB cap) and hands them to the Electron clipboard; seat tmux runs `set-clipboard on` + clipboard terminal-feature (applies on seat restart).
- Electron right-click: context menu (copy/paste/select all) on the SPA and the /ac console view; terminal keeps copy-on-select.
- copyText goes bridge-first (window.stationClipboard) before navigator.clipboard / execCommand.
- firstrun: creates ~/.hugpy/.env and migrates HUGPY_BASE out of ~/.config/hugpy-agent/agent.env (so Settings is no longer overridden by the unit EnvironmentFile).

* 

## 1.0.154 — 2026-10-02

* ⚙ settings (File → Settings…, Ctrl+, or the ⚙ toolbar button): edits the shared
  `~/.hugpy/.env` ($HUGPY_HOME) every hugpy package reads — fleet URL, fleet API key,
  fleet fallback switch, serve URL/token, toolserver URL/token. A value exported in the
  shell or unit overrides the file and is shown locked as `env`; secrets are write-only;
  changing a URL asks to confirm old → new host. `/api/settings` (POST needs homebase +
  provision capability + CSRF); the audit records URL changes as old -> new.
* One fleet pointer: `HUGPY_BASE` resolves env → `~/.hugpy/.env` → `http://127.0.0.1:7002`
  (abstract_toolserver.hugpy_home). Station's own `dev.hugpy.ai` / `HUGPY_URL` defaults
  are gone (hugpy provider, transcribe proxy, B model picker).
* URLs that carry credentials must be https, or plain http only to loopback / LAN.
* Seats never silently leave this machine's central: once it has answered, a down
  central stays the target unless `HUGPY_FLEET_FALLBACK=1` and an operator-set Fleet URL;
  the move is reported (`front.fallback`). A client box with no local central uses the
  Fleet URL as its primary target.
* ledger hook: PreCompact safety net (files a toolserver handoff when compaction fires
  before the rollover monitor) — was live-only since 2026-10-01, now shipped.
* abstract-claude pin 0.1.96 → 0.1.97.
* Drift gate rows A/B are direction-aware: source/pin AHEAD of the installed version's
  shipped commit passes; a live copy differing from what shipped still fails.

## 1.0.153 — 2026-10-02

* Operator 2026-10-02 (screenshot: stacked HUGPY AGENT headers when scrolling up): the
  serve-tui seat keeps the ALTERNATE screen (`tmux setw alternate-screen on` in its own
  window — the socket-wide `off` stays for claude-code/opencode, which have no scrollback
  of their own), so TUI redraws no longer pile into tmux history; and the client no
  longer hijacks the wheel into tmux copy-mode for that seat — xterm turns it into
  mouse reports and the TUI scrolls its own transcript.
* While a ⌂ shell is on stage the frontier picker (`tui` · `tmux · <provider> ▾`) stays
  in the bar; clicking either leaves the shell and mounts that seat.

## 1.0.152 — 2026-10-02

* 🐞 bug scan (log_findings.py): a bare `429` is no longer a rate limit — serve's
  `[rollover] sweep #429:` lines filed two HIGH rate_limit_429 findings (r3746/r3747,
  B proposal p2232). 429 counts only as an HTTP status (after HTTP[/1.x], `status=`/
  `code=`/`error=`, inside an access-log quote, or before "Too Many Requests"); the
  `[rollover] sweep #N` bookkeeping lines are on the IGNORE list. Wording matches
  (Too Many Requests, rate limit, RateLimitError) unchanged. Test added.

## 1.0.151 — 2026-10-02

* Operator 2026-10-02: MULTIPLE SHELLS on a locus. The ⌂ shell button is a split:
  ⌂ goes to the last used shell (back to frontier when already on one), ＋ opens the
  next shell (`shell#N`, its own PTY — ws_hostterm `?surface=shell&inst=N`, which the
  backend already persisted per instance), × closes the extra shell you are on (its
  PTY only; the tmux shell in the locus lives on). `__fvSurface.addShell/closeShell`
  now exist for the SPA.

## 1.0.150 — 2026-10-02

* Operator 2026-10-02: the frontier picker's `serve` button is labelled **`tui`** — the serve-sessions seat IS the hugpy-agent TUI over this locus's abstract-claude serve (backend key `serve-tui` unchanged).

## 1.0.149 — 2026-10-02

* Operator 2026-10-02: the ⚠ loop/finding strip no longer paints over the terminal.
  Its rows (crash/retry loops, log findings, station holds — GET /api/loops) live in
  the steward tab's 📡 feed as a new **⚠ alerts** subtab with an `all | critical`
  filter. critical = an active non-inert loop, a high-severity finding, or a station
  skip; critical rows read red (left bar + tint) there AND in the 🐞 review. The strip
  is off by default (localStorage `fv-loop-strip=1` restores it for a browser).
* Frontier picker: the `tmux` button is a SPLIT button — the left half goes to the
  last chosen terminal (`tmux · Claude`), the right ▾ opens the provider menu
  (Claude · ChatGPT · Hugpy), replacing the separate provider dropdown.

## 1.0.148 — 2026-10-02

* Operator 2026-10-02: the SERVE SESSIONS (keeper/chat/worker/local of a locus's
  abstract-claude serve) are driven from the hugpy-agent TUI, replacing the /ac/ web
  console pane. New frontier Terminal backend `serve-tui` = `hugpy-agent tui --serve
  <serve url as seen from the locus>` in tmux session `keeper-serve-tui`
  (TERM_SURFACES / BACKEND_TMUX_SESSION / TMUX_SEAT_LABELS "Serve sessions · Hugpy
  Agent TUI"). The picker's "serve" button mounts that seat (disabled when the locus
  has no answering serve or no hugpy-agent); "tmux" returns to the provider seat. The
  /ac/ proxy route and native-view plumbing stay for scripts but no pane frames them;
  the SPA's VM-pane paneSrc() no longer special-cases "/ac/".
* Pins: abstract-claude 0.1.96, abstract-serve-core 0.1.19 (cross-locus
  session_message `<locus>:keeper`), abstract-gpt 0.1.17, hugpy-agent 0.1.100 (TUI:
  locus tabs/picker, mouse session switch, typing-always-types), abstract-toolserver
  0.0.47 (comms_ping live push to the target locus's registered serve_url). The
  previous pins (hugpy-agent 0.1.85) predate `hugpy-agent tui` and were DOWNGRADING
  fleet venvs on every serve start.
* Carried from the post-1.0.147 tree: 📨 nudges delivered into the locus's own
  keeper-claude seat (_nudge_seat_on via prompt_send.deliver_seat); LXD discovery
  failures back off 10 min and log once.

## 1.0.147 — 2026-10-01

* Pin abstract-serve-core 0.1.16: operator interrupt = stop (no operator:interrupt
  hold, no retry); the prompt queue drains at turn end.
* Merge st-keeper-card (steward keeper card = the locus's serve Keeper role, t4262)
  and st-switches (disabling-switch audit, station side, t4260/t4250) — see the
  "unreleased" sections below.
* serve-run provisions the venv before baking the UI (F3.17).

## 1.0.146 — 2026-10-01

* Pin abstract-serve-core 0.1.15 (queue-first collator, context bounds for every
  backend, standing-session defaults; t4246 t4252).
* The seat handoff lives in the toolserver; /resume = pull (st-handoff, below).

## unreleased (st-handoff) — 2026-10-01 — the seat handoff lives in the toolserver; /resume = pull

* Operator ruling 2026-10-01: "the toolserver should house the handoffs … /resume should
  allow for its pull. The file-pointer system is ambiguous and bound to fail." The
  `handoffs` row (toolserver ≥ 0.0.41: `handoff_request locus text [task fork]`) is the
  ONLY place a handoff lives. `<state>/frontier-handoff.md` is RETIRED: no launch reads
  it, nothing consumes/deletes it; a leftover file is renamed `.retired-<stamp>` at
  station start and its text filed as a row when none is open (audit line, never silent).
* Launch: the tmux seat directive's "Session init prompt (handoff)" layer is the
  toolserver's one-paragraph POINTER (`handoff/station → layer`, fetched right before the
  launch; an unreachable toolserver yields an explicit "pull it yourself" line). The BODY is
  injected ONCE by the seat's SessionStart hook (`hooks/ledger_hook.py` → `handoff/pull`
  with the seat's own session id; the row becomes `consumed` by it; serve per-turn resumes
  never re-inject). HANDOFF_ID in a spawned seat's env pins the exact row.
* `/api/handoff/spawn` is VERIFIED: a seat must still be alive 2.5 s after launch (a dead
  pane is reported with its last lines and killed — no ghost "spun" seats, the h27–h29
  failure); a fresh seat gets `--session-id` (returned as `session_id`) and
  `EXCHANGE_LOCUS`/`HANDOFF_ID`. Features add `verify`, `session-id`.
* Steward tab "session init prompt": GET shows the exact init prompt the next seat
  receives (`handoff/station → init_prompt`, same text as `handoff_pull`), POST files a
  row (`by=operator-steward`, no spawn), empty text abandons the open row. Remote loci use
  the same central rows (`_remote_handoff` and the `handoff` cfg file are gone).
* Idle relaunch files the rolling init prompt as a row instead of writing the file.
## unreleased (st-keeper-card) — 2026-10-01 — steward keeper card = the serve Keeper

* Steward › sessions: the Keeper card is the locus's serve Keeper session (model,
  pending model, serve context gauge); model picker lists the full catalog; tmux seat
  demoted to an emergency sub-card (t4262). New `GET/POST /api/locus/keeper?vm=`
  (roster keeper role + `/api/console/sessions` gauge + `/api/console/models`
  catalog; POST `{action:"set_model", model, backend?}` forwards to that serve's
  roster, `set_provider` first when the backend changes; upstream errors are
  passed through as `{ok:false, error}`). The old "context 4,443,744 / 1,000,000"
  number was the cumulative relayed total — it is now a separate "relayed" line.
## unreleased (st-switches) — 2026-10-01 — the disabling-switch audit, station side (t4260, t4250)

Operator ruling d4254: a core guarantee (prompts go out at idle, pings and digests
are delivered, standing sessions can act, the keeper is reachable) never has an off
switch, a silent hold, an attempt cap or an unbounded defer; the only interruptions
are explicit operator actions that are visible, owned and expiring. Station side of
the 2026-10-01 audit (`AUDIT.md` §3):

* **Holds are visible** (new `station_holds.py`): every coalesce / skip / wait /
  inert code default the station performs is a row on the ⚠ strip (`GET /api/loops`
  → `holds`, rendered by fleetview-term.js) with a count — `station:switch`,
  `station:hold`, `station:skip`, `station:pending`. Persisted in
  `<state>/station-holds.json`.
* 1 Digest: a BUSY keeper no longer "defers" the digest tick after tick — it is
  submitted through the comms-nudge path (`_nudge_serve` → `/api/console/chat`) and
  QUEUES behind the turn; state `queued` carries the queue message id
  (`queue_message_ids`, `queued_behind`). `deferred` is gone; an unreadable keeper is
  `NOT delivered` (todo_digest.decide: `unreachable`, no `defer`).
* 2 Reminders: `_REMIND_CAP` (3 attempts, then withhold) is removed; only the
  `_REMIND_MIN_GAP` coalesce spaces reminders; the comment reads "delivery #N, no cap".
* 3 `STATION_REMIND_MITIGATOR` (and every other inert-able default) is exposed with
  its live value in `/api/about` → `switches` and `/api/loops` → `config.switches`,
  and shows on the strip as `⚙ switch` when off.
* 4 Nudge drain: a "wait" behind a busy turn is bounded (`keeper_nudge.WAIT_MAX_S`,
  15 min) → `comms-nudge-requeue` audit line + `count:comms-nudge-requeue` strip
  counter; while waiting, a `hold:comms-nudge-inflight` row shows it. A serve-core
  0.1.15 hold (`{by, until}`) is waited out, never released by the station.
* 5 "board status unknown" (toolserver down) → `comms-nudge-skip` audit (once per
  reason) + a `skip:comms-nudge@<locus>` strip row with the pending count; nothing
  is dropped. 6 Every `comms-nudge-skip` (no serve, serve refused, tmux off) mirrors
  reason + pending count into the strip, cleared on the next successful send; the
  5-min batch window shows "N pending, next in Xs".
* 7 Hand-off dedupe (`handoff-dispatch.json`) has a 6 h age floor
  (`STATION_HANDOFF_REQUEUE_SECS`): an identical open set is re-queued after it
  (`handoff-requeue` audit, "re-queued" note in the batch).
* 8 ✍ prompt: a serve the station cannot reach / resolve no longer ends in `failed`
  — the prompt is RE-PENDED to the locus's `prompt-pending.json` with the reason
  (state `pending`, `pending_id`), retried every `STATION_PROMPT_PENDING_TICK`
  (60 s) until a serve session takes it; `GET|POST /api/prompt/pending?vm=` lists /
  retries; a `pending:prompt@<locus>` strip row counts them. Bug reports never end in
  `failed` either (state `pending` + reason, retried every pass).
* 9 Bug-report outbox: a report queued behind a busy worker turn is waited for at
  most `bug_route.INFLIGHT_MAX_S` (15 min, `STATION_BUGREPORT_INFLIGHT_MAX`), then
  pulled out (`inflight_cancel`) and re-sent coalesced (`bug-report-requeue` audit +
  strip counter).
* 10 `frontier-keeper.json` and `frontier-delegate.json` record `by`, `at`,
  `expires` on every write (`POST {expires_s|expires}`); GET returns them; a disable
  / delegate-ON past its expiry reads as lifted. A disabled frontier keeper is a
  `⚙ switch` strip row.
* 11 Every locus has a keeper that knows its locus (t4250): provisioning
  (`/api/locus/serve/provision`) and the new `POST /api/locus/serve/standing?vm=`
  ship `standing_permission_mode: "bypassPermissions"`, `standing_cwd: <locus $HOME>`
  and `permission_mode_by_label` for keeper/chat/worker into the locus's
  `AC_ROOT/config.json` (missing keys only — idempotent, operator values kept;
  `locus_exec.standing_config`), record the locus facts (home, gate, user units,
  trees, serve, registry goal / tree rules from `loci/pointers`) in its
  `directives/station.json` → `nuances`, and render the keeper directive with a
  **LOCUS NUANCES** block (`directive-templates/nuances-keeper.md`).
* 12 Serve-core 0.1.15: the station sends queue action `release` (`/api/prompt/status`
  POST, the nudge kick) and tolerates both the 0.1.14 and 0.1.15 response shapes
  (`keeper_nudge.queue_action_ok`); a 0.1.14 serve (HTTP 400 "Unknown queue action")
  gets `retry` as the fallback. `/api/prompt/status` still accepts `retry` from older
  UIs and sends it as `release`.
* Tests: `test_switches_146.py` (one focused test per change; the standing-config
  script runs for real against a temp state dir) + the touched suites updated.

## 1.0.145 — 2026-10-01 — mirrored board notes are no longer clipped

* Board: the central→local mirror/update path clipped notes at 2000 chars and comments
  at 2000, which cut operator RUN scripts off before their closing fence on the
  operator tab (the central text and the materialized scripts were complete). Notes
  now mirror up to 20k and comments up to 8k.

## 1.0.144 — 2026-10-01 — every locus gets a serve chat and a keeper; no silent suppression; the board as a static reference

* Serve chat + keeper on EVERY locus: `station-serve-provision` sets up a locus's own
  serve as its own user from shipped resources (`POST /api/locus/serve/provision`,
  "＋ serve" button); each locus's own tmux seats are listed (`frontier.seats`);
  LXD guests' serves are reached through a relay; `prompt_send` and nudges accept a
  locus's raw keeper session id; the B pane, delegate and fs switches act on the
  selected locus.
* Comms pings are delivered into every managed locus's keeper serve session
  (claimed, observed as `sender->receiver`, marked delivered); a delegation to
  another locus is no longer dropped as self-origin.
* No silent suppression (d4187): the ping gate no longer withholds — every request
  ping nudges; the issue gate only annotates; inert silencing and B proposals are off
  by default (`STATION_NOTIFY_INERT_SILENCES`, `STATION_B_PROPOSE`); the old B
  reminder-verdict cycle is off (`STATION_REMIND_MITIGATOR`, board t4178).
* Bug reports go only to the locus's WORKER serve session as `finding` board items
  with delivery state (d4188); 1.0.143-era finding pings are forwarded and closed.
* Reminders: a deterministic open-todos DELTA digest into each keeper serve session
  (30 min cadence when idle, ≤1.5k chars, never to tmux, no attempt cap, skips
  recorded; findings excluded) — `todo_digest.py`, one `[digest]` row per locus.
* Board as a static reference: ⚑ operator / ⚖ proposal / 🔖 bookmark / 🧭 direction
  items render in full via `static/board-md.js` following `docs/BOARD-ITEM-FORMAT.md`
  (fenced blocks with language label + ⧉ copy, copy-all, ⚠ unformatted-command /
  placeholder badges, prose folds while code stays visible).
* Operator items carry a RUN script (one self-contained bash block, DO then VERIFY;
  `ROLLBACK RUN:` optional), materialized to `<state>/operator-scripts/<id>.sh`
  (+ `.rollback.sh`, `.done` on close) with a one-liner in the item; each run records
  stamped output + a JSON sidecar under `results/<id>/` and is attached once to the
  item as a RESULT comment with `result`/`flag` annotations; the item shows the
  one-liner (⧉ copy), ▶ copy script, and the ✔/✖ result card. B draft-run endpoint.
* Directives: a Station Toolkit section (what exists for delegation and the exact
  call shape; tool names verified against the live toolserver) + a "Board items"
  section; keeper no longer receives bug reports; B never gates pings.
* Observability v2 (OFF by default): actions linked to requests by event-listener
  stacks (labelled "inferred" tier for scheduler tasks; timers/page-load never
  attributed); one-view chain action → stack → request → handler/traceback →
  response → consumer; per-request trace headers; filters, trace-next-N, span level
  and module filters; inference off/summary/explain (explicit per trace); saved
  traces + provenance ingest; settings-at-open fix. Server half specified for hugpy.
* Fixes: console drawer no longer truncates notes at 2000 chars; B fix proposals use
  the proposal shape; paste box on shell/local surfaces pastes into its own pane.
* Pins: abstract-serve-core 0.1.14 (relay to a busy console session uses its own
  drained queue; truncation marked; queue-full is an error; speech chips; per-session
  directives).

## 1.0.143 — 2026-10-01 — shell back, copy/paste, a prompt bar that delivers, speech, observability, per-session directives

* ⌂ shell returns to the terminal bar: a persistent terminal on the selected locus as its
  own user (vm_mgr@ae on the host) via the 1.0.141 locus transport; unreachable loci
  disable it with the reason.
* Terminal copy: right-click on a selection copies at once (no Shift-juggling); selections
  survive TUI redraws; Option+drag selects on macOS. Still no copy-on-select (2026-09-15).
* Terminal paste: Ctrl+V / Ctrl+Shift+V paste directly (native paste → Electron clipboard
  → Clipboard API → paste box); bracketed paste via xterm; the redundant right-click
  "Paste" menu is gone. Electron exposes stationClipboard over IPC. Fix: the paste box
  on shell/local surfaces pastes into its own pane, not the keeper seat.
* ✍ prompt bar delivers directly: POST /api/prompt/send to the locus's serve session
  (selected / keeper head) or its tmux seat, with delivered / queued / NOT-delivered
  state and reply tracking (/api/prompt/status); the prompt-inbox file-pointer path
  survives only as a labelled fallback.
* Clipboard images (image/*) attach in the prompt bar; copied paths stay text; 📋 image.
* Opt-in speech (default OFF): 🎙 dictation (Web Speech, else station Whisper) and
  🔊 read serve replies aloud — in the prompt bar and (serve-core 0.1.13) the /ac console.
* Observability phase 1 (OFF by default; Help menu or HUGPY_STATION_OBSERVABILITY=1):
  in-app trace browser joining action → initiator stack → X-Hugpy-Trace-Id request →
  flask handler span → output → response; filters + inference level (off/summary).
* Directives are distinct per locus × standing session (keeper / chat / worker / local)
  plus tmux, generated by directives.py from shipped templates, live locus facts and
  operator overlays; serve sessions receive theirs every turn (AC_SESSION_DIRECTIVES_DIR).
  tmux seats are framed as fallback / inference arm; the steward editor gains a session
  picker; delegation gains a "serve" switch.
* Pins: abstract-serve-core 0.1.13.

## 1.0.142 — 2026-10-01 — the /ac console is baked from the package that serves it

* station-bake-webui: the console dist is baked from abstract_serve_core/webui
  (abstract-serve-core SERVES the console; it was baked from abstract_claude's
  stale copy, so serve UI fixes never reached the browser). abstract_serve and
  abstract_claude remain fallbacks for older installs.
* REQUIREMENTS pins: abstract-serve-core 0.1.12 (import name renamed
  abstract_serve -> abstract_serve_core, transitional alias kept; serve fixes:
  history order, tool calls after reload, mid-turn model change, live
  processing status, per-unit timestamps, responding model), abstract-claude
  0.1.71, abstract-gpt 0.1.15, hugpy-agent 0.1.85.
* unshipped-artifacts: re-acked ac-loci.json (stale hugpy :9125 pin cleared)
  and the 7006 unit (as edited 2026-09-30).

## 1.0.141 — 2026-10-01 — any station views any locus exactly as that locus's own host does

Principle (operator): every station is perfect as host of its own locus; only the
REMOTE path broke. There is now ONE code path (`resources/backend/locus_exec.py`)
that runs identically on this host (`bash -s`), over ssh (`ssh … bash -s` on the
mux) or in an LXD guest (`lxc exec … bash -s`). Fixes from
/tmp/station-remote-locus-audit.md (S1, S2, R1–R7).

* Identity (R1). `keeper` is vm_mgr's REGISTERED locus key, no longer a synonym
  for "this station". The host is the reserved tokens `""`/`@self`/`@keeper`/`host`
  or this station's OWN locus name (STATION_LOCUS / the registry). `_keeper_locus()`
  no longer falls back to `keeper`: an unconfigured station shows "locus not
  configured" (board 409 `unconfigured`, UI banner, no file↔DB sync) instead of
  silently reading and syncing into vm_mgr's board. New `GET /api/station/identity`.
  The host board routes are `/api/vm/@self/*` (and `@keeper`); a request for the
  station's own name is answered in-process (no 307). A middleware folds any host
  spelling in `?vm=` to `@keeper`. Several registered rows on one endpoint (keeper +
  ae-mgr at vm_mgr@192.168.1.100) resolve deterministically: kind=station, then a
  deliberately registered row over an auto-registered "ssh host added on" one, then
  a legacy-alias target; a true tie is never guessed. Legacy alias `ae-vm-mgr` →
  `keeper` is built in (STATION_LOCUS and dropdown names). The loci sync picks the
  self row the same way (no dict-order dependence). The UI (`isHostVm`, TodoDrawer,
  messages, `acHostSeat`, locus labels) uses the station's own names; an ssh locus
  named `keeper` stays selectable on other stations.
* Seat launch (S1, R2). `_tmux_persist`, `_seat_launch_script`, `_seat_launch_remote`
  and `_ssh_remote_launch_script` are gone. `_seat_launch` writes the seat script ON
  THE TARGET (heredoc on stdin into the target's `<state>/seat-launch`, 0700) and the
  PTY runs the same short `tmux -L console new-session -A -s <sess> "bash <file>"`
  everywhere — the tmux argv stays < 1 KB with a 20 KB directive ("command too long"
  fixed). If the script cannot be written the seat says why; there is no inline
  fallback. The script's env prelude is identical on every target (resolves the
  target's state dir, imports the seat keys from ITS env files, exports
  EXCHANGE_LOCUS/HUGPY_LOCUS = the target locus); the host additionally pins its own
  process env. Directive, guidance, handoff, models, fs/delegate switches and the A
  template are read ON the target (`locus_exec.read_cfg`), so a remote seat never
  gets the viewing station's directive. mct `{ws}`/model/label rewrites now happen
  before the script is written (they were dead code since mct v2). A reattach does no
  remote I/O. The host relaunch uses the same launcher (`seat_start_detached`).
  Unstick, paste (`body.vm`), show, live `/model` and every `_locus_run`/finder/journal
  spawn go through the same transport.
* Serve on a remote locus (S2, R3). `abstract-claude-serve-run` publishes where it
  listens: `<state>/ac-serve-<instance>.json` {port, pid} and a central seat_state row
  (seat `keeper`, status.surface=serve, status.port) via `abstract-claude seat-report`
  watching the serve pid. The station discovers a locus's serve from that row, else
  by the same probe run on the target (own pid only — never another user's serve),
  and reaches it with `ssh -O forward -L 127.0.0.1:<eph>:127.0.0.1:<port>` on the
  existing mux. ac-loci.json is now an optional override. `/ac/@<locus>/`, the Serve
  button, `/api/frontier/limits?vm=`, `/api/ac/rollover?vm=` and the serve token
  meter (`/api/frontier/cache?vm=`) use the SELECTED locus's serve. The runner no
  longer defaults EXCHANGE_LOCUS to `keeper`.
* Per-locus config (R4/R5). `/api/frontier/directive|handoff|models` and
  `/api/b/model` take `?vm=` and read/write the target's own files; the UI sends the
  selected locus on every seat-config call (SeatPanel `seatQ(vm)`, VM_SCOPED_API,
  the session model picker). B chat for an ssh locus runs that locus's own
  `hugpy_agent.mct.b_answer` on the target (no host-B leak); unknown names 404.
  `/api/a/usage?vm=` reads the central exchanges table for that locus. Findings and
  loops are filtered to rows tagged with the locus; steward log / keeper directives
  are labelled "this station". Live turn flow, B guidance, fs policy, delegate and
  GPT settings for another locus stay an honest 404.
* lxc-only paths (R6). The files API (`/api/stations/<name>/fs/*`) works for the
  host and every ssh locus (one python on the target, fenced under that user's
  $HOME, payloads on stdin); LXD guests keep `lxc file`. The journal probe never
  picks another user's station unit (hugpy showed hugpy-demo's): without a unit of
  its own a locus shows its own user journal.
* The station writes `~/.config/hugpy-station/state-dir` at startup (when its state
  dir is elsewhere) so peers' scripts find it.
* Tests: `resources/backend/test_locus_exec.py` (10: fake ssh runner, 20 KB directive
  → tmux argv < 1 KB, identical script for host and ssh, real local runs of the
  materialize/config/fs/probe scripts, forward over the mux),
  `resources/backend/test_remote_locus_141.py` (10: identity, ambiguity, legacy alias,
  unconfigured state, target-only seat config + env), `test_locus_central.py` updated.

## 1.0.140 — 2026-10-01 — every keeper ping goes through the toolserver issue gate

Fixes the serve keeper flood (/tmp/serve-flood-report.md: 47 of 48 keeper turns
in 24 h were station nudges, 80 of them from loop-detector key churn). Needs
abstract_toolserver >= 0.0.39 (category `issue`).

* Gate first, ping second: the loop detector (`_loops_act` → `_gate_loops`, one
  `issue_observe` per new/updated loop row, kind `loop:<source>`, subject = the
  loop identity), log findings (`_notify_findings` → `_gate_findings`, one observe
  per sighting with `n` = count growth, kind `finding:<kind>`, subject = the
  finding's existing `signature`, source `<unit>@<locus>` so sightings land on the
  86 imported issues) and B's proposal ✉ (`_propose_deliver`, source `b-propose`)
  ping only when the gate returns `page=true`. Caller / unit / client session are
  passed so the gate's self-origin guard can tell the keeper's own runs apart
  (`loop_detector._finding` now carries `caller`/`session_id`; a systemd restart
  loop is never self-origin).
* The comms relay (`_deliver_pings` → `_gate_pings`): a ping carrying
  `issue_fp=` was paged at its source and is nudged only while that issue is still
  `raised`; any other ping (channel `ch_…`, operator/station comms, an older
  station) is observed first (kind `channel`|`comms`) and nudged only on a page.
* PAGING FAILS CLOSED: no gate decision (toolserver down, bad reply) = nothing is
  sent; it is logged and re-judged next pass. History/disposition writes FAIL OPEN.
* B: queried only when `issue_b_query_ok(fp)`; its prompt carries
  `issue_memory(fp)` for THAT fingerprint only (replaces the NotifyBook
  prior-proposal block; NotifyBook stays as the read-through fallback); every
  outcome is recorded with `issue_record_b`; novelty is judged against the issue's
  own priors.
* Dispositions → registry: finding/loop closes and `POST /api/findings/<sig>/disposition`
  go to `issue_set` (inert → benign, accept/plain close → processed, reject →
  raised), proposal verdicts also to `issue_record_action` (accept = the fix).
  Each close is forwarded once; the station's own auto-resolve is not a disposition.
* `STATION_NUDGE_TMUX` (default 0): a failed serve delivery NEVER falls back to the
  tmux keeper pane unless this is explicitly 1 — the item stays pending on the
  board and the failure is logged (`comms-nudge-skip`).
* Delivery addresses the keeper SERVE SESSION, resolved fresh on every send (roster
  role → serve's rollover chain head). A nudge handed to a console session is
  watched until it drains (`inflight` in comms-nudge-pending.json): queued behind a
  turn on an `auto=off` session it is started with `POST /api/console/queue
  action=retry` once the session is idle; stranded on a rolled-over / wiped
  session or behind someone else's hold it is removed there and re-pended for the
  head. No new nudge is stacked on an undrained one. Serve itself is unchanged.
* `STATION_BUGSCAN=0` is now a master off: it wins over the persisted
  `bugscan/state.json` `on:true` (the scan loop, `_bugscan_run`, and the panel's
  on/now return 409); state.json is left as the operator set it.
* Tests: `resources/backend/test_issue_gate.py` (20, toolserver mocked), updated
  `test_keeper_nudge.py`, `test_channel_ping_nudge.py`.

## 1.0.139 — 2026-10-01 — a launch never kills the running Station

* CRITICAL FIX (1.0.138 VM test): `abstract-claude-serve-run` started serve
  without `--no-browser`, so serve-core's auto-open ran `hugpy-station --serve
  claude` through the full launcher on every Station launch; with port 8899 held,
  the launcher's reaper (`pkill -f hugpy-station/hugpy-station`, `pkill -f
  resources/backend/server.py`, `fuser -k -9`) killed the Station that had just
  started serve, and the headless `hugpy-station-web@` backend with it.
  - The runner now passes `--no-browser` (probed from `serve --help`; a serve
    without the flag is started with no DISPLAY/WAYLAND_DISPLAY so it cannot open
    a window).
  - The launcher has no name- or port-based kill left. `--serve` mode touches no
    process at all (serve-app joins this user's serve on 9124-9127, else starts
    one). Station mode examines only the listener on the station port: a live
    Station of this user -> hand off (main.js now takes Electron's single-instance
    lock and focuses the running window); a backend whose desktop Station
    (`HUGPY_STATION_DESKTOP_PID`) is gone -> that one pid is reaped; anything else
    (headless unit, other users, hand-run server.py) is left alone and the new
    Station takes the next free port. A stopped (^Z) Station gets a message, exit 4.
    A live Station is recognised by its binary (/proc/<pid>/exe) or argv[0] cut at
    the first space — Chromium rewrites the main process's cmdline into one
    space-joined string, which (caught in the pre-release VM test) made a second
    `hugpy-station` reap the running Station's own backend as "stale".
    Test: `tests/test_launcher_no_kill.py` (run it in a VM, never on a live host).
* seat-provision: "claude ok" now means a `claude` CLI that runs (`claude
  --version`), not the exit status of `curl | bash` (offline, that was 0 with no
  claude installed).
* tmux is a declared Recommends; without it the backend answers rc 127 + a clear
  message instead of FileNotFoundError tracebacks, `/api/term/backends` reports
  `frontier.tmux.available=false` and the UI disables the tmux button with the
  reason. Deb Depends now include curl, python3-venv, python3-pip (asserted by
  build-release.sh).

## 1.0.138 — 2026-09-30 — locus-specific station (every locus is a central-DB key)

* The station is LOCUS-SPECIFIC: the top-left dropdown pick is just a different
  `locus` key on the central (toolserver) tables. `_central_locus()` maps EVERY
  dropdown locus to its central key — host aliases (`''`/keeper/@keeper/host, or
  a pointer at this very user@host) -> `_keeper_locus()`; an operator alias
  (`$STATE/locus-aliases.json`); a registered locus is its own key; an
  unregistered ssh host / LXD guest whose endpoint is exactly ONE registered
  locus's endpoint resolves to it (hs-fresh -> hs-fresh-ubuntu, a-brain ->
  a-brain-coder-next); anything else is its own name. Resolution waits for the
  first loci sync after a restart, so an early write never lands on the bare name.
* ☑ board read AND write (add / edit / status / comment / delete / ✨ assist / 📨
  ping / ⧉ brief) go to the selected locus's central `todos` rows for ssh hosts,
  LXD guests and registered loci alike (`/api/vm/<locus>/todo`,
  `/api/mct/todo?vm=`). LXD guests no longer read `~ubuntu/todo.json` through
  lxc exec. The host keeper keeps its file failsafe (never fewer items than the
  file). Central board reads take the locus's whole slice (cap 500 -> 5000).
* ◳ canvas, ✉ messages (central comms inbox / comms ping, no lxc mail file),
  ✍ prompts, 🎯 rolling state, seat auth and mct seat status read the locus's
  central rows for every locus kind — no workbench, sidecar or station file on
  the locus. The drawer header names the central key when it differs (`→ key`).
  `todo-history` answers an honest 404 off-host (no central todo journal yet).
* Channel push for any harness (station half): a comms ping whose ref is a
  channel id (`ch_…`, "(ref ch_…)" in the note) skips the 300 s
  NUDGE_MIN_INTERVAL batch window and is pushed at once through `_nudge_frontier`
  — serve first (`_nudge_serve`, wait=false, queued, never interrupts), tmux
  fallback. Such pings stay kind=request (message-kind pings are closed before
  the nudge). Test: `test_channel_ping_nudge.py`.
* Carried fix (1.0.134-1.0.137 UI blank): the 🌐 browser panel regex is
  `/^https?:\/\//i` again; `test_static_js_syntax.py` `node --check`s every inline
  script.
* Source guard also refuses direct edits to /etc/ufw/* (use the gate action
  ufw.apply-rules: test → backup → apply → health check → auto-restore). A one-line
  invalid before.rules took the host off the network on 2026-09-30.
* `resources/serve-app/serve-core.js`: the join-first probe (STATION_CONSOLE_AC,
  HUGPY_AGENT_SERVE, then 9124/9125/9126/9127) and the backend preselect now live
  in ONE module used by both `hugpy-station --serve` (serve-app/main.js) and the
  Station's native console view (main.js: startServeIfNeeded joins any answering
  console and pins the backend's /ac proxy to it; SERVE_BACKEND /
  `--console-backend=` preselects on first show). Node test: tests/serve-core.test.js.
* hugpy serve window (`hugpy-station --serve [claude|gpt|hugpy]`, resources/serve-app,
  `abstract-claude-console.desktop`); pins `abstract-claude==0.1.69`,
  `abstract-serve-core==0.1.9`. `preload.js` ships in app.asar (window.stationConsole).

## 1.0.137 — 2026-09-30 — bounded SSH/tmux seat launch

* FIX (station UI gone since 1.0.134): the 🌐 browser panel's URL check was the
  double-escaped regex `/^https?:\\/\\//i` — a SyntaxError ("Invalid regular
  expression flags") that stopped index.html's whole inline script, so React never
  mounted and only the frontier terminal column rendered. Now `/^https?:\/\//i`;
  `resources/backend/test_static_js_syntax.py` runs `node --check` over every inline
  script and static/*.js.
* Source guard (PreToolUse hook `hooks/source_guard_hook.py`, wired by
  `a-settings-template.json` + firstrun): refuses copying/worktree-ing/cloning a
  source out of place and direct `twine upload`; points at the abstract-pypit
  release loop (`<staging>/<project>/push.sh stage|release`). Symlinked sources in
  /srv/pyit/dev are resolved to their real path.
* Loops already DECIDED upon are logged, not re-pinged: loop-detector rows go
  through the notifier's per-signature dispositions (`keeper_notify.gate_loops`);
  loop board items carry the `inert:/accept` close line, read back by
  `_loops_read_dispositions`. (One already-decided loop re-pinged 3x had set off a
  17 MB keeper turn.)

* Remote frontier seat commands are uploaded through a short-lived 0700 local
  wrapper and executed from a remote launch file. tmux and the interactive SSH
  argv no longer carry the generated prompt/settings payload, preventing the
  `command too long` fallback to a plain login shell.

## 1.0.131 — 2026-09-29

### abstract-claude 0.1.67 / abstract-serve-core 0.1.7 — the collator goes away after send

* Pin `abstract-claude==0.1.67`, `abstract-serve-core==0.1.7`.
* A sent prompt no longer reappears in the console collator (#kl-qpanel): a turn that
  failed or was interrupted after its prompt reached the conversation used to re-queue
  the originals as HELD. serve-core now marks a delivered batch `failed`; the console
  drops delivered ids that come back, closes the collator on every send, and reopens
  it only for a new explicit queue.
* abstract-claude main now carries the whole 2026-09-29 line in one release:
  permission cards, session rollover banner (session-rollover.js), central toolserver.

## 1.0.130 — 2026-09-30 — toolserver centralized (abstract-toolserver 0.0.31)

* Pin `abstract-toolserver==0.0.31` (Tier 1, client side: client / discovery / MCP bridge;
  stdlib-only). Service floor 0.0.31 (`# @service … url_env=HUGPY_TOOLSERVER_URL`).
* Consumers re-pinned on the shared client/bridge: `abstract-claude==0.1.65`,
  `abstract-serve-core==0.1.6`, `abstract-gpt==0.1.12`, `hugpy-agent[mct,serve]==0.1.82`.
* No hardcoded toolserver URL: backend `TS_UPSTREAM`, seat MCP entries, hooks and
  firstrun use explicit config (HUGPY_TOOLSERVER_URL / STATION_CONSOLE_TOOLSERVER /
  TOOLSERVER_URL) else the toolserver ADVERTISED on this host (abstract-toolserver
  discovery). Seat MCP entries carry TOOLSERVER_URL only when configured; the
  settings template drops `EXCHANGE_INGEST_URL=127.0.0.1:7004`.
* firstrun: no default STATION_CONSOLE_TOOLSERVER; handshake via
  `abstract-toolserver ensure --no-start` + `/healthz` version.
* Built with FORCE_DRIFT=1: drift rows were source-ahead-of-install (this release's
  own backend changes), the new pin not yet provisioned, and the dev abstract_claude
  tree (0.1.61) lagging the release lineage (0.1.65).

## 1.0.129 — 2026-09-30 — hugpy-agent 0.1.80

* Pin `hugpy-agent[mct,serve]==0.1.80` (codex lineage: OpenCode-default launch with
  `--allow-all` and the harness settings table, toolserver MCP entry fix, serve-style
  TUI tool calls, session signals + lease to hugpy central, B findings lookup, `--version`).
* unshipped-artifacts: keeper `abstract-claude-serve@.service` re-pinned — it is now
  byte-identical to the shipped unit (relinked by 1.0.128's station-serve-update).
* Companion releases tonight: abstract-toolserver 0.0.30 (service floor unchanged, 0.0.27),
  hugpy 0.2.5.

## 1.0.128 — 2026-09-30 — serve modules from PyPI; the station release owns the serve

* Operator: "I'd rather the serve be handled from the PyPI packages that serve it,
  through the hugpy-station update that houses it." abstract-claude 0.1.64
  (tag abstract-claude-v0.1.64 = 807adab) and abstract-serve-core 0.1.4 (tag
  abstract-serve-core-v0.1.4 = 4cb2390) are PUBLISHED on PyPI; the pins are plain
  PyPI pins again. 1.0.127's local-wheel mechanism is reverted.
* `station-serve-update` (run by firstrun in user/self mode): migrates a
  hand-written run-<instance>.sh ONCE into `<state>/env/serve-<instance>.env`
  (station-serve-update-parse; tokens never copied) and installs the shipped
  `abstract-claude-serve@.service` (the hand unit kept as *.hand-<ts>); when the
  serve venv differs from the pins, the runner changed, or a legacy unit is live,
  it schedules ONE waiter via `systemd-run --user`: wait for /api/state idle (no
  turn), max 30 min, then provision the serve venv from the pins, switch a legacy
  unit to abstract-claude-serve@<instance>, restart, verify; a forced restart or an
  unhealthy serve mails the keeper. `<state>/env/serve-<instance>.defer` keeps it
  to the file migration (the keeper: every keeper agent runs inside that serve).
* The shipped runner `abstract-claude-serve-run` covers both loci: instance env,
  state-root toolserver creds, TOOLSERVER_*/EXCHANGE_*/AC_USAGE_BACKEND defaults,
  `AC_B_GATEWAY`, `AC_UI_BAKE=0`, and a start marker.
* Drift row C also accepts the release tag `abstract-claude-v<pin>`.
* Tests: tests/test_provision_pins.py (3), tests/test_serve_update.py (8).

## 1.0.127 — 2026-09-29 — pins follow shipped source via locked local release wheels

* `abstract-claude==0.1.64` and `abstract-serve-core==0.1.4` are pinned to the
  release wheels built from `/srv/pyit/dev/abstract_claude@807adab`
  (serve-permissions) and `/srv/vm_mgr/tmp/serve-release/abstract_serve@4cb2390`
  (serve-integ) — NOT published to PyPI (the operator's call). The wheels ship in
  `resources/wheels/`, recorded with sha256 + source repo@commit in
  `resources/wheels.lock`; `resources/bin/station-wheel-lock` verifies the lock.
* `station-provision-venv` passes `--find-links resources/wheels`, so every
  provision (firstrun's seat venv, the shipped serve runner) installs the locked
  wheels instead of downgrading to the last PyPI pin. The build's PyPI pin gate
  accepts a sha-verified locked wheel for a pin; the layout check ships only
  locked wheels; drift row C checks the pin against pyproject.toml at the locked
  commit. Drift rows B and C are green without FORCE_DRIFT.
* Tests: `tests/test_provision_local_wheels.py` (6 — lock verify / sha
  mismatch / pin mismatch / unlocked wheel / shipped lock / a real provision
  from find-links into a temp venv).

## 1.0.126 — 2026-09-29 — nudges reach console-backed keeper sessions

* 1.0.125 handed nudges to serve's `/api/session/<role>/message` relay, which
  only resumes NATIVE claude sessions. The keeper role on this station is a
  console session (`cs-…`, hugpy backend), so serve accepted (202) and then
  failed with "--resume requires a valid session ID" — the nudge was lost while
  the station logged it delivered. Console sessions now go through
  `POST /api/console/chat` (runs when idle, queues in the console queue panel
  when busy); native sessions keep the relay route and the station reads
  serve's relay log for the hand-off's outcome — a failed hand-off falls back
  to the tmux pane instead of being dropped.

## 1.0.125 — 2026-09-29 — relay nudges go to the keeper's SERVE session; detector precision

* **Keeper nudge → serve** (operator: the 📨 nudges "should be going to the
  keeper's session directly in the serve"). Until now the station typed them
  into the legacy tmux `keeper-claude` pane (`tmux send-keys`, only when that
  pane sat idle at an empty prompt). Now `keeper_nudge.py` + `_nudge_frontier`:
  serve ALWAYS first — `POST <STATION_CONSOLE_AC>/api/session/<role>/message`
  (role `STATION_NUDGE_SERVE_ROLE`, default `keeper`; wait=false, lossless:
  serve runs it when idle, queues it for the turn boundary when busy); the tmux
  pane only when serve is unreachable / errors / has no live keeper session.
  Every nudge is audited with the target used (`comms-nudge … → serve|tmux`).
* **One nudge per 5 min** (`STATION_NUDGE_MIN_INTERVAL`) summarising all
  pending pings, deduped by (source, signature) across stations ("7 new board
  pings (1 distinct)"); the board's OPEN set is re-read at send time — a ping
  closed since it arrived is dropped and never counted (r1284, 04:17Z).
* **Fleet dedupe**: central's calls/jobs/queue/workers are watched only by the
  central-watcher station (`STATION_LOOP_CENTRAL_WATCHER`, default the keeper
  locus); other stations watch their own units and logs.
* **"stacked" means overlapping**: >= 3 calls of one (caller, model, prompt)
  in flight at once (processing/started → processed/ended). Sequential
  repetition is "repeated" and reported only over the caller token budget
  (`STATION_LOOP_CALLER_BUDGET`, 200k/day); an identical (non-growing) request
  re-sent >= 3x stays a loop signal. An agent run (each call's messages a
  strict prefix-extension of the previous, no overlap) is ONE run, flagged only
  past 50 steps or the token budget.
* **Declared callers** (`STATION_LOOP_DECLARED_CALLERS`, default tidy-eval,
  test-fire, grade) are budgeted (token_burn) and never loop-alarmed.
* Findings / loops reach the keeper board as ONE `[ping]` request (comms_ping
  kind=request — so the relay nudges), the ping doubling as the first ✉;
  re-mails are kind=message. The action is written into the item's NOTE as
  well as its text, so a triage edit of the text never loses the command.
  B's proposals stay `type:proposal` board rows.
* Tests: test_keeper_nudge (9), test_loop_detector (24), test_keeper_notify (18).

## 1.0.124 — 2026-09-29 — 🐞 bug scan greps + parses, findings go to B

* The bug scan no longer sends the journal window to B / the fleet's
  Coder-Next slot (~40k tokens every ~11 min per locus: op, hs-fresh-ubuntu,
  ae-hugpy). `log_findings.py` is a pattern table (crash_loop, rate_limit_429,
  http_5xx, traceback, oom, refused, timeout, other_error; systemd unit
  failures folded per unit, >= 3 runs = crash_loop) over the same gathered
  sources. Findings carry kind, source, count, first/last seen, <= 5 sample
  lines, a normalised signature (dedup key) and central's `FIX:` sentence when
  the error carries one. Emitted when new, when the count doubles (+3), or when
  it returns after 1 h quiet.
* B's channel: every scan (and every loop-detector cycle) rewrites
  `<workspace>/.hugpy_agent/mct/findings.json` (bug scan rows + active loops +
  emitted history). "what's broken" / errors / 429 / crash asks to
  `/api/b/chat` are answered from it with `tokens 0 · lookup`;
  `GET /api/b/findings` serves the set. hugpy-agent's b_lookup reads the same
  file. Tests: `test_log_findings.py` (16 cases).
* **Findings are PUSHED to the keeper** (operator: "it was a waste because it
  was never brought to the attention of the keeper"). `keeper_notify.py` is ONE
  notifier for loops and findings: a medium+ finding gets a ⚠ strip row, ONE ✉
  (dedup by signature, 30 min cooldown, re-mailed on count-doubling / return)
  and ONE keeper-board todo with the exact command, auto-resolved after 1 h
  quiet. Low severity = strip + findings.json only. Seat-pane tails (terminal
  prose) are strip-only unless `STATION_NOTIFY_SEAT_PANES=1`.
* **Remote stations deliver to the KEEPER** (`STATION_KEEPER_LOCUS`, default
  `keeper`): a station whose locus differs posts board rows on the keeper's
  slice via the toolserver and ✉ as `comms/ping kind=message`, tagged with its
  locus; unreachable = silent, retried next scan. Loops now use the same path
  (1.0.122-123 filed a remote station's loops on its OWN board).
* **token_burn**: the station meters its own model spend per task
  (`b_chat`, `b_propose_fix`); a task projected above
  `STATION_TOKEN_BUDGET` (200k/day; per task `STATION_TOKEN_BUDGET_<TASK>`)
  is a finding like any other. `GET /api/findings/notify` shows the rates.
* **B proposes fixes** (`b_propose.py`) for a NEW medium+ signature (or one
  recurring after it was resolved): deterministic context (traceback frame,
  else a grep of the signature's literal text across this backend, hugpy_agent
  and the unit's ExecStart script; ±25 lines, ~6k-token cap) → one JSON-mode
  call through B's Gateway (temperature 0, max 600 tokens), one in flight,
  deferred while central's queue has waiting jobs, at most `STATION_B_PROPOSE_PER_SCAN` (2) per scan → a keeper-board
  `type:proposal` by `B@<locus>` + one ✉, linked both ways with the finding
  (strip shows "B proposed: <id>"). Junk JSON never blocks the notifier.
* **Dispositions per signature**: open | proposed | accepted | rejected |
  inert. The keeper closes the board item with `inert: <reason>`,
  `reject: <reason>` or `accept` (plain close of a proposal = accepted), or
  `POST /api/findings/<sig>/disposition`. Inert silences notifications and
  proposals (still counted, greyed in the strip) until a MATERIAL change —
  rate >= 10x the marked rate, a new source/unit/locus, or a higher severity —
  and the re-opened notice says why. Every proposal is kept with its
  disposition and fed back to B, which must propose something materially
  different or answer `no_new_fix`; a mechanical novelty gate (token Jaccard
  >= 0.7 on the fix, or the same command set) drops duplicates; max 3
  proposals per signature per 7 days; an accepted fix that does not hold is
  re-proposed with that context.
* Tests: `test_keeper_notify.py` (18), `test_b_propose.py` (14).

## 1.0.123 — 2026-09-29 — loop detector sees peer loci on this host

* The crash-loop detector now also reads the station units of PEER loci on the
  same host (tonight's 120+ restart `hugpy-station-web.service` belonged to user
  hugpy and was invisible to 1.0.122). Peers: `STATION_LOOP_PEER_USERS` (default
  `hugpy`) plus loopback `ac-loci.json` names that are local accounts; units:
  `STATION_LOOP_PEER_UNITS` (`hugpy-station-web`, `abstract-claude-serve-station`,
  `hugpy-agent-serve`). Read with `sudo -n -u <user> XDG_RUNTIME_DIR=/run/user/<uid>
  systemctl --user show` (fallback `systemctl --user -M <user>@`). Same
  thresholds; the alert names the owning user and carries
  `journalctl --user -M <user>@ -u <unit> -n 50`. `GET /api/loops` `config` now
  lists `units` and `peers`. Test: peer-unit fixture (16 cases).

## 1.0.122 — 2026-09-29 — crash-loop detector, B fallback guard, drift row-31 ack

Operator, 2026-09-29: "I've never once seen the station inform me of any crash
loops." Three loops ran silently that day (central retrying a PEFT adapter load
on a permanent error, a health probe pinging Coder-Next on a timer, the serve
reducer stacking JSON calls on the one Coder-Next slot). The station now
detects loops and tells the operator.

* **`resources/backend/loop_detector.py`** (new, pure logic, fixture-tested):
  systemd units (NRestarts +3 in 10 min, or auto-restart seen twice), central
  jobs (attempt/retry >= 3, or an active job with no progress for 5 min), the
  same (model, error) recurring 3x in 10 min (numbers normalized, so
  `after 97s`/`after 99s` are one error; the central `FIX:` sentence becomes
  the action), identical (caller, model, prompt-hash) calls 3x in 10 min
  (rows without a prompt fall back to (caller, model) >= 6), >= 4 live queue
  rows on one model for 3 consecutive cycles, and worker `agent_boot_at`
  moving twice in 10 min. State persists in `<state>/loops.json`.
* **`server.py`**: `_loops_cycle` rides the existing 60 s reminder tick
  (`_todo_reminder_loop`, which now starts even with reminders off); each
  loop gets ONE ✉ keeper mail (30 min cooldown per key), ONE board todo on the
  keeper locus (`by=station-loops`, note carries the fenced command), and the
  todo is resolved when the loop stops. `GET /api/loops` returns
  `{active, recent, config}`; `?refresh=1` runs a pass. Env:
  `STATION_LOOP_UNITS_USER`, `STATION_LOOP_UNITS_SYSTEM`,
  `STATION_LOOP_CENTRAL`, `STATION_LOOP_CALLS_LIMIT`.
* **`fleetview-term.js`**: a fourth-row `#fv-loop-strip` under the rolling-
  state banner — one row per loop (source · identity ×count · first/last ·
  "do: <command>" + copy), hidden when the set is empty, polled every 30 s.
* Tests: `resources/backend/test_loop_detector.py` (15 cases; runs under
  pytest or plain `python3 …/test_loop_detector.py`).

* **B fallback guard** (`server.py` `_b_answer`, the third B path reached
  only when neither the VM nor the host one-shot of `hugpy_agent.mct.b_answer`
  works): an ack / help / state question, or any message against an EMPTY
  state (no policy, empty catalog, no derived memory), is answered
  deterministically with the same `(timestamp · tokens 0 · duration · lookup)`
  metadata line and never calls the Gateway model — mirrors
  `hugpy_agent.mct.b_lookup` (0.1.74). Tests:
  `resources/backend/test_b_fallback_guard.py` (6).
* Drift inventory: row 31 (`/srv/hugpy/abstract-claude-serve/run-station.sh`)
  re-ACKed at its current md5 — hand-edit by hugpy 2026-09-29 03:27 CDT,
  content unexplained, ACKed by keeper 2026-09-30 pending board r1160.
* Carries the 1.0.121 tree as shipped by keeper-codex (snapshot d91a5df).

## 1.0.120 — 2026-09-29

* Pins the provider-neutral `abstract-serve` runtime. Claude, GPT, and Hugpy
  entry points now share the same Serve Coordinator, queue/hold behavior,
  session store, and provider picker; Hugpy also exposes `serve --console`.
* Pins abstract-claude 0.1.61, abstract-gpt 0.1.11, and hugpy-agent 0.1.78.
* OpenCode and all provider harnesses use the shared Toolserver MCP bridge from
  `abstract-serve-core` with environment-only credentials.
* Hugpy's OpenCode frontend now uses a per-user branded binary copy when the
  system OpenCode install is not writable, so its splash reliably says
  “Hugpy Agent”.
* The frontier Hugpy terminal seat now launches the OpenCode harness
  (`hugpy-agent harness`) and pins hugpy-agent 0.1.75 so fresh Debian installs
  receive the required CLI command.
* Pins abstract-claude 0.1.59, carrying lossless prompt handling, explicit
  mediation/tidy controls, durable Hugpy tool and approval history, compact
  tool presentation, and live transcript ordering/reload fixes.

## 1.0.116 — 2026-09-28

* Serve has one conversation model selector grouped by Claude, GPT, and Hugpy.
  Selecting a model calls the durable session switch API; the conversation ID
  and history survive. The same selection works in browser and Electron views.
* Pins abstract-claude 0.1.58, abstract-gpt 0.1.8, and hugpy-agent 0.1.72.
  GPT Serve discovers models and switches an existing conversation. Hugpy Serve
  can finish ordinary conversation replies without making unnecessary tool calls.

## 1.0.115 — 2026-09-28

### Shared Serve provider switching

* Pins `abstract-claude==0.1.56`, which keeps one durable console conversation
  while switching its Claude, GPT, or Hugpy provider. The console session ID and
  SQLite history remain stable; only the provider-native session changes.
* The Serve session overlay exposes the switch on the selected conversation.

## 1.0.114 — 2026-09-27

### Session model picker

* Added one session-toolbar dropdown for workspace sessions and direct Claude, Codex, and Hugpy model selection. Choices save immediately through the existing model APIs.

## 1.0.113 — 2026-09-24

### Per-user station state is `~/.config/hugpy-station` (XDG) — never `~/hugpy-station`

Installing 1.0.112 recreated the retired `~/hugpy-station` because `firstrun`
(and every state-defaulting script) still defaulted the state dir to
`$HOME/hugpy-station`, and even labelled the canonical `~/.config/hugpy-station`
as "legacy" — backwards. Canonical per-user state is now
`$HUGPY_STATION_STATE`, else `${XDG_CONFIG_HOME:-$HOME/.config}/hugpy-station`,
matching `mct_gateway.py` `_home()` (fixed in 1.0.112). `vm_mgr`'s explicit
`HUGPY_STATION_STATE=/srv/vm_mgr/hugpy-station` (7006 unit + `--base`) is
unchanged — only the *default* moved.

* **`resources/bin/hugpy-station-firstrun`**: state default →
  `$HOME_DIR/.config/hugpy-station`. The `~/.config/hugpy-station` reconciliation
  is no longer called "LEGACY" (renamed `CFG_LINK`): by default it IS the state
  dir (the whole symlink block is a no-op), and only when
  `HUGPY_STATION_STATE` points elsewhere is it migrated + symlinked onto that
  state. `firstrun` never `mkdir`s under `$HOME/hugpy-station`; a lingering old
  `~/hugpy-station` is neither migrated nor deleted — just noted once.
* **`resources/bin/{hugpy-station-user-install,abstract-claude-serve-run,
  hugpy-station-web-run,hugpy-station-board-run,mct-push,mct-pull,
  hugpy-station-locus,station-provision-venv}`** and
  **`resources/station-stack/opt/station-keeper/seat-provision.sh`**: every
  `$HOME/hugpy-station` state default → the XDG default above. `mct-push`/
  `mct-pull` no longer prefer an existing `~/hugpy-station`. The app-location
  fallback probes (`station-provision-venv`, `seat-provision.sh`) now point at
  `${HUGPY_STATION_STATE:-…/.config/hugpy-station}/app/current/resources`.
* **`build/linux-after-install.tpl`**: the toolserver-credential "already
  present" check no longer probes the retired `~/hugpy-station/toolserver.env`.
* **`resources/systemd/hugpy-station-web@.service`**,
  **`resources/backend/static/docs/STATION-TOOLS-DIGEST.md`**: doc/comment
  defaults updated to the XDG path.

### release.sh keeps `prod/` in sync on promote/rollback

`release.sh serve` (called by both `promote` and `rollback`) now rewrites
`prod/` to hold EXACTLY ONE versioned deb symlink
(`hugpy-station_<ver>_amd64.deb`) into the absolute `latest/` path, so
`drift-gate.py` `check_deb` never sees a dangling `prod/` deb after a promote.

## 1.0.112 — 2026-09-24

### Bundled-wheel provisioning DEPRECATED — pins install from PyPI

* **`resources/REQUIREMENTS.txt`** (moved out of the old `resources/wheels/` dir)
  is now the single pin source. The four Tier-1 pins — `abstract-claude==0.1.55`,
  `abstract-gpt==0.1.4`, `hugpy-agent[mct]==0.1.71`, `abstract-search==0.0.0.42`
  — are all published on PyPI at exactly these versions and install from PyPI. No
  bundled `.whl`, no `--find-links`. Transitive deps were always resolved from
  PyPI (the provision path never used `--no-index`), so nothing offline is lost.
* **`station-provision-venv` / `seat-provision.sh`** drop `--find-links` and
  install the pins from PyPI with the same force-upgrade semantics (`pip install
  -U`), so an already-installed OLDER version is still lifted to the pin. Both
  now locate `resources/REQUIREMENTS.txt` (the `$HOME/hugpy-station/app/current/
  resources/wheels` probe is gone). `seat-provision.sh`'s legacy per-wheel
  fallback is replaced by a plain PyPI install.
* **`build-release.sh`**: the "every pin has a matching bundled wheel" gate is
  removed; a new **PyPI pin gate** fails the build unless every pin resolves on
  PyPI (`pip download --no-deps`). The layout check now requires
  `resources/REQUIREMENTS.txt` and fails if any `resources/wheels/` artifact
  ships. `electron-builder.yml` ships `resources/REQUIREMENTS.txt` (not the
  wheels dir).
* **`station-drift-check.sh`**: check **B** now compares the serve venv's
  installed `abstract_claude` against the `REQUIREMENTS.txt` pin; check **C**
  compares the pin against `/srv/pyit/dev/abstract_claude`'s `pyproject.toml`
  version. Neither references a staged wheel any more.
* The retired wheels are moved to
  `ARCHIVE/HUGPY_OLD/station-bundled-wheels-2026-09-24/` (MANIFEST + sha256).
* **`mct_gateway.py :: _home()`** now defaults the per-user state dir to
  `~/.config/hugpy-station` (XDG-aware), matching `server.py`,
  `seat-provision.sh`, and `abstract_claude` — it used to default to
  `~/hugpy-station`, which planted a stray state dir.
* **`build/linux-after-install.tpl`** carries the board-loop `.retired` guard:
  the postinst board-mirror loop skips any locus with
  `/etc/hugpy-station/<user>.retired`, so a reinstall / autoupdate never
  resurrects a retired `hugpy-station-board@<user>`.

## 1.0.111 — 2026-09-24

### One source per bundled module; live re-pinned to it

* **`resources/wheels/REQUIREMENTS.txt`** pins `abstract-claude==0.1.55`,
  `abstract-gpt==0.1.4`, `hugpy-agent[mct]==0.1.71` (wheels bundled; all built
  from `/srv/pyit/dev/<pkg>`, the single source). Firstrun no longer re-pins the
  seat venv back to 0.1.51 / 0.1.3 / 0.1.60. 0.1.55 carries the hub `forward`
  proxy (the `/api/chat` routes are gone); 0.1.4 fixes `abstract-gpt launch|exec`
  argument passing; 0.1.71 adds direct harness flags + the fleet TUI.
* **`station-drift-check.sh`**: check C compares the staged wheel against
  `/srv/pyit/dev/abstract_claude` (the stale `ac-wt/c144` worktree is archived);
  check F (git dirty/unpushed) is a warning, not DRIFT — git state is
  informational, content-vs-live is the gate.
* **`hugpy-station-locus enable`** is a no-op when
  `/etc/hugpy-station/<user>.retired` exists, so a reinstall no longer
  resurrects a retired `hugpy-station-web@<user>` (ae retires its per-user
  instances; station.hugpy.ai is the live station).

## 1.0.110 — 2026-09-19

### TODO: summarise this release

* 

## 1.0.108 — 2026-09-18

### C→B→A arbitration: operator prompts delivered VERBATIM

* **`server.py :: _mct_b_collate`** no longer asks a model to "compile" the
  operator's messages — it returns a single queued message exactly as typed and
  joins multiple verbatim. The old model paraphrase (which rewrote prompts into a
  1KB "digest" of the local model's guesses, and re-fired on every 1s poll tick
  with no backoff — the 2-prompts-become-~20-fable-calls blowout) is gone,
  preserved only behind `MCT_B_MODEL_COLLATE=1`.
* **`mct_arbitration.py :: commit`** delivers a single message with no marker
  (the operator's exact words); only a genuine multi-message merge gets a neutral
  `[Operator sent N messages, delivered verbatim]` header. The old
  `[B-compiled operator digest — …]` wrapper is removed.

### Pinned module manifest + force-upgrade provisioning (no more "flail")

* **`resources/wheels/REQUIREMENTS.txt`** — one pinned source of truth for the
  module set: Tier-1 libraries (abstract-claude 0.1.49, abstract-gpt 0.1.3,
  hugpy-agent[mct] 0.1.59, abstract-search 0.0.0.42), bundled-wheel authoritative
  (PyPI lags several); Tier-2 service floor (toolserver ≥0.0.27, a singleton the
  station calls, not installed).
* **`resources/bin/station-provision-venv`** installs/force-upgrades that set into
  a venv from the bundled wheels. `abstract-claude-serve-run` now self-provisions
  the serve venv on every start (the fix for the serve venv that silently drifted
  to abstract_claude 0.1.44 while the bundle shipped 0.1.49); `seat-provision.sh`
  is manifest-driven; `hugpy-station-firstrun` re-provisions the seat venv on every
  install and handshakes the toolserver version. `build-release.sh` fails the build
  if the manifest, the routine, or any pinned wheel is missing.

## 1.0.106 — 2026-09-18

### TODO: summarise this release

* 

## 1.0.105 — 2026-09-18

### TODO: summarise this release

* 

## 1.0.104 — 2026-09-18

### Shell surface: persistent terminals in the locus

* The **shell** surface is now a real persistent terminal in its locus, like
  the keeper seats — previously it ran as a bare PTY child of the station
  server, so switching tabs or any station restart (deploy / relaunch) lost it.
  It is now backed by tmux (`new-session -A`) on a dedicated `shell` socket
  IN the locus, so it survives detach AND restarts; reopening the shell for a
  locus reattaches to the same live session.
* Uniform across every locus a station runs on: host shell (host tmux, escaping
  the station cgroup via a systemd scope like the keepers), lxc VM shells (tmux
  inside the VM), and ssh loci (tmux on the remote). Falls back to the old bare
  shell when a locus has no tmux — no regression.
* Extra independent shells still come from the multi-instance path (`inst`
  2..32), each its own persistent session. The dedicated `shell` socket keeps
  these out of keeper (`console`) seat enumeration. Pulled `shell-tmux:` seats
  (already a tmux attach) are unchanged.

## 1.0.103 — 2026-09-18

### Terminal seat: freeze + paste fixes (root causes, not workarounds)

* **Freeze eliminated.** xterm’s `RenderService` paused painting via an
  IntersectionObserver whose un-pause is dropped by Electron/webview engines,
  leaving `_isPaused` stuck true so buffered PTY bytes never painted though the
  socket was live (the “freezes while I’m in it, needs a focus event, then
  unreliably” symptom). These panes are shown/hidden by CSS and never detached,
  so the pause only ever hurt: `_isPaused` is now pinned false at `term.open()`.
  The 1s safety interval additionally clears a dropped `requestAnimationFrame`
  (`_renderDebouncer._animationFrame` still set a second later = stuck).
* **Paste no longer autosubmits.** `fvPaste()` sends straight to the PTY,
  bypassing xterm’s own paste, so multi-line clipboard text arrived as raw
  keystrokes and a TUI prompt (claude-code) ran it line by line. It is now
  wrapped in bracketed-paste markers when the app has that mode on
  (`term.modes.bracketedPasteMode`), so it lands as ONE paste.
* Both fixes are in `fleetview-term.js` only; the tmux substrate and the PTY
  path are untouched — the defects were always in the viewer, not tmux.

## 1.0.102 — 2026-09-18

### Keeper surface naming: “Terminal” / “Serve” (operator-facing)

* The non-serve keeper surface is now labelled **Terminal** in the UI — the
  interactive-TUI seat (a full-screen CLI painted through xterm.js over a
  WebSocket-fed PTY), with per-backend labels `Terminal · Claude Code`,
  `Terminal · ChatGPT Codex`, `Terminal · MCT pointer exchange`. The serve
  surface reads **Serve console**. Renames operator strings in `server.py`
  (`TMUX_SEAT_LABELS`, `AC_SERVE_LABEL`) and `fleetview-term.js`
  (`BACKEND_LABELS`, surface tips, status line) only.
* Rationale: “tmux” named the surface after its sturdiest sub-layer. tmux is only
  the persistence/multiplexing substrate; the freeze / copy-paste / unreliability
  the operator hit all live in the Terminal *viewer* (xterm.js RenderService
  pause, three unsynchronised selection layers, resize storms), not tmux — which
  passes every server-side probe.
* Wire tokens unchanged for compatibility: `KEEPER_SURFACE` value `"tmux"`, the
  `seat:"tmux"` API field, `STATION_KEEPER_SURFACE`, the `fv-keeper-surface`
  localStorage key, and the `keeper-claude` / `keeper-codex` tmux session names.

## 1.0.100 — 2026-09-17

### Operator console spec (abstract-claude 0.1.43); legacy fleetview bar retired

* Bundled abstract-claude 0.1.43 (board t390): model lists from the Anthropic
  Models API in every picker, a picked model is staged and applied on the next
  send, searchable pickers, per-turn and per-session cost from a maintained price
  table, `archive | new` on every standing-session row, an inter-session relay
  through B (`POST /api/session/<role>/message`), attachments via the composer
  `+`, an expandable input, copy buttons (prompt / reply / turn / session), and
  a Local pane that looks like the frontier sessions.
* fleetview-term (tmux seats): the `B: on`, `A: on`, `⧉ copy` chips and the
  `/cmds` palette are gone (spec#5) — the serve console supersedes them. Copy
  from a terminal still works (right-click, Ctrl/Cmd+Shift+C); the gates stay
  always-on server-side.

## 1.0.99 — 2026-09-16

### Context warning before rollover; worker failsafe when B fails (abstract-claude 0.1.41)

* Bundled abstract-claude 0.1.41: an always-visible `ctx N / threshold` chip in
  the console (amber from 80 %, red when due, countdown while a roll is
  pending; click = roll now / cancel) and the roll banner moved clear of the
  panels; when the station's local model (B) is offline or fails, the Local
  pane and the queue tidy fall back to the Worker session.

## 1.0.98 — 2026-09-16

### Standing sessions, type-letter board ids, honest reminder notes; bundles abstract-claude 0.1.40

* Bundled abstract-claude 0.1.40: standing session roster (Keeper ledger-bound,
  Chat, Worker pinned to a small model, Local = this station's B chat rendered
  in the console pane with shared history); a prompt typed while the server is
  mid-turn goes straight to the collating queue instead of producing the faux
  "queued as …" exchange that vanished on refresh; session events carry the
  full reply and the chat re-pulls its history when a turn it did not stream
  ends (no more manual refresh); no baked frontier model (t261).
* Board ids carry the type's first letter: t todo, r request, b bookmark,
  o operator, p proposal, d direction, m message (operator ask; the number is
  the identity, any letter resolves; ref chips in older notes keep jumping).
* Reminder hand-off comments name the real delivery path — "queued for the
  keeper serve chat (reminders panel)" on the serve surface, "serve is DOWN"
  when /api/state fails, tmux wording only on tmux (t360 → t362).
* `abstract-claude-serve-run` regenerates the /ac console UI dist at start,
  so every locus's console matches the bundled serve (t361).

## 1.0.97 — 2026-09-16

### Serve chat streams live through /ac; reminder cycle no longer pushes its own batches

* `/ac` proxy streams every non-HTML upstream body as it arrives
  (`StreamResponse`, `X-Accel-Buffering: no`) with no total timeout
  (`ClientTimeout(total=None, sock_connect=10, sock_read=3600)`). Before, the
  chat's SSE reply was buffered until the turn ended and any turn over five
  minutes died with a 500 — the console showed nothing until a page refresh.
* Board reminder cycle: `[prompt] …` rows (the board mirror of a queued
  prompt) are pointers, not work — skipped; a new reminder batch closes the
  older still-open station-reminder prompts via `prompt/done`; the central →
  local board sync now mirrors edits (status/text/note/priority), not just new
  rows, so items closed centrally stop being reminded.
* Bundled abstract-claude 0.1.39: the console follows a rolled session's
  successor once (no more 5 s re-click/reload blip on an archived session).

## 1.0.96 — 2026-09-16

### Bundled abstract-claude 0.1.38 — per-call metadata in the chat

* Every tool call, response, prompt and whole turn shows its start timestamp,
  duration, token count and model, with a progress wheel while active
  (`/api/session/events` now carries model / usage / msg_id / tool_id / chars;
  keeper-live.js renders turn headers, per-bubble summaries and spinners).
* Toolserver (separate tree): `todo_batch` — several board actions in one
  round trip (`{"ops":[{"op":"add"|"update"|"done"|"remove", ...}]}`).

## 1.0.95 — 2026-09-16

### MCT adapter: a named session id binds the whole pane (t321, second pass)

* `NativeAdapter.probe()` now collects `--session-id` from EVERY process under
  the seat pane before choosing a transcript. Helper children whose argv merely
  contains "claude" (the MCP bridge `abstract_claude mcp`, seat-report) reached
  `_choose` without a session id and adopted the newest transcript in the
  project — the keeper serve chat's — even after 1.0.93. With a named id the
  adapter binds to that transcript or waits; it never adopts another.

## 1.0.94 — 2026-09-16

### Bundled abstract-claude 0.1.37 — automated session rollover

* A serve chat whose last API call's context reaches `rollover_context_tokens`
  (250k) is inefficient: every tool step re-reads it. serve now measures each
  session at turn end; in `auto` mode it schedules a roll with a 60 s cancel
  window (banner in the chat), `manual` only offers "roll now". A roll archives
  the old session (never deleted; collapsed in the sidebar) and starts a fresh
  one from the task ledger; the ledger is bound to the successor's session id.
  Precondition: a stale ledger gets ONE checkpoint turn first
  (`rollover_ledger_fresh_turns`, 0 = off). Config in `abstract-claude/config.json`
  or `AC_ROLLOVER_*` env; `GET/POST /api/session/rollover`.

## 1.0.93 — 2026-09-16

### Prompts no longer fan out to the tmux seat (t319–t323)

Root cause chain seen 2026-09-16 13:02–13:07 UTC: a prompt-panel submission went
to the MCT tmux seat while serve led; a serve seat-wipe flipped the pane to the
tmux terminal and spawned a fresh keeper-claude session; two right-clicks pasted
the operator's question into that seat; the MCT adapter had pinned itself to the
serve chat's transcript.

* `POST /api/mct/prompt` returns 409 while `STATION_KEEPER_SURFACE=serve`
  (`{"seat":"tmux"}` addresses the terminal seat explicitly) — t319.
* `keeper_surface` reports the CONFIGURED surface; the probe rides `ac_serve.ok`.
  The `/ac` proxy serves a self-retrying "keeper chat restarting…" page on 502,
  and index.html no longer falls back to the terminal on a blip — t320.
* Seat launch scripts pass `--session-id "$__sid"` (also exported as
  `MCT_SEAT_SID`); `mct_gateway.NativeAdapter._choose` treats a named session id
  as binding and never falls through to "newest transcript" — t321.
* Terminal right-click never pastes; paste is the Ctrl/Cmd+Shift+V chord only — t323.
* Bundled abstract-claude 0.1.36: `/api/usage/sessions[].kind` (`chat`|`seat`)
  and sidebar badges via keeper-live.js — t322.

## 1.0.92 — 2026-09-16

### Reminders go to the toolserver prompt queue, not the terminal

* `_frontier_dispatch()` no longer TYPES a pointer line into the live frontier
  pty. With the keeper surface on `serve` the pty is not the keeper at all, and
  on a handoff-roll the cycle composed one line PER OPEN TODO — the operator saw
  seventeen pointer lines land in the input in a single burst (2026-09-16).
  Delivery is now `prompt/submit` on the toolserver, and the whole cycle is
  collapsed into ONE queued item ("N open items for keeper") that lists each
  id + title + next step with its `prompt.md` path as a pointer.
* `_flush_dispatch_batch()` dedups on the sorted open-id set + newest item ts
  (state in `<state>/handoff-dispatch.json`, cross-checked against prompts still
  pending on the locus), so an unchanged open set never requeues.
* The pty write survives ONLY for a tmux keeper surface with the new
  `STATION_REMINDER_PTY=1` explicitly set. Default off; in serve mode never.
* The cycle now cross-checks the toolserver board and skips items already
  `done` there. The file board (`todo.json`) is the UI's source but is not
  updated by the toolserver's `todo_done`, which is why the burst re-dispatched
  items closed hours earlier.
* The `prompt-inbox` dirs stay as the audit copy but are capped:
  `_prompt_inbox_sweep()` keeps the newest 50 each cycle
  (`STATION_PROMPT_INBOX_KEEP`). The 255-dir backlog was archived.
* A gateway failure mid-cycle now `break`s instead of `return`ing, so anything
  already composed still reaches the queue.

### Bundled abstract-claude

* Ships `abstract_claude-0.1.35-py3-none-any.whl` (was 0.1.34): toolserver usage
  backend with a one-row merge, prompt reducer, cold-start endpoint, MCP
  governor, harness origin classifier, `/api/session/events`, `/api/reminders`,
  and the keeper-live overlay. Seat provisioning installs it into a seat venv on
  the next seat launch.

## 1.0.91 — 2026-09-16

### abstract-claude serve is the keeper surface (t-serve / t296)

* `abstract-claude serve` is now the DEFAULT frontier surface. server.py gains
  `KEEPER_SURFACE` (`STATION_KEEPER_SURFACE`, default `serve`), `_ac_serve_state()`,
  `_ac_serve_restart()`, `_keeper_surface_now()` and `_ac_serve_cache_doc()`.
  The console renders it same-origin at `/ac/` through the existing `ac_proxy`.
* `/api/…/state` publishes `keeper_surface`, `ac_serve` and
  `frontier.backend_labels`, so the picker lists the serve console FIRST and the
  tmux backends stay selectable as explicit "terminal seat (tmux) · …" choices.
  **Nothing removes or hides a tmux seat, and no code path added here ever kills
  a tmux session.** `STATION_KEEPER_SURFACE=tmux` restores the old default.
* `/api/frontier/relaunch` in serve mode restarts the `abstract-claude-serve@station`
  USER unit instead of recycling a tmux seat; `?surface=serve|tmux` is the
  canonical switch and `?backend=` is accepted as an alias.
* `/api/seat/wipe` on the host seat defaults to serve's own reset (drop the
  labelled per-launch session dirs, restart the unit). `keeper-claude` and
  `keeper-codex` are explicitly listed as kept.
* `/api/frontier/cache` accepts `backend=serve` (alias `ac`/`abstract-claude`)
  and reads serve's own usage DB, so the token meter keeps working. On the host
  seat in serve mode it is the default, with the tmux transcript probe as the
  fallback.
* The idle-relaunch loop stays quiet in serve mode — there is no idle tmux
  prompt to roll, and it must never recycle a seat behind the operator's back.
* fleetview-term.js: `AC_FORCE` / `acOffered` / `acSetForce` / `acSync` /
  `acView`, the serve entry in the frontier picker, and the `fv-keeper-surface`
  localStorage preference. index.html gates `paneSrc` on the surface.
* Docs updated for the new nomenclature: `frontier-directive.md`,
  `static/docs/NOMENCLATURE.md`, `static/docs/STATION-FEATURES-PER-LOCUS.md`
  (backends are now `serve` / `mct` / `claude-code` / `codex`; `mct-deprecated`
  and `clawd-code` are retired from the picker).

### MCT fixes

* **t272** — deterministic transcript choice. `NativeAdapter._session_ids()`
  reads `--session-id` / `--resume=…` off the pane's own cmdline; a named
  session wins over mtime, the chosen transcript is pinned so a seat's read
  offset cannot flap, and the source already adopted for a seat beats a newer
  stranger.
* **t249** — reply-as-pointer is rendered as plain text from the ledger. A bare
  acknowledgement is dropped; when the seat wrote prose around the pointer only
  the raw `mct://` line is removed. No handle is ever shown to the operator and
  both forms stay verbatim in the raw archive.
* **t274** — token-lean details. Harness notices, subagent result blobs and tool
  output are recorded as BOUNDED excerpts (`bound()`, `MCT_HARNESS_EXCERPT`,
  `MCT_TOOL_EXCERPT`); each trimmed detail carries a `record` pointer at the
  untouched provider record in the raw archive. Wire schema unchanged.
* **t275** — a reply to a cancelled/interrupted turn is published, not refused;
  the turn completes with an audit note.
* Task notifications delivered as plain-string `user` records are parsed (they
  were only read off `attachment` records before).
* **t266** — pending TUI choice prompts. `mct_http.py` reads a tmux-backed
  seat's own pane and publishes the pending-choice card on the live status
  route, so an AskUserQuestion / permission box can be answered from the console
  instead of the raw terminal. `static/mct-renderer.js` renders it.
* `mct_selftest.py` covers all of the above: 62/62.

### I/O guard (the 854 GB incident)

* Every seat's tmux server — and therefore every child a seat or its subagents
  spawn — starts de-prioritised. `_scope_prefix()` now wraps the command in
  `nice -n 10 ionice -c 3` and gives the systemd user scope `CPUWeight=50`
  (plus `IOWeight=20` where `_io_delegated()` finds the cgroup v2 `io`
  controller actually delegated — most hosts delegate only cpu/memory/pids, so
  `IOWeight=` would otherwise be accepted and silently ignored).
* New `resources/bin/sweep-orphan-seat-scopes` + its user `.service`/`.timer`
  reap `tmux-spawn-*.scope` units left behind by dead seats. Fail-safe by
  contract: it aborts rather than touch a scope backing a live tmux session.
* `resources/backend/a-settings-template.json` ships a `permissions.deny` list
  blocking whole-filesystem `ugrep`/`bfs`/`rg`/`grep`/`find`/`fd` invocations.
  Deny globs are evadable and the scheduler class is not — this is the second
  line, not the first.

### Packaging

* `resources/wheels/` ships **abstract_claude 0.1.34** (0.1.31 dropped); serve
  needs 0.1.34.
* New shipped user units: `resources/systemd/abstract-claude-serve@.service`,
  `sweep-orphan-seat-scopes.service`, `sweep-orphan-seat-scopes.timer`, with
  `resources/bin/abstract-claude-serve-run` as the serve ExecStart. None are
  enabled by the package — `hugpy-station-firstrun` links the two new CLIs into
  `~/.local/bin` and each unit's header gives the two-line enable recipe.
## 1.0.121

- Correct OpenCode Toolserver MCP configuration schema (`enabled` and argv-list command); pin hugpy-agent 0.1.79.
