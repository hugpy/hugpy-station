/* FleetView terminal — LEFT-DOCKED pane injected into the existing console UI.
   The terminal owns the left side (like the web console's keeper terminal) and
   the console's tabs/drawers keep the right. Connects to /wsterm (host PTY).
   Auto-opens on load; the ⌨ edge-tab collapses/restores it. Self-contained.

   Three PERSISTENT surfaces (MCT design: Local Keeper | Frontier Keeper |
   Terminal Shell) ride the bar as a static switch. Each surface is its own
   xterm + websocket, created lazily and kept alive while the page lives, so
   switching never loses a session; server-side the PTYs are stationary
   (reaper-exempt, scrollback replayed on reattach), so even a page reload
   comes back to the same sessions. The backend dropdown restarts only its own
   surface. The A gate button is the design's "Frontier Keeper: Enabled /
   Disabled": disabling prevents STARTING a frontier session (server-enforced)
   but never stops a running one and never touches local/shell.
   Surface/backend keys map to commands SERVER-side (TERM_SURFACES). */
(function () {
  if (window.__fleetviewTerm) return;
  window.__fleetviewTerm = true;

  // Width: persisted — drag the right edge to set where the right-hand column
  // (the top tabs' drawers) begins. Top/bottom: fallbacks, re-measured from
  // the console's real header (nav) and status bar (floor) at mount + resize.
  var W = localStorage.getItem("fv-width") || "56%";
  var TOP = 40, BOT = 26;

  // Fallback copy of the server's whitelist KEYS (commands live server-side
  // only); replaced by /api/term/backends when it answers.
  var SURFACES = {
    local:    { "default": "opencode",
                backends: { "opencode": { available: true },
                            "qwen-code": { available: true } } },
    frontier: { "default": "claude-code",
                backends: { "mct": { available: true },
                            "claude-code": { available: true } } },
    shell:    { "default": "exec",
                backends: { "exec": { available: true },
                            "ssh":  { available: true } } }
  };
  var SURFACE_TIPS = {
    local:    "Local Keeper — talk to B (hugpy-agent) directly; works without A",
    frontier: "Frontier Keeper — A's seat; claude-code is the base terminal, mct the file-pointer exchange over it",
    shell:    "Terminal Shell — your own shell; output enters model context only when you explicitly share it"
  };
  // Display names only — backend KEYS are identity (storage, URLs, the
  // server's whitelist) and never change. The local seat execs the OpenCode
  // TUI, but the seat itself is `hugpy-agent console` (operator, 2026-08-13):
  // it reads as "Hugpy Agent".
  // mct promotion (2026-08-27): the pointer-exchange arbiter IS mct now (the
  // original broker mct-deprecated was retired from the picker 2026-08-28).
  var BACKEND_LABELS = { "opencode": "Hugpy Agent",
                         "mct": "mct · pointer exchange" };
  function backendLabel(k) { return BACKEND_LABELS[k] || k; }
  var frontierEnabled = true;
  var bEnabled = true;   // B (local keeper) gate — server-backed, default ON
  // Active VM (set by the SPA via __fvVm): the shell surface follows it —
  // its PTY runs `station shell` INSIDE that VM. Server keeps one persistent
  // session per VM ("shell:<vm>"), so switching replays that VM's scrollback.
  var activeVm = "";
  var surface = localStorage.getItem("fv-surface") || "frontier";
  if (!SURFACES[surface]) surface = "frontier";
  var chosen = {};   // surface -> chosen backend key
  try { chosen = JSON.parse(localStorage.getItem("fv-backends") || "{}") || {}; }
  catch (e) { chosen = {}; }

  var css = document.createElement("style");
  css.textContent =
    "#fv-term-pane{position:fixed;left:0;top:" + TOP + "px;bottom:" + BOT + "px;" +
    "width:" + W + ";z-index:2147482999;background:#0d1117;" +
    "border-right:1px solid #30363d;display:flex;flex-direction:column;" +
    "box-shadow:6px 0 18px rgba(0,0,0,.35)}" +
    "#fv-term-pane.closed{display:none}" +
    "#fv-term-bar{height:26px;display:flex;align-items:center;gap:8px;" +
    "padding:0 10px;background:#161b22;border-bottom:1px solid #262d38;" +
    "color:#b6bec9;font:12px system-ui,sans-serif;flex:none}" +
    "#fv-term-bar .dot{width:8px;height:8px;border-radius:50%;background:#d05}" +
    "#fv-term-bar .dot.ok{background:#2ea043}" +
    "#fv-term-bar .fv-sw{cursor:pointer;padding:1px 7px;border-radius:4px;" +
    "color:#8b949e;border:1px solid transparent;user-select:none}" +
    "#fv-term-bar .fv-sw.on{color:#e6e9ef;background:#21262d;border-color:#30363d}" +
    "#fv-term-bar .fv-sw.gated{text-decoration:line-through;opacity:.7}" +
    "#fv-gate,#fv-bgate{cursor:pointer;padding:1px 7px;border-radius:4px;user-select:none;" +
    "border:1px solid #30363d;color:#2ea043}" +
    "#fv-gate.off{color:#f85149}" +
    "#fv-bgate.off{color:#d29922}" +
    ".fv-lane{cursor:pointer;padding:1px 6px;border-radius:4px;user-select:none;" +
    "border:1px solid #30363d;color:#8b949e;font-family:ui-monospace,SFMono-Regular,monospace}" +
    ".fv-lane:hover{color:#e6e9ef;background:#21262d}" +
    "#fv-lane-bang{color:#d29922}" +
    ".fv-lane.hidden{display:none}" +
    "#fv-backend{background:#0d1117;color:#b6bec9;border:1px solid #30363d;" +
    "border-radius:4px;font:12px system-ui,sans-serif;max-width:130px}" +
    "#fv-backend.hidden{display:none}" +
    "#fv-session{background:#0d1117;color:#b6bec9;border:1px solid #30363d;" +
    "border-radius:4px;font:12px system-ui,sans-serif;max-width:150px}" +
    "#fv-session.hidden,#fv-session-new.hidden{display:none}" +
    "#fv-session-new{cursor:pointer;color:#8b949e;padding:0 4px;user-select:none}" +
    "#fv-term-drag{position:absolute;top:0;bottom:0;right:-3px;width:7px;" +
    "cursor:ew-resize;z-index:3}" +
    "#fv-term-hide{margin-left:auto;cursor:pointer;color:#8b949e}" +
    "#fv-term-host{flex:1;padding:4px;overflow:hidden;position:relative}" +
    ".fv-view{position:absolute;inset:4px;display:none}" +
    ".fv-view.on{display:block}" +
    "#fv-term-tab{position:fixed;left:0;top:50%;transform:translateY(-50%);" +
    "z-index:2147483000;background:#238636;color:#fff;border:1px solid #2ea043;" +
    "border-left:none;border-radius:0 8px 8px 0;padding:10px 6px;" +
    "font:13px system-ui,sans-serif;cursor:pointer;writing-mode:vertical-rl;" +
    "display:none}" +
    "#fv-term-tab.show{display:block}";
  document.head.appendChild(css);

  var pane = document.createElement("div");
  pane.id = "fv-term-pane";
  pane.innerHTML =
    '<div id="fv-term-bar"><span class="dot" id="fv-dot"></span>' +
    '<span id="fv-status">terminal</span>' +
    '<select id="fv-backend" title="backend for this surface — switching DETACHES (each backend keeps its own tmux session; switch back to resume it)"></select>' +
    '<select id="fv-session" title="frontier session (workspace) - switch or create"></select>' +
    '<span id="fv-session-new" title="new frontier session">+</span>' +
    '<span id="fv-bgate" title="B (local keeper): Allowed/Blocked — default ON. Gates only the direct B channels (💬 B chat); it never switches the frontier backend and never restarts anything.">B: on</span>' +
    '<span id="fv-gate" title="Frontier Keeper: Enabled/Disabled — gates only A launches; local, shell, and a running frontier session are unaffected">A: on</span>' +
    '<span class="fv-lane hidden" id="fv-lane-bang" data-lane="!" title="mct INTERRUPT lane: types ! at the REPL prompt — finish the line and Enter to kill A mid-turn; the next turn opens with a reconcile brief">!</span>' +
    '<span class="fv-lane hidden" id="fv-lane-plus" data-lane="+" title="mct APPEND lane: types + at the REPL prompt — your line gets its OWN turn, after current work and any pending digest">+</span>' +
    '<span class="fv-lane hidden" id="fv-lane-todo" data-lane="todo: " title="mct CAPTURE lane: types todo: at the REPL prompt — no turn spent; the line is appended to the workspace todo.md">todo:</span>' +
    '<span id="fv-term-hide" title="collapse">⇤ hide</span></div>' +
    '<div id="fv-term-host"></div>' +
    '<div id="fv-term-drag" title="drag to set where the right-hand column begins"></div>';

  var tab = document.createElement("button");
  tab.id = "fv-term-tab";
  tab.textContent = "⌨ terminal";

  var views = {};          // surface -> {el, term, fit, ws}
  var enc = new TextEncoder();
  var started = false;
  var sessions = [];              // [{name,active,has_state}] from /api/mct/sessions
  var activeSession = null;       // server is the source of truth (loadSessions sets it)

  function loadSessions(cb) {
    fetch("/api/mct/sessions", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (j && j.sessions) { sessions = j.sessions; activeSession = j.active; }
        renderBar();
        if (cb) cb();
      })
      .catch(function () { if (cb) cb(); });
  }

  function renderSession() {
    var sel = pane.querySelector("#fv-session");
    var nb = pane.querySelector("#fv-session-new");
    if (!sel) return;
    if (surface !== "frontier") { sel.className = "hidden"; if (nb) nb.className = "hidden"; return; }
    sel.className = ""; if (nb) nb.className = "";
    sel.innerHTML = "";
    var names = sessions.length ? sessions : [{ name: activeSession || "repl", active: true, has_state: true }];
    for (var i = 0; i < names.length; i++) {
      var s = names[i], opt = document.createElement("option");
      opt.value = s.name;
      opt.textContent = s.name + (s.has_state ? "" : " \u00b7new");
      if (s.name === activeSession) opt.selected = true;
      sel.appendChild(opt);
    }
  }

  // A session (workspace) change needs a FRESH process only for the mct
  // backend (the workspace is baked into its launch command); killing whatever
  // else is live — e.g. the claude-code seat — destroyed its conversation for
  // nothing (2026-08-27).
  function resessionFrontier() {
    if (surface !== "frontier") { show("frontier"); return; }
    if (backendFor("frontier") === "mct") restart("frontier");
    else show("frontier");
  }

  function selectSession(name) {
    if (!name || name === activeSession) return;
    fetch("/api/mct/sessions", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op: "select", name: name }) })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.sessions) { sessions = j.sessions; activeSession = j.active; }
        renderBar();
        resessionFrontier();
      })
      .catch(function () {});
  }

  function newSession() {
    var name = (window.prompt("New frontier session name (A-Za-z0-9_-):", "") || "").trim();
    if (!name) return;
    fetch("/api/mct/sessions", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op: "create", name: name }) })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.error) { window.alert(j.error); return; }
        if (j && j.sessions) { sessions = j.sessions; activeSession = j.active; }
        renderBar();
        resessionFrontier();
      })
      .catch(function () {});
  }

  function backendFor(s) {
    var spec = SURFACES[s] || {};
    var b = chosen[s] || spec["default"] || "";
    if (spec.backends && spec.backends[b] === undefined) b = spec["default"] || "";
    return b;
  }

  function sizeOf(v) {
    if (v && v.ws && v.ws.readyState === 1)
      v.ws.send(JSON.stringify({ t: "size", rows: v.term.rows, cols: v.term.cols }));
  }

  function setStatus() {
    var dot = document.getElementById("fv-dot");
    var st = document.getElementById("fv-status");
    var v = views[surface];
    var live = v && v.ws && v.ws.readyState === 1;
    dot.classList.toggle("ok", !!live);
    var ground = (activeVm && activeVm !== "@keeper") ? activeVm : "host";
    var shellName = (backendFor("shell") === "ssh"
                     && activeVm && activeVm !== "@keeper") ? "ssh" : "shell";
    st.textContent = surface === "shell"
      ? (activeVm === "@keeper" ? "keeper@host"
         : activeVm ? shellName + "@" + activeVm : "shell@host")
        + (live ? "" : " (disconnected)")
      : surface + ":" + backendLabel(backendFor(surface)) + "@" + ground
        + (live ? "" : " (disconnected)");
  }

  function renderBar() {
    renderSession();
    var sws = pane.querySelectorAll(".fv-sw");
    for (var i = 0; i < sws.length; i++) {
      var el = sws[i], s = el.getAttribute("data-s");
      el.className = "fv-sw" + (s === surface ? " on" : "") +
        (s === "frontier" && !frontierEnabled ? " gated" : "");
      el.title = SURFACE_TIPS[s] || "";
    }
    var gate = pane.querySelector("#fv-gate");
    gate.textContent = frontierEnabled ? "A: on" : "A: off";
    gate.className = frontierEnabled ? "" : "off";
    /* B gate chip: a real, server-backed allow/block switch for B (the local
       keeper), default ON. It is fully independent of the frontier seat —
       the old chip aliased the backend picker (B:off ⇔ claude-code) and
       flipping it RESTARTED the frontier surface; that coupling is gone. */
    var bgate = pane.querySelector("#fv-bgate");
    bgate.textContent = bEnabled ? "B: on" : "B: off";
    bgate.className = bEnabled ? "" : "off";
    /* mct lane chips only make sense when the active surface is the mct
       pointer-exchange REPL — anywhere else they would type stray prefixes
       into a shell. */
    var lanesOn = surface === "frontier" && backendFor("frontier") === "mct";
    var lanes = pane.querySelectorAll(".fv-lane");
    for (var li = 0; li < lanes.length; li++)
      lanes[li].className = "fv-lane" + (lanesOn ? "" : " hidden");
    var sel = pane.querySelector("#fv-backend");
    var spec = SURFACES[surface] || { backends: {} };
    var keys = Object.keys(spec.backends || {});
    if (!keys.length) { sel.className = "hidden"; sel.innerHTML = ""; setStatus(); return; }
    sel.className = "";
    sel.innerHTML = "";
    var current = backendFor(surface);
    for (var k = 0; k < keys.length; k++) {
      var key = keys[k], info = spec.backends[key] || {};
      var opt = document.createElement("option");
      opt.value = key;
      opt.textContent = backendLabel(key) + (key === spec["default"] ? " (default)" : "") +
        (info.available === false ? " — not installed" : "");
      opt.disabled = info.available === false;
      opt.selected = key === current;
      sel.appendChild(opt);
    }
    setStatus();
  }

  /* Copy must WORK from every terminal pane — VISIBLY (operator 2026-08-27:
     "i need to copy from this app in the terminal anywhere"). Three rules:
     1. A busy TUI (claude-code, the REPLs) redraws constantly and xterm drops
        the live selection on redraw — so the last NON-EMPTY selection is kept
        in _fvSelLast, and Ctrl/Cmd+Shift+C copies it even after the highlight
        has already been wiped.
     2. Copy-on-select waits 250ms for the drag to settle (no partial-drag
        clipboard spam), then copies and TOASTS "copied N chars ✓" bottom-
        right — silence meant nobody could tell whether copy worked at all.
     3. navigator.clipboard is tried whenever it exists (not only on secure
        origins), falling back to the execCommand textarea trick; the toast
        reports which outcome actually happened. Plain Ctrl+C stays SIGINT. */
  var _fvSelLast = "";
  var _fvToastEl = null, _fvToastTimer = null;
  function fvToast(msg, bad) {
    if (!_fvToastEl) {
      _fvToastEl = document.createElement("div");
      _fvToastEl.style.cssText = "position:fixed;right:10px;bottom:10px;" +
        "z-index:9999;padding:4px 10px;border-radius:6px;pointer-events:none;" +
        "font:12px ui-monospace,monospace;transition:opacity .4s;opacity:0;" +
        "border:1px solid #2b567a;background:#16324a;color:#9fd4ff;";
      document.body.appendChild(_fvToastEl);
    }
    _fvToastEl.textContent = msg;
    _fvToastEl.style.background = bad ? "#4a1616" : "#16324a";
    _fvToastEl.style.borderColor = bad ? "#7a2b2b" : "#2b567a";
    _fvToastEl.style.color = bad ? "#ff9f9f" : "#9fd4ff";
    _fvToastEl.style.opacity = "1";
    clearTimeout(_fvToastTimer);
    _fvToastTimer = setTimeout(function () { _fvToastEl.style.opacity = "0"; }, 1400);
  }
  function fvCopyFallback(t) {
    var ta = document.createElement("textarea");
    ta.value = t; ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;";
    var prev = document.activeElement;
    document.body.appendChild(ta); ta.focus(); ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    try { if (prev && prev.focus) prev.focus(); } catch (e) {}
    return ok;
  }
  function fvCopy(t, announce) {
    if (!t) { if (announce) fvToast("nothing selected to copy", true); return; }
    _fvSelLast = t;
    var done = function (ok) {
      if (announce) fvToast(ok ? "copied " + t.length + " chars ✓"
                               : "copy FAILED — select again", !ok);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(
        function () { done(true); },
        function () { done(fvCopyFallback(t)); });
    } else done(fvCopyFallback(t));
  }

  function view(s) {
    if (views[s]) return views[s];
    var el = document.createElement("div");
    el.className = "fv-view";
    document.getElementById("fv-term-host").appendChild(el);
    var term = new Terminal({ fontSize: 13, cursorBlink: true,
      theme: { background: "#0d1117", foreground: "#e6e9ef" } });
    var fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(el);
    var v = views[s] = { el: el, term: term, fit: fit, ws: null };
    term.onData(function (d) {
      if (!v.ws || v.ws.readyState !== 1) return;
      if (v._inCopyScroll) {
        // typing jumps back live: cancel copy-mode first — the server works
        // its ws messages in order, so the cancel lands before the keystroke.
        v._inCopyScroll = false;
        try { v.ws.send(JSON.stringify({ t: "scroll", cancel: true })); } catch (e) {}
      }
      v.ws.send(enc.encode(d));
    });
    // Wheel → tmux copy-mode scroll (model surfaces only: their backends live
    // in tmux with mouse OFF — selection-copy depends on that — and prefix
    // None, so the scroll ctl is the ONLY way into pane history. Shell PTYs
    // keep xterm's native scrollback). capture:true runs before xterm's own
    // wheel handling, which would otherwise turn the wheel into arrow keys.
    if (s === "frontier" || s === "local") {
      v._whAcc = 0; v._whT = null;
      el.addEventListener("wheel", function (e) {
        if (!v.ws || v.ws.readyState !== 1) return;
        e.preventDefault(); e.stopPropagation();
        v._whAcc += (e.deltaMode === 1 ? e.deltaY : e.deltaY / 20);
        if (v._whT) return;
        v._whT = setTimeout(function () {
          var n = Math.round(-v._whAcc);   // wheel up (negative deltaY) = back
          v._whAcc = 0; v._whT = null;
          if (!n || !v.ws || v.ws.readyState !== 1) return;
          if (n > 0) v._inCopyScroll = true;
          try { v.ws.send(JSON.stringify({ t: "scroll", n: n })); } catch (err) {}
        }, 80);
      }, { passive: false, capture: true });
    }
    term.onSelectionChange(function () {
      var t = term.getSelection();
      if (!t) return;
      _fvSelLast = t;             // survives a TUI redraw wiping the highlight
      clearTimeout(v._selDeb);    // settle: copy once the drag stops changing
      v._selDeb = setTimeout(function () { fvCopy(t, true); }, 250);
    });
    term.attachCustomKeyEventHandler(function (e) {
      if (e.type === "keydown" && (e.ctrlKey || e.metaKey) && e.shiftKey
          && (e.key === "C" || e.key === "c")) {
        // live selection first; else the last one a redraw already wiped
        fvCopy(term.getSelection() || _fvSelLast, true);
        return false;             // handled here — not sent to the PTY
      }
      return true;
    });
    return v;
  }

  /* A-failure watch (operator, 2026-08-12): when the frontier terminal prints
     mct's failure banner, raise `fv-a-failed` so the SPA can bring up the 💬 B
     chat — temporary inference to get answers / sort out what happened while A
     is down. Guards: markers inside the reconnect scrollback REPLAY are
     ignored (old banners must not pop the drawer), and events are debounced. */
  var A_FAIL_MARK = "[A did not answer";
  function watchAFail(v, data) {
    try {
      var chunk = v._afDec.decode(data, { stream: true });
      // rolling PLAINTEXT context: the console-side B chat grounds in a HOST
      // workspace while the frontier session's real state lives in the VM, so
      // the seeded prompt must carry its own evidence — the operator's
      // message, the failure banner, and any inline B reply are all here.
      v._afLog = ((v._afLog || "") + chunk).slice(-6000);
      var txt = v._afTail + chunk;
      var hit = txt.indexOf(A_FAIL_MARK) !== -1;
      v._afTail = txt.slice(-96);            // marker may split across chunks
      if (!hit) return;
      var now = Date.now();
      if (now < v._afArmedAt) return;        // still inside the replay burst
      if (v._afFiredAt && now - v._afFiredAt < 30000) return;
      v._afFiredAt = now;
      var tail = v._afLog
        .replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)/g, "")   // OSC sequences
        .replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, "")        // CSI colors/moves
        .replace(/\r/g, "")
        .replace(/[\x00-\x08\x0b-\x1f]/g, "");
      window.dispatchEvent(new CustomEvent("fv-a-failed", { detail: { tail: tail } }));
    } catch (e) {}
  }

  function connect(s) {
    var v = view(s);
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var mine = new WebSocket(proto + "://" + location.host + "/wsterm" +
      "?rows=" + v.term.rows + "&cols=" + v.term.cols +
      "&surface=" + encodeURIComponent(s) +
      "&backend=" + encodeURIComponent(backendFor(s)) +
      (s === "frontier" && activeSession ? "&session=" + encodeURIComponent(activeSession) : "") +
      (activeVm ? "&vm=" + encodeURIComponent(activeVm) : ""));   // grounding: every surface follows the active VM
    v.ws = mine;
    mine.binaryType = "arraybuffer";
    v._afDec = new TextDecoder("utf-8"); v._afTail = ""; v._afLog = "";
    v._afArmedAt = Date.now() + 2000;        // scrollback replay window
    mine.onopen = function () { if (v.ws === mine) { sizeOf(v); setStatus(); } };
    mine.onclose = function () { if (v.ws === mine) setStatus(); };
    mine.onmessage = function (e) {
      if (v.ws !== mine) return;
      if (typeof e.data === "string") {
        try {
          var ctl = JSON.parse(e.data);
          if (ctl.t === "error" && ctl.msg)
            v.term.write("\r\n\x1b[2m[" + ctl.msg + "]\x1b[0m\r\n");
          if (ctl.t === "exit")
            v.term.write("\r\n\x1b[2m[session ended]\x1b[0m\r\n");
        } catch (err) {}
        return;
      }
      if (s === "frontier") watchAFail(v, e.data);
      v.term.write(new Uint8Array(e.data));
    };
  }

  function show(s) {
    surface = s;
    localStorage.setItem("fv-surface", surface);
    try { window.dispatchEvent(new CustomEvent("fv-surface", { detail: s })); } catch (e) {}
    for (var k in views) views[k].el.classList.toggle("on", k === s);
    var v = view(s);
    v.el.classList.add("on");
    if (!v.ws || v.ws.readyState === 3 /* CLOSED */) {
      // reattach: the server-side session persists and replays scrollback,
      // so clear the stale copy first to avoid doubling it.
      if (v.ws) v.term.reset();
      connect(s);
    }
    renderBar();
    setTimeout(function () { v.fit.fit(); sizeOf(v); v.term.focus(); }, 30);
  }

  function restart(s) {
    // EXPLICIT relaunch of this surface's backend: kill its own tmux session
    // and start fresh. Reserved for the relaunch command — a mere backend
    // switch must use detach() below, or it destroys the conversation it is
    // switching away from (the 2026-08-27 "session keeps resetting" bug).
    var v = view(s);
    if (v.ws) {
      var old = v.ws;
      v.ws = null;
      try { if (old.readyState === 1) old.send(JSON.stringify({ t: "kill" })); } catch (e) {}
      try { old.close(); } catch (e) {}
    }
    v.term.reset();
    connect(s);
    setTimeout(function () { v.fit.fit(); sizeOf(v); v.term.focus(); }, 30);
  }

  function detach(s) {
    // backend change: release this PTY only — server-side the old backend's
    // own tmux session stays alive (each backend has its own; switching back
    // reattaches with scrollback), and connect() attaches the new one.
    var v = view(s);
    if (v.ws) {
      var old = v.ws;
      v.ws = null;
      try { if (old.readyState === 1) old.send(JSON.stringify({ t: "detach" })); } catch (e) {}
      try { old.close(); } catch (e) {}
    }
    v.term.reset();
    connect(s);
    setTimeout(function () { v.fit.fit(); sizeOf(v); v.term.focus(); }, 30);
  }

  function start() {
    if (started) return;
    if (typeof Terminal === "undefined") { console.warn("xterm not loaded"); return; }
    started = true;
    renderBar();
    loadSessions();
    show(surface);
    window.addEventListener("resize", function () {
      if (pane.classList.contains("closed")) return;
      var v = views[surface];
      if (v) { v.fit.fit(); sizeOf(v); }
    });
  }

  function open() {
    pane.classList.remove("closed");
    tab.classList.remove("show");
    start();
    var v = views[surface];
    setTimeout(function () {
      if (v) { v.fit.fit(); sizeOf(v); v.term.focus(); }
    }, 60);
  }
  function close() {
    pane.classList.add("closed");
    tab.classList.add("show");
  }

  function refreshMeta() {
    // availability follows the grounding: probe the active VM's seats
    var q = (activeVm && activeVm !== "@keeper")
      ? "?vm=" + encodeURIComponent(activeVm) : "";
    fetch("/api/term/backends" + q, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || !j.frontier) return;
        frontierEnabled = j.frontier_enabled !== false;
        delete j.frontier_enabled;
        bEnabled = j.b_enabled !== false;   // absent (old backend) = ON
        delete j.b_enabled;
        SURFACES = j;
        renderBar();
      })
      .catch(function () {});
  }

  function mount() {
    document.body.appendChild(pane);
    document.body.appendChild(tab);
    pane.querySelector("#fv-term-hide").addEventListener("click", close);
    tab.addEventListener("click", open);
    var sws = pane.querySelectorAll(".fv-sw");
    for (var i = 0; i < sws.length; i++) {
      sws[i].addEventListener("click", function (ev) {
        var s = ev.target.getAttribute("data-s");
        if (s && s !== surface) show(s);
      });
    }
    pane.querySelector("#fv-backend").addEventListener("change", function (ev) {
      chosen[surface] = ev.target.value;
      localStorage.setItem("fv-backends", JSON.stringify(chosen));
      detach(surface);   // never kill: the old backend's session survives
    });
    pane.querySelector("#fv-session").addEventListener("change", function (ev) {
      selectSession(ev.target.value);
    });
    pane.querySelector("#fv-session-new").addEventListener("click", newSession);
    // geometry: pin to the console's real header (nav) and status bar (floor);
    // best-effort measurement of full-width fixed bars, with the defaults as
    // the floor values.
    function rcDrawerWidth() {
      // the right column (.design-panel): flex-basis var(--rcw,44%), clamped
      // to [360px, var(--rcmax,660px)]; the vars move with the ⇔ width modes.
      var cs = getComputedStyle(document.documentElement);
      var rcw = parseFloat(cs.getPropertyValue("--rcw")) || 44;
      var rcmax = parseFloat(cs.getPropertyValue("--rcmax")) || 660;
      return Math.min(Math.max(innerWidth * rcw / 100, 360), rcmax);
    }
    function measure() {
      var top = TOP, bot = BOT;
      var els = document.body.children;
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        if (el === pane || el === tab || !el.getBoundingClientRect) continue;
        var cs;
        try { cs = getComputedStyle(el); } catch (e) { continue; }
        if (cs.position !== "fixed" && cs.position !== "sticky") continue;
        var r = el.getBoundingClientRect();
        if (r.height === 0 || r.height > 120 || r.width < innerWidth * 0.9) continue;
        if (r.top <= 1) top = Math.max(top, r.bottom);
        if (Math.abs(r.bottom - innerHeight) <= 1)
          bot = Math.max(bot, innerHeight - r.top);
      }
      pane.style.top = top + "px";
      pane.style.bottom = bot + "px";
      if (!localStorage.getItem("fv-width")) {
        // default width: touch the right-hand column's edge (a manual drag,
        // once made, wins from then on).
        var w = Math.max(innerWidth * 0.2, innerWidth - rcDrawerWidth());
        pane.style.width = Math.round(w) + "px";
        var v = views[surface];
        if (v) { v.fit.fit(); sizeOf(v); }
      }
    }
    measure();
    window.addEventListener("resize", measure);
    // the ⇔ width modes toggle classes on <html>; follow the drawer's new edge
    new MutationObserver(measure).observe(document.documentElement,
      { attributes: true, attributeFilter: ["class"] });

    // drag the right edge: the terminal's width is wherever you set the
    // boundary with the right-hand column; persisted across sessions.
    var drag = pane.querySelector("#fv-term-drag");
    drag.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      drag.setPointerCapture(e.pointerId);
      function mv(ev) {
        var pct = Math.min(85, Math.max(15, 100 * ev.clientX / innerWidth));
        if (window.__applySplit) { window.__applySplit(pct); }   // P4: unify left+right = 100%
        else { pane.style.width = pct.toFixed(1) + "%"; }
      }
      function up() {
        drag.removeEventListener("pointermove", mv);
        drag.removeEventListener("pointerup", up);
        localStorage.setItem("fv-width", pane.style.width);
        var v = views[surface];
        if (v) { v.fit.fit(); sizeOf(v); }
      }
      drag.addEventListener("pointermove", mv);
      drag.addEventListener("pointerup", up);
    });

    // prompt → the proper receiver: whichever surface is active (or an
    // explicit one via window.__fvSend(text, surface) — e.g. prompt studio).
    function sendPrompt(text, s) {
      s = s || surface;
      if (!views[s] || !views[s].ws || views[s].ws.readyState !== 1) show(s);
      var v = views[s];
      if (v && v.ws && v.ws.readyState === 1) {
        v.ws.send(enc.encode(text + "\r"));
        return true;
      }
      return false;
    }
    window.__fvSend = sendPrompt;
    // (the old one-line "prompt → frontier" bar is retired: the ✍ prompt
    // panel and the ▤ /cmds palette are the two sanctioned input surfaces)

    pane.querySelector("#fv-bgate").addEventListener("click", function () {
      // Flip the server-backed B gate. NOTHING else: no backend switch, no
      // restart — an old backend without the endpoint leaves the chip as-is.
      var want = !bEnabled;
      fetch("/api/term/bgate", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: want })
      }).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          if (j && j.ok) { bEnabled = j.enabled; renderBar(); }
        })
        .catch(function () {});
    });
    // mct2 lane chips: a click TYPES the lane prefix into the REPL as plain
    // keystrokes (no Enter) and focuses the terminal — the wire carries
    // exactly what a base-terminal user would type (!msg / +msg / todo: msg),
    // so the protocol works identically without the UI.
    var laneEls = pane.querySelectorAll(".fv-lane");
    for (var le = 0; le < laneEls.length; le++) {
      laneEls[le].addEventListener("click", function () {
        var prefix = this.getAttribute("data-lane");
        var v = views.frontier;
        if (v && v.ws && v.ws.readyState === 1) {
          v.ws.send(enc.encode(prefix));
          v.term.focus();
        }
      });
    }
    pane.querySelector("#fv-gate").addEventListener("click", function () {
      var want = !frontierEnabled;
      fetch("/api/term/frontier", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: want })
      }).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          if (j && j.ok) { frontierEnabled = j.enabled; renderBar(); }
        })
        .catch(function () {});
    });
    refreshMeta();
    open();                       // terminal-first: auto-open on load
  }
  window.__fvSurface = {   // the top bar drives the terminal surface through this
    set: function (s) { if (SURFACES[s]) show(s); },
    get: function () { return surface; },
    list: function () { return Object.keys(SURFACES); }
  };
  window.__fvVm = {   // the SPA reports its active VM; EVERY surface follows it
    set: function (vm) {
      vm = vm || "";
      if (vm === activeVm) return;
      activeVm = vm;
      // Grounding rule: a VM's A/B/local/shell are ITS OWN — all three
      // surfaces reground on a VM switch, each to that VM's persistent
      // server-side session (detach, never kill: scrollback survives).
      for (var s in views) {
        var v = views[s];
        if (!v) continue;
        if (v.ws) {
          var old = v.ws; v.ws = null;
          try { old.close(); } catch (e) {}
        }
        v.term.reset();
      }
      if (views[surface]) { connect(surface); setStatus(); }
      refreshMeta();                        // availability is per-VM now
    },
    get: function () { return activeVm; }
  };
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);

  /* ── /cmds palette — a UI over the mct REPL's slash-command suite ────────
     Every command the frontier REPL understands, as a dropdown + typed inputs,
     sent into the live PTY via __fvSend. The manifest mirrors repl.py's
     dispatch table (the server remains the source of truth — this is a
     convenience surface, not a second implementation); /help in the terminal
     always shows the authoritative list. Targets the frontier surface (mct);
     the button dims on other surfaces/backends where these commands mean
     nothing. */
  var MCT_CMDS = [
    { g: "inspect", c: "/tail", tip: "last N lines of the rolling log",
      args: [{ ph: "20", def: "20" }] },
    { g: "inspect", c: "/log", tip: "show a transcript: A's, B's, or the whole ledger",
      args: [{ opts: ["a", "b", "all"] }] },
    { g: "inspect", c: "/trace", tip: "the last turn, step by step", args: [] },
    { g: "inspect", c: "/acache", tip: "A's durable transcript cache", args: [] },
    { g: "inspect", c: "/tokens", tip: "token + cost accounting", args: [] },
    { g: "inspect", c: "/metrics", tip: "session metrics", args: [] },
    { g: "inspect", c: "/map", tip: "context map — what A can currently see", args: [] },
    { g: "inspect", c: "/where", tip: "locate an object by id",
      args: [{ ph: "object id" }] },
    { g: "inspect", c: "/memory", tip: "B's memory state", args: [] },
    { g: "inspect", c: "/save-logs", tip: "write session logs to disk", args: [] },
    { g: "sources", c: "/sources", tip: "list whitelisted sources", args: [] },
    { g: "sources", c: "/root", tip: "whitelist a directory root for A pulls",
      args: [{ ph: "alias" }, { ph: "/path/to/dir" }] },
    { g: "sources", c: "/file", tip: "whitelist a single file",
      args: [{ ph: "alias" }, { ph: "/path/to/file" }] },
    { g: "sources", c: "/source", tip: "inspect/adjust one source",
      args: [{ ph: "alias" }, { ph: "(optional path)", opt: true }] },
    { g: "config", c: "/model", tip: "set A's model — pick from the list, or choose custom… to type one",
      args: [{ ph: "model id", combo: "a" }] },
    { g: "config", c: "/bmodel", tip: "set B's model — the fleet's live registry (ready first), or custom…",
      args: [{ ph: "fleet model id", combo: "b", wide: true }] },
    { g: "config", c: "/frontier", tip: "gate A launches on/off",
      args: [{ opts: ["on", "off"] }] },
    { g: "config", c: "/fsreq", tip: "may A request extra files? (B still brokers everything)",
      args: [{ opts: ["on", "off"] }] },
    { g: "config", c: "/policy", tip: "show the active policy", args: [] },
    { g: "b-channel", c: "/b", tip: "speak to B directly (never relayed to A)",
      args: [{ ph: "message to B", wide: true }] },
    { g: "b-channel", c: "/bstate", tip: "B's state", args: [] },
    { g: "session", c: "/help", tip: "authoritative command list", args: [] },
    { g: "session", c: "sign in / relaunch A", special: "relaunch",
      tip: "restart the frontier surface — if claude isn't signed in there, its " +
           "OAuth sign-in flow starts right in the tab (URL prints, clickable)",
      args: [] },
    { g: "session", c: "/exit", tip: "end the REPL session", args: [] },
    { g: "config", c: "/native", tip: "A's native Claude Code tools (all = A can bypass B)",
      args: [{ opts: ["none", "off_host", "all"] }] },
    { g: "sources", c: "/files", tip: "list registered files", args: [] },
    { g: "inspect", c: "/quiet", tip: "toggle the inline A/B relay", args: [] }
  ];

  var pcss = document.createElement("style");
  pcss.textContent =
    "#fv-cmds-btn{cursor:pointer;padding:1px 7px;border-radius:4px;color:#8b949e;" +
    "border:1px solid #30363d;user-select:none}" +
    "#fv-cmds-btn.on{color:#e6e9ef;background:#21262d}" +
    "#fv-cmds-btn.dim{opacity:.45}" +
    "#fv-cmds{position:absolute;top:26px;left:0;right:0;z-index:5;display:none;" +
    "background:#161b22;border-bottom:1px solid #30363d;padding:8px 10px;" +
    "font:12px system-ui,sans-serif;color:#b6bec9;" +
    "display:none;gap:8px;align-items:center;flex-wrap:wrap}" +
    "#fv-cmds.open{display:flex}" +
    "#fv-cmds select,#fv-cmds input{background:#0d1117;color:#e6e9ef;" +
    "border:1px solid #30363d;border-radius:4px;font:12px system-ui,sans-serif;" +
    "padding:2px 6px}" +
    "#fv-cmds input.wide{flex:1 1 160px;min-width:120px}" +
    "#fv-cmds select.wide{max-width:280px}" +
    "#fv-cmds button{background:#238636;color:#fff;border:1px solid #2ea043;" +
    "border-radius:4px;padding:2px 10px;cursor:pointer;font:12px system-ui,sans-serif}" +
    "#fv-cmds .tip{flex-basis:100%;color:#8b949e;font-size:11px;margin-top:2px}";
  document.head.appendChild(pcss);

  function mountPalette() {
    var bar = document.getElementById("fv-term-bar");
    if (!bar) { setTimeout(mountPalette, 400); return; }
    var btn = document.createElement("span");
    btn.id = "fv-cmds-btn";
    btn.textContent = "▤ /cmds";
    btn.title = "command palette for the frontier (mct) REPL";
    var hide = document.getElementById("fv-term-hide");
    bar.insertBefore(btn, hide);

    var panel = document.createElement("div");
    panel.id = "fv-cmds";
    var sel = document.createElement("select");
    var groups = {};
    for (var i = 0; i < MCT_CMDS.length; i++) {
      var m = MCT_CMDS[i];
      if (!groups[m.g]) {
        groups[m.g] = document.createElement("optgroup");
        groups[m.g].label = m.g;
        sel.appendChild(groups[m.g]);
      }
      var o = document.createElement("option");
      o.value = i; o.textContent = m.c;
      groups[m.g].appendChild(o);
    }
    var argsHost = document.createElement("span");
    argsHost.style.display = "inline-flex";
    argsHost.style.gap = "6px";
    argsHost.style.flexWrap = "wrap";

    /* combo args: a REAL dropdown of the known choices — no typing needed —
       with one last "type a custom value…" row that swaps the select for a
       free-text input (Esc goes back to the list). The list is the fluent
       path; custom is the escape hatch for anything the menu doesn't offer.
       A's choices are the claude aliases; B's come from the fleet's live
       model registry (/api/llm/models), ready models first. */
    var COMBO_SRC = {
      a: function (cb) { cb([{ v: "sonnet" }, { v: "opus" }, { v: "haiku" }]); },
      b: function (cb) {
        fetch("/api/llm/models", { credentials: "same-origin" })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (j) {
            var rows = ((j && j.models) || []).filter(function (m2) { return m2.key; });
            rows.sort(function (x, y) { return (y.ready === true) - (x.ready === true); });
            cb(rows.map(function (m2) {
              return { v: m2.key,
                       t: m2.key + (m2.ready ? "" : " — not loaded") };
            }));
          })
          .catch(function () { cb([]); });
      }
    };
    function fillCombo(el, rows) {
      el.innerHTML = "";
      for (var r = 0; r < rows.length; r++) {
        var op = document.createElement("option");
        op.value = rows[r].v;
        op.textContent = rows[r].t || rows[r].v;
        el.appendChild(op);
      }
      var cu = document.createElement("option");
      cu.value = "";                       // an unswitched run never sends it
      cu.setAttribute("data-custom", "1");
      cu.textContent = "type a custom value…";
      el.appendChild(cu);
      // an empty registry (fetch failed / nothing loaded) leaves only custom —
      // land on it so the input appears immediately instead of a bare menu
      if (!rows.length) { el.selectedIndex = 0; el.onchange && el.onchange(); }
    }
    function comboArg(spec, idx) {
      var wrap = document.createElement("span");
      wrap.style.display = "inline-flex"; wrap.style.gap = "4px";
      var el = document.createElement("select");
      if (spec.wide) el.className = "wide";
      var inp = document.createElement("input");
      inp.placeholder = (spec.ph || "custom value") + " — Esc: back to the list";
      if (spec.wide) inp.className = "wide";
      inp.style.display = "none";
      // exactly ONE of the pair carries data-arg, so runCmd reads the live one
      el.setAttribute("data-arg", idx);
      el.onchange = function () {
        var so = el.selectedOptions && el.selectedOptions[0];
        var toCustom = !!(so && so.getAttribute("data-custom"));
        inp.style.display = toCustom ? "" : "none";
        el.style.display = toCustom ? "none" : "";
        if (toCustom) { el.removeAttribute("data-arg");
                        inp.setAttribute("data-arg", idx); inp.focus(); }
      };
      inp.addEventListener("keydown", function (ev) {
        if (ev.key === "Escape") {
          ev.stopPropagation();
          inp.removeAttribute("data-arg"); inp.style.display = "none";
          el.setAttribute("data-arg", idx); el.style.display = "";
          el.selectedIndex = 0;
        }
      });
      var loading = document.createElement("option");
      loading.textContent = "loading…"; loading.value = "";
      el.appendChild(loading);
      (COMBO_SRC[spec.combo] || function (cb) { cb([]); })(function (rows) {
        fillCombo(el, rows);
      });
      wrap.appendChild(el); wrap.appendChild(inp);
      return wrap;
    }
    var run = document.createElement("button");
    run.textContent = "run ↵";
    var tip = document.createElement("div");
    tip.className = "tip";
    panel.appendChild(sel); panel.appendChild(argsHost);
    panel.appendChild(run); panel.appendChild(tip);
    document.getElementById("fv-term-pane").appendChild(panel);

    function current() { return MCT_CMDS[parseInt(sel.value || "0", 10)] || MCT_CMDS[0]; }

    function renderArgs() {
      var m = current();
      argsHost.innerHTML = "";
      for (var a = 0; a < m.args.length; a++) {
        var spec = m.args[a], el;
        if (spec.combo) {                 // dropdown of known choices + custom…
          argsHost.appendChild(comboArg(spec, a));
          continue;
        }
        if (spec.opts) {
          el = document.createElement("select");
          for (var o2 = 0; o2 < spec.opts.length; o2++) {
            var op = document.createElement("option");
            op.value = op.textContent = spec.opts[o2];
            el.appendChild(op);
          }
        } else {
          el = document.createElement("input");
          el.placeholder = spec.ph || "";
          if (spec.def) el.value = spec.def;
          if (spec.wide) el.className = "wide";
        }
        el.setAttribute("data-arg", a);
        argsHost.appendChild(el);
      }
      tip.textContent = m.c + " — " + m.tip;
    }

    function runCmd() {
      var m = current();
      if (m.special === "relaunch") {
        // fresh PTY: frontier-launch's preflight runs again — with credentials
        // it goes straight to mct; without them the sign-in flow (URL) appears.
        show("frontier");
        restart("frontier");
        panel.classList.remove("open");
        btn.classList.remove("on");
        return;
      }
      var parts = [m.c];
      var els = argsHost.querySelectorAll("[data-arg]");
      for (var a = 0; a < els.length; a++) {
        var val = (els[a].value || "").trim();
        if (!val && !(m.args[a] && m.args[a].opt)) {
          if (m.args[a] && !m.args[a].opts) { els[a].focus(); return; }
        }
        if (val) parts.push(val);
      }
      // the suite belongs to the mct REPL: route to the frontier surface
      // (switching to it if needed) — __fvSend handles connect-on-demand.
      if (window.__fvSend) window.__fvSend(parts.join(" "), "frontier");
      panel.classList.remove("open");
      btn.classList.remove("on");
    }

    sel.addEventListener("change", renderArgs);
    run.addEventListener("click", runCmd);
    panel.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); runCmd(); }
      if (e.key === "Escape") { panel.classList.remove("open"); btn.classList.remove("on"); }
    });
    btn.addEventListener("click", function () {
      var opening = !panel.classList.contains("open");
      panel.classList.toggle("open", opening);
      btn.classList.toggle("on", opening);
      if (opening) {
        renderArgs();
        var first = argsHost.querySelector("input,select");
        (first || sel).focus();
      }
    });
    // dim the affordance when the active surface isn't the mct frontier —
    // the commands still route there, but make the target obvious.
    setInterval(function () {
      var st = document.getElementById("fv-status");
      var onMct = st && /^frontier:mct/.test(st.textContent || "");
      btn.classList.toggle("dim", !onMct);
    }, 1500);
    renderArgs();
  }
  mountPalette();

})();
