'use strict';
// hugpy Station desktop shell.
// On launch: spawn the bundled console backend (server.py) on a private
// loopback port, wait for it to answer, then load it in a native window.
// The backend serves both the UI and the /api it needs, and manages the
// LOCAL machine's LXD. On quit we kill the backend child.

const electron = require('electron');
const { app, BrowserWindow, Menu, shell, dialog, ipcMain } = electron;
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');
// observability (OFF by default): only the side-effect-free switch loads here.
const obsToggle = require('./observability/toggle');

const HOST = '127.0.0.1';
const PORT = process.env.CONSOLE_PORT || '8899'; // private port; avoids clashing with a system console-standalone.service on 8800
const BASE = `http://${HOST}:${PORT}`;

// abstract-claude serve — the keeper console (abstract_claude PyPI package).
// server.py reverse-proxies it same-origin at /ac/ (STATION_CONSOLE_AC). Nothing
// else launches it, so we own it here: spawn resources/bin/abstract-claude-serve-run
// (the same wrapper the abstract-claude-serve@station systemd unit's ExecStart
// runs — it sets up AC_ROOT, the /ac UI rewrite and the settings template, then
// exec's `abstract-claude serve --host $AC_HOST --port $AC_PORT`). We pin
// STATION_CONSOLE_AC on the backend so the /ac proxy and serve agree on the port.
const AC_HOST = '127.0.0.1';
const AC_PORT = process.env.AC_PORT || '9124'; // serve's default listen port (AC_PORT)
const AC_BASE = process.env.STATION_CONSOLE_AC || `http://${AC_HOST}:${AC_PORT}`;
// serve-core (1.0.138): the join-first probe + backend preselect shared with the
// standalone serve window (resources/serve-app/main.js) — "Serve IS the Electron
// serve app". Ships unpacked next to serve-app (extraResources).
const serveCore = require(app.isPackaged
  ? path.join(process.resourcesPath, 'serve-app', 'serve-core.js')
  : path.join(__dirname, 'resources', 'serve-app', 'serve-core.js'));
// The console the /ac proxy and the native view front: AC_BASE, or the console
// the join-first probe found already answering (startServeIfNeeded).
let acBase = AC_BASE;

let backend = null;
let backendReady = false;
let serve = null;         // null when an external instance already owns AC_BASE
let serveReady = false;
let win = null;

// ── abstract-claude serve console as a NATIVE view ──────────────────────────
// The frontend used to frame /ac/ in an <iframe> inside #fv-term-pane. Here we
// render it as a real Electron view (WebContentsView, or BrowserView on older
// Electron) overlaid on the window, driven by the renderer over IPC (the
// window.stationConsole bridge — see preload.js). The view is anchored to the
// pane's measured rect and kept hidden until the renderer asks it to show.
const HAS_WCV = typeof electron.WebContentsView === 'function';
const AC_CONSOLE_URL = BASE + '/ac/'; // same-origin /ac proxy (shim + holding page)
let consoleView = null;
let consoleShown = false;
let consolePreselected = false;
// An EXPLICIT first backend for the native console (SERVE_BACKEND=claude|gpt|hugpy
// or --console-backend=<b>) is applied with serve-core's preselect on the first
// show — the same selection the standalone serve window makes. Unset (the
// default) leaves the selection to the console / the SPA's frontier picker.
const CONSOLE_BACKEND = (() => {
  const a = process.argv.find((x) => x.startsWith('--console-backend='));
  const b = (a && a.slice('--console-backend='.length)) || process.env.SERVE_BACKEND || '';
  return serveCore.BACKENDS.includes(b) ? b : '';
})();
let browserView = null;
let browserShown = false;

function createConsoleView() {
  if (!win || consoleView) return consoleView;
  const opts = { webPreferences: { contextIsolation: true } };
  if (HAS_WCV) {
    consoleView = new electron.WebContentsView(opts);
    win.contentView.addChildView(consoleView); // overlays the SPA web contents
  } else {
    consoleView = new electron.BrowserView(opts);
    win.addBrowserView(consoleView);
  }
  // external links from the console open in the system browser
  consoleView.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
  // start hidden and warm: load the same-origin /ac proxy (it retries while
  // serve boots), collapsed to nothing until the renderer positions it.
  consoleView.setBounds({ x: 0, y: 0, width: 0, height: 0 });
  consoleView.webContents.loadURL(AC_CONSOLE_URL);
  return consoleView;
}

function setConsoleBounds(rect) {
  if (!consoleView || !rect) return;
  consoleView.setBounds({
    x: Math.round(rect.x || 0),
    y: Math.round(rect.y || 0),
    width: Math.max(0, Math.round(rect.width || 0)),
    height: Math.max(0, Math.round(rect.height || 0)),
  });
}

function showConsole(rect) {
  createConsoleView();
  if (!consoleView) return;
  consoleShown = true;
  try { if (typeof consoleView.setVisible === 'function') consoleView.setVisible(true); } catch (_) {}
  setConsoleBounds(rect);
  if (CONSOLE_BACKEND && !consolePreselected) {
    consolePreselected = true;
    const wc = consoleView.webContents;
    const run = () => serveCore.preselect({ consoleBase: acBase, backend: CONSOLE_BACKEND,
      exec: (js) => wc.executeJavaScript(js) })
      .catch((err) => console.error('[ac-serve] preselect:', err.message));
    if (wc.isLoading()) wc.once('did-finish-load', run); else run();
  }
}

function hideConsole() {
  if (!consoleView) return;
  consoleShown = false;
  try { if (typeof consoleView.setVisible === 'function') consoleView.setVisible(false); } catch (_) {}
  // setVisible isn't on every Electron View version; collapsing the bounds too
  // guarantees it leaves no sliver over the pane.
  setConsoleBounds({ x: 0, y: 0, width: 0, height: 0 });
}

function registerConsoleIpc() {
  ipcMain.on('console:show', (_e, rect) => showConsole(rect));
  ipcMain.on('console:hide', () => hideConsole());
  // ignore stray resize pings while hidden (ResizeObserver keeps firing)
  ipcMain.on('console:setBounds', (_e, rect) => { if (consoleShown) setConsoleBounds(rect); });
  ipcMain.on('console:reload', () => {
    if (consoleView) { try { consoleView.webContents.reload(); } catch (_) {} }
  });
}

// 1.0.143: the Station page's clipboard (preload window.stationClipboard) —
// text only, answered only for the main Station window's own page.
function registerClipboardIpc() {
  const fromStation = (e) => !!(win && e && e.sender && e.sender.id === win.webContents.id);
  ipcMain.handle('clipboard:readText', (e) => (fromStation(e) ? electron.clipboard.readText() : ''));
  ipcMain.handle('clipboard:writeText', (e, text) => {
    if (!fromStation(e)) return false;
    electron.clipboard.writeText(String(text == null ? '' : text).slice(0, 8 * 1024 * 1024));
    return true;
  });
}

function createBrowserView() {
  if (!win || browserView) return browserView;
  const opts = { webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false } };
  if (HAS_WCV) {
    browserView = new electron.WebContentsView(opts);
    win.contentView.addChildView(browserView);
  } else {
    browserView = new electron.BrowserView(opts);
    win.addBrowserView(browserView);
  }
  browserView.setBounds({ x: 0, y: 0, width: 0, height: 0 });
  browserView.webContents.setWindowOpenHandler(({ url }) => { browserView.webContents.loadURL(url); return { action: 'deny' }; });
  const emit = (kind, body) => win.webContents.send('browser:event', { kind, body, url: browserView.webContents.getURL(), ts: Date.now() / 1000 });
  browserView.webContents.on('console-message', (_e, level, message, line, sourceId) => emit('browser_console', { level, message, line, sourceId }));
  browserView.webContents.on('did-start-loading', () => emit('browser_navigation', { phase: 'start' }));
  browserView.webContents.on('did-stop-loading', () => emit('browser_navigation', { phase: 'stop', title: browserView.webContents.getTitle() }));
  browserView.webContents.on('did-fail-load', (_e, code, description, validatedURL) => emit('browser_error', { code, description, validatedURL }));
  browserView.webContents.session.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
    emit('browser_request', { requestId: details.id, method: details.method, resourceType: details.resourceType, url: details.url });
    callback({});
  });
  browserView.webContents.session.webRequest.onCompleted({ urls: ['<all_urls>'] }, details => emit('browser_response', { requestId: details.id, statusCode: details.statusCode, url: details.url, method: details.method }));
  return browserView;
}

function setBrowserBounds(rect) {
  if (!browserView || !rect) return;
  browserView.setBounds({ x: Math.round(rect.x || 0), y: Math.round(rect.y || 0), width: Math.max(0, Math.round(rect.width || 0)), height: Math.max(0, Math.round(rect.height || 0)) });
}

function registerBrowserIpc() {
  ipcMain.on('browser:open-capture', () => openObservability());
  ipcMain.on('browser:show', (_e, rect) => { createBrowserView(); browserShown = true; try { browserView.setVisible(true); } catch (_) {} setBrowserBounds(rect); });
  ipcMain.on('browser:hide', () => { if (!browserView) return; browserShown = false; try { browserView.setVisible(false); } catch (_) {} setBrowserBounds({ x: 0, y: 0, width: 0, height: 0 }); });
  ipcMain.on('browser:setBounds', (_e, rect) => { if (browserShown) setBrowserBounds(rect); });
  ipcMain.on('browser:navigate', (_e, url) => { if (browserView && /^https?:\/\//i.test(String(url || '').trim())) browserView.webContents.loadURL(String(url).trim()); });
  ipcMain.on('browser:reload', () => { if (browserView) browserView.webContents.reload(); });
}

function backendDir() {
  // packaged -> resources/backend ; dev -> ./backend
  return app.isPackaged
    ? path.join(process.resourcesPath, 'backend')
    : path.join(__dirname, 'backend');
}

let pythonChoice = null;

// The backend needs aiohttp. Trust the launcher's probed choice
// (HUGPY_STATION_PYTHON), else probe: distro interpreter first (that's where
// the package dependency python3-aiohttp lives), then whatever is on PATH.
// A conda/pyenv `python3` first on PATH with a half-installed aiohttp
// produced a window that could never connect (1.0.41 on Fedora).
function pythonExe() {
  if (pythonChoice) return pythonChoice;
  const { spawnSync } = require('child_process');
  const cands = [
    process.env.HUGPY_STATION_PYTHON,
    ...(process.platform === 'win32'
      ? ['python', 'py']
      : ['/usr/bin/python3', '/usr/local/bin/python3', 'python3', 'python']),
  ].filter(Boolean);
  for (const c of cands) {
    try {
      const r = spawnSync(c, ['-c', 'import aiohttp'], { stdio: 'ignore', timeout: 5000 });
      if (r.status === 0) { pythonChoice = c; return c; }
    } catch (_) { /* try next */ }
  }
  pythonChoice = cands[cands.length - 1] || 'python3';
  console.error(`[backend] no interpreter with aiohttp found among ${cands.join(', ')}; falling back to ${pythonChoice}`);
  return pythonChoice;
}

function startBackend() {
  const dir = backendDir();
  const server = path.join(dir, 'server.py');
  backend = spawn(pythonExe(), [server], {
    cwd: dir,
    // STATION_CONSOLE_AC points server.py's /ac proxy at the serve we own or joined.
    // HUGPY_STATION_DESKTOP_PID (1.0.139) marks this backend as OURS: the launcher
    // reaps a backend holding the station port only when the Station named here
    // is gone — never the headless hugpy-station-web@ backend or a live Station.
    env: { ...process.env, HOST, PORT, STATION_CONSOLE_AC: acBase,
           HUGPY_STATION_DESKTOP_PID: String(process.pid) },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  backend.stdout.on('data', (d) => console.log('[backend]', d.toString().trimEnd()));
  backend.stderr.on('data', (d) => console.error('[backend]', d.toString().trimEnd()));
  backend.on('error', (err) => {
    dialog.showErrorBox(
      'Backend failed to start',
      `Could not launch the console backend with "${pythonExe()}".\n\n` +
        `Make sure Python 3 (and python3-aiohttp) is installed.\n\n${err.message}`
    );
  });
  backend.on('exit', (code, sig) => {
    console.error(`[backend] exited code=${code} sig=${sig}`);
    backend = null;
    // A backend that dies before answering leaves a blank window; say why.
    if (!backendReady && code !== 0) {
      dialog.showErrorBox(
        'Console backend exited',
        `"${pythonExe()} server.py" exited with code ${code} before the console came up.\n\n` +
          `Usually a missing Python dependency (aiohttp) for that interpreter. ` +
          `Set HUGPY_STATION_PYTHON to one that has it, or install it:\n` +
          `  ${pythonExe()} -m pip install aiohttp\n\n` +
          `Details: ~/.cache/hugpy-station-launch.log`
      );
    }
  });
}

function waitForBackend(retries = 60, delayMs = 500) {
  return new Promise((resolve, reject) => {
    const tick = (n) => {
      const req = http.get(BASE + '/', (res) => {
        res.resume();
        backendReady = true;
        resolve();
      });
      req.on('error', () => {
        if (n <= 0) return reject(new Error('backend did not answer in time'));
        setTimeout(() => tick(n - 1), delayMs);
      });
    };
    tick(retries);
  });
}

function stopBackend() {
  if (backend && !backend.killed) {
    try {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/pid', String(backend.pid), '/t', '/f']);
      } else {
        backend.kill('SIGTERM');
      }
    } catch (_) {}
  }
}

// Path to the bundled serve wrapper: packaged -> resources/bin ; dev -> ./resources/bin.
function serveRunPath() {
  return app.isPackaged
    ? path.join(process.resourcesPath, 'bin', 'abstract-claude-serve-run')
    : path.join(__dirname, 'resources', 'bin', 'abstract-claude-serve-run');
}

// Is something already answering on the serve upstream? (e.g. the systemd unit
// abstract-claude-serve@station is already running — don't spawn a duplicate.)
function acAnswering() {
  return new Promise((resolve) => {
    const req = http.get(AC_BASE + '/', (res) => { res.resume(); resolve(true); });
    req.on('error', () => resolve(false));
    req.setTimeout(1500, () => { req.destroy(); resolve(false); });
  });
}

function startServe() {
  const runner = serveRunPath();
  serve = spawn(runner, ['station'], {
    // AC_HOST/AC_PORT tell the wrapper where to listen; must match AC_BASE above.
    env: { ...process.env, AC_HOST, AC_PORT },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  serve.stdout.on('data', (d) => console.log('[ac-serve]', d.toString().trimEnd()));
  serve.stderr.on('data', (d) => console.error('[ac-serve]', d.toString().trimEnd()));
  // Serve being slow or absent is non-fatal: the backend's /ac holding page
  // retries on its own, so we only log — never a modal that blocks the window.
  serve.on('error', (err) => console.error('[ac-serve] failed to start:', err.message));
  serve.on('exit', (code, sig) => {
    console.error(`[ac-serve] exited code=${code} sig=${sig}`);
    serve = null;
  });
}

// JOIN FIRST (serve-core, same probe as `hugpy-station --serve`): attach to a
// console already answering on AC_BASE, HUGPY_AGENT_SERVE or 9124-9127; only
// when none answers do we spawn our own on AC_PORT. A pre-health-path serve that
// answers `/` on AC_BASE is still joined (the pre-1.0.138 acAnswering check).
// Either way only THIS user's serve is joined (serve-core ownedByMe): serves are
// per user, with their own credentials and sessions.
async function startServeIfNeeded() {
  const found = await serveCore.findConsole(serveCore.consoleCandidates({ first: AC_BASE, acPort: AC_PORT }));
  if (found || (await acAnswering() && serveCore.ownedByMe(AC_BASE))) {
    acBase = found || AC_BASE;
    serveReady = true; // an external instance (systemd unit, serve window) already answers
    console.log('[ac-serve] console already answering at', acBase, '- joining it, not spawning a duplicate');
    return;
  }
  startServe();
}

// Best-effort readiness log. Never blocks window creation — the /ac page retries.
function waitForServe(retries = 40, delayMs = 500) {
  return new Promise((resolve, reject) => {
    const tick = (n) => {
      const req = http.get(acBase + '/', (res) => {
        res.resume();
        serveReady = true;
        console.log('[ac-serve] answering at', acBase);
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

// Only kill serve if WE spawned it (serve is null for an external instance).
// The wrapper exec's abstract-claude in place, so the child pid IS serve.
function stopServe() {
  if (serve && !serve.killed) {
    try {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/pid', String(serve.pid), '/t', '/f']);
      } else {
        serve.kill('SIGTERM');
      }
    } catch (_) {}
  }
}

// Help → About: version, working directory, source, attributions. The backend
// facts come from /api/about fetched IN THE PAGE (its session cookie makes the
// call work whether or not a console password is set); if the backend is down
// the dialog still opens with what the shell knows on its own.
async function showAbout() {
  let a = {};
  try {
    if (win) {
      a = (await win.webContents.executeJavaScript(
        `fetch('/api/about').then(r => r.ok ? r.json() : {}).catch(() => ({}))`, true)) || {};
    }
  } catch (_) { /* fall through to shell-known facts */ }
  dialog.showMessageBox(win, {
    type: 'info',
    title: 'About hugpy Station',
    message: `hugpy Station ${a.version || app.getVersion()}`,
    detail:
      `working directory: ${a.backend_dir || backendDir()}\n` +
      `app shell: ${app.isPackaged ? path.dirname(process.resourcesPath) : __dirname}\n` +
      `backend: ${BASE}${a.python ? `  ·  python ${a.python}` : ''}\n` +
      `source: ${a.source || 'station-app monorepo (hugpy project)'}\n\n` +
      `${a.attribution || 'hugpy Station is part of the hugpy project (https://hugpy.ai).'}\n\n` +
      `© ${a.author || 'putkoff (hugpy.ai)'} — ${a.homepage || 'https://hugpy.ai'}`,
  });
}

// ── observability: the in-app trace browser (observability/README.md) ──────
// OFF by default. Only observability/toggle.js (no side effects) is loaded at
// startup; the capture engine is required on first open and only when enabled,
// so with the toggle off the Station runs exactly as before. Replaces the
// external hugpy-observability .deb launcher (never shipped/installed).
let obsModule = null;

async function openObservability() {
  const userData = app.getPath('userData');
  if (!obsToggle.isEnabled(userData)) {
    if (obsToggle.envOverride() === false) {
      dialog.showMessageBox(win, { type: 'info', title: 'Observability',
        message: 'Observability is disabled by HUGPY_STATION_OBSERVABILITY=0.' });
      return;
    }
    const { response } = await dialog.showMessageBox(win, { type: 'question', title: 'Observability',
      message: 'Observability is off.',
      detail: 'It opens a separate trace browser (own session) that attaches Chrome DevTools Protocol to the page you load, ' +
        'tags targeted requests with a per-request X-Hugpy-Trace-Id and reads the matching server spans. Nothing runs while it is off.',
      buttons: ['Enable and open', 'Cancel'], defaultId: 0, cancelId: 1 });
    if (response !== 0) return;
    obsToggle.setEnabled(userData, true);
    buildMenu();
  }
  try {
    obsModule = obsModule || require('./observability');
    // saved traces are also filed in this Station's provenance ledger
    await obsModule.open({ userData, provenanceUrl: `${BASE}/api/provenance/ingest` });
  } catch (err) {
    console.error('[observability]', err);
    dialog.showErrorBox('Observability failed to open', String(err && err.message || err));
  }
}

function setObservabilityEnabled(on) {
  obsToggle.setEnabled(app.getPath('userData'), on);
  if (!on && obsModule) obsModule.close();
  buildMenu();
}

function buildMenu() {
  const template = [
    {
      label: 'File',
      submenu: [{ role: 'quit' }],
    },
    {
      label: 'View',
      submenu: [
        { label: 'Reload', accelerator: 'CmdOrCtrl+R', click: () => win && win.reload() },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Help',
      submenu: [
        {
          label: 'Open in browser',
          click: () => shell.openExternal(BASE),
        },
        {
          label: 'Observability (trace browser)',
          click: openObservability,
        },
        {
          label: 'Enable observability',
          type: 'checkbox',
          checked: obsToggle.isEnabled(app.getPath('userData')),
          enabled: obsToggle.envOverride() === null,   // env-forced: show, don't fight it
          click: (item) => setObservabilityEnabled(item.checked),
        },
        { type: 'separator' },
        {
          label: 'About hugpy Station',
          click: showAbout,
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 820,
    backgroundColor: '#0d1117',
    title: 'hugpy Station',
    // preload exposes window.stationConsole (contextBridge) so the SPA can
    // drive the native /ac console view; contextIsolation stays on.
    webPreferences: { contextIsolation: true, preload: path.join(__dirname, 'preload.js') },
  });
  // external links open in the system browser, not inside the shell
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
  win.loadURL(BASE);
  // build the native console view now so /ac starts warming (hidden until the
  // renderer measures #fv-term-pane and asks for it).
  createConsoleView();
  win.on('closed', () => {
    win = null; consoleView = null; consoleShown = false; browserView = null; browserShown = false;
    if (obsModule) obsModule.close();   // the trace browser never outlives the Station window
  });
}

// `hugpy-station --serve [claude|gpt|hugpy]`: run ONLY hugpy serve
// (resources/serve-app: the shared Serve console, attach-or-launch) on this
// runtime. None of the Station startup below runs: no backend, no Station
// window, no serve spawn beyond serve-app's own.
if (process.argv.some((a) => a === '--serve' || a.startsWith('--serve='))) {
  require(app.isPackaged
    ? path.join(process.resourcesPath, 'serve-app', 'main.js')
    : path.join(__dirname, 'resources', 'serve-app', 'main.js'));
  return;
}

// ONE Station per user (1.0.139). A second `hugpy-station` (desktop entry, shell,
// the launcher handing off because the port is held by this Station) focuses the
// running window and quits — it never starts a second backend, and nothing kills
// the running one (the pre-1.0.139 launcher pkill'd it). `--serve` returned above
// and keeps its own userData, so a serve window never contends for this lock.
if (!app.requestSingleInstanceLock()) {
  console.log('[station] hugpy Station is already running for this user - focusing it');
  app.quit();
  return;
}
app.on('second-instance', () => {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
});

app.whenReady().then(async () => {
  buildMenu();
  registerConsoleIpc();
  registerBrowserIpc();
  registerClipboardIpc();
  // Bring serve up first so 9124 is coming up while the backend boots; skip if
  // an external instance already owns it.
  await startServeIfNeeded();
  startBackend();
  try {
    await waitForBackend();
  } catch (err) {
    dialog.showErrorBox('Console backend unavailable', err.message);
  }
  // Log when serve answers, but don't hold the window on it (the /ac page retries).
  waitForServe().catch(() => console.error('[ac-serve] not answering yet; /ac will retry'));
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

function stopChildren() { stopServe(); stopBackend(); }
app.on('before-quit', stopChildren);
app.on('will-quit', stopChildren);
process.on('exit', stopChildren);
