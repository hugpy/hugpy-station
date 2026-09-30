# Fleet console — UI guide

> Vocabulary: **locus**, **seat**, **backend**, **model** (and A / B / C) are used here in the fixed senses of [NOMENCLATURE.md](NOMENCLATURE.md).

What every part of the console is, how to use it, and the use-case it was
designed for. This is the **user end**: you, at the console, driving the fleet.

The console is one page. Across the top: a **VM tab strip** and an **actions
bar**. The middle is the **stage** (terminals). The right side is where a
**drawer** opens (design / to-do / bugs / search). One auth prompt covers
everything.

---

## VM tabs & power

Each running or stopped VM is a tab; click to select. Stopped VMs are dimmed but
selectable. Power actions live in the actions bar: **▶ start / ⏹ stop /
⟳ restart** (protected VMs refuse stop/restart by policy). If the console can't
read fleet state it says so loudly (a stale-state warning) rather than showing
you stale power dots as if they were live — **by design: a console that can't
see the fleet must never pretend it can.**

**Use case:** one place to see and power the whole fleet, with the truth about
what it can and can't currently see.

**Removing a locus (1.0.80).** In the 🖥 stations drawer each VM / ssh host has
two ways out: **◌ hide locus** drops it from the tab strip and dropdown while
leaving the machine untouched (it stays in the drawer greyed, with **◉ unhide**;
stored in `~/.config/hugpy-station/hidden-loci.json`), and the destructive
**🗑 delete** (LXD guest, irreversible) / **✕ remove** (ssh host, also archives
the locus centrally). Hide when you just want a quieter strip.

---

## Terminals: keeper vs shell, and the side dock

Each VM has two terminals, toggled in the actions bar:

- **keeper** — the VM's Claude session (the keeper). The green dot shows whether
  the keeper is live; **☉ launch keeper** starts it if not.
- **shell** — a plain shell in the VM.

**◫ shell aside** docks the shell *beside* the keeper instead of replacing it,
so you can watch both. The keeper keeps the stage (it's the primary workspace);
the shell borrows a narrower slice. Switching tabs or modes is **lossless** —
the terminals never reload, so scrollback and sockets survive.

**Use case:** work with the keeper as your primary surface while keeping an eye
on a shell — without losing either session's state.

*(Coming: a view-only attach so glancing at another keeper's screen can't steal
your terminal's geometry — operator task o3.)*

---

## ⬚ Design drawer — wireframe exchange

A lightweight wireframe editor. Sketch a UI on a 1280×800 canvas (boxes with
roles: container, nav, sidebar, button, input, text, image, list, note).

- **→ VM** hands the design to the locus (toolserver `canvas/put`, kind
  `design`) and nudges its keeper with a `[canvas]` board request.
- **← VM** pulls back what the keeper wrote or revised (`canvas/get`). Edits on
  either side land live — the drawer follows the DB change bus.
- **⧉** copies it as a fenced block to paste into the keeper chat.

**Use case:** hand a layout to the keeper and get one back — the same sketch you
then have it build in the repo. Design and build share one artifact instead of
drifting.

### The canonical example — a real operator sketch, 1:1

This is an actual operator wireframe from this station (2026-07-17), verbatim —
a 3D-identity screen: nav bar, sidebar with active/session/settings, name and
version fields, a turnable viewport beside the approved 3D id, uploaded and
canonical panels below. It is exactly what the exchange format looks like in
flight: paste this block into the drawer via the toolbar's 📋 import and it
loads onto the canvas; **→ VM** hands it to the keeper; the keeper's revision
comes back with **← VM**.

```
{"schema":"wireframe.v1","canvas":{"w":1280,"h":800},"shapes":[{"code":"a","role":"nav","label":"Nav","x":0,"y":0,"w":1280,"h":60},{"code":"b","role":"sidebar","label":"Sidebar","x":0,"y":60,"w":300,"h":720},{"code":"c","role":"nav","label":"active","x":0,"y":80,"w":80,"h":40},{"code":"d","role":"nav","label":"session","x":80,"y":80,"w":100,"h":40},{"code":"f","role":"nav","label":"settings","x":180,"y":80,"w":100,"h":40},{"code":"g","role":"nav","label":"Nav","x":300,"y":60,"w":940,"h":60},{"code":"h","role":"nav","label":"Nav","x":760,"y":100,"w":20,"h":20},{"code":"i","role":"nav","label":"name or new","x":300,"y":120,"w":440,"h":40},{"code":"j","role":"nav","label":"Nav","x":820,"y":140,"w":20,"h":20},{"code":"k","role":"nav","label":"version or new","x":740,"y":120,"w":500,"h":40},{"code":"l","role":"nav","label":"turnable","x":300,"y":160,"w":440,"h":320},{"code":"m","role":"nav","label":"Nav","x":660,"y":440,"w":20,"h":20},{"code":"n","role":"nav","label":"appproved 3d Id","x":740,"y":160,"w":500,"h":320},{"code":"o","role":"nav","label":"uploaded","x":300,"y":480,"w":440,"h":280},{"code":"p","role":"nav","label":"canonical","x":740,"y":480,"w":500,"h":280},{"code":"q","role":"container","label":"settings","x":0,"y":120,"w":280,"h":40},{"code":"r","role":"container","label":"Container","x":1000,"y":140,"w":20,"h":20},{"code":"s","role":"container","label":"Container","x":1000,"y":140,"w":20,"h":20},{"code":"t","role":"container","label":"Container","x":580,"y":160,"w":20,"h":20},{"code":"u","role":"container","label":"Container","x":580,"y":160,"w":20,"h":20}]}
```


---

## ⋔ Flow drawer — process-flow exchange

The Design drawer's sibling for **control flow** instead of layout: a flowchart
editor on the same 1280×800 canvas (schema `flow.v1` — see
`docs/FLOW-SCHEMA.md`). Nodes carry standard chart roles (start, end, process,
**decision** — drawn as a diamond, it is where ambiguity lives — io,
subprocess, and dashed `note` annotations); directed edges carry the branch
condition as a label (`yes` / `no` / `timeout` …), and loops render as real
back-edges (no DAG assumption). The document carries a lifecycle: **derived**
(the keeper machine-read it from source — what the code *does*) → **proposed**
(either side revised it toward what it *should* do) → **agreed** (your
sign-off; it is now a spec to implement against). Editing nodes or edges while
agreed automatically drops it back to proposed, and every save bumps the `rev`
counter so board notes can cite an exact revision ("as of flow rev 4").

- **→ VM** hands the flow to the locus (toolserver `canvas/put`, kind `flow`,
  bumps rev) and nudges its keeper with a `[canvas]` board request.
- **← VM** pulls back what the keeper derived or revised (`canvas/get`); a
  keeper's `canvas_put` also lands live over the DB change bus.
- **⧉** copies it as a fenced ` ```flow ` block; the toolbar's **📋** imports
  one the keeper printed — still handy for chat, no longer the only road.

**Use case:** the keeper derives the flow of a real program, you redraw the
loop the way it *should* work, mark it agreed — and the keeper implements
against that chart instead of prose.

---

## ☑ To-do board — the work queue

A shared board per VM (`~/todo.json`), the keeper's work queue. It **notifies
the keeper**: adding an item pings the keeper's terminal within seconds — but
only *between* turns, so **queuing work never interrupts work in progress.** The
whole point is to hand the keeper tasks without breaking its focus.

**The count on the ☑ button** shows the active VM's standing (still-open) work
at a glance — open todos/requests, open operator tasks, and proposals awaiting
your decision — so you know there's work pending without opening the drawer. It
tints when a proposal needs you, and turns **red** when any open item is
high-priority.

Inside, the board is **tabbed**, each tab with a live count:

- **queue** — todos & requests you've filed for the keeper.
- **⚖ proposals** — see below.
- **⚑ operator** — see below.
- **🔖 bookmarks** — stable builds / checkpoints the keeper pins.
- **🧭 direction** — standing operator rulings the keeper carries forward; no dedicated tab yet, they render in the **queue** lane.
- **done** — completed items.

**Item types:**

- **request** — ask the keeper to do something.
- **todo** — a plain task.
- **bookmark** — a pinned checkpoint (usually keeper-authored).
- **⚑ operator** — a task only *you* can do (a root step, a host edit). These
  can carry explicit structure so there's no ambiguity about what to run:
  - a **task** chip (INSPECT / APPLY / AGGREGATE / R&D / VERIFY …),
  - a **@worker** tag (which box or account it runs on),
  - a **command block** with a ⧉ copy button — copy the exact command(s) and
    paste them in the right terminal. `#` lines are caveats and aren't copied.
- **⚖ proposal** — a keeper-authored decision card: the subject, **pros/cons**
  columns, a keeper **recommendation**, and **accept / decline**. Use case: when
  a choice has real trade-offs, the keeper lays them out and you decide on the
  record instead of in scrollback.
- **🧭 direction** — a standing ruling or invariant from the operator ("always
  X", "never Y") that governs later work. Not a task and not the keeper's
  opinion — only the operator's word becomes a direction; the keeper carries it
  forward and marks where it was applied.

**Which lane** (full contract: [TODO-BOARD-SOP.md](TODO-BOARD-SOP.md)):

| type | tab · glyph | id | when to use / when NOT | note template |
|---|---|---|---|---|
| `todo` | queue · ☑ | `t<N>` | your own queue — incl. anything the operator MIGHT want. NOT an ask the operator must action. | `SCOPE: … — DONE: …` |
| `request` | queue · ✋ | `t<N>` | an ask AT the keeper (operator/peer filed it). NOT a self-note. | `ASK <who>: … — DONE: …` |
| `bookmark` | 🔖 | `bm<N>` | a stable build / shipped commit / verified checkpoint. NOT a plan. | `SHIPPED <UTC>: … — VERIFIED: … — ROLLBACK: …` |
| `operator` | ⚑ | `o<N>` | ONE action only the operator can take (privilege you lack), with a copyable `cmd`. NOT a wish-list. | `@worker: … task: …` + `cmd:` block |
| `proposal` | ⚖ | `p<N>` | a decision wanted FROM the operator: pros/cons/rec. NOT self-decided. | `PROBLEM: … / OPTIONS: A) … B) … / REC: …` |
| `direction` | queue · 🧭 | `d<N>` | a standing operator ruling/invariant you carry forward. NOT a one-off task or your own opinion. | `RULING (<who, date>): "<quote>" — STATE: <APPLIED\|FOLDED\|CLOSED>` |

**Priority:** any actionable item can carry `"priority": "medium"` or `"high"`
(absent = low, the default). High wears a red chip, medium amber, low none; open
items sort high → low within their tab, and clicking the chip cycles the
priority. *Honest caveat: persisting a priority written from the console still
needs a console-api whitelist update — until then the UI tells you when it
didn't stick. Keeper-written priorities always render.*

**💬 comments** thread under any item (and inside proposal cards). **❓ query**
on an operator task is a question whose answer unblocks it — its thread opens
automatically so you can answer in place. *(Operator-written comments persist
once the backend whitelist widens — operator task o8. Until then the box tells
you honestly if a comment didn't save.)*

**Every row/card also shows its own id** as a small, dim, monospace tag (e.g.
`t18`) near the front — deliberately quieter and untinted compared to the ref
chips below (which link to *other* items) — that copies to the clipboard on click.

**Ref chips** surface, right on the row/card face (no expanding needed), any git
commit or other board id an item's own text/note/comments cite — a note like
"SHIPPED LIVE (49f9029...)" or "see o8 / t18" gets a clickable `t18` chip and a
monospace `⧉ 49f9029` chip instead of leaving the reference buried in prose. An
id chip only ever appears when that id genuinely exists on the loaded board (a
stray word never chips); clicking it jumps you to that item — switching tab,
scrolling it into view, and flashing it briefly — across queue/proposals/
operator/bookmarks/done. Clicking a commit chip copies the full sha (there's no
code viewer to jump to). More than a handful collapse behind a **+N** that
expands on click. An item with nothing to cite shows no ref row at all.

**Note timeline** — keeper notes grow by appending dated updates (`… -- keeper
2026-07-22: …`), and a long-lived item's note becomes a wall of text. The drawer
renders that convention as a timeline instead: the note folds to its **newest**
update, with the earlier ones behind a **+N earlier** toggle (click to expand,
click again to fold), and each parsed update wears a small dim `keeper 07-22`
chip naming who/when opened it. Purely a rendering fold: the stored note is
untouched, segment text shows verbatim (the chip is additive), ref chips still
see the whole note even while folded, and a note that never used the convention
renders exactly as before — the splitter only fires on a ` -- ` followed by an
author + date opener, never on a mid-sentence dash.

Toolbar: **✨ tidy** (the keeper's local model proposes a deduped/prioritized
list — applied only after you approve), **🔗** (attach a Discord channel so
messages there queue items here), **⧉ brief keeper** (prompts the station's live keeper about the board — types the brief plus the open items into its terminal; copies the brief only if no keeper is running),
**↻** refresh.

**Use case:** a durable, shared work surface between you and the keeper — file
work without interrupting, see what's standing at a glance, decide the judgment
calls on proposal cards, and hand yourself the exact commands for the steps only
you can run.

### Write items like these — the canonical templates

Every example below is **the literal item from this board, 1:1** — the exact
JSON as it sits in `~/todo.json`, not a cleaned-up rendition. They are the
quality bar: an item carries its **evidence and its lifecycle**, so anyone
reading it later — operator, keeper, or a successor session — gets the whole
story from the card alone.

**⚖ proposal — the template the others follow.** The text is a *decision
question*, not a statement. Pros and cons are complete sentences carrying the
*why*, with measured numbers wherever they exist. The recommendation names a
default action and what unblocks it. The verdict (`[decided: …]`) lands in the
note **with the context the decision was made against** — the card stays as
the record of *why* the call went that way. This is p6, exactly as decided:

```json
{
  "id": "p6",
  "type": "proposal",
  "text": "Finder tool (t20): reimplement the collect/imports engine in portable stdlib, or depend on your abstract_utilities backend?",
  "note": "[decided: WRAP THE REAL abstract_utilities] Operator call 2026-07-17: '/srv/pyit/dev is the source' + 'yes it is heavy, it also works well and is generally faster than either human or llm hand sifting' -- canonical behavior over lightness, against keeper rec, operator's code operator's call. Pilot repointed mid-flight: vendor-sync {abstract_utilities,abstract_paths,abstract_modules} from ae into finder/vendor-src (read-only pull, bin/sync-from-ae.sh for updates), venv, thin adapter + same CLI/JSON contract. Contract tests now guard adapter + sync drift instead of reimplementation drift.",
  "status": "done",
  "by": "keeper",
  "ts": 1784279001,
  "pros": [
    "REIMPLEMENT (recommended): a small stdlib walker on each VM, no deps, mine to maintain, identical filter contract; ships as an agent CLI (JSON out) the keeper/subagents call INSTEAD of reading files, plus a console drawer later. AST import extraction is actually more correct than the source regex pass. Works on any keeper VM including ones with no /srv/pyit.",
    "REUSE abstract_utilities: byte-identical to what you already use (incl. semantic type-groups + SSH mode that could run centrally and reach VMs), zero semantic drift, and improvements flow back to your IDE.",
    "Either way the user (console drawer) and agents (CLI/JSON) share ONE engine -- the whole point is that an LLM stops grepping a tree by hand."
  ],
  "cons": [
    "REIMPLEMENT: two implementations of the same filters can drift from your IDE's; I'd pin the contract in tests and mirror your canonical kwarg names to limit it.",
    "REUSE: abstract_utilities is heavy, interdependent, ae/host-side, and YOUR code, not mine to own on every VM; it shells out to find and carries SSH plumbing the console doesn't need -- more surface for a shared fleet tool.",
    "Both need a host route (/api/vm/<vm>/finder) for the console half, same one-liner class as bugreport's o5; the agent CLI needs neither."
  ],
  "rec": "Reimplement the engine in stdlib as a per-VM agent CLI now (reversible, unblocks the token savings for subagents today), mirroring your canonical filter names so behavior tracks abstract_utilities; add the console drawer after you pick this fork and the host route lands. If you'd rather I wrap the real abstract_utilities instead (canonical behavior, flows back to your IDE), say so and I'll repoint the pilot -- nothing shipped is wasted since the CLI contract stays the same."
}
```

**⚑ operator — explicit enough to run without asking anything.** `worker`
(which box/account), `task` (the category chip), `cmd` (exact commands — the
`#` line is a caveat, excluded from the ⧉ copy), and a note carrying the *why*
and the safety rail. This is o6, exactly as filed:

```json
{
  "id": "o6",
  "type": "operator",
  "worker": "ae",
  "task": "inspect",
  "cmd": [
    "journalctl -b -u 'systemd-fsck@*' --no-pager | tail -30",
    "lsblk -f | grep -B1 -A1 2da3e5d3 || blkid | grep 2da3e5d3",
    "# then decide: unmounted manual fsck vs replace -- do NOT fsck a mounted filesystem"
  ],
  "text": "ae worker: failed systemd-fsck on disk uuid 2da3e5d3-457d-4dd7-8186-fed36edd6b33 -- a filesystem check that didn't pass",
  "note": "Found by keeper via new SSH access to ae (solcatcher@192.168.1.100) 2026-07-17. systemd-fsck@dev-disk-by-uuid-2da3e5d3... shows loaded/failed. A failed fsck is the kind of thing that gets loud later (mount degradation / data risk) -- worth a look when next on that box; likely needs an offline check or manual fsck. NOTE: the other failed units on ae (bluebook-transcribe, tdd-nightly) are EXPECTED -- operator confirmed the nightlies are paused. abstract-hugpy-worker is masked. Keeper touched nothing on ae.",
  "status": "done",
  "by": "keeper",
  "ts": 1784300672
}
```

**✋ request — the note is the lifecycle.** The text stays the ask, verbatim;
the note *evolves* — investigation → root cause → resolution, each with its
evidence, ending in the shipped state. This is t18, exactly as closed:

```json
{
  "id": "t18",
  "type": "request",
  "text": "design drawer loads in the sidebar but cannot be interacted with",
  "note": "FIXED LIVE 2026-07-17 (1209b82, deploy 20260717-074401). Root cause: CSS class collision -- the console's unscoped .hint rule (position:absolute; inset:0) grabbed wireframe.js's own 'wf-status hint' status line and stretched it invisibly over the whole drawer; every click landed on it. Broken since the repo seed. Fix: scope the rule to .stage. Reproduced + verified in headless chromium: 44/44 interaction steps across desktop/crowded/narrow/mobile.",
  "status": "done",
  "by": "user",
  "ts": 1784270100
}
```

**🔖 bookmark — a checkpoint someone can return to.** What + commit + receipt,
plus the verification evidence that made it stable. This is bm9, exactly as
pinned:

```json
{
  "id": "bm9",
  "type": "bookmark",
  "text": "finder repointed onto abstract_search -- 327MB->13MB, startup 0.85s->0.04s, keeper-verified",
  "note": "finder @ 2f05594, engine = operator's abstract_search share repo (editable install -- operator edits flow in live). 16/16 tests incl. import-isolation guard; live proofs re-run by keeper. Behavior deltas (intentional, from the revision): collect prunes default excludes (console-ui .js 5 vs 69 raw; --no-add restores), grep ignores comment-only matches, dir/type filters are exact path segments now. map retired pending upstream home in abstract_search (exit 2 with clear message). Imports extractor still abstract_paths code via surgical load; re-sync surprise corroborated: upstream content_utils is being retired to backs/.",
  "status": "done",
  "by": "keeper",
  "ts": 1784289000
}
```

**☑ todo — small, honest, current.** Deliberately the anti-template: a plain
task with its status kept true (`open`→`doing`→`done`) and the outcome added
to the note when there is one. Honest disclosure: every real todo on this
board grew into a decision record, so the ideal specimen is *smaller* than any
live one — the minimal shape is just:

```json
{"id": "t99", "type": "todo", "text": "rotate the staging cert",
 "note": "", "status": "open", "by": "user", "ts": 1784300000}
```

Add `"priority": "medium"` or `"high"` only when the urgency is real — absent
is the canonical low, and console-written priority persists only once the
console-api whitelist learns the field (keeper-written items carry it fine).

**💬 comments & ❓ queries** ride any of these: a `query` is the question whose
answer unblocks an operator task (answer it in the thread); `comments` are the
discussion that would otherwise be lost to chat scrollback. Field shapes
(`comments` is a list of `{by, ts, text}`; first real specimens land once the
o8 whitelist is applied):

```json
{"query": "which disk should replace 2da3e5d3 — the spare 4TB or a new order?",
 "comments": [{"by": "operator", "ts": 1784300500, "text": "spare 4TB — it's on the shelf"}]}
```

---

## 🛡 Steward drawer — keeper watchdog & standing directives

Per-VM keeper-restart safety net, plus config sections that live in the same
drawer. **off / dry-run / on** modes govern whether the steward may restart a
VM's keeper on transcript growth (a live restart is armed only behind a
confirm); a status readout shows transcript growth, the current **session
age** (with its cap, when one is set), and time to the next eligible restart.

A **session budget** slider sets the growth threshold that arms a restart,
labelled in estimated tokens out of a 1M ceiling (~4 bytes/token) with the
honest bytes figure alongside — the bytes number is what the steward actually
enforces. Drag to preview; the value is saved when you release the handle,
and the drawer always re-reads the host's answer, so what you see after a
save is what the steward actually holds (on a host that doesn't keep the
field yet, the slider simply snaps back).

A **max session age** select (next to the cadence control) sets a *time* gate
(board t89): once a session is older than the chosen age — 1h through 24h, or
**off** to disable it — the steward arms a lull-restart regardless of growth.
**off** is a real setting that clears any environment-set gate, not just "leave
alone."

A **Reset now** button asks the steward to restart this keeper at the next
quiet moment (a confirm gate, then *"reset requested — fires at the next
lull"*). It's consumed at most once by the relay and never interrupts work
mid-stride.

Both fields are two-generation aware, like the budget slider. Against a host
that predates a field, changes to the **other** controls still save (the
drawer drops just the unsupported field and notes which host update it's
waiting on), while a change to the field the host doesn't accept — the budget
against an o18-era host, the max-age against a t89-era host, or a reset the
route doesn't know — answers an honest *"host route not yet updated…"* and
snaps back, saving nothing. Nothing in the drawer is ever disabled while
that's true.

### ⚙ Agent deploy directive

A standing instruction, in your own words, for **how** the keeper should
deploy agents on this VM — e.g. "delegate fixes to lower-tier agents; keeper
orchestrates and verifies." Edit it here and **save**; the keeper reads and
honors it on its next pass. If the panel says *"set by keeper"* instead of
showing an editable box, the keeper hasn't minted this VM's directive slot
yet — nothing is broken, there's just nothing to edit until it does.

---

## 🐞 Bugs drawer — log triage & health

The VM's bug reporter. A scanner in the VM watches logs (journal by severity,
specific units, files) and publishes findings; the drawer shows them.

- **⟳ scan now** triggers a fresh scan; **source checkboxes** scope it to the
  logs you care about.
- **Findings** list real errors/anomalies, each citing the log line it came
  from. Deterministic pattern findings always appear; a local-LLM **digest**
  adds explanations (marked ✨) — and every digest claim must cite a line, or
  it's discarded, so it can't make things up.
- A **status strip** shows failed units and the health of key services.
- Scans never block the UI — it says "scan queued" and refreshes when the
  report is ready.

**Use case:** "is anything wrong on this box, and where exactly?" — answered
without hand-reading `journalctl`, and honest even when the digest model is
unavailable (the deterministic findings still publish).

*(Requires the bug-reporter route enabled on the host and the scanner installed
in the VM.)*

---

## 🔍 Search drawer — find files, content & imports

Deterministic search across a VM's files, for when you'd otherwise hand-grep a
tree. Three modes:

- **grep** — content search; results grouped by file with line numbers. Use for
  "where does this string/symbol appear?"
- **files** — list files under a path by extension/filter. Use for "what files
  are here?"
- **imports** — Python import map with a reverse index: the imports show as
  chips; click one to see which files use it. Use for "who imports X?"

The root path is remembered per VM. Results cap with a "narrow the search" note
so a huge result can't wedge the view.

**Use case:** instant, exact answers to file/content/dependency questions —
built to save the time (and, for the keeper, the tokens) of walking a tree by
hand. It runs the same engine the keeper's own tooling uses.

*(Requires the finder route enabled on the host and `finder` installed in the
target VM — blackbird today; other VMs light up as it's rolled out.)*

---

## Honest "not yet enabled" states — by design

Where a feature needs a host-side piece that isn't in place yet, the console
says so plainly rather than pretending. That's deliberate: **the console never
shows you a success it didn't actually achieve.** A drawer that says "route not
yet enabled" or a comment box that says "not saved" is telling you the truth
about the backend — and will start working, unchanged, the moment the host side
lands.
