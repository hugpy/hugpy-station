# observability — the Station's in-app trace browser

A browser inside the Station Electron app (with Chrome DevTools) that traces a round trip end to end:

```
1 action ─▶ listener ─▶ 2 JS stack ─▶ request (headers/body) ─▶ 3 Flask request ─▶ 4 handler + Python stack
                                                                                          │
6← consumer code ◀── 6 browser response ◀── HTTP status ◀── 5 output path / exception + traceback
```

**OFF by default, zero cost when off.** At startup `main.js` loads only `toggle.js`, which has no side
effects. The capture engine is required on first open, and only when the feature is enabled:

- turn it on from *Help → Enable observability* (saved in `<userData>/observability.json`), or force it
  with `HUGPY_STATION_OBSERVABILITY=1|0`;
- open it from *Help → Observability (trace browser)* or the 🌐 tab's "open live capture browser".

Closing the window detaches CDP and disarms everything. The page runs in its own partition
(`persist:hugpy-observability`) and shares no Station cookies.

## What each piece does

| piece | file | how |
|---|---|---|
| hop 1 action | `capture.js` | Capture-phase click/submit/change/key listeners run in an isolated world (`hugpy-obs`) and report through `Runtime.addBinding`. The page's JS is never patched. For each action, `DOMDebugger.getEventListeners` reads the listeners on the target and its ancestors: location, name, and source extent. The node is re-resolved into the page world, because listeners are per world. |
| action ↔ request | `link.js` | **Exact:** the root frame of the request's initiator chain (the bottom frame of the last async parent) must run inside one of the action's listeners. Among matching actions, the latest one before the request wins. This is an ordering rule, not a time window. **Inferred** (labelled as such): if the root is a non-timer task that no listener contains (React's scheduler: click → setState → `useEffect` → fetch has no V8 async edge), link it only to the immediately preceding action, and only within 1 s. Timer-, parser- and page-load-rooted requests are never attributed. |
| hop 2 stack | `capture.js` | `Network.requestWillBeSent.initiator` plus async parents (`Debugger.setAsyncCallStackDepth(32)`). `setSkipAllPauses(true)` stops any `debugger;` statement from freezing the page. |
| tag | `capture.js` | `Fetch.requestPaused` → `continueRequest` adds the contract headers per request (below). Only requests the filter targets are paused. A page-set id is replaced and kept as `pageTraceId`. |
| request | `capture.js` | Headers (authorization and cookies redacted) and a body preview (`previewBytes`, default 2 KB). |
| hops 3–5 | `spans.js` | Read-only `GET <origin>/api/console/trace?trace_id=<echoed id>`, retried until the row is final, because the server writes asynchronously. **Schema 2:** the server-recorded view, real `exception` / `handled` spans, and the request's traceback. **Schema 1** (live central today): the view is inferred under `dispatch_request` and a raise is reconstructed from return lines. |
| hop 6 + consumer | `capture.js`, `link.js` | Status, headers and body preview. A consumer is a console/exception entry whose async stack passes through a function of the request's initiator chain, logged after the response and before the next action (a stack join). DOM mutations within 300 ms of the response are listed separately and labelled as timing. |
| inference | `summary.js`, `explain.js` | `off` (default); `summary` (deterministic, local); `explain` (enables an **explicit per-trace button** that sends one trace to an OpenAI-compatible endpoint, by default hugpy central's `http://127.0.0.1:7002/v1`, configurable in the viewer). `infer()` never calls a model, and the IPC refuses unless the level is `explain`. |
| targeting | `filters.js`, viewer | `domains`, `routes`, `methods` (empty = all), `types` and `action` filters. *Tag only while armed*. *Trace next N actions* auto-saves the linked traces as one artifact when the run settles. Server span level `none\|handler\|app\|all` and `modules` globs are sent as headers. |
| saved traces | `artifacts.js` | `<userData>/observability/traces/*.json` plus `index.jsonl`, listed in the *saved* tab. Each save is also posted to the Station's provenance ledger (`/api/provenance/ingest`, kind `observability_trace`). The body names the handler and traceback `file:line` items, so provenance intersects them with edits. |

## Server contract (what the Station relies on)

This is the server half. hugpy central owns its implementation (`console_trace_routes.py`).
`test/contract/console_trace_ref.py` is a reference implementation, and the stub runs it.

- **Request headers (sent by the Station):**
  - `X-Hugpy-Trace: 1`
  - `X-Hugpy-Trace-Id: <one id per request>`
  - `X-Hugpy-Trace-Session: <grouping id>` (the page's own session header is kept)
  - `X-Hugpy-Trace-Spans: none|handler|app|all` (default `app`)
  - optional `X-Hugpy-Trace-Modules: glob,glob`
- **Response headers:**
  - `X-Hugpy-Trace-Id`: the id actually stored. A reused id is suffixed `~xxxx`, and the Station joins on the echo.
  - `X-Hugpy-Trace-Server: console_trace/2; spans=<level>`, or `…; skipped=rate|route`.
- **`GET /api/console/trace?trace_id=ID`** returns `{schema: 2, pending, request: {...}, spans: [...]}`.
  - `request` fields: `id, started_at, finished_at, method, path, status, duration_ms, session_id, requested_id, endpoint, view{function,module,file,line}, exception{type,message,traceback[{function,file,line,code,app}]}, error, spans_level, span_count, spans_truncated, pid`.
  - `spans` fields: `seq, event(call|return|exception|handled), depth, function, module, file, line, at, duration_ms, error`. A `return` after an unhandled exception carries `error: "unwound: …"`.
  - `pending: true` means the final write is still queued. The Station retries for ~7 s.
- `GET /api/console/trace?session_id=S` lists a session's requests. `GET /api/console/trace/stats` reports the writer's counters, including `dropped`.
- Writes never block a request and are never silently dropped. The store is bounded.

## Verification (`test/`, never shipped)

- `node --test observability/test/unit.test.js`: 23 tests. They cover filters, the toggle default, link tiers and extents, consumers, spans schema 1 and 2 (real stub fixtures), summary, explain (call count, bounded prompt) and artifacts.
- `python -m pytest observability/test/contract`: 12 tests of the reference server. They cover per-request ids and echo, real exceptions, handled exceptions, span levels and modules, 20 concurrent requests, a locked DB with no blocking and no loss, counted queue-full drops, bounded store, rate/route limits, and the existing-tracer guard.
- `test/contract/stub_server.py` runs on scratch state only. It provides:
  - a central-like app with the reference trace;
  - a demo page wired like the hugpy UI (delegated root listener, direct listener, poller);
  - an OpenAI-compatible stub;
  - Station provenance over the real `provenance.py`;
  - optionally, hugpy's built `console_dist`, served read-only in place.
- `test/e2e.js` drives the real window under `xvfb-run electron` with trusted clicks and writes `hops.json`, verdicts and screenshots. On hosts without unprivileged user namespaces, set `CHROME_DEVEL_SANDBOX=/opt/hugpy-station/chrome-sandbox`.
- Evidence: `test/evidence/phase2-*`.
