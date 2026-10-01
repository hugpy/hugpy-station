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
   surface. (The A/B gate chips, the ⧉ copy chip and the ▤ /cmds palette were
   deprecated 2026-09-17 — the keeper surface is the abstract-claude serve
   console; the Terminal seats are emergency-only. Copy still works from every
   pane via right-click / Ctrl-Cmd+Shift+C.)
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
    frontier: { "default": "mct",
                backends: { "mct": { available: true },
                            "claude-code": { available: true },
                            "codex": { available: true },
                            "hugpy": { available: true } } },
    shell:    { "default": "exec",
                backends: { "exec": { available: true },
                            "ssh":  { available: true } } }
  };
  var SURFACE_TIPS = {
    local:    "Local Keeper — talk to B (hugpy-agent) directly; works without A",
    frontier: "Frontier Keeper \u2014 default surface is the Serve console (abstract-claude serve at /ac/); the Terminal seats (Claude Code, ChatGPT via Codex, Hugpy agent, MCT pointer exchange) stay available here as optional choices",
    shell:    "Terminal Shell — your own shell; output enters model context only when you explicitly share it"
  };
  // Display names only — backend KEYS are identity (storage, URLs, the
  // server's whitelist) and never change. The local seat execs the OpenCode
  // TUI, but the seat itself is `hugpy-agent console` (operator, 2026-08-13):
  // it reads as "Hugpy Agent".
  // mct promotion (2026-08-27): the pointer-exchange arbiter IS mct now (the
  // original broker mct-deprecated was retired from the picker 2026-08-28).
  // t-serve (2026-09-16): the keeper surface on this locus is abstract-claude
  // serve (the /ac/ console pane). The frontier Terminal seats are kept and stay
  // selectable, but they are no longer the default — label them so the picker
  // says what they are. The server (/api/term/backends frontier.backend_labels)
  // is the source of truth and overrides these at runtime.
  // NAMING (2026-09-18): operator-facing name is "Terminal" (interactive-TUI
  // seat - a full-screen CLI painted through xterm.js over a WebSocket-fed PTY).
  // tmux is only the persistence substrate, NOT the category and NOT the freeze
  // cause (xterm.js RenderService; see forceRepaint). Wire token stays "tmux".
  var BACKEND_LABELS = { "opencode": "Hugpy Agent",
                         "codex": "Terminal \u00b7 ChatGPT Codex",
                         "claude-code": "Terminal \u00b7 Claude Code",
                         "hugpy": "Terminal \u00b7 Hugpy agent",
                         "mct": "Terminal \u00b7 MCT pointer exchange",
                         "serve": "Serve console" };
  var KEEPER_SURFACE = "serve";      // from /api/term/backends keeper_surface (of the ACTIVE locus)
  /* 1.0.101 (2026-09-17, vm_mgr): per-locus serve consoles. refreshMeta() probes
     /api/term/backends?vm=<active locus>; when the station reports that locus's
     OWN abstract-claude serve (ac_serve.path, e.g. /ac/@hugpy/ for the hugpy
     locus), the frontier view frames THAT console — same rules as the host seat.
     A locus without one keeps its tmux seat. Shared with the SPA's paneSrc()
     via window.__acPaths. */
  var AC_LOCI = {};                  // locus -> {path, ok, label}
  try { window.__acPaths = AC_LOCI; } catch (e) {}
  function acLocusKey() { return acHostSeat() ? "@keeper" : activeVm; }
  function acPathNow() {
    if (acHostSeat()) return "/ac/";
    var e = AC_LOCI[activeVm]; return e && e.path ? e.path : null;
  }
  function backendLabel(k) { return BACKEND_LABELS[k] || k; }
  // A/B gates (formerly frontierEnabled / bEnabled): the bar chips are gone
  // (deprecated 2026-09-17). The client treats both as always-enabled — their
  // old "default ON" — and never gates anything; the server-side
  // /api/term/frontier and /api/term/bgate endpoints still exist for scripts.
  // Active VM (set by the SPA via __fvVm): the shell surface follows it —
  // its PTY runs `station shell` INSIDE that VM. Server keeps one persistent
  // session per VM ("shell:<vm>"), so switching replays that VM's scrollback.
  var activeVm = "";
  var surface = localStorage.getItem("fv-surface") || "frontier";
  if (!SURFACES[surface]) surface = "frontier";
  var chosen = {};   // surface -> chosen backend key
  var frontierNative = localStorage.getItem("fv-frontier-native") || "claude-code";
  // SESSION-PULL-PATCH 2026-09-02: attached pulled/jump-in seats — "pull:<tmux>" -> {tmux}. Kept apart
  // from SURFACES (refreshMeta replaces that map from /api/term/backends).
  var PULLS = {};
  try { chosen = JSON.parse(localStorage.getItem("fv-backends") || "{}") || {}; }
  catch (e) { chosen = {}; }
  // MCT became the frontier default; migrate the prior implicit choice once.
  if (!localStorage.getItem("fv-frontier-default-mct-v1")) {
    chosen.frontier = "mct";
    localStorage.setItem("fv-backends", JSON.stringify(chosen));
    localStorage.setItem("fv-frontier-default-mct-v1", "1");
  }

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
    ".fv-lane{cursor:pointer;padding:1px 6px;border-radius:4px;user-select:none;" +
    "border:1px solid #30363d;color:#8b949e;font-family:ui-monospace,SFMono-Regular,monospace}" +
    ".fv-lane:hover{color:#e6e9ef;background:#21262d}" +
    "#fv-lane-bang{color:#d29922}" +
    ".fv-lane.hidden{visibility:hidden}" +   /* reserve space so the toolbar height/width is identical across backends (UI-stability) */
    /* 1.0.65: the backend picker is BUTTONS (operator): the chosen/displayed one is lit */
    "#fv-backend{display:inline-flex;gap:2px;align-items:stretch;flex:none}" +   /* never shrink -> long 'mct·pointer exchange' label can't be squeezed into a 2nd line */
    "#fv-backend button{background:#0d1117;color:#8b949e;border:1px solid #30363d;" +
    "border-radius:4px;font:12px system-ui,sans-serif;padding:1px 8px;cursor:pointer;white-space:nowrap}" +   /* keep each backend tab on one line (UI-stability) */
    "#fv-backend button:hover{color:#e6e9ef;background:#21262d}" +
    "#fv-backend button.on{color:#e6e9ef;background:#1f6feb;border-color:#388bfd}" +
    "#fv-backend button:disabled{opacity:.45;cursor:not-allowed}" +
    "#fv-backend.hidden{display:none}" +
    "#fv-session{background:#0d1117;color:#b6bec9;border:1px solid #30363d;" +
    "border-radius:4px;font:12px system-ui,sans-serif;max-width:320px}" +
    "#fv-session.hidden,#fv-session-new.hidden{display:none}" +
    "#fv-session-new{cursor:pointer;color:#8b949e;padding:0 4px;user-select:none}" +
    "#fv-term-drag{position:absolute;top:0;bottom:0;right:-3px;width:7px;" +
    "cursor:ew-resize;z-index:3}" +
    "#fv-term-hide{margin-left:auto;cursor:pointer;color:#8b949e}" +
    /* 1.0.143 ⌂ shell: a terminal on the CURRENT locus as its own user */
    "#fv-shell{flex:none;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" +
    "background:#0d1117;color:#8b949e;border:1px solid #30363d;border-radius:4px;" +
    "font:12px system-ui,sans-serif;padding:1px 8px;cursor:pointer}" +
    "#fv-shell:hover{color:#e6e9ef;background:#21262d}" +
    "#fv-shell.on{color:#e6e9ef;background:#1f6feb;border-color:#388bfd}" +
    "#fv-shell:disabled{opacity:.45;cursor:not-allowed}" +
    "#fv-term-host{flex:1;padding:4px;overflow:hidden;position:relative}" +
    ".fv-view{position:absolute;inset:4px;display:none}" +
    ".fv-view.on{display:block}" +
    /* paste bypass (operator 2026-09-17): a native browser paste into a real
       textarea, then one POST straight into the tmux pane — no Clipboard API,
       no secure context, no xterm keystroke path. */
    ".fv-pastebtn{position:absolute;right:8px;bottom:8px;z-index:6;cursor:pointer;font:11px ui-monospace,monospace;color:#9fd4ff;background:#16324a;border:1px solid #2b567a;border-radius:6px;padding:2px 7px;opacity:.55}" +
    ".fv-pastebtn:hover{opacity:1}" +
    ".fv-pastebox{position:absolute;right:8px;bottom:32px;z-index:7;display:none;width:min(420px,70%);background:#0d1117;border:1px solid #2b567a;border-radius:8px;padding:6px;box-shadow:0 6px 24px rgba(0,0,0,.55)}" +
    ".fv-pastebox.on{display:block}" +
    ".fv-pastebox textarea{width:100%;height:96px;resize:vertical;box-sizing:border-box;background:#0d1117;color:#e6e9ef;border:1px solid #262d38;border-radius:6px;padding:5px;font:12px ui-monospace,monospace}" +
    ".fv-pastebox .row{display:flex;gap:6px;align-items:center;margin-top:5px;font:11px ui-monospace,monospace;color:#7d8794}" +
    ".fv-pastebox button{cursor:pointer;font:11px ui-monospace,monospace;color:#9fd4ff;background:#16324a;border:1px solid #2b567a;border-radius:6px;padding:3px 9px}" +
    "#fv-term-tab{position:fixed;left:0;top:50%;transform:translateY(-50%);" +
    "z-index:2147483000;background:#238636;color:#fff;border:1px solid #2ea043;" +
    "border-left:none;border-radius:0 8px 8px 0;padding:10px 6px;" +
    "font:13px system-ui,sans-serif;cursor:pointer;writing-mode:vertical-rl;" +
    "display:none}" +
    "#fv-term-tab.show{display:block}" +
    /* third row: the rolling-state banner (fleet judge's derived objective for
       this locus's frontier seat), full width, directly under #fv-term-bar */
    "#fv-roll-banner{flex:none;display:flex;align-items:center;gap:8px;" +
    "padding:2px 10px;min-height:20px;background:#0f1620;" +
    "border-bottom:1px solid #262d38;color:#8b949e;" +
    "font:11px system-ui,sans-serif;white-space:nowrap;overflow:hidden}" +
    "#fv-roll-banner .fv-roll-k{color:#d29922;font-weight:600;flex:none}" +
    "#fv-roll-banner .fv-roll-meta{color:#6e7681;flex:none}" +
    "#fv-roll-banner .fv-roll-obj{color:#b6bec9;overflow:hidden;text-overflow:ellipsis}" +
    "#fv-roll-banner.warn .fv-roll-obj{color:#f0883e}" +
    /* fourth row (1.0.118): the ⚠ loop strip — crash/retry loops the station
       detected (GET /api/loops). Hidden when the set is empty; one row per
       loop, never a toast per event. */
    "#fv-loop-strip{flex:none;display:none;flex-direction:column;gap:1px;" +
    "padding:2px 10px;background:#2a1414;border-bottom:1px solid #7a2b2b;" +
    "color:#ffb3b3;font:11px system-ui,sans-serif;max-height:96px;overflow:auto}" +
    "#fv-loop-strip.on{display:flex}" +
    "#fv-loop-strip .fv-loop{display:flex;align-items:center;gap:8px;white-space:nowrap;min-height:18px}" +
    "#fv-loop-strip .fv-loop-k{color:#ff9f9f;font-weight:600;flex:none}" +
    "#fv-loop-strip .fv-loop-src{color:#f0883e;flex:none;font-family:ui-monospace,monospace}" +
    "#fv-loop-strip .fv-loop-id{color:#ffd8d8;overflow:hidden;text-overflow:ellipsis;max-width:34%}" +
    "#fv-loop-strip .fv-loop-meta{color:#c08585;flex:none}" +
    "#fv-loop-strip .fv-loop-do{color:#e6e9ef;overflow:hidden;text-overflow:ellipsis;font-family:ui-monospace,monospace;flex:1}" +
    "#fv-loop-strip .fv-loop-copy{cursor:pointer;flex:none;color:#9fd4ff;background:#16324a;border:1px solid #2b567a;border-radius:5px;padding:0 6px;font:10px ui-monospace,monospace}" +
    "#fv-loop-strip .fv-loop.cleared{opacity:.55}";
  document.head.appendChild(css);

  var pane = document.createElement("div");
  pane.id = "fv-term-pane";
  // 2026-09-18 (operator): the frontier/keeper picker and session controls lead
  // the bar; the connection dot + status relay ("frontier:…@host") now sit to
  // their RIGHT (moved after the picker/session/lanes).
  pane.innerHTML =
    '<div id="fv-term-bar">' +
    '<span id="fv-backend" title="frontier communication mode — the serve console vs the tmux terminal seat"></span>' +
    '<select id="fv-session" title="frontier workspace and Claude, Codex, or Hugpy model - select to switch"></select>' +
    '<span id="fv-session-new" title="new frontier session">+</span>' +
    '<span class="fv-lane hidden" id="fv-lane-bang" data-lane="!" title="mct INTERRUPT lane: types ! at the REPL prompt — finish the line and Enter to kill A mid-turn; the next turn opens with a reconcile brief">!</span>' +
    '<span class="fv-lane hidden" id="fv-lane-plus" data-lane="+" title="mct APPEND lane: types + at the REPL prompt — your line gets its OWN turn, after current work and any pending digest">+</span>' +
    '<span class="fv-lane hidden" id="fv-lane-todo" data-lane="todo: " title="mct CAPTURE lane: types todo: at the REPL prompt — no turn spent; the line is appended to the workspace todo.md">todo:</span>' +
    '<span class="dot" id="fv-dot"></span>' +
    '<span id="fv-status">terminal</span>' +
    '<button type="button" id="fv-shell" title="open a shell on the selected locus as its own user">⌂ shell</button>' +
    '<span id="fv-term-hide" title="collapse">⇤ hide</span></div>' +
    '<div id="fv-roll-banner" title="rolling state — the fleet judge\'s derived objective for this locus\'s frontier seat">' +
    '<span class="fv-roll-k">🎯 rolling state</span>' +
    '<span class="fv-roll-meta"></span>' +
    '<span class="fv-roll-obj">…</span></div>' +
    '<div id="fv-loop-strip" title="crash / retry loops and log findings the station detected (GET /api/loops) — one row each; medium+ ones also mailed the keeper once and filed one keeper-board item"></div>' +
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
  var sessionModelChoices = { claude: [], hugpy: [], models: { "claude-code": "", codex: "" }, hugpyModel: "" };
  var sessionModelChoicesLoaded = false;

  function loadSessionModelChoices() {
    if (sessionModelChoicesLoaded) return;
    sessionModelChoicesLoaded = true;
    Promise.all([
      fetch("/api/frontier/models" + vmQ(), { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; }),
      fetch("/api/b/model" + vmQ(), { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; })
    ]).then(function (docs) {
      var f = docs[0] || {}, b = docs[1] || {};
      sessionModelChoices.models = f.models || sessionModelChoices.models;
      sessionModelChoices.claude = Array.isArray(f.choices) ? f.choices : [];
      sessionModelChoices.hugpy = Array.isArray(b.candidates) ? b.candidates.map(function (m) { return m.key; }).filter(Boolean) : [];
      sessionModelChoices.hugpyModel = b.model || "";
      renderSession();
    }).catch(function () { sessionModelChoicesLoaded = false; });
  }

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
    loadSessionModelChoices();
    function group(label) {
      var g = document.createElement("optgroup"); g.label = label; sel.appendChild(g); return g;
    }
    function addModelOptions(g, provider, choices, current, defaultLabel) {
      var list = (choices || []).slice();
      if (current && list.indexOf(current) < 0) list.unshift(current);
      if (provider === "codex") {
        ["gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gpt-5.2-codex"].forEach(function (id) {
          if (list.indexOf(id) < 0) list.push(id);
        });
      }
      if (!list.length || list.indexOf("") < 0) list.unshift("");
      list.forEach(function (id) {
        var opt = document.createElement("option");
        opt.value = "@model|" + provider + "|" + id;
        opt.textContent = id || defaultLabel;
        opt.title = "Select " + (id || defaultLabel) + " for " + provider;
        if (id === current) opt.textContent += " · selected";
        g.appendChild(opt);
      });
    }
    addModelOptions(group("Claude models"), "claude-code", sessionModelChoices.claude,
      sessionModelChoices.models["claude-code"] || "", "Account default");
    addModelOptions(group("Codex models"), "codex", [], sessionModelChoices.models.codex || "", "Account default");
    addModelOptions(group("Hugpy models"), "hugpy", sessionModelChoices.hugpy,
      sessionModelChoices.hugpyModel || "", "Package default");
  }

  function selectSessionModel(value) {
    var p = value.split("|"), provider = p[1], model = p.slice(2).join("|");
    var url, body;
    if (provider === "claude-code" || provider === "codex" || provider === "hugpy") {
      url = provider === "codex" ? "/api/gpt" : "/api/frontier/models" + vmQ();
      body = provider === "codex" ? { default_model: model } : { [provider]: model };
    } else return;
    fetch(url, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.ok ? r.json() : r.json().then(function (e) { throw new Error(e.error || "Model update failed"); }); })
      .then(function () {
        if (provider === "codex") sessionModelChoices.models.codex = model;
        else if (provider === "claude-code") sessionModelChoices.models["claude-code"] = model;
        else { sessionModelChoices.models.hugpy = model; sessionModelChoices.hugpyModel = model; }
        renderSession();
        var st = document.getElementById("fv-status");
        if (st) { var previous = st.textContent; st.textContent = (model || "default") + " selected"; setTimeout(function () { if (st.textContent.indexOf(" selected") >= 0) st.textContent = previous; }, 1800); }
      }).catch(function (e) { window.alert(e.message || "Could not update model"); renderSession(); });
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
    // mct (pointer-exchange) was retired from the keeper picker (2026-09-18): the
    // frontier "tmux" seat is the selected keeper provider now, never the mct
    // seat. Remap a legacy/stored mct choice so it can't be launched from the UI.
    if (s === "frontier" && b === "mct") b = frontierNative;
    return b;
  }

  function sizeOf(v) {
    if (v && v.ws && v.ws.readyState === 1)
      v.ws.send(JSON.stringify({ t: "size", rows: v.term.rows, cols: v.term.cols }));
  }

  /* 1.0.143 ⌂ shell (operator 2026-10-01: "the shell button must come back").
     One permanent button in the pane bar opens the SHELL surface on the
     CURRENT locus as that locus's own user — the host's service user here
     (e.g. vm_mgr@ae), an ssh locus's login user, an LXD guest's ubuntu. It is
     the existing persistent shell (ws_hostterm surface=shell → tmux -L shell
     in the locus via the 1.0.141 locus transport), not a new path. The label
     is the server's own answer (/api/term/backends shell.who); a locus the
     station cannot reach disables the button and the tooltip says why.
     Clicking it again while on the shell goes back to the frontier surface. */
  function shellInfo() { return (SURFACES && SURFACES.shell) || {}; }
  function renderShellBtn() {
    var b = document.getElementById("fv-shell");
    if (!b) return;
    var info = shellInfo(), who = info.who || "";
    var where = acHostSeat() ? "this host" : activeVm;
    var down = info.available === false;
    var on = surface === "shell" && !down;
    b.textContent = "⌂ " + (who || "shell");
    b.className = on ? "on" : "";
    b.disabled = down && surface !== "shell";
    b.title = down
      ? "shell unavailable on " + where + " — " + (info.reason || "the station cannot reach this locus")
      : on ? "back to the frontier surface (the shell keeps running; reopen it here)"
           : "open a terminal on " + where + (who ? " as " + who : "") +
             " (persistent: tmux -L shell in the locus; Shift+drag selects inside mouse-mode apps)";
  }

  function setStatus() {
    renderShellBtn();
    var dot = document.getElementById("fv-dot");
    var st = document.getElementById("fv-status");
    if (acOn()) {   // t296: the serve console is its own surface — no PTY, no dot to lie about
      if (dot) dot.classList.add("ok");
      if (st) st.textContent = "frontier:Serve console@" + (acHostSeat() ? "host" : activeVm);
      return;
    }
    var v = views[surface];
    var live = v && v.ws && v.ws.readyState === 1;
    dot.classList.toggle("ok", !!live);
    var ground = (activeVm && activeVm !== "@keeper") ? activeVm : "host";
    var shellName = (backendFor("shell") === "ssh"
                     && activeVm && activeVm !== "@keeper") ? "ssh" : "shell";
    st.textContent = surface === "shell"
      ? (shellInfo().who ? "shell:" + shellInfo().who
         : acHostSeat() ? "shell@host" : shellName + "@" + activeVm)
        + (live ? "" : " (disconnected)")
      : surface + ":" + backendLabel(backendFor(surface)) +
        (surface === "frontier" && backendFor(surface) === "mct" ? " → " + backendLabel(frontierNative) + " · conversation" : "") + "@" + ground
        + (live ? "" : " (disconnected)");
  }

  function renderBar() {
    renderSession();
    var sws = pane.querySelectorAll(".fv-sw");
    for (var i = 0; i < sws.length; i++) {
      var el = sws[i], s = el.getAttribute("data-s");
      el.className = "fv-sw" + (s === surface ? " on" : "");
      el.title = SURFACE_TIPS[s] || "";
    }
    /* MCT mode exposes the pointer communication lanes on the selected native
       frontier seat. */
    var lanesOn = false; // Legacy standalone REPL controls do not apply to pointer exchange.
    var lanes = pane.querySelectorAll(".fv-lane");
    for (var li = 0; li < lanes.length; li++)
      lanes[li].className = "fv-lane" + (lanesOn ? "" : " hidden");
    var sel = pane.querySelector("#fv-backend");
    var spec = SURFACES[surface] || { backends: {} };
    var keys = Object.keys(spec.backends || {});
    // 1.0.143: exec-vs-ssh only means something for an LXD guest's shell; the
    // host / ssh-locus shell has exactly one way in, so no picker.
    if (surface === "shell" && spec.kind && spec.kind !== "lxc") keys = [];
    if (!keys.length) { sel.className = "hidden"; sel.innerHTML = ""; setStatus(); return; }
    sel.className = "";
    sel.innerHTML = "";
    var current = backendFor(surface);
    // Frontier picker: a provider dropdown (ChatGPT / Claude — which model backs
    // the terminal seat) plus a two-button keeper-mode switch, serve vs tmux.
    if (surface === "frontier") {
      if (acOn()) {
        var modelPicker = document.createElement("select");
        modelPicker.id = "fv-serve-model";
        modelPicker.setAttribute("aria-label", "Conversation model");
        modelPicker.title = "Claude, GPT, or Hugpy model for this conversation";
        modelPicker.style.maxWidth = "360px";
        sel.appendChild(modelPicker);
        populateServeModels(modelPicker);
      } else {
      var provider = document.createElement("select");
      provider.id = "fv-frontier-provider";
      provider.title = "Keeper provider";
      provider.setAttribute("aria-label", "Keeper provider");
      [["codex", "ChatGPT"], ["claude-code", "Claude"], ["hugpy", "Hugpy"]].forEach(function (entry) {
        var option = document.createElement("option");
        option.value = entry[0];
        option.textContent = entry[1];
        option.selected = entry[0] === frontierNative;
        option.disabled = !spec.backends[entry[0]] || spec.backends[entry[0]].available === false;
        provider.appendChild(option);
      });
      provider.addEventListener("change", function (ev) {
        var method = backendFor("frontier");
        frontierNative = ev.target.value;
        localStorage.setItem("fv-frontier-native", frontierNative);
        chosen.frontier = (method === "mct" && frontierNative !== "hugpy") ? "mct" : frontierNative;
        localStorage.setItem("fv-backends", JSON.stringify(chosen));
        /* One stable Serve frontend: a provider change is carried into the
           existing console, never used as a reason to mount a terminal seat. */
        detach("frontier");
        renderBar();
        refreshMeta();
      });
      sel.appendChild(provider);
      }
      /* Keeper mode is a two-button switch (2026-09-18): the abstract-claude
         "serve" console vs the "tmux" terminal seat (which model backs the seat
         is the provider select above). "serve" is the keeper surface, not a
         TERM_SURFACES backend, so it never enters `chosen` — AC_FORCE carries
         the pick; "tmux" selects the provider seat via setBackend. The former
         MCT pointer-exchange option was dropped from the picker. */
      var seatInfo = spec.backends[frontierNative] || {};
      var bServe = document.createElement("button");
      bServe.type = "button";
      bServe.setAttribute("data-mode", "serve");
      bServe.textContent = "serve";
      bServe.title = "Shared Serve — the /ac console (keeper surface)";
      bServe.disabled = !acOffered();
      bServe.className = acOn() ? "on" : "";
      bServe.addEventListener("click", function () {
        if (acOn()) return;
        acSetForce("serve");
        acSync();
        renderBar();
      });
      sel.appendChild(bServe);
      /* 1.0.144: a locus without a serve of its own gets ONE explicit action —
         provision its standing serve (keeper/chat/worker/local) ON the locus, as
         its user, from the station's shipped resources (POST
         /api/locus/serve/provision). Being the host has no bearing on it. */
      var srv = spec.serve || {};
      if (!acOffered() && !acHostSeat() && srv.ok === false) {
        var bProv = document.createElement("button");
        bProv.type = "button";
        bProv.setAttribute("data-mode", "provision");
        bProv.textContent = "＋ serve";
        bProv.title = "Provision " + activeVm + "'s own serve (runs as " + activeVm + "'s user, from the station's shipped resources)" +
          (srv.error ? " — now: " + srv.error : "");
        bProv.addEventListener("click", function () {
          if (!confirm("Provision a standing abstract-claude serve on " + activeVm + "? It runs as that locus's user (user unit abstract-claude-serve@station) and starts its keeper session.")) return;
          bProv.disabled = true; bProv.textContent = "provisioning…";
          fetch("/api/locus/serve/provision", { method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json" }, body: JSON.stringify({ vm: activeVm }) })
            .then(function (r) { return r.json(); })
            .then(function (d) {
              fvToast(d.ok ? ("serve on " + activeVm + ": 127.0.0.1:" + d.port + " (" + (d.version || "") + ")")
                           : ("serve provision failed: " + (d.error || "?")), !d.ok);
              refreshMeta();
            })
            .catch(function (e) { fvToast("serve provision failed: " + e, true); refreshMeta(); });
        });
        sel.appendChild(bProv);
      }
      var bTmux = document.createElement("button");
      bTmux.type = "button";
      bTmux.setAttribute("data-mode", "tmux");
      bTmux.textContent = "tmux";
      /* 1.0.139: tmux is a Recommends — /api/term/backends reports frontier.tmux;
         without it the seat cannot start, so the button is disabled and says why. */
      var tmuxInfo = spec.tmux || {};
      var tmuxMissing = tmuxInfo.available === false;
      bTmux.title = tmuxMissing
        ? (tmuxInfo.reason || "tmux is not installed — the terminal (tmux) seat is unavailable (install it: sudo apt install tmux)")
        : seatInfo.available === false
          ? "terminal seat (tmux) unavailable — " + backendLabel(frontierNative) + " is not installed on this locus"
          : "terminal seat (tmux) — the selected keeper provider in a PTY";
      // 1.0.144: the locus's OWN running seats (listed on its console socket, as its user)
      var running = (spec.seats || []).map(function (x) { return x.name + (x.attached ? "*" : ""); });
      if (running.length) bTmux.title += " · running on " + (acHostSeat() ? "this host" : activeVm) + ": " + running.join(", ");
      if (spec.seat_live) bTmux.setAttribute("data-seat-live", "1");
      bTmux.disabled = seatInfo.available === false || tmuxMissing;
      bTmux.className = acOn() ? "" : "on";
      bTmux.addEventListener("click", function () {
        if (!acOn() && backendFor("frontier") === frontierNative) { show("frontier"); return; }
        setBackend("frontier", frontierNative, true);   // leaves serve, mounts the tmux seat
      });
      sel.appendChild(bTmux);
      setStatus();
      return;
    }
    // Other surfaces retain their compact backend buttons.
    for (var k = 0; k < keys.length; k++) {
      var key = keys[k], info = spec.backends[key] || {};
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("data-backend", key);
      b.textContent = backendLabel(key);
      b.title = backendLabel(key) + (key === spec["default"] ? " (default)" : "") +
        (info.available === false ? " — not installed" : (key === current ? " — displayed" : " — switch (detaches; the other backend's session keeps running)"));
      b.disabled = info.available === false;
      b.className = key === current ? "on" : "";
      b.addEventListener("click", function (ev) {
        var want = ev.currentTarget.getAttribute("data-backend");
        if (!want || want === backendFor(surface)) return;
        setBackend(surface, want, true);
      });
      sel.appendChild(b);
    }
    setStatus();
  }

  /* 1.0.65: ONE backend setter (buttons, __fvBackend, the SPA's one-off asks).
     persist=false = a one-off (e.g. the login walkthrough opening claude-code)
     that must not change the operator's remembered default. */
  function setBackend(s, key, persist) {
    var spec = SURFACES[s] || { backends: {} };
    if (!spec.backends || spec.backends[key] === undefined) return false;
    /* t296: picking a frontier backend by hand is an explicit ask for that
       tmux seat, so it overrides the serve surface for this tab (?keeper=serve
       or a reload brings the console back). */
    var acWasOn = (s === "frontier" && acOn());
    if (acWasOn) acSetForce("tmux");
    var was = backendFor(s);
    chosen[s] = key;
    if (s === "frontier" && (key === "codex" || key === "claude-code" || key === "hugpy")) {
      frontierNative = key;
      if (persist !== false) localStorage.setItem("fv-frontier-native", key);
    }
    if (persist !== false) localStorage.setItem("fv-backends", JSON.stringify(chosen));
    if (s === surface) {
      if (acWasOn) acSync();        // leaves the console, mounts+connects the tmux seat
      else if (was !== key) detach(s);   // never kill: the old backend's session survives
      refreshMeta();
    }
    return true;
  }
  window.__fvBackend = {
    set: function (s, key, opts) { return setBackend(s, key, !(opts && opts.persist === false)); },
    get: function (s) { return backendFor(s || surface); }
  };

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
  /* 1.0.143: in the desktop app the clipboard goes through Electron's own
     clipboard (preload window.stationClipboard → main process), which needs
     no focus, permission or secure context; a plain browser uses the async
     Clipboard API, then the execCommand fallback (copy) or rejects (read). */
  function fvBridge() {
    var b = window.stationClipboard;
    return (b && typeof b.writeText === "function" && typeof b.readText === "function") ? b : null;
  }
  function fvClipRead() {
    var b = fvBridge();
    if (b) return Promise.resolve(b.readText()).then(function (t) { return String(t || ""); });
    if (navigator.clipboard && navigator.clipboard.readText) return navigator.clipboard.readText();
    return Promise.reject(new Error("clipboard read unavailable"));
  }
  function fvCopy(t, announce) {
    if (!t) { if (announce) fvToast("nothing selected to copy", true); return; }
    _fvSelLast = t;
    var done = function (ok) {
      if (announce) fvToast(ok ? "copied " + t.length + " chars ✓"
                               : "copy FAILED — select again", !ok);
    };
    var b = fvBridge();
    if (b) {
      Promise.resolve(b.writeText(t)).then(function () { done(true); },
                                           function () { done(fvCopyFallback(t)); });
    } else if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(
        function () { done(true); },
        function () { done(fvCopyFallback(t)); });
    } else done(fvCopyFallback(t));
  }

  /* ── t296 (2026-09-16, vm_mgr): the SERVE keeper surface in FleetView ──────
     The HOST keeper seat lives HERE, not on the SPA stage: index.html never
     mounts a stage pane for the host (picking "⌂ host" sets active=null, and
     the stage only maps `panes`, which are keyed by a real VM). mount() ends
     with open() — "terminal-first: auto-open on load" — so the root landing
     page IS this pane, surface "frontier", locus host. That is why the
     paneSrc()/keeper_surface gate added to index.html never fired for the
     operator: it only ever sees VM panes.
     So honour the same verdict here. When the station reports
     keeper_surface === "serve" (and abstract-claude serve actually answers)
     AND the selected locus is this host, the frontier view is the /ac/ console
     in an iframe — the station's same-origin proxy to STATION_CONSOLE_AC.
     Everything else is untouched: a VM's own keeper, the shell/local surfaces,
     a pulled seat, and keeper_surface === "tmux" all keep the xterm/tmux path,
     and the xterm views are never destroyed — flipping back is lossless.
     Escape hatches: ?keeper=tmux on the console URL, or clicking any frontier
     backend button (an explicit "give me the tmux seat" for this tab).
     Reversible: drop this block, the acOn() guards in show()/setStatus(), and
     the acSync() calls in refreshMeta()/__fvVm.set. */
  /* AC_FORCE is the operator's OWN pick in the frontier dropdown: "" = follow
     the server verdict, "serve" = the /ac/ console, "tmux" = a terminal seat.
     Persisted (localStorage "fv-keeper-surface") so refreshMeta() and locus
     switches can never snap the selection back; ?keeper= still wins for the
     tab and is written through so a reload keeps it. */
  var AC_FORCE = "";
  try { var _acls = localStorage.getItem("fv-keeper-surface");
        if (_acls === "serve" || _acls === "tmux") AC_FORCE = _acls; } catch (e) {}
  try {
    var _acq = new URLSearchParams(location.search).get("keeper");
    if (_acq === "serve" || _acq === "tmux") AC_FORCE = _acq;
  } catch (e) {}
  function acSetForce(v) {
    AC_FORCE = (v === "serve" || v === "tmux") ? v : "";
    try {
      if (AC_FORCE) localStorage.setItem("fv-keeper-surface", AC_FORCE);
      else localStorage.removeItem("fv-keeper-surface");
    } catch (e) {}
  }
  /* Is the serve console even on the menu for this locus? Listed whenever the
     station says its surface is serve (so the operator can come BACK to it
     after a tmux detour), or whenever they have explicitly forced it. */
  function acOffered() {
    return !!acPathNow() && (KEEPER_SURFACE === "serve" || AC_FORCE === "serve");
  }
  function acHostSeat() {
    // 1.0.141: the bare "keeper" is vm_mgr's locus, not this host — the host is
    // the reserved tokens or this station's OWN locus name (window.__fvSelf).
    if (!activeVm || activeVm === "@keeper" || activeVm === "@self" || activeVm === "host") return true;
    var me = (window.__fvSelf && window.__fvSelf.names) || [];
    return me.indexOf(activeVm) !== -1;
  }
  // ?vm=<selected locus> for seat-config calls ("" on the host seat)
  function vmQ(pre) { return acHostSeat() ? "" : ((pre || "?") + "vm=" + encodeURIComponent(activeVm)); }
  function acOn() {
    if (surface !== "frontier" || !acPathNow()) return false;
    return AC_FORCE ? AC_FORCE === "serve" : KEEPER_SURFACE === "serve";
  }
  var acEl = null;
  // The /ac console placeholder. It is only ever a positioned, empty anchor:
  // in Electron a NATIVE WebContentsView (owned by main.js) is overlaid on its
  // measured rect (see acNative* below); in a plain browser acFrame() drops the
  // old <iframe> into it so the console still works outside the desktop shell.
  function acView() {
    if (acEl) return acEl;
    acEl = document.createElement("div");
    acEl.id = "fv-ac-view";
    acEl.className = "fv-view";
    document.getElementById("fv-term-host").appendChild(acEl);
    return acEl;
  }
  // iframe fallback: used in a plain browser (no window.stationConsole) and for
  // a per-locus console path the host native view can't frame. In Electron on
  // the host keeper the native view overlays the placeholder instead.
  function acFrame() {
    var f = document.getElementById("fv-ac-frame");
    if (f) return f;
    f = document.createElement("iframe");
    f.id = "fv-ac-frame";
    f.title = "Shared Serve — keeper console";
    f.src = acPathNow() || "/ac/";
    f.setAttribute("allow", "clipboard-read; clipboard-write");
    f.style.cssText = "width:100%;height:100%;border:0;display:block;background:#0d1117";
    acView().appendChild(f);
    return f;
  }
  // The browser frame and Electron native view share this origin's selection.
  // Model selection changes the durable conversation, not the frame URL.
  var serveModelBusy = false;
  async function serveRequest(route, body) {
    var response = await fetch((acPathNow() || "/ac/") + "api/" + route, {
      method: body === undefined ? "GET" : "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    var doc = await response.json();
    if (!response.ok || doc.error || doc.ok === false) throw Error(doc.error || "Model selection failed");
    return doc;
  }
  async function populateServeModels(picker) {
    if (serveModelBusy) return;
    picker.disabled = true;
    picker.replaceChildren(new Option("Loading models…", ""));
    try {
      var docs = await Promise.all([serveRequest("session/roster"), serveRequest("console/sessions")]);
      if (!picker.isConnected) return;
      var roster = docs[0], sessions = docs[1].sessions || [];
      var sid = localStorage.getItem("ac_selected") || "";
      var current = sessions.find(function (s) { return s.id === sid || s.native_id === sid; }) ||
                    (roster.roles || []).find(function (r) { return r.session_id === sid; });
      picker.replaceChildren(new Option(current ? "Select model…" : "Choose model · new conversation", ""));
      ["claude", "gpt", "hugpy"].forEach(function (backend) {
        var group = document.createElement("optgroup"); group.label = {claude:"Claude",gpt:"GPT",hugpy:"Hugpy"}[backend];
        (roster.provider_options || []).filter(function (m) { return m.backend === backend; }).forEach(function (m) {
          var opt = new Option(m.label, JSON.stringify([m.backend, m.model]));
          opt.selected = !!current && (current.backend || "claude") === m.backend && (current.model || "") === m.model;
          group.appendChild(opt);
        });
        if (group.children.length) picker.appendChild(group);
      });
      picker.disabled = !!(current && current.busy);
      picker.onchange = async function () {
        if (!picker.value || serveModelBusy) return;
        var choice = JSON.parse(picker.value);
        var selected = localStorage.getItem("ac_selected") || "";
        if (selected !== sid) { populateServeModels(picker); return; }
        serveModelBusy = true; picker.disabled = true;
        try {
          var role = (roster.roles || []).find(function (r) { return r.session_id === selected && r.role !== "local"; });
          var result;
          if (role) {
            result = await serveRequest("session/roster", {action:"set_provider", role:role.role,
              backend:choice[0], model:choice[1], by:"station-model-picker"});
          } else {
            var session = sessions.find(function (s) { return s.id === selected || s.native_id === selected; });
            if (!session && selected) session = await serveRequest("console/sessions", {backend:"claude", native_id:selected});
            result = session ? await serveRequest("console/switch", {session_id:session.id, backend:choice[0], model:choice[1]}) :
              await serveRequest("console/sessions", {backend:choice[0], model:choice[1]});
          }
          var target = result.session_id || result.id;
          localStorage.setItem("ac_selected", target); localStorage.setItem("kl-goto", target);
          if (acNativeApplicable()) window.stationConsole.reload();
          else { var frame = acFrame(); frame.src = acPathNow(); }
          fvToast("Conversation model: " + choice[0] + " · " + (choice[1] || "default"));
        } catch (err) { fvToast(err.message, true); }
        finally { serveModelBusy = false; populateServeModels(picker); }
      };
    } catch (err) { picker.replaceChildren(new Option(err.message, "")); }
  }
  window.addEventListener("storage", function (event) {
    if (event.key === "ac_selected") {
      var picker = document.getElementById("fv-serve-model"); if (picker) populateServeModels(picker);
    }
  });
  // ── NATIVE /ac console (Electron desktop shell) ─────────────────────────────
  // window.stationConsole (preload contextBridge) drives a real WebContentsView
  // overlaid on the pane. Every call is guarded so a plain browser (where the
  // bridge is absent) falls back to acFrame() and keeps working. The native view
  // frames the HOST keeper (/ac/); a per-locus console path uses the iframe.
  function acNativeAvail() { return !!window.stationConsole; }
  function acNativeApplicable() { return acNativeAvail() && (acPathNow() === "/ac/"); }
  function acRect() {
    var host = document.getElementById("fv-term-host");
    var el = (acEl && acEl.classList.contains("on")) ? acEl : host;
    if (!el) return null;
    var r = el.getBoundingClientRect();
    return { x: r.left, y: r.top, width: r.width, height: r.height };
  }
  var _acRO = null;
  function acNativeShow() {
    if (!acNativeAvail()) return;
    var r = acRect(); if (!r) return;
    window.stationConsole.show(r);
    // keep the overlay glued to the pane as it resizes/moves (drag, split,
    // window resize, header/floor re-measure all change the host box).
    if (!_acRO && window.ResizeObserver) {
      var host = document.getElementById("fv-term-host");
      if (host) { _acRO = new ResizeObserver(acNativeBounds); _acRO.observe(host); }
    }
  }
  function acNativeHide() { if (acNativeAvail()) window.stationConsole.hide(); }
  function acNativeBounds() {
    if (!acNativeAvail()) return;
    if (!acOn() || pane.classList.contains("closed")) return;
    var r = acRect(); if (!r) return;
    window.stationConsole.setBounds(r);
  }
  var _acWas = null;                 // last applied verdict (null = never applied)
  /* The verdict arrives async (refreshMeta) and moves with the locus, so
     re-enter show() only when it actually FLIPS — re-showing on every poll
     would refit/refocus the terminal under the operator's hands. */
  function acSync() {
    var on = acOn();
    if (on && acEl) {                // 1.0.101: the locus changed under the frame -> its own console
      if (acNativeApplicable()) {
        acNativeShow();              // native host view: just (re)position it
      } else {
        var fr = acFrame(), want = acPathNow();
        if (fr && want && fr.getAttribute("src") !== want) { fr.setAttribute("src", want); fr.title = "Shared Serve — " + (acHostSeat() ? "keeper" : activeVm) + " console"; }
      }
    }
    if (_acWas === on) return false;
    _acWas = on;
    show(surface);
    return true;
  }

  function view(s) {
    if (views[s]) return views[s];
    var el = document.createElement("div");
    el.className = "fv-view";
    document.getElementById("fv-term-host").appendChild(el);
    var term = new Terminal({ fontSize: 13, cursorBlink: true,
      // 1.0.143: selecting inside mouse-mode TUIs — Shift+drag (Linux/Windows,
      // xterm's built-in force-selection) or Option+drag on macOS; a
      // right-click never re-selects a word (it copies the selection you made).
      macOptionClickForcesSelection: true, rightClickSelectsWord: false,
      theme: { background: "#0d1117", foreground: "#e6e9ef" } });
    var fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    var nativeEl = document.createElement("div"); nativeEl.className = "fv-native"; el.appendChild(nativeEl);
    term.open(nativeEl);
    /* FREEZE FIX (2026-09-18): Electron/webview engines drop the
       IntersectionObserver un-pause, wedging xterm's RenderService
       (_isPaused stuck true) so buffered PTY bytes never paint though the
       socket is live. These panes are shown/hidden by CSS and never
       detached, so the pause optimisation only ever hurts here — pin
       _isPaused false so painting can never wedge. forceRepaint + the wake
       handlers below stay as a second line of defence, and the safety
       interval also clears a dropped requestAnimationFrame. */
    try {
      var _rs0 = term._core && term._core._renderService;
      if (_rs0) Object.defineProperty(_rs0, "_isPaused",
        { get: function () { return false; }, set: function () {}, configurable: true });
    } catch (e) {}
    var v = views[s] = { el: el, nativeEl: nativeEl, term: term, fit: fit, ws: null };
    if (s === "frontier") v.mct = window.MctRenderer(el);
    function fvSend(d) {              // the ONE path keystrokes take to the PTY
      if (!v.ws || v.ws.readyState !== 1) return;
      if (s === "frontier" && backendFor(s) === "mct") return;
      if (v._inCopyScroll) {
        // typing jumps back live: cancel copy-mode first — the server works
        // its ws messages in order, so the cancel lands before the keystroke.
        v._inCopyScroll = false;
        try { v.ws.send(JSON.stringify({ t: "scroll", cancel: true })); } catch (e) {}
      }
      v.ws.send(enc.encode(d));
      v._lastSend = Date.now();     // p510(d): last operator keystroke → PTY (liveness watchdog)
    }
    term.onData(fvSend);
    // Wheel → tmux copy-mode scroll (model surfaces only: their backends live
    // in tmux with mouse OFF — selection-copy depends on that — and prefix
    // None, so the scroll ctl is the ONLY way into pane history. Shell PTYs
    // keep xterm's native scrollback). capture:true runs before xterm's own
    // wheel handling, which would otherwise turn the wheel into arrow keys.
    if (s === "frontier" || s === "local") {
      v._whAcc = 0; v._whT = null;
      nativeEl.addEventListener("wheel", function (e) {
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
    /* NO copy-on-select (operator 2026-09-15, reaffirmed 2026-10-01):
       highlighting is a reading aid, so a drag never touches the clipboard.
       Copy is EXPLICIT — Ctrl/Cmd+Shift+C, or a right-click on a selection.
       v._selKeep holds the last non-empty selection so those explicit copies
       still work after a busy TUI redraw wiped the highlight; it is dropped on
       the next left click or keystroke (a new intent), never by a redraw.
       Mouse-mode apps (claude-code, codex, opencode turn on DEC mouse
       tracking) own plain drags: Shift+drag selects (xterm's force-selection
       modifier on Linux/Windows; Option+drag on macOS via
       macOptionClickForcesSelection). Shift may be released before copying. */
    v._selKeep = "";
    term.onSelectionChange(function () {
      var t = term.getSelection();
      if (t) { _fvSelLast = t; v._selKeep = t; v._selAt = Date.now(); }
    });
    function selNow() { return term.getSelection() || v._selKeep || ""; }
    /* PASTE (1.0.143, operator 2026-10-01): Ctrl+V and Ctrl/Cmd+Shift+V paste
       directly — no menu. Every route ends in xterm's own term.paste(), which
       converts newlines and adds the bracketed-paste markers ONLY when the app
       turned that mode on (1.0.104's bracketed paste, now done once, by xterm).
       Order: (1) the browser's NATIVE paste event that the real keystroke
       fires on xterm's textarea — needs no clipboard permission and no secure
       context; (2) if none arrives within 250 ms, the Electron bridge
       (window.stationClipboard) or the async Clipboard API; (3) if that is
       denied, the paste… box opens (a native Ctrl+V into a real textarea). */
    function fvPasteText(txt) {
      if (!v.ws || v.ws.readyState !== 1) { fvToast("not connected", true); return; }
      if (!txt) { fvToast("clipboard has no text", true); return; }
      term.paste(txt);
      fvToast("pasted " + txt.length + " chars");
    }
    function fvPasteApi() {
      v._pasteApiAt = Date.now();
      fvClipRead().then(fvPasteText, function () {
        fvToast("clipboard read blocked here — paste into the box, then send", true);
        pOpen(true);
      });
    }
    term.attachCustomKeyEventHandler(function (e) {
      if (e.type === "keydown" && (e.ctrlKey || e.metaKey) && e.shiftKey && !e.altKey
          && (e.key === "C" || e.key === "c")) {
        // preventDefault: `return false` stops xterm, but WITHOUT this the
        // browser's native Ctrl/Cmd+Shift+C default still fires — belt-and-braces.
        e.preventDefault();
        // live selection first; else the one a redraw already wiped
        fvCopy(selNow() || _fvSelLast, true);
        return false;             // handled here — not sent to the PTY
      }
      if (e.type === "keydown" && (e.ctrlKey || e.metaKey) && !e.altKey
          && (e.key === "V" || e.key === "v")) {
        // Ctrl+V / Ctrl+Shift+V (and Cmd+V): NOT sent to the PTY as ^V, and NOT
        // preventDefault-ed — the browser's own paste event is route (1); the
        // capture listener below takes it (one paste, never two).
        clearTimeout(v._pasteArmT);
        v._pasteArmT = setTimeout(function () { v._pasteArmT = null; fvPasteApi(); }, 250);
        return false;
      }
      /* Ctrl+_ (operator 2026-09-15: "it cannot be done in the browser, it
         just shrinks the view"): the browser eats Ctrl+Shift+- as zoom-out
         before xterm sees it. With the terminal focused, swallow it and hand
         the PTY 0x1F (C-_, claude-code's undo). Plain Ctrl+- (no shift) is
         NOT intercepted — that stays browser zoom. */
      if (e.type === "keydown" && e.ctrlKey && !e.metaKey && !e.altKey
          && (e.key === "_" || (e.shiftKey && (e.key === "-" || e.code === "Minus")))) {
        e.preventDefault();
        fvSend("\x1f");
        return false;
      }
      if (e.type === "keydown" && !/^(Shift|Control|Alt|Meta|AltGraph|CapsLock)$/.test(e.key))
        v._selKeep = "";          // typing = a new intent; the kept selection goes
      return true;
    });
    /* ── paste bypass panel (operator 2026-09-17) ─────────────────────────────
       The browser paste paths can all fail at once on this host (no Clipboard
       API read, a swallowed chord). This chip is the path that cannot fail:
       the operator pastes into a REAL textarea with a native Ctrl+V — always
       allowed, no secure context needed. On the frontier seat (and a pulled
       seat) one POST injects it into that tmux pane by session name via
       /api/term/paste (tmux send-keys, same precedent as /api/term/unstick);
       1.0.143: every other surface (⌂ shell, local) goes over this pane's own
       socket with term.paste — /api/term/paste only knows the keeper seat, so
       it used to land in the WRONG pane. No Enter is ever sent; the text lands
       in the prompt and the operator submits it. */
    var pbtn = document.createElement("div");
    pbtn.className = "fv-pastebtn";
    pbtn.textContent = "paste…";
    pbtn.title = "paste into this pane without the browser clipboard";
    var pbox = document.createElement("div");
    pbox.className = "fv-pastebox";
    pbox.innerHTML = '<textarea placeholder="paste here (Ctrl+V), then send →"></textarea>' +
      '<div class="row"><span class="msg" style="flex:1"></span>' +
      '<button type="button" class="cancel">close</button>' +
      '<button type="button" class="send">send to pane</button></div>';
    el.appendChild(pbox); el.appendChild(pbtn);
    var pta = pbox.querySelector("textarea"), pmsg = pbox.querySelector(".msg");
    function pOpen(on) {
      pbox.classList.toggle("on", !!on);
      if (on) { pmsg.textContent = ""; try { pta.focus(); } catch (e) {} }
      else { try { term.focus(); } catch (e) {} }
    }
    pbtn.addEventListener("click", function () { pOpen(!pbox.classList.contains("on")); });
    pbox.querySelector(".cancel").addEventListener("click", function () { pOpen(false); });
    pbox.querySelector(".send").addEventListener("click", function () {
      var txt = pta.value;
      if (!txt) { pmsg.textContent = "nothing to send"; return; }
      if (!(s === "frontier" || PULLS[s])) {
        if (!v.ws || v.ws.readyState !== 1) { pmsg.textContent = "not connected"; return; }
        term.paste(txt);
        pta.value = ""; pOpen(false);
        fvToast("pasted " + txt.length + " chars");
        return;
      }
      pmsg.textContent = "sending…";
      var body = { text: txt };
      if (PULLS[s] && PULLS[s].tmux) body.session = PULLS[s].tmux;
      else if (!acHostSeat()) body.vm = activeVm;     // 1.0.141: the SELECTED locus's pane
      fetch("/api/term/paste", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
        .then(function (r) { return r.json().then(function (d) { return { s: r.status, d: d }; }); })
        .then(function (o) {
          if (o.d && o.d.ok) {
            pta.value = ""; pOpen(false);
            fvToast("pasted " + o.d.chars + " chars into " + o.d.session);
          } else {
            pmsg.textContent = (o.d && (o.d.error || o.d.detail)) || ("HTTP " + o.s);
          }
        })
        .catch(function (e) { pmsg.textContent = String((e && e.message) || e); });
    });
    /* RIGHT-CLICK (1.0.143, operator 2026-10-01): no menu in the terminal —
       the 2026-09-17 helper-textarea stretch that enabled the browser's
       native menu (whose only use was "Paste") is gone. A right-click on a
       selection (live, or kept from before a TUI redraw) copies it at once —
       on mousedown, so the click never reaches the app (which would redraw
       and wipe the highlight) and Shift no longer has to be held. With no
       selection the click belongs to the app (mouse reporting) and only the
       browser menu is suppressed. The paste… box keeps its native menu. */
    var _ctxCopiedAt = 0;
    function inBox(ev) { return pbox.contains(ev.target) || pbtn.contains(ev.target); }
    el.addEventListener("mousedown", function (ev) {
      if (inBox(ev)) return;
      if (ev.button === 0) { if (!ev.shiftKey) v._selKeep = ""; return; }
      if (ev.button !== 2) return;
      var t = selNow();
      if (!t) return;
      ev.preventDefault(); ev.stopPropagation();
      _ctxCopiedAt = Date.now();
      fvCopy(t, true);
    }, true);
    el.addEventListener("contextmenu", function (ev) {
      if (inBox(ev)) return;
      ev.preventDefault(); ev.stopPropagation();
      if (Date.now() - _ctxCopiedAt < 1500) return;      // mousedown already copied it
      var t = selNow();
      if (t) { fvCopy(t, true); return; }
      var mouseApp = false;
      try { mouseApp = !!term.modes && term.modes.mouseTrackingMode !== "none"; } catch (e) {}
      fvToast(mouseApp ? "nothing selected — Shift+drag selects here (the app has the mouse)"
                       : "nothing selected — paste: Ctrl+V / Ctrl+Shift+V");
    }, true);
    el.addEventListener("paste", function (ev) {
      if (inBox(ev)) return;                              // the box's own textarea
      if (v._pasteArmT) { clearTimeout(v._pasteArmT); v._pasteArmT = null; }
      ev.preventDefault(); ev.stopPropagation();          // ONE path: ours, not xterm's too
      if (Date.now() - (v._pasteApiAt || 0) < 1000) return; // the API fallback already pasted it
      var txt = "";
      try { txt = ev.clipboardData.getData("text/plain"); } catch (e) {}
      fvPasteText(txt);
    }, true);
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

  function prepareView(s) {
    var v = view(s), mediated = s === "frontier" && backendFor(s) === "mct";
    v.nativeEl.style.display = mediated ? "none" : "block";
    if (v.mct) v.mct.configure(frontierNative, activeVm, mediated && surface === "frontier");
    return v;
  }
  function focusView(v) {
    if (v.mct && backendFor("frontier") === "mct") v.mct.focus(); else v.term.focus();
  }
  function connect(s) {
    var v = prepareView(s);
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var mine = new WebSocket(proto + "://" + location.host + "/wsterm" +
      "?rows=" + v.term.rows + "&cols=" + v.term.cols +
      (PULLS[s] ? "&surface=shell&vm=@keeper&tmux=" + encodeURIComponent(PULLS[s].tmux) : "") +
      (PULLS[s] ? "&surface_key=" : "&surface=") + encodeURIComponent(s) +
      "&backend=" + encodeURIComponent(backendFor(s)) +
      (s === "frontier" ? "&native=" + encodeURIComponent(frontierNative) : "") +
      (s === "frontier" && activeSession ? "&session=" + encodeURIComponent(activeSession) : "") +
      (activeVm ? "&vm=" + encodeURIComponent(activeVm) : ""));   // grounding: every surface follows the active VM
    v.ws = mine;
    mine.binaryType = "arraybuffer";
    v._afDec = new TextDecoder("utf-8"); v._afTail = ""; v._afLog = "";
    v._afArmedAt = Date.now() + 2000;        // scrollback replay window
    mine.onopen = function () {
      if (v.ws === mine) { v._retry = 0; v._exited = false; sizeOf(v); setStatus(); }
    };
    mine.onclose = function () {
      if (v.ws !== mine) return;      // restart()/detach() closed it on purpose
      setStatus();
      scheduleReconnect(s, v);
    };
    mine.onmessage = function (e) {
      if (v.ws !== mine) return;
      if (typeof e.data === "string") {
        try {
          var ctl = JSON.parse(e.data);
          if (ctl.t === "error" && ctl.msg)
            v.term.write("\r\n\x1b[2m[" + ctl.msg + "]\x1b[0m\r\n");
          if (ctl.t === "exit") {
            v._exited = true;         // a finished session is not re-spawned behind the operator's back
            v.term.write("\r\n\x1b[2m[session ended]\x1b[0m\r\n");
          }
        } catch (err) {}
        return;
      }
      if (s === "frontier" && backendFor(s) === "mct") return;
      if (s === "frontier") watchAFail(v, e.data);
      v._lastByte = Date.now();     // p510(d): last PTY byte received (liveness watchdog)
      v.term.write(new Uint8Array(e.data));
    };
  }

  /* Auto-reconnect (operator 2026-09-02: "comes back unresponsive after a
     while, click elsewhere and back and it works"). A dropped socket — the
     backend's 30s heartbeat closes it after a laptop sleep, a wifi hop or a
     NAT/VPN reset — used to sit dead until the surface was re-shown, because
     only show() ever called connect(). Now: a close that was NOT ours
     (restart/detach null v.ws first) retries with backoff 1s→30s while the
     pane is open, and any wake-up signal (tab visible again, window focus,
     network back) reconnects the active surface immediately. The server
     keeps the PTY in tmux and replays scrollback, so the pane is reset
     first to avoid a doubled copy. A session that told us it EXITED is left
     alone — re-showing it is the operator's explicit ask. */
  function reconnect(s, v) {
    if (!v || v._exited) return;
    if (v.ws && v.ws.readyState !== 3 /* CLOSED */) return;
    if (v.ws) v.term.reset();
    connect(s);
    setTimeout(function () { v.fit.fit(); sizeOf(v); }, 30);
  }
  function scheduleReconnect(s, v) {
    if (v._exited || pane.classList.contains("closed")) return;
    clearTimeout(v._retryT);
    var n = v._retry = (v._retry || 0) + 1;
    var wait = Math.min(30000, 1000 * Math.pow(2, n - 1));
    v._retryT = setTimeout(function () {
      if (v.ws && v.ws.readyState !== 3) return;
      if (pane.classList.contains("closed")) return;
      if (s !== surface) return;    // background surfaces reconnect on show()
      reconnect(s, v);
    }, wait);
  }
  /* RENDER-FREEZE FIX (operator 2026-09-14: the live claude-code seat "freezes
     while I'm in it, and after a focus event; takes another focus event to
     unfreeze, unreliably"). Root cause is xterm.js, not tmux or the socket:
     xterm's RenderService pauses painting via an IntersectionObserver on the
     terminal element (RenderService._handleIntersectionChange → _isPaused). While
     the tab/window is hidden or occluded, isIntersecting is false, so every
     term.write() only sets _needsFullRefresh and paints NOTHING (refreshRows: the
     _isPaused branch). Recovery depends on the observer firing isIntersecting=true
     again on re-show — which embedded/webview engines drop, leaving _isPaused
     stuck true (and/or the RenderDebouncer's _animationFrame stuck set after a
     dropped rAF), so buffered PTY bytes never paint even though the socket is
     alive. forceRepaint() clears that stuck state and repaints the CURRENT buffer
     — no buffer reflow, no PTY resize/SIGWINCH — so a live TUI keeps updating. */
  function forceRepaint(v) {
    if (!v || !v.term) return;
    var rs = v.term._core && v.term._core._renderService;
    if (rs) {
      try {
        rs._isPaused = false;                                 // clear a stuck IntersectionObserver pause
        rs._needsFullRefresh = false;
        if (rs._renderDebouncer) rs._renderDebouncer._animationFrame = undefined;  // release a dropped rAF
        rs.refreshRows(0, v.term.rows - 1);                   // full repaint of the current buffer
        return;
      } catch (e) {}
    }
    try { v.term.refresh(0, v.term.rows - 1); } catch (e) {}  // fallback for other xterm builds
  }
  function wake() {
    if (pane.classList.contains("closed")) return;
    var v = views[surface];
    if (!v) return;
    if (v.ws && v.ws.readyState === 1) {
      // Connected, not dropped: the stall is a paused/stuck renderer, not a dead
      // socket. Repaint the active surface so it updates without a 2nd focus event.
      forceRepaint(v);
      try { v.fit.fit(); sizeOf(v); } catch (e) {}   // p510(c): refit-on-wake — a resize while hidden left the grid stale
      return;
    }
    clearTimeout(v._retryT);
    v._retry = 0;
    reconnect(surface, v);
  }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") wake();
  });
  window.addEventListener("focus", wake);
  window.addEventListener("online", wake);
  window.addEventListener("pageshow", wake);
  // Belt-and-suspenders: a visible, connected terminal must never sit frozen. If
  // xterm's renderer is paused while its own surface is actually on-screen (a
  // dropped IntersectionObserver un-pause), force it live — no focus needed.
  setInterval(function () {
    if (document.visibilityState !== "visible") return;
    if (pane.classList.contains("closed")) return;
    var v = views[surface];
    if (!v || !v.term || !(v.ws && v.ws.readyState === 1)) return;
    var rs = v.term._core && v.term._core._renderService;
    if (!rs) return;
    // _isPaused is pinned false at open; the remaining wedge is a dropped
    // rAF that leaves _animationFrame set so no further frame is scheduled.
    // Still set a full second later => stuck: clear it and repaint.
    var rd = rs._renderDebouncer;
    if (rd && rd._animationFrame != null) {
      try { cancelAnimationFrame(rd._animationFrame); } catch (e) {}
      rd._animationFrame = undefined; forceRepaint(v);
    } else if (rs._isPaused || rs._needsFullRefresh) forceRepaint(v);
    // p510(d) LIVENESS WATCHDOG: a half-open socket (a NAT/VPN drop the browser
    // has not yet turned into onclose) still reports readyState OPEN and swallows
    // keystrokes while returning nothing — the seat "freezes while I'm in it."
    // If the operator has typed SINCE the last byte we received and the socket
    // has been silent past the threshold, force a reconnect (the server keeps the
    // PTY in tmux and replays scrollback). An idle-but-healthy pane never trips
    // this: with no keystroke, _lastSend stays older than _lastByte.
    var now = Date.now(), SILENT_MS = 30000;
    if (v._lastSend && v._lastSend > (v._lastByte || 0)
        && (now - v._lastSend) > SILENT_MS && (now - (v._lastByte || 0)) > SILENT_MS) {
      v._lastSend = 0;                          // one shot per wedge; the reconnect replays bytes → resets the clock
      if (v.ws) { var oldw = v.ws; v.ws = null; try { oldw.close(); } catch (e) {} }
      v.term.reset();
      reconnect(surface, v);
    }
  }, 1000);

  function show(s) {
    surface = s;
    localStorage.setItem("fv-surface", surface);
    try { window.dispatchEvent(new CustomEvent("fv-surface", { detail: s })); } catch (e) {}
    for (var k in views) views[k].el.classList.toggle("on", k === s);
    if (acOn()) {   // t296: serve console replaces the host frontier xterm seat
      _acWas = true;
      for (var k2 in views) views[k2].el.classList.remove("on");
      acView().classList.add("on");
      if (acNativeApplicable()) {    // native overlay for the host /ac console
        var oldf = document.getElementById("fv-ac-frame"); if (oldf) oldf.remove();
        acNativeShow();
      } else {                       // plain browser or per-locus path: iframe
        acNativeHide();
        acFrame();
      }
      renderBar();                  // re-renders the picker with serve selected
      return;
    }
    _acWas = false;
    if (acEl) acEl.classList.remove("on");
    acNativeHide();
    var v = prepareView(s);
    if (s !== "frontier" && views.frontier && views.frontier.mct) views.frontier.mct.configure(frontierNative, activeVm, false);
    v.el.classList.add("on");
    if (!v.ws || v.ws.readyState === 3 /* CLOSED */) {
      // reattach: the server-side session persists and replays scrollback,
      // so clear the stale copy first to avoid doubling it. An explicit
      // re-show also revives an exited session (the operator asked for it).
      clearTimeout(v._retryT); v._retry = 0; v._exited = false;
      if (v.ws) v.term.reset();
      connect(s);
    }
    renderBar();
    setTimeout(function () { v.fit.fit(); sizeOf(v); focusView(v); }, 30);
  }

  function restart(s) {
    // EXPLICIT relaunch of this surface's backend: kill its own tmux session
    // and start fresh. Reserved for the relaunch command — a mere backend
    // switch must use detach() below, or it destroys the conversation it is
    // switching away from (the 2026-08-27 "session keeps resetting" bug).
    var v = view(s);
    clearTimeout(v._retryT); v._retry = 0; v._exited = false;
    if (v.ws) {
      var old = v.ws;
      v.ws = null;
      try { if (old.readyState === 1) old.send(JSON.stringify({ t: "kill" })); } catch (e) {}
      try { old.close(); } catch (e) {}
    }
    v.term.reset();
    connect(s);
    setTimeout(function () { v.fit.fit(); sizeOf(v); focusView(v); }, 30);
  }

  function detach(s) {
    // backend change: release this PTY only — server-side the old backend's
    // own tmux session stays alive (each backend has its own; switching back
    // reattaches with scrollback), and connect() attaches the new one.
    var v = view(s);
    clearTimeout(v._retryT); v._retry = 0; v._exited = false;
    if (v.ws) {
      var old = v.ws;
      v.ws = null;
      try { if (old.readyState === 1) old.send(JSON.stringify({ t: "detach" })); } catch (e) {}
      try { old.close(); } catch (e) {}
    }
    v.term.reset();
    connect(s);
    setTimeout(function () { v.fit.fit(); sizeOf(v); focusView(v); }, 30);
  }

  function start() {
    if (started) return;
    if (typeof Terminal === "undefined") { console.warn("xterm not loaded"); return; }
    started = true;
    renderBar();
    loadSessions();
    show(surface);
    refreshRollState();
    if (!window.__fvRollTimer) window.__fvRollTimer = setInterval(refreshRollState, 30000);
    refreshLoops();
    if (!window.__fvLoopTimer) window.__fvLoopTimer = setInterval(refreshLoops, 30000);
    window.addEventListener("resize", function () {
      if (pane.classList.contains("closed")) return;
      if (acOn()) { acNativeBounds(); return; }
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
      if (acOn()) { acNativeShow(); return; }   // re-show the native overlay
      if (v) { v.fit.fit(); sizeOf(v); focusView(v); }
    }, 60);
  }
  function close() {
    pane.classList.add("closed");
    tab.classList.add("show");
    acNativeHide();                             // never leave the overlay floating
  }

  // Third-row rolling-state banner: mirror the steward's /api/frontier/state
  // (the fleet judge's derived objective for the active locus's frontier seat)
  // as a compact full-width line under #fv-term-bar. Reactive: refreshed on
  // mount, on a poll, on locus change, and after each refreshMeta().
  function fmtRollAge(s) {
    s = +s || 0;
    return s < 60 ? s + "s" : s < 3600 ? Math.floor(s / 60) + "m" : Math.floor(s / 3600) + "h";
  }
  function refreshRollState() {
    var banner = document.getElementById("fv-roll-banner");
    if (!banner) return;
    var metaEl = banner.querySelector(".fv-roll-meta");
    var objEl = banner.querySelector(".fv-roll-obj");
    var q = (activeVm && activeVm !== "@keeper") ? "?vm=" + encodeURIComponent(activeVm) : "";
    fetch("/api/frontier/state" + q, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        banner.classList.remove("warn");
        var meta = "", line = "", tip = "rolling state";
        if (!j) { line = "unavailable"; }
        else if (!j.ok) { line = j.error || "unavailable"; }
        else if (!j.exists) { line = "none yet for " + (j.locus || "this locus") + " — the fleet judge rolls every 10 min once the seat has turns"; }
        else {
          var st = j.state || {};
          meta = (j.locus || "") + " · " + (j.model || "?") + " · " + fmtRollAge(j.age_s) + " ago · " + (j.n_exchanges || 0) + " turns";
          var blockers = (st.blockers || []).slice(0, 3).join(" · ");
          var next = (st.next_steps || []).slice(0, 4).join(" · ");
          line = st.objective || "—";
          if (blockers) { line = "blocked: " + blockers + " — " + line; banner.classList.add("warn"); }
          tip = "rolling state · " + meta + "\nobjective: " + (st.objective || "—") +
                (next ? "\nnext: " + next : "") + (blockers ? "\nblocked: " + blockers : "");
        }
        if (metaEl) metaEl.textContent = meta;
        if (objEl) objEl.textContent = line;
        banner.title = tip;
      })
      .catch(function () { if (objEl) objEl.textContent = "unavailable"; });
  }

  // Fourth-row ⚠ loop strip (1.0.118): mirror GET /api/loops — every active
  // crash/retry loop the backend detector holds (systemd NRestarts, central
  // job retries/stalls, repeated identical prompts, queue stacking, worker
  // re-execs) as one readable row: source · identity ×count · first/last ·
  // the one-line "what to do" with a copy button. Empty set = strip hidden.
  function fmtLoopClock(t) {
    if (!t) return "?";
    var d = new Date(t * 1000);
    return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  }
  function refreshLoops() {
    var strip = document.getElementById("fv-loop-strip");
    if (!strip) return;
    fetch("/api/loops" + vmQ(), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        var rows = (j && j.ok && j.active) ? j.active : [];
        var recent = (j && j.ok && j.recent) ? j.recent.slice(0, 2) : [];
        // 1.0.124: log findings share the strip — pushed to the keeper the same
        // way; an inert signature stays listed, greyed, never notified.
        var finds = (j && j.ok && j.findings) ? j.findings.slice(0, 8) : [];
        // 1.0.146 (t4260): every station hold / skip / coalesce / inert switch
        // is a strip row too \u2014 a defer the operator cannot see is a silent hold.
        var holds = (j && j.ok && j.holds) ? j.holds.slice(0, 12) : [];
        strip.textContent = "";
        if (!rows.length && !recent.length && !finds.length && !holds.length) { strip.classList.remove("on"); return; }
        strip.classList.add("on");
        rows.concat(recent).concat(finds).concat(holds).forEach(function (l) {
          var isFind = String(l.source || "").indexOf("finding:") === 0;
          var isHold = !!l.hold;
          var row = document.createElement("div");
          row.className = "fv-loop" + ((l.active && !l.inert) ? "" : " cleared") + (isHold ? " hold" : "");
          var k = document.createElement("span"); k.className = "fv-loop-k";
          var holdK = { "station:switch": "\u2699 switch", "station:hold": "\u23f8 hold",
                        "station:skip": "\u26a0 skip", "station:pending": "\u23f3 pending" };
          k.textContent = isHold ? (holdK[l.source] || "\u23f8 hold")
                        : isFind ? (l.inert ? "\u00b7 inert" : "\ud83d\udc1e " + (l.severity || "finding"))
                                 : (l.active ? "\u26a0 loop" : "\u2713 cleared");
          var src = document.createElement("span"); src.className = "fv-loop-src"; src.textContent = l.source || "?";
          var id = document.createElement("span"); id.className = "fv-loop-id";
          id.textContent = (l.identity || "?") + " \u00d7" + (l.count || 0);
          var meta = document.createElement("span"); meta.className = "fv-loop-meta";
          meta.textContent = "first " + fmtLoopClock(l.first_seen) + " \u00b7 last " + fmtLoopClock(l.last_seen) +
            (l.active ? "" : " \u00b7 stopped " + fmtLoopClock(l.cleared_at)) + (l.board_id ? " \u00b7 board " + l.board_id : "") +
            (l.proposal_id ? " \u00b7 B proposed: " + l.proposal_id : "") +
            (l.inert ? " \u00b7 inert: " + (l.disposition_reason || "") : "");
          var doEl = document.createElement("span"); doEl.className = "fv-loop-do";
          doEl.textContent = "do: " + (l.action || "\u2014");
          var cp = document.createElement("button"); cp.className = "fv-loop-copy"; cp.textContent = "copy";
          cp.title = "copy the command to act on this loop";
          cp.onclick = function () { fvCopy(l.action || "", true); };
          row.title = (l.detail || "") + "\n\ndo: " + (l.action || "") + "\nkey: " + (l.key || "") +
            (l.sigkey ? "\nsignature: " + l.sigkey + " \u00b7 disposition: " + (l.disposition || "open") : "") +
            (l.reopen_reason ? "\nwhy now: " + l.reopen_reason : "");
          row.appendChild(k); row.appendChild(src); row.appendChild(id); row.appendChild(meta);
          row.appendChild(doEl); row.appendChild(cp);
          strip.appendChild(row);
        });
      })
      .catch(function () { /* backend unreachable: keep the last strip */ });
  }

  var _metaSeq = 0;
  function refreshMeta() {
    // availability follows the grounding: probe the active VM's seats
    var q = (activeVm && activeVm !== "@keeper")
      ? "?vm=" + encodeURIComponent(activeVm) : "";
    // 1.0.143: an unreachable locus answers late (its probe times out); a
    // reply for a locus that is no longer selected must not overwrite the
    // current one's surfaces (it re-disabled ⌂ shell after switching back).
    var seq = ++_metaSeq;
    fetch("/api/term/backends" + q, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (seq !== _metaSeq) return;
        if (!j || !j.frontier) return;
        // gate flags are metadata (chips deprecated 2026-09-17) — strip them
        // so SURFACES never grows phantom tabs; the client stays always-on.
        delete j.frontier_enabled;
        delete j.b_enabled;
        // t-serve: these are surface METADATA, not surfaces — strip them before
        // SURFACES is replaced, or list() would grow phantom tabs.
        KEEPER_SURFACE = j.keeper_surface || "tmux";
        // 1.0.101: remember the ACTIVE locus's own console path (host: /ac/)
        if (j.ac_serve && j.ac_serve.path && j.ac_serve.url) AC_LOCI[acLocusKey()] = { path: j.ac_serve.path, ok: !!j.ac_serve.ok, label: j.ac_serve.label || "" };
        else delete AC_LOCI[acLocusKey()];
        try { window.__keeperSurface && (window.__keeperSurface.surface = KEEPER_SURFACE,
              window.__keeperSurface.ok = !!(j.ac_serve && j.ac_serve.ok)); } catch (e) {}
        delete j.keeper_surface;
        delete j.ac_serve;
        if (j.frontier && j.frontier.backend_labels) {
          for (var bk in j.frontier.backend_labels) {
            if (Object.prototype.hasOwnProperty.call(j.frontier.backend_labels, bk))
              BACKEND_LABELS[bk] = j.frontier.backend_labels[bk];
          }
        }
        SURFACES = j;
        renderBar();
        acSync();                    // t296: the surface verdict just landed
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
    // (1.0.65: the backend buttons bind their own click in refreshMeta → setBackend)
    pane.querySelector("#fv-session").addEventListener("change", function (ev) {
      if (ev.target.value.indexOf("@model|") === 0) selectSessionModel(ev.target.value);
      else selectSession(ev.target.value);
    });
    pane.querySelector("#fv-session-new").addEventListener("click", newSession);
    pane.querySelector("#fv-shell").addEventListener("click", function () {
      if (surface === "shell") { show("frontier"); return; }
      if (shellInfo().available === false) { setStatus(); return; }
      show("shell");
    });
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
    function sendPrompt(text, s, opts) {
      s = s || surface;
      /* t296: while the serve console owns the frontier slot there is no PTY
         and no mct renderer to type into — say so instead of dereferencing a
         view that show() deliberately never built. */
      if (s === "frontier" && acOn()) {
        try { fvToast("frontier is the serve console — type in the pane", true); } catch (e) {}
        return false;
      }
      if (s === "frontier" && backendFor(s) === "mct") {
        show(s);
        if (!views[s] || !views[s].mct) return false;
        views[s].mct.submit(text, []).catch(function (err) { alert("MCT: " + err.message); });
        return true;
      }
      if (!views[s] || !views[s].ws || views[s].ws.readyState !== 1) show(s);
      var v = views[s];
      if (v && v.ws && v.ws.readyState === 1) {
        if (opts && opts.submit) {
          // claude-code (Ink/React TUI) treats a line whose text and trailing
          // Enter arrive in ONE write as a bracketed paste and inserts a
          // NEWLINE instead of submitting (operator 2026-09-10: "just placing
          // the text in the prompt"). Send the text, then the Enter as a
          // SEPARATE keystroke a beat later so the TUI — and the mct REPL —
          // actually submit the line.
          v.ws.send(enc.encode(text));
          var ws = v.ws;
          setTimeout(function () {
            try { if (ws.readyState === 1) ws.send(enc.encode("\r")); } catch (e) {}
          }, 140);
        } else {
          v.ws.send(enc.encode(text + "\r"));
        }
        return true;
      }
      return false;
    }
    window.__fvSend = sendPrompt;
    window.__fvMct = {
      submit: function (text, files) {
        if (backendFor("frontier") !== "mct") return Promise.reject(new Error("Select MCT first"));
        show("frontier");
        return views.frontier.mct.submit(text, files || []);
      }
    };
    // (the old one-line "prompt → frontier" bar is retired: the ✍ prompt
    // panel is the sanctioned input surface; the ▤ /cmds palette and the
    // A/B gate chips were deprecated 2026-09-17 — use the serve console)

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
    // (the ⧉ copy bar chip was deprecated 2026-09-17; copy still works from
    // every pane: right-click on a selection, Ctrl/Cmd+Shift+C → fvCopy)
    refreshMeta();
    open();                       // terminal-first: auto-open on load
  }
  window.__fvSurface = {   // the top bar drives the terminal surface through this
    set: function (s) { if (SURFACES[s] || PULLS[s]) show(s); },
    // ONE-CLICK WIPE (operator 2026-09-14): after a seat wipe kills this
    // surface's tmux session server-side, the operator's still-open socket
    // only closes a beat later — so show()'s "reconnect if closed" guard races
    // the wipe and skips, and the delayed close then sets _exited (which blocks
    // every auto-reconnect). reopen() puts the surface on stage AND force-starts
    // a fresh session unconditionally (restart(): drop the old socket, connect
    // → ws_hostterm `new-session -A` → a fresh, still-logged-in seat), grounded
    // on the active VM + chosen backend — so wiped host OR peer loci come back
    // alive in one click via the canonical seat launcher.
    reopen: function (s) {
      if (!(SURFACES[s] || PULLS[s])) return;
      show(s);
      /* t296: the serve console has no tmux session to relaunch — reload the
         proxied app instead, which is the equivalent "come back fresh". */
      if (acOn()) {
        if (acNativeApplicable()) { acNativeShow(); if (window.stationConsole) window.stationConsole.reload(); }
        else { try { document.getElementById("fv-ac-frame").src = acPathNow() || "/ac/"; } catch (e) {} }
        return;
      }
      restart(s);
    },
    get: function () { return surface; },
    // p510(a) RECOVER a frozen viewer from the UI (previously only possible from a
    // shell). First forceRepaint() clears a stuck xterm renderer (the common
    // freeze); then a NON-DESTRUCTIVE hard reattach — it mirrors show()'s reattach
    // but sends NO {t:"detach"}/{t:"kill"}, so the server-side tmux session and
    // scrollback survive: close the (maybe half-open) socket, connect() reattaches
    // via `new-session -A` + scrollback replay. reconnect() is deliberately NOT
    // used here — it early-returns on a still-OPEN socket.
    recover: function (s) {
      s = s || surface;
      if (!(SURFACES[s] || PULLS[s])) return;
      if (surface !== s) show(s);
      if (acOn()) { if (window.__fvSurface.reopen) window.__fvSurface.reopen(s); return; }
      var v = view(s);
      if (!v) return;
      forceRepaint(v);
      clearTimeout(v._retryT); v._retry = 0; v._exited = false;
      if (v.ws) { var old = v.ws; v.ws = null; try { old.close(); } catch (e) {} }
      v.term.reset();
      connect(s);
      setTimeout(function () { v.fit.fit(); sizeOf(v); focusView(v); }, 30);
    },
    list: function () { return Object.keys(SURFACES).concat(Object.keys(PULLS)); },
    // SESSION-PULL-PATCH 2026-09-02: attach a pulled / jump-in seat (a tmux session on the keeper
    // socket, spawned by /api/handoff/spawn) as its own terminal tab.
    attach: function (name) {
      name = String(name || "").trim().toLowerCase();
      if (!/^[a-z0-9][a-z0-9-]{0,40}$/.test(name)) return "";
      var key = "pull:" + name;
      PULLS[key] = { tmux: name };
      show(key);
      return key;
    },
    detachPull: function (key) {
      if (!PULLS[key]) return;
      var v = views[key];
      if (v) {
        if (v.ws) { var old = v.ws; v.ws = null; try { old.close(); } catch (e) {} }
        try { v.term.dispose(); } catch (e) {}
        try { v.el.remove(); } catch (e) {}
        delete views[key];
      }
      delete PULLS[key];
      if (surface === key) show("shell");
    }
  };
  var prevVm = null;   // 1.0.65: the locus chosen BEFORE the current one (the ↩ button)
  window.__fvVm = {   // the SPA reports its active VM; EVERY surface follows it
    prev: function () { return prevVm; },
    set: function (vm) {
      vm = vm || "";
      if (vm === activeVm) return;
      prevVm = activeVm;
      activeVm = vm;
      sessionModelChoicesLoaded = false;    // 1.0.141: model pickers are per locus
      if (SURFACES.shell) { delete SURFACES.shell.who; delete SURFACES.shell.available; delete SURFACES.shell.reason; }  // 1.0.143: ⌂ shell is per locus
      try { window.dispatchEvent(new CustomEvent("fv-vm", { detail: { vm: vm, prev: prevVm } })); } catch (e) {}
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
      if (!acSync() && !acOn() && views[surface]) { connect(surface); setStatus(); }
      refreshMeta();                        // availability is per-VM now
      refreshRollState();                   // rolling state is per-locus too
      try { refreshLoops(); } catch (e) {}  // 1.0.141: findings strip follows the locus
    },
    get: function () { return activeVm; }
  };
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);

  // (the ▤ /cmds palette — the mct REPL slash-command form — was deprecated
  //  2026-09-17: use the abstract-claude serve console; /help in the tmux
  //  seat still lists the REPL's commands)

})();
