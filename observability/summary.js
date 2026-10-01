'use strict';
// Inference levels — PURE. How much interpretation a trace gets on top of the
// raw capture. Default OFF: the viewer shows the joined hops and nothing else.
//
//   off      raw hops only (no narrative)
//   summary  deterministic narrative of the six hops, computed locally from the
//            capture — no model, no network
//   explain  the summary, plus an EXPLICIT per-trace "explain" action that sends
//            the trace to a configured OpenAI-compatible endpoint (explain.js).
//            infer() itself never calls a model — selecting the level only
//            enables the button.
const LEVELS = ['off', 'summary', 'explain'];

function frameText(f) {
  if (!f) return '';
  const file = String(f.url || '').split('/').pop() || '(inline)';
  return `${f.functionName || '(anonymous)'} @ ${file}:${(f.lineNumber || 0) + 1}`;
}

function topFrame(stack) {
  const f = (stack || []).find((x) => x && x.url && !/^(chrome|devtools|extensions)::?/.test(x.url)) || (stack || [])[0];
  return frameText(f);
}

function hop1(t) {
  const l = t.link || {};
  if (t.action) {
    const tg = t.action.target || {};
    const lis = l.listener ? ` → ${l.listener.type} listener ${l.listener.name || '(anonymous)'} on ${l.listener.node || '?'}` : '';
    return `1) ${t.action.action}${t.action.key ? ' ' + t.action.key : ''} on ${tg.selector || tg.tag || '?'}` +
      `${tg.text ? ` ("${String(tg.text).slice(0, 40)}")` : ''}${lis} ` +
      (l.inferred ? `[INFERRED, no stack link: ${l.reason}]` : `[joined by ${l.join || '?'}${l.sync ? ', sent synchronously in the listener' : ', async chain'}]`);
  }
  return `1) no user action: ${l.reason || 'not linked'}`;
}

function summarize(t) {
  if (!t) return '';
  const parts = [hop1(t)];
  const ini = t.initiator || {};
  parts.push(`2) ${t.method} ${t.path || t.url} initiated by ${ini.type || '?'}` +
    (ini.stack && ini.stack.length ? ` from ${topFrame(ini.stack)}` : '') +
    (ini.asyncParents && ini.asyncParents.length ? ` (async via ${ini.asyncParents.map((p) => p.description).join(' ← ')})` : '') +
    (t.request && t.request.postDataPreview ? `; body ${t.request.postDataPreview.length} chars` : ''));
  const s = t.server;
  if (!t.traceId) parts.push('3-5) not trace-tagged (outside the inject filter): no server spans requested');
  else if (t.serverHeader && /skipped=/.test(t.serverHeader)) parts.push(`3-5) server answered but did not trace: ${t.serverHeader}`);
  else if (!s || s.pending && !s.request) parts.push('3-5) server spans pending');
  else if (!s.supported) parts.push(`3-5) origin has no console_trace endpoint (${(s.notes || []).join('; ')})`);
  else {
    const r = s.request;
    parts.push(r ? `3) reached Flask: ${r.method} ${r.path}${r.endpoint ? ` [${r.endpoint}]` : ''} (server ${r.duration_ms == null ? 'unfinished' : Math.round(r.duration_ms) + ' ms'}, spans ${s.level || 'app'})`
      : s.reached ? `3) reached Flask (${s.counts.spans} spans keyed by this id; request row missing)` : '3) nothing stored server-side for this trace id');
    parts.push(s.handler ? `4) handler ${s.handler.function} @ ${s.handler.short}:${s.handler.line}` +
      `${s.handler.duration_ms == null ? '' : ` ran ${Number(s.handler.duration_ms).toFixed(1)} ms`}, ${s.counts.app_calls} app calls` +
      (s.callees && s.callees.length ? `; called ${s.callees.filter((c) => c.app).slice(0, 4).map((c) => c.function).join(', ')}` : '')
      : '4) handler not identified');
    let five = `5) output: ${(s.output || []).map((o) => o.function).join(' → ') || '?'}`;
    if (s.raised) {
      five += `; RAISED ${s.raised.type ? s.raised.type + ': ' + (s.raised.message || '') + ' ' : ''}(${s.raised.method}): ` +
        s.raised.traceback.filter((f) => f.app !== false).map((f) => `${f.function}:${f.line}`).join(' → ');
    }
    if (s.handled && s.handled.length) five += `; ${s.handled.length} exception(s) raised and handled inside (${s.handled.map((h) => h.function).join(', ')})`;
    parts.push(five);
  }
  const rs = t.response;
  if (!rs) parts.push('6) no response yet');
  else if (rs.failed) parts.push(`6) browser: FAILED ${rs.errorText || ''}`);
  else {
    parts.push(`6) browser received ${rs.status} ${rs.mimeType || ''} ${rs.encodedDataLength == null ? '' : rs.encodedDataLength + ' B'}` +
      `${rs.totalMs == null ? '' : ` in ${Math.round(rs.totalMs)} ms`}` +
      ((t.consumers || []).length ? `; consumed by ${t.consumers.map((c) => `${c.via} ([${c.level}] ${c.text.slice(0, 40)})`).join(', ')}` : '') +
      ((t.domEffects || []).length ? `; DOM after response: ${t.domEffects.map((d) => d.selector).slice(0, 3).join(', ')}` : ''));
  }
  return parts.join('\n');
}

function infer(trace, level) {
  const lv = LEVELS.includes(level) ? level : 'off';
  if (lv === 'off') return { level: lv, text: '' };
  const text = summarize(trace);
  if (lv === 'summary') return { level: lv, text };
  return { level: lv, text, explain: trace && trace.explanation ? trace.explanation : null,
    note: trace && trace.explanation ? '' : 'press "explain" to send this trace to the configured model (never automatic)' };
}

module.exports = { LEVELS, summarize, infer, topFrame, frameText };
