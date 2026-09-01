/* wireframe.js — bidirectional UI-design conveyance for the station console.

   A wireframe is a JSON document (schema "wireframe.v1", canvas 1280x800,
   shapes with role/label/geometry — the same schema as abstract_ide's
   wireframeTab, so files interchange with the desktop tool). It travels both
   ways through the per-station LLM chat:

     model -> human   the model emits a fenced ```wireframe block; app.js
                      renders it inline as an SVG thumbnail (WF.thumbSVG) with
                      an "open in designer" action.
     human -> model   the Design drawer (WF.mountEditor) is a grid/snap editor;
                      "Attach" embeds the current design into the next chat
                      message as the same fenced block.

   Imports into the editor:
     screenshot  a vision model estimates regions. The prompt asks for
                 bbox_2d [x1,y1,x2,y2] PIXEL coords — the format Qwen2.5-VL's
                 grounding training uses — and the parser also tolerates
                 percent x/y/w/h so other models still land.
     HTML        no LLM at all: the browser renders it in a sandboxed iframe
                 (scripts off) and we read exact getBoundingClientRect geometry.
     URL         /api/wf/fetch proxies the HTML (same-origin), then the iframe
                 path measures it. Approximate for JS-built pages.

   No dependencies; app.js passes transport callbacks (LLM complete, URL fetch)
   into mountEditor so this file stays UI-only. */
(function () {
  "use strict";

  const CANVAS_W = 1280, CANVAS_H = 800, GRID = 20, SCHEMA = "wireframe.v1";
  const SVGNS = "http://www.w3.org/2000/svg";

  /* role palette — single source of truth for colour + variant rendering
     (kept identical to the desktop wireframeTab so designs look the same). */
  const PALETTE = {
    container: { name: "Container", fill: "#EAF2FB", border: "#4A90D9", variant: "plain" },
    nav:       { name: "Nav",       fill: "#D6E4F0", border: "#2C6FB5", variant: "plain" },
    sidebar:   { name: "Sidebar",   fill: "#E8E8F7", border: "#6A5ACD", variant: "plain" },
    button:    { name: "Button",    fill: "#DFF0D8", border: "#3C763D", variant: "rounded" },
    input:     { name: "Input",     fill: "#FFFFFF", border: "#999999", variant: "underline" },
    text:      { name: "Text",      fill: "#FFFFFF", border: "#CCCCCC", variant: "textlines" },
    image:     { name: "Image",     fill: "#EEEEEE", border: "#AAAAAA", variant: "imagecross" },
    list:      { name: "List",      fill: "#FFF8E1", border: "#C9A227", variant: "dividers" },
    note:      { name: "Note",      fill: "#FFF9C4", border: "#E0D060", variant: "note" },
  };
  const ROLE_ORDER = ["container", "nav", "sidebar", "button", "input", "text", "image", "list", "note"];

  // layout-significant HTML tags -> roles (mirrors the desktop html_import)
  const TAG_ROLE = {
    header: "nav", nav: "nav", footer: "nav",
    aside: "sidebar",
    main: "container", section: "container", article: "container",
    form: "container", div: "container",
    button: "button", a: "button",
    input: "input", textarea: "input", select: "input",
    img: "image", picture: "image", svg: "image", video: "image",
    ul: "list", ol: "list", table: "list",
    h1: "text", h2: "text", h3: "text", p: "text", label: "text",
  };
  const MEASURE_TAGS = Object.keys(TAG_ROLE).filter((t) => t !== "div");

  /* ---------------- state helpers ---------------- */
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const snapTo = (v, g) => Math.round(v / (g || GRID)) * (g || GRID);
  function codeFor(n) {              // 1 -> a, 26 -> z, 27 -> aa
    let s = "";
    while (n > 0) { const r = (n - 1) % 26; s = String.fromCharCode(97 + r) + s; n = Math.floor((n - 1) / 26); }
    return s;
  }
  function codeNum(code) {
    let n = 0;
    for (const ch of String(code || "")) {
      if (ch < "a" || ch > "z") return 0;
      n = n * 26 + (ch.charCodeAt(0) - 96);
    }
    return n;
  }

  function blank() {
    return { schema: SCHEMA, canvas: { w: CANVAS_W, h: CANVAS_H },
             grid: { size: GRID, snap: true, visible: true }, shapes: [] };
  }

  /* Normalize any shapes-ish list into clean shape dicts: valid role, short
     label, integer geometry clamped to the canvas, ids/codes backfilled. */
  function normalizeShapes(list, opts) {
    const o = opts || {};
    const out = [];
    let maxId = 0, maxCode = 0;
    for (const s of list || []) {
      if (!s || typeof s !== "object") continue;
      let x = +s.x, y = +s.y, w = +s.w, h = +s.h;
      if ([x, y, w, h].some((v) => !isFinite(v))) continue;
      if (o.percent) { x = x / 100 * CANVAS_W; y = y / 100 * CANVAS_H; w = w / 100 * CANVAS_W; h = h / 100 * CANVAS_H; }
      if (o.scaleX) { x *= o.scaleX; w *= o.scaleX; }
      if (o.scaleY) { y *= o.scaleY; h *= o.scaleY; }
      const role = String(s.role || "container").split("|")[0].trim().toLowerCase();
      const sh = {
        id: typeof s.id === "string" ? s.id : null,
        code: typeof s.code === "string" ? s.code.toLowerCase() : "",
        x: clamp(snapTo(x), 0, CANVAS_W - GRID),
        y: clamp(snapTo(y), 0, CANVAS_H - GRID),
        w: Math.max(GRID, snapTo(w)), h: Math.max(GRID, snapTo(h)),
        role: PALETTE[role] ? role : "container",
        label: String(s.label || s.name || s.text || "").trim().slice(0, 60),
        z: isFinite(+s.z) ? +s.z : out.length,
      };
      sh.w = Math.min(sh.w, CANVAS_W - sh.x);
      sh.h = Math.min(sh.h, CANVAS_H - sh.y);
      if (!sh.label) sh.label = PALETTE[sh.role].name;
      out.push(sh);
      const idn = parseInt(String(sh.id || "").replace(/^s/, ""), 10);
      if (idn > maxId) maxId = idn;
      const cn = codeNum(sh.code);
      if (cn > maxCode) maxCode = cn;
    }
    for (const sh of out) {                    // backfill ids/codes without colliding
      if (!sh.id) sh.id = "s" + (++maxId);
      if (!sh.code) sh.code = codeFor(++maxCode);
    }
    return out;
  }

  function normalizeState(raw) {
    const st = blank();
    if (raw && typeof raw === "object") {
      if (raw.grid && typeof raw.grid === "object") {
        st.grid.size = [10, 20, 40].includes(+raw.grid.size) ? +raw.grid.size : GRID;
        if (raw.grid.snap === false) st.grid.snap = false;
      }
      st.shapes = normalizeShapes(raw.shapes || []);
    }
    return st;
  }

  /* Tolerant JSON extraction: model output arrives with prose, fences, or
     trailing commas. Returns the parsed value or null. */
  function looseJSON(text) {
    let t = String(text || "");
    const fence = t.match(/```[a-zA-Z]*\s*\n([\s\S]*?)```/);
    if (fence) t = fence[1];
    const a = t.search(/[[{]/);
    if (a < 0) return null;
    const closer = t[a] === "{" ? "}" : "]";
    const b = t.lastIndexOf(closer);
    if (b <= a) return null;
    t = t.slice(a, b + 1);
    try { return JSON.parse(t); } catch (e) {}
    try { return JSON.parse(t.replace(/,\s*([}\]])/g, "$1")); } catch (e) {}
    return null;
  }

  /* Parse anything wireframe-ish (full state, {shapes:[…]} px, {regions:[…]}
     percent, or a bare array) into a normalized state, or null. */
  function parse(text) {
    const data = typeof text === "string" ? looseJSON(text) : text;
    if (!data) return null;
    let list = null, percent = false;
    if (Array.isArray(data)) list = data;
    else if (Array.isArray(data.shapes)) list = data.shapes;
    else if (Array.isArray(data.regions)) { list = data.regions; percent = true; }
    if (!list) return null;
    // bare/percent heuristics: coordinates that all fit 0-100 are percentages
    if (percent || list.every((s) => s && +s.x <= 100 && +s.y <= 100 && (+s.x + +s.w) <= 101 && (+s.y + +s.h) <= 101)) {
      const st = blank();
      st.shapes = normalizeShapes(list, { percent: true });
      // …unless the doc explicitly declared a matching canvas (then it was px)
      if (data.canvas && +data.canvas.w === CANVAS_W) st.shapes = normalizeShapes(list);
      return st.shapes.length ? st : null;
    }
    const st = normalizeState({ grid: data.grid, shapes: list });
    return st.shapes.length ? st : null;
  }

  /* Compact form for embedding in chat (short, model-friendly). */
  function toChat(state) {
    return JSON.stringify({
      schema: SCHEMA, canvas: { w: CANVAS_W, h: CANVAS_H },
      shapes: sortedShapes(state).map((s) => ({
        code: s.code, role: s.role, label: s.label, x: s.x, y: s.y, w: s.w, h: s.h,
      })),
    });
  }
  function sortedShapes(state) {
    return (state.shapes || []).slice().sort((a, b) => (a.z - b.z) || (codeNum(a.code) - codeNum(b.code)));
  }

  /* Split message text into segments: {t:"text",s} and {t:"wf",state,raw}. */
  const WF_BLOCK_RE = /```(?:wireframe|wf)\s*\n([\s\S]*?)```/g;
  function extractBlocks(text) {
    const out = [];
    let last = 0, m;
    WF_BLOCK_RE.lastIndex = 0;
    while ((m = WF_BLOCK_RE.exec(text || ""))) {
      if (m.index > last) out.push({ t: "text", s: text.slice(last, m.index) });
      const state = parse(m[1]);
      if (state) out.push({ t: "wf", state, raw: m[1].trim() });
      else out.push({ t: "text", s: m[0] });
      last = m.index + m[0].length;
    }
    const rest = (text || "").slice(last);
    if (rest) out.push({ t: "text", s: rest });
    return out;
  }

  /* ---------------- styles (self-injected) ----------------
     One stylesheet shipped with the module so BOTH consoles (station console
     :8800 and the fleet web-console behind console.abstractendeavors.com) get
     identical editor/thumbnail chrome without duplicating CSS. Palette rides
     --wf-* custom properties with dark defaults; a host can override them. */
  const WF_CSS = `
.wf-ed, .wf-thumb, .wf-modal-bg {
  --wf-bg:#0d1117; --wf-panel:#151b24; --wf-panel2:#1b2330; --wf-line:#2a3140;
  --wf-fg:#c9d3e4; --wf-muted:#7d8799; --wf-accent:#3d95f5; --wf-bad:#f06060;
  font: 13px/1.4 ui-monospace, Menlo, Consolas, monospace; color: var(--wf-fg);
}
.wf-ed { display:flex; flex-direction:column; min-height:0; position:relative; flex:1; background:var(--wf-panel); }
.wf-tools { display:flex; align-items:center; gap:4px; flex-wrap:wrap; flex:none;
  padding:6px 8px; background:var(--wf-panel2); border-bottom:1px solid var(--wf-line); }
.wf-b { background:var(--wf-panel); color:var(--wf-fg); border:1px solid var(--wf-line);
  border-radius:5px; padding:3px 8px; cursor:pointer; font-size:12px; }
.wf-b:hover { border-color:var(--wf-accent); }
.wf-b.on { border-color:var(--wf-accent); color:var(--wf-accent); }
.wf-b.wf-action { border-color:var(--wf-accent); color:var(--wf-accent); font-weight:600; }
.wf-role, .wf-prop-role { background:var(--wf-bg); color:var(--wf-fg); border:1px solid var(--wf-line);
  border-radius:5px; padding:3px 6px; font:12px ui-monospace,Menlo,Consolas,monospace; }
.wf-sep { width:1px; height:18px; background:var(--wf-line); margin:0 3px; }
.wf-spacer { flex:1; }
.wf-canvas-wrap { position:relative; flex:none; padding:8px; outline:none; }
.wf-canvas-wrap:focus { box-shadow: inset 0 0 0 1px var(--wf-accent); }
.wf-svg { display:block; width:100%; height:auto; aspect-ratio:1280/800; border-radius:4px; touch-action:none; }
.wf-label-edit { position:absolute; z-index:5; background:#fff; color:#222; border:1px solid #1E6FD9;
  border-radius:3px; padding:2px 6px; font:12px ui-monospace,Menlo,Consolas,monospace; }
.wf-props { display:flex; align-items:center; gap:6px; flex:none; padding:5px 10px; border-top:1px solid var(--wf-line); }
.wf-prop-code { min-width:18px; color:var(--wf-accent); }
.wf-prop-label { flex:1; background:var(--wf-bg); color:var(--wf-fg); border:1px solid var(--wf-line);
  border-radius:5px; padding:3px 8px; font:12px ui-monospace,Menlo,Consolas,monospace; }
.wf-prop-label:focus { outline:none; border-color:var(--wf-accent); }
.wf-prop-geom { white-space:nowrap; }
.wf-legend { display:flex; flex-wrap:wrap; gap:4px; flex:none; padding:6px 10px; }
.wf-chip { border:1px solid var(--wf-line); border-radius:10px; padding:1px 8px; font-size:11px;
  color:var(--wf-fg); cursor:pointer; background:var(--wf-bg); }
.wf-chip.sel { background:var(--wf-panel2); color:var(--wf-accent); }
.wf-status { flex:none; padding:4px 10px 8px; min-height:18px; }
.wf-status.err { color:var(--wf-bad); }
.wf-confirm { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:8px 10px;
  border:1px solid var(--wf-bad); border-radius:6px; background:#1a1214; }
.wf-confirm span { flex:1 1 100%; color:var(--wf-fg); }
.wf-confirm .wf-confirm-ok { border-color:var(--wf-bad); color:var(--wf-bad); }
.wf-ed .hint, .wf-modal .hint { color:var(--wf-muted); font-size:12px; opacity:1; margin-right:0; }
.wf-thumb { white-space:normal; background:var(--wf-bg); border:1px solid var(--wf-line);
  border-radius:6px; padding:6px; cursor:pointer; max-width:420px; }
.wf-thumb:hover { border-color:var(--wf-accent); }
.wf-thumb svg { display:block; width:100%; height:auto; border-radius:3px; }
.wf-thumb-bar { display:flex; align-items:center; gap:8px; padding-top:5px; }
.wf-thumb-bar .hint { flex:1; color:var(--wf-muted); font-size:12px; }
.wf-thumb-bar button { background:var(--wf-panel); color:var(--wf-fg); border:1px solid var(--wf-line);
  border-radius:5px; padding:2px 8px; cursor:pointer; font-size:11px; }
.wf-thumb-bar button:hover { border-color:var(--wf-accent); }
.llm-seg + .wf-thumb, .wf-thumb + .llm-seg { margin-top:8px; }
.wf-modal-bg { position:absolute; inset:0; z-index:10; background:rgba(4,8,16,.72);
  display:flex; align-items:flex-start; justify-content:center; padding-top:40px; }
.wf-modal { background:var(--wf-panel); border:1px solid var(--wf-line); border-radius:8px;
  padding:14px; width:min(480px,92%); }
.wf-modal h4 { margin:0 0 6px; color:var(--wf-accent); }
.wf-modal p { margin:0 0 10px; }
.wf-modal select, .wf-modal textarea { width:100%; background:var(--wf-bg); color:var(--wf-fg);
  border:1px solid var(--wf-line); border-radius:5px; padding:6px 8px;
  font:12px ui-monospace,Menlo,Consolas,monospace; }
.wf-modal textarea { resize:vertical; }
.wf-modal-btns { display:flex; justify-content:flex-end; gap:8px; margin-top:12px; }
.wf-modal-btns button { background:var(--wf-panel2); color:var(--wf-fg); border:1px solid var(--wf-line);
  border-radius:5px; padding:5px 14px; cursor:pointer; }
.wf-modal-btns button:last-child { background:var(--wf-accent); color:#04101f;
  border-color:var(--wf-accent); font-weight:600; }`;

  function ensureStyles() {
    if (document.getElementById("wf-style")) return;
    const s = document.createElement("style");
    s.id = "wf-style";
    s.textContent = WF_CSS;
    document.head.appendChild(s);
  }

  /* ---------------- SVG rendering ---------------- */
  function el(tag, attrs, parent) {
    const n = document.createElementNS(SVGNS, tag);
    for (const k in attrs || {}) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  function paintShape(g, s, forEditor) {
    const pal = PALETTE[s.role] || PALETTE.container;
    const v = pal.variant;
    const r = el("rect", {
      x: s.x, y: s.y, width: s.w, height: s.h,
      fill: pal.fill, "fill-opacity": v === "note" ? 0.78 : 1,
      stroke: v === "note" ? "none" : pal.border, "stroke-width": 2,
      rx: v === "rounded" ? 8 : v === "note" ? 4 : 0,
    }, g);
    if (forEditor) r.setAttribute("class", "wf-rect");
    const hint = { stroke: pal.border, "stroke-opacity": 0.45, "stroke-width": 1.5 };
    if (v === "underline") {
      el("line", { x1: s.x + 6, y1: s.y + s.h - 6, x2: s.x + s.w - 6, y2: s.y + s.h - 6, ...hint }, g);
    } else if (v === "imagecross") {
      el("line", { x1: s.x, y1: s.y, x2: s.x + s.w, y2: s.y + s.h, ...hint }, g);
      el("line", { x1: s.x + s.w, y1: s.y, x2: s.x, y2: s.y + s.h, ...hint }, g);
    } else if (v === "textlines") {
      const n = Math.min(4, Math.max(1, Math.floor(s.h / 24)));
      for (let i = 0; i < n; i++) {
        const yy = s.y + 14 + i * 18;
        if (yy > s.y + s.h - 8) break;
        const w = (i === n - 1 ? 0.5 : 1) * (s.w - 16);
        el("line", { x1: s.x + 8, y1: yy, x2: s.x + 8 + w, y2: yy, ...hint }, g);
      }
    } else if (v === "dividers") {
      const step = Math.max(24, Math.floor(s.h / 4));
      for (let yy = s.y + step; yy < s.y + s.h - 6; yy += step)
        el("line", { x1: s.x + 5, y1: yy, x2: s.x + s.w - 5, y2: yy, ...hint }, g);
    }
    // compact code centered; label under it when the box has room
    const cx = s.x + s.w / 2, cy = s.y + s.h / 2;
    const showLabel = s.w >= 120 && s.h >= 56;
    const code = el("text", {
      x: cx, y: showLabel ? cy - 4 : cy, fill: "#222", "font-size": 24, "font-weight": 700,
      "text-anchor": "middle", "dominant-baseline": "middle",
    }, g);
    code.textContent = s.code;
    if (showLabel) {
      const lab = el("text", {
        x: cx, y: cy + 18, fill: "#333", "font-size": 15,
        "text-anchor": "middle", "dominant-baseline": "middle",
      }, g);
      lab.textContent = s.label.length > Math.floor(s.w / 9) ? s.label.slice(0, Math.floor(s.w / 9) - 1) + "…" : s.label;
    }
    const title = el("title", {}, g);
    title.textContent = `${s.code} · ${pal.name} · ${s.label}`;
  }

  function baseSVG(withGrid, gridSize) {
    const svg = el("svg", { viewBox: `0 0 ${CANVAS_W} ${CANVAS_H}`, preserveAspectRatio: "xMidYMid meet" });
    el("rect", { x: 0, y: 0, width: CANVAS_W, height: CANVAS_H, fill: "#FBFBFD", stroke: "#9AA3B2", "stroke-width": 2 }, svg);
    if (withGrid) {
      const g = gridSize || GRID;
      const defs = el("defs", {}, svg);
      const pat = el("pattern", { id: "wfgrid", width: g, height: g, patternUnits: "userSpaceOnUse" }, defs);
      el("path", { d: `M ${g} 0 L 0 0 0 ${g}`, fill: "none", stroke: "#E3E6EC", "stroke-width": 1 }, pat);
      el("rect", { x: 0, y: 0, width: CANVAS_W, height: CANVAS_H, fill: "url(#wfgrid)" }, svg);
    }
    return svg;
  }

  /* Read-only thumbnail for chat transcript blocks. */
  function thumbSVG(state) {
    ensureStyles();
    const svg = baseSVG(false);
    svg.setAttribute("class", "wf-thumb-svg");
    for (const s of sortedShapes(state)) paintShape(el("g", {}, svg), s, false);
    return svg;
  }

  /* ---------------- import: vision (screenshot) ---------------- */
  function visionPrompt(w, h) {
    return (
      `This is a ${w}x${h} pixel screenshot of a user interface. Identify every ` +
      `major rectangular region: navigation bars, sidebars, content panels, ` +
      `buttons, inputs, images, lists, text blocks. Reply with ONLY a JSON ` +
      `object, no prose, exactly in this form:\n` +
      `{"regions":[{"role":"nav","label":"top nav","bbox_2d":[x1,y1,x2,y2]}]}\n` +
      `- bbox_2d is [left, top, right, bottom] in PIXELS of the ${w}x${h} image.\n` +
      `- role is one of: container, nav, sidebar, button, input, text, image, list.\n` +
      `- label: 2-4 words naming the region.\n` +
      `List the large background regions first, then the elements inside them. ` +
      `Locate each box as precisely as you can.`
    );
  }

  /* Vision reply -> shapes. Accepts bbox_2d/bbox [x1,y1,x2,y2] pixel boxes
     (Qwen-VL grounding format) or x/y/w/h (percent if everything fits 0-100). */
  function parseVisionReply(text, imgW, imgH) {
    const data = looseJSON(text);
    if (!data) return [];
    const list = Array.isArray(data) ? data : (data.regions || data.shapes || data.boxes || []);
    const px = [];
    let sawBox = false, allPct = true;
    for (const r of list) {
      if (!r || typeof r !== "object") continue;
      const bb = r.bbox_2d || r.bbox || r.box;
      let x, y, w, h;
      if (Array.isArray(bb) && bb.length === 4) {
        sawBox = true;
        x = +bb[0]; y = +bb[1]; w = +bb[2] - +bb[0]; h = +bb[3] - +bb[1];
      } else {
        x = +(r.x !== undefined ? r.x : r.left); y = +(r.y !== undefined ? r.y : r.top);
        w = +(r.w !== undefined ? r.w : r.width); h = +(r.h !== undefined ? r.h : r.height);
      }
      if ([x, y, w, h].some((v) => !isFinite(v)) || w <= 0 || h <= 0) continue;
      if (x + w > 100 || y + h > 100) allPct = false;
      px.push({ role: r.role, label: r.label || r.name, x, y, w, h });
    }
    if (!px.length) return [];
    const pct = !sawBox && allPct;
    const shapes = normalizeShapes(
      px.sort((a, b) => (b.w * b.h) - (a.w * a.h)),
      pct ? { percent: true } : { scaleX: CANVAS_W / (imgW || CANVAS_W), scaleY: CANVAS_H / (imgH || CANVAS_H) });
    shapes.forEach((s, i) => (s.z = i));
    return shapes;
  }

  /* Downscale an image file to <=maxSide px and return a PNG data URL + dims
     (small local VL models 500 on big inputs; 896 = 32 Qwen patches). */
  function imageToDataURL(file, maxSide) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      const url = URL.createObjectURL(file);
      img.onload = () => {
        URL.revokeObjectURL(url);
        const k = Math.min(1, (maxSide || 896) / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * k)), h = Math.max(1, Math.round(img.height * k));
        const cv = document.createElement("canvas");
        cv.width = w; cv.height = h;
        cv.getContext("2d").drawImage(img, 0, 0, w, h);
        resolve({ dataUrl: cv.toDataURL("image/png"), w, h });
      };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("could not load image")); };
      img.src = url;
    });
  }

  /* ---------------- import: HTML measured in a sandboxed iframe ----------- */
  /* Render HTML with the browser's own engine (scripts disabled) and read the
     true layout boxes — exact geometry, no model guessing. */
  function measureHTML(html, base) {
    return new Promise((resolve, reject) => {
      const frame = document.createElement("iframe");
      frame.setAttribute("sandbox", "allow-same-origin");   // layout yes, scripts no
      frame.style.cssText = `position:fixed;left:-10020px;top:0;width:${CANVAS_W}px;height:${CANVAS_H}px;border:0;`;
      let doc = String(html || "");
      if (base) {
        const tag = `<base href="${String(base).replace(/"/g, "&quot;")}">`;
        doc = /<head[^>]*>/i.test(doc) ? doc.replace(/<head[^>]*>/i, (m) => m + tag)
            : tag + doc;
      }
      const done = (fn) => { try { fn(); } finally { frame.remove(); } };
      frame.onload = () => setTimeout(() => done(() => {
        try {
          const idoc = frame.contentDocument;
          const win = frame.contentWindow;
          const boxes = [], seen = new Set();
          for (const node of idoc.querySelectorAll(MEASURE_TAGS.join(","))) {
            const r = node.getBoundingClientRect();
            if (r.width < 24 || r.height < 12) continue;
            const key = node.tagName + "|" + Math.round(r.x) + "|" + Math.round(r.y) +
                        "|" + Math.round(r.width) + "|" + Math.round(r.height);
            if (seen.has(key)) continue;
            seen.add(key);
            const text = (node.textContent || node.getAttribute("aria-label") ||
                          node.getAttribute("alt") || node.getAttribute("placeholder") || "")
                         .replace(/\s+/g, " ").trim();
            boxes.push({ tag: node.tagName.toLowerCase(),
                         x: r.x + win.scrollX, y: r.y + win.scrollY,
                         w: r.width, h: r.height, text: text.slice(0, 40) });
          }
          resolve(boxesToShapes(boxes));
        } catch (e) { reject(e); }
      }), 350);                                  // settle: late CSS/img layout
      document.body.appendChild(frame);
      frame.srcdoc = doc;
      setTimeout(() => done(() => reject(new Error("render timed out"))), 20000);
    });
  }

  function boxesToShapes(boxes, maxShapes) {
    boxes = (boxes || []).filter((b) => b.w >= 24 && b.h >= 12);
    if (!boxes.length) return [];
    const maxX = Math.max(...boxes.map((b) => b.x + b.w));
    const maxY = Math.max(...boxes.map((b) => b.y + b.h));
    const k = Math.min(CANVAS_W / Math.max(maxX, 1), CANVAS_H / Math.max(maxY, 1), 1);
    boxes.sort((a, b) => (b.w * b.h) - (a.w * a.h));
    boxes = boxes.slice(0, maxShapes || 60);
    const shapes = normalizeShapes(boxes.map((b) => ({
      role: TAG_ROLE[b.tag] || "container",
      label: b.text || b.tag,
      x: b.x * k, y: b.y * k, w: b.w * k, h: b.h * k,
    })));
    shapes.forEach((s, i) => (s.z = i));
    return shapes;
  }

  /* ---------------- the editor ---------------- */
  const HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
  const HANDLE_CURSOR = { nw: "nwse-resize", se: "nwse-resize", ne: "nesw-resize", sw: "nesw-resize",
                          n: "ns-resize", s: "ns-resize", e: "ew-resize", w: "ew-resize" };

  function mountEditor(host, opts) {
    opts = opts || {};
    ensureStyles();
    host.classList.add("wf-ed");
    host.innerHTML = `
      <div class="wf-tools">
        <button class="wf-b wf-add" title="Add box: click-drag on the canvas (Esc to leave)">▭ Add</button>
        <select class="wf-role" title="Role for new boxes"></select>
        <button class="wf-b wf-dup" title="Duplicate selection (Ctrl+D)">Dup</button>
        <button class="wf-b wf-del" title="Delete selection (Del)">Del</button>
        <button class="wf-b wf-undo" title="Undo (Ctrl+Z)">↶</button>
        <button class="wf-b wf-redo" title="Redo (Ctrl+Shift+Z)">↷</button>
        <button class="wf-b wf-clear" title="Clear the canvas">✕ Clear</button>
        <span class="wf-sep"></span>
        <button class="wf-b wf-imp-img" title="From screenshot — a vision model estimates the regions (approximate)">📷</button>
        <button class="wf-b wf-imp-html" title="From HTML — paste markup, the browser measures its real layout (exact)">&lt;/&gt;</button>
        <button class="wf-b wf-imp-url" title="From URL — fetch the page and measure its layout (no JS, approximate)">🔗</button>
        <button class="wf-b wf-imp-paste" title="Paste a design — a wireframe block or JSON from a chat/keeper reply">📋</button>
        <span class="wf-spacer"></span>
        <span class="wf-actions"></span>
        <button class="wf-b wf-dl" title="Download wireframe.json">⤓</button>
        <button class="wf-b wf-openf" title="Load a wireframe .json">⤒</button>
        <input type="file" class="wf-file-img" accept="image/*" hidden>
        <input type="file" class="wf-file-json" accept=".json,application/json" hidden>
      </div>
      <div class="wf-canvas-wrap" tabindex="0"></div>
      <div class="wf-props">
        <b class="wf-prop-code">—</b>
        <select class="wf-prop-role"></select>
        <input class="wf-prop-label" type="text" placeholder="label" spellcheck="false">
        <span class="wf-prop-geom hint"></span>
      </div>
      <div class="wf-legend"></div>
      <div class="wf-status hint"></div>`;
    const q = (s) => host.querySelector(s);
    const wrap = q(".wf-canvas-wrap");

    for (const sel of [q(".wf-role"), q(".wf-prop-role")]) {
      for (const r of ROLE_ORDER) {
        const o = document.createElement("option");
        o.value = r; o.textContent = PALETTE[r].name;
        sel.appendChild(o);
      }
    }

    const ed = {
      state: blank(), sel: null, addMode: false,
      undoStack: [], redoStack: [], nodes: new Map(),
      onChange: opts.onChange || null,
    };

    /* -- status line -- */
    let statusT = null;
    ed.status = (msg, isErr, sticky) => {
      const s = q(".wf-status");
      s.textContent = msg || "";
      s.classList.toggle("err", !!isErr);
      clearTimeout(statusT);
      if (msg && !sticky) statusT = setTimeout(() => (s.textContent = ""), 6000);
    };

    /* -- inline destructive-action gate (board t85) --
       Same idiom as the steward drawer's red "arm live restarts?" confirm (and
       identical to flow.js's ed.confirm): the question + confirm/cancel render
       IN the status line, nothing fires until the operator answers, and cancel
       means NO write of any kind. A later ed.status() call replaces the gate. */
    ed.confirm = (msg, okLabel, onOk) => {
      const s = q(".wf-status");
      clearTimeout(statusT);
      s.classList.remove("err");
      s.textContent = "";
      const box = document.createElement("div");
      box.className = "wf-confirm";
      const m = document.createElement("span");
      m.textContent = msg;
      const ok = document.createElement("button");
      ok.className = "wf-b wf-confirm-ok";
      ok.textContent = okLabel || "confirm";
      const no = document.createElement("button");
      no.className = "wf-b wf-confirm-cancel";
      no.textContent = "cancel";
      ok.onclick = () => { box.remove(); onOk(); };
      no.onclick = () => { box.remove(); ed.status("cancelled — nothing was written"); };
      box.append(m, ok, no);
      s.appendChild(box);
    };

    /* -- undo -- */
    const snapshot = () => JSON.stringify(ed.state);
    function pushUndo(before) {
      const after = snapshot();
      if (after === before) return false;
      ed.undoStack.push(before);
      if (ed.undoStack.length > 60) ed.undoStack.shift();
      ed.redoStack.length = 0;
      changed();
      return true;
    }
    function undo() {
      if (!ed.undoStack.length) return;
      ed.redoStack.push(snapshot());
      ed.state = JSON.parse(ed.undoStack.pop());
      ed.sel = null; render(); changed();
    }
    function redo() {
      if (!ed.redoStack.length) return;
      ed.undoStack.push(snapshot());
      ed.state = JSON.parse(ed.redoStack.pop());
      ed.sel = null; render(); changed();
    }
    function changed() { if (ed.onChange) ed.onChange(ed.state); }

    /* -- canvas -- */
    const svg = baseSVG(true, GRID);
    svg.setAttribute("class", "wf-svg");
    const shapesG = el("g", {}, svg);
    const overlayG = el("g", {}, svg);
    wrap.appendChild(svg);
    const labelInput = document.createElement("input");
    labelInput.className = "wf-label-edit";
    labelInput.hidden = true;
    labelInput.spellcheck = false;
    wrap.appendChild(labelInput);

    const byId = (sid) => ed.state.shapes.find((s) => s.id === sid);
    // fresh id/code for ONE new shape without re-normalizing (and re-snapping)
    // the shapes already on the canvas
    function newIds() {
      let maxId = 0, maxCode = 0;
      for (const s of ed.state.shapes) {
        const n = parseInt(String(s.id || "").replace(/^s/, ""), 10);
        if (n > maxId) maxId = n;
        const c = codeNum(s.code);
        if (c > maxCode) maxCode = c;
      }
      return { id: "s" + (maxId + 1), code: codeFor(maxCode + 1) };
    }
    const gsz = () => ed.state.grid.size || GRID;
    const snapv = (v) => (ed.state.grid.snap === false ? Math.round(v) : snapTo(v, gsz()));

    function evPt(ev) {
      const r = svg.getBoundingClientRect();
      return { x: (ev.clientX - r.left) * CANVAS_W / r.width,
               y: (ev.clientY - r.top) * CANVAS_H / r.height };
    }
    function shapeAt(pt) {
      const list = sortedShapes(ed.state);
      for (let i = list.length - 1; i >= 0; i--) {      // topmost first
        const s = list[i];
        if (pt.x >= s.x && pt.x <= s.x + s.w && pt.y >= s.y && pt.y <= s.y + s.h) return s;
      }
      return null;
    }
    function handleAt(pt) {
      const s = ed.sel && byId(ed.sel);
      if (!s) return null;
      const tol = 14;
      const xs = { w: s.x, c: s.x + s.w / 2, e: s.x + s.w };
      const ys = { n: s.y, m: s.y + s.h / 2, s: s.y + s.h };
      const pos = { nw: [xs.w, ys.n], n: [xs.c, ys.n], ne: [xs.e, ys.n], e: [xs.e, ys.m],
                    se: [xs.e, ys.s], s: [xs.c, ys.s], sw: [xs.w, ys.s], w: [xs.w, ys.m] };
      for (const h of HANDLES) {
        if (Math.abs(pt.x - pos[h][0]) <= tol && Math.abs(pt.y - pos[h][1]) <= tol) return h;
      }
      return null;
    }

    function render() {
      shapesG.textContent = "";
      overlayG.textContent = "";
      ed.nodes.clear();
      for (const s of sortedShapes(ed.state)) {
        const g = el("g", { "data-sid": s.id }, shapesG);
        paintShape(g, s, true);
        ed.nodes.set(s.id, g);
      }
      const s = ed.sel && byId(ed.sel);
      if (s) {
        el("rect", { x: s.x, y: s.y, width: s.w, height: s.h, fill: "none",
                     stroke: "#1E6FD9", "stroke-width": 2, "stroke-dasharray": "6 4" }, overlayG);
        const xs = { w: s.x, c: s.x + s.w / 2, e: s.x + s.w };
        const ys = { n: s.y, m: s.y + s.h / 2, s: s.y + s.h };
        const pos = { nw: [xs.w, ys.n], n: [xs.c, ys.n], ne: [xs.e, ys.n], e: [xs.e, ys.m],
                      se: [xs.e, ys.s], s: [xs.c, ys.s], sw: [xs.w, ys.s], w: [xs.w, ys.m] };
        for (const h of HANDLES)
          el("rect", { x: pos[h][0] - 7, y: pos[h][1] - 7, width: 14, height: 14,
                       fill: "#fff", stroke: "#1E6FD9", "stroke-width": 1.5 }, overlayG);
      }
      renderProps();
      renderLegend();
    }
    function rerenderShape(s) {         // cheap in-drag update: repaint one group
      const g = ed.nodes.get(s.id);
      if (!g) return;
      g.textContent = "";
      paintShape(g, s, true);
      overlayG.textContent = "";        // handles follow on release (render())
    }

    function renderProps() {
      const s = ed.sel && byId(ed.sel);
      q(".wf-prop-code").textContent = s ? s.code : "—";
      q(".wf-prop-role").value = s ? s.role : "container";
      q(".wf-prop-role").disabled = q(".wf-prop-label").disabled = !s;
      if (document.activeElement !== q(".wf-prop-label"))
        q(".wf-prop-label").value = s ? s.label : "";
      q(".wf-prop-geom").textContent = s ? `${s.x},${s.y} · ${s.w}×${s.h}` : "";
    }
    function renderLegend() {
      const lg = q(".wf-legend");
      lg.textContent = "";
      for (const s of ed.state.shapes.slice().sort((a, b) => codeNum(a.code) - codeNum(b.code))) {
        const chip = document.createElement("span");
        chip.className = "wf-chip" + (s.id === ed.sel ? " sel" : "");
        chip.style.borderColor = PALETTE[s.role].border;
        chip.textContent = `${s.code} ${s.label}`;
        chip.title = PALETTE[s.role].name;
        chip.onclick = () => { ed.sel = s.id; render(); };
        lg.appendChild(chip);
      }
    }

    /* -- pointer interaction -- */
    let drag = null;                    // {type, sid, before, off|start|handle, rect0}
    svg.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0) return;
      wrap.focus();
      commitLabelEdit();
      const pt = evPt(ev);
      const before = snapshot();
      if (ed.addMode) {
        const role = q(".wf-role").value;
        const s = {
          ...newIds(),
          x: clamp(snapv(pt.x), 0, CANVAS_W - gsz()), y: clamp(snapv(pt.y), 0, CANVAS_H - gsz()),
          w: gsz(), h: gsz(), role, label: PALETTE[role].name, z: ed.state.shapes.length,
        };
        ed.state.shapes.push(s);
        ed.sel = s.id;
        drag = { type: "create", sid: s.id, start: { x: s.x, y: s.y }, before };
        render();
      } else {
        const h = handleAt(pt);
        if (h) {
          const s = byId(ed.sel);
          drag = { type: "resize", sid: s.id, handle: h, rect0: { ...s }, before };
        } else {
          const s = shapeAt(pt);
          if (s) {
            if (ed.sel !== s.id) { ed.sel = s.id; render(); }
            drag = { type: "move", sid: s.id, off: { x: pt.x - s.x, y: pt.y - s.y }, before, moved: false };
          } else if (ed.sel) {
            ed.sel = null; render();
          }
        }
      }
      if (drag) { svg.setPointerCapture(ev.pointerId); ev.preventDefault(); }
    });
    svg.addEventListener("pointermove", (ev) => {
      const pt = evPt(ev);
      if (!drag) {                                       // hover cursor feedback
        svg.style.cursor = ed.addMode ? "crosshair"
          : (handleAt(pt) ? HANDLE_CURSOR[handleAt(pt)] : (shapeAt(pt) ? "move" : "default"));
        return;
      }
      const s = byId(drag.sid);
      if (!s) return;
      if (drag.type === "move") {
        s.x = clamp(snapv(pt.x - drag.off.x), 0, CANVAS_W - s.w);
        s.y = clamp(snapv(pt.y - drag.off.y), 0, CANVAS_H - s.h);
        drag.moved = true;
      } else if (drag.type === "create") {
        const x2 = clamp(snapv(pt.x), 0, CANVAS_W), y2 = clamp(snapv(pt.y), 0, CANVAS_H);
        s.x = Math.min(drag.start.x, x2); s.y = Math.min(drag.start.y, y2);
        s.w = Math.max(gsz(), Math.abs(x2 - drag.start.x));
        s.h = Math.max(gsz(), Math.abs(y2 - drag.start.y));
      } else if (drag.type === "resize") {
        const r0 = drag.rect0, h = drag.handle;
        let L = r0.x, T = r0.y, R = r0.x + r0.w, B = r0.y + r0.h;
        if (h.includes("w")) L = clamp(snapv(pt.x), 0, R - gsz());
        if (h.includes("e")) R = clamp(snapv(pt.x), L + gsz(), CANVAS_W);
        if (h.includes("n")) T = clamp(snapv(pt.y), 0, B - gsz());
        if (h.includes("s")) B = clamp(snapv(pt.y), T + gsz(), CANVAS_H);
        s.x = L; s.y = T; s.w = R - L; s.h = B - T;
      }
      rerenderShape(s);
      renderProps();
    });
    const endDrag = (ev) => {
      if (!drag) return;
      const d = drag; drag = null;
      try { svg.releasePointerCapture(ev.pointerId); } catch (e) {}
      pushUndo(d.before);
      render();
      if (d.type === "create") startLabelEdit(byId(d.sid));   // name it right away
    };
    svg.addEventListener("pointerup", endDrag);
    svg.addEventListener("pointercancel", endDrag);
    svg.addEventListener("dblclick", (ev) => {
      const s = shapeAt(evPt(ev));
      if (s) { ed.sel = s.id; render(); startLabelEdit(s); }
    });

    /* -- inline label editing (floating input over the shape) -- */
    let labelSid = null, labelBefore = null;
    function startLabelEdit(s) {
      if (!s) return;
      labelSid = s.id;
      labelBefore = snapshot();
      const r = svg.getBoundingClientRect(), w = wrap.getBoundingClientRect();
      const kx = r.width / CANVAS_W, ky = r.height / CANVAS_H;
      labelInput.hidden = false;
      labelInput.style.left = (r.left - w.left + s.x * kx) + "px";
      labelInput.style.top = (r.top - w.top + (s.y + s.h / 2) * ky - 11) + "px";
      labelInput.style.width = Math.max(90, s.w * kx) + "px";
      labelInput.value = s.label;
      labelInput.focus();
      labelInput.select();
    }
    function commitLabelEdit() {
      if (labelInput.hidden || labelSid === null) return;
      const s = byId(labelSid);
      labelInput.hidden = true;
      if (s) {
        const v = labelInput.value.trim();
        s.label = v || PALETTE[s.role].name;
        pushUndo(labelBefore);
        render();
      }
      labelSid = null;
    }
    labelInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commitLabelEdit(); wrap.focus(); }
      if (e.key === "Escape") { labelInput.hidden = true; labelSid = null; wrap.focus(); }
      e.stopPropagation();
    });
    labelInput.addEventListener("blur", commitLabelEdit);

    /* -- keyboard on the canvas -- */
    wrap.addEventListener("keydown", (e) => {
      if (!labelInput.hidden) return;
      const s = ed.sel && byId(ed.sel);
      if (e.key === "Escape") { setAddMode(false); ed.sel = null; render(); return; }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
        e.preventDefault(); (e.shiftKey ? redo : undo)(); return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "d") { e.preventDefault(); duplicate(); return; }
      if (!s) return;
      if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSel(); return; }
      const step = e.shiftKey ? gsz() * 4 : gsz();
      const mv = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
      if (mv) {
        e.preventDefault();
        const before = snapshot();
        s.x = clamp(s.x + mv[0], 0, CANVAS_W - s.w);
        s.y = clamp(s.y + mv[1], 0, CANVAS_H - s.h);
        pushUndo(before); render();
      }
    });

    /* -- toolbar -- */
    function setAddMode(on) {
      ed.addMode = on;
      q(".wf-add").classList.toggle("on", on);
      svg.style.cursor = on ? "crosshair" : "default";
    }
    q(".wf-add").onclick = () => setAddMode(!ed.addMode);
    q(".wf-undo").onclick = undo;
    q(".wf-redo").onclick = redo;
    function deleteSel() {
      if (!ed.sel) return;
      const before = snapshot();
      ed.state.shapes = ed.state.shapes.filter((s) => s.id !== ed.sel);
      ed.sel = null;
      pushUndo(before); render();
    }
    function duplicate() {
      const s = ed.sel && byId(ed.sel);
      if (!s) return;
      const before = snapshot();
      const copy = { ...s, ...newIds(), x: Math.min(s.x + gsz(), CANVAS_W - s.w),
                     y: Math.min(s.y + gsz(), CANVAS_H - s.h), z: ed.state.shapes.length };
      ed.state.shapes.push(copy);
      ed.sel = copy.id;
      pushUndo(before); render();
    }
    q(".wf-del").onclick = deleteSel;
    q(".wf-dup").onclick = duplicate;
    q(".wf-clear").onclick = () => {
      if (!ed.state.shapes.length) return;
      const before = snapshot();
      ed.state.shapes = []; ed.sel = null;
      pushUndo(before); render();
    };
    q(".wf-prop-role").onchange = (e) => {
      const s = ed.sel && byId(ed.sel);
      if (!s) return;
      const before = snapshot();
      s.role = e.target.value;
      if (s.label === "" || ROLE_ORDER.some((r) => s.label === PALETTE[r].name)) s.label = PALETTE[s.role].name;
      pushUndo(before); render();
    };
    q(".wf-prop-label").addEventListener("change", (e) => {
      const s = ed.sel && byId(ed.sel);
      if (!s || s.label === e.target.value.trim()) return;
      const before = snapshot();
      s.label = e.target.value.trim() || PALETTE[s.role].name;
      pushUndo(before); render();
    });

    /* -- save / load file -- */
    q(".wf-dl").onclick = () => {
      const a = document.createElement("a");
      a.href = "data:application/json;charset=utf-8," + encodeURIComponent(JSON.stringify(ed.state, null, 2));
      a.download = "wireframe.json";
      document.body.appendChild(a); a.click(); a.remove();
    };
    q(".wf-openf").onclick = () => q(".wf-file-json").click();
    q(".wf-file-json").onchange = (e) => {
      const f = e.target.files[0];
      e.target.value = "";
      if (!f) return;
      f.text().then((t) => {
        const st = parse(t);
        if (!st) return ed.status("not a readable wireframe json", true);
        loadWithUndo(st);
        ed.status(`loaded ${st.shapes.length} shapes from ${f.name}`);
      });
    };

    function loadWithUndo(st) {
      const before = snapshot();
      ed.state = normalizeState(st);
      ed.sel = null;
      pushUndo(before);
      render();
    }

    /* -- small in-drawer modal -- */
    function modal(build) {
      const bg = document.createElement("div");
      bg.className = "wf-modal-bg";
      const box = document.createElement("div");
      box.className = "wf-modal";
      bg.appendChild(box);
      host.appendChild(bg);
      const close = () => bg.remove();
      bg.onclick = (e) => { if (e.target === bg) close(); };
      build(box, close);
      return close;
    }

    // imports that need a transport hide when the host didn't wire one
    if (!opts.llm || !opts.llm.complete) q(".wf-imp-img").style.display = "none";
    if (!opts.fetchHTML) q(".wf-imp-url").style.display = "none";

    /* -- import: paste a wireframe block / JSON (e.g. from a keeper reply) -- */
    q(".wf-imp-paste").onclick = () => modal((box, close) => {
      box.innerHTML = `
        <h4>Paste a design</h4>
        <p class="hint">Paste a \`\`\`wireframe block or wireframe JSON — e.g. copied from a keeper or chat reply.</p>
        <textarea class="wfm-paste" rows="8" spellcheck="false" placeholder='\`\`\`wireframe\n{"schema":"wireframe.v1",…}\n\`\`\`'></textarea>
        <div class="wf-modal-btns"><button class="wfm-cancel">Cancel</button><button class="wfm-go">Load</button></div>`;
      box.querySelector(".wfm-cancel").onclick = close;
      box.querySelector(".wfm-go").onclick = () => {
        const st = parse(box.querySelector(".wfm-paste").value);
        if (!st) return ed.status("couldn't read a wireframe out of that", true);
        close();
        loadWithUndo(st);
        ed.status(`loaded ${st.shapes.length} shapes from paste`);
      };
      box.querySelector(".wfm-paste").focus();
    });

    /* -- import: screenshot via vision model -- */
    q(".wf-imp-img").onclick = () => {
      if (!opts.llm || !opts.llm.complete) return ed.status("no LLM transport wired", true);
      q(".wf-file-img").click();
    };
    q(".wf-file-img").onchange = async (e) => {
      const f = e.target.files[0];
      e.target.value = "";
      if (!f) return;
      let models = [];
      try { models = (await opts.llm.models()) || []; } catch (err) {}
      models = models.filter((m) => m.ready && m.key);
      if (!models.length) return ed.status("no models available", true);
      const visRe = /vl|vision|llava|moondream|minicpm|internvl|smolvlm|idefics|pix|fuyu/i;
      const guess = models.find((m) => visRe.test(m.label)) || models[0];
      modal((box, close) => {
        box.innerHTML = `
          <h4>Screenshot → wireframe</h4>
          <p class="hint">A vision model estimates the regions — a starting point to adjust, not to-scale. Pick a vision-capable model:</p>
          <select class="wfm-model"></select>
          <div class="wf-modal-btns"><button class="wfm-cancel">Cancel</button><button class="wfm-go">Analyze</button></div>`;
        const sel = box.querySelector(".wfm-model");
        for (const m of models) {
          const o = document.createElement("option");
          o.value = `${m.provider}::${m.key}`; o.textContent = m.label;
          if (m === guess) o.selected = true;
          sel.appendChild(o);
        }
        box.querySelector(".wfm-cancel").onclick = close;
        box.querySelector(".wfm-go").onclick = () => { close(); visionImport(f, sel.value); };
      });
    };
    async function visionImport(file, modelSel) {
      try {
        ed.status("reading image…", false, true);
        const { dataUrl, w, h } = await imageToDataURL(file, 896);
        const messages = [{ role: "user", content: [
          { type: "text", text: visionPrompt(w, h) },
          { type: "image_url", image_url: { url: dataUrl } },
        ] }];
        let text = null, lastErr = null;
        for (let i = 0; i < 4; i++) {                     // VL swap-slots cold-load
          ed.status(`analyzing with ${modelSel.split("::")[1] || modelSel}…` +
                    (i ? ` (retry ${i}, model may be cold-loading)` : " (may cold-load)"), false, true);
          try { text = await opts.llm.complete(modelSel, messages, 1200); break; }
          catch (err) {
            lastErr = err;
            if (!/50[023]|load|busy|timeout/i.test(String(err.message))) break;
            await new Promise((r) => setTimeout(r, 12000));
          }
        }
        if (text === null) throw lastErr || new Error("no reply");
        const shapes = parseVisionReply(text, w, h);
        if (!shapes.length) return ed.status("model returned no usable regions — try a clearer screenshot", true);
        loadWithUndo({ shapes });
        ed.status(`${shapes.length} regions from screenshot (approximate — adjust as needed)`);
      } catch (err) {
        ed.status("vision import failed: " + err.message, true);
      }
    }

    /* -- import: pasted HTML (exact, browser-measured) -- */
    q(".wf-imp-html").onclick = () => modal((box, close) => {
      box.innerHTML = `
        <h4>HTML → wireframe</h4>
        <p class="hint">Paste markup; the browser renders it (scripts off) and the real layout boxes become shapes.</p>
        <textarea class="wfm-html" rows="8" spellcheck="false" placeholder="&lt;html&gt;…"></textarea>
        <div class="wf-modal-btns"><button class="wfm-cancel">Cancel</button><button class="wfm-go">Measure</button></div>`;
      box.querySelector(".wfm-cancel").onclick = close;
      box.querySelector(".wfm-go").onclick = async () => {
        const html = box.querySelector(".wfm-html").value;
        if (!html.trim()) return;
        close();
        try {
          ed.status("rendering + measuring…", false, true);
          const shapes = await measureHTML(html, null);
          if (!shapes.length) return ed.status("no layout regions found in that HTML", true);
          loadWithUndo({ shapes });
          ed.status(`${shapes.length} regions measured from HTML`);
        } catch (err) { ed.status("HTML import failed: " + err.message, true); }
      };
      box.querySelector(".wfm-html").focus();
    });

    /* -- import: live URL (server-fetched, browser-measured, no JS) -- */
    q(".wf-imp-url").onclick = async () => {
      if (!opts.fetchHTML) return ed.status("no URL fetcher wired", true);
      let url = prompt("Page URL:");
      if (!url || !url.trim()) return;
      url = url.trim();
      if (!/^https?:\/\//.test(url)) url = "https://" + url;
      try {
        ed.status("fetching " + url + "…", false, true);
        const r = await opts.fetchHTML(url);
        ed.status("rendering + measuring…", false, true);
        const shapes = await measureHTML(r.html, r.base || url);
        if (!shapes.length) return ed.status("no layout regions extracted (JS-built page?)", true);
        loadWithUndo({ shapes });
        ed.status(`${shapes.length} regions from ${url} (static render — no JS)`);
      } catch (err) { ed.status("URL import failed: " + err.message, true); }
    };

    /* -- host-app actions (e.g. "Attach to chat") -- */
    for (const a of opts.actions || []) {
      const b = document.createElement("button");
      b.className = "wf-b wf-action";
      b.textContent = a.label;
      if (a.title) b.title = a.title;
      b.onclick = () => a.fn(ed);
      q(".wf-actions").appendChild(b);
    }

    /* -- public surface -- */
    ed.load = (st, skipUndo) => {
      if (skipUndo) { ed.state = normalizeState(st); ed.sel = null; render(); }
      else loadWithUndo(st);
    };
    ed.serialize = () => JSON.parse(snapshot());
    ed.toChat = () => toChat(ed.state);
    ed.isEmpty = () => !ed.state.shapes.length;

    render();
    return ed;
  }

  window.WF = {
    SCHEMA, CANVAS_W, CANVAS_H, GRID, PALETTE, ROLE_ORDER,
    blank, parse, toChat, extractBlocks, thumbSVG, mountEditor,
    visionPrompt, parseVisionReply, imageToDataURL, measureHTML,
    _test: { looseJSON, normalizeShapes, boxesToShapes, codeFor, codeNum },
  };
})();
