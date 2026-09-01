# Cinema creation — end-to-end inference flow map (as built, 2026-08-20 night)

> **🔖 CENTRAL GOAL OF THE DIRECTIVE**
> **Every scene, every prompt, gets a steady and vigilant stream of inference
> from its best model for its target implementation — selected per call from
> evidence, executed under a journal, judged by something other than itself,
> written back as evidence, repaired only where it failed, and audited out
> loud. A prompt is never "made and prayed for"; a generation is never wasted.**
>
> Everything below is graded against that sentence. Where the system still
> makes a prayer, it is a **TODO**.

Legend: `[LIVE]` runs on a real fleet today · `[FAKES]` proven only under test
seams (backend not seated) · `[LIB]` implemented, not yet on the live path.
Code anchors are under `abstract_hugpy_dev/src/abstract_hugpy_dev/`.

---

## 0. Entry points (who starts a build)

| Entry | Path | Reaches the vigilant path? |
| --- | --- | --- |
| Job bus `video_performance` | `video_intel/runners/performance_relay.run_video_performance` → `oracle/recipes/video_performance.run_performance_on_dag` (default; `ORACLE_DAG_RECIPE=0` opts out) | **Yes** (since tonight) |
| Script-first UI | `flask_app/app/routes/script_first_routes.py` (`/runs/*/plot|screenplay|audio_master|lock|segments|promote`) → `oracle/script_first.py` | Authoring yes; per-segment render via `live_segment_dispatch` now selects, but does not journal/judge/repair |
| Single oracle call | `POST /api/oracle/route` → `router.resolve_route` → `runtime.execute_route` → `evaluation.evaluate` | Selection via seams only when called through `performance._executable_route`; selection is now inside `router.resolve_route` (TODO-4 done) |
| Legacy studio UI | `POST /video/jobs/generate_movie` / `generate_scene` → `runners/movie.py`, `runners/scene.py` | **No** — outside the oracle; carries pixels segment→segment (**TODO-2**) |

> 🔖 *Goal check:* only the job-bus path currently satisfies the goal end to
> end. The others are partial or legacy.

---

## 1. Flow map

```
operator request
  │
  ▼
[1] Authority ─────────── oracle/authority.py · RightsManifest · typed gate BEFORE any route
  │   likeness/voice refs → authorized? no → refused (SOURCE_AUTHORITY_MISSING), no model is ever asked
  ▼
[2] Snapshot ──────────── oracle/production.py GenerationSnapshot · only pre-run prompts/refs/registry_version
  ▼
[3] Plot / Screenplay ─── oracle/screenplay.py, script_first.py            INFERENCE #1, #2  [LIVE]
  │   capability text.chat; model = routing-matrix primary for plot.construct / screenplay.complete
  │   (script_first.resolve_authoring_model) → router with requested_model
  ▼
[4] Continuity / Breakdown / Shot list ── derived deterministically from the screenplay at lock (no inference)
  ▼
[5] Audio master ──────── oracle/audio_master.build_audio_master             INFERENCE #3 (TTS ×N/line), #4 (ASR), #5 (similarity)  [LIVE when chatterbox seated]
  │   per line: N TTS candidates (seed-varied) → round-trip transcribe → speaker similarity → RANKED → AudioMaster
  │   model: performance._executable_route → selection.requested_model_for (ledger+matrix+VRAM/quality/latency) → router
  ▼
[6] Production lock ───── production.ProductionLock · screenplay, continuity, audio, shot plan, registry_version frozen
  ▼
[7] Sibling SegmentSpecs ─ segments.compile_segments · one prompt per segment from the LOCKED context only
  │   assert_siblings + validator: no segment reads another; PlanGraph emitted (to_plan_graph)
  ▼
[8] Visual DAG ────────── recipes/video_performance.build_visual_graph → dag_runtime.DagRuntime (SQLite-WAL journal)
  │
  │   per segment (one chain, no cross-chain edges):
  │
  │   [8a] spatial:<seg> (when a SpatialSceneManifest exists)   oracle/spatial.py        [LIB → wired, no live manifests yet]
  │        validate 9 faults vs lock/fps/cast → ConditioningRequest (Fold 1→2 payload) → else typed gap, NO render
  │
  │   [8b] kf:<seg>  FANOUT image.generate                        INFERENCE #6  [LIVE]
  │        candidates = max(seam floor, prompt_compiler multiplicity by difficulty)
  │        candidate i: prompt = locked prompt (i=0) | + angle emphasis (identity/physics/camera/lighting/performance)
  │                     model  = selection.select(candidate_index=i, exclude=failed) → PINNED → seam → router
  │                     receipt: model, decision, latency, seed, angle
  │
  │   [8c] kfjudge:<seg>  JUDGE image.understand                  INFERENCE #7  [LIVE]
  │        evaluation.run_judge(generator_model=…) → judge ≠ generator enforced → Scorecard
  │        verdict → ReliabilityLedger against the PRODUCING model (remember_producer / note_verdict_for_ref)
  │        first hard-pass wins; none → node FAILED with repair code
  │
  │   [8d] clip:<seg>  FANOUT video.generate.i2v                  INFERENCE #8  [FAKES — no i2v seated]
  │   [8e] clipjudge:<seg>  JUDGE video.understand                INFERENCE #9  [FAKES — no clip judge seated]
  │
  │   failure → repair_controller.diagnose → responsible PRODUCER root → smallest path → retry_nodes (failed model
  │             excluded, fresh seed) | replan (param change, repair budget) → run continues; siblings untouched
  │
  ▼
[9] assemble (TASK video.assemble, model_free) ── ffmpeg concat over the audio master        [LIVE]
  ▼
[10] Final evaluation ──── postproduction.py (assembly order, pacing, audio alignment, color) · re-transcription   [LIVE partial]
  ▼
[11] Steward ──────────── steward.Steward.check() at run end → calibration, streaks, starvation, gap rate, matrix
  │                        staleness, cache honesty → bounded rebalance of SelectionPolicy; GET/POST /oracle/steward
  ▼
deliverable: video_ref + per-shot scorecards + receipts (model per candidate) + repairs + limitations + steward report
(kill −9 anywhere in [8]–[9] → resume: zero succeeded nodes repeated)
```

### 1.1 How a model is chosen at each inference point (the heart of the goal)

`oracle/selection.select()` — per call, per candidate:

1. compatibility (catalog view eligible) → else **CAPABILITY_GAP** (never a default)
2. authority → router's typed gate
3. registration → eligible `model_ids` − caller exclusions (models that failed this shot)
4. health seam
5. resources → per-model VRAM vs node `ResourceRequest` / goal budget
6. quality profile → PREVIEW speed / BEST quality / BALANCED
7. latency budget vs matrix latency
8. **reliability ledger** → measured ok-rate, judge pass-rate, repair codes, latency for THIS capability
9. routing-matrix primary/fallback + candidate evidence

→ ranked candidates with reasons · **spread**: candidate *i* → i-th ranked distinct model within margin ·
**exploration**: every Nth call the runner-up · decision + fallback on the receipt ·
outcome (ok / hard_pass / repair_code / latency / predicted score) written back per candidate.

### 1.2 Where inference happens and with what, today

| # | Step | Capability | Live backend | Selection | Judge | Ledger write-back |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | plot | text.chat | local LLM (GGUF) | matrix primary + selector | validators | **yes** (accept/reject) |
| 2 | screenplay | text.chat | local LLM | matrix primary + selector | validators | **yes** |
| 3 | TTS per line | audio.tts | chatterbox (when seated) | selector via seam | similarity + ASR round-trip | **yes** (per candidate) |
| 4 | ASR | audio.transcribe.word_timestamps | whisper | selector via seam | — | execution only |
| 5 | speaker similarity | audio.speaker_similarity | **none seated** → unscored | — | — | — |
| 6 | keyframes ×N | image.generate | image worker | per-candidate, spread, exclude | VL judge ≠ generator | **yes** |
| 7 | keyframe judge | image.understand | Qwen-VL | selector (advisory) | — | judge execution only |
| 8 | clips ×N | video.generate.i2v | **none seated** | per-candidate (fakes) | — | yes (fakes) |
| 9 | clip judge | video.understand | **none seated** | advisory | — | — |
| 10 | assemble | video.assemble | ffmpeg | model_free | technical checks | — |
| 11 | final re-transcribe | audio.transcribe | whisper | selector via seam | — | execution only |

> 🔖 *Goal check:* steps 6–7 meet the goal fully. Steps 1–4 select from
> evidence but do not yet feed verdicts back. Steps 5, 8, 9 are the fleet
> gaps that make the cinema deliverable fakes-only today.

---

## 2. TODOs — known gaps and pitfalls (robust form)

Each TODO: **Why it matters to the goal** · **Where** · **Done when** (testable)
· **Effort** (S ≤ ½ day, M ≤ 2 days, L > 2 days) · **Pitfall** (what goes
wrong if skipped or done naïvely). `[EXT]` = needs more than code (see §3).

### A. Blocks the deliverable
- [ ] **TODO-1 `[EXT]` Seat an i2v model + clip judge.** Why: steps 8–9 are
  fakes; no cinema output exists. Where: GPU worker heartbeat
  `task_capabilities` += `video.generate.i2v`, `video.understand`;
  `router.resolve_route` video deferral (`router.py:277-290`); adapter + probe
  in `catalog.py`. Done when: `run_performance_on_dag` on the real fleet
  returns `visual.ok` with `clip:*` receipts naming a real model and
  `clipjudge:*` scorecards. Effort: M (+ download/licence). Pitfall: seating
  without a probe → an adapter that wants `prompt` vs `text` fails 40 min in.
- [ ] **TODO-1b `[EXT]` Seat chatterbox-tts + a speaker-embedding model.**
  Why: voice similarity is UNSCORED; `require_similarity` must stay False.
  Done when: `audio_master` scorecards carry a measured similarity and
  `SpeechPolicy(require_similarity=True)` passes on the happy path. Effort: M.

### B. Prayer points still in the live path
- [~] **TODO-2 Legacy pixel chain** (declared + switch landed; default flip = P8). Why: invariant 9 can be violated from the
  old UI while the oracle claims it holds. Where: `video_intel/runners/scene.py:438-453`
  (`chain=True` default, `scene_schema.py:66`), `runners/movie.py:319,415,565`,
  routes `video_routes.py:502,561`. Done when: `chain` defaults False, any
  chained run carries `limitations += "legacy pixel chain"` in its manifest,
  and the UI labels the job "legacy (pixel-chained)"; or the routes delegate to
  the recipe. Effort: S–M. Pitfall: flipping the default silently changes
  existing users' output — surface it.
- [x] **TODO-3 Verdict attribution for every inference point** (TTS, storyboard, authoring, keyframes, clips; ASR is execution-only). Why: selection
  for TTS/ASR/storyboard/authoring never learns. Where: `audio_master.build_audio_master`
  (per candidate: similarity + round-trip → `note_verdict`), `storyboard.py:819-880`,
  `script_first.resolve_authoring_model` + Track A/B rubric results,
  `evaluation.evaluate` (done). Done when: a live build writes ≥1 ledger row
  per TTS candidate, storyboard frame and authored artifact, attributed to the
  producing model; `ReliabilityLedger.capabilities()` lists them. Effort: M.
  Pitfall: attributing a verdict to the JUDGE model (see the DagRuntime guard).
- [x] **TODO-4 One selection policy** (`router._select_model`; legacy default is the fallback). Why: raw `/oracle/route` and any non-seam
  caller still get router defaults — two policies drift. Where:
  `router.resolve_route:307-330` → call `selection.select` for steps 5–9 when
  `requested_model is None`. Done when: a raw route response's `reasons`
  include the ledger/VRAM/budget explanation and `test_oracle_route.py` shows
  the default branch gone. Effort: M. Pitfall: recursion — the selector's
  catalog view must not call the router.
- [ ] **TODO-5 k113: `planner_mode` enforced + B→A disclosure gate + non-identifying fallback.**
  Where: `contracts.py:643`, `plan.py:553`, `py/hugpy_agent/.../mct/a_adapter.py`
  (consult `ArtifactManifest.frontier_may_access`), `authority.py:26,51`.
  Done when: a Frontier-disabled run cannot produce a plan with
  `planner_mode=frontier`; an unauthorized likeness yields traits-only output
  with a receipt saying so; an artifact with `frontier_may_access=False`
  never reaches A (test on the adapter). Effort: M–L. Pitfall `[EXT]`: needs the
  operator's written policy (P6).
- [ ] **TODO-6 k115 multi-judge evidence.** Where: `evaluation.run_judge/evaluate`,
  `contracts.Scorecard.confidence/disagreements`. Done when: identity/semantic
  classes run ≥2 independent judges (distinct models, neither the generator),
  `disagreements` names them when they differ, `confidence` is agreement-based,
  and `steward._calibration` uses it. Effort: M. Pitfall: two judges of the
  same model family agreeing is not independence — require distinct models.
- [ ] **TODO-7 k119 spatial evaluators + Track D.** Where: new
  `oracle/evaluators/spatial.py` over `spatial.ConditioningRequest` +
  rendered passes; `benchmark_cases.TRACKS += "D"`; emit GEOMETRY_DRIFT /
  CAMERA_PATH_MISMATCH / COLLISION_VIOLATION. Done when: a deliberately
  shifted silhouette/depth pass is rejected with the right code and the repair
  goes to `spatial.render`/`video.generate` only. Effort: L `[EXT]` (needs P4/P5).
- [ ] **TODO-8 Live `SpatialSceneManifest` producer.** Where: Fold 1 adapters
  (`oracle/adapters/spatial.py`, `physics.py`), `segments.compile_segments(spatial_refs=)`.
  Done when: a real run has `spatial_ref` set for ≥1 segment, the
  `spatial:<seg>` gate fires live and its `ConditioningRequest` reaches the
  generator adapter. Effort: L `[EXT]` (P3).
- [x] **TODO-9 Authoring ledgered.** Where: `script_first.resolve_authoring_model`
  + `screenplay.py` validators. Done when: plot/screenplay validator outcomes
  write to the ledger under `text.chat`/operation and a bad streak demotes the
  matrix primary within a session. Effort: S.
- [ ] **TODO-10 Peak-VRAM benchmarking + CLI.** Where: `benchmark.py:218-237`
  (`_vram_snapshot`), worker NVML sampling; create `scripts/oracle-benchmark`.
  Done when: a ceiling-mode row records `peak_vram_bytes` sampled during the
  run (not before/after), the CLI exists and is documented, and a test
  exercises VRAM capture with a fake sampler. Effort: M.

### C. Correctness hardening
- [x] **TODO-11 Static validation of the visual DAG** (`validate_visual_graph`; found and fixed a real port-type fault). Where:
  `recipes/video_performance.build_visual_graph` → `validator.validate(graph, catalog_view, goal)`
  before `DagRuntime.start`. Done when: a graph with a judge pinned to the
  generator or a port-kind mismatch is refused before any node runs; report on
  `VisualResult`. Effort: S.
- [ ] **TODO-12 Real concurrency for independent chains.** Where:
  `DagRuntime.step` (`max_parallel` is a serial slice). Done when: two segment
  chains execute on two workers concurrently with the journal as the only
  shared truth (leases prevent double execution). Effort: L (Phase 10).
- [ ] **TODO-13 Post-production spatial continuity.** Where: `postproduction.py`.
  Done when: cross-shot camera/placement checks produce notes and a repair
  code when adjacent shots contradict the continuity/spatial state. Effort: M.
- [ ] **TODO-14 One tone scale.** Where: `segments.py:441,651,976`, `production.py`,
  `performance.segment_context` (×10), `spatial.StyleSpec`. Done when: tone is
  0–10 on every contract with exactly one conversion at the legacy boundary,
  and `tone_profile()` is what the render adapters read. Effort: S. Pitfall:
  silent double-scaling (0.2 → 2.0 → 20).
- [ ] **TODO-15 Structured difficulty signals.** Where: `prompt_compiler.extract_signals`.
  Done when: signals come from shot plan/manifest fields (entities, tracks,
  contacts, camera move enum) with the regex path as labelled fallback.
  Effort: S–M.
- [ ] **TODO-16 Persist producer attribution.** Where: `selection._PRODUCERS`
  → `ArtifactManifest`/central. Done when: attribution survives a process
  restart and a cross-worker judge. Effort: M (with P9).
- [~] **TODO-17 Schedule the steward** (`deploy/hugpy-steward.{service,timer}` written; operator installs; UI feed pending). Where: systemd timer or `sentinel`
  case → `POST /oracle/steward`; alarms into the UI feed. Done when: an alarm
  (streak/gap-rate) appears in the console within one interval without a
  run. Effort: S.
- [ ] **TODO-18 UI/MCT surfaces.** Where: `react/video_intelligence_ui`,
  station console. Done when: per node the operator sees selection decision,
  candidates by model, scorecards, repair plans, steward report, DAG state;
  spatial overlays once P3 exists. Effort: L (P7).
- [ ] **TODO-19 Commit boundary + token rotation.** Done when: today's files
  are committed on `dev` separately from the foreign dirt and the two tokens
  are rotated. Effort: S `[EXT]` (operator).

## 3. Proposals that need more than internal code changes

These require operator decisions, hardware, external software, licensing, or
publishing — the codebase alone cannot close them.

### P1 — Seat the missing backends on the fleet (unblocks the deliverable)
- i2v: pick and install one image-to-video model the fleet can hold (80 GB now; Wan 2.1 i2v-14B 720p is already named in the catalog); register its adapter + probe; heartbeat `task_capabilities` must advertise `video.generate.i2v`.
- Clip judge: a video-capable VLM (Qwen2.5/3-VL) seated as `video.understand`.
- chatterbox-tts on a GPU worker; a speaker-embedding model (e.g. ECAPA/WavLM-SV) for `audio.speaker_similarity`.
- **Decision:** which worker hosts which; eviction policy under the allocator.

### P2 — Run the qualification sweep and publish the routing matrix
- Tracks A–C on every eligible model in both modes; fix peak-VRAM capture first (nvidia-smi / NVML sampling on the worker, not heartbeat delta).
- Output: versioned `routing_matrix.json` the selector verifies against `registry_version`.
- **Needs:** GPU time on all workers; operator sign-off on the published grid (HF dataset per the placement doc).

### P3 — Fold 1 spatial capture: assets, tools, hardware
- glTF/USD import adapters (FBX/OBJ/BVH via Blender or Assimp in a worker container) → `SpatialSceneManifest` producer; a rig library for the characters (licensed or authored).
- Physics backend (Tier 3): Blender/Bullet or MuJoCo/PhysX in a sandboxed worker; cache + settings provenance.
- Mocap (Tier 2): optical/skeletal capture source, or a video-pose pipeline (e.g. a pose-estimation model) as the first real-time-ish track.
- **Needs:** tool installs on workers, licenses for rigs/assets, possibly hardware.

### P4 — Fold 2 dense conditioning + Tier 3 neural scene backends
- ControlNet-style depth/normal/pose/segmentation conditioning for the seated image/video models (ComfyUI workflows behind the adapter, versioned).
- 3DGS / 4DGS / NeRF as separate backend capabilities (`spatial.scene.neural.*`) with measured drift thresholds.
- **Needs:** model weights + licenses, ComfyUI custom nodes, GPU headroom.

### P5 — Spatial evaluators (Track D) need reference data
- Landmark/silhouette/depth/flow metrics require ground truth from the manifest render (depth/normal passes) — depends on P3/P4; plus a face/identity embedding model for identity similarity.
- **Needs:** the same backends, plus a representative test-case set (static, moving character, moving camera, multi-character, props, occlusion, fast limbs, cloth/hair, cuts, tone endpoints) authored with rigs.

### P6 — Rights, consent and the Frontier disclosure policy (operator policy)
- A written authority policy: what counts as authorization for a real person's likeness/voice; how the non-identifying fallback is described to the operator; what B may disclose to A.
- **Needs:** an operator decision, then k113 enforces it.

### P7 — UI work in the React repo
- `react/video_intelligence_ui`: per-node selection/receipt/scorecard/repair/steward views, DAG state, candidate comparison, spatial overlays (skeleton/silhouette/depth/bbox/camera path).
- **Needs:** frontend work and a design pass; endpoints largely exist (`/oracle/selection`, `/oracle/steward`, `/oracle/route`, run manifests with `dag`).

### P8 — Legacy studio path decision
- Either retire `/video/jobs/generate_movie|scene` or relabel it "legacy (pixel-chained)" in the UI with the flag surfaced.
- **Needs:** operator decision; it changes what users of the old UI see.

### P9 — Persistence and fleet fan-out (Phase 10)
- Move producer attribution + reliability ledger to a shared store (central) so workers share evidence; run independent segment chains concurrently on different workers; keep the SQLite journal as the single workflow truth (evaluate Temporal only if the journal measurably limits).
- **Needs:** central schema/endpoints; deployment.

### P10 — Publishing and hygiene
- Commit boundary for today's work; rotate the two leaked tokens (already logged in the roadmap); publish the capability grid + stationary dataset to HF; align package versions (see `PLACEMENT-DISTRIBUTION.md` §4).

---

> 🔖 *Reading this map against the goal:* the **control plane is built and
> verified** (journal, selection, compiler, judges-not-generators, repair,
> steward, spatial contract). What separates today from a finished cinema
> build is **fleet seating (P1), evidence (P2), and geometry sources (P3–P5)**
> — external work — plus the internal TODOs 2–18 that close the remaining
> "prayer" points.
