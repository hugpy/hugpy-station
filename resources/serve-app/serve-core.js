'use strict';
// serve-core — the ONE join-first probe + backend preselect for the shared Serve
// console (abstract_serve), used by BOTH Electron shells:
//   * resources/serve-app/main.js  (`hugpy-station --serve [claude|gpt|hugpy]`)
//   * main.js — the hugpy Station's native console view (the /ac WebContentsView)
// so "Serve IS the Electron serve app": the Station attaches to a console that
// already answers exactly the way the standalone window does, and both select a
// launching backend first with the same code. Plain node (http only) — this file
// ships unpacked under resources/serve-app/ and is required from both.
// Serves are PER USER (own credentials, own sessions), so join-first joins only
// a console this uid owns; otherwise the caller spawns its own, as before.

const fs = require('fs');
const http = require('http');

const AC_HOST = '127.0.0.1';
const BACKENDS = ['claude', 'gpt', 'hugpy'];
// The console's own health path (abstract_serve.serve_cli checks the same one).
const HEALTH_PATH = '/api/console/sessions';

// Where a console may already answer, in the order the packages look for one:
// the caller's own base (the Station's STATION_CONSOLE_AC), hugpy_agent's
// HUGPY_AGENT_SERVE, then 9124 (keeper), 9125 (hugpy locus), 9126 (hugpy_agent),
// 9127 (abstract_gpt serve). Normalised to http://host:port, de-duplicated.
function consoleCandidates({ first, env = process.env, acPort } = {}) {
  const port = acPort || env.AC_PORT || '9124';
  return [first, env.STATION_CONSOLE_AC, env.HUGPY_AGENT_SERVE,
    ...[port, '9125', '9126', '9127'].map((p) => `${AC_HOST}:${p}`)]
    .filter(Boolean)
    .map((u) => (/^https?:\/\//.test(u) ? u : 'http://' + u).replace(/\/+$/, ''))
    .filter((u, i, all) => all.indexOf(u) === i);
}

// `--serve <b>` / `--serve=<b>` / a bare backend arg / SERVE_BACKEND; default claude.
function pickBackend(argv = process.argv.slice(1), env = process.env) {
  const at = argv.indexOf('--serve');
  const flag = argv.find((a) => a.startsWith('--serve='));
  return [flag && flag.slice('--serve='.length), at >= 0 ? argv[at + 1] : '',
    argv.find((a) => BACKENDS.includes(a)), env.SERVE_BACKEND]
    .find((b) => BACKENDS.includes(b)) || 'claude';
}

// serve reads exactly Content-Length bytes of a POST body, so always send it.
function getJson(url, body, timeoutMs = 1500) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : JSON.stringify(body);
    const req = http.request(url, {
      method: data ? 'POST' : 'GET',
      headers: data ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } : {},
    }, (res) => {
      let buf = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { buf += c; });
      res.on('end', () => {
        if (res.statusCode < 200 || res.statusCode >= 300) return reject(new Error(`${url} -> HTTP ${res.statusCode}`));
        try { resolve(JSON.parse(buf)); } catch (e) { reject(e); }
      });
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error(`${url} timed out`)));
    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
}

// Serves are PER USER: each runs with its user's own credentials and sessions
// (vm_mgr's on 9124, hugpy's on 9125), so JOIN FIRST may only join a console
// this uid owns; joining another user's would mean working in their sessions.
// A loopback console's owner is the uid on its LISTEN socket in the kernel's
// table (/proc/net/tcp, tcp6), which a serve cannot misreport. With no such row
// (not Linux, another netns, a remote base) it is the console's own report on
// the health path (abstract-serve-core >= 0.1.10: owner.uid). Unknown or
// ambiguous = not ours.
const MY_UID = typeof process.getuid === 'function' ? process.getuid() : null;

// uids of the sockets LISTENing on `port` on this host ([] without a kernel table).
function listenerUids(port, read = (f) => fs.readFileSync(f, 'utf8')) {
  const want = ':' + Number(port).toString(16).toUpperCase().padStart(4, '0');
  const uids = new Set();
  for (const file of ['/proc/net/tcp', '/proc/net/tcp6']) {
    let text = '';
    try { text = read(file); } catch (_) { continue; }
    for (const line of text.split('\n').slice(1)) {
      const col = line.trim().split(/\s+/); // sl local rem st tx:rx tr:when retrnsmt uid ...
      if (col.length > 7 && col[3] === '0A' && col[1].endsWith(want)) uids.add(Number(col[7]));
    }
  }
  return [...uids];
}

function isLoopback(hostname) {
  const h = String(hostname).replace(/^\[|\]$/g, '');
  return h === 'localhost' || h === '::1' || /^127\./.test(h);
}

// The uid owning the console at `base` (health = its health-path reply), or null.
function consoleOwner(base, health, read) {
  let url;
  try { url = new URL(base); } catch (_) { return null; }
  if (isLoopback(url.hostname)) {
    const uids = listenerUids(url.port || (url.protocol === 'https:' ? 443 : 80), read);
    if (uids.length) return uids.length === 1 ? uids[0] : null; // two owners on one port: never join
  }
  const uid = health && health.owner && health.owner.uid;
  return Number.isInteger(uid) ? uid : null;
}

// Is the console at `base` this user's? Off POSIX (no uids) every one counts.
function ownedByMe(base, health, { uid = MY_UID, owner = consoleOwner } = {}) {
  return uid === null || owner(base, health) === uid;
}

// JOIN FIRST: the first candidate whose console answers the health path AND is
// this user's (ownedByMe), else null (the caller spawns one). Probed strictly in
// order, one at a time.
async function findConsole(candidates, probe = getJson, { uid = MY_UID, owner = consoleOwner, log = console.log } = {}) {
  for (const base of candidates) {
    let health;
    try { health = await probe(base + HEALTH_PATH); } catch (_) { continue; }
    if (ownedByMe(base, health, { uid, owner })) return base;
    const who = owner(base, health);
    log(`[serve] ${base} is ${who === null ? 'an unverified user' : 'uid ' + who}'s console, not uid ${uid}'s: not joining`);
  }
  return null;
}

// Each console bakes its UI base into index.html's asset paths (/ac, /claude, ...).
function uiBaseOf(base, fallback = process.env.AC_UI_BASE || '/ac') {
  return new Promise((resolve) => {
    const req = http.get(base + '/', (res) => {
      let html = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { html += c; });
      res.on('end', () => {
        const m = html.match(/"((?:\/[^"/]+)*)\/assets\//);
        resolve(m ? m[1] : fallback);
      });
    });
    req.on('error', () => resolve(fallback));
    req.setTimeout(3000, () => { req.destroy(); resolve(fallback); });
  });
}

// Select the launching package's backend first, the same way the console's own
// model picker does (keeper-live.js setConversationModel / showProviderSession):
// reuse the newest console session on that backend, else POST
// /api/console/sessions, then point localStorage ac_selected + kl-goto at it and
// reload. Claude is the console's own default view, so for claude it only drops
// another backend's selection. Standing-role sessions (keeper/chat/worker, from
// /api/session/roster) are never picked. A backend this install lacks is left
// alone; the console lists only the installed ones.
//   exec(js) runs js in the console page (webContents.executeJavaScript).
// Returns what it did: 'unavailable' | 'kept' | 'claude-default' | 'selected:<id>'.
async function preselect({ consoleBase, backend, exec, fetchJson = getJson, log = console.log }) {
  const d = await fetchJson(consoleBase + HEALTH_PATH);
  const backends = d.backends || [];
  if (!backends.includes(backend)) {
    log(`[serve] ${backend} is not available here (backends: ${backends.join(', ')}); selection unchanged`);
    return 'unavailable';
  }
  const { sel, mode } = JSON.parse(await exec(
    'JSON.stringify({sel: localStorage.getItem("ac_selected"), mode: localStorage.getItem("acp-new-mode") || ""})'));
  const sessions = d.sessions || [];               // newest first (ORDER BY updated DESC)
  const cur = sel ? sessions.find((s) => s.id === sel || s.native_id === sel) : undefined;
  if (cur ? cur.backend === backend : backend === 'claude') return 'kept';
  if (backend === 'claude') {
    log('[serve] selecting claude (console default view)');
    await exec('localStorage.removeItem("ac_selected"); location.reload();');
    return 'claude-default';
  }
  const roster = await fetchJson(consoleBase + '/api/session/roster').catch(() => ({}));
  const standing = new Set((roster.roles || []).flatMap((r) => [r.session_id, r.live_session_id]).filter(Boolean));
  let pick = sessions.find((s) => s.backend === backend && !standing.has(s.id) && !standing.has(s.native_id));
  if (!pick) {
    pick = await fetchJson(consoleBase + HEALTH_PATH, { backend, permission_mode: mode }, 15000);
  }
  log(`[serve] selecting ${backend} session ${pick.id}`);
  await exec(
    `localStorage.setItem("ac_selected", ${JSON.stringify(pick.id)}); ` +
    `localStorage.setItem("kl-goto", ${JSON.stringify(pick.id)}); location.reload();`);
  return 'selected:' + pick.id;
}

module.exports = { AC_HOST, BACKENDS, HEALTH_PATH, consoleCandidates, pickBackend, getJson,
  listenerUids, consoleOwner, ownedByMe, findConsole, uiBaseOf, preselect };
