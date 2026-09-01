# keeper-relay

Watches the operator's Discord comms channel and types new operator messages
into the keeper's tmux pane, so an idle keeper can be reached without the
operator opening the web console.

Status: **installed, stopped, disabled, dry-run by default.** Enable only after
reading "The threat this accepts" below.

## The threat this accepts

The keeper's Claude runs with `--dangerously-skip-permissions`. Its footer says
so out loud:

```
⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt
```

**Anything typed into that pane becomes a keeper turn with full authority and no
confirmation prompt.** This daemon's entire purpose is to type into that pane.
So the security question is not "can the relay be exploited" — it is *"who can
reach the keyboard, and in what shape does the text arrive."*

What this means concretely, stated plainly:

> **Whoever controls the `devinthadude_` Discord account can execute arbitrary
> actions on this station as `ubuntu`, without a permission prompt.** The relay
> reduces the authentication of a root-equivalent channel to "controls a Discord
> account." That is the trade the operator explicitly accepted. Nothing in this
> code softens it, and no amount of sanitization can — sanitization controls the
> *shape* of the text, never its *meaning*.

Everything below narrows the blast radius around that accepted core. None of it
removes it.

## Controls

| # | Control | Mechanism |
|---|---|---|
| 1 | Allowlist, fail-closed | `direction=="in"` AND exact author match. Anything else drops, with a logged reason. |
| 2 | Sanitize before send-keys | Whole ANSI sequences removed; all control/format chars stripped; newlines → spaces; capped at 1500 chars. |
| 3 | Own cursor | `~/.local/state/keeper-relay/state.json`. Never `hugpy-escalate`'s watermark. |
| 4 | Idle check + queue | Five signals must all agree before typing; otherwise queue and retry. |
| 5 | Audit trail | Append-only JSONL: hash + 40-char prefix, never full content. |
| 6 | Kill switch | `systemctl stop keeper-relay` or `touch ~/.keeper-relay.disabled`. |
| 7 | Dry-run default | Injects nothing until the keeper explicitly opts in. |

### 1. Allowlist (fail-closed)

Inject only if `direction == "in"` **and** `author` is an exact string match in
`KEEPER_RELAY_ALLOW` (default `devinthadude_`). Everything else is dropped and
logged:

- our own `out` echoes (the channel echoes our posts back — the loop trap);
- other humans, e.g. `ClosetShark`, a real collaborator **explicitly not
  authorized**;
- bots, unknown authors, missing/non-string authors, non-numeric `ts`,
  malformed records, unparseable payloads.

Matching is exact — `devinthadude_2`, `xdevinthadude_`, `DEVINTHADUDE_` and
` devinthadude_` are all rejected. An empty allowlist drops everything.

### 2. Sanitization

The dangerous character is the **newline**: `send-keys -l` replays it as a
RETURN, which would submit early and let the remainder of the message run as its
*own* keeper turn. So:

1. NFC-normalise.
2. Remove whole ANSI sequences (CSI/OSC/2-char). Dropping the lone `ESC` is
   enough for safety — the residue is inert without it — but it leaves visible
   `[31m` junk in the prompt, so the entire sequence goes.
3. Strip every remaining `Cc`/`Cf`/`Cs`/`Co` char: C0/C1 controls, zero-width
   and bidi-override chars (trojan-source-style display spoofing), surrogates,
   private-use.
4. Newlines/CRs/tabs → spaces; collapse whitespace runs; trim.
5. Cap at 1500 chars with a visible `…[truncated by keeper-relay]` marker.
6. Empty after all that → drop.

The text is then prefixed with provenance: `[relay:devinthadude_] `.

**Image attachments are fetched, everything else is skipped.** The operator's
UI-bug screenshots are load-bearing context, so — and only for a message that
already passed the allowlist *and* produced non-empty sanitized text — the relay
fetches image attachments to `~/relay-inbox/` and injects the **local path** so
the keeper can Read the file: `[attachment: /home/ubuntu/relay-inbox/relay-…png]`.
Anything skipped adds one aggregate `[+N attachment(s) skipped]` (reasons go to
the audit log, not the tag). Every gate is fail-closed and a failed gate skips
*that attachment only* — it never blocks the message text:

- `https` only; host exactly `cdn.discordapp.com` or `media.discordapp.net`;
- **redirects refused** (a dedicated opener raises on any 3xx — the CDN serves
  bytes directly, so a redirect would be a chance to leave the allowlisted host);
- `content_type` ∈ {png, jpeg, gif, webp}, checked on **both** the declared field
  and the served `Content-Type` header, which must agree on the same allowed type;
- declared `size` an int ≤ 8 MiB, **and** the cap re-enforced while streaming in
  chunks (the declaration is never trusted — a stream past the cap aborts);
- **magic bytes** must match the type, or the bytes are discarded;
- at most 4 per message; 15s per fetch (worst case 4×15s stalls one cycle);
- the local name is **ours** — `relay-<UTC>-<msg-ts>-<n>.<ext>`, written
  atomically, never overwriting. The remote `filename` is attacker-controlled and
  touches neither the disk name nor the tag; the signed CDN `url` is never logged.

Fetched files are pruned after 30 days (only our `relay-*` names — the keeper's
own files in that directory are never touched). Any unexpected error in the whole
attachment stage degrades to "count them, fetch nothing" and the text still flows.

The payload reaches tmux as its **own argv element** — `send-keys -t T -l -- TEXT`
— via `subprocess.run` with a list. No `shell=True`, no string interpolation,
ever. The `--` guards a payload starting with `-`. Verified: a payload of
`; rm -rf /srv/share; $(touch /tmp/PWNED); \`id\`` lands in the box as inert
literal text and fires nothing.

### 3. Own cursor

`~/.local/state/keeper-relay/state.json` holds `last_ts` + the persisted queue.
It is deliberately **not** the `hugpy-escalate` watermark: a shared cursor means
whoever polls first eats the message. On first run the cursor starts at **now**,
so channel history is never replayed into the keeper's keyboard. A corrupt state
file is treated as a first run, not a crash.

### 4. Busy detection — what was chosen and why

`pane_current_command` is **useless** for this pane: it reads `claude` whether
the keeper is idle or mid-turn, because a TUI is one long-lived process. The
signals below were measured on this box against a real Claude TUI. **All five
must agree**; any disagreement, any read error, any ambiguity → queue and retry.

| Signal | Idle | Not injectable |
|---|---|---|
| `esc to interrupt` in pane text | absent | present → turn in flight |
| `pane_current_command` | `claude` | anything else → **TUI gone, see below** |
| `alternate_on` | `1` | `0` → TUI not up |
| `pane_in_mode` | `0` | `1` → copy-mode eats send-keys |
| cursor on `❯` row at col 2 | yes | else → half-typed text in the box |

- **`esc to interrupt`** is the busy marker. Confirmed by A/B on the *live keeper
  pane itself*: mid-turn it showed
  `⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← 1 agent`;
  once the turn finished, the same pane showed
  `⏵⏵ bypass permissions on (shift+tab to cycle) · ← 1 agent · ↓ to manage`
  — marker gone. A real idle TUI on a scratch socket greps zero for it. The
  *spinner word* is useless — it's randomised (`Seasoning…`, `Baked for 4s`) —
  so the stable footer string is used instead.
- **"Idle" means the main input box is free, not that the station is quiet.**
  Observed live: the keeper's box read idle while three background agents were
  still running. That is the correct signal for this relay's purpose (reaching an
  idle keeper), but see Residual risk #6.
- **`pane_current_command != claude` is the most important check.** If Claude
  exits, the pane falls back to a **bash prompt**. Typing an operator's sentence
  there hands it to the *shell*. This is a hard stop, not a heuristic.
- **The empty-box check reads the cursor, not the row's text.** The idle box
  renders a dim `Try "fix lint errors"` placeholder, so the row is *not* blank
  when empty — text-matching would false-positive on the placeholder. Measured:
  empty box → `cursor_x=2`; 27 typed chars → `cursor_x=29`. But `cursor_x` alone
  is insufficient, because a 1500-char payload **wraps** and a continuation row
  can put the cursor back at column 2 — so we also require the cursor to be on
  the row carrying the `❯`, which continuation rows never do.

**Failure modes of this detection, honestly:**

- *Version-brittle.* `esc to interrupt`, `❯`, and the 2-column offset are Claude
  Code UI details, not an API. A UI change silently breaks detection. It breaks
  toward **queueing** (false-busy) in most cases, but a change that *removed* the
  marker would break toward **false-idle**, which is the dangerous direction.
  Re-verify these constants after any Claude Code upgrade.
- *False-busy is cheap, false-idle is not.* The literal string `esc to interrupt`
  appearing anywhere in visible pane output (e.g. the keeper reading this very
  README in its pane) reads as busy → the relay just waits. Harmless.
- *TOCTOU.* Between the idle check and `send-keys` there is a ~ms window in which
  a human at the web console could start typing. The relay would append to their
  line. Not closable from outside tmux — see Residual risk.
- *Fixed 2-column offset* assumes the box's `❯ ` prefix. A themed/re-laid-out
  prompt would need `EMPTY_COL` re-measured.

### Delivery semantics: at-most-once, deliberately

At most **one** message is injected per cycle, and only into a confirmed-idle
pane; after injection the pane goes busy, so the next cycle naturally holds. The
queue item is popped **before** typing. If confirmation fails, the message is
**not** retried:

> A duplicated instruction to a bypass-permissions keeper ("delete the thing")
> is worse than a lost one the operator can simply resend.

Injection is confirmed in two stages, both polled rather than read once (the TUI
renders asynchronously — **measured at ~60ms**; reading immediately saw an empty
box and stranded the message unsent):

1. after `send-keys -l`, wait up to 3s for the text to appear in the box — if it
   never lands, **Enter is never sent**;
2. after `Enter`, wait up to 10s for the box to clear. If it doesn't, the text is
   `C-u`-cleared — otherwise it would sit unsent in front of the operator *and*
   wedge the relay forever, since its own idle check would see a non-empty box on
   every future cycle.

### 5. Audit trail

`~/.local/state/keeper-relay/audit.jsonl`, append-only. Every message gets a
record: `ts`, `author`, `decision` (`accept`/`drop`/`hold`/`dry-run`/`inject`/
`inject-unconfirmed`/`error`), `reason`, `queued`, `sha256` of the raw content,
and a 40-char sanitized `prefix` for legibility. **The full content is never
written** — it may carry operator secrets. The hash lets you prove what was
injected without storing it.

```sh
tail -f ~/.local/state/keeper-relay/audit.jsonl | python3 -m json.tool
```

### 6. Kill switch

```sh
touch ~/.keeper-relay.disabled     # honoured every cycle, no restart needed
sudo systemctl stop keeper-relay   # full stop
rm ~/.keeper-relay.disabled        # resume
```

### 7. Dry-run

Default. Polls, filters, sanitizes, checks idle, logs what it *would* inject —
and injects nothing. `--status` prints config (endpoint masked), cursor, queue
depth, kill-switch state, and the live pane's current verdict.

### 8. Session steward (OFF by default) — the lull-triggered restart

Long keeper sessions drift into uncontrolled context compaction; a deliberate
restart with a written handoff beats an unpredictable squeeze (proposal p4,
`LOCAL-LLM-OFFLOAD.md` §4). **The core principle is *restart in lulls*: the ~2h
cadence does not force a restart — it *arms* one; the armed restart then *fires*
only at a confirmed pane lull (the same idle gate every injection uses), never
mid-turn.** The clock is the floor; the lull is the trigger.

Per cycle (only when `KEEPER_RELAY_STEWARD=on`):

- **arm** — at/past the cadence, measure the newest `*.jsonl` transcript's growth
  since the last reset. `< FLOOR` = an idle session, skip silently; `>= THRESHOLD`
  = arm. (Tokens are the trigger; the clock is only the cadence.)
- **warn** — inject one idle-gated `[steward]` line telling the keeper to write
  survivors to `~/handoff/next.md` or extend, and set a `WARN_LEAD` deadline.
- **extend** — the keeper writes an epoch or `+45m`/`+1h` to `~/handoff/hold`; the
  steward pushes the deadline (bounded to `MAX_EXTEND` from now, so a wedged
  session can't dodge restarts forever), re-warns, and consumes the hold.
- **fire** — past the deadline *and* at a confirmed lull, restart. A busy pane at
  the deadline **waits** for the lull. The restart rotates `~/handoff/next.md` →
  `last.md` (or writes a stub) and — in `live` mode — types a graceful `/exit`
  then relaunches `keeper-launch.sh` in the same tmux session.

**Two independent off-switches, both defaulting safe.** The whole feature is off
(`KEEPER_RELAY_STEWARD` unset), and even when on the restart action is
`KEEPER_RELAY_STEWARD_RESTART=dry` — it logs `would_run` + rotates the handoff +
resets, and never touches the pane. `live` is opt-in on top of the feature flag,
and is guarded by **both** that env **and** a freshly-confirmed idle gate.

### 9. Board event relay — prompt decisions & done-transitions (ON by default)

Board **transitions** ride a separate queue class (`author=="board-event"`).
Detection is described below; **delivery gating is the attribution gate in §10** —
transitions are QUEUE work and wait for the keeper to be *between tasks*, exactly
like a new-item `[board]` notice. (History: they were once exempt from the quiet
gate on the theory that a decision is what the keeper is waiting on; operator
2026-07-29 removed that — a momentary gap between tool calls is not a lull, and the
exemption echoed the keeper's own writes back mid-task. Only a **ping**
(`exempt_quiet`, KEEPER-TASK-comms-ping) skips the gate now, delivering on the idle
gate alone.)

Detection **reuses the journal's per-item fingerprint map** (`state["todo_fp"]`) as
the *before* snapshot — the event watcher runs **before** `journal_cycle` advances
it, so there is no second differ. Two classes, off the same per-cycle board read:

- **proposal decision** — a `type=proposal` item whose note carries an
  `[accepted]`/`[declined]` prefix (`PROP_MARK`, case-insensitive). One event per
  `(id, decision)`: `accepted → declined` re-fires (new token); a plain note/text
  edit does not. A proposal that is *both* decided and flipped done emits **one**
  decision event, not also a done.
- **done-transition** — any **non-bookmark** item whose status changes to `done`
  from `open`/`doing`. An item that *appears* already-done never fires (the add
  watcher owns new items). `type=operator` dones are labelled distinctly (`⚑`).

One idle-gated line per cycle batch, `+N more` on overflow:

```
[board] ✔ p8 ACCEPTED "adopt the widget" · ✕ p9 DECLINED "…" · ✓ t44 done · ⚑ o11 done — check ~/todo.json
```

Dedup is by stable event id (`evt:<id>:<accepted|declined|done>`) in a persisted
ring; **first run seeds present decisions silently** (no storm on deploy).

**Self-suppression — the keeper must not be pinged about its own hand.** Creates are
already suppressed by `via:"keeper"` (the add watcher's protocol); **transitions**
have their own signal. On every mutation of an existing item, the `todo` CLI
(`todo-cli`, `~/.local/bin/todo`) appends one `{ts, ids, op}` record to
`~/.local/state/todo-cli/last-write.json`. When the relay detects a transition
whose id has a **fresh** marker (younger than `EVENT_SUPPRESS_WINDOW` = 90 s, and
predating this read), it attributes the change to the keeper, **drops the event**
(still marking it seen so it never fires later), and audits a `board-event-suppress`
row. A **console-originated** change leaves no marker and **fires normally**; a
**stale** marker (older than the window) is not trusted and fires. The relay never
writes the board, so it has nothing of its own to suppress.

### 10. Attribution gate — board delivery is timed to the KEEPER, not a clock

Board work (`author=="board"` new-item notices **and** `author=="board-event"`
transitions) is QUEUE work: it delivers only when the keeper is **between tasks**.
This **replaces the old quiet-window lull** (operator 2026-08-31: the lull "hasn't
worked out", and time periods are out — the 2026-07-29 ruling already turned the
max-hold cap OFF by default). The pane is confirmed idle first (turn complete, box
empty — the same gate every injection uses); *between-tasks* is the additional test,
and it is decided by the keeper's own **task/status attribution**, in order:

1. **board `doing` status** — nothing in flight ⇒ between tasks (deterministic);
2. **printed `status=done|blocked`** in the just-finished reply ⇒ release (even with
   something still `doing`), matched by `STATUS_DONE_RE`;
3. **hugpy-agent judge** (ambiguous path only — something `doing`, no printed
   terminal): `hugpy-agent run` is asked to `final_answer` **RELEASE**/**HOLD** on
   the keeper's reply; released only on a clean RELEASE. Opt-in
   (`KEEPER_RELAY_ATTRIB_AGENT`), consulted **at most once per distinct reply**
   (cached by a fingerprint of the reply in `state["attrib_judge"]`), and
   **fail-closed**: any error/timeout/unclear verdict HOLDS.

Everything **fails closed**: an unreadable board HOLDS (never deliver blind); a held
item stays `open` on the board as the **durable backstop** (the keeper's launch board
read and the console board catch it). The **fallback timer**
(`BOARD_MAX_HOLD_SECS`, default `0` = off) is the only time escape, per-station and
enabled only if one demonstrably starves. **Exempt from the gate** (idle gate alone):
direct operator lines, the `[lull]` digest, the steward, `[peer-msg]`
(`author==PEER_AUTHOR`), and **pings** (`exempt_quiet`).

## Config

Read from the environment, else `/home/ubuntu/.env` (0600 ubuntu:ubuntu).

| Key | Default | Meaning |
|---|---|---|
| `STATION_DISCORD_API` | — | operator comms channel. **The only source.** (legacy name `BLACKBIRD_DISCORD_API` still read, deprecation-logged) |
| `KEEPER_RELAY_ALLOW` | `devinthadude_` | comma-separated exact authors |
| `KEEPER_RELAY_TARGET` | `keeper:0.0` | tmux target |
| `KEEPER_RELAY_SOCKET` | `console` | tmux `-L` socket |
| `KEEPER_RELAY_LIVE` | unset | `1` == inject for real |
| `KEEPER_RELAY_INBOX` | `/home/ubuntu/relay-inbox` | where fetched images are saved |
| `KEEPER_RELAY_STEWARD` | `off` | `on` enables the session steward (§8) |
| `KEEPER_RELAY_STEWARD_RESTART` | `dry` | the restart action: `dry` (log+reset) or `live` |
| `KEEPER_RELAY_STEWARD_CADENCE_MIN` | `120` | restart cadence floor, minutes |
| `KEEPER_RELAY_STEWARD_WARN_LEAD_MIN` | `15` | warning-to-earliest-fire lead, minutes |
| `KEEPER_RELAY_STEWARD_THRESHOLD_BYTES` | `300000` | transcript growth that arms a restart |
| `KEEPER_RELAY_STEWARD_FLOOR_BYTES` | `50000` | below this growth = idle session, skip silently |
| `KEEPER_RELAY_STEWARD_MAX_EXTEND_MIN` | `60` | ceiling on a single hold extension, minutes |
| `KEEPER_RELAY_STEWARD_TRANSCRIPT_DIR` | keeper `~/.claude/projects/<slug>` | newest `*.jsonl` = live session |
| `KEEPER_RELAY_BOARD_EVENTS` | `on` | `off` disables the board event relay (§9) |
| `KEEPER_RELAY_BOARD_MAX_HOLD_SECS` | `0` (off) | attribution-gate fallback timer (§10): force a starved board item out after N s. `0` = never. Also settable via the `cfg-relay` bookmark. |
| `KEEPER_RELAY_ATTRIB_AGENT` | unset | `1` enables the hugpy-agent between-tasks judge on the ambiguous path (§10) |
| `KEEPER_RELAY_ATTRIB_AGENT_BIN` | `hugpy-agent` | the agent binary invoked as `<bin> run <prompt> --policy readonly` |
| `KEEPER_RELAY_ATTRIB_AGENT_TIMEOUT` | `45` | seconds before the agent judge times out (→ fail-closed HOLD) |
| `KEEPER_RELAY_TODO_CLI_MARKER` | `~/.local/state/todo-cli/last-write.json` | the todo-cli self-suppression marker the relay reads |

`BLACKBIRD_TODO_API` is the **todo bridge, not this relay's source.** Endpoint
values are never logged, printed, or committed; `--status` masks them and fetch
errors report only the exception type, never the URL.

## Enabling (keeper does this after review)

```sh
# 1. watch it in dry-run first
sudo systemctl start keeper-relay
journalctl -u keeper-relay -f
tail -f ~/.local/state/keeper-relay/audit.jsonl

# 2. only when the audit log looks right, go live
sudo systemctl edit keeper-relay      # add: [Service]
                                      #      Environment=KEEPER_RELAY_LIVE=1
sudo systemctl restart keeper-relay
sudo systemctl enable keeper-relay    # survive reboot
```

## Tests

```sh
cd /srv/share/projects/blackbird/keeper-relay && python3 -m unittest test_keeper_relay -v
```

315 offline tests. No network, no real `send-keys`, no live pane — `tmux`,
`urlopen`, and the attachment opener are all mocked. Pane fixtures are verbatim
captures from this box (live keeper busy footer; real idle TUI footer). Covers
sanitization, the fail-closed filter, cursor persistence/first-run-from-now,
queue-on-busy, at-most-once, the slow-render regression, the wrapped-line box
check, the image-attachment fetch (happy path, every fail-closed gate,
per-message cap, remote-filename containment, prune retention), the board
watcher + journal, the board **event** relay (§9: decisions, done-transitions,
operator-done labelling, the non-fire cases, dedup + re-decision, self-suppression
via the todo-cli marker incl. stale/console/absent, batching + overflow, the
idle-gate delivery exemption past a quiet-held notice), the lull relay, and the
session steward (arm/floor-skip/seed, warn format + idle gate, lull-gated fire with
busy-pane defer, hold parse + bound + clear, handoff rotation, dry
reset-without-pane-touch, live path guarded, feature-off inertness).

## Residual risk — what is NOT mitigated

1. **The accepted core, restated: a compromised `devinthadude_` Discord account
   is arbitrary code execution on this station.** Discord account security is now
   station security. No control here touches this.
2. **Prompt injection is not mitigated and cannot be.** Sanitization governs the
   *shape* of text, never its *meaning*. A well-formed, control-char-free
   sentence from the allowlisted author is passed through verbatim and is exactly
   as dangerous as its content. The `[relay:...]` tag is provenance, **not** a
   trust boundary — and a message can contain the literal text `[relay:...]`, so
   the tag is spoofable within the message body. Do not build downstream logic
   that trusts it.
3. **No authentication of the author beyond the channel's own claim.** The relay
   trusts the `author` field the bridge reports. If the bridge is compromised or
   spoofable, the allowlist is worthless. It is a filter, not an authenticator —
   there is no signature to verify.
4. **TOCTOU on the idle check.** ~ms between reading the pane and typing. A human
   typing at the web console in that window gets the relay's text spliced onto
   their line. Unclosable without cooperation from inside the pane; the window is
   small and the failure is visible (garbled line), not silent.
5. **UI-string brittleness.** See "Failure modes" above. A Claude Code upgrade
   that changes the footer or prompt glyph can break detection, and one specific
   change (removing the busy marker) would break toward false-idle. There is no
   stable API for "is this agent busy" — this is scraping, and it should be
   re-verified on upgrade.
6. **Idle is not the same as "safe to interrupt."** The relay proves the pane is
   at an empty prompt, not that the keeper is *finished*. A message injected
   between two turns of a multi-step task becomes part of that task's context.
   Observed live: the keeper's box reads idle **while background agents are still
   running** — injecting then starts a new main-thread turn concurrent with that
   background work. The relay has no way to see a "finished" state, because the
   TUI does not expose one.
7. **At-most-once means operator messages can be silently lost** (see above).
   The audit log records every such loss as `inject-unconfirmed`, but the
   operator gets no delivery receipt on Discord — from their side it just looks
   ignored.
8. **A stale `--dry-run` reading can mislead.** The pane verdict in `--status` is
   a snapshot; the keeper's state changes constantly.
9. **The relay does not rate-limit.** A flood from the allowlisted author queues
   up to 20 and drops the oldest with an audit record; it will keep feeding the
   keeper one message per idle window. There is no per-hour cap.
10. **Fetched images are attacker-influenced bytes on disk.** The controls above
    bound *what* lands (an ≤8 MiB file whose magic matches an image type, from a
    Discord CDN host, under a name we chose) — but the pixel content is whatever
    the allowlisted sender uploaded, and the keeper is told to `Read` it. This is
    the same accepted core as the text path (control of the account is control of
    the station); the fetch just extends it from text to images. It does not
    execute the file, render it, or trust the remote filename — but a malicious
    image parser downstream of the keeper's `Read` is outside this relay's reach.
