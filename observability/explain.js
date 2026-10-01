'use strict';
// The "explain" inference level: ONE explicit request per button press to an
// OpenAI-compatible chat endpoint. Never called by infer(), by capture, or on a
// timer — index.js calls explain() only from the obs:explain IPC, and only when
// the configured level is 'explain'.
//
// Endpoint (toggle.js `explain`): default hugpy central's /v1 at 127.0.0.1:7002;
// the toolserver or any OpenAI-compatible base works. `model` empty = first id
// from GET <base>/models. `apiKeyEnv` names an env var holding a bearer token.
const summary = require('./summary');

const MAX_PROMPT = 14000;
const SYSTEM = 'You explain one traced web request for a developer debugging the hugpy console. ' +
  'The trace joins a browser action, its JavaScript stack, the HTTP request, the Flask handler and its ' +
  'Python call/exception spans, and the browser response. Explain what happened, in order, and name the ' +
  'exact function and line where behaviour diverged if anything failed. Only use facts present in the trace; ' +
  'say "not in the trace" otherwise. Be concise.';

function trimFrames(frames, n) {
  return (frames || []).slice(0, n).map((f) => `${f.functionName || f.function || '(anon)'} ${String(f.url || f.short || f.file || '').split('/').slice(-2).join('/')}:${(f.lineNumber != null ? f.lineNumber + 1 : f.line) || '?'}`);
}

function buildPrompt(t) {
  const s = t.server || {};
  const doc = {
    summary: summary.summarize(t),
    action: t.action ? { action: t.action.action, target: t.action.target, link: t.link && { join: t.link.join, listener: t.link.listener, sync: t.link.sync } } : (t.link ? { unlinked: t.link.reason } : null),
    js_stack: trimFrames((t.initiator || {}).stack, 12),
    async_parents: ((t.initiator || {}).asyncParents || []).slice(0, 4).map((p) => ({ via: p.description, frames: trimFrames(p.frames, 6) })),
    request: { method: t.method, url: t.url, headers: (t.request || {}).headers, body: (t.request || {}).postDataPreview },
    server: s.supported ? { request: s.request && { status: s.request.status, endpoint: s.request.endpoint, duration_ms: s.request.duration_ms, error: s.request.error },
      handler: s.handler && { function: s.handler.function, file: s.handler.short, line: s.handler.line },
      callees: (s.callees || []).filter((c) => c.app).slice(0, 12).map((c) => `${c.function} ${c.short}:${c.line}${c.raised ? ' (raised)' : ''}`),
      output: (s.output || []).map((o) => `${o.role}: ${o.function}`),
      raised: s.raised && { type: s.raised.type, message: s.raised.message, traceback: trimFrames(s.raised.traceback, 15) },
      handled: (s.handled || []).map((h) => `${h.function}:${h.line}`) } : { notes: s.notes },
    response: t.response && { status: t.response.status, mime: t.response.mimeType, ms: t.response.totalMs && Math.round(t.response.totalMs),
      body: t.response.body, failed: t.response.failed, error: t.response.errorText },
    consumers: t.consumers, dom_effects: t.domEffects,
  };
  let text = JSON.stringify(doc, null, 1);
  if (text.length > MAX_PROMPT) text = text.slice(0, MAX_PROMPT) + '\n…(truncated)';
  return [{ role: 'system', content: SYSTEM }, { role: 'user', content: 'TRACE:\n' + text }];
}

function baseOf(url) {
  return String(url || '').replace(/\/+$/, '').replace(/\/chat\/completions$/, '');
}

async function explain(trace, cfg = {}, fetchImpl = fetch) {
  const base = baseOf(cfg.url || 'http://127.0.0.1:7002/v1');
  const headers = { 'Content-Type': 'application/json' };
  const key = cfg.apiKeyEnv ? process.env[cfg.apiKeyEnv] : '';
  if (key) headers.Authorization = `Bearer ${key}`;
  let model = cfg.model || '';
  if (!model) {
    const r = await fetchImpl(`${base}/models`, { headers, signal: AbortSignal.timeout(5000) });
    const d = r.ok ? await r.json() : null;
    model = d && d.data && d.data[0] && d.data[0].id;
    if (!model) throw new Error(`no model: GET ${base}/models -> ${r.status}`);
  }
  const t0 = Date.now();
  const r = await fetchImpl(`${base}/chat/completions`, { method: 'POST', headers,
    body: JSON.stringify({ model, messages: buildPrompt(trace), temperature: 0.1, max_tokens: cfg.maxTokens || 700 }),
    signal: AbortSignal.timeout(cfg.timeoutMs || 120000) });
  const d = await r.json().catch(() => null);
  if (!r.ok || !d || !d.choices) throw new Error(`POST ${base}/chat/completions -> ${r.status} ${d && d.error ? JSON.stringify(d.error).slice(0, 200) : ''}`);
  return { text: String(d.choices[0].message.content || ''), model, endpoint: `${base}/chat/completions`,
    ms: Date.now() - t0, at: new Date().toISOString() };
}

module.exports = { explain, buildPrompt, baseOf, SYSTEM };
