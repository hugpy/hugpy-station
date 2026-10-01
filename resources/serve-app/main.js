'use strict';
// hugpy serve — the shared Serve console (abstract_serve) in a standalone window.
// The launcher is lifted as-is from the hugpy Station shell (app.asar main.js:
// acAnswering / startServe / startServeIfNeeded / waitForServe / stopServe):
// attach to a console that is already answering, else spawn
// resources/bin/abstract-claude-serve-run, and only ever kill a child we spawned.
// The console itself discovers the installed backends (each of abstract-claude,
// abstract-gpt, hugpy-agent registers in abstract_serve's provider group), so the
// one choice made here is which backend is selected first:
//   run.sh [claude|gpt|hugpy]        hugpy-station --serve [claude|gpt|hugpy]
// (default claude; or SERVE_BACKEND=...).

const { app, BrowserWindow, shell } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const http = require('http');
// join-first probe + backend preselect: shared with the Station's console view
const core = require('./serve-core');

const { AC_HOST } = core;
const AC_PORT = process.env.AC_PORT || '9124';
const BACKEND = core.pickBackend();

// Where a console may already answer (see serve-core.consoleCandidates): the
// station's STATION_CONSOLE_AC, hugpy_agent's HUGPY_AGENT_SERVE, then 9124
// (keeper), 9125 (hugpy locus), 9126 (hugpy_agent), 9127 (abstract_gpt serve).
const CANDIDATES = core.consoleCandidates({ acPort: AC_PORT });

// No usable GPU on this display: without this the window paints blank.
app.disableHardwareAcceleration();
// Own profile, so `hugpy-station --serve` never shares the running Station's.
app.setPath('userData', path.join(app.getPath('appData'), 'hugpy-serve'));

let serve = null;         // null when an external console already answers
let win = null;
let consoleBase = null;
let uiUrl = null;
let preselected = false;

function serveRunPath() {
  const local = path.join(__dirname, '..', 'bin', 'abstract-claude-serve-run');
  return fs.existsSync(local) ? local : '/opt/hugpy-station/resources/bin/abstract-claude-serve-run';
}

function startServe() {
  const runner = serveRunPath();
  serve = spawn(runner, ['station'], {
    env: { ...process.env, AC_HOST, AC_PORT },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  serve.stdout.on('data', (d) => console.log('[ac-serve]', d.toString().trimEnd()));
  serve.stderr.on('data', (d) => console.error('[ac-serve]', d.toString().trimEnd()));
  serve.on('error', (err) => console.error('[ac-serve] failed to start:', err.message));
  serve.on('exit', (code, sig) => {
    console.error(`[ac-serve] exited code=${code} sig=${sig}`);
    serve = null;
  });
}

function waitForServe(retries = 40, delayMs = 500) {
  return new Promise((resolve, reject) => {
    const tick = (n) => {
      const req = http.get(consoleBase + '/', (res) => {
        res.resume();
        console.log('[ac-serve] answering at', consoleBase);
        resolve();
      });
      req.on('error', () => {
        if (n <= 0) return reject(new Error('ac serve did not answer in time'));
        setTimeout(() => tick(n - 1), delayMs);
      });
    };
    tick(retries);
  });
}

// Only kill serve if WE spawned it. The wrapper exec's abstract-claude in
// place, so the child pid IS serve.
function stopServe() {
  if (serve && !serve.killed) {
    try { serve.kill('SIGTERM'); } catch (_) {}
  }
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    title: 'hugpy serve',
    backgroundColor: '#0d1117',
    webPreferences: { contextIsolation: true },
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
  // a spawned serve can still be coming up: retry instead of leaving an error page
  win.webContents.on('did-fail-load', (_e, _code, _desc, url, isMainFrame) => {
    if (isMainFrame && win && uiUrl) setTimeout(() => win && win.loadURL(uiUrl), 2000);
  });
  win.webContents.on('did-finish-load', () => {
    if (preselected || !uiUrl || !win.webContents.getURL().startsWith(uiUrl)) return;
    preselected = true;
    core.preselect({ consoleBase, backend: BACKEND, exec: (js) => win.webContents.executeJavaScript(js) })
      .catch((err) => console.error('[serve] preselect:', err.message));
  });
  win.on('closed', () => { win = null; });
}

app.whenReady().then(async () => {
  console.log('[serve] backend first:', BACKEND);
  consoleBase = await core.findConsole(CANDIDATES);
  if (consoleBase) {
    console.log('[ac-serve] console already answering at', consoleBase, '- not spawning a duplicate');
  } else {
    consoleBase = `http://${AC_HOST}:${AC_PORT}`;
    startServe();
  }
  createWindow();
  try { await waitForServe(); } catch (err) { console.error('[ac-serve]', err.message); }
  uiUrl = consoleBase + (await core.uiBaseOf(consoleBase)) + '/';
  if (win) win.loadURL(uiUrl);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) { createWindow(); win.loadURL(uiUrl); }
  });
});

app.on('window-all-closed', () => { app.quit(); });
app.on('before-quit', stopServe);
app.on('will-quit', stopServe);
process.on('exit', stopServe);
