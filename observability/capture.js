'use strict';
// CDP capture for ONE WebContents (the observability browser's page view).
//
// Everything goes through webContents.debugger (Chrome DevTools Protocol 1.3);
// nothing is monkeypatched in the page's own JS world:
//   hop 1  user action      — capture-phase listeners in an ISOLATED world
//                             ("hugpy-obs") report click/submit/change/key via a
//                             Runtime binding. For each action the listeners on
//                             its target and ancestors are read with
//                             DOMDebugger.getEventListeners (location + name).
//   hop 2  initiator stack  — Network.requestWillBeSent.initiator.stack, with
//                             async parents (Debugger.setAsyncCallStackDepth).
//                             link.js joins request→action when the chain's ROOT
//                             frame is one of the action's listeners (no timing).
//   tag    contract headers — Fetch.requestPaused → continueRequest adds
//                             X-Hugpy-Trace: 1, a per-request X-Hugpy-Trace-Id,
//                             X-Hugpy-Trace-Session, -Spans, -Modules (README.md, "Server contract"),
//                             ONLY for requests the inject filter selects (Fetch
//                             patterns are scoped, so other traffic never pauses)
//   hops 3-5 server spans   — after loadingFinished, read-only GETs of
//                             <origin>/api/console/trace?trace_id=<echoed id>
//                             until the row is final (the server writes async)
//   hop 6  response         — status/headers/body preview; consumers = console
//                             entries whose async stack passes through the
//                             request's initiating functions (link.consumers);
//                             DOM mutations after the response (timing, labelled)
//   console                 — Runtime.consoleAPICalled / exceptionThrown + stacks
//
// Debugger is enabled for async stacks and script sources only;
// setSkipAllPauses(true) guarantees a `debugger;` statement can't freeze the page.

const { EventEmitter } = require('events');
const crypto = require('crypto');
const filters = require('./filters');
const spans = require('./spans');
const link = require('./link');

const WORLD = 'hugpy-obs';
const BINDING = '__hugpyObsAction';
const TRACE_HEADER = 'X-Hugpy-Trace-Id';
const TRACE_ON_HEADER = 'X-Hugpy-Trace';
const SESSION_HEADER = 'X-Hugpy-Trace-Session';
const SPANS_HEADER = 'X-Hugpy-Trace-Spans';
const MODULES_HEADER = 'X-Hugpy-Trace-Modules';
const SERVER_HEADER = 'X-Hugpy-Trace-Server';
const OBJ_GROUP = 'hugpy-obs';

// Runs in the isolated world: DOM events are shared across worlds, JS globals
// are not, so this sees every user action without touching the page's code.
// Targets are kept (bounded) so the main process can ask for their listeners.
const ACTION_SCRIPT = `(() => {
  if (window.__hugpyObsInstalled) return; window.__hugpyObsInstalled = true;
  const send = (o) => { try { window.${BINDING}(JSON.stringify(o)); } catch (_) {} };
  const targets = new Map(); let seq = 0;
  const cssPath = (el) => {
    if (!el || el.nodeType !== 1) return el === document ? 'document' : '';
    if (el.id) return el.tagName.toLowerCase() + '#' + CSS.escape(el.id);
    const parts = [];
    for (let n = el, i = 0; n && n.nodeType === 1 && i < 5; n = n.parentElement, i++) {
      let p = n.tagName.toLowerCase();
      if (n.id) { parts.unshift(p + '#' + CSS.escape(n.id)); break; }
      if (n.classList && n.classList.length) p += '.' + [...n.classList].slice(0, 2).map(CSS.escape).join('.');
      const par = n.parentElement;
      if (par) { const same = [...par.children].filter((x) => x.tagName === n.tagName); if (same.length > 1) p += ':nth-of-type(' + (same.indexOf(n) + 1) + ')'; }
      parts.unshift(p);
    }
    return parts.join(' > ');
  };
  const describe = (el) => {
    const e = el && el.nodeType === 1 ? el : (el && el.parentElement);
    if (!e) return {};
    const a = (k) => (e.getAttribute && e.getAttribute(k)) || '';
    return { tag: e.tagName.toLowerCase(), id: e.id || '', name: a('name'), role: a('role'), type: a('type'),
      text: String(e.innerText || a('aria-label') || a('title') || '').trim().slice(0, 80), selector: cssPath(e) };
  };
  window.__hugpyObsChain = (id) => { const out = []; for (let n = targets.get(id); n; n = n.parentNode) out.push(n); out.push(window); return out; };
  window.__hugpyObsChainLabels = (id) => window.__hugpyObsChain(id).map((n) => n === window ? 'window' : n === document ? 'document' : cssPath(n));
  const record = (action, e, extra) => {
    const id = ++seq; targets.set(id, e.target); if (targets.size > 64) targets.delete(targets.keys().next().value);
    send({ kind: 'action', id, action, ts: Date.now(), trusted: e.isTrusted, target: describe(e.target), ...(extra || {}) });
  };
  window.addEventListener('click', function __hugpyObsOnClick(e) { record('click', e); }, true);
  window.addEventListener('submit', function __hugpyObsOnSubmit(e) { record('submit', e); }, true);
  window.addEventListener('change', function __hugpyObsOnChange(e) { record('change', e,
    { valueLength: typeof e.target.value === 'string' ? e.target.value.length : undefined }); }, true);
  window.addEventListener('keydown', function __hugpyObsOnKey(e) { if (['Enter', 'Escape', ' '].includes(e.key) || e.ctrlKey || e.metaKey)
    record('key', e, { key: e.key }); }, true);
  // DOM effects (no stacks in CDP without pausing: reported as timing-joined)
  let pend = []; let timer = null;
  const flush = () => { timer = null; if (pend.length) send({ kind: 'dom', ts: Date.now(), muts: pend.slice(0, 12) }); pend = []; };
  const start = () => {
    new MutationObserver((ms) => {
      for (const m of ms) {
        if (pend.length >= 12) break;
        const t = m.target.nodeType === 1 ? m.target : m.target.parentElement;
        pend.push({ type: m.type, selector: cssPath(t), added: m.addedNodes.length, removed: m.removedNodes.length,
          text: String((t && t.textContent) || '').trim().slice(0, 60) });
      }
      if (!timer) timer = setTimeout(flush, 60);
    }).observe(document.documentElement, { childList: true, subtree: true, characterData: true, attributes: false });
  };
  if (document.documentElement) start(); else document.addEventListener('DOMContentLoaded', start);
})();`;

function hexId(prefix) { return prefix + crypto.randomBytes(8).toString('hex'); }

function frames(cf, max = 40) {
  return (cf || []).slice(0, max).map((f) => ({ functionName: f.functionName || '', url: f.url || '', scriptId: f.scriptId,
    lineNumber: f.lineNumber, columnNumber: f.columnNumber }));
}

function stackOf(st, maxParents = 16) {
  const out = { stack: frames(st && st.callFrames), asyncParents: [] };
  let p = st && st.parent; let n = 0;
  while (p && n < maxParents) { out.asyncParents.push({ description: p.description || 'async', frames: frames(p.callFrames, 20) }); p = p.parent; n++; }
  return out;
}

function initiatorOf(ini) {
  if (!ini) return null;
  const out = { type: ini.type, url: ini.url || '', lineNumber: ini.lineNumber, stack: [], asyncParents: [] };
  if (ini.stack) {
    Object.assign(out, stackOf(ini.stack));
    // the TRUE bottom frame (display frames are truncated; the link needs the
    // frame the browser invoked, i.e. the last frame of the last async parent)
    let seg = ini.stack; let n = 0; let root = null;
    while (seg && n < 64) { const cf = seg.callFrames || []; if (cf.length) root = cf[cf.length - 1]; seg = seg.parent; n++; }
    if (root) out.root = frames([root])[0];
    if (ini.stack.parentId || seg) { out.asyncParentId = ini.stack.parentId || null; out.truncated = true; }
  }
  return out;
}

const SECRET_HEADERS = /^(authorization|cookie|set-cookie|proxy-authorization|x-api-key|x-auth-token)$/i;
function cleanHeaders(h) {
  const out = {};
  for (const [k, v] of Object.entries(h || {})) out[k] = SECRET_HEADERS.test(k) ? '(redacted)' : String(v).slice(0, 300);
  return out;
}

class Capture extends EventEmitter {
  constructor(wc, opts = {}) {
    super();
    this.wc = wc;
    this.filter = filters.normalize(opts.filter);
    this.server = { spans: 'app', modules: '', ...(opts.server || {}) };
    this.maxTraces = opts.maxTraces || 1000;
    this.spanBase = opts.spanBase || ((origin) => origin);
    this.captureBodies = opts.captureBodies !== false;
    this.previewBytes = opts.previewBytes || 2048;
    this.sessionId = hexId('obs-s-');
    this.traces = new Map();      // CDP requestId -> trace
    this.byNetwork = new Map();   // networkId -> trace id assigned in Fetch.requestPaused
    this.pageIds = new Map();     // networkId -> trace id the PAGE had set (replaced by ours)
    this.actions = [];
    this.dom = [];
    this.console = [];
    this.armed = null;
    this.active = false;
    this._scriptId = null;
    this._names = new Map();      // scriptId:line:col -> function name
    this._sources = new Map();    // scriptId -> source (name fallback)
    this._onMessage = (_e, method, params) => { try { this._handle(method, params || {}); } catch (err) { this.emit('error', err); } };
    this._onDetach = (_e, reason) => { this.active = false; this.emit('state', { capturing: false, reason }); };
  }

  _send(method, params) {
    if (process.env.OBS_DEBUG) console.log('[obs cdp]', method);
    return this.wc.debugger.sendCommand(method, params || {});
  }

  async start() {
    if (this.active) return;
    const dbg = this.wc.debugger;
    if (!dbg.isAttached()) dbg.attach('1.3');
    dbg.on('message', this._onMessage);
    dbg.on('detach', this._onDetach);
    this.active = true;
    await this._send('Network.enable', { maxPostDataSize: this.captureBodies ? this.previewBytes : 0 });
    await this._send('Runtime.enable');
    await this._send('Page.enable');
    await this._send('Debugger.enable');
    await this._send('Debugger.setSkipAllPauses', { skip: true });
    await this._send('Debugger.setAsyncCallStackDepth', { maxDepth: 32 });
    await this._send('Runtime.addBinding', { name: BINDING, executionContextName: WORLD });
    const r = await this._send('Page.addScriptToEvaluateOnNewDocument', { source: ACTION_SCRIPT, worldName: WORLD });
    this._scriptId = r && r.identifier;
    await this._installInCurrentDocument();
    await this._applyFetch();
    this.emit('state', { capturing: true });
  }

  async _installInCurrentDocument() {
    try {
      const { frameTree } = await this._send('Page.getFrameTree');
      const { executionContextId } = await this._send('Page.createIsolatedWorld', { frameId: frameTree.frame.id, worldName: WORLD });
      await this._send('Runtime.evaluate', { expression: ACTION_SCRIPT, contextId: executionContextId });
    } catch (_) { /* about:blank before first load — the new-document script covers it */ }
  }

  async _applyFetch() {
    if (!this.active) return;
    await this._send('Fetch.disable').catch(() => {});
    await this._send('Fetch.enable', { patterns: filters.fetchPatterns(this.filter) });
  }

  async stop() {
    if (!this.active) return;
    const dbg = this.wc.debugger;
    try {
      await this._send('Fetch.disable');
      if (this._scriptId) await this._send('Page.removeScriptToEvaluateOnNewDocument', { identifier: this._scriptId });
      await this._send('Runtime.removeBinding', { name: BINDING });
      await this._send('Debugger.disable');
    } catch (_) {}
    dbg.removeListener('message', this._onMessage);
    dbg.removeListener('detach', this._onDetach);
    try { if (dbg.isAttached()) dbg.detach(); } catch (_) {}
    this.active = false;
    this.emit('state', { capturing: false });
  }

  async setFilter(f) {
    this.filter = filters.normalize(f);
    await this._applyFetch();
  }

  setServer(s) { this.server = { ...this.server, ...(s || {}) }; }

  clear() { this.traces.clear(); this.byNetwork.clear(); this.pageIds.clear(); this.actions = []; this.dom = []; this.console = []; }

  // ── "trace next N actions" ────────────────────────────────────────────────
  arm(n, opts = {}) {
    const count = Math.max(1, Math.min(50, Number(n) || 1));
    this.armed = { id: hexId('arm-'), total: count, remaining: count, actionIds: [], startedAt: Date.now(),
      lastActionAt: null, settleMs: opts.settleMs || 2500, maxWaitMs: opts.maxWaitMs || 15000, timer: null };
    this.emit('armed', this._armedState());
    return this._armedState();
  }

  disarm() { if (this.armed && this.armed.timer) clearInterval(this.armed.timer); this.armed = null; this.emit('armed', null); }

  _armedState() {
    const a = this.armed;
    return a ? { id: a.id, total: a.total, remaining: a.remaining, actionIds: [...a.actionIds] } : null;
  }

  _armedTraces(a) {
    return [...this.traces.values()].filter((t) => t.action && a.actionIds.includes(t.action.id));
  }

  _armedTick() {
    const a = this.armed; if (!a || a.remaining > 0) return;
    const ts = this._armedTraces(a);
    const settled = ts.every((t) => t.response && (!t.traceId || !t.injected || (t.server && !t.server.pending && !t.server.joining)));
    const quiet = Date.now() - a.lastActionAt >= a.settleMs;
    if ((settled && quiet) || Date.now() - a.lastActionAt > a.maxWaitMs) {
      clearInterval(a.timer);
      this.armed = null;
      this.emit('armed-done', { id: a.id, actionIds: a.actionIds, keys: ts.map((t) => t.key) });
      this.emit('armed', null);
    }
  }

  _armedOpen() { return !!this.armed; }

  _store(t) {
    this.traces.set(t.requestId, t);
    while (this.traces.size > this.maxTraces) this.traces.delete(this.traces.keys().next().value);
  }

  _link(t) {
    if (t.action && t.link && !t.link.inferred) return;
    const r = link.linkRequest(t, this.actions);
    t.link = { join: r.join || null, reason: r.reason || null, pending: !!r.pending, sync: !!r.sync, inferred: !!r.inferred,
      origin: r.origin ? { kind: r.origin.kind, via: r.origin.via, frame: r.origin.frame } : null,
      listener: r.listener || null, deltaMs: r.deltaMs == null ? null : r.deltaMs };
    t.action = r.action || null;
  }

  async _resolveListeners(a, contextId) {
    const types = link.ACTION_EVENT_TYPES[a.action] || [a.action];
    const out = [];
    try {
      const ev = await this._send('Runtime.evaluate', { expression: `window.__hugpyObsChain(${a.id})`, contextId, objectGroup: OBJ_GROUP });
      const lab = await this._send('Runtime.evaluate', { expression: `window.__hugpyObsChainLabels(${a.id})`, contextId, returnByValue: true });
      const labels = (lab.result && lab.result.value) || [];
      const props = await this._send('Runtime.getProperties', { objectId: ev.result.objectId, ownProperties: true });
      const nodes = props.result.filter((p) => /^\d+$/.test(p.name) && p.value && p.value.objectId)
        .sort((x, y) => x.name - y.name).map((p) => ({ objectId: p.value.objectId, label: labels[+p.name] || p.value.description }));
      for (const n of nodes) {
        // Listeners are per JS world: an isolated-world handle sees none of the
        // page's. Re-resolve the same DOM node (backendNodeId) in the page world.
        let objectId;
        if (n.label === 'window') {
          objectId = (await this._send('Runtime.evaluate', { expression: 'window', objectGroup: OBJ_GROUP })).result.objectId;
        } else {
          const { node } = await this._send('DOM.describeNode', { objectId: n.objectId });
          objectId = (await this._send('DOM.resolveNode', { backendNodeId: node.backendNodeId, objectGroup: OBJ_GROUP })).object.objectId;
        }
        const { listeners } = await this._send('DOMDebugger.getEventListeners', { objectId, depth: 0, objectGroup: OBJ_GROUP });
        for (const l of listeners || []) {
          if (!types.includes(l.type)) continue;
          const name = await this._listenerName(l);
          if (/^__hugpyObs/.test(name)) continue;           // our own isolated-world listeners
          out.push({ type: l.type, useCapture: !!l.useCapture, scriptId: l.scriptId, lineNumber: l.lineNumber,
            columnNumber: l.columnNumber, name, node: n.label, ...link.extentOf(l.handler && l.handler.description, l.lineNumber, l.columnNumber) });
        }
      }
    } catch (err) {
      a.listenerError = err.message;
    } finally {
      this._send('Runtime.releaseObjectGroup', { objectGroup: OBJ_GROUP }).catch(() => {});
    }
    return out;
  }

  async _listenerName(l) {
    const key = `${l.scriptId}:${l.lineNumber}:${l.columnNumber}`;
    if (this._names.has(key)) return this._names.get(key);
    let name = '';
    try {
      if (l.handler && l.handler.objectId) {
        const r = await this._send('Runtime.callFunctionOn', { objectId: l.handler.objectId,
          functionDeclaration: 'function () { return this.name; }', returnByValue: true });
        name = String((r.result && r.result.value) || '').replace(/^(bound )+/, '');
      }
      if (!name && l.scriptId) {
        if (!this._sources.has(l.scriptId)) {
          const s = await this._send('Debugger.getScriptSource', { scriptId: l.scriptId });
          this._sources.set(l.scriptId, s.scriptSource || '');
          if (this._sources.size > 40) this._sources.delete(this._sources.keys().next().value);
        }
        name = link.functionNameAt(this._sources.get(l.scriptId), l.lineNumber, l.columnNumber);
      }
    } catch (_) {}
    this._names.set(key, name);
    return name;
  }

  async _onAction(a, contextId) {
    a.listeners = null;
    this.actions.push(a);
    if (this.actions.length > 300) this.actions.shift();
    if (this.armed && this.armed.remaining > 0) {
      this.armed.actionIds.push(a.id); this.armed.remaining--; this.armed.lastActionAt = Date.now();
      a.armed = this.armed.id;
      if (this.armed.remaining === 0 && !this.armed.timer) this.armed.timer = setInterval(() => this._armedTick(), 300);
      this.emit('armed', this._armedState());
    }
    this.emit('action', a);
    a.listeners = await this._resolveListeners(a, contextId);
    // relink anything that was waiting for listener data
    for (const t of this.traces.values()) {
      if (!t.action && t.link && (t.link.pending || !t.link.join)) { this._link(t); if (t.action) this.emit('trace', t); }
    }
    this.emit('action', a);
  }

  _handle(method, p) {
    switch (method) {
      case 'Runtime.bindingCalled': {
        if (p.name !== BINDING) return;
        let a; try { a = JSON.parse(p.payload); } catch (_) { return; }
        if (a.kind === 'dom') {
          this.dom.push(a); if (this.dom.length > 300) this.dom.shift();
          return;
        }
        this._onAction(a, p.executionContextId).catch((err) => this.emit('error', err));
        return;
      }
      case 'Fetch.requestPaused': return this._paused(p);
      case 'Network.requestWillBeSent': {
        const req = p.request || {};
        let t = this.traces.get(p.requestId);
        if (t && p.redirectResponse) {
          t.redirects.push({ url: t.url, status: p.redirectResponse.status });
          t.url = req.url; t.path = pathOf(req.url);
          return this.emit('trace', t);
        }
        if (!filters.shouldRecord(this.filter, req.url, p.type)) return;
        const given = headerValue(req.headers, TRACE_HEADER);
        const ours = this.byNetwork.get(p.requestId);
        t = { key: p.requestId, requestId: p.requestId, traceId: ours || given || null,
          injected: !!ours && ours !== given, pageTagged: !!given, pageTraceId: given || this.pageIds.get(p.requestId) || null,
          url: req.url, path: pathOf(req.url), method: req.method, resourceType: p.type,
          wallTime: p.wallTime, t0: p.timestamp, documentURL: p.documentURL,
          initiator: initiatorOf(p.initiator), action: null, link: null, redirects: [], sentHeaders: {},
          request: { headers: cleanHeaders(req.headers), hasPostData: !!req.hasPostData,
            postDataPreview: req.postData ? String(req.postData).slice(0, this.previewBytes) : null },
          response: null, server: null, serverTraceId: null, serverHeader: null, consumers: [], domEffects: [] };
        this._link(t);
        this._store(t);
        return this.emit('trace', t);
      }
      case 'Network.requestWillBeSentExtraInfo': {
        const t = this.traces.get(p.requestId);
        if (t) { t.sentHeaders = cleanHeaders(p.headers); this.emit('trace', t); }
        return;
      }
      case 'Network.responseReceived': {
        const t = this.traces.get(p.requestId); if (!t) return;
        const r = p.response || {};
        t.response = { status: r.status, statusText: r.statusText, mimeType: r.mimeType, protocol: r.protocol,
          remote: r.remoteIPAddress ? `${r.remoteIPAddress}:${r.remotePort}` : '', headers: cleanHeaders(r.headers),
          fromCache: !!(r.fromDiskCache || r.fromServiceWorker), receivedMs: Math.round((p.timestamp - t.t0) * 1000) };
        t.serverTraceId = headerValue(r.headers, TRACE_HEADER) || null;
        t.serverHeader = headerValue(r.headers, SERVER_HEADER) || null;
        return this.emit('trace', t);
      }
      case 'Network.loadingFinished': {
        const t = this.traces.get(p.requestId); if (!t) return;
        t.response = t.response || {};
        t.response.encodedDataLength = p.encodedDataLength;
        t.response.totalMs = (p.timestamp - t.t0) * 1000;
        t.response.finishedWall = t.wallTime + (p.timestamp - t.t0);
        if (!t.action) this._link(t);
        this.emit('trace', t);
        if (this.captureBodies && /^(XHR|Fetch)$/.test(t.resourceType)) this._body(t);
        if (t.traceId && t.injected) this._joinServer(t);   // never join a pooled page-set id
        return;
      }
      case 'Network.loadingFailed': {
        const t = this.traces.get(p.requestId); if (!t) return;
        t.response = { ...(t.response || {}), failed: true, errorText: p.errorText, canceled: !!p.canceled,
          blockedReason: p.blockedReason || p.corsErrorStatus && p.corsErrorStatus.corsError || '',
          totalMs: (p.timestamp - t.t0) * 1000, finishedWall: t.wallTime + (p.timestamp - t.t0) };
        return this.emit('trace', t);
      }
      case 'Runtime.consoleAPICalled': {
        this._console({ level: p.type, ts: p.timestamp, tsWall: p.timestamp,
          text: (p.args || []).map(remoteText).join(' ').slice(0, 2000), ...stackOf(p.stackTrace, 8) });
        return;
      }
      case 'Runtime.exceptionThrown': {
        const d = p.exceptionDetails || {};
        this._console({ level: 'exception', ts: p.timestamp, tsWall: p.timestamp,
          text: ((d.exception && d.exception.description) || d.text || 'exception').slice(0, 4000),
          ...stackOf(d.stackTrace, 8), url: d.url, lineNumber: d.lineNumber });
        return;
      }
      default:
    }
  }

  _console(entry) {
    this.console.push(entry);
    if (this.console.length > 500) this.console.shift();
    this.emit('console', entry);
  }

  _shouldInject(req, resourceType) {
    if (!filters.shouldInject(this.filter, req.url, resourceType, req.method)) return false;
    if (this.filter.tagWhen === 'armed' && !this._armedOpen()) return false;
    return true;
  }

  async _paused(p) {
    // MUST always continue the request, whatever happens — a paused request
    // that is never continued hangs the page.
    const req = p.request || {};
    try {
      const existing = headerValue(req.headers, TRACE_HEADER);
      if (!this._shouldInject(req, p.resourceType)) {
        if (existing && p.networkId) this.byNetwork.set(p.networkId, existing);
        return await this._send('Fetch.continueRequest', { requestId: p.requestId });
      }
      // A page-set id is REPLACED by a per-request one (and kept as pageTraceId):
      // one id per request is the contract; a pooled id merges server rows.
      const traceId = hexId('obs-');
      if (existing) this.pageIds.set(p.networkId, existing);
      if (p.networkId) {
        this.byNetwork.set(p.networkId, traceId);
        if (this.byNetwork.size > 4000) this.byNetwork.delete(this.byNetwork.keys().next().value);
        if (this.pageIds.size > 4000) this.pageIds.delete(this.pageIds.keys().next().value);
        const t = this.traces.get(p.networkId);
        if (t) { t.traceId = traceId; t.injected = true; if (existing) t.pageTraceId = existing; this.emit('trace', t); }
      }
      const pageSession = headerValue(req.headers, SESSION_HEADER);
      const headers = Object.entries(req.headers || {})
        .filter(([name]) => !/^x-hugpy-trace(-id|-spans|-modules)?$/i.test(name))
        .map(([name, value]) => ({ name, value: String(value) }));
      headers.push({ name: TRACE_HEADER, value: traceId }, { name: TRACE_ON_HEADER, value: '1' },
        { name: SPANS_HEADER, value: this.server.spans || 'app' });
      if (!pageSession) headers.push({ name: SESSION_HEADER, value: this.sessionId });
      if (this.server.modules) headers.push({ name: MODULES_HEADER, value: String(this.server.modules) });
      await this._send('Fetch.continueRequest', { requestId: p.requestId, headers });
    } catch (err) {
      this._send('Fetch.continueRequest', { requestId: p.requestId }).catch(() => {});
      this.emit('error', err);
    }
  }

  async _body(t) {
    try {
      const r = await this._send('Network.getResponseBody', { requestId: t.requestId });
      t.response.body = r.base64Encoded ? `(base64, ${r.body.length} chars)` : String(r.body).slice(0, this.previewBytes);
      this.emit('trace', t);
    } catch (_) {}
  }

  async _joinServer(t, attempt = 0) {
    const delays = [150, 300, 600, 1200, 2000, 3000];
    let origin; try { origin = new URL(t.url).origin; } catch (_) { return; }
    const base = String(this.spanBase(origin) || origin).replace(/\/+$/, '');
    const id = t.serverTraceId || t.traceId;
    const url = `${base}/api/console/trace?trace_id=${encodeURIComponent(id)}`;
    if (attempt === 0) t.server = { pending: true, joining: true, url };
    if (t.serverHeader && /skipped=/.test(t.serverHeader)) {
      t.server = { supported: true, skipped: t.serverHeader, url, notes: [`server did not trace: ${t.serverHeader}`], counts: {} };
      return this.emit('trace', t);
    }
    try {
      const res = await fetch(url, { method: 'GET', headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(5000) });
      let doc = null;
      if (res.ok && /json/.test(res.headers.get('content-type') || '')) doc = await res.json();
      const a = spans.analyze(doc);
      const final = doc && doc.request && !a.pending;
      if (doc && !final && attempt < delays.length) {
        t.server = { ...a, url, joining: true, attempts: attempt + 1 };
        setTimeout(() => this._joinServer(t, attempt + 1), delays[attempt]);
        return this.emit('trace', t);
      }
      if (!doc) a.notes.unshift(`GET ${url} -> ${res.status}`);
      t.server = { ...a, url, joining: false, attempts: attempt + 1 };
    } catch (err) {
      t.server = { supported: false, url, joining: false, notes: [`span fetch failed: ${err.message}`], counts: {} };
    }
    this.emit('trace', t);
  }

  // hop 6 inverse: who consumed the response (computed on demand)
  finalize(t) {
    if (!t) return t;
    const ref = t.action ? t.action.ts : (t.wallTime || 0) * 1000;
    const next = this.actions.find((a) => a.ts > ref + 1 && (!t.action || a.id !== t.action.id));
    t.consumers = link.consumers(t, this.console, { before: next ? next.ts : null });
    const done = t.response && t.response.finishedWall ? t.response.finishedWall * 1000 : null;
    t.domEffects = !done ? [] : this.dom.filter((d) => d.ts >= done - 5 && d.ts <= done + 300)
      .flatMap((d) => d.muts.map((m) => ({ ...m, afterMs: Math.round(d.ts - done), join: 'timing (≤300 ms after response)' })))
      .slice(0, 6);
    return t;
  }

  list() {
    return [...this.traces.values()].filter((t) => filters.actionMatches(this.filter, t.action));
  }
}

function pathOf(url) { try { const u = new URL(url); return u.pathname + u.search; } catch (_) { return url; } }
function headerValue(h, name) {
  const k = Object.keys(h || {}).find((x) => x.toLowerCase() === name.toLowerCase());
  return k ? String(h[k]) : '';
}
function remoteText(a) {
  if (!a) return '';
  if ('value' in a) return typeof a.value === 'string' ? a.value : JSON.stringify(a.value);
  return a.description || a.type || '';
}

module.exports = { Capture, WORLD, BINDING, TRACE_HEADER, TRACE_ON_HEADER, SESSION_HEADER, SPANS_HEADER,
  MODULES_HEADER, SERVER_HEADER, ACTION_SCRIPT, cleanHeaders, initiatorOf };
