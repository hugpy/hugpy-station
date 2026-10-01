# Board item format — the canonical shape of operator items and proposals

> One static reference for the operator and every agent. The toolserver checks
> `operator` and `proposal` items against it when they are written (`todo_add`,
> `todo_update`, `todo_batch`). Lanes, ids, signing and lifecycle are in
> [TODO-BOARD-SOP.md](TODO-BOARD-SOP.md); this file covers **how the text and
> note are laid out**. *(v1, 2026-10-01. Change this file, not a copy of it.)*

## 0. Shared rules

- An item is its **title** (`text`, one line) plus a **note** made of
  **sections**. A section starts at the beginning of a line with its header in
  capitals and a colon (`WHY:`). The content follows on the same line or on the
  lines below it, and runs until the next header.
- Commands always go in **fenced blocks with a language tag**: ` ```bash `
  (or `sql`, `python`, `text`, …). The ⧉ button copies one block, so put
  **one action in each block**. A multi-line block is fine when it is one action
  (a heredoc, a `\`-continued line).
- Commands are pasted as written, so a block has **no prompt markers** (`$ `,
  `>>> `). It has **no placeholders** (`<you>`, `<host>`, `{{x}}`) unless a value
  is stated: write the real value, or set it on the first line (`HOST=computron`)
  and use `$HOST`. A `#` line is a comment. Use it only for a caveat.
- Keep the meaning in the note. Comments (`[comment operator …]`) are appended
  after the sections and never replace them.

## 1. `type=operator` — something only the operator can run (⚑ tab)

**Gate first.** On a locus that has `hugpy-gate`, run `sudo hugpy-gate list`
before you file one. If an action or a grant covers the step, run it yourself
(`auto`: `sudo hugpy-gate run <id> -p k=v`; `confirm`: `approve` then `run
--confirm`). An operator item is only for what no gate action covers.

**Title:** `[root] <imperative summary>` when it needs root or sudo on a host,
otherwise `[OP] <imperative summary>` (a credential, a web account, another
account's tree, a physical step).

**Note sections, in this order:**

| section | required | content |
|---|---|---|
| `WHY:` | yes | One or two lines: what is broken or blocked, and what running this unblocks. Note any doubt here. |
| `WHO:` | yes | The account and host to run it as (`root via sudo on ae`, `solcatcher on computron`, `putkoff on pypi.org`). |
| `GATE:` | yes | `no hugpy-gate action covers this because …` (or `n/a — <locus> has no gate`). |
| `DO:` | yes | One or more fenced blocks, one action each, in the order to run them. |
| `VERIFY:` | yes | A fenced block with the check command. |
| `EXPECT:` | yes | What the check prints when it worked. |
| `RUN:` | yes | **ONE** fenced ```bash block that is a complete script the operator copies and runs from the clipboard: `#!/usr/bin/env bash`, `set -euo pipefail`, a comment with the item id and title, explicit `cd /absolute/dir` where needed (never assume the cwd), the DO steps in order, then the VERIFY checks with the expected output as comments. It exits non-zero on failure (`set -e`; use `test "$(…)" = …` for checks, since `systemctl is-active` and friends return non-zero on the result you want). It runs as the operator (solcatcher) from `~` on the host: `sudo` inline where root is needed, `ssh <host>` inline for other hosts, with the host named. No placeholders: state the value, or make the script refuse with a clear message. |
| `ROLLBACK:` | if applicable | A fenced block that undoes it, or `none — <why>`. |
| `ROLLBACK RUN:` | if `ROLLBACK` has a block | The rollback as one complete script, same shape as `RUN`. |
| `WHERE:` | optional | The absolute directory the commands run from; the normaliser turns it into the `cd` line of a synthesized `RUN`. |

**The script is a pre-made file.** The station writes every open operator
item's `RUN` block to `<state>/operator-scripts/<id>.sh` on the item's locus
(and `ROLLBACK RUN` to `<id>.rollback.sh`; 0755, world-readable, header
comments with the id, title, updated time and source board, then the block
verbatim). On the keeper locus that is
`/srv/vm_mgr/hugpy-station/operator-scripts/<id>.sh`. The files are derived on
every board read, renamed `.done` when the item closes, and never hand-edited.
The item JSON carries `one_liner` / `rollback_one_liner` (not stored text —
added by the station) and the console shows them with their own copy button:

```bash
bash /srv/vm_mgr/hugpy-station/operator-scripts/o845.sh
```

The operator runs that line as solcatcher from `~` on the host (ae); the
script's own absolute `cd`s give the directory control, so the cwd never
matters. When an item has no `RUN`, `POST /api/board/operator/<id>/draft-run`
asks B to draft one as a comment on the item (never applied by itself).

**The result is pointed to, never searched for.** Each script wraps itself: its
full stdout and stderr, every line stamped `HH:MM:SS`, go to
`<state>/operator-scripts/results/<id>/<ts>.log`, with a sidecar
`<ts>.json` — `{item, kind: run|rollback, script, sha256, started, ended,
exit, host, user, cwd, log}` — and the script's own exit code is preserved
(the last stderr line says `exit N · <log path>`). `results/` is setgid and
group-writable for the operator's account; if it is not writable the script
records to `~/operator-results/<id>/` and says so. At the next board read the
station attaches each new result to its item exactly once: a comment
`RESULT <ts> exit N (completed|FAILED) · <log path>` with the last 5 lines,
and in the item JSON (annotation, not stored text)

```json
"result": {"exit": 0, "path": ".../results/o845/20261001T140000Z.log",
           "ts": "2026-10-01T14:00:03Z", "kind": "run", "tail": ["…up to 5 lines…"],
           "sidecar": ".../results/o845/20261001T140000Z.json"},
"flag":   {"status": "completed", "reason": "exit 0", "detail": "script run completed · <log path>",
           "by": "operator-script", "ts": "2026-10-01T14:00:03Z"}
```

(`status: fail`, `reason: exit N` on a non-zero exit). The item is never
closed by a result; the operator or the keeper closes it after reading it.

`DO` and `VERIFY` document, one block per step; `RUN` executes. A writer who
leaves `RUN` out gets one synthesized from `DO` + `VERIFY` + `EXPECT` (see §5);
write it yourself when the steps need a guard, a loop or a different host.

The keeper verifies with its own read-only check and closes the item. The
operator only marks and comments.

**Example:**

````text
title: [root] Stop the crash-looping hugpy-chat bridge on ae
note:
WHY: hugpy-chat.service restarts every 5 s (ModuleNotFoundError: httpx) and points at the retired :7015.
WHO: root via sudo on ae
GATE: no hugpy-gate action covers this because svc.control's allowlist does not include hugpy-chat.service.
DO:
```bash
sudo systemctl disable --now hugpy-chat.service
```
VERIFY:
```bash
systemctl is-active hugpy-chat.service; systemctl is-enabled hugpy-chat.service
```
EXPECT: inactive / disabled
RUN:
```bash
#!/usr/bin/env bash
set -euo pipefail
# o845 [root] Stop the crash-looping hugpy-chat bridge on ae
# WHO: root via sudo on ae — run from ~ on ae
sudo systemctl disable --now hugpy-chat.service
# --- VERIFY (expected: inactive / disabled) ---
test "$(systemctl is-active hugpy-chat.service)" = inactive
test "$(systemctl is-enabled hugpy-chat.service)" = disabled
echo "OK: hugpy-chat.service inactive and disabled"
```
ROLLBACK:
```bash
sudo systemctl enable --now hugpy-chat.service
```
ROLLBACK RUN:
```bash
#!/usr/bin/env bash
set -euo pipefail
# ROLLBACK o845
sudo systemctl enable --now hugpy-chat.service
test "$(systemctl is-active hugpy-chat.service)" = active
```
````

## 2. `type=proposal` — a decision the operator makes (⚖ tab)

**Title:** `<area> — <the decision as a question>?`

**Note sections, in this order:**

| section | required | content |
|---|---|---|
| `PROBLEM:` | yes | One or two lines: what is wrong or missing, with numbers where you have them. |
| `OPTIONS:` | yes | Two or more lines `A) <option> — + <pro> / − <con>`, one option per line. |
| `REC:` | yes | The option you recommend, why, and what it unblocks. |
| `DECISION:` | yes | Agents write `pending (operator)`. Only the operator fills it in. Accept/decline in the console stamps `[accepted]` / `[declined]` at the head of the note and closes the item. (B's `[B proposal]` fix cards are for the keeper instead: `pending (keeper: disposition line below)`, closed with `accept` / `reject: …` / `inert: …`.) |
| `SKETCH:` | yes | The implementation in fenced blocks (` ```diff `, ` ```python `, ` ```bash `, or ` ```text ` for a plan). |

The card stays as the record of why a decision went the way it did. Never
decide your own proposal, and build only what was accepted.

**Example:**

````text
title: toolserver — enforce the board format on write, or lint only?
note:
PROBLEM: operator items arrive as prose with inline commands; the operator cannot copy them reliably.
OPTIONS:
A) enforce in todo_add/update/batch — + every writer is held to one shape / − an old writer's add fails until it is updated
B) lint only (warn in the result) — + nothing breaks / − bad items still land
REC: A — strict shape is the operator's ask; TOOLSERVER_BOARD_FORMAT=warn is the escape hatch.
DECISION: pending (operator)
SKETCH:
```text
board_format.check(type, text, note) -> (text, note, normalized, problems)
todo_add/update/batch: problems -> ToolError naming the sections
```
````

## 3. `type=bookmark` — a place to return to (🔖 tab)

Title: what it is. Note: `WHAT:` the state or build · `WHERE:` the commit, path,
URL or ledger key · `RETURN:` how to get back to it (a fenced block when it is a
command). A shipped build adds `VERIFIED:` (the evidence) and `ROLLBACK:` (the
previous id). The short form `SHIPPED <UTC>: … — VERIFIED: … — ROLLBACK: …`
from TODO-BOARD-SOP §2 is still accepted. Not checked on write.

## 4. `type=direction` — a standing ruling (🧭, queue lane)

Title: `🧭 <RULE IN CAPITALS> (operator, <date>): <the rule, quoted>`. Note:
`DERIVE: <the doc / directive template / code that must absorb it>`. Once it is
folded in, add `STATE: APPLIED in <where>` and close it. Only the operator's
words become a direction. Not checked on write.

## 5. What the toolserver does on write

- **Checked:** `operator` and `proposal` items, on `todo_add`, on `todo_update`
  when it changes `text` or `note`, and on every `add`/`update` op in
  `todo_batch`. Not checked: an update that only closes the item
  (`status=done`) and anything written with `by=operator`.
- **Normalized (safe, reported in the result as `format_normalized`):** header
  variants (`why:`, `**Why:**`, `## Why`, `cmd:`, `Expected:`,
  `Recommendation:`) are rewritten to the canonical header. A command section
  with no fence is wrapped in ` ```bash ` (a `SKETCH` with no fence is wrapped
  in ` ```text `). An untagged fence in a command section gets `bash`. `$ `
  prompt markers are stripped when every command line in the block has one.
  An operator title with no tag gets `[OP] `. A missing `RUN` is synthesized
  from the `DO` blocks, then the `VERIFY` blocks with `EXPECT` as a comment,
  under the script header, `# WHO:` and a `cd` from `WHERE:` (reported as
  `run_synthesized`); a missing `ROLLBACK RUN` likewise from `ROLLBACK`
  (`rollback_run_synthesized`). Text is only ever added or moved, never
  removed or reworded.
- **Refused (the error names every problem):** a missing required section
  (`RUN` only when there is no `DO` block to build it from), a command section
  with no fenced block, a `RUN` that is not exactly one block starting with
  `#!/usr/bin/env bash` and `set -euo pipefail`, a placeholder with no value,
  mixed prompt markers, fewer than two options. Fix the item and write it again.
- **Legacy items:** an update to an item that was already non-conformant is
  accepted as long as it adds no new problem, so the operator can still
  comment on it and it can still be closed. Reformat an item when you next work
  on it.
- `TOOLSERVER_BOARD_FORMAT=enforce` (the default) · `warn` (store the item and
  return `format_problems`) · `off`.
