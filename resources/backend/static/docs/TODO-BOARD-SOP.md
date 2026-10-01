# The to-do board — standard operating procedure (fleet SOP)

> Vocabulary: **locus**, **seat**, **backend**, **model** (and A / B / C) are used here in the fixed senses of [NOMENCLATURE.md](NOMENCLATURE.md).

**Audience: every station keeper.** This is the contract for `~/todo.json` — the
shared task board between you, the operator, and the fleet console. The file IS
the interface: the console edits it host-side, you edit it in-VM, and the
keeper-relay watches it. Treat it as the single source of truth for what you
owe, what you decided, and what you shipped.
*(Fleet SOP v2, 2026-09-18 — canonical source
`resources/backend/static/docs/TODO-BOARD-SOP.md` in the station-app tree;
propose changes there.)*

## 1. Pick the lane — the six item types

Choose the type first; it decides the tab, the id-prefix, and the note shape.

| type | tab · glyph | id | when to use / when NOT | required fields | note template |
|---|---|---|---|---|---|
| `todo` | queue · ☑ | `t<N>` | a unit of work on YOUR queue — incl. anything the operator MIGHT want done. NOT for asks the operator must action (that is `operator`). | `text`, `status`, `by` | `SCOPE: … — DONE: …` |
| `request` | queue · ✋ | `t<N>` | an ask directed AT the keeper (operator or a peer filed it). NOT a self-note — that is a `todo`. | `text`, `by` | `ASK <who>: … — DONE: …` |
| `bookmark` | 🔖 | `bm<N>` | a checkpoint at every stable build / shipped commit / verified state. NOT a wish or a plan. | `text`, `note` (id + receipt), `by` | `SHIPPED <UTC>: … — VERIFIED: … — ROLLBACK: …` |
| `operator` | ⚑ | `o<N>` | ONE concrete action only the operator can take — behind a privilege you lack (root/sudo, a credential, a physical/account step), with a copyable `cmd`. NOT the operator's wish-list (those are `todo`). | `text`, `note`, `by` | title `[root]\|[OP] …` + `WHY/WHO/GATE/DO/VERIFY/EXPECT/RUN(/ROLLBACK)` — [BOARD-ITEM-FORMAT.md](BOARD-ITEM-FORMAT.md) |
| `proposal` | ⚖ | `p<N>` | a decision you want FROM the operator, with pros/cons/rec. NOT something you may decide yourself. | `text`, `note`, `by` | `PROBLEM/OPTIONS/REC/DECISION/SKETCH` — [BOARD-ITEM-FORMAT.md](BOARD-ITEM-FORMAT.md) |
| `direction` | queue · 🧭 | `d<N>` | a standing ruling / invariant from the operator — a determination that governs later work ("always X", "never Y"). NOT a one-off task and NOT your own opinion; only the operator's word becomes a direction. | `text`, `note` (the ruling), `status`, `by` | `RULING (<who, date>): "<quote>" — STATE: <APPLIED\|FOLDED\|CLOSED where>` |

`direction` has no dedicated console tab today; it renders in the **queue**
lane. It is nonetheless a live, first-class type (~20 items in flight) — a
persisted operator ruling you carry forward, distinct from a `proposal` (a
question) or a `request` (a task).

## 2. Note-format templates — one per type

Each type earns its keep with a note in a fixed shape, so a reader (operator,
peer, or you after a restart) gets the whole picture from the note alone.

- **todo** — `SCOPE: <what/where> — DONE: <observable check>`
  > `SCOPE: rotate toolserver cert on ae — DONE: openssl s_client shows the new expiry`
- **request** — `ASK <who>: <exact ask> — DONE: <what closes it>`
  > `ASK hugpy-keeper: rebuild the model index — DONE: /health lists all 6 models`
- **bookmark** — `SHIPPED <UTC>: <build/commit id> — VERIFIED: <evidence> — ROLLBACK: <prev id/path>`
  > `SHIPPED 2026-09-18T06:20Z: station 1.0.106 (a1b2c3d) — VERIFIED: deb installs, /health 200 — ROLLBACK: 1.0.105 (9f8e7d6)`
- **operator** — the sectioned shape in [BOARD-ITEM-FORMAT.md](BOARD-ITEM-FORMAT.md) §1 (title `[root]|[OP] …`; `WHY` `WHO` `GATE` `DO` `VERIFY` `EXPECT` `RUN` `ROLLBACK`, one fenced ```bash block per action and `RUN` = one complete copy-paste script). The toolserver refuses an operator item without it.
- **proposal** — [BOARD-ITEM-FORMAT.md](BOARD-ITEM-FORMAT.md) §2: `PROBLEM` `OPTIONS` (`A) … — + pro / − con`) `REC` `DECISION: pending (operator)` `SKETCH` (fenced). `pros`/`cons`/`rec` fields, where a board carries them, mirror the note.
- **direction** — `RULING (<operator determination, date>): "<quote>" — STATE: <APPLIED|FOLDED|CLOSED where>`
  > `RULING (operator, 2026-08-27): "keepers always sign <locus>-keeper" — STATE: APPLIED in TODO-BOARD-SOP §3`

*(Operator items and proposals are checked on write by the toolserver — see
BOARD-ITEM-FORMAT.md §5. The other types are not checked.)*

## 3. Signing — who wrote it

**Sign explicitly (nomenclature ruling 2026-08-27).** A keeper signs
`by: "<locus>-keeper"`. The bare `keeper` is LEGACY: readers still accept it as
"this board's own keeper", but writers must **never mint it again**. The same
rule holds for `via` (who physically wrote): `via: "<locus>-keeper"`, never bare
`keeper`.

| authoring context | `by` / `via` |
|---|---|
| the host's keeper | `host-keeper` |
| hugpy's keeper | `hugpy-keeper` |
| any locus keeper `<L>` | `<L>-keeper` |
| the operator, from the console | `operator` |
| filed on the operator's behalf | `user` |
| a host agent commenting | `A` / `B` |

Non-conformant values to avoid: bare `keeper` (legacy, read-only), `claude` /
`assistant` / a model name, a plain hostname without `-keeper`, or an empty
`by`. If in doubt, use `<your-locus>-keeper`. See docs → nomenclature.

## 4. Lifecycle & status

`open → doing → done`, kept current. `doing` means you are actively on it this
session. Everything you finish gets `done` plus a **note or comment recording
what shipped and how it was verified** (commit, deploy receipt, test counts).
The board is the audit trail: "done" with no evidence is not done.

- **Priority.** `high` = blocks the operator or a ship / actively breaking;
  `medium` = should land this cycle, nothing waits on it; `low` (default) =
  backlog. **If everything is high, nothing is.**
- **Title convention.** `<area> — <imperative>`, e.g.
  `toolserver — rotate the TLS cert`.
- **Proposals** carry `pros: []`, `cons: []`, `rec: "..."`. The console renders
  accept/decline buttons. A decision is recorded as **status `done` + a
  `[accepted] ` or `[declined] ` prefix on the note** (your original note text
  is preserved after the marker). Do not self-decide your own proposal — the
  decision is the operator's.
- **Directions** are a standing state, not a task you burn down: mark the note
  **`APPLIED`** (folded into a doc/behavior, cite where), **`FOLDED`** (merged
  into another ruling/doc), or **`CLOSED`** (superseded/retired). Keep the
  operator's quote verbatim.

## 5. Comments — the discussion layer

Every item can carry `comments: [{by, ts, text}]`. Use them to answer the
operator IN-THREAD (the answer lives where the question was asked), to record
decisions, and to hand off context. The operator's comments reach you via the
relay; your comments are visible in the console immediately.

## 6. The file, schema & ids (mechanics)

`~/todo.json`, schema `todo.v1`:

```json
{"schema": "todo.v1", "items": [
  {"id": "t12", "type": "todo|request|bookmark|operator|proposal|direction",
   "text": "...", "note": "...", "status": "open|doing|done",
   "by": "<locus>-keeper|operator|user|...", "ts": 1784300000}
]}
```

- **Keep the JSON valid.** The console refuses a malformed file rather than
  repairing it; a broken board is an outage for the operator.
- Extra fields survive round-trips (the console passes unknown fields through
  verbatim) — richness like `pros`/`cons`/`rec`/`comments`/`via` is safe.
- **Write through a validating tool, not by hand.** Use the `todo` CLI where
  provisioned. If you must write raw JSON, re-validate before saving.
- **Ids must be unique across the whole board, regardless of type.** By-id
  ops (accept, status, note, comments, delete) hit the FIRST match; a
  duplicated id silently mis-routes writes. (Fleet incident 2026-07-17: a
  proposal sharing `t33` with a done request made operator accepts "not
  stick" and clobbered a note.)
- Namespaces by convention: console-minted items get `t<N>`; keeper-authored
  use a distinct prefix per type — `p<N>` proposals, `o<N>` operator items,
  `bm<N>` bookmarks, `d<N>` directions. **Never hand-mint a `t`-prefixed id** —
  that's the console allocator's namespace and you WILL collide with it.
- Reserved ids: `cfg-relay` (the relay settings carrier — see §7). Reserved
  ids are keeper-minted; the console can only update them.

## 7. How items reach the keeper (delivery tiers)

The keeper-relay watches the board and injects notices into your session:

1. **Operator direct messages** — prompt (a conversation, never queued long).
2. **Board events** — proposal accepts/declines and done-transitions ping you
   PROMPTLY (they are what you're waiting on). Your own CLI transitions
   self-suppress — you are not pinged about yourself.
3. **New items** (`[board] +…`) — delivered after a **quiet period** of keeper
   inactivity (default 10 min) so queued work doesn't interrupt a live turn;
   a **max-hold cap** (default 1 h) guarantees delivery even if you never go
   quiet.
4. **Lull digest** — a catch-all at idle for anything unseen (stale comments,
   missed items), on a cooldown.

The windows are per-VM, set by the **`cfg-relay` bookmark** (note =
`{"board_quiet_secs": N, "board_max_hold_secs": M}`) — the operator tunes it
from the console's ⏱ knob; you may read it but should not fight the
operator's setting.

## 8. Standing intake — the loop you owe

- **Check the board at session start and whenever you assess work.** Directives
  and tasks land there without interrupting you; the board is your queue, not
  your memory.
- `request` items are operator asks: assess → (assign if substantive) →
  verify → record the outcome on the item → `done`.
- File `proposal` cards BEFORE building anything that changes core behavior,
  is hard to revert, or re-scopes an ask. Build only what was accepted.
- File `operator` items ONLY for a blocker you cannot clear yourself — an action
  behind a privilege you lack (root/sudo on a box, a credential, a physical or
  account step) — with a copyable `cmd`. Never sit on a blocker silently.
  The ⚑ operator tab is the operator's inbox of YOUR asks, not a place to park
  the operator's own potential todos: those go on the queue as `todo`.
- Honor open `direction` items: they are standing rulings that constrain the
  work above. When one becomes real in a doc or behavior, mark it `APPLIED`.
- Bookmark every stable build/promote/publish with its receipt.

## 9. Honesty rules (non-negotiable)

- **Verify-after-write**: a backend `ok:true` is not proof a field landed —
  re-read and check when it matters (the update op silently drops
  non-whitelisted fields).
- **Never mark shipped what you didn't verify.** Deploy receipts, test counts,
  live probes — evidence goes in the note/comment.
- **Don't rewrite history**: decided proposals keep their cards (the pros/cons
  ARE the record); notes append rather than replace when the old text carries
  context; the history journal (`~/todo-history.jsonl`, where provisioned) is
  append-only.
- Test items on a live board are prefixed clearly (e.g. `KEEPER-TEST … DELETE`)
  and removed the moment the test ends.

## 10. Anti-patterns (each one has bitten a real keeper)

- Hand-minting `t`-ids → id collision with the console allocator (§6).
- Deciding your own proposal → the decision belongs to the operator.
- "done" with an empty note → unverifiable, treated as not done.
- Signing bare `keeper` or a model name → non-conformant; use `<locus>-keeper` (§3).
- Marking everything `high` → priority stops meaning anything (§4).
- Writing settings/config into ad-hoc board fields → use the reserved
  carriers (§7) or file an operator item; the update whitelist will silently
  eat anything else.
- Leaving test residue on the board → the operator sees ghosts; always clean.
- A malformed board file → the console goes dark on it; validate every write.
- **Ad-hoc load→edit→dump scripts on the live board** → a lost-update race
  with concurrent console writes; a real operator request was destroyed 16 s
  after filing this way (2026-07-18, by a keeper's own cleanup script). Bulk
  edits go through the validating CLI (or take the shared board lock), never
  a bare `json.dump`.
