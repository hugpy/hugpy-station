'use strict';
// Server half of the join — PURE (no Electron). Turns the response of
// GET <origin>/api/console/trace?trace_id=<id> (README.md, "Server contract") into hops 3, 4, 5:
//
//   hop 3  request reached Flask   — the request row (session, endpoint, status)
//   hop 4  handler executed         — schema 2: the row's `view` (the server read it
//                                     from url_rule -> view_functions, no guessing);
//                                     schema 1: first non-Flask call under
//                                     flask.app.dispatch_request
//   hop 5  stack to the output      — handler return → make_response →
//                                     finalize_request / process_response → status;
//                                     on a raise: schema 2 has real `exception`
//                                     span events and the request's traceback;
//                                     schema 1 is reconstructed from return lines
//
// Span depths are relative to the moment the tracer was installed (inside
// before_request); anchors are matched by function+file, not by absolute depth.

const LIB_RE = /\/(site|dist)-packages\/|\/lib\/python\d|^<frozen|^<string>/;
const FLASK_RE = /\/flask\/[^/]+\.py$/;
const WERKZEUG_RE = /\/werkzeug\//;
const OUTPUT_FNS = new Set(['make_response', 'finalize_request', 'process_response']);
const ERROR_FNS = new Set(['handle_user_exception', 'handle_exception', 'handle_http_exception', 'log_exception']);

function isApp(file) { return !!file && !LIB_RE.test(file); }

function shortFile(file) {
  if (!file) return '';
  const m = file.match(/(?:site|dist)-packages\/(.*)$/);
  if (m) return m[1];
  const parts = file.split('/');
  return parts.slice(-3).join('/');
}

function frame(s) {
  return { seq: s.seq, event: s.event, depth: s.depth, function: s.function, file: s.file, module: s.module || '',
    short: shortFile(s.file), line: s.line, at: s.at, duration_ms: s.duration_ms,
    error: s.error || null, app: isApp(s.file) };
}

// Index of the return span that closes the call at index i (same depth+function).
function matchReturn(spans, i) {
  const c = spans[i];
  for (let j = i + 1; j < spans.length; j++) {
    const s = spans[j];
    if (s.event === 'return' && s.depth === c.depth && s.function === c.function) return j;
    if (s.depth < c.depth && s.event === 'call') break;
  }
  return -1;
}

function analyze(doc, opts = {}) {
  const maxTree = opts.maxTree || 600;
  const out = { supported: false, schema: 1, reached: false, pending: false, request: null, handler: null, output: [],
    exceptions: [], handled: [], raised: null, callees: [], tree: [], level: null,
    counts: { spans: 0, calls: 0, app_calls: 0, exceptions: 0 }, notes: [] };
  if (!doc || typeof doc !== 'object' || !('spans' in doc)) {
    out.notes.push('origin did not answer /api/console/trace in the console_trace shape');
    return out;
  }
  out.supported = true;
  out.schema = doc.schema || 1;
  out.request = doc.request || null;
  out.pending = !!doc.pending || !!(out.request && out.request.finished_at == null);
  const r = out.request || {};
  out.level = r.spans_level || null;
  const nspans = Array.isArray(doc.spans) ? doc.spans.length : 0;
  // hop 3 holds if EITHER the envelope row or any span is keyed by our id: the
  // server only records spans for a request whose header it honored.
  out.reached = !!out.request || nspans > 0;
  if (!out.request && nspans) out.notes.push('spans exist but the request row is missing (server dropped a write)');
  else if (!out.request) out.notes.push('nothing stored for this trace id: header not honored, skipped (rate/route), or the write was dropped');
  else if (out.pending) out.notes.push('request row has no finished_at yet (still running, or its final write is queued)');
  if (r.spans_truncated) out.notes.push(`server truncated ${r.spans_truncated} span(s) at its cap`);
  const spans = Array.isArray(doc.spans) ? doc.spans.slice().sort((a, b) => a.seq - b.seq) : [];
  out.counts.spans = spans.length;
  for (const s of spans) {
    if (s.event === 'call') { out.counts.calls++; if (isApp(s.file)) out.counts.app_calls++; }
    if (s.event === 'exception') out.counts.exceptions++;
  }

  // hop 4 — the view
  const di = spans.findIndex((s) => s.event === 'call' && s.function === 'dispatch_request' && FLASK_RE.test(s.file || ''));
  let hi = -1;
  const view = r.view && typeof r.view === 'object' ? r.view : null;
  if (view) {
    hi = spans.findIndex((s) => s.event === 'call' && s.function === view.function && (!view.file || s.file === view.file));
  } else if (di >= 0) {
    const d = spans[di].depth;
    let fallback = -1;
    for (let j = di + 1; j < spans.length; j++) {
      const s = spans[j];
      if (s.depth <= d) break;
      if (s.event !== 'call' || s.depth !== d + 1 || FLASK_RE.test(s.file || '')) continue;
      if (isApp(s.file)) { hi = j; break; }                 // the view (app code)
      if (fallback < 0 && !WERKZEUG_RE.test(s.file || '')) fallback = j;  // view installed as a package
    }
    if (hi < 0) hi = fallback;
    if (hi < 0) out.notes.push('dispatch_request seen but no view call under it');
  } else if (spans.length) {
    out.notes.push('flask dispatch_request not in spans; handler is the first application frame (heuristic)');
    hi = spans.findIndex((s) => s.event === 'call' && isApp(s.file));
  }
  if (view) {
    out.handler = { function: view.function, module: view.module || '', file: view.file, short: shortFile(view.file),
      line: view.line, app: isApp(view.file), method: 'server view (url_rule)', endpoint: r.endpoint || null,
      returned: null, duration_ms: null, seq: hi >= 0 ? spans[hi].seq : null };
  } else if (hi >= 0) {
    out.handler = { ...frame(spans[hi]), method: di >= 0 ? 'dispatch_request' : 'first-app-frame', returned: null };
  }
  if (hi >= 0) {
    const h = spans[hi];
    const ri = matchReturn(spans, hi);
    out.handler.returned = ri >= 0;
    out.handler.duration_ms = ri >= 0 ? spans[ri].duration_ms : null;
    // direct callees of the handler, app frames first (the "what did it do")
    const end = ri >= 0 ? ri : spans.length;
    for (let j = hi + 1; j < end; j++) {
      const s = spans[j];
      if (s.event === 'call' && s.depth === h.depth + 1) {
        const rj = matchReturn(spans, j);
        out.callees.push({ ...frame(s), duration_ms: rj >= 0 ? spans[rj].duration_ms : null,
          raised: rj >= 0 && /^unwound/.test(spans[rj].error || '') });
      }
    }
    out.callees.sort((a, b) => (b.app - a.app) || ((b.duration_ms || 0) - (a.duration_ms || 0)));
    out.callees = out.callees.slice(0, 40);
    // hop 5 — the path from the handler's return to the HTTP response (inverted: handler first)
    if (ri >= 0) {
      const unwound = /^unwound/.test(spans[ri].error || '');
      out.output.push({ ...frame(spans[ri]), role: unwound ? 'handler unwound (raised)' : 'handler returned' });
      const seen = new Set();
      for (let j = ri + 1; j < spans.length; j++) {
        const s = spans[j];
        if (s.event === 'call' && (OUTPUT_FNS.has(s.function) || ERROR_FNS.has(s.function)) && FLASK_RE.test(s.file || '') && !seen.has(s.function)) {
          seen.add(s.function);
          const rj = matchReturn(spans, j);
          out.output.push({ ...frame(s), duration_ms: rj >= 0 ? spans[rj].duration_ms : null,
            role: ERROR_FNS.has(s.function) ? 'error handling' : 'response built' });
        }
      }
    }
  } else if (view) out.notes.push(`view ${view.function} not in the spans (spans level ${out.level || '?'})`);
  if (out.request) {
    out.output.push({ role: 'status', function: 'HTTP ' + (r.status == null ? '?' : r.status), duration_ms: r.duration_ms });
  }
  out.exceptions = spans.filter((s) => s.event === 'exception').slice(0, 50).map(frame);
  out.handled = spans.filter((s) => s.event === 'handled').slice(0, 20).map(frame);

  // the raise
  const exc = r.exception && typeof r.exception === 'object' ? r.exception : null;
  if (exc) {
    out.raised = { via: 'got_request_exception', method: 'server traceback (schema 2)', type: exc.type, message: exc.message,
      traceback: (exc.traceback || []).map((f) => ({ function: f.function, file: f.file, short: shortFile(f.file), line: f.line,
        code: f.code || '', app: f.app != null ? !!f.app : isApp(f.file) })) };
  } else if (out.exceptions.length && out.output[0] && /raised/.test(out.output[0].role)) {
    // unwound with exception spans but no unhandled-exception row (e.g. an HTTPException / abort)
    const first = out.exceptions[0];
    out.raised = { via: 'exception spans', method: 'span exception events (schema 2)', type: String(first.error || '').split(':')[0],
      message: String(first.error || '').split(': ').slice(1).join(': '),
      traceback: out.exceptions.filter((f) => f.app).reverse() };
  } else if (!out.counts.exceptions) {
    // schema 1: sys.setprofile never delivers 'exception' events, so a raise is
    // recovered from Flask's handle_*_exception call and the run of return events
    // just before it (the unwinding stack, each with the line it exited on).
    const ei = spans.findIndex((s, k) => k > di && di >= 0 && s.event === 'call' &&
      (s.function === 'handle_user_exception' || s.function === 'handle_exception') && FLASK_RE.test(s.file || ''));
    if (ei > 0) {
      const unwind = [];
      for (let j = ei - 1; j >= 0 && spans[j].event === 'return'; j--) unwind.push(frame(spans[j]));
      out.raised = { via: spans[ei].function, method: 'reconstructed from return lines (schema 1: setprofile has no exception events)',
        traceback: unwind.filter((f) => f.app || f.function === 'dispatch_request') };
      const first = out.output[0];
      if (first && first.role === 'handler returned') first.role = 'handler unwound (raised)';
    }
  }

  // A condensed tree for the viewer: app frames, Flask anchors, exceptions.
  for (const s of spans) {
    if (out.tree.length >= maxTree) { out.notes.push(`tree truncated at ${maxTree} rows`); break; }
    if (s.event === 'call' && (isApp(s.file) || s.function === 'dispatch_request')) out.tree.push(frame(s));
    else if (s.event === 'exception' || s.event === 'handled') out.tree.push(frame(s));
  }
  return out;
}

module.exports = { analyze, isApp, shortFile };
