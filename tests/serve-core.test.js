'use strict';
// serve-core (resources/serve-app/serve-core.js): the join-first probe order and
// the backend preselect shared by the Station console view and the serve window.
// Run:  node --test tests/serve-core.test.js      (no deps; stubs for fetch/exec)
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const core = require(path.join(__dirname, '..', 'resources', 'serve-app', 'serve-core.js'));

test('candidates: caller base, STATION_CONSOLE_AC, HUGPY_AGENT_SERVE, then 9124..9127, deduped', () => {
  assert.deepEqual(core.consoleCandidates({ env: {} }), [
    'http://127.0.0.1:9124', 'http://127.0.0.1:9125', 'http://127.0.0.1:9126', 'http://127.0.0.1:9127']);
  assert.deepEqual(core.consoleCandidates({
    first: 'http://127.0.0.1:9124/',
    env: { STATION_CONSOLE_AC: '127.0.0.1:9124', HUGPY_AGENT_SERVE: 'http://10.0.0.5:9300' },
  }), ['http://127.0.0.1:9124', 'http://10.0.0.5:9300', 'http://127.0.0.1:9125',
       'http://127.0.0.1:9126', 'http://127.0.0.1:9127']);
  assert.equal(core.consoleCandidates({ env: { AC_PORT: '9200' } })[0], 'http://127.0.0.1:9200');
});

test('findConsole probes strictly in order and joins the FIRST that answers the health path', async () => {
  const seen = [];
  const up = new Set(['http://127.0.0.1:9126', 'http://127.0.0.1:9127']);
  const probe = async (url) => {
    seen.push(url);
    const base = url.slice(0, -core.HEALTH_PATH.length);
    if (!url.endsWith(core.HEALTH_PATH) || !up.has(base)) throw new Error('down');
    return { sessions: [] };
  };
  const mine = { uid: 1003, owner: () => 1003 };
  const got = await core.findConsole(core.consoleCandidates({ env: {} }), probe, mine);
  assert.equal(got, 'http://127.0.0.1:9126');
  assert.deepEqual(seen, ['http://127.0.0.1:9124', 'http://127.0.0.1:9125', 'http://127.0.0.1:9126']
    .map((b) => b + '/api/console/sessions'));
  assert.equal(await core.findConsole(['http://127.0.0.1:1'], async () => { throw new Error('x'); }, mine), null);
});

// /proc/net/tcp{,6} as the kernel writes it: header, then one row per socket
// (st 0A = LISTEN; uid is column 8). Ports in hex: 9124 = 23A4, 9125 = 23A5.
const HEAD = '  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n';
const row = (addr, port, st, uid) =>
  `   0: ${addr}:${port.toString(16).toUpperCase()} 00000000:0000 ${st} 00000000:00000000 00:00000000 00000000  ${uid}  0 1 1\n`;
const PROC = {
  '/proc/net/tcp': HEAD + row('0100007F', 9124, '0A', 1003) + row('0100007F', 9125, '0A', 1012)
    + row('0100007F', 9126, '01', 1012) + row('0100007F', 9126, '0A', 1003),
  '/proc/net/tcp6': HEAD + row('00000000000000000000000001000000', 9127, '0A', 1012)
    + row('00000000000000000000000000000000', 9128, '0A', 1003) + row('0100007F', 9128, '0A', 1012),
};
const readProc = (f) => { if (!(f in PROC)) throw new Error('ENOENT'); return PROC[f]; };

test('listenerUids: the uid on each LISTEN socket of a port, from tcp and tcp6', () => {
  assert.deepEqual(core.listenerUids(9124, readProc), [1003]);
  assert.deepEqual(core.listenerUids(9125, readProc), [1012]);
  assert.deepEqual(core.listenerUids(9126, readProc), [1003]);        // the ESTABLISHED row is not a listener
  assert.deepEqual(core.listenerUids(9127, readProc), [1012]);        // ::1, tcp6
  assert.deepEqual(core.listenerUids(9200, readProc), []);
  assert.deepEqual(core.listenerUids(9124, () => { throw new Error('no /proc'); }), []);
});

test('consoleOwner: the kernel uid for a loopback console; its own report elsewhere; else unknown', () => {
  const liar = { owner: { uid: 1003 } };                               // a serve cannot claim someone else's port
  assert.equal(core.consoleOwner('http://127.0.0.1:9125', liar, readProc), 1012);
  assert.equal(core.consoleOwner('http://localhost:9124', {}, readProc), 1003);
  assert.equal(core.consoleOwner('http://[::1]:9127', liar, readProc), 1012);
  assert.equal(core.consoleOwner('http://127.0.0.1:9128', liar, readProc), null);   // two owners: ambiguous
  assert.equal(core.consoleOwner('http://127.0.0.1:9200', { owner: { uid: 1003 } }, readProc), 1003); // no row: its report
  assert.equal(core.consoleOwner('http://10.0.0.5:9124', { owner: { uid: 1012 } }, readProc), 1012); // not loopback
  assert.equal(core.consoleOwner('http://10.0.0.5:9300', { sessions: [] }, readProc), null);  // pre-0.1.10 serve
  assert.equal(core.consoleOwner('not a url', {}, readProc), null);
});

test('findConsole joins only a console owned by the SAME uid; another user\'s is skipped, none -> spawn', async () => {
  const owners = { 'http://127.0.0.1:9124': 1012, 'http://127.0.0.1:9125': 1003, 'http://127.0.0.1:9126': null };
  const up = async () => ({ sessions: [] });
  const owner = (base) => owners[base];
  const logs = [];
  const log = (m) => logs.push(m);
  const cands = core.consoleCandidates({ env: {} });
  // hugpy (1012) is on 9124 here: vm_mgr (1003) skips it and joins its own on 9125
  assert.equal(await core.findConsole(cands, up, { uid: 1003, owner, log }), 'http://127.0.0.1:9125');
  assert.match(logs[0], /9124 is uid 1012's console, not uid 1003's: not joining/);
  // hugpy finds only vm_mgr's and an unverifiable one: joins neither -> spawns its own
  assert.equal(await core.findConsole(['http://127.0.0.1:9125', 'http://127.0.0.1:9126'], up, { uid: 1012, owner, log }), null);
  assert.match(logs.at(-1), /9126 is an unverified user's console/);
  // the default owner check reads the kernel table: real /proc, a port nobody listens on
  assert.equal(core.ownedByMe('http://127.0.0.1:1', { owner: { uid: process.getuid() } }), true);
  assert.equal(core.ownedByMe('http://127.0.0.1:1', { owner: { uid: process.getuid() + 1 } }), false);
  assert.equal(core.ownedByMe('http://127.0.0.1:1', {}, { uid: null }), true);   // no uids (not POSIX)
});

test('pickBackend: --serve=, --serve <b>, bare arg, SERVE_BACKEND, default claude', () => {
  assert.equal(core.pickBackend(['--serve=gpt'], {}), 'gpt');
  assert.equal(core.pickBackend(['--serve', 'hugpy'], {}), 'hugpy');
  assert.equal(core.pickBackend(['x', 'gpt'], {}), 'gpt');
  assert.equal(core.pickBackend([], { SERVE_BACKEND: 'hugpy' }), 'hugpy');
  assert.equal(core.pickBackend(['--serve', 'nope'], {}), 'claude');
});

function page(store) {
  const ran = [];
  const exec = async (js) => {
    ran.push(js);
    if (js.startsWith('JSON.stringify')) return JSON.stringify({ sel: store.ac_selected || null, mode: store.mode || '' });
    return undefined;
  };
  return { ran, exec };
}

test('preselect: absent backend is left alone; current selection on the backend is kept', async () => {
  const fetchJson = async () => ({ backends: ['claude'], sessions: [] });
  const p = page({});
  assert.equal(await core.preselect({ consoleBase: 'b', backend: 'gpt', exec: p.exec, fetchJson, log() {} }), 'unavailable');
  assert.equal(p.ran.length, 0);
  const f2 = async () => ({ backends: ['claude', 'gpt'], sessions: [{ id: 's1', backend: 'gpt' }] });
  const p2 = page({ ac_selected: 's1' });
  assert.equal(await core.preselect({ consoleBase: 'b', backend: 'gpt', exec: p2.exec, fetchJson: f2, log() {} }), 'kept');
});

test('preselect: claude drops another backend selection; gpt reuses newest non-standing session', async () => {
  const sessions = [{ id: 'g-standing', backend: 'gpt' }, { id: 'g2', backend: 'gpt' }, { id: 'c1', backend: 'claude' }];
  const fetchJson = async (url) => url.endsWith('/api/session/roster')
    ? { roles: [{ session_id: 'g-standing' }] } : { backends: ['claude', 'gpt'], sessions };
  const p = page({ ac_selected: 'g2' });
  assert.equal(await core.preselect({ consoleBase: 'b', backend: 'claude', exec: p.exec, fetchJson, log() {} }), 'claude-default');
  assert.match(p.ran[1], /removeItem\("ac_selected"\)/);
  const q = page({ ac_selected: 'c1' });
  assert.equal(await core.preselect({ consoleBase: 'b', backend: 'gpt', exec: q.exec, fetchJson, log() {} }), 'selected:g2');
  assert.match(q.ran[1], /"ac_selected", "g2"/);
});

test('preselect: no session on the backend -> POST a new one with the console permission mode', async () => {
  const posts = [];
  const fetchJson = async (url, body) => {
    if (body) { posts.push([url, body]); return { id: 'h-new' }; }
    return url.endsWith('/api/session/roster') ? {} : { backends: ['claude', 'hugpy'], sessions: [] };
  };
  const p = page({ mode: 'plan' });
  assert.equal(await core.preselect({ consoleBase: 'http://c', backend: 'hugpy', exec: p.exec, fetchJson, log() {} }), 'selected:h-new');
  assert.deepEqual(posts, [['http://c/api/console/sessions', { backend: 'hugpy', permission_mode: 'plan' }]]);
});
