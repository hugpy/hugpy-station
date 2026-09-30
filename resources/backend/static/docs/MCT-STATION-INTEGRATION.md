# Station MCT integration

The operator's contract is **C → B → A** for every station MCT message.
The local keeper B compiles wording; only C selects the disposition.
See [QUERY-ARBITRATION-SOP.md](QUERY-ARBITRATION-SOP.md).

## Current station path

- The ledger saves each original prompt and attachment before acknowledging intake.
- While A works, unprefixed messages accumulate. At the next turn boundary,
  B compiles one explicitly marked digest. Its manifest retains pointers to
  every original message and attachment.
- A message arriving while B compiles invalidates that batch; B recompiles
  before dispatch. Failed B calls retain the queue and never bypass B.
- `+` keeps a separate turn after the digest; `!` requests interruption with
  a reconcile brief; `todo:` and `?` capture on the board without an A turn.
  A bare `!` holds its brief until another operator message arrives.
- The native Codex and Claude Code adapters receive only the digest pointer.
  The generic native terminal remains an operator escape hatch; its keystrokes
  are not a station MCT composer input.

## GPT settings and tracking

The steward GPT panel edits the next launch's model, reasoning effort and
permission mode. The launcher uses `abstract-gpt launch` when installed and
passes the station's `AG_ROOT`; existing wrapper configuration is preserved.
`--dangerous` maps to Codex's `--dangerously-bypass-approvals-and-sandbox`.
Turning it off explicitly selects workspace-write and on-request approval.

The live tracker follows the Codex process under `keeper-codex` and reads
metadata from its open rollout. It never substitutes another session's newest
file. It reports session identity, actual model/permissions and token usage;
saved settings take effect at the next launch.

## Upgraded abstract-claude console

The operator's reference is <https://dev.hugpy.ai/claude/>. The `/clauide`
spelling serves the site's generic landing page.

The inspected console exposes `/claude/api/usage/sessions`,
`/usage/session?id=…`, `/session/history?id=…`, `/session/agents?id=…` and
`POST /session/chat` with `{session_id, prompt, model}` and streamed events.
These are useful session/history/usage contracts for a station adapter.

The web console's `_chat_stream` starts or resumes `claude -p` directly.
The terminal `mct_repl.Arbiter` separately implements the four lanes and a
deterministic `_digest`; it does not establish inferred B collation for the
HTTP chat route. Reusing that web route must therefore place it **after**
the station's B intake. The shared web service's sessions must not be mistaken
for the current locus's frontier session. Any headless adapter must retain
provider identity, session continuity, cancellation and usage accounting.
