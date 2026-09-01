'use strict';
// hugpy Station desktop shell.
// On launch: spawn the bundled console backend (server.py) on a private
// loopback port, wait for it to answer, then load it in a native window.
// The backend serves both the UI and the /api it needs, and manages the
// LOCAL machine's LXD. On quit we kill the backend child.

const { app, BrowserWindow, Menu, shell, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');

const HOST = '127.0.0.1';
const PORT = process.env.CONSOLE_PORT || '8899'; // private port; avoids clashing with a system console-standalone.service on 8800
const BASE = `http://${HOST}:${PORT}`;

let backend = null;
let backendReady = false;
let win = null;

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
    env: { ...process.env, HOST, PORT },
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
    webPreferences: { contextIsolation: true },
  });
  // external links open in the system browser, not inside the shell
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
  win.loadURL(BASE);
  win.on('closed', () => { win = null; });
}

app.whenReady().then(async () => {
  buildMenu();
  startBackend();
  try {
    await waitForBackend();
  } catch (err) {
    dialog.showErrorBox('Console backend unavailable', err.message);
  }
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', stopBackend);
app.on('will-quit', stopBackend);
process.on('exit', stopBackend);
