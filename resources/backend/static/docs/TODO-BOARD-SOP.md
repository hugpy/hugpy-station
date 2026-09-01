# The to-do board — standard operating procedure (fleet SOP)

> Vocabulary: **locus**, **seat**, **backend**, **model** (and A / B / C) are used here in the fixed senses of [NOMENCLATURE.md](NOMENCLATURE.md).

**Audience: every station keeper.** This is the contract for `~/todo.json` — the
shared task board between you, the operator, and the fleet console. The file IS
the interface: the console edits it host-side, you edit it in-VM, and the
keeper-relay watches it. Treat it as the single source of truth for what you
owe, what you decided, and what you shipped.
*(Fleet SOP v1, 2026-07-18 — maintained by the blackbird keeper in
`console-ui/docs/TODO-BOARD-SOP.md`; propose changes there.)*

## 1. The file and its schema

`~/todo.json`, schema `todo.v1`:

```json
{"schema": "todo.v1", "items": [
  {"id": "t12", "type": "todo|request|bookmark|operator|proposal",
   "text": "...", "note": "...", "status": "open|doing|done",
   "by": "<locus>-keeper|operator|user|...", "ts": 1784300000}
]}
```

- **Sign explicitly (nomenclature ruling 2026-08-27).** A keeper signs
  `by: "<locus>-keeper"` — the host's keeper signs `host-keeper`, hugpy's
  signs `hugpy-keeper`. The operator is `operator` (console) or `user`
  (items filed on their behalf); host agents comment as `A`/`B`. The bare
  `keeper` is LEGACY: readers still accept it as "this board's own keeper",
  writers must never mint it again. Same rule for `via` (who wrote):
  `via: "<locus>-keeper"`, never bare `keeper`. See docs → nomenclature.
- **Keep the JSON valid.** The console refuses a malformed file rather than
  repairing it; a broken board is an outage for the operator.
- Extra fields survive round-trips (the console passes unknown fields through
  verbatim) — richness like `pros`/`cons`/`rec`/`comments`/`via` is safe.
- **Write through a validating tool, not by hand.** Use the `todo` CLI where
  provisioned. If you must write raw JSON, re-validate before saving.

## 2. Item types — what each one means

| type | glyph | meaning | who creates |
|---|---|---|---|
| `todo` | ☑ | a unit of work on your queue | anyone |
| `request` | ✋ | an ask directed at the keeper | operator (console/Discord), peers |
| `bookmark` | 🔖 | a checkpoint: stable build, shipped commit, verified state | keeper, at every stable point |
| `operator` | ⚑ | an action ONLY the operator can take (host-side/root step) | keeper |
| `proposal` | ⚖ | a decision you want FROM the operator, with pros/cons/rec | keeper |

- **Proposals** carry `pros: []`, `cons: []`, `rec: "..."`. The console renders
  accept/decline buttons. A decision is recorded as **status `done` + a
  `[accepted] ` or `[declined] ` prefix on the note** (your original note text
  is preserved after the marker). Do not self-decide your own proposal — the
  decision is the operator's.
- **Operator items** should carry explicit structure when useful: `worker`
  (which box/account), `task` (inspect/aggregate/apply/verify/…), `cmd`
  (copyable command(s) — string or list). Make the operator's action
  copy-paste-runnable.
- **Bookmarks** are your rollback map: commit hash or path in the `note`,
  enough detail that a future keeper (or you after a restart) can reconstruct
  state from it.

## 3. Ids — the rules that keep writes honest

- **Ids must be unique across the whole board, regardless of type.** By-id
  ops (accept, status, note, comments, delete) hit the FIRST match; a
  duplicated id silently mis-routes writes. (Fleet incident 2026-07-17: a
  proposal sharing `t33` with a done request made operator accepts "not
  stick" and clobbered a note.)
- Namespaces by convention: console-minted items get `t<N>`; keeper-authored
  use a distinct prefix per type — `p<N>` proposals, `o<N>` operator items,
  `bm<N>` bookmarks. **Never hand-mint a `t`-prefixed id** — that's the
  console allocator's namespace and you WILL collide with it.
- Reserved ids: `cfg-relay` (the relay settings carrier — see §6). Reserved
  ids are keeper-minted; the console can only update them.

## 4. Status discipline

`open → doing → done`, kept current. `doing` means you are actively on it this
session. Everything you finish gets `done` plus a **note or comment recording
what shipped and how it was verified** (commit, deploy receipt, test counts).
The board is the audit trail: "done" with no evidence is not done.

## 5. Comments — the discussion layer

Every item can carry `comments: [{by, ts, text}]`. Use them to answer the
operator IN-THREAD (the answer lives where the question was asked), to record
decisions, and to hand off context. The operator's comments reach you via the
relay; your comments are visible in the console immediately.

## 6. How items reach the keeper (delivery tiers)

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

## 7. Standing intake — the loop you owe

- **Check the board at session start and whenever you assess work.** Directives
  and tasks land there without interrupting you; the board is your queue, not
  your memory.
- `request` items are operator asks: assess → (assign if substantive) →
  verify → record the outcome on the item → `done`.
- File `proposal` cards BEFORE building anything that changes core behavior,
  is hard to revert, or re-scopes an ask. Build only what was accepted.
- File `operator` items for anything needing host/root action — with a
  copyable `cmd`. Never sit on a blocker silently.
- Bookmark every stable build/promote/publish with its receipt.

## 8. Honesty rules (non-negotiable)

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

## 9. Templates

The console's 📖 docs drawer carries literal 1:1 JSON examples of every item
type ("Write items like these") — copy their shape. The proposal card p6 there
is the canonical decision-request form.

## 10. Anti-patterns (each one has bitten a real keeper)

- Hand-minting `t`-ids → id collision with the console allocator (§3).
- Deciding your own proposal → the decision belongs to the operator.
- "done" with an empty note → unverifiable, treated as not done.
- Writing settings/config into ad-hoc board fields → use the reserved
  carriers (§6) or file an operator item; the update whitelist will silently
  eat anything else.
- Leaving test residue on the board → the operator sees ghosts; always clean.
- A malformed board file → the console goes dark on it; validate every write.
- **Ad-hoc load→edit→dump scripts on the live board** → a lost-update race
  with concurrent console writes; a real operator request was destroyed 16 s
  after filing this way (2026-07-18, by a keeper's own cleanup script). Bulk
  edits go through the validating CLI (or take the shared board lock), never
  a bare `json.dump`.
