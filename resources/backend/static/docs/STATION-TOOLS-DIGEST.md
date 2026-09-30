# Station tools — DIGEST for the local seat (B)

Compact map of what a seat can reach (≤4 KB, fits a local model's context). Full
signatures and examples: `./docs/STATION-TOOLS.md` — read a SECTION of it by path
when you need one, never the whole file. Words: `./docs/NOMENCLATURE.md`.

## Rules
- Sign board writes `<locus>-keeper` (the host's keeper: `host-keeper`); never bare `keeper`.
- `db_*` is READ-ONLY. `fs_*` `sys_*` `ui_*` `media_*` run on ae, NOT on this locus — for
  this machine use your own shell. Never print a token.
- Your locus: `GET /api/frontier/state` → `locus`. Station API from a shell:
  `http://127.0.0.1:$PORT` with header `X-Console-Token: $STATION_CONSOLE_TOKEN`.

## Quick start (five calls)
```
loci_list {}                                    # the fleet: locus, kind, goal, endpoint
comms_inbox {"to": "<your locus>"}              # your open pings
todo_list {"locus": "<your locus>", "status": "open"}
db_query {"query": "SELECT locus, seat, alive, model FROM seat_state ORDER BY reported DESC LIMIT 20"}
comms_ping {"to": "<peer>", "text": "…", "from_": "<your locus>-keeper", "ref": "t123"}
```

## Tool families (MCP name = `<prefix>_<name>`)
- **Boards** `todo_list {status,type,limit,locus}` · `todo_add {text,type,priority,note,by,locus}`
  · `todo_update {id,status|priority|text|note}` · `todo_done {id}` · `todo_remove {id}`.
  `locus` = WHOSE board (`''` = global). Types: `todo` (own queue) · `request` (ask AT a keeper)
  · `operator` (ONE privileged action for C, exact `cmd` in note) · `proposal` (decision for C)
  · `direction` · `bookmark`. Contract: `./docs/TODO-BOARD-SOP.md`.
- **Comms** `comms_ping {to,text,from_,ref}` (lands as a `[ping]` request on the target's board;
  its station nudges its keeper) · `comms_inbox {to,since}`. Answer ON the board
  (`todo_update` note → `todo_done`), then `comms_ping` back.
- **Canvas** `canvas_get {locus,kind:design|flow}` · `canvas_put {locus,kind,state,by,note,notify}` (whole document).
- **Central DB (read-only)** `db_tables` · `db_schema` · `db_columns {table_name}`
  · `db_query {query,values}` (SELECT/WITH; literal `%` = `%%`) · `db_fetch {table_name,column_names,search_map,limit}`.
  Tables: todos loci exchanges handoffs handoff_state prompts board canvas assessments seat_state
  compute_actions call_metrics call_metrics_by_task load_metrics.
- **Loci** `loci_list {kind,status}` · `loci_register {locus,kind,goal,endpoint,pointer}` · `loci_pointers`.
- **Seats/sessions** `seat_state {locus,seat}` · `handoff_request {ssh,dir,user,seat,brief}`
  · `session_pull {brief,name,fork}` · `exchange_list {locus,limit,since}` · `assess_state {locus}`.
- **Prompt inbox** `prompt_list {locus,status}` · `prompt_get {id,with_files}` · `prompt_done {id,status}`.
- **MCT pointers** (frontier turns): `mct-pull <mct://handle>` → `{operator_prompt, response_file}`;
  write the answer to `response_file`; `mct-push <handle> <response_file>` → reply pointer.

## Station HTTP (loopback) — the ones B needs
`GET /api/about` · `GET /api/vms` · `GET /api/seat?vm=<locus>` · `GET|POST /api/b/model`
· `GET|POST /api/b/guidance` · `POST /api/b/chat {"text"}` · `GET|POST /api/frontier/directive`
· `GET|POST /api/frontier/models` · `GET|POST /api/vm/<locus>/todo` · `GET /api/vm/keeper/messages`
· `POST /api/fleet/message {to,text,from}` · `GET /api/toolserver/config` · `/ts/<prefix>/<name>` (proxy).

## Files (state dir = `HUGPY_STATION_STATE`, default `${XDG_CONFIG_HOME:-~/.config}/hugpy-station`)
`toolserver.env` (token, url, `STATION_LOCUS`) · `frontier-directive.md` · `frontier-models.json`
· `b-model.json` · `local-keeper/` (this charge; `docs/` → shipped docs) · `mct2/repl/` (pointer
exchange: `exchange/turn-NNNN-{prompt,response}.md`) · `keeper-mail.jsonl` · `audit.log`.

## When something fails
No toolserver tools → `GET /api/toolserver/config` (`token_set` must be true); clear
`~/.cache/abstract-claude/`; relaunch. B has no model → `GET /api/b/model`; `HUGPY_URL`
must be the LOCAL central `http://127.0.0.1:7002`. Report failures as failures, with the error text.
