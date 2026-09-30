# Nomenclature (reference for consistency)

Fixed by the operator 2026-08-22; expanded 2026-08-27. Use these words, in
these senses, in UI labels, docs, code comments, todo items and prompts. B's
prompt-revision pass (composer quick-key) checks operator prompts against this
file.

| Term | Means | Not to be confused with |
|---|---|---|
| **locus** | The machine whose filesystem, processes and shell the seats ground in. What the locus dropdown selects. Kind is an attribute: `lxd` (a guest VM), `ssh` (a remote host), `host` (this machine, shown as **⌂ host**). | console, station, instance, VM |
| **seat** | One attachable terminal surface on a locus: **A** (frontier), **B**/**local** (local keeper), **shell**. Each seat runs one **backend**. | session, tab |
| **backend** | The program a seat runs: frontier `serve` (`abstract-claude serve` — the `/ac/` console, the DEFAULT keeper surface since 2026-09-16) / `mct` (the pointer-exchange arbiter, promoted 2026-08-27) / `claude-code` / `codex`; local `opencode` / `qwen-code`; shell `exec` / `ssh`. Every frontier backend except `serve` has its own tmux session (`keeper-claude`, `keeper-codex`, …) and is labelled "terminal seat (tmux) · …" in the picker; `serve` is a user unit (`abstract-claude-serve@station`), not a seat. Retired from the picker: `mct-deprecated`, `clawd-code`. | model, surface |
| **model** | The LLM a backend calls (e.g. A's claude alias, B's local model). | backend |
| **A** | The frontier (metered) model/seat. | keeper (A is *a* keeper; "the keeper" unqualified = the frontier seat of the active locus) |
| **B** | The local, free model that mediates C→B→A, judges todo pings, revises prompts. Mediates the `mct` exchange (and lived inside the deprecated broker `mct`). | local shell |
| **C** | The operator (you). | — |
| **frontier** | Adjective for the metered side: the A surface, its backends (`serve` / `mct` / `claude-code` / `codex`) and the paid API model behind them. "The frontier" unqualified = the A surface of the active locus. | local |
| **local** | Adjective for the free, on-hardware side: the B seat, its backends (`opencode` / `qwen-code`) and the local model. Also the short name of the B seat itself. | the `host` locus (that is a *place*; local is a *cost side*) |
| **shell** | The third seat: a plain terminal on the locus, no model attached. Its backend is `exec` (host/lxd) or `ssh` (remote loci). | B, "local shell" as a name for B |
| **VM** | Loosely used in code and API paths (`/api/vm/<name>/…`) where **locus** is meant. In prose, prefer locus; say "LXD guest" only when the kind itself matters. | locus of kind `ssh` (an ssh host is not a VM here, even if it is a guest elsewhere) |
| **ssh** | A locus *kind* (reached over ssh) and the shell-seat backend used for such loci. Transport only — it never names the machine. | a different class of machine |
| **keeper** | An AGENT, only ever an agent: a persistent agent seat that survives console restarts (tmux on socket `console`). Qualify it: frontier keeper (A), local keeper (B/local). **Never a machine** — ruled 2026-08-27; the host locus used to be shown as "⌂ keeper"/`@keeper` and that reading is retired. | a machine/locus; the station app |
| **keeper-vm** | RETIRED. Was used when addressing a locus's keepers to mean what is simply the **host**. Say host. | — |
| **station** | Product name only: *hugpy Station* (the desktop app + its backend). Do not use for a VM or a locus. | locus, host |
| **console** | The UI (drawers, tabs, terminals) and its backend process (`server.py`, `console-api`). | locus |
| **host / host seat** | The machine running hugpy Station itself — the `host` locus, displayed **⌂ host** everywhere. Exactly ONE entry in the locus dropdown and the 🖥 loci tab. | locus in general, keeper |
| **`@keeper` / `keeper` (wire aliases)** | Internal values only, both meaning the host locus: `@keeper` is the seat/sudo API sentinel, `keeper` the console-api board/station alias. They ride the wire for compatibility and never appear in UI labels, docs prose or prompts. | keeper the agent |
| **instance** | Avoid. Say locus (or VM when the kind matters). | — |
| **session** (mct) | A named mct workspace (`~/.mct/<name>`) selected in the frontier seat. | tmux session, seat |
| **todo item / flag** | Board entry with a status flag: `completed`, `operator_needed{ambiguous_request, operator_action}`, `fail{not_implementable}`, `in_process{detail, completion_indicator, log}`. See `docs/flows/todo-push-triage.flow.json`. | — |
| **flow** | A `flow.v1` process document in `docs/flows/`, linked to code by `implements` / `# flow:` anchors (`docs/FLOWS.md`). | wireframe (design) |
| **prompt-inbox** | `~/.mct/repl/prompt-inbox/<ts>/prompt.md` — where operator prompts land; the seat gets a one-line pointer. | chat |
| **signature (`by`/`via`)** | Board authorship, explicit: a keeper signs `<locus>-keeper` (host's = `host-keeper`), the operator `operator`/`user`, host agents `A`/`B`, bridges `discord:<name>`. Bare `keeper` is legacy — read as "this board's own keeper", never written anew. | the keeper term itself |

## Rules

- Transport never names the thing: an ssh host and an LXD guest are both a
  **locus**; `kind` says how it is reached.
- One machine, one locus: the host appears exactly once, as **⌂ host**.
  `@keeper`/`keeper` survive on the wire only (see their row); **keeper names
  agents, never machines** (2026-08-27 consolidation).
- Qualify "keeper" and "session" unless context makes the sense unambiguous.
- New terms go here first, then into code/UI.
