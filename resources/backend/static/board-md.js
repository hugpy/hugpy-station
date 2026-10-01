/* board-md.js — STATIC-REFERENCE renderer for board items (station 1.0.144).
 *
 * Operator (2026-10-01): "The operator tab is mostly for cmd executions for the
 * operator, and it's difficult to parse the code manually from it. That and the
 * proposals should strictly retain the proper formatting outlined in the docs.
 * It's meant to be a static reference for operator and agent alike."
 * Standing convention (operator 2026-09-16): a command request is a fenced
 * block  ```{type} … ```  WITH A COPY BUTTON. The canonical layout is
 * docs/BOARD-ITEM-FORMAT.md (v1, 2026-10-01: sections `WHY:` `DO:` `VERIFY:` …,
 * one action per fenced block, no prompt markers, no placeholders); the older
 * TODO-BOARD-SOP §2 operator `cmd:` block (indented lines; `#` lines are
 * caveats, not copied) still renders as a copyable block.
 *
 * So ⚑ operator, ⚖ proposal, 🔖 bookmark and 🧭 direction items (text, note
 * and comments) render through THIS module instead of the compact one-line
 * row: full text, every newline kept, nothing truncated; code always visible.
 *
 * Two halves:
 *   PURE (node-testable, tests/board-md.test.js): parse, inline, copyPayload,
 *     commandBlocks, unformatted, looksLikeCommand, collapsePlan.
 *   RENDER: makeRenderer({html, useState, useEffect, copyText}) -> components
 *     {Item, Body, Code}. Every scrap of text rides as a React child (React
 *     escapes it) — never innerHTML, no links/URLs are made clickable — so the
 *     path is XSS-inert by construction.
 *
 * It NEVER rewrites the stored text: the "unformatted command" badge only
 * points at a command-looking line outside a fence so the writer can fix it.
 *
 * window.BoardMd in the page, module.exports under node.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BoardMd = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /* ---------------------------------------------------------------- parse -- */
  var FENCE_OPEN = /^(\s*)```\s*([A-Za-z0-9_+.#-]*)[^`]*$/;
  var FENCE_CLOSE = /^\s*```\s*$/;
  var HEADING = /^(#{1,4})\s+(\S.*)$/;
  var LIST = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
  var QUOTE = /^\s*>\s?(.*)$/;
  var HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
  var TABLE = /^\s*\|.*\|\s*$/;
  // SOP operator block: a `cmd:` line (optionally with the first command on it)
  var CMD_LABEL = /^\s*cmd:\s*(.*)$/i;
  var INDENTED = /^(?: {2,}|\t)\S/;
  var CAVEAT = /^\s*#/;

  function lines(src) {
    return String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n");
  }
  // a fence opener counts only when a closing ``` exists further down — an
  // unterminated fence stays literal text (never swallow the rest of the item)
  function fenceCloses(ls, i) {
    if (!FENCE_OPEN.test(ls[i])) return false;
    for (var j = i + 1; j < ls.length; j++) if (FENCE_CLOSE.test(ls[j])) return true;
    return false;
  }
  function dedent(ls, n) {
    if (!n) return ls;
    return ls.map(function (l) {
      var k = 0;
      while (k < n && k < l.length && (l[k] === " " || l[k] === "\t")) k++;
      return l.slice(k);
    });
  }
  function isBlockStart(ls, i) {
    var l = ls[i];
    return l.trim() === "" || fenceCloses(ls, i) || HEADING.test(l) || LIST.test(l) ||
      QUOTE.test(l) || HR.test(l) || TABLE.test(l) || CMD_LABEL.test(l);
  }

  /* text -> blocks. Kinds:
     {kind:"fence", lang, text}              ```lang … ``` (content verbatim)
     {kind:"cmd", lang:"cmd", text}          SOP `cmd:` + indented lines (+ `#` caveats)
     {kind:"h", level, text}
     {kind:"list", items:[{depth, marker, text}]}   markers kept as written
     {kind:"quote", text} | {kind:"table", text} | {kind:"hr"}
     {kind:"p", text}                        inner newlines preserved
     `line` on every block = its first source line index (badge anchoring). */
  function parse(src) {
    var ls = lines(src), out = [], i = 0;
    while (i < ls.length) {
      var l = ls[i];
      if (l.trim() === "") { i++; continue; }
      if (fenceCloses(ls, i)) {
        var m = FENCE_OPEN.exec(l), start = i, buf = [];
        i++;
        while (!FENCE_CLOSE.test(ls[i])) buf.push(ls[i++]);
        i++;
        out.push({ kind: "fence", lang: m[2] || "", text: dedent(buf, m[1].length).join("\n"), line: start });
        continue;
      }
      var c = CMD_LABEL.exec(l);
      if (c) {
        var cs = i, body = [];
        if (c[1].trim() !== "") body.push(c[1]);
        i++;
        while (i < ls.length && ls[i].trim() !== "" && (INDENTED.test(ls[i]) || CAVEAT.test(ls[i])))
          body.push(ls[i++]);
        if (body.length) {
          var ind = Math.min.apply(null, body.filter(function (b) { return !CAVEAT.test(b); })
            .map(function (b) { return /^\s*/.exec(b)[0].length; }).concat([99]));
          out.push({ kind: "cmd", lang: "cmd", text: dedent(body, ind === 99 ? 0 : ind).join("\n"), line: cs });
          continue;
        }
        out.push({ kind: "p", text: l, line: cs });   // a bare "cmd:" with nothing under it
        continue;
      }
      if (HR.test(l)) { out.push({ kind: "hr", line: i }); i++; continue; }
      var h = HEADING.exec(l);
      if (h) { out.push({ kind: "h", level: h[1].length, text: h[2].trim(), line: i }); i++; continue; }
      if (TABLE.test(l)) {
        var ts = i, tb = [];
        while (i < ls.length && TABLE.test(ls[i])) tb.push(ls[i++]);
        out.push({ kind: "table", text: tb.join("\n"), line: ts });
        continue;
      }
      if (QUOTE.test(l)) {
        var qs = i, qb = [];
        while (i < ls.length && QUOTE.test(ls[i])) qb.push(QUOTE.exec(ls[i++])[1]);
        out.push({ kind: "quote", text: qb.join("\n"), line: qs });
        continue;
      }
      if (LIST.test(l)) {
        var ls0 = i, items = [];
        while (i < ls.length) {
          var li = LIST.exec(ls[i]);
          if (li) {
            items.push({ depth: Math.min(3, Math.floor(li[1].replace(/\t/g, "  ").length / 2)),
                         marker: li[2], text: li[3], line: i });
            i++; continue;
          }
          // an indented non-bullet line continues the previous item (newline kept)
          if (ls[i].trim() !== "" && INDENTED.test(ls[i]) && !fenceCloses(ls, i) && !CMD_LABEL.test(ls[i])) {
            items[items.length - 1].text += "\n" + ls[i].trim();
            i++; continue;
          }
          break;
        }
        out.push({ kind: "list", items: items, line: ls0 });
        continue;
      }
      var ps = i, pb = [l];
      i++;
      while (i < ls.length && !isBlockStart(ls, i)) pb.push(ls[i++]);
      out.push({ kind: "p", text: pb.join("\n"), line: ps });
    }
    return out;
  }

  /* ---------------------------------------------------------------- inline -- */
  /* Section headers are emphasised so a reader can scan an item at a glance:
     BOARD-ITEM-FORMAT.md v1 (operator WHY / WHO / GATE / DO / VERIFY / EXPECT /
     ROLLBACK; proposal PROBLEM / OPTIONS / REC / DECISION / SKETCH; bookmark
     WHAT / WHERE / RETURN / VERIFIED; direction DERIVE / STATE) plus the older
     TODO-BOARD-SOP §2 short forms (SCOPE / DONE, SHIPPED, RULING, ASK, @worker /
     task / cmd). Emphasis only — the text is never moved or rewritten. */
  var LABEL = /(^|[\s/(—–-])((?:ROLLBACK RUN|RUN|WHY|WHO|GATE|DO|VERIFY|EXPECT|ROLLBACK|PROBLEM|OPTIONS|REC|DECISION|SKETCH|WHAT|WHERE|RETURN|VERIFIED|DERIVE|STATE|SCOPE|DONE|@worker|task|cmd)|(?:ASK [^:\n`]{1,40}?)|(?:SHIPPED[^\n`]{0,32}?)|(?:RULING(?: \([^)\n`]{0,60}\))?)):(?=\s|$)/g;
  function labels(s, out) {
    var last = 0, m;
    LABEL.lastIndex = 0;
    while ((m = LABEL.exec(s))) {
      var at = m.index + m[1].length;
      if (at > last) bold(s.slice(last, at), out);
      out.push({ t: m[2] + ":", label: true });
      last = at + m[2].length + 1;
    }
    if (last < s.length) bold(s.slice(last), out);
  }
  function bold(s, out) {
    var re = /\*\*([^*\n]+)\*\*/g, last = 0, m;
    while ((m = re.exec(s))) {
      if (m.index > last) out.push({ t: s.slice(last, m.index) });
      out.push({ t: m[1], bold: true });
      last = m.index + m[0].length;
    }
    if (last < s.length) out.push({ t: s.slice(last) });
  }
  /* text -> spans [{t, code?|bold?|label?}]. `code` spans are taken first and
     are never re-scanned (a ** or a label inside backticks stays literal). */
  function inline(text) {
    var s = String(text == null ? "" : text), out = [], re = /`([^`\n]+)`/g, last = 0, m;
    while ((m = re.exec(s))) {
      if (m.index > last) labels(s.slice(last, m.index), out);
      out.push({ t: m[1], code: true });
      last = m.index + m[0].length;
    }
    if (last < s.length) labels(s.slice(last), out);
    return out;
  }

  /* ------------------------------------------------------------- commands -- */
  var SHELL_LANGS = /^(?:sh|bash|shell|console|zsh|fish|cmd|sudo|root|ps|powershell|pwsh|terminal|shell-session)$/i;
  var CMD_START = new RegExp("^(?:" + [
    "\\$\\s+\\S", "sudo\\s+\\S",
    "(?:systemctl|journalctl|loginctl|setfacl|usermod|useradd|chown|chmod|kubectl|pkill|killall)\\s+\\S",
    "apt(?:-get)?\\s+(?:(?:install|update|upgrade|remove|purge)\\b|-)", "dpkg\\s+-",
    "ufw\\s+(?:(?:allow|deny|status|enable|disable|reload|delete|route)\\b|-)",
    "(?:docker|podman)\\s+(?:(?:run|exec|ps|compose|pull|build|stop|start|restart|rm|logs)\\b|-)",
    "virsh\\s+(?:(?:start|shutdown|destroy|list|dominfo|console|snapshot\\S*|define|undefine|edit)\\b|-)",
    "nginx\\s+-", "certbot\\s+(?:(?:renew|certonly|certificates)\\b|-)", "reboot\\s*$",
    "npm\\s+(?:(?:install|i|ci|run|exec|publish|test)\\b|-)", "npx\\s+\\S", "node\\s+(?:-|\\S+\\.[cm]?js\\b)",
    "pip3?\\s+(?:(?:install|uninstall|show|list|freeze|download)\\b|-)",
    "git\\s+(?:clone|pull|push|fetch|checkout|switch|commit|log|status|diff|reset|worktree|tag|merge|rebase|show|-C)\\b",
    "(?:ssh|curl|wget|scp|rsync)\\s+(?:-|\\S+@|https?:|\\S*[/:])",
    "python3?\\s+(?:-m\\s|-c\\s|\\S+\\.py\\b)",
    "(?:cd|mkdir|rm|cp|mv|ln|cat|tee|install)\\s+(?:-\\S+\\s+)*[~/.]\\S*",
    "kill\\s+-?\\d", "export\\s+[A-Z_][A-Z0-9_]*=",
  ].join("|") + ")");
  var SUDO_MID = /\bsudo\s+(?:-\S+\s+)*(?:systemctl|apt|apt-get|dpkg|install|tee|ufw|chown|chmod|setfacl|usermod|loginctl|nginx|mkdir|ln|rm|cp|mv|bash|sh|cat|journalctl|reboot|virsh|docker|python3?|pip3?|-u\s|-i\b|\/)/;
  /* one line, outside any fence: does it read as a shell command? Inline `code`
     is excluded (a command in backticks is already formatted), list/quote
     markers are stripped first. */
  function looksLikeCommand(line) {
    var s = String(line == null ? "" : line).replace(/`[^`\n]+`/g, " ").trim();
    s = s.replace(/^(?:>\s*)+/, "").replace(/^(?:[-*+]|\d{1,3}[.)])\s+/, "").trim();
    s = s.replace(/^[A-Z][A-Z_]{1,15}:\s*/, "");          // "VERIFY: systemctl …" — a header, then a bare command
    if (!s) return false;
    return CMD_START.test(s) || SUDO_MID.test(s);
  }
  function isCommandBlock(b) {
    if (b.kind === "cmd") return true;
    if (b.kind !== "fence") return false;
    if (SHELL_LANGS.test(b.lang)) return true;
    if (b.lang) return false;
    return lines(b.text).some(looksLikeCommand);
  }
  /* What ⧉ puts on the clipboard — the block's content exactly, minus:
     - prompt markers ("$ ", ">>> " / "... "): BOARD-ITEM-FORMAT.md says a block
       carries none, but a legacy transcript block copies only its prompted
       lines (marker stripped) — the unprompted rest is output;
     - for a SOP `cmd:` block, its `#` caveat lines (SOP: "not copied").
     Never the fence markers or the language label. */
  function copyPayload(b) {
    var ls = lines(b.text);
    if (b.kind === "cmd") return ls.filter(function (l) { return !CAVEAT.test(l); }).join("\n");
    var PR = /^\s*(?:\$|>>>)\s/, CONT = /^\s*\.\.\.\s/;
    var py = ls.some(function (l) { return /^\s*>>>\s/.test(l); });
    var prompted = ls.filter(function (l) { return PR.test(l) || (py && CONT.test(l)); });
    if (prompted.length) return prompted.map(function (l) { return l.replace(PR, "").replace(CONT, ""); }).join("\n");
    return b.text;
  }
  /* BOARD-ITEM-FORMAT.md §0: no placeholders (`<host>`, `{{x}}`) in a command
     block — they would be pasted verbatim. -> the distinct placeholders found. */
  function placeholders(b) {
    if (!isCommandBlock(b)) return [];
    var seen = {}, out = [], re = /(^|[^<\w])(<[a-z][a-z0-9_ -]{0,24}>|\{\{[^}\n]{1,30}\}\})/gi, m, t = copyPayload(b);
    while ((m = re.exec(t))) if (!seen[m[2]]) { seen[m[2]] = true; out.push(m[2]); }
    return out;
  }
  /* Operator 2026-10-01: an operator item carries a required `RUN:` section —
     ONE fenced ```bash block that is a complete, self-contained script the
     operator copies and runs from ~ on their host — and an optional
     `ROLLBACK RUN:` second one. A section header is a line that opens with it
     (content may follow on the same line); its script is the FIRST fence after
     the header, before the next header. -> {run: block|null, rollback: block|null}.
     Mark-only: a missing RUN is badged, never synthesised. */
  var RUN_RE = /^\s*(?:\*\*|#+\s*)?(ROLLBACK RUN|RUN)\s*:/i;
  var HEADER_RE = /^\s*(?:\*\*|#+\s*)?[A-Z][A-Z ]{1,20}:/;
  function scripts(src) {
    var ls = lines(src), out = { run: null, rollback: null };
    var bl = parse(src);
    var fences = bl.filter(function (b) { return b.kind === "fence" || b.kind === "cmd"; });
    var heads = [];
    bl.forEach(function (b) {               // only prose blocks carry headers
      if (b.kind !== "p" && b.kind !== "h" && b.kind !== "list" && b.kind !== "quote") return;
      var n = b.kind === "list" ? b.items.length : lines(b.text).length;
      for (var k = 0; k < n; k++) {
        var raw = b.kind === "list" ? ls[b.items[k].line] : ls[b.line + k];
        if (raw == null) continue;
        var m = RUN_RE.exec(raw.replace(/^\s*(?:[-*+]|\d{1,3}[.)])\s+/, ""));
        if (m) heads.push({ key: m[1].toUpperCase() === "RUN" ? "run" : "rollback", line: b.kind === "list" ? b.items[k].line : b.line + k });
        else if (HEADER_RE.test(raw)) heads.push({ key: null, line: b.line + k });
      }
    });
    heads.sort(function (a, b) { return a.line - b.line; });
    heads.forEach(function (h, i) {
      if (!h.key || out[h.key]) return;
      var end = i + 1 < heads.length ? heads[i + 1].line : Infinity;
      var f = fences.find(function (b) { return b.line > h.line && b.line < end; });
      if (f) out[h.key] = f;
    });
    return out;
  }
  /* a one-liner field: a single non-empty line, or "" (anything else — null,
     a multi-line string, an object — renders nothing). Never rewritten. */
  function oneLiner(v) {
    if (typeof v !== "string") return "";
    var t = v.trim();
    return t && t.indexOf("\n") < 0 ? t : "";
  }
  /* a station-attached run result: {exit, path, ts, tail, kind} with any field
     missing -> a normalised view, or null when there is nothing to show.
     `exit` becomes a number or null; ts an epoch (s or ms) or a string; nothing
     is invented for an absent field. Never rewritten. */
  function resultView(r) {
    if (!r || typeof r !== "object") return null;
    var out = { exit: null, path: "", ts: "", tail: "", kind: "" };
    if (r.exit !== undefined && r.exit !== null && r.exit !== "" && isFinite(Number(r.exit))) out.exit = Number(r.exit);
    if (typeof r.path === "string" && r.path.trim()) out.path = r.path.trim();
    if (typeof r.tail === "string" && r.tail.trim() !== "") out.tail = r.tail.replace(/\r\n?/g, "\n").replace(/\n+$/, "");
    if (r.kind === "run" || r.kind === "rollback") out.kind = r.kind;
    if (typeof r.ts === "number" && isFinite(r.ts)) {
      var ms = r.ts > 1e12 ? r.ts : r.ts * 1000;
      out.ts = new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
    } else if (typeof r.ts === "string" && r.ts.trim()) out.ts = r.ts.trim();
    return (out.exit !== null || out.path || out.ts || out.tail) ? out : null;
  }
  /* every runnable block across several sources, in reading order */
  function commandBlocks(srcs) {
    var out = [];
    (Array.isArray(srcs) ? srcs : [srcs]).forEach(function (s) {
      parse(s).forEach(function (b) { if (isCommandBlock(b)) out.push(b); });
    });
    return out;
  }
  /* command-looking lines that sit OUTSIDE a fence / cmd: block ->
     [{line (0-based, in src), text}]. Headings are skipped (a "# caveat" style
     line is prose). */
  function unformatted(src) {
    var ls = lines(src), out = [];
    parse(src).forEach(function (b) {
      if (b.kind === "fence" || b.kind === "cmd" || b.kind === "hr" || b.kind === "h") return;
      var n = b.kind === "list" ? null : lines(b.text).length;
      if (b.kind === "list") {
        b.items.forEach(function (it) {
          lines(it.text).forEach(function (t, k) {
            if (looksLikeCommand(t)) out.push({ line: it.line + k, text: String(ls[it.line + k]).trim() });
          });
        });
        return;
      }
      for (var k = 0; k < n; k++) {
        var raw = ls[b.line + k];
        if (raw != null && looksLikeCommand(raw)) out.push({ line: b.line + k, text: raw.trim() });
      }
    });
    return out;
  }

  /* -------------------------------------------------------------- collapse -- */
  var LONG_LINES = 14, KEEP_LINES = 8;
  function proseLines(b) {
    if (b.kind === "hr") return 1;
    if (b.kind === "h") return 1;
    if (b.kind === "list") return b.items.reduce(function (n, it) { return n + lines(it.text).length; }, 0);
    return lines(b.text).length;
  }
  /* blocks -> {long, hidden:Set(index)}. Long = more than LONG_LINES prose
     lines; collapsed, prose past the first KEEP_LINES hides behind the expander
     while EVERY fence / cmd block stays visible (operator: keep the code). */
  function collapsePlan(blockLists) {
    var all = [];
    blockLists.forEach(function (bl, s) { bl.forEach(function (b, i) { all.push({ s: s, i: i, b: b }); }); });
    var prose = all.filter(function (x) { return !isCode(x.b); })
      .reduce(function (n, x) { return n + proseLines(x.b); }, 0);
    var hidden = {}, used = 0, hiddenLines = 0;
    if (prose <= LONG_LINES) return { long: false, hidden: hidden, hiddenLines: 0 };
    all.forEach(function (x) {
      if (isCode(x.b)) return;
      var n = proseLines(x.b);
      if (used >= KEEP_LINES) { hidden[x.s + ":" + x.i] = true; hiddenLines += n; }
      used += n;
    });
    return { long: true, hidden: hidden, hiddenLines: hiddenLines };
  }
  function isCode(b) { return b.kind === "fence" || b.kind === "cmd" || b.kind === "table"; }

  /* ---------------------------------------------------------------- render -- */
  /* styles ride with the module (one <style>, injected once) so index.html only
     carries the hooks — the station palette vars (--bg/--fg/--dim/--edge/
     --accent/--warn/--ok) are the page's own. */
  var CSS = [
    ".bmd{display:block;flex:1 1 100%;min-width:0;overflow-wrap:anywhere;color:var(--fg);}",
    ".bmd-item{margin-top:3px;}",
    ".bmd.bmd-inl{display:inline;}.bmd-inl>.bmd-p:first-child{display:inline;}",
    ".bmd-item.done .bmd-text{color:var(--dim);}",
    ".bmd-p{margin:0 0 6px;white-space:pre-wrap;}",
    ".bmd>*:last-child,.bmd-text>*:last-child,.bmd-note>*:last-child{margin-bottom:0;}",
    ".bmd-h{font-weight:600;margin:6px 0 4px;}.bmd-h1{font-size:1.12em;}.bmd-h2{font-size:1.06em;}.bmd-h3,.bmd-h4{font-size:1em;color:var(--dim);}",
    ".bmd-ul{list-style:none;margin:0 0 6px;padding-left:4px;}",
    ".bmd-ul li{display:flex;gap:6px;margin-bottom:2px;}",
    ".bmd-mk{flex:none;color:var(--dim);min-width:1ch;}",
    ".bmd-li{white-space:pre-wrap;min-width:0;}",
    ".bmd-quote{margin:0 0 6px;padding:2px 8px;border-left:2px solid var(--edge);color:var(--dim);white-space:pre-wrap;}",
    ".bmd-hr{border:none;border-top:1px solid var(--edge);margin:6px 0;}",
    ".bmd-table{margin:0 0 6px;font:12px/1.4 ui-monospace,monospace;white-space:pre;overflow-x:auto;}",
    ".bmd-code{font-family:ui-monospace,monospace;font-size:.92em;background:var(--bg);border:1px solid var(--edge);border-radius:4px;padding:0 3px;}",
    ".bmd-label{color:var(--accent);font-weight:600;}",
    ".bmd-pre{margin:3px 0 6px;border:1px solid var(--edge);border-radius:6px;background:var(--bg);min-width:0;max-width:100%;}",
    ".bmd-pre.run{border-left:3px solid var(--accent);}",
    ".bmd-pre-bar{display:flex;align-items:center;gap:6px;padding:1px 4px 1px 8px;border-bottom:1px solid var(--edge);font-size:10px;}",
    ".bmd-lang{text-transform:lowercase;letter-spacing:.4px;color:var(--dim);font-family:ui-monospace,monospace;flex:1;}",
    ".bmd-lang.none{font-style:italic;opacity:.7;}",
    ".bmd-copy,.bmd-copyall,.bmd-fold,.bmd-more{background:none;border:1px solid var(--edge);border-radius:4px;color:var(--dim);cursor:pointer;font:inherit;font-size:10px;padding:0 5px;line-height:1.6;}",
    ".bmd-copy:hover,.bmd-copyall:hover,.bmd-fold:hover,.bmd-more:hover{color:var(--accent);border-color:var(--accent);}",
    ".bmd-copy.ok,.bmd-copyall.ok{color:var(--ok);border-color:var(--ok);}",
    ".bmd-pre.bmd-script-run{border-left:3px solid var(--ok);}",
    ".bmd-pre.bmd-script-rollback{border-left:3px solid var(--warn);}",
    ".bmd-role{font-size:10px;letter-spacing:.5px;color:var(--ok);font-weight:600;}",
    ".bmd-script-rollback .bmd-role{color:var(--warn);}",
    ".bmd-copy.run,.bmd-script-btn.run{color:var(--ok);border-color:var(--ok);font-weight:600;}",
    ".bmd-copy.rollback,.bmd-script-btn.rollback{color:var(--warn);border-color:var(--warn);}",
    ".bmd-script-btn{font-size:11px;padding:1px 8px;}",
    ".bmd-ol-row{display:flex;flex-direction:column;gap:3px;margin:0 0 5px;}",
    ".bmd-ol{display:flex;align-items:center;gap:8px;min-width:0;padding:3px 8px;border:1px solid var(--edge);border-radius:6px;background:var(--bg);}",
    ".bmd-ol.run{border-left:3px solid var(--ok);}.bmd-ol.rollback{border-left:3px solid var(--warn);}",
    ".bmd-ol.rollback .bmd-role{color:var(--warn);}",
    ".bmd-res{margin:0 0 5px;padding:3px 8px;border:1px solid var(--edge);border-radius:6px;background:var(--bg);font-size:11px;}",
    ".bmd-res.ok{border-left:3px solid var(--ok);}.bmd-res.bad{border-left:3px solid var(--bad);}",
    ".bmd-res-head{display:flex;align-items:center;gap:6px;flex-wrap:wrap;}",
    ".bmd-res-exit{font-weight:600;}.bmd-res.ok .bmd-res-exit{color:var(--ok);}.bmd-res.bad .bmd-res-exit{color:var(--bad);}",
    ".bmd-res-ts{color:var(--dim);}.bmd-res-fold{margin-left:auto;}",
    ".bmd-role.rollback{color:var(--warn);}",
    ".bmd-res-path{display:flex;align-items:center;gap:8px;min-width:0;margin-top:2px;}",
    ".bmd-res-tail{margin:4px 0 2px;padding:5px 8px;background:var(--panel);border:1px solid var(--edge);border-radius:5px;font:11px/1.4 ui-monospace,monospace;white-space:pre;overflow-x:auto;max-height:240px;overflow-y:auto;color:var(--fg);}",
    ".bmd-ol-cmd{flex:1;min-width:0;font:12px/1.5 ui-monospace,monospace;white-space:nowrap;overflow-x:auto;color:var(--fg);}",
    ".bmd-pre pre{margin:0;padding:6px 8px;overflow-x:auto;font:12px/1.45 ui-monospace,monospace;white-space:pre;color:var(--fg);}",
    ".bmd-cmt{color:var(--dim);}",
    ".bmd-tools{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 4px;}",
    ".bmd-badge{font-size:10px;color:var(--warn);border:1px dashed var(--warn);border-radius:4px;padding:0 5px;cursor:help;opacity:.85;}",
    ".bmd-unfmt{text-decoration:underline dotted var(--warn);text-underline-offset:3px;}",
    ".bmd-more{display:block;margin:0 0 6px;}",
    ".bmd-note{margin-top:4px;padding-left:8px;border-left:2px solid var(--edge);color:var(--fg);}",
    ".bmd-note-lbl{display:block;font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px;}",
  ].join("\n");
  function injectStyle(doc) {
    if (!doc || !doc.head || doc.getElementById("board-md-css")) return;
    var st = doc.createElement("style");
    st.id = "board-md-css";
    st.textContent = CSS;
    doc.head.appendChild(st);
  }

  function makeRenderer(d) {
    var html = d.html, useState = d.useState, useEffect = d.useEffect, copyText = d.copyText;
    injectStyle(d.document || (typeof document !== "undefined" ? document : null));

    function useTick() {
      var st = useState(false), ok = st[0], set = st[1];
      useEffect(function () {
        if (!ok) return undefined;
        var t = setTimeout(function () { set(false); }, 1200);
        return function () { clearTimeout(t); };
      }, [ok]);
      return [ok, set];
    }
    var stop = function (e) { e.stopPropagation(); };

    function spans(text) {
      return inline(text).map(function (sp, i) {
        if (sp.code) return html`<code key=${i} className="bmd-code">${sp.t}</code>`;
        if (sp.label) return html`<b key=${i} className="bmd-label">${sp.t}</b>`;
        if (sp.bold) return html`<b key=${i}>${sp.t}</b>`;
        return sp.t;
      });
    }
    // paragraph text line-by-line so a flagged line can carry its own mark
    function lineSpans(text, firstLine, flagged) {
      var ls = lines(text);
      return ls.map(function (l, k) {
        var bad = flagged && flagged[firstLine + k];
        return html`<span key=${k} className=${bad ? "bmd-unfmt" : undefined}
            title=${bad ? "unformatted command — fence it as ```bash … ``` (TODO-BOARD-SOP §2)" : undefined}>${spans(l)}${k < ls.length - 1 ? "\n" : ""}</span>`;
      });
    }

    function Code(p) {
      var b = p.block, t = useTick(), ok = t[0], setOk = t[1];
      var payload = copyPayload(b), ph = placeholders(b);
      var cmd = b.kind === "cmd";
      var label = cmd ? "cmd" : (b.lang || "code");
      var ls = lines(b.text);
      var role = p.role || "";             // "run" | "rollback" — the item's script blocks
      var roleLbl = role === "run" ? "▶ copy script" : role === "rollback" ? "↩ copy rollback script" : "⧉ copy";
      return html`
        <div className=${"bmd-pre" + (isCommandBlock(b) ? " run" : "") + (role ? " bmd-script bmd-script-" + role : "")} onDblClick=${stop}>
          <div className="bmd-pre-bar">
            <span className=${"bmd-lang" + (b.lang || cmd ? "" : " none")}
                  title=${b.lang || cmd ? "block type" : "no language tag — the convention is ```{type}"}>${label}</span>
            ${ph.length > 0 && html`<span className="bmd-badge" title=${"placeholder(s) pasted verbatim — state the real value (BOARD-ITEM-FORMAT.md §0): " + ph.join(" ")}>⚠ placeholder</span>`}
            ${role && html`<span className="bmd-role" title=${role === "run" ? "RUN: the self-contained script — copy and run from ~ on your host" : "ROLLBACK RUN: undoes the RUN script"}>${role === "run" ? "RUN" : "ROLLBACK RUN"}</span>`}
            <button className=${"bmd-copy" + (role ? " " + role : "") + (ok ? " ok" : "")}
                    title=${"copy exactly this block's content" + (payload !== b.text ? " (prompt markers / # caveats left out)" : "")}
                    onClick=${function (e) { e.stopPropagation(); if (copyText(payload)) { setOk(true); if (p.onCopy) p.onCopy(role ? (role === "run" ? "script copied" : "rollback script copied") : "block copied"); } }}>
              ${ok ? "✓ copied" : roleLbl}</button>
          </div>
          <pre><code>${cmd
            ? ls.map(function (l, k) { return html`<span key=${k} className=${CAVEAT.test(l) ? "bmd-cmt" : undefined}>${l}${k < ls.length - 1 ? "\n" : ""}</span>`; })
            : b.text}</code></pre>
        </div>`;
    }

    // scripts() re-parses, so blocks match by position (same source), not identity
    var same = function (a, b) { return !!(a && b && a.line === b.line && a.kind === b.kind); };
    var roleOf = function (b, roles) { return roles ? (same(roles.run, b) ? "run" : same(roles.rollback, b) ? "rollback" : "") : ""; };
    function block(b, k, flagged, onCopy, roles) {
      switch (b.kind) {
        case "fence": case "cmd": return html`<${Code} key=${k} block=${b} onCopy=${onCopy} role=${roleOf(b, roles)} />`;
        case "hr": return html`<hr key=${k} className="bmd-hr" />`;
        case "h": return html`<div key=${k} className=${"bmd-h bmd-h" + b.level}>${spans(b.text)}</div>`;
        case "table": return html`<pre key=${k} className="bmd-table">${b.text}</pre>`;
        case "quote": return html`<blockquote key=${k} className="bmd-quote">${lineSpans(b.text, b.line, flagged)}</blockquote>`;
        case "list": return html`<ul key=${k} className="bmd-ul">${b.items.map(function (it, j) {
          var bad = flagged && lines(it.text).some(function (_, q) { return flagged[it.line + q]; });
          return html`<li key=${j} style=${{ marginLeft: it.depth * 16 + "px" }}>
            <span className="bmd-mk">${/^\d/.test(it.marker) ? it.marker : "•"}</span>
            <span className=${"bmd-li" + (bad ? " bmd-unfmt" : "")}>${lineSpans(it.text, -1, null)}</span></li>`;
        })}</ul>`;
        default: return html`<p key=${k} className="bmd-p">${lineSpans(b.text, b.line, flagged)}</p>`;
      }
    }
    function flagMap(src) {
      var m = {};
      unformatted(src).forEach(function (u) { m[u.line] = true; });
      return m;
    }
    // blocks of one source, honouring a collapse plan for source index s
    function blocksView(bl, s, plan, open, flagged, onCopy, onOpen, roles) {
      var out = [], gap = 0, k = 0;
      var flush = function () {
        if (!gap) return;
        out.push(html`<button key=${"gap" + k++} className="bmd-more" onClick=${function (e) { e.stopPropagation(); onOpen(); }}
            title="show the full text">⋯ ${gap} more line${gap === 1 ? "" : "s"}</button>`);
        gap = 0;
      };
      bl.forEach(function (b, i) {
        if (!open && plan.hidden[s + ":" + i]) { gap += proseLines(b); return; }
        flush();
        out.push(block(b, "b" + i, flagged, onCopy, roles));
      });
      flush();
      return out;
    }

    /* Body: one markdown source (a comment, a pros/rec line). */
    function Body(p) {
      var flagged = flagMap(p.src);
      return html`<div className=${"bmd" + (p.cls ? " " + p.cls : "")}>${parse(p.src).map(function (b, i) { return block(b, i, flagged, p.onCopy); })}</div>`;
    }

    /* Item: an item's text + note as one static reference — the unformatted-
       command badge, the per-item "copy all commands", and the long-item
       expander (code stays visible while collapsed). `extraCmd` = the item's
       structured `cmd` field payload (rendered by the row) so copy-all covers
       it too. `noteCls`/`noteLabel` style the note sub-block. */
    /* a station-generated one-liner: one line of monospace + its own copy button */
    function OneLiner(p) {
      var t = useTick(), ok = t[0], setOk = t[1];
      var run = p.role === "run";
      return html`
        <div className=${"bmd-ol " + p.role} onDblClick=${stop}
             title=${run ? "one-liner: runs the RUN script the station wrote to disk" : "one-liner: runs the ROLLBACK RUN script the station wrote to disk"}>
          <span className="bmd-role">${run ? "ONE-LINER" : "ROLLBACK ONE-LINER"}</span>
          <code className="bmd-ol-cmd">${p.cmd}</code>
          <button className=${"bmd-copy " + p.role + (ok ? " ok" : "")}
                  title=${"copy exactly this command" + (run ? "" : " (rollback)")}
                  onClick=${function (e) { e.stopPropagation(); if (copyText(p.cmd)) { setOk(true); if (p.onCopy) p.onCopy(run ? "one-liner copied" : "rollback one-liner copied"); } }}>
            ${ok ? "✓ copied" : (run ? "⧉ copy one-liner" : "⧉ copy rollback one-liner")}</button>
        </div>`;
    }
    /* a station-attached run result, directly under the one-liner row */
    function Result(p) {
      var r = p.result;
      var t = useTick(), ok = t[0], setOk = t[1];
      var st = useState(false), open = st[0], setOpen = st[1];
      var okExit = r.exit === 0, hasExit = r.exit !== null;
      var tailLines = r.tail ? lines(r.tail).length : 0;
      return html`
        <div className=${"bmd-res" + (hasExit ? (okExit ? " ok" : " bad") : "")} onDblClick=${stop}>
          <div className="bmd-res-head">
            ${r.kind && html`<span className=${"bmd-role " + r.kind}>${r.kind === "run" ? "RUN RESULT" : "ROLLBACK RESULT"}</span>`}
            ${hasExit && html`<span className="bmd-res-exit" title=${okExit ? "the script exited 0" : "the script exited non-zero"}>${okExit ? "✔ exit 0" : "✖ exit " + r.exit}</span>`}
            ${r.ts && html`<span className="bmd-res-ts">${hasExit ? " · " : ""}${r.ts}</span>`}
            ${r.tail && html`<button className="bmd-fold bmd-res-fold" onClick=${function (e) { e.stopPropagation(); setOpen(!open); }}
                title=${open ? "hide the log tail" : "show the last " + tailLines + " line" + (tailLines === 1 ? "" : "s") + " of the log"}>
                ${open ? "− tail" : "+ tail (" + tailLines + ")"}</button>`}
          </div>
          ${r.path && html`<div className="bmd-res-path">
            <code className="bmd-ol-cmd" title="the log file on the station host">${r.path}</code>
            <button className=${"bmd-copy" + (ok ? " ok" : "")} title="copy the log path"
                    onClick=${function (e) { e.stopPropagation(); if (copyText(r.path)) { setOk(true); if (p.onCopy) p.onCopy("path copied"); } }}>
              ${ok ? "✓ copied" : "⧉ copy path"}</button></div>`}
          ${open && r.tail && html`<pre className="bmd-res-tail"><code>${r.tail}</code></pre>`}
        </div>`;
    }
    /* `operator` true (a ⚑ item): the RUN / ROLLBACK RUN script blocks get the
       prominent "▶ copy script" / "↩ copy rollback script" buttons (top of the
       item AND beside the block); "copy all N commands" stays only for an item
       with NO RUN; no RUN at all -> "⚠ no RUN script" badge. */
    function Item(p) {
      var st = useState(false), open = st[0], setOpen = st[1];
      var t = useTick(), ok = t[0], setOk = t[1];
      var tr = useTick(), okRun = tr[0], setOkRun = tr[1];
      var tb = useTick(), okRb = tb[0], setOkRb = tb[1];
      var srcs = [p.text || "", p.note || ""];
      var bls = srcs.map(parse);
      var plan = collapsePlan(bls);
      var flags = srcs.map(flagMap);
      var bad = unformatted(srcs[0]).concat(unformatted(srcs[1]));
      var sc = scripts(srcs[1]), roleSrc = 1;
      if (!sc.run && !sc.rollback) { sc = scripts(srcs[0]); roleSrc = 0; }
      var noRun = !!p.operator && !sc.run;
      var runs = bls[0].concat(bls[1]).filter(isCommandBlock).map(copyPayload);
      if (p.extraCmd) runs.push(p.extraCmd);
      var all = runs.join("\n");
      var copyAll = !sc.run && runs.length > 1;
      var onOpen = function () { setOpen(true); };
      var copyScript = function (b, set, msg) {
        return function (e) { e.stopPropagation(); if (copyText(copyPayload(b))) { set(true); if (p.onCopy) p.onCopy(msg); } };
      };
      // station-generated one-liners (the RUN block materialised to a file on disk):
      // `one_liner` / `rollback_one_liner` on the item JSON. Absent -> nothing at all.
      var ol = oneLiner(p.oneLiner), rol = oneLiner(p.rollbackOneLiner);
      var res = resultView(p.result);
      return html`
        <div className=${"bmd bmd-item" + (p.done ? " done" : "")} onDblClick=${p.onDblClick}>
          ${(ol || rol) && html`<div className="bmd-ol-row">
            ${ol && html`<${OneLiner} cmd=${ol} role="run" onCopy=${p.onCopy} />`}
            ${rol && html`<${OneLiner} cmd=${rol} role="rollback" onCopy=${p.onCopy} />`}
          </div>`}
          ${res && html`<${Result} result=${res} onCopy=${p.onCopy} />`}
          ${(bad.length > 0 || copyAll || plan.long || sc.run || sc.rollback || noRun) && html`
            <div className="bmd-tools">
              ${sc.run && html`<button className=${"bmd-copyall bmd-script-btn run" + (okRun ? " ok" : "")}
                  title="copy the RUN script exactly — paste and run it from ~ on your host"
                  onClick=${copyScript(sc.run, setOkRun, "script copied")}>${okRun ? "✓ script copied" : "▶ copy script"}</button>`}
              ${sc.rollback && html`<button className=${"bmd-copyall bmd-script-btn rollback" + (okRb ? " ok" : "")}
                  title="copy the ROLLBACK RUN script exactly — undoes the RUN script"
                  onClick=${copyScript(sc.rollback, setOkRb, "rollback script copied")}>${okRb ? "✓ rollback copied" : "↩ copy rollback script"}</button>`}
              ${noRun && html`<span className="bmd-badge"
                  title="operator items carry a RUN: section — one fenced bash block, a complete self-contained script run from ~ (BOARD-ITEM-FORMAT.md). Not rewritten here: fix the item at its source.">⚠ no RUN script</span>`}
              ${bad.length > 0 && html`<span className="bmd-badge"
                  title=${"command-looking line" + (bad.length === 1 ? "" : "s") + " outside a fenced block — the convention is ```{type} … ``` (TODO-BOARD-SOP §2):\n" +
                          bad.map(function (u) { return "  " + u.text; }).join("\n")}>
                  ⚠ unformatted command${bad.length === 1 ? "" : "s"} (${bad.length})</span>`}
              ${copyAll && html`<button className=${"bmd-copyall" + (ok ? " ok" : "")}
                  title=${"copy all " + runs.length + " command blocks, in order, one after another"}
                  onClick=${function (e) { e.stopPropagation(); if (copyText(all)) { setOk(true); if (p.onCopy) p.onCopy("all commands copied"); } }}>
                  ${ok ? "✓ copied" : "⧉ copy all " + runs.length + " commands"}</button>`}
              ${plan.long && html`<button className="bmd-fold" onClick=${function (e) { e.stopPropagation(); setOpen(!open); }}
                  title=${open ? "collapse the prose (code stays visible)" : "show the full text"}>
                  ${open ? "− collapse" : "+ expand (" + plan.hiddenLines + " lines)"}</button>`}
            </div>`}
          ${srcs[0].trim() !== "" && html`<div className="bmd-text">${blocksView(bls[0], 0, plan, open, flags[0], p.onCopy, onOpen, roleSrc === 0 ? sc : null)}</div>`}
          ${srcs[1].trim() !== "" && html`<div className=${"bmd-note" + (p.noteCls ? " " + p.noteCls : "")}>
              ${p.noteLabel && html`<span className="bmd-note-lbl">${p.noteLabel}</span>`}
              ${blocksView(bls[1], 1, plan, open, flags[1], p.onCopy, onOpen, roleSrc === 1 ? sc : null)}</div>`}
        </div>`;
    }

    return { Item: Item, Body: Body, Code: Code, OneLiner: OneLiner, Result: Result };
  }

  return { parse: parse, inline: inline, copyPayload: copyPayload, commandBlocks: commandBlocks,
           isCommandBlock: isCommandBlock, placeholders: placeholders, scripts: scripts, oneLiner: oneLiner, resultView: resultView, looksLikeCommand: looksLikeCommand, unformatted: unformatted,
           collapsePlan: collapsePlan, makeRenderer: makeRenderer, LONG_LINES: LONG_LINES, KEEP_LINES: KEEP_LINES };
});
