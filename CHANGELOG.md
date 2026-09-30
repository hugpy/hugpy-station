# hugpy Station — changelog

Versions before 1.0.91 are recorded in git history and in README.md; this file
starts at the release that introduced it.

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
