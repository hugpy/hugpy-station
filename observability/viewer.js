'use strict';
// Viewer for the observability window. All page data is rendered via
// textContent (never innerHTML) — captured URLs/stacks/bodies are untrusted input.
const api = window.hugpyObs;
const $ = (id) => document.getElementById(id);
const rows = new Map();
let selected = null;
let level = 'off';

function el(tag, cls, ...kids) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  for (const k of kids) if (k != null && k !== false) n.append(k instanceof Node ? k : String(k));
  return n;
}
const csv = (v) => (Array.isArray(v) ? v.join(', ') : String(v || ''));
const ms = (v) => (v == null ? '?' : `${Number(v).toFixed(v < 10 ? 2 : 0)} ms`);

// ── layout: the native page view tracks #page ───────────────────────────────
function place() { const r = $('page').getBoundingClientRect(); api.layout({ x: r.left, y: r.top, width: r.width, height: r.height }); }
new ResizeObserver(place).observe($('page'));
window.addEventListener('resize', place);

// ── request list ─────────────────────────────────────────────────────────────
function renderRow(r) {
  let tr = document.querySelector(`tr[data-key="${CSS.escape(r.key)}"]`);
  if (!tr) {
    tr = el('tr'); tr.dataset.key = r.key; tr.onclick = () => select(r.key);
    $('rows').prepend(tr);
  }
  tr.replaceChildren(
    el('td', '', r.action || '—'),
    el('td', r.link.startsWith('listener') ? 'tag' : r.link.startsWith('inferred') ? 'warn' : '', r.link),
    el('td', r.injected || r.pageTagged ? 'tag' : '', `${r.method} ${r.host}${r.path}`),
    el('td', r.status === 'ERR' || r.status >= 400 ? 'err' : '', r.status == null ? '…' : r.status),
    el('td', '', r.totalMs == null ? '' : r.totalMs),
    el('td', String(r.server).startsWith('!!') ? 'err' : '', r.server || ''));
  tr.title = r.traceId ? `trace ${r.traceId}` : 'not trace-tagged';
  if (selected === r.key) { tr.classList.add('sel'); select(r.key, true); }
}
function status(msg) { $('status').textContent = msg || `${rows.size} requests`; }
api.onRows((list) => { for (const r of list) { rows.set(r.key, r); renderRow(r); } status(); });

// ── the chain view ───────────────────────────────────────────────────────────
function frameLine(f, cls) {
  const file = String(f.url || '').split('/').pop() || '(inline)';
  return el('div', cls || 'fr', `${f.functionName || '(anonymous)'}  ${file}:${(f.lineNumber || 0) + 1}:${(f.columnNumber || 0) + 1}`);
}
function pyLine(s, extra) {
  return el('div', 'fr' + (s.app === false ? ' lib' : ''),
    `${s.function}  ${s.short || s.file}:${s.line}${s.duration_ms == null ? '' : `  ${ms(s.duration_ms)}`}${s.error ? `  !! ${s.error}` : ''}${extra || ''}`);
}
function hop(n, title, ok, ...body) {
  return el('div', 'hop ' + (ok ? 'ok' : 'missing'), el('h4', '', `${n}  ${title}`), ...body);
}
const arrow = (t) => el('div', 'arrow', `▼ ${t || ''}`);
function kv(obj) {
  return Object.entries(obj || {}).map(([k, v]) => el('div', 'kv', `${k}: `, el('b', '', v)));
}

function render(t, inference, opts = {}) {
  const s = t.server, rs = t.response, ini = t.initiator || {}, l = t.link || {};
  const out = [];
  out.push(el('div', 'dim', `trace ${t.traceId || '(untagged)'}${t.serverTraceId && t.serverTraceId !== t.traceId ? ` (server stored as ${t.serverTraceId})` : ''} · ${t.method} ${t.url}${t.pageTraceId ? ` · page id ${t.pageTraceId} replaced` : ''}${opts.saved ? ' · SAVED artifact' : ''}`));
  if (!opts.saved) {
    out.push(el('div', 'actions',
      Object.assign(el('button', '', '💾 save trace'), { onclick: async () => { const r = await api.save(t.key); status(r && r.ok ? `saved ${r.file}${r.provenance && r.provenance.ok ? ' · provenance #' + r.provenance.event_id : ''}` : `save failed ${r && r.error}`); } }),
      level === 'explain' ? Object.assign(el('button', '', '✨ explain (sends this trace to the model)'), { onclick: async (e) => {
        e.target.disabled = true; status('explaining…');
        const r = await api.explain(t.key); status(r && r.ok ? `explained by ${r.explanation.model} in ${r.explanation.ms} ms` : `explain failed: ${r && r.error}`);
        select(t.key, true);
      } }) : null));
  }
  if (inference && inference.text) {
    out.push(el('div', 'hop ok', el('h4', '', `inference · ${inference.level}`), el('pre', '', inference.text),
      inference.explain ? el('div', '', el('h4', '', `explanation · ${inference.explain.model} · ${inference.explain.endpoint}`), el('pre', '', inference.explain.text)) : null,
      inference.note ? el('div', 'dim', inference.note) : null));
  }
  // 1 — action + the listener that handled it
  if (t.action) {
    const tg = t.action.target || {};
    out.push(hop(1, 'front-end action', true,
      el('div', '', `${t.action.action}${t.action.key ? ' ' + t.action.key : ''} on ${tg.selector || '?'}  "${tg.text || ''}"${t.action.trusted ? '' : '  (synthetic)'}`),
      l.listener ? el('div', 'fr', `listener: ${l.listener.type}${l.listener.useCapture ? ' (capture)' : ''} → ${l.listener.name || '(anonymous)'} on ${l.listener.node}  [script ${l.listener.scriptId} ${l.listener.lineNumber + 1}:${l.listener.columnNumber + 1}]`) : null,
      l.inferred ? el('div', 'bad', `INFERRED link (no stack edge): ${l.reason}`)
        : el('div', 'dim', `joined by listener stack: the request's root frame runs inside this listener${l.sync ? ', sent synchronously during the event' : ', through an async chain'}${l.deltaMs == null ? '' : ` · sent +${l.deltaMs} ms`}`),
      l.inferred && l.origin && l.origin.frame ? el('div', 'fr', `chain root: ${l.origin.frame.functionName || '(anonymous)'} ${String(l.origin.frame.url || '').split('/').pop()}:${(l.origin.frame.lineNumber || 0) + 1}:${(l.origin.frame.columnNumber || 0) + 1}${l.origin.via ? ` via ${l.origin.via}` : ''}`) : null));
  } else {
    out.push(hop(1, 'front-end action', false, el('div', 'dim', `not linked: ${l.reason || 'no action'}`),
      l.origin && l.origin.frame ? el('div', 'fr', `chain root: ${l.origin.frame.functionName || '(anonymous)'} ${String(l.origin.frame.url || '').split('/').pop()}:${(l.origin.frame.lineNumber || 0) + 1}${l.origin.via ? ` via ${l.origin.via}` : ''}`) : null));
  }
  out.push(arrow('JS stack'));
  // 2 — JS stack (sync frames, then async parents)
  const stack = (ini.stack || []).map((f) => frameLine(f));
  for (const p of ini.asyncParents || []) { stack.push(el('div', 'dim', `── async: ${p.description}`)); for (const f of p.frames) stack.push(frameLine(f)); }
  out.push(hop(2, `initiator stack → ${t.method} ${t.path}`, stack.length > 0,
    el('div', 'dim', `initiator ${ini.type || '?'} · ${t.resourceType}`),
    ...(stack.length ? stack : [el('div', 'dim', 'no JS stack (parser/navigation-initiated)')])));
  out.push(arrow('request'));
  const rq = t.request || {};
  const sent = t.sentHeaders && Object.keys(t.sentHeaders).length ? t.sentHeaders : rq.headers;
  out.push(el('div', 'hop ok', el('h4', '', `request  ${t.method} ${t.url}`),
    el('details', '', el('summary', 'dim', `headers (${Object.keys(sent || {}).length}; trace: ${Object.entries(sent || {}).filter(([k]) => /^x-hugpy-trace/i.test(k)).map(([k, v]) => `${k}=${v}`).join(' ') || 'none on the wire'})`), ...kv(sent)),
    rq.postDataPreview ? el('pre', 'dim', `body: ${rq.postDataPreview}`) : rq.hasPostData ? el('div', 'dim', 'body: (larger than the preview limit)') : null));
  out.push(arrow('server'));
  // 3–5
  if (!t.traceId) out.push(hop('3–5', 'server', false, el('div', 'dim', 'not trace-tagged (outside the domain/route/method filter, or tagging only while armed)')));
  else if (!s) out.push(hop('3–5', 'server', false, el('div', 'dim', 'waiting for the response…')));
  else if (s.skipped) out.push(hop('3–5', 'server', false, el('div', 'dim', `server answered but did not trace: ${s.skipped}`)));
  else if (s.joining && !s.request) out.push(hop('3–5', 'server', false, el('div', 'dim', `joining spans… (attempt ${s.attempts || 1})`)));
  else if (!s.supported) out.push(hop('3–5', 'server', false, el('div', 'dim', `${(s.notes || []).join(' · ')}`), el('div', 'dim', s.url)));
  else {
    const r = s.request;
    out.push(hop(3, 'request reached Flask', !!s.reached, r ? el('div', '', `${r.method} ${r.path} → ${r.status == null ? 'unfinished' : r.status}${r.endpoint ? ` · endpoint ${r.endpoint}` : ''} · server ${ms(r.duration_ms)} · spans ${s.level || '?'} (${s.counts.spans}, ${s.counts.app_calls} app calls)`) : el('div', 'dim', s.reached ? `${s.counts.spans} spans keyed by this id — request row missing` : 'nothing stored for this trace id'),
      r ? el('div', 'dim', `session ${r.session_id || '—'} · pid ${r.pid || '?'} · schema ${s.schema}${r && t.wallTime ? ` · browser→server start Δ ${Math.round((r.started_at - t.wallTime) * 1000)} ms` : ''}`) : null));
    out.push(hop(4, 'handler executed', !!s.handler, s.handler ? pyLine(s.handler, `  [${s.handler.method}]${s.handler.module ? '  ' + s.handler.module : ''}`) : el('div', 'dim', 'not identified'),
      ...(s.callees || []).slice(0, 15).map((c) => pyLine(c, c.raised ? '  (raised)' : ''))));
    out.push(arrow('output'));
    out.push(hop(5, s.raised ? 'stack to the output — RAISED' : 'stack to the output', (s.output || []).length > 1,
      ...(s.output || []).map((o) => el('div', /raised/.test(o.role) ? 'bad' : 'fr', `${o.role}: ${o.function}${o.short ? `  ${o.short}:${o.line}` : ''}${o.duration_ms == null ? '' : `  ${ms(o.duration_ms)}`}`)),
      s.raised ? el('div', 'bad', `${s.raised.type ? s.raised.type + ': ' : ''}${s.raised.message || ''}  (${s.raised.method})`) : null,
      ...(s.raised ? s.raised.traceback.map((f) => el('div', f.app === false ? 'fr lib' : 'bad', `  ${f.function}  ${f.short || f.file}:${f.line}${f.code ? '   ' + f.code : ''}`)) : []),
      ...(s.handled || []).map((h) => el('div', 'dim', `handled inside ${h.function} ${h.short}:${h.line}`))));
    const tree = el('details', '', el('summary', 'dim', `Python execution tree (${s.tree.length} rows: app frames, dispatch, exceptions)`));
    const base = s.tree.length ? Math.min(...s.tree.map((x) => x.depth)) : 0;
    for (const x of s.tree) tree.append(pyLine({ ...x, function: '  '.repeat(Math.min(40, x.depth - base)) + (x.event === 'exception' ? '!! ' : x.event === 'handled' ? '~~ ' : '') + x.function }));
    out.push(tree);
    if ((s.notes || []).length) out.push(el('div', 'dim', s.notes.join(' · ')));
  }
  out.push(arrow('response'));
  // 6 — response, then who consumed it
  out.push(hop(6, 'browser response', !!(rs && !rs.failed && rs.status),
    !rs ? el('div', 'dim', 'pending') : rs.failed ? el('div', 'bad', `failed: ${rs.errorText} ${rs.blockedReason || ''}`)
      : el('div', '', `${rs.status} ${rs.statusText || ''} · ${rs.mimeType || ''} · ${rs.encodedDataLength == null ? '?' : rs.encodedDataLength} B · ${rs.totalMs == null ? '?' : Math.round(rs.totalMs)} ms · ${rs.remote || ''}${rs.fromCache ? ' · cache' : ''}${t.serverHeader ? ' · ' + t.serverHeader : ''}`),
    rs && rs.body ? el('pre', 'dim', `body: ${rs.body}`) : null,
    rs && rs.headers ? el('details', '', el('summary', 'dim', 'response headers'), ...kv(rs.headers)) : null));
  const cons = t.consumers || [], dom = t.domEffects || [];
  out.push(hop('6←', 'consumed by page code', cons.length > 0,
    ...(cons.length ? cons.map((c) => el('div', c.level === 'error' || c.level === 'exception' ? 'bad' : 'fr',
      `[${c.level}] ${c.text}  — in ${c.via} (${String((c.at || {}).url || '').split('/').pop()}:${((c.at || {}).lineNumber || 0) + 1}) +${c.afterMs} ms · stack join`))
      : [el('div', 'dim', 'no console/exception entry whose async stack passes through the initiating code')]),
    ...dom.map((d) => el('div', 'dim', `DOM ${d.type} ${d.selector} "${d.text}" +${d.afterMs} ms · ${d.join}`))));
  return out;
}

async function select(key, quiet) {
  selected = key;
  if (!quiet) document.querySelectorAll('tr.sel').forEach((t) => t.classList.remove('sel'));
  const tr = document.querySelector(`tr[data-key="${CSS.escape(key)}"]`); if (tr) tr.classList.add('sel');
  const d = await api.get(key); if (!d || selected !== key) return;
  $('detail').replaceChildren(...render(d.trace, d.inference));
}

// ── console / saved tabs ─────────────────────────────────────────────────────
function addConsole(e) {
  const top = (e.stack || [])[0];
  const n = el('div', 'c-row ' + e.level, `[${e.level}] ${e.text}`);
  if (top) n.append(el('div', 'dim', `  at ${top.functionName || '(anonymous)'} ${String(top.url).split('/').pop()}:${(top.lineNumber || 0) + 1}`));
  $('console').prepend(n);
  while ($('console').childElementCount > 300) $('console').lastChild.remove();
}
api.onConsole(addConsole);
async function loadSaved() {
  const list = await api.saved();
  $('saved').replaceChildren(...(list || []).map((e) => {
    const n = el('div', 'saved-row', `${e.saved_at.slice(0, 19)} · ${e.reason}${e.label ? ' ' + e.label : ''} · ${e.count} trace(s) · ${e.traces.map((b) => `${b.method} ${String(b.url).replace(/^https?:\/\/[^/]+/, '')} ${b.status}${b.raised ? ' !!' : ''}`).slice(0, 3).join(', ')}`);
    n.title = e.file;
    n.onclick = async () => {
      const r = await api.loadSaved(e.file); if (!r || !r.ok) { status(r && r.error); return; }
      selected = null;
      $('detail').replaceChildren(el('div', 'dim', `artifact ${e.file}`), ...r.doc.traces.flatMap((t) => [...render(t, { level: 'summary', text: t.summary }, { saved: true }), el('hr')]));
    };
    return n;
  }));
  if (!list || !list.length) $('saved').append(el('div', 'dim', 'nothing saved yet — "save trace", or arm "trace next N actions"'));
}
document.querySelectorAll('nav [data-tab]').forEach((b) => b.onclick = () => {
  document.querySelectorAll('nav [data-tab]').forEach((x) => x.classList.toggle('on', x === b));
  for (const id of ['traces', 'console', 'saved']) $(id).classList.toggle('hidden', b.dataset.tab !== id);
  if (b.dataset.tab === 'saved') loadSaved();
});

// ── toolbar ──────────────────────────────────────────────────────────────────
$('go').onclick = async () => { const r = await api.navigate($('url').value); if (r && !r.ok) status(r.error); };
$('url').onkeydown = (e) => { if (e.key === 'Enter') $('go').onclick(); };
$('back').onclick = () => api.back();
$('reload').onclick = () => api.reload();
$('devtools').onclick = () => api.devtools();
$('capturing').onchange = async (e) => { const r = await api.capture(e.target.checked); e.target.checked = !!(r && r.capturing); };
$('clear').onclick = async () => { await api.clear(); rows.clear(); $('rows').replaceChildren(); $('console').replaceChildren(); $('detail').replaceChildren(); status(); };
$('export').onclick = async () => { const p = await api.exportJson(); status(p ? `exported ${p}` : 'export failed'); };
$('apply').onclick = async () => {
  const f = await api.setFilter({ domains: $('f-domains').value, routes: $('f-routes').value, methods: $('f-methods').value,
    types: $('f-types').value, action: $('f-action').value, tagWhen: $('f-tagwhen').value });
  status('filter applied');
  if (f) { const all = await api.list(); rows.clear(); $('rows').replaceChildren(); for (const r of all) { rows.set(r.key, r); renderRow(r); } }
};
$('s-apply').onclick = async () => { const s = await api.setServer({ spans: $('s-spans').value, modules: $('s-modules').value }); status(`server spans ${s.spans}${s.modules ? ' · ' + s.modules : ''}`); };
$('arm').onclick = async () => {
  if ($('arm').dataset.on === '1') { await api.disarm(); return; }
  await api.arm($('arm-n').value);
};
function showArmed(a) {
  $('arm').dataset.on = a ? '1' : '0';
  $('arm').textContent = a ? 'disarm' : 'arm';
  $('armed').textContent = a ? `armed: ${a.total - a.remaining}/${a.total} actions${a.remaining ? '' : ' — settling'}` : '';
}
api.onArmed(showArmed);
api.onArmedDone((d) => { showArmed(null); status(d.entry ? `armed run saved: ${d.entry.count} trace(s) → ${d.entry.file}` : 'armed run: no linked requests'); });
function setLevel(lv) { level = lv; document.body.classList.toggle('explain', lv === 'explain'); }
$('inference').onchange = async (e) => { setLevel(await api.setInference(e.target.value)); if (selected) select(selected, true); };
$('x-apply').onclick = async () => { const c = await api.setExplain({ url: $('x-url').value, model: $('x-model').value }); status(`explain → ${c.url} ${c.model || '(first model)'}`); };
api.onUrl((u) => { $('url').value = u; });
api.onCaptureState((s) => { $('capturing').checked = !!s.capturing; });

(async () => {
  const st = await api.state();
  const f = st.config.filter;
  $('f-domains').value = csv(f.domains); $('f-routes').value = csv(f.routes); $('f-methods').value = csv(f.methods);
  $('f-types').value = csv(f.types); $('f-action').value = f.action || ''; $('f-tagwhen').value = f.tagWhen || 'always';
  $('s-spans').value = st.config.server.spans; $('s-modules').value = st.config.server.modules || '';
  $('x-url').value = st.config.explain.url || ''; $('x-model').value = st.config.explain.model || '';
  for (const lv of st.levels) $('inference').append(Object.assign(document.createElement('option'), { value: lv, textContent: lv, selected: lv === st.config.inference }));
  setLevel(st.config.inference);
  $('url').value = st.url && st.url !== 'about:blank' ? st.url : (st.config.lastUrl || '');
  $('capturing').checked = !!st.capturing;
  showArmed(st.armed);
  place();
})();
