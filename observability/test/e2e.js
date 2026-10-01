'use strict';
// End-to-end verification harness (never shipped). Runs the REAL observability
// window under Electron, drives real (trusted) clicks into the inspected page,
// and writes the joined capture plus per-check verdicts as evidence.
//
//   scenario "contract" (default) — against test/contract/stub_server.py:
//     xvfb-run -a electron observability/test/e2e.js --url http://127.0.0.1:7393/ \
//         --provenance http://127.0.0.1:7394 --out /srv/vm_mgr/tmp/obs-evidence2/contract
//   scenario "ui" — any page, given selectors; --readonly-host cancels non-GET
//     writes; --filter '{"methods":["NONE"]}' disables tagging:
//     … e2e.js --scenario ui --url http://127.0.0.1:7395/ --click 'sel1,sel2' --out …
const { app, session } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');

const arg = (k, d) => { const i = process.argv.indexOf('--' + k); return i > 0 ? process.argv[i + 1] : d; };
const flag = (k) => process.argv.includes('--' + k);
const URL_ = arg('url');
const OUT = arg('out', '/srv/vm_mgr/tmp/obs-evidence2/run');
const SCENARIO = arg('scenario', 'contract');
const CLICKS = (arg('click', '') || '').split(',').filter(Boolean);
const FILTER = arg('filter') ? JSON.parse(arg('filter')) : null;
const READONLY = (arg('readonly-host', '') || '').split(',').filter(Boolean);
const PROV = arg('provenance', '');
const PARTITION = 'obs-harness';

if (process.env.OBS_NO_SANDBOX) app.commandLine.appendSwitch('no-sandbox');
if (process.env.OBS_NO_SHM) app.commandLine.appendSwitch('disable-dev-shm-usage');
if (process.env.OBS_NO_GPU) { app.disableHardwareAcceleration(); app.commandLine.appendSwitch('disable-gpu'); }
const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'obs-e2e-'));
app.setPath('userData', userData);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function brief(t) {
  const s = t.server || {};
  const l = t.link || {};
  return {
    key: t.key, traceId: t.traceId, serverTraceId: t.serverTraceId, req: `${t.method} ${t.path}`, type: t.resourceType,
    hop1_action: t.action ? `${t.action.action} ${t.action.target.selector} (trusted=${t.action.trusted})` : null,
    hop1_listener: l.listener ? `${l.listener.type} → ${l.listener.name || '(anon)'} on ${l.listener.node}` : null,
    hop1_join: t.action ? `${l.join}${l.sync ? ' (sync in listener)' : ' (async chain)'}` : `unlinked: ${l.reason}`,
    hop2_stack: t.initiator ? `${t.initiator.type}: ${(t.initiator.stack || []).slice(0, 4).map((f) => f.functionName || '(anon)').join(' < ')}` +
      ((t.initiator.asyncParents || []).length ? ` | ${t.initiator.asyncParents.map((p) => p.description + '[' + p.frames.slice(0, 2).map((f) => f.functionName || '(anon)').join(',') + ']').join(' < ')}` : '') : null,
    request_headers_on_wire: Object.fromEntries(Object.entries(t.sentHeaders || {}).filter(([k]) => /^x-hugpy/i.test(k))),
    request_body: t.request && t.request.postDataPreview,
    hop3_server: s.request ? `${s.request.method} ${s.request.path} -> ${s.request.status} endpoint=${s.request.endpoint} session=${s.request.session_id} spans=${s.level}(${s.counts.spans})` : (s.notes || []).join('; ') || null,
    hop4_handler: s.handler ? `${s.handler.function} ${s.handler.short}:${s.handler.line} [${s.handler.method}]` : null,
    hop4_callees: s.callees ? s.callees.filter((c) => c.app).map((c) => c.function + (c.raised ? '(raised)' : '')).slice(0, 6) : null,
    hop5_output: s.output ? s.output.map((o) => `${o.role}: ${o.function}`) : null,
    hop5_raised: s.raised ? `${s.raised.type}: ${s.raised.message} | ${s.raised.method} | ` + s.raised.traceback.filter((f) => f.app !== false).map((f) => `${f.function}:${f.line}`).join(' -> ') : null,
    hop5_exception_spans: (s.exceptions || []).map((x) => `${x.function}:${x.line} ${x.error}`).slice(0, 5),
    hop5_handled: (s.handled || []).map((x) => `${x.function}:${x.line}`),
    hop6_response: t.response ? (t.response.failed ? `FAILED ${t.response.errorText}` : `${t.response.status} ${t.response.mimeType} ${t.response.encodedDataLength}B ${Math.round(t.response.totalMs)}ms ${t.serverHeader || ''}`) : null,
    hop6_body: t.response && t.response.body ? String(t.response.body).slice(0, 160) : null,
    hop6_consumers: (t.consumers || []).map((c) => `[${c.level}] ${c.text.slice(0, 60)} via ${c.via} (${c.join})`),
    hop6_dom: (t.domEffects || []).map((d) => `${d.type} ${d.selector} +${d.afterMs}ms (${d.join})`).slice(0, 3),
  };
}

app.whenReady().then(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const log = [];
  const checks = [];
  const note = (s) => { log.push(s); console.log('[e2e]', s); };
  const check = (name, ok, detail) => { checks.push({ name, ok: !!ok, detail }); note(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' — ' + (typeof detail === 'string' ? detail : JSON.stringify(detail)) : ''}`); };
  const toggle = require('../toggle');
  check('feature OFF by default (fresh userData, env unset)', toggle.isEnabled(userData) === false || !!process.env.HUGPY_STATION_OBSERVABILITY);
  toggle.setEnabled(userData, true);

  const blocked = [];
  if (READONLY.length) {
    session.fromPartition(PARTITION).webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (d, cb) => {
      let host = ''; try { host = new URL(d.url).host; } catch (_) {}
      if (READONLY.includes(host) && !['GET', 'HEAD', 'OPTIONS'].includes(d.method)) { blocked.push(`${d.method} ${d.url}`); return cb({ cancel: true }); }
      cb({});
    });
  }

  const origin = new URL(URL_).origin;
  const obs = require('../index');
  const h = await obs.open({ userData, url: URL_, partition: PARTITION,
    filter: FILTER || { domains: [new URL(URL_).host], routes: ['/api/*'] },
    explain: { url: `${origin}/v1`, model: '' },
    provenanceUrl: PROV ? `${PROV}/api/provenance/ingest` : '' });
  const wc = h.view.webContents;
  const viewer = (js) => h.win.webContents.executeJavaScript(js);
  await new Promise((r) => (wc.isLoading() ? wc.once('did-stop-loading', r) : r()));
  await sleep(1200);
  note(`loaded ${wc.getURL()} capture.active=${h.capture.active} session=${h.capture.sessionId}`);

  const click = async (sel) => {
    const rect = await wc.executeJavaScript(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return null; e.scrollIntoView({block:'center'}); const r = e.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()`);
    if (!rect) { note(`click ${sel}: element not found`); return null; }
    wc.focus();
    const x = Math.round(rect.x), y = Math.round(rect.y);
    wc.sendInputEvent({ type: 'mouseDown', x, y, button: 'left', clickCount: 1 });
    wc.sendInputEvent({ type: 'mouseUp', x, y, button: 'left', clickCount: 1 });
    note(`clicked ${sel} at ${x},${y}`);
    return true;
  };
  const settle = async (max = 15000) => {
    const end = Date.now() + max;
    while (Date.now() < end) {
      const busy = [...h.capture.traces.values()].filter((t) => !t.response || (t.injected && (!t.server || t.server.joining)));
      if (!busy.length) return true;
      await sleep(200);
    }
    return false;
  };
  const tracesFor = (sel) => h.capture.list().filter((t) => t.action && t.action.target && t.action.target.selector === sel);
  const shot = async (key, name) => {
    try {
      await viewer(`(() => { const r = document.querySelector('tr[data-key="${key}"]'); if (r) r.click(); })()`);
      await sleep(700);
      fs.writeFileSync(path.join(OUT, name), (await h.win.webContents.capturePage()).toPNG());
    } catch (e) { note('screenshot failed: ' + e.message); }
  };

  if (SCENARIO === 'ui') {
    for (const sel of CLICKS) { await click(sel); await sleep(1500); }
    await settle();
  } else try {
    // ── contract scenario ───────────────────────────────────────────────────
    await sleep(2000);                               // let the setInterval poller tick
    await click('#load'); await sleep(900);
    await click('#save'); await sleep(900);
    wc.openDevTools({ mode: 'detach' }); await sleep(1500);
    check('DevTools coexists with the CDP capture', wc.isDevToolsOpened() && h.capture.active && wc.debugger.isAttached());
    await click('#chain'); await sleep(1200);
    await click('#boom'); await sleep(900);
    await click('#handled'); await sleep(900);
    await click('#xhr'); await sleep(900);
    await click('#noop'); await sleep(500);
    await click('#burst'); await sleep(1500);
    check('all tagged requests settled (response + server join)', await settle(20000));

    const finalize = (t) => h.capture.finalize(t);
    const one = (sel, pathRe) => tracesFor(sel).map(finalize).find((t) => pathRe.test(t.path));
    const full = (t) => t && t.action && t.link.join === 'listener-stack' && t.injected && t.server && t.server.request &&
      t.server.request.finished_at && t.server.handler && t.response && !t.response.failed;

    // GET
    const g = one('button#load', /^\/api\/items\?n=3/);
    check('GET: all 6 hops joined', full(g) && g.server.handler.function === 'list_items' && g.response.status === 200, g && brief(g));
    check('GET: listener is the delegated root listener', g && g.link.listener && g.link.listener.name === 'dispatchRootClick' && /div#root/.test(g.link.listener.node));
    check('GET: response consumed by page code (stack join)', g && g.consumers.some((c) => /items loaded/.test(c.text)));
    // POST
    const p = one('button#save', /^\/api\/items$/);
    check('POST: all 6 hops joined', full(p) && p.method === 'POST' && p.server.handler.function === 'create_item' && p.response.status === 201, p && brief(p));
    check('POST: request body preview + handler callees', p && /widget/.test(p.request.postDataPreview || '') && p.server.callees.some((c) => c.function === '_validate'));
    // await chain
    const ch = tracesFor('button#chain').map(finalize);
    check('await chain: 2 requests, both linked through async parents', ch.length === 2 && ch.every((t) => full(t) && !t.link.sync &&
      (t.initiator.asyncParents || []).some((x) => /await/.test(x.description))), ch.map(brief));
    check('await chain: handlers list_items then get_item', ch.map((t) => t.server && t.server.handler && t.server.handler.function).join(',') === 'list_items,get_item');
    // raise
    const b = one('button#boom', /^\/api\/boom/);
    check('raise: all 6 hops joined, 500', full(b) && b.response.status === 500, b && brief(b));
    check('raise: real server traceback boom → _load_config → _explode', b && b.server.raised && b.server.raised.type === 'ValueError' &&
      b.server.raised.traceback.filter((f) => f.app).map((f) => f.function).join('>').endsWith('boom>_load_config>_explode'));
    check('raise: exception span events (not reconstructed)', b && b.server.exceptions.length >= 3 && /schema 2/.test(b.server.raised.method));
    check('raise: consumer is the page catch (console.error)', b && b.consumers.some((c) => c.level === 'error' && /boom failed/.test(c.text)));
    const hd = one('button#handled', /^\/api\/handled/);
    check('handled raise: shown as handled, not as an error', hd && !hd.server.raised && hd.server.handled.some((x) => x.function === 'handled') && hd.response.status === 200);
    const x = one('button#xhr', /^\/api\/slow/);
    check('XHR from a direct listener: linked sync', x && x.link.sync && x.link.listener && x.link.listener.name === 'onXhrClick' && full(x));
    // timers are not user actions
    const polls = h.capture.list().filter((t) => /^\/api\/poll/.test(t.path));
    check('poller requests are NOT attributed to actions', polls.length >= 2 && polls.every((t) => !t.action && t.link.origin && t.link.origin.kind === 'timer'),
      polls.map((t) => t.link && t.link.reason));
    // burst
    const burst = tracesFor('button#burst').map(finalize);
    const ids = new Set(burst.map((t) => t.traceId));
    const complete = burst.filter((t) => full(t) && t.server.handler.function === 'get_item' && t.server.counts.spans > 0);
    let stats = null;
    try { stats = await (await fetch(`${origin}/api/console/trace/stats`)).json(); } catch (_) {}
    check('burst: 20 distinct per-request trace ids', burst.length === 20 && ids.size === 20, { requests: burst.length, ids: ids.size });
    check('burst: 20 complete traces (row final + handler + spans)', complete.length === 20, { complete: complete.length });
    check('server: zero dropped trace writes', stats && stats.dropped && stats.dropped.queue_full === 0 && stats.dropped.write_error === 0, stats);
    const sess = new Set(burst.map((t) => t.server && t.server.request && t.server.request.session_id));
    check('burst: all grouped under the window session id', sess.size === 1 && sess.has(h.capture.sessionId));
    await shot(g.key, 'viewer-get.png');
    await shot(p.key, 'viewer-post.png');
    await shot(b.key, 'viewer-raise.png');

    // explain: refused unless the level is 'explain'; one call per press
    const llm = async () => (await (await fetch(`${origin}/stub/llm-calls`)).json()).count;
    const before = await llm();
    await viewer(`window.hugpyObs.setInference('summary')`);
    const refused = await viewer(`window.hugpyObs.explain(${JSON.stringify(b.key)})`);
    const mid = await llm();
    check('explain refused at level "summary" and no model call', refused && !refused.ok && mid === before, refused);
    await viewer(`window.hugpyObs.setInference('explain')`);
    const ex = await viewer(`window.hugpyObs.explain(${JSON.stringify(b.key)})`);
    const after = await llm();
    check('explain at level "explain": exactly one model call', ex && ex.ok && after === before + 1 && /STUB EXPLANATION/.test(ex.explanation.text), ex && ex.explanation);
    await shot(b.key, 'viewer-raise-explained.png');
    await viewer(`window.hugpyObs.setInference('summary')`);

    // save + provenance
    const saved = await viewer(`window.hugpyObs.save(${JSON.stringify(b.key)})`);
    check('save: artifact written + provenance event', saved && saved.ok && fs.existsSync(saved.file) && saved.provenance && saved.provenance.ok, saved);

    // trace next N actions (tag only while armed)
    await viewer(`window.hugpyObs.setFilter({ tagWhen: 'armed' })`);
    const armedDone = new Promise((r) => { h.onArmedDone = (d, entry) => r({ d, entry }); });
    await viewer(`window.hugpyObs.arm(2)`);
    await click('#load'); await sleep(700);
    await click('#boom');
    const ad = await Promise.race([armedDone, sleep(20000).then(() => null)]);
    check('armed: "trace next 2 actions" auto-saved a bundle of the linked traces', ad && ad.entry && ad.entry.count >= 2 && ad.entry.provenance.ok, ad && ad.entry);
    await sleep(2500);
    const lateLoad = [...h.capture.traces.values()].filter((t) => /^\/api\/poll/.test(t.path) && t.wallTime * 1000 > Date.now() - 2000);
    check('armed: after the run, tagging stops (tagWhen=armed)', lateLoad.every((t) => !t.injected), lateLoad.map((t) => t.traceId));
    // the saved list (viewer) and the provenance ledger
    const list = await viewer(`window.hugpyObs.saved()`);
    check('saved tab lists the artifacts', Array.isArray(list) && list.length >= 2, list && list.map((e) => e.file));
    if (PROV) {
      const pv = await (await fetch(`${PROV}/api/provenance`)).json();
      const evs = (pv.events || []).filter((e) => e.kind === 'observability_trace');
      check('provenance ledger has observability_trace events with server file items', evs.length >= 2 && evs.some((e) => (e.items || []).some((i) => /stub_server\.py/.test(i.item))),
        evs.map((e) => ({ id: e.id, artifact: e.body.artifact, items: (e.items || []).map((i) => `${i.item}:${i.line}`).slice(0, 4) })));
    }
  } catch (err) {
    check('scenario ran to the end', false, err.stack);
  }

  const file = h.exportTo(OUT);
  const traces = h.capture.list().map((t) => h.capture.finalize(t));
  fs.writeFileSync(path.join(OUT, 'hops.json'), JSON.stringify({ url: URL_, scenario: SCENARIO, session: h.capture.sessionId,
    filter: h.capture.filter, server: h.capture.server, log, blocked, checks, hops: traces.map(brief),
    actions: h.capture.actions.map((a) => ({ id: a.id, action: a.action, target: a.target.selector, listeners: a.listeners, err: a.listenerError })),
    export: file }, null, 2));
  const failed = checks.filter((c) => !c.ok);
  note(`wrote ${path.join(OUT, 'hops.json')} (${traces.length} traces, ${checks.length - failed.length}/${checks.length} checks passed, ${blocked.length} blocked writes)`);
  const savedDir = path.join(userData, 'observability', 'traces');
  note(`saved artifacts: ${fs.existsSync(savedDir) ? fs.readdirSync(savedDir).length : 0} file(s) in ${savedDir}`);
  if (fs.existsSync(savedDir)) fs.cpSync(savedDir, path.join(OUT, 'saved-traces'), { recursive: true });
  obs.close();
  await sleep(300);
  fs.rmSync(userData, { recursive: true, force: true });
  app.exit(failed.length ? 2 : 0);
}).catch((e) => { console.error('[e2e] FAILED', e); app.exit(1); });
