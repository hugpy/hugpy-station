'use strict';
// Action ↔ request ↔ consumer linking by STACKS — PURE (no Electron), unit-tested.
//
// Phase 1 joined an action to "the request that started ≤2 s after it". That
// attributes a poller's tick to whatever was clicked last, and a slow handler's
// request to nothing. Here a request is linked to an action only when the ROOT
// of its initiator stack (the bottom frame of the last async parent — the frame
// the browser invoked) is an event listener that was registered, for that
// action's event type, on the action's target or one of its ancestors
// (DOMDebugger.getEventListeners, captured when the action happened). Among the
// actions whose listeners match, the latest one that precedes the request wins
// (an ORDER constraint, never a time window). A chain rooted in a timer, the
// parser or a script's top level is never attributed to a user action.
//
// Consumers (hop 6 inverse): a console/exception entry is a consumer of a
// request when its async stack passes through a function that also appears in
// the request's initiator chain (the code that awaited the response) and it was
// logged after the response arrived. DOM effects carry no stack in CDP without
// pausing the page; they are reported separately and labelled as timing-joined.

// event types whose listeners can carry each recorded action
const ACTION_EVENT_TYPES = Object.freeze({
  click: ['click', 'mousedown', 'mouseup', 'pointerdown', 'pointerup', 'auxclick', 'dblclick'],
  submit: ['submit'],
  change: ['change', 'input'],
  key: ['keydown', 'keyup', 'keypress'],
});

const TIMER_RE = /^(setTimeout|setInterval|requestAnimationFrame|requestIdleCallback)$/;

// The full initiator chain: sync frames, then each async parent's frames.
function chain(initiator) {
  const out = [];
  if (!initiator) return out;
  for (const f of initiator.stack || []) out.push({ ...f, segment: 0, boundary: null });
  (initiator.asyncParents || []).forEach((p, i) => {
    (p.frames || []).forEach((f, j) => out.push({ ...f, segment: i + 1, boundary: j === 0 ? p.description : null }));
  });
  return out;
}

// Where did this request's JS start? { kind, frame, via }
//   kind: listener-candidate | timer | script | parser | none
function origin(initiator) {
  if (!initiator) return { kind: 'none', frame: null, via: '' };
  if (initiator.type === 'parser' || initiator.type === 'preload' || initiator.type === 'preflight') {
    return { kind: 'parser', frame: null, via: initiator.type };
  }
  const parents = initiator.asyncParents || [];
  const frames = parents.length ? parents[parents.length - 1].frames || [] : initiator.stack || [];
  const root = initiator.root || (frames.length ? frames[frames.length - 1] : null);
  // the description of the LAST async boundary is the API that scheduled the
  // root segment's continuation (Promise.then / await / setTimeout / …)
  const via = parents.length ? parents[parents.length - 1].description || '' : '';
  if (!root) return { kind: 'none', frame: null, via };
  if (initiator.truncated) return { kind: 'truncated', frame: root, via };
  // A root at a script's top level (no function name, column 0-ish) or reached
  // through a timer boundary whose root is not a listener is not a user action.
  if (TIMER_RE.test(via)) return { kind: 'timer', frame: root, via };
  return { kind: 'listener-candidate', frame: root, via };
}

function cmpPos(a, b) {
  return (a.lineNumber - b.lineNumber) || ((a.columnNumber || 0) - (b.columnNumber || 0));
}

// Source extent of a listener function from its handler description (the
// function's source text). V8 locates a function at its parameter list, so the
// text before the first '(' (e.g. "function name") is stepped back over.
// Native/bound functions have no source: no extent (name-only matching).
function extentOf(description, lineNumber, columnNumber) {
  const src = typeof description === 'string' ? description : '';
  if (!src || /\[native code\]\s*\}\s*$/.test(src) || lineNumber == null) return {};
  const m = src.match(/^\s*(?:async\s+)?(?:function\b[^(]*|get\s+|set\s+|static\s+|[A-Za-z_$][\w$]*\s*)?\(/);
  const off = m ? m[0].length - 1 : 0;
  const startCol = Math.max(0, (columnNumber || 0) - off);
  const lines = src.split('\n');
  const endLine = lineNumber + lines.length - 1;
  const endColumn = lines.length === 1 ? startCol + src.length : lines[lines.length - 1].length;
  return { startColumn: startCol, endLine, endColumn };
}

// Does `frame` execute inside the listener function `l`?
// l: { scriptId, lineNumber, columnNumber, name, startColumn?, endLine?, endColumn? }
//   with an extent: the frame's position must lie inside it (+ names agree when both known)
//   without one (native/bound wrapper): an exact, non-empty function name match only
// Position alone is never enough: on a one-line minified bundle "after the
// listener's start" is half the file.
function listenerContains(l, frame) {
  if (!l || !frame) return false;
  const nameOnly = () => !!l.name && l.name === frame.functionName &&
    (!l.scriptId || String(l.scriptId) === '0' || String(l.scriptId) === String(frame.scriptId));
  if (l.endLine == null || !l.scriptId || String(l.scriptId) === '0') return nameOnly();
  if (String(l.scriptId) !== String(frame.scriptId)) return false;
  const start = { lineNumber: l.lineNumber, columnNumber: l.startColumn != null ? l.startColumn : l.columnNumber };
  const end = { lineNumber: l.endLine, columnNumber: l.endColumn };
  if (cmpPos(frame, start) < 0 || cmpPos(frame, end) > 0) return false;
  if (l.name && frame.functionName && l.name !== frame.functionName) return false;
  return true;
}

// Pick, among listeners in the same script that could contain `frame`, the one
// starting closest before it (innermost start), so nested functions resolve right.
function bestListener(listeners, frame) {
  let best = null;
  for (const l of listeners || []) {
    if (!listenerContains(l, frame)) continue;
    if (!best || cmpPos(l, best) > 0 || (l.name && !best.name)) best = l;
  }
  return best;
}

// Link one request to one of `actions` (each with .listeners from DOMDebugger).
// Returns { action, listener, join, sync, origin } or { action: null, reason, origin }.
//
// Two tiers, never mixed up:
//   'listener-stack'          EXACT: the chain's root frame runs inside one of the
//                             action's listeners
//   'scheduled-after-action'  INFERRED: the root is a non-timer task callback that
//                             no listener contains — e.g. React's scheduler
//                             (MessageChannel), which V8 records no async edge for,
//                             so click → setState → useEffect → fetch has no stack
//                             link. Only taken when the action is the immediately
//                             preceding one, it hit page listeners, and the request
//                             left within `scheduledWindowMs` (default 1000).
function linkRequest(trace, actions, opts = {}) {
  const o = origin(trace.initiator);
  if (o.kind !== 'listener-candidate') {
    return { action: null, origin: o, reason: o.kind === 'timer' ? `timer-driven (${o.via})`
      : o.kind === 'parser' ? 'parser/navigation initiated' : o.kind === 'truncated' ? 'initiator chain truncated'
        : 'no JS initiator stack' };
  }
  const sentAt = (trace.wallTime || 0) * 1000;
  let pending = false;
  for (let i = actions.length - 1; i >= 0; i--) {
    const a = actions[i];
    if (sentAt && a.ts > sentAt + 50) continue;        // after the request was sent
    // a newer action whose listeners are still being read could be the one:
    // never fall through to an older action past it (that would be a guess)
    if (!a.listeners) { pending = true; break; }
    const types = ACTION_EVENT_TYPES[a.action] || [a.action];
    const cands = a.listeners.filter((l) => types.includes(l.type));
    const l = bestListener(cands, o.frame);
    if (l) {
      return { action: a, listener: l, origin: o, join: 'listener-stack',
        sync: !(trace.initiator.asyncParents || []).length, deltaMs: sentAt ? Math.max(0, Math.round(sentAt - a.ts)) : null };
    }
  }
  const rootName = o.frame.functionName || '(anonymous)';
  if (!pending && opts.inferScheduled !== false) {
    const win = opts.scheduledWindowMs || 1000;
    const prev = [...actions].reverse().find((a) => !sentAt || a.ts <= sentAt + 50);
    if (prev && prev.listeners && prev.listeners.length && sentAt && sentAt - prev.ts <= win) {
      return { action: prev, listener: null, origin: o, join: 'scheduled-after-action', inferred: true, sync: false,
        deltaMs: Math.max(0, Math.round(sentAt - prev.ts)),
        reason: `inferred: root ${rootName} is a scheduled task (no listener contains it); the request left ${Math.round(sentAt - prev.ts)} ms after this action with no action between` };
    }
  }
  return { action: null, origin: o, pending,
    reason: pending ? 'listener lookup pending' : `root ${rootName} is not a listener of any recorded action` };
}

// Functions (scriptId + non-empty name) the request's initiator chain passes through.
function chainFunctions(initiator) {
  const s = new Set();
  for (const f of chain(initiator)) if (f.functionName) s.add(`${f.scriptId}|${f.functionName}`);
  return s;
}

// Console/exception entries that consumed this request's response (stack join).
// entry: { ts (ms epoch), stack: [frames], asyncParents: [{frames}] }
function consumers(trace, entries, opts = {}) {
  const max = opts.max || 3;
  const fns = chainFunctions(trace.initiator);
  const doneAt = trace.response && trace.response.finishedWall ? trace.response.finishedWall * 1000 : null;
  if (!fns.size || !doneAt) return [];
  const out = [];
  for (const e of entries || []) {
    if (e.tsWall == null || e.tsWall < doneAt - 5) continue;
    if (opts.before && e.tsWall > opts.before) continue;   // a later action's code, not this response's
    const frames = [...(e.stack || []), ...((e.asyncParents || []).flatMap((p) => p.frames || []))];
    const hit = frames.find((f) => f.functionName && fns.has(`${f.scriptId}|${f.functionName}`));
    if (hit) {
      out.push({ level: e.level, text: String(e.text || '').slice(0, 300), via: hit.functionName,
        at: e.stack && e.stack[0] ? e.stack[0] : hit, afterMs: Math.round(e.tsWall - doneAt), join: 'stack' });
      if (out.length >= max) break;
    }
  }
  return out;
}

// Name of the function starting at (line, col) in `source` (listener location).
function functionNameAt(source, lineNumber, columnNumber) {
  if (typeof source !== 'string') return '';
  const lines = source.split('\n');
  const line = lines[lineNumber] || '';
  const before = line.slice(Math.max(0, columnNumber - 120), columnNumber + 1);
  const after = line.slice(columnNumber, columnNumber + 160);
  let m = after.match(/^(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(/);
  if (m) return m[1];
  m = after.match(/^(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{/);      // method shorthand
  if (m && !['function', 'if', 'for', 'while', 'switch', 'catch'].includes(m[1])) return m[1];
  // `name = (…) =>` / `name: function` / `const name = async (e) =>`
  m = before.match(/([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?(?:function\b[^(]*)?\(?[^()]*$/);
  if (m) return m[1];
  return '';
}

module.exports = { ACTION_EVENT_TYPES, chain, origin, extentOf, listenerContains, bestListener, linkRequest,
  chainFunctions, consumers, functionNameAt };
