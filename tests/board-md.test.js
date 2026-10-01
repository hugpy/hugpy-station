'use strict';
// board-md (resources/backend/static/board-md.js): the static-reference renderer
// for ⚑ operator / ⚖ proposal / 🔖 bookmark / 🧭 direction items.
// Run:  node --test tests/board-md.test.js      (no deps)
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const B = require(path.join(__dirname, '..', 'resources', 'backend', 'static', 'board-md.js'));

const FENCE = '```';

test('fences: language label, verbatim body, unterminated fence stays text', () => {
  const bl = B.parse(['before', FENCE + 'bash', 'sudo systemctl restart x', '  indented kept', FENCE, 'after'].join('\n'));
  assert.deepEqual(bl.map(b => b.kind), ['p', 'fence', 'p']);
  assert.equal(bl[1].lang, 'bash');
  assert.equal(bl[1].text, 'sudo systemctl restart x\n  indented kept');
  const open = B.parse(['text', FENCE + 'sh', 'never closed'].join('\n'));
  assert.deepEqual(open.map(b => b.kind), ['p']);
  assert.match(open[0].text, /never closed/);
  const bare = B.parse([FENCE, 'x', FENCE].join('\n'));
  assert.equal(bare[0].lang, '');
});

test('copy payload: exact content, prompt markers and SOP caveats left out', () => {
  const plain = B.parse([FENCE + 'bash', 'a && b', '# keep this comment', FENCE].join('\n'))[0];
  assert.equal(B.copyPayload(plain), 'a && b\n# keep this comment');
  const transcript = B.parse([FENCE + 'console', '$ ls /srv', 'out-line', '$ whoami', FENCE].join('\n'))[0];
  assert.equal(B.copyPayload(transcript), 'ls /srv\nwhoami');
  // TODO-BOARD-SOP §2 operator shape: cmd: + indented lines, `#` caveats not copied
  const sop = B.parse(['@worker: ae (root)  task: APPLY', 'cmd:', '  systemctl restart hugpy-station-web@hugpy',
    '  systemctl status hugpy-station-web@hugpy', '# only after the deb is in place'].join('\n'));
  assert.deepEqual(sop.map(b => b.kind), ['p', 'cmd']);
  assert.equal(B.copyPayload(sop[1]),
    'systemctl restart hugpy-station-web@hugpy\nsystemctl status hugpy-station-web@hugpy');
  assert.equal(B.parse('cmd: sudo reboot')[0].kind, 'cmd');
});

test('commandBlocks: shell fences + cmd blocks, not json/python; across text and note', () => {
  const text = [FENCE + 'bash', 'one', FENCE, FENCE + 'json', '{"a":1}', FENCE].join('\n');
  const note = ['cmd:', '  two', '', FENCE, 'sudo three', FENCE, FENCE, 'just prose', FENCE].join('\n');
  assert.deepEqual(B.commandBlocks([text, note]).map(B.copyPayload), ['one', 'two', 'sudo three']);
});

test('inline: code spans, bold, SOP labels; nothing inside code is re-scanned', () => {
  const sp = B.inline('PROBLEM: x / OPTIONS: A) y B) z / REC: `a **b** REC: c` **sure**');
  assert.deepEqual(sp.filter(s => s.label).map(s => s.t), ['PROBLEM:', 'OPTIONS:', 'REC:']);
  assert.deepEqual(sp.find(s => s.code), { t: 'a **b** REC: c', code: true });
  assert.deepEqual(sp.find(s => s.bold), { t: 'sure', bold: true });
  assert.equal(sp.map(s => s.t).join(''), 'PROBLEM: x / OPTIONS: A) y B) z / REC: a **b** REC: c sure');
  const shipped = B.inline('SHIPPED 2026-09-18T06:20Z: station 1.0.106 — VERIFIED: ok');
  assert.deepEqual(shipped.filter(s => s.label).map(s => s.t), ['SHIPPED 2026-09-18T06:20Z:', 'VERIFIED:']);
});

test('lists, headings, tables, newlines preserved (no reflow)', () => {
  const bl = B.parse(['## Steps', '1. first', '   continued', '2) second', '  - nested', '', 'line one', 'line two', '| a | b |'].join('\n'));
  assert.deepEqual(bl.map(b => b.kind), ['h', 'list', 'p', 'table']);
  assert.equal(bl[0].level, 2);
  assert.deepEqual(bl[1].items.map(i => [i.marker, i.depth, i.text]),
    [['1.', 0, 'first\ncontinued'], ['2)', 0, 'second'], ['-', 1, 'nested']]);
  assert.equal(bl[2].text, 'line one\nline two');
});

test('escaping: markup is carried as plain text, never interpreted', () => {
  const evil = '<img src=x onerror=alert(1)> <script>alert(2)</script> [x](javascript:alert(3))';
  const bl = B.parse(evil);
  assert.equal(bl[0].kind, 'p');
  assert.equal(bl[0].text, evil);
  assert.equal(B.inline(evil).map(s => s.t).join(''), evil);
  // the renderer only ever hands text as children/props — check with a recording html tag
  const calls = [];
  const html = (strings, ...vals) => { calls.push({ strings: strings.join('§'), vals }); return { strings, vals }; };
  const R = B.makeRenderer({ html, useState: v => [v, () => {}], useEffect: () => {}, copyText: () => true, document: null });
  R.Body({ src: evil + '\n' + FENCE + 'html\n<b>x</b>\n' + FENCE });
  assert.ok(calls.every(c => !/dangerouslySetInnerHTML|innerHTML/.test(c.strings)), 'no innerHTML path');
  const deep = (v) => Array.isArray(v) ? v.flatMap(deep) : [v];
  assert.ok(calls.some(c => deep(c.vals).includes(evil)), 'paragraph line passed as a text child');
  const fence = calls.flatMap(c => c.vals).find(v => v && v.kind === 'fence');
  assert.equal(fence.text, '<b>x</b>');
  calls.length = 0;
  R.Code({ block: fence });
  assert.ok(calls.some(c => c.vals.includes('<b>x</b>')), 'fence body passed as a text child');
  assert.ok(calls.some(c => c.vals.includes('html')), 'language label rendered');
});

test('unformatted-command detector: flags bare commands outside fences only', () => {
  const src = [
    'Restart the web unit:',
    'sudo systemctl restart hugpy-station-web@hugpy',
    '$ journalctl -u x -n 50',
    '- systemctl status foo',
    'needs sudo access on ae, see `sudo -l`',
    'git is the source of truth',
    FENCE + 'bash', 'sudo systemctl daemon-reload', FENCE,
    'cmd:', '  systemctl restart y',
    'then run sudo apt install nginx on the host',
  ].join('\n');
  const u = B.unformatted(src);
  assert.deepEqual(u.map(x => x.line), [1, 2, 3, 11]);
  assert.deepEqual(u.map(x => x.text), ['sudo systemctl restart hugpy-station-web@hugpy',
    '$ journalctl -u x -n 50', '- systemctl status foo', 'then run sudo apt install nginx on the host']);
  assert.equal(B.looksLikeCommand('`sudo systemctl restart x`'), false);
  assert.equal(B.looksLikeCommand('cd the directory is wrong'), false);
  assert.equal(B.looksLikeCommand('cd /srv/vm_mgr && make'), true);
  assert.deepEqual(B.unformatted(['ok', FENCE + 'sh', '$ rm -rf /tmp/x', FENCE].join('\n')), []);
});

test('collapse plan: long prose folds, code blocks never hide', () => {
  const prose = Array.from({ length: 30 }, (_, i) => 'para ' + i).join('\n\n');
  const text = prose + '\n' + FENCE + 'bash\nsudo x\n' + FENCE + '\n' + prose;
  const bl = B.parse(text);
  const plan = B.collapsePlan([bl, []]);
  assert.equal(plan.long, true);
  bl.forEach((b, i) => { if (b.kind === 'fence') assert.ok(!plan.hidden['0:' + i]); });
  assert.equal(plan.hiddenLines, 60 - B.KEEP_LINES);
  assert.equal(B.collapsePlan([B.parse('short\n\ntext'), []]).long, false);
});

// BOARD-ITEM-FORMAT.md v1 (st-boardfmt, 2026-10-01): the spec's own examples
const SPEC_OP = [
  'WHY: hugpy-chat.service restarts every 5 s (ModuleNotFoundError: httpx) and points at the retired :7015.',
  'WHO: root via sudo on ae',
  'GATE: no hugpy-gate action covers this because svc.control\'s allowlist does not include hugpy-chat.service.',
  'DO:', FENCE + 'bash', 'sudo systemctl disable --now hugpy-chat.service', FENCE,
  'VERIFY:', FENCE + 'bash', 'systemctl is-active hugpy-chat.service; systemctl is-enabled hugpy-chat.service', FENCE,
  'EXPECT: inactive / disabled',
  'ROLLBACK:', FENCE + 'bash', 'sudo systemctl enable --now hugpy-chat.service', FENCE,
].join('\n');

test('spec operator example: clean, three copyable blocks, headers emphasised', () => {
  assert.deepEqual(B.unformatted(SPEC_OP), []);
  const runs = B.commandBlocks([SPEC_OP]).map(B.copyPayload);
  assert.deepEqual(runs, ['sudo systemctl disable --now hugpy-chat.service',
    'systemctl is-active hugpy-chat.service; systemctl is-enabled hugpy-chat.service',
    'sudo systemctl enable --now hugpy-chat.service']);
  const labels = B.parse(SPEC_OP).filter(b => b.kind === 'p')
    .flatMap(b => B.inline(b.text)).filter(s => s.label).map(s => s.t);
  assert.deepEqual(labels, ['WHY:', 'WHO:', 'GATE:', 'DO:', 'VERIFY:', 'EXPECT:', 'ROLLBACK:']);
  // the same item written as prose: a header followed by a bare command is flagged
  const bad = 'WHY: x\nDO: sudo systemctl disable --now hugpy-chat.service\nVERIFY: systemctl is-active hugpy-chat.service';
  assert.deepEqual(B.unformatted(bad).map(u => u.line), [1, 2]);
});

test('spec proposal example: OPTIONS lines kept one per line, SKETCH text fence', () => {
  const note = ['PROBLEM: operator items arrive as prose', 'OPTIONS:',
    'A) enforce in todo_add — + one shape / − old writers fail', 'B) lint only — + nothing breaks / − bad items land',
    'REC: A', 'DECISION: pending (operator)', 'SKETCH:', FENCE + 'text', 'board_format.check(type, text, note)', FENCE].join('\n');
  const bl = B.parse(note);
  assert.deepEqual(bl.map(b => b.kind), ['p', 'fence']);
  assert.equal(bl[0].text.split('\n').length, 7);
  assert.equal(bl[1].lang, 'text');
  assert.equal(B.isCommandBlock(bl[1]), false);
  assert.deepEqual(B.unformatted(note), []);
});

test('prompt markers and placeholders', () => {
  const py = B.parse([FENCE + 'python', '>>> import x', '... x.y()', 'result', FENCE].join('\n'))[0];
  assert.equal(B.copyPayload(py), 'import x\nx.y()');
  const ph = B.parse([FENCE + 'bash', 'ssh <you>@<host> "cat {{path}}"', 'HOST=computron; ping $HOST 2>&1 <<EOF', FENCE].join('\n'))[0];
  assert.deepEqual(B.placeholders(ph), ['<you>', '<host>', '{{path}}']);
  const ok = B.parse([FENCE + 'bash', 'HOST=computron', 'ssh vm_mgr@$HOST true < /dev/null', FENCE].join('\n'))[0];
  assert.deepEqual(B.placeholders(ok), []);
});

// operator 2026-10-01: required RUN: section (one self-contained bash script) +
// optional ROLLBACK RUN:
test('scripts: RUN and ROLLBACK RUN blocks, first fence after the header only', () => {
  const note = ['WHY: x', 'WHO: root via sudo on ae', 'GATE: n/a', 'DO:', FENCE + 'bash', 'sudo step1', FENCE,
    'VERIFY:', FENCE + 'bash', 'systemctl is-active x', FENCE, 'EXPECT: active',
    'RUN:', FENCE + 'bash', '#!/usr/bin/env bash', 'set -euo pipefail', 'sudo step1', 'systemctl is-active x', FENCE,
    'ROLLBACK RUN:', FENCE + 'bash', 'sudo systemctl enable --now x', FENCE].join('\n');
  const sc = B.scripts(note);
  assert.equal(B.copyPayload(sc.run), '#!/usr/bin/env bash\nset -euo pipefail\nsudo step1\nsystemctl is-active x');
  assert.equal(B.copyPayload(sc.rollback), 'sudo systemctl enable --now x');
  // header variants and no script
  assert.equal(B.scripts('**RUN:**\n' + FENCE + 'bash\na\n' + FENCE).run.text, 'a');
  assert.equal(B.scripts('## Run:\n' + FENCE + 'bash\na\n' + FENCE).run.text, 'a');
  assert.deepEqual(B.scripts(SPEC_OP), { run: null, rollback: null });
  // a RUN header whose section holds no fence (prose only) yields nothing; the DO fence is not borrowed
  assert.equal(B.scripts('DO:\n' + FENCE + 'bash\nx\n' + FENCE + '\nRUN: see above\nEXPECT: ok').run, null);
  // "RUN:" inside a fence is code, not a header
  assert.equal(B.scripts(FENCE + 'bash\necho RUN:\n' + FENCE).run, null);
});

test('operator item: ▶ copy script at top + beside the block; no-RUN badge; copy-all only without RUN', () => {
  const calls = [];
  const html = (strings, ...vals) => { calls.push({ s: strings.join('§'), vals }); return { strings, vals }; };
  const R = B.makeRenderer({ html, useState: v => [v, () => {}], useEffect: () => {}, copyText: () => true, document: null });
  const deep = (v) => Array.isArray(v) ? v.flatMap(deep) : [v];
  const text = (c) => deep(c.vals).filter(x => typeof x === 'string').join(' ') + ' ' + c.s;
  const withRun = ['DO:', FENCE + 'bash', 'a', FENCE, 'VERIFY:', FENCE + 'bash', 'b', FENCE, 'RUN:', FENCE + 'bash', 'a && b', FENCE].join('\n');
  R.Item({ operator: true, text: '[root] t', note: withRun });
  let all = calls.map(text).join('\n');
  assert.match(all, /▶ copy script/);
  assert.doesNotMatch(all, /no RUN script/);
  assert.doesNotMatch(all, /copy all \d+ commands/);
  // the block itself renders with role=run (Code gets the ▶ label)
  const codeCall = calls.find(c => c.vals.some(v => v === 'run'));
  assert.ok(codeCall, 'RUN fence passed to Code with role=run');
  calls.length = 0;
  R.Code({ block: B.scripts(withRun).run, role: 'run' });
  assert.match(calls.map(text).join('\n'), /▶ copy script/);
  calls.length = 0;
  R.Code({ block: B.scripts(withRun).run, role: 'rollback' });
  assert.match(calls.map(text).join('\n'), /↩ copy rollback script/);
  calls.length = 0;
  R.Item({ operator: true, text: '[root] t', note: SPEC_OP });
  all = calls.map(text).join('\n');
  assert.match(all, /⚠ no RUN script/);
  assert.match(all, /copy all 3 commands/);
  calls.length = 0;
  R.Item({ operator: false, text: 'p', note: SPEC_OP });   // a proposal is never badged for RUN
  assert.doesNotMatch(calls.map(text).join('\n'), /no RUN script/);
});

// station-generated one-liners (`one_liner` / `rollback_one_liner` on the item JSON)
test('one-liner: rendered at the very top with its own copy button; absent -> nothing', () => {
  assert.equal(B.oneLiner(' bash /srv/vm_mgr/hugpy-station/operator-scripts/o845.sh '), 'bash /srv/vm_mgr/hugpy-station/operator-scripts/o845.sh');
  assert.equal(B.oneLiner(''), ''); assert.equal(B.oneLiner(null), ''); assert.equal(B.oneLiner('a\nb'), ''); assert.equal(B.oneLiner({}), '');
  const calls = []; const copied = [];
  const html = (strings, ...vals) => { calls.push({ s: strings.join('§'), vals }); return { strings, vals }; };
  const R = B.makeRenderer({ html, useState: v => [v, () => {}], useEffect: () => {}, copyText: t => { copied.push(t); return true; }, document: null });
  const deep = (v) => Array.isArray(v) ? v.flatMap(deep) : [v];
  const text = (c) => deep(c.vals).filter(x => typeof x === 'string').join(' ') + ' ' + c.s;
  const note = ['RUN:', FENCE + 'bash', 'echo run', FENCE].join('\n');
  const ol = 'bash /srv/vm_mgr/hugpy-station/operator-scripts/o845.sh', rol = 'bash /srv/vm_mgr/hugpy-station/operator-scripts/o845.rollback.sh';
  R.Item({ operator: true, text: '[root] t', note, oneLiner: ol, rollbackOneLiner: rol });
  const item = calls[calls.length - 1];
  // the one-liner row is the FIRST child of the item, above the ▶ copy script tools
  const kids = item.vals.filter(v => v && typeof v === 'object');
  assert.ok(kids[0].strings.join('').includes('bmd-ol-row'), 'one-liner row first');
  assert.ok(calls.some(c => c.vals.includes(ol)), 'run one-liner passed as a prop');
  assert.ok(calls.some(c => c.vals.includes(rol)), 'rollback one-liner passed as a prop');
  calls.length = 0;
  R.OneLiner({ cmd: ol, role: 'run' });
  let all = calls.map(text).join('\n');
  assert.match(all, /⧉ copy one-liner/); assert.ok(deep(calls[0].vals).includes(ol));
  const click = deep(calls[0].vals).find(v => typeof v === 'function' && String(v).includes('copyText'));
  click({ stopPropagation() {} }); assert.deepEqual(copied, [ol]);
  calls.length = 0;
  R.OneLiner({ cmd: rol, role: 'rollback' });
  assert.match(calls.map(text).join('\n'), /⧉ copy rollback one-liner/);
  // absent field: no one-liner row, no badge about it
  calls.length = 0;
  R.Item({ operator: true, text: '[root] t', note });
  all = calls.map(text).join('\n');
  assert.doesNotMatch(all, /one-liner/i);
});

// station-attached run result on an operator item
test('result: exit/ts/path/tail rendered as present; absent -> nothing', () => {
  const full = { exit: 0, path: '/srv/vm_mgr/hugpy-station/operator-scripts/results/o845/1790880000.log', ts: 1790880000, tail: 'a\nb\nc\n', kind: 'run' };
  const v = B.resultView(full);
  assert.equal(v.exit, 0); assert.equal(v.ts, '2026-10-01T18:40:00Z'); assert.equal(v.tail, 'a\nb\nc'); assert.equal(v.kind, 'run');
  assert.equal(B.resultView({ exit: '3', ts: '2026-10-01 14:00' }).exit, 3);
  assert.equal(B.resultView({ ts: 1790880000123 }).ts, '2026-10-01T18:40:00Z');
  assert.equal(B.resultView(null), null); assert.equal(B.resultView({}), null); assert.equal(B.resultView('x'), null);
  assert.equal(B.resultView({ exit: null, path: '', tail: '  ' }), null);
  assert.equal(B.resultView({ kind: 'bogus', exit: 1 }).kind, '');
  const calls = []; const copied = [];
  const html = (strings, ...vals) => { calls.push({ strings, vals }); return { strings, vals }; };
  const R = B.makeRenderer({ html, useState: v => [v, () => {}], useEffect: () => {}, copyText: t => { copied.push(t); return true; }, document: null });
  const deep = (x) => Array.isArray(x) ? x.flatMap(deep) : [x];
  const txt = () => calls.map(c => c.strings.join(' ') + ' ' + deep(c.vals).filter(x => typeof x === 'string').join(' ')).join('\n');
  R.Result({ result: v });
  let all = txt();
  assert.match(all, /✔ exit 0/); assert.match(all, /2026-10-01T18:40:00Z/); assert.match(all, /⧉ copy path/); assert.match(all, /\+ tail \(3\)/);
  assert.doesNotMatch(all, /bmd-res-tail/, 'tail folded by default');
  const pathCall = calls.find(c => c.vals.includes(full.path));
  assert.ok(pathCall, 'log path rendered as a text child');
  const click = deep(pathCall.vals).find(f => typeof f === 'function' && String(f).includes('copyText'));
  click({ stopPropagation() {} }); assert.deepEqual(copied, [full.path]);
  calls.length = 0;
  R.Result({ result: B.resultView({ exit: 2, kind: 'rollback' }) });
  all = txt();
  assert.match(all, /✖ exit 2/); assert.match(all, /ROLLBACK RESULT/); assert.doesNotMatch(all, /copy path|tail/);
  // the item: result sits directly under the one-liner row; absent -> no result markup
  calls.length = 0;
  R.Item({ operator: true, text: '[root] t', note: 'RUN:\n' + FENCE + 'bash\nx\n' + FENCE, oneLiner: 'bash x.sh', result: full });
  const item = calls[calls.length - 1];
  const kids = item.vals.filter(x => x && typeof x === 'object');
  assert.ok(kids[0].strings.join('').includes('bmd-ol-row'));
  assert.ok(kids[1].vals.includes(R.Result) && kids[1].vals.some(x => x && x.path === full.path), 'result right after the one-liner row');
  calls.length = 0;
  R.Item({ operator: true, text: '[root] t', note: 'RUN:\n' + FENCE + 'bash\nx\n' + FENCE });
  assert.doesNotMatch(txt(), /exit|copy path|RESULT/);
});
