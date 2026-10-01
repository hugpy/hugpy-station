'use strict';
// The OFF-by-default switch, and the persisted observability settings.
// This is the ONLY observability file main.js loads at startup; it has no side
// effects at require time and touches nothing but <userData>/observability.json.
// The capture engine (index.js / capture.js) is required lazily, on first open.
//
//   HUGPY_STATION_OBSERVABILITY=1   force on   (overrides the persisted flag)
//   HUGPY_STATION_OBSERVABILITY=0   force off
//   otherwise                       <userData>/observability.json {"enabled": …}
//                                   — default false; flipped from the Help menu

const fs = require('fs');
const path = require('path');

const DEFAULT_CONFIG = Object.freeze({
  enabled: false,
  inference: 'off',
  lastUrl: '',
  // see filters.js — empty lists = everything (all methods are tagged)
  filter: { domains: [], routes: [], methods: [], types: ['XHR', 'Fetch'], action: '', tagWhen: 'always' },
  // what the server is asked to record (README.md, "Server contract"): X-Hugpy-Trace-Spans
  // none|handler|app|all and X-Hugpy-Trace-Modules (module-name globs)
  server: { spans: 'app', modules: '' },
  // origin -> base URL whose /api/console/trace holds that origin's spans
  // (e.g. "https://hugpy.ai": "http://127.0.0.1:7002"); unlisted = same origin
  spanBases: {},
  // request/response body previews (bounded, XHR/Fetch only; auth headers redacted)
  captureBodies: true,
  previewBytes: 2048,
  // 'explain' inference: OpenAI-compatible base; used ONLY on an explicit
  // per-trace "explain" press while the level is 'explain'
  explain: { url: 'http://127.0.0.1:7002/v1', model: '', apiKeyEnv: 'HUGPY_OBS_EXPLAIN_KEY' },
  // Station provenance ingest for saved traces ('' = main.js supplies the
  // Station backend's /api/provenance/ingest)
  provenanceUrl: '',
});

function configPath(userData) { return path.join(userData, 'observability.json'); }

function load(userData) {
  try {
    const raw = JSON.parse(fs.readFileSync(configPath(userData), 'utf8'));
    return { ...DEFAULT_CONFIG, ...raw, filter: { ...DEFAULT_CONFIG.filter, ...(raw.filter || {}) },
      server: { ...DEFAULT_CONFIG.server, ...(raw.server || {}) }, explain: { ...DEFAULT_CONFIG.explain, ...(raw.explain || {}) } };
  } catch (_) {
    return { ...DEFAULT_CONFIG, filter: { ...DEFAULT_CONFIG.filter }, server: { ...DEFAULT_CONFIG.server },
      explain: { ...DEFAULT_CONFIG.explain } };
  }
}

function save(userData, patch) {
  const next = { ...load(userData), ...patch };
  fs.mkdirSync(userData, { recursive: true });
  fs.writeFileSync(configPath(userData), JSON.stringify(next, null, 2));
  return next;
}

function envOverride() {
  const v = String(process.env.HUGPY_STATION_OBSERVABILITY || '').trim().toLowerCase();
  if (['1', 'true', 'on', 'yes'].includes(v)) return true;
  if (['0', 'false', 'off', 'no'].includes(v)) return false;
  return null;
}

function isEnabled(userData) {
  const e = envOverride();
  return e === null ? !!load(userData).enabled : e;
}

function setEnabled(userData, on) { return save(userData, { enabled: !!on }); }

module.exports = { DEFAULT_CONFIG, load, save, isEnabled, setEnabled, envOverride, configPath };
