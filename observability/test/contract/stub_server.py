#!/usr/bin/env python3
"""Contract stub for the Station observability browser (never shipped).

Runs the server half of observability/README.md ("Server contract") on scratch state only:

  :PORT       a central-like Flask app: console_trace v2 (console_trace_ref.py)
              mounted under /api, a demo page whose event wiring mirrors the
              hugpy UI (one delegated root listener like React, a direct listener,
              a setInterval poller), demo routes (GET, POST, await chain, raise,
              handled raise, burst), and an OpenAI-compatible /v1 stub for the
              'explain' inference level (counts its calls).
  :PROV_PORT  a station-backend-like app exposing /api/provenance(/ingest) over
              the Station's REAL provenance.py, with HUGPY_STATION_STATE pointed
              at a scratch dir.

    HUGPY_CONSOLE_TRACE_DB=/srv/vm_mgr/tmp/obs-evidence2/stub/trace.sqlite3 \
    HUGPY_STATION_STATE=/srv/vm_mgr/tmp/obs-evidence2/stub/station-state \
    /srv/hugpy/venv/bin/python stub_server.py --port 7393 --prov-port 7394

Refuses to start unless both state paths are set and are not production paths.
"""
import argparse
import os
import sys
import threading
import time

from flask import Flask, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=7393)
ap.add_argument("--prov-port", type=int, default=7394)
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--hugpy-ui", default="", help="hugpy's built console_dist, served READ-ONLY in place on --ui-port")
ap.add_argument("--ui-port", type=int, default=7395)
args = ap.parse_args()

for var in ("HUGPY_CONSOLE_TRACE_DB", "HUGPY_STATION_STATE"):
    v = os.environ.get(var, "")
    assert v and "/tmp" in v and not v.startswith("/srv/hugpy"), f"{var} must point at a scratch path, got {v!r}"

sys.path.insert(0, HERE)
import console_trace_ref as ct  # noqa: E402

app = Flask("obs_contract_stub")
ct.install_console_trace(app)
app.register_blueprint(ct.trace_bp, url_prefix="/api")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>obs contract stub</title>
<style>body{font:14px sans-serif;margin:16px}button,select{margin:4px}#out{white-space:pre-wrap;background:#eee;padding:6px}</style>
</head><body><h3>observability contract stub</h3>
<div id="root">
  <button data-action="load" id="load">Load items</button>
  <button data-action="save" id="save">Save (POST)</button>
  <button data-action="chain" id="chain">Async chain</button>
  <button data-action="boom" id="boom">Boom (500)</button>
  <button data-action="handled" id="handled">Handled raise</button>
  <button data-action="burst" id="burst">Burst x20</button>
  <button data-action="noop" id="noop">No request</button>
</div>
<button id="xhr">XHR slow</button>
<select id="kind"><option value="1">1</option><option value="4">4</option></select>
<pre id="out"></pre><div id="list"></div>
<script src="/static/app.js"></script></body></html>"""

APP_JS = r"""
const out = (t) => { document.getElementById('out').textContent = t; };
async function api(path, init) {
  const r = await fetch(path, init);
  if (!r.ok) throw new Error(path + ' -> HTTP ' + r.status);
  return r.json();
}
const handlers = {
  async load() {
    const data = await api('/api/items?n=3');
    out(JSON.stringify(data));
    console.log('items loaded', data.items.length);
  },
  async save() {
    const created = await api('/api/items', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'widget', qty: 2 }) });
    out('created ' + created.item.id);
    console.log('saved item', created.item.id);
  },
  async chain() {
    await new Promise((r) => setTimeout(r, 15));
    const first = await api('/api/items?n=1');
    const detail = await api('/api/items/' + first.items[0].id);
    out('chain -> ' + detail.item.name);
    console.log('chain done', detail.item.name);
  },
  async boom() {
    try { await api('/api/boom'); }
    catch (e) { console.error('boom failed', e.message); out('boom: ' + e.message); }
  },
  async handled() {
    const r = await api('/api/handled');
    out('handled: ' + r.fallback);
  },
  async burst() {
    const ids = Array.from({ length: 20 }, (_, i) => i + 1);
    const all = await Promise.all(ids.map((i) => api('/api/items/' + i)));
    out('burst ' + all.length);
    console.log('burst done', all.length);
  },
  noop() { out('nothing requested'); },
};
// React-like: ONE delegated listener on the root container dispatches every click.
function dispatchRootClick(ev) {
  const el = ev.target.closest('[data-action]');
  if (el && handlers[el.dataset.action]) handlers[el.dataset.action](ev);
}
document.getElementById('root').addEventListener('click', dispatchRootClick);
// A direct listener (XHR).
function onXhrClick() {
  const x = new XMLHttpRequest();
  x.open('GET', '/api/slow');
  x.onload = () => out(x.responseText);
  x.send();
}
document.getElementById('xhr').addEventListener('click', onXhrClick);
document.getElementById('kind').addEventListener('change', async function onKindChange(e) {
  const d = await api('/api/items?n=' + e.target.value);
  out('kind ' + d.items.length);
});
// A timer-driven poller: its requests must NOT be attributed to user actions.
setInterval(function pollStatus() { fetch('/api/poll').then((r) => r.json()).catch(() => {}); }, 1500);
"""

ITEMS = {}


def _fmt(i):
    return {"id": i, "name": f"item-{i}"}


def build_items(n):
    return [_fmt(i) for i in range(1, n + 1)]


@app.get("/")
def index():
    return PAGE


@app.get("/static/app.js")
def app_js():
    return APP_JS, 200, {"Content-Type": "application/javascript"}


@app.get("/api/items")
def list_items():
    n = int(request.args.get("n", 2))
    return jsonify({"items": build_items(n)})


def _validate(doc):
    if not isinstance(doc, dict) or not doc.get("name"):
        raise ValueError("name is required")
    return {"name": str(doc["name"]), "qty": int(doc.get("qty", 1))}


def _store(fields):
    item = {"id": 1000 + len(ITEMS), **fields}
    ITEMS[item["id"]] = item
    return item


@app.post("/api/items")
def create_item():
    item = _store(_validate(request.get_json(silent=True)))
    return jsonify({"item": item}), 201


@app.get("/api/items/<int:item_id>")
def get_item(item_id):
    time.sleep(0.002)
    return jsonify({"item": ITEMS.get(item_id) or _fmt(item_id)})


def _slow_work():
    time.sleep(0.05)
    return "slow done"


@app.get("/api/slow")
def slow():
    return jsonify({"msg": _slow_work()})


def _explode(key):
    raise ValueError(f"config key {key!r} is missing")


def _load_config(key):
    return _explode(key)


@app.get("/api/boom")
def boom():
    return jsonify(_load_config("model_path"))


def _risky():
    raise KeyError("cache-miss")


@app.get("/api/handled")
def handled():
    try:
        value = _risky()
    except KeyError:
        value = "default"
    return jsonify({"fallback": value})


@app.get("/api/poll")
def poll():
    return jsonify({"ok": True, "at": time.time()})


# ── OpenAI-compatible stub for the explain level ──────────────────────────
LLM_CALLS = []


@app.get("/v1/models")
def models():
    return jsonify({"object": "list", "data": [{"id": "stub-explainer", "object": "model"}]})


@app.post("/v1/chat/completions")
def chat():
    body = request.get_json(silent=True) or {}
    msgs = body.get("messages") or []
    user = next((m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
    LLM_CALLS.append({"at": time.time(), "model": body.get("model"), "chars": len(user)})
    first = [ln for ln in user.splitlines() if ln.strip()][:3]
    text = ("STUB EXPLANATION (deterministic stub, not a model): received a trace of "
            f"{len(user)} chars; it begins: " + " | ".join(first)[:300])
    return jsonify({"id": f"stub-{len(LLM_CALLS)}", "object": "chat.completion", "model": body.get("model"),
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": text}}]})


@app.get("/stub/llm-calls")
def llm_calls():
    return jsonify({"count": len(LLM_CALLS), "calls": LLM_CALLS})


# ── station-backend-like provenance (the Station's real provenance.py) ────
prov = Flask("obs_provenance_stub")
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "..", "resources", "backend")))
import provenance  # noqa: E402


@prov.post("/api/provenance/ingest")
def prov_ingest():
    body = request.get_json(silent=True) or {}
    eid = provenance.record(str(body.get("kind") or "browser_event")[:80], body.get("body", body),
                            source=str(body.get("source") or "observability-browser")[:120],
                            actor=str(body.get("actor") or "browser")[:120],
                            session_id=str(body.get("session_id") or "")[:160],
                            trace_id=str(body.get("trace_id") or "")[:160],
                            operation=str(body.get("operation") or "browser")[:120])
    return jsonify({"ok": True, "event_id": eid})


@prov.get("/api/provenance")
def prov_rows():
    return jsonify({"ok": True, **provenance.rows(int(request.args.get("after", 0)), 500)})


# ── the REAL hugpy console bundle (read in place, never copied or written), with
#    its backend answered by stubs on a separate port ─────────────────────────
ui = Flask("obs_hugpy_ui_stub", static_folder=None)
ct.install_console_trace(ui)
ui.register_blueprint(ct.trace_bp, url_prefix="/api", name="console_trace_ui")


@ui.get("/api/auth/config")
def ui_auth_config():
    return jsonify({"mode": "open", "base": None})


@ui.get("/api/readiness")
def ui_readiness():
    return jsonify({"central": {"version": "stub", "auth_mode": "open"}, "storage": {"exists": True, "model_count": 0},
                    "serving": {"any_serving": False}, "console": {}})


def _stub_payload(path):
    """Shape-agnostic answer for any other hugpy API path the console asks for."""
    return {"stub": True, "path": path, "items": [], "data": [], "workers": [], "models": []}


@ui.route("/api/<path:sub>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def ui_api(sub):
    return jsonify(_stub_payload("/api/" + sub))


@ui.route("/llm/<path:sub>", methods=["GET", "POST"])
def ui_llm(sub):
    return jsonify(_stub_payload("/llm/" + sub))


@ui.get("/")
@ui.get("/<path:sub>")
def ui_static(sub=""):
    from flask import send_from_directory
    root = os.path.abspath(args.hugpy_ui)
    full = os.path.abspath(os.path.join(root, sub))
    if sub and full.startswith(root + os.sep) and os.path.isfile(full):
        return send_from_directory(root, sub)
    return send_from_directory(root, "index.html")


if __name__ == "__main__":
    threading.Thread(target=lambda: prov.run(host=args.host, port=args.prov_port, threaded=True),
                     daemon=True).start()
    if args.hugpy_ui:
        threading.Thread(target=lambda: ui.run(host=args.host, port=args.ui_port, threaded=True), daemon=True).start()
    app.run(host=args.host, port=args.port, threaded=True)
