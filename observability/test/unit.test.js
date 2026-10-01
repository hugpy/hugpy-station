'use strict';
// node --test station-app/observability/test/unit.test.js   (no Electron needed)
// Fixtures are real GET /api/console/trace?trace_id=… responses from
// test/target_app.py, which mounts hugpy central's unmodified console_trace module.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const filters = require('../filters');
const spans = require('../spans');
const summary = require('../summary');
const toggle = require('../toggle');
const link = require('../link');
const explain = require('../explain');
const artifacts = require('../artifacts');

const fx = (n) => JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', n), 'utf8'));

test('filters: defaults record XHR/Fetch everywhere and tag every method; tagWhen', () => {
  assert.ok(filters.shouldRecord({}, 'http://a:1/api/x', 'Fetch'));
  assert.ok(!filters.shouldRecord({}, 'http://a:1/app.js', 'Script'));
  assert.ok(filters.shouldInject({}, 'http://a:1/api/x', 'XHR', 'GET'));
  assert.ok(filters.shouldInject({}, 'http://a:1/api/x', 'XHR', 'POST'));
  assert.ok(!filters.shouldInject({ methods: ['GET'] }, 'http://a:1/api/x', 'XHR', 'POST'));
  assert.strictEqual(filters.normalize({}).tagWhen, 'always');
  assert.strictEqual(filters.normalize({ tagWhen: 'armed' }).tagWhen, 'armed');
  assert.strictEqual(filters.normalize({ tagWhen: 'bogus' }).tagWhen, 'always');
});

test('filters: domain / route / method targeting', () => {
  const f = { domains: '127.0.0.1:7002, *.hugpy.ai', routes: ['/api/*'], methods: ['GET', 'POST'] };
  assert.ok(filters.shouldInject(f, 'http://127.0.0.1:7002/api/readiness', 'Fetch', 'POST'));
  assert.ok(filters.shouldInject(f, 'https://dev.hugpy.ai/api/x', 'Fetch', 'GET'));
  assert.ok(!filters.shouldRecord(f, 'http://127.0.0.1:7006/api/x', 'Fetch'));
  assert.ok(!filters.shouldRecord(f, 'http://127.0.0.1:7002/llm/workers', 'Fetch'));
  assert.ok(!filters.shouldInject({ ...f, methods: ['GET'] }, 'http://127.0.0.1:7002/api/x', 'Fetch', 'DELETE'));
  assert.ok(!filters.shouldRecord(f, 'chrome-extension://x/y', 'Fetch'));
});

test('filters: action filter and Fetch patterns', () => {
  const a = { action: 'click', target: { selector: 'button#save', text: 'Save' } };
  assert.ok(filters.actionMatches({ action: 'save' }, a));
  assert.ok(!filters.actionMatches({ action: 'delete' }, a));
  assert.ok(!filters.actionMatches({ action: 'save' }, null));
  assert.ok(filters.actionMatches({}, null));
  assert.deepStrictEqual(filters.fetchPatterns({ domains: ['127.0.0.1:7002'], types: ['Fetch'] }),
    [{ urlPattern: '*://127.0.0.1:7002/*', requestStage: 'Request', resourceType: 'Fetch' }]);
});

test('spans: normal request -> handler, callees, output path', () => {
  const a = spans.analyze(fx('console_trace_fx-items.json'));
  assert.ok(a.supported && a.reached);
  assert.strictEqual(a.request.status, 200);
  assert.strictEqual(a.handler.function, 'list_items');
  assert.strictEqual(a.handler.method, 'dispatch_request');
  assert.ok(a.callees.some((c) => c.function === 'build_items' && c.app));
  assert.deepStrictEqual(a.output.map((o) => o.function).slice(0, 2), ['list_items', 'finalize_request']);
  assert.strictEqual(a.output[a.output.length - 1].function, 'HTTP 200');
  assert.strictEqual(a.raised, null);
});

test('spans: a raise is reconstructed from return lines (setprofile has no exception events)', () => {
  const a = spans.analyze(fx('console_trace_fx-boom.json'));
  assert.strictEqual(a.counts.exceptions, 0, 'console_trace never stores exception spans');
  assert.strictEqual(a.handler.function, 'boom');
  assert.ok(a.raised);
  assert.strictEqual(a.raised.via, 'handle_user_exception');
  assert.deepStrictEqual(a.raised.traceback.map((f) => f.function), ['dispatch_request', 'boom', '_explode']);
  assert.strictEqual(a.output[0].role, 'handler unwound (raised)');
  assert.strictEqual(a.request.status, 500);
});

test('spans: missing row with spans still counts as reached; non-console_trace origin is unsupported', () => {
  const doc = fx('console_trace_fx-items.json');
  const a = spans.analyze({ request: null, spans: doc.spans });
  assert.ok(a.reached && !a.request && a.handler.function === 'list_items');
  assert.ok(a.notes.some((n) => /request row is missing/.test(n)));
  const b = spans.analyze({ request: null, spans: [] });
  assert.ok(!b.reached);
  assert.ok(!spans.analyze(null).supported);
  assert.ok(!spans.analyze({ error: 'not found' }).supported);
});

test('summary: off by default, deterministic summary; explain level never calls a model by itself', () => {
  const t = { method: 'GET', path: '/api/items?n=2', injected: true, traceId: 'obs-1',
    action: { action: 'click', target: { selector: 'button#load', text: 'Load' } },
    link: { join: 'listener-stack', sync: true, listener: { type: 'click', name: 'onLoad', node: 'button#load' } },
    initiator: { type: 'script', stack: [{ functionName: 'loadItems', url: 'http://x/app.js', lineNumber: 2 }], asyncParents: [] },
    server: spans.analyze(fx('console_trace_fx-items.json')),
    response: { status: 200, mimeType: 'application/json', encodedDataLength: 200, totalMs: 40 } };
  assert.strictEqual(summary.infer(t, undefined).text, '');
  const s = summary.infer(t, 'summary').text;
  for (const k of ['1) click on button#load', 'click listener onLoad', 'joined by listener-stack', 'loadItems @ app.js:3',
    '3) reached Flask', '4) handler list_items', '6) browser received 200'])
    assert.ok(s.includes(k), `summary missing "${k}":\n${s}`);
  const ex = summary.infer(t, 'explain');
  assert.strictEqual(ex.explain, null);
  assert.match(ex.note, /press "explain"/);
  const inf = summary.infer({ ...t, link: { join: 'scheduled-after-action', inferred: true, reason: 'inferred: root x is a scheduled task' } }, 'summary').text;
  assert.match(inf, /INFERRED, no stack link/);
});

test('toggle: OFF by default, persisted, env override wins', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'obs-toggle-'));
  const saved = process.env.HUGPY_STATION_OBSERVABILITY;
  try {
    delete process.env.HUGPY_STATION_OBSERVABILITY;
    assert.strictEqual(toggle.isEnabled(dir), false);
    assert.ok(!fs.existsSync(toggle.configPath(dir)), 'reading the toggle must not create files');
    toggle.setEnabled(dir, true);
    assert.strictEqual(toggle.isEnabled(dir), true);
    process.env.HUGPY_STATION_OBSERVABILITY = '0';
    assert.strictEqual(toggle.isEnabled(dir), false);
    toggle.setEnabled(dir, false); delete process.env.HUGPY_STATION_OBSERVABILITY;
    process.env.HUGPY_STATION_OBSERVABILITY = '1';
    assert.strictEqual(toggle.isEnabled(dir), true);
    assert.deepStrictEqual(toggle.load(dir).filter.methods, []);
    assert.strictEqual(toggle.load(dir).inference, 'off');
    assert.deepStrictEqual(toggle.load(dir).server, { spans: 'app', modules: '' });
    assert.strictEqual(toggle.load(dir).explain.url, 'http://127.0.0.1:7002/v1');
    toggle.save(dir, { explain: { model: 'm' } });
    assert.strictEqual(toggle.load(dir).explain.url, 'http://127.0.0.1:7002/v1', 'partial explain config keeps defaults');
  } finally {
    if (saved === undefined) delete process.env.HUGPY_STATION_OBSERVABILITY; else process.env.HUGPY_STATION_OBSERVABILITY = saved;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// ── link.js: action ↔ request by listener stacks ─────────────────────────────
const F = (functionName, lineNumber, columnNumber, scriptId = '7') => ({ functionName, lineNumber, columnNumber, scriptId, url: 'http://x/app.js' });
const rootListener = { type: 'click', name: 'dispatchRootClick', node: 'div#root', scriptId: '7', lineNumber: 40, columnNumber: 26,
  ...link.extentOf('function dispatchRootClick(ev) {\n  go(ev);\n}', 40, 26) };

test('link: extentOf steps back over "function name" and spans the source', () => {
  assert.deepStrictEqual(link.extentOf('function f(a) {\n  x();\n}', 10, 10), { startColumn: 0, endLine: 12, endColumn: 1 });
  assert.deepStrictEqual(link.extentOf('(e) => go(e)', 0, 500), { startColumn: 500, endLine: 0, endColumn: 512 });
  assert.deepStrictEqual(link.extentOf('function () { [native code] }', 0, 5), {});
});

test('link: a request whose root frame runs in the action listener is linked (sync and async)', () => {
  const actions = [{ id: 1, action: 'click', ts: 1000, target: { selector: 'button#load' }, listeners: [rootListener] }];
  const sync = { wallTime: 1.01, initiator: { type: 'script', stack: [F('api', 3, 10), F('load', 20, 5), F('dispatchRootClick', 41, 4)], asyncParents: [] } };
  const r = link.linkRequest(sync, actions);
  assert.strictEqual(r.action.id, 1); assert.strictEqual(r.join, 'listener-stack'); assert.ok(r.sync);
  const asyncT = { wallTime: 1.2, initiator: { type: 'script', stack: [F('api', 3, 10)],
    asyncParents: [{ description: 'await', frames: [F('chain', 30, 2), F('dispatchRootClick', 41, 4)] }] } };
  const r2 = link.linkRequest(asyncT, actions);
  assert.strictEqual(r2.join, 'listener-stack'); assert.ok(!r2.sync);
});

test('link: timers, page load and non-listener roots are never attributed by stack', () => {
  const actions = [{ id: 1, action: 'click', ts: 1000, target: {}, listeners: [rootListener] }];
  const poll = { wallTime: 1.05, initiator: { type: 'script', stack: [F('pollStatus', 90, 3)],
    asyncParents: [{ description: 'setInterval', frames: [F('', 90, 0)] }] } };
  const r = link.linkRequest(poll, actions);
  assert.strictEqual(r.action, null); assert.match(r.reason, /timer-driven \(setInterval\)/);
  const parser = { wallTime: 1.05, initiator: { type: 'parser' } };
  assert.strictEqual(link.linkRequest(parser, actions).action, null);
  // root outside every listener's extent, same script, AFTER the listener start: position alone must not link
  const other = { wallTime: 5, initiator: { type: 'script', stack: [F('', 400, 9)], asyncParents: [] } };
  assert.strictEqual(link.linkRequest(other, actions, { inferScheduled: false }).action, null);
  // a request sent BEFORE the action is never linked to it
  const early = { wallTime: 0.5, initiator: sync0() };
  assert.strictEqual(link.linkRequest(early, actions).action, null);
  function sync0() { return { type: 'script', stack: [F('dispatchRootClick', 41, 4)], asyncParents: [] }; }
});

test('link: a pending newer action blocks falling back to an older one; latest matching action wins', () => {
  const a1 = { id: 1, action: 'click', ts: 1000, target: {}, listeners: [rootListener] };
  const a2 = { id: 2, action: 'click', ts: 2000, target: {}, listeners: null };
  const t = { wallTime: 2.01, initiator: { type: 'script', stack: [F('dispatchRootClick', 41, 4)], asyncParents: [] } };
  const r = link.linkRequest(t, [a1, a2]);
  assert.strictEqual(r.action, null); assert.ok(r.pending);
  a2.listeners = [rootListener];
  assert.strictEqual(link.linkRequest(t, [a1, a2]).action.id, 2);
});

test('link: inferred scheduler-task tier is labelled and bounded', () => {
  const a1 = { id: 1, action: 'click', ts: 1000, target: {}, listeners: [rootListener] };
  const sched = (wall) => ({ wallTime: wall, initiator: { type: 'script', stack: [F('fetchWorkers', 900, 1)],
    asyncParents: [{ description: 'await', frames: [F('', 1200, 7)] }] } });
  const r = link.linkRequest(sched(1.004), [a1]);
  assert.strictEqual(r.join, 'scheduled-after-action'); assert.ok(r.inferred); assert.match(r.reason, /^inferred:/);
  assert.strictEqual(link.linkRequest(sched(2.5), [a1]).action, null, 'outside the window');
  assert.strictEqual(link.linkRequest(sched(1.004), [{ ...a1, listeners: [] }]).action, null, 'action hit no page listener');
});

test('link: bound/native listeners (no extent) match by exact name only', () => {
  const bound = { type: 'click', name: 'Vn', scriptId: '7', lineNumber: 1, columnNumber: 5 };
  assert.ok(link.listenerContains(bound, F('Vn', 1, 900)));
  assert.ok(!link.listenerContains(bound, F('', 1, 900)));
  assert.ok(!link.listenerContains(bound, F('Wn', 1, 900)));
});

test('link: consumers are joined by stack, after the response, before the next action', () => {
  const t = { initiator: { type: 'script', stack: [F('api', 3, 10), F('load', 20, 5)], asyncParents: [] },
    response: { finishedWall: 10 } };
  const entries = [
    { level: 'log', tsWall: 9990, text: 'too early', stack: [F('load', 22, 1)] },
    { level: 'log', tsWall: 10020, text: 'items loaded', stack: [F('load', 22, 1)] },
    { level: 'log', tsWall: 10030, text: 'unrelated', stack: [F('other', 50, 1)] },
    { level: 'log', tsWall: 12000, text: 'next click', stack: [F('load', 22, 1)] },
  ];
  const c = link.consumers(t, entries, { before: 11000 });
  assert.deepStrictEqual(c.map((x) => x.text), ['items loaded']);
  assert.strictEqual(c[0].via, 'load'); assert.strictEqual(c[0].join, 'stack');
});

test('link: functionNameAt reads names at a listener location', () => {
  const src = 'x;\nconst a = 1; function onSave(e) { }\nel.onclick = (e) => go();\nconst h = { onKey(e) { } };';
  assert.strictEqual(link.functionNameAt(src, 1, 13), 'onSave');
  assert.strictEqual(link.functionNameAt(src, 3, 12), 'onKey');
});

// ── spans.js: schema 2 (console_trace v2 reference, real stub responses) ─────
test('spans v2: the server view is authoritative; session and level are carried', () => {
  const a = spans.analyze(fx('console_trace_v2-items.json'));
  assert.strictEqual(a.schema, 2);
  assert.strictEqual(a.handler.function, 'list_items');
  assert.strictEqual(a.handler.method, 'server view (url_rule)');
  assert.strictEqual(a.handler.endpoint, 'list_items');
  assert.ok(a.handler.returned);
  assert.ok(a.callees.some((c) => c.function === 'build_items'));
  assert.strictEqual(a.request.session_id, 'fx-session');
  assert.strictEqual(a.level, 'app');
  assert.ok(!a.pending && !a.raised);
});

test('spans v2: a raise has real exception spans and the server traceback', () => {
  const a = spans.analyze(fx('console_trace_v2-boom.json'));
  assert.strictEqual(a.request.status, 500);
  assert.strictEqual(a.raised.type, 'ValueError');
  assert.match(a.raised.method, /schema 2/);
  assert.deepStrictEqual(a.raised.traceback.filter((f) => f.app).map((f) => f.function).slice(-3), ['boom', '_load_config', '_explode']);
  assert.ok(a.exceptions.length >= 3);
  assert.strictEqual(a.output[0].role, 'handler unwound (raised)');
  assert.ok(a.callees.some((c) => c.function === '_load_config' && c.raised));
});

test('spans v2: a handled exception is reported as handled, not raised', () => {
  const a = spans.analyze(fx('console_trace_v2-handled.json'));
  assert.strictEqual(a.raised, null);
  assert.ok(a.handled.some((h) => h.function === 'handled'));
  assert.ok(a.exceptions.some((e) => e.function === '_risky'));
  assert.strictEqual(a.output[0].role, 'handler returned');
});

test('spans: a pending (queued) row is not final', () => {
  const doc = fx('console_trace_v2-items.json');
  const a = spans.analyze({ ...doc, pending: true, request: { ...doc.request, finished_at: null } });
  assert.ok(a.pending);
});

// ── explain.js / artifacts.js ────────────────────────────────────────────────
test('explain: one POST per call, bounded prompt, model from /models when unset', async () => {
  const calls = [];
  const fake = async (url, init) => {
    calls.push({ url, method: (init && init.method) || 'GET', body: init && init.body });
    if (url.endsWith('/models')) return { ok: true, status: 200, json: async () => ({ data: [{ id: 'm1' }] }) };
    return { ok: true, status: 200, json: async () => ({ choices: [{ message: { content: 'because' } }] }) };
  };
  const big = { method: 'GET', url: 'http://x/api', path: '/api', response: { body: 'x'.repeat(50000), status: 200 } };
  const r = await explain.explain(big, { url: 'http://h:1/v1/' }, fake);
  assert.strictEqual(r.text, 'because'); assert.strictEqual(r.model, 'm1');
  assert.deepStrictEqual(calls.map((c) => `${c.method} ${c.url}`), ['GET http://h:1/v1/models', 'POST http://h:1/v1/chat/completions']);
  const body = JSON.parse(calls[1].body);
  assert.ok(body.messages[1].content.length < 15000);
  calls.length = 0;
  await explain.explain(big, { url: 'http://h:1/v1', model: 'given' }, fake);
  assert.deepStrictEqual(calls.map((c) => c.method), ['POST']);
});

test('artifacts: save, list, load (confined to the traces dir), provenance event names server files', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'obs-art-'));
  try {
    const t = { traceId: 'obs-9', key: 'k', method: 'GET', url: 'http://x/api/boom', path: '/api/boom',
      response: { status: 500 }, server: spans.analyze(fx('console_trace_v2-boom.json')),
      action: { action: 'click', target: { selector: 'button#boom' } }, link: { join: 'listener-stack' } };
    const e = artifacts.save(dir, t, { session: 's1' });
    assert.ok(fs.existsSync(e.file));
    assert.strictEqual(e.traces[0].handler.split(' ')[0], 'boom');
    assert.match(e.traces[0].raised, /^ValueError/);
    const list = artifacts.list(dir);
    assert.strictEqual(list.length, 1);
    assert.strictEqual(artifacts.load(dir, list[0].file).traces[0].traceId, 'obs-9');
    assert.throws(() => artifacts.load(dir, '/etc/passwd'));
    const ev = artifacts.provenanceEvent(e, [t]);
    assert.strictEqual(ev.kind, 'observability_trace');
    assert.ok(ev.body.server_items.some((i) => /stub_server\.py:\d+$/.test(i)));
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('artifacts: provenance ingest is skipped without a url and never throws', async () => {
  assert.deepStrictEqual(await artifacts.toProvenance('', {}, []), { ok: false, skipped: 'no provenance url' });
  const r = await artifacts.toProvenance('http://127.0.0.1:9/x', { traces: [] }, [], async () => { throw new Error('down'); });
  assert.strictEqual(r.ok, false); assert.strictEqual(r.error, 'down');
});
