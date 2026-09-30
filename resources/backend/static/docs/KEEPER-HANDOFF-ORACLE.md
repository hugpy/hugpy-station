# Keeper handoff — 2026-08-20 late night (oracle / vigilant inference)

Supersedes nothing: this is a NEW line of work beside the 07-25 handoffs
(fleet/GGUF). **Verify everything here — state drifts.** Read in this order:

1. `IDEA_PHASE/FLOW-cinema-inference-end-to-end.md` — the flow map, the 🔖
   central goal, the robust TODO list (§2), the external proposals (§3).
   The same flow is on the host console canvas (toolserver `canvas/get
   locus=<locus> kind=flow`, ⋔ drawer, `/api/vm/@keeper/flow`).
2. `IDEA_PHASE/METHOD-vigilant-inference.md` — the method, what is built,
   the independent verification (§7) and what it changed.
3. `IDEA_PHASE/GAP-AUDIT-oracle-2026-08-20.md` — the pre-implementation audit.
4. `IDEA_PHASE/ROADMAP-oracle-video.md` — status log (k-numbers); tonight's
   entries are the last three.

## The goal (do not drift from it)

Every scene, every prompt: inference from its best model for its target
implementation — selected per call from evidence, journaled, judged by
something other than itself, written back as evidence, repaired only where it
failed, audited out loud. No prompt is "made and prayed for". The operator
said explicitly: the fleet has ~80 GB VRAM and more coming — **spend
inference** (candidates, heavier tiers); do not ration.

## State at handoff

- Repo: `/srv/share/projects/hugpy/dev` (`dev`; mounted on op's box at
  `/home/op/mnt/solcatcher/...`, slow — use `timeout`). **All of tonight's
  work is UNCOMMITTED** in a tree that also carries foreign dirt
  (`console_dist`, `video_routes.py`, …). Commit boundary is the operator's
  call (TODO-19). New files:
  `oracle/{dag_runtime,repair_controller,selection,prompt_compiler,steward,spatial}.py`,
  `oracle/recipes/video_performance.py`, tests `test_oracle_{dag_runtime,
  repair_controller,selection,prompt_compiler_steward,performance_vigilant,
  recipe_dag,spatial,vigilant_live}.py`, the IDEA_PHASE docs above,
  `directions/PLACEMENT-DISTRIBUTION.md` (rev 2), `directions/ANNOUNCEMENT-DRAFTS.md`.
  Modified: `performance.py`, `evaluation.py`, `contracts.py` (3 repair codes),
  `script_first.py`, `runtime.py`, `router.py` untouched, `oracle/__init__.py`,
  `flask_app/.../oracle_routes.py` (+`/oracle/steward`, `/oracle/selection`),
  `video_intel/runners/performance_relay.py` (DAG by default), `tests/conftest.py`
  (`ORACLE_LEDGER_PATH` → scratch), `tests/test_oracle_contracts.py`.
- Tests: `cd abstract_hugpy_dev && venv/bin/python -m pytest -q tests/test_oracle_*.py -p no:cacheprovider`
  → **1255 passed** at handoff (after TODO-2/3/4/9/11/17 landed; see FLOW §2 checkboxes). No GPU on this VM; everything visual is fakes.
- Live fleet gaps (not code): **no i2v model, no clip judge, no speaker-embedding
  model, chatterbox-tts not seated**; no benchmark matrix published yet.

## What was built tonight (one line each; details in METHOD §2/§4/§7)

k111 durable DAG (journal, leases, cache, resume, controls, revisions) ·
k112 repair controller (code → producer root → smallest path) · k113a per-call
selection + reliability ledger + spread/exploration, pinned into live seams ·
k113b prompt compiler (context/length/multiplicity/angles by difficulty) ·
k113c steward (calibration/streaks/starvation/gap/staleness, bounded
rebalance; runs per DAG run; routes) · recipe on the DAG (relay default) ·
judge ≠ generator at runtime + verdict write-back · k116 spatial contract
(canonical coords, manifest, 9-fault validator, projection, Fold 1→2 payload,
tone 0–10 w/ geometry floor, explicit tier fallback) + `spatial:<seg>` gate.

## Start here (internal TODOs in order; external ones need the operator)

1. DONE tonight: TODO-11, TODO-3, TODO-4, TODO-9; TODO-2 declared (default flip = P8); TODO-17 units written (operator installs).
2. TODO-14 one tone scale (S) · TODO-15 structured difficulty signals (S–M).
3. TODO-5 k113 / TODO-6 k115 / TODO-10 peak-VRAM + CLI (M each).
4. TODO-13 post-production spatial continuity · TODO-16 persist attribution · TODO-12 concurrency.
Then the `[EXT]` items with the operator: P1 seating, P2 sweep, P3–P5 spatial sources.

## Pitfalls learned tonight (so you don't relearn them)

- Performance-test `Fakes` consume verdict lists GLOBALLY across segments.
- `plan.py` refuses a capability on structural kinds (JOIN/GATE/REDUCE/ASSEMBLE);
  ffmpeg concat is a TASK with `params.model_free=True`.
- JUDGE nodes: selection is advisory (the seam resolves its judge); a judge's
  verdict must be ledgered against the PRODUCER, never the judge.
- Seam-unavailable / capability gaps are terminal — never retried blindly.
- `FrozenParams` nests; use `.to_dict()` before JSON.
- The relay's DAG path uses the LIVE process selector; capabilities not in the
  live catalog (e.g. `video.understand`) are gaps there even when fakes pass.
- Camera movement vocabulary is `production.CAMERA_MOVES`; `camera` dict keys
  are validated at lock.

## How to read a handoff

It is a map drawn at a moment. Re-run the suite, re-read METHOD §7's "still
open" list, and check `git status` before trusting any line above.
