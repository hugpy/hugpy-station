'use strict';
// prompt-tools (resources/backend/static/prompt-tools.js): clipboard image vs
// path/text classification by MIME, and the speech capability / engine choice.
// Run:  node --test tests/prompt-tools.test.js      (no deps)
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const T = require(path.join(__dirname, '..', 'resources', 'backend', 'static', 'prompt-tools.js'));

test('a screenshot (image/png item) is an image attachment', () => {
  assert.deepEqual(T.classifyClipboard(['Files'], [{ kind: 'file', type: 'image/png' }]), { kind: 'image', index: [0] });
  // "copy image" from a browser: image/png + text/html -> still the image
  assert.deepEqual(T.classifyClipboard(['text/html', 'Files'],
    [{ kind: 'string', type: 'text/html' }, { kind: 'file', type: 'image/png' }]), { kind: 'image', index: [1] });
});

test('a copied file PATH stays text, even when the file itself is an image', () => {
  assert.deepEqual(T.classifyClipboard(['text/plain', 'text/uri-list', 'Files'],
    [{ kind: 'string', type: 'text/plain' }, { kind: 'file', type: 'image/png' }]), { kind: 'text' });
  assert.deepEqual(T.classifyClipboard(['x-special/gnome-copied-files', 'text/plain'],
    [{ kind: 'string', type: 'text/plain' }]), { kind: 'text' });
  assert.deepEqual(T.classifyClipboard(['text/plain'], [{ kind: 'string', type: 'text/plain' }]), { kind: 'text' });
  assert.deepEqual(T.classifyClipboard([], []), { kind: 'text' });
});

test('pickImageType / clipName', () => {
  assert.equal(T.pickImageType(['text/html', 'image/png']), 'image/png');
  assert.equal(T.pickImageType(['text/uri-list', 'image/png']), '');
  assert.equal(T.pickImageType(['text/plain']), '');
  assert.match(T.clipName('image/jpeg', Date.UTC(2026, 9, 1, 12, 0, 0)), /^clipboard-202610\d\d-\d{6}\.jpg$/);
});

const store = () => { const m = {}; return { getItem: k => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); } }; };
const rec = { MediaRecorder: function () {}, navigator: { userAgent: 'Mozilla/5.0 Chrome/124', mediaDevices: { getUserMedia() {} } } };

test('stt: Web Speech in a browser that has it; Whisper in Electron; unavailable without either', () => {
  assert.equal(T.sttSupport(Object.assign({ webkitSpeechRecognition: function () {} }, rec), store()).engine, 'webspeech');
  const electron = { webkitSpeechRecognition: function () {}, MediaRecorder: function () {},
    navigator: { userAgent: 'Mozilla/5.0 hugpy-station/1.0.143 Chrome/124 Electron/30.5.1', mediaDevices: { getUserMedia() {} } } };
  const e = T.sttSupport(electron, store());
  assert.equal(e.engine, 'whisper'); assert.match(e.reason, /no speech-recognition backend/);
  const s = store(); s.setItem(T.BROKEN_KEY, '1');
  assert.equal(T.sttSupport(Object.assign({ webkitSpeechRecognition: function () {} }, rec), s).engine, 'whisper');
  const none = T.sttSupport({ navigator: { userAgent: 'x' } }, store());
  assert.equal(none.engine, 'none'); assert.match(none.reason, /unavailable/);
});

test('tts support + speakable text', () => {
  assert.equal(T.ttsSupport({}).ok, false);
  assert.equal(T.ttsSupport({ speechSynthesis: { getVoices: () => [1, 2] }, SpeechSynthesisUtterance: function () {} }).voices, 2);
  assert.equal(T.speakable('## Done\nSee `x` and ```js\nlong()\n``` [link](http://a)'), 'Done See x and (code block) link');
  assert.match(T.speakable('a'.repeat(5000)), /reply truncated\)$/);
});

test('Dictation reports unavailable instead of failing silently', () => {
  const states = [];
  const d = T.Dictation({ win: { navigator: { userAgent: 'x' } }, store: store(), transcribe: async () => '',
    onText() {}, onState: (s, why) => states.push([s, why]) });
  d.toggle();
  assert.equal(states[0][0], 'unavailable');
  assert.match(states[0][1], /no Web Speech recognition/);
});
