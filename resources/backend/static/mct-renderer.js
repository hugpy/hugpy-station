/* MCT displays ordered conversation events, never native terminal bytes. */
(function () {
  'use strict';
  var style = document.createElement('style');
  style.textContent = '.mct-conversation{position:absolute;inset:0;display:flex;flex-direction:column;background:#0d1117;color:#e6e9ef;font:14px/1.55 system-ui,sans-serif}.mct-history{flex:1;min-height:0;overflow:auto;padding:18px max(16px,calc((100% - 900px)/2))}.mct-turn{margin-bottom:24px}.mct-message{white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0}.mct-user{padding:12px 16px;background:#182333;border:1px solid #2b405a;border-radius:10px}.mct-label{font-size:11px;letter-spacing:.07em;color:#8da9cb;text-transform:uppercase;white-space:normal}.mct-activity{color:#9da9ba;font:12px/1.6 ui-monospace,monospace;margin:6px 0}.mct-activity summary{cursor:pointer}.mct-activity pre{max-height:320px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}.mct-note{padding:8px 16px;color:#98a9bd;font-size:12px;border-top:1px solid #253244}.mct-compose{display:flex;gap:8px;padding:10px 14px;border-top:1px solid #253244;align-items:flex-end}.mct-compose textarea{resize:vertical;min-height:58px;max-height:240px;flex:1;background:#131c29;border:1px solid #37475c;border-radius:8px;color:inherit;padding:10px;font:inherit}.mct-conversation button,.mct-files button{background:#243b56;color:#e6e9ef;border:1px solid #385779;border-radius:5px;padding:6px 10px;cursor:pointer}.mct-conversation button:disabled{opacity:.5;cursor:wait}.mct-pending{padding:0 14px}.mct-pending button{margin:4px;font-size:12px}.mct-files{padding:0 14px;font-size:12px}.mct-error{color:#f2a89c}.fv-native{position:absolute;inset:0}.mct-attachment{font-size:12px;margin-right:12px;color:#a8ccff}.mct-md{margin-top:4px}.mct-md p{margin:6px 0}.mct-md-h{font-weight:700;margin:10px 0 4px}.mct-code{background:#1b2634;padding:1px 4px;border-radius:4px;font:12px ui-monospace,monospace}.mct-code-block{background:#0a0f16;border:1px solid #253244;border-radius:6px;padding:10px;overflow:auto;font:12px/1.5 ui-monospace,monospace;white-space:pre}.mct-list{margin:4px 0 4px 20px}.mct-pointer{display:inline-block;margin-top:6px;font-size:11px;color:#8da9cb;text-decoration:none;border:1px solid #385779;border-radius:5px;padding:2px 8px}.mct-harness{color:#6f7a88;font-size:11px;margin:4px 0}.mct-harness summary{color:#6f7a88}.mct-status{display:flex;align-items:center;gap:10px;margin:10px 2px 2px;font-size:12px;color:#8da9cb}.mct-status button{background:transparent;border:0;color:#7e93ac;text-decoration:underline;padding:0;font:inherit;cursor:pointer}.mct-status button:hover{color:#c9d6e6}.mct-status-interrupt{color:#f2c48c}.mct-dots{display:inline-flex;gap:4px;align-items:center}.mct-dots i{width:5px;height:5px;border-radius:50%;background:#5b8fd6;display:inline-block;animation:mctPulse 1.2s infinite ease-in-out}.mct-dots i:nth-child(2){animation-delay:.18s}.mct-dots i:nth-child(3){animation-delay:.36s}.mct-idle .mct-dot{width:6px;height:6px;border-radius:50%;background:#5a6a7d;display:inline-block}@keyframes mctPulse{0%,80%,100%{opacity:.25;transform:scale(.7)}40%{opacity:1;transform:scale(1)}}.mct-choice{margin:8px 0 4px;padding:10px 12px;border:1px solid #6b5426;border-left:3px solid #e0a94a;border-radius:8px;background:#221c10}.mct-choice-head{color:#f0c674;font-size:12px;font-weight:600;margin-bottom:2px}.mct-choice-note{color:#c9b68c;font-size:11px;margin-bottom:6px}.mct-choice-q{color:#e6e9ef;font-size:13px;margin:4px 0 8px;white-space:pre-wrap;overflow-wrap:anywhere}.mct-choice-opts{display:flex;flex-direction:column;gap:4px;margin-bottom:8px}.mct-choice-opts button{text-align:left;background:#2b3a4d;border:1px solid #44607f}.mct-choice-opts button.on{border-color:#e0a94a;box-shadow:inset 2px 0 0 #e0a94a}.mct-choice-reply{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.mct-choice-reply input{flex:1;min-width:180px;background:#131c29;border:1px solid #37475c;border-radius:6px;color:inherit;padding:6px 8px;font:inherit}.mct-choice-keys button{font-size:11px;padding:4px 8px}.mct-choice pre{margin:6px 0 0;max-height:180px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.45 ui-monospace,monospace;color:#9da9ba}.mct-choice summary{color:#c9b68c;font-size:11px;cursor:pointer}.mct-note.mct-choice-pending{color:#f0c674}';
  document.head.appendChild(style);

  function node(tag, cls, text) {
    var n = document.createElement(tag); n.className = cls || '';
    if (text != null) n.textContent = text;
    return n;
  }
  /* Minimal SAFE markdown → DOM: element creation + textContent only, never
     innerHTML, so a reply that contains a stray <script> or a javascript:
     link comes out as inert literal text (mirrors index.html's mdRender,
     which builds React elements for the same reason — this build DOM nodes
     directly since the MCT panel isn't a React tree). Unsupported syntax
     degrades to plain text, never to markup. */
  var MD_INLINE = /(`[^`]+`)|(\*\*[^*]+?\*\*)|(\*[^*]+?\*)|(\[[^\]]+\]\((https?:\/\/[^\s)]+)\))/;
  function mdInline(parent, text) {
    var rest = String(text);
    while (rest) {
      var m = MD_INLINE.exec(rest);
      if (!m) { parent.appendChild(document.createTextNode(rest)); break; }
      if (m.index > 0) parent.appendChild(document.createTextNode(rest.slice(0, m.index)));
      var tok = m[0];
      if (m[1]) { parent.appendChild(node('code', 'mct-code', tok.slice(1, -1))); }
      else if (m[2]) { var strong = document.createElement('strong'); mdInline(strong, tok.slice(2, -2)); parent.appendChild(strong); }
      else if (m[3]) { var em = document.createElement('em'); mdInline(em, tok.slice(1, -1)); parent.appendChild(em); }
      else {
        var lm = /^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/.exec(tok);
        var a = document.createElement('a'); a.href = lm[2]; a.target = '_blank'; a.rel = 'noopener noreferrer';
        mdInline(a, lm[1]); parent.appendChild(a);
      }
      rest = rest.slice(m.index + tok.length);
    }
  }
  function mdBlock(container, src) {
    var parts = String(src).replace(/\r\n?/g, '\n').split(/```[^\n]*\n([\s\S]*?)```/);
    for (var i = 0; i < parts.length; i++) {
      if (i % 2 === 1) { // fenced code, raw, never inline-parsed
        var pre = node('pre', 'mct-code-block'); pre.appendChild(node('code', '', parts[i])); container.appendChild(pre);
        continue;
      }
      parts[i].split(/\n{2,}/).forEach(function (para) {
        if (!para.trim()) return;
        var lines = para.split('\n');
        if (/^\s*[-*]\s+/.test(lines[0]) && lines.every(function (l) { return /^\s*[-*]\s+/.test(l); })) {
          var ul = node('ul', 'mct-list');
          lines.forEach(function (l) { var li = document.createElement('li'); mdInline(li, l.replace(/^\s*[-*]\s+/, '')); ul.appendChild(li); });
          container.appendChild(ul); return;
        }
        var hm = lines.length === 1 && /^#{1,3}\s+(.*)$/.exec(lines[0]);
        if (hm) { container.appendChild(node('div', 'mct-md-h')); mdInline(container.lastChild, hm[1]); return; }
        var p = document.createElement('p'); mdInline(p, lines.join('\n')); container.appendChild(p);
      });
    }
  }
  async function request(path, data) {
    var options = {credentials: 'same-origin', headers: {}};
    if (data) {
      options.method = 'POST'; options.headers['Content-Type'] = 'application/json';
      var csrf = document.cookie.match(/(?:^|;\s*)sc_csrf=([^;]+)/);
      if (csrf) options.headers['X-Console-CSRF'] = decodeURIComponent(csrf[1]);
      options.body = JSON.stringify(data);
    }
    var response = await fetch(path, options), doc;
    var raw = await response.text();
    try { doc = JSON.parse(raw); } catch (_) { doc = {error: raw}; }
    if (!response.ok) throw new Error(doc.error || 'Request failed (' + response.status + ')');
    return doc;
  }

  window.MctRenderer = function (host) {
    var root = node('div', 'mct-conversation');
    var history = node('div', 'mct-history'); history.setAttribute('role', 'log'); history.setAttribute('aria-label', 'MCT conversation');
    var note = node('div', 'mct-note', 'Loading conversation…');
    var pending = node('div', 'mct-pending'), fileList = node('div', 'mct-files');
    var compose = node('form', 'mct-compose');
    var input = node('textarea'); input.placeholder = 'Message via B — pending messages combine into one prompt…'; input.setAttribute('aria-label', 'Message via local keeper B');
    var pick = node('input'); pick.type = 'file'; pick.multiple = true; pick.hidden = true;
    var attach = node('button', '', 'Attach'); attach.type = 'button'; attach.onclick = function () { pick.click(); };
    var send = node('button', '', 'Send'); send.type = 'submit';
    compose.append(input, attach, send, pick); root.append(history, pending, fileList, note, compose); host.appendChild(root);
    var current = null, views = {}, active = false, timer = null, polling = false;

    function key(p, vm) { return p + '@' + (vm || '@keeper'); }
    function query(c) { return '?provider=' + encodeURIComponent(c.provider) + '&vm=' + encodeURIComponent(c.vm); }
    function save(c) {
      try { sessionStorage.setItem('mct-draft:' + c.key, JSON.stringify({text:c.draft, request:c.request})); } catch (_) {}
    }
    function files(c) {
      fileList.replaceChildren();
      c.files.forEach(function (f, i) {
        var b = node('button', '', f.name + ' ×'); b.type = 'button';
        b.onclick = function () { c.files.splice(i, 1); c.request = null; files(c); };
        fileList.appendChild(b);
      });
    }
    function message(c, e) {
      if (c.seen.has(e.id)) return; c.seen.add(e.id);
      var turn = c.turns[e.turn_id];
      if (!turn) {
        turn = c.turns[e.turn_id] = node('section', 'mct-turn');
        turn.dataset.turn = e.turn_id; c.history.appendChild(turn);
      }
      var d = e.detail || {}, block, who = c.provider === 'codex' ? 'ChatGPT' : 'Claude';
      if (e.kind === 'harness') {
        // mct v2: harness-injected user-role content (subagent notices,
        // system reminders, local /commands) — a collapsed grey system line,
        // never rendered as the operator.
        block = node('details', 'mct-activity mct-harness');
        block.appendChild(node('summary', '', '⚙ ' + e.text));
        block.appendChild(node('pre', '', (d.text || '').slice(0, 4000) || '(no detail)'));
        turn.appendChild(block); c.nodes[e.id] = block; return;
      }
      if (e.kind === 'artifact') {
        // mct v2: additional material the seat published for this turn (mct-push --kind artifact)
        block = node('div', 'mct-message');
        block.appendChild(node('div', 'mct-label', who + ' · artifact'));
        var art = node('a', 'mct-attachment', (d.name || 'artifact') + ' (' + (d.size || 0) + ' bytes)');
        art.title = d.object || ''; art.href = '/api/mct/object?handle=' + encodeURIComponent(d.object || ''); art.download = d.name || 'artifact';
        block.appendChild(art); turn.appendChild(block); c.nodes[e.id] = block; return;
      }
      if (e.kind === 'user' || e.kind === 'assistant' || e.kind === 'commentary' || e.kind === 'response') {
        if (d.replaces && c.nodes[d.replaces]) c.nodes[d.replaces].remove();
        // Collapse the headless duplicate: the streamed trailing assistant text
        // arrives as a 'commentary' event and again as the canonical 'response'.
        // Show it once — when the response lands, drop a matching commentary.
        var norm = String(e.text || '').trim();
        turn.comments = turn.comments || [];
        if (e.kind === 'response' || e.kind === 'assistant') {
          turn.comments = turn.comments.filter(function (cm) {
            if (cm.text === norm) { cm.node.remove(); delete c.nodes[cm.id]; return false; }
            return true;
          });
        }
        block = node('div', 'mct-message' + (e.kind === 'user' ? ' mct-user' : ''));
        // mct v2: 'response' = the reply the seat PUBLISHED into the ledger (mct-push): immutable, sha-verified
        block.appendChild(node('div', 'mct-label', e.kind === 'user' ? 'You' : e.kind === 'response' ? who + ' · published reply' : who));
        if (e.kind === 'user') block.appendChild(node('div', '', e.text));
        else mdBlock(block.appendChild(node('div', 'mct-md')), e.text); // reply text, rendered — never the bare mct:// pointer
        (d.attachments || []).forEach(function (f) {
          var a = node('a', 'mct-attachment', f.name);
          a.href = '/api/mct/object?handle=' + encodeURIComponent(f.object); a.download = f.name; block.appendChild(a);
        });
        if (d.object) { // the published response object itself: open/download the exact file the pointer names
          var ptr = node('a', 'mct-pointer', '⧉ open pointer');
          ptr.href = '/api/mct/object?handle=' + encodeURIComponent(d.object); ptr.download = d.name || 'reply.md'; ptr.title = d.object;
          block.appendChild(ptr);
        }
      } else if (e.kind === 'activity') {
        if (!turn.activity) {
          turn.activity = node('details', 'mct-activity');
          turn.summary = node('summary', '', 'Activity');
          turn.activity.appendChild(turn.summary); turn.appendChild(turn.activity); turn.count = 0;
        }
        turn.summary.textContent = 'Activity · ' + (++turn.count) + ' · ' + e.text;
        block = node('details', 'mct-activity'); block.appendChild(node('summary', '', e.text));
        block.addEventListener('toggle', function () {
          if (!block.open || block.loaded) return; block.loaded = true;
          var detail = node('pre', '', 'Loading…'); block.appendChild(detail);
          request('/api/mct/event' + query(c) + '&id=' + e.id).then(function (full) {
            detail.textContent = JSON.stringify(full.detail, null, 2);
          }).catch(function (err) { detail.textContent = err.message; block.loaded = false; });
        });
        turn.activity.appendChild(block); c.nodes[e.id] = block; return;
      } else {
        if (e.kind === 'status') {
          if (!turn.status) { turn.status = node('div', 'mct-activity'); turn.appendChild(turn.status); }
          turn.status.textContent = e.text; return;
        }
        block = node('div', 'mct-activity mct-error', e.text);
      }
      turn.appendChild(block); c.nodes[e.id] = block;
      if (e.kind === 'commentary') { turn.comments = turn.comments || []; turn.comments.push({node: block, text: String(e.text || '').trim(), id: e.id}); }
      if (turn._footer) turn.appendChild(turn._footer);   // keep the live status line last
    }

    // A per-turn status line (queued/working/delivering/…) rendered inline
    // under its own conversation bubble — replaces the detached status-chip
    // wall, so every state is anchored to the message it belongs to.
    var STATUS = {queued:'Queued', sending:'Sending', sent:'Delivering', working:'Working',
                  uncertain:'Delivery uncertain', interrupted:'A message was interrupted; resend it when ready'};
    var ACTIVE = {queued:1, sending:1, sent:1, working:1};
    function turnStatus(c, t) {
      var turn = c.turns[t.id];
      if (!turn) return;
      var f = turn._footer || (turn._footer = node('div', 'mct-status'));
      f.replaceChildren(); f.className = 'mct-status';
      if (ACTIVE[t.status]) {
        var dots = node('span', 'mct-dots'); dots.append(node('i'), node('i'), node('i'));
        f.append(dots, node('span', '', (c.provider === 'codex' ? 'ChatGPT' : 'Claude') + ' · ' + STATUS[t.status] + '…'));
      } else if (t.status === 'interrupted') {
        f.className = 'mct-status mct-status-interrupt';
        f.append(node('span', 'mct-idle', ''), node('span', '', STATUS.interrupted));
      } else {
        f.append(node('span', 'mct-idle', ''), node('span', '', STATUS[t.status] || t.status));
      }
      if (ACTIVE[t.status]) {
        var cancel = node('button', '', 'Cancel'); cancel.type = 'button';
        cancel.onclick = async function () {
          cancel.disabled = true;
          try { await request('/api/mct/interaction/cancel', {provider:c.provider, vm:c.vm, turn:t.id}); }
          catch (err) { cancel.disabled = false; note.className = 'mct-note mct-error'; note.textContent = err.message; }
        };
        f.appendChild(cancel);
      }
      turn.appendChild(f);   // anchor at the bottom of the bubble
    }

    /* t266 — a tmux-backed seat parked on an AskUserQuestion / permission /
       select box stops writing its transcript, so the MCT turn would sit at
       "Working…" forever. The live route now reports what the pane shows;
       render it here with an answer path over POST /api/mct/choice (the same
       tmux send-keys the broker delivers pointers on). Detection is best
       effort: the raw terminal lines and a free-text respond box are always
       offered alongside the parsed options. */
    function choiceCard(c, pc) {
      var box = node('div', 'mct-choice');
      box.setAttribute('role', 'alert');
      box.append(node('div', 'mct-choice-head', '⚠ ' + (c.provider === 'codex' ? 'ChatGPT' : 'Claude')
        + ' is waiting on a choice in its terminal' + (pc.seat ? ' (' + pc.seat + ')' : '')));
      box.append(node('div', 'mct-choice-note', pc.notice || 'Answer it here to let the turn continue.'));
      if (pc.question) box.append(node('div', 'mct-choice-q', pc.question));
      var busy = false;
      async function answer(payload, button) {
        if (busy) return;
        busy = true; if (button) button.disabled = true;
        try {
          payload.provider = c.provider; payload.vm = c.vm;
          await request('/api/mct/choice', payload);
          c.choiceSig = null;           // force a redraw from the next poll
          clearTimeout(timer); timer = setTimeout(poll, 250);
        } catch (err) {
          note.className = 'mct-note mct-error'; note.textContent = err.message;
          if (button) button.disabled = false;
        } finally { busy = false; }
      }
      var opts = node('div', 'mct-choice-opts');
      (pc.options || []).forEach(function (o, i) {
        var label = (o.number != null ? o.number + '. ' : '') + o.label;
        var b = node('button', o.selected ? 'on' : '', label + (o.selected ? '   ← selected' : ''));
        b.type = 'button';
        b.onclick = function () { answer({index: i, label: o.label}, b); };
        opts.appendChild(b);
      });
      if (opts.childElementCount) box.append(opts);
      var reply = node('div', 'mct-choice-reply');
      var free = node('input'); free.type = 'text';
      free.placeholder = pc.confidence === 'high' ? 'Or type a reply for the terminal…'
                                                  : 'Type the answer this prompt expects…';
      free.setAttribute('aria-label', 'Respond to the terminal prompt');
      var go = node('button', '', 'Respond'); go.type = 'button';
      go.onclick = function () {
        var v = free.value;
        if (!v.trim()) return;
        free.value = '';
        answer({text: v}, null);
      };
      free.onkeydown = function (e) { if (e.key === 'Enter') { e.preventDefault(); go.click(); } };
      var keys = node('span', 'mct-choice-keys');
      [['↑', 'Up'], ['↓', 'Down'], ['Enter', 'Enter'], ['Esc', 'Escape']].forEach(function (k) {
        var b = node('button', '', k[0]); b.type = 'button'; b.title = 'Send ' + k[1];
        b.onclick = function () { answer({keys: [k[1]]}, null); };
        keys.appendChild(b);
      });
      reply.append(free, go, keys); box.append(reply);
      if (pc.lines && pc.lines.length) {
        var det = node('details'); det.append(node('summary', '', 'What the terminal is showing'));
        det.append(node('pre', '', pc.lines.join('\n')));
        box.append(det);
      }
      return box;
    }

    async function poll() {
      clearTimeout(timer);
      if (!active || !current || polling) return;
      polling = true; var c = current;
      try {
        var doc = await request('/api/mct/interaction' + query(c) + '&after=' + c.cursor);
        var stick = history.scrollHeight - history.scrollTop - history.clientHeight < 100;
        doc.events.forEach(function (e) { message(c, e); }); c.cursor = doc.cursor;
        if (c === current) {
          if (stick) history.scrollTop = history.scrollHeight;
          note.className = 'mct-note' + (doc.error ? ' mct-error' : '');
          note.textContent = doc.error || (((doc.delivery && doc.delivery.message) || (doc.busy ? 'Working' : 'Ready')) + (doc.delivery && doc.delivery.queued ? ' · ' + (doc.mediation || 'Waiting for B') : '') + ' · ' + (doc.archive || 'Saved locally'));
          if (doc.pending_choice && !doc.error) {
            note.className = 'mct-note mct-choice-pending';
            note.textContent = '⚠ Waiting on a choice in the seat terminal — answer it above · ' + note.textContent;
          }
          // status now lives inline on each turn; `pending` carries the t266
          // choice card. Only rebuild when it actually changes, so a half-typed
          // response is not wiped by the next poll.
          var pc = doc.pending_choice || null;
          var sig = pc ? JSON.stringify([pc.kind, pc.question, pc.selected, pc.options, pc.lines]) : '';
          if (sig !== c.choiceSig) {
            c.choiceSig = sig;
            pending.replaceChildren();
            if (pc) pending.appendChild(choiceCard(c, pc));
          }
          var live = {};
          doc.turns.forEach(function (t) { live[t.id] = 1; turnStatus(c, t); });
          Object.keys(c.turns).forEach(function (id) {
            var s = c.turns[id];
            if (s && s._footer && !live[id]) { s._footer.remove(); s._footer = null; }
          });
          if (stick) history.scrollTop = history.scrollHeight;
        }
        timer = setTimeout(poll, doc.events.length === 200 ? 10 : (doc.busy || doc.turns.length ? 400 : 1500));
      } catch (err) {
        if (c === current) { note.className = 'mct-note mct-error'; note.textContent = err.message; }
        timer = setTimeout(poll, 2500);
      } finally { polling = false; }
    }

    async function submit(text, attachments) {
      var c = current;
      if (!c || !active) throw new Error('Open MCT for the selected frontier first');
      if (c.sending) throw new Error('A message is already being saved');
      // Keep a stable request ID across an ambiguous network failure/retry.
      var payload = JSON.stringify([text, attachments || []]);
      if (!c.request || c.request.payload !== payload) c.request = {id:crypto.randomUUID(), payload:payload};
      c.sending = true; send.disabled = true; save(c);
      try {
        var result = await request('/api/mct/interaction', {provider:c.provider, vm:c.vm, request_id:c.request.id, text:text, files:attachments || []});
        c.request = null; save(c); return result;
      } finally { c.sending = false; if (c === current) send.disabled = false; }
    }
    compose.onsubmit = async function (e) {
      e.preventDefault(); var c = current, text = input.value, attached = c.files.slice();
      if (!text.trim() && !attached.length) return;
      try {
        await submit(text, attached);
        if (c === current && input.value === text) { input.value = ''; c.draft = ''; c.files = []; files(c); save(c); }
        note.textContent = 'Saved with B · pending messages will be combined'; poll();
      } catch (err) { if (c === current) { note.className = 'mct-note mct-error'; note.textContent = err.message; } }
    };
    input.oninput = function () { if (current) { current.draft = input.value; save(current); } };
    input.onkeydown = function (e) { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); compose.requestSubmit(); } };
    pick.onchange = async function () {
      var c = current, selected = Array.from(pick.files); pick.value = '';
      try {
        var added = await Promise.all(selected.map(function (f) { return new Promise(function (resolve, reject) {
          var reader = new FileReader(); reader.onload = function () { resolve({name:f.name, dataUrl:reader.result}); };
          reader.onerror = reject; reader.readAsDataURL(f);
        }); }));
        c.files.push.apply(c.files, added); if (c === current) files(c);
      } catch (_) { note.textContent = 'Could not read the attachment. Please attach it again.'; }
    };
    return {
      configure: function (provider, vm, enabled) {
        active = !!enabled; root.style.display = active ? 'flex' : 'none';
        if (!active) { clearTimeout(timer); return; }
        var k = key(provider, vm);
        if (!current || current.key !== k) {
          var cached = {}; try { cached = JSON.parse(sessionStorage.getItem('mct-draft:' + k)) || {}; } catch (_) {}
          if (current) current.scroll = history.scrollTop;
          current = views[k] || (views[k] = {key:k, provider:provider, vm:vm || '', cursor:0, turns:{}, nodes:{}, seen:new Set(), history:node('div'), draft:cached.text || '', request:cached.request, files:[]});
          history.replaceChildren(current.history); input.value = current.draft; files(current); history.scrollTop = current.scroll || 0;
          note.textContent = 'Loading conversation…'; pending.replaceChildren(); current.choiceSig = null;
        }
        send.disabled = !!current.sending; poll();
      },
      submit: submit,
      focus: function () { if (active) input.focus(); },
      draft: function (text) { input.value += text; input.oninput(); input.focus(); }
    };
  };
})();
