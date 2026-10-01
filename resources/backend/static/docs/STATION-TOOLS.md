# Station tools — the agent-facing reference (hugpy Station 1.0.85)

> Vocabulary: **locus**, **seat**, **backend**, **model** (and A / B / C) are used here in the fixed senses of [NOMENCLATURE.md](NOMENCLATURE.md). A **keeper** is an agent, never a machine; this machine is the `host` locus, shown **⌂ host**.

Audience: every seat — the frontier keeper (A, `claude-code`/`mct`), the local keeper (B, `opencode`/`qwen-code`) and the operator's scripts. Read this before the other guides; it is the map of what you can reach from a seat and how.

## 0. Quick start for a seat (five calls)

```
loci_list {}                                   # who is in the fleet (locus, kind, goal, endpoint)
comms_inbox {"to": "<your locus>"}             # your open pings (your locus: GET /api/frontier/state → locus)
todo_list {"locus": "<your locus>", "status": "open"}   # your board
db_query {"query": "SELECT locus, seat, alive, model FROM seat_state ORDER BY reported DESC LIMIT 20"}
comms_ping {"to": "<peer locus>", "text": "…", "from_": "<your locus>-keeper", "ref": "t123"}
```

Rules that hold everywhere: `db_*` is read-only; `fs_*`/`sys_*`/`ui_*`/`media_*` run on ae, not here; sign board writes `<locus>-keeper`; never print a token.

---

## 1. What the station is

- **hugpy Station** = an Electron shell (`main.js`) over a self-contained aiohttp backend (`resources/backend/server.py`) serving one single-file React/htm page (`static/index.html`). The shell starts the backend on loopback: `http://127.0.0.1:8899` (`CONSOLE_PORT`, exported to the backend and every seat as `PORT`).
- **Seats live in tmux** on the dedicated socket `console` (`tmux -L console`): `keeper-claude` (A, claude-code), `keeper-mct` (A, mct), the local seat (B), shell seats and `handoff-h<id>` / pulled sessions. A seat survives a console restart; the page re-attaches over `/wsterm`.
- **Loci** (`kind`): `lxd` = a guest VM on this host (reached with `lxc exec`), `ssh` = a remote host (reached over ssh, multiplexed via `FV_STATE_HOME/ssh-mux`), `host` = this machine (**⌂ host**; wire aliases `@keeper` / `keeper` ride the API only). A locus appears exactly once — one machine, one locus.
- **The leg.** Every locus that takes part in comms runs a *station leg*: the desktop app's backend, or a headless copy — the systemd template `hugpy-station-web@<user>` (loopback, `PORT=8898` by default, env from `/etc/hugpy-station/toolserver.env` + `/etc/hugpy-station/<user>.env`, refuses to start without a console password). `hugpy-station-locus enable <user> [--locus NAME]` wires an account as a locus: ports, `STATION_LOCUS`, the toolserver token, `loci/register` (kind `station`, endpoint `<user>@<lan-ip>`), then `systemctl enable --now hugpy-station-web@<user>`. Every leg polls the toolserver every ~20 s (`STATION_CONSOLE_LOCI_POLL`): `loci/pointers` (the dropdown) and `comms/inbox` (the pings — §5).
- Grounding rule: a seat always grounds in the ACTIVE locus — its files, processes and shell are that machine's. The toolserver's `fs_*` / `sys_*` tools are the one exception (§3, they run on ae).

## 2. The toolserver

- URL: the toolserver advertised on this host (`abstract-toolserver endpoint`; Flask). A remote one is explicit config: `HUGPY_TOOLSERVER_URL` / `STATION_CONSOLE_TOOLSERVER` (e.g. `https://dev.hugpy.ai/toolserver`). Auth: header `X-Operator-Token: <token>` (`Authorization: Bearer <token>` is accepted too).
- **Where the token lives.** `FV_STATE_HOME/toolserver.env` (0600) carries `HUGPY_OPERATOR_TOKEN=…` (optionally `STATION_CONSOLE_TOOLSERVER=…`, `STATION_LOCUS=…`, `HUGPY_URL`/`HUGPY_BASE`). `server.py` loads it into its own environment at startup (`_load_toolserver_env`; a real env var wins; the aliases `STATION_CONSOLE_TOOLSERVER_TOKEN`, `TOOLSERVER_OPERATOR_TOKEN`, `TOOLSERVER_TOKEN` are set from it), so every seat the station spawns **inherits `HUGPY_OPERATOR_TOKEN`**. `hugpy-station-toolserver set` / `POST /api/toolserver/config {token,url?}` rewrite the file live. Read it in a shell only as `TOK=$(sed -n 's/^HUGPY_OPERATOR_TOKEN=//p' ~/.config/hugpy-station/toolserver.env)` and use it as a header — never echo it.
- **The MCP bridge: `abstract-claude mcp`** (python package `abstract-claude`, ≥ 0.1.30). A stdio MCP server that discovers `GET /endpoints` (+ `?help=true` per tool) and exposes each tool natively. It reads `TOOLSERVER_URL` and the token from its environment (`TOOLSERVER_TOKEN`; ≥ 0.1.30 also `HUGPY_OPERATOR_TOKEN` and the token files `~/.config/hugpy/operator.env`, `~/.config/hugpy-station/toolserver.env`) and caches its discovered tool list under `~/.cache/abstract-claude/` — remove that directory when the tool set looks stale.
- **How each seat gets it.** claude-code: the launch prelude (`_SEAT_TRUST_B64` in server.py, via `abstract-claude launch`) writes `mcpServers.toolserver = {command: abstract-claude, args: [mcp], env: {TOOLSERVER_URL, TOOLSERVER_TOKEN}}` into the seat's `.claude.json`, the token read from the seat user's own credential files (never from the command line). opencode: `~/.config/opencode/opencode.jsonc` (or `.json`) `mcp.toolserver` with `environment.TOOLSERVER_TOKEN = "{env:HUGPY_OPERATOR_TOKEN}"` and `instructions` pointing at the charge + this file. qwen-code: `~/.qwen/settings.json` `mcpServers.toolserver` with `env.TOOLSERVER_TOKEN = "$HUGPY_OPERATOR_TOKEN"`. The last two are fitted by `_fit_local_seat_tools` (1.0.85) at backend startup and on every local-seat launch; no literal token is ever written; a custom `toolserver` entry is never touched, and the station's own entry is only *completed* (missing keys filled in) — an operator's `"enabled": false`, own `TOOLSERVER_URL`, `timeout` or extra keys survive every restart. A symlinked config file is written through (the link is kept).
- **Tool naming.** `/<prefix>/<name>` → tool `<prefix>_<name>` (`db/query` → `db_query`, `comms/ping` → `comms_ping`). Claude Code shows them as `mcp__toolserver__<prefix>_<name>`. A few tools carry a bare function name in `?help=true` (`fs/read_span` → `read_span`, `sys/run_cmd` → `run_cmd`); the MCP name still follows the URL.
- **Direct HTTP** (scripts): `GET /endpoints` lists `{endpoint, methods, url}` (tool endpoints are the `tools_bp.*` ones); `GET <url>?help=true` → `{"<name>": {"doc", "params": [{name, default}]}}`; call with `POST <url>` and a JSON body of the params; results come back as `{"result": …}` or `{"error": …}`. Example: `curl -s -H "X-Operator-Token: $TOK" -H 'Content-Type: application/json' -d '{"to":"op"}' https://toolserver.hugpy.ai/comms/inbox`. **Never `GET /events/stream`** from a script — it is a Server-Sent-Events stream that never ends (the station's events relay owns it).
- Prefixes: `assess board canvas claude comms db events exchange fs handoff image loci media metrics prompt seat session sys text todo ui vl vm vmpool web`.

## 3. Tool families and signatures

Fetched from `?help=true` on 2026-09-08. Defaults in parentheses; `None` = required unless the doc says otherwise.

### Boards (`todo/*`) — the central per-locus boards (schema todo.v1; SOP: [TODO-BOARD-SOP.md](TODO-BOARD-SOP.md))
| tool | params | what it does |
|---|---|---|
| `todo_list` | `status, type, limit (200), locus` | Items newest first; `locus` filters the central board to one keeper's slice. |
| `todo_add` | `text, type ('todo'), priority, note (''), by (''), source ('toolserver'), locus ('')` | Add an item. `locus` = WHOSE board (`''` = global). `type`: `todo` (the keeper's own queue — also where anything the operator MIGHT want goes), `request` (an ask AT the keeper), `bookmark` (a checkpoint), `operator` (ONE concrete action only the operator can take, `cmd` in the note — never a wish-list), `proposal` (a decision wanted FROM the operator: pros/cons/rec), `direction` (a standing operator ruling/invariant you carry forward). `priority`: low\|medium\|high. |
| `todo_update` | `id, status, priority, text, note` | Update fields (`status` = open\|doing\|done). |
| `todo_done` | `id` | Shortcut for `status=done`. |
| `todo_remove` | `id` | Delete. |
| `board_list` | `locus, status, limit (500)` | The watchdog-mirrored projection of each locus's `~/todo.json` (LXD guests), newest-updated first. |
| `board_summary` | — | Per-locus overview joined with the registry: goal, endpoint, item counts. |

**Item types at a glance** (full contract: [TODO-BOARD-SOP.md](TODO-BOARD-SOP.md)):

| type | tab · glyph | id | when to use / when NOT | note template |
|---|---|---|---|---|
| `todo` | queue · ☑ | `t<N>` | your own queue — incl. anything the operator MIGHT want. NOT an ask the operator must action. | `SCOPE: … — DONE: …` |
| `request` | queue · ✋ | `t<N>` | an ask AT the keeper (operator/peer filed it). NOT a self-note. | `ASK <who>: … — DONE: …` |
| `bookmark` | 🔖 | `bm<N>` | a stable build / shipped commit / verified checkpoint. NOT a plan. | `SHIPPED <UTC>: … — VERIFIED: … — ROLLBACK: …` |
| `operator` | ⚑ | `o<N>` | ONE action only the operator can take (privilege you lack), with a copyable `cmd`. NOT a wish-list. | [BOARD-ITEM-FORMAT.md](BOARD-ITEM-FORMAT.md) §1: `[root]\|[OP]` title + WHY/WHO/GATE/DO/VERIFY/EXPECT |
| `proposal` | ⚖ | `p<N>` | a decision wanted FROM the operator: pros/cons/rec. NOT self-decided. | [BOARD-ITEM-FORMAT.md](BOARD-ITEM-FORMAT.md) §2: PROBLEM/OPTIONS/REC/DECISION/SKETCH |
| `direction` | queue · 🧭 | `d<N>` | a standing operator ruling/invariant you carry forward. NOT a one-off task or your own opinion. | `RULING (<who, date>): "<quote>" — STATE: <APPLIED\|FOLDED\|CLOSED>` |

### Comms (`comms/*`) — protocol in §5
| tool | params | what it does |
|---|---|---|
| `comms_ping` | `to, text, from_ (''), ref ('')` | Posts a marked `[ping]` high-priority `request` on the TARGET locus's board (`to` = a locus name, `[a-z0-9-]`) → `{id, to, from, ref, ts, note}`. Durable (it stays on the board) and prompt (the target leg nudges its keeper). |
| `comms_inbox` | `to, since (0)` | The target locus's open pings — items whose note is a `[ping]` marker, newest first → `{pings: [...]}`. |

### Canvas (`canvas/*`) — design + flow documents, one row per (locus, kind)
| tool | params | what it does |
|---|---|---|
| `canvas_get` | `locus, kind ('flow')` | `kind`: `design` (wireframe.v1 shapes on a 1280×800 grid) \| `flow` (flow.v1 nodes+edges, lifecycle derived→proposed→agreed, rev counter) → `{locus, kind, state\|null, rev, by, note, updated}`. |
| `canvas_put` | `locus, kind, state, by (''), note (''), notify (False)` | Write the WHOLE document (a put replaces; validated fail-closed; a flow's `rev` bumps on every changed put). `notify=true` also posts a `[canvas]` request on the locus's board — for a deliberate hand-off, never autosave → `{locus, kind, rev, changed, updated, state}`. |
| `canvas_list` | `locus ('')` | Which loci hold which documents (no bodies). |

### Central DB (`db/*`) — READ-ONLY (details §4)
| tool | params | what it does |
|---|---|---|
| `db_tables` | `schema ('public')` | Table names. |
| `db_schema` | `schema ('public')` | Every table → its columns. |
| `db_columns` | `table_name, schema ('public')` | Columns (name + type) of one table. |
| `db_query` | `query, values` | A raw SELECT/WITH query; writes are rejected by the gate. Write a literal `%` as `%%`. |
| `db_fetch` | `table_name, column_names ('*'), search_map, any_value (False), limit (100), schema ('public')` | Identifier-composed SELECT: you supply params, not SQL. |

### Loci (`loci/*`) — the registry
| tool | params | what it does |
|---|---|---|
| `loci_list` | `kind, status ('active')` | Registered loci (kind vm\|station\|session\|handoff\|ssh) with goal, endpoint, pointer; `status=''` = all. |
| `loci_register` | `locus, kind ('station'), goal (''), endpoint (''), pointer, reactivate (False)` | Upsert by name; a given `pointer` is MERGED (send a key as null to clear it); `endpoint` = `user@host[:port]`. Every station's dropdown picks it up within ~20 s. An archived locus stays archived unless `reactivate=true`. |
| `loci_pointers` | `kind ('')` | THE distribution feed the legs poll: every active locus reachable over ssh + every pulled claude session with its seat handle. |

### Seats and sessions (`handoff/*`, `session/*`, `seat/*`, `assess/*`, `exchange/*`)
| tool | params | what it does |
|---|---|---|
| `handoff_request` | `locus, text, task (''), fork (false), spawn (true), ssh, dir, user, seat ('claude'), source_session ('')` | Leave a handoff for the NEXT session on the locus — ONE toolserver row (state open → spun → claimed → consumed → done), carrying the ledger pointer + your session id. With ssh/dir/user a station seat is asked for; `spun` only when the station VERIFIED it running. Pullable either way. |
| `handoff_pull` | `locus or id, session_id (''), consume (true)` | The /resume: the init prompt for the newest open handoff (text + ledger pointer + what the previous seat did, from the exchanges DB). The SessionStart hook calls it with the seat's session id; the row becomes `consumed` by that session, idempotent per session, loud for another. |
| `handoff_claim` | `id, status ('claimed'), by, session_id, station_handle` | Idempotent state transitions (claimed / consumed / done / abandoned); invalid transitions are errors. |
| `handoff_station` | `locus ('')` | Station seat API probe; with a locus also `init_prompt` (the same text handoff_pull returns) and `layer` (the launch pointer) — steward tab, launch and tool never diverge. |
| `handoff_list` | `status, limit (50), mode` | Handoffs newest first (status pending\|spun\|claimed\|done\|abandoned; mode seat\|pull). |
| `handoff_claim` | `id, station_handle (''), status ('claimed')` | Attach the created station session (or mark done/abandoned). |
| `session_pull` | `session_id, ssh, dir, user, brief, locus, machine, fork (True), seat ('claude'), name` | PULL a live Claude Code session into a station seat; the bridge auto-fills id/ssh/dir/user — an agent normally passes just `brief`. `fork` keeps the origin intact; `name` = the pulled session's tmux seat and locus (default `sess-<id8>`). |
| `session_list` | `status (''), limit (50)` | Pulled sessions joined with their session locus pointer. |
| `seat_state` | `locus, seat` | Seat state as last reported (rows older than 90 s carry `stale=true`) → `[{locus, seat, alive, stale, pid, age_s, model, session_id, auth_ok, host, login, status, reported, started}]`. |
| `seat_report` | `locus, seat, alive (True), pid, age_s, model, session_id, auth_ok, host, login, status, started` | Publish a seat's state (the seat side does this every 30 s). |
| `assess_state` | `locus` | The current rolling state + init prompt for a locus (what `/api/frontier/state` shows). |
| `exchange_list` | `locus, limit (50), since, full (False)` | Exchanges newest first (the mct pointer-exchange turns); text truncated to 400 chars unless `full=true`. |

### Prompt inbox (`prompt/*`)
| tool | params | what it does |
|---|---|---|
| `prompt_submit` | `locus, text (''), files, by (''), session ('repl'), source ('station')` | Deliver an operator prompt (+ files `[{name,type,dataUrl\|b64}]`, ≤20, ≤64 MiB) to a locus through the central inbox: stored on ae, a `[prompt]` board item on the locus → `{id, locus, path, prompt_path, pointer, files, board_id, ts}`. |
| `prompt_list` | `locus, status ('open'), limit (50)` | Prompts newest first (`open\|delivered\|done`, `''` = all). |
| `prompt_get` | `id, with_files (False)` | One prompt: text, path, pointer, files (`with_files=true` returns them base64 for a seat on another machine). |
| `prompt_done` | `id, status ('done')` | Mark delivered\|done and close its board item. |

### Runs ON the toolserver host (ae), not on your locus
`fs_read_span {path, ranges}` (1-based inclusive line ranges), `fs_read_file`, `fs_read_json`, `fs_write_file`, `fs_glob`, `fs_search`, `fs_find_*`, `fs_extract`, `fs_imports`; `sys_run_cmd {cmd}` (only binaries in `TOOLSERVER_CMD_ALLOWLIST`, off by default); `ui_*` (screens, windows, OCR, click-verify), `media_*` (transcribe, ocr, pdf_text, summarize, keywords), `image_*`, `text_*`, `web_*`, `vl_*`, `vm_*` / `vmpool_*`. Paths you pass are ae paths; for the machine you are seated on, use your own shell.

### Worked examples (JSON bodies as the MCP tool receives them)

```
todo_add    {"locus": "hugpy", "type": "request", "priority": "high", "text": "rotate the toolserver cert", "note": "expires 2026-09-20", "by": "op-keeper"}
todo_update {"id": "t412", "status": "doing", "note": "cert renewed, restarting nginx — verifying"}
todo_done   {"id": "t412"}
comms_ping  {"to": "hugpy", "text": "t412 done: cert valid to 2026-12-19 (openssl s_client output on the board)", "from_": "op-keeper", "ref": "t412"}
comms_inbox {"to": "op"}                                   → {"pings": [{"id": "t415", "text": "...", "note": "[ping] from hugpy-keeper", ...}]}
canvas_get  {"locus": "op", "kind": "flow"}                → {"locus", "kind", "state": {...flow.v1...}, "rev": 7, "by", "note", "updated"}
canvas_put  {"locus": "op", "kind": "flow", "state": {...}, "by": "op-keeper", "note": "flow rev 8: added the nudge retry", "notify": false}
db_columns  {"table_name": "todos"}
db_query    {"query": "SELECT id, locus, status, left(text, 60) FROM todos WHERE note LIKE '[ping]%%' ORDER BY created DESC LIMIT 20"}
db_fetch    {"table_name": "loci", "column_names": "locus,kind,endpoint,status", "search_map": {"status": "active"}, "limit": 50}
seat_state  {"locus": "op"}                                 → [{"locus": "op", "seat": "claude-code", "alive": true, "stale": false, ...}]
session_pull {"brief": "continue the 1.0.85 db drawer; state is on the op board (t420)"}
```

## 4. Databases

The central Postgres (`hugpy`), 14 tables (`db_tables`, 2026-09-08): `todos, loci, exchanges, handoffs, handoff_state, prompts, board, canvas, assessments, seat_state, compute_actions, call_metrics, call_metrics_by_task, load_metrics`.

| table | columns |
|---|---|
| `loci` | locus, kind, goal, endpoint, status, created, updated, pointer |
| `todos` | id, type, status, priority, text, note, by, source, created, updated, locus |
| `board` | locus, item_id, status, type, ts, updated, data |
| `canvas` | locus, kind, doc, rev, by, note, created, updated |
| `exchanges` | id, locus, turn, prompt, response, model, pointers, tokens, duration_s, ts |
| `handoffs` | id, locus, source_session, ssh, dir, login, seat, brief, status, station_handle, created, updated, mode, session_id, fork, station, tmux |
| `seat_state` | locus, seat, alive, pid, age_s, model, session_id, auth_ok, host, login, status, reported, started |
| others | `handoff_state` (id, locus, state, init_prompt, upto_ts, upto_turn, model, n_exchanges, ts), `prompts` (id, locus, session, path, text, files, by, source, board_id, status, created, updated), `assessments` (id, locus, focus_item_id, focus_summary, fulfilled, rationale, assessed_by, pending, signal, ts), `compute_actions`, `call_metrics`, `call_metrics_by_task`, `load_metrics` — `db_columns {table_name}` for the rest. |

- **Read-only from tools.** `db_query` accepts SELECT / WITH only; anything else is rejected upstream. Writes go through the domain tools (`todo_*`, `canvas_put`, `loci_register`, `prompt_*`, `seat_report`) which validate.
- **The `%%` quirk.** The query passes through a placeholder layer, so a literal `%` (LIKE patterns, `to_char` formats) must be written `%%`: `db_query {"query": "SELECT id, text FROM todos WHERE note LIKE '[ping]%%' AND locus = %s", "values": ["op"]}`.
- **Direct Postgres** (operator / scripts): `db.hugpy.ai:5432` — LAN split-horizon to `192.168.1.100`, role `hugpy`, TLS (Let's Encrypt certificate — a public CA, so a stock client verifies it).
- **Desktop viewer:** `http://127.0.0.1:8771` (`hugpy-dbview`) on the op desktop; no desktop toolserver is allowed.
- **Through the station:** `/ts/db/<tables|schema|columns|query|fetch>` (same-origin proxy, allow-listed in 1.0.85) — what the 🗄 db drawer (being added in 1.0.85) uses.

## 5. Inter-locus comms, end to end

1. **Send.** `comms_ping {to: "<locus>", text, from_: "<your locus>-keeper", ref}` — or `POST /api/fleet/message {"to": "<locus>", "text": "...", "from": "..."}` on your station, which does the same for an ssh locus (`via: "board"`), while for an `lxd` guest it appends a mail row inside the VM with `lxc exec` and for `to: "keeper"` (the ⌂ host) it writes the host inbox directly.
2. **Land.** The ping is a `[ping]` high-priority `request` item on the TARGET locus's central board (`todos`, `locus = <target>`, note `[ping] from <sender>`). It is durable: it stays until answered.
3. **Deliver.** The target locus's station leg, on every loci-sync (~20 s), calls `comms/inbox {to: STATION_LOCUS}` (`_deliver_pings`). Every ping not yet in `FV_STATE_HOME/comms-delivered.json` (`{"ids": [...]}`, last 2000) becomes one ✉ row in `FV_STATE_HOME/keeper-mail.jsonl` (`{"id": "m<ms>-<hex>", "ts", "from", "to": "keeper", "text": "[board <ping id>] …", "via": "fleet-mail", "unread": true}`) and is queued in `FV_STATE_HOME/comms-nudge-pending.json` (`{"pings": [...]}`, last 50).
4. **Nudge.** `_nudge_frontier` types ONE line into the frontier claude pane — `📨 N new board ping(s) for <locus> — latest <id>: <text…> … read them with comms_inbox to=<locus> and answer on the board` — **only when** a live claude pane exists on the `console` tmux socket (session `keeper-claude`, else any session whose active pane runs claude), the seat probe says not busy, and the captured input line is an EMPTY `❯` prompt (never over a running turn or something the operator is typing). Otherwise the pings stay pending and `FV_STATE_HOME/audit.log` gets one `comms-nudge-skip` line with the reason (`no live claude pane on the keeper socket`, `seat busy (…)`, `prompt not empty/idle (…)`, `capture-pane rc=…`), retried every sync; a delivered nudge logs `comms-nudge`.
5. **Answer.** The keeper reads `comms_inbox {to: <its locus>}`, does the work, replies ON the board (`todo_update {id, note}` then `todo_done {id}`) and pings back with `comms_ping {to: <sender>}`. Sign `by`/`from_` as `<locus>-keeper` (the host's keeper: `host-keeper`), never bare `keeper`. Ids: pings are board ids (`t…`); mail rows are `m…`; quote the ping id in the reply so the thread can be followed.
6. **Who is who.** A station's own locus is `STATION_LOCUS` in `toolserver.env` (`/api/toolserver/config`, the `locus` field of `/api/frontier/state`), else the registered locus whose endpoint is `<this user>@<one of this host's ips>`. Example fleet: `op` (`op@192.168.1.113`, the desktop), `hugpy` (`hugpy@192.168.1.100`), `ae-vm-mgr` (`vm_mgr@192.168.1.100`).

## 6. Boards and canvas

- **Boards** are todo.v1 (`{"schema":"todo.v1","items":[{id, type, text, note, status, by, ts, …}]}`). The central `todos` table holds one slice per locus (`locus` column); an LXD guest additionally keeps `~/todo.json` inside the VM, mirrored to `board`. Item types, statuses and the signing rule are in [TODO-BOARD-SOP.md](TODO-BOARD-SOP.md) — read it before actioning anything; extra fields survive round-trips; keep the JSON valid.
- **Canvas** documents live in the `canvas` table, one row per (locus, kind): `design` = `wireframe.v1` (`{"schema":"wireframe.v1","canvas":{"w":1280,"h":800},"shapes":[{code, role, label, x, y, w, h}]}`), `flow` = `flow.v1` (nodes + edges, lifecycle derived→proposed→agreed, `rev` bumps on change). `canvas_get` / `canvas_put` are whole-document; the ◳ canvas tab shows the same row live over the DB change bus. Nothing reads `~/wireframe.json` / `~/flow.json` any more. Formats: [UI-GUIDE.md](UI-GUIDE.md).

## 7. Station HTTP API cheat-sheet (loopback `http://127.0.0.1:$PORT`, desktop 8899)

| method | route | purpose |
|---|---|---|
| GET | `/api/about` | name, version, backend dir, python. |
| GET | `/api/vms` | every locus: the sidecar's LXD rows + ssh hosts (kind `ssh`); the ⌂ host is not listed as an ssh host of itself (1.0.85). |
| GET | `/api/seat?vm=<locus>` | everything that locus's seats launch with: grounding, models, directive source, fs switch, sudo, claude auth, frontier toggle. |
| GET | `/api/term/backends[?vm=]` | the surface/backend whitelist + which binaries are installed there. |
| GET / POST | `/api/toolserver/config` | `{url, token_set, path, loci_sync}`; POST `{token, url?}` rewrites `toolserver.env` and resyncs. |
| GET | `/api/frontier/state[?vm=]` | the rolling state + init prompt (toolserver `assess/state`) + `busy`, `idle_s`, idle-relaunch verdict. |
| GET / POST | `/api/frontier/directive` | the directive file verbatim + the composed text each frontier seat receives; POST `{text}` (`""` restores the shipped default). |
| GET / POST | `/api/frontier/handoff` | the session init prompt (`frontier-handoff.md`); POST `{text}` (`""` deletes). |
| GET / POST | `/api/frontier/models` | the model each frontier seat launches with (`{mct, claude-code}`). |
| GET | `/api/frontier/cache?vm=` | the frontier claude-code seat's prompt-cache holdings and lapse time. |
| POST | `/api/frontier/relaunch?vm=@keeper` | a fresh host frontier seat on the init prompt. |
| GET / POST | `/api/b/model` | the local keeper's model (`{"model": ""}` = package default). |
| GET | `/api/claude/auth?vm=` | does the seat A would run in have a Claude login (with the exact steps if not). |
| GET / POST | `/api/vm/<locus>/todo` | that locus's board (todo.v1); `keeper` / `host` = the ⌂ host board. |
| GET | `/api/vm/keeper/messages` | the ⌂ host ✉ inbox (`keeper-mail.jsonl`, served rows are marked read). |
| POST | `/api/fleet/message` | `{to, text, from}` — mail to a locus (ssh locus → board ping, §5). |
| GET / POST | `/api/mct/todo`, `/api/mct/todo/history`, `/api/mct/todo/assist` | the active mct session's board, its history, LLM reword/split/tidy. |
| POST | `/api/mct/prompt` `{text, files}` | compose a prompt into the mct prompt-inbox (`~/.mct/repl/prompt-inbox/<ts>/prompt.md`). |
| GET | `/api/mct/live`, `/api/mct/sessions`, `/api/mct/vm`, `/api/mct2/status`, `/api/mct2/exchange?file=` | the frontier's live turn log, named mct workspaces, LXD candidates, the pointer-exchange REPL's flight/queue, one exchange file. |
| GET / POST | `/api/handoff/spawn` | GET = capability probe `{features, station, socket}`; POST `{id, seat, ssh, dir, user, brief, resume?, fork?, name?}` materializes a toolserver handoff / session pull as a tmux seat (called by the toolserver with `X-Console-Token`). |
| GET | `/api/sessions/pulled` | pulled claude sessions + whether their tmux seat is live here (with `/wsterm` attach URLs). |
| GET | `/api/events` | Server-Sent Events: one `change` event per DB change `{table, op, locus, id, ts}`; keepalive every 15 s. |
| * | `/ts/<prefix>/<name>` | same-origin proxy to the toolserver. Allow-list (`_TS_ALLOWED`): `assess/ handoff/ exchange/ todo/ loci/ board/ session/` + (1.0.85) `db/ comms/ canvas/ seat/ prompt/`. Everything else → 403 `path not allowed via /ts`. |

The desktop backend runs open on loopback unless a console password is set (`python3 server.py --set-password`); a headless leg always requires one. Scripts may pass `STATION_CONSOLE_TOKEN` as `X-Console-Token` / `?token=`.

## 8. Files on disk

| path | what |
|---|---|
| `FV_STATE_HOME` = `~/.config/hugpy-station` (`HUGPY_STATION_STATE` overrides) | the station's state/config dir. |
| `toolserver.env` | `HUGPY_OPERATOR_TOKEN`, optional `STATION_CONSOLE_TOOLSERVER`, `STATION_LOCUS`, `HUGPY_URL`/`HUGPY_BASE` (0600). |
| `fleet_ssh_key(.pub)`, `ssh-hosts.json`, `hidden-loci.json`, `ssh-mux/` | the fleet ssh key, operator-added ssh loci, hidden loci, ssh multiplex sockets. |
| `frontier-directive.md` (override of the shipped `resources/backend/frontier-directive.md`), `frontier-handoff.md`, `frontier-models.json`, `frontier-keeper.json` | A's directive, the session init prompt, the frontier models, the Frontier Keeper toggle. |
| `a-settings-template.json`, `claude-oauth-token` | the settings template every claude-code seat is seeded from; the durable oauth token. |
| `b-model.json`, `b-gate.json`; `~/.config/hugpy-agent/agent.env` | B's model + gate; the hugpy-agent env B launches with. |
| `local-keeper/` | B's host workspace: `AGENTS.md` (the standing charge, marker `<!-- hugpy-station local-keeper charge vN -->`), `QWEN.md` → `AGENTS.md`, `docs/` → the shipped `static/docs`. The local seat cd's here. |
| `~/.config/opencode/opencode.json(c)`, `~/.config/opencode/AGENTS.md`; `~/.qwen/settings.json` | the local seat's backend configs carrying `toolserver` (§2). |
| `mct/`, `mct2/repl/` | the mct workspaces (deprecated broker; the pointer-exchange REPL: `status.json`, `exchange/turn-NNNN-{prompt,response}.md`). |
| `keeper-mail.jsonl`, `comms-delivered.json`, `comms-nudge-pending.json`, `audit.log` | the ✉ inbox, delivered ping ids, pending nudges, the audit trail (`comms-nudge`, `comms-nudge-skip`, `local-seat-fit`, `frontier-relaunch`, …). |
| `~/.cache/hugpy-station-launch.log` | the launcher's log (`station-fix.sh`; the shell's error dialog points here). |
| `~/.cache/abstract-claude/` | the MCP bridge's discovery cache. |
| `~/.claude-sessions/<stamp>-<pid>-<label>/` | the per-launch claude-code config dirs (`abstract-claude launch`). |

## 9. Troubleshooting

- **No toolserver tools in the seat.** (1) `GET /api/toolserver/config` → `token_set` must be true (else `hugpy-station-toolserver set <token>` / the ⚙ panel); (2) the bridge must be `abstract-claude >= 0.1.30` (`abstract-claude -V`; `python3 -m pip install -U abstract-claude`); (3) clear `~/.cache/abstract-claude/` and relaunch the seat; (4) `curl -s -H "X-Operator-Token: $TOK" https://toolserver.hugpy.ai/endpoints | head -c 300` proves the token from the same account. Never paste the token into a prompt or a board.
- **"this station has no locus in the toolserver yet".** `STATION_LOCUS=<name>` in `toolserver.env` (restart the backend), and the locus registered: `loci_register {locus, kind: "station", endpoint: "<user>@<lan-ip>", goal}` (or `hugpy-station-locus enable <user> --locus <name>` on a headless host). `/api/toolserver/config` → `loci_sync.distributed` lists what the sync sees.
- **B has no model / B chat 502.** B's models come from the hugpy central API on ae (`:7002`, `dev.hugpy.ai/api/models`); `HUGPY_URL`/`HUGPY_BASE` in `toolserver.env` (or the headless unit's env) must point at it; `GET /api/b/model` shows `effective`/`default`.
- **Pings arrive on the board but no 📨 nudge.** Check `audit.log` for the latest `comms-nudge-skip` reason: `seat busy` (mid-turn — retried next sync), `no live claude pane on the keeper socket` (no `keeper-claude` session: launch A), `prompt not empty/idle` (text sits in the input line). The ✉ mail is already delivered either way (`/api/vm/keeper/messages`).
- **Locus missing from the dropdown / shows twice.** The sync drops names that fail `[a-z0-9-]`, LXD guests already listed, and (1.0.85) the station's own endpoint; `hidden-loci.json` hides operator-removed ones. `/api/vms` is the merged truth.
- **`/ts/...` → 403.** The prefix is not in the allow-list (§7); use the MCP tool or curl the toolserver directly.
