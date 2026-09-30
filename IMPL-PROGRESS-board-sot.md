# IMPL-PROGRESS — board data-flow fix (DB source-of-truth, file failsafe)

Date: 2026-09-18
Author: jrputkey55@gmail.com (via Claude Code)
File edited (CANONICAL source only): resources/backend/server.py
Backup: resources/backend/server.py.bak-boardsot-20260918-074254
py_compile: PASS. No services restarted, nothing deployed, no live install touched.

## Canonical host locus chosen: `keeper`
Resolved via a new helper `_keeper_locus()` = `_station_locus() or "keeper"`.
- STATION_LOCUS is NOT set on the running host station (hugpy-station-web@vm_mgr),
  so `_station_locus()` returns "" here. Every existing host writer already falls
  back to the literal `keeper` (`_station_locus() or "keeper"` at the comms sender
  and the MCT_LOCUS seat-env lines).
- The DB proves it: toolserver `todos` table has 297 rows under locus `keeper`,
  0 under `op-op`, 4 under `op`. `keeper` is where this host board actually lives.
- So read, write and the file<->DB sync now all use `_keeper_locus()` -> one locus.
- RECOMMENDATION (config, not shipped here): set `STATION_LOCUS=keeper` explicitly
  in /etc/hugpy-station/vm_mgr.env so the value is pinned rather than relying on the
  `or "keeper"` fallback, ending the op-op/keeper ambiguity for good.

## Verified data-flow map (real file:line, canonical server.py)
- Routes /api/mct/todo + /api/vm/keeper/todo -> `mct_todo_get` (4017) / `mct_todo_post` (4034).
- `_sidecar_vm` (2501): /api/vm/keeper/* and vm in {@keeper,host} -> "" (host-native).
- `_central_locus(vm)` (2544): returns "" for host/keeper/@keeper and for LXD; only a
  remote ssh-host locus is non-empty. => host keeper always got "" -> fell to the file.
- DB path: `_central_todo_state` (2626) / `_central_todo_get` (2632) / `_central_todo_post`
  (2642) -> `_ts_call(app,"todo/list|todo/add|todo/update|todo/remove")` (6501).
- File path: `_mct_todo_path` (2976) = $HUGPY_HOME/.hugpy/state/todo.json (here
  /srv/vm_mgr/.hugpy/state/todo.json); `_mct_todo_read` (3006); `_mct_todo_write` (3022,
  atomic tmp+replace under `_todo_locked` flock).
- Reverse sync: `_todo_ts_sync_once` (3050) — DB->file, add-only + central-edit merge,
  pulls {locus, ""}.

## ROOT CAUSE (why DB items never displayed)
`_todo_ts_sync_once` began with `locus = _station_locus(); if not locus: return`.
On the host keeper `_station_locus()` is "" (STATION_LOCUS unset), so the entire
DB->file reverse sync early-returned every tick and never ran. Central `todos`
items (MCP todo_add, comms_ping, seat writes — all under `keeper`) therefore never
reached the file the UI reads. The read side then only ever saw the file.

## What I changed (4 surgical edits)
1. NEW `_keeper_locus()` helper (after `_locus_name_for`, ~10861).
2. `_todo_ts_sync_once` (3050): `locus = _station_locus()` -> `locus = _keeper_locus()`.
   Revives the dead reverse sync so the `keeper` DB slice merges into the file.
3. `mct_todo_get` (4017) host-keeper branch: DB-PRIMARY-WHEN-SAFE. Read
   `_central_todo_state(app, _keeper_locus())`; serve it when reachable AND its item
   count >= the file (`central is not None and len>=len`), else serve the file. Any
   DB error/unreachable silently serves the file. VM/ssh behavior above is untouched.
4. `mct_todo_post` (4034) host-keeper branch: after the existing atomic file write +
   journaling + triage (unchanged), mirror the SAME op to the DB via
   `_central_todo_post(request, _keeper_locus())` (best-effort). A DB failure never
   fails the request; it returns ok with a `warning` field. One call => both stores.

## SAFETY INVARIANT honored (and why DB-PRIMARY was deliberately NOT a blind swap)
Hard measured fact at implementation time: the local file has **429 items**, the DB
`todos` keeper slice has **297**. A blind DB-primary read would have shown 297 and
DROPPED ~132 operator-visible items — a direct violation of "never show fewer than
the file." Per the task STOP clause I kept the file as the safe default and gated the
DB read behind `len(central) >= len(file)`. Result today: the board still serves the
file (429 >= 297 is false), so NOTHING is lost; and the revived reverse sync
back-fills the `keeper` DB items INTO the file so they finally display. As the DB
catches up (dual-write + sync) the read self-heals to true DB-primary automatically.

## board-mirror vs toolserver DB (investigated)
- board-mirror (/srv/hugpy/services/board-mirror/board_mirror.py, units
  board-mirror@{hugpy,vm_mgr,...}) reflects ~/todo.json into a table named **`board`**,
  keyed (locus,item_id), via SOLCATCHER_POSTGRESQL_*.
- The station UI/MCP board reads the toolserver **`todos`** table (todo/list).
- Both tables live in the SAME database (`toolserver`, 127.0.0.1:5432, user vm_mgr):
  I confirmed both `board` (keeper=429, ae-vm-mgr=164, hugpy=6) and `todos`
  (keeper=297, ...) exist in that DB.
- FINDING: same DB, DIFFERENT tables. board-mirror is a one-way file->`board`
  projection for a separate fleet view; it does NOT feed `todos`, so it is orthogonal
  to this fix and needs NO config change for the fix to work. (Note the latent oddity:
  the same file is projected to `board` while the UI reads `todos` — two central
  representations of "the board." Unifying them is out of scope here.)
- board-mirror uses KEEPER_STATION for its locus; station-app uses STATION_LOCUS. If
  you later pin STATION_LOCUS=keeper, keep board-mirror@vm_mgr on KEEPER_STATION=keeper
  too so the two agree. No ~/.env or live edits made.

## Known residual limitations (documented, not shipped-risky)
- id spaces differ: the file uses its own t<n>/name ids; `todos` uses BIGSERIAL shown
  as t<bigint>. So a dual-written `add` lands under different ids in the two stores,
  and the reverse sync (add-only by id) can leave a duplicate in the failsafe file.
  Visible only when the DB is down and the file is served. Full id unification is a
  larger change intentionally deferred (it would touch journaling/triage/todo_apply).
- Mirroring an update/del/comment whose id exists only in the file (not yet in the DB)
  returns a `warning` (no DB row) rather than failing — failsafe by design.

## To make it LIVE (NOT done here)
This is a next-build change. A rebuild+install of the .deb (or, for an immediate live
test, a mirrored edit to the /opt/hugpy-station install + backend restart) is required
for the running GUI to pick it up. Nothing is lost meanwhile: the existing file path
already serves the current 429 items.
