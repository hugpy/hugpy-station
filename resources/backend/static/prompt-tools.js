/* prompt-tools.js — ✍ prompt helpers (1.0.143): clipboard classification and
 * speech (dictation + reply read-aloud). No framework; window.__fvPromptTools
 * in the page, module.exports under node (tests/prompt-tools.test.js).
 *
 * CLIPBOARD (operator 2026-10-01): the terminal cannot take file input, so the
 * prompt bar does. An IMAGE on the clipboard (screenshot / "copy image") is
 * attached exactly like 📎 media; a copied file PATH (text/plain,
 * text/uri-list, x-special/gnome-copied-files) stays TEXT. The marker is the
 * MIME type, never a guess at the content.
 *
 * SPEECH — default OFF, toggles persisted per browser (localStorage):
 *   dictation  Web Speech SpeechRecognition when the browser has a working
 *              one; Electron's Chromium exposes webkitSpeechRecognition but has
 *              no recognition backend (it fails with "network"), so there — or
 *              after any such failure — the station's Whisper route is used
 *              (MediaRecorder -> POST /api/voice/transcribe -> hugpy
 *              /api/ml/transcribe, the 1.0.133-1.0.137 engine). Neither -> an
 *              explicit "unavailable" state.
 *   read aloud speechSynthesis (local voice; nothing leaves the browser).
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.__fvPromptTools = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var PATH_TYPES = ["text/uri-list", "x-special/gnome-copied-files", "application/x-kde-cutselection"];

  /* types: clipboardData.types (or ClipboardItem.types); items: [{kind, type}]
     (clipboardData.items). -> {kind:"image", index:[i…]} | {kind:"text"}.
     A file copied in a file manager carries a path type, so it stays text even
     when Chromium also exposes the file itself as an image item. */
  function classifyClipboard(types, items) {
    types = Array.prototype.slice.call(types || []).map(function (t) { return String(t).toLowerCase(); });
    if (types.some(function (t) { return PATH_TYPES.indexOf(t) >= 0; })) return { kind: "text" };
    var idx = [];
    Array.prototype.forEach.call(items || [], function (it, i) {
      var ty = String((it && it.type) || "").toLowerCase();
      if (ty.indexOf("image/") === 0 && (!it.kind || it.kind === "file")) idx.push(i);
    });
    return idx.length ? { kind: "image", index: idx } : { kind: "text" };
  }

  /* the image/* type of a ClipboardItem's types, or "" */
  function pickImageType(types) {
    var ts = Array.prototype.slice.call(types || []);
    if (ts.some(function (t) { return PATH_TYPES.indexOf(String(t).toLowerCase()) >= 0; })) return "";
    for (var i = 0; i < ts.length; i++) if (String(ts[i]).toLowerCase().indexOf("image/") === 0) return ts[i];
    return "";
  }

  function extFor(mime) {
    var m = /^image\/([a-z0-9.+-]+)/i.exec(mime || "");
    var e = m ? m[1].toLowerCase() : "png";
    return e === "jpeg" ? "jpg" : (e === "svg+xml" ? "svg" : e);
  }
  function clipName(mime, now) {
    var d = new Date(now || Date.now());
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return "clipboard-" + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-" +
      p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + "." + extFor(mime);
  }

  /* ---------- speech: capability ---------- */
  var BROKEN_KEY = "fv-stt-webspeech-broken";
  function isElectron(win) { return /\bElectron\//.test(String((win.navigator || {}).userAgent || "")); }
  function sttSupport(win, store) {
    var SR = win.SpeechRecognition || win.webkitSpeechRecognition;
    var nav = win.navigator || {};
    var canRecord = !!(win.MediaRecorder && nav.mediaDevices && nav.mediaDevices.getUserMedia);
    var broken = false;
    try { broken = !!(store && store.getItem(BROKEN_KEY) === "1"); } catch (e) {}
    if (SR && !isElectron(win) && !broken) return { engine: "webspeech", label: "browser speech recognition" };
    var why = !SR ? "this browser has no Web Speech recognition"
      : isElectron(win) ? "the Station app's Chromium has no speech-recognition backend"
      : "browser speech recognition failed here before";
    if (canRecord) return { engine: "whisper", label: "station Whisper", reason: why + " — using the station's Whisper" };
    return { engine: "none", reason: why + ", and microphone recording is unavailable" };
  }
  function ttsSupport(win) {
    var ss = win.speechSynthesis;
    if (!ss || !win.SpeechSynthesisUtterance) return { ok: false, reason: "this browser has no speech synthesis" };
    var n = 0;
    try { n = (ss.getVoices() || []).length; } catch (e) {}
    // voices load async in Chromium; 0 now is not final — the caller re-checks on speak
    return { ok: true, voices: n };
  }

  /* reply text worth speaking: no code blocks, no markdown noise, capped */
  function speakable(text, cap) {
    var t = String(text || "")
      .replace(/```[\s\S]*?```/g, " (code block) ")
      .replace(/`([^`]*)`/g, "$1")
      .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/^#{1,6}\s+/gm, "")
      .replace(/[*_~>|]+/g, " ")
      .replace(/\s+/g, " ").trim();
    cap = cap || 4000;
    return t.length > cap ? t.slice(0, cap) + " … (reply truncated)" : t;
  }

  /* ---------- speech: dictation controller ----------
     opts: {win, store, transcribe(blob)->Promise<text>, onText(text), onState(state, detail)}
     states: idle | listening | recording | transcribing | error | unavailable */
  function Dictation(opts) {
    var win = opts.win, store = opts.store, st = { rec: null, sr: null, chunks: [], busy: false };
    function state(s, d) { try { opts.onState && opts.onState(s, d || ""); } catch (e) {} }
    function startWhisper(note) {
      var nav = win.navigator;
      return nav.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
        var mime = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"].filter(function (m) {
          return win.MediaRecorder.isTypeSupported && win.MediaRecorder.isTypeSupported(m); })[0] || "";
        st.chunks = [];
        st.rec = new win.MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
        st.rec.ondataavailable = function (e) { if (e.data && e.data.size) st.chunks.push(e.data); };
        st.rec.onerror = function () { stream.getTracks().forEach(function (t) { t.stop(); }); st.rec = null; state("error", "recording failed"); };
        st.rec.onstop = function () {
          stream.getTracks().forEach(function (t) { t.stop(); });
          var blob = new win.Blob(st.chunks, { type: (st.rec && st.rec.mimeType) || mime || "audio/webm" });
          st.rec = null; st.busy = true; state("transcribing", "station Whisper");
          Promise.resolve(opts.transcribe(blob)).then(function (text) {
            text = String(text || "").trim();
            if (!text) throw new Error("Whisper returned no text");
            opts.onText(text); state("idle", "");
          }).catch(function (e) { state("error", "transcription: " + ((e && e.message) || e)); })
            .then(function () { st.busy = false; });
        };
        st.rec.start();
        state("recording", note || "station Whisper — click again to stop");
      }).catch(function (e) { state("error", "microphone: " + ((e && e.message) || e)); });
    }
    function startWebSpeech() {
      var SR = win.SpeechRecognition || win.webkitSpeechRecognition;
      var sr = new SR(), gotAny = false;
      sr.continuous = true; sr.interimResults = false;
      try { sr.lang = (win.navigator && win.navigator.language) || "en-US"; } catch (e) {}
      sr.onresult = function (ev) {
        for (var i = ev.resultIndex; i < ev.results.length; i++) {
          if (ev.results[i].isFinal) { gotAny = true; opts.onText(String(ev.results[i][0].transcript || "").trim()); }
        }
      };
      sr.onerror = function (ev) {
        var err = (ev && ev.error) || "error";
        st.sr = null;
        if (err === "network" || err === "service-not-allowed" || err === "language-not-supported") {
          // no recognition backend here: remember it and fall back to Whisper at once
          try { store && store.setItem(BROKEN_KEY, "1"); } catch (e) {}
          if (!gotAny && win.MediaRecorder) { startWhisper("browser recognition unavailable (" + err + ") — recording for station Whisper; click again to stop"); return; }
          state("error", "browser speech recognition unavailable (" + err + ")");
        } else if (err === "not-allowed") state("error", "microphone permission denied");
        else if (err !== "aborted" && err !== "no-speech") state("error", "speech recognition: " + err);
      };
      sr.onend = function () { if (st.sr === sr) { st.sr = null; state("idle", ""); } };
      st.sr = sr;
      try { sr.start(); state("listening", "browser speech recognition — click again to stop"); }
      catch (e) { st.sr = null; state("error", "speech recognition: " + ((e && e.message) || e)); }
    }
    return {
      active: function () { return !!(st.rec || st.sr || st.busy); },
      support: function () { return sttSupport(win, store); },
      toggle: function () {
        if (st.busy) return;
        if (st.sr) { try { st.sr.stop(); } catch (e) {} return; }
        if (st.rec) { try { st.rec.stop(); } catch (e) {} return; }
        var sup = sttSupport(win, store);
        if (sup.engine === "webspeech") startWebSpeech();
        else if (sup.engine === "whisper") startWhisper();
        else state("unavailable", sup.reason);
      },
      stop: function () {
        if (st.sr) try { st.sr.abort(); } catch (e) {}
        if (st.rec) try { st.rec.stop(); } catch (e) {}
      }
    };
  }

  /* speak one text with the browser voice; -> "" or the reason it could not */
  function speak(win, text) {
    var sup = ttsSupport(win);
    if (!sup.ok) return sup.reason;
    var t = speakable(text);
    if (!t) return "";
    try {
      win.speechSynthesis.cancel();
      win.speechSynthesis.speak(new win.SpeechSynthesisUtterance(t));
    } catch (e) { return "speech synthesis: " + ((e && e.message) || e); }
    return sup.voices ? "" : "no voices reported yet (on Linux the Station needs speech-dispatcher voices)";
  }

  return { classifyClipboard: classifyClipboard, pickImageType: pickImageType, clipName: clipName,
           sttSupport: sttSupport, ttsSupport: ttsSupport, speakable: speakable,
           Dictation: Dictation, speak: speak, isElectron: isElectron, BROKEN_KEY: BROKEN_KEY };
});
