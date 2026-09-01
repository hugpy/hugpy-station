/* flow.js — bidirectional process-FLOW conveyance for the fleet console (t84).

   A flow is a JSON document (schema "flow.v1" — see docs/FLOW-SCHEMA.md, the
   contract this file is built against): a program's control flow as a standard
   flowchart. Nodes carry a role (start | end | process | decision | io |
   subprocess | note), directed edges carry a branch-condition label, and the
   document carries a lifecycle status (derived → proposed → agreed), a rev
   counter, and derived_from provenance. Like wireframe.v1 it is a
   file-as-interface: the keeper derives one from real source, the operator
   revises it in the console's ⋔ flow drawer, either side edits until the
   document is an unambiguous agreement on what the flow SHOULD be.

   It travels the same three roads as a wireframe:
     ~/flow.json in the VM   host route /api/vm/<vm>/flow ("→ VM" / "← VM")
     a fenced ```flow block  ⧉ copies one; the 📋 import loads one
     localStorage            per-VM autosave in the drawer

   Contract points implemented here (FLOW-SCHEMA.md):
     - fail-closed validation: a reader DROPS the whole document unless schema
       matches, every node has string id/role/label + finite numeric x/y/w/h,
       ids are unique, and every edge endpoint resolves to a node id.
     - unknown roles render as `process` (forward-compat) but the raw role
       string is preserved; unknown top-level keys are preserved on rewrite
       (either side may be older than the document).
     - any edit to nodes/edges while status is "agreed" drops the document
       back to "proposed" automatically.
     - rev bumps on serialize-for-save (→ VM / ⤓ download), not on mere edits.
     - cycles are expected (loops are flow) — the renderer routes upward
       back-edges around the nodes instead of assuming a DAG.
     - decision is visually distinct (a diamond); note is dashed, no fill.

   No dependencies; self-injected chrome, same module shape as wireframe.js. */
(function () {
  "use strict";

  const SCHEMA = "flow.v1";
  const CANVAS_W = 1280, CANVAS_H = 800, GRID = 20;
  const SVGNS = "http://www.w3.org/2000/svg";
  const STATUSES = ["derived", "proposed", "agreed"];

  /* role palette — kept in the wireframe.js visual family (same canvas paper,
     same border weight) so the two drawers read as siblings. */
  const ROLES = {
    start:      { name: "Start",      fill: "#DFF0D8", border: "#3C763D" },
    end:        { name: "End",        fill: "#F2DEDE", border: "#A94442" },
    process:    { name: "Process",    fill: "#EAF2FB", border: "#4A90D9" },
    decision:   { name: "Decision",   fill: "#FFF3D6", border: "#C9862B" },
    io:         { name: "I/O",        fill: "#E8E8F7", border: "#6A5ACD" },
    subprocess: { name: "Subprocess", fill: "#E2F0EF", border: "#2E8B84" },
    note:       { name: "Note",       fill: "none",    border: "#8A94A6" },
  };
  const ROLE_ORDER = ["start", "end", "process", "decision", "io", "subprocess", "note"];
  // default geometry for a freshly placed node, per role
  const ROLE_SIZE = { start: [160, 60], end: [160, 60], process: [180, 60], decision: [180, 80],
                      io: [180, 60], subprocess: [180, 60], note: [220, 60] };

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const snap = (v) => Math.round(v / GRID) * GRID;
  const styleFor = (role) => ROLES[role] || ROLES.process;   // unknown role → process (forward-compat)

  function blank() {
    // a hand-started document is an operator revision, not a machine read:
    // it begins life "proposed" (the "derived" status is the keeper's claim).
    return { schema: SCHEMA, title: "", status: "proposed", rev: 1,
             canvas: { w: CANVAS_W, h: CANVAS_H, grid: GRID }, nodes: [], edges: [] };
  }

  /* built-in demo for the empty state — a small retry loop that shows every
     visual family (start/process/decision/end/note) plus a real cycle. */
  function demo() {
    const d = blank();
    d.title = "demo: retry loop";
    d.nodes = [
      { id: "d1", role: "start",    label: "request arrives",  x: 100, y: 60,  w: 180, h: 60 },
      { id: "d2", role: "process",  label: "attempt the call", x: 100, y: 200, w: 180, h: 60 },
      { id: "d3", role: "decision", label: "succeeded?",       x: 100, y: 340, w: 180, h: 80 },
      { id: "d4", role: "end",      label: "done",             x: 460, y: 340, w: 160, h: 60 },
      { id: "d5", role: "note",     label: "edit me — ⧉ copies this as a ```flow block for the keeper", x: 460, y: 60, w: 260, h: 80 },
    ];
    d.edges = [
      { from: "d1", to: "d2", label: "" },
      { from: "d2", to: "d3", label: "" },
      { from: "d3", to: "d4", label: "yes" },
      { from: "d3", to: "d2", label: "no: retry" },
    ];
    return d;
  }

  /* ---------------- validation (fail-closed) ----------------
     Returns a normalized copy of the document, or null — never a partial
     rescue. Per the schema: unknown top-level keys ride through untouched
     (the spread below keeps them); node/edge extra keys are preserved too. */
  function normalize(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    if (raw.schema !== SCHEMA) return null;
    if (!Array.isArray(raw.nodes)) return null;
    const nodes = [], ids = new Set();
    for (const n of raw.nodes) {
      if (!n || typeof n !== "object") return null;
      if (typeof n.id !== "string" || !n.id) return null;
      if (typeof n.role !== "string" || typeof n.label !== "string") return null;
      const x = +n.x, y = +n.y, w = +n.w, h = +n.h;
      if (![x, y, w, h].every(isFinite)) return null;
      if (ids.has(n.id)) return null;                      // duplicate id → drop the document
      ids.add(n.id);
      nodes.push({ ...n, x, y, w: Math.max(GRID, w), h: Math.max(GRID, h) });
    }
    const rawEdges = raw.edges == null ? [] : raw.edges;
    if (!Array.isArray(rawEdges)) return null;
    const edges = [];
    for (const e of rawEdges) {
      if (!e || typeof e !== "object") return null;
      if (typeof e.from !== "string" || typeof e.to !== "string") return null;
      if (!ids.has(e.from) || !ids.has(e.to)) return null; // dangling endpoint → drop the document
      edges.push({ ...e, label: typeof e.label === "string" ? e.label : "" });
    }
    const doc = { ...raw };                                // unknown top-level keys preserved
    doc.schema = SCHEMA;
    doc.title = typeof raw.title === "string" ? raw.title : "";
    doc.status = STATUSES.includes(raw.status) ? raw.status : "proposed";
    doc.rev = Number.isInteger(+raw.rev) && +raw.rev >= 1 ? +raw.rev : 1;
    doc.canvas = { w: CANVAS_W, h: CANVAS_H, grid: GRID };
    doc.nodes = nodes;
    doc.edges = edges;
    return doc;
  }

  /* Tolerant JSON extraction (same idiom as wireframe.js): fenced blocks,
     surrounding prose, trailing commas. Validation stays strict — this only
     finds the JSON, normalize() decides whether it is a flow. */
  function looseJSON(text) {
    let t = String(text || "");
    const fence = t.match(/```[a-zA-Z]*\s*\n([\s\S]*?)```/);
    if (fence) t = fence[1];
    const a = t.indexOf("{");
    if (a < 0) return null;
    const b = t.lastIndexOf("}");
    if (b <= a) return null;
    t = t.slice(a, b + 1);
    try { return JSON.parse(t); } catch (e) {}
    try { return JSON.parse(t.replace(/,\s*([}\]])/g, "$1")); } catch (e) {}
    return null;
  }

  /* parse: anything flow-ish (a ```flow block, bare JSON, or an object) →
     a normalized flow.v1 document, or null (fail-closed). */
  function parse(text) {
    const data = typeof text === "string" ? looseJSON(text) : text;
    return data ? normalize(data) : null;
  }

  /* Chat/exchange form: the FULL document (compact) — status, rev,
     derived_from, and any unknown top-level keys included, so a ⧉ block that
     comes back through 📋 (or the keeper's rewrite) loses nothing. */
  function toChat(doc) { return JSON.stringify(doc); }

  /* ---------------- styles (self-injected, --fl-* themable) ---------------- */
  const FL_CSS = `
.fl-ed, .fl-modal-bg {
  --fl-bg:#0d1117; --fl-panel:#151b24; --fl-panel2:#1b2330; --fl-line:#2a3140;
  --fl-fg:#c9d3e4; --fl-muted:#7d8799; --fl-accent:#3d95f5; --fl-bad:#f06060;
  font: 13px/1.4 ui-monospace, Menlo, Consolas, monospace; color: var(--fl-fg);
}
.fl-ed { display:flex; flex-direction:column; min-height:0; position:relative; flex:1; background:var(--fl-panel); }
.fl-tools { display:flex; align-items:center; gap:4px; flex-wrap:wrap; flex:none;
  padding:6px 8px; background:var(--fl-panel2); border-bottom:1px solid var(--fl-line); }
.fl-b { background:var(--fl-panel); color:var(--fl-fg); border:1px solid var(--fl-line);
  border-radius:5px; padding:3px 8px; cursor:pointer; font-size:12px; }
.fl-b:hover { border-color:var(--fl-accent); }
.fl-b.on { border-color:var(--fl-accent); color:var(--fl-accent); }
.fl-b.fl-action { border-color:var(--fl-accent); color:var(--fl-accent); font-weight:600; }
.fl-role, .fl-prop-role { background:var(--fl-bg); color:var(--fl-fg); border:1px solid var(--fl-line);
  border-radius:5px; padding:3px 6px; font:12px ui-monospace,Menlo,Consolas,monospace; }
.fl-sep { width:1px; height:18px; background:var(--fl-line); margin:0 3px; }
.fl-spacer { flex:1; }
.fl-chip { border:1px solid var(--fl-line); border-radius:10px; padding:1px 8px; font-size:11px;
  color:var(--fl-muted); cursor:pointer; background:var(--fl-bg); }
.fl-chip.on { border-color:var(--fl-accent); color:var(--fl-accent); background:var(--fl-panel2); }
.fl-rev { color:var(--fl-muted); font-size:11px; padding:0 4px; }
.fl-meta { display:flex; align-items:center; gap:8px; flex:none; padding:5px 10px 0; }
.fl-title { flex:1; background:var(--fl-bg); color:var(--fl-fg); border:1px solid var(--fl-line);
  border-radius:5px; padding:3px 8px; font:12px ui-monospace,Menlo,Consolas,monospace; }
.fl-title:focus { outline:none; border-color:var(--fl-accent); }
.fl-derived { color:var(--fl-muted); font-size:11px; white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; max-width:45%; }
.fl-canvas-wrap { position:relative; flex:none; padding:8px; outline:none; }
.fl-canvas-wrap:focus { box-shadow: inset 0 0 0 1px var(--fl-accent); }
/* full screen — lift the whole editor out of the drawer to cover the viewport;
   the canvas wrap grows + scrolls so a large flow is fully pannable. */
.fl-ed.fl-full { position:fixed; inset:0; z-index:9999; width:auto; max-width:none; }
.fl-ed.fl-full .fl-canvas-wrap { flex:1 1 auto; overflow:auto; }
.fl-svg { display:block; width:100%; height:auto; aspect-ratio:1280/800; border-radius:4px; touch-action:none; }
.fl-label-edit { position:absolute; z-index:5; background:#fff; color:#222; border:1px solid #1E6FD9;
  border-radius:3px; padding:2px 6px; font:12px ui-monospace,Menlo,Consolas,monospace; }
.fl-props { display:flex; align-items:center; gap:6px; flex:none; padding:5px 10px; border-top:1px solid var(--fl-line); }
.fl-prop-id { min-width:24px; color:var(--fl-accent); }
.fl-prop-label { flex:1; background:var(--fl-bg); color:var(--fl-fg); border:1px solid var(--fl-line);
  border-radius:5px; padding:3px 8px; font:12px ui-monospace,Menlo,Consolas,monospace; }
.fl-prop-label:focus { outline:none; border-color:var(--fl-accent); }
.fl-prop-geom { white-space:nowrap; color:var(--fl-muted); font-size:12px; }
.fl-status { flex:none; padding:4px 10px 8px; min-height:18px; color:var(--fl-muted); font-size:12px; }
.fl-status.err { color:var(--fl-bad); }
.fl-confirm { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:8px 10px;
  border:1px solid var(--fl-bad); border-radius:6px; background:#1a1214; }
.fl-confirm span { flex:1 1 100%; color:var(--fl-fg); }
.fl-confirm .fl-confirm-ok { border-color:var(--fl-bad); color:var(--fl-bad); }
.fl-modal-bg { position:absolute; inset:0; z-index:10; background:rgba(4,8,16,.72);
  display:flex; align-items:flex-start; justify-content:center; padding-top:40px; }
.fl-modal { background:var(--fl-panel); border:1px solid var(--fl-line); border-radius:8px;
  padding:14px; width:min(480px,92%); }
.fl-modal h4 { margin:0 0 6px; color:var(--fl-accent); }
.fl-modal p { margin:0 0 10px; color:var(--fl-muted); font-size:12px; }
.fl-modal textarea { width:100%; background:var(--fl-bg); color:var(--fl-fg);
  border:1px solid var(--fl-line); border-radius:5px; padding:6px 8px; resize:vertical;
  font:12px ui-monospace,Menlo,Consolas,monospace; }
.fl-modal-btns { display:flex; justify-content:flex-end; gap:8px; margin-top:12px; }
.fl-modal-btns button { background:var(--fl-panel2); color:var(--fl-fg); border:1px solid var(--fl-line);
  border-radius:5px; padding:5px 14px; cursor:pointer; }
.fl-modal-btns button:last-child { background:var(--fl-accent); color:#04101f;
  border-color:var(--fl-accent); font-weight:600; }`;

  function ensureStyles() {
    if (document.getElementById("fl-style")) return;
    const s = document.createElement("style");
    s.id = "fl-style";
    s.textContent = FL_CSS;
    document.head.appendChild(s);
  }

  /* ---------------- SVG rendering ---------------- */
  function el(tag, attrs, parent) {
    const n = document.createElementNS(SVGNS, tag);
    for (const k in attrs || {}) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  function baseSVG() {
    const svg = el("svg", { viewBox: `0 0 ${CANVAS_W} ${CANVAS_H}`, preserveAspectRatio: "xMidYMid meet" });
    const defs = el("defs", {}, svg);
    const pat = el("pattern", { id: "flgrid", width: GRID, height: GRID, patternUnits: "userSpaceOnUse" }, defs);
    el("path", { d: `M ${GRID} 0 L 0 0 0 ${GRID}`, fill: "none", stroke: "#E3E6EC", "stroke-width": 1 }, pat);
    for (const [id, color] of [["flarrow", "#5A6578"], ["flarrowsel", "#1E6FD9"]]) {
      const m = el("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5,
                               markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, defs);
      el("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: color }, m);
    }
    el("rect", { x: 0, y: 0, width: CANVAS_W, height: CANVAS_H, fill: "#FBFBFD", stroke: "#9AA3B2", "stroke-width": 2 }, svg);
    el("rect", { x: 0, y: 0, width: CANVAS_W, height: CANVAS_H, fill: "url(#flgrid)" }, svg);
    return svg;
  }

  // greedy word-wrap for node labels: maxChars per line, maxLines total,
  // "…" when the label doesn't fit. Never returns an empty array.
  function wrapLabel(label, maxChars, maxLines) {
    maxChars = Math.max(4, maxChars);
    maxLines = Math.max(1, maxLines);
    const words = String(label).split(/\s+/).filter(Boolean);
    const out = [];
    let cur = "", truncated = false;
    for (let i = 0; i < words.length; i++) {
      let w = words[i];
      if (w.length > maxChars) w = w.slice(0, maxChars - 1) + "…";
      const cand = cur ? cur + " " + w : w;
      if (cand.length <= maxChars) { cur = cand; continue; }
      if (out.length === maxLines - 1) { truncated = true; break; }
      out.push(cur);
      cur = w;
    }
    if (cur) out.push(cur);
    if (truncated) out[out.length - 1] = out[out.length - 1].slice(0, maxChars - 1) + "…";
    return out.length ? out : [""];
  }

  /* Draw one node into group g. Shapes per role: start/end = stadium,
     decision = diamond (visually distinct — it is where ambiguity lives),
     io = parallelogram, subprocess = rect with double side bars,
     note = dashed border + no fill, anything else (incl. unknown) = process rect. */
  function paintNode(g, n) {
    const role = ROLES[n.role] ? n.role : "process";
    const st = ROLES[role];
    const cx = n.x + n.w / 2, cy = n.y + n.h / 2;
    const base = { fill: st.fill, stroke: st.border, "stroke-width": 2 };
    if (role === "decision") {
      el("polygon", { points: `${cx},${n.y} ${n.x + n.w},${cy} ${cx},${n.y + n.h} ${n.x},${cy}`, ...base }, g);
    } else if (role === "io") {
      const k = Math.min(16, n.w / 5);
      el("polygon", { points: `${n.x + k},${n.y} ${n.x + n.w},${n.y} ${n.x + n.w - k},${n.y + n.h} ${n.x},${n.y + n.h}`, ...base }, g);
    } else if (role === "note") {
      el("rect", { x: n.x, y: n.y, width: n.w, height: n.h, rx: 4,
                   fill: "#FBFBFD", "fill-opacity": 0.01,        // near-invisible fill, but still a pointer target
                   stroke: st.border, "stroke-width": 1.5, "stroke-dasharray": "5 4" }, g);
    } else {
      const rx = role === "start" || role === "end" ? n.h / 2 : 3;
      el("rect", { x: n.x, y: n.y, width: n.w, height: n.h, rx, ...base }, g);
      if (role === "subprocess") {
        for (const bx of [n.x + 8, n.x + n.w - 8])
          el("line", { x1: bx, y1: n.y, x2: bx, y2: n.y + n.h, stroke: st.border, "stroke-width": 1.5 }, g);
      }
    }
    // label, word-wrapped to the shape's interior (decision/io lose side room)
    const inset = role === "decision" ? 0.30 : role === "io" ? 0.12 : 0.06;
    const maxChars = Math.max(6, Math.floor((n.w * (1 - 2 * inset)) / 6.4));
    const maxLines = Math.max(1, Math.floor((n.h - 12) / 14));
    const lines = wrapLabel(n.label, maxChars, maxLines);
    const y0 = cy - (lines.length - 1) * 7;
    for (let i = 0; i < lines.length; i++) {
      const t = el("text", { x: cx, y: y0 + i * 14, fill: role === "note" ? "#5A6578" : "#233",
                             "font-size": 12, "font-style": role === "note" ? "italic" : "normal",
                             "text-anchor": "middle", "dominant-baseline": "middle" }, g);
      t.textContent = lines[i];
    }
    // the id anchors discussion ("as of flow rev 4, f7…") — keep it visible
    const idt = el("text", { x: n.x + (role === "decision" ? n.w / 2 : 4), y: n.y - 5,
                             fill: "#9AA3B2", "font-size": 10,
                             "text-anchor": role === "decision" ? "middle" : "start" }, g);
    idt.textContent = n.id;
    const title = el("title", {}, g);
    title.textContent = `${n.id} · ${styleFor(n.role).name}${ROLES[n.role] ? "" : ` (unknown role "${n.role}")`} · ${n.label}`;
  }

  /* Edge routing: pick facing sides by the dominant axis, straight when the
     anchors align, one clean elbow otherwise. Upward vertical edges (the
     back-edge of a loop) route AROUND the left of both nodes — cycles are
     expected, so they must read as loops, not as lines stabbing through the
     chart. Self-edges get a small right-hand loop. Returns the point list. */
  function edgePoints(a, b) {
    if (a === b) {
      const x = a.x + a.w, y = a.y + a.h / 2;
      return [[x, y - 10], [x + 36, y - 10], [x + 36, y + 10], [x, y + 10]];
    }
    const acx = a.x + a.w / 2, acy = a.y + a.h / 2;
    const bcx = b.x + b.w / 2, bcy = b.y + b.h / 2;
    const dx = bcx - acx, dy = bcy - acy;
    if (Math.abs(dx) > Math.abs(dy)) {                     // horizontal-dominant
      const sx = dx > 0 ? a.x + a.w : a.x, tx = dx > 0 ? b.x : b.x + b.w;
      if (Math.abs(acy - bcy) < 6) return [[sx, acy], [tx, bcy]];
      const mx = (sx + tx) / 2;
      return [[sx, acy], [mx, acy], [mx, bcy], [tx, bcy]];
    }
    if (dy < 0) {                                          // upward back-edge: loop around the left
      const ox = Math.min(a.x, b.x) - 28;
      return [[a.x, acy], [ox, acy], [ox, bcy], [b.x, bcy]];
    }
    const sy = a.y + a.h, ty = b.y;                        // downward
    if (Math.abs(acx - bcx) < 6) return [[acx, sy], [bcx, ty]];
    const my = (sy + ty) / 2;
    return [[acx, sy], [acx, my], [bcx, my], [bcx, ty]];
  }
  const ptsToPath = (pts) => pts.map((p, i) => (i ? "L" : "M") + p[0] + " " + p[1]).join(" ");
  function edgeMid(pts) {                                  // label anchor: middle segment's midpoint
    const i = Math.floor((pts.length - 1) / 2);
    const p = pts[i], q = pts[i + 1] || p;
    return [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2];
  }

  /* Draw one edge (visible stroke + fat invisible hit path) into group g. */
  function paintEdge(g, e, i, byId, selected) {
    const a = byId(e.from), b = byId(e.to);
    if (!a || !b) return;
    const pts = edgePoints(a, b);
    const d = ptsToPath(pts);
    el("path", { d, fill: "none", stroke: selected ? "#1E6FD9" : "#5A6578",
                 "stroke-width": selected ? 2.6 : 1.6, class: "fl-edge",
                 "data-from": e.from, "data-to": e.to,
                 "marker-end": selected ? "url(#flarrowsel)" : "url(#flarrow)" }, g);
    const hit = el("path", { d, fill: "none", stroke: "rgba(0,0,0,0)", "stroke-width": 14,
                             class: "fl-ehit", "data-ei": i }, g);
    hit.style.cursor = "pointer";
    if (e.label) {
      const [mx, my] = edgeMid(pts);
      const t = el("text", { x: mx, y: my - 5, fill: selected ? "#1E6FD9" : "#444", "font-size": 12,
                             class: "fl-elabel", "data-ei": i, "text-anchor": "middle",
                             "paint-order": "stroke", stroke: "#FBFBFD", "stroke-width": 4 }, g);
      t.textContent = e.label;
    }
    const title = el("title", {}, hit);
    title.textContent = `${e.from} → ${e.to}${e.label ? " · " + e.label : ""}`;
  }

  /* ---------------- the editor ---------------- */
  function mountEditor(host, opts) {
    opts = opts || {};
    ensureStyles();
    host.classList.add("fl-ed");
    host.innerHTML = `
      <div class="fl-tools">
        <button class="fl-b fl-addnode" title="Add node: pick a role, then click the canvas (Esc to leave)">＋ node</button>
        <select class="fl-role" title="Role for new nodes"></select>
        <button class="fl-b fl-addedge" title="Add edge: click the source node, then the target (Esc cancels)">⇢ edge</button>
        <button class="fl-b fl-del" title="Delete selection (Del)">✕</button>
        <button class="fl-b fl-undo" title="Undo (Ctrl+Z)">↶</button>
        <button class="fl-b fl-redo" title="Redo (Ctrl+Shift+Z)">↷</button>
        <span class="fl-sep"></span>
        <span class="fl-chips" title="lifecycle: derived (machine-read) → proposed (revised) → agreed (operator sign-off). Editing nodes/edges while agreed drops it back to proposed."></span>
        <span class="fl-rev"></span>
        <span class="fl-spacer"></span>
        <span class="fl-actions"></span>
        <button class="fl-b fl-full" title="Full screen (Esc to exit)">⛶</button>
        <button class="fl-b fl-demo" title="Load the built-in demo flow">◇</button>
        <button class="fl-b fl-paste" title="Paste a \`\`\`flow block or flow JSON">📋</button>
        <button class="fl-b fl-dl" title="Download flow.json (bumps rev — a save)">⤓</button>
        <button class="fl-b fl-openf" title="Load a flow .json file">⤒</button>
        <input type="file" class="fl-file-json" accept=".json,application/json" hidden>
      </div>
      <div class="fl-meta">
        <input class="fl-title" type="text" placeholder="flow title" spellcheck="false">
        <span class="fl-derived" title=""></span>
      </div>
      <div class="fl-canvas-wrap" tabindex="0"></div>
      <div class="fl-props">
        <b class="fl-prop-id">—</b>
        <select class="fl-prop-role"></select>
        <input class="fl-prop-label" type="text" placeholder="label (branch condition on an edge)" spellcheck="false">
        <span class="fl-prop-geom"></span>
      </div>
      <div class="fl-status"></div>`;
    const q = (s) => host.querySelector(s);
    const wrap = q(".fl-canvas-wrap");

    for (const r of ROLE_ORDER) {
      const o = document.createElement("option");
      o.value = r; o.textContent = ROLES[r].name;
      q(".fl-role").appendChild(o);
    }

    const ed = {
      doc: blank(),
      sel: null,               // null | {t:"node", id} | {t:"edge", i}
      addMode: false, edgeMode: false, edgeFrom: null,
      undoStack: [], redoStack: [],
      onChange: opts.onChange || null,
    };

    /* -- status line -- */
    let statusT = null;
    ed.status = (msg, isErr, sticky) => {
      const s = q(".fl-status");
      s.textContent = msg || "";
      s.classList.toggle("err", !!isErr);
      clearTimeout(statusT);
      if (msg && !sticky) statusT = setTimeout(() => { s.textContent = ""; emptyHint(); }, 6000);
    };
    function emptyHint() {
      if (!ed.doc.nodes.length && !q(".fl-status").textContent)
        ed.status("empty — ＋ node to start, ◇ loads a demo, 📋 pastes a ```flow block", false, true);
    }

    /* -- inline destructive-action gate (board t85) --
       Same idiom as the steward drawer's red "arm live restarts?" confirm:
       the question + confirm/cancel render IN the status/action line, nothing
       fires until the operator answers, and cancel means NO write of any kind.
       Any later ed.status() call replaces the gate (it never lingers). */
    ed.confirm = (msg, okLabel, onOk) => {
      const s = q(".fl-status");
      clearTimeout(statusT);
      s.classList.remove("err");
      s.textContent = "";
      const box = document.createElement("div");
      box.className = "fl-confirm";
      const m = document.createElement("span");
      m.textContent = msg;
      const ok = document.createElement("button");
      ok.className = "fl-b fl-confirm-ok";
      ok.textContent = okLabel || "confirm";
      const no = document.createElement("button");
      no.className = "fl-b fl-confirm-cancel";
      no.textContent = "cancel";
      ok.onclick = () => { box.remove(); onOk(); };
      no.onclick = () => { box.remove(); ed.status("cancelled — nothing was written"); };
      box.append(m, ok, no);
      s.appendChild(box);
    };

    /* -- undo + the agreed→proposed lifecycle rule -- */
    const snapshot = () => JSON.stringify(ed.doc);
    const structKey = () => JSON.stringify([ed.doc.nodes, ed.doc.edges]);
    /* Every mutation commits through here. `before`/`beforeStruct` are the
       snapshots taken before the mutation; if the nodes/edges actually changed
       while the document was "agreed", the edit itself demotes it to
       "proposed" (FLOW-SCHEMA.md lifecycle) — an agreed flow with silent
       edits would be a forged sign-off. */
    function commit(before, beforeStruct) {
      if (snapshot() === before) return false;
      if (beforeStruct !== undefined && beforeStruct !== structKey() && ed.doc.status === "agreed") {
        ed.doc.status = "proposed";
        ed.status("edited while agreed — status dropped back to proposed");
      }
      ed.undoStack.push(before);
      if (ed.undoStack.length > 60) ed.undoStack.shift();
      ed.redoStack.length = 0;
      changed();
      return true;
    }
    function undo() {
      if (!ed.undoStack.length) return;
      ed.redoStack.push(snapshot());
      ed.doc = JSON.parse(ed.undoStack.pop());
      ed.sel = null; render(); changed();
    }
    function redo() {
      if (!ed.redoStack.length) return;
      ed.undoStack.push(snapshot());
      ed.doc = JSON.parse(ed.redoStack.pop());
      ed.sel = null; render(); changed();
    }
    function changed() { if (ed.onChange) ed.onChange(ed.doc); }

    /* -- canvas -- */
    const svg = baseSVG();
    svg.setAttribute("class", "fl-svg");
    const edgesG = el("g", {}, svg);
    const nodesG = el("g", {}, svg);
    const overlayG = el("g", {}, svg);
    wrap.appendChild(svg);
    const labelInput = document.createElement("input");
    labelInput.className = "fl-label-edit";
    labelInput.hidden = true;
    labelInput.spellcheck = false;
    wrap.appendChild(labelInput);

    const byId = (id) => ed.doc.nodes.find((n) => n.id === id);
    const selNode = () => (ed.sel && ed.sel.t === "node" ? byId(ed.sel.id) : null);
    const selEdge = () => (ed.sel && ed.sel.t === "edge" ? ed.doc.edges[ed.sel.i] : null);
    function newId() {                       // short, stable, never reused (ids anchor discussion)
      let max = 0;
      for (const n of ed.doc.nodes) {
        const m = /^f(\d+)$/.exec(n.id);
        if (m && +m[1] > max) max = +m[1];
      }
      return "f" + (max + 1);
    }

    function evPt(ev) {
      const r = svg.getBoundingClientRect();
      return { x: (ev.clientX - r.left) * CANVAS_W / r.width,
               y: (ev.clientY - r.top) * CANVAS_H / r.height };
    }
    function nodeAt(pt) {
      for (let i = ed.doc.nodes.length - 1; i >= 0; i--) {  // topmost (last-drawn) first
        const n = ed.doc.nodes[i];
        if (pt.x >= n.x && pt.x <= n.x + n.w && pt.y >= n.y && pt.y <= n.y + n.h) return n;
      }
      return null;
    }

    function render() {
      edgesG.textContent = "";
      nodesG.textContent = "";
      overlayG.textContent = "";
      ed.doc.edges.forEach((e, i) =>
        paintEdge(edgesG, e, i, byId, !!(ed.sel && ed.sel.t === "edge" && ed.sel.i === i)));
      for (const n of ed.doc.nodes) {
        const g = el("g", { "data-nid": n.id }, nodesG);
        paintNode(g, n);
      }
      const n = selNode();
      if (n) el("rect", { x: n.x - 4, y: n.y - 4, width: n.w + 8, height: n.h + 8, fill: "none",
                          stroke: "#1E6FD9", "stroke-width": 2, "stroke-dasharray": "6 4" }, overlayG);
      const src = ed.edgeMode && ed.edgeFrom && byId(ed.edgeFrom);
      if (src) el("rect", { x: src.x - 4, y: src.y - 4, width: src.w + 8, height: src.h + 8, fill: "none",
                            stroke: "#2E8B84", "stroke-width": 2, "stroke-dasharray": "3 3" }, overlayG);
      renderMeta();
      renderProps();
      emptyHint();
    }

    function renderMeta() {
      const chips = q(".fl-chips");
      chips.textContent = "";
      for (const st of STATUSES) {
        const c = document.createElement("span");
        c.className = "fl-chip" + (ed.doc.status === st ? " on" : "");
        c.setAttribute("data-st", st);
        c.textContent = st;
        c.onclick = () => {
          if (ed.doc.status === st) return;
          const before = snapshot();
          ed.doc.status = st;         // a status change is deliberate — no struct rule here
          commit(before);
          render();
        };
        chips.appendChild(c);
      }
      q(".fl-rev").textContent = "rev " + ed.doc.rev;
      if (document.activeElement !== q(".fl-title")) q(".fl-title").value = ed.doc.title;
      const df = ed.doc.derived_from;
      const dtxt = df && typeof df === "object"
        ? "derived from " + [df.path, df.commit && "@" + df.commit, df.scope && "(" + df.scope + ")"].filter(Boolean).join(" ")
        : "";
      q(".fl-derived").textContent = dtxt;
      q(".fl-derived").title = dtxt;
    }

    function renderProps() {
      const n = selNode(), e = selEdge();
      q(".fl-prop-id").textContent = n ? n.id : e ? e.from + "→" + e.to : "—";
      const roleSel = q(".fl-prop-role");
      roleSel.textContent = "";
      if (n) {
        const roles = ROLE_ORDER.slice();
        if (!ROLES[n.role]) roles.push(n.role);            // unknown role: keep it selectable/visible
        for (const r of roles) {
          const o = document.createElement("option");
          o.value = r; o.textContent = ROLES[r] ? ROLES[r].name : r + " (?)";
          roleSel.appendChild(o);
        }
        roleSel.value = n.role;
      }
      roleSel.disabled = !n;
      roleSel.style.visibility = e ? "hidden" : "visible";
      const lab = q(".fl-prop-label");
      lab.disabled = !n && !e;
      if (document.activeElement !== lab) lab.value = n ? n.label : e ? e.label : "";
      lab.placeholder = e ? "branch condition (yes / no / timeout …)" : "label";
      q(".fl-prop-geom").textContent = n ? `${n.x},${n.y} · ${n.w}×${n.h}` : "";
    }

    /* -- pointer interaction -- */
    let drag = null;                        // {nid, off, before, beforeStruct}
    svg.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0) return;
      wrap.focus();
      commitLabelEdit();
      // edge hit paths select the edge (they sit above the visible strokes)
      const ei = ev.target && ev.target.getAttribute && ev.target.getAttribute("data-ei");
      if (ei !== null && ei !== undefined && !ed.addMode && !ed.edgeMode) {
        ed.sel = { t: "edge", i: +ei };
        render();
        return;
      }
      const pt = evPt(ev);
      if (ed.addMode) {
        const role = q(".fl-role").value;
        const [w, h] = ROLE_SIZE[role] || [180, 60];
        const before = snapshot(), beforeStruct = structKey();
        const n = { id: newId(), role, label: styleFor(role).name,
                    x: clamp(snap(pt.x - w / 2), 0, CANVAS_W - w),
                    y: clamp(snap(pt.y - h / 2), 0, CANVAS_H - h), w, h };
        ed.doc.nodes.push(n);
        ed.sel = { t: "node", id: n.id };
        commit(before, beforeStruct);
        render();
        startLabelEdit({ t: "node", id: n.id });
        return;
      }
      const n = nodeAt(pt);
      if (ed.edgeMode) {
        if (!n) { setEdgeMode(false); render(); return; }
        if (!ed.edgeFrom) {
          ed.edgeFrom = n.id;
          ed.status("edge: " + n.id + " → … now click the target node (Esc cancels)", false, true);
          render();
          return;
        }
        const before = snapshot(), beforeStruct = structKey();
        ed.doc.edges.push({ from: ed.edgeFrom, to: n.id, label: "" });
        ed.sel = { t: "edge", i: ed.doc.edges.length - 1 };
        setEdgeMode(false);
        commit(before, beforeStruct);
        render();
        ed.status("edge added — label its branch condition below (a decision's branches MUST be labeled)");
        q(".fl-prop-label").focus();
        return;
      }
      if (n) {
        ed.sel = { t: "node", id: n.id };
        drag = { nid: n.id, off: { x: pt.x - n.x, y: pt.y - n.y },
                 before: snapshot(), beforeStruct: structKey(), moved: false };
        svg.setPointerCapture(ev.pointerId);
        ev.preventDefault();
        render();
      } else if (ed.sel) {
        ed.sel = null;
        render();
      }
    });
    svg.addEventListener("pointermove", (ev) => {
      if (!drag) {
        const pt = evPt(ev);
        svg.style.cursor = (ed.addMode || ed.edgeMode) ? "crosshair" : (nodeAt(pt) ? "move" : "default");
        return;
      }
      const n = byId(drag.nid);
      if (!n) return;
      const pt = evPt(ev);
      n.x = clamp(snap(pt.x - drag.off.x), 0, CANVAS_W - n.w);   // grid-snap 20 on drag
      n.y = clamp(snap(pt.y - drag.off.y), 0, CANVAS_H - n.h);
      drag.moved = true;
      render();
    });
    const endDrag = (ev) => {
      if (!drag) return;
      const d = drag; drag = null;
      try { svg.releasePointerCapture(ev.pointerId); } catch (e) {}
      commit(d.before, d.beforeStruct);
      render();
    };
    svg.addEventListener("pointerup", endDrag);
    svg.addEventListener("pointercancel", endDrag);
    svg.addEventListener("dblclick", (ev) => {
      const ei = ev.target && ev.target.getAttribute && ev.target.getAttribute("data-ei");
      if (ei !== null && ei !== undefined) {
        ed.sel = { t: "edge", i: +ei };
        render();
        startLabelEdit(ed.sel);
        return;
      }
      const n = nodeAt(evPt(ev));
      if (n) { ed.sel = { t: "node", id: n.id }; render(); startLabelEdit(ed.sel); }
    });

    /* -- inline label editing (floating input over node / edge midpoint) -- */
    let labelSel = null, labelBefore = null, labelBeforeStruct = null;
    function startLabelEdit(sel) {
      const n = sel.t === "node" ? byId(sel.id) : null;
      const e = sel.t === "edge" ? ed.doc.edges[sel.i] : null;
      if (!n && !e) return;
      labelSel = sel;
      labelBefore = snapshot();
      labelBeforeStruct = structKey();
      const r = svg.getBoundingClientRect(), w = wrap.getBoundingClientRect();
      const kx = r.width / CANVAS_W, ky = r.height / CANVAS_H;
      let cx, cy, ww;
      if (n) { cx = n.x; cy = n.y + n.h / 2; ww = n.w; }
      else {
        const a = byId(e.from), b = byId(e.to);
        const [mx, my] = edgeMid(edgePoints(a, b));
        cx = mx - 60; cy = my; ww = 120;
      }
      labelInput.hidden = false;
      labelInput.style.left = (r.left - w.left + cx * kx) + "px";
      labelInput.style.top = (r.top - w.top + cy * ky - 11) + "px";
      labelInput.style.width = Math.max(90, ww * kx) + "px";
      labelInput.value = n ? n.label : e.label;
      labelInput.focus();
      labelInput.select();
    }
    function commitLabelEdit() {
      if (labelInput.hidden || !labelSel) return;
      const n = labelSel.t === "node" ? byId(labelSel.id) : null;
      const e = labelSel.t === "edge" ? ed.doc.edges[labelSel.i] : null;
      labelInput.hidden = true;
      const v = labelInput.value.trim();
      if (n) n.label = v || styleFor(n.role).name;
      else if (e) e.label = v;
      if (n || e) { commit(labelBefore, labelBeforeStruct); render(); }
      labelSel = null;
    }
    labelInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commitLabelEdit(); wrap.focus(); }
      if (e.key === "Escape") { labelInput.hidden = true; labelSel = null; wrap.focus(); }
      e.stopPropagation();
    });
    labelInput.addEventListener("blur", commitLabelEdit);

    /* -- keyboard -- */
    wrap.addEventListener("keydown", (e) => {
      if (!labelInput.hidden) return;
      if (e.key === "Escape") { setAddMode(false); setEdgeMode(false); ed.sel = null; render(); return; }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
        e.preventDefault(); (e.shiftKey ? redo : undo)(); return;
      }
      if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSel(); return; }
      const n = selNode();
      if (!n) return;
      const step = e.shiftKey ? GRID * 4 : GRID;
      const mv = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
      if (mv) {
        e.preventDefault();
        const before = snapshot(), beforeStruct = structKey();
        n.x = clamp(n.x + mv[0], 0, CANVAS_W - n.w);
        n.y = clamp(n.y + mv[1], 0, CANVAS_H - n.h);
        commit(before, beforeStruct); render();
      }
    });

    /* -- toolbar -- */
    function setAddMode(on) {
      ed.addMode = on;
      if (on) setEdgeMode(false);
      q(".fl-addnode").classList.toggle("on", on);
      svg.style.cursor = on ? "crosshair" : "default";
    }
    function setEdgeMode(on) {
      ed.edgeMode = on;
      ed.edgeFrom = null;
      if (on) { ed.addMode = false; q(".fl-addnode").classList.remove("on"); }
      q(".fl-addedge").classList.toggle("on", on);
      svg.style.cursor = on ? "crosshair" : "default";
      if (on) ed.status("edge: click the SOURCE node…", false, true);
      else if (!on && !labelSel) ed.status("");
    }
    q(".fl-addnode").onclick = () => setAddMode(!ed.addMode);
    q(".fl-addedge").onclick = () => { setEdgeMode(!ed.edgeMode); render(); };
    q(".fl-undo").onclick = undo;
    q(".fl-redo").onclick = redo;
    function deleteSel() {
      if (!ed.sel) return;
      const before = snapshot(), beforeStruct = structKey();
      if (ed.sel.t === "node") {
        const id = ed.sel.id;
        ed.doc.nodes = ed.doc.nodes.filter((n) => n.id !== id);
        ed.doc.edges = ed.doc.edges.filter((e) => e.from !== id && e.to !== id);
      } else {
        ed.doc.edges.splice(ed.sel.i, 1);
      }
      ed.sel = null;
      commit(before, beforeStruct); render();
    }
    q(".fl-del").onclick = deleteSel;
    q(".fl-demo").onclick = () => { loadWithUndo(demo()); ed.status("demo flow loaded — a small retry loop (note the back-edge)"); };
    /* full-screen toggle: a class lifts .fl-ed to a fixed viewport overlay.
       Esc exits via a capture-phase listener so it fires before the canvas's
       own Escape (which only cancels add/edge modes), never fighting it. */
    (function () {
      const fullBtn = q(".fl-full");
      function escFull(e) { if (e.key === "Escape") { e.stopPropagation(); setFull(false); } }
      function setFull(on) {
        host.classList.toggle("fl-full", on);
        fullBtn.classList.toggle("on", on);
        fullBtn.title = on ? "Exit full screen (Esc)" : "Full screen (Esc to exit)";
        if (on) document.addEventListener("keydown", escFull, true);
        else document.removeEventListener("keydown", escFull, true);
        // let the SVG re-fit to the new viewport size if the editor exposes it
        if (typeof ed.relayout === "function") ed.relayout();
      }
      fullBtn.onclick = () => setFull(!host.classList.contains("fl-full"));
    })();
    q(".fl-prop-role").onchange = (e) => {
      const n = selNode();
      if (!n) return;
      const before = snapshot(), beforeStruct = structKey();
      const old = styleFor(n.role).name;
      n.role = e.target.value;
      if (!n.label || n.label === old) n.label = styleFor(n.role).name;
      commit(before, beforeStruct); render();
    };
    q(".fl-prop-label").addEventListener("change", (e) => {
      const n = selNode(), ee = selEdge();
      const v = e.target.value.trim();
      if (n && n.label !== v) {
        const before = snapshot(), beforeStruct = structKey();
        n.label = v || styleFor(n.role).name;
        commit(before, beforeStruct); render();
      } else if (ee && ee.label !== v) {
        const before = snapshot(), beforeStruct = structKey();
        ee.label = v;
        commit(before, beforeStruct); render();
      }
    });
    q(".fl-title").addEventListener("change", (e) => {
      const v = e.target.value.trim();
      if (ed.doc.title === v) return;
      const before = snapshot();
      ed.doc.title = v;                     // title is not a node/edge edit — agreed stays agreed
      commit(before); renderMeta();
    });

    /* -- save / load file -- */
    q(".fl-dl").onclick = () => {
      const doc = ed.serializeForSave();    // a download IS a save: rev bumps
      const a = document.createElement("a");
      a.href = "data:application/json;charset=utf-8," + encodeURIComponent(JSON.stringify(doc, null, 2));
      a.download = "flow.json";
      document.body.appendChild(a); a.click(); a.remove();
      ed.status("downloaded flow.json (rev " + doc.rev + ")");
    };
    q(".fl-openf").onclick = () => q(".fl-file-json").click();
    q(".fl-file-json").onchange = (e) => {
      const f = e.target.files[0];
      e.target.value = "";
      if (!f) return;
      f.text().then((t) => {
        const st = parse(t);
        if (!st) return ed.status("not a valid flow.v1 document — dropped (fail-closed)", true);
        loadWithUndo(st);
        ed.status(`loaded "${st.title || f.name}" — ${st.nodes.length} nodes, ${st.edges.length} edges, rev ${st.rev}`);
      });
    };

    function loadWithUndo(doc) {
      const before = snapshot();
      ed.doc = doc;
      ed.sel = null;
      // a load replaces the document wholesale — the agreed→proposed rule is
      // about edits WITHIN a doc, so commit without the struct check.
      commit(before);
      render();
    }

    /* -- paste import (fenced ```flow block or bare JSON) -- */
    q(".fl-paste").onclick = () => {
      const bg = document.createElement("div");
      bg.className = "fl-modal-bg";
      const box = document.createElement("div");
      box.className = "fl-modal";
      box.innerHTML = `
        <h4>Paste a flow</h4>
        <p>Paste a \`\`\`flow block or flow.v1 JSON — e.g. copied from a keeper or chat reply. Invalid documents are dropped whole (fail-closed).</p>
        <textarea class="flm-paste" rows="8" spellcheck="false" placeholder='\`\`\`flow\n{"schema":"flow.v1",…}\n\`\`\`'></textarea>
        <div class="fl-modal-btns"><button class="flm-cancel">Cancel</button><button class="flm-go">Load</button></div>`;
      bg.appendChild(box);
      host.appendChild(bg);
      const close = () => bg.remove();
      bg.onclick = (e) => { if (e.target === bg) close(); };
      box.querySelector(".flm-cancel").onclick = close;
      box.querySelector(".flm-go").onclick = () => {
        const st = parse(box.querySelector(".flm-paste").value);
        close();
        if (!st) return ed.status("not a valid flow.v1 document — dropped (fail-closed)", true);
        loadWithUndo(st);
        ed.status(`loaded "${st.title || "untitled"}" from paste — ${st.nodes.length} nodes, ${st.edges.length} edges, rev ${st.rev}`);
      };
      box.querySelector(".flm-paste").focus();
    };

    /* -- host-app actions (→ VM / ← VM / ⧉) -- */
    for (const a of opts.actions || []) {
      const b = document.createElement("button");
      b.className = "fl-b fl-action";
      b.textContent = a.label;
      if (a.title) b.title = a.title;
      b.onclick = () => a.fn(ed);
      q(".fl-actions").appendChild(b);
    }

    /* -- public surface -- */
    // load: validates fail-closed. Returns true when the document landed,
    // false (current canvas untouched + a calm error) when it was dropped.
    ed.load = (raw, skipUndo) => {
      const st = typeof raw === "object" && raw !== null && raw.schema === SCHEMA && Array.isArray(raw.nodes)
        ? normalize(raw) : parse(raw);
      if (!st) {
        ed.status("not a valid flow.v1 document — dropped (fail-closed)", true);
        return false;
      }
      if (skipUndo) { ed.doc = st; ed.sel = null; render(); }
      else loadWithUndo(st);
      return true;
    };
    ed.serialize = () => JSON.parse(snapshot());
    // serialize-for-save: rev bumps here (FLOW-SCHEMA.md: "bump on every save
    // by either side"), never on mere edits — ties board notes to revisions.
    ed.serializeForSave = () => {
      ed.doc.rev = (Number.isInteger(+ed.doc.rev) ? +ed.doc.rev : 0) + 1;
      changed();
      renderMeta();
      return ed.serialize();
    };
    ed.toChat = () => toChat(ed.serialize());
    ed.isEmpty = () => !ed.doc.nodes.length;

    render();
    return ed;
  }

  window.FLOW = {
    SCHEMA, CANVAS_W, CANVAS_H, GRID, ROLES, ROLE_ORDER, STATUSES,
    blank, demo, parse, toChat, mountEditor,
    _test: { looseJSON, normalize, wrapLabel, edgePoints },
  };
})();
