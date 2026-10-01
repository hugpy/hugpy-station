#!/usr/bin/env python3
"""Local verification target for the observability browser (never shipped).

A tiny Flask app that mounts hugpy central's OWN console_trace module — loaded
read-only from its file, unmodified — so hops 3-5 are verified against the exact
server code :7002 runs, without touching :7002 or its trace DB.

    HUGPY_CONSOLE_TRACE_DB=/srv/vm_mgr/tmp/obs-target/trace.sqlite3 \
    CONSOLE_TRACE_PY=/srv/hugpy/src/hugpy/py/services/hugpy_server/src/hugpy_server/app/routes/console_trace_routes.py \
    /srv/hugpy/venv/bin/python target_app.py --port 7391
"""
import argparse
import importlib.util
import os
import time

from flask import Flask, jsonify, request

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=7391)
args = ap.parse_args()

assert os.environ.get("HUGPY_CONSOLE_TRACE_DB"), "set HUGPY_CONSOLE_TRACE_DB to a scratch path"
spec = importlib.util.spec_from_file_location("console_trace_routes", os.environ["CONSOLE_TRACE_PY"])
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

app = Flask(__name__)
ct.install_console_trace(app)
app.register_blueprint(ct.trace_bp, url_prefix="/api")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>obs target</title></head><body>
<h3>observability target</h3>
<button id="load">Load items</button> <button id="xhr">XHR slow</button>
<button id="save">Save (POST)</button> <button id="boom">Boom (500)</button> <button id="chain">Async chain</button>
<pre id="out"></pre>
<script src="/static/app.js"></script></body></html>"""

APP_JS = """
async function loadItems(n) {
  const r = await fetch('/api/items?n=' + n);
  return r.json();
}
async function onLoadClick() {
  const data = await loadItems(3);
  document.getElementById('out').textContent = JSON.stringify(data);
  console.log('items loaded', data.items.length);
}
function onXhrClick() {
  const x = new XMLHttpRequest();
  x.open('GET', '/api/slow');
  x.onload = () => { document.getElementById('out').textContent = x.responseText; };
  x.send();
}
function onSaveClick() {
  fetch('/api/items', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"name":"x"}' });
}
function onBoomClick() {
  fetch('/api/boom').then(r => { if (!r.ok) throw new Error('boom status ' + r.status); })
    .catch(e => console.error('boom failed', e));
}
async function onChainClick() {
  await new Promise((r) => setTimeout(r, 10));
  const data = await loadItems(1);
  document.getElementById('out').textContent = 'chain ' + data.items.length;
}
document.getElementById('load').addEventListener('click', onLoadClick);
document.getElementById('chain').addEventListener('click', onChainClick);
document.getElementById('xhr').addEventListener('click', onXhrClick);
document.getElementById('save').addEventListener('click', onSaveClick);
document.getElementById('boom').addEventListener('click', onBoomClick);
"""


def _fmt(i):
    return {"id": i, "name": f"item-{i}"}


def build_items(n):
    return [_fmt(i) for i in range(n)]


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


@app.post("/api/items")
def create_item():
    return jsonify({"ok": True, "saw_trace_header": bool(request.headers.get("X-Hugpy-Trace-Id"))}), 201


def _slow_work():
    time.sleep(0.05)
    return "slow done"


@app.get("/api/slow")
def slow():
    return jsonify({"msg": _slow_work()})


def _explode():
    raise ValueError("deliberate failure for the trace")


@app.get("/api/boom")
def boom():
    _explode()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=args.port, threaded=True)
