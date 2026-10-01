'use strict';
// Targeting filters for the observability browser — PURE (no Electron), so
// they are unit-tested by test/unit.test.js with plain node.
//
// A filter decides two things for every request the page makes:
//   record — is it kept in the trace store at all?
//   inject — does it get X-Hugpy-Trace-Id / X-Hugpy-Trace: 1 (server spans)?
// Empty lists mean "everything" for that dimension. Globs: `*` = any run of
// characters, `?` = one character; matched case-insensitively.
//
//   domains  ['127.0.0.1:7002', '*.hugpy.ai']   host[:port] of the request URL
//   routes   ['/api/*']                          URL path (no query)
//   methods  ['GET', 'POST']                     HTTP methods to INJECT into
//                                                (empty = all: the headers only
//                                                ask the server to trace; they
//                                                never change what it does)
//   types    ['XHR', 'Fetch']                    CDP resource types to record
//   action   'button#save' | 'Save'              keep only requests LINKED (by
//                                                listener stack) to an action
//                                                whose selector/text contains it
//   tagWhen  'always' | 'armed'                  'armed': inject only while a
//                                                "trace next N actions" run is open

const DEFAULTS = Object.freeze({
  domains: [],
  routes: [],
  methods: [],               // all methods (phase 1 tagged GET only; POST tracing is the point)
  types: ['XHR', 'Fetch'],
  action: '',
  tagWhen: 'always',
});

function globToRegExp(glob) {
  const src = String(glob).trim().replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp('^' + src + '$', 'i');
}

function list(v) {
  if (Array.isArray(v)) return v.map((x) => String(x).trim()).filter(Boolean);
  if (typeof v === 'string') return v.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
  return [];
}

function normalize(f) {
  f = f || {};
  return {
    domains: list(f.domains !== undefined ? f.domains : DEFAULTS.domains),
    routes: list(f.routes !== undefined ? f.routes : DEFAULTS.routes),
    methods: list(f.methods !== undefined ? f.methods : DEFAULTS.methods).map((m) => m.toUpperCase()),
    types: list(f.types !== undefined ? f.types : DEFAULTS.types),
    action: String(f.action || '').trim(),
    tagWhen: f.tagWhen === 'armed' ? 'armed' : 'always',
  };
}

function anyGlob(globs, value) {
  if (!globs.length) return true;
  return globs.some((g) => globToRegExp(g).test(value));
}

function parse(url) {
  try { return new URL(url); } catch (_) { return null; }
}

// Should a request with this URL / resource type be recorded?
function shouldRecord(filter, url, resourceType) {
  const f = normalize(filter);
  const u = parse(url);
  if (!u || !/^https?:$/.test(u.protocol)) return false;
  if (f.types.length && resourceType && !f.types.some((t) => t.toLowerCase() === String(resourceType).toLowerCase())) return false;
  return anyGlob(f.domains, u.host) && anyGlob(f.routes, u.pathname);
}

// Should this request carry the trace headers? Recorded AND method allowed.
function shouldInject(filter, url, resourceType, method) {
  const f = normalize(filter);
  if (!shouldRecord(f, url, resourceType)) return false;
  if (!f.methods.length) return true;
  return f.methods.includes(String(method || 'GET').toUpperCase());
}

// Does an (attributed) action pass the action filter?
function actionMatches(filter, action) {
  const f = normalize(filter);
  if (!f.action) return true;
  if (!action) return false;
  const needle = f.action.toLowerCase();
  const t = action.target || {};
  return [t.selector, t.text, t.id, t.name, action.action]
    .some((s) => String(s || '').toLowerCase().includes(needle));
}

// CDP Fetch.enable patterns: pause only what we may inject into, so untargeted
// traffic never round-trips through the main process.
function fetchPatterns(filter) {
  const f = normalize(filter);
  const types = f.types.length ? f.types : [null];
  const domains = f.domains.length ? f.domains : ['*'];
  const out = [];
  for (const d of domains) {
    for (const t of types) {
      const p = { urlPattern: d === '*' ? '*' : `*://${d}/*`, requestStage: 'Request' };
      if (t) p.resourceType = t;
      out.push(p);
    }
  }
  return out;
}

module.exports = { DEFAULTS, normalize, shouldRecord, shouldInject, actionMatches, fetchPatterns, globToRegExp };
