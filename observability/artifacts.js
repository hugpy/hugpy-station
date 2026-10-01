'use strict';
// Saved traces — durable artifacts the Station can list (no Electron needed).
//
//   <userData>/observability/traces/<stamp>-<id>.json   one artifact (1..n traces)
//   <userData>/observability/traces/index.jsonl          one line per artifact
//
// Every save is also offered to the Station's provenance ledger
// (POST <station backend>/api/provenance/ingest, kind "observability_trace").
// The body names the artifact path and the server file:line of the handler and
// the traceback, so provenance's item index intersects a trace with the edits
// that touched those files. The ingest is best-effort; the artifact is the record.
const fs = require('fs');
const path = require('path');
const summary = require('./summary');

const SCHEMA = 'hugpy-obs-trace/1';

function dirOf(userData) { return path.join(userData, 'observability', 'traces'); }

function slug(s) { return String(s || 'trace').replace(/[^A-Za-z0-9._-]+/g, '-').slice(0, 60); }

function brief(t) {
  const s = t.server || {};
  return {
    traceId: t.traceId || null, method: t.method, url: t.url,
    status: t.response ? (t.response.failed ? 'ERR' : t.response.status) : null,
    action: t.action ? `${t.action.action} ${(t.action.target || {}).selector || ''}`.trim() : null,
    link: t.link ? (t.link.join || t.link.reason || null) : null,
    handler: s.handler ? `${s.handler.function} ${s.handler.short || s.handler.file}:${s.handler.line}` : null,
    raised: s.raised ? `${s.raised.type || 'raise'}: ${s.raised.message || ''}`.trim() : null,
  };
}

// traces: one trace or an array; meta: { label, filter, url, session, reason }
function save(userData, traces, meta = {}) {
  const list = (Array.isArray(traces) ? traces : [traces]).filter(Boolean);
  if (!list.length) throw new Error('nothing to save');
  const dir = dirOf(userData);
  fs.mkdirSync(dir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const id = list.length === 1 ? (list[0].traceId || list[0].key) : `${meta.label || 'bundle'}-${list.length}`;
  const file = path.join(dir, `${stamp}-${slug(id)}.json`);
  const doc = { schema: SCHEMA, saved_at: new Date().toISOString(), label: meta.label || '', reason: meta.reason || 'manual',
    page: meta.url || '', session: meta.session || '', filter: meta.filter || null,
    traces: list.map((t) => ({ ...t, summary: summary.summarize(t) })) };
  fs.writeFileSync(file, JSON.stringify(doc, null, 2));
  const entry = { file, saved_at: doc.saved_at, label: doc.label, reason: doc.reason, count: list.length,
    session: doc.session, traces: list.map(brief) };
  fs.appendFileSync(path.join(dir, 'index.jsonl'), JSON.stringify(entry) + '\n');
  return entry;
}

function list(userData, limit = 200) {
  let raw = '';
  try { raw = fs.readFileSync(path.join(dirOf(userData), 'index.jsonl'), 'utf8'); } catch (_) { return []; }
  const out = [];
  for (const line of raw.split('\n')) {
    if (!line.trim()) continue;
    try { const e = JSON.parse(line); if (fs.existsSync(e.file)) out.push(e); } catch (_) {}
  }
  return out.slice(-limit).reverse();
}

function load(userData, file) {
  const dir = dirOf(userData);
  const p = path.resolve(String(file || ''));
  if (path.dirname(p) !== path.resolve(dir)) throw new Error('not a saved trace');
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

// Provenance event body for one saved artifact.
function provenanceEvent(entry, traces) {
  const items = [];
  for (const t of traces) {
    const s = t.server || {};
    if (s.handler && s.handler.file) items.push(`${s.handler.file}:${s.handler.line}`);
    for (const f of (s.raised && s.raised.traceback) || []) if (f.app !== false && f.file) items.push(`${f.file}:${f.line}`);
  }
  return {
    kind: 'observability_trace', source: 'station-observability', actor: 'operator', operation: 'trace-save',
    trace_id: traces.length === 1 ? (traces[0].traceId || '') : '', session_id: entry.session || '',
    body: { artifact: entry.file, saved_at: entry.saved_at, reason: entry.reason, traces: entry.traces,
      server_items: [...new Set(items)].slice(0, 60) },
  };
}

async function toProvenance(url, entry, traces, fetchImpl = fetch) {
  if (!url) return { ok: false, skipped: 'no provenance url' };
  try {
    const r = await fetchImpl(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(provenanceEvent(entry, traces)), signal: AbortSignal.timeout(3000) });
    const d = await r.json().catch(() => ({}));
    return { ok: r.ok && d.ok !== false, status: r.status, event_id: d.event_id || null };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

module.exports = { SCHEMA, dirOf, save, list, load, brief, provenanceEvent, toProvenance };
