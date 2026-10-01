'use strict';
// Viewer bridge: a frozen, minimal API over contextBridge. The viewer never
// sees ipcRenderer, and the inspected page (a separate WebContentsView with no
// preload) never sees any of this.
const { contextBridge, ipcRenderer } = require('electron');

const sub = (ch) => (fn) => ipcRenderer.on(ch, (_e, d) => fn(d));

contextBridge.exposeInMainWorld('hugpyObs', {
  state: () => ipcRenderer.invoke('obs:state'),
  navigate: (url) => ipcRenderer.invoke('obs:navigate', String(url || '')),
  reload: () => ipcRenderer.invoke('obs:reload'),
  back: () => ipcRenderer.invoke('obs:back'),
  devtools: () => ipcRenderer.invoke('obs:devtools'),
  capture: (on) => ipcRenderer.invoke('obs:capture', !!on),
  setFilter: (f) => ipcRenderer.invoke('obs:filter', f),
  setInference: (lv) => ipcRenderer.invoke('obs:inference', String(lv || 'off')),
  setSpanBases: (m) => ipcRenderer.invoke('obs:spanBases', m),
  setServer: (s) => ipcRenderer.invoke('obs:server', { spans: String((s && s.spans) || ''), modules: String((s && s.modules) || '') }),
  setExplain: (c) => ipcRenderer.invoke('obs:explainCfg', { url: String((c && c.url) || ''), model: String((c && c.model) || '') }),
  arm: (n) => ipcRenderer.invoke('obs:arm', +n || 1),
  disarm: () => ipcRenderer.invoke('obs:disarm'),
  save: (key) => ipcRenderer.invoke('obs:save', String(key)),
  saved: () => ipcRenderer.invoke('obs:saved'),
  loadSaved: (file) => ipcRenderer.invoke('obs:loadSaved', String(file || '')),
  explain: (key) => ipcRenderer.invoke('obs:explain', String(key)),
  list: () => ipcRenderer.invoke('obs:list'),
  get: (key) => ipcRenderer.invoke('obs:get', String(key)),
  consoleEntries: () => ipcRenderer.invoke('obs:console'),
  clear: () => ipcRenderer.invoke('obs:clear'),
  exportJson: () => ipcRenderer.invoke('obs:export'),
  layout: (r) => ipcRenderer.send('obs:layout', { x: +r.x || 0, y: +r.y || 0, width: +r.width || 0, height: +r.height || 0 }),
  onRows: sub('obs:rows'),
  onConsole: sub('obs:console-entry'),
  onAction: sub('obs:action'),
  onCaptureState: sub('obs:capture-state'),
  onUrl: sub('obs:url'),
  onArmed: sub('obs:armed'),
  onArmedDone: sub('obs:armed-done'),
});
