# Vigilant inference — the method (rev 2, 2026-08-20)

> Every scene, every prompt, gets a steady and vigilant stream of inference
> from its best model for its target implementation. Otherwise a prompt is
> made, a prayer is said, and a generation goes to waste.

This document is the method for making that true in hugpy, and the record of
how far it is implemented. It sits on top of the oracle directive
(`hugpy_oracle_video_architecture.md`) and the gap audit
(`GAP-AUDIT-oracle-2026-08-20.md`). Status marks: **[DONE]** landed with
tests · **[WIRED]** reachable from live builds · **[NEXT]** specified, not built.

---

## 0. The one-sentence method

A build is a DAG of typed nodes; **at every node, at every attempt, at every
candidate**, the system (1) compiles exactly the context the next prompt
needs from the locked artifacts, (2) selects the model for *that call* from
evidence, (3) executes under a journal, (4) judges with independent evidence,
(5) writes the verdict back as evidence for the next selection, (6) repairs
only the responsible subgraph, and (7) audits itself for calibration,
streaks, starvation and gaps — out loud.

```
locked artifacts ──► prompt_compiler ──► selection ──► dag_runtime ──► adapters
       ▲                  │                 ▲              │
       │                  │ context plan    │ ledger       │ receipts
       │                  ▼                 │              ▼
   repair_controller ◄── scorecards ◄── evaluators ◄── artifacts
       ▲                                    │
       └──────────── steward (calibration · streaks · starvation · gaps · rebalance)
```

---

## 1. Why the old path wastes generations (audited, not assumed)

| Inference point | What chose the model before this work |
| --- | --- |
| plot / screenplay | newest routing matrix primary (the only evidence-routed step) — one model for the whole script |
| storyboard, TTS per line, ASR, keyframes ×3, judge | `router.resolve_route` default: `requested → deterministic → only-eligible → TASK_DEFAULTS` |
| clips, clip judge | deferred / gap (not executed) |

The router never read the benchmark matrix, VRAM hints, quality profile, budget,
or any record of outcomes. Three keyframe candidates differed only by seed.
A judge's verdict changed nothing about the next selection.

---

## 2. The components

### 2.1 Durable DAG runtime — `oracle/dag_runtime.py` **[DONE]**
SQLite-WAL node journal; record-before-run; leases + heartbeat expiry;
idempotency keys; cross-run content-addressed execution cache; resume after
kill (zero completed nodes repeated); pause / unpause / cancel / approve /
reject / retry_node / retry_nodes / replan; graph-revision history with a
bounded repair budget; journaled resource reservations. Independent siblings
keep running when one fails. 25 tests.

### 2.2 Repair controller — `oracle/repair_controller.py` **[DONE]**
Policy for all 18 repair codes → responsible root (nearest owning ancestor) →
smallest path subgraph → `retry_nodes` (fresh randomness, attempt-bounded) or
`replan` (param change, budget-bounded). Identity drift on a clip regenerates
keyframe + clip, never audio or transcript. `RepairCode` gained
GEOMETRY_DRIFT, CAMERA_PATH_MISMATCH, COLLISION_VIOLATION. 11 tests.

### 2.3 Per-call selection — `oracle/selection.py` **[DONE] [WIRED]**
`select()` runs the directive's ordered resolution per call and returns a
`SelectionDecision` with every candidate's verdict and reason:

1. compatibility (catalog view eligible) → else CAPABILITY_GAP
2. authority (delegated to the router's typed gate, recorded)
3. registration (eligible `model_ids`; caller exclusions)
4. health (seam)
5. resources — per-model VRAM vs node `ResourceRequest` / goal budget
6. quality profile — PREVIEW→speed, BEST→quality, BALANCED blend
7. latency budget — matrix latency vs `budget.max_seconds`
8. reliability — the **ReliabilityLedger**: measured ok-rate, judge pass-rate,
   repair codes, latency per (capability, model); below floor with enough
   samples → rejected *during the run*
9. recommendation — matrix primary/fallback + candidate evidence

Score = weighted matrix quality + matrix ok + ledger pass + ledger ok + speed
(+ primary bonus). **Candidate spread**: FANOUT candidate *i* → i-th ranked
distinct model within `spread_margin` (judge compares models, not seeds).
**Exploration**: every Nth call the runner-up is tried if near and stale, so
evidence never goes cold. A gap is a typed failure, never a silent default.

Wiring: `DagRuntime(selector=…)` selects per candidate, stamps
`model_id` + full decision on the receipt, excludes the failed model on
retry, and writes ok/hard_pass/repair_code/latency/score back per candidate.
`performance._executable_route` (every live TTS line, keyframe, storyboard,
judge) asks `selection.requested_model_for()`; `runtime.execute_route`
ledgers every execution. Env: `ORACLE_LEDGER_PATH`, `ORACLE_SELECTION_DISABLE`.
22 tests.

### 2.4 Prompt compiler — `oracle/prompt_compiler.py` **[DONE]**
Between every prompt: `extract_signals(segment)` → characters, moving
characters, interactions, **props with momentum**, camera motion class,
occlusion, cloth/hair, dialogue density, duration, cuts, tone endpoint,
presence of a spatial manifest. `difficulty_score()` → 0..1 with reasons
(soft-saturating). `compile_context()` →

* **sections** in priority order from the *locked* artifacts only
  (identity constraints, negative constraints, `state_after` never truncated;
  scene, `state_before`, shot, audio window, spatial summary, tone, lighting,
  production design budgeted);
* **length** = f(model context window, prompt share, difficulty, profile);
* **multiplicity** = 1 + difficulty × (cap−1), cap by profile and time budget;
  **angles** identity / physics / camera / lighting / performance assigned per
  variant; `spread_model` when >1 eligible model.

`render_prompt()` puts the angle's emphasis first so it survives adapter-side
truncation. Missing required sections and missing spatial manifests are said
in `reasons`, never invented. 8 tests.

### 2.5 Steward — `oracle/steward.py` **[DONE]**
`Steward.check()` → `HealthReport` (never silent):
calibration (rank agreement of selection score vs judge pass, per capability),
failure streaks with dominant repair code (alarm), starvation of eligible
models (warn → raise exploration), gap share of failures (alarm: fleet
problem), matrix staleness, cache honesty. **Rebalance**: bounded, logged,
reversible weight shifts toward the better predictor and exploration-rate
adjustments, applied via `Selector.rebalance()`. 6 tests.

---

## 3. How a cinema build runs under the method

For each node the runtime is about to execute:

1. `gather_inputs` from succeeded predecessors via typed edges (sibling
   invariant is structural: a segment has no edge from another segment).
2. `prompt_compiler.compile_context(segment_spec)` → context plan: what to
   carry, how long, how many variants, which angles (wired: `performance.
   segment_context` → `_keyframe_for`/`_clip_for` and the DAG executor).
3. For each variant *i*: `selection.select(capability, node, ctx.candidate=i)`
   → model + reasons; `render_prompt(plan, variant_i)`.
4. `journal.lease → mark_running` (record-before-run) → adapter executes →
   receipt carries model, selection, latency, candidate.
5. Evaluator(s) → `Scorecard` (technical + specialist + independent VLM).
   Verdict → ledger per candidate.
6. Fail → `repair_controller.diagnose` → smallest subgraph → `retry_nodes`
   (other model excluded automatically) or budgeted `replan`.
7. Steward on a timer / per run end → report; policy nudged within bounds.

---

## 4. Units A–C — landed 2026-08-20 (late)

| # | Unit | Status |
| --- | --- | --- |
| A | **Recipe on the runtime** — `oracle/recipes/video_performance.py`: linear stages to the lock via `run_performance(stop_after="segments")`, then keyframes/judge/clips/judge/assemble as a `PlanGraph` (one chain per segment, no cross-chain edges) under `DagRuntime`; `run_performance_on_dag`, `run_visual_stages`, `resume_visual_stages`; driver loops run → `RepairController` → run within the repair budget | **[DONE]** — kill after segment 1's keyframes, resume, zero keyframes repeated; judge rejection on seg 1 regenerates `kf:s1`+`kfjudge:s1` only; unbound seam = typed CAPABILITY_GAP on the node, siblings still produced |
| B | **Compiler in the recipe** — `performance.segment_context()` adapts a locked `SegmentSpec`; `_keyframe_for`/`_clip_for` take `max(seam floor, plan.candidates)` candidates; candidate 0 is the locked prompt verbatim, later candidates carry the plan's angle emphasis; same in the DAG executor | **[DONE]** — BEST-profile action shot with floor 1 survives two rejections; compiler fault is a limitation note, never a failed build |
| C | **Judge verdicts in the live path** — `selection.remember_producer(ref, capability, model)` at every producing seam; `note_verdict_for_ref` after every keyframe/clip card; judge nodes never ledger the producer's verdict as their own | **[DONE]** — one ledger row per produced keyframe, attributed to the producing model |

## 5. What remains, in order

| # | Unit | Done when |
| --- | --- | --- |
| D | **k113 planner + disclosure gate** — `planner_mode` enforced; `a_adapter` honours `frontier_may_access`; non-identifying voice/likeness fallback | Frontier-disabled run cannot show A-participation; unauthorized identity yields traits only |
| E | **k115 judge policy** — ≥2 independent judges for semantic/identity classes; `confidence`/`disagreements` populated; runtime generator≠judge | disagreement appears on a real card; steward calibration uses it |
| F | **k116 SpatialSceneManifest + validator** — contract per directive §8; reject the nine faults; manifest feeds the compiler's signals | invalid manifest never reaches admission; compiler drops the "unconstrained" penalty on valid ones |
| G | **k119 spatial evaluators + Track D** — reprojection, silhouette IoU, depth/normal error, flow warp, camera drift, collision → the 3 spatial codes | damaged geometry rejected with the right code; repair goes to spatial.render / video.generate only |
| H | **Router: matrix + resources + budget** — fold selection steps 5–9 into `router.resolve_route` for non-seam callers | a raw `/oracle/route` explains VRAM/budget/ledger in its reasons |
| I | **Clips live** — seat an i2v adapter behind `video.generate.i2v`; lift the `video.*` deferral for seated capabilities; `video.understand` judge | a 15–30 s, 3-line, 2-character slice renders with per-shot scorecards |
| J | **UI/MCT** — per node: selection decision, candidates by model, scorecards, repair plans, steward report, DAG state | the operator sees *why this model, for this shot, right now* |
| K | **Route `/api/performance` through `run_performance_on_dag`** — `performance_relay.py` + `script_first_routes.py` call the DAG entry point; `state.json` becomes a reader | a live build is resumable and repairable from the UI |

Operator gates that no code removes: a GPU worker seating chatterbox-tts and
an i2v model; a benchmark sweep to publish a matrix; the two token rotations
already logged in the roadmap.

---

## 5. Acceptance (automated, all green today unless marked)

- Resume after kill repeats zero completed nodes — `test_oracle_dag_runtime.py`
- Sibling failure does not stop independent siblings — `test_oracle_repair_controller.py`
- Identity failure repairs keyframe+clip only — same
- Geometry failure replans with stronger geometry, siblings untouched — same
- Selection rejects by VRAM / latency / health / reliability with reasons — `test_oracle_selection.py`
- Measured failures demote the matrix primary during a run — same
- All-models-bad is reported as a gap, not defaulted — same
- FANOUT candidates go to distinct models; each is ledgered — same
- Failed attempt excludes that model on retry — same
- Difficulty multiplies prompts across angles; budget caps it — `test_oracle_prompt_compiler_steward.py`
- Invariant sections never truncated; missing ones reported — same
- Steward alarms on streaks, warns on starvation, rebalances within bounds — same
- Recipe on the DAG: resume repeats nothing; judge rejection repairs one chain; gaps are typed — `test_oracle_recipe_dag.py`
- Live recipe: compiler multiplicity + angles; verdicts attributed to producers — `test_oracle_performance_vigilant.py`
- **[NEXT]** A live GPU build through `/api/performance` (unit K + I)

---

## 6. Operating it

```
ORACLE_LEDGER_PATH=/path/reliability.sqlite     # default ~/.hugpy/oracle/reliability.sqlite
ORACLE_SELECTION_DISABLE=1                       # fall back to router defaults (diagnostic only)
ORACLE_ROUTING_MATRIX=/path/routing_matrix.json  # pin a matrix (else newest registry-verified)
```

```python
from abstract_hugpy_dev.oracle import (RunJournal, DagRuntime, Selector, ReliabilityLedger,
                                       RepairController, Steward)
ledger = ReliabilityLedger(path)
sel = Selector(ledger=ledger)                       # live catalog + newest matrix
rt = DagRuntime(RunJournal(db), executor, evaluator=judge, selector=sel)
rt.start(graph, run_id); rt.run(run_id)
RepairController(rt).repair(run_id, failed_node)    # smallest subgraph
Steward(ledger, selector=sel, journal=rt.journal).check().to_dict()
```

---

## 7. Independent verification (2026-08-20, fresh agent, adversarial) and what it changed

A verifier with no stake in the claims ran the suite (1225 at the time) and
graded 24 criteria PROVEN / PARTIAL / MISSING with file:line evidence. The
invariants held: snapshot-only influence, sibling segments (prompt level),
lineage digests, continuity chaining, parallel≡sequential structure,
resume-after-kill, promotion semantics, authority gate, probes, smallest-
subgraph repair, final scorecard/receipts/limitations — all PROVEN by named
tests. It found ten real gaps. Fixed in the same session:

| Gap | Fix | Proof |
| --- | --- | --- |
| DAG selector's pick never reached the seam → possible misattribution | `selection.pinned()` binds the per-candidate decision for the seam call; `_executable_route` honours the pin; attribution prefers the seam's receipt model, pin as fallback | `test_oracle_vigilant_live.py::test_pinned_model_reaches_the_router` |
| Spread / exclusion dead in live seams | `_keyframe_for`/`_clip_for` select per candidate (`candidate_index`, `candidates`, `exclude=failed_models`) | `::test_live_keyframe_candidates_spread_and_exclude_failed_model` |
| UI-reachable `script_first.live_segment_dispatch` bypassed selection | now asks `requested_model_for` and records the decision on the body | code path; covered by existing script_first tests |
| Runtime judge independence not enforced | `run_judge(..., generator_model=)` excludes the generator via selection and refuses self-judgment; `evaluate()` passes `route.model_id` and writes the verdict to the ledger | `::test_judge_that_is_the_generator_is_refused` |
| Steward never run | runs at the end of every DAG run (`VisualResult.steward`); `GET/POST /oracle/steward`; `POST /oracle/selection` explains a pick | `::test_dag_run_ends_with_a_steward_report` |
| `run_performance_on_dag` unreachable | the bus relay (`performance_relay.run_video_performance`) routes through it by default (`ORACLE_DAG_RECIPE=0` to opt out); visual result folded into the manifest under `dag` | `test_oracle_performance.py::test_the_relay_carries_the_whole_manifest_on_success` now passes through the DAG |
| Spatial contract absent | `oracle/spatial.py` (k116): canonical coordinate contract + conversions, `SpatialSceneManifest`, nine-fault validator, camera projection, Fold 1→2 `ConditioningRequest`, frame-alignment report, tone 0–10 with geometry floor, explicit `TierFallback`; `spatial:<seg>` gate node in the visual graph | `test_oracle_spatial.py` (18), `test_oracle_recipe_dag.py` (+2) |
| JUDGE / model-free nodes failed on selection gaps | JUDGE nodes: selection advisory (seam resolves); `model_free` param for deterministic nodes | relay test |

Still open from the verification (honest):

1. **`gen_clip` / `judge_clip` unbound live** — no i2v model seated; clips exist only under fakes (operator gate).
2. **Legacy pixel chaining** in `runners/scene.py` (`chain=True` default) and `runners/movie.py` (last-frame carry) is still UI-reachable outside the oracle; needs an explicit legacy flag or routing through the recipe.
3. **VRAM in benchmarks** is heartbeat-delta, not peak, and tests stub it; `scripts/oracle-benchmark` CLI referenced but absent.
4. **`planner_mode` not enforced; `frontier_may_access` not consulted by `a_adapter`** (k113 proper).
5. **Multi-judge confidence/disagreement** (k115) — still single-judge.
6. **Track D + spatial evaluators** — contract exists; metrics/evaluators next (k119).
7. **Post-production spatial continuity** notes absent.
8. **`validate()` not applied to the visual DAG** (structural checks only at `start`).
9. Verdict attribution not yet on TTS/transcribe/storyboard paths.
10. `DagRuntime.max_parallel` is a serial slice; true concurrency is Phase 10.
