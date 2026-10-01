'use strict';
// hugpy Station observability — the trace browser window.
//
// Loaded LAZILY by main.js, and only when the toggle (toggle.js) is on. One
// window: the viewer (viewer.html, sandboxed, preload bridge only) on the left
// and the page under inspection on the right as a native WebContentsView in its
// OWN session partition (persist:hugpy-observability), so nothing it does
// touches the Station's cookies, its /ac console view or its backend.
// capture.js does the CDP work; link.js joins actions/requests/consumers by
// stacks; spans.js joins the server's spans (README.md, "Server contract"); summary.js and
// explain.js are the inference levels; artifacts.js saves traces (+ provenance).
// Closing the window detaches everything.

const electron = require('electron');
const { BrowserWindow, ipcMain } = electron;
const path = require('path');
const fs = require('fs');
const toggle = require('./toggle');
const { Capture } = require('./capture');
const summary = require('./summary');
const artifacts = require('./artifacts');
const explainer = require('./explain');

let current = null;
let ipcRegistered = false;

function row(t) {
  let host = '';
  try { host = new URL(t.url).host; } catch (_) {}
  const s = t.server;
  const l = t.link || {};
  return {
    key: t.key, traceId: t.traceId, method: t.method, path: t.path, host, type: t.resourceType,
    status: t.response ? (t.response.failed ? 'ERR' : t.response.status) : null,
    totalMs: t.response && t.response.totalMs != null ? Math.round(t.response.totalMs) : null,
    action: t.action ? `${t.action.action} ${(t.action.target || {}).selector || ''}`.trim() : '',
    link: t.action ? (l.inferred ? 'inferred·task' : l.sync ? 'listener·sync' : 'listener·async') : l.pending ? 'pending' : (l.origin && l.origin.kind === 'timer' ? `timer` : '—'),
    injected: !!t.injected, pageTagged: !!t.pageTagged,
    server: !s ? '' : s.skipped ? 'skipped' : s.joining ? 'joining' : !s.supported ? 'n/a'
      : s.raised ? `!! ${s.handler ? s.handler.function : ''}` : s.handler ? s.handler.function : (s.request ? 'no handler' : 'no row'),
    wallTime: t.wallTime,
  };
}

function provenanceUrl() {
  return (current && (current.cfg.provenanceUrl || current.provenanceUrl)) || '';
}

async function saveTraces(traces, meta) {
  const list = traces.map((t) => current.capture.finalize(t));
  const entry = artifacts.save(current.userData, list, { url: current.view.webContents.getURL(),
    session: current.capture.sessionId, filter: current.cfg.filter, ...meta });
  entry.provenance = await artifacts.toProvenance(provenanceUrl(), entry, list);
  return entry;
}

function registerIpc() {
  if (ipcRegistered) return;
  ipcRegistered = true;
  const own = (e) => current && e.sender === current.win.webContents;
  const h = (ch, fn) => ipcMain.handle(ch, async (e, ...a) => (own(e) ? fn(...a) : null));
  h('obs:state', () => ({ config: current.cfg, capturing: current.capture.active, url: current.view.webContents.getURL(),
    levels: summary.LEVELS, session: current.capture.sessionId, armed: current.capture._armedState() }));
  h('obs:navigate', (url) => {
    const u = String(url || '').trim();
    if (!/^https?:\/\//i.test(u)) return { ok: false, error: 'http(s) URLs only' };
    current.cfg = toggle.save(current.userData, { lastUrl: u });
    current.view.webContents.loadURL(u).catch(() => {});
    return { ok: true };
  });
  h('obs:reload', () => { current.view.webContents.reload(); return { ok: true }; });
  h('obs:back', () => { const wc = current.view.webContents; if (wc.canGoBack()) wc.goBack(); return { ok: true }; });
  h('obs:devtools', () => { current.view.webContents.openDevTools({ mode: 'detach' }); return { ok: true }; });
  h('obs:capture', async (on) => { if (on) await current.capture.start(); else await current.capture.stop(); return { capturing: current.capture.active }; });
  h('obs:filter', async (f) => {
    current.cfg = toggle.save(current.userData, { filter: { ...current.cfg.filter, ...(f || {}) } });
    await current.capture.setFilter(current.cfg.filter);
    return current.cfg.filter;
  });
  h('obs:server', (s) => {
    const spans = ['none', 'handler', 'app', 'all'].includes(s && s.spans) ? s.spans : current.cfg.server.spans;
    const modules = String((s && s.modules) || '').trim().slice(0, 300);
    current.cfg = toggle.save(current.userData, { server: { spans, modules } });
    current.capture.setServer(current.cfg.server);
    return current.cfg.server;
  });
  h('obs:inference', (level) => { current.cfg = toggle.save(current.userData, { inference: summary.LEVELS.includes(level) ? level : 'off' }); return current.cfg.inference; });
  h('obs:explainCfg', (c) => {
    const e = { ...current.cfg.explain };
    if (c && typeof c.url === 'string' && /^https?:\/\//i.test(c.url.trim())) e.url = c.url.trim();
    if (c && typeof c.model === 'string') e.model = c.model.trim().slice(0, 200);
    current.cfg = toggle.save(current.userData, { explain: e });
    return current.cfg.explain;
  });
  h('obs:spanBases', (m) => { current.cfg = toggle.save(current.userData, { spanBases: m && typeof m === 'object' ? m : {} }); return current.cfg.spanBases; });
  h('obs:list', () => current.capture.list().map(row));
  h('obs:get', (key) => {
    const t = current.capture.traces.get(key);
    return t ? { trace: current.capture.finalize(t), inference: summary.infer(t, current.cfg.inference) } : null;
  });
  h('obs:console', () => current.capture.console.slice(-300));
  h('obs:clear', () => { current.capture.clear(); return { ok: true }; });
  h('obs:export', () => exportTo(path.join(current.userData, 'observability', 'exports')));
  h('obs:arm', (n) => current.capture.arm(n));
  h('obs:disarm', () => { current.capture.disarm(); return null; });
  h('obs:save', async (key) => {
    const t = current.capture.traces.get(key);
    if (!t) return { ok: false, error: 'no such trace' };
    return { ok: true, ...(await saveTraces([t], { reason: 'manual' })) };
  });
  h('obs:saved', () => artifacts.list(current.userData));
  h('obs:loadSaved', (file) => {
    try { return { ok: true, doc: artifacts.load(current.userData, file) }; } catch (err) { return { ok: false, error: err.message }; }
  });
  // explain: explicit, per trace, and only at the 'explain' level
  h('obs:explain', async (key) => {
    if (current.cfg.inference !== 'explain') return { ok: false, error: 'inference level is not "explain"' };
    const t = current.capture.traces.get(key);
    if (!t) return { ok: false, error: 'no such trace' };
    try {
      t.explanation = await explainer.explain(current.capture.finalize(t), current.cfg.explain);
      return { ok: true, explanation: t.explanation };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });
  ipcMain.on('obs:layout', (e, r) => {
    if (!own(e) || !r) return;
    current.view.setBounds({ x: Math.round(+r.x || 0), y: Math.round(+r.y || 0),
      width: Math.max(0, Math.round(+r.width || 0)), height: Math.max(0, Math.round(+r.height || 0)) });
  });
}

function exportTo(dir) {
  if (!current) return null;
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `obs-${new Date().toISOString().replace(/[:.]/g, '-')}.json`);
  fs.writeFileSync(file, JSON.stringify({ exported_at: new Date().toISOString(), url: current.view.webContents.getURL(),
    session: current.capture.sessionId, filter: current.cfg.filter, server: current.cfg.server, inference: current.cfg.inference,
    traces: current.capture.list().map((t) => ({ ...current.capture.finalize(t), inference: summary.infer(t, current.cfg.inference) })),
    actions: current.capture.actions, console: current.capture.console }, null, 2));
  return file;
}

// opts: { userData, url?, filter?, show? (default true), partition?, provenanceUrl? }
async function open(opts = {}) {
  if (current) {
    current.win.show(); current.win.focus();
    if (opts.url) current.view.webContents.loadURL(opts.url).catch(() => {});
    return current;
  }
  registerIpc();
  const userData = opts.userData;
  let cfg = toggle.load(userData);
  // explicit overrides are persisted: every later toggle.save() re-reads the file,
  // so an in-memory override would silently revert on the first settings change
  const patch = {};
  if (opts.filter) patch.filter = { ...cfg.filter, ...opts.filter };
  if (opts.server) patch.server = { ...cfg.server, ...opts.server };
  if (opts.explain) patch.explain = { ...cfg.explain, ...opts.explain };
  if (Object.keys(patch).length) cfg = toggle.save(userData, patch);
  const win = new BrowserWindow({ width: 1600, height: 980, show: opts.show !== false, backgroundColor: '#0c1118',
    title: 'hugpy Station — Observability',
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true } });
  const vopts = { webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false,
    partition: opts.partition || 'persist:hugpy-observability' } };
  let view;
  if (typeof electron.WebContentsView === 'function') { view = new electron.WebContentsView(vopts); win.contentView.addChildView(view); }
  else { view = new electron.BrowserView(vopts); win.addBrowserView(view); }
  view.setBounds({ x: 0, y: 0, width: 0, height: 0 });
  view.webContents.setWindowOpenHandler(({ url }) => { view.webContents.loadURL(url).catch(() => {}); return { action: 'deny' }; });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));

  const capture = new Capture(view.webContents, { filter: cfg.filter, server: cfg.server, captureBodies: cfg.captureBodies,
    previewBytes: cfg.previewBytes, spanBase: (origin) => (current && current.cfg.spanBases[origin]) || origin });
  current = { win, view, capture, cfg, userData, exportTo, saveTraces, provenanceUrl: opts.provenanceUrl || '' };

  // Throttled push of changed rows to the viewer (CDP is chatty).
  const dirty = new Set(); let timer = null;
  const send = (ch, data) => { if (!win.isDestroyed()) win.webContents.send(ch, data); };
  capture.on('trace', (t) => {
    dirty.add(t.key);
    if (!timer) timer = setTimeout(() => { timer = null; const rows = [...dirty].map((k) => capture.traces.get(k)).filter(Boolean).map(row); dirty.clear(); send('obs:rows', rows); }, 150);
  });
  capture.on('console', (e) => send('obs:console-entry', e));
  capture.on('action', (a) => send('obs:action', { id: a.id, action: a.action, target: a.target, listeners: a.listeners }));
  capture.on('state', (s) => send('obs:capture-state', s));
  capture.on('armed', (a) => send('obs:armed', a));
  capture.on('armed-done', async (d) => {
    try {
      const traces = d.keys.map((k) => capture.traces.get(k)).filter(Boolean);
      const entry = traces.length ? await saveTraces(traces, { reason: 'armed', label: `next-${d.actionIds.length}-actions` }) : null;
      send('obs:armed-done', { ...d, entry });
      if (current && current.onArmedDone) current.onArmedDone(d, entry);
    } catch (err) { console.error('[observability] armed save failed', err); }
  });
  capture.on('error', (err) => console.error('[observability]', err && err.message));
  view.webContents.on('did-navigate', (_e, url) => send('obs:url', url));
  view.webContents.on('did-navigate-in-page', (_e, url) => send('obs:url', url));

  win.on('closed', () => {
    if (timer) clearTimeout(timer);
    capture.disarm();
    capture.stop().catch(() => {});
    current = null;
  });
  await win.loadFile(path.join(__dirname, 'viewer.html'));
  // CDP commands to a view that has never loaded hang (no renderer yet): give it
  // a blank document, then attach BEFORE the first real navigation so hop 1
  // hooks and header tagging exist from the first request on.
  await view.webContents.loadURL('about:blank').catch(() => {});
  await capture.start();
  const url = opts.url || cfg.lastUrl;
  if (url) view.webContents.loadURL(url).catch(() => {});
  return current;
}

function close() { if (current && !current.win.isDestroyed()) current.win.close(); }
function get() { return current; }

module.exports = { open, close, get, row };
