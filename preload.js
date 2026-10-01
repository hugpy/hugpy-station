'use strict';
// hugpy Station preload — bridges the SPA renderer to the native
// abstract-claude serve console view owned by main.js. contextIsolation is on,
// so we hand the page a tiny, frozen API over contextBridge instead of exposing
// ipcRenderer. The frontend (static/fleetview-term.js) measures #fv-term-pane
// and calls these to position/show/hide the overlaid /ac console view. Every
// caller in the page guards with `if (window.stationConsole)` so the same UI
// still works in a plain browser at :8899 (where this bridge is absent).

const { contextBridge, ipcRenderer } = require('electron');

function rect(r) {
  r = r || {};
  return { x: +r.x || 0, y: +r.y || 0, width: +r.width || 0, height: +r.height || 0 };
}

contextBridge.exposeInMainWorld('stationConsole', {
  show: (r) => ipcRenderer.send('console:show', rect(r)),
  hide: () => ipcRenderer.send('console:hide'),
  setBounds: (r) => ipcRenderer.send('console:setBounds', rect(r)),
  reload: () => ipcRenderer.send('console:reload'),
});

// 1.0.143: the terminal's copy/paste fallback. Electron's clipboard module is
// not available to a sandboxed preload, so main.js owns it (ipcMain.handle);
// text only, and only the system clipboard — nothing else crosses the bridge.
contextBridge.exposeInMainWorld('stationClipboard', {
  readText: () => ipcRenderer.invoke('clipboard:readText'),
  writeText: (t) => ipcRenderer.invoke('clipboard:writeText', String(t == null ? '' : t)),
});

contextBridge.exposeInMainWorld('stationBrowser', {
  openCapture: () => ipcRenderer.send('browser:open-capture'),
  show: (r) => ipcRenderer.send('browser:show', rect(r)),
  hide: () => ipcRenderer.send('browser:hide'),
  setBounds: (r) => ipcRenderer.send('browser:setBounds', rect(r)),
  navigate: (url) => ipcRenderer.send('browser:navigate', String(url || '')),
  reload: () => ipcRenderer.send('browser:reload'),
  onEvent: (fn) => ipcRenderer.on('browser:event', (_event, data) => fn(data)),
});
