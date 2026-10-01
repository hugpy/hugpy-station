#!/usr/bin/env python3
"""
station-console — a web console for the LXD dev stations.

One tab per station, each a real terminal piped through `lxc exec` (so it works
regardless of the macvlan host<->VM limitation), plus live status and power /
snapshot controls. Serves a browser UI and the WebSocket/REST backend it talks
to. Runs on the host (headless-friendly); open it from any LAN browser.

    python3 server.py --set-password     # set the login password (writes .auth)
    python3 server.py                    # serve on 0.0.0.0:8800

Auth: a login form + signed session cookie (cookies ride the WS handshake too).
The password is stored hashed in console/.auth (pbkdf2-sha256). Scripts can
instead pass an API token (STATION_CONSOLE_TOKEN) via X-Console-Token / ?token=.
If no password is set, the console stays OPEN and prints a warning.

Optional TLS: set STATION_CONSOLE_CERT / STATION_CONSOLE_KEY (recommended, since
form auth over plain HTTP sends the password in cleartext on the LAN).
"""
import asyncio
import base64
import fcntl
import getpass
import logging
import hashlib
import hmac
import ipaddress
import json
import os
import pty
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import signal
import contextlib
import ssl
import struct
import sys
import termios
import time
from pathlib import Path

try:
    import aiohttp
except ImportError:  # no system python3-aiohttp: vendored wheels (built for Ubuntu noble / py3.12)
    sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
    import aiohttp
from aiohttp import WSMsgType, web

import locus_exec as LX   # 1.0.141: ONE code path to act on a locus (self / ssh / lxd)
import prompt_send as PS  # 1.0.143: ✍ prompt delivers directly (serve session / tmux seat)
import todo_digest as TD  # 1.0.144: deterministic open-todos digest -> each locus's keeper session
import operator_scripts as OPS  # 1.0.144: operator items' RUN blocks as files on disk
import directives as DV   # 1.0.143: per-(locus x session) directives + init briefs, one source of record

ROOT = Path(__file__).resolve().parent          # .../vm_mgr/console
VM_BIN = ROOT.parent / "bin"                      # .../vm_mgr/bin (vm-* scripts)
STATIC = ROOT / "static"
# keeper (overseer) read-only surfaces: the host-generated oversight feed, the
# conversation-log store, and the broker control audit. The console only READS
# these — the keeper VM acts through its gated broker, not the console.
KEEPER_DIR = ROOT.parent / "keeper"
KEEPER_FEED = KEEPER_DIR / "state" / "fleet.json"
KEEPER_SESS_FILE = KEEPER_DIR / "logs" / "conversations" / "sessions.json"
KEEPER_AUDIT = KEEPER_DIR / "logs" / "control-audit.jsonl"
KEEPER_DIRV_LOG = KEEPER_DIR / "directives" / "log.jsonl"
KEEPER_DIRV_CUR = KEEPER_DIR / "directives" / "current.json"
AUTH_FILE = ROOT / ".auth"                        # pbkdf2 password hash
SECRET_FILE = ROOT / ".secret"                    # cookie-signing key

# HOST-LOCAL PATCH: under systemd the service PATH misses the service user's
# ~/.local/bin, where the host keeper's `claude` lives — without this the
# frontier "claude-code" backend always reports unavailable.
_local_bin = os.path.expanduser("~/.local/bin")
if os.path.isdir(_local_bin) and _local_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _local_bin + ":" + os.environ["PATH"]

# A relocated python (conda prefix move) can carry a compiled-in CA path that
# no longer exists — every outbound HTTPS then fails "unable to get local
# issuer certificate" (URL imports, public LLM gateways, B's fallback base).
# If the interpreter has NO working default CA store, point SSL_CERT_FILE at
# certifi's bundle. An operator-set SSL_CERT_FILE always wins; a healthy
# interpreter is left alone.
if "SSL_CERT_FILE" not in os.environ:
    import ssl as _ssl
    _vp = _ssl.get_default_verify_paths()
    if not _vp.cafile and not _vp.capath:
        try:
            import certifi
            os.environ["SSL_CERT_FILE"] = certifi.where()
        except ImportError:
            pass
# vm-bridge gate dir (host-authoritative). A station's comms channel is OPEN iff
# <name>.gate exists — the exact file the broker and the vm-bridge CLI read, so
# toggling it here stays in sync with `vm-bridge on/off`.
BRIDGE_STATE = Path(os.environ.get("BRIDGE_STATE",
                                   str(ROOT.parent / "bridge-state")))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8800"))
TOKEN = os.environ.get("STATION_CONSOLE_TOKEN", "")           # API token (scripts)
USERNAME = os.environ.get("STATION_CONSOLE_USER", "admin")
SESSION_TTL = int(os.environ.get("STATION_CONSOLE_SESSION_TTL", "86400"))
COOKIE = "sc_session"
CERT = os.environ.get("STATION_CONSOLE_CERT", "")
KEY = os.environ.get("STATION_CONSOLE_KEY", "")
# auto-enable TLS if console.crt/console.key sit next to this file (any launch path)
if not (CERT and KEY) and (ROOT / "console.crt").exists() and (ROOT / "console.key").exists():
    CERT, KEY = str(ROOT / "console.crt"), str(ROOT / "console.key")
SHOW_BASE = os.environ.get("STATION_CONSOLE_SHOW_BASE", "") == "1"
BASE_NAME = "sandbox"
# shell in as the unprivileged dev user (uid/gid 1000 == the host dev user).
SH_UID = os.environ.get("STATION_CONSOLE_UID", "1000")
SH_GID = os.environ.get("STATION_CONSOLE_GID", "1000")
SH_HOME = os.environ.get("STATION_CONSOLE_HOME", "/home/ubuntu")

PASSWORD_HASH = os.environ.get("STATION_CONSOLE_PASSWORD_HASH", "")
if not PASSWORD_HASH and AUTH_FILE.exists():
    PASSWORD_HASH = AUTH_FILE.read_text().strip()
# 🌐 browser access: the UI-set password lands in the user's config dir —
# ROOT is root-owned once installed, so ROOT/.auth stays the root/standalone
# path and this is the packaged-app one. Precedence: env, ROOT, then here.
USER_AUTH_FILE = Path(os.environ.get("HUGPY_STATION_STATE") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"),
    "hugpy-station")) / ".auth"
# The cookie-signing key gets the SAME dual-path treatment as .auth (and for
# the same reason): ROOT is root-owned once installed via the deb, so a
# packaged app running as the desktop user CANNOT create ROOT/.secret — a
# latent crash that shipped keys masked until the 2026-08-13 sanitization
# (every deb >=1.0.12 had wrongly SHIPPED one shared key; with it gone, first
# launch died on PermissionError and the shell opened blank). Precedence:
# ROOT/.secret when present/creatable (root/standalone installs, workbench
# services), else the user config dir.
USER_SECRET_FILE = USER_AUTH_FILE.parent / ".secret"
if not PASSWORD_HASH and USER_AUTH_FILE.exists():
    PASSWORD_HASH = USER_AUTH_FILE.read_text().strip()
# Token-only deployments are real (a workbench console inside a VM, fronted
# by a harness that injects X-Console-Token): a set TOKEN must gate access
# even with no password hash, or such an install is open to its whole LAN.
AUTH_REQUIRED = bool(PASSWORD_HASH or TOKEN)
USE_TLS = bool(CERT and KEY)
# Behind a TLS-terminating proxy (hugpy-station-web@ + nginx/certbot) the
# console itself speaks plain http but the BROWSER is on https — cookies
# must still be Secure. The unit sets this.
SECURE_COOKIES = USE_TLS or os.environ.get("STATION_CONSOLE_PROXY_TLS") == "1"

# --- provisioning surface (Model B: create/delete/clone are homebase/VPN-only) #
# Source networks allowed to perform privileged ops. Public (proxied) clients
# arrive with X-Real-IP = their real address, so they fall outside these nets;
# VPN/LAN/local clients fall inside. Primary enforcement is also the nginx
# allow-list on those routes — this is defense-in-depth.
# Fail-closed default: localhost only. Add your VPN/LAN CIDRs (comma-separated)
# via STATION_CONSOLE_PROVISION_NETS to let those source nets provision.
PROVISION_NETS = [
    ipaddress.ip_network(c.strip()) for c in os.environ.get(
        "STATION_CONSOLE_PROVISION_NETS",
        "127.0.0.0/8",
    ).split(",") if c.strip()
]
AUDIT_FILE = Path(os.environ.get("STATION_CONSOLE_AUDIT", str(ROOT / "audit.log")))
# login throttle: block an IP after N failed logins within WINDOW seconds.
LOGIN_MAX_FAILS = int(os.environ.get("STATION_CONSOLE_LOGIN_MAX_FAILS", "5"))
LOGIN_WINDOW = int(os.environ.get("STATION_CONSOLE_LOGIN_WINDOW", "300"))
_login_fails = {}   # ip -> [timestamps]


# --------------------------------------------------------------------------- #
# auth primitives (stdlib only)
# --------------------------------------------------------------------------- #
def hash_password(pw, iters=200_000):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters)
    return f"pbkdf2_sha256${iters}${salt.hex()}${dk.hex()}"


def verify_password(pw, stored):
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _secret():
    # Read guard (2026-08-13, found live on teststation): an existing key can
    # still be UNREADABLE — e.g. a root-owned ROOT/.secret planted by a
    # one-off root run of the backend under the deb. exists() alone then
    # crashes every non-root launch; skip past what we cannot read.
    for src in (SECRET_FILE, USER_SECRET_FILE):
        try:
            if src.exists():
                return src.read_bytes()
        except OSError:
            continue
    s = secrets.token_bytes(32)
    for target in (SECRET_FILE, USER_SECRET_FILE):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(s)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            return s
        except OSError:
            continue  # ROOT is root-owned under the deb — fall to user state
    raise RuntimeError(
        "no writable location for the cookie-signing key "
        f"(tried {SECRET_FILE} and {USER_SECRET_FILE})")


SECRET = _secret()


def make_session(user, caps=("provision",)):
    # strip '=' padding so the value is cookie-safe (no quoting -> no round-trip mangling)
    payload = base64.urlsafe_b64encode(
        json.dumps({"u": user, "caps": list(caps),
                    "exp": int(time.time()) + SESSION_TTL}).encode()
    ).rstrip(b"=").decode()
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def valid_session(cookie):
    if not cookie or "." not in cookie:
        return False
    payload, sig = cookie.rsplit(".", 1)
    good = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return False
    try:
        pad = "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload + pad))
        return data.get("exp", 0) > time.time()
    except Exception:
        return False


def is_authed(request):
    if not AUTH_REQUIRED:
        return True
    if valid_session(request.cookies.get(COOKIE, "")):
        return True
    if TOKEN:                                       # script/API access
        tok = request.headers.get("X-Console-Token") or request.query.get("token", "")
        if tok and hmac.compare_digest(tok, TOKEN):
            return True
    return False


# --------------------------------------------------------------------------- #
# provisioning authZ: source (homebase/VPN), capability, CSRF, audit, throttle
# --------------------------------------------------------------------------- #
def client_ip(request):
    # Behind nginx the true client is in X-Real-IP (the proxy overwrites any
    # client-supplied value); direct VPN-tunnel hits land in request.remote.
    xr = request.headers.get("X-Real-IP")
    if xr:
        return xr.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote or ""


def is_homebase(request):
    try:
        ip = ipaddress.ip_address(client_ip(request))
    except ValueError:
        return False
    return any(ip in net for net in PROVISION_NETS)


def session_claims(request):
    cookie = request.cookies.get(COOKIE, "")
    if not valid_session(cookie):
        return None
    payload = cookie.rsplit(".", 1)[0]
    try:
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return None


def request_caps(request):
    claims = session_claims(request)
    if claims is not None:
        return set(claims.get("caps", []))
    if TOKEN:                                # a valid header token == trusted script
        tok = request.headers.get("X-Console-Token", "")
        if tok and hmac.compare_digest(tok, TOKEN):
            return {"provision"}
    return set()


def can_provision(request):
    return "provision" in request_caps(request)


def csrf_token_for(cookie):
    # double-submit: token derived from the session, exposed in a readable cookie;
    # the browser echoes it in X-Console-CSRF on mutating (privileged) requests.
    return hmac.new(SECRET, ("csrf:" + cookie).encode(), hashlib.sha256).hexdigest()


def csrf_ok(request):
    cookie = request.cookies.get(COOKIE, "")
    if not cookie:                           # header-token scripts carry no ambient cookie
        return bool(TOKEN and hmac.compare_digest(
            request.headers.get("X-Console-Token", ""), TOKEN))
    sent = request.headers.get("X-Console-CSRF", "")
    return bool(sent) and hmac.compare_digest(sent, csrf_token_for(cookie))


# ── 📜 console log feed (board t1, operator 2026-09-10: "a live log feed
# something similar to journalctl -f but specific for the console"). A ring of
# the backend's OWN log records (every station.* logger, warnings from any
# library, audit lines mirrored below) served as a backlog (/api/steward/log)
# and a live SSE stream (/api/steward/log/stream). Independent of journald so
# it works identically for the dev unit and the packaged desktop app.
import collections
_LOG_RING = collections.deque(maxlen=2000)
_LOG_SUBS = set()            # one asyncio.Queue per open stream
_LOG_LOOP = None             # the server loop — records may come from executor threads
_LOG_SEQ = [0]
_alog = logging.getLogger("station.audit")


class _RingLogHandler(logging.Handler):
    def emit(self, record):
        if record.name.startswith("aiohttp.access"):      # request noise: not a console event
            return
        try:
            msg = self.format(record)
        except Exception:
            msg = str(record.getMessage())
        _LOG_SEQ[0] += 1
        doc = {"seq": _LOG_SEQ[0], "ts": round(record.created, 3), "level": record.levelname,
               "name": record.name, "msg": msg[:4000]}
        _LOG_RING.append(doc)
        # The ring is a view; provenance is the durable source.  Keep this
        # best-effort so a locked/full state DB can never break application
        # logging.
        try:
            from provenance import record_log
            record_log(doc)
        except Exception:
            pass
        loop = _LOG_LOOP
        if loop is not None and _LOG_SUBS:
            try:
                loop.call_soon_threadsafe(_log_fanout, doc)
            except RuntimeError:
                pass                                       # loop closing


def _log_fanout(doc):
    for q in list(_LOG_SUBS):
        try:
            q.put_nowait(doc)
        except asyncio.QueueFull:
            pass


def _install_log_ring():
    root = logging.getLogger()
    if any(isinstance(h, _RingLogHandler) for h in root.handlers):
        return
    if not root.handlers:
        # Once root has a handler, logging's lastResort (WARNING+ → stderr) no
        # longer applies; keep that path so journald still sees warnings.
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.WARNING)
        sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(sh)
    h = _RingLogHandler()
    h.setLevel(logging.DEBUG)
    h.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(h)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)


_install_log_ring()


async def _start_log_ring(app):
    global _LOG_LOOP
    _LOG_LOOP = asyncio.get_running_loop()
    _alog.info("console backend up — log feed live (ring %d)", _LOG_RING.maxlen)


async def _start_handoff_retire(app):
    """1.0.146: <state>/frontier-handoff.md is no longer read by any launch. A
    leftover file is renamed (never silently dropped) and its text is filed as
    a toolserver handoff row when none is open for this locus, so what was
    written for a successor still reaches one."""
    try:
        if not FRONTIER_HANDOFF_PATH.exists():
            return
        text = FRONTIER_HANDOFF_PATH.read_text(encoding="utf-8").strip()
        dst = FRONTIER_HANDOFF_PATH.with_name(FRONTIER_HANDOFF_PATH.name + ".retired-" + time.strftime("%Y%m%d-%H%M%S"))
        FRONTIER_HANDOFF_PATH.rename(dst)
        filed = ""
        locus = _station_locus()
        if text and locus:
            try:
                st = await _ts_call(app, "handoff/station", {"locus": locus}, timeout=10)
                if not (st or {}).get("handoff"):
                    row = await _ts_call(app, "handoff/request",
                                         {"locus": locus, "text": text, "by": "station-retire-file", "spawn": False},
                                         timeout=20)
                    filed = f"; filed as toolserver handoff {row.get('id')}"
                else:
                    filed = f"; NOT filed — handoff {st['handoff']['id']} is already open on {locus}"
            except Exception as e:                           # noqa: BLE001
                filed = f"; NOT filed (toolserver: {str(e)[:120]}) — file it with handoff_request"
        _audit_line("frontier-handoff", f"legacy file retired → {dst.name} ({len(text)} chars){filed}")
    except Exception as e:                                   # noqa: BLE001
        logging.getLogger("station.handoff").warning("legacy handoff file: %s", e)


async def _start_directives(app):
    """1.0.143: every station start re-derives <state>/directives/rendered/ —
    a hand edit there never survives, by construction."""
    n = await asyncio.get_running_loop().run_in_executor(None, _render_session_directives)
    _alog.info("directives: rendered for %s (%d changed)", _station_locus() or "(unconfigured locus)", len(n or []))


async def api_steward_log(request):
    """GET /api/steward/log?tail=N — the console backend's own recent log
    records, oldest first: {seq, ts, level, name, msg}."""
    try:
        n = max(1, min(_LOG_RING.maxlen, int(request.query.get("tail") or 300)))
    except ValueError:
        n = 300
    rows = list(_LOG_RING)[-n:]
    # 1.0.141: the backend's own process log — labelled, never passed off as a locus's
    return web.json_response({"ok": True, "rows": rows, "seq": _LOG_SEQ[0], "capacity": _LOG_RING.maxlen,
                              "scope": "this station (backend process log)"})


async def api_steward_log_stream(request):
    """GET /api/steward/log/stream?after=SEQ — Server-Sent Events: the backlog
    newer than SEQ first, then one `log` event per record as it happens;
    `: keepalive` every 15 s. EventSource reconnects on its own."""
    try:
        after = int(request.query.get("after") or 0)
    except ValueError:
        after = 0
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    q = asyncio.Queue(maxsize=1000)
    _LOG_SUBS.add(q)
    try:
        await resp.write(b": connected\n\n")
        for doc in [d for d in list(_LOG_RING) if d["seq"] > after]:
            await resp.write(("event: log\ndata: " + json.dumps(doc) + "\n\n").encode())
        while True:
            try:
                doc = await asyncio.wait_for(q.get(), 15)
                await resp.write(("event: log\ndata: " + json.dumps(doc) + "\n\n").encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        _LOG_SUBS.discard(q)
    return resp


async def api_provenance(request):
    """Durable agent/tool/edit/log ledger for the Station investigator tab."""
    try:
        from provenance import rows
        after = max(0, int(request.query.get("after") or 0))
        limit = max(1, min(2000, int(request.query.get("limit") or 300)))
        return web.json_response({"ok": True, **rows(after, limit)})
    except Exception as e:
        _alog.exception("provenance read failed")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_provenance_stream(request):
    """SSE cursor stream; the client receives durable rows after its cursor."""
    try:
        cursor = max(0, int(request.query.get("after") or 0))
    except ValueError:
        cursor = 0
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    try:
        await resp.write(b": connected\n\n")
        while True:
            try:
                from provenance import rows
                doc = rows(cursor, 200)
                if doc["events"]:
                    cursor = doc["cursor"]
                    await resp.write(("event: provenance\ndata: " + json.dumps(doc) + "\n\n").encode())
                else:
                    await resp.write(b": keepalive\n\n")
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except (ConnectionResetError, RuntimeError):
                break
            except Exception as e:
                await resp.write(("event: error\ndata: " + json.dumps({"error": str(e)}) + "\n\n").encode())
                await asyncio.sleep(2)
    except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
        pass
    return resp


async def api_provenance_query(request):
    """Record that the investigator has queried the editor of a finding."""
    body = await request.json()
    finding_id = int(body.get("finding_id") or 0)
    message = str(body.get("message") or "")[:4000]
    try:
        from provenance import mark_query, record
        mark_query(finding_id)
        eid = record("editor_query", {"finding_id": finding_id, "message": message},
                      source="provenance-investigator", actor="local-agent",
                      operation="query-editor")
        return web.json_response({"ok": True, "event_id": eid})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_provenance_ingest(request):
    """Append an observation emitted by the standalone browser console."""
    body = await request.json()
    kind = str(body.get("kind") or "browser_event")[:80]
    payload = body.get("body") if "body" in body else body
    try:
        parent_id = int(body["parent_id"]) if body.get("parent_id") is not None else None
    except (TypeError, ValueError):
        parent_id = None
    try:
        from provenance import record
        eid = record(kind, payload,
                     source=str(body.get("source") or "observability-browser")[:120],
                     actor=str(body.get("actor") or "browser")[:120],
                     session_id=str(body.get("session_id") or "")[:160],
                     trace_id=str(body.get("trace_id") or "")[:160],
                     operation=str(body.get("operation") or "browser")[:120],
                     parent_id=parent_id)
        return web.json_response({"ok": True, "event_id": eid})
    except Exception as e:
        _alog.exception("provenance ingest failed")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_provenance_investigate(request):
    """Ask the local keeper agent to compare expected vs observed execution.

    This is intentionally an explicit action from the provenance tab.  The
    prompt contains bounded ledger records; the full records remain durable
    in SQLite and can be inspected by the investigator later.
    """
    body = await request.json()
    expected = str(body.get("expected") or "").strip()[:8000]
    if not expected:
        return web.json_response({"ok": False, "error": "expected outcome is required"}, status=400)
    try:
        from provenance import rows, record
        snapshot = rows(max(0, int(body.get("after") or 0)), 250)
        prompt = (
            "You are the local test investigator for Hugpy Station. Compare the expected "
            "outcome below with the observed agent outputs, tool calls, edits, and logs. "
            "Trace execution in order, identify the first divergence, name the file/item "
            "and editor responsible, distinguish a warning from a propagated error, and "
            "propose the smallest testable edit. Do not claim a fix without evidence.\n\n"
            "EXPECTED:\n" + expected + "\n\nOBSERVED LEDGER:\n" +
            json.dumps(snapshot, ensure_ascii=False, default=str)[:50000])
        result = await asyncio.to_thread(_b_answer, prompt, [], "@keeper")
        reply = result.get("reply", "") if isinstance(result, dict) else str(result)
        eid = record("investigation", {"expected": expected, "reply": reply,
                                        "event_count": len(snapshot.get("events") or [])},
                      source="provenance-investigator", actor="local-agent",
                      operation="compare-expected-observed")
        return web.json_response({"ok": True, "event_id": eid, "reply": reply})
    except Exception as e:
        _alog.exception("provenance investigation failed")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


def audit(request, action, detail="", ok=True):
    _alog.log(logging.INFO if ok else logging.WARNING, "%s %s", action, detail)
    try:
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "ip": client_ip(request),
                "user": (session_claims(request) or {}).get(
                    "u", "token" if request.headers.get("X-Console-Token") else "?"),
                "action": action, "detail": detail, "ok": bool(ok),
            }) + "\n")
    except OSError:
        pass


def login_blocked(ip):
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
    _login_fails[ip] = fails
    return len(fails) >= LOGIN_MAX_FAILS


def login_failed(ip):
    _login_fails.setdefault(ip, []).append(time.time())


def require_provision(request):
    """Raise the right HTTP error unless this request may provision from homebase
    with a valid CSRF token. Call at the top of create/delete/clone handlers."""
    if not is_homebase(request):
        audit(request, "provision-denied", "not homebase", ok=False)
        raise web.HTTPForbidden(reason="provisioning is homebase/VPN-only")
    if not can_provision(request):
        raise web.HTTPForbidden(reason="missing provision capability")
    if not csrf_ok(request):
        raise web.HTTPForbidden(reason="bad or missing CSRF token")


_AC_FRAME_STATION_APIS = ("/api/b/",)
_AC_FRAME_REF_RE = re.compile(r"^https?://[^/]+/ac/@([A-Za-z0-9_.-]+)/")


@web.middleware
async def locus_vm_mw(request, handler):
    """1.0.141: ONE spelling of the host on the wire inside the backend. A ?vm=
    naming the host — @self, host, or this station's OWN locus name in any
    documented spelling — reaches every handler as the historic "@keeper"
    sentinel, so no handler needs to know the station's name. Any other value
    (including the bare `keeper` on a station that is not vm_mgr's) is a
    remote locus and passes through untouched."""
    v = request.query.get("vm")
    if v is None and request.path.startswith(_AC_FRAME_STATION_APIS):
        # 1.0.144: a locus console framed at /ac/@<locus>/ calls the STATION's
        # own API at the origin root (keeper-live.js stationApi: B chat, B model,
        # models, voice) without a locus — it used to be answered by the HOST's
        # B. The frame's Referer names its locus; ground the call there.
        m = _AC_FRAME_REF_RE.search(request.headers.get("Referer") or "")
        if m:
            v = m.group(1)
            request = request.clone(rel_url=request.rel_url.update_query(vm=v))
    if v is not None and v.strip() and v.strip() != "@keeper" and _is_host_vm(v.strip()):
        request = request.clone(rel_url=request.rel_url.update_query(vm="@keeper"))
    return await handler(request)


@web.middleware
async def auth_mw(request, handler):
    # login/logout always reachable; everything else needs a valid session/token.
    if request.path in ("/login", "/logout"):
        return await handler(request)
    # token→session exchange: a tokened page link (the harness's "open this
    # VM's console" handoff) becomes a normal session cookie, so the SPA's
    # same-origin API calls work after the first navigation. The token is
    # dropped from the URL on the redirect.
    _tok = request.query.get("token", "")
    if (TOKEN and _tok and hmac.compare_digest(_tok, TOKEN)
            and request.method == "GET"
            and "text/html" in request.headers.get("Accept", "")):
        sess = make_session("token")
        resp = web.HTTPFound(request.path)
        resp.set_cookie(COOKIE, sess, httponly=True,
                        samesite="Strict", max_age=SESSION_TTL, secure=SECURE_COOKIES)
        resp.set_cookie("sc_csrf", csrf_token_for(sess), httponly=False,
                        samesite="Strict", max_age=SESSION_TTL, secure=SECURE_COOKIES)
        audit(request, "login", "token-exchange", ok=True)
        return resp
    if not is_authed(request):
        accepts_html = "text/html" in request.headers.get("Accept", "")
        if request.method == "GET" and accepts_html:
            raise web.HTTPFound("/login")
        return web.json_response({"error": "unauthorized"}, status=401)
    # Open mode (no password/token — startup already refuses this off loopback):
    # mint the session on the first page load, exactly like the token exchange
    # above. Without it there is no session cookie, so request_caps() is empty
    # and csrf_ok() can never pass — every provisioning call 403'd even though
    # the console was deliberately open. The sc_csrf companion keeps the
    # double-submit check meaningful: a cross-origin page can neither read the
    # cookie nor attach the X-Console-CSRF header without a (refused) preflight.
    _open_sess = None
    if (not AUTH_REQUIRED and request.method == "GET"
            and "text/html" in request.headers.get("Accept", "")
            and not valid_session(request.cookies.get(COOKIE, ""))):
        _open_sess = make_session("open")
    resp = await handler(request)
    if _open_sess is not None:
        resp.set_cookie(COOKIE, _open_sess, httponly=True,
                        samesite="Strict", max_age=SESSION_TTL, secure=SECURE_COOKIES)
        resp.set_cookie("sc_csrf", csrf_token_for(_open_sess), httponly=False,
                        samesite="Strict", max_age=SESSION_TTL, secure=SECURE_COOKIES)
    # Never let the browser cache the UI assets. Stale, cached app.js/style.css was
    # the real root of the recurring "copy stopped working again" reports — the file
    # on disk was correct but the browser kept running an old copy. no-store forces a
    # fresh fetch on every load so a deployed fix is always what actually runs.
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
    return resp


# --------------------------------------------------------------------------- #
# station discovery
# --------------------------------------------------------------------------- #
# 1.0.139: tmux is a Recommends, not a Depends. Without it every tmux probe
# (seat liveness, nudges, pane reads — many on background ticks) used to raise
# FileNotFoundError out of create_subprocess_exec and log a traceback. A missing
# tmux is now an ordinary failed command (rc 127 + this message): callers already
# handle rc != 0, /api/term/backends reports it, and the UI disables the tmux seat.
TMUX_MISSING_MSG = "tmux is not installed on this host — the terminal (tmux) seat is unavailable (install it: sudo apt install tmux)"
_TMUX_CACHE = {"ts": 0.0, "ok": False}


def _tmux_available():
    """True when a `tmux` binary is on PATH. Re-probed every 30 s, so installing
    tmux while the station runs is picked up without a restart."""
    now = time.monotonic()
    if not _TMUX_CACHE["ts"] or now - _TMUX_CACHE["ts"] > 30:
        _TMUX_CACHE.update(ts=now, ok=bool(shutil.which("tmux")))
    return _TMUX_CACHE["ok"]


def _tmux_missing(args):
    return bool(args) and args[0] == "tmux" and not _tmux_available()


async def _run(*args):
    if _tmux_missing(args):
        return 127, "", TMUX_MISSING_MSG
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


# --------------------------------------------------------------------------- #
# file-browser helpers — run inside the station as the same unprivileged dev
# user the terminal uses (uid/gid 1000), so the browser grants no new privilege
# beyond what the shell already gives.
# --------------------------------------------------------------------------- #
FS_READ_LIMIT = 1 << 20          # 1 MiB cap for inline view/edit
# Whole-request body cap for uploads (aiohttp default is 1 MiB, far too small
# for folder uploads). Generous default; override with STATION_CONSOLE_MAX_UPLOAD.
MAX_UPLOAD_SIZE = int(os.environ.get("STATION_CONSOLE_MAX_UPLOAD", str(8 * 1024**3)))

# Per-station owner for files/dirs CREATED via the file browser (upload, new
# file, new folder). Defaults to the dev user (SH_UID/SH_GID). Some VMs have a
# dedicated workspace owner (e.g. a shared workspace uid:gid like 1001:1001) so
# operator-created files land owned correctly for that VM — configure those per
# VM via STATION_CONSOLE_FS_OWNERS="name=uid:gid,name2=uid:gid". Empty default:
# no VM is special-cased until the operator sets it.
FS_OWNERS = {}
for _spec in os.environ.get("STATION_CONSOLE_FS_OWNERS", "").split(","):
    _spec = _spec.strip()
    if "=" in _spec and ":" in _spec.split("=", 1)[1]:
        _n, _ug = _spec.split("=", 1)
        _u, _g = _ug.split(":", 1)
        FS_OWNERS[_n.strip()] = (_u.strip(), _g.strip())


def _fs_owner(name):
    """uid, gid that files/dirs created in `name` should be owned by."""
    return FS_OWNERS.get(name, (SH_UID, SH_GID))


# Per-station identity for INTERACTIVE exec (terminal + keeper): (uid, gid).
# Defaults to the dev user (SH_UID/SH_GID). For VMs whose work lives under a
# shared group, run with that group as the PRIMARY gid so the shell/keeper can
# read and write the group's files — e.g. keep uid=ubuntu (the claude install +
# HOME live there) and change only the gid to a shared workspace group. This must
# be the primary group: `lxc exec --group` does NOT load supplementary groups, so
# VM-side membership (usermod -aG) alone has no effect on a console-launched exec.
# Configure per VM via STATION_CONSOLE_EXEC_IDENTS="name=uid:gid,name2=uid:gid".
# Empty default: no VM is special-cased until the operator sets it.
EXEC_IDENTS = {}
for _spec in os.environ.get("STATION_CONSOLE_EXEC_IDENTS", "").split(","):
    _spec = _spec.strip()
    if "=" in _spec and ":" in _spec.split("=", 1)[1]:
        _n, _ug = _spec.split("=", 1)
        _u, _g = _ug.split(":", 1)
        EXEC_IDENTS[_n.strip()] = (_u.strip(), _g.strip())


def _exec_ident(name):
    """uid, gid the interactive terminal/keeper for `name` should run as."""
    return EXEC_IDENTS.get(name, (SH_UID, SH_GID))


async def _lxc(name, *cmd):
    """Run a command inside the station as the dev user. Returns (rc, out, err)."""
    return await _run("lxc", "exec", name,
                      "--user", SH_UID, "--group", SH_GID, "--", *cmd)


def _safe_path(p):
    """Normalize to an absolute container path; '' -> the dev home."""
    p = (p or "").strip() or SH_HOME
    if not p.startswith("/"):
        p = SH_HOME.rstrip("/") + "/" + p
    return os.path.normpath(p)


def _safe_relname(rel):
    """Sanitize an uploaded (possibly nested) filename into a safe relative path.
    Drops drive/leading slashes and any '.'/'..' components, so a folder upload
    can recreate its tree but can never escape the destination dir. '' if empty."""
    rel = (rel or "").replace("\\", "/")
    parts = [p for p in rel.split("/") if p and p not in (".", "..")]
    return "/".join(parts)


def _cpath(name, path):
    """`lxc file` operand for an absolute container path: 'station/abs/path'."""
    return f"{name}{path}"          # path already starts with '/'


def _lan_ip(inst):
    net = (inst.get("state") or {}).get("network") or {}
    for iface, info in net.items():
        if iface == "lo" or iface.startswith(("docker", "br-", "veth")):
            continue
        for addr in info.get("addresses", []):
            ip = addr.get("address", "")
            if addr.get("family") == "inet" and ip.startswith("192.168."):
                return ip
    return ""


def _inet_ips(inst):
    """Every IPv4 address an LXD guest holds (not loopback / docker / veth) —
    _central_locus() matches these against registered locus endpoints."""
    net = (inst.get("state") or {}).get("network") or {}
    out = []
    for iface, info in net.items():
        if iface == "lo" or iface.startswith(("docker", "br-", "veth")):
            continue
        for addr in info.get("addresses", []):
            if addr.get("family") == "inet" and addr.get("address"):
                out.append(addr["address"])
    return out


def _project(inst):
    dev = (inst.get("devices") or {}).get("project") or {}
    src = dev.get("source", "")
    return os.path.basename(src) if src else ""


def _bridge_open(name):
    return (BRIDGE_STATE / f"{name}.gate").exists()


_DISCOVER_CACHE = {"ts": 0.0, "stations": None, "err": None}
_DISCOVER_TTL = 2.0  # collapse rapid known_names()/discover() calls into one `lxc list`


async def discover():
    # Workbench mode (the console installed INSIDE a station, no LXD of its
    # own): no stations is a normal answer, not a crash — the terminal
    # surfaces, board, feed, and B chat are the point of such an install.
    # 2026-09-18: short TTL cache so per-request known_names() callers do not
    # each shell out to `lxc list` (was spawning dozens of snap.lxd transient
    # scopes per minute, ×3 station users).
    _now = time.monotonic()
    if _now - _DISCOVER_CACHE["ts"] < _DISCOVER_TTL and (
            _DISCOVER_CACHE["stations"] is not None or _DISCOVER_CACHE["err"] is not None):
        if _DISCOVER_CACHE["err"] is not None:
            raise RuntimeError(_DISCOVER_CACHE["err"])
        return _DISCOVER_CACHE["stations"]
    try:
        rc, out, err = await _run("lxc", "list", "--format", "json")
    except OSError:
        _DISCOVER_CACHE.update(ts=_now, stations=[], err=None)
        return []
    if rc != 0:
        _DISCOVER_CACHE.update(ts=_now, stations=None, err=f"lxc list failed: {err.strip()}")
        raise RuntimeError(_DISCOVER_CACHE["err"])
    stations = []
    for inst in json.loads(out):
        name = inst["name"]
        is_base = name == BASE_NAME
        if is_base and not SHOW_BASE:
            continue
        stations.append({
            "name": name,
            "status": inst.get("status", "Unknown"),
            "ip": _lan_ip(inst),
            "ips": _inet_ips(inst),     # 1.0.138: every inet addr (locus endpoint match)
            "project": _project(inst) or ("(base image)" if is_base else ""),
            "base": is_base,
            "bridge": _bridge_open(name),
        })
    stations.sort(key=lambda s: (s["base"], s["name"]))
    _DISCOVER_CACHE.update(ts=_now, stations=stations, err=None)
    return stations


async def known_names():
    """Addressable station names: LXD guests (when lxc works here) plus the
    ssh/toolserver loci. 2026-09-03: on ae the vm_mgr user cannot run the snap
    lxc CLI ("home directories outside of /home needs configuration"), which
    made every caller a raw 500 — lxc failure now degrades to the loci list."""
    try:
        names = {s["name"] for s in await discover()}
    except RuntimeError as e:
        logging.getLogger("station.fleet").warning("known_names: %s", e)
        names = set()
    return names | set(_ssh_hosts())


# --------------------------------------------------------------------------- #
# auth routes
# --------------------------------------------------------------------------- #
async def login_get(request):
    return web.FileResponse(STATIC / "login.html")


async def login_post(request):
    ip = client_ip(request)
    if login_blocked(ip):
        audit(request, "login", "rate-limited", ok=False)
        raise web.HTTPFound("/login?e=locked")
    data = await request.post()
    user = data.get("user", "")
    pw = data.get("password", "")
    if AUTH_REQUIRED and user == USERNAME and verify_password(pw, PASSWORD_HASH):
        sess = make_session(user)
        resp = web.HTTPFound("/")
        resp.set_cookie(COOKIE, sess, httponly=True,
                        samesite="Strict", max_age=SESSION_TTL, secure=SECURE_COOKIES)
        # readable companion for double-submit CSRF (intentionally not httponly)
        resp.set_cookie("sc_csrf", csrf_token_for(sess), httponly=False,
                        samesite="Strict", max_age=SESSION_TTL, secure=SECURE_COOKIES)
        audit(request, "login", user, ok=True)
        return resp
    login_failed(ip)
    audit(request, "login", f"user={user}", ok=False)
    raise web.HTTPFound("/login?e=1")


async def logout(request):
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE)
    resp.del_cookie("sc_csrf")
    return resp


# --------------------------------------------------------------------------- #
# REST: status + power/snapshot actions
# --------------------------------------------------------------------------- #
async def api_console_url(request):
    """GET /api/stations/{name}/console-url — the harness→workbench handoff.

    Each station may run its OWN hugpy-station (workbench mode) inside the VM;
    this harness is the switcher between them. Returns a one-click tokened URL
    (the workbench exchanges it for a session cookie). The token lives in
    $VMCONSOLE_TOKEN_DIR/vmconsole-<name>.token, written by install-vm-console."""
    return _retired("workbench console-url", request.match_info["name"])
    name = request.match_info["name"]
    h = _ssh_host(name)
    if name not in await known_names() and not h:
        return web.json_response({"error": f"unknown locus {name}"}, status=404)
    tokf = _vmconsole_token_dir() / f"vmconsole-{name}.token"
    try:
        token = tokf.read_text().strip()
    except OSError:
        token = ""
    if h:                      # ssh locus: the workbench serves on its address
        ip = h["host"]
    else:
        rc, out, _err = await _run("lxc", "list", name, "-c", "4", "--format", "csv")
        # csv quotes the cell (it can hold several "addr (iface)" lines) — strip.
        ip = (out.splitlines() or [""])[0].strip('"').split(" ")[0].strip() if rc == 0 else ""
    if not token or not ip:
        return web.json_response(
            {"ok": False, "error": "no workbench console on this locus "
             "(install: install-vm-console <vm> / install-ssh-console <host>)"}, status=404)
    port = os.environ.get("VMCONSOLE_PORT", "8800")
    return web.json_response({"ok": True,
                              "url": f"http://{ip}:{port}/?token={token}"})


async def api_stations(request):
    return web.json_response({"stations": await discover(),
                              "auth": AUTH_REQUIRED, "user": USERNAME,
                              "can_provision": can_provision(request),
                              "homebase": is_homebase(request)})


ACTIONS = {
    "start":    lambda n: ("lxc", "start", n),
    "stop":     lambda n: ("lxc", "stop", n),
    "restart":  lambda n: ("lxc", "restart", n),
    "snapshot": lambda n: ("lxc", "snapshot", n),
}


async def api_action(request):
    name = request.match_info["name"]
    action = request.match_info["action"]
    if name not in await known_names():
        return web.json_response({"error": f"unknown station {name}"}, status=404)
    if action not in ACTIONS:
        return web.json_response({"error": f"unknown action {action}"}, status=400)
    env = {**os.environ, "VM_NAME": name} if action == "snapshot" else None
    proc = await asyncio.create_subprocess_exec(
        *ACTIONS[action](name), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return web.json_response({
        "ok": proc.returncode == 0, "action": action, "name": name,
        "output": out.decode(errors="replace").strip(),
    })


async def api_bridge(request):
    """Open/close this station's vm-bridge gate (POST {"on": bool}). Toggles the
    same <name>.gate file the broker/CLI use, so the switch and `vm-bridge
    on/off` are one and the same."""
    name = request.match_info["name"]
    if name not in await known_names():
        return web.json_response({"error": f"unknown station {name}"}, status=404)
    on = bool((await request.json()).get("on"))
    gate = BRIDGE_STATE / f"{name}.gate"
    try:
        BRIDGE_STATE.mkdir(parents=True, exist_ok=True)
        if on:
            gate.touch(exist_ok=True)
        else:
            gate.unlink(missing_ok=True)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True, "name": name, "bridge": gate.exists()})


# --------------------------------------------------------------------------- #
# REST: provisioning — create (homebase/VPN-only, via the canonical vm-new).
# Args are built as an argv LIST (never a shell string) and every field is
# validated here; vm-new re-validates as defense-in-depth.
# --------------------------------------------------------------------------- #
JOBS = {}   # job_id -> {status, rc, lines, name, started}
_VALID_PROFILE = {"default", "isolated"}
_VALID_NET = {"nat", "lan"}
_VALID_CLAUDE = {"per-vm", "shared"}
_NAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_SIZE_RE = re.compile(r"[0-9]{1,4}(MiB|GiB)$")


# --------------------------------------------------------------------------- #
# keeper (overseer) read-only surface — fleet feed, conversation logs, backups,
# control audit. All GET, behind auth_mw; no mutation, so no CSRF/provision gate.
# The console reads the host-side stores; it never grants the keeper VM new reach.
# --------------------------------------------------------------------------- #
def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


_KEEPER_VM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


async def api_keeper_fleet(request):
    """The oversight feed (fleet.json). ?refresh=1 regenerates it first."""
    if request.query.get("refresh") == "1":
        await _run(str(VM_BIN / "keeper-collect"), "--quiet")
    feed = _read_json(KEEPER_FEED, None)
    if feed is None:
        return web.json_response({"error": "no feed yet (keeper-collect)"}, status=503)
    return web.json_response(feed)


async def api_keeper_sessions(request):
    """Conversation-log session registry, most-recently-active first."""
    sess = _read_json(KEEPER_SESS_FILE, {})
    rows = sorted(sess.values(), key=lambda r: r.get("last") or "", reverse=True)
    vm = request.query.get("vm")
    if vm:
        rows = [r for r in rows if r.get("vm") == vm]
    return web.json_response({"sessions": rows[:int(request.query.get("limit", 200))],
                              "total": len(sess)})


async def _keeper_logs(*args):
    rc, out, err = await _run(str(VM_BIN / "keeper-logs"), *args)
    return web.json_response({"ok": rc == 0, "text": out or err})


async def api_keeper_log_search(request):
    q = request.query.get("q", "").strip()
    if not q:
        return web.json_response({"error": "q required"}, status=400)
    args = ["search", q, "--limit", str(min(int(request.query.get("limit", 80)), 300))]
    vm = request.query.get("vm")
    if vm and _KEEPER_VM_RE.match(vm):
        args += ["--vm", vm]
    if request.query.get("kind"):
        args += ["--kind", request.query["kind"]]
    return await _keeper_logs(*args)


async def api_keeper_log_show(request):
    s = request.query.get("session", "").strip()
    if not re.match(r"^[A-Za-z0-9-]{1,40}$", s):
        return web.json_response({"error": "bad session id"}, status=400)
    return await _keeper_logs("show", s)


async def api_keeper_backups(request):
    # 1.0.85: keeper-backup is a vm_mgr-only tool — hugpy Station does not ship
    # it (VM_BIN/keeper-backup absent → FileNotFoundError → HTTP 500 before).
    exe = VM_BIN / "keeper-backup"
    if not exe.is_file():
        return web.json_response({"ok": False, "text": "keeper-backup is not shipped with hugpy Station (vm_mgr-only tool)"})
    try:
        rc, out, err = await _run(str(exe), "status")
    except OSError as e:
        return web.json_response({"ok": False, "text": f"keeper-backup could not run: {e}"})
    return web.json_response({"ok": rc == 0, "text": out or err})


async def api_keeper_audit(request):
    """Recent keeper-broker control-audit events (newest last)."""
    n = min(int(request.query.get("n", 60)), 500)
    lines, rows = [], []
    if KEEPER_AUDIT.exists():
        lines = KEEPER_AUDIT.read_text().splitlines()[-n:]
    for ln in lines:
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return web.json_response({"events": rows})


async def api_keeper_directives(request):
    """Directive version history grouped by name + current versions."""
    cur = _read_json(KEEPER_DIRV_CUR, {})
    by_name = {}
    if KEEPER_DIRV_LOG.exists():
        for ln in KEEPER_DIRV_LOG.read_text().splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            by_name.setdefault(r["name"], []).append(r)
    return web.json_response({"current": cur, "directives": by_name, "scope": "this station"})


async def _keeper_directive(*args):
    rc, out, err = await _run(str(VM_BIN / "keeper-directive"), *args)
    return web.json_response({"ok": rc == 0, "text": out or err})


async def api_keeper_dir_show(request):
    name = request.query.get("name", "")
    if not _KEEPER_VM_RE.match(name):
        return web.json_response({"error": "bad name"}, status=400)
    args = ["show", name]
    if request.query.get("seq", "").isdigit():
        args.append(request.query["seq"])
    return await _keeper_directive(*args)


async def api_keeper_dir_diff(request):
    name = request.query.get("name", "")
    if not _KEEPER_VM_RE.match(name):
        return web.json_response({"error": "bad name"}, status=400)
    args = ["diff", name]
    for k in ("a", "b"):
        if request.query.get(k, "").isdigit():
            args.append(request.query[k])
    return await _keeper_directive(*args)


async def api_keeper_dir_resolve(request):
    vm = request.query.get("vm", "")
    if not _KEEPER_VM_RE.match(vm):
        return web.json_response({"error": "bad vm"}, status=400)
    return await _keeper_directive("resolve", vm)


def _vm_new_argv(body):
    """Validated create-request -> (name, vm-new argv list). Raises ValueError."""
    name = str(body.get("name", "")).strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError("invalid name")
    if name in ("sandbox", "default", "golden", "none", "keeper"):
        raise ValueError("reserved name")   # keeper = the local machine's seat
    argv = [str(VM_BIN / "vm-new"), name, "--yes"]
    src = body.get("source", "golden") or "golden"
    if src != "golden":
        if not src.startswith("clone:"):
            raise ValueError("source must be 'golden' or 'clone:<instance>'")
        inst = src.split(":", 1)[1]
        if not _NAME_RE.fullmatch(inst):
            raise ValueError("bad source instance")
        argv += ["--from", inst]
    prof = body.get("profile", "default")
    if prof not in _VALID_PROFILE:
        raise ValueError("bad profile")
    net = body.get("net", "nat")
    if net not in _VALID_NET:
        raise ValueError("bad net")
    argv += ["--profile", prof, "--net", net]
    res = body.get("resources") or {}
    if "cpu" in res:
        cpu = int(res["cpu"])
        if not 1 <= cpu <= 32:
            raise ValueError("cpu out of range (1-32)")
        argv += ["--cpu", str(cpu)]
    for k in ("mem", "disk"):
        if k in res:
            if not _SIZE_RE.fullmatch(str(res[k])):
                raise ValueError(f"bad {k} (e.g. 8GiB)")
            argv += [f"--{k}", str(res[k])]
    opts = body.get("options") or {}
    claude = opts.get("claude", "per-vm")
    if claude not in _VALID_CLAUDE:
        raise ValueError("bad claude")
    argv += ["--claude", claude]
    if opts.get("llm_station") is False:
        argv += ["--no-llm-station"]
    if opts.get("snapshot"):
        argv += ["--snapshot"]
    proj = body.get("project")
    if proj:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(proj)):
            raise ValueError("bad project")
        argv += ["--project", str(proj)]
    return name, argv


async def _post_create(job, name):
    """Citizenship hook: a freshly created station becomes a FULL citizen —
    its own workbench console, comms watcher, the lot — before the job is
    reported done. The mechanism is packaged; the steps are the site's:
    an executable `post-create` next to this file, run as `post-create <vm>`
    (vm_mgr ships one calling install-vm-console + fleet-comms-install).
    Hook failure downgrades to a warning: the VM exists and works, it is
    just not wired — the lines say exactly what to run by hand."""
    hook = ROOT / "post-create"
    if not (hook.is_file() and os.access(hook, os.X_OK)):
        return
    job["lines"].append(f"[post-create] {hook} {name}")
    try:
        proc = await asyncio.create_subprocess_exec(
            str(hook), name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        async for raw in proc.stdout:
            job["lines"].append("[post-create] "
                                + raw.decode(errors="replace").rstrip("\n"))
        rc = await proc.wait()
        if rc != 0:
            job["lines"].append(f"[post-create] WARNING: exited {rc} — the VM "
                                f"is up but not fully wired; run manually: "
                                f"{hook} {name}")
    except Exception as e:                               # noqa: BLE001
        job["lines"].append(f"[post-create] WARNING: {e}")


async def _run_build(job_id, argv):
    job = JOBS[job_id]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        async for raw in proc.stdout:
            job["lines"].append(raw.decode(errors="replace").rstrip("\n"))
        job["rc"] = await proc.wait()
        if job["rc"] == 0:
            await _post_create(job, job.get("name", ""))
        job["status"] = "done" if job["rc"] == 0 else "failed"
    except Exception as e:                               # noqa: BLE001 (report any failure)
        job["lines"].append(f"[error] {e}")
        job["status"], job["rc"] = "failed", -1


async def api_create(request):
    require_provision(request)                           # homebase + capability + CSRF
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    try:
        name, argv = _vm_new_argv(body)
    except (ValueError, KeyError, TypeError) as e:
        audit(request, "create", f"rejected: {e}", ok=False)
        return web.json_response({"error": f"invalid request: {e}"}, status=400)
    if name in await known_names():
        return web.json_response({"error": f"'{name}' already exists"}, status=409)
    if any(j["status"] == "running" for j in JOBS.values()):
        return web.json_response({"error": "another build is in progress"}, status=409)
    job_id = secrets.token_hex(8)
    JOBS[job_id] = {"status": "running", "rc": None, "lines": [],
                    "name": name, "started": time.time()}
    audit(request, "create", f"name={name} args={argv[2:]}", ok=True)
    asyncio.create_task(_run_build(job_id, argv))
    return web.json_response({"ok": True, "job_id": job_id, "name": name})


async def api_job(request):
    job = JOBS.get(request.match_info["id"])
    if not job:
        return web.json_response({"error": "unknown job"}, status=404)
    return web.json_response({"status": job["status"], "rc": job["rc"],
                              "name": job["name"], "lines": job["lines"]})


# --------------------------------------------------------------------------- #
# REST: in-house LLM chat (per-station panel). A small provider registry so N
# backends are reached the same way; the chat endpoint is a same-origin SSE
# proxy (the browser can't reach the gateways directly, and this keeps the LLM
# calls behind the console's auth). To add a backend, add an entry here and,
# if it isn't hugpy-shaped, a branch in _provider_models / _provider_chat.
# --------------------------------------------------------------------------- #
# Each provider speaks the OpenAI HTTP API (/v1/models, /v1/chat/completions w/
# SSE). Any OpenAI-compatible gateway is one entry here — no per-backend code.
LLM_PROVIDERS = {
    "hugpy": {
        "label": "hugpy",
        "type": "openai",
        "base": os.environ.get("HUGPY_URL", ""),
        "api_key": os.environ.get("HUGPY_API_KEY", ""),
    },
}
# hugpy's keeper REPL, run from the module at its source on the share (it is
# stdlib-only by design, so any python3 runs it without installing hugpy).
# It is now a module inside the abstract_hugpy_dev package (package-relative
# import on central.py), so it's launched via keeper_launcher.py, which loads
# it surgically without importing the package's heavy __init__.py.
KEEPER_SRC = os.environ.get(
    "HUGPY_KEEPER",
    str(ROOT.parent / "share" / "projects" / "hugpy" / "dev" / "abstract_hugpy_dev"
        / "src" / "abstract_hugpy_dev" / "keeper.py"))
KEEPER_LAUNCHER = str(ROOT / "keeper_launcher.py")
CHAT_LLM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=3600)


def _provider_headers(prov):
    return {"Authorization": f"Bearer {prov['api_key']}"} if prov.get("api_key") else {}


async def _provider_models(pid, prov):
    if prov["type"] != "openai":
        return []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(prov["base"] + "/v1/models", headers=_provider_headers(prov)) as r:
            data = (await r.json()).get("data", [])
    out = []
    for m in data:                          # /v1/models already lists only ready models
        mid = m.get("id", "")
        if not mid:
            continue
        out.append({"provider": pid, "key": mid, "label": mid,
                    "status": "installed", "task": m.get("task", ""), "ready": True})
    out.sort(key=lambda x: x["label"].lower())
    return out


def _provider_chat_target(prov, body):
    """Return (url, json_payload, headers) for a chat request to this provider."""
    if prov["type"] == "openai":
        return (prov["base"] + "/v1/chat/completions",
                {"model": body.get("model", ""),
                 "messages": body.get("messages", []),
                 "max_tokens": int(body.get("max_new_tokens", 1024)),
                 "stream": True},
                _provider_headers(prov))
    raise ValueError(f"unsupported provider type {prov['type']}")


async def api_llm_models(request):
    models = []
    for pid, prov in LLM_PROVIDERS.items():
        try:
            models.extend(await _provider_models(pid, prov))
        except Exception as e:                       # a down backend shouldn't 500 the picker
            models.append({"provider": pid, "key": "", "label": f"({prov['label']} unavailable: {e})",
                           "status": "error", "task": "", "ready": False})
    return web.json_response({"models": models})


async def api_voice_transcribe(request):
    """Same-origin Whisper proxy for the Station /ac voice control.

    The browser posts the MediaRecorder multipart body here; the station
    forwards it to the local Hugpy gateway with the station's server-side
    credential, so no fleet token is exposed to the page.
    """
    body = await request.read()
    if not body or len(body) > 25 * 1024 * 1024:
        return web.json_response({"error": "audio is empty or exceeds 25 MB"}, status=413)
    ctype = request.headers.get("Content-Type", "")
    if not ctype.startswith("multipart/form-data;"):
        return web.json_response({"error": "expected multipart audio upload"}, status=415)
    base = (os.environ.get("HUGPY_URL") or _LOCAL_HUGPY_FRONT).rstrip("/")
    token = (os.environ.get("HUGPY_API_KEY") or
             os.environ.get("HUGPY_OPERATOR_TOKEN") or "").strip()
    headers = {"Content-Type": ctype}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
            async with s.post(base + "/api/ml/transcribe", data=body, headers=headers) as r:
                payload = await r.read()
                out_type = r.headers.get("Content-Type", "application/json")
                return web.Response(status=r.status, body=payload,
                                    headers={"Content-Type": out_type})
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return web.json_response({"error": "transcription gateway unavailable: " + str(e)}, status=502)


# Taught to the model when the chat's Design drawer is in play (body.design).
# The fenced ```wireframe blocks it describes are rendered by the UI as live
# layout thumbnails and round-trip into the drawer's editor (wireframe.js);
# the schema matches abstract_ide's wireframeTab so files interchange.
WIREFRAME_PROMPT = (
    "The user exchanges UI wireframes with you as fenced code blocks. When you "
    "propose or revise a UI layout, output one block in EXACTLY this form:\n"
    "```wireframe\n"
    '{"schema":"wireframe.v1","canvas":{"w":1280,"h":800},"shapes":[\n'
    '{"role":"nav","label":"top nav","x":0,"y":0,"w":1280,"h":80},\n'
    '{"role":"button","label":"save","x":1140,"y":20,"w":120,"h":40}]}\n'
    "```\n"
    "Rules: coordinates are pixels on the fixed 1280x800 canvas; role is one of "
    "container|nav|sidebar|button|input|text|image|list|note; label is a short "
    "name in the user's words; keep x,y,w,h multiples of 20; list big background "
    "regions first so smaller elements sit on top of them. A wireframe block in "
    "the user's message is their CURRENT design — read it and refer to its "
    "shapes by label. Keep prose outside the block brief."
)


def _with_design_prompt(body):
    """If the client flagged design mode, prepend the wireframe system prompt
    (unless the conversation already carries a system message)."""
    if body.get("design"):
        msgs = body.get("messages") or []
        if not any(m.get("role") == "system" for m in msgs):
            body["messages"] = [{"role": "system", "content": WIREFRAME_PROMPT}] + msgs
    return body


async def api_llm_chat(request):
    """SSE proxy: stream a chat completion from the chosen provider to the browser.
    Body: {provider, model, messages:[{role,content}], max_new_tokens?, station?,
    design?} — design=true teaches the model the ```wireframe block format."""
    body = await request.json()
    prov = LLM_PROVIDERS.get(body.get("provider", "hugpy"))
    if not prov:
        return web.json_response({"error": "unknown provider"}, status=400)
    url, payload, up_headers = _provider_chat_target(prov, _with_design_prompt(body))

    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",          # don't let nginx buffer the stream
    })
    await resp.prepare(request)
    try:
        async with aiohttp.ClientSession(timeout=CHAT_LLM_TIMEOUT) as s:
            async with s.post(url, json=payload, headers=up_headers) as up:
                if up.status != 200:
                    detail = (await up.text())[:500]
                    await resp.write(f'data: {{"type":"error","message":{json.dumps(detail)}}}\n\n'.encode())
                else:
                    async for chunk in up.content.iter_any():   # pass SSE through verbatim
                        await resp.write(chunk)
    except Exception as e:
        await resp.write(f'data: {{"type":"error","message":{json.dumps(str(e))}}}\n\n'.encode())
    await resp.write_eof()
    return resp


# --------------------------------------------------------------------------- #
# REST: station-aware LLM agent. Same models as the chat panel, but the model
# may act in the station: it emits a fenced `action` block, the console runs it
# INSIDE the station via `lxc exec` as the unprivileged dev user (uid 1000 — the
# same surface the terminal/file-browser already expose), feeds the output back
# as an Observation, and loops. Provider-agnostic (no native tool-calling
# needed), so it works the same across every in-house model.
# --------------------------------------------------------------------------- #
AGENT_MAX_STEPS = int(os.environ.get("LLM_AGENT_MAX_STEPS", "8"))
AGENT_OBS_LIMIT = int(os.environ.get("LLM_AGENT_OBS_LIMIT", "6000"))
_ACTION_RE = re.compile(r"```(?:action|bash|sh|shell)[^\n]*\n(.*?)```", re.DOTALL)


def _agent_system_prompt(station):
    return (
        f"You are an assistant working INSIDE the LXD station '{station}'. You have a "
        f"shell there as the unprivileged dev user (home {SH_HOME}; the project is under "
        f"/srv/share/projects). You can read the filesystem and run commands to satisfy "
        f"the user's request.\n\n"
        f"To run a command, output EXACTLY one fenced block and then stop:\n"
        f"```action\n<one shell command>\n```\n"
        f"You will receive its output as an 'Observation:' and may then run more commands. "
        f"When you have the final answer, reply normally WITHOUT an action block. Keep "
        f"commands non-interactive."
    )


def _parse_action(text):
    matches = _ACTION_RE.findall(text)
    if matches:
        return matches[-1].strip()
    for line in text.splitlines():               # fallback: "ACTION: <cmd>"
        if line.strip().lower().startswith("action:"):
            return line.split(":", 1)[1].strip()
    return None


async def _exec_in_station(name, command, parent_event_id=None):
    started = time.time()
    try:
        from provenance import record
        tool_event = record("tool_call", {"command": command, "station": name},
                            source="station-agent", actor="agent", session_id=name,
                            operation="execute", parent_id=parent_event_id)
    except Exception:
        tool_event = None
    rc, out, err = await _lxc(name, "bash", "-lc", command)
    output = (out or "")
    if err and err.strip():
        output += "\n[stderr]\n" + err
    output = output.strip()
    if len(output) > AGENT_OBS_LIMIT:
        output = output[:AGENT_OBS_LIMIT] + f"\n…[truncated, {len(output)} chars total]"
    result = output or "(no output)"
    try:
        from provenance import record
        record("tool_result", {"command": command, "station": name, "rc": rc,
                                "duration_ms": round((time.time() - started) * 1000),
                                "output": result}, source="station-agent", actor="runtime",
               session_id=name, operation="execute-result", parent_id=tool_event)
        # Link the returned result to the exact call that produced it.
        if tool_event:
            record("tool_result_link", {"tool_event": tool_event, "rc": rc},
                   source="station-agent", actor="runtime", session_id=name,
                   operation="returns", parent_id=tool_event)
    except Exception:
        pass
    return rc, result, tool_event


async def _sse(resp, obj):
    await resp.write(("data: " + json.dumps(obj) + "\n\n").encode())


async def _agent_stream_step(session, url, payload, headers, resp):
    """One model turn: stream tokens to the browser, return the full text."""
    text = ""
    try:
        from provenance import record
        record("agent_output", {"request": payload, "phase": "start"},
               source="station-agent", actor="model",
               session_id=str(payload.get("model") or "unknown"), operation="model-request")
    except Exception:
        pass
    async with session.post(url, json=payload, headers=headers) as up:
        if up.status != 200:
            await _sse(resp, {"type": "error", "message": (await up.text())[:500]})
            return text
        buf = ""
        async for chunk in up.content.iter_any():
            buf += chunk.decode("utf-8", "replace")
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                dline = next((l for l in block.split("\n") if l.startswith("data:")), None)
                if not dline:
                    continue
                data = dline[5:].strip()
                if data == "[DONE]":
                    continue
                try:
                    p = json.loads(data)
                except ValueError:
                    continue
                piece = (p.get("choices") or [{}])[0].get("delta", {}).get("content")
                if piece:
                    text += piece
                    await _sse(resp, {"type": "token", "text": piece})
    try:
        from provenance import record
        output_event = record("agent_output", {"phase": "complete", "text": text},
               source="station-agent", actor="model",
               session_id=str(payload.get("model") or "unknown"), operation="model-output")
    except Exception:
        pass
    return text, output_event if 'output_event' in locals() else None


async def api_llm_agent(request):
    body = await request.json()
    prov = LLM_PROVIDERS.get(body.get("provider", "hugpy"))
    if not prov:
        return web.json_response({"error": "unknown provider"}, status=400)
    station = body.get("station", "")
    if station not in await known_names():
        return web.json_response({"error": f"unknown station {station}"}, status=400)
    model = body.get("model", "")
    max_tokens = int(body.get("max_new_tokens", 1024))

    convo = [{"role": "system", "content": _agent_system_prompt(station)}]
    if body.get("design"):
        convo[0]["content"] += "\n\n" + WIREFRAME_PROMPT
    convo += [m for m in body.get("messages", []) if m.get("role") in ("user", "assistant")]

    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    url, headers = prov["base"] + "/v1/chat/completions", _provider_headers(prov)
    try:
        async with aiohttp.ClientSession(timeout=CHAT_LLM_TIMEOUT) as s:
            stopped = True
            for _ in range(AGENT_MAX_STEPS):
                payload = {"model": model, "messages": convo, "max_tokens": max_tokens, "stream": True}
                text, output_event = await _agent_stream_step(s, url, payload, headers, resp)
                convo.append({"role": "assistant", "content": text})
                action = _parse_action(text)
                if not action:
                    stopped = False
                    break
                await _sse(resp, {"type": "action", "command": action})
                rc, output, tool_event = await _exec_in_station(station, action, output_event)
                await _sse(resp, {"type": "observation", "rc": rc, "output": output})
                convo.append({"role": "user", "content": f"Observation (exit {rc}):\n{output}"})
                try:
                    from provenance import record
                    record("observation", {"rc": rc, "output": output},
                           source="station-agent", actor="runtime", session_id=station,
                           operation="feeds-model", parent_id=tool_event)
                except Exception:
                    pass
            if stopped:
                await _sse(resp, {"type": "error", "message": f"stopped after {AGENT_MAX_STEPS} action steps"})
        await _sse(resp, {"type": "done"})
    except Exception as e:
        await _sse(resp, {"type": "error", "message": str(e)})
    await resp.write_eof()
    return resp


# --------------------------------------------------------------------------- #
# REST: page fetch for the Design drawer's URL import. The browser can't read
# a cross-origin page's DOM, so the console fetches the HTML and hands it back
# same-origin; the client renders it in a sandboxed iframe (scripts OFF) and
# measures the real layout boxes. Auth-gated like every route.
# --------------------------------------------------------------------------- #
WF_FETCH_MAX = 2_500_000                     # HTML only; cap the body we read


async def api_wf_fetch(request):
    url = (request.query.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return web.json_response({"error": "url must be http(s)"}, status=400)
    try:
        timeout = aiohttp.ClientTimeout(total=25)
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) station-console-wireframe"}
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, headers=headers, max_redirects=5) as r:
                ctype = r.headers.get("Content-Type", "")
                if r.status != 200:
                    return web.json_response({"error": f"HTTP {r.status} from {url}"}, status=502)
                if ctype and "html" not in ctype and "text" not in ctype:
                    return web.json_response({"error": f"not an HTML page ({ctype})"}, status=415)
                raw = await r.content.read(WF_FETCH_MAX)
                html = raw.decode(r.charset or "utf-8", "replace")
                # <base> is injected client-side from `base` so relative CSS/img resolve
                return web.json_response({"html": html, "base": str(r.url)})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)


# --------------------------------------------------------------------------- #
# Keeper: a stationary terminal per station in which an LLM lives as the
# station's keeper. One concept, two backends:
#   - "claude": Claude Code launched INSIDE the station as the dev user (it is
#     installed in-station), handed the keeper charge as its opening prompt.
#   - "<provider>::<model>": hugpy's own keeper REPL (`hugpy keeper`), run
#     straight from the hugpy source on the share (stdlib-only, no install) on
#     the host — the gateway is host-local and macvlan blocks guest->host — and
#     acting in the station via `lxc exec` as the same unprivileged dev user.
# Either way it is one persistent PTY session per station,
# reattached on reconnect and exempt from the detached-session reaper.
# --------------------------------------------------------------------------- #
KEEPER_TMUX_SOCK = "console"   # dedicated tmux socket for persistent keepers

# A (frontier keeper) defaults — the operator's console session defaults
# (2026-08-04), handed to every claude keeper launch via --settings. Session-
# scoped: the in-station user's own settings files are untouched and still
# supply anything omitted here.
A_DEFAULT_SETTINGS = json.dumps({
    "enableWorkflows": False,
    "workflowKeywordTriggerEnabled": False,
    "workflowSizeGuideline": "small",
    "enableArtifact": False,
    "autoCompactEnabled": False,
    "autoScrollEnabled": False,
    "terminalProgressBarEnabled": False,
    "showTurnDuration": False,
    "useAutoModeDuringPlan": True,
    "skipDangerousModePermissionPrompt": True,
    "permissions": {"defaultMode": "auto"},
    "worktree": {"baseRef": "head"},
}, separators=(",", ":"))

# Editable template overriding the built-in defaults above. Writable state
# (template edits, the frontier toggle) lives under the user's config dir —
# /opt is root-owned once installed, so ROOT's shipped copy is the read-only
# default and edits land in FV_STATE_HOME. Read at every launch, so editing
# (directly or via POST /api/a/template) reconfigures the NEXT keeper/terminal
# A launch — no rebuild, no restart.
FV_STATE_HOME = Path(os.environ.get("HUGPY_STATION_STATE") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"),
    "hugpy-station"))

# --- toolserver credential (1.0.63) ------------------------------------------
# The station CARRIES its own toolserver credential: FV_STATE_HOME/toolserver.env
# (KEY=VALUE lines, 0600) — written by the package's after-install from the
# installer's HUGPY_OPERATOR_TOKEN, or later by `hugpy-station-toolserver set` /
# POST /api/toolserver/config. Loaded into the process environment at startup
# (a real env var always wins), so loci-sync / handoff / exchange calls to the
# token-gated toolserver authenticate, AND every seat this station spawns
# inherits HUGPY_OPERATOR_TOKEN (the mct's exchange uploads, the abstract-claude
# MCP bridge). Built into the install; no shell-of-launch dependency.
TOOLSERVER_ENV_PATH = FV_STATE_HOME / "toolserver.env"
_TS_TOKEN_NAMES = ("STATION_CONSOLE_TOOLSERVER_TOKEN", "HUGPY_OPERATOR_TOKEN",
                   "TOOLSERVER_OPERATOR_TOKEN", "TOOLSERVER_TOKEN")
# 1.0.65: the same file may carry the hugpy central base (HUGPY_URL / HUGPY_BASE) for
# the console-api sidecar's model list + B chat — ae: http://127.0.0.1:7002 (default),
# other hosts: https://api.hugpy.ai or the LAN address.
_TS_ENV_KEYS = ("STATION_CONSOLE_TOOLSERVER", "HUGPY_URL", "HUGPY_BASE",
                "STATION_LOCUS", "STATION_IDLE_RELAUNCH_MIN") + _TS_TOKEN_NAMES


def _load_toolserver_env(path=None):
    """Read toolserver.env into os.environ (env wins). Returns the keys loaded."""
    loaded = []
    try:
        lines = Path(path or TOOLSERVER_ENV_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in _TS_ENV_KEYS and v and not os.environ.get(k):
            os.environ[k] = v
            loaded.append(k)
    tok = next((os.environ.get(n) for n in _TS_TOKEN_NAMES if os.environ.get(n)), "")
    if tok:                                   # one token serves every consumer name
        for n in _TS_TOKEN_NAMES:
            os.environ.setdefault(n, tok)
    return loaded


def _write_toolserver_env(token, url=""):
    """Persist {token, url} to toolserver.env (0600) and apply it live."""
    TOOLSERVER_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = "# hugpy-station toolserver credential — written by the station; 0600\n"
    if url:
        body += "STATION_CONSOLE_TOOLSERVER=%s\n" % url
    body += "HUGPY_OPERATOR_TOKEN=%s\n" % token
    tmp = TOOLSERVER_ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOOLSERVER_ENV_PATH)
    for n in _TS_TOKEN_NAMES:
        os.environ[n] = token
    if url:
        os.environ["STATION_CONSOLE_TOOLSERVER"] = url


_TOOLSERVER_ENV_LOADED = _load_toolserver_env()


# --- toolserver ENDPOINT (2026-09-30, abstract-toolserver CENTRALIZATION.md) ---
# No hardcoded toolserver URL. Explicit configuration wins (HUGPY_TOOLSERVER_URL,
# STATION_CONSOLE_TOOLSERVER from toolserver.env, TOOLSERVER_URL); otherwise the
# toolserver ADVERTISED on this host is discovered via abstract_toolserver
# (pinned in REQUIREMENTS.txt). The backend may run under a python without the
# package, so the seat venv's `abstract-toolserver endpoint` CLI is the fallback.
_TS_URL_KEYS = ("HUGPY_TOOLSERVER_URL", "STATION_CONSOLE_TOOLSERVER", "TOOLSERVER_URL")
_TS_LOCAL_DEFAULT = "http://127.0.0.1:7004"   # last resort only (no package, no CLI)


def _ts_configured_url():
    """The operator's explicit toolserver URL, or '' (then: discover)."""
    for k in _TS_URL_KEYS:
        v = (os.environ.get(k) or "").strip()
        if v:
            return v.rstrip("/")
    return ""


def _ts_discovered_url():
    """The toolserver advertised on this host ('' when none)."""
    try:
        from abstract_toolserver import discovery as _tsd
        ad = _tsd.find_endpoint()
        return (ad or {}).get("url", "")
    except ImportError:
        pass
    except Exception:
        return ""
    import shutil
    import subprocess
    cli = (shutil.which("abstract-toolserver")
           or str(Path.home() / ".local/share/station-seats/venv/bin/abstract-toolserver"))
    if not os.path.exists(cli):
        return ""
    try:
        r = subprocess.run([cli, "endpoint"], capture_output=True, text=True, timeout=15)
        return r.stdout.strip().splitlines()[0].strip() if r.returncode == 0 and r.stdout.strip() else ""
    except Exception:
        return ""


def _ts_resolve_url():
    """configured -> discovered -> local default."""
    return _ts_configured_url() or _ts_discovered_url() or _TS_LOCAL_DEFAULT


def _ts_shared_client(url=None, token=None, timeout=None):
    """abstract_toolserver.client.ToolserverClient when importable, else None."""
    try:
        from abstract_toolserver.client import ToolserverClient
    except ImportError:
        return None
    # url None -> the client's own resolution (configured -> advertised, with the
    # advertised token_file as a token source)
    return ToolserverClient(url=url or _ts_configured_url() or None, token=token or None,
                            timeout=timeout or 120)
A_SETTINGS_TEMPLATE_PATH = (Path(os.environ["A_SETTINGS_TEMPLATE"])
                            if os.environ.get("A_SETTINGS_TEMPLATE")
                            else FV_STATE_HOME / "a-settings-template.json")
A_SETTINGS_SHIPPED = ROOT / "a-settings-template.json"


def _a_template_doc(cfg=None):
    """(doc, source, path) — the user template, else the shipped file, else
    the built-in defaults. cfg (1.0.141): the target locus's own template."""
    if cfg is not None:
        d = _cfg_json(cfg, "a_template")
        if isinstance(d, dict) and d:
            return d, "user", Path(str(cfg.get("state") or "")) / "a-settings-template.json"
    for path, src in ((A_SETTINGS_TEMPLATE_PATH, "user"),
                      (A_SETTINGS_SHIPPED, "shipped")):
        if cfg is not None and src == "user":
            continue
        try:
            doc = json.loads(path.read_text())
            if isinstance(doc, dict) and doc:
                return doc, src, path
        except (OSError, ValueError):
            continue
    return json.loads(A_DEFAULT_SETTINGS), "builtin", A_SETTINGS_TEMPLATE_PATH


def _a_settings_json() -> str:
    """The A settings template as compact JSON."""
    return json.dumps(_a_template_doc()[0], separators=(",", ":"))


# ── 1.0.146 (t4260 / d4254): every station hold, skip, coalesce and inert code
# default is RECORDED here and shown on the ⚠ strip (GET /api/loops → holds).
try:
    import station_holds as _sh
except ImportError:                                  # pragma: no cover — packaging error
    _sh = None
_HOLDS = None


def _holds():
    global _HOLDS
    if _HOLDS is None and _sh is not None:
        _HOLDS = _sh.Holds(state_path=str(FV_STATE_HOME / "station-holds.json"))
    return _HOLDS


def _hold_note(key, source, identity, detail="", action="", **kw):
    h = _holds()
    if h is None:
        return None
    try:
        return h.note(key, source, identity, detail, action, **kw)
    except Exception:                                # noqa: BLE001 — a strip row never breaks a loop
        return None


def _hold_clear(key):
    h = _holds()
    if h is None:
        return None
    try:
        return h.clear(key)
    except Exception:                                # noqa: BLE001
        return None


def _hold_rows():
    h = _holds()
    try:
        return h.rows() if h is not None else []
    except Exception:                                # noqa: BLE001
        return []


def _toggle_owner(request):
    """Who flipped an operator toggle (session user / token / ip)."""
    try:
        claims = session_claims(request) or {}
        return str(claims.get("u") or ("token" if request.headers.get("X-Console-Token") else client_ip(request)))[:40]
    except Exception:                                # noqa: BLE001
        return "operator"


def _toggle_expires(body, now=None):
    """`expires` (epoch) or `expires_s` (seconds from now) of a toggle body — 0 = none."""
    now = time.time() if now is None else now
    try:
        if body.get("expires"):
            return int(float(body["expires"]))
        if body.get("expires_s"):
            return int(now + float(body["expires_s"]))
    except (TypeError, ValueError):
        pass
    return 0


def _toggle_expired(doc, now=None):
    exp = int((doc or {}).get("expires") or 0)
    return bool(exp) and (time.time() if now is None else now) >= exp


def _frontier_doc(now=None) -> dict:
    """frontier-keeper.json as {enabled, by, at, expires, expired}. 1.0.146:
    every write carries by/at/expires; a DISABLE past its expiry reads as
    enabled again (an interruption is owned, visible and expiring — d4254)."""
    try:
        doc = json.loads((FV_STATE_HOME / "frontier-keeper.json").read_text())
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    out = {"enabled": bool(doc.get("enabled", True)), "by": str(doc.get("by") or ""),
           "at": int(doc.get("at") or 0), "expires": int(doc.get("expires") or 0), "expired": False}
    if not out["enabled"] and _toggle_expired(out, now):
        out["enabled"], out["expired"] = True, True
    return out


def _frontier_enabled() -> bool:
    """The Frontier Keeper: Enabled/Disabled toggle (default enabled). Gates
    only A-surface launches; Local Keeper and Shell are never gated."""
    return _frontier_doc()["enabled"]


async def term_frontier(request):
    """GET/POST /api/term/frontier — flip or read the Frontier Keeper toggle.
    Disabling does not stop a running frontier session (keeper-gate semantics);
    it only prevents starting a new one. POST {"enabled": bool, "expires_s"?:
    N | "expires"?: epoch} — the file records by / at / expires (1.0.146)."""
    if request.method == "POST":
        try:
            body = await request.json()
            enabled = bool(body["enabled"])
        except Exception:
            return web.json_response(
                {"error": 'body must be {"enabled": <bool>}'}, status=400)
        now = int(time.time())
        doc = {"enabled": enabled, "by": _toggle_owner(request), "at": now,
               "expires": 0 if enabled else _toggle_expires(body, now)}
        FV_STATE_HOME.mkdir(parents=True, exist_ok=True)
        (FV_STATE_HOME / "frontier-keeper.json").write_text(json.dumps(doc) + "\n")
        if enabled:
            _hold_clear("switch:frontier-keeper")
        else:
            _hold_note("switch:frontier-keeper", _sh.SOURCE_SWITCH if _sh else "station:switch",
                       "frontier keeper DISABLED by %s" % doc["by"],
                       "A-surface launches are gated%s" % (
                           " until " + time.strftime("%H:%M", time.localtime(doc["expires"])) if doc["expires"]
                           else " — NO expiry set"),
                       "POST /api/term/frontier {\"enabled\": true}", by=doc["by"], expires=doc["expires"])
        audit(request, "frontier-toggle", "enabled=%s by %s expires=%s" % (enabled, doc["by"], doc["expires"] or "-"))
        return web.json_response(dict(doc, ok=True))
    return web.json_response(_frontier_doc())


def _b_enabled() -> bool:
    """The B (local keeper) gate: Allowed/Blocked toggle (default ALLOWED).
    Gates only the direct B channels (💬 B chat); it never touches the
    frontier seat, its backend choice, or any running session."""
    try:
        doc = json.loads((FV_STATE_HOME / "b-gate.json").read_text())
        return bool(doc.get("enabled", True))
    except (OSError, ValueError):
        return True


async def term_bgate(request):
    """GET/POST /api/term/bgate — flip or read the B (local keeper) gate.
    Fully independent of the frontier seat: flipping it never switches a
    backend and never restarts anything (the old B chip aliased the frontier
    backend picker, so flipping it restarted the claude-code seat — gone)."""
    if request.method == "POST":
        try:
            body = await request.json()
            enabled = bool(body["enabled"])
        except Exception:
            return web.json_response(
                {"error": 'body must be {"enabled": <bool>}'}, status=400)
        FV_STATE_HOME.mkdir(parents=True, exist_ok=True)
        (FV_STATE_HOME / "b-gate.json").write_text(
            json.dumps({"enabled": enabled}) + "\n")
        return web.json_response({"ok": True, "enabled": enabled})
    return web.json_response({"enabled": _b_enabled()})


async def term_unstick(request):
    """POST /api/term/unstick — send the raw undo byte (Ctrl+_ / 0x1F) straight
    into the live keeper-claude tmux pane's PTY, by session name, over tmux
    send-keys — NOT over the browser's /wsterm keystroke path.

    operator 2026-09-15/16 ("is it fixed or not... it's not fixed"): the
    Ctrl+_ browser-zoom fix in fleetview-term.js calls fvSend, which (a)
    no-ops whenever the frontier surface is showing its mct-pointer-exchange
    display (the default) and (b) even unblocked, /wsterm's websocket for
    that display is bound to the DIFFERENT keeper-mct tmux session, not the
    stuck keeper-claude one. Neither the mode nor which browser view is open
    matters here: this always targets the real keeper-claude session by
    name, the same way _ping_station_keeper / the /model live-apply path
    already do."""
    # 1.0.141: ?vm= (or body.vm) = the SELECTED locus's seat, through the same
    # locus target every seat action uses ('' / @keeper = this host).
    try:
        _b = await request.json() if request.can_read_body else {}
    except Exception:
        _b = {}
    vm = (request.query.get("vm") or str((_b or {}).get("vm") or "")).strip()
    vm = "" if _is_host_vm(vm) else vm
    sess = _tmux_session_for("frontier", "claude-code") or "keeper-claude"
    rc, _o, _e = await _tmux_on(vm, "has-session", "-t", "=" + sess)
    if rc != 0:
        audit(request, "term-unstick", sess + f"@{vm or 'host'} (no live seat)", ok=False)
        return web.json_response(
            {"ok": False, "error": "no live keeper-claude seat", "session": sess, "vm": vm or "@keeper"},
            status=409)
    rc, _o, err = await _tmux_on(vm, "send-keys", "-t", "=" + sess + ":", "C-_")
    audit(request, "term-unstick", sess, ok=(rc == 0))
    if rc != 0:
        return web.json_response(
            {"ok": False, "error": (err or "").strip()[:200], "session": sess},
            status=502)
    return web.json_response({"ok": True, "session": sess})


# --- paste bypass: /api/term/paste (operator 2026-09-17) --------------------
# The operator cannot paste into the browser terminal at all: right-click is
# swallowed by fleetview-term.js's contextmenu handler and Ctrl+V shows the
# "not allowed" cursor, so the browser keystroke path is a dead end. Same wall
# term_unstick hit for Ctrl+_, and the same resolution: do NOT go through
# /wsterm — inject into the live tmux pane by SESSION NAME with send-keys.
TERM_PASTE_MAX = 200_000        # matches queue.py MAX_TEXT
TERM_PASTE_CHUNK = 2000         # well under the execve argv limit


async def term_paste(request):
    """POST /api/term/paste {"text": str, "session"?: str} — type text straight
    into the live keeper tmux pane, by session name, over tmux send-keys.

    NEVER sends Enter: the text is inserted and the operator reviews and
    submits it themselves. Newline handling (tested on tmux 3.4): send-keys -l
    passes a real "\\n" through as 0x0A, which a TUI reads as Enter and would
    submit early — so multi-line text is wrapped in bracketed-paste markers
    (ESC[200~ … ESC[201~), which claude-code/ink and readline treat as ONE
    paste with embedded newlines. Single-line text is sent bare, so a pane
    that has bracketed paste off never sees stray "200~" noise.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": 'body must be {"text": <str>}'}, status=400)
    text = body.get("text")
    if not isinstance(text, str) or not text:
        return web.json_response(
            {"error": 'body must be {"text": <non-empty str>}'}, status=400)
    if len(text) > TERM_PASTE_MAX:
        audit(request, "term-paste", "%d chars (over cap)" % len(text), ok=False)
        return web.json_response(
            {"ok": False, "error": "text over %d chars" % TERM_PASTE_MAX,
             "len": len(text)}, status=413)
    sess = (body.get("session") or "").strip() \
        or _tmux_session_for("frontier", "claude-code") or "keeper-claude"
    # 1.0.141: the SELECTED locus's pane (body.vm / ?vm=), same locus target.
    vm = (str(body.get("vm") or "") or request.query.get("vm") or "").strip()
    vm = "" if (_is_host_vm(vm) or body.get("session")) else vm   # a pulled seat is host-side
    rc0, _o0, _e0 = await _tmux_on(vm, "has-session", "-t", "=" + sess)
    if rc0 != 0:
        audit(request, "term-paste", sess + f"@{vm or 'host'} (no live seat)", ok=False)
        return web.json_response(
            {"ok": False, "error": "no live seat", "session": sess}, status=409)
    chunks = [text[i:i + TERM_PASTE_CHUNK]
              for i in range(0, len(text), TERM_PASTE_CHUNK)]
    bracketed = "\n" in text
    parts = (["\x1b[200~"] + chunks + ["\x1b[201~"]) if bracketed else chunks
    for part in parts:                       # sequential: order is the paste
        rc, _o, err = await _tmux_on(vm, "send-keys", "-t", "=" + sess + ":", "-l", "--", part)
        if rc != 0:
            audit(request, "term-paste",
                  "%s (%d chars, send-keys rc=%d)" % (sess, len(text), rc), ok=False)
            return web.json_response(
                {"ok": False, "error": (err or "").strip()[:200], "session": sess},
                status=502)
    audit(request, "term-paste", "%s (%d chars)" % (sess, len(text)), ok=True)
    return web.json_response({"ok": True, "session": sess, "chars": len(text),
                              "chunks": len(chunks), "bracketed": bracketed})


# --- 🌐 browser access: the console IS a web app — serve it to LAN browsers -
# The Electron shell loads http://127.0.0.1:<port>; "Open in browser" (Help
# menu) opens the same URL on this machine, which always works. LAN browsers
# need two things, both operator-set here: a password (stored hashed in
# USER_AUTH_FILE — from then on every NEW request logs in, the app window
# included) and the enable toggle (browser-access.json). The wide bind is
# applied at STARTUP (main block): flipping the toggle takes effect on the
# next console launch, which the UI says plainly.
BROWSER_ACCESS_PATH = FV_STATE_HOME / "browser-access.json"


def _browser_access_enabled():
    try:
        return bool(json.loads(BROWSER_ACCESS_PATH.read_text()).get("enabled"))
    except (OSError, ValueError):
        return False


def _lan_urls():
    scheme = "https" if USE_TLS else "http"
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        out = ""
    return [f"{scheme}://{ip}:{PORT}/" for ip in out.split() if ":" not in ip]


async def browser_access_get(request):
    enabled = _browser_access_enabled()
    scheme = "https" if USE_TLS else "http"
    return web.json_response({
        "enabled": enabled,
        "password_set": bool(PASSWORD_HASH),
        "auth_required": AUTH_REQUIRED,
        "listening": HOST,      # what THIS run bound; the toggle applies next launch
        "port": PORT,
        "local_url": f"{scheme}://127.0.0.1:{PORT}/",
        "urls": _lan_urls() if (enabled and AUTH_REQUIRED) else [],
        "tls": USE_TLS,
    })


async def browser_access_post(request):
    """POST /api/browser-access {"password"?: str, "enabled"?: bool}.
    password: stored hashed (pbkdf2) in USER_AUTH_FILE and applied LIVE —
    new requests need a login from now on (existing cookies stay valid).
    enabled: refuses to turn on without a password; the wide bind lands on
    the next launch."""
    global PASSWORD_HASH, AUTH_REQUIRED
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    pw = str(body.get("password") or "")
    if pw:
        if len(pw) < 8:
            return web.json_response(
                {"error": "password too short (8 characters minimum)"}, status=400)
        FV_STATE_HOME.mkdir(parents=True, exist_ok=True)
        USER_AUTH_FILE.write_text(hash_password(pw) + "\n")
        try:
            os.chmod(USER_AUTH_FILE, 0o600)
        except OSError:
            pass
        # live only when this store is the active one (env/ROOT still win)
        if not (os.environ.get("STATION_CONSOLE_PASSWORD_HASH")
                or AUTH_FILE.exists()):
            PASSWORD_HASH = USER_AUTH_FILE.read_text().strip()
            AUTH_REQUIRED = True
        audit(request, "browser-access", "password set", ok=True)
    if "enabled" in body:
        want = bool(body.get("enabled"))
        if want and not PASSWORD_HASH:
            return web.json_response(
                {"error": "set a password first — LAN access refuses to run open"},
                status=400)
        FV_STATE_HOME.mkdir(parents=True, exist_ok=True)
        BROWSER_ACCESS_PATH.write_text(json.dumps({"enabled": want}) + "\n")
        audit(request, "browser-access", f"enabled={want}", ok=True)
    return await browser_access_get(request)


# ~/.claude entries that are cache/session state and safe to clear. Credentials
# (.credentials.json) and settings.json are deliberately NOT in this list.
A_STATE_DIRS = ("projects", "todos", "shell-snapshots", "statsig",
                "file-history", "session-env")
_A_VM_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$")


async def a_template_get(request):
    """GET /api/a/template — the current A settings template and its source."""
    doc, src, path = _a_template_doc()
    return web.json_response({"template": doc, "source": src, "path": str(path)})


async def a_template_post(request):
    """POST /api/a/template — replace the template (non-empty JSON object).
    2026-08-27 (operator): a save must be IMMEDIATELY visible to the seat, so
    after writing the template this also stamps the merged doc (template + fs
    deny-list when mediated — the exact bytes the next launch would write)
    onto the HOST frontier seat's live settings.json. Best-effort: the
    template write is the durable act; a failed stamp is reported in
    `apply_error`, never a 500. VM seats still re-stamp at their own launch;
    POST /api/a/clear {"what":"settings"} remains the push path for those."""
    try:
        doc = await request.json()
        if not isinstance(doc, dict) or not doc:
            raise ValueError
    except Exception:
        return web.json_response(
            {"error": "body must be a non-empty JSON object"}, status=400)
    A_SETTINGS_TEMPLATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    A_SETTINGS_TEMPLATE_PATH.write_text(json.dumps(doc, indent=2) + "\n")
    applied, apply_err = None, None
    try:
        payload = _claude_seat_settings_json()
        rc, _out, err = await _locus_run(
            "", 'mkdir -p "$HOME/.claude-seat/frontier" && printf %s '
                + shlex.quote(payload)
                + ' > "$HOME/.claude-seat/frontier/settings.json"')
        if rc == 0:
            applied = "host"
        else:
            apply_err = ((err or "").strip() or ("rc=" + str(rc)))[-300:]
    except Exception as e:
        apply_err = str(e)
    return web.json_response({"ok": True, "path": str(A_SETTINGS_TEMPLATE_PATH),
                              "applied": applied, "apply_error": apply_err})


# The MCT workspace the frontier terminal's default backend uses; mct-usage
# reads the same one so the steward drawer's numbers describe what you see.
# The MCT workspace lives in the app's OWN data home (FV_STATE_HOME/mct), not a
# scattered ~/.mct dotfile. First run migrates a legacy ~/.mct in, the app's own
# way (atomic rename within /home). FLEETVIEW_MCT_WS still overrides to a ws path.
_LEGACY_MCT_ROOT = Path.home() / ".mct"
_APP_MCT_ROOT = FV_STATE_HOME / "mct"
if not os.environ.get("FLEETVIEW_MCT_WS"):
    try:
        _APP_MCT_ROOT.parent.mkdir(parents=True, exist_ok=True)
        if _LEGACY_MCT_ROOT.is_dir() and not _APP_MCT_ROOT.exists():
            os.rename(_LEGACY_MCT_ROOT, _APP_MCT_ROOT)  # atomic, same filesystem
        _APP_MCT_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # best-effort; base falls back to legacy only if app home is unusable
# App home is AUTHORITATIVE: the live base is never ~/.mct — legacy is only a
# migration source. Fall back to legacy solely if app home couldn't be created.
_mct_base = _APP_MCT_ROOT if _APP_MCT_ROOT.exists() else _LEGACY_MCT_ROOT
FV_MCT_WS = os.environ.get("FLEETVIEW_MCT_WS") or str(_mct_base / "repl")
# mct v2 (2026-09-15): ONE canonical mct workspace — FV_MCT_WS / the active
# session (<state>/mct/<session>, default <state>/mct/repl). The retired
# standalone-REPL workspace <state>/mct2/repl is read as a one-time migration
# / fallback source only (guidance, exchange log); nothing writes there now.
_LEGACY_MCT2_WS = FV_STATE_HOME / "mct2" / "repl"

# --- Named MCT sessions (workspaces). The console is a multi-session workbench:
# each session is its own ~/.mct/<name>/ dir (own object store, derived memory,
# .claude scope, todo). One is "active" (a pointer file); the host APIs below all
# target the active session, and the frontier terminal spawns
# `hugpy-agent mct --workspace ~/.mct/<name>`. -----------------------------------
MCT_ROOT = Path(FV_MCT_WS).parent
DEFAULT_SESSION = Path(FV_MCT_WS).name or "repl"
_ACTIVE_PTR = MCT_ROOT / ".active-session"
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _active_session():
    try:
        n = _ACTIVE_PTR.read_text(encoding="utf-8").strip()
        if n and _SESSION_RE.match(n):
            return n
    except OSError:
        pass
    return DEFAULT_SESSION


def _set_active_session(name):
    if not _SESSION_RE.match(name or ""):
        return False
    try:
        MCT_ROOT.mkdir(parents=True, exist_ok=True)
        _ACTIVE_PTR.write_text(name + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def _session_ws(name=None):
    return MCT_ROOT / (name or _active_session())


def _hugpy_path(sub, name, *legacy):
    # HUGPY_HOME base dir for hugpy runtime files (default ~/.hugpy, split into
    # state/config/logs/run). Byte-compatible with _platform/paths.py — keep in
    # sync. Migrates a legacy ~/<f> on first resolve so writes land organized.
    base = os.environ.get("HUGPY_HOME") or os.path.join(os.path.expanduser("~"), ".hugpy")
    d = os.path.join(base, sub)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    new = os.path.join(d, name)
    try:
        if not os.path.exists(new):
            for lname in legacy:
                old = os.path.join(os.path.expanduser("~"), lname)
                if os.path.exists(old) and os.path.abspath(old) != os.path.abspath(new):
                    try:
                        os.replace(old, new)
                    except OSError:
                        import shutil
                        shutil.move(old, new)
                    break
    except OSError:
        pass
    return new


def _active_ws():
    return _session_ws()


def _list_sessions():
    active = _active_session()
    seen, out = set(), []
    try:
        for p in sorted(MCT_ROOT.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                out.append({"name": p.name, "active": p.name == active,
                            "has_state": (p / ".hugpy_agent" / "mct" / "mct.db").exists()})
                seen.add(p.name)
    except OSError:
        pass
    for n in (DEFAULT_SESSION, active):
        if n not in seen:
            out.append({"name": n, "active": n == active, "has_state": False})
            seen.add(n)
    out.sort(key=lambda s: (not s["active"], s["name"]))
    return out


async def mct_sessions_get(request):
    """GET /api/mct/sessions - named MCT workspaces + which is active."""
    return web.json_response({"ok": True, "active": _active_session(),
                              "sessions": _list_sessions()})


async def mct_sessions_post(request):
    """POST /api/mct/sessions {op:"create"|"select"|"delete", name}. create/select
    make it active; delete refuses the active or default session."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    op = (body.get("op") or "").strip()
    name = (body.get("name") or "").strip()
    if not _SESSION_RE.match(name):
        return web.json_response({"error": "bad session name (A-Za-z0-9_-, <=64)"}, status=400)
    if op in ("create", "select"):
        try:
            (MCT_ROOT / name).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=500)
        _set_active_session(name)
    elif op == "delete":
        if name == DEFAULT_SESSION:
            return web.json_response({"error": "cannot delete the default session"}, status=400)
        if name == _active_session():
            return web.json_response({"error": "select another session before deleting this one"}, status=400)
        try:
            if (MCT_ROOT / name).exists():
                shutil.rmtree(MCT_ROOT / name)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=500)
    else:
        return web.json_response({"error": "op must be create|select|delete"}, status=400)
    return web.json_response({"ok": True, "active": _active_session(),
                              "sessions": _list_sessions()})


# ── MCT (abstract-claude mct) usage from the seat's OWN transcript ─────────────
# The promoted `mct` backend is `abstract-claude mct <ws>`: it drives Claude Code
# with --resume, so its exact per-request usage lives in a Claude Code transcript
# (~/.claude-sessions/<stamp>-<pid>/projects/<ws slug>/<session_id>.jsonl), NOT in
# the hugpy-agent broker's object store that `hugpy-agent mct-usage` reads. The
# steward drawer's "MCT (host A)" tracker therefore never moved (operator,
# 2026-09-10): the endpoint kept asking the deprecated <state>/mct/repl workspace.
# <ws>/session.json (written by abstract_claude.mct_repl) names the live session;
# this reads that transcript and shapes the same report `hugpy-agent mct-usage`
# returns, so the widget renders unchanged. The broker path stays as fallback.
_MCT2_WS_CANDIDATES = (
    lambda: _active_ws(),                       # mct v2: the canonical workspace first
    lambda: FV_STATE_HOME / "mct2" / "repl",    # retired standalone-REPL workspace (read-only fallback)
    lambda: Path.home() / ".config" / "hugpy-station" / "mct2" / "repl",
)
_MODEL_OUTPUT_PRICE = {"claude-fable-5-1": 50.0, "claude-fable-5": 50.0, "claude-opus-5": 25.0,
                       "claude-opus-4-8": 25.0, "claude-opus-4-7": 25.0, "claude-opus-4-6": 25.0,
                       "claude-sonnet-5": 10.0, "claude-sonnet-4-6": 15.0, "claude-haiku-4-5": 5.0}


def _mct2_live_workspace():
    """The abstract-claude mct workspace whose session.json names a live session,
    or None. The station's own state home first; the packaged app's home second
    (a dev console attaching to the packaged keeper-mct tmux session sees that)."""
    for mk in _MCT2_WS_CANDIDATES:
        try:
            ws = mk()
            if (ws / "session.json").is_file():
                return ws
        except OSError:
            continue
    return None


def _mct2_transcript_for(session_id):
    """Newest transcript file for a Claude Code session id across the config dirs a
    seat may run from (per-launch ~/.claude-sessions/<stamp>, legacy ~/.claude-seat,
    the account's ~/.claude). None if not found."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", session_id):
        return None
    import glob as _glob
    home = str(Path.home())
    cands = []
    for pat in (os.path.join(home, ".claude-sessions", "*", "projects", "*", session_id + ".jsonl"),
                os.path.join(home, ".claude-seat", "*", "projects", "*", session_id + ".jsonl"),
                os.path.join(home, ".claude", "projects", "*", session_id + ".jsonl")):
        cands.extend(_glob.glob(pat))
    if not cands:
        return None
    return max(cands, key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0)


def _mct2_usage_report(ws, turns_keep=20):
    """Build the mct-usage report dict from the live abstract-claude mct session's
    transcript. Sync + file-bound: run it via asyncio.to_thread."""
    try:
        sid = (json.loads((ws / "session.json").read_text()).get("session_id") or "").strip()
    except (OSError, ValueError, AttributeError):
        sid = ""
    base = {"workspace": str(ws), "backend": "mct", "source": "abstract-claude transcript", "sessions": []}
    if not sid:
        base["error"] = "no MCT session yet (session.json has no session_id)"
        return base
    path = _mct2_transcript_for(sid)
    if not path:
        base["sessions"] = [{"session_id": sid, "report": _mct2_empty_report(), "per_turn": [], "cache": None}]
        base["error"] = "transcript for session %s not found yet" % sid[:8]
        return base
    return _transcript_usage_report(path, sid, base, turns_keep)


def _transcript_usage_report(path, sid, base, turns_keep=20):
    """Exact usage/cost report for ONE Claude Code transcript (any seat: the mct
    pointer-exchange session or the claude-code seat itself — board t5 puts a
    tracker under each). Sync + file-bound: run it via asyncio.to_thread."""
    # One transcript line per content block, all carrying the same request usage
    # (apiBlockIndex 0,1,…): dedupe by requestId, keeping the LAST row per request.
    by_req, order = {}, []
    model = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if '"usage"' not in ln:
                    continue
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if r.get("type") != "assistant":
                    continue
                m = r.get("message") or {}
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue
                if not any(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                                         "cache_read_input_tokens", "cache_creation_input_tokens")):
                    continue                      # synthetic / zero rows are not turns
                rid = r.get("requestId") or r.get("uuid") or str(len(order))
                if rid not in by_req:
                    order.append(rid)
                by_req[rid] = (r, m, u)
                model = m.get("model") or model
    except OSError as e:
        base["error"] = str(e)
        return base
    rows = [by_req[k] for k in order]
    p_in = next((v for k, v in _MODEL_INPUT_PRICE.items() if model.startswith(k)), None)
    p_out = next((v for k, v in _MODEL_OUTPUT_PRICE.items() if model.startswith(k)), None)

    def _ts(r):
        t = r.get("timestamp") or ""
        try:
            import datetime as _dt
            return _dt.datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=_dt.timezone.utc).timestamp()
        except Exception:
            return 0.0

    agg = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    billed = 0.0
    per_turn = []
    for r, m, u in rows:
        i = int(u.get("input_tokens") or 0); o = int(u.get("output_tokens") or 0)
        cr = int(u.get("cache_read_input_tokens") or 0); cw = int(u.get("cache_creation_input_tokens") or 0)
        cc = u.get("cache_creation") or {}
        wmult = 2.0 if int(cc.get("ephemeral_1h_input_tokens") or 0) > 0 else 1.25
        agg["input"] += i; agg["output"] += o; agg["cache_read"] += cr; agg["cache_write"] += cw
        billed += i + cw * wmult + cr * 0.1
        per_turn.append({"ts": int(_ts(r)), "model": m.get("model") or "", "input": i, "output": o,
                         "cache_write": cw, "cache_read": cr, "stop_reason": m.get("stop_reason") or ""})
    relayed = agg["input"] + agg["cache_write"] + agg["cache_read"]
    cost = ((billed / 1e6) * p_in if p_in else 0.0) + ((agg["output"] / 1e6) * p_out if p_out else 0.0)
    report = {
        "turns": len(rows), "input": agg["input"], "cache_write": agg["cache_write"],
        "cache_read": agg["cache_read"], "output": agg["output"],
        "relayed_input": relayed, "billed_input_equiv": round(billed, 1),
        "cost_usd": round(cost, 4) if (p_in or p_out) else 0.0,
        "cached_pct": round(agg["cache_read"] / relayed * 100, 1) if relayed else 0.0,
        "input_cost_saved_by_cache_pct": round((1 - billed / relayed) * 100, 1) if relayed else 0.0,
        "model": model, "price_known": bool(p_in and p_out),
    }
    cache = None
    if rows:
        r, m, u = rows[-1]
        cc = u.get("cache_creation") or {}
        ttl = 3600 if int(cc.get("ephemeral_1h_input_tokens") or 0) > 0 else 300
        last_ts = _ts(r)
        age = max(0.0, time.time() - last_ts)
        cr = int(u.get("cache_read_input_tokens") or 0); cw = int(u.get("cache_creation_input_tokens") or 0)
        cache = {"age_sec": round(age, 1), "ttl_sec": ttl,
                 "state": "expected-valid" if age < ttl else "expired",
                 "cached_tokens": cr + cw, "cache_read": cr, "cache_creation": cw,
                 "context_tokens": int(u.get("input_tokens") or 0) + cr + cw,
                 "last_request_ts": int(last_ts), "expires_at": int(last_ts + ttl),
                 "model": m.get("model") or "", "stop_reason": m.get("stop_reason") or ""}
    base["transcript"] = path
    base["sessions"] = [{"session_id": sid, "report": report, "per_turn": per_turn[-turns_keep:], "cache": cache}]
    return base


def _mct2_empty_report():
    return {"turns": 0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
            "relayed_input": 0, "billed_input_equiv": 0, "cost_usd": 0.0,
            "cached_pct": 0.0, "input_cost_saved_by_cache_pct": 0.0}


async def _tmux_has(sess):
    if not sess:
        return False
    rc, _o, _e = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "has-session", "-t", "=" + sess)
    return rc == 0


async def _claude_seat_transcript():
    """Newest transcript of the live keeper-claude seat: its pane cwd → Claude
    Code's project slug → newest *.jsonl across the config dirs a seat runs
    from (per-launch ~/.claude-sessions/<stamp>, legacy ~/.claude-seat, the
    account's ~/.claude). '' when no live seat or nothing written yet."""
    sess = _tmux_session_for("frontier", "claude-code") or "keeper-claude"
    rc, cwd, _e = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "display-message", "-p",
                             "-t", "=" + sess + ":", "#{pane_current_path}")
    cwd = (cwd or "").strip() if rc == 0 else ""
    if not cwd:
        return ""
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    import glob as _glob
    home = str(Path.home())
    for pat in (".claude-sessions/*/projects/%s/*.jsonl", ".claude-seat/*/projects/%s/*.jsonl",
                ".claude/projects/%s/*.jsonl"):
        c = _glob.glob(os.path.join(home, pat % slug))
        if c:
            return max(c, key=os.path.getmtime)
    return ""


async def a_usage(request):
    """GET /api/a/usage?backend=mct|claude-code — precise token/cost accounting
    + cache timing for a frontier seat, from that seat's OWN Claude Code
    transcript (board t5: one tracker per frontier backend). mct = the
    abstract-claude pointer-exchange session (see _mct2_usage_report);
    claude-code = the keeper-claude seat's newest transcript. No backend =
    mct, and when no mct2 session exists the deprecated broker's
    `hugpy-agent mct-usage` still answers. With an active VM the readout comes
    from THAT VM's workbench (grounding rule)."""
    vm, key = _locus_scope(request)
    if vm:
        # 1.0.141: another locus's usage comes from the CENTRAL exchanges table
        # keyed by that locus (its seats' Stop hook / serve record every turn) —
        # never from this host's transcripts.
        try:
            rep = await _ts_call(request.app, "exchange/usage_report", {"locus": key, "last": 20}, timeout=15)
        except Exception as e:                           # noqa: BLE001
            return web.json_response({"ok": False, "locus": key, "remote": True,
                                      "error": f"usage for {key} unavailable (toolserver exchanges): {str(e)[:200]}"},
                                     status=502)
        tot = (rep or {}).get("totals") or {}
        return web.json_response({
            "ok": True, "locus": key, "remote": True, "backend": request.query.get("backend") or "mct",
            "source": f"toolserver exchanges (locus={key})", "sessions": [],
            "turns": tot.get("runs") or 0, "input": tot.get("input_tokens") or 0,
            "cache_write": tot.get("cache_creation_tokens") or 0, "cache_read": tot.get("cache_read_tokens") or 0,
            "output": tot.get("output_tokens") or 0, "cost_usd": tot.get("total_cost_usd") or 0.0,
            "by_source": (rep or {}).get("by_source") or [], "recent": (rep or {}).get("recent") or []})
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
    backend = (request.query.get("backend") or "mct").strip()
    if backend == "codex":
        return web.json_response({"backend": "codex", "sessions": [],
                                  "source": "Codex native /status",
                                  "error": "Use /status in the ChatGPT lane for usage; harness accounting is not connected."})
    if backend == "claude-code":
        sess = _tmux_session_for("frontier", "claude-code") or "keeper-claude"
        live = await _tmux_has(sess)
        base = {"backend": "claude-code", "source": "claude-code seat transcript", "sessions": [],
                "live_session": sess if live else ""}
        path = await _claude_seat_transcript() if live else ""
        if not path:
            base["error"] = "no live claude-code seat" if not live else "the seat has not written a transcript yet"
            return web.json_response(base)
        sid = os.path.basename(path)[:-len(".jsonl")]
        try:
            doc = await asyncio.wait_for(asyncio.to_thread(_transcript_usage_report, path, sid, base), timeout=20)
        except asyncio.TimeoutError:
            return web.json_response({"error": "seat transcript read timed out"}, status=502)
        return web.json_response(doc)
    ws2 = _mct2_live_workspace()
    if ws2 is not None:
        try:
            doc = await asyncio.wait_for(asyncio.to_thread(_mct2_usage_report, ws2), timeout=20)
        except asyncio.TimeoutError:
            return web.json_response({"error": "mct transcript read timed out"}, status=502)
        doc["live_session"] = (_tmux_session_for("frontier", "mct") or "keeper-mct") if await _tmux_has(
            _tmux_session_for("frontier", "mct") or "keeper-mct") else ""
        return web.json_response(doc)
    exe = shutil.which("hugpy-agent")
    if not exe:
        return web.json_response({"error": "hugpy-agent not on PATH"}, status=502)
    try:
        rc, out, err = await asyncio.wait_for(
            _run(exe, "mct-usage", str(_active_ws())), timeout=20)
    except asyncio.TimeoutError:
        return web.json_response({"error": "mct-usage timed out"}, status=502)
    if rc != 0:
        return web.json_response({"error": err.strip() or "mct-usage failed"},
                                 status=502)
    try:
        return web.json_response(json.loads(out))
    except ValueError:
        return web.json_response({"error": "mct-usage returned non-JSON"},
                                 status=502)


# Operator standing guidance to B on how to frame the frontier model (A). The
# console's 🧭 sidebar writes it here; the MCT broker reads the SAME file each
# turn (session._operator_guidance) and injects it as an L0 governing
# instruction, so it always reaches A at top authority. Same workspace the
# frontier terminal uses (FV_MCT_WS), so the two ends agree on the path.
def _bg_path():
    """The COMPOSED file B reads each turn (frontier directive + operator text)."""
    return _active_ws() / "operator-guidance.md"


def _bg_user_path(ws=None):
    """The operator's OWN guidance text (🧭 panel). 1.0.41: kept apart from the
    composed file so the frontier directive can be prepended without ever
    clobbering what the operator wrote. One-time migration: an existing
    composed file with no user file IS the user text."""
    ws = ws or _active_ws()
    up = ws / "operator-guidance.user.md"
    cp = ws / "operator-guidance.md"
    if not up.exists():
        # mct v2 (2026-09-15): the canonical workspace is FV_MCT_WS / the active
        # session; a user file left in the retired mct2/repl workspace migrates
        # once so a guidance edit made there is not silently lost.
        legacy = _LEGACY_MCT2_WS / "operator-guidance.user.md"
        src = legacy if legacy.exists() else cp if cp.exists() else None
        if src is not None:
            try:
                # A COMPOSED file (directive + user text) must never become the
                # user text — that is how the directive got doubled at every
                # later composition. Strip the directive on the way in.
                ws.mkdir(parents=True, exist_ok=True)
                up.write_text(_strip_directive(src.read_text(encoding="utf-8")), encoding="utf-8")
            except OSError:
                pass
    return up


def _strip_directive(text):
    """Remove every copy of the frontier directive (and the composition
    headers it travels with) from operator guidance text, so the directive is
    composed into a seat's governing instruction EXACTLY once.
    1.0.143: every generated directive block (BEGIN/END-fenced by
    directives.py), composer header and switch section goes mechanically
    (DV.strip_generated); the operator's tmux overlay / legacy file text too."""
    t = text or ""
    try:
        d = _frontier_directive_text()[0].strip()
    except Exception:
        d = ""
    if d and d in t:
        t = t.replace(d, "")
    return DV.strip_generated(t)


def _cfg_text(cfg, key):
    """1.0.141: a seat-config snapshot's file text (locus_exec.read_cfg shape:
    {"state", "files": {key: text|None}}); None = the file is absent there."""
    return ((cfg or {}).get("files") or {}).get(key)


def _cfg_json(cfg, key):
    t = _cfg_text(cfg, key)
    try:
        return json.loads(t) if t else None
    except ValueError:
        return None


# ── 1.0.146: the seat handoff lives in the TOOLSERVER (operator 2026-10-01:
# "the toolserver should house the handoffs … /resume should allow for its
# pull"). <state>/frontier-handoff.md is retired: no file an operator or agent
# has to place, no station-side consumption. The launch directive carries only
# the one-paragraph POINTER the toolserver renders (handoff/station → layer);
# the body is injected ONCE by the seat's SessionStart hook (ledger_hook.py →
# handoff/pull, which marks the row consumed by that session id). The steward
# tab reads/writes the same row (api_frontier_handoff → handoff/station,
# handoff/request).
_HANDOFF_LAYER = {}          # locus -> the toolserver's launch layer ('' = none open / unreachable)


async def _handoff_layer_refresh(app, locus):
    """Fetch the launch layer for `locus` from the toolserver (handoff/station)
    right before a seat launch. Unreachable toolserver → an explicit layer that
    says so (the seat must then pull by hand), never a silent blank."""
    locus = _canon_locus(locus or "") or _station_locus()
    if not locus:
        _HANDOFF_LAYER[locus] = ""
        return ""
    try:
        doc = await _ts_call(app, "handoff/station", {"locus": locus}, timeout=10)
        layer = str((doc or {}).get("layer") or "")
    except Exception as e:                                   # noqa: BLE001
        layer = (f"The toolserver could not be asked for an open handoff on locus {locus} "
                 f"({str(e)[:120]}). Before working, run handoff_pull locus={locus} yourself — "
                 "a handoff may be waiting.")
    _HANDOFF_LAYER[locus] = layer
    return layer


def _frontier_handoff_pending(cfg=None):
    """The launch layer last fetched for the locus (text or '')."""
    loc = str((cfg or {}).get("locus") or "") or _station_locus()
    return _HANDOFF_LAYER.get(_canon_locus(loc) or loc, "")


def _compose_guidance(user_text):
    """What B injects as the governing_instruction: the frontier directive,
    then the operator's guidance, then (this launch only) the session init
    prompt. The steward tab shows exactly this.
    1.0.143: the mct seat is a TMUX seat — it gets the generated tmux
    directive (fallback / inference arm, never "the keeper") + the operator
    guidance + the tmux overlay + its delegation switch + the handoff."""
    snap = _dirv_snapshot()
    snap["files"]["guidance"] = _strip_directive(user_text)   # the directive is composed ONCE, here
    return DV.compose("tmux", snap, fx=_dirv_facts(), backend="mct", with_handoff=True)


def _render_guidance(ws=None):
    """Re-render <ws>/operator-guidance.md from the directive + user text.
    Called on every guidance/directive save and at every host-grounded mct
    launch, so the mct seat can never run on a stale composition."""
    ws = ws or _active_ws()
    try:
        ws.mkdir(parents=True, exist_ok=True)
        up = _bg_user_path(ws)
        user = up.read_text(encoding="utf-8") if up.exists() else ""
        (ws / "operator-guidance.md").write_text(_compose_guidance(user), encoding="utf-8")
        return True
    except OSError:
        return False


async def _push_guidance_to_ssh(name, session):
    d = _push_directive(name)
    ws = f"$HOME/.mct/{session}"
    script = (f'mkdir -p "$HOME/.config/hugpy-station" {ws}; '
              f'cat > "$HOME/.config/hugpy-station/frontier-directive.md" <<\'__HSDIR__\'\n{d}\n__HSDIR__\n'
              f'u={ws}/operator-guidance.user.md; c={ws}/operator-guidance.md; '
              f'[ -f "$u" ] || : > "$u";'
              f'{{ cat "$HOME/.config/hugpy-station/frontier-directive.md"; '
              f'if [ -s "$u" ]; then printf "\\n\\n# Operator guidance\\n"; cat "$u"; fi; echo; }} > "$c"')
    rc, _o, _e = await _ssh_run(name, script, 20)
    return rc == 0


async def _push_mct_to_locus(vm):
    """Locus-grounded mct (operator 2026-08-27; abstract-claude 2026-08-31): the
    REPL now ships in the `abstract-claude` package, so ENSURE it's installed in
    the locus (best-effort) and compose fresh guidance there at every launch
    (same freshness + directive-path rules). Best effort; the seat still
    launches if this fails — `abstract-claude mct` then reports the missing
    package in the terminal."""
    # 2026-09-14: a peer machine (SSH-reachable in the loci feed) runs its OWN
    # hugpy-station and owns its frontier-directive.md — pushing ours there
    # overwrites it (this clobbered a peer keeper's appended init-prompt once).
    # Only push guidance to loci WE manage (lxd guests / local host); a peer
    # station composes its own from its own directive.
    if vm in _TS_LOCI.get("hosts", {}):
        return True
    d = _push_directive(vm)
    ws = "$HOME/.config/hugpy-station/mct2/repl"
    script = ('command -v abstract-claude >/dev/null 2>&1 || '
              'pip install --user -q -U abstract-claude >/dev/null 2>&1 || '
              'pipx install abstract-claude >/dev/null 2>&1 || true; '
              f'mkdir -p {ws}; '
              f'cat > "$HOME/.config/hugpy-station/frontier-directive.md" <<\'__HSDIR__\'\n{d}\n__HSDIR__\n'
              f'u={ws}/operator-guidance.user.md; c={ws}/operator-guidance.md; '
              f'[ -f "$u" ] || : > "$u";'
              f'{{ cat "$HOME/.config/hugpy-station/frontier-directive.md"; '
              f'if [ -s "$u" ]; then printf "\\n\\n# Operator guidance\\n"; cat "$u"; fi; echo; }} > "$c"')
    if _ssh_host(vm):
        rc, _o, _e = await _ssh_run(vm, script, 60)
        return rc == 0
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", vm, "--", "sudo", "-u", "ubuntu", "-H", "bash", "-c", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(proc.communicate(), 60)
        return proc.returncode == 0
    except Exception:
        return False


async def _push_guidance_to_vm(vm, session):
    """VM-grounded mct seat: the VM's workbench composes its own file from ITS
    directive — so hand it the host's directive first (same path rule:
    ~/.config/hugpy-station/frontier-directive.md of the dev user), then
    compose the session's file from that + whatever user guidance the VM
    already holds. Best effort; the seat still launches if this fails."""
    d = _push_directive(vm)
    ws = f"{VM_MCT_ROOT}/{session}"
    script = (f'mkdir -p "$HOME/.config/hugpy-station" {shlex.quote(ws)}; '
              f'cat > "$HOME/.config/hugpy-station/frontier-directive.md"; '
              f'u={shlex.quote(ws)}/operator-guidance.user.md; c={shlex.quote(ws)}/operator-guidance.md; '
              f'[ -f "$u" ] || : > "$u";'
              f'{{ cat "$HOME/.config/hugpy-station/frontier-directive.md"; '
              f'if [ -s "$u" ]; then printf "\n\n# Operator guidance\n"; cat "$u"; fi; echo; }} > "$c"')
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", vm, "--", "sudo", "-u", "ubuntu", "-H", "bash", "-c", script,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(proc.communicate(d.encode("utf-8")), 20)
        return proc.returncode == 0
    except Exception:
        return False


async def b_guidance_get(request):
    """GET /api/b/guidance - the operator's standing guidance to B (how to frame A)."""
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
    try:
        up = _bg_user_path()
        text = up.read_text(encoding="utf-8") if up.exists() else ""
        composed = _bg_path().read_text(encoding="utf-8") if _bg_path().exists() else ""
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"text": text, "path": str(up),
                              "composed": composed, "composed_path": str(_bg_path()),
                              "note": "B injects composed = frontier directive + this text"})


async def b_guidance_post(request):
    """POST /api/b/guidance {"text": "..."} - set/replace the standing guidance the
    MCT broker injects into every A turn as an L0 governing instruction. An empty
    (or whitespace-only) text clears it."""
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
    try:
        body = await request.json()
        text = body.get("text", "")
        if not isinstance(text, str):
            raise ValueError
    except Exception:
        return web.json_response({"error": 'body must be {"text": string}'}, status=400)
    try:
        up = _bg_user_path()
        up.parent.mkdir(parents=True, exist_ok=True)
        if text.strip():
            up.write_text(text, encoding="utf-8")
            did = f"guidance saved ({len(text)} chars)"
        else:
            if up.exists():
                up.unlink()
            did = "guidance cleared"
        _render_guidance()
        _render_session_directives()        # 1.0.143: the keeper serve session's directive carries it
        did += " — composed with the tmux directive into operator-guidance.md and into the keeper session's directive"
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True, "did": did, "path": str(up), "composed_path": str(_bg_path())})


# --- Direct session with B (the broker), independent of the frontier REPL. -----
# B answers from its own durable broker state (policy, catalog, derived memory),
# never in A's voice. We run a BrokerServer over the SAME workspace the frontier
# terminal uses, so it reconstructs B's state from disk (read-only for chat).
# Multi-turn history is client-held and threaded in. Blocking work (broker
# construction + gateway call) runs in an executor so the event loop is free.
_CONSOLE_B = {}

_B_SYSMSG = (
    "You are B - the broker/curator of a Mediated Context Terminal. You curate "
    "bounded context for A (a confined Claude) and keep the ledger, catalog, and "
    "derived memory. Answer the operator directly, concisely, in first person as "
    "B. Ground every claim in the state below; when the state does not contain "
    "the answer, say so plainly.\n\n=== your current state ===\n")


def _b_local_brief():
    """1.0.143: B's directive + init brief (session kind `local`) for this
    locus — the rendered file when present, else composed now. '' on error."""
    try:
        p = FV_STATE_HOME / DV.RENDERED_DIR / "local.md"
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
        return _session_directive("local").strip()
    except Exception:                                    # noqa: BLE001
        return ""


def _console_b_server():
    ws = str(_active_ws())
    srv = _CONSOLE_B.get(ws)
    if srv is None:
        from hugpy_agent.mct.session import BrokerServer
        srv = BrokerServer(ws, sink=lambda *_a, **_k: None)
        _CONSOLE_B[ws] = srv
    return srv


def _latest_session_id(srv):
    import sqlite3
    db = _active_ws() / ".hugpy_agent" / "mct" / "mct.db"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute(
            "SELECT session_id FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()
        con.close()
        if row:
            return row[0]
    except Exception:
        pass
    return srv.open_session("console-b")


# Grounding follows the session (operator, 2026-08-12): A, B, and the local
# keeper are grounded in the VM they are attributed to — the host keeper's
# models are the ONLY host-grounded set. The frontier session's real state
# (ledger/catalog/derived memory) lives in $STATION_CONSOLE_MODEL_VM at
# $STATION_CONSOLE_VM_MCT_ROOT/<session>, so the console's B chat asks the B
# THERE (one-shot exec of hugpy_agent.mct.b_answer). The in-process host
# grounding below remains only as the fallback for deployments without a
# model VM — where "the host" IS the machine the models are attributed to.
VM_MCT_ROOT = os.environ.get("STATION_CONSOLE_VM_MCT_ROOT", "/home/ubuntu/.mct")

# --- Per-VM sidecar grounding (operator, 2026-08-20): the drawers follow the
# terminal. Each VM runs its OWN station (workbench) against its own files, so
# when the SPA has a VM active the harness FORWARDS the drawer APIs (usage,
# guidance, todo, live flow, prompt inbox, frontier-fs) to that VM's workbench
# instead of answering from host files — the harness is a switchboard, not a
# second source of truth. Before forwarding, the workbench's active MCT
# session is aligned to the harness's, so its drawers read the SAME locus the
# frontier PTY grounds in ($VM_MCT_ROOT/<session>). No VM active (or the
# @keeper host seat, or the /api/vm/keeper/* aliases) keeps the host-native
# path. A VM with no reachable workbench answers 502 with the install hint —
# a host-file answer about a VM surface would be a lie.
# ── WORKBENCH TRANSPORT RETIRED (operator 2026-09-02: "that was before the
# central db. all of that can be gutted"). A locus used to run its OWN station
# ("workbench") that this harness reached at http://<host>:8800 with a
# vmconsole-<locus>.token. The address was per MACHINE, so two loci on one host
# collided (hugpy's drawers hit vm_mgr's station → 401), every station needed a
# copy of every token, and a closed port hung every drawer 60 s. The toolserver
# (DB arm) is the one thing reachable from anywhere that knows every locus, so
# per-locus drawers are served from it: board = todo/*, prompts = prompt/*;
# the rest answer an honest 404 until their toolserver source lands
# (fleet-consolidation/STATION-LOCUS-MAP-2026-09-02.md §2).
def _retired(what, vm=""):
    return web.json_response(
        {"ok": False, "retired": True,
         "error": (what + (f" for {vm}" if vm else "")
                   + ": served by the toolserver (DB arm) — not wired yet; the per-locus"
                     " workbench transport was retired 2026-09-02")}, status=404)


async def _sidecar_vm(request):
    """The locus a sidecar request grounds in: ?vm= (the SPA's active locus)
    when it names a real station OR an ssh host (both run their own workbench
    console — install-vm-console / install-ssh-console), else the static
    model-VM pin. '' = host-native."""
    m = re.match(r"^/api/vm/([^/]+)/", request.path)
    if m and _is_host_vm(m.group(1)):
        return ""              # the host's own board/aliases (@self/@keeper/own locus name)
    vm = (request.query.get("vm") or "").strip()
    if _is_host_vm(vm):
        return ""
    if vm:
        await _loci_ready()        # 1.0.138: callers map vm -> its central key next
        if _ssh_host(vm):
            return vm
        try:
            names = await known_names()
        except Exception:      # lxc unavailable (snap home-outside-/home as a
            names = set()      # service user, lxd down): never 500 a drawer
        if vm in names:
            return vm
    return ""                  # host-native (the static model-VM pin retired with the workbench)


# ── central (DB-arm) boards for ssh-host loci (2026-09-02) ───────────────────
# An ssh-host locus on THIS machine (ae-vm-mgr and hugpy are both
# 192.168.1.100) has no workbench of its own: :8800 here is this very station
# behind nginx, so forwarding looped back onto the HOST board with a foreign
# token (401) or died on a missing token file (502) — "the todo does not show
# up for anything other than host". Those loci's boards live in the toolserver
# `todos` table (todo/list locus=… — the same slice every keeper, seat and
# comms.ping writes with todo_add), so the ☑ drawer reads AND writes that
# slice for them. A REMOTE ssh host with a vmconsole token keeps its own
# workbench; one without a token falls back to the central slice, not a 502.
_LOCAL_IPS_CACHE = {"ts": 0.0, "ips": set()}


def _local_ips_cached():
    now = time.time()
    if now - _LOCAL_IPS_CACHE["ts"] > 300 or not _LOCAL_IPS_CACHE["ips"]:
        _LOCAL_IPS_CACHE["ips"] = _local_ips()
        _LOCAL_IPS_CACHE["ts"] = now
    return _LOCAL_IPS_CACHE["ips"]


# ── 1.0.138: ONE station, every other locus is a central-DB key ──────────────
# Operator 2026-09-30: "this station is to be locus specific. the top left
# dropdown switches locus' … have all features pointed to that host. so really it
# just needs to repoint the db endpoint to that locus' table/row" — and no
# hugpy-station install / sidecar / workbench may be REQUIRED on any locus but the
# one host the browser uses. So a dropdown pick is just a different `locus` key on
# the central (toolserver) tables (todos, canvas, prompts, comms pings, seat_state,
# assessments); only the live terminal still needs the machine itself.
# 1.0.141: "keeper" is vm_mgr's REGISTERED locus key, never a synonym for "this
# station". The host is addressed by reserved tokens that can never be a locus
# key ('' / @self / @keeper / host) or by this station's OWN locus name
# (STATION_LOCUS / the registry) — so on any other station a dropdown entry
# named `keeper` is vm_mgr's locus, not the viewer's own board.
_HOST_VMS = ("", "@self", "@keeper", "host")
HOST_TOKEN = "@self"
# Documented legacy spellings of a registered locus (docs/STATION-FEATURES-PER-
# LOCUS.md: "ae-vm-mgr remains only as a loci alias row" of keeper).
_LEGACY_LOCUS_ALIASES = {"ae-vm-mgr": "keeper", "ae_vm_mgr": "keeper"}
LOCUS_ALIASES_PATH = FV_STATE_HOME / "locus-aliases.json"


def _canon_locus(name):
    """A locus name with its documented legacy alias folded (ae-vm-mgr -> keeper)."""
    n = (name or "").strip().lower()
    return _LEGACY_LOCUS_ALIASES.get(n, n)


def _is_host_vm(vm):
    """The host seat: a reserved token, or this station's own locus name (in
    any documented spelling). Never the bare word `keeper` unless `keeper` IS
    this station's locus."""
    v = (vm or "").strip()
    if v in _HOST_VMS:
        return True
    own = _station_locus()
    return bool(own) and _canon_locus(v) == own


def _locus_aliases():
    """Optional operator map {dropdown name: central locus key} in
    $FV_STATE_HOME/locus-aliases.json — for a locus whose rows were filed under
    another name (e.g. {"solcatcher": "ae-solcatcher"}). Absent file = {}."""
    doc = _read_json(LOCUS_ALIASES_PATH, {}) or {}
    if not isinstance(doc, dict):
        return {}
    out = {}
    for k, v in doc.items():
        v = str(v or "").strip().lower()
        if isinstance(k, str) and _SSH_NAME_RE.match(v):
            out[k.strip()] = v
    return out


def _lxd_ips(name):
    """The guest's IPv4s from the discover() cache (no lxc call here)."""
    for st in (_DISCOVER_CACHE.get("stations") or []):
        if st.get("name") == name:
            return list(st.get("ips") or ([st["ip"]] if st.get("ip") else []))
    return []


def _endpoint(h):
    try:
        port = int(h.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    return (str(h.get("user") or "root"), str(h.get("host") or ""), port)


def _endpoints_of(vm):
    """{(user, host, port)} a dropdown locus is reached at: its ssh pointer, or
    ubuntu@<each guest ip> for an LXD guest (the guest's dev/seat user)."""
    h = _ssh_host(vm)
    if h:
        return {_endpoint(h)}
    return {("ubuntu", ip, 22) for ip in _lxd_ips(vm)}


def _self_locus_names():
    """This station's own locus name plus every legacy spelling of it."""
    own = _station_locus()
    if not own:
        return set()
    return {own} | {a for a, k in _LEGACY_LOCUS_ALIASES.items() if k == own}


def _pick_endpoint_locus(names, registered=None):
    """ONE locus for an endpoint that several registered rows share (keeper and
    ae-mgr are both vm_mgr@192.168.1.100:22). Deterministic: a kind=station row
    beats any other kind; a row the station itself auto-registered when an ssh
    host was added ("ssh host added on ...") loses to a deliberately registered
    one; a legacy-alias target beats a plain name. A TIE between the best two
    is never broken by guessing (no lexical pick): '' then, so the caller keeps
    the bare name (an empty slice beats someone else's board)."""
    registered = registered if registered is not None else (_TS_LOCI.get("hosts") or {})
    names = sorted(set(n for n in names if n))
    if len(names) <= 1:
        return names[0] if names else ""
    alias_targets = set(_LEGACY_LOCUS_ALIASES.values())

    def rank(n):
        h = registered.get(n) or {}
        kind = str(h.get("kind") or "")
        auto = str(h.get("goal") or "").startswith("ssh host added on ")
        return (kind != "station", auto, n not in alias_targets)
    ranked = sorted(names, key=rank)
    if rank(ranked[0]) == rank(ranked[1]):
        return ""
    return ranked[0]


def _central_locus(vm):
    """The central (toolserver DB) locus key whose rows serve dropdown locus `vm`
    — for EVERY locus kind, so switching the dropdown is only a different key:
      * the host seat ('' / keeper / @keeper / host / this station's own locus
        name, or any pointer at this very user@host) -> _keeper_locus();
      * an operator alias (locus-aliases.json) wins next;
      * a locus REGISTERED in the toolserver (loci/pointers) is its own key;
      * an unregistered ssh host / LXD guest whose endpoint is exactly ONE
        registered locus's endpoint resolves to that locus (one machine, one
        locus: hs-fresh -> hs-fresh-ubuntu, a-brain -> a-brain-coder-next);
      * anything else is its own name.
    '' only for a string that cannot be a locus key."""
    v = (vm or "").strip()
    if v in _HOST_VMS:
        return _keeper_locus()            # '' when this station has no locus yet
    if not _SSH_NAME_RE.match(v):
        return ""
    alias = _locus_aliases().get(v)
    if alias:
        return alias
    if v in _self_locus_names():
        return _keeper_locus()
    registered = _TS_LOCI.get("hosts") or {}
    if v in registered:
        return v
    if v in _LEGACY_LOCUS_ALIASES:        # 1.0.141: documented legacy spelling
        return _LEGACY_LOCUS_ALIASES[v]
    eps = _endpoints_of(v)
    if eps:
        me, ips = getpass.getuser(), _local_ips_cached()
        if _keeper_locus() and any(u == me and host in ips and port == 22 for u, host, port in eps):
            return _keeper_locus()            # a pointer at THIS station's own user@host
        hits = [n for n, h in registered.items()
                if isinstance(h, dict) and _endpoint(h) in eps]
        if hits:                          # 1.0.141: several rows on one endpoint -> one, deterministically
            return _pick_endpoint_locus(hits, registered) or v
    return v


async def _loci_ready(timeout=10.0):
    """Wait (bounded) for the first loci/pointers sync ATTEMPT after startup:
    until it lands, registered loci are unknown and an endpoint alias (hs-fresh
    -> hs-fresh-ubuntu) would resolve to the bare name — a write in the first
    seconds after a restart must not land on the wrong key."""
    end = time.monotonic() + timeout
    while not _TS_LOCI.get("synced") and time.monotonic() < end:
        await asyncio.sleep(0.2)


async def _known_locus_key(name):
    """_central_locus(name) when `name` is a locus this station knows (host
    alias, ssh host, LXD guest, registered locus); '' for an unknown name — a
    typo in a URL must never read or write some other locus's rows."""
    if _is_host_vm(name):
        return _keeper_locus()
    await _loci_ready()
    if (_ssh_host(name) or name in (_TS_LOCI.get("hosts") or {})
            or name in _self_locus_names() or name in await _known_names_safe()):
        return _central_locus(name)
    return ""


def _central_todo_path(locus):
    return f"{TS_UPSTREAM.rstrip('/')}/todo/list?locus={locus}"


# The central (toolserver) `todos` table has NO comments column — comments live
# IN the note (the established idiom: see _central_todo_post). We encode each as
# ONE human-readable note line that also carries the exact epoch (#<ts>), so the
# read side (_central_item) reconstructs a todo.v1 comments array whose {by,ts,
# text} round-trips EXACTLY — the console's postComment() verify-after-write then
# matches without any frontend change (its o8 note: "when it lands, landed starts
# coming back true"). Legacy lines without #<ts> parse with a minute-resolution ts.
_CENTRAL_COMMENT_RE = re.compile(
    r"^\[comment\s+(?P<by>\S+)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)"
    r"(?:\s+#(?P<ts>\d+))?\]\s?(?P<text>.*)$")


def _encode_comment(c):
    """One todo.v1 comment dict -> a single note line, parseable by
    _split_note_comments. Single-line (newlines flattened) so one comment is
    always one line."""
    by = str((c or {}).get("by") or "operator")[:24]
    ts = (c or {}).get("ts")
    ts = int(ts) if isinstance(ts, (int, float)) else int(time.time())
    text = str((c or {}).get("text") or "").strip().replace("\r", " ").replace("\n", " ")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    return f"[comment {by} {when} #{ts}] {text}"


def _split_note_comments(note):
    """Split a central note into (human_note, comments[]). `[comment ...]` lines
    are pulled into a todo.v1 comments array so the console renders them as a 💬
    thread exactly like a local board; every other line stays in the human note."""
    human, comments = [], []
    for ln in str(note or "").split("\n"):
        m = _CENTRAL_COMMENT_RE.match(ln)
        if not m:
            human.append(ln)
            continue
        if m.group("ts"):
            ts = int(m.group("ts"))
        else:
            try:
                ts = int(time.mktime(time.strptime(m.group("date")[:16], "%Y-%m-%d %H:%M")))
            except Exception:
                ts = 0
        comments.append({"by": m.group("by")[:24], "ts": ts, "text": m.group("text")})
    return "\n".join(human), comments


def _central_item(r):
    """toolserver todos row -> todo.v1 item (same field names, low priority
    pops the field exactly like todo_norm)."""
    if not isinstance(r, dict) or not str(r.get("text") or "").strip():
        return None
    human, comments = _split_note_comments(r.get("note"))
    it = {"id": str(r.get("id") or ""),
          "type": r.get("type") if r.get("type") in TODO_TYPES else "todo",
          "text": str(r.get("text"))[:500],
          "note": human,      # never truncated (operator 2026-09-14; BOARD-ITEM-FORMAT.md)
          "status": r.get("status") if r.get("status") in TODO_STATUS else "open",
          "by": str(r.get("by") or "")[:24],
          "ts": int(r.get("updated") or r.get("created") or 0)}
    p = str(r.get("priority") or "").strip().lower()
    if p in ("medium", "high"):
        it["priority"] = p
    if r.get("source"):
        it["source"] = str(r["source"])[:64]
    if comments:
        it["comments"] = comments
    return it


# 1.0.138: a locus's WHOLE central slice (hs-fresh-ubuntu holds 700+ rows; the
# old 500 cap hid the oldest and made comments on them 404 "no such item").
CENTRAL_TODO_LIMIT = 5000


async def _central_todo_state(app, locus):
    rows = await _ts_call(app, "todo/list", {"locus": locus, "limit": CENTRAL_TODO_LIMIT}, timeout=15)
    items = [i for i in (_central_item(r) for r in reversed(rows or [])) if i]
    await _operator_scripts_attach(app, locus, rows or [], items)
    _operator_scripts_sync(locus, items, "central:" + locus)
    return {"schema": "todo.v1", "items": items, "locus": locus, "central": True}


async def _operator_scripts_attach(app, locus, rows, items):
    """1.0.144: a script run leaves <ts>.json + <ts>.log under
    <state>/operator-scripts/results/<id>/. Each NEW sidecar is attached to its
    item exactly once: a RESULT comment on the central note (the RAW note, so
    sections and earlier comments survive), then the registry mark — a failed
    post is retried at the next read, a posted one never repeats. The item is
    never closed here; `annotate` (in the sync) adds `result` + `flag`."""
    try:
        if not locus or locus != _keeper_locus():
            return
        pend = OPS.pending_results(FV_STATE_HOME, items)
    except Exception as e:                                   # noqa: BLE001
        logging.getLogger("station.board").warning("operator-scripts results scan skipped: %s", e)
        return
    raw = {str(r.get("id") or ""): str(r.get("note") or "") for r in rows if isinstance(r, dict)}
    for p in pend:
        note = raw.get(p["id"])
        if note is None:
            continue
        try:
            await _ts_call(app, "todo/update", {"id": p["id"], "note": note.rstrip("\n") + "\n" + p["comment"]}, timeout=15)
            raw[p["id"]] = note.rstrip("\n") + "\n" + p["comment"]
            OPS.mark_attached(FV_STATE_HOME, p["sidecar"], p["id"])
            for it in items:                                 # the response shows the comment at once
                if it.get("id") == p["id"]:
                    it["note"] = _split_note_comments(raw[p["id"]])[0]
        except Exception as e:                               # noqa: BLE001
            logging.getLogger("station.board").warning("operator-scripts result %s not attached: %s", p["sidecar"], e)


def _operator_scripts_sync(locus, items, source):
    """1.0.144: THIS locus's operator items' RUN / ROLLBACK RUN blocks live as
    files under <state>/operator-scripts/ (operator 2026-10-01: a pre-made
    script, one command from ~ on the host). Every board read reconciles the
    dir and annotates the items with `one_liner` / `rollback_one_liner` for the
    renderer; the stored text is never rewritten. Other loci's slices are only
    read here, so they get no files. Fail open: a disk error never blocks a read."""
    try:
        if locus and locus == _keeper_locus():
            OPS.sync(FV_STATE_HOME, items, source)
            OPS.annotate(FV_STATE_HOME, items)
    except Exception as e:                                   # noqa: BLE001
        logging.getLogger("station.board").warning("operator-scripts sync skipped: %s", e)


async def _central_todo_get(request, locus):
    try:
        state = await _central_todo_state(request.app, locus)
    except Exception as e:
        return web.json_response(
            {"ok": False, "error": f"{locus}: central board unreachable — {e}"}, status=502)
    return web.json_response({"ok": True, "state": state, "central": True,
                              "path": _central_todo_path(locus)})


async def _central_todo_post(request, locus):
    """The ☑ drawer's op set against the central slice: add / set / edit /
    remove / resolve / comment (UI dialect) and add / update / del (canonical)
    map onto todo/add, todo/update, todo/remove. `replace` has no central
    equivalent. Comments have no column: they append to the note."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    verb = (body.get("op") or "").strip()
    app = request.app
    tid = str(body.get("id") or "")
    try:
        if verb == "add":
            item = body.get("item") if isinstance(body.get("item"), dict) else body
            text = str(item.get("text") or "").strip()
            if not text:
                return web.json_response({"ok": False, "error": "item needs non-empty text"}, status=400)
            pr = str(item.get("priority") or "").strip().lower()
            await _ts_call(app, "todo/add", {
                "text": text[:500], "type": item.get("type") or "todo",
                "priority": pr if pr in MCT_TODO_PRIOS else None,
                "note": str(item.get("note") or ""),
                "by": str(item.get("by") or "operator")[:24],
                "source": "station:" + _station_id(), "locus": locus}, timeout=15)
        elif verb in ("set", "edit", "update"):
            f = body.get("fields") if verb == "update" else body
            f = f if isinstance(f, dict) else {}
            upd = {"id": tid}
            if str(f.get("status", "")).lower() in MCT_TODO_STATUSES:
                upd["status"] = str(f["status"]).lower()
            if "priority" in f:
                p = str(f.get("priority") or "").strip().lower()
                upd["priority"] = p if p in MCT_TODO_PRIOS else "low"
            if "text" in f and str(f.get("text") or "").strip():
                upd["text"] = str(f["text"]).strip()[:500]
            if "note" in f:
                upd["note"] = str(f.get("note") or "")
            # comments have no column on a central board — persist them IN the note
            # (the established idiom). The console POSTs the FULL comments array, so
            # rebuild note = human text + every comment re-encoded, reading the RAW
            # (uncapped) note so a long human note AND any prior comments both survive.
            if isinstance(f.get("comments"), list):
                lines = [_encode_comment(c) for c in f["comments"]
                         if isinstance(c, dict) and str(c.get("text") or "").strip()]
                if lines:
                    if "note" in upd:
                        human = _split_note_comments(upd["note"])[0]
                    else:
                        rows = await _ts_call(app, "todo/list",
                                              {"locus": locus, "limit": CENTRAL_TODO_LIMIT}, timeout=15)
                        raw = next((str(r.get("note") or "") for r in (rows or [])
                                    if str(r.get("id") or "") == tid), None)
                        if raw is None:   # never clobber a note we couldn't read back
                            return web.json_response({"ok": False, "error": "no such item"}, status=404)
                        human = _split_note_comments(raw)[0]
                    upd["note"] = (human + ("\n" if human else "")) + "\n".join(lines)
            if len(upd) == 1:
                return web.json_response({"ok": False, "error": "nothing to update"}, status=400)
            await _ts_call(app, "todo/update", upd, timeout=15)
        elif verb in ("remove", "del"):
            await _ts_call(app, "todo/remove", {"id": tid}, timeout=15)
        elif verb in ("resolve", "comment"):
            if verb == "resolve" and body.get("verdict") not in ("accepted", "declined"):
                return web.json_response({"error": "verdict must be accepted|declined"}, status=400)
            ctext = (body.get("text") or "").strip()
            if verb == "comment" and not ctext:
                return web.json_response({"error": "comment needs text"}, status=400)
            # the RAW stored note (human text + encoded comments, uncapped): a
            # resolve/comment must keep every section and every prior comment
            rows = await _ts_call(app, "todo/list", {"locus": locus, "limit": CENTRAL_TODO_LIMIT}, timeout=15)
            raw = next((str(r.get("note") or "") for r in (rows or [])
                        if str(r.get("id") or "") == tid), None)
            if raw is None:
                return web.json_response({"error": "no such item"}, status=404)
            if verb == "resolve":
                upd = {"id": tid, "status": "done", "note": "[" + body["verdict"] + "] " + raw}
            else:
                upd = {"id": tid, "note": ((raw + "\n") if raw else "")
                       + "[comment operator " + time.strftime("%Y-%m-%d %H:%M") + "] " + ctext}
            await _ts_call(app, "todo/update", upd, timeout=15)
        elif verb == "replace":
            return web.json_response(
                {"ok": False, "error": "replace is not supported on a central (toolserver) board"},
                status=400)
        else:
            return web.json_response({"ok": False, "error": f"unknown op {verb!r}"}, status=400)
        state = await _central_todo_state(app, locus)
    except RuntimeError as e:          # toolserver said no ({"error": ...})
        msg = str(e)
        return web.json_response({"ok": False, "error": msg},
                                 status=404 if msg.startswith("no todo") else 400)
    except Exception as e:
        return web.json_response(
            {"ok": False, "error": f"{locus}: central board unreachable — {e}"}, status=502)
    return web.json_response({"ok": True, "state": state, "central": True,
                              "path": _central_todo_path(locus)})


# ◳ canvas on the DB arm (operator 2026-09-03): a locus's design (wireframe.v1)
# and flow (flow.v1) documents live in the toolserver `canvas` table
# (canvas/get|put|list) — ONE copy that this drawer, every other station and
# every seat (MCP canvas_get/canvas_put) share; puts fan out on the change bus
# (table "canvas") so open drawers reload live. Replaces the retired per-locus
# sidecar files (~/wireframe.json, ~/flow.json) for ssh loci AND the host seat
# (which is a locus too — _station_locus()). The JSON contract is the one the
# drawer always spoke: GET → {ok, state, rev, path}, POST {state[, notify]}.
def _central_canvas_path(locus, kind):
    return f"{TS_UPSTREAM.rstrip('/')}/canvas/get locus={locus} kind={kind}"


async def _central_canvas_route(request, locus, kind):
    app = request.app
    if request.method == "GET":
        try:
            r = await _ts_call(app, "canvas/get", {"locus": locus, "kind": kind}, timeout=15)
        except Exception as e:
            return web.json_response(
                {"ok": False, "error": f"{locus}: central canvas unreachable — {e}"}, status=502)
        if not isinstance(r, dict) or not r.get("state"):
            return web.json_response({"ok": False, "central": True, "locus": locus,
                                      "error": f"no {kind} for {locus} yet"})
        return web.json_response({"ok": True, "central": True, "locus": locus, "vm": locus,
                                  "state": r["state"], "rev": r.get("rev", 0),
                                  "by": r.get("by", ""), "mtime": r.get("updated", 0),
                                  "path": _central_canvas_path(locus, kind)})
    if request.method != "POST":
        raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
    try:
        body = await request.json()
    except Exception:
        body = {}
    st = (body or {}).get("state")
    if not isinstance(st, dict):
        return web.json_response({"error": f"body must carry a {kind} state object"}, status=400)
    try:
        r = await _ts_call(app, "canvas/put", {
            "locus": locus, "kind": kind, "state": st,
            "by": str(body.get("by") or "operator")[:40],
            "note": str(body.get("note") or "")[:500],
            "notify": bool(body.get("notify"))}, timeout=20)
    except Exception as e:
        msg = str(e)
        status = 400 if "rejected" in msg else 502
        return web.json_response({"ok": False, "error": msg}, status=status)
    audit(request, kind + "_push@" + locus,
          f"rev {r.get('rev')}" + (" +notify" if body.get("notify") else ""), ok=True)
    return web.json_response({"ok": True, "central": True, "locus": locus, "vm": locus,
                              "rev": r.get("rev", 0), "changed": r.get("changed"),
                              "notified": r.get("notified"), "mtime": r.get("updated", 0),
                              "state": r.get("state"),
                              "path": _central_canvas_path(locus, kind)})


async def _central_todo_route(request, locus):
    if request.method == "GET":
        return await _central_todo_get(request, locus)
    if request.method == "POST":
        return await _central_todo_post(request, locus)
    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])


_PING_FROM_RE = re.compile(r"^\[ping\](?:\[msg\])?\s*(?:from\s+)?(\S+)")


async def _central_messages(request, locus):
    """GET /api/vm/<locus>/messages for a non-host locus (1.0.138): its ✉ inbox
    is the central comms lane — comms/inbox to=<locus> (the [ping] rows every
    comms_ping/fleet message files on that locus's board) — mapped onto the
    bugreport-api {ok, messages:[{id, ts, from, to, text}]} shape, oldest first."""
    if request.method != "GET":
        raise web.HTTPMethodNotAllowed(request.method, ["GET"])
    try:
        doc = await _ts_call(request.app, "comms/inbox", {"to": locus}, timeout=15)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"{locus}: comms inbox unreachable — {e}"},
                                 status=502)
    rows = []
    for p in (doc.get("pings") if isinstance(doc, dict) else None) or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        m = _PING_FROM_RE.match(str(p.get("note") or ""))
        rows.append({"id": str(p["id"]), "ts": int(p.get("created") or p.get("updated") or 0),
                     "from": (m.group(1) if m else "") or str(p.get("by") or "board"),
                     "to": locus, "text": str(p.get("text") or ""), "status": p.get("status"),
                     "via": "board"})
    rows.sort(key=lambda r: r["ts"])
    return web.json_response({"ok": True, "central": True, "locus": locus,
                              "messages": rows[-FLEET_MAIL_MAX:]})


async def _central_todo_brief(request, locus):
    """POST /api/vm/<locus>/todo/brief for a non-host locus (1.0.138): the brief
    + a snapshot of the locus's OPEN central items ride the comms lane
    (comms/ping to=<locus>), which the locus's keeper sees in its inbox — no
    lxc exec / ~/todo.json / live pane on the locus required."""
    try:
        body = await request.json()
        text = " ".join(str(body.get("text") or "").split())
        if not text:
            raise ValueError
    except Exception:
        return web.json_response({"error": 'body must be {"text": "<brief>"}'}, status=400)
    try:
        state = await _central_todo_state(request.app, locus)
        summary = _todo_brief_summary(json.dumps({"items": state["items"]}))
        res = await _ts_call(request.app, "comms/ping", {
            "to": locus, "from_": _keeper_locus() or getpass.getuser(), "kind": "message",
            "text": ("☑ board brief from the operator's console: " + text[:FLEET_MSG_MAX]
                     + " " + summary + " Please act on the open items on your board."),
        }, timeout=15)
    except Exception as e:
        audit(request, "todo-brief", f"locus={locus} via=board failed: {e}", ok=False)
        return web.json_response({"ok": False, "error": f"{locus}: board ping failed — {e}"}, status=502)
    audit(request, "todo-brief", f"locus={locus} via=board", ok=True)
    return web.json_response({"ok": True, "pinged": False, "via": "board", "locus": locus,
                              "id": (res or {}).get("id") if isinstance(res, dict) else None,
                              "open": sum(1 for i in state["items"]
                                          if i.get("status") in ("open", "doing"))})


async def _central_todo_ping(request, locus):
    """📨 per-item ping for a non-host locus (1.0.138): reads the item from the
    locus's central rows and files ONE comms ping on that locus."""
    try:
        body = await request.json()
        tid = str(body.get("id") or "").strip()
        if not tid:
            raise ValueError
    except Exception:
        return web.json_response({"error": 'body must be {"id"}'}, status=400)
    try:
        items = (await _central_todo_state(request.app, locus))["items"]
    except Exception as e:
        return web.json_response({"ok": False, "error": f"{locus}: central board unreachable — {e}"},
                                 status=502)
    it = next((i for i in items if i.get("id") == tid), None)
    if not it:
        return web.json_response({"ok": False, "error": f"no board item {tid}"}, status=404)
    extra = " ".join(str(body.get("text") or "").split())[:200]
    line = (f"📨 board {tid} [{it.get('status') or 'open'}/{it.get('priority') or 'low'}] from the operator: "
            + " ".join(str(it.get("text") or "").split())[:300]
            + (f" — {extra}" if extra else "")
            + " — please tend to it and update the item on the board.")
    try:
        res = await _ts_call(request.app, "comms/ping", {
            "to": locus, "from_": _keeper_locus() or getpass.getuser(), "kind": "message", "text": line}, timeout=15)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"{locus}: board ping failed — {e}"}, status=502)
    _audit_line("todo-ping", f"{tid} → {locus} (board)")
    return web.json_response({"ok": True, "id": tid, "via": "board", "locus": locus,
                              "ping": (res or {}).get("id") if isinstance(res, dict) else None})


async def _sidecar_proxy(request):
    """None = host-native (answer from this station's own files); else the
    locus's drawer is a toolserver concern — 404 until wired (see _retired)."""
    vm = await _sidecar_vm(request)
    if not vm:
        return None
    return _retired(request.path, vm)


def _b_answer_in_vm(text, history, vm=""):
    vm = vm or MODEL_VM   # the SPA's active VM wins; static pin is the fallback
    if not vm:            # workbench mode — the local grounding IS correct
        raise RuntimeError("no model VM: ground locally")
    payload = json.dumps({"workspace": f"{VM_MCT_ROOT}/{_active_session()}",
                          "text": text, "history": (history or [])[-20:]})
    r = subprocess.run(
        ["lxc", "exec", vm, "--user", SH_UID, "--group", SH_GID,
         "--env", f"HOME={SH_HOME}", "--",
         "python3", "-m", "hugpy_agent.mct.b_answer"],
        input=payload.encode(), capture_output=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[-400:]
                           or f"b_answer rc={r.returncode}")
    out = json.loads(r.stdout.decode())
    if not (isinstance(out, dict) and out.get("reply")):
        raise RuntimeError("b_answer returned no reply")
    return {"reply": out["reply"], "offline": bool(out.get("offline"))}


_HUGPY_PY = ""     # cached interpreter that can import hugpy_agent


def _hugpy_python():
    """A python that can import hugpy_agent: the server's own (dev installs
    where server.py runs under it), else the one the hugpy-agent CLI is
    shebanged with (the real install — e.g. miniconda, while the packaged
    backend runs the system python). '' when neither works. hugpy_agent IS B;
    the host must reach it the same one-shot way a VM does, whatever
    interpreter it happens to live in."""
    global _HUGPY_PY
    if _HUGPY_PY:
        return _HUGPY_PY
    cands = [sys.executable]
    cli = shutil.which("hugpy-agent")
    if cli:
        try:
            line1 = open(cli, encoding="utf-8", errors="replace").readline().strip()
            if line1.startswith("#!"):
                cands.append(line1[2:].split()[0])
        except OSError:
            pass
    for py in cands:
        try:
            r = subprocess.run([py, "-c", "import hugpy_agent.mct.b_answer"],
                               capture_output=True, timeout=20)
        except Exception:
            continue
        if r.returncode == 0:
            _HUGPY_PY = py
            return py
    return ""


# --- Lookup-first guard for the in-process fallback (operator, 2026-09-29):
# mirrors hugpy_agent.mct.b_lookup, which the one-shot paths above already
# run. An ack / help / state question, or ANY message against an EMPTY state
# (no policy, empty catalog, no derived memory), is answered from the state
# text with the same "(ts · tokens 0 · dur · lookup)" metadata line — the
# fleet model is never spent on a string lookup. Anything else keeps the
# gateway path below unchanged.
_B_LOOKUP_MODEL = "lookup"
_B_ACK_WORDS = frozenset((
    "ok", "okay", "k", "kk", "thanks", "thank", "thx", "ty", "got", "it", "cool",
    "great", "nice", "noted", "ack", "yes", "no", "sure", "alright", "roger",
    "cheers", "you", "fine", "good", "perfect", "done", "right", "yep", "yup", "nope"))
_B_HELP_RE = re.compile(
    r"^\s*/?help\b|^\s*(commands|usage)\s*\??\s*$|what can you do|how do i (use|talk|ask)"
    r"|what (commands|do you (support|accept))", re.I)
_B_STATE_BARE_RE = re.compile(
    r"^\s*(status|state|what do you have|what do you know|what have you got|what is your state"
    r"|what'?s your state|readout|overview|summary of (your )?state|/?bstate)\s*[?.!]*\s*$", re.I)
_B_STATE_FIELD_RE = re.compile(
    r"\b(policy|policies|governing instruction|catalog|catalogue|sources?|entries|files"
    r"|objects|pullable|memory|memories|facts?|decisions?|derived|remember|learned|tokens?"
    r"|cost|spend|spent|usage|budget|logs?|rolling log|ledger|events?|event log|history"
    r"|last turn|turn|previous turn|last answer)\b", re.I)
_B_STATE_ASK_RE = re.compile(
    r"\b(what|which|show|list|print|dump|display|tell me|how many|how much|do you have"
    r"|have you|is there|are there|any|give me|report|read out|readout|status|state"
    r"|whats|what's|contents?|current)\b", re.I)
_B_HELP_TEXT = (
    "I answer from my own state without inference: ask what is in the catalog / "
    "policy / memory / ledger / tokens / log, or 'status'. Only a request that "
    "needs synthesis over non-empty state (summarize, judge, rewrite) goes to "
    "the fleet model. In the terminal: /bstate shows the state, /policy sets "
    "the governing instruction, /root + /file or /source populate the catalog, "
    "/memory lists derived facts, /tokens the spend, /tail the rolling log.")
_B_EMPTY_TEXT = (
    "My state is empty — no policy, no catalog entries, no derived memory. A "
    "policy is set with /policy <text>; the catalog fills when sources are "
    "registered (/root + /file, /source) or A pulls through me; derived memory "
    "is written by compaction after A's turns.")


def _b_meta_line(model, tokens, started):
    """The operator's per-reply line: (timestamp · tokens N · duration · model)."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return (f"({ts} · tokens {int(tokens)} · "
            f"{max(0.0, time.monotonic() - started):.3f}s · {model})")


def _b_state_empty(sess, srv):
    """True when B has nothing to ground on: no policy, empty catalog, no facts.
    Same three fields hugpy_agent.mct.b_lookup.BState.empty checks."""
    if getattr(sess, "_policy_text", None) or getattr(sess, "_policy_pointer", None):
        return False
    try:
        sess._materialize_file_sources()
    except Exception:
        pass
    if getattr(sess, "_catalog", None):
        return False
    try:
        if srv.compaction.facts(sess.session_id):
            return False
    except Exception:
        pass
    return True


def _b_lookup_kind(text):
    """'ack' | 'help' | 'state' | None (None = needs synthesis). Deterministic,
    no I/O — the same shape as b_lookup.classify minus search/meta."""
    t = (text or "").strip()
    words = re.findall(r"[a-z']+", t.lower())
    if not words:
        return "ack"
    if len(words) <= 4 and "?" not in t and all(w in _B_ACK_WORDS for w in words):
        return "ack"
    if _B_HELP_RE.search(t):
        return "help"
    if _B_STATE_BARE_RE.match(t) or (_B_STATE_FIELD_RE.search(t) and _B_STATE_ASK_RE.search(t)):
        return "state"
    return None


def _b_fallback_lookup(text, ground, empty, started):
    """The deterministic reply (metadata line attached) for an ack / help /
    state question or an empty state; None when the message needs the model."""
    kind = _b_lookup_kind(text)
    if kind == "ack":
        body = "Noted."
    elif kind == "help":
        body = _B_HELP_TEXT
    elif empty:
        body = _B_EMPTY_TEXT
    elif kind == "state":
        body = ground
    else:
        return None
    return f"{body.rstrip()}\n{_b_meta_line(_B_LOOKUP_MODEL, 0, started)}"


def _b_answer(text, history, vm=""):
    if vm in ("", "@keeper") and _b_findings_ask(text):
        # "what's broken" is a lookup over this station's findings (1.0.124)
        return {"reply": _b_findings_reply(_b_findings_rows(), time.monotonic()),
                "offline": False, "model": _B_LOOKUP_MODEL, "tokens": 0}
    try:
        if vm != "@keeper":       # the host keeper seat NEVER defers to the pin
            return _b_answer_in_vm(text, history, vm)
    except Exception:
        pass                      # VM path unavailable — host-side fallback
    # Host one-shot, symmetric with the VM path: exec hugpy_agent.mct.b_answer
    # under whatever interpreter owns hugpy_agent, because the server's own
    # python (packaged installs) may not have the module at all.
    py = _hugpy_python()
    if py:
        # 1.0.143: "brief" = B's own per-session directive (the `local` kind).
        # hugpy_agent.mct.b_answer does not read it yet (it composes its own
        # B_SYSMSG); the in-process fallback below does.
        payload = json.dumps({"workspace": str(_active_ws()), "text": text,
                              "history": (history or [])[-20:], "brief": _b_local_brief()})
        try:
            r = subprocess.run([py, "-m", "hugpy_agent.mct.b_answer"],
                               input=payload.encode(), capture_output=True,
                               timeout=180)
            if r.returncode == 0:
                out = json.loads(r.stdout.decode())
                if isinstance(out, dict) and out.get("reply"):
                    return {"reply": out["reply"],
                            "offline": bool(out.get("offline"))}
        except Exception:
            pass                  # fall through to in-process grounding
    from hugpy_agent.mct.repl import _b_state_text
    started = time.monotonic()
    srv = _console_b_server()
    sess = srv.session(_latest_session_id(srv))
    ground = _b_state_text(sess, srv, {"last": None})
    hit = _b_fallback_lookup(text, ground, _b_state_empty(sess, srv), started)
    if hit is not None:           # lookup answered — no model call
        return {"reply": hit, "offline": False, "model": _B_LOOKUP_MODEL, "tokens": 0}
    brief = _b_local_brief()
    msgs = [{"role": "system", "content": (brief + "\n\n" if brief else "") + _B_SYSMSG + ground}]
    for m in (history or [])[-20:]:
        r, c = m.get("role"), m.get("content")
        if r in ("user", "assistant") and isinstance(c, str):
            msgs.append({"role": r, "content": c})
    msgs.append({"role": "user", "content": text})
    try:
        from hugpy_agent.config import load_config
        from hugpy_agent.gateway import Gateway
        gw = Gateway.from_config(load_config())
        res = gw.chat(msgs, max_tokens=700)
        reply = getattr(res, "text", None) or getattr(res, "content", None) or str(res)
        _meter_tokens("b_chat", int(getattr(res, "est_tokens", 0) or 0)
                      or (sum(len(m.get("content") or "") for m in msgs) + len(reply or "")) // 4)
        return {"reply": (reply or "").strip() or "(no reply)", "offline": False}
    except Exception as exc:
        return {"reply": "[B offline - deterministic state readout]\n"
                         f"(gateway unavailable: {type(exc).__name__}: {exc})\n\n" + ground,
                "offline": True}


async def _b_answer_remote(vm, text, history):
    """B on a remote locus: its own hugpy_agent.mct.b_answer, run ON the
    target (payload on stdin), grounded in ITS station workspace."""
    payload = base64.b64encode(json.dumps({"workspace": "", "text": text,
                                           "history": (history or [])[-20:]}).encode()).decode()
    script = (LX.STATE_SH
              + 'export B_WS="$S/mct/repl"; export PATH="$HOME/.local/bin:$PATH"\n'
              + 'py=""; hb="$(command -v hugpy-agent 2>/dev/null)"; '
              + 'for c in "$( [ -n "$hb" ] && sed -n \'1s/^#! *//p\' "$hb")" "$S/serve-venv/bin/python" python3; do '
              + '[ -n "$c" ] && "$c" -c "import hugpy_agent" 2>/dev/null && { py="$c"; break; }; done\n'
              + '[ -n "$py" ] || { echo "hugpy_agent (B) is not installed for $(id -un)@$(hostname)" >&2; exit 3; }\n'
              + "base64 -d <<'__B141__' | \"$py\" -c 'import json,os,sys; d=json.load(sys.stdin); "
              + "d[\"workspace\"]=os.environ[\"B_WS\"]; print(json.dumps(d))' | exec \"$py\" -m hugpy_agent.mct.b_answer\n"
              + "\n".join(payload[i:i + 76] for i in range(0, len(payload), 76)) + "\n__B141__\n")
    rc, out, err = await LX.run(_locus_target(vm), script, timeout=200)
    try:
        d = json.loads((out or "").strip().splitlines()[-1])
        if isinstance(d, dict) and d.get("reply"):
            return {"reply": d["reply"], "offline": bool(d.get("offline")), "grounded_in": vm}
    except Exception:                                    # noqa: BLE001
        pass
    return {"error": f"B on {vm} unavailable: " + ((err or out or f"rc={rc}").strip()[-300:])}


async def b_chat(request):
    """POST /api/b/chat {"text": "...", "history"?: [{role,content}...],
    "vm"?: "<name>"} - a direct, grounded conversation with B. With "vm" (the
    SPA's active VM) the question goes to the B seated IN that VM (grounding
    rule); otherwise the static model-VM pin, else the host's own B. Returns
    {"reply", "offline"}."""
    try:
        body = await request.json()
        text = (body.get("text") or "").strip()
        history = body.get("history") or []
        if not text:
            raise ValueError
    except Exception:
        return web.json_response(
            {"error": 'body must be {"text": non-empty string, "history"?: [...]}'}, status=400)
    if not _b_enabled():
        return web.json_response({
            "reply": "[B is switched off — flip the B chip in the terminal "
                     "bar (or POST /api/term/bgate) to re-enable]",
            "offline": True})
    # 1.0.144: ?vm= too — a locus console's B pane (framed at /ac/@<locus>/)
    # gets it from locus_vm_mw via its Referer; the body wins when both are set.
    vm = str(body.get("vm") or request.query.get("vm") or "").strip()
    if vm and _is_host_vm(vm):
        vm = "@keeper"
    elif vm and _ssh_host(vm) and _locus_target(vm).remote:
        # 1.0.141: an ssh locus is answered by THAT locus's own B, run on the
        # target through the one locus transport — never this station's B
        # grounded on this host's files (the old silent host leak).
        res = await _b_answer_remote(vm, text, history)
        if res.get("error"):
            return web.json_response({"ok": False, "locus": vm, **res}, status=502)
        return web.json_response({"ok": True, "locus": vm, **res})
    elif vm and vm not in await _known_names_safe():
        return web.json_response({"ok": False, "error": f"unknown locus {vm}"}, status=404)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _b_answer, text, history, vm)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)
    return web.json_response({"ok": True, **result})


# --- Host-native session todo (todo.v1) — the CANONICAL vm_mgr todo mechanism,
# ported from console-api (srv/vm_mgr/console/console-api) with its hardening
# intact: flock on <ws>/.todo.lock (10s wait) around fresh-read→apply→write,
# atomic tmp+mv, sequential t<n> ids that never collide with an existing id
# (o10), ambiguous-id refusal on update/del (o10), id-preserving replace (o15),
# todo_norm field limits, and read-never-clobbers (an unparseable file is an
# ERROR, not a blank board). The board lives in the CURRENT MCT workspace
# (todo.json), shared with the agent — the file IS the interface. -------------
MCT_TODO_STATUSES = ("open", "doing", "done")
# `finding` (2026-10-01): a bug report owned by the locus's WORKER session — the
# keeper's reminder digest / nudges exclude it (bug_route.BOARD_TYPE).
MCT_TODO_TYPES = ("todo", "request", "bookmark", "operator", "proposal", "direction", "finding")
MCT_TODO_PRIOS = ("low", "medium", "high")
TODO_TYPES = set(MCT_TODO_TYPES)
TODO_STATUS = set(MCT_TODO_STATUSES)
TODO_MAX_ITEMS = 500


def _todo_history_path():
    return Path(_hugpy_path("state", "todo-history.jsonl", "todo-history.jsonl"))


def _todo_hist(entry):
    try:
        p = _todo_history_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), **entry}) + "\n")
    except OSError:
        pass


def _mct_todo_path():
    return Path(_hugpy_path("state", "todo.json", "todo.json"))


@contextlib.contextmanager
def _todo_locked():
    """Host-native port of console-api's `flock -w 10 .todo.lock` (o13): one
    shared lock next to the board serializes every writer — console, agent's
    MCP tool, and any CLI — at file granularity."""
    ws = _active_ws()
    ws.mkdir(parents=True, exist_ok=True)
    fh = open(ws / ".todo.lock", "a+")
    try:
        deadline = time.time() + 10
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise OSError("todo lock: timed out after 10s")
                time.sleep(0.2)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _mct_todo_read():
    """Missing file -> blank list; unparseable file -> error (never clobber
    something the keeper wrote by treating it as blank)."""
    p = _mct_todo_path()
    if not p.exists():
        return {"schema": "todo.v1", "items": []}, ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ValueError("no items list")
        return {"schema": "todo.v1", "items": items}, ""
    except (OSError, ValueError):
        return None, f"{p} is not valid todo JSON — fix or remove it"


def _mct_todo_write(state):
    # o10 v4: atomic replace — a concurrent reader never sees a torn board
    p = _mct_todo_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


# ── toolserver → file board sync (2026-09-15) ────────────────────────────────
# The keeper board UI is file-primary (t186: /api/vm/keeper/todo always reads
# _mct_todo_path(), never the toolserver — see _sidecar_vm/_central_locus,
# "the host keeper's own board/aliases stay host"). board-mirror@<locus> only
# runs FILE -> central `board` table, one-way, read-only on the file by design
# ("truth stays local; a keeper edit never blocks on the DB"). Nothing ever
# ran the other direction, so an item created straight in the toolserver
# `todos` table (todo_add/todo_update over MCP, or comms_ping) never reached
# the local file the UI reads — it was invisible to the operator no matter
# how long they waited. This adds the missing reverse leg, symmetric with
# board-mirror's guarantees: read-only on the toolserver, ADD-ONLY on the file
# (never edits/removes an item already there — a local edit always wins),
# fail-open (no toolserver, no locus, bad response -> idle, UI keeps working
# off the file). Pulls this station's own locus (_station_locus()) plus the
# fleet-wide '' locus so cross-locus items (t2..t246-style) land too.
_TODO_TS_SYNC_POLL = int(os.environ.get("STATION_TODO_TS_SYNC_POLL", "30") or 30)
_tslog = logging.getLogger("station.todo-ts-sync")


async def _todo_ts_sync_once(app):
    # board-sot 2026-09-18: was _station_locus(), which returns '' on the host
    # keeper (STATION_LOCUS unset) and made this whole DB->file reverse sync
    # early-return every tick — the reason central `todos` items never reached
    # the file the UI reads. 1.0.141: _keeper_locus() is this station's OWN locus;
    # '' (not configured) means no sync at all — never the `keeper` slice.
    locus = _keeper_locus()
    if not locus:
        return
    rows = []
    for loc in {locus, ""}:
        try:
            rows.extend(await _ts_call(app, "todo/list", {"locus": loc, "limit": 500}, timeout=15))
        except Exception as e:
            _tslog.warning("todo/list locus=%r: %s", loc, str(e)[:200])
    items = [i for i in (_central_item(r) for r in rows) if i]
    if not items:
        return
    with _todo_locked():
        state, err = _mct_todo_read()
        if state is None:
            _tslog.warning("skip merge: %s", err)
            return
        have = {it.get("id"): it for it in state["items"] if isinstance(it, dict)}
        added = [it for it in items if it.get("id") and it["id"] not in have]
        # 1.0.97 (t338): mirror central EDITS too, not just new rows. The
        # central board is where the keeper closes/annotates items, so a
        # local copy that only ever gained rows kept showing done items as
        # open — and B's reminder cycle (which reads this file) kept pushing
        # them. Central-owned fields follow the newer central row; local-only
        # state (B comments, watch counters, flag) is preserved.
        updated = 0
        for it in items:
            loc = have.get(it.get("id"))
            if loc is None or int(it.get("ts") or 0) <= int(loc.get("ts") or 0):
                continue
            for k in ("type", "text", "note", "status", "by", "ts", "source"):
                if k in it:
                    loc[k] = it[k]
            if it.get("priority"):
                loc["priority"] = it["priority"]
            else:
                loc.pop("priority", None)
            if it.get("comments") and not loc.get("comments"):
                loc["comments"] = it["comments"]
            updated += 1
        if not added and not updated:
            return
        state["items"] = state["items"] + added
        _mct_todo_write(state)
    _tslog.info("merged %d new + %d updated toolserver item(s) into %s",
                len(added), updated, _mct_todo_path())


async def _todo_ts_sync_loop(app):
    await asyncio.sleep(5)   # let the station finish startup before the first pull
    while True:
        try:
            await _todo_ts_sync_once(app)
        except Exception as e:
            _tslog.warning("sync tick failed: %s", str(e)[:200])
        await asyncio.sleep(_TODO_TS_SYNC_POLL)


async def _start_todo_ts_sync(app):
    if os.environ.get("STATION_DEV_QUIET") == "1":   # 1.0.144: a dev copy never syncs the live board file
        return
    app["todo_ts_sync"] = asyncio.create_task(_todo_ts_sync_loop(app))


async def _stop_todo_ts_sync(app):
    t = app.get("todo_ts_sync")
    if t:
        t.cancel()


# Board ids carry the TYPE's first letter (t todo, r request, b bookmark, o operator,
# p proposal, d direction, m message — operator 2026-09-16, "makes my sifting
# streamlined"). The number is the identity; the letter is presentation.
TODO_TYPE_LETTER = {"todo": "t", "request": "r", "bookmark": "b", "operator": "o",
                    "proposal": "p", "direction": "d", "message": "m"}


def todo_type_letter(typ):
    return TODO_TYPE_LETTER.get(str(typ or "").strip().lower(), "t")


def todo_next_id(items):
    n = 0
    for it in items:
        m = re.match(r"^[a-z]*(\d+)$", str(it.get("id", "")))
        if m:
            n = max(n, int(m.group(1)))
    return n + 1


def todo_norm(raw, by, nid):
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text", "")).strip()[:500]
    if not text:
        return None
    typ = str(raw.get("type", "todo")).strip().lower()
    status = str(raw.get("status", "open")).strip().lower()
    typ = typ if typ in TODO_TYPES else "todo"
    return {"id": f"{todo_type_letter(typ)}{nid}",
            "type": typ,
            "text": text,
            "note": str(raw.get("note", "") or "").strip(),     # never truncated (BOARD-ITEM-FORMAT.md)
            **({"priority": str(raw.get("priority")).strip().lower()}
               if str(raw.get("priority", "")).strip().lower() in ("medium", "high")
               else {}),
            "status": status if status in TODO_STATUS else "open",
            "by": str(raw.get("by") or by)[:24],
            "ts": int(time.time())}


def todo_apply(state, body):
    """Apply one op ({op:add|update|del|replace, ...}) to the state, in place."""
    items = state["items"]
    op = body.get("op")
    if op == "add":
        it = todo_norm(body.get("item") or {}, "user", todo_next_id(items))
        if not it:
            return None, "item needs non-empty text"
        if len(items) >= TODO_MAX_ITEMS:
            return None, f"list is full (>{TODO_MAX_ITEMS})"
        # o10: never mint an id any existing item already holds (any type/prefix)
        _o10_ids = {str(x.get("id")) for x in items if isinstance(x, dict)}
        while str(it.get("id")) in _o10_ids:
            _o10_m = re.match(r"^([A-Za-z]+)(\d+)$", str(it.get("id")))
            it["id"] = (_o10_m.group(1) + str(int(_o10_m.group(2)) + 1)) if _o10_m else (str(it.get("id")) + "x")
        items.append(it)
        return state, ""
    if op == "update":
        # o10 ambiguity (update): a shared id means by-id ops hit the wrong twin — refuse
        _o10_tid = str(body.get("id", ""))
        _o10_twins = [_x for _x in items if isinstance(_x, dict) and str(_x.get("id")) == _o10_tid]
        if len(_o10_twins) > 1:
            return None, f"ambiguous id {_o10_tid!r}: " + str(len(_o10_twins)) + " items share it - re-id required (o10)"
        it = next((i for i in items if i.get("id") == body.get("id")), None)
        if not it:
            return None, f"no item {body.get('id')}"
        f = body.get("fields") or {}
        if "text" in f and str(f["text"]).strip():
            it["text"] = str(f["text"]).strip()[:500]
        if "note" in f:
            it["note"] = str(f["note"] or "").strip()[:20000]   # 2026-10-01: operator RUN scripts exceed 2k; mirrored notes must never be clipped (t4221)
        if str(f.get("priority", "")).strip().lower() in ("low", "medium", "high"):
            p = str(f["priority"]).strip().lower()
            if p == "low":
                it.pop("priority", None)
            else:
                it["priority"] = p
        if str(f.get("status", "")).lower() in TODO_STATUS:
            it["status"] = str(f["status"]).lower()
        if str(f.get("type", "")).lower() in TODO_TYPES:
            it["type"] = str(f["type"]).lower()
        if isinstance(f.get("comments"), list):
            clean = []
            for c in f["comments"][:50]:
                if not isinstance(c, dict):
                    continue
                ctext = str(c.get("text", "")).strip()[:8000]
                if not ctext:
                    continue
                cts = c.get("ts")
                clean.append({"by": str(c.get("by") or "operator")[:24],
                              "ts": int(cts) if isinstance(cts, (int, float)) else int(time.time()),
                              "text": ctext})
            it["comments"] = clean
        if "flag" in f:
            fl = f.get("flag")
            if isinstance(fl, dict) and fl.get("status") in TODO_FLAGS:
                it["flag"] = {"status": fl["status"], "reason": str(fl.get("reason") or "")[:120],
                              "detail": str(fl.get("detail") or "")[:400],
                              "by": str(fl.get("by") or body.get("by") or "operator")[:24],
                              "ts": int(time.time())}
            elif fl is None:
                it.pop("flag", None)
        if "query" in f:
            q = str(f.get("query") or "").strip()[:500]
            if q:
                it["query"] = q
            else:
                it.pop("query", None)
        it["ts"] = int(time.time())
        return state, ""
    if op == "del":
        # o10 ambiguity (del): a shared id would silently delete BOTH twins - refuse
        _o10_twins = [i for i in items if i.get("id") == body.get("id")]
        if len(_o10_twins) > 1:
            return None, f"ambiguous id {body.get('id')!r}: {len(_o10_twins)} items share it - re-id required (o10)"
        n = len(items)
        state["items"] = [i for i in items if i.get("id") != body.get("id")]
        return (state, "") if len(state["items"]) < n else (None, f"no item {body.get('id')}")
    if op == "replace":
        new = body.get("items")
        if not isinstance(new, list) or len(new) > TODO_MAX_ITEMS:
            return None, "replace needs items: [...]"
        out, nid = [], todo_next_id(new)   # keep survivors' ids; re-id the rest
        # o15: count incoming ids ONCE — only a non-empty id that is UNIQUE
        # within the incoming list is kept verbatim
        _o15_counts = {}
        for _o15_raw in new:
            if isinstance(_o15_raw, dict):
                _o15_k = str(_o15_raw.get("id", "") or "").strip()
                if _o15_k:
                    _o15_counts[_o15_k] = _o15_counts.get(_o15_k, 0) + 1
        for raw in new:
            it = todo_norm(raw, str(raw.get("by") or "user") if isinstance(raw, dict) else "user", 0)
            if not it:
                continue
            _o15_id = str(raw.get("id", "") or "").strip() if isinstance(raw, dict) else ""
            keep = bool(_o15_id) and _o15_counts.get(_o15_id, 0) == 1
            if keep:
                it["id"] = _o15_id
                it["ts"] = int(raw.get("ts") or it["ts"])
            else:
                it["id"] = f"{todo_type_letter(it.get('type'))}{nid}"
                nid += 1
            # o10: ids stay unique within the replaced list too
            _o10_seen = {str(x.get("id")) for x in out}
            while str(it.get("id")) in _o10_seen:
                _o10_m = re.match(r"^([A-Za-z]+)(\d+)$", str(it.get("id")))
                it["id"] = (_o10_m.group(1) + str(int(_o10_m.group(2)) + 1)) if _o10_m else (str(it.get("id")) + "x")
            out.append(it)
        state["items"] = out
        return state, ""
    return None, f"unknown op {op!r}"


# --- Locus finder (POST /api/mct/finder). The search tab enumerates the ACTIVE
# LOCUS — the host the seats ground in — never the operator's own client. The
# pre-1.0.90 route was host-native (os.walk on whichever machine ran this
# backend, i.e. the desktop client's own disk). body.vm is the wire locus and
# resolves like a seat launch: "@keeper"/"host" = this station's own host; a
# name = lxd guest or ssh host; "" = MODEL_VM. Transport mirrors _in_vm /
# handoff_spawn: local-self -> bash here; ssh locus -> ssh via its ssh-host
# entry (_ssh_argv); lxd guest -> lxc exec as ubuntu. The locus side is one
# bash script over find/grep/cat (nothing to install there). Roots must sit
# under the seat user's $HOME (the dir seats cd into); output is bounded by
# entries / depth / bytes / time. Result shape is unchanged for the drawer. ----
FINDER_MAX_ENTRIES = 2000        # rows per answer (the drawer shows SEARCH_CAP=500)
FINDER_MAX_DEPTH = 12            # find -maxdepth ceiling
FINDER_MAX_BYTES = 1_500_000     # stdout cap (head -c); also the view-file cap
FINDER_TIMEOUT = 60              # s, the ssh/lxc hop included
_FINDER_TOK_RE = re.compile(r"^[^\x00/\\]{1,64}$")   # ext / dir-name / glob tokens
_FINDER_LOCUS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _finder_roots(body):
    """body.roots -> [abs path] (1-8, absolute, no NUL, no '..' hops). Only
    roots[0] is walked; the locus resolves it and fences it under $HOME."""
    roots = body.get("roots")
    if not isinstance(roots, list) or not (1 <= len(roots) <= 8):
        return None
    out = []
    for r in roots:
        if (not isinstance(r, str) or "\x00" in r or not r.startswith("/")
                or ".." in r.split("/")):
            return None
        out.append(os.path.normpath(r))
    return out


async def _finder_ground(req_vm):
    """Wire locus -> (ground, err); '' = this station's own host. Same rule as
    api_seat_status / the terminal launch. The operator's own client (`op`,
    kind=session, board-only) is never a locus name, so it can never be listed."""
    v = (req_vm or "").strip()
    if v and _is_host_vm(v):
        return "", ""
    v = v or MODEL_VM
    if not v:
        return "", ""
    if not _FINDER_LOCUS_RE.match(v):
        return None, "bad locus name"
    if v not in await all_locus_names():
        return None, f"{v} is not a searchable locus (an lxd guest, an ssh host, or this host)"
    return v, ""


def _finder_kind(ground):
    return "host" if not ground else ("ssh" if _ssh_host(ground) else "lxd")


async def _finder_exec(ground, script, timeout=FINDER_TIMEOUT):
    """Run the finder script in the locus as its seat user -> (rc, out, err).
    1.0.141: the one locus transport (locus_exec): self (this host, or an
    ssh-host entry naming THIS station's own user@host), ssh (stdin over the
    entry's key/port/mux, BatchMode), lxd (lxc exec as ubuntu)."""
    rc, out, err = await LX.run(_locus_target(ground), script + "\n", timeout=timeout)
    if rc == 124:
        err = f"finder timed out after {timeout}s"
    return rc, out, err


def _finder_script(op, body, root):
    """The locus-side bash: resolve + fence the root under $HOME, then ONE
    bounded find / grep / cat pipeline. stdout = home NL root NL data."""
    q = shlex.quote
    toks = lambda key: [t for t in _fv_list(body, key)[:16] if _FINDER_TOK_RE.match(t)]
    hidden = bool(body.get("no_add"))           # raw: keep dot-entries
    depth = 1 if body.get("no_recursive") else _fv_int(body, "maxdepth")
    depth = FINDER_MAX_DEPTH if depth is None or depth < 1 else min(depth, FINDER_MAX_DEPTH)
    n = FINDER_MAX_ENTRIES
    lim = _fv_int(body, "limit")
    if lim and 0 < lim < n:
        n = lim
    cap = f" | head -c {FINDER_MAX_BYTES}"
    pre = ("H=$(cd ~ 2>/dev/null && pwd -P) || { echo 'no home in this locus' >&2; exit 90; }\n"
           + ("printf '%s\\n' \"$H\"; exit 0\n" if op == "home" else "")
           + f"P={q(root)}\n"
           "R=$(realpath -e -- \"$P\" 2>/dev/null) || R=$(readlink -f -- \"$P\" 2>/dev/null) || R=\"$P\"\n"
           "[ -e \"$R\" ] || { echo \"$P does not exist in this locus\" >&2; exit 91; }\n"
           "case \"$R\" in \"$H\"|\"$H\"/*) ;; *) echo \"$P is outside the locus root $H\" >&2; exit 92;; esac\n"
           "printf '%s\\n%s\\n' \"$H\" \"$R\"\n")
    if op == "home":
        return pre
    if op == "view":
        return pre + ("[ -f \"$R\" ] || { echo \"$P is not a regular file\" >&2; exit 93; }\n"
                      "wc -l < \"$R\"\n"
                      f"head -c {FINDER_MAX_BYTES} -- \"$R\"\nexit 0\n")
    if op == "dirs":                            # depth-1, for the browse picker
        # p513: enumerate FILES too (drop -type d), tagging each row with its
        # find %y type so _finder_parse can split dirs vs files. The `dirs`
        # array stays populated (fetchDirs cache + dir navigation depend on it);
        # `files` is the new sibling array the browse UI reads.
        return pre + ("find \"$R\" -mindepth 1 -maxdepth 1 "
                      + ("" if hidden else "! -name '.*' ")
                      + f"-printf '%y\\t%p\\n' 2>/dev/null | sort | head -n {n}{cap}\nexit 0\n")
    # collect / grep share the walk: prune dot-entries (unless raw) and
    # exclude_dir subtrees, then the ext / glob / dir / type / size / mtime tests.
    prune = []
    if not hidden:
        prune.append("-name '.*'")
    xd = toks("exclude_dir")
    if xd:
        prune.append("-type d \\( " + " -o ".join("-iname " + q(d) for d in xd) + " \\)")
    tests = []
    ext = [e.lstrip(".") for e in toks("ext") if e.lstrip(".")]
    if ext:
        tests.append("\\( " + " -o ".join("-iname " + q("*." + e) for e in ext) + " \\)")
    for e in toks("exclude_ext"):
        if e.lstrip("."):
            tests.append("! -iname " + q("*." + e.lstrip(".")))
    pats = toks("pattern")
    if pats:
        tests.append("\\( " + " -o ".join("-iname " + q(p) for p in pats) + " \\)")
    for p in toks("exclude_pattern"):
        tests.append("! -iname " + q(p))
    dirs = toks("dir")
    if dirs:
        tests.append("\\( " + " -o ".join("-path " + q("*/" + d + "/*") for d in dirs) + " \\)")
    if op == "grep":
        tests.append("-type f")
    else:
        t = (_fv_list(body, "type") or [""])[0]
        if t in ("f", "d", "l"):
            tests.append("-type " + t)
        mn, mx = _fv_int(body, "min_size"), _fv_int(body, "max_size")
        if mn is not None and mn > 0:
            tests.append(f"-size +{mn - 1}c")
        if mx is not None and mx >= 0:
            tests.append(f"-size -{mx + 1}c")
        a, b = _fv_date(body, "after"), _fv_date(body, "before")
        if a is not None:
            tests.append(f"-newermt @{int(a)}")
        if b is not None:
            tests.append(f"! -newermt @{int(b)}")
    find = f"find \"$R\" -mindepth 1 -maxdepth {depth} "
    if prune:
        find += "\\( " + " -o ".join(prune) + " \\) -prune -o "
    find += " ".join(tests) + " "
    if op == "collect":
        return pre + find + f"-printf '%y\\t%s\\t%T@\\t%p\\n' 2>/dev/null | head -n {n}{cap}\nexit 0\n"
    # grep: fixed strings, any-of at the line level (ALL-match is settled
    # station-side per file); -Z NUL-terminates the path so ':' in names is safe.
    strings = [s for s in _fv_list(body, "string")[:5] if "\x00" not in s and len(s) <= 256]
    grep = "grep -nIHZ -F " + " ".join("-e " + q(s) for s in strings) + " --"
    return pre + find + (f"-print0 2>/dev/null | xargs -0 -r {grep} 2>/dev/null"
                         f" | cut -b1-800 | head -n {n}{cap}\nexit 0\n")


def _finder_parse(op, body, out):
    """Locus stdout -> (result in the drawer's shape, {home, root})."""
    home, _, rest = out.partition("\n")
    root, _, data = rest.partition("\n")
    meta = {"home": home, "root": root}
    if op == "home":
        return {"home": home}, {"home": home, "root": home}
    if op == "dirs":
        # p513: rows are now "%y\t%p" — split by find type into dirs vs files.
        # 'd' -> dirs (nav + fetchDirs cache), everything else -> files.
        dirs, files = [], []
        for l in data.split("\n"):
            if not l:
                continue
            typ, sep, path = l.partition("\t")
            if not sep:                         # tolerate an untyped row
                path, typ = typ, "d"
            (dirs if typ == "d" else files).append(path)
        return {"dirs": dirs, "files": files}, meta
    if op == "view":
        tot, _, text = data.partition("\n")
        all_lines = text.split("\n")
        try:
            total = int(tot.strip()) if len(text) >= FINDER_MAX_BYTES else len(all_lines)
        except ValueError:
            total = len(all_lines)
        spec = (_fv_list(body, "lines") or [""])[0]
        ranges = []
        if not spec:
            ranges = [(1, len(all_lines))]
        else:
            for part in spec.split(","):
                part = part.strip()
                if "-" in part:
                    a, b = part.split("-", 1)
                    try: ranges.append((int(a), int(b)))
                    except ValueError: pass
                elif part.isdigit():
                    ranges.append((int(part), int(part)))
        spans = []
        for a, b in ranges:
            a = max(1, a); b = min(len(all_lines), b)
            spans.append({"lines": [{"line": n, "content": all_lines[n - 1]}
                                    for n in range(a, b + 1)]})
        return {"path": root, "total": total, "spans": spans}, meta
    if op == "collect":                         # rows: type TAB size TAB mtime TAB path
        rows = []
        for l in data.split("\n"):
            p = l.split("\t", 3)
            if len(p) != 4:
                continue
            try:
                rows.append({"path": p[3], "type": p[0], "size": int(p[1]), "mtime": int(float(p[2]))})
            except ValueError:
                continue
        sort = (_fv_list(body, "sort") or ["name"])[0]
        key = {"size": lambda r: r["size"], "mtime": lambda r: r["mtime"]}.get(sort, lambda r: r["path"])
        rows.sort(key=key, reverse=bool(body.get("reverse")))
        lim = _fv_int(body, "limit")
        if lim and lim > 0:
            rows = rows[:lim]
        return {"files": rows if body.get("meta") else [r["path"] for r in rows]}, meta
    # grep: path NUL line ':' content per row. ALL-match (the default) keeps a
    # file only when its hit lines cover every string (findContent's total_strings).
    strings = _fv_list(body, "string")[:5]
    by = {}
    for l in data.split("\n"):
        path, nul, rest = l.partition("\0")
        if not nul:
            continue
        ln, _, content = rest.partition(":")
        if not ln.isdigit():
            continue
        by.setdefault(path, []).append({"line": int(ln), "content": content})
    results = []
    for path, lines in by.items():
        if not body.get("any") and len(strings) > 1:
            blob = "\n".join(x["content"] for x in lines)
            if not all(s in blob for s in strings):
                continue
        results.append({"file_path": path, "lines": lines})
    return {"results": results}, meta


def _fv_list(body, key):
    v = body.get(key)
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def _fv_int(body, key):
    v = body.get(key)
    v = v[0] if isinstance(v, list) and v else v
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _fv_date(body, key):
    v = body.get(key)
    v = v[0] if isinstance(v, list) and v else v
    if not v:
        return None
    import datetime as _dt
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
        try:
            return _dt.datetime.strptime(str(v), fmt).timestamp()
        except ValueError:
            continue
    return None


def _finder_filters(body):
    """Map SearchDrawer body params -> abstract_search filter kwargs."""
    kw = {}
    ext = [e.lstrip(".").lower() for e in _fv_list(body, "ext")]
    xext = [e.lstrip(".").lower() for e in _fv_list(body, "exclude_ext")]
    if ext:  kw["allowed_exts"] = ext
    if xext: kw["exclude_exts"] = xext
    if _fv_list(body, "dir"):             kw["allowed_dirs"] = _fv_list(body, "dir")
    if _fv_list(body, "exclude_dir"):     kw["exclude_dirs"] = _fv_list(body, "exclude_dir")
    if _fv_list(body, "pattern"):         kw["allowed_patterns"] = _fv_list(body, "pattern")
    if _fv_list(body, "exclude_pattern"): kw["exclude_patterns"] = _fv_list(body, "exclude_pattern")
    if _fv_list(body, "type"):            kw["allowed_types"] = _fv_list(body, "type")
    kw["recursive"] = not body.get("no_recursive")
    return kw


async def mct_finder(request):
    """POST /api/mct/finder {op, vm, roots, ...filters} — grep | collect |
    dirs | view | home, run IN the active locus (see the header above). Gated
    by auth_mw like every /api route. Answers carry locus/kind/home/root so
    the drawer can show where it is looking and fence its root picker."""
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)
    op = (body.get("op") or "").strip()
    if op not in ("home", "grep", "collect", "dirs", "view"):
        return web.json_response({"ok": False, "error": "op must be home|grep|collect|dirs|view"}, status=400)
    ground, gerr = await _finder_ground(body.get("vm"))
    if ground is None:
        return web.json_response({"ok": False, "error": gerr}, status=400)
    who = {"locus": ground or "@keeper", "kind": _finder_kind(ground)}
    roots = ["/"] if op == "home" else _finder_roots(body)
    if roots is None:
        return web.json_response({"ok": False, "error": "roots must be 1-8 absolute locus paths (no ..)",
                                  **who}, status=400)
    if op == "grep" and not _fv_list(body, "string"):
        return web.json_response({"ok": False, "error": "grep needs at least one string", **who}, status=400)
    rc, out, err = await _finder_exec(ground, _finder_script(op, body, roots[0]))
    if rc != 0:
        msg = (err.strip().splitlines() or [f"finder rc {rc}"])[-1]
        # 90-93 are the script's own root/home verdicts (operator-fixable);
        # anything else is the hop (ssh/lxc) or the locus failing.
        return web.json_response({"ok": False, "error": msg, **who},
                                 status=400 if 90 <= rc <= 93 else 502)
    try:
        result, meta = _finder_parse(op, body, out)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e), **who}, status=500)
    return web.json_response({"ok": True, "result": result, **who, **meta,
                              "truncated": len(out) >= FINDER_MAX_BYTES})


def _safe_stat(path, attr):
    try:
        return getattr(os.stat(path), attr)
    except OSError:
        return 0



# --- VM-as-session (prototype). A "VM" is just another session target: an LXD
# station on THIS host. vm-mgr's `vm-new` builds one (the console "calls this and
# streams its output" per its design); `lxc delete` tears it down. Create/delete
# ride the existing homebase+provision+CSRF gate. --------------------------------
# Last resort is the site layer's own bin dir (ROOT/../bin) — vm-new is a
# vm_mgr script, not on the service's PATH; the old dev-machine fallback path
# made create 502 everywhere else.
VM_NEW_BIN = (os.environ.get("VM_NEW_BIN") or shutil.which("vm-new")
              or str(VM_BIN / "vm-new"))
_VM_STATION_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_VM_RES_RE = re.compile(r"^[0-9]{1,5}(GiB|MiB)?$")
_VM_JOBS = {}   # name -> {state, log} for in-flight vm-new builds (this process)


async def mct_vm_get(request):
    """GET /api/mct/vm — LXD stations on this host, as candidate VM sessions."""
    lxc_err = ""
    try:
        rc, out, err = await _run("lxc", "list", "--format", "csv", "-c", "ns")
    except Exception as e:      # noqa: BLE001
        rc, out, err = 1, "", str(e)
    if rc != 0:
        # 2026-09-03: no lxc here (e.g. the snap CLI refuses vm_mgr on ae) must
        # not blank the 🖥 drawer — the ssh/toolserver loci still list, and the
        # reason rides along as `lxc_error`.
        lxc_err = (err or out).strip() or "lxc list failed"
        out = ""
    vms = []
    for line in (out or "").splitlines():
        parts = line.split(",")
        if parts and parts[0]:
            name = parts[0]
            vms.append({"name": name, "status": (parts[1] if len(parts) > 1 else "").strip(),
                        "building": _VM_JOBS.get(name, {}).get("state") == "building"})
    for name, job in _VM_JOBS.items():
        if job.get("state") == "building" and not any(v["name"] == name for v in vms):
            vms.append({"name": name, "status": "BUILDING", "building": True})
    for n, h in sorted(_ssh_hosts().items()):
        vms.append({"name": n, "kind": "ssh", "host": h["host"], "user": h.get("user", "root"),
                    "port": h.get("port", 22), "building": False,
                    "status": "RUNNING" if await _ssh_alive(h) else "UNREACHABLE"})
    hidden = _hidden_loci()
    for v in vms:                       # the drawer sees hidden ones too (to unhide them)
        v["hidden"] = v["name"] in hidden
    return web.json_response({"ok": True, "vms": vms, "hidden": sorted(hidden),
                              "lxc_error": lxc_err or None,
                              "vm_new": bool(VM_NEW_BIN and os.path.exists(VM_NEW_BIN))})


async def mct_vm_post(request):
    """POST /api/mct/vm {op:"create"|"delete", name, cpu?, mem?, disk?, instance_only?}."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    op = (body.get("op") or "create").strip()
    name = (body.get("name") or "").strip()
    if not _VM_STATION_RE.match(name):
        return web.json_response({"ok": False, "error": "bad station name (a-z, then a-z0-9-, <=31)"}, status=400)
    if name in ("golden", "sandbox", "default", "none"):
        return web.json_response({"ok": False, "error": "reserved name"}, status=400)
    if op in ("hide", "unhide"):
        # ◌ non-destructive: a view preference of THIS station, no provision gate
        hidden = _hidden_loci()
        (hidden.add if op == "hide" else hidden.discard)(name)
        try:
            _hidden_loci_save(hidden)
        except OSError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        audit(request, "locus-" + op, name, ok=True)
        return web.json_response({"ok": True, "op": op, "name": name, "hidden": sorted(hidden)})
    try:
        require_provision(request)           # homebase + provision cap + CSRF
    except web.HTTPException:
        raise

    if op == "delete":
        rc, out, err = await _run("lxc", "delete", name, "--force", timeout=120)
        audit(request, "vm-delete", name, ok=(rc == 0))
        if rc != 0:
            return web.json_response({"ok": False, "error": (err or out).strip() or "delete failed"}, status=502)
        _VM_JOBS.pop(name, None)
        return web.json_response({"ok": True, "deleted": name})

    if op == "create":
        if not (VM_NEW_BIN and os.path.exists(VM_NEW_BIN)):
            # 1.0.22+: the package ships its own builder at ../bin/vm-new (the
            # console IS the vm_mgr for VMs created under it), so this only
            # fires on a broken/stripped install or a stale VM_NEW_BIN.
            return web.json_response(
                {"ok": False, "error": "vm-new not found — reinstall "
                 "hugpy-station (its packaged builder is missing) or set "
                 "VM_NEW_BIN to a site vm-new"}, status=502)
        if _VM_JOBS.get(name, {}).get("state") == "building":
            return web.json_response({"ok": False, "error": "already building"}, status=409)
        cpu = str(body.get("cpu") or "4")
        mem = str(body.get("mem") or "8GiB")
        disk = str(body.get("disk") or "100GiB")
        if not (cpu.isdigit() and _VM_RES_RE.match(mem) and _VM_RES_RE.match(disk)):
            return web.json_response({"ok": False, "error": "bad cpu/mem/disk"}, status=400)
        argv = [VM_NEW_BIN, name, "--cpu", cpu, "--mem", mem, "--disk", disk, "-y"]
        if body.get("instance_only"):
            argv.append("--instance-only")
        if (body.get("profile") or "") == "isolated":
            argv += ["--profile", "isolated"]
        FV_STATE_HOME.mkdir(parents=True, exist_ok=True)
        logp = FV_STATE_HOME / ("vm-new-" + name + ".log")
        try:
            logf = open(logp, "wb")
            proc = subprocess.Popen(argv, stdout=logf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=502)
        _VM_JOBS[name] = {"state": "building", "log": str(logp), "pid": proc.pid}
        audit(request, "vm-create", name)
        return web.json_response({"ok": True, "started": name, "log": str(logp),
                                  "argv": argv})

    return web.json_response({"ok": False, "error": "op must be create|delete"}, status=400)


def _mct2_live_turns():
    """The mct (pointer-exchange) backend's turn log, from its canonical
    record: <ws>/exchange/turn-NNNN-{prompt,response}.md plus status.json.
    Shaped like mct.db turns so 📡 live renders both streams as one."""
    # mct v2: the canonical workspace first, the retired mct2/repl one as a
    # read-only fallback (its archived exchange files stay visible).
    base = _active_ws()
    if not (base / "exchange").is_dir():
        base = _LEGACY_MCT2_WS
    exch = base / "exchange"
    if not exch.is_dir():
        return []
    st = _read_json(base / "status.json", {}) or {}
    working = st.get("turn") if st.get("state") == "working" else None
    turns = []
    for p in sorted(exch.glob("turn-*-prompt.md"))[-12:]:
        tag = p.name[:-len("-prompt.md")]
        resp = exch / (tag + "-response.md")
        iso = lambda path: time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime))
        # path/file ride each event so 📡 live can show WHERE the turn lives
        # and expand the file's contents on click (/api/mct2/exchange).
        evs = [{"type": "prompt.written", "actor": "C.operator", "ts": iso(p),
                "path": str(p), "file": p.name}]
        if resp.exists():
            evs.append({"type": "response.written", "actor": "A.frontier",
                        "ts": iso(resp), "path": str(resp), "file": resp.name})
            state = "done"
        else:
            state = "working" if working == tag else "awaiting-response"
        turns.append({"turn_id": tag, "state": state, "created_at": iso(p),
                      "backend": "mct", "events": evs})
    return turns


def _mct_ledger_live_turns(app, limit=12):
    """The ledger half of /api/mct/live: recent native-seat turns with their
    pointer events. Nothing inline — bodies are pulled on demand by handle."""
    broker = app.get("mct_broker") if app is not None else None
    if broker is None:
        return []
    return broker.store.turn_log(limit=limit)


_MCT_LIVE_OBJECT_MAX = 256 * 1024


async def mct_live_object(request):
    """GET /api/mct/live/object?handle=mct://… — render ONE ledger object for
    the operator's live view: {content, name, mime, size, sha256, truncated}.
    Text is decoded (replacement on bad bytes) and capped; binaries report
    metadata only (download via /api/mct/object)."""
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
    broker = request.app.get("mct_broker")
    if broker is None:
        return web.json_response({"ok": False, "error": "no MCT ledger on this station"}, status=404)
    handle = (request.query.get("handle") or "").strip()
    try:
        meta = await asyncio.to_thread(broker.store.describe, handle)
        raw = await asyncio.to_thread(broker.store.pull, handle)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=404)
    mime = meta.get("mime") or "application/octet-stream"
    textual = mime.startswith("text/") or mime.split(";")[0] in ("application/json", "application/xml", "application/x-yaml", "application/javascript")
    content, truncated = "", False
    if textual:
        if len(raw) > _MCT_LIVE_OBJECT_MAX:
            raw, truncated = raw[:_MCT_LIVE_OBJECT_MAX], True
        content = raw.decode("utf-8", "replace")
        if meta.get("kind") == "context":
            try:   # the manifest is a pointer set; show it pretty and without the seat protocol text
                doc = json.loads(content)
                doc.pop("response_instructions", None)
                content = json.dumps(doc, indent=1, ensure_ascii=False)
            except ValueError:
                pass
    return web.json_response({"ok": True, "handle": handle, "content": content, "textual": textual,
                              "truncated": truncated, "name": meta.get("name"), "mime": mime,
                              "kind": meta.get("kind"), "size": meta.get("size"), "sha256": meta.get("sha256"),
                              "origin": meta.get("origin") or "broker",
                              "download": "/api/mct/object?handle=" + __import__("urllib.parse").parse.quote(handle, safe="")})


async def mct_live(request):
    """GET /api/mct/live — the frontier's live turn log: the mct backend's
    pointer-exchange turns (exchange files + status.json) MERGED with the
    deprecated broker MCT's mct.db turns and their event flow (turn.ingested /
    a.input / ... with the actor on each), i.e. the live C<->B<->A
    interaction. Each turn carries its backend. With an active VM the flow
    comes from THAT VM's workbench (grounding rule)."""
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
    import sqlite3
    out, sid = [], None
    try:
        # mct v2: the durable ledger's turns FIRST — every event is a pointer
        # (prompt/attachment/context/response/artifact handles); the operator
        # reads a reply by pulling its object (/api/mct/live/object?handle=).
        out.extend(_mct_ledger_live_turns(request.app))
    except Exception:
        pass                       # the ledger must never sink the view
    try:
        out.extend(_mct2_live_turns())
    except Exception:
        pass                       # the exchange log must never sink the view
    db = _active_ws() / ".hugpy_agent" / "mct" / "mct.db"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            srow = con.execute(
                "SELECT session_id FROM turns ORDER BY created_at DESC LIMIT 1").fetchone()
            sid = srow["session_id"] if srow else None
            turns = con.execute(
                "SELECT session_id, turn_id, state, epoch, created_at FROM turns "
                "WHERE (? IS NULL OR session_id=?) ORDER BY created_at DESC LIMIT 12",
                (sid, sid)).fetchall()
            for t in turns:
                evs = con.execute(
                    "SELECT sequence, type, actor, timestamp FROM events "
                    "WHERE session_id=? AND turn_id=? ORDER BY sequence",
                    (t["session_id"], t["turn_id"])).fetchall()
                out.append({"turn_id": t["turn_id"], "state": t["state"],
                            "created_at": t["created_at"],
                            "backend": "mct-deprecated",
                            "events": [{"type": e["type"], "actor": e["actor"],
                                        "ts": e["timestamp"]} for e in evs]})
            con.close()
        except Exception as e:
            if not out:
                return web.json_response({"ok": False, "error": str(e)}, status=502)
    out.sort(key=lambda t: t.get("created_at") or "")
    out = out[-12:]                # oldest-first within the recent window
    return web.json_response({"ok": True, "turns": out,
                              "session": sid or _active_session()})


async def _central_prompt(request, vm, locus=None):
    locus = locus or _central_locus(vm) or vm   # 1.0.138: the locus's central key
    try:
        body = await request.json()
        text = (body.get("text") or "").strip()
        files = [f for f in (body.get("files") or []) if isinstance(f, dict)][:20]
    except Exception:
        return web.json_response({"error": "bad body"}, status=400)
    if not text and not files:
        return web.json_response({"error": "empty prompt"}, status=400)
    try:
        doc = await _ts_call(request.app, "prompt/submit", {
            "locus": locus, "text": text, "files": files, "by": _station_id(),
            "session": _active_session()}, timeout=120)
    except Exception as e:
        return web.json_response(
            {"ok": False, "error": f"{vm}: toolserver prompt inbox unreachable — {e}"}, status=502)
    if not isinstance(doc, dict) or not doc.get("path"):
        return web.json_response({"ok": False, "error": f"{vm}: prompt/submit gave no path"}, status=502)
    submitted = False
    sess = None if body.get("save_only") else _live_surface_session(_surface_key("frontier", vm, _active_session()))
    if sess is not None:
        try:
            # 1.0.143: text, then Enter as a SEPARATE write — an Ink TUI reads
            # text+"\r" arriving in one write as a paste and never submits.
            sess.write(str(doc.get("pointer") or ("Handle the operator prompt at "
                       + str(doc["path"]) + "/prompt.md — read it via a pull, then respond.")).encode("utf-8"))
            await asyncio.sleep(0.3)
            sess.write(b"\r")
            submitted = True
        except Exception:
            submitted = False
    n = len(doc.get("files") or [])
    note = (("sent → frontier (A) @" + vm) if submitted
            else ("posted → " + vm + " board " + str(doc.get("board_id") or "") + " (no frontier seat here)"))
    note += " · toolserver " + str(doc.get("id") or "") + (" · " + str(n) + " file(s)" if n else "")
    return web.json_response({"ok": True, "note": note, "submitted": submitted,
                              "path": str(doc["path"]), "central": True,
                              "prompt_id": doc.get("id"), "board_id": doc.get("board_id")})


async def _mct_native_seat():
    """The native seat (codex|claude-code) the ledger should address: the live
    frontier tmux session mapped through NATIVE_SEATS, else '' (no seat)."""
    try:
        from mct_gateway import NATIVE_SEATS as _seats
    except ImportError:
        return ""
    sess = await _live_frontier_session()
    if not sess:
        return ""
    for provider, tmux_name in _seats.items():
        if sess == tmux_name:
            return provider
    return ""


async def mct_prompt(request):
    """POST /api/mct/prompt {text, files:[{name,type,dataUrl}]} - save a composed
    prompt + attached media into the active session workspace's prompt-inbox, for the
    agent (B/A) to pick up. Returns the saved dir. With an active VM the
    prompt is SAVED by that VM's workbench (files land in the VM workspace —
    the only place its A can read them), then the pointer line is typed into
    the HARNESS's frontier PTY for that VM, which is the live seat here."""
    vm = await _sidecar_vm(request)
    if vm and _central_locus(vm) != _keeper_locus():
        # Operator 2026-09-02 ("just have it sent to the toolserver"): a non-host
        # locus's prompt + files go to the toolserver's central prompt inbox
        # (prompt/submit) — stored on ae where every seat can read them, with a
        # [prompt] board item on that locus (the lane comms.ping uses). If this
        # station holds the locus's frontier seat, the pointer is typed into it.
        return await _central_prompt(request, vm)
    import base64, re as _re
    try:
        body = await request.json()
        text = (body.get("text") or "").strip()
        files = body.get("files") or []
    except Exception:
        return web.json_response({"error": "bad body"}, status=400)
    if not text and not files:
        return web.json_response({"error": "empty prompt"}, status=400)
    # t319 (2026-09-16): while abstract-claude serve LEADS the host frontier,
    # the operator's prompt must not be fanned out to the tmux MCT seat behind
    # their back (the seat answered a question the serve chat was already
    # handling). Mirror of fleetview-term.js sendPrompt's t296 guard. An
    # explicit {"seat": "tmux"} still reaches the terminal seat on purpose.
    if KEEPER_SURFACE == "serve" and (body.get("seat") or "").strip().lower() != "tmux":
        return web.json_response(
            {"error": "frontier is the serve console — type in the chat pane "
                      "(send {\"seat\": \"tmux\"} to address the terminal seat explicitly)",
             "keeper_surface": "serve", "refused": True}, status=409)
    # mct v2 (2026-09-15): with the durable ledger present, the composed prompt
    # becomes an immutable object set and the Broker types ONE pointer line
    # into the live native seat (durable, sha-verified, idempotent, visible in
    # /api/mct/live). No prompt-inbox file, no client-typed pointer. The
    # inbox path below stays as the fallback when no ledger/seat exists.
    broker = None if body.get("save_only") else request.app.get("mct_broker")
    if broker is not None:
        seat = await _mct_native_seat()
        if seat:
            try:
                import hashlib as _hl
                digest = _hl.sha256((text + "\0" + "\0".join(str(f.get("name") or "") + ":" + str(f.get("dataUrl") or "")[-64:] for f in files[:20])).encode("utf-8")).hexdigest()[:24]
                rid = "prompt-" + time.strftime("%Y%m%d%H%M%S") + "-" + digest
                turn = await asyncio.to_thread(broker.store.submit, seat, rid, text, [f for f in files[:20] if isinstance(f, dict)])
                audit(request, "mct-prompt", f"ledger turn {turn['id']} → {seat}", ok=True)
                return web.json_response({"ok": True, "submitted": True, "central": False,
                                          "note": "queued → " + seat + " as MCT context " + str(turn["context"]) + " (pointer delivered by the broker)",
                                          "turn": turn["id"], "context": turn["context"], "seat": seat})
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = _active_ws() / "prompt-inbox" / ts
    saved = []
    try:
        dst.mkdir(parents=True, exist_ok=True)
        if text:
            (dst / "prompt.md").write_text(text, encoding="utf-8")
        for f in files[:20]:
            name = _re.sub(r"[^A-Za-z0-9._-]", "_", str(f.get("name") or "file"))[:80] or "file"
            m = _re.match(r"data:[^;]*;base64,(.*)$", f.get("dataUrl") or "")
            if not m:
                continue
            (dst / name).write_bytes(base64.b64decode(m.group(1)))
            saved.append(name)
    except (OSError, ValueError) as e:
        return web.json_response({"error": str(e)}, status=500)
    # Reach the agent by pointing it at the saved prompt/media (file-pointing keeps
    # A's context lean). The pointer is delivered to the live seat by the FRONTEND
    # (PromptPanel -> window.__fvSend(pointer, "frontier", {submit:true})), NOT typed
    # here. Two reasons the old server-side write was wrong:
    #   1. TARGET: _frontier_live_session() returned whichever frontier backend's PTY
    #      came first in FV_SESSIONS (mct AND claude-code both keep a live session once
    #      switched, since a switch DETACHES rather than kills), so it typed into
    #      claude-code regardless of which backend is actually displayed in the station.
    #   2. SUBMIT: a single-write "\r" does not submit in the claude-code Ink TUI (it
    #      reads text+Enter arriving in one write as a paste and just inserts a newline).
    # The frontend bridge targets the ACTIVE frontier backend (its ws carries
    # &backend=<chosen>) and submits with a SEPARATE, delayed Enter. We SAVE the prompt
    # + files and return the pointer for the UI to inject.
    pointer = ("Handle the operator prompt at " + str(dst / "prompt.md")
               + (" (with " + str(len(saved)) + " attached file(s) in " + str(dst)
                  + ": " + ", ".join(saved) + ")" if saved else "")
               + " — read it via a pull, then respond.")
    # 1.0.143: this inbox+pointer path is the LEGACY FILE-POINTER FALLBACK. The ✍
    # prompt bar delivers through POST /api/prompt/send (serve session / tmux
    # seat) and only reaches here when the operator explicitly picks "save to
    # inbox (file-pointer fallback)" after a failed send. Nothing consumes
    # prompt-inbox/ by itself: the reply says so.
    note = ("saved \u2192 prompt-inbox/" + ts
            + (" \u00b7 " + str(len(saved)) + " file(s)" if saved else "")
            + " (file-pointer fallback \u2014 NOT delivered; point a session at " + str(dst) + ")")
    return web.json_response({"ok": True, "note": note, "submitted": False, "legacy": "file-pointer",
                              "pointer": pointer, "path": str(dst)})


# ── ✍ prompt delivery (1.0.143, operator 2026-10-01) ─────────────────────────
# The prompt bar sends the TEXT to the target session directly — the locus's
# serve console session (POST /api/console/chat on THAT locus's serve, from
# _ac_resolve) or its Terminal tmux seat (send-keys + a separate Enter) — and
# reports delivered / queued / unconfirmed / NOT delivered + reason. Logic in
# prompt_send.py; this is the wiring. /api/mct/prompt's prompt-inbox pointer
# stays only as the explicit, labelled file-pointer fallback.
def _serve_caller(app, base):
    """call(method, path, body) -> (status, json|None) against one serve upstream."""
    base = (base or AC_UPSTREAM).rstrip("/")

    async def call(method, path, body=None):
        sess = app.get("proxy_sess")
        if sess is None:
            sess = app["proxy_sess"] = aiohttp.ClientSession()
        to = aiohttp.ClientTimeout(total=60 if path.startswith("/api/session/attach") else 15)
        async with sess.request(method, base + path, json=body, timeout=to) as r:
            doc = (await r.json(content_type=None)
                   if r.content_type and "json" in r.content_type else None)
            return r.status, doc
    return call


async def _serve_head_of(call, role=None):
    """(sid, why): the serve's keeper session — roster role -> rollover head."""
    role = role or NUDGE_SERVE_ROLE
    st, roster = await call("GET", "/api/session/roster")
    if st != 200 or not isinstance(roster, dict):
        return "", "serve roster HTTP %s" % st
    try:
        _st, roll = await call("GET", "/api/session/rollover")
    except Exception:                                    # noqa: BLE001 — no chain: the roster wins
        roll = None
    if _knudge is not None:
        return _knudge.resolve_keeper_session(roster, roll if isinstance(roll, dict) else None, role)
    ent = next((e for e in roster.get("roles") or [] if isinstance(e, dict) and e.get("role") == role), None)
    sid = str((ent or {}).get("session_id") or "")
    return (sid, "roster") if sid else ("", "no live %s session in serve" % role)


async def _serve_queue_release(call, sid, by="station"):
    """POST /api/console/queue action=release (serve-core 0.1.15: lifts a hold
    and kicks the queue). A 0.1.14 serve does not know "release" (HTTP 400
    "Unknown queue action") — it is sent "retry", the same operation there.
    -> (status, doc) in either release's shape (keeper_nudge.queue_action_ok)."""
    st, doc = await call("POST", "/api/console/queue", {"session_id": sid, "action": _knudge.QUEUE_RELEASE
                                                         if _knudge else "release", "by": by})
    if st == 400 and "unknown queue action" in str((doc or {}).get("error") if isinstance(doc, dict) else "").lower():
        st, doc = await call("POST", "/api/console/queue", {"session_id": sid, "action": "retry"})
    return st, doc


def _prompt_store_local(files):
    """Seat attachments for the HOST: plain files under the active workspace's
    prompt-inbox/<ts>/ (storage only — the prompt text itself is sent)."""
    import base64 as _b64
    dst = _active_ws() / "prompt-inbox" / (time.strftime("%Y%m%d-%H%M%S") + "-att")
    dst.mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        mime, b64 = PS.split_data_url(f.get("dataUrl"))
        name = re.sub(r"[^A-Za-z0-9._-]", "_", str(f.get("name") or "file"))[:80].strip("._") or "file"
        p = dst / name
        n = 1
        while p.exists():
            n += 1
            p = dst / ("%d-%s" % (n, name))
        data = _b64.b64decode(b64)
        p.write_bytes(data)
        out.append({"path": str(p), "name": name, "mime": str(f.get("type") or mime or ""), "size": len(data)})
    return out


async def api_prompt_send(request):
    """POST /api/prompt/send[?vm=] {text, files:[{name,type,dataUrl}], target:
    auto|serve|seat, session_id?, seat_backend?, force?} -> PS.result dict
    (+ locus, surface). HTTP 200 for every decided outcome (ok/state say
    which); 400 only for a malformed body."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response(PS.result("failed", "", "bad body"), status=400)
    if not isinstance(body, dict):
        return web.json_response(PS.result("failed", "", "bad body"), status=400)
    text = str(body.get("text") or "")
    files = PS.clean_files(body.get("files"))
    if not text.strip() and not files:
        return web.json_response(PS.result("failed", "", "empty prompt"), status=400)
    vm = await _sidecar_vm(request)
    try:
        surface, ac = await _keeper_surface_now(vm)
    except Exception as e:                               # noqa: BLE001
        surface, ac = KEEPER_SURFACE, {"ok": False, "error": str(e)}
    target = PS.choose_target(body.get("target"), surface)
    locus = vm or "host"
    if target == "serve":
        if not ac.get("url"):
            res = PS.result("failed", "serve", ac.get("error") or ("no abstract-claude serve for locus " + locus))
        elif not ac.get("ok"):
            res = PS.result("failed", "serve", "serve not answering: " + str(ac.get("error") or ac.get("url")))
        else:
            call = _serve_caller(request.app, ac["url"])
            res = await PS.deliver_serve(call, text, files, session_id=str(body.get("session_id") or ""),
                                         head=lambda: _serve_head_of(call),
                                         fallback_head=str(body.get("target") or "auto") == "auto")
            res["serve"] = ac.get("path") or ""
    else:
        backend = str(body.get("seat_backend") or "")
        sess = _tmux_session_for("frontier", backend)
        if not sess:      # mct (no tmux of its own) / unknown: the native seat it rides
            sess = ((await _live_frontier_session()) if not vm else "") or "keeper-claude"
        out_text, store_err = text, ""
        if files:
            try:
                if vm:       # a non-host locus: the central inbox on ae (prompt/submit) keeps the files
                    doc = await _ts_call(request.app, "prompt/submit", {
                        "locus": _central_locus(vm) or vm, "text": text, "files": files,
                        "by": _station_id(), "session": _active_session()}, timeout=120)
                    items = [{"path": os.path.join(str(doc.get("path") or ""), n), "name": n}
                             for n in (doc.get("files") or [])]
                else:
                    items = await asyncio.to_thread(_prompt_store_local, files)
                out_text = text + PS.attachments_block(items)
            except Exception as e:                       # noqa: BLE001
                store_err = "attachments not stored: %s" % e
        if store_err:
            res = PS.result("failed", "seat", store_err)
        else:
            res = await PS.deliver_seat(lambda *a: _tmux_on(vm, *a), sess, out_text,
                                        force=bool(body.get("force")), label="%s@%s" % (sess, locus))
    res.update(locus=locus, surface=surface)
    if PS.repend_worthy(res):
        # 1.0.146 (t4260 / d4254): a prompt the serve head could not take right
        # now is RE-PENDED to the locus's prompt-pending file with the reason and
        # retried every tick (_prompt_pending_loop) — never a final "failed"
        ent = _prompt_repend(vm, locus, text, files, str(body.get("session_id") or ""),
                             str(body.get("target") or "auto"), res.get("reason") or "")
        res = PS.result("pending", "serve", res.get("reason") or "", locus=locus, surface=surface,
                        pending_id=ent["id"], attempts=0,
                        detail="re-pended for locus %s — retried every %ds until a serve session takes it"
                               % (locus, PROMPT_PENDING_TICK))
    audit(request, "prompt-send", "%s → %s %s %s %s" % (locus, res.get("target"), res.get("state"),
                                                        res.get("session_id") or res.get("seat") or res.get("pending_id") or "",
                                                        (res.get("reason") or "")[:160]),
          ok=bool(res.get("ok")))
    return web.json_response(res)


# ── 1.0.146: the per-locus prompt-pending file (re-pended ✍ prompts) ───────────
PROMPT_PENDING_TICK = max(10, int(os.environ.get("STATION_PROMPT_PENDING_TICK") or "60"))
PROMPT_PENDING_MAX_FILE_B = 2_000_000       # attachments ride along as data URLs up to this


def _prompt_pending_path(vm):
    if not vm or _is_host_vm(vm):
        return FV_STATE_HOME / "prompt-pending.json"
    return _locus_state_dir(_central_locus(vm) or vm) / "prompt-pending.json"


def _prompt_pending(vm):
    doc = _read_json(_prompt_pending_path(vm), {}) or {}
    return [e for e in (doc.get("items") or []) if isinstance(e, dict) and e.get("id")]


def _prompt_pending_write(vm, items):
    p = _prompt_pending_path(vm)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(p, {"items": list(items), "saved": int(time.time())})
    key = "pending:prompt@" + (vm or "host")
    if items:
        _hold_note(key, _sh.SOURCE_PENDING if _sh else "station:pending",
                   "✍ prompt(s) re-pended for %s" % (vm or "host"),
                   "; ".join("%s: %s" % (e.get("id"), (e.get("reason") or "")[:80]) for e in items[-3:]),
                   "GET /api/prompt/pending?vm=%s — delivered automatically when the serve session answers"
                   % (vm or ""), count=len(items))
    else:
        _hold_clear(key)


def _prompt_repend(vm, locus, text, files, session_id, target, reason):
    """Append one failed ✍ prompt to the locus's pending file (never drops)."""
    items = _prompt_pending(vm)
    keep = [f for f in (files or []) if len(str(f.get("dataUrl") or "")) <= PROMPT_PENDING_MAX_FILE_B]
    ent = {"id": "pp-" + secrets.token_hex(4), "vm": vm or "", "locus": locus, "text": text,
           "files": keep, "dropped_files": max(0, len(files or []) - len(keep)),
           "session_id": session_id, "target": target, "reason": str(reason)[:300],
           "at": int(time.time()), "attempts": 0, "last_attempt": 0, "last_reason": str(reason)[:300]}
    items.append(ent)
    _prompt_pending_write(vm, items)
    _audit_line("prompt-repend", "%s %s: %s" % (locus, ent["id"], ent["reason"][:160]))
    return ent


async def _prompt_pending_once(app, vm):
    """Retry every re-pended prompt of one locus; -> [(id, state, reason)]."""
    items = _prompt_pending(vm)
    if not items:
        return []
    out, keep = [], []
    try:
        surface, ac = await _keeper_surface_now(vm)
    except Exception as e:                               # noqa: BLE001
        surface, ac = KEEPER_SURFACE, {"ok": False, "error": str(e)}
    now = int(time.time())
    for ent in items:
        res = None
        if ac.get("url") and ac.get("ok"):
            call = _serve_caller(app, ac["url"])
            res = await PS.deliver_serve(call, ent.get("text") or "", ent.get("files") or [],
                                         session_id=str(ent.get("session_id") or ""),
                                         head=lambda c=call: _serve_head_of(c),
                                         fallback_head=str(ent.get("target") or "auto") == "auto")
        else:
            res = PS.result("failed", "serve", ac.get("error") or "no abstract-claude serve for locus " + (vm or "host"))
        ent["attempts"] = int(ent.get("attempts") or 0) + 1
        ent["last_attempt"] = now
        if res.get("ok"):
            out.append((ent["id"], res.get("state"), ""))
            _audit_line("prompt-repend-delivered", "%s %s → %s %s after %d attempt(s)"
                        % (ent.get("locus"), ent["id"], res.get("state"), res.get("session_id") or "", ent["attempts"]))
            continue
        ent["last_reason"] = str(res.get("reason") or "")[:300]
        keep.append(ent)
        out.append((ent["id"], "pending", ent["last_reason"]))
    _prompt_pending_write(vm, keep)
    return out


def _prompt_pending_vms():
    """Every locus with a pending file: the host + each <state>/loci/<locus>/."""
    vms = [""]
    try:
        for d in (FV_STATE_HOME / "loci").iterdir():
            if (d / "prompt-pending.json").is_file():
                vms.append(d.name)
    except OSError:
        pass
    return vms


async def _prompt_pending_loop(app):
    await asyncio.sleep(min(20, PROMPT_PENDING_TICK))
    while True:
        for vm in _prompt_pending_vms():
            try:
                await _prompt_pending_once(app, vm)
            except asyncio.CancelledError:
                raise
            except Exception as e:                       # noqa: BLE001
                _rlog.warning("prompt pending %s: %s", vm or "host", e)
        await asyncio.sleep(PROMPT_PENDING_TICK)


async def _start_prompt_pending(app):
    app["prompt_pending"] = asyncio.create_task(_prompt_pending_loop(app))


async def _stop_prompt_pending(app):
    t = app.get("prompt_pending")
    if t:
        t.cancel()


async def api_prompt_pending(request):
    """GET /api/prompt/pending[?vm=] — the re-pended ✍ prompts of a locus (text
    clipped); POST {action:"retry"} runs one delivery pass now."""
    vm = await _sidecar_vm(request)
    if request.method == "POST":
        res = await _prompt_pending_once(request.app, vm)
        return web.json_response({"ok": True, "vm": vm or "@self", "results": [
            {"id": i, "state": s, "reason": r} for i, s, r in res]})
    items = [{k: e.get(k) for k in ("id", "locus", "session_id", "target", "reason", "at", "attempts",
                                      "last_attempt", "last_reason", "dropped_files")}
             | {"text": str(e.get("text") or "")[:300], "files": len(e.get("files") or [])}
             for e in _prompt_pending(vm)]
    return web.json_response({"ok": True, "vm": vm or "@self", "items": items,
                              "path": str(_prompt_pending_path(vm)), "tick_s": PROMPT_PENDING_TICK})


async def api_prompt_status(request):
    """GET /api/prompt/status?vm=&session_id=&ids=a,b&since=<cursor> -> where a
    serve-delivered prompt is now (PS.classify_status) + the reply when done.
    POST {session_id, action:"release"} starts a held/stalled serve queue
    (serve-core 0.1.15 action; "retry" is accepted from older UIs and sent as
    "release" — 0.1.14 only knew "retry", 0.1.15 keeps it as an alias)."""
    vm = await _sidecar_vm(request)
    url, _unit, _path, key = await _ac_resolve(vm)
    if not url:
        return web.json_response({"state": "unknown", "error": "no serve for locus " + (key or vm or "host")})
    call = _serve_caller(request.app, url)
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
        sid = str((body or {}).get("session_id") or "")
        if not sid or (body or {}).get("action") not in ("retry", "release"):
            return web.json_response({"ok": False, "error": "need {session_id, action:'release'}"}, status=400)
        st, doc = await _serve_queue_release(call, sid, by=_toggle_owner(request))
        ok = _knudge.queue_action_ok(st, doc) if _knudge else st == 200
        audit(request, "prompt-release", "%s %s HTTP %s" % (vm or "host", sid, st), ok=ok)
        return web.json_response({"ok": ok, "status": st, "result": doc, "action": "release"})
    sid = request.query.get("session_id") or ""
    ids = [i for i in (request.query.get("ids") or "").split(",") if i]
    if not sid or not ids:
        return web.json_response({"state": "unknown", "error": "need session_id and ids"}, status=400)
    try:
        since = int(request.query.get("since") or 0)
    except ValueError:
        since = 0
    try:
        qst, q = await call("GET", "/api/console/queue?id=" + sid)
        est, ev = await call("GET", "/api/console/events?id=%s&since=%d" % (sid, since))
    except Exception as e:                               # noqa: BLE001
        return web.json_response({"state": "unknown", "error": "serve unreachable: %s" % e})
    events = (ev or {}).get("events") if est == 200 and isinstance(ev, dict) else []
    out = PS.classify_status(ids, q if qst == 200 and isinstance(q, dict) else None, events or [])
    out["session_id"] = sid
    # the events page is capped (500): hand back the last seq so the next poll
    # continues from there instead of re-reading a long turn's first page
    out["cursor"] = max([since] + [int(e.get("seq") or 0) for e in (events or []) if isinstance(e, dict)])
    return web.json_response(out)


async def mct_todo_get(request):
    """GET /api/mct/todo - the current session's todo.v1 board. With an
    active VM the board is THAT VM's own (via its workbench); the
    /api/vm/keeper/todo alias always stays the host keeper's board.
    1.0.138: every non-host locus kind (ssh host, LXD guest, registered locus)
    reads ITS central `todos` rows — no workbench/lxc file on the locus."""
    vm = await _sidecar_vm(request)
    if vm:
        loc = _central_locus(vm)
        if not loc:
            return _retired(request.path, vm)
        if loc != _keeper_locus():
            return await _central_todo_get(request, loc)
        # a dropdown alias of THIS host falls through to the host board below
    # host keeper (board-sot 2026-09-18): the central DB `todos` slice is the
    # source of truth, the local file is the FAILSAFE. Serve the central board
    # when it is reachable AND at least as complete as the file; otherwise serve
    # the file. SAFETY INVARIANT: never show FEWER items than the file has — so a
    # DB that is unreachable, errors, or is momentarily shorter than the file
    # (e.g. before the reverse sync has back-filled it) silently keeps the file.
    kloc = _keeper_locus()
    if not kloc:                     # 1.0.141: never fall back to vm_mgr's `keeper` slice
        return _unconfigured_response("board")
    try:
        central = await _central_todo_state(request.app, kloc)
    except Exception:
        central = None
    state, err = _mct_todo_read()
    if central is not None and (state is None
                                or len(central["items"]) >= len(state["items"])):
        return web.json_response({"ok": True, "state": central, "central": True,
                                  "locus": kloc,
                                  "path": _central_todo_path(kloc)})
    if state is None:
        return web.json_response({"ok": False, "error": err}, status=502)
    _operator_scripts_sync(kloc, state["items"], "file:" + str(_mct_todo_path()))
    return web.json_response({"ok": True, "state": state, "path": str(_mct_todo_path())})


async def api_board_operator_draft_run(request):
    """POST /api/board/operator/{id}/draft-run — ask B to DRAFT a RUN block
    (BOARD-ITEM-FORMAT.md) from an operator item's WHY/DO/VERIFY/EXPECT when it
    has none, and file the draft as a comment on the item. Never applied: the
    keeper (or the operator) moves it into a RUN: section by hand. Unused by
    the UI by default (1.0.144)."""
    tid = str(request.match_info.get("id") or "").strip()
    kloc = _keeper_locus()
    if not kloc:
        return _unconfigured_response("board")
    try:
        rows = await _ts_call(request.app, "todo/list", {"locus": kloc, "limit": CENTRAL_TODO_LIMIT}, timeout=15)
    except Exception as e:                                   # noqa: BLE001
        return web.json_response({"ok": False, "error": f"central board unreachable — {e}"}, status=502)
    row = next((r for r in (rows or []) if str(r.get("id") or "") == tid), None)
    if not row or row.get("type") != "operator":
        return web.json_response({"ok": False, "error": "no such operator item"}, status=404)
    note = str(row.get("note") or "")
    if OPS.run_blocks(note).get("RUN") is not None:
        return web.json_response({"ok": False, "error": "item already has a RUN block"}, status=409)
    prompt = ("Draft the RUN: block of this hugpy board operator item. Output ONLY one fenced ```bash "
              "block: a complete script starting with '#!/usr/bin/env bash' and 'set -euo pipefail', "
              "a comment with the item id and title, explicit absolute 'cd' where a step needs a "
              "directory, the DO steps in order, then the VERIFY checks written as 'test' commands "
              "that exit non-zero on failure, with the expected output as comments. It runs as the "
              "operator from ~ on the host; use sudo / ssh <host> inline. No placeholders.\n\n"
              f"id: {tid}\ntitle: {row.get('text')}\n\n{note}")
    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(None, _todo_keeper_chat, [{"role": "user", "content": prompt}], 900)
    except Exception as e:                                   # noqa: BLE001
        return web.json_response({"ok": False, "error": f"B: {e}"}, status=502)
    m = re.search(r"```(?:bash|sh)?\s*\n(.*?)```", reply or "", re.DOTALL)
    if not m or "#!/usr/bin/env bash" not in m.group(1):
        return web.json_response({"ok": False, "error": "B returned no usable script"}, status=502)
    draft = m.group(1).strip("\n")
    comment = ("[comment B@%s %s] DRAFT RUN (not applied — review, then move it into a RUN: section):\n```bash\n%s\n```"
               % (_station_id(), time.strftime("%Y-%m-%d %H:%M"), draft))
    try:
        await _ts_call(request.app, "todo/update", {"id": tid, "note": note.rstrip("\n") + "\n" + comment}, timeout=15)
    except Exception as e:                                   # noqa: BLE001
        return web.json_response({"ok": False, "error": f"comment not saved — {e}", "draft": draft}, status=502)
    return web.json_response({"ok": True, "id": tid, "draft": draft, "applied": False})


async def mct_todo_post(request):
    """POST /api/mct/todo (and /api/vm/keeper/todo) - the canonical vm_mgr todo
    op set, verbatim: add{item:{...}} | update{id, fields:{...}} | del{id} |
    replace{items:[...]}. The UI's legacy verbs (add{text,...}, set, edit,
    remove, resolve, comment) are translated onto those ops so both dialects
    run through ONE engine (todo_apply). fresh read -> apply -> write is
    serialized under the board's flock (o10 v4); a decision rides in on note +
    status (the canonical proposal contract). Mutations journal CANONICAL
    events ({event, id, type, detail}) that the drawer's history tab renders
    natively (added/status/text/note/comment/removed).
    1.0.138: every non-host locus kind writes ITS central `todos` rows."""
    vm = await _sidecar_vm(request)
    if vm:
        loc = _central_locus(vm)
        if not loc:
            return _retired(request.path, vm)
        if loc != _keeper_locus():
            return await _central_todo_post(request, loc)
    if not _keeper_locus():          # 1.0.141: no silent write into another locus's slice
        return _unconfigured_response("board")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    verb = (body.get("op") or "").strip()
    events = []
    triage = None
    try:
        with _todo_locked():
            state, err = _mct_todo_read()
            if state is None:
                return web.json_response({"ok": False, "error": err}, status=502)
            items = state["items"]

            def find(i):
                for it in items:
                    if isinstance(it, dict) and it.get("id") == i:
                        return it
                return None

            # legacy-verb translation (UI dialect) -> canonical ops
            if verb == "add" and "item" not in body:
                body = {"op": "add", "item": {
                    "type": body.get("type"), "text": body.get("text"),
                    "note": body.get("note"), "priority": body.get("priority"),
                    "by": "operator"}}
            elif verb == "set":
                body = {"op": "update", "id": body.get("id"),
                        "fields": {"status": body.get("status")}}
            elif verb == "edit":
                f = {k: body[k] for k in ("text", "note", "priority") if k in body}
                if "priority" in body and body.get("priority") not in ("medium", "high"):
                    f["priority"] = "low"     # canonical: low pops the field
                body = {"op": "update", "id": body.get("id"), "fields": f}
            elif verb == "remove":
                body = {"op": "del", "id": body.get("id")}
            elif verb == "resolve":
                if body.get("verdict") not in ("accepted", "declined"):
                    return web.json_response(
                        {"error": "verdict must be accepted|declined"}, status=400)
                it = find(body.get("id"))
                if not it:
                    return web.json_response({"error": "no such item"}, status=404)
                body = {"op": "update", "id": body.get("id"),
                        "fields": {"status": "done",
                                   "note": "[" + body["verdict"] + "] "
                                           + (it.get("note") or "")}}
            elif verb == "comment":
                ctext = (body.get("text") or "").strip()
                if not ctext:
                    return web.json_response({"error": "comment needs text"},
                                             status=400)
                it = find(body.get("id"))
                if not it:
                    return web.json_response({"error": "no such item"}, status=404)
                comments = [c for c in (it.get("comments") or [])
                            if isinstance(c, dict)]
                comments.append({"by": "operator", "ts": int(time.time()),
                                 "text": ctext})
                body = {"op": "update", "id": body.get("id"),
                        "fields": {"comments": comments}}

            op = body.get("op")
            prior = find(body.get("id")) if op in ("update", "del") else None
            prior = dict(prior) if isinstance(prior, dict) else None
            state, err = todo_apply(state, body)
            if state is None:
                code = 404 if str(err).startswith("no item") else 400
                return web.json_response({"ok": False, "error": err}, status=code)
            try:
                _mct_todo_write(state)
            except OSError as e:
                return web.json_response({"error": str(e)}, status=500)
            # canonical journal events, in the shapes histLine renders
            if op == "add" and state["items"]:
                it = state["items"][-1]
                events.append({"event": "added", "id": it.get("id"),
                               "type": it.get("type"),
                               "detail": {"text": it.get("text")}})
            elif op == "update" and prior:
                f = body.get("fields") or {}
                i, t = prior.get("id"), prior.get("type")
                if verb == "comment":
                    c = (f.get("comments") or [{}])[-1]
                    events.append({"event": "comment", "id": i, "type": t,
                                   "detail": {"by": c.get("by"),
                                              "text": c.get("text")}})
                else:
                    if str(f.get("status", "")).lower() in MCT_TODO_STATUSES:
                        events.append({"event": "status", "id": i, "type": t,
                                       "detail": {"from": prior.get("status"),
                                                  "to": str(f["status"]).lower()}})
                    if "text" in f and str(f.get("text") or "").strip():
                        events.append({"event": "text", "id": i, "type": t,
                                       "detail": {"to": str(f["text"]).strip()[:500]}})
                    if "note" in f and verb != "resolve":
                        events.append({"event": "note", "id": i, "type": t,
                                       "detail": {"to": str(f.get("note") or "").strip()[:2000]}})
                    if not events:
                        events.append({"event": "update", "id": i, "type": t,
                                       "detail": {}})
            elif op == "del" and prior:
                events.append({"event": "removed", "id": prior.get("id"),
                               "type": prior.get("type"),
                               "detail": {"text": prior.get("text")}})
            elif op == "replace":
                events.append({"event": "replace", "id": "",
                               "detail": {"n": len(state["items"])}})
            # ☑ board triage capture (dispatched OUTSIDE the lock): fresh
            # operator adds and operator comments ping B for routing; A's and
            # B's own writes never do (no self-trigger loops).
            if FV_TODO_TRIAGE:
                if op == "add" and state["items"]:
                    it = state["items"][-1]
                    if (str(it.get("by")) not in ("A", "B", "hugpy")
                            and it.get("type") in ("todo", "request", "operator")):
                        triage = (dict(it), "add", None)
                elif verb == "comment" and prior is not None:
                    cl = (body.get("fields") or {}).get("comments") or [{}]
                    last = cl[-1] if isinstance(cl[-1], dict) else {}
                    if str(last.get("by")) not in ("A", "B", "hugpy"):
                        triage = (dict(prior), "comment",
                                  str(last.get("text") or ""))
    except OSError as e:
        return web.json_response({"error": str(e)}, status=503)
    for ev in events:
        _todo_hist(ev)
    if triage is not None:
        asyncio.create_task(_todo_triage(*triage))
    # host keeper dual write (board-sot 2026-09-18): the local file is the
    # failsafe and was just written atomically above; mirror the SAME op into
    # the central DB `todos` slice under the one canonical locus. A DB failure
    # never fails the request — the file already holds the change — it only
    # surfaces as a `warning`. Reuses _central_todo_post (it re-reads the cached
    # request body, so it sees the original UI-dialect op and its full add /
    # update / del / resolve / comment mapping, incl. note-encoded comments).
    warning = None
    try:
        mresp = await _central_todo_post(request, _keeper_locus())
        if getattr(mresp, "status", 200) != 200:
            try:
                mj = json.loads((mresp.body or b"{}").decode("utf-8", "replace"))
            except Exception:
                mj = {}
            warning = ("central mirror not applied (" + str(mresp.status) + "): "
                       + str(mj.get("error", ""))[:160])
    except Exception as e:
        warning = "central mirror failed (change saved to the local file): " + str(e)[:160]
    payload = {"ok": True, "state": state, "path": str(_mct_todo_path())}
    if warning:
        payload["warning"] = warning
    return web.json_response(payload)


async def mct_todo_history(request):
    """GET /api/mct/todo/history - recent board actions (newest first)."""
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
    p = _todo_history_path()
    out = []
    try:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines()[-200:]:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    out.reverse()
    return web.json_response({"ok": True, "events": out})


_TODO_ASSIST_PROMPTS = {
    "reword": "Reword this to-do item to be crisp and actionable. Return ONLY the rewritten text, one line, no preamble.",
    "split":  "Split this to-do into 2-4 concrete sub-tasks. Return ONE per line, no numbering, no preamble.",
    "tidy":   "Rewrite this board's items to be crisp and consistent, one idea each. Return ONE item per line, no numbering, no preamble.",
}


_HUGPY_CHAT_ONESHOT = r'''import json, sys
from hugpy_agent.config import load_config
from hugpy_agent.gateway import Gateway
d = json.load(sys.stdin)
gw = Gateway.from_config(load_config(overrides=d.get("overrides") or {}))
res = gw.chat(d["messages"], max_tokens=int(d.get("max_tokens") or 500))
if not res.ok:
    raise RuntimeError(res.error or "B call failed")
text = getattr(res, "text", None) or getattr(res, "content", None) or ""
if not isinstance(text, str) or not text.strip():
    raise RuntimeError("B returned no text; operator messages remain queued")
print(json.dumps({"text": text.strip()}))
'''


def _hugpy_chat(messages, max_tokens=500, overrides=None):
    """ONE gateway chat turn through hugpy_agent (B's brain). In-process when
    the server's own interpreter has the package; otherwise a one-shot exec
    under the interpreter that does (_hugpy_python — the packaged backend runs
    the system python while hugpy_agent lives in the seats venv / miniconda).
    Board t4 (operator 2026-09-10): every ✨ tidy / readability click died with
    "No module named hugpy_agent" on exactly such installs. Sync — call it from
    an executor thread."""
    try:
        from hugpy_agent.config import load_config
        from hugpy_agent.gateway import Gateway
    except ImportError:
        py = _hugpy_python()
        if not py:
            raise RuntimeError("hugpy_agent is not importable by any known python "
                               "(install hugpy-agent, or run the backend under its interpreter)")
        r = subprocess.run([py, "-c", _HUGPY_CHAT_ONESHOT],
                           input=json.dumps({"messages": messages, "max_tokens": max_tokens,
                                             "overrides": dict(_b_overrides(), **(overrides or {}))}),
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            tail = [ln for ln in (r.stderr or "").strip().splitlines() if ln.strip()][-1:] or ["exit %d" % r.returncode]
            raise RuntimeError("hugpy_agent one-shot failed: " + tail[0][:300])
        for ln in reversed((r.stdout or "").strip().splitlines()):
            try:
                return _hugpy_chat_check((json.loads(ln).get("text") or "").strip())
            except ValueError:
                continue
        raise RuntimeError("hugpy_agent one-shot returned no JSON")
    gw = Gateway.from_config(load_config(overrides=dict(_b_overrides(), **(overrides or {}))))
    res = gw.chat(messages, max_tokens=max_tokens)
    if getattr(res, "ok", True) is False:
        raise RuntimeError(str(getattr(res, "error", "B call failed")))
    return _hugpy_chat_check(getattr(res, "text", None) or getattr(res, "content", None) or "")


def _hugpy_chat_check(text):
    """Only a nonempty model answer may become a B digest or proposal."""
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("B returned no text; operator messages remain queued")
    text = text.strip()
    if text.startswith("[error:"):
        raise RuntimeError(text.strip("[]").strip())
    if text.startswith("ChatResult(") and "native_tool_calls=" in text:
        raise RuntimeError("B returned a result object instead of an answer; messages remain queued")
    return text


def _mct_b_collate(messages):
    """Compile queued operator messages into ONE prompt for frontier A.

    Default is VERBATIM: the operator's words reach A exactly as typed, and
    multiple queued messages are joined unchanged. This deliberately replaces
    B's old model-rewrite, which paraphrased operator prompts and "flagged
    ambiguities" (mangling them) and — because it re-ran on every 1s poll tick
    with no backoff — fanned a short burst of messages into many metered model
    calls. Set MCT_B_MODEL_COLLATE=1 to restore the old model-driven rewrite."""
    if not _b_enabled():
        raise RuntimeError("B is disabled; messages are saved until B is enabled")
    texts = [(m.get("text") or "").strip() for m in messages]
    texts = [t for t in texts if t]
    if not texts:
        raise RuntimeError("B returned an empty digest")
    if os.environ.get("MCT_B_MODEL_COLLATE") != "1":
        # Verbatim: no model call, no paraphrase, no per-tick fan-out.
        return texts[0] if len(texts) == 1 else "\n\n---\n\n".join(texts)
    base = os.environ.get("MCT_B_BASE")
    if not base and _port_open(7002):
        base = _LOCAL_HUGPY_FRONT + "/api/v1"
    overrides = {"timeout": 60}
    if base:
        overrides["base"] = base
    instruction = """You are B, the local keeper. Compile the operator messages below into ONE prompt for frontier A.
Follow QUERY-ARBITRATION-SOP: B infers wording; C dictates disposition. Do not execute the requests.
Preserve every concrete requirement, constraint and correction; remove repetition and superseded wording.
When intent is ambiguous, retain the original wording and explicitly flag the ambiguity. Do not invent intent.
Keep relevant provenance and reconcile instructions. Do not select a different lane or create extra tasks.
Attachments and original messages remain available through the supplied pointers; do not invent their contents.
Return only the compiled prompt, with no answer or commentary. The station adds the B-digest marker and provenance."""
    answer = _hugpy_chat([{"role": "system", "content": instruction},
                          {"role": "user", "content": json.dumps(messages, ensure_ascii=False)}],
                         max_tokens=2400, overrides=overrides)
    if not answer.strip():
        raise RuntimeError("B returned an empty digest")
    return answer


def _mct_b_capture(message):
    """C-explicit capture: idempotent board item with original context pointer."""
    marker = "mct-capture:" + message["id"]
    with _todo_locked():
        for item in _mct_todo_read().get("items", []):
            if marker in str(item.get("note", "")):
                return item["id"]
    compiled = _mct_b_collate([message])
    with _todo_locked():
        state = _mct_todo_read()
        for item in state.get("items", []):
            if marker in str(item.get("note", "")):
                return item["id"]
        state, error = todo_apply(state, {"op": "add", "by": "operator", "item": {
            "type": "todo", "status": "open", "by": "operator", "text": compiled[:500],
            "note": marker + "\nOriginal message and attachments: " + message["context"] + "\n" + compiled[:1400]}})
        if error:
            raise RuntimeError(error)
        _mct_todo_write(state)
        return state["items"][-1]["id"]


def _todo_assist_call(mode, text):
    sysmsg = _TODO_ASSIST_PROMPTS.get(mode, _TODO_ASSIST_PROMPTS["reword"])
    return _hugpy_chat([{"role": "system", "content": sysmsg},
                        {"role": "user", "content": text}], max_tokens=500)


async def _run_timeout(args, timeout):
    """_run with a hard timeout: the child is killed when it overruns (rc -1)."""
    if _tmux_missing(args):
        return 127, "", TMUX_MISSING_MSG
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return -1, "", "timeout"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _tmux_type_line(sess, line, timeout=8):
    """Type ONE line + Enter into a tmux session's active pane, bounded.
    `tmux send-keys -l <long line>` has been seen to block indefinitely when
    the pane's pty input is not being drained (2026-09-10: two stuck senders
    hung their request handlers) — so each step is time-limited and a stuck
    sender is killed rather than left waiting. Returns True when both the
    text and the Enter went through."""
    tk = ["tmux", "-L", KEEPER_TMUX_SOCK, "send-keys", "-t", "=" + sess + ":"]
    rc, _o, _e = await _run_timeout(tk + ["-l", line], timeout)
    if rc != 0:
        return False
    await asyncio.sleep(0.6)
    rc, _o, _e = await _run_timeout(tk + ["Enter"], timeout)
    return rc == 0


async def _frontier_pane_type(line):
    """Type ONE line + Enter into the live frontier seat's pane: the claude-code
    seat when one is live, else the mct pointer-exchange REPL session. Claude
    Code queues input that arrives mid-turn, so nothing in flight is disturbed.
    Returns the tmux session typed into, '' when no live seat."""
    sess = await _live_frontier_session()
    if not sess:
        cand = _tmux_session_for("frontier", "mct") or "keeper-mct"
        rc, _o, _e = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "has-session", "-t", "=" + cand)
        sess = cand if rc == 0 else ""
    if not sess:
        return ""
    return sess if await _tmux_type_line(sess, line) else ""


async def mct_todo_ping(request):
    """POST /api/mct/todo/ping {id, text?} — board t2 (operator 2026-09-10): the
    📨 button on a board row pings the keeper about THAT item. Types one line
    into the live frontier seat naming the item, its status/priority and text
    (plus an optional operator note), and journals it. Not a board mutation.
    1.0.138: a non-host locus is pinged on its central board (comms lane)."""
    vm = await _sidecar_vm(request)
    if vm:
        loc = _central_locus(vm)
        if not loc:
            return _retired(request.path, vm)
        if loc != _keeper_locus():
            return await _central_todo_ping(request, loc)
    try:
        body = await request.json()
        tid = str(body.get("id") or "").strip()
        if not tid:
            raise ValueError
    except Exception:
        return web.json_response({"error": 'body must be {"id"}'}, status=400)
    state, err = _mct_todo_read()
    if state is None:
        return web.json_response({"ok": False, "error": err}, status=502)
    it = next((i for i in (state.get("items") or []) if i.get("id") == tid), None)
    if not it:
        return web.json_response({"ok": False, "error": f"no board item {tid}"}, status=404)
    extra = " ".join(str(body.get("text") or "").split())[:200]
    line = (f"📨 board {tid} [{it.get('status') or 'open'}/{it.get('priority') or 'low'}] from the operator: "
            + " ".join(str(it.get("text") or "").split())[:300]
            + (f" — {extra}" if extra else "")
            + " — please tend to it and update the item on the board (POST /api/mct/todo op=set / comment).")
    sess = await _frontier_pane_type(line)
    if not sess:
        return web.json_response({"ok": False, "error": "no live frontier seat to ping"}, status=502)
    _audit_line("todo-ping", f"{tid} → {sess}")
    return web.json_response({"ok": True, "id": tid, "session": sess})


async def mct_todo_assist(request):
    """POST /api/mct/todo/assist {mode, text} - LLM reword/split/tidy via the hugpy
    gateway. Returns {result} (a preview the operator applies); never mutates the board.
    Pure text in / text out on this station's LLM, so it serves EVERY locus
    (1.0.138: no longer 404s off-host)."""
    try:
        body = await request.json()
        mode = (body.get("mode") or "reword").strip()
        text = (body.get("text") or "").strip()
        if not text:
            raise ValueError
    except Exception:
        return web.json_response({"error": 'body must be {"mode","text"}'}, status=400)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _todo_assist_call, mode, text)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"assist unavailable: {e}"}, status=502)
    return web.json_response({"ok": True, "result": result})


# ✎ B readability revision of an operator prompt against docs/NOMENCLATURE.md
# (operator, 2026-08-22): B corrects wording that is UNAMBIGUOUSLY mislabelled
# per the glossary and asks for clarity where a term is ambiguous. Preview
# only — the composer applies it; nothing is sent.
_PROMPT_REVISE_SYS = """You are B, the local keeper of a hugpy Station. Revise the operator's prompt for
readability and naming consistency using ONLY the glossary below. Rules:
- Fix typos and grammar; keep the operator's intent, tone and every concrete detail. Do not add ideas.
- Where a word is used in a sense the glossary says is WRONG and the intended term is unambiguous
  (e.g. "instance" or "station" for the machine the dropdown selects -> "locus"; "model" for a
  seat's program -> "backend"), correct it and list the correction.
- Where a term could mean more than one glossary sense and the context does not settle it
  (e.g. bare "keeper", bare "session"), do NOT guess: leave it, and list a short clarifying question.
- Return ONLY JSON: {"revised": "<full revised prompt>", "corrections": [{"from": "...", "to": "...", "why": "..."}],
  "ambiguities": [{"span": "...", "question": "..."}]}

GLOSSARY (docs/NOMENCLATURE.md):
"""


def _prompt_revise_call(text):
    try:
        glossary = (STATIC / "docs" / "NOMENCLATURE.md").read_text(encoding="utf-8")
    except OSError:
        glossary = "(glossary missing)"
    raw = _hugpy_chat([{"role": "system", "content": _PROMPT_REVISE_SYS + glossary},
                       {"role": "user", "content": text}], max_tokens=1200)
    m = re.search(r"```[a-zA-Z]*\s*\n(.*?)```", raw, re.DOTALL)
    body = m.group(1) if m else raw
    i, j = body.find("{"), body.rfind("}")
    d = json.loads(body[i:j + 1]) if i >= 0 and j > i else {}
    return {"revised": str(d.get("revised") or "").strip(),
            "corrections": [c for c in (d.get("corrections") or []) if isinstance(c, dict)],
            "ambiguities": [a for a in (d.get("ambiguities") or []) if isinstance(a, dict)],
            "raw": raw if not d else ""}


async def mct_prompt_revise(request):
    """POST /api/mct/prompt/revise {text} -> {revised, corrections[], ambiguities[]}."""
    try:
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            raise ValueError
    except Exception:
        return web.json_response({"error": 'body must be {"text"}'}, status=400)
    try:
        out = await asyncio.get_event_loop().run_in_executor(None, _prompt_revise_call, text)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"B unavailable: {e}"}, status=502)
    out["ok"] = True
    return web.json_response(out)


# Standing charge for the Local Keeper's host workspace: written once (operator
# edits survive), docs symlinked in so ./docs/ always matches the installed
# console. The local surface cd's here, so opencode/qwen pick AGENTS.md up as
# their standing instructions.
# 1.0.85: the 1.0.84 text is kept VERBATIM as _LOCAL_KEEPER_AGENTS_V1 so an
# untouched v1 file is recognised (and upgraded); the live charge below carries
# a version marker on its first line for the same purpose from now on.
_LOCAL_KEEPER_AGENTS_V1 = """\
# Local Keeper — fleet console host workspace

You are B, the Local Keeper, running host-side in the hugpy Station's
**local seat**. The operator (C) talks to you directly here. The frontier
keeper (A) is a separate seat — you never answer in its voice.

## Nomenclature (fixed — use these words, in these senses)
Full table: ./docs/NOMENCLATURE.md. A **locus** is the machine the seats
ground in (`kind`: `lxd` guest, `ssh` host, or `host` = `@keeper`). A **seat**
is one attachable terminal surface on a locus — A (frontier), B/local, shell.
A **backend** is the program a seat runs (`mct`/`claude-code`;
`opencode`/`qwen-code`; `exec`/`ssh`), each in its own tmux session. A
**model** is the LLM a backend calls. Qualify "keeper" and "session" when
ambiguous; never say "instance" — say locus.

## To-do boards (fully in your reach)
- Each station keeps its board at ~/todo.json INSIDE the VM (schema todo.v1).
- Read/edit via: `lxc exec <station> -- cat /home/ubuntu/todo.json`
  (write back only VALID JSON — the console refuses a malformed board, and a
  broken board is an outage for the operator).
- BEFORE actioning any item, read and follow ./docs/TODO-BOARD-SOP.md. The SOP
  is the contract: adhere to it whenever you actionize a to-do.

## Design & flow (◳ canvas)
- Design (wireframe.v1) and flow (flow.v1) documents live in the toolserver
  `canvas` table, one per (locus, kind): read with `canvas/get`
  (MCP tool `canvas_get`) and write with `canvas/put` (`canvas_put`, whole
  document, validated; add `notify=true` for a deliberate hand-off). The
  console's ◳ canvas tab shows the same row live. Do NOT write ~/wireframe.json
  or ~/flow.json — nothing reads them any more. Formats: ./docs/UI-GUIDE.md.

## Mid-turn query arbitration
- When C sends a message while A's turn is in flight, dispose of it per
  ./docs/QUERY-ARBITRATION-SOP.md — four lanes: interrupt (C-explicit only),
  coalesce (default; marked digest), append (own turn after), capture (to-do,
  no turn). B infers wording; C dictates disposition. Never interrupt or
  capture on your own inference.

## Canonical docs — read before acting
- ./docs/TODO-BOARD-SOP.md   — the to-do board contract
- ./docs/QUERY-ARBITRATION-SOP.md — mid-turn query arbitration (the four lanes)
- ./docs/KEEPER-DEV-GUIDE.md — keeper development guide
- ./docs/UI-GUIDE.md         — console UI, design/flow drawer formats
"""


# 1.0.85: the live charge (v2). First line = the version marker _local_keeper_ws()
# keys on. Nomenclature-correct (the host is "⌂ host"), names the toolserver MCP
# bridge + the station HTTP API as B's tools, points at docs/STATION-TOOLS.md.
LOCAL_KEEPER_AGENTS = """\
<!-- hugpy-station local-keeper charge v2 -->
# Local keeper (B) — standing charge for the ⌂ host workspace

You are **B, the local keeper**: the free, on-hardware model seated in the
hugpy Station's **local seat** on the ⌂ host locus. The operator (**C**) talks
to you directly here. The frontier keeper (**A**) is a separate, metered seat —
you never answer in its voice; you do its legwork (search, reading, bulk
output) so its tokens go to judgement, and you return pointers, not dumps.

## Vocabulary (fixed by the operator — ./docs/NOMENCLATURE.md)
- **locus** = a machine (`kind`: `lxd` guest, `ssh` host, `host` = this one, shown **⌂ host**). Never say "instance".
- **seat** = one terminal surface on a locus: **A** (frontier), **B/local** (you), **shell**.
- **backend** = the program a seat runs (A: `claude-code`/`mct`; B: `opencode`/`qwen-code`; shell: `exec`/`ssh`); **model** = the LLM it calls.
- **keeper** = an AGENT, never a machine: frontier keeper (A), local keeper (B). Qualify it.
- **C** = the operator. **station** = the product (hugpy Station) only — never a machine or a locus.
- Sign every board write `<locus>-keeper` (the host's keeper signs `host-keeper`); never bare `keeper`.

## Your tools

### 1. The toolserver MCP bridge (native tools)
`abstract-claude mcp` bridges the toolserver (discovered on this host, or your configured URL) into your seat: every
tool `/<prefix>/<name>` is the native tool `<prefix>_<name>`. The full reference
with every signature is **./docs/STATION-TOOLS.md** — read it first. Families:

- **Boards** — `todo_list {status,type,limit,locus}`, `todo_add {text,type,priority,note,by,locus}`,
  `todo_update {id,status|priority|text|note}`, `todo_done {id}`, `todo_remove {id}`.
  `locus` = WHOSE board the item is on (`''` = global). Read ./docs/TODO-BOARD-SOP.md BEFORE acting on any item.
- **Comms** — `comms_ping {to,text,from_,ref}`, `comms_inbox {to,since}`. The protocol: a ping lands
  as a `[ping]` high-priority request on the TARGET locus's board; that locus's station leg turns it
  into ✉ mail and types ONE 📨 nudge into its frontier pane. To answer one: read
  `comms_inbox to=<your locus>`, reply ON the board (`todo_update` note / `todo_done`) and
  `comms_ping` back, signed `<locus>-keeper`. This station's own locus is `STATION_LOCUS`
  (in `/api/toolserver/config` and the `locus` field of `/api/frontier/state`).
- **Canvas** — `canvas_get {locus, kind: design|flow}`, `canvas_put {locus,kind,state,by,note,notify}`:
  whole documents (wireframe.v1 / flow.v1), one row per (locus, kind); `notify=true` only for a deliberate hand-off.
- **Central DB (READ-ONLY)** — `db_tables`, `db_schema`, `db_columns {table_name}`,
  `db_query {query,values}` (SELECT/WITH only, writes are rejected; a literal `%` must be written `%%`),
  `db_fetch {table_name,column_names,search_map,limit}`. The 14 tables: todos, loci, exchanges, handoffs,
  handoff_state, prompts, board, canvas, assessments, seat_state, compute_actions, call_metrics,
  call_metrics_by_task, load_metrics.
- **Loci** — `loci_list {kind,status}`, `loci_register {locus,kind,goal,endpoint,pointer}`, `loci_pointers`.
- **Seats & sessions** — `handoff_request {locus,text,task,fork}` (the toolserver row IS the handoff;
  `handoff_pull {locus|id}` = /resume: the successor pulls it, consumed once per session), `session_pull {brief,name,fork}`,
  `exchange_list {locus,limit,since}`, `assess_state {locus}`, `seat_state {locus,seat}`.
- **Prompt inbox** — `prompt_list {locus,status}`, `prompt_get {id,with_files}`, `prompt_done {id,status}`.
- **Execute ON the toolserver host (ae), NOT on this locus** — `fs_*`, `sys_run_cmd`, `ui_*`, `media_*`,
  `image_*`, `web_*`: the paths you pass them are ae paths. For THIS machine use your own shell.

### 2. The station HTTP API (loopback)
Base `http://127.0.0.1:${PORT:-8899}` (the desktop shell exports PORT=8899; a headless
leg uses its unit's PORT). Useful routes:
- `GET /api/vms` — every locus (LXD rows + ssh hosts); `GET /api/seat?vm=<locus>` — what that locus's seats launch with.
- `GET /api/frontier/state` — the rolling state + init prompt of this locus (its `locus` field names it).
- `GET|POST /api/vm/<locus>/todo` — a board (todo.v1); `GET /api/vm/keeper/messages` — the ⌂ host ✉ inbox.
- `POST /api/fleet/message {"to":"<locus>","text":"...","from":"<you>"}` — send mail; an ssh locus gets a board ping.
- `GET|POST /api/b/model` — your own model (`{"model":""}` = the package default).
- `/ts/<prefix>/<name>` — same-origin proxy to the toolserver for the allow-listed prefixes
  (assess, handoff, exchange, todo, loci, board, session, db, comms, canvas, seat, prompt).
- `GET /api/toolserver/config` — the toolserver URL, whether a token is set, this station's loci sync.

## How to read the docs (./docs is the shipped set, always current)
1. **./docs/STATION-TOOLS.md** — tools, DB, comms, API, files on disk, troubleshooting. FIRST.
2. ./docs/TODO-BOARD-SOP.md — the board contract (before actioning any item).
3. ./docs/NOMENCLATURE.md — the words.
4. ./docs/QUERY-ARBITRATION-SOP.md — mid-turn queries: four lanes; B infers wording, C dictates disposition.
5. ./docs/KEEPER-DEV-GUIDE.md — keeper development.
6. ./docs/UI-GUIDE.md — the console UI and the design/flow document formats.

## Honesty rules (non-negotiable)
- **Verify after write.** Read back what you changed (the file, the board item, the canvas rev) before you report it.
- **Receipts.** Every "done" carries its evidence: the command and its output, the id, the rev, the path.
- **Never mark done without evidence.** `todo_done` only after the verification step; otherwise `doing` + a note saying what is left.
- **Never print secrets.** Tokens (HUGPY_OPERATOR_TOKEN, API keys, oauth), key files and .env bodies
  never enter a transcript, a board note or a canvas — say "token set", never its value.
- **Report failures as failures**, with the error text — never as something you could not verify.
"""
LOCAL_KEEPER_CHARGE_VERSION = 2
_LOCAL_KEEPER_MARKER_RE = re.compile(r"^<!--\s*hugpy-station local-keeper charge v(\d+)\s*-->\s*$")
# 1.0.85: md5 (utf-8) of EVERY charge body ever shipped without a marker — the
# four LOCAL_KEEPER_AGENTS texts in the monorepo's git history. A station
# installed before 1.0.84 still carries one of the older three (this desktop
# had the 8994a27d "clawd-code" text), and only the exact 1.0.84 body is
# _LOCAL_KEEPER_AGENTS_V1 — so the upgrade must recognise all of them, or B
# (and qwen-code, which reads nothing but QWEN.md → AGENTS.md) keeps a charge
# with no tools section forever. Anything NOT in this set and without our
# marker is operator-customized and is never touched.
_LOCAL_KEEPER_SHIPPED_MD5 = {
    "3a725a1402d29f3e0924a2e7314531a3",   # d71b1be3 · 1.0.84 (== _LOCAL_KEEPER_AGENTS_V1)
    "5c53846e7ff72fd9b5fbe579589063fb",   # 30a9a10b
    "c4580dd09b1d55335014ad761a2f6bc9",   # 8994a27d · "clawd-code" era
    "98b85e8ce5d9e3737d897917bc637f11",   # f6e23988 · first charge
}


def _local_keeper_charge_stale(text):
    """1.0.85: True when an AGENTS.md is OURS and older than the shipped charge —
    the exact v1 text, any historical shipped body (_LOCAL_KEEPER_SHIPPED_MD5),
    or our marker line with a lower version. Anything else is
    operator-customized and must be left alone."""
    if text == _LOCAL_KEEPER_AGENTS_V1:
        return True
    if hashlib.md5(text.encode("utf-8")).hexdigest() in _LOCAL_KEEPER_SHIPPED_MD5:
        return True
    m = _LOCAL_KEEPER_MARKER_RE.match((text.splitlines() or [""])[0])
    return bool(m) and int(m.group(1)) < LOCAL_KEEPER_CHARGE_VERSION


def _write_json_atomic(path, doc):
    """1.0.85: tmp + os.replace so a seat never reads a half-written config.
    The path is resolved first: a dotfiles-managed ~/.config/opencode/opencode.json
    or ~/.qwen/settings.json is commonly a symlink, and os.replace over the LINK
    would swap it for a regular file and silently detach the operator's copy."""
    path = Path(path).resolve()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _fit_local_seat_tools(lk):
    """1.0.85: fit the local seat's backends (opencode, qwen-code) with the
    toolserver MCP bridge + the standing charge, the way _SEAT_TRUST_B64 fits
    every claude-code seat. Idempotent, best-effort (OSError/ValueError are
    swallowed per backend), atomic, and every other key in a seat's config
    survives. NO literal token is ever written: a seat spawned by the station
    inherits HUGPY_OPERATOR_TOKEN (toolserver.env, loaded at startup) and the
    config references it by NAME ({env:...} for opencode, $VAR for qwen-code).
    An existing `toolserver` entry is only COMPLETED (missing keys filled in)
    when it is our own bridge (command abstract-claude) — an operator's
    "enabled": false, custom TOOLSERVER_URL, timeout or any extra key survives
    every backend start / seat launch; a custom server is never touched. One
    audit line, only when something changed."""
    changed = []
    # 2026-09-30: only an EXPLICITLY configured URL is written into a seat's
    # MCP entry; without one the bridge discovers the local toolserver itself.
    url = _ts_configured_url()
    home = Path.home()
    # a. opencode — global config (~/.config/opencode/opencode.jsonc wins when it
    #    parses as JSON; comments/trailing commas → leave it alone, fit .json).
    #    `instructions` carries the charge + the tools doc because the launcher
    #    (hugpy-agent console --frontend opencode) chdirs away from the ws, so a
    #    project AGENTS.md there is NOT picked up.
    try:
        ocdir = home / ".config" / "opencode"
        ocdir.mkdir(parents=True, exist_ok=True)
        cfg, doc = ocdir / "opencode.jsonc", None
        if cfg.is_file():
            try:
                doc = json.loads(cfg.read_text(encoding="utf-8"))
            except ValueError:
                doc = None
        if not isinstance(doc, dict):
            cfg = ocdir / "opencode.json"
            doc = json.loads(cfg.read_text(encoding="utf-8")) if cfg.is_file() else {}
        if isinstance(doc, dict):
            before = json.dumps(doc, sort_keys=True)
            mcp = doc.get("mcp") if isinstance(doc.get("mcp"), dict) else {}
            cur = mcp.get("toolserver") if isinstance(mcp.get("toolserver"), dict) else {}
            cmd = cur.get("command")
            cmd0 = (cmd[0] if isinstance(cmd, list) and cmd else
                    (cmd.split() or [""])[0] if isinstance(cmd, str) else "")
            if not cur:
                _env = {"TOOLSERVER_TOKEN": "{env:HUGPY_OPERATOR_TOKEN}"}
                if url:
                    _env["TOOLSERVER_URL"] = url
                mcp["toolserver"] = {"type": "local", "command": ["abstract-claude", "mcp"],
                                     "environment": _env, "enabled": True}
            elif cmd0 == "abstract-claude":
                # ours — merge, never assign: an operator's enabled:false / own
                # URL / timeout / extra keys are theirs to keep.
                cur.setdefault("type", "local")
                cur.setdefault("command", ["abstract-claude", "mcp"])
                cur.setdefault("enabled", True)
                env = cur.get("environment") if isinstance(cur.get("environment"), dict) else {}
                if url:
                    env.setdefault("TOOLSERVER_URL", url)
                env.setdefault("TOOLSERVER_TOKEN", "{env:HUGPY_OPERATOR_TOKEN}")
                cur["environment"] = env
                mcp["toolserver"] = cur
            doc["mcp"] = mcp
            ins = doc.get("instructions")
            ins = list(ins) if isinstance(ins, list) else ([ins] if isinstance(ins, str) and ins else [])
            # MCP exposes the tool schemas natively; avoid injecting the full
            # reference document into every OpenCode prompt.
            for p in (str(lk / "AGENTS.md"),):
                if p not in ins:
                    ins.append(p)
            doc["instructions"] = ins
            if json.dumps(doc, sort_keys=True) != before:
                _write_json_atomic(cfg, doc)
                changed.append("opencode:" + cfg.name)
        link = ocdir / "AGENTS.md"       # opencode also reads this global charge
        if not link.exists() and not link.is_symlink():
            link.symlink_to(lk / "AGENTS.md")
            changed.append("opencode:AGENTS.md->local-keeper")
    except (OSError, ValueError) as e:
        logging.getLogger("station.local-seat").warning("opencode fit skipped: %s", e)
    # b. qwen-code — ~/.qwen/settings.json (Gemini-CLI style mcpServers); only
    #    the toolserver key is added to an existing file, a minimal file is
    #    created when there is none. QWEN.md in the ws is the charge (cwd = ws).
    try:
        qcfg = home / ".qwen" / "settings.json"
        doc = json.loads(qcfg.read_text(encoding="utf-8")) if qcfg.is_file() else {}
        if isinstance(doc, dict):
            before = json.dumps(doc, sort_keys=True)
            ms = doc.get("mcpServers") if isinstance(doc.get("mcpServers"), dict) else {}
            cur = ms.get("toolserver") if isinstance(ms.get("toolserver"), dict) else {}
            if not cur:
                _env = {"TOOLSERVER_TOKEN": "$HUGPY_OPERATOR_TOKEN"}
                if url:
                    _env["TOOLSERVER_URL"] = url
                ms["toolserver"] = {"command": "abstract-claude", "args": ["mcp"], "env": _env}
            elif cur.get("command") == "abstract-claude":
                # ours — merge (see the opencode branch above)
                cur.setdefault("args", ["mcp"])
                env = cur.get("env") if isinstance(cur.get("env"), dict) else {}
                if url:
                    env.setdefault("TOOLSERVER_URL", url)
                env.setdefault("TOOLSERVER_TOKEN", "$HUGPY_OPERATOR_TOKEN")
                cur["env"] = env
                ms["toolserver"] = cur
            doc["mcpServers"] = ms
            if json.dumps(doc, sort_keys=True) != before:
                qcfg.parent.mkdir(parents=True, exist_ok=True)
                _write_json_atomic(qcfg, doc)
                changed.append("qwen-code:settings.json")
    except (OSError, ValueError) as e:
        logging.getLogger("station.local-seat").warning("qwen-code fit skipped: %s", e)
    if changed:
        _audit_line("local-seat-fit", "; ".join(changed) + f" · url={url} · token by env name, never literal")
    return changed


def _local_keeper_ws(fit=True) -> "Path | None":
    """Ensure FV_STATE_HOME/local-keeper exists with AGENTS.md (+ QWEN.md → it)
    + ./docs, and — 1.0.85 — that the local seat's backends are fitted with the
    toolserver bridge (fit=False: the workspace only; startup fits separately)."""
    lk = FV_STATE_HOME / "local-keeper"
    try:
        lk.mkdir(parents=True, exist_ok=True)
        agents = lk / "AGENTS.md"
        # 1.0.85: write when absent OR when what is there is OUR older charge (the
        # exact v1 text, or our marker with a lower version) — an operator-edited
        # file is never overwritten.
        try:
            cur = agents.read_text(encoding="utf-8") if agents.exists() else None
        except OSError:
            cur = None
        if cur is None or _local_keeper_charge_stale(cur):
            agents.write_text(LOCAL_KEEPER_AGENTS, encoding="utf-8")
        # 1.0.85: qwen-code reads QWEN.md from its cwd (the local seat cd's here):
        # the same charge under the name it looks for. A plain-file QWEN.md is
        # replaced by the link only when it is ours (verbatim or any shipped
        # body); an operator's OWN symlink (→ some custom charge) is kept — only
        # a dangling link, or one that already reaches lk/AGENTS.md by another
        # spelling, is re-pointed at the relative "AGENTS.md".
        qwen = lk / "QWEN.md"
        if qwen.is_symlink():
            tgt = qwen.resolve(strict=False)
            if (not tgt.exists() or tgt == agents.resolve()) and os.readlink(str(qwen)) != "AGENTS.md":
                qwen.unlink(); qwen.symlink_to("AGENTS.md")
        elif qwen.exists():
            try:
                qtxt = qwen.read_text(encoding="utf-8")
            except OSError:
                qtxt = None
            if qtxt is not None and (qtxt in (agents.read_text(encoding="utf-8"), LOCAL_KEEPER_AGENTS)
                                     or _local_keeper_charge_stale(qtxt)):
                qwen.unlink(); qwen.symlink_to("AGENTS.md")
        else:
            qwen.symlink_to("AGENTS.md")
        docs = lk / "docs"
        want = ROOT / "static" / "docs"
        if docs.is_symlink() and docs.resolve() != want.resolve():
            docs.unlink()          # stale link (e.g. pre-upgrade install path)
        if not docs.exists():
            docs.symlink_to(want)
        if fit:
            _fit_local_seat_tools(lk)   # 1.0.85: after AGENTS/docs are in place
        return lk
    except OSError:
        return None


async def _start_local_seat_fit(app):
    if os.environ.get("STATION_DEV_QUIET") == "1":   # dev copy: never act on live seats
        return
    """1.0.85: at backend startup lay the local keeper's workspace down and fit
    the local seat's backends with the toolserver bridge — never fatal."""
    try:
        lk = _local_keeper_ws(fit=False)
        if lk is not None:
            _fit_local_seat_tools(lk)
    except Exception as e:  # noqa: BLE001 — a seat fit must never break startup
        logging.getLogger("station.local-seat").warning("local seat fit at startup: %s", e)


async def a_clear(request):
    """POST /api/a/clear {"target":"host"|"<vm>", "what":"state"|"settings"|"all"}.

    state    — remove A's cache/session dirs under ~/.claude (A_STATE_DIRS);
               credentials and settings.json are never touched.
    settings — reset ~/.claude/settings.json to the template.
    all      — both. VM targets act inside the station via lxc as the dev user.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    target = (body.get("target") or "host").strip()
    what = (body.get("what") or "state").strip()
    if what not in ("state", "settings", "all"):
        return web.json_response({"error": "what must be state|settings|all"},
                                 status=400)
    payload = json.dumps(json.loads(_a_settings_json()), indent=2) + "\n"
    did = []
    if target == "host":
        base = Path.home() / ".claude"
        if what in ("state", "all"):
            for d in A_STATE_DIRS:
                p = base / d
                if p.exists():
                    shutil.rmtree(p, ignore_errors=True)
                    did.append(f"cleared {p}")
        if what in ("settings", "all"):
            base.mkdir(parents=True, exist_ok=True)
            (base / "settings.json").write_text(payload)
            did.append(f"template -> {base / 'settings.json'}")
        return web.json_response({"ok": True, "target": "host", "did": did})
    if not _A_VM_RE.match(target):
        return web.json_response({"error": "bad target"}, status=400)
    if what in ("state", "all"):
        dirs = " ".join(shlex.quote(f"{SH_HOME}/.claude/{d}") for d in A_STATE_DIRS)
        rc, _out, err = await _lxc(target, "bash", "-lc", f"rm -rf {dirs}")
        if rc != 0:
            return web.json_response({"error": err.strip() or "state clear failed"},
                                     status=502)
        did.append("cleared state dirs")
    if what in ("settings", "all"):
        script = ("mkdir -p " + shlex.quote(f"{SH_HOME}/.claude") + " && printf %s "
                  + shlex.quote(payload) + " > "
                  + shlex.quote(f"{SH_HOME}/.claude/settings.json"))
        rc, _out, err = await _lxc(target, "bash", "-lc", script)
        if rc != 0:
            return web.json_response({"error": err.strip() or "settings reset failed"},
                                     status=502)
        did.append("template -> settings.json")
    return web.json_response({"ok": True, "target": target, "did": did})


# --------------------------------------------------------------------------- #
# REST: file browser (read/write) inside a station, as the dev user
# --------------------------------------------------------------------------- #
async def _require_station(request):
    """Return the station name if known, else raise a 404 HTTPException."""
    name = request.match_info["name"]
    if name not in await known_names():
        raise web.HTTPNotFound(text=json.dumps({"error": f"unknown station {name}"}),
                               content_type="application/json")
    return name


async def _fs_locus(request):
    """1.0.141: the 📁 files API works on EVERY locus kind. An LXD guest keeps
    its lxc file path (None here); the host (@self/@keeper/own name) and any
    ssh locus go through locus_exec.fs — one python on the target, fenced
    under that user's $HOME, payloads on stdin."""
    name = request.match_info["name"]
    if _is_host_vm(name) or name in ("@keeper", "@self"):
        return _locus_target("")
    await _loci_ready()
    if _ssh_host(name):
        return _locus_target(name)
    if name in await _known_names_safe():
        return None
    raise web.HTTPNotFound(text=json.dumps({"error": f"unknown locus {name}"}),
                           content_type="application/json")


def _fs_reply(d):
    if not isinstance(d, dict):
        return web.json_response({"error": "bad reply"}, status=502)
    if d.get("error"):
        st = 403 if "outside the locus home" in d["error"] or "refusing" in d["error"] else 400
        return web.json_response(d, status=st)
    return web.json_response(d)


async def _fs_via(t, op, request):
    if op in ("list", "read", "download"):
        arg = {"path": request.query.get("path", "")}
        d = await LX.fs(t, op, arg, cap=(FS_READ_LIMIT if op == "read" else 64 * 1024 * 1024))
        if op == "download" and isinstance(d, dict) and d.get("b64") is not None:
            fname = os.path.basename(request.query.get("path", "")) or "download"
            return web.Response(body=base64.b64decode(d["b64"]),
                                headers={"Content-Type": "application/octet-stream",
                                         "Content-Disposition": f'attachment; filename="{fname}"'})
        return _fs_reply(d)
    if op == "upload":
        dest = request.query.get("path", "")
        saved = []
        reader = await request.multipart()
        async for part in reader:
            if part.name != "file":
                continue
            rel = _safe_relname(part.filename) or "upload"
            data = await part.read(decode=False)
            d = await LX.fs(t, "write", {"path": (dest.rstrip("/") + "/" + rel) if dest else rel}, data=bytes(data))
            if d.get("error"):
                return _fs_reply(d)
            saved.append(rel)
        return web.json_response({"ok": True, "saved": saved})
    body = await request.json()
    if op == "write":
        return _fs_reply(await LX.fs(t, "write", {"path": body.get("path", "")},
                                     data=str(body.get("content", "")).encode("utf-8")))
    if op == "rename":
        return _fs_reply(await LX.fs(t, "rename", {"src": body.get("src", ""), "dst": body.get("dst", "")}))
    return _fs_reply(await LX.fs(t, op, {"path": body.get("path", "")}))


async def fs_list(request):
    _t = await _fs_locus(request)
    if _t is not None:
        return await _fs_via(_t, "list", request)
    name = await _require_station(request)
    path = _safe_path(request.query.get("path", ""))
    # %y = file type, %Y = type after symlink deref, %s size, %T@ mtime, %p path
    rc, out, err = await _lxc(
        name, "find", path, "-maxdepth", "1", "-mindepth", "1",
        "-printf", r"%y\t%Y\t%s\t%T@\t%p\n",
    )
    if rc != 0:
        return web.json_response({"error": err.strip() or "cannot list"}, status=400)
    entries = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        ftype, deref, size, mtime, fpath = parts
        is_dir = ftype == "d" or (ftype == "l" and deref == "d")
        entries.append({
            "name": os.path.basename(fpath),
            "path": fpath,
            "dir": is_dir,
            "link": ftype == "l",
            "size": int(size or 0),
            "mtime": float(mtime or 0),
        })
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    parent = os.path.dirname(path.rstrip("/")) or "/"
    return web.json_response({"path": path, "parent": parent, "entries": entries})


async def fs_read(request):
    _t = await _fs_locus(request)
    if _t is not None:
        return await _fs_via(_t, "read", request)
    name = await _require_station(request)
    path = _safe_path(request.query.get("path", ""))
    rc, out, _ = await _lxc(name, "stat", "-c", "%s", path)
    if rc != 0:
        return web.json_response({"error": "no such file"}, status=404)
    size = int(out.strip() or 0)
    if size > FS_READ_LIMIT:
        return web.json_response({"too_large": True, "size": size})
    rc, raw, err = await _run("lxc", "file", "pull", _cpath(name, path), "-")
    if rc != 0:
        return web.json_response({"error": err.strip() or "read failed"}, status=400)
    data = raw.encode("utf-8", "surrogateescape") if isinstance(raw, str) else raw
    if b"\x00" in data[:8192]:
        return web.json_response({"binary": True, "size": size})
    return web.json_response({"text": data.decode("utf-8", "replace"), "size": size})


async def _push_bytes(name, path, data):
    """lxc file push from stdin -> container path, owned by the station's fs owner.
    --create-dirs applies the same uid/gid to any parent dirs it makes."""
    uid, gid = _fs_owner(name)
    proc = await asyncio.create_subprocess_exec(
        "lxc", "file", "push", "-", _cpath(name, path),
        "--uid", uid, "--gid", gid, "--mode", "0644", "--create-dirs",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate(input=data)
    return proc.returncode, out.decode(errors="replace")


async def fs_write(request):
    _t = await _fs_locus(request)
    if _t is not None:
        return await _fs_via(_t, "write", request)
    name = await _require_station(request)
    body = await request.json()
    path = _safe_path(body.get("path", ""))
    content = body.get("content", "")
    rc, out = await _push_bytes(name, path, content.encode("utf-8"))
    if rc != 0:
        return web.json_response({"error": out.strip() or "write failed"}, status=400)
    return web.json_response({"ok": True, "path": path})


async def fs_mkdir(request):
    _t = await _fs_locus(request)
    if _t is not None:
        return await _fs_via(_t, "mkdir", request)
    name = await _require_station(request)
    path = _safe_path((await request.json()).get("path", ""))
    uid, gid = _fs_owner(name)
    rc, out, err = await _run("lxc", "exec", name, "--user", uid, "--group", gid,
                              "--", "mkdir", "-p", path)
    if rc != 0:
        return web.json_response({"error": err.strip() or "mkdir failed"}, status=400)
    return web.json_response({"ok": True, "path": path})


async def fs_rename(request):
    _t = await _fs_locus(request)
    if _t is not None:
        return await _fs_via(_t, "rename", request)
    name = await _require_station(request)
    body = await request.json()
    src, dst = _safe_path(body.get("src", "")), _safe_path(body.get("dst", ""))
    rc, out, err = await _lxc(name, "mv", "-n", src, dst)
    if rc != 0:
        return web.json_response({"error": err.strip() or "rename failed"}, status=400)
    return web.json_response({"ok": True, "src": src, "dst": dst})


async def fs_delete(request):
    _t = await _fs_locus(request)
    if _t is not None:
        return await _fs_via(_t, "delete", request)
    name = await _require_station(request)
    path = _safe_path((await request.json()).get("path", ""))
    if path in ("/", SH_HOME):
        return web.json_response({"error": "refusing to delete that path"}, status=400)
    rc, out, err = await _lxc(name, "rm", "-rf", path)
    if rc != 0:
        return web.json_response({"error": err.strip() or "delete failed"}, status=400)
    return web.json_response({"ok": True, "path": path})


async def fs_download(request):
    _t = await _fs_locus(request)
    if _t is not None:
        return await _fs_via(_t, "download", request)
    name = await _require_station(request)
    path = _safe_path(request.query.get("path", ""))
    proc = await asyncio.create_subprocess_exec(
        "lxc", "file", "pull", _cpath(name, path), "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    fname = os.path.basename(path) or "download"
    resp = web.StreamResponse()
    resp.headers["Content-Type"] = "application/octet-stream"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    await resp.prepare(request)
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            await resp.write(chunk)
    finally:
        await proc.wait()
    await resp.write_eof()
    return resp


async def fs_upload(request):
    _t = await _fs_locus(request)
    if _t is not None:
        return await _fs_via(_t, "upload", request)
    name = await _require_station(request)
    dest_dir = _safe_path(request.query.get("path", ""))
    saved = []
    reader = await request.multipart()
    async for part in reader:
        if part.name != "file":
            continue
        # filename may carry a relative path (folder/drag uploads); preserve the
        # tree but sanitize it. --create-dirs in _push_bytes builds subdirs.
        rel = _safe_relname(part.filename) or "upload"
        data = await part.read(decode=False)
        rc, out = await _push_bytes(name, dest_dir.rstrip("/") + "/" + rel, data)
        if rc != 0:
            return web.json_response({"error": out.strip() or "upload failed"}, status=400)
        saved.append(rel)
    return web.json_response({"ok": True, "saved": saved})


# --------------------------------------------------------------------------- #
# WebSocket terminal: persistent PTY sessions <-> `lxc exec <name> bash`
#
# A session (PTY + shell) is keyed by a random sid and OUTLIVES the websocket:
# refresh/navigate away and the shell keeps running; reconnect with ?sid= and
# you're reattached with the recent scrollback replayed. The shell dies only
# when it exits, the client explicitly kills it (closing a pane), or it has
# been detached longer than STATION_CONSOLE_DETACHED_TTL (0 = keep forever).
#
# WS protocol: BINARY = terminal bytes both ways; TEXT = JSON control
# ({"t":"size"}, {"t":"kill"} from client; {"t":"sid"}, {"t":"exit"},
#  {"t":"error"} from server).
# --------------------------------------------------------------------------- #
SCROLLBACK_LIMIT = int(os.environ.get("STATION_CONSOLE_SCROLLBACK", str(256 * 1024)))
DETACHED_TTL = int(os.environ.get("STATION_CONSOLE_DETACHED_TTL", "0"))

SESSIONS = {}           # sid -> TermSession


def _set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


class TermSession:
    def __init__(self, sid, station, cmd=None, env=None, keeper=False, backend="",
                 slot="main", init_rows=None, init_cols=None):
        self.sid = sid
        self.station = station
        self.cmd = cmd              # argv override; default = in-station login shell
        self.env = env              # env override (host-spawned keepers)
        self.keeper = keeper        # keeper sessions persist (reaper-exempt)
        self.backend = backend
        self.slot = slot            # project slot for keepers (map key)
        # initial PTY size: a full-screen TUI (claude) paints its first frame at
        # this size, so launch it at the client's real geometry, not a stale 24x80.
        self.init_rows = init_rows or 24
        self.init_cols = init_cols or 80
        self.master = None
        self.proc = None
        self.ws = None              # attached websocket (one viewer at a time)
        self.buf = bytearray()      # scrollback replayed on (re)attach
        self.outq = None            # FIFO of pending outbound chunks (per attach)
        self.writer = None          # single task draining outq -> ws, in order
        self.detached_at = None
        self.alive = False

    async def start(self):
        master, slave = pty.openpty()
        _set_winsize(master, self.init_rows, self.init_cols)
        tuid, tgid = _exec_ident(self.station)
        argv = self.cmd or [
            "lxc", "exec", self.station,
            "--user", tuid, "--group", tgid, "--cwd", SH_HOME,
            "--env", "TERM=xterm-256color", "--env", f"HOME={SH_HOME}",
            "--", "bash", "-l",
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *argv, env=self.env,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        os.close(slave)
        self.master = master
        self.alive = True
        asyncio.get_event_loop().add_reader(master, self._on_readable)

    def _on_readable(self):
        try:
            data = os.read(self.master, 65536)
        except OSError:
            data = b""
        if not data:                # shell exited
            self.close()
            return
        self.buf.extend(data)
        if len(self.buf) > SCROLLBACK_LIMIT:
            del self.buf[:len(self.buf) - SCROLLBACK_LIMIT]
        # Hand the chunk to the single writer task. Queuing (rather than spawning
        # one send task per read) is what keeps PTY byte order intact: aiohttp's
        # send_bytes awaits the transport drain, so N concurrent send tasks would
        # resume in arbitrary order and shred a fast TUI's output (Claude Code).
        if self.outq is not None:
            self.outq.put_nowait(bytes(data))

    def write(self, data):
        if self.alive:
            try:
                os.write(self.master, data)
            except OSError:
                pass

    def attach(self, ws):
        """Bind a (re)connected websocket: replay scrollback then stream live
        output through one ordered writer task. Call with the loop running."""
        self._stop_writer()
        self.ws = ws
        self.detached_at = None
        self.outq = asyncio.Queue()
        if self.buf:                            # scrollback first, in order
            self.outq.put_nowait(bytes(self.buf))
        self.writer = asyncio.get_event_loop().create_task(self._drain(self.outq, ws))

    async def _drain(self, outq, ws):
        """Drain one attach's queue to its ws, strictly FIFO — the single point
        that touches send_bytes, so chunks can never overtake one another."""
        try:
            while True:
                data = await outq.get()
                if data is None or ws.closed:
                    break
                await ws.send_bytes(data)
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            pass

    def _stop_writer(self):
        if self.writer is not None and not self.writer.done():
            self.writer.cancel()
        self.writer = None
        self.outq = None

    def detach(self):
        self.ws = None
        self.detached_at = time.time()
        self._stop_writer()

    def close(self):
        if not self.alive:
            return
        self.alive = False
        self._stop_writer()
        loop = asyncio.get_event_loop()
        try:
            loop.remove_reader(self.master)
        except (ValueError, OSError):
            pass
        try:
            self.proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        # The pty child runs in its own session with NO controlling tty (the
        # slave was inherited, never opened), so closing the master below never
        # HUPs it — and an interactive `bash -i` fallback ignores SIGTERM. Hang
        # up the whole process group explicitly (board t9).
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGHUP)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.close(self.master)
        except OSError:
            pass
        SESSIONS.pop(self.sid, None)
        ws, self.ws = self.ws, None
        if ws is not None and not ws.closed:
            loop.create_task(_notify_exit(ws))


async def _notify_exit(ws):
    try:
        await ws.send_str(json.dumps({"t": "exit"}))
        await ws.close()
    except Exception:
        pass


async def _reaper(app):
    try:
        while True:
            await asyncio.sleep(300)
            if not DETACHED_TTL:
                continue
            now = time.time()
            for s in list(SESSIONS.values()):
                if s.keeper:                    # keepers are stationary — never reaped
                    continue
                if s.ws is None and s.detached_at and \
                        now - s.detached_at > DETACHED_TTL:
                    s.close()
    except asyncio.CancelledError:
        pass


# --------------------------------------------------------------------------- #
# static + app wiring
# --------------------------------------------------------------------------- #
async def index(request):
    # stamp asset URLs with the newest static mtime so browsers never pair a
    # fresh app.js with a stale style.css (or vice versa) after an update
    html = (STATIC / "index.html").read_text()
    # stamp with the newest mtime among whatever static assets actually exist
    # (the buildless UI has no app.js/style.css — only stat files that are present)
    _cands = [(STATIC / f) for f in
              ("app.js", "style.css", "index.html", "keeper.js", "wireframe.js", "flow.js")]
    _mtimes = [p.stat().st_mtime for p in _cands if p.exists()]
    ver = int(max(_mtimes)) if _mtimes else 0
    return web.Response(text=html.replace("{v}", str(ver)),
                        content_type="text/html",
                        headers={"Cache-Control": "no-cache"})


async def _start_reaper(app):
    app["reaper"] = asyncio.create_task(_reaper(app))


async def _stop_reaper(app):
    app["reaper"].cancel()
    for s in list(SESSIONS.values()):
        s.close()


# ---------------------------------------------------------------------------
# console-api sidecar + reverse proxy.
#
# The console UI talks to the fleet management API (/api/vms, /api/vm/<vm>/*,
# /api/health, /api/models, /api/console/*, /api/discord/*). That surface is the
# bundled `console-api` (stdlib) — the SAME backend the web console uses. We run
# it as a private loopback sidecar and forward those namespaces to it, so the
# desktop app has the full console functionality while this aiohttp server keeps
# owning static, the terminal, login, and the file browser. console-api inherits
# this process's environment, so the box's HUGPY_ENV_FILE / ROOT_DIR / lxd
# access flow through unchanged.
# ---------------------------------------------------------------------------
CONSOLE_API_PORT = int(os.environ.get("CONSOLE_API_PORT", "8766"))
CONSOLE_API_BASE = f"http://127.0.0.1:{CONSOLE_API_PORT}"

# bugreport-api sidecar: the per-VM namespaces the web console routes to
# bugreport-api.service (finder=search, bugreport=bugs, steward, flow,
# todo-history, todo-revision, view-push, keeper-ask, keeper-env). Same model
# as console-api: a private loopback sidecar, stdlib + lxc, inheriting env.
BUGREPORT_API_PORT = int(os.environ.get("BUGREPORT_API_PORT", "8767"))
BUGREPORT_API_BASE = f"http://127.0.0.1:{BUGREPORT_API_PORT}"
# per-VM suffixes bugreport-api owns; everything else under /api/vm/* is console-api
BUGREPORT_SUFFIXES = ("finder", "bugreport", "steward", "flow", "todo-history",
                      "todo-revision", "view-push", "keeper-ask", "keeper-env",
                      "messages")   # HOST-LOCAL PATCH: ✉ bridge-mail route


def _dies_with_parent():
    """preexec_fn: the sidecar gets SIGTERM the instant its parent dies. An
    orphaned sidecar is worse than a dead one — it squats the loopback port
    with STALE code, every later launch's fresh sidecar loses the bind race
    and exits, and the proxy silently serves the relic (observed 2026-08-12:
    an Aug-8 console-api still announcing the pre-rename station list four
    days and three upgrades later)."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)
    except Exception:      # noqa: BLE001 — best-effort; the reap below backstops
        pass


def _reap_stale_sidecar(script_path):
    """SIGTERM any leftover process still running this exact sidecar script —
    the pre-PDEATHSIG escape hatch for orphans older than this build."""
    me = os.getpid()
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or int(p.name) == me:
            continue
        try:
            argv = (p / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(a.decode(errors="replace") == str(script_path) for a in argv[1:3]):
            try:
                os.kill(int(p.name), signal.SIGTERM)
            except OSError:
                pass


async def _start_console_api(app):
    app["proxy_sess"] = aiohttp.ClientSession()
    api_script = ROOT / "console-api"
    if not api_script.exists():
        app["console_api_proc"] = None
        return
    _reap_stale_sidecar(api_script)
    env = {**os.environ, "CONSOLE_API_PORT": str(CONSOLE_API_PORT)}
    app["console_api_proc"] = await asyncio.create_subprocess_exec(
        sys.executable, str(api_script), env=env, preexec_fn=_dies_with_parent,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    # give it a moment to bind before the first proxied request arrives
    for _ in range(40):
        try:
            async with app["proxy_sess"].get(
                    CONSOLE_API_BASE + "/api/health",
                    timeout=aiohttp.ClientTimeout(total=1)) as r:
                await r.read()
                break
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(0.25)


async def _stop_console_api(app):
    sess = app.get("proxy_sess")
    if sess is not None:
        await sess.close()
    proc = app.get("console_api_proc")
    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


_PROXY_HOP = {"host", "connection", "keep-alive", "transfer-encoding",
              "content-length", "te", "trailer", "upgrade"}


# /api/vm/<locus>/<tail> tails the central DB serves for EVERY locus kind
# (1.0.138). An LXD guest's OTHER tails (power, keeper launch/gate, compose,
# steward, bugreport, …) still go to the lxc sidecars: they act on the machine.
_CENTRAL_VM_TAILS = ("todo", "design", "flow", "messages", "todo/brief",
                     "todo/assist", "todo-history")


async def api_station_identity(request):
    """GET /api/station/identity — who THIS station is (1.0.141): its own locus
    (STATION_LOCUS / the registry; '' = not configured), every spelling that
    means "this host" on the wire, and the reserved host token the UI uses."""
    await _loci_ready(3.0)
    own = _station_locus()
    return web.json_response({
        "ok": True, "locus": own, "configured": bool(own),
        "self_names": sorted(_self_locus_names()),
        "host_token": HOST_TOKEN, "host_tokens": [t for t in _HOST_VMS if t],
        "legacy_aliases": dict(_LEGACY_LOCUS_ALIASES),
        "source": ("STATION_LOCUS" if (os.environ.get("STATION_LOCUS") or "").strip()
                   else "registry" if own else ""),
        "error": "" if own else LOCUS_UNCONFIGURED})


def _write_state_pointer():
    """1.0.141: tell any peer that reaches this user over ssh where this
    station's state dir is (locus_exec.STATE_SH reads it), and pin it into
    this process's env so the self target resolves the same dir."""
    os.environ.setdefault("HUGPY_STATION_STATE", str(FV_STATE_HOME))
    default = Path.home() / ".config" / "hugpy-station"
    try:
        ptr = default / "state-dir"
        if FV_STATE_HOME.resolve() == default.resolve():
            if ptr.exists():
                ptr.unlink()
            return
        default.mkdir(parents=True, exist_ok=True)
        if not ptr.exists() or ptr.read_text().strip() != str(FV_STATE_HOME):
            ptr.write_text(str(FV_STATE_HOME) + "\n")
    except OSError:
        pass


async def _host_vm_dispatch(request, tail):
    """/api/vm/<this station's own locus>/<tail> -> the host handler for tail
    (the same handlers /api/vm/@self/<tail> is routed to)."""
    meth = request.method
    if tail == "todo":
        return await (mct_todo_post(request) if meth == "POST" else mct_todo_get(request))
    if tail in ("design", "flow"):
        return await api_host_canvas(request, tail)
    table = {"todo-history": keeper_todo_history, "todo/assist": keeper_todo_assist,
             "todo-revision": keeper_todo_revision, "todo/discord": keeper_todo_discord,
             "messages": keeper_messages_get}
    h = table.get(tail)
    if h is None:
        return web.json_response({"ok": False, "error": f"no host route for {tail}"}, status=404)
    return await h(request)


async def _central_vm_reroute(request):
    """/api/vm/<locus>/<tail> for a NON-host locus: the board (todo), canvas
    (design/flow), ✉ messages (comms inbox), ⧉ brief (comms ping) and ✨ assist
    are answered from the locus's CENTRAL rows (toolserver DB) — never from a
    workbench or an lxc-exec'd file on the locus (operator 2026-09-30: one
    station, every other locus is a DB key). ssh-host loci never fall through to
    the lxc-only sidecars. None = not ours, the caller proceeds to its sidecar."""
    m = re.match(r"^/api/vm/([^/]+)/(.+)$", request.path)
    if not m:
        return None
    if _is_host_vm(m.group(1)):
        # 1.0.141: this station's own locus NAME (e.g. `keeper` on vm_mgr's
        # station) is answered by the host routes in-process; the reserved
        # tokens have their own literal routes.
        if m.group(1) not in _HOST_VMS:
            return await _host_vm_dispatch(request, m.group(2))
        return None
    name, tail = m.group(1), m.group(2)
    ssh = bool(_ssh_host(name))
    if not ssh and tail not in _CENTRAL_VM_TAILS:
        return None
    loc = await _known_locus_key(name)
    if not loc:
        if not ssh:
            return web.json_response({"ok": False, "error": f"unknown locus {name}"}, status=404)
        return _retired(tail, name)
    if loc and loc == _keeper_locus() and tail in _CENTRAL_VM_TAILS:
        # a dropdown pointer at THIS station → the host routes (file failsafe),
        # answered in-process (no redirect: a POST must not need a 307 follow)
        return await _host_vm_dispatch(request, tail)
    if tail == "todo":
        return await _central_todo_route(request, loc)
    if tail in ("design", "flow"):
        return await _central_canvas_route(request, loc, tail)
    if tail == "messages":
        return await _central_messages(request, loc)
    if tail == "todo/brief":
        return await _central_todo_brief(request, loc)
    if tail == "todo/assist":
        return await _todo_assist(request, loc)
    if tail == "todo-history":
        return _retired("todo-history (the central todos table keeps no event journal yet)", name)
    return _retired(tail, name)


async def api_proxy(request):
    """Forward a request to the console-api sidecar, verbatim (a locus's
    central tails — board, canvas, messages — reroute to the DB first)."""
    resp = await _central_vm_reroute(request)
    if resp is not None:
        return resp
    sess = request.app.get("proxy_sess")
    if sess is None:
        return web.json_response({"error": "console-api not started"}, status=502)
    target = CONSOLE_API_BASE + request.path_qs
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in _PROXY_HOP}
    body = await request.read()
    try:
        async with sess.request(request.method, target, headers=fwd,
                                data=body, allow_redirects=False) as r:
            payload = await r.read()
            out = {k: v for k, v in r.headers.items()
                   if k.lower() not in _PROXY_HOP | {"content-encoding"}}
            return web.Response(status=r.status, body=payload, headers=out)
    except aiohttp.ClientError as e:
        return web.json_response(
            {"error": f"console-api unreachable: {e}"}, status=502)


# ── /term: same-origin proxy to the local ttyd (web-console.service) ─────────
# The SPA's VM panes iframe /term/?arg=<vm>&arg=<mode>; ttyd runs term-wrapper,
# which `lxc exec`s into the NAMED vm — that is what makes a pane's shell land
# inside the switched-to VM instead of on this host. In the console-vm
# packaging nginx provided this same-origin route; on a bare-host station
# console there is no fronting nginx, so the backend carries the proxy itself.
# Auth: this route sits behind the console's own login like every page, and the
# proxy injects ttyd's Basic credential server-side (the operator's webconsole
# password never reaches the browser).
TTYD_UPSTREAM = os.environ.get("STATION_CONSOLE_TTYD", "http://127.0.0.1:7681")


def _ttyd_auth_header():
    cand = [Path(os.environ["STATION_CONSOLE_TTYD_PASS_FILE"])] \
        if os.environ.get("STATION_CONSOLE_TTYD_PASS_FILE") else []
    for p in cand + [Path("/srv/vm_mgr/secrets/webconsole.pass"),
                     Path.home() / ".fleet/webconsole.pass"]:
        try:
            pw = p.read_text().strip()
        except OSError:
            continue
        if pw:
            return "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()
    return None


# Injected into every proxied ttyd page: copy must WORK in the /term panes
# too, not just the SPA's own xterm views. On plain-http origins (the in-VM
# workbenches) navigator.clipboard does not exist, so ttyd/xterm's own copy
# path dies silently — polyfill it with the execCommand fallback, add
# copy-on-select and Ctrl/Cmd+Shift+C off ttyd's exposed window.term.
_TERM_COPY_SHIM = """<script>(function(){
  var last='';var te=null,tt=null,deb=null;
  function toast(m,bad){if(!te){te=document.createElement('div');
    te.style.cssText='position:fixed;right:10px;bottom:10px;z-index:9999;'+
    'padding:4px 10px;border-radius:6px;pointer-events:none;'+
    'font:12px ui-monospace,monospace;transition:opacity .4s;opacity:0;'+
    'border:1px solid #2b567a;background:#16324a;color:#9fd4ff;';
    document.body.appendChild(te);}
    te.textContent=m;te.style.background=bad?'#4a1616':'#16324a';
    te.style.color=bad?'#ff9f9f':'#9fd4ff';te.style.opacity='1';
    clearTimeout(tt);tt=setTimeout(function(){te.style.opacity='0'},1400);}
  function fb(t){var ta=document.createElement('textarea');ta.value=t;
    ta.setAttribute('readonly','');ta.style.cssText=
    'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;';
    var prev=document.activeElement;document.body.appendChild(ta);
    ta.focus();ta.select();var ok=false;
    try{ok=document.execCommand('copy')}catch(e){}
    document.body.removeChild(ta);
    try{prev&&prev.focus&&prev.focus()}catch(e){}return ok;}
  function cp(t,ann){if(!t){if(ann)toast('nothing selected to copy',true);return;}
    last=t;
    var done=function(ok){if(ann)toast(ok?'copied '+t.length+' chars \\u2713'
                                         :'copy FAILED \\u2014 select again',!ok);};
    if(navigator.clipboard&&navigator.clipboard.writeText)
      navigator.clipboard.writeText(t).then(function(){done(true)},
        function(){done(fb(t))});
    else done(fb(t));}
  if(!window.isSecureContext||!navigator.clipboard){
    try{Object.defineProperty(navigator,'clipboard',{configurable:true,
      value:{writeText:function(t){return fb(t)?Promise.resolve()
                                              :Promise.reject(new Error('copy'))}}})}catch(e){}}
  function sel(){var T=window.term;
    if(T&&T.getSelection)try{return T.getSelection()}catch(e){}
    return String(document.getSelection()||'');}
  document.addEventListener('keydown',function(e){
    if((e.ctrlKey||e.metaKey)&&e.shiftKey&&(e.key==='C'||e.key==='c')){
      var t=sel()||last;cp(t,true);
      if(t){e.preventDefault();e.stopPropagation();}}},true);
  var n=0,iv=setInterval(function(){var T=window.term;
    if(!T||!T.onSelectionChange){if(++n>40)clearInterval(iv);return;}
    clearInterval(iv);
    T.onSelectionChange(function(){var t=T.getSelection();
      if(!t)return;last=t;
      clearTimeout(deb);deb=setTimeout(function(){cp(t,true)},250);});},500);
})()</script>"""


def _inject_copy_shim(body, content_type):
    if "text/html" not in (content_type or ""):
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    for anchor in ("</body>", "</html>"):
        if anchor in text:
            return text.replace(anchor, _TERM_COPY_SHIM + anchor, 1).encode("utf-8")
    return (text + _TERM_COPY_SHIM).encode("utf-8")


async def term_proxy(request):
    tail = request.match_info.get("tail", "")
    qs = ("?" + request.query_string) if request.query_string else ""
    target = f"{TTYD_UPSTREAM}/term/{tail}{qs}"
    auth = _ttyd_auth_header()
    hdrs = {"Authorization": auth} if auth else {}

    if request.headers.get("Upgrade", "").lower() == "websocket":
        # ttyd speaks the "tty" subprotocol; advertise it both ways.
        ws_client = web.WebSocketResponse(protocols=("tty",), heartbeat=30)
        await ws_client.prepare(request)
        try:
            async with aiohttp.ClientSession() as sess, \
                       sess.ws_connect(target, protocols=("tty",),
                                       headers=hdrs, max_msg_size=0) as ws_up:
                async def pump(src, dst):
                    async for m in src:
                        if m.type == WSMsgType.BINARY:
                            await dst.send_bytes(m.data)
                        elif m.type == WSMsgType.TEXT:
                            await dst.send_str(m.data)
                        else:
                            break
                done, pending = await asyncio.wait(
                    [asyncio.create_task(pump(ws_client, ws_up)),
                     asyncio.create_task(pump(ws_up, ws_client))],
                    return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except (aiohttp.ClientError, OSError):
            with contextlib.suppress(Exception):
                await ws_client.send_str(json.dumps(
                    {"t": "error", "msg": "ttyd unreachable — is web-console.service running?"}))
        finally:
            await ws_client.close()
        return ws_client

    sess = request.app.get("proxy_sess")
    if sess is None:
        return web.json_response({"error": "proxy session not started"}, status=502)
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in _PROXY_HOP}
    fwd.update(hdrs)
    try:
        async with sess.request(request.method, target, headers=fwd,
                                data=await request.read(),
                                allow_redirects=False) as r:
            out = {k: v for k, v in r.headers.items()
                   if k.lower() not in _PROXY_HOP
                   | {"content-encoding", "content-length"}}
            body = _inject_copy_shim(await r.read(),
                                     r.headers.get("Content-Type", ""))
            return web.Response(status=r.status, body=body, headers=out)
    except aiohttp.ClientError:
        return web.Response(
            status=502, content_type="text/html",
            text="<pre>terminal backend (ttyd) unreachable — "
                 "start it with: sudo systemctl start web-console.service</pre>")


# ── /ac: same-origin proxy to the abstract-claude console (:9111) ────────────
# Surfaces `abstract-claude serve` (THE keeper surface) as a station pane.
# d411 (2026-09-17): a DUMB pass-through — it strips the /ac prefix on the way
# upstream and rewrites NOTHING. serve owns the prefix natively (AC_UI_BASE=/ac
# and a dist baked for /ac), so its own calls resolve under /ac while a call the
# console aims at the ORIGIN ROOT (keeper-live.js stationApi -> /api/b/chat)
# still reaches THIS app. Sits behind the console login like every route;
# AC itself is loopback-only with no auth of its own.
AC_UPSTREAM = os.environ.get("STATION_CONSOLE_AC", "http://127.0.0.1:9111")
# /ac requests are proxied WITHOUT a total deadline: a chat turn streams for as
# long as claude works (minutes to an hour). Connect is still bounded.
_AC_PROXY_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)

# ── t-serve (2026-09-16, vm_mgr): abstract-claude serve IS the keeper surface ──
# Determination: on this locus the primary keeper surface is `abstract-claude
# serve` — USER unit abstract-claude-serve@station (127.0.0.1:9124, AC_ROOT =
# <state>/abstract-claude, the SAME root the station's own seats use), rendered
# at /ac/ through ac_proxy above. The tmux keeper seats (keeper-claude /
# keeper-codex) are NOT removed and NOT hidden: they stay selectable as explicit
# "terminal seat (tmux)" choices and every tmux code path below is untouched.
# STATION_KEEPER_SURFACE=tmux restores the old default. NOTHING added here ever
# kills a tmux session — the serve-mode relaunch/wipe restart a USER unit, and
# seats live in their own scopes.
KEEPER_SURFACE = (os.environ.get("STATION_KEEPER_SURFACE") or "serve").strip().lower()
AC_SERVE_UNIT = (os.environ.get("STATION_AC_SERVE_UNIT") or "abstract-claude-serve@station.service").strip()
# NAMING (2026-09-18): operator-facing surface names are "Serve" (the
# abstract-claude serve console) and "Terminal" (the interactive-TUI seat -
# a full-screen CLI painted through xterm.js over a WebSocket-fed PTY). tmux is
# only the persistence/multiplexing substrate under the Terminal seat, NOT the
# category and NOT the freeze cause (that is xterm.js RenderService). The wire
# tokens stay "tmux" (KEEPER_SURFACE value, seat:"tmux" API, STATION_KEEPER_SURFACE,
# fv-keeper-surface) for contract compatibility.
AC_SERVE_LABEL = "Serve console"
TMUX_SEAT_LABELS = {"mct": "Terminal \u00b7 MCT pointer exchange",
                    "claude-code": "Terminal \u00b7 Claude Code",
                    "codex": "Terminal \u00b7 ChatGPT Codex",
                    "hugpy": "Terminal \u00b7 Hugpy agent"}

# ── per-locus serve consoles (1.0.101, 2026-09-17, vm_mgr; operator: "the hugpy
# locus must have abstract-claude serve integrated") ──────────────────────────
# A locus other than this host can own an abstract-claude serve of its own
# (hugpy: the hugpy user's unit abstract-claude-serve-station on 127.0.0.1:9125,
# the SAME 0.1.44 build + UI as the keeper's). The console frames it same-origin
# at /ac/@<locus>/ through ac_proxy (the fetch shim and the dist's absolute
# "/ac/..." references are rewritten to that base on the way through), and
# /api/term/backends?vm=<locus> reports THAT serve as the locus's keeper
# surface, so FleetView's frontier view shows the locus's own console instead
# of a tmux seat. Config: STATION_CONSOLE_AC_LOCI ("hugpy=http://127.0.0.1:9125
# [|unit][,name=url...]") overridden by <state>/ac-loci.json
# ({"hugpy": {"url": ..., "unit": ..., "label": ...}}; re-read on change, no
# restart). The host seat ("" / @keeper) keeps AC_UPSTREAM; a locus without an
# entry keeps its tmux seat exactly as before.
AC_LOCI_PATH = FV_STATE_HOME / "ac-loci.json"
_AC_LOCI_CACHE = {"mtime": None, "doc": {}}


def _ac_loci_env():
    out = {}
    for part in (os.environ.get("STATION_CONSOLE_AC_LOCI") or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, rest = part.partition("=")
        url, _, unit = rest.partition("|")
        if name.strip() and url.strip():
            out[name.strip()] = {"url": url.strip(), "unit": unit.strip()}
    return out


def _ac_loci():
    """{locus: {url, unit, label}} — env entries, overridden by ac-loci.json."""
    doc = dict(_ac_loci_env())
    try:
        st = AC_LOCI_PATH.stat()
        if _AC_LOCI_CACHE["mtime"] != st.st_mtime:
            _AC_LOCI_CACHE["doc"] = _read_json(AC_LOCI_PATH, {}) or {}
            _AC_LOCI_CACHE["mtime"] = st.st_mtime
        for k, v in (_AC_LOCI_CACHE["doc"] or {}).items():
            if isinstance(v, str):
                v = {"url": v}
            if isinstance(v, dict) and v.get("url"):
                doc[str(k)] = dict(v)
    except OSError:
        _AC_LOCI_CACHE.update(mtime=None, doc={})
    return doc


def _ac_locus_key(vm):
    vm = (vm or "").strip()
    return "" if _is_host_vm(vm) else vm


_AC_FORWARDS = LX.ServeForwards()      # 1.0.141: locus -> ssh -L forward to its serve
_AC_BRIDGES = LX.BridgeForwards()      # 1.0.144: lxd guest -> stdio bridge to its serve
_AC_DISCOVERED = {}                    # locus -> {url, unit, port, source, error, ts}
_AC_TS_APP = {}                        # a private toolserver client session for discovery
_AC_DISCOVER_TTL = 30


def _ac_target(vm=""):
    """(upstream url, unit, console base path, locus key) for a locus's serve.
    url '' = that locus has no reachable serve. Sync view: the host, an
    ac-loci.json override, else the last _ac_resolve() discovery (1.0.141)."""
    key = _ac_locus_key(vm)
    if not key:
        return AC_UPSTREAM, AC_SERVE_UNIT, "/ac/", ""
    ent = _ac_loci().get(key)
    if ent:
        return ((ent.get("url") or "").rstrip("/"),
                ent.get("unit") or "abstract-claude-serve-station.service",
                f"/ac/@{key}/", key)
    d = _AC_DISCOVERED.get(key) or {}
    return d.get("url") or "", d.get("unit") or "abstract-claude-serve@station.service", f"/ac/@{key}/", key


async def _ac_discover_port(t):
    """The target's own serve port: 1) its central seat_state row (seat
    'keeper', status.port — published by abstract-claude-serve-run's
    seat-report), 2) the same probe script run ON the target (its
    <state>/ac-serve-*.json, else its own listening abstract-claude serve).
    -> (port|None, source, error)."""
    if t.locus:
        try:
            rows = await _ts_call(_AC_TS_APP, "seat/state", {"locus": t.locus, "seat": "keeper"}, timeout=6)
            for r in (rows or []):
                st = (r or {}).get("status") or {}
                if (isinstance(st, dict) and st.get("surface") == "serve" and st.get("port")
                        and r.get("alive") and not r.get("stale")):
                    return int(st["port"]), "seat_state", ""
        except Exception:                                # noqa: BLE001
            pass
    d = await LX.probe_serve(t)
    if d.get("ok") and d.get("port"):
        return int(d["port"]), "probe:" + str(d.get("source") or ""), ""
    return None, "", str(d.get("error") or "no serve found")


async def _ac_resolve(vm):
    """1.0.141: a remote locus's serve WITHOUT a hand-made ac-loci.json. The
    url is always 127.0.0.1:<port> as seen ON the target; an ssh locus is
    reached through `ssh -O forward -L` on the existing mux, so the station
    talks to http://127.0.0.1:<local forward>. -> _ac_target() tuple."""
    key = _ac_locus_key(vm)
    if not key or _ac_loci().get(key):
        return _ac_target(vm)
    t = _locus_target(key)
    if not t.remote:
        return AC_UPSTREAM, AC_SERVE_UNIT, "/ac/", ""
    d = _AC_DISCOVERED.get(key) or {}
    fresh = d.get("ts") and time.time() - d["ts"] < _AC_DISCOVER_TTL
    if fresh and d.get("url") and LX.port_open(d.get("lport") or 0):
        return _ac_target(vm)
    if fresh and d.get("error") and not d.get("url"):
        return _ac_target(vm)
    port, src, err = await _ac_discover_port(t)
    rec = {"ts": time.time(), "port": port, "source": src, "error": err, "url": "",
           "unit": "abstract-claude-serve@station.service"}
    if port:
        try:
            if t.kind == LX.KIND_SSH:
                lport = await _AC_FORWARDS.ensure(t, port)
                via = f"ssh -L 127.0.0.1:{lport} -> {key}:127.0.0.1:{port}"
            else:
                # 1.0.144: an lxd guest's serve listens on the GUEST's loopback —
                # reached through a local listener bridged by `lxc exec` per connection
                lport = await _AC_BRIDGES.ensure(t, port)
                via = f"lxc bridge 127.0.0.1:{lport} -> {key}:127.0.0.1:{port}"
            rec.update(url=f"http://127.0.0.1:{lport}", lport=lport, via=via)
        except (LX.LocusError, OSError) as e:
            rec["error"] = str(e)
    _AC_DISCOVERED[key] = rec
    return _ac_target(vm)


async def _ac_get(path, timeout=8, base=None):
    """GET a JSON doc from a serve upstream (default: the keeper's); None on any failure."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get((base or AC_UPSTREAM).rstrip("/") + path) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception:                                    # noqa: BLE001
        return None


async def _ac_post(path, payload=None, timeout=8, base=None):
    """POST a JSON body to a serve upstream (default: the keeper's); the decoded
    JSON reply, or None on any failure. The JSON-body twin of _ac_get (p509)."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.post((base or AC_UPSTREAM).rstrip("/") + path, json=(payload or {})) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception:                                    # noqa: BLE001
        return None


async def _ac_serve_state(vm=""):
    """Does the locus's serve answer /api/state? Returns the surface descriptor
    the console uses to decide whether serve leads or the tmux seat does.
    vm '' / @keeper = this host's keeper serve; another locus = its own serve
    from _ac_target (1.0.101), framed at path (/ac/@<locus>/)."""
    url, unit, path, key = await _ac_resolve(vm)
    ent = _ac_loci().get(key) if key else None
    disc = _AC_DISCOVERED.get(key) or {} if key else {}
    base = {"url": url, "path": path, "unit": unit, "locus": key or "@keeper",
            "label": (ent or {}).get("label") or AC_SERVE_LABEL,
            "source": ("ac-loci" if ent else disc.get("source") or ("host" if not key else "")),
            "via": disc.get("via") or ""}
    if key and not url:
        return dict(base, ok=False, error="no reachable abstract-claude serve on locus "
                    + key + ": " + (disc.get("error") or "not discovered")
                    + " (override: ac-loci.json / STATION_CONSOLE_AC_LOCI)")
    doc = await _ac_get("/api/state", timeout=2.5, base=url)
    if not isinstance(doc, dict):
        return dict(base, ok=False, error="no /api/state from " + url)
    cfg = doc.get("config") or {}
    return dict(base, ok=True, root=doc.get("root") or cfg.get("root") or "",
                model=cfg.get("default_model") or "",
                fallback_model=cfg.get("quota_fallback_model") or "",
                fresh_session_mode=cfg.get("fresh_session_mode") or "")


async def _ac_serve_restart(reason="", vm=""):
    """The serve-mode 'relaunch': restart the abstract-claude-serve USER unit.
    Deliberately NOT a tmux kill — no seat, and certainly not keeper-claude, is
    touched. Waits for /api/state to answer again before reporting ok.
    1.0.101: vm = another locus restarts THAT locus's unit as its seat user
    (ssh / lxd grounding via _locus_run)."""
    url, unit, _path, key = await _ac_resolve(vm)
    if key and not url:
        return {"ok": False, "surface": "serve", "unit": "", "locus": key,
                "error": "no abstract-claude serve configured for locus " + key}
    if key:
        rc, out, err = await _locus_run(key, "systemctl --user restart " + shlex.quote(unit), timeout=60)
        if rc != 0:
            return {"ok": False, "surface": "serve", "unit": unit, "locus": key,
                    "error": "systemctl --user restart failed: " + ((out or "") + (err or ""))[:200]}
    else:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return {"ok": False, "surface": "serve", "unit": unit,
                    "error": "systemctl --user restart failed: " + (out or b"").decode()[:200]}
    for _ in range(24):
        await asyncio.sleep(0.5)
        if (await _ac_serve_state(vm)).get("ok"):
            return {"ok": True, "surface": "serve", "unit": unit, "locus": key or "@keeper",
                    "reason": reason, "killed": None,
                    "note": "abstract-claude serve restarted; no tmux session was touched"}
    return {"ok": False, "surface": "serve", "unit": unit, "locus": key or "@keeper",
            "error": "serve did not answer /api/state after restart"}


async def _keeper_surface_now(vm=""):
    """('serve'|'tmux', ac_state). t320 (2026-09-16): serve leads whenever it is
    the CONFIGURED surface — a restart/wipe blip must not flip the console to
    the tmux terminal (which spawned a fresh keeper-claude seat and split the
    operator's prompts). The probe result rides ac["ok"]; the /ac proxy shows a
    self-retrying "restarting" page while serve is down.
    1.0.101: vm = another locus → ITS serve (ac-loci.json) leads when one is
    configured; a locus without one keeps its tmux seat."""
    ac = await _ac_serve_state(vm)
    if _ac_locus_key(vm):
        return ("serve" if (ac.get("url") and KEEPER_SURFACE == "serve") else "tmux"), ac
    return ("serve" if KEEPER_SURFACE == "serve" else "tmux"), ac


# ── 1.0.144: provision + start a locus's OWN serve, from the station ─────────
# "Every locus needs a serve chat and a keeper" (operator 2026-10-01). The serve
# is provisioned ON the locus, AS its user, through the locus transport
# (locus_exec: bash -s / ssh / lxc) by the SHIPPED resources/bin/station-serve-
# provision, shipped on stdin — the same script whatever the locus kind. Every
# artifact it lays down is derived from shipped resources (see its header); the
# locus's sessions then run its own rendered directives (the runner calls
# directives.py --locus <locus> on every start).
SERVE_PROVISION_SH = ROOT.parent / "bin" / "station-serve-provision"
_SERVE_PROVISION_LOCK = asyncio.Lock()


async def _resolve_vm_name(v):
    """A wire locus name -> '' (this host) | the dropdown name | None (unknown).
    The body-safe twin of _sidecar_vm (which reads ?vm= only)."""
    v = (v or "").strip()
    if not v or _is_host_vm(v):
        return ""
    await _loci_ready()
    if _ssh_host(v):
        return v
    try:
        names = await known_names()
    except Exception:                                    # noqa: BLE001
        names = set()
    return v if v in names else None


async def _locus_seats(vm):
    """(ok, [{name, attached, created}], err) — the tmux sessions on the
    locus's OWN console socket, listed AS that locus's user."""
    try:
        return await LX.list_seats(_locus_target(vm))
    except Exception as e:                               # noqa: BLE001
        return False, [], str(e)[:200]


async def api_locus_serve(request):
    """GET /api/locus/serve?vm= — the locus's serve + seats, one call:
    {locus, kind, who, serve: _ac_serve_state, seats: [...], managed: {...}}."""
    vm = await _sidecar_vm(request)
    t = _locus_target(vm)
    ac = await _ac_serve_state(vm)
    ok, seats, err = await _locus_seats(vm)
    return web.json_response({"locus": t.locus or _station_locus(), "vm": vm or "@self", "kind": t.kind,
                              "serve": ac, "seats": seats, "seats_ok": ok, "seats_error": err,
                              "managed": _serve_managed().get(vm) if vm else None,
                              "provision": {"path": "/api/locus/serve/provision", "method": "POST"}})


async def api_locus_serve_provision(request):
    """POST /api/locus/serve/provision[?vm=] {vm?, instance?, port?} — run the
    shipped station-serve-provision ON the locus as its user; on success the
    station delivers that locus's comms pings + digest to its keeper session
    (serve-managed.json). Idempotent: a healthy serve is reported, not restarted."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    want = str(request.query.get("vm") or body.get("vm") or "").strip()
    vm = await _resolve_vm_name(want)
    if vm is None:
        return web.json_response({"ok": False, "error": "unknown locus " + want}, status=404)
    inst = str(body.get("instance") or "station")
    if not re.match(r"^[A-Za-z0-9_-]{1,32}$", inst):
        return web.json_response({"ok": False, "error": "bad instance"}, status=400)
    port = body.get("port")
    try:
        port = int(port) if port else None
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "bad port"}, status=400)
    try:
        text = SERVE_PROVISION_SH.read_text(encoding="utf-8")
    except OSError as e:
        return web.json_response({"ok": False, "error": "station-serve-provision not shipped: %s" % e}, status=500)
    t = _locus_target(vm)
    if _SERVE_PROVISION_LOCK.locked():
        return web.json_response({"ok": False, "error": "a provision is already running"}, status=409)
    async with _SERVE_PROVISION_LOCK:
        res = await LX.serve_provision(t, text, instance=inst, locus=t.locus or _station_locus(), port=port)
    key = _ac_locus_key(vm)
    _AC_DISCOVERED.pop(key, None)                       # rediscover through the transport now
    if res.get("ok") and vm:
        _serve_managed_put(vm, {"locus": t.locus or vm, "kind": t.kind, "instance": inst,
                                "port": res.get("port"), "user": res.get("user"),
                                "app": res.get("app"), "version": res.get("version"),
                                "provisioned": int(time.time()), "by": _station_id()})
    if res.get("ok"):
        # 1.0.146 (t4250): the standing roles' permission mode + cwd into the locus's
        # config.json and the keeper's LOCUS NUANCES block — idempotent (missing keys only)
        try:
            res["standing"] = await _locus_standing_apply(request.app, vm, t, instance=inst, port=res.get("port"))
        except Exception as e:                           # noqa: BLE001 — the serve is up; say what is missing
            res["standing"] = {"ok": False, "error": str(e)[:300]}
    res["serve"] = await _ac_serve_state(vm)
    res.update(vm=vm or "@self", kind=t.kind)
    audit(request, "serve-provision", "%s (%s) → %s %s" % (vm or "@self", t.kind,
          "ok" if res.get("ok") else "FAILED", (res.get("error") or res.get("port") or "")),
          ok=bool(res.get("ok")))
    return web.json_response(res, status=200 if res.get("ok") else 502)


# ── t4262: the locus's KEEPER = its serve roster Keeper role ─────────────────
# The steward › sessions keeper card used to show the tmux seat (keeper-claude:
# three native sessions on three models, "delegate-only", and the cumulative
# relayed-token total drawn as a 1M context gauge). The Keeper is the serve
# roster's keeper role: ONE session, ONE model (+ a staged pending_model), and
# the context gauge that session's own rollover gauge. The tmux seat is the
# emergency surface and rides along, marked as such.
async def _ac_call(path, payload=None, base=None, timeout=8):
    """One serve request with the error KEPT: (ok, status, body) where body is
    the decoded JSON, else the upstream text. Unlike _ac_get/_ac_post nothing is
    swallowed — the card shows the upstream's own words (t4262)."""
    url = (base or AC_UPSTREAM).rstrip("/") + path
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            req = s.post(url, json=(payload or {})) if payload is not None else s.get(url)
            async with req as r:
                text = await r.text()
                try:
                    body = json.loads(text) if text else {}
                except ValueError:
                    body = text
                return r.status == 200, r.status, body
    except Exception as e:                               # noqa: BLE001
        return False, 0, "%s: %s" % (type(e).__name__, e)


def _ac_err_text(status, body):
    """The upstream's own error words for a failed _ac_call."""
    if isinstance(body, dict):
        msg = body.get("error") or body.get("message") or json.dumps(body)[:300]
    else:
        msg = str(body)[:300]
    return ("HTTP %d: " % status if status else "") + msg


async def _locus_keeper_base(vm):
    """(serve url, console path, locus key, error) for the keeper route."""
    url, _unit, path, key = await _ac_resolve(vm)
    if vm and not url:
        return "", path, key, ("no reachable serve on " + vm + ": "
                               + ((_AC_DISCOVERED.get(key) or {}).get("error") or "not discovered"))
    return url or AC_UPSTREAM, path, key, ""


async def _locus_keeper_seat(app, vm):
    """The tmux seat summary (frontier state) — the EMERGENCY surface, best
    effort and bounded so a slow toolserver never holds the serve card."""
    seat = {"emergency": True, "surface": "tmux",
            "session": (_tmux_session_for("frontier", "claude-code") or "keeper-claude") if not vm else "keeper-claude"}
    try:
        st = await asyncio.wait_for(_rolling_state(app, vm), timeout=6)
    except Exception as e:                               # noqa: BLE001
        return dict(seat, ok=False, error=str(e)[:200])
    if not isinstance(st, dict):
        return dict(seat, ok=False, error="no frontier state")
    seat.update(ok=bool(st.get("ok")), error=st.get("error") or "",
                session=st.get("seat_session") or seat["session"],
                busy=bool(st.get("busy")), locus=st.get("locus") or "",
                idle_relaunch=st.get("idle_relaunch"))
    return seat


async def _locus_keeper_doc(app, vm):
    """The /api/locus/keeper payload for vm ('' = this host)."""
    base, path, key, err = await _locus_keeper_base(vm)
    locus = key or _station_locus() or "host"
    out = {"ok": False, "locus": locus, "vm": vm or "@self",
           "serve": {"url": base, "path": path}, "keeper": None, "models": [], "relayed": None}
    if err:
        out["error"] = err
        out["seat"] = await _locus_keeper_seat(app, vm)
        return out
    ok, st, roster = await _ac_call("/api/session/roster", base=base)
    if not ok or not isinstance(roster, dict):
        out["error"] = "serve roster: " + _ac_err_text(st, roster)
        out["seat"] = await _locus_keeper_seat(app, vm)
        return out
    role = next((r for r in (roster.get("roles") or []) if isinstance(r, dict) and r.get("role") == "keeper"), None)
    if not role:
        out["error"] = "serve roster has no keeper role"
        out["seat"] = await _locus_keeper_seat(app, vm)
        return out
    sid = str(role.get("live_session_id") or role.get("session_id") or "")
    keeper = {"session_id": sid, "native_id": "", "backend": role.get("backend") or "",
              "model": role.get("model") or "", "pending_model": role.get("pending_model") or "",
              "default_model": role.get("default_model") or "",
              "label": role.get("label") or "Keeper", "model_by": role.get("model_by") or "",
              "model_at": role.get("model_at"), "gauge": None, "busy": None, "last_turn_ts": None,
              "cwd": ""}
    errors = []
    ok, st, sess = await _ac_call("/api/console/sessions", base=base)
    if ok and isinstance(sess, dict):
        rows = sess.get("sessions") if isinstance(sess.get("sessions"), list) else []
        row = next((s for s in rows if isinstance(s, dict) and (s.get("id") == sid or s.get("session_id") == sid)), None)
        if row:
            g = ((row.get("rollover") or {}).get("gauge") or {}) if isinstance(row.get("rollover"), dict) else {}
            keeper.update(native_id=row.get("native_id") or "", busy=bool(row.get("busy")),
                          last_turn_ts=row.get("updated") or row.get("created"),
                          label=row.get("label") or keeper["label"], cwd=row.get("cwd") or "",
                          actual_model=row.get("actual_model") or "",
                          gauge={"tokens": g.get("tokens") or 0, "window": g.get("window") or 0,
                                 "pct": g.get("pct") or 0, "source": g.get("source") or "",
                                 "model": g.get("model") or ""} if g else None)
        else:
            errors.append("keeper session %s not in /api/console/sessions" % (sid or "(none)"))
    else:
        errors.append("serve sessions: " + _ac_err_text(st, sess))
    ok, st, cat = await _ac_call("/api/console/models", base=base)
    if ok and isinstance(cat, dict) and isinstance(cat.get("models"), list):
        out["models"] = [{"backend": m.get("backend") or "", "model": m.get("model") or "",
                          "label": m.get("label") or m.get("model") or "", "source": "serve"}
                         for m in cat["models"] if isinstance(m, dict)]
    else:
        errors.append("serve models: " + _ac_err_text(st, cat))
    # the cumulative relayed total — the number the old gauge mislabelled as
    # context; from the serve's own usage DB, shown as a separate line.
    cache = await _ac_serve_cache_doc(base=base)
    if isinstance(cache, dict):
        tot = cache.get("totals") or {}
        out["relayed"] = {"tokens": tot.get("total_tokens") or 0, "runs": cache.get("requests_total") or 0,
                          "cost_usd": tot.get("cost_usd") or 0, "source": "serve:/api/usage/report"}
    out.update(ok=True, keeper=keeper, errors=errors, seat=await _locus_keeper_seat(app, vm))
    return out


async def api_locus_keeper(request):
    """GET /api/locus/keeper?vm=<locus> → {locus, serve:{url,path}, keeper:{session_id,
    native_id, backend, model, pending_model, label, gauge:{tokens,window,pct,source},
    busy, last_turn_ts}, models:[{backend, model, label, source}], relayed, seat:{…,
    emergency:true}}.
    POST /api/locus/keeper {vm, action:"set_model", model, backend?} → forwards to
    that serve's roster (set_provider first when the backend changes, then
    set_model) and returns the roster status. Errors: {ok:false, error:<upstream text>}."""
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:                                # noqa: BLE001
            body = {}
        body = body if isinstance(body, dict) else {}
        want = str(request.query.get("vm") or body.get("vm") or "").strip()
    else:
        body = {}
        want = str(request.query.get("vm") or "").strip()
    vm = await _resolve_vm_name(want)
    if vm is None:
        return web.json_response({"ok": False, "error": "unknown locus " + want}, status=404)
    if request.method == "GET":
        return web.json_response(await _locus_keeper_doc(request.app, vm))
    action = str(body.get("action") or "set_model").strip()
    if action != "set_model":
        return web.json_response({"ok": False, "error": "unknown action %r" % action}, status=400)
    model = str(body.get("model") or "").strip()
    if model and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", model):
        return web.json_response({"ok": False, "error": "bad model id"}, status=400)
    backend = str(body.get("backend") or "").strip()
    by = str(body.get("by") or "station-ui")[:40]
    base, _path, key, err = await _locus_keeper_base(vm)
    if err:
        return web.json_response({"ok": False, "error": err, "locus": key or vm}, status=502)
    steps = []
    if backend:
        ok, st, roster = await _ac_call("/api/session/roster", base=base)
        if not ok or not isinstance(roster, dict):
            return web.json_response({"ok": False, "error": "serve roster: " + _ac_err_text(st, roster)}, status=502)
        role = next((r for r in (roster.get("roles") or []) if isinstance(r, dict) and r.get("role") == "keeper"), {})
        if backend != (role.get("backend") or ""):
            ok, st, res = await _ac_call("/api/session/roster",
                                         {"action": "set_provider", "role": "keeper", "backend": backend,
                                          "model": model, "by": by}, base=base)
            if not ok or not isinstance(res, dict) or not res.get("ok", True):
                return web.json_response({"ok": False, "step": "set_provider",
                                          "error": "serve set_provider: " + _ac_err_text(st, res)}, status=502)
            steps.append("set_provider:" + backend)
    ok, st, res = await _ac_call("/api/session/roster",
                                 {"action": "set_model", "role": "keeper", "model": model, "by": by}, base=base)
    if not ok or not isinstance(res, dict) or not res.get("ok", True):
        return web.json_response({"ok": False, "step": "set_model", "steps": steps,
                                  "error": "serve set_model: " + _ac_err_text(st, res)}, status=502)
    steps.append("set_model:" + (model or "(serve default)"))
    audit(request, "keeper-model", "%s → %s%s" % (key or "@self", model or "(serve default)",
          (" [" + backend + "]") if backend else ""), ok=True)
    role = next((r for r in (res.get("roles") or []) if isinstance(r, dict) and r.get("role") == "keeper"), {})
    return web.json_response({"ok": True, "locus": key or _station_locus() or "host", "steps": steps,
                              "staged": bool(res.get("staged")), "model": res.get("model", role.get("model")),
                              "pending_model": res.get("pending_model", role.get("pending_model")) or "",
                              "session_id": res.get("session_id") or role.get("session_id") or "",
                              "roster": res})


# ── 1.0.146 (t4250): "every locus has a keeper that knows its locus" ────────────
STANDING_ROLES = ("keeper", "chat", "worker")
STANDING_PERMISSION_MODE = os.environ.get("STATION_STANDING_PERMISSION_MODE") or "bypassPermissions"


def _standing_config_keys():
    """What every managed locus's AC_ROOT/config.json must carry for its standing
    sessions (serve-core >= 0.1.4 permission_mode_by_label; 0.1.15 standing_*).
    '~' = that locus user's $HOME, resolved ON the locus."""
    return {"standing_permission_mode": STANDING_PERMISSION_MODE, "standing_cwd": "~",
            "permission_mode_by_label": {r: STANDING_PERMISSION_MODE for r in STANDING_ROLES}}


def _nuances_from(facts, pointer, managed, vm, t, instance="", port=None):
    """The LOCUS NUANCES record (directives.nuances_block renders it) from the
    facts read ON the locus (standing_config), the loci registry pointer
    (toolserver loci/pointers) and the station's serve-managed record."""
    pointer = pointer if isinstance(pointer, dict) else {}
    managed = managed if isinstance(managed, dict) else {}
    goal = str(pointer.get("goal") or "")
    rules = [r for r in (pointer.get("tree_rules") or pointer.get("rules") or []) if isinstance(r, str)]
    for ln in goal.splitlines():                     # a registry goal may spell tree rules inline
        if re.match(r"^\s*(trees?|source|src)\b.*:", ln, re.I):
            rules.append(ln.strip()[:200])
    inst = instance or managed.get("instance") or "station"
    return {"home": facts.get("home") or _locus_home(pointer.get("user") or managed.get("user") or ""),
            "user": facts.get("user") or pointer.get("user") or managed.get("user") or "",
            "hostname": pointer.get("host") or ("this host" if not t.remote else t.name),
            "kind": t.kind, "state": facts.get("state") or "", "gate": facts.get("gate") or "",
            "sudo_n": bool(facts.get("sudo_n")), "units": list(facts.get("units") or []),
            "trees": list(facts.get("trees") or []), "tree_rules": rules[:8],
            "serve": {"instance": inst, "port": port or managed.get("port") or "",
                      "unit": "abstract-claude-serve@%s.service" % inst,
                      "permission_mode": STANDING_PERMISSION_MODE, "cwd": facts.get("home") or "~"},
            "goal": goal[:600], "registry": "toolserver loci/pointers" if pointer else "station serve-managed.json",
            "updated": time.strftime("%Y-%m-%d %H:%M")}


async def _locus_standing_apply(app, vm, t, instance="", port=None):
    """Ship the standing roles' config into the locus's AC_ROOT/config.json
    (missing keys only), record the LOCUS NUANCES in its directives/station.json
    and re-render its directives (the keeper reads them next turn). Idempotent."""
    facts = await LX.standing_config(t, _standing_config_keys())
    pointer = {}
    try:
        feed = await _ts_call(app, "loci/pointers", {}, timeout=8)
        pointer = ((feed or {}).get("ssh_hosts") or {}).get(t.locus or vm) or {}
    except Exception:                                    # noqa: BLE001 — registry down: facts only
        pointer = {}
    nuances = _nuances_from(facts, pointer, _serve_managed().get(vm) if vm else {}, vm, t, instance, port)
    try:
        cfg = await _seat_cfg(t)
        cur = _cfg_json(cfg, "station")
    except LX.LocusError:
        cur = None
    st = dict(cur if isinstance(cur, dict) else {}, nuances=nuances)
    if not t.remote:
        st.update(port=PORT, locus=_station_locus())
    path = await LX.write_cfg(t, "station", json.dumps(st))
    if t.remote:
        rendered, why = await _remote_render(t)
    else:
        rendered, why = bool(_render_session_directives()), "host render"
    _audit_line("locus-standing", "%s: config %s added %s; nuances → %s; render %s (%s)"
                % (vm or "@self", facts.get("path"), ",".join(facts.get("added") or []) or "nothing (all present)",
                   path, "ok" if rendered else "FAILED", (why or "")[:120]))
    return {"ok": True, "config": {"path": facts.get("path"), "added": facts.get("added") or [],
                                   "present": facts.get("present") or []},
            "nuances": nuances, "station_json": path, "rendered": rendered, "render_detail": why}


async def api_locus_serve_standing(request):
    """POST /api/locus/serve/standing[?vm=] — (re)apply the standing roles'
    config + LOCUS NUANCES to a locus without re-provisioning its serve."""
    want = str(request.query.get("vm") or "").strip()
    vm = await _resolve_vm_name(want) if want else ""
    if vm is None:
        return web.json_response({"ok": False, "error": "unknown locus " + want}, status=404)
    t = _locus_target(vm)
    try:
        res = await _locus_standing_apply(request.app, vm, t)
    except LX.LocusError as e:
        return web.json_response({"ok": False, "vm": vm or "@self", "error": str(e)}, status=502)
    audit(request, "locus-standing", "%s added=%s" % (vm or "@self", ",".join(res["config"]["added"]) or "-"))
    return web.json_response(dict(res, vm=vm or "@self"))


async def _ac_serve_cache_doc(base=None):
    """The /api/frontier/cache shape, built from the keeper serve's OWN usage DB
    (/api/usage/report + /api/usage/cache) instead of a tmux seat transcript.
    Same keys the SeatMeter already renders, so the token meter keeps working
    when serve is the surface. Best-effort: None when serve is unreachable.
    base (1.0.141) = the SELECTED locus's serve (its forward), default host."""
    rep = await _ac_get("/api/usage/report?last=50", base=base)
    if not isinstance(rep, dict):
        return None
    cache = await _ac_get("/api/usage/cache", base=base) or {}
    totals = rep.get("totals") or {}
    recent = rep.get("recent") or []
    newest = recent[0] if recent else {}
    doc = {"ok": True, "backend": "serve", "seat": "abstract-claude serve",
           "seat_live": True, "surface": "serve", "upstream": base or AC_UPSTREAM,
           "unit": AC_SERVE_UNIT, "busy": False,
           "session_id": newest.get("session_id") or "",
           "model": newest.get("model") or "",
           "context_tokens": 0, "cached_tokens": 0, "cache_read": 0,
           "cache_creation": 0, "output_tokens": 0,
           "requests_total": totals.get("runs") or 0, "requests_last_hour": 0,
           "ttl_s": None, "remaining_s": 0, "age_s": None,
           "totals": {"total_tokens": totals.get("total_tokens") or 0,
                      "cache_read": totals.get("cache_read_tokens") or 0,
                      "cache_creation": totals.get("cache_creation_tokens") or 0,
                      "output": totals.get("output_tokens") or 0,
                      "cost_usd": totals.get("total_cost_usd") or 0}}

    from datetime import datetime as _dtcls

    def _ts(v):
        try:
            return _dtcls.fromisoformat(str(v)).timestamp()
        except Exception:                                # noqa: BLE001
            return None
    now = time.time()
    doc["requests_last_hour"] = sum(
        1 for r in recent if (_ts(r.get("ts")) or 0) >= now - 3600)
    if newest:
        cr = newest.get("cache_read_tokens") or 0
        cc = newest.get("cache_creation_tokens") or 0
        doc["cache_read"], doc["cache_creation"] = cr, cc
        doc["cached_tokens"] = cr + cc
        doc["output_tokens"] = newest.get("output_tokens") or 0
        doc["context_tokens"] = (newest.get("input_tokens") or 0) + cr + cc
        t = _ts(newest.get("ts"))
        if t:
            doc["age_s"] = int(max(0, now - t))
    for s in (cache.get("sessions") or []):
        if s.get("session_id") == doc["session_id"]:
            doc["ttl_s"] = s.get("ttl_s")
            doc["expires_at"] = s.get("expires_at")
            doc["remaining_s"] = int(max(0, s.get("remaining_s") or 0))
            break
    return doc


# d411 (2026-09-17): the /ac fetch shim is GONE — no HTML/JS rewriting for the
# station console. It wrapped window.fetch and prefixed EVERY root-relative URL
# with /ac, so it could not tell serve's own API from the station's:
# keeper-live.js stationApi() deliberately targets the ORIGIN ROOT to reach the
# STATION's /api/b/chat, and the shim rewrote that to /ac/api/b/chat -> serve
# has no such route -> 404 and a dead Local (B) pane. No rewrite is needed: the
# served dist is already baked for /ac (resources/bin/abstract-claude-serve-run)
# and serve strips the prefix natively (AC_UI_BASE=/ac -> abstract_claude
# server.py _strip_base). ac_proxy below is now a DUMB same-origin pass-through.


def _ac_rebase(text, base):
    """1.0.101: the serve dist is baked for /ac (webui-station); under a locus
    base (/ac/@hugpy) every absolute "/ac/..." reference must follow.

    d411: load-bearing ONLY for the per-locus consoles (/ac/@<locus>), whose
    remote serve runs as a different user on a different host and so cannot be
    re-pointed with AC_UI_BASE from here. A no-op for the plain /ac console."""
    if base == "/ac":
        return text
    return (text.replace('"/ac/', '"' + base + '/').replace("'/ac/", "'" + base + "/")
                .replace('"/ac"', '"' + base + '"'))


def _ac_rebase_body(body, base):
    """Per-locus only: rewrite baked "/ac/..." refs to "/ac/@<locus>/...".
    Returns the body byte-for-byte for the plain /ac console (base == "/ac")
    and for anything that is not utf-8."""
    if base == "/ac":
        return body
    try:
        return _ac_rebase(body.decode("utf-8"), base).encode("utf-8")
    except UnicodeDecodeError:
        return body


# 1.0.97 (t337): the /ac chat turn can run for many minutes with no upstream
# bytes in between tool calls; never cap the whole request, only the connect
# and the gap between reads.
_AC_PROXY_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=3600)


async def ac_proxy(request):
    tail = request.match_info.get("tail", "")
    upstream, unit, base = AC_UPSTREAM, AC_SERVE_UNIT, "/ac"
    # 1.0.101: /ac/@<locus>/... frames that locus's OWN serve (ac-loci.json)
    m = re.match(r"@([A-Za-z0-9_.-]+)(?:/(.*))?$", tail)
    if m:
        url, unit2, path, key = await _ac_resolve(m.group(1))
        if not url:
            return web.json_response({"error": "no reachable abstract-claude serve on locus " + key + ": "
                                      + ((_AC_DISCOVERED.get(key) or {}).get("error") or "not discovered"),
                                      "hint": "add it to " + str(AC_LOCI_PATH) + " or STATION_CONSOLE_AC_LOCI"}, status=404)
        if m.group(2) is None:                      # /ac/@hugpy -> /ac/@hugpy/ (relative asset paths)
            raise web.HTTPFound(path)
        upstream, unit, base, tail = url, unit2, path.rstrip("/"), (m.group(2) or "")
    qs = ("?" + request.query_string) if request.query_string else ""
    target = f"{upstream}/{tail}{qs}"
    sess = request.app.get("proxy_sess")
    if sess is None:
        return web.json_response({"error": "proxy session not started"}, status=502)
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in _PROXY_HOP}
    try:
        async with sess.request(request.method, target, headers=fwd,
                                data=await request.read(),
                                allow_redirects=False,
                                timeout=_AC_PROXY_TIMEOUT) as r:
            out = {k: v for k, v in r.headers.items()
                   if k.lower() not in _PROXY_HOP
                   | {"content-encoding", "content-length"}}
            ctype = r.headers.get("Content-Type", "")
            if "text/html" in ctype:
                # d411: NO shim injection. For the plain /ac console this is a
                # byte-for-byte pass-through; only a per-locus base rebases.
                return web.Response(status=r.status, headers=out,
                                    body=_ac_rebase_body(await r.read(), base))
            if base != "/ac" and (tail.split("?")[0].endswith(".js") or "javascript" in ctype):
                # a locus console: the bundle's baked "/ac/api" etc. must follow the base
                return web.Response(status=r.status, headers=out,
                                    body=_ac_rebase_body(await r.read(), base))
            # 1.0.97 (t337): stream every non-HTML body through AS IT ARRIVES.
            # The chat reply is SSE (serve _chat_stream); `await r.read()` held
            # it until the turn ended, so the console showed no live progress,
            # and past aiohttp's default 5-min total timeout the request died
            # with a 500 and the reply only appeared after a page refresh.
            out["X-Accel-Buffering"] = "no"          # nginx: do not re-buffer
            out.setdefault("Cache-Control", "no-store")
            resp = web.StreamResponse(status=r.status, headers=out)
            await resp.prepare(request)
            try:
                async for chunk in r.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
            except (ConnectionResetError, asyncio.CancelledError):
                pass                                 # browser went away mid-stream
            return resp
    except (aiohttp.ClientError, asyncio.TimeoutError):
        # t320: a self-retrying holding page — serve restarts (seat wipe,
        # relaunch, upgrade) take a few seconds; the pane must wait here,
        # never fall back to a tmux seat.
        return web.Response(
            status=502, content_type="text/html",
            headers={"Cache-Control": "no-store", "Retry-After": "3"},
            text="<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=3>"
                 "<body style=\"background:#0d1117;color:#e6e9ef;font:14px system-ui;"
                 "display:flex;align-items:center;justify-content:center;height:100vh;margin:0\">"
                 "<div><b>keeper chat restarting…</b><br><span style=\"color:#8b949e\">"
                 "abstract-claude serve at " + upstream + " is not answering yet "
                 "(unit " + unit + "); retrying every 3 s.</span></div></body>")


# ── /ts: gated same-origin proxy to the toolserver (focus/handoff/board/db) ──
# Lets the SPA (FocusChip, handoff panels, the 🗄 db drawer) and local scripts
# reach the toolserver without CORS. Deliberately an ALLOW-LIST of tool prefixes
# — this is NOT a general toolserver pass-through (no fs/sys/ui/media: those
# execute on the toolserver host). 1.0.85: db/ (the central Postgres — the
# read-only SELECT/WITH gate is enforced upstream by db/query), comms/ (pings +
# inbox), canvas/ (design/flow documents), seat/ and prompt/ join the list so
# the UI and scripts reach them through the station.
TS_UPSTREAM = _ts_resolve_url()     # configured -> advertised on this host -> local default
_TS_ALLOWED = ("assess/", "handoff/", "exchange/", "todo/", "loci/", "board/", "session/",
               "db/", "comms/", "canvas/", "seat/", "prompt/")   # 1.0.85: + db/comms/canvas/seat/prompt


async def ts_proxy(request):
    tail = request.match_info.get("tail", "")
    if not tail.startswith(_TS_ALLOWED):
        return web.json_response({"error": "path not allowed via /ts"}, status=403)
    qs = ("?" + request.query_string) if request.query_string else ""
    target = f"{TS_UPSTREAM.rstrip('/')}/{tail}{qs}"
    sess = request.app.get("proxy_sess")
    if sess is None:
        return web.json_response({"error": "proxy session not started"}, status=502)
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in _PROXY_HOP}
    # 1.0.85: the STATION carries the toolserver credential (toolserver.env →
    # HUGPY_OPERATOR_TOKEN, see _ts_headers) — the SPA never has it, so every
    # /ts call used to reach the toolserver anonymous and come back 401
    # "unauthorized" (why the FocusChip was silently hidden). Attach it ONLY
    # when the caller sent no credential of its own: an explicit
    # X-Operator-Token / Authorization wins, so a local script keeps its own
    # identity. Case-blind — a browser fetch sends lower-case header names.
    if not any(k.lower() in ("x-operator-token", "authorization") for k in fwd):
        tok = _ts_headers().get("X-Operator-Token")
        if tok:
            fwd["X-Operator-Token"] = tok
    try:
        async with sess.request(request.method, target, headers=fwd,
                                data=await request.read(),
                                allow_redirects=False) as r:
            out = {k: v for k, v in r.headers.items()
                   if k.lower() not in _PROXY_HOP | {"content-encoding",
                                                     "content-length"}}
            return web.Response(status=r.status, body=await r.read(), headers=out)
    except aiohttp.ClientError as e:
        return web.json_response({"error": f"toolserver unreachable: {e}"},
                                 status=502)


# ── SESSION-PULL-PATCH 2026-09-02: session pull + resume-capable seats ───────────────────────────
_PULL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HANDOFF_SPAWN_FEATURES = ["resume", "fork", "name", "ssh-hosts", "probe", "verify", "session-id"]
HANDOFF_SPAWN_VERIFY_S = 2.5     # seconds a fresh seat must stay alive before it is reported spun


def _station_id():
    return f"{getpass.getuser()}@{socket.gethostname().split('.')[0]}"


def _local_ips():
    ips = {"127.0.0.1", "localhost", socket.gethostname(), socket.gethostname().split(".")[0]}
    try:
        for fam, _t, _p, _c, sa in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(sa[0])
    except OSError:
        pass
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout
        ips.update(out.split())
    except Exception:
        pass
    return ips


def _is_self_target(ssh_t):
    """user@host[:port] names THIS station's own service user on this machine —
    the seat can run locally, no ssh hop (and no key requirement)."""
    user, _, hostport = ssh_t.rpartition("@")
    host = hostport.partition(":")[0]
    return bool(user) and user == getpass.getuser() and host in _local_ips()


def _ssh_host_for_target(user, host, port=22):
    """A registered ssh-host (local file or toolserver-distributed) that reaches
    user@host:port — a seat spawned there uses its key/port instead of a bare
    `ssh user@host` (which only works where the service user's default key is
    already authorized)."""
    try:
        port = int(port or 22)
    except (TypeError, ValueError):
        port = 22
    for n, h in _ssh_hosts().items():
        if (h.get("host") == host and (h.get("user") or "root") == user
                and int(h.get("port") or 22) == port):
            return n, h
    return "", None


# ── 1.0.63: toolserver credential (the station carries it; see TOOLSERVER_ENV_PATH)
async def api_toolserver_config_get(request):
    tok = os.environ.get("HUGPY_OPERATOR_TOKEN", "")
    return web.json_response({
        "ok": True, "path": str(TOOLSERVER_ENV_PATH), "file": TOOLSERVER_ENV_PATH.is_file(),
        "url": TS_UPSTREAM, "token_set": bool(tok),
        "token_preview": (tok[:4] + "…" + tok[-2:]) if tok else "",
        "loaded_from_file": _TOOLSERVER_ENV_LOADED,
        "loci_sync": {"ts": _TS_LOCI["ts"], "error": _TS_LOCI["error"],
                      "distributed": sorted(_TS_LOCI["hosts"])}})


async def api_toolserver_config_post(request):
    """POST {token, url?} — write toolserver.env (0600), apply live, resync loci."""
    global TS_UPSTREAM
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    tok = str(body.get("token") or "").strip()
    url = str(body.get("url") or "").strip()
    if not re.match(r"^[A-Za-z0-9_.:-]{16,200}$", tok):
        return web.json_response({"ok": False, "error": "token looks wrong (16-200 url-safe chars)"},
                                 status=400)
    if url and not re.match(r"^https?://[A-Za-z0-9.:\[\]-]+(/[A-Za-z0-9._/-]*)?$", url):
        return web.json_response({"ok": False, "error": "url must be http(s)://host[:port][/path]"},
                                 status=400)
    _write_toolserver_env(tok, url)
    if url:
        TS_UPSTREAM = url
    _TS_LOCI.update(error="", ts=0)
    await _loci_sync_once(request.app)
    audit(request, "toolserver-config", f"url={url or TS_UPSTREAM} token={tok[:4]}…")
    return await api_toolserver_config_get(request)


async def handoff_spawn(request):
    """GET  /api/handoff/spawn → {ok, features, station, socket} — capability
    probe: the toolserver's session/pull asks for 'resume' here before it POSTs.
    POST /api/handoff/spawn {id, seat, ssh, dir, user, brief, resume?, fork?, name?}
    — materialize a toolserver handoff as a REAL seat: a detached tmux session on
    the keeper socket. Without `resume` it runs `claude <brief>` at {ssh, dir,
    user} (the jump-in seat). With `resume` (a Claude Code session uuid) it runs
    `claude --resume <id>` (+ --fork-session unless fork=false) there — the
    SESSION PULL: the same transcript, full context, now seated in this station.
    `name` picks the tmux session (default handoff-h<id>). An ssh target that
    matches a registered ssh-host (local or toolserver-distributed) is reached
    with that host's key/port. Called by the toolserver (HANDOFF_SPAWN_URL) with
    X-Console-Token, or by an operator session; auth_mw has gated this route.
    1.0.146: the reply is VERIFIED — the seat must still be alive after
    HANDOFF_SPAWN_VERIFY_S (a dead pane is reported with its last lines and the
    session is killed, never left as a ghost 'spun' seat); a fresh seat is
    given its Claude session id (`--session-id`, returned as session_id) and
    the env HANDOFF_ID=<handoff> / EXCHANGE_LOCUS=<locus> so its SessionStart
    hook pulls exactly this handoff row from the toolserver."""
    if request.method == "GET":
        return web.json_response({"ok": True, "features": HANDOFF_SPAWN_FEATURES,
                                  "station": _station_id(), "socket": KEEPER_TMUX_SOCK})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    hid = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("id") or "x"))
    name = (str(body.get("name") or "").strip().lower()) or f"handoff-h{hid}"
    if not _PULL_NAME_RE.match(name):
        return web.json_response({"ok": False, "error": "bad seat name"}, status=400)
    seat = (body.get("seat") or "claude").strip().lower()
    ssh_t = (body.get("ssh") or "").strip()
    dirp = (body.get("dir") or "").strip() or "~"
    brief = (body.get("brief") or "").strip()
    resume = str(body.get("resume") or "").strip().lower()
    if resume and not _UUID_RE.match(resume):
        return web.json_response({"ok": False, "error": "bad resume session id"}, status=400)
    fork = body.get("fork", True)
    if not isinstance(fork, bool):
        fork = str(fork).strip().lower() in ("1", "true", "yes", "on")
    if ssh_t and not re.match(r"^[A-Za-z0-9._@:-]+$", ssh_t):
        return web.json_response({"ok": False, "error": "bad ssh target"}, status=400)
    locus = _canon_locus(str(body.get("locus") or "")) or ""
    handoff_id = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("handoff") or ""))[:16]
    session_id = "" if resume else str(uuid.uuid4())
    env_pre = ""
    if locus:
        env_pre += f"export EXCHANGE_LOCUS={shlex.quote(locus)}; "
    if handoff_id:
        env_pre += f"export HANDOFF_ID={shlex.quote(handoff_id)}; "
    inner = (env_pre + f"cd {shlex.quote(dirp)} 2>/dev/null; "
             # 1.0.63: pre-seed folder trust (same snippet the frontier seat uses) so a
             # freshly spawned seat never stalls at Claude Code's trust dialog
             f'printf %s {_SEAT_TRUST_B64} | base64 -d | python3 - "$HOME/.claude.json" "$(pwd)" {shlex.quote(_ts_configured_url())} >/dev/null 2>&1 || true; '
             "claude")
    if resume:
        inner += " --resume " + shlex.quote(resume) + (" --fork-session" if fork else "")
    else:
        inner += " --session-id " + session_id
        if brief:
            inner += " " + shlex.quote(brief)
    via = "local"
    if ssh_t and _is_self_target(ssh_t):
        via = "local-self"          # the session runs as THIS station's own user@host
        cmd = inner
    elif ssh_t:
        user, _, hostport = ssh_t.rpartition("@")
        host, _, port = hostport.partition(":")
        hname, h = _ssh_host_for_target(user or "root", host, port or 22)
        if h:
            argv_ssh = ["ssh", "-tt"] + _ssh_opts(h, True) + [_ssh_target(h), inner]
            via = "ssh-host:" + hname
        else:
            argv_ssh = ["ssh", "-tt"] + (["-p", port] if port.isdigit() else []) + \
                       [(user + "@" if user else "") + host, inner]
            via = "ssh"
        cmd = " ".join(shlex.quote(a) for a in argv_ssh)
    else:
        cmd = inner
    if not _tmux_available():
        return web.json_response({"ok": False, "error": TMUX_MISSING_MSG}, status=503)
    # remain-on-exit is set in the SAME tmux command so a seat that dies at once
    # leaves its last lines readable instead of vanishing with the session.
    argv = _scope_prefix() + ["tmux", "-L", KEEPER_TMUX_SOCK, "new-session", "-d", "-s", name,
                              "bash", "-lc", cmd, ";", "set-option", "-t", "=" + name, "remain-on-exit", "on"]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return web.json_response(
            {"ok": False, "error": (out or b"").decode()[:300] or
             f"tmux exited {proc.returncode} (duplicate session, or the seat exited at once)"}, status=502)
    alive, tail = await _seat_verify_alive(name, HANDOFF_SPAWN_VERIFY_S)
    if not alive:
        audit(request, "handoff-spawn", f"{name} DIED at launch via={via} target={ssh_t or 'local'}:{dirp}: {tail[:160]}", ok=False)
        return web.json_response({"ok": False, "error": "seat exited at launch: " + (tail or "no output")[:400],
                                  "session": name, "via": via}, status=502)
    handle = f"tmux -L {KEEPER_TMUX_SOCK} attach -t {name}"
    audit(request, "handoff-spawn",
          f"{name} seat={seat} via={via} target={ssh_t or 'local'}:{dirp} verified"
          + (f" resume={resume[:8]} fork={fork}" if resume else f" session_id={session_id[:8]}")
          + (f" handoff={handoff_id}" if handoff_id else ""))
    return web.json_response({"ok": True, "handle": handle, "session": name, "verified": True,
                              "session_id": session_id, "handoff": handoff_id, "locus": locus,
                              "socket": KEEPER_TMUX_SOCK, "resumed": resume, "fork": fork,
                              "via": via, "station": _station_id(),
                              "attach": f"/wsterm?surface=shell&vm=@keeper&tmux={name}"})


async def _seat_verify_alive(name, wait_s):
    """(alive, tail): after wait_s, is the seat's pane still running? A dead
    pane's last lines are captured and the session is killed (no ghost seat);
    a live one gets remain-on-exit switched back off."""
    await asyncio.sleep(wait_s)
    rc, out, _ = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "display-message", "-p", "-t", "=" + name,
                            "#{pane_dead} #{pane_dead_status}")
    if rc != 0:
        return False, "tmux session vanished (" + (out or "").strip()[:200] + ")"
    dead, _, status = (out or "").strip().partition(" ")
    if dead == "1":
        rc2, cap, _ = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "capture-pane", "-p", "-t", "=" + name, "-S", "-12")
        lines = [ln.rstrip() for ln in (cap or "").splitlines() if ln.strip()] if rc2 == 0 else []
        await _run("tmux", "-L", KEEPER_TMUX_SOCK, "kill-session", "-t", "=" + name)
        return False, f"rc={status or '?'}: " + " | ".join(lines[-6:])
    await _run("tmux", "-L", KEEPER_TMUX_SOCK, "set-option", "-t", "=" + name, "remain-on-exit", "off")
    return True, ""


async def api_sessions_pulled(request):
    """GET /api/sessions/pulled — claude sessions pulled into the fleet (the
    toolserver's loci feed) + whether each one's tmux seat is live on THIS
    station's keeper socket, plus any live jump-in/pull seats the feed does not
    know. Each live row carries the /wsterm attach URL."""
    live = set()
    rc, out, _err = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "list-sessions", "-F", "#S")
    if rc == 0:
        live = {ln.strip() for ln in out.splitlines() if ln.strip()}
    rows = []
    for sdoc in _TS_LOCI["sessions"]:
        name = str(sdoc.get("tmux") or "")
        is_live = bool(name and name in live)
        rows.append(dict(sdoc, live=is_live, kind="pull",
                         attach=(f"/wsterm?surface=shell&vm=@keeper&tmux={name}" if is_live else "")))
    known = {r.get("tmux") for r in rows}
    for n in sorted(live):
        if n.startswith(("handoff-", "pull-")) and n not in known:
            rows.append({"locus": n, "tmux": n, "live": True, "kind": "seat", "goal": "",
                         "session_id": "", "attach": f"/wsterm?surface=shell&vm=@keeper&tmux={n}"})
    return web.json_response({"ok": True, "sessions": rows, "station": _station_id(),
                              "loci_ts": _TS_LOCI["ts"], "loci_error": _TS_LOCI["error"],
                              "toolserver": TS_UPSTREAM})


async def _start_bugreport_api(app):
    api_script = ROOT / "bugreport-api.py"
    if not api_script.exists():
        app["bugreport_api_proc"] = None
        return
    if app.get("proxy_sess") is None:            # console-api starts first & makes it
        app["proxy_sess"] = aiohttp.ClientSession()
    _reap_stale_sidecar(api_script)
    env = {**os.environ, "BUGREPORT_API_PORT": str(BUGREPORT_API_PORT)}
    app["bugreport_api_proc"] = await asyncio.create_subprocess_exec(
        sys.executable, str(api_script), env=env, preexec_fn=_dies_with_parent,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    for _ in range(40):
        try:
            async with app["proxy_sess"].get(
                    BUGREPORT_API_BASE + "/healthz",
                    timeout=aiohttp.ClientTimeout(total=1)) as r:
                await r.read()
                break
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(0.25)


async def _stop_bugreport_api(app):
    proc = app.get("bugreport_api_proc")
    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


async def bugreport_proxy(request):
    """Forward a request to the bugreport-api sidecar, verbatim (a locus's
    central tails reroute to the DB first)."""
    resp = await _central_vm_reroute(request)
    if resp is not None:
        return resp
    sess = request.app.get("proxy_sess")
    if sess is None:
        return web.json_response({"error": "bugreport-api not started"}, status=502)
    target = BUGREPORT_API_BASE + request.path_qs
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in _PROXY_HOP}
    body = await request.read()
    try:
        async with sess.request(request.method, target, headers=fwd,
                                data=body, allow_redirects=False) as r:
            payload = await r.read()
            out = {k: v for k, v in r.headers.items()
                   if k.lower() not in _PROXY_HOP | {"content-encoding"}}
            return web.Response(status=r.status, body=payload, headers=out)
    except aiohttp.ClientError as e:
        return web.json_response(
            {"error": f"bugreport-api unreachable: {e}"}, status=502)


# The in-app terminal's three fixed surfaces and their selectable backends
# (MCT design: Local Keeper C<->B, Frontier Keeper C->B->A, Terminal Shell).
# Keys are what the UI sends; commands are a SERVER-side whitelist so the
# browser can never inject a command line.
# GROUNDING FOLLOWS THE ACTIVE VM (operator, 2026-08-12): each station's model
# surfaces are ITS OWN — A, B, and the local keeper live and ground IN the VM
# the SPA has active, exec'd in as the unprivileged dev user, where that
# station's agents and credentials live (vm-new seeds them). With no VM active
# (the "keeper" seat = this machine) the surfaces ground on the host — the
# fleet's one deliberate privileged seat — unless the site pins a static model
# VM via its unit:
#   Environment=STATION_CONSOLE_MODEL_VM=hugpy   (ae's site config does this)
# The static pin is the fallback grounding, not an override: an active VM wins.
MODEL_VM = os.environ.get("STATION_CONSOLE_MODEL_VM", "")


# Folder-trust seed (op, 2026-09-01): Claude Code stops at its "Is this a
# project you trust?" dialog on a fresh locus and the seat exits to a shell —
# hasCompletedOnboarding alone is NOT enough (trust is per-project). Mark the
# seat's workspace ($(pwd)) trusted in the shared .claude.json. base64 so it
# survives the tmux/ssh quoting layers with no shell metachars to escape.
# 1.0.83 (operator 2026-09-04: "launch literally needs to create a new .claude"):
# every claude-code seat/keeper launch goes through `abstract-claude launch`, which
# materializes a BRAND-NEW config dir per launch (~/.claude-sessions/<stamp>-<pid>-
# <label>, CLAUDE_CONFIG_DIR) seeded from the A settings template
# ($AC_SETTINGS_JSON), the login identity + freshest credentials from the seat
# user's ~/.claude, folder trust for the workspace and the toolserver MCP bridge.
# The seat prelude self-upgrades abstract-claude to this floor so the fresh-dir
# behaviour is guaranteed on every locus, then falls back to the legacy
# persistent ~/.claude-seat/<label> only if abstract-claude cannot be had.
AC_MIN_VERSION = "0.1.50"
_AC_ENSURE = (
    'export PATH="$HOME/.local/bin:$PATH"; '
    'ac_ok=0; if command -v abstract-claude >/dev/null 2>&1; then '
    'ac_v="$(abstract-claude -V 2>/dev/null | grep -oE \'[0-9]+(\\.[0-9]+)+\' | tail -1)"; '
    'python3 -c \'import sys;t=lambda v:tuple(int(x) for x in v.split("."));'
    'sys.exit(0 if t(sys.argv[1])>=t(sys.argv[2]) else 1)\' "${ac_v:-0}" ' + AC_MIN_VERSION + ' '
    '>/dev/null 2>&1 && ac_ok=1; fi; '
    'if [ "$ac_ok" != 1 ]; then '
    'echo "  abstract-claude: ensuring >= ' + AC_MIN_VERSION + ' (fresh per-launch .claude)"; '
    # upgrade with the interpreter that OWNS the `abstract-claude` command (a seat
    # venv shim, pipx, --user, conda) — `pip --user` alone leaves a venv shim stale.
    'ac_py=""; ac_exe="$(command -v abstract-claude 2>/dev/null)"; '
    '[ -n "$ac_exe" ] && ac_py="$(sed -n \'1s/^#! *//p\' "$ac_exe" 2>/dev/null)"; '
    '{ { [ -n "$ac_py" ] && [ -x "$ac_py" ] && "$ac_py" -m pip install -q -U "abstract-claude>=' + AC_MIN_VERSION + '" >/dev/null 2>&1; } '
    '|| python3 -m pip install --user -q -U "abstract-claude>=' + AC_MIN_VERSION + '" >/dev/null 2>&1 '
    '|| pipx upgrade abstract-claude >/dev/null 2>&1 || pipx install abstract-claude >/dev/null 2>&1; } || true; '
    'hash -r 2>/dev/null; '
    # re-CHECK the version (not mere presence): an old abstract-claude that the
    # upgrade could not replace must not be trusted with the fresh-dir launch.
    'if command -v abstract-claude >/dev/null 2>&1; then '
    'ac_v="$(abstract-claude -V 2>/dev/null | grep -oE \'[0-9]+(\\.[0-9]+)+\' | tail -1)"; '
    'python3 -c \'import sys;t=lambda v:tuple(int(x) for x in v.split("."));'
    'sys.exit(0 if t(sys.argv[1])>=t(sys.argv[2]) else 1)\' "${ac_v:-0}" ' + AC_MIN_VERSION + ' '
    '>/dev/null 2>&1 && ac_ok=1; fi; fi; ')
_SEAT_TRUST_B64 = "aW1wb3J0IGpzb24sc3lzLG9zLHJlCnAsd3M9c3lzLmFyZ3ZbMV0sc3lzLmFyZ3ZbMl0KdXJsPXN5cy5hcmd2WzNdIGlmIGxlbihzeXMuYXJndik+MyBlbHNlICIiCnRyeToKICAgIGQ9anNvbi5sb2FkKG9wZW4ocCkpCmV4Y2VwdCBFeGNlcHRpb246CiAgICBkPXt9CmQuc2V0ZGVmYXVsdCgnaGFzQ29tcGxldGVkT25ib2FyZGluZycsVHJ1ZSkKZC5zZXRkZWZhdWx0KCdieXBhc3NQZXJtaXNzaW9uc01vZGVBY2NlcHRlZCcsVHJ1ZSkKZC5zZXRkZWZhdWx0KCdwcm9qZWN0cycse30pLnNldGRlZmF1bHQod3Mse30pWydoYXNUcnVzdERpYWxvZ0FjY2VwdGVkJ109VHJ1ZQojIHRvb2xzZXJ2ZXIgTUNQIGJyaWRnZSAoMS4wLjcwKTogZXZlcnkgY2xhdWRlLWNvZGUgc2VhdCBnZXRzIGl0LCBmcm9tIHRoZSBzZWF0IHVzZXIncyBPV04KIyBjcmVkZW50aWFsIGZpbGVzIChkZWItcHJvdmlzaW9uZWQpIC0gdGhlIHRva2VuIG5ldmVyIHJpZGVzIHRoZSBsYXVuY2ggY29tbWFuZCBsaW5lLgp0b2s9IiIKaG9tZT1vcy5wYXRoLmV4cGFuZHVzZXIoIn4iKQpmb3IgZiBpbiAob3MucGF0aC5qb2luKGhvbWUsIi5jb25maWciLCJodWdweSIsIm9wZXJhdG9yLmVudiIpLCBvcy5wYXRoLmpvaW4oaG9tZSwiLmNvbmZpZyIsImh1Z3B5LXN0YXRpb24iLCJ0b29sc2VydmVyLmVudiIpKToKICAgIHRyeToKICAgICAgICBmb3IgbG4gaW4gb3BlbihmKToKICAgICAgICAgICAgbT1yZS5tYXRjaChyIl4oVE9PTFNFUlZFUl9UT0tFTnxIVUdQWV9PUEVSQVRPUl9UT0tFTnxUT09MU0VSVkVSX09QRVJBVE9SX1RPS0VOKT0oLispJCIsbG4uc3RyaXAoKSkKICAgICAgICAgICAgaWYgbSBhbmQgbS5ncm91cCgyKS5zdHJpcCgpOiB0b2s9bS5ncm91cCgyKS5zdHJpcCgpLnN0cmlwKCciJyk7IGJyZWFrCiAgICBleGNlcHQgT1NFcnJvcjoKICAgICAgICBwYXNzCiAgICBpZiB0b2s6IGJyZWFrCmlmIHRvazoKICAgIG1zPWQuc2V0ZGVmYXVsdCgnbWNwU2VydmVycycse30pCiAgICBjdXI9bXMuZ2V0KCd0b29sc2VydmVyJykgb3Ige30KICAgIGlmIG5vdCBjdXIgb3IgY3VyLmdldCgnY29tbWFuZCcpPT0nYWJzdHJhY3QtY2xhdWRlJzoKICAgICAgICAjIDIwMjYtMDktMzA6IFVSTCBvbmx5IHdoZW4gY29uZmlndXJlZCAoZWxzZSB0aGUgYnJpZGdlIGRpc2NvdmVycyB0aGUgbG9jYWwgdG9vbHNlcnZlcikKICAgICAgICB1PXVybCBvciAoY3VyLmdldCgnZW52Jykgb3Ige30pLmdldCgnVE9PTFNFUlZFUl9VUkwnKSBvciAnJwogICAgICAgIGVudj17J1RPT0xTRVJWRVJfVE9LRU4nOnRva30KICAgICAgICBpZiB1OiBlbnZbJ1RPT0xTRVJWRVJfVVJMJ109dQogICAgICAgIG1zWyd0b29sc2VydmVyJ109eyd0eXBlJzonc3RkaW8nLCdjb21tYW5kJzonYWJzdHJhY3QtY2xhdWRlJywnYXJncyc6WydtY3AnXSwnZW52JzplbnZ9Cmpzb24uZHVtcChkLG9wZW4ocCwndycpKQo="


def _claude_seat_cmd(label: str, model_key: str = "claude-code", cfg=None) -> str:
    """The interactive Claude seat, launched EXACTLY like the MCT's A
    (hugpy_agent claude_adapter — operator, 2026-08-20: the two must not
    differ): a FRESH per-seat config dir on every spawn (wiped cache), the A
    settings template written into it, and auth SYMLINKED from the seat
    user's real ~/.claude — never copied, because OAuth refresh tokens
    rotate on use and a private copy is invalidated the first time any other
    seat refreshes. $HOME expands in the seat's own locus (host service user
    or the VM dev user), so this one string is correct everywhere.

    1.0.41 — EXPLICIT seat: the seat (a) states its locus/user/model/fs
    switch before starting, (b) when NO Claude login exists it prints the
    exact OAuth steps and then lets Claude Code's own login picker run (the
    credentials it writes land through the symlink in the seat user's real
    ~/.claude, so the login persists for every later seat), (c) when the
    `claude` binary is missing it prints the install line instead of
    silently dropping to a shell, and (d) passes the operator's model and the
    frontier directive (--append-system-prompt) — the same text the steward
    tab shows."""
    # 1.0.141: cfg = the TARGET locus's seat-config snapshot (read ON the
    # target by locus_exec.read_cfg) — directive, models, fs/delegate switches,
    # template and handoff are the target's own, never the viewing station's.
    tmpl = shlex.quote(_claude_seat_settings_json(cfg))
    _sysp_text = _claude_seat_system_prompt(cfg)
    sysp = shlex.quote(_sysp_text)
    ts_url_q = (shlex.quote(_ts_configured_url()) if not (cfg or {}).get("remote")
                else '"${HUGPY_TOOLSERVER_URL:-${TOOLSERVER_URL:-}}"')
    # mct v2: the native seat launched FOR the mct mode runs the operator's
    # "mct" model (frontier-models.json); the plain claude-code mode keeps its own.
    models = _frontier_models(cfg)
    model = models.get(model_key) if model_key in models else None
    if model is None:
        model = models["claude-code"]
    mflag = (" --model " + shlex.quote(model)) if model else ""
    fs = "MEDIATED (file tools denied; route via B)" if _frontier_fs_mediated(cfg) else "DIRECT"
    banner = (
        'echo "── hugpy-station claude seat ─────────────────────────────────"; '
        'echo "  seat      : ' + label + '"; '
        'echo "  user@host : $(id -un)@$(hostname)"; '
        'echo "  model     : ' + (model or "Claude Code default") + '"; '
        'echo "  fs switch : ' + fs + '"; '
        'echo "  directive : tmux seat (fallback / inference arm) · ' + str(len(_sysp_text)) + ' chars via --append-system-prompt (see 🛡 steward → directive)"; '
        'echo "  handoff   : toolserver row (handoff_pull) — injected by the SessionStart hook, never a file"; '
        'echo "  config    : FRESH per launch — abstract-claude launch → ~/.claude-sessions/<stamp>-<pid>-' + label + ' (login/identity carried from ~/.claude; legacy $c only if abstract-claude is unavailable)"; ')
    nologin = (
        'if [ ! -s "$HOME/.claude/.credentials.json" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then '
        'echo ""; echo "  ⚠ NO CLAUDE LOGIN for $(id -un)@$(hostname) — creating one now."; '
        'echo "    1. Claude Code will show its login picker: choose \"Claude account with subscription\" (OAuth)."; '
        'echo "    2. Open the printed URL on ANY device, sign in, approve, paste the code back here."; '
        'echo "    3. Credentials persist at $HOME/.claude/.credentials.json for this seat."; '
        'echo "    Offline alt: run \"claude setup-token\" elsewhere and export ANTHROPIC_API_KEY here."; '
        'echo ""; fi; ')
    noclaude = (
        'if ! command -v claude >/dev/null 2>&1; then '
        'echo ""; echo "  ✗ claude is NOT INSTALLED for $(id -un)@$(hostname)."; '
        'echo "    install: curl -fsSL https://claude.ai/install.sh | bash   (or: npm i -g @anthropic-ai/claude-code)"; '
        'echo "    then reopen this seat."; echo ""; exec bash -l; fi; ')
    # 1.0.41b (operator): the seat dir is NOT wiped any more — wiping it made
    # Claude Code re-run onboarding (theme/settings) and re-ask for login on
    # every open, because onboarding state + the OAuth account record live in
    # .claude.json, which a fresh dir lacks. Now: credentials AND .claude.json
    # are symlinked to the seat user's real ones (login once, everywhere);
    # only settings.json is rewritten from the template at each launch.
    # 1.0.83: FRESH per launch via `abstract-claude launch` (see AC_MIN_VERSION);
    # the 1.0.41b symlinked persistent seat dir below is now the FALLBACK only.
    # t321 (2026-09-16): every seat names its OWN Claude session id on the
    # command line. mct_gateway's NativeAdapter honours --session-id first and
    # (since t321) never falls through to "newest transcript" when one is
    # named — that fallthrough had pinned the adapter to the keeper SERVE
    # chat's transcript, which lives in the same ~/.claude/projects dir.
    fresh = ('__sid="$(python3 -c "import uuid;print(uuid.uuid4())" 2>/dev/null || cat /proc/sys/kernel/random/uuid)"; '
             'export MCT_SEAT_SID="$__sid"; '
             'if [ "$ac_ok" = 1 ]; then '
             f'AC_SETTINGS_JSON={tmpl} AC_SESSION_LABEL={shlex.quote(label)} '
             f'exec abstract-claude launch -- --dangerously-skip-permissions{mflag} '
             '--session-id "$__sid" --append-system-prompt "$__SYSP"; fi; '
             'echo "  ⚠ abstract-claude unavailable — LEGACY persistent seat dir $HOME/.claude-seat/' + label + '"; ')
    # t240: the real model rides the seat env too, so sidecars (seat-report)
    # and the seat's own tools can read it instead of a baked default.
    model_env = (f'export AC_MODEL={shlex.quote(model)} SEAT_MODEL={shlex.quote(model)} MCT_SEAT=claude-code; ' if model
                 else 'export MCT_SEAT=claude-code; ')
    return (f'__SYSP={sysp}; ' + model_env + _AC_ENSURE + banner + noclaude + nologin + fresh +
            'c="$HOME/.claude-seat/' + label + '"; '
            'mkdir -p "$c" "$HOME/.claude"; '
            # Claude Code rewrites these files atomically (tmp + rename), which
            # turns the symlink into a plain file holding the NEWEST token /
            # state. Adopt it back into the shared location before relinking —
            # discarding it left ~/.claude with a rotated-out refresh token and
            # every seat failing auth ("Failed to authenticate", silently).
            '[ -e "$c/.credentials.json" ] && [ ! -L "$c/.credentials.json" ] '
            '&& mv -f "$c/.credentials.json" "$HOME/.claude/.credentials.json"; '
            'ln -sf "$HOME/.claude/.credentials.json" "$c/.credentials.json"; '
            'j="$HOME/.claude.json"; [ -f "$j" ] || j="$HOME/.claude/.claude.json"; '
            '[ -e "$c/.claude.json" ] && [ ! -L "$c/.claude.json" ] && mv -f "$c/.claude.json" "$j"; '
            '[ -f "$j" ] || { printf %s \'{"hasCompletedOnboarding":true}\' > "$j"; }; '
            'ln -sf "$j" "$c/.claude.json"; '
            # seed folder-trust for the workspace (see _SEAT_TRUST_B64) so a
            # fresh locus's first claude-code seat does not die at the trust dialog
            f'printf %s {_SEAT_TRUST_B64} | base64 -d | python3 - "$j" "$(pwd)" {ts_url_q} >/dev/null 2>&1 || true; '
            f'printf %s {tmpl} > "$c/settings.json"; '
            # mct v2: even the LEGACY (no abstract-claude launch) path attaches
            # the toolserver MCP explicitly — operator: "the keeper processes
            # need to be launched with the toolserver MCP". Same server entry
            # `abstract-claude launch` writes (abstract-claude mcp + token).
            '__MCP=""; __tok="${TOOLSERVER_TOKEN:-${HUGPY_OPERATOR_TOKEN:-${STATION_CONSOLE_TOOLSERVER_TOKEN:-}}}"; '
            'if [ -n "$__tok" ] && command -v abstract-claude >/dev/null 2>&1; then '
            # 2026-09-30: TOOLSERVER_URL only when explicitly configured; else the
            # bridge discovers the toolserver advertised on this host.
            '__tsu="${HUGPY_TOOLSERVER_URL:-${TOOLSERVER_URL:-${STATION_CONSOLE_TOOLSERVER:-}}}"; '
            'if [ -n "$__tsu" ]; then '
            '__MCP=$(printf \'{"mcpServers":{"toolserver":{"type":"stdio","command":"abstract-claude","args":["mcp"],"env":{"TOOLSERVER_URL":"%s","TOOLSERVER_TOKEN":"%s"}}}}\' '
            '"$__tsu" "$__tok"); else '
            '__MCP=$(printf \'{"mcpServers":{"toolserver":{"type":"stdio","command":"abstract-claude","args":["mcp"],"env":{"TOOLSERVER_TOKEN":"%s"}}}}\' '
            '"$__tok"); fi; fi; '
            f'CLAUDE_CONFIG_DIR="$c" claude --dangerously-skip-permissions{mflag} --session-id "$__sid" '
            '${__MCP:+--mcp-config "$__MCP"} --append-system-prompt "$__SYSP"')


# --- ⇅ SSH HOSTS (1.0.41b, operator): an EXISTING machine, added by ssh, is a
# station like any LXD VM — it sits in the dropdown, and every surface for it
# (shell, frontier mct/claude-code, local, seat probe, sudo) runs over ssh the
# way a VM's runs over `lxc exec`. Stored host-side, never in the browser. ----
SSH_HOSTS_PATH = FV_STATE_HOME / "ssh-hosts.json"
# ◌ hidden loci (1.0.80, operator 2026-09-03: "remove test-vm / ubuntu-hugpy
# as loci"): a per-station list of locus names the tab strip / dropdown must
# NOT show. Non-destructive — the LXD guest or ssh host is untouched and still
# listed (flagged hidden) in the 🖥 stations drawer, where it can be unhidden.
# The destructive alternatives (🗑 delete VM, ✕ remove ssh host) stay as they are.
HIDDEN_LOCI_PATH = FV_STATE_HOME / "hidden-loci.json"


def _hidden_loci():
    try:
        v = json.loads(HIDDEN_LOCI_PATH.read_text())
        return {str(x) for x in v} if isinstance(v, list) else set()
    except (OSError, ValueError):
        return set()


def _hidden_loci_save(names):
    HIDDEN_LOCI_PATH.parent.mkdir(parents=True, exist_ok=True)
    HIDDEN_LOCI_PATH.write_text(json.dumps(sorted(names)) + "\n")

# --- Claude OAuth for the whole fleet (operator, 2026-08-21) ----------------
# `claude setup-token` gives a LONG-LIVED token read from CLAUDE_CODE_OAUTH_TOKEN;
# unlike ~/.claude/.credentials.json it never rotates, so seats stop drifting
# apart and failing auth. bin/claude-oauth-sync stores it here and installs it
# on the host and in every VM / ssh host; vm-new and ssh-host-add call it for
# new loci. Exporting it into THIS process makes every host-grounded child
# (seat, mct, console-api) inherit it without a re-login.
CLAUDE_OAUTH_TOKEN_PATH = FV_STATE_HOME / "claude-oauth-token"
CLAUDE_OAUTH_SYNC_BIN = shutil.which("claude-oauth-sync") or (
    str(ROOT / "bin" / "claude-oauth-sync") if (ROOT / "bin" / "claude-oauth-sync").is_file()
    else str(ROOT.parent / "bin" / "claude-oauth-sync") if (ROOT.parent / "bin" / "claude-oauth-sync").is_file()
    else "")


# Per-locus seat-CLI provisioning (2026-09-01): a hand-added ssh host — unlike
# a VM (vm-new installs claude) — lacks the seat user's ~/.local/bin/claude, and
# /usr/local/bin/claude (claude-home-guard) requires exactly that, so the frontier
# claude-code seat dies 'claude is not installed for <user>'. On ssh-host-add we
# install the seat CLIs on the locus (claude-code needs `claude`; mct needs
# `abstract-claude`), mirroring the token sync. Best effort; run over the locus's
# own ssh transport via _locus_run. The check is ~/.local/bin/claude specifically
# (the shim requires it — a PATH hit on the shim itself is not enough).
_SEAT_CLI_PROVISION_SH = (
    'export PATH="$HOME/.local/bin:$PATH"; '
    'if [ ! -x "$HOME/.local/bin/claude" ]; then '
    'curl -fsSL https://claude.ai/install.sh | bash '
    '|| { command -v npm >/dev/null 2>&1 && npm i -g @anthropic-ai/claude-code; } || true; '
    'fi; '
    'if ! command -v abstract-claude >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/abstract-claude" ]; then '
    '{ command -v pipx >/dev/null 2>&1 && pipx install abstract-claude; } '
    '|| python3 -m pip install --user abstract-claude || true; '
    'fi; '
    'echo seat-provision-ssh: '
    'claude=$([ -x "$HOME/.local/bin/claude" ] && echo ok || echo MISSING) '
    'abstract-claude=$(command -v abstract-claude >/dev/null 2>&1 && echo ok || echo MISSING)'
)


def _export_fleet_oauth_token() -> bool:
    try:
        tok = CLAUDE_OAUTH_TOKEN_PATH.read_text().strip()
    except OSError:
        tok = ""
    if not tok:
        # Fallback: the seat provisioner / claude-oauth-sync write the durable
        # token in KEY=VALUE form to these env files. Read it so the backend and
        # provisioner cannot drift onto different tokens (op, 2026-09-01).
        for envp in (Path.home() / ".claude-oauth.env",
                     Path.home() / ".config/claude-auth/env"):
            try:
                for line in envp.read_text().splitlines():
                    if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                        tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            except OSError:
                continue
            if tok:
                break
    if not tok:
        # Fleet chain step 3 (operator 2026-09-02, "everything uses the same
        # oauth"): no local store → ask the toolserver, the fleet's only holder,
        # with this station's operator token — the same call the shells' hook
        # (/etc/profile.d/hugpy-claude-auth.sh) and abstract-claude make.
        tok = _fetch_toolserver_oauth_token()
    if tok and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return bool(tok)


def _fetch_toolserver_oauth_token(timeout=5):
    import urllib.request
    op = (os.environ.get("STATION_CONSOLE_TOOLSERVER_TOKEN")
          or os.environ.get("HUGPY_OPERATOR_TOKEN") or "").strip()
    if not op:
        return ""
    # 2026-09-30: one resolved endpoint (TS_UPSTREAM = configured -> discovered),
    # no hardcoded fallback hosts; the shared client when the package is here.
    client = _ts_shared_client(None, op, timeout)
    if client is not None:
        try:
            doc = client.post("/claude/oauth_token", {})
            t = ((doc or {}).get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip() if isinstance(doc, dict) else ""
            return t if t.startswith("sk-ant-oat") and len(t) >= 80 else ""
        except Exception:
            return ""
    for base in [TS_UPSTREAM]:
        req = urllib.request.Request(base.rstrip("/") + "/claude/oauth_token", data=b"{}", method="POST",
                                     headers={"X-Operator-Token": op, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                doc = json.loads(r.read().decode("utf-8"))
        except Exception:
            continue
        t = ((doc.get("result") or {}).get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip() if isinstance(doc, dict) else ""
        if t.startswith("sk-ant-oat") and len(t) >= 80:
            return t
    return ""


_export_fleet_oauth_token()
_SSH_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,253}$")
_SSH_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")


# ── SESSION-PULL-PATCH 2026-09-02: toolserver-distributed loci ──────────────────────────────────
# The toolserver DB is the CENTRAL point (operator 2026-09-02): a station's
# ssh-host list is its LOCAL file merged with the toolserver's loci/pointers
# feed — machines registered by agents (loci/register), by session/pull, or by
# another station's operator. Local entries win on a name clash; names that
# are LXD guests on this host are skipped (no ambiguity in _locus_run).
_TS_LOCI = {"hosts": {}, "sessions": [], "ts": 0, "error": "", "lxd": set(),
            "self": ""}   # 1.0.85: this station's own locus name (hidden from hosts)
_TS_LOCI_POLL = int(os.environ.get("STATION_CONSOLE_LOCI_POLL", "20") or 20)


def _ts_headers():
    tok = (os.environ.get("STATION_CONSOLE_TOOLSERVER_TOKEN")
           or os.environ.get("HUGPY_OPERATOR_TOKEN") or "").strip()
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if tok:
        h["X-Operator-Token"] = tok
    return h


async def _ts_call(app, path, body=None, timeout=8):
    """POST a toolserver tool ({'result': ...} unwrapped; {'error'} raised)."""
    sess = app.get("proxy_sess")
    if sess is None:
        sess = app["proxy_sess"] = aiohttp.ClientSession()
    url = TS_UPSTREAM.rstrip("/") + "/" + path.lstrip("/")
    async with sess.post(url, json=(body or {}), headers=_ts_headers(),
                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        data = await r.json(content_type=None)
    if isinstance(data, dict) and "error" in data and "result" not in data:
        raise RuntimeError(str(data["error"])[:200])
    return data.get("result", data) if isinstance(data, dict) else data


async def _loci_sync_once(app):
    try:
        feed = await _ts_call(app, "loci/pointers", {})
        # LXD guests ONLY — known_names() also returns the ssh loci (its
        # 2026-09-03 lxc-failure fallback), which made every other sync skip
        # the whole feed as "guests" and blink the loci in and out.
        try:
            lxd = {s["name"] for s in await discover()}
        except Exception:
            lxd = _TS_LOCI["lxd"]
        hosts = {}
        # 1.0.85: one machine, one locus (NOMENCLATURE rule): the desktop's own
        # locus is registered with an endpoint so peers see it, but it must not
        # appear in its own dropdown as an ssh host. Only its NAME is kept
        # (_TS_LOCI["self"]) so _station_locus() still resolves without
        # STATION_LOCUS set. "Self" is the explicit STATION_LOCUS name (so a
        # drifted endpoint IP — DHCP, second NIC, WAN — cannot make this
        # station reappear in its own dropdown) OR <this user>@<one of this
        # host's ips> on the plain ssh port — a guest reached by a port-forward
        # on our own IP (op@192.168.1.113:2222) is a different machine.
        me, my_ips, self_rows = getpass.getuser(), _local_ips_cached(), {}
        env_locus = _canon_locus(os.environ.get("STATION_LOCUS") or "")
        for n, h in (feed.get("ssh_hosts") or {}).items():
            if not _SSH_NAME_RE.match(n) or n in lxd or not isinstance(h, dict):
                continue
            host = str(h.get("host") or ""); user = str(h.get("user") or "root")
            if not _SSH_HOST_RE.match(host) or not _SSH_USER_RE.match(user):
                continue
            try:
                port = int(h.get("port") or 22)
            except (TypeError, ValueError):
                port = 22
            is_self = (n == env_locus) or (user == me and host in my_ips and port == 22)
            if is_self:                            # 1.0.85: this station itself
                self_rows[n] = h
                continue
            key = str(h.get("key") or "")
            hosts[n] = {"host": host, "user": user, "port": port,
                        "key": key if key and os.path.isfile(os.path.expanduser(key)) else "",
                        "added": "", "source": "toolserver", "goal": str(h.get("goal") or ""),
                        "kind": str(h.get("kind") or "")}
        # 1.0.141: several rows on this station's own endpoint (keeper + ae-mgr)
        # resolve to ONE name deterministically, never dict order.
        self_locus = env_locus or _pick_endpoint_locus(list(self_rows), self_rows)
        _TS_LOCI.update(hosts=hosts, sessions=list(feed.get("sessions") or []),
                        ts=int(time.time()), error="", lxd=lxd, self=self_locus)   # 1.0.85: self
    except Exception as e:
        _TS_LOCI["error"] = str(e)[:200]
    _TS_LOCI["synced"] = True    # 1.0.138: first attempt done (see _loci_ready)
    for tgt in _delivery_targets("ping"):
        try:
            if tgt["host"]:
                await _deliver_pings(app)
                await _nudge_frontier(app, _station_locus() or "")
            else:
                await _deliver_pings_locus(app, tgt)
        except Exception as e:  # noqa: BLE001
            _rlog.warning("ping delivery %s: %s", tgt.get("locus"), e)


# ── 1.0.69: comms delivery — board pings reach THIS station's seat ─────────────
# Plan §3: the toolserver board is the durable channel; the station's loci-sync
# is the delivery leg. Every sync: comms/inbox for this station's locus → new
# pings become keeper-mail rows (✉ badge) AND, when the frontier claude-code seat
# is idle at its prompt, one nudge line is typed into it so the keeper actually
# looks. Never while the seat is working. Delivered ids are remembered.
_PINGS_SEEN_PATH = FV_STATE_HOME / "comms-delivered.json"
_NUDGE_PENDING_PATH = FV_STATE_HOME / "comms-nudge-pending.json"


def _pings_seen(path=None):
    doc = _read_json(path or _PINGS_SEEN_PATH, {}) or {}
    return set(doc.get("ids") or [])


def _pings_seen_write(ids, path=None):
    path = path or _PINGS_SEEN_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ids": sorted(ids)[-2000:]}), encoding="utf-8")
    except OSError:
        pass


# ── 1.0.144: delivery to EVERY locus's keeper serve session ─────────────────────
# Operator 2026-10-01: "being the station's host should have NO bearing on having a
# serve chat and a keeper". Until 1.0.143 the comms leg (comms/inbox -> 📨 nudge into
# the keeper's serve session) ran for THIS station's own locus only, against
# AC_UPSTREAM, so a ping to any other locus (hugpy: r4165) reached no session at all.
# Now the same leg runs per locus: the host (its own files, as before) plus every
# locus whose serve this station provisioned (serve-managed.json) or that
# STATION_DELIVERY_LOCI names — through that locus's own serve (_ac_resolve: ssh -L
# forward / lxd bridge). A remote locus's pings are CLAIMED centrally (comms/claim)
# first, so a locus that also runs its own station is never nudged twice, and marked
# comms/delivered once the nudge landed. Per-locus state: <state>/loci/<locus>/.
SERVE_MANAGED_PATH = FV_STATE_HOME / "serve-managed.json"


def _serve_managed():
    doc = _read_json(SERVE_MANAGED_PATH, {}) or {}
    return {str(k): v for k, v in doc.items() if isinstance(v, dict) and _SSH_NAME_RE.match(str(k))}


def _serve_managed_put(vm, rec):
    doc = _serve_managed()
    doc[vm] = dict(doc.get(vm) or {}, **rec)
    _write_json_atomic(SERVE_MANAGED_PATH, doc)


def _delivery_targets(kind=""):
    """[{vm, locus, host}] — the loci whose keeper serve session THIS station
    delivers to (kind 'ping': comms pings, 'digest': the open-todos digest).
    STATION_<KIND>_LOCI, else STATION_DELIVERY_LOCI (comma list; '@self' = this
    station's own locus) replaces the default: the host locus (not in a
    STATION_DEV_QUIET copy) + every managed locus."""
    own = _station_locus()
    env = os.environ.get("STATION_%s_LOCI" % kind.upper()) if kind else None
    if env is None:
        env = os.environ.get("STATION_DELIVERY_LOCI")
    if env is not None:
        names = [n.strip() for n in env.split(",") if n.strip()]
    else:
        names = ([] if os.environ.get("STATION_DEV_QUIET") == "1" else ["@self"]) + sorted(_serve_managed())
    out, seen = [], set()
    for n in names:
        if _is_host_vm(n):
            if own and own not in seen:
                seen.add(own)
                out.append({"vm": "", "locus": own, "host": True})
            continue
        loc = _central_locus(n) or n
        if loc and loc not in seen:
            seen.add(loc)
            out.append({"vm": n, "locus": loc, "host": False})
    return out


def _locus_state_dir(locus):
    return FV_STATE_HOME / "loci" / re.sub(r"[^a-z0-9._-]+", "-", (locus or "").lower())


def _nudge_paths(target):
    """(seen, pending) files of one delivery target (the host keeps its own)."""
    if target.get("host"):
        return _PINGS_SEEN_PATH, _NUDGE_PENDING_PATH
    d = _locus_state_dir(target["locus"])
    return d / "comms-delivered.json", d / "comms-nudge-pending.json"


async def _live_frontier_session():
    """The tmux session (on the keeper socket) holding a live Claude seat: the
    canonical keeper-claude when it exists, else ANY session on that socket whose
    active pane runs claude — a keeper launched outside this station (hugpy on
    ae, 2026-09-03: abstract-claude) must still get its board nudge. '' = none."""
    want = _tmux_session_for("frontier", "claude-code") or "keeper-claude"
    rc, _o, _e = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "has-session", "-t", "=" + want)
    if rc == 0:
        return want
    rc, out, _e = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "list-panes", "-a", "-F",
                             "#{session_name}\t#{pane_active}\t#{pane_current_command}")
    if rc != 0:
        return ""
    for ln in (out or "").splitlines():
        parts = ln.split("\t")
        if len(parts) == 3 and parts[1] == "1" and parts[2] in ("claude", "node"):
            return parts[0]
    return ""


async def _deliver_pings(app):
    locus = _station_locus()
    if not locus:
        return
    doc = await _ts_call(app, "comms/inbox", {"to": locus}, timeout=10)
    pings = [p for p in (doc.get("pings") or []) if isinstance(p, dict) and p.get("id")]
    if not pings:
        return
    seen = _pings_seen()
    new = [p for p in pings if str(p["id"]) not in seen]
    if not new:
        return
    # 2026-10-01 (operator): a BUG REPORT never reaches the keeper — an older
    # station still posts findings / loops / B proposals as [ping] requests; they
    # are re-routed to the worker (bug_route), not mailed, not nudged.
    bugs = [p for p in new if _bugr is not None and _bugr.is_bug_ping(p)]
    bug_ids = {str(p["id"]) for p in bugs}
    mail = [p for p in new if str(p["id"]) not in bug_ids]
    rows = _keeper_mail_rows()
    for p in mail:
        frm = re.sub(r"^\[ping\](?:\[msg\])?\s*(?:from\s+)?", "", str(p.get("note") or "")).split(" ")[0] or "board"
        rows.append(_mail_row(frm[:24], "keeper", f"[board {p['id']}] {str(p.get('text') or '')[:2000]}", unread=True))
    if mail:
        _keeper_mail_write(rows)
    # mail is delivered; remember the ids so a sync never re-appends them.
    seen.update(str(p["id"]) for p in new)
    _pings_seen_write(seen)
    if bugs:
        await _reroute_bug_pings(app, bugs)
    # A conversational [ping][msg] (fleet ✉ compose) is now in the ✉ inbox above and
    # needs no board footprint — CLOSE it so it never sits in the WORK queue. A real
    # request-ping has no [msg] marker and stays open as its durable backstop.
    for p in mail:
        if "[msg]" in str(p.get("note") or "").lower():
            try:
                await _ts_call(app, "todo/done", {"id": p["id"]}, timeout=10)
            except Exception as e:  # noqa: BLE001 — toolserver down: leave it open
                _rlog.warning("close msg ping %s: %s", p.get("id"), e)
    # the nudge is SEPARATE and RETRIED: a busy seat (mid-turn), a missing pane or a
    # non-empty input line only defers it to the next sync — the keeper must
    # eventually see one 📨 line (2026-09-03: hugpy's keeper was mid-turn when its
    # first pings arrived and would never have been told).
    # 2026-10-01 (t4178): EVERY request ping is nudged — no gate, no B; the flood
    # guards are the batch window + one hand-off in flight (_nudge_frontier).
    nudge = await _gate_pings(app, mail, locus=locus)
    pend = _nudge_pending()
    pend.extend(p for p in nudge if str(p["id"]) not in {str(q["id"]) for q in pend})
    _nudge_pending_write(pend)
    await _nudge_frontier(app, locus)


async def _reroute_bug_pings(app, bugs):
    """Legacy bug-report pings -> the bug outbox (worker of the origin locus when
    that locus is known here, else this station's own worker); the keeper's
    [ping] request is closed with a note saying where it went (never silent)."""
    box = _bug_outbox()
    if box is None:
        return
    hosts = set((_TS_LOCI.get("hosts") or {}).keys()) | set(_TS_LOCI.get("lxd") or [])
    for p in bugs:
        origin = _bugr.ping_origin(p)
        vm = origin if origin and origin in hosts else ""
        text = str(p.get("text") or "")
        first = (text.strip().splitlines() or [""])[0]
        box.offer("ping:" + str(p["id"]), vm=vm, locus=_bug_locus(vm), kind="rerouted-ping",
                  title=first[:400], body=text[:4000] + "\n\n(re-routed from keeper board %s%s)"
                  % (p["id"], (", origin " + origin) if origin else ""),
                  severity="medium", count=1, source=_ping_sender(p))
        try:
            await _ts_call(app, "todo/update", {"id": p["id"], "status": "done",
                                                "note": str(p.get("note") or "") + "\n\nre-routed by the station "
                                                "(operator 2026-10-01): bug reports go to the WORKER session of "
                                                "locus %s, not the keeper — see its `finding` board item."
                                                % (_bug_locus(vm) or vm or "this station")}, timeout=10)
        except Exception as e:                           # noqa: BLE001 — still in the outbox; board item stays open
            _rlog.warning("reroute bug ping %s: %s", p.get("id"), e)
        _audit_line("comms-bug-reroute", "%s → worker of %s" % (p.get("id"), vm or "host"))
    box.save()
    await _bug_pump(app)


def _ping_sender(p):
    return re.sub(r"^\[ping\](?:\[msg\])?\s*(?:from\s+)?", "", str(p.get("note") or "")).split(" ")[0] or "board"


async def _gate_pings(app, pings, include_msg=False, locus=None):
    """Which new comms pings nudge the keeper: EVERY request ping (a
    conversational [ping][msg] is filed in ✉ and closed instead, unless
    include_msg). 2026-10-01 (no silent suppression, d4187 / t4178): the 1.0.140
    issue-gate decision — nudge only on page=true, fail closed when the toolserver
    is down — is gone; the gate is consulted for ANNOTATION only (issue_fp rides
    along when known) and can never withhold a ping. Bug reports never get here
    (_deliver_pings re-routes them to the worker). 1.0.144: a ping delivered to
    ANOTHER locus is observed as `sender->receiver` so the gate's self-origin
    globs do not file a keeper -> hugpy delegation as that locus's own run."""
    gate = _issue_gate(app)
    out = []
    for p in pings or []:
        if "[msg]" in str(p.get("note") or "").lower() and not include_msg:
            continue
        pid = str(p.get("id"))
        fp = _ig.fp_of(p) if gate is not None else None
        if fp:
            out.append(dict(p, issue_fp=fp))
            continue
        if gate is not None:
            try:
                frm = _ping_sender(p)
                chan = _is_channel_ping(p)
                ref = str(p.get("ref") or "")
                if chan and not ref.startswith("ch_"):
                    m = _CHANNEL_REF_RE.search(str(p.get("note") or ""))
                    ref = m.group(1) if m else ref
                text = str(p.get("text") or "")
                caller = frm if (not locus or _canon_locus(frm) == locus) else "%s->%s" % (frm, locus)
                res = await gate.observe(source=("channel:%s" % ref) if chan else ("comms:%s" % frm),
                                         kind="channel" if chan else "comms",
                                         subject=_ig.ping_subject(p) or text[:200], text=text[:600], severity=1,
                                         caller=caller, meta={"ping": pid, "ref": ref, "from": frm},
                                         locus=locus)
                if res and res.get("fp"):
                    p = dict(p, issue_fp=res["fp"])
                    if not res.get("page"):
                        _audit_line("comms-gate", "%s: %s %s — annotated, still nudged (no suppression)"
                                    % (pid, res.get("fp"), res.get("reason")))
            except Exception as e:                       # noqa: BLE001 — annotation only; never blocks delivery
                _audit_line("comms-gate", "%s: gate observe failed (%s) — nudged anyway" % (pid, e))
        out.append(p)
    return out


def _nudge_pending(path=None):
    doc = _read_json(path or _NUDGE_PENDING_PATH, {}) or {}
    return [p for p in (doc.get("pings") or []) if isinstance(p, dict) and p.get("id")]


_KEEP = object()


def _nudge_pending_write(pings, last_sent=None, inflight=_KEEP, path=None):
    """pings + last_sent + (1.0.140) the in-flight serve hand-off being watched
    until it drains (``inflight`` = {sid, message_ids, items, at}; kept unless
    given; None clears it)."""
    _NUDGE_PENDING_PATH = path or globals()["_NUDGE_PENDING_PATH"]
    doc = _read_json(_NUDGE_PENDING_PATH, {}) or {}
    if last_sent is None:
        last_sent = doc.get("last_sent") or 0
    if inflight is _KEEP:
        inflight = doc.get("inflight")
    body = {"pings": pings[-50:], "last_sent": last_sent}
    if inflight:
        body["inflight"] = inflight
    try:
        _NUDGE_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        _NUDGE_PENDING_PATH.write_text(json.dumps(body), encoding="utf-8")
    except OSError:
        pass


try:
    import keeper_nudge as _knudge
except ImportError:                                  # pragma: no cover — packaging error
    _knudge = None

# 1.0.125 (operator): the 📨 nudge goes to the keeper's session IN SERVE, always;
# the legacy tmux pane is only the fallback when serve is unreachable/unhealthy.
NUDGE_SERVE_ROLE = (os.environ.get("STATION_NUDGE_SERVE_ROLE") or "keeper").strip().lower()
NUDGE_MIN_INTERVAL = max(0, int(os.environ.get("STATION_NUDGE_MIN_INTERVAL") or "300"))
# 1.0.138 channel push for any harness: a comms ping whose ref is a CHANNEL id
# (`ch_…` — toolserver channel_bind pings the bound locus with comms_ping
# ref=<channel>, kind=request) is live conversation, not board mail: it skips the
# NUDGE_MIN_INTERVAL batch window and is pushed at once through the same serve-
# first / tmux-fallback nudge. comms_ping stores the ref in the note "(ref <ref>)".
_CHANNEL_REF_RE = re.compile(r"\(ref (ch_[A-Za-z0-9_.:-]+)")


def _is_channel_ping(p):
    if not isinstance(p, dict):
        return False
    if str(p.get("ref") or "").startswith("ch_"):
        return True
    return bool(_CHANNEL_REF_RE.search(str(p.get("note") or "")))


async def _nudge_serve(app, line, base=None):
    """Hand the nudge to serve's standing keeper session: POST
    /api/session/<role>/message (wait=false, lossless). Serve runs it at once
    when idle and queues it for the turn boundary when busy — never interrupts.
    base (1.0.144) = the target locus's serve (default: this host's)."""
    sess = app.get("proxy_sess")
    if sess is None:
        sess = app["proxy_sess"] = aiohttp.ClientSession()
    base = (base or AC_UPSTREAM).rstrip("/")
    to = aiohttp.ClientTimeout(total=8)
    app["_rt"]["nudge_last_submit"] = None
    # 1.0.140: the keeper SESSION, resolved fresh on EVERY send (roster role ->
    # rollover chain head) — it changes across rollover, restart and wipe.
    sid, why = await _keeper_console_head(app, base)
    if not sid:
        return False, why
    if sid.startswith("cs-"):
        # 1.0.126: a CONSOLE session (claude/gpt/hugpy via console_service) is fed
        # through its own submit — POST /api/console/chat runs it now when idle and
        # queues it (console queue panel) when busy. serve's /api/session/<role>/message
        # relay only resumes NATIVE claude sessions: on a cs- id it accepted (202)
        # and then failed with "--resume requires a valid session ID" (1.0.125).
        async with sess.post(base + "/api/console/chat", json={"session_id": sid, "prompt": line},
                             timeout=to) as r:
            res = await r.json(content_type=None) if r.content_type and "json" in r.content_type else {}
            if r.status == 200 and (res or {}).get("message_ids"):
                # watched until it drains (_nudge_drain): an auto=off session does not
                # pick a queued message up at the turn boundary by itself
                app["_rt"]["nudge_last_submit"] = {"sid": str(res.get("session_id") or sid),
                                                   "message_ids": [str(m) for m in res["message_ids"]],
                                                   "queued": bool(res.get("queued"))}
                return True, "serve:%s %s via %s (%s)" % (NUDGE_SERVE_ROLE, sid, why,
                                                          "queued" if res.get("queued") else "sent")
            return False, "serve console chat HTTP %d %s" % (r.status, str((res or {}).get("error") or "")[:120])
    body = {"text": line, "from": "station", "by": "station-relay", "wait": False, "via_b": False}
    async with sess.post(base + "/api/session/%s/message" % NUDGE_SERVE_ROLE, json=body, timeout=to) as r:
        res = await r.json(content_type=None) if r.content_type and "json" in r.content_type else {}
        if not (r.status in (200, 202) or (r.status == 409 and (res or {}).get("queued"))):
            return False, "serve message HTTP %d %s" % (r.status, str((res or {}).get("error") or "")[:120])
    how = "queued" if r.status == 409 else ("accepted" if r.status == 202 else "delivered")
    rid = str((res or {}).get("id") or "")
    if r.status == 202 and rid:
        # wait=false hides the outcome: read serve's relay log for this id so an
        # accepted-then-failed hand-off falls back instead of being lost
        await asyncio.sleep(4)
        try:
            async with sess.get(base + "/api/session/relay?limit=30", timeout=to) as rr:
                ents = (await rr.json(content_type=None) or {}).get("entries") or []
            ent = next((e for e in ents if str(e.get("id")) == rid), None)
            if ent and ent.get("b_status") == "error" and not ent.get("queued"):
                return False, "serve relay failed: %s" % str(ent.get("reason") or "")[:140]
        except Exception:                                # noqa: BLE001 — no log = still running
            pass
    return True, "serve:%s %s (%s)" % (NUDGE_SERVE_ROLE, sid, how)


async def _keeper_console_head(app, base=None):
    """(sid, why): the keeper head as its CONSOLE session. 1.0.144: a NATIVE
    keeper id (a roster-started keeper on a freshly provisioned locus) is
    resolved to the console session that wraps it — adopted the way serve's own
    console adopts it (an existing one is returned) — so every prompt to that
    transcript rides ONE queue (console chat / prompt_send / nudge), and the
    drain compares like with like (a native head vs a cs- in-flight id read as
    "rolled over" and re-queued the nudge every tick)."""
    sid, why = await (_keeper_serve_session(app, base) if base else _keeper_serve_session(app))
    if sid and not sid.startswith("cs-"):
        try:
            st, ad = await (_serve_post(app, "/api/console/sessions", {"backend": "claude", "native_id": sid},
                                        base=base) if base else
                            _serve_post(app, "/api/console/sessions", {"backend": "claude", "native_id": sid}))
            if st == 200 and isinstance(ad, dict) and str(ad.get("id") or "").startswith("cs-"):
                sid, why = str(ad["id"]), why + " (console)"
        except Exception:                                # noqa: BLE001 — older serve: the native id
            pass
    return sid, why


async def _serve_get(app, path, timeout=8, base=None):
    """GET a serve JSON route -> (status, doc). base = a locus's serve (1.0.144)."""
    sess = app.get("proxy_sess")
    if sess is None:
        sess = app["proxy_sess"] = aiohttp.ClientSession()
    async with sess.get((base or AC_UPSTREAM).rstrip("/") + path, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        doc = await r.json(content_type=None) if r.content_type and "json" in r.content_type else None
        return r.status, doc


async def _serve_post(app, path, body, timeout=8, base=None):
    sess = app.get("proxy_sess")
    if sess is None:
        sess = app["proxy_sess"] = aiohttp.ClientSession()
    async with sess.post((base or AC_UPSTREAM).rstrip("/") + path, json=body,
                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        doc = await r.json(content_type=None) if r.content_type and "json" in r.content_type else None
        return r.status, doc


async def _keeper_serve_session(app, base=None):
    """(sid, why) of the keeper's live serve session: the roster's role binding
    followed down serve's rollover chain. Never cached across sends."""
    st, roster = await _serve_get(app, "/api/session/roster", base=base)
    if st != 200:
        return "", "serve roster HTTP %d" % st
    try:
        _st2, roll = await _serve_get(app, "/api/session/rollover", base=base)
    except Exception:                                    # noqa: BLE001 — no chain info: the roster wins
        roll = None
    return _knudge.resolve_keeper_session(roster, roll if isinstance(roll, dict) else None, NUDGE_SERVE_ROLE)


async def _nudge_drain(app, base=None, path=None):
    """1.0.140: follow the last serve hand-off until it DRAINS. -> True while it
    is still in flight (no new nudge is sent on top of it). A message queued
    behind a turn on an auto=off session is kicked (POST /api/console/queue
    action=retry) once the session is idle; one stranded on a session that is
    no longer the keeper head (rollover / wipe), a gone session, or behind
    someone else's hold is pulled out and its pings re-pended for the head."""
    doc = _read_json(path or _NUDGE_PENDING_PATH, {}) or {}
    w = doc.get("inflight")
    if not w or _knudge is None:
        return False
    sid = str(w.get("sid") or "")
    kb = {"base": base} if base else {}                  # 1.0.144: a remote locus's serve
    try:
        head, _why = await _keeper_console_head(app, base)
        st, q = await _serve_get(app, "/api/console/queue?id=" + sid, **kb)
        q = q if (st == 200 and isinstance(q, dict) and "items" in q) else None
    except Exception as e:                               # noqa: BLE001 — serve down: keep watching
        _rlog.warning("nudge drain %s: %s", sid, e)
        return True
    now = time.time()
    act = _knudge.drain_action(w, q, head, now)
    hkey = "hold:comms-nudge-inflight@" + (base or "host")
    if act == "done":
        _nudge_pending_write(_nudge_pending(path), inflight=None, path=path)
        _audit_line("comms-nudge-drained", "%s %s" % (sid, ",".join(w.get("message_ids") or [])))
        _hold_clear(hkey)
        return False
    if act == "wait":
        # 1.0.146: a bounded wait, visible on the strip while it lasts
        waited = now - float(w.get("at") or now)
        hold = (q or {}).get("hold") if isinstance((q or {}).get("hold"), dict) else None
        _hold_note(hkey, _sh.SOURCE_HOLD if _sh else "station:hold",
                   "📨 nudge queued behind %s" % (("a hold by %s" % hold.get("by")) if hold else "a busy turn"),
                   "%d ping(s) handed to keeper session %s %s ago; re-pended after %d min"
                   % (len(w.get("items") or []), sid[:12], _sh.fmt_age(waited) if _sh else "%ds" % waited,
                      _knudge.WAIT_MAX_S // 60), "nothing to do — it goes out at the turn boundary",
                   count=len(w.get("items") or []))
        return True
    if act == "kick":
        try:
            st, r = await _serve_queue_release(lambda m, p, b: _serve_post(app, p, b, **kb), sid, by="station-nudge")
            _audit_line("comms-nudge-kick", "%s queued nudge started (release HTTP %d%s)"
                        % (sid, st, "" if _knudge.queue_action_ok(st, r) else " — not accepted"))
        except Exception as e:                           # noqa: BLE001
            _rlog.warning("nudge kick %s: %s", sid, e)
        return True
    # requeue: take ours out of that queue, hand the pings back for the head
    for mid in w.get("message_ids") or []:
        try:
            if q is not None:
                await _serve_post(app, "/api/console/queue", {"session_id": sid, "action": "remove", "id": mid}, **kb)
        except Exception:                                # noqa: BLE001
            pass
    pend = _nudge_pending(path)
    have = {str(p.get("id")) for p in pend}
    pend = [p for p in (w.get("items") or []) if str(p.get("id")) not in have] + pend
    _nudge_pending_write(pend, last_sent=0, inflight=None, path=path)
    expired = _knudge.wait_expired(w, now) and q is not None and (q.get("busy") or q.get("paused"))
    why = ("waited %d min behind a busy turn (bound %d min)" % ((now - float(w.get("at") or now)) // 60,
                                                             _knudge.WAIT_MAX_S // 60)
           if expired else "session is not the keeper head / gone / holds other originals")
    _audit_line("comms-nudge-requeue", "%s → head %s: %d ping(s) re-pended — %s"
                % (sid, head or "?", len(w.get("items") or []), why))
    _hold_clear(hkey)
    _hold_note("count:comms-nudge-requeue@" + (base or "host"), _sh.SOURCE_HOLD if _sh else "station:hold",
               "📨 nudge re-pended (wait bound / head moved)", why,
               "re-sent on the next tick to the live keeper head", inc=True)
    return False


async def _nudge_tmux(app, line):
    """The legacy path, unchanged: type the line into the tmux keeper pane, only
    when the seat is idle at an EMPTY prompt."""
    sess = await _live_frontier_session()
    if not sess:
        return False, "no live claude pane on the keeper socket"
    try:
        rc, out, _ = await _locus_run_fast("", _CACHE_PROBE, 15)
        cd = json.loads((out or "").strip().splitlines()[-1]) if rc == 0 else {}
    except Exception:
        cd = {}
    # 1.0.85: a FRESH seat (no transcript / no request yet) is idle, not "failed"
    if not cd.get("ok") and cd.get("error") not in ("no transcript", "no requests yet"):
        return False, f"seat probe failed ({sess})"
    if cd.get("busy"):
        return False, f"seat busy ({sess})"
    # target-PANE form: "=name:" = that exact session, its active pane
    rc, pane, _ = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "capture-pane", "-p", "-t", "=" + sess + ":", "-S", "-6")
    last_prompt = [ln for ln in (pane or "").splitlines() if ln.lstrip().startswith("❯")]
    if rc != 0:
        return False, f"capture-pane rc={rc} ({sess})"
    if not last_prompt or last_prompt[-1].strip() != "❯" or "esc to interrupt" in pane:
        return False, f"prompt not empty/idle ({sess})"
    tk = ["tmux", "-L", KEEPER_TMUX_SOCK, "send-keys", "-t", "=" + sess + ":"]
    await _run(*tk, "Escape"); await asyncio.sleep(0.3)
    await _run(*tk, "C-u"); await asyncio.sleep(0.3)
    await _run(*tk, "-l", line); await asyncio.sleep(1.0)
    await _run(*tk, "Enter")
    return True, f"tmux:{sess}"


async def _nudge_frontier(app, locus, base=None, path=None, remote=False):
    """ONE 📨 line for everything pending — at most every NUDGE_MIN_INTERVAL,
    only items STILL OPEN at send time (re-read from the board), deduped by
    (source, signature) across stations; serve first, tmux only as fallback.
    1.0.144: base/path = a remote locus's serve + its pending file; a remote
    locus never gets a tmux nudge, and its delivered pings are marked
    comms/delivered centrally."""
    if _knudge is None:
        return
    if await _nudge_drain(app, base, path):              # 1.0.140: one hand-off in flight at a time
        return
    pend = _nudge_pending(path)
    if not pend:
        return
    skip_key = "comms_nudge_skip_reason" + ("@" + locus if remote else "")
    hkey = "skip:comms-nudge@" + locus
    ts_err = ""
    try:
        res = await _ts_call(app, "todo/list", {"status": "open", "locus": locus, "limit": 500}, timeout=10)
        open_ids = {str(r.get("id")) for r in (res or []) if isinstance(r, dict)}
    except Exception as e:                               # noqa: BLE001 — cannot verify: nothing is sent
        open_ids = None
        ts_err = str(e)[:120]
    doc = _read_json(path or _NUDGE_PENDING_PATH, {}) or {}
    # a pending CHANNEL ping (ref ch_…) is pushed now — no batch window
    interval = 0 if any(_is_channel_ping(p) for p in pend) else NUDGE_MIN_INTERVAL
    pl = _knudge.plan(pend, open_ids, time.time(), float(doc.get("last_sent") or 0), interval)
    if pl["dropped"]:
        _nudge_pending_write([p for p in pend if str(p["id"]) not in set(pl["dropped"])],
                             last_sent=doc.get("last_sent"), path=path)
        _audit_line("comms-nudge-drop", "closed since arrival: " + ", ".join(pl["dropped"][:20]))
    if not pl["send"]:
        if pl.get("why") == "board status unknown":
            # 1.0.146 (t4260): never a silent no-send — audit (once per reason) + strip row
            detail = "toolserver unreachable (%s): %d ping(s) pending, open set unverifiable" % (ts_err, len(pend))
            if app["_rt"].get(skip_key) != detail:
                app["_rt"][skip_key] = detail
                _audit_line("comms-nudge-skip", f"{locus}: {detail}")
            _hold_note(hkey, _sh.SOURCE_SKIP if _sh else "station:skip", "📨 nudge for %s not sent" % locus,
                       detail, "check the toolserver (/healthz); the pings stay pending and go out when it answers",
                       count=len(pend))
        elif pl.get("why") == "batch window" and pl["items"]:
            # S12: the 5-min batch window is a coalesce, shown with its countdown
            _hold_note("hold:comms-nudge-window@" + locus, _sh.SOURCE_HOLD if _sh else "station:hold",
                       "📨 %d ping(s) for %s batched" % (len(pl["items"]), locus),
                       "next nudge in %ds (NUDGE_MIN_INTERVAL %ds)" % (pl.get("wait_s") or 0, interval),
                       "nothing to do — coalesced into one line", count=len(pl["items"]))
        else:
            _hold_clear("hold:comms-nudge-window@" + locus)
        return
    _hold_clear("hold:comms-nudge-window@" + locus)
    line = _knudge.summarize(pl["items"], locus)
    if remote:
        out = await _knudge.deliver(line, lambda ln: _nudge_serve(app, ln, base),
                                    lambda ln: _no_tmux(ln), allow_tmux=False)
    else:
        out = await _knudge.deliver(line, lambda ln: _nudge_serve(app, ln), lambda ln: _nudge_tmux(app, ln))
    if out["target"] == "none":
        if app["_rt"].get(skip_key) != out["detail"]:
            app["_rt"][skip_key] = out["detail"]
            _audit_line("comms-nudge-skip", f"{locus}: {len(pl['items'])} pending; {out['detail']}")
        # 1.0.146 (t4260): the skip reason + pending count on the ⚠ strip, not only audit.log
        _hold_note(hkey, _sh.SOURCE_SKIP if _sh else "station:skip", "📨 nudge for %s not sent" % locus,
                   "%d pending; %s" % (len(pl["items"]), out["detail"]),
                   "bring the keeper serve session up (/api/locus/serve/provision) — the pings stay pending",
                   count=len(pl["items"]))
        return
    app["_rt"][skip_key] = ""
    _hold_clear(hkey)
    sub = app["_rt"].get("nudge_last_submit") if out["target"] == "serve" else None
    inflight = ({"sid": sub["sid"], "message_ids": sub["message_ids"], "items": pl["items"],
                 "at": int(time.time())} if sub else None)
    _nudge_pending_write([], last_sent=time.time(), inflight=inflight, path=path)
    _audit_line("comms-nudge", f"{locus}: {len(pl['items'])} ping(s) → {out['target']} [{out['detail']}]; "
                               f"latest {pl['items'][-1]['id']}")
    if remote:
        for p in pl["items"]:
            try:
                await _ts_call(app, "comms/delivered", {"id": str(p["id"]), "by": _station_id()}, timeout=10)
            except Exception as e:                       # noqa: BLE001 — older toolserver: no ledger
                _rlog.warning("comms/delivered %s: %s", p.get("id"), e)


async def _no_tmux(_line):
    return False, "remote locus: tmux nudges are off"


async def _deliver_pings_locus(app, target):
    """1.0.144: the comms leg for ANOTHER locus (target = _delivery_targets()
    row): comms/inbox to=<locus> -> claim each new ping centrally -> the issue
    gate -> ONE 📨 line into that locus's keeper serve session. Conversational
    [ping][msg] rows have no ✉ tab on a remote locus, so they ride the nudge
    (gated like the rest) and stay open for that keeper to close."""
    locus, vm = target["locus"], target["vm"]
    seen_p, pend_p = _nudge_paths(target)
    doc = await _ts_call(app, "comms/inbox", {"to": locus}, timeout=10)
    pings = [p for p in (doc.get("pings") or []) if isinstance(p, dict) and p.get("id")]
    seen = _pings_seen(seen_p)
    new = [p for p in pings if str(p["id"]) not in seen]
    won = []
    for p in new:
        try:
            c = await _ts_call(app, "comms/claim", {"id": str(p["id"]), "by": _station_id(),
                                                    "lease_s": 3600}, timeout=10)
        except Exception as e:                           # noqa: BLE001 — no claim ledger: deliver
            c = {"won": True, "reason": str(e)[:80]}
        c = c or {}
        if c.get("won") or (str(c.get("claimed_by") or "") == _station_id() and not c.get("delivered_by")):
            won.append(p)                                # ours (or our own earlier lease)
        else:
            _audit_line("comms-claim-lost", "%s %s: %s" % (locus, p["id"], (c or {}).get("claimed_by")
                                                          or (c or {}).get("delivered_by") or (c or {}).get("reason")))
    if new:
        seen.update(str(p["id"]) for p in new)
        _pings_seen_write(seen, seen_p)
    if won:
        nudge = await _gate_pings(app, won, include_msg=True, locus=locus)
        pend = _nudge_pending(pend_p)
        have = {str(q["id"]) for q in pend}
        pend.extend(p for p in nudge if str(p["id"]) not in have)
        _nudge_pending_write(pend, path=pend_p)
    if not _nudge_pending(pend_p) and not (_read_json(pend_p, {}) or {}).get("inflight"):
        return
    url = (await _ac_resolve(vm))[0]
    if not url:
        err = (_AC_DISCOVERED.get(_ac_locus_key(vm)) or {}).get("error") or "not discovered"
        _audit_line("comms-nudge-skip", "%s: no reachable serve (%s)" % (locus, err))
        _hold_note("skip:comms-nudge@" + locus, _sh.SOURCE_SKIP if _sh else "station:skip",
                   "📨 nudge for %s not sent" % locus, "%d pending; no reachable serve (%s)"
                   % (len(_nudge_pending(pend_p)), err),
                   "provision / start that locus's serve — the pings stay pending", count=len(_nudge_pending(pend_p)))
        return
    await _nudge_frontier(app, locus, base=url, path=pend_p, remote=True)


# ── live change bus (1.0.77): ONE subscription to the toolserver's SSE stream
# (Postgres LISTEN/NOTIFY on every per-locus table), fanned out to every open
# browser tab over GET /api/events. Drawers reload on a matching event instead
# of polling; the stream is a hint, never the data.
_evlog = logging.getLogger("station.events")


async def _events_relay_loop(app):
    app.setdefault("event_subs", set())
    backoff = 1
    while True:
        try:
            sess = app.get("proxy_sess")
            if sess is None:
                sess = app["proxy_sess"] = aiohttp.ClientSession()
            url = TS_UPSTREAM.rstrip("/") + "/events/stream"
            async with sess.get(url, headers=_ts_headers(),
                                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=90)) as r:
                if r.status != 200:
                    raise RuntimeError(f"events/stream HTTP {r.status}")
                backoff = 1
                app["_rt"]["events_connected"] = time.time()
                async for raw in r.content:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        doc = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    app["_rt"]["events_last"] = doc
                    for q in list(app["event_subs"]):
                        try:
                            q.put_nowait(doc)
                        except asyncio.QueueFull:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            app["_rt"]["events_connected"] = 0
            _evlog.warning("events relay: %s (retry in %ss)", e, backoff)
        await asyncio.sleep(backoff)
        backoff = min(30, backoff * 2)


async def _start_events_relay(app):
    app["events_relay"] = asyncio.create_task(_events_relay_loop(app))


async def _stop_events_relay(app):
    t = app.get("events_relay")
    if t:
        t.cancel()


async def api_events(request):
    """GET /api/events — Server-Sent Events: one `change` event per DB change
    {table, op, locus, id, ts}; `: keepalive` every 15 s."""
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    q = asyncio.Queue(maxsize=500)
    subs = request.app.setdefault("event_subs", set())
    subs.add(q)
    try:
        await resp.write(b": connected\n\n")
        while True:
            try:
                doc = await asyncio.wait_for(q.get(), 15)
                await resp.write(("event: change\ndata: " + json.dumps(doc) + "\n\n").encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        subs.discard(q)
    return resp


async def _loci_sync_loop(app):
    while True:
        await _loci_sync_once(app)
        await asyncio.sleep(_TS_LOCI_POLL)


async def _start_loci_sync(app):
    app["loci_sync"] = asyncio.create_task(_loci_sync_loop(app))


async def _stop_loci_sync(app):
    t = app.get("loci_sync")
    if t:
        t.cancel()


def _ssh_hosts_local():
    doc = _read_json(SSH_HOSTS_PATH, {}) or {}
    return {k: v for k, v in doc.items() if isinstance(v, dict) and _SSH_NAME_RE.match(k)}


def _ssh_hosts():
    """Local ssh-hosts.json merged over the toolserver-distributed loci."""
    merged = dict(_TS_LOCI["hosts"])
    merged.update(_ssh_hosts_local())
    return merged


def _ssh_host(name):
    return _ssh_hosts().get(name or "")


def _ssh_opts(h, interactive=False):
    key = h.get("key") or ""
    if not key:
        fk = FV_STATE_HOME / "fleet_ssh_key"
        key = str(fk) if fk.is_file() else ""
    o = ["-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR",
         "-o", "ConnectTimeout=8", "-p", str(int(h.get("port") or 22))]
    # 1.0.75: MULTIPLEX every ssh to a locus over one master connection. The
    # drawers poll an ssh-host locus every 5-10 s and each poll was a fresh
    # login (~0.8 s, 650 logins/90 min into hugpy@ae from the op desktop on
    # 2026-09-02, 25 sessions open at once) — that contention is what froze
    # the seat's own PTY. ControlPersist keeps the master 120 s past the last
    # use, so a burst of probes costs one login. Socket dir is private (0700).
    try:
        mux = FV_STATE_HOME / "ssh-mux"
        mux.mkdir(parents=True, exist_ok=True)
        os.chmod(mux, 0o700)
        o += ["-o", "ControlMaster=auto", "-o", "ControlPath=" + str(mux / "%C"),
              "-o", "ControlPersist=120"]
    except OSError:
        pass
    if not interactive:
        o += ["-o", "BatchMode=yes"]
    if key:
        o += ["-o", "IdentitiesOnly=yes", "-i", key]
    return o


def _ssh_target(h):
    return f"{h.get('user') or 'root'}@{h['host']}"


def _ssh_argv(h, interactive=False):
    return ["ssh"] + _ssh_opts(h, interactive) + [_ssh_target(h)]


def _ssh_shell_cmd(h):
    """The interactive login over ssh (password prompts allowed)."""
    return " ".join(shlex.quote(a) for a in ["ssh", "-t"] + _ssh_opts(h, True) + [_ssh_target(h)])


async def _ssh_run(name, script, timeout=30, stdin=None):
    """Run a bash script on an ssh host (non-interactive). (rc, out, err)."""
    h = _ssh_host(name)
    if not h:
        return 127, "", f"unknown ssh host {name}"
    try:
        proc = await asyncio.create_subprocess_exec(
            *_ssh_argv(h), "bash", "-s",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(((stdin if stdin is not None else script)).encode()), timeout)
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        return 124, "", "ssh timed out"
    except OSError as e:
        return 127, "", str(e)


async def _locus_run(vm, script, timeout=30):
    """Run a bash script in a locus as its seat user: '' host, VM (ubuntu), or
    ssh host. 1.0.141: one transport (locus_exec) — the script rides stdin."""
    return await LX.run(_locus_target(vm), "exec bash -lc " + shlex.quote(script) + "\n",
                        timeout=timeout)


async def _locus_run_fast(vm, script, timeout=10):
    """_locus_run minus the host login shell: `bash -lc` sources the
    operator's full profile (~1.2s here) — fine for kill/wipe, hopeless for
    interactive paths like wheel scrolling. Host runs plain bash; VM/ssh
    grounding keeps the login shell (the transport dominates there)."""
    t = _locus_target(vm)
    if not t.remote:
        return await LX.run(t, script + "\n", timeout=timeout)
    return await _locus_run(vm, script, timeout)


async def _locus_spawn(vm, script):
    """The STREAMING twin of _locus_run: start a long-running bash script in a
    locus with stdout/stderr piped, instead of waiting for it to finish. Same
    grounding — '' host, ssh host, lxd guest. Used by the journal follow.
    1.0.141: the same locus_exec transport (script on stdin, then closed)."""
    pipe = asyncio.subprocess.PIPE
    proc = await asyncio.create_subprocess_exec(
        *LX.script_argv(_locus_target(vm)), stdin=pipe, stdout=pipe, stderr=pipe)
    with contextlib.suppress(Exception):
        proc.stdin.write(("exec bash -lc " + shlex.quote(script) + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()
    return proc


# ── 📜 locus journal (operator 2026-09-15: "the log, it should be a journalctl
# -f of the station's keeper user"). The 📜 log tab used to render THIS
# backend's own python log ring (/api/steward/log*, still served for the
# steward and still reachable behind the panel's `console ring` toggle). It now
# follows the JOURNAL of the locus's STATION KEEPER USER — the user that
# locus's station-web unit runs as — so the panel shows what actually happens
# in the locus home: the station unit, the seat scopes, every unit that user
# owns. Grounding is the usual one: '' = this host, an ssh host, an lxd guest.
JOURNAL_TAIL = int(os.environ.get("STATION_JOURNAL_TAIL", "200"))    # backfill lines (journalctl -n)
JOURNAL_RING = int(os.environ.get("STATION_JOURNAL_RING", "2000"))   # per-locus ring a reconnect backfills from

# Resolve the station keeper user ON the locus — the user ITS station-web unit
# runs as. Order matters, because a box can carry BOTH (ae does): the SYSTEM
# instance named after the login user (hugpy-station-web@hugpy, :8899) wins,
# else that user's own USER unit (`systemctl --user … hugpy-station-web`, the
# vm_mgr keeper seat on :8898), else any system instance on the box (whose
# journal we may not be allowed to read — journald then says so, on stderr,
# and the panel shows that line). k=v lines out.
_JOURNAL_PROBE_SH = r"""
me=$(id -un); u=$me; unit=""; mode=""; sysunit=""
for s in $(systemctl list-units --no-pager --no-legend --all --type=service 'hugpy-station-web@*' 2>/dev/null \
           | sed 's/^[^A-Za-z0-9]*//' | awk '{print $1}'); do
  i=${s#hugpy-station-web@}; i=${i%.service}
  [ -n "$i" ] || continue
  [ -z "$sysunit" ] && sysunit=$s
  if [ "$i" = "$me" ]; then sysunit=$s; break; fi
done
inst=${sysunit#hugpy-station-web@}; inst=${inst%.service}
useru=$(systemctl --user list-units --no-pager --no-legend --all --type=service 'hugpy-station-web*' 2>/dev/null \
        | sed 's/^[^A-Za-z0-9]*//' | awk 'NR==1{print $1}')
if [ -n "$sysunit" ] && [ "$inst" = "$me" ]; then
  unit=$sysunit; mode=system; u=$inst
elif [ -n "$useru" ]; then
  unit=$useru; mode=user; u=$me
else
  # 1.0.141: NEVER another user's station unit (hugpy resolved to hugpy-demo's).
  # No station unit of our own -> this user's own journal: every unit it owns.
  unit=""; mode=user; u=$me
fi
if [ "$mode" = system ]; then uid=$(id -u "$u" 2>/dev/null || id -u); else uid=$(id -u); fi
echo "user=$u"; echo "uid=$uid"; echo "mode=$mode"; echo "unit=$unit"; echo "host=$(hostname)"
"""

_JOURNAL_SRC = {}          # locus → (resolved_at, source dict) — a probe is an ssh hop, so cache it


def _journal_argv(src, follow=True, tail=None, since=""):
    """journalctl for a resolved source. `--user` when the station runs as a
    USER unit — that user's own journal, which is also where its seat scopes
    land. Otherwise the UID's records OR'd (`+`) with the station unit: that is
    exactly what `-u <unit>` expands to, written out, because systemd logs the
    unit's own start/fail lines as root and an AND would hide every one."""
    a = ["journalctl", "-o", "short-iso", "--no-pager"]
    if (src or {}).get("mode") == "user":
        a.append("--user")
    if since:
        a += ["--since", since]
    else:
        a += ["-n", str(max(1, int(tail or JOURNAL_TAIL)))]
    if follow:
        a.append("-f")
    if (src or {}).get("mode") != "user":
        unit = src.get("unit") or "hugpy-station-web.service"
        a += ["_UID=" + str(src.get("uid") or 0), "+", "_SYSTEMD_UNIT=" + unit, "+", "UNIT=" + unit]
    return a


async def _journal_source(vm, refresh=False):
    """Who the station keeper user IS on this locus + the exact journalctl that
    follows them: {user, uid, mode, unit, host, locus, cmd, label, ok, error}."""
    vm = vm or ""
    hit = _JOURNAL_SRC.get(vm)
    if hit and not refresh and time.time() - hit[0] < 300:
        return hit[1]
    rc, out, err = await _locus_run(vm, _JOURNAL_PROBE_SH, timeout=25)
    kv = {}
    for ln in (out or "").splitlines():
        k, eq, v = ln.strip().partition("=")
        if k and eq:
            kv[k] = v.strip()
    src = {"vm": vm, "user": kv.get("user", ""), "uid": kv.get("uid", ""),
           "mode": kv.get("mode", ""), "unit": kv.get("unit", ""),
           "host": kv.get("host", "") or (vm or "localhost")}
    src["locus"] = vm or (_station_locus() or src["host"])
    src["ok"] = bool(src["user"])
    src["error"] = "" if src["ok"] else (
        ((err or out or "").strip() or f"probe failed (rc={rc})")[:300])
    src["cmd"] = " ".join(shlex.quote(a) for a in _journal_argv(src)) if src["ok"] else ""
    src["label"] = (f"journalctl -f · {src['user']}@{src['locus']}"
                    if src["ok"] else "journal unavailable")
    _JOURNAL_SRC[vm] = (time.time(), src)
    return src


async def _reap_proc(p):
    with contextlib.suppress(Exception):
        await p.wait()


class _JournalFeed:
    """One `journalctl -f` per locus, shared by every open panel: a bounded ring
    (so a reconnecting panel backfills instantly) plus one queue per subscriber.
    The pipe restarts with backoff whenever it drops (ssh reset, journald
    rotation, a locus rebooting) and is torn down when the last panel closes —
    nothing follows a locus nobody is watching."""

    def __init__(self, vm):
        self.vm = vm or ""
        self.ring = collections.deque(maxlen=JOURNAL_RING)
        self.subs = set()
        self.seq = 0
        self.src = None
        self.task = None
        self.proc = None

    def emit(self, line, kind="line"):
        self.seq += 1
        doc = {"seq": self.seq, "ts": round(time.time(), 3), "kind": kind,
               "line": str(line)[:4000]}
        self.ring.append(doc)
        for q in list(self.subs):
            try:
                q.put_nowait(doc)
            except asyncio.QueueFull:
                pass
        return doc

    async def _drain_err(self, stream):
        # journald's refusal lands HERE ("…users in groups 'adm', 'systemd-journal'
        # can see all messages"): show the exact reason, never an empty panel.
        while True:
            ln = await stream.readline()
            if not ln:
                return
            t = ln.decode(errors="replace").rstrip()
            if t:
                self.emit(t, "error")

    async def _pump(self):
        backoff = 2
        while self.subs:
            src = await _journal_source(
                self.vm, refresh=bool(self.src is not None and not self.src.get("ok")))
            self.src = src
            if not src.get("ok"):
                self.emit(src.get("error") or "journal source unresolved", "error")
                await asyncio.sleep(30)
                continue
            self.emit(src["cmd"], "meta")
            try:
                self.proc = await _locus_spawn(self.vm, src["cmd"])
            except OSError as e:
                self.emit(f"cannot start journalctl: {e}", "error")
            else:
                errt = asyncio.create_task(self._drain_err(self.proc.stderr))
                try:
                    while True:
                        try:
                            raw = await self.proc.stdout.readline()
                        except ValueError:      # one line past the stream limit
                            continue
                        if not raw:
                            break
                        backoff = 2
                        self.emit(raw.decode(errors="replace").rstrip("\n"))
                finally:
                    errt.cancel()
                    p, self.proc = self.proc, None
                    if p is not None:
                        with contextlib.suppress(Exception):
                            p.kill()
                        asyncio.ensure_future(_reap_proc(p))
            if not self.subs:
                break
            self.emit(f"journal stream ended — reconnecting in {backoff}s", "meta")
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)

    def subscribe(self):
        q = asyncio.Queue(maxsize=2000)
        self.subs.add(q)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._pump())
        return q

    def unsubscribe(self, q):
        self.subs.discard(q)
        if self.subs:
            return
        t, self.task = self.task, None
        if t is not None:
            t.cancel()
        p, self.proc = self.proc, None
        if p is not None:
            with contextlib.suppress(Exception):
                p.kill()
            asyncio.ensure_future(_reap_proc(p))


_JOURNAL_FEEDS = {}


def _journal_feed(vm):
    f = _JOURNAL_FEEDS.get(vm or "")
    if f is None:
        f = _JOURNAL_FEEDS[vm or ""] = _JournalFeed(vm)
    return f


def _journal_vm(request):
    vm = (request.query.get("vm") or "").strip()
    return "" if vm == "@keeper" else vm          # the host seat IS the local locus


async def api_locus_journal(request):
    """GET /api/locus/journal?vm=<locus>&refresh=1 — the RESOLVED source for a
    locus: {user, uid, mode, unit, host, locus, cmd, label} (+ error when the
    probe cannot find the station user). The lines come over the SSE below."""
    src = await _journal_source(_journal_vm(request),
                                refresh=request.query.get("refresh") in ("1", "true"))
    feed = _JOURNAL_FEEDS.get(_journal_vm(request))
    return web.json_response({"ok": bool(src.get("ok")), "source": src,
                              "tail": JOURNAL_TAIL, "ring": JOURNAL_RING,
                              "held": len(feed.ring) if feed else 0})


async def api_locus_journal_stream(request):
    """GET /api/locus/journal/stream?vm=<locus>&after=SEQ — Server-Sent Events:
    a `meta` event carrying the resolved source, then the ring newer than SEQ
    (the follow's own `-n` is the backfill), then one `line` per journal line as
    it lands; `: keepalive` every 15 s. EventSource reconnects on its own and
    `after` resumes without a gap or a duplicate."""
    vm = _journal_vm(request)
    try:
        after = int(request.query.get("after") or 0)
    except ValueError:
        after = 0
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    feed = _journal_feed(vm)
    q = feed.subscribe()
    try:
        await resp.write(b": connected\n\n")
        src = await _journal_source(vm)
        await resp.write(("event: meta\ndata: " + json.dumps({"source": src}) + "\n\n").encode())
        for doc in [d for d in list(feed.ring) if d["seq"] > after]:
            await resp.write(("event: line\ndata: " + json.dumps(doc) + "\n\n").encode())
        while True:
            try:
                doc = await asyncio.wait_for(q.get(), 15)
                await resp.write(("event: line\ndata: " + json.dumps(doc) + "\n\n").encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        feed.unsubscribe(q)
    return resp


# ── 🐞 bug scan (operator 2026-09-15: "an intermittent local agent review of
# the logs, to identify any running problems within the locus home"). A
# scheduler task inside the station takes the last window of the SAME journal
# the 📜 log tab follows — plus this backend's own warnings and, on the host
# locus, the error tails of the seat panes — and GREPS + PARSES it with the
# deterministic pattern table in log_findings.py (1.0.124; operator 2026-09-29:
# "the errors are grepped and parsed; when found, presented to B"). Until
# 1.0.123 this window went to the fleet's Coder-Next slot under a review prompt
# (~40k tokens every ~11 min per locus) — no model is called now. Findings are
# persisted to <state>/bugscan/findings.jsonl deduped by (kind, source,
# normalised signature), so a problem that keeps recurring is ONE row with a
# count. A finding is EMITTED when new, when its count jumps, or when it
# returns after an hour of silence; every scan republishes the set to B's
# lookup surface (<workspace>/.hugpy_agent/mct/findings.json, see
# _b_findings_publish) and a NEW high finding posts ONE board item.
# The separate "report a bug" flow (bugreport-api sidecar, /api/vm/<vm>/bugreport)
# is untouched — that is the VM-side scanner, this is the local keeper's review.
BUGSCAN_ON = (os.environ.get("STATION_BUGSCAN", "1").strip().lower()
              not in ("0", "false", "off", "no"))          # default: review this station
BUGSCAN_INTERVAL = max(60, int(os.environ.get("STATION_BUGSCAN_INTERVAL", "600")))   # 10 min
BUGSCAN_WINDOW = os.environ.get("STATION_BUGSCAN_WINDOW", "20 min ago")
BUGSCAN_LINES = max(50, int(os.environ.get("STATION_BUGSCAN_LINES", "400")))
BUGSCAN_TIMEOUT = max(30, int(os.environ.get("STATION_BUGSCAN_TIMEOUT", "240")))
BUGSCAN_BOARD = (os.environ.get("STATION_BUGSCAN_BOARD", "1").strip().lower()
                 not in ("0", "false", "off", "no"))       # a NEW high finding → one board item
BUGSCAN_KEEP = max(50, int(os.environ.get("STATION_BUGSCAN_KEEP", "400")))

_bslog = logging.getLogger("station.bugscan")

try:
    import log_findings as _logf
except ImportError:                                  # pragma: no cover — packaging error
    _logf = None
try:                                                 # 1.0.124: findings are PUSHED to the keeper
    import keeper_notify as _kn
    import b_propose as _bprop
except ImportError:                                  # pragma: no cover — packaging error
    _kn = _bprop = None
try:                                                 # 1.0.140: the toolserver issue gate
    import issue_gate as _ig
except ImportError:                                  # pragma: no cover — packaging error
    _ig = None
try:                                                 # 2026-10-01: bug reports -> the locus's WORKER
    import bug_route as _bugr
except ImportError:                                  # pragma: no cover — packaging error
    _bugr = None

# The keeper's locus. 2026-10-01 (operator): bug reports (findings, loops, issue
# raises, B proposals) NO LONGER go to the keeper — they go to the WORKER serve
# session of the locus they concern (bug_route.py); KEEPER_TARGET only names the
# issue registry's locus now.
KEEPER_TARGET = (os.environ.get("STATION_KEEPER_LOCUS") or "keeper").strip().lower() or "keeper"
# B proposes fixes for new findings — OFF by default since 2026-10-01 (operator:
# "take B out of it for now", board t4178 [F2.5]); STATION_B_PROPOSE=1 turns it
# back on, and its proposals then go to the worker like every bug report.
B_PROPOSE_ON = (os.environ.get("STATION_B_PROPOSE", "0").strip().lower()
                in ("1", "true", "on", "yes"))
B_PROPOSE_PER_SCAN = max(1, int(os.environ.get("STATION_B_PROPOSE_PER_SCAN") or "2"))  # the rest stay due
_NOTIFY = None
_METER = None
_PROPOSE_BUSY = False
_PROPOSE_TASK = None
_BUG_OUTBOX = None
_BUG_LOCK = None


def _notify_book():
    global _NOTIFY
    if _NOTIFY is None and _kn is not None:
        seat = os.environ.get("STATION_NOTIFY_SEAT_PANES", "0").strip().lower() in ("1", "true", "on", "yes")
        _NOTIFY = _kn.NotifyBook(state_path=str(_bugscan_dir() / "notify.json"),
                                 strip_only_prefixes=() if seat else ("seat:",))
    return _NOTIFY


def _token_meter():
    """This station's own model spend per task (token_burn findings)."""
    global _METER
    if _METER is None and _kn is not None:
        pre = "STATION_TOKEN_BUDGET_"
        budgets = {}
        for k, v in os.environ.items():
            if k.startswith(pre):
                try:
                    budgets[k[len(pre):].lower()] = int(v)
                except ValueError:
                    pass
        _METER = _kn.TokenMeter(state_path=str(FV_STATE_HOME / "token-meter.json"),
                                budget=int(os.environ.get("STATION_TOKEN_BUDGET") or "200000"),
                                budgets=budgets)
    return _METER


def _meter_tokens(task, tokens):
    try:
        m = _token_meter()
        if m is not None and tokens:
            m.add(task, int(tokens))
    except Exception:                                    # noqa: BLE001 — metering never breaks a call
        pass


def _notify_origin():
    return _station_locus() or _station_id()


def _issue_gate(app):
    """The toolserver issue registry (abstract_toolserver >= 0.0.39). Since
    2026-10-01 (t4178) it is an ANNOTATION only: a sighting is observed (fail
    OPEN — no answer changes nothing) so the registry keeps the history and the
    report carries the issue's state; it never decides whether anything is sent."""
    if _ig is None:
        return None

    async def call(path, body):
        return await _ts_call(app, path, body, timeout=10)
    return _ig.Gate(call, locus=KEEPER_TARGET)


# ── bug reports -> the locus's WORKER serve session (operator 2026-10-01) ──────────
def _bug_outbox():
    global _BUG_OUTBOX
    if _BUG_OUTBOX is None and _bugr is not None:
        _BUG_OUTBOX = _bugr.Outbox(
            state_path=str(FV_STATE_HOME / "bugreport-outbox.json"),
            min_interval=int(os.environ.get("STATION_BUGREPORT_MIN_INTERVAL") or _bugr.MIN_INTERVAL_S),
            self_interval=int(os.environ.get("STATION_BUGREPORT_SELF_INTERVAL") or _bugr.SELF_INTERVAL_S),
            inflight_max=int(os.environ.get("STATION_BUGREPORT_INFLIGHT_MAX") or _bugr.INFLIGHT_MAX_S))
    return _BUG_OUTBOX


def _bug_locus(vm):
    """The central board slice a bug report of dropdown locus `vm` lands on."""
    return (_central_locus(vm) if vm else _keeper_locus()) or ""


class _BugIO:
    """bug_route I/O for one locus: board writes on the locus's own slice (type
    `finding`, never a [ping]) and delivery to that locus's WORKER serve session
    through the ✍ prompt bar's per-locus serve path (prompt_send.deliver_serve)."""

    def __init__(self, app, vm):
        self.app, self.vm = app, vm or ""

    async def board_add(self, e, text, note):
        loc = e.get("locus") or _bug_locus(self.vm)
        if not loc:
            raise RuntimeError(LOCUS_UNCONFIGURED)
        res = await _ts_call(self.app, "todo/add", {"text": text, "type": _bugr.BOARD_TYPE,
                                                    "priority": e.get("priority") or "medium", "note": note,
                                                    "by": "bugscan@" + (_notify_origin() or "station")[:14],
                                                    "locus": loc, "source": _bugr.SOURCE}, timeout=15)
        if isinstance(res, dict) and res.get("error"):
            raise RuntimeError(res["error"])
        return str((res or {}).get("id") or "") if isinstance(res, dict) else ""

    async def board_update(self, e, note):
        await _ts_call(self.app, "todo/update", {"id": e["board_id"], "note": note}, timeout=15)

    async def board_done(self, e, note):
        await _ts_call(self.app, "todo/update", {"id": e["board_id"], "status": "done", "note": note}, timeout=15)

    async def _serve(self):
        url, _unit, _path, _key = await _ac_resolve(self.vm)
        if not url:
            return None, "no abstract-claude serve for locus " + (self.vm or "host")
        return _serve_caller(self.app, url), ""

    async def deliver(self, text):
        call, why = await self._serve()
        if call is None:
            return PS.result("failed", "serve", why)
        res = await PS.deliver_serve(call, text, [], session_id="",
                                     head=lambda: _serve_head_of(call, role=_bugr.ROLE))
        if res.get("ok") or "not a console session" not in str(res.get("reason") or ""):
            return res
        # a native (non console-service) worker session: serve's role relay runs it,
        # or queues it on that session; the relay log has the outcome
        try:
            st, doc = await call("POST", "/api/session/%s/message" % _bugr.ROLE,
                                 {"text": text, "from": "station", "by": "station-bug-report",
                                  "wait": False, "via_b": False})
        except Exception as ex:                          # noqa: BLE001
            return PS.result("failed", "serve", "serve relay unreachable: %s" % ex)
        if st in (200, 202) or (st == 409 and (doc or {}).get("queued")):
            return PS.result("queued", "serve", "", session_id=str((doc or {}).get("session_id") or ""),
                             detail="accepted by serve's %s relay (id %s)" % (_bugr.ROLE, (doc or {}).get("id")))
        return PS.result("failed", "serve", "serve relay HTTP %s %s" % (st, str((doc or {}).get("error") or "")[:160]))

    async def inflight_busy(self, w):
        call, _why = await self._serve()
        if call is None:
            return True
        st, q = await call("GET", "/api/console/queue?id=" + str(w.get("sid") or ""), None)
        if st != 200 or not isinstance(q, dict):
            return False                                 # the session is gone: nothing is queued behind
        ids = {str(i.get("id")) for i in q.get("items") or [] if isinstance(i, dict)}
        return bool(ids & {str(m) for m in w.get("message_ids") or []})

    async def inflight_cancel(self, w):
        """1.0.146: the drain deadline passed — pull our stale report out of the
        worker queue (a fresh coalesced one goes out instead)."""
        call, _why = await self._serve()
        if call is None:
            return
        for mid in w.get("message_ids") or []:
            await call("POST", "/api/console/queue", {"session_id": str(w.get("sid") or ""),
                                                      "action": "remove", "id": str(mid)})


async def _bug_pump(app):
    """One delivery pass per locus with pending bug reports. Never raises."""
    global _BUG_LOCK
    box = _bug_outbox()
    if box is None:
        return []
    if _BUG_LOCK is None:
        _BUG_LOCK = asyncio.Lock()
    out = []
    async with _BUG_LOCK:
        for vm in box.vms():
            try:
                st = await box.run_pass(vm, _BugIO(app, vm), locus_label=_bug_locus(vm) or vm or "this station")
                out.append(st)
                hk = "hold:bug-report@" + (vm or "host")
                if st.get("requeued"):
                    _audit_line("bug-report-requeue", "%s: %s" % (vm or "host", st["requeued"]))
                    _hold_note("count:bug-report-requeue@" + (vm or "host"), _sh.SOURCE_HOLD if _sh else "station:hold",
                               "🐞 bug report re-sent (worker drain deadline)", st["requeued"],
                               "nothing to do — the worker gets a fresh coalesced report", inc=True)
                if st.get("sent") or (st.get("state") == "pending" and st.get("reason")):
                    _audit_line("bug-report", "%s → worker: %s %s %s" % (vm or "host", st.get("state"),
                                                                          st.get("sent"), st.get("reason") or ""))
                if st.get("state") in ("deferred", "pending") and st.get("reason"):
                    _hold_note(hk, _sh.SOURCE_HOLD if _sh else "station:hold",
                               "🐞 bug report(s) for %s %s" % (vm or "host", st["state"]), st["reason"],
                               "retried every pass; worker session via /api/locus/serve/provision",
                               count=len(box.pending(vm)))
                else:
                    _hold_clear(hk)
            except Exception as e:                       # noqa: BLE001
                _bslog.warning("bug report pass %s: %s", vm or "host", e)
    return out


async def _bug_observe(gate, kw):
    """Annotation only (fail OPEN): the issue registry's state for this sighting."""
    if gate is None or not kw:
        return None
    res = await gate.observe(**kw)
    return res if isinstance(res, dict) else None


def _finding_observe_spec(r, origin):
    src = str(r.get("source") or "")
    cnt, last = int(r.get("count") or 0), int(r.get("gate_count") or 0)
    return {"source": "%s@%s" % (src or "log", r.get("locus") or origin),
            "kind": "finding:%s" % (r.get("kind") or "other"),
            "subject": r.get("signature") or "", "text": (r.get("sample_lines") or [""])[0],
            "severity": r.get("severity"), "caller": src,
            "meta": {"unit": src, "finding": r.get("key"), "origin": origin}, "n": max(1, cnt - last)}


async def _report_findings(app, vm, book, ev, now):
    """Findings -> bug reports for the WORKER of locus `vm` (bug_route). Every
    reported row (severity >= medium, not seat-pane prose — classification, both
    still on the ⚠ strip) is offered; the outbox coalesces and retries, so a
    report is never dropped. The issue registry only annotates (fail open)."""
    box = _bug_outbox()
    if box is None:
        return 0
    gate, origin, n = _issue_gate(app), _notify_origin(), 0
    fresh = {id(r) for r in (ev.get("mail") or [])}
    seen = set()
    for r in list(ev.get("mail") or []) + list(ev.get("board") or []):
        if id(r) in seen or not book.notify_row(r):
            continue
        seen.add(id(r))
        r["vm"] = vm or ""
        res = await _bug_observe(gate, _finding_observe_spec(r, origin)) if id(r) in fresh else None
        if res:
            r["issue_fp"], r["issue_state"] = res.get("fp"), res.get("state")
            r["gate_count"] = int(r.get("count") or 0)
        text, _note = _kn.fmt_board(r, book.sig(r["sigkey"]))
        ann = " · ".join(x for x in (book.annotation(r), (res or {}).get("annotation") or "") if x)
        if r.get("issue_fp"):
            ann = (ann + " · " if ann else "") + "issue_fp=" + r["issue_fp"]
        box.offer("finding:" + r["key"], vm=vm or "", locus=_bug_locus(vm), kind="finding:%s" % r.get("kind"),
                  title=text, body=_kn.fmt_mail(r, book.sig(r["sigkey"])), severity=r.get("severity"),
                  count=r.get("count"), source=r.get("source"), annotation=ann, priority=book.priority_of(r),
                  fresh=id(r) in fresh)
        r["mailed_at"], r["pending_mail"] = now, False
        n += 1
    for r in ev.get("cleared") or []:
        box.clear("finding:" + r["key"], _kn.fmt_resolve(r))
    box.save()
    return n


def _bug_link_board(book, idx):
    """Copy the outbox's board ids back onto the NotifyBook rows / findings records."""
    box = _bug_outbox()
    if box is None:
        return
    for k, r in book.rows.items():
        e = box.items.get("finding:" + k)
        if e and e.get("board_id") and r.get("board_id") != e["board_id"]:
            r["board_id"] = e["board_id"]
            rec = idx.get(k)
            if rec and rec.get("board") != e["board_id"]:
                _bugscan_append(dict(rec, board=e["board_id"]))


class _BugBoard:
    """closed(ids) over every board slice the outbox writes to (disposition reads)."""

    def __init__(self, app):
        self.app = app

    async def closed(self, ids):
        box, want, out = _bug_outbox(), {str(i) for i in ids}, {}
        loci = {e.get("locus") for e in (box.items.values() if box else []) if e.get("locus")}
        loci.add(_keeper_locus() or KEEPER_TARGET)
        for loc in sorted(x for x in loci if x):
            res = await _ts_call(self.app, "todo/list", {"status": "done", "locus": loc, "limit": 500}, timeout=15)
            for r in res or []:
                if isinstance(r, dict) and str(r.get("id")) in want:
                    out[str(r["id"])] = r.get("note") or ""
        if box is not None:
            for bid in out:
                box.board_closed(bid)
        return out

_BUGSCAN_ERR_RE = re.compile(
    r"(?i)\b(error|traceback|exception|denied|refused|failed|fatal|429|rate.?limit|timed out)\b")

_BUGSCAN_FINDINGS = None    # hash → newest row (the jsonl is an append log)
_BUGSCAN_STATE = None       # {"loci": {locus: {on, interval, last, note}}}
_BUGSCAN_BUSY = {}


def _bugscan_dir():
    d = Path(_hugpy_path("state", "bugscan"))
    with contextlib.suppress(OSError):
        d.mkdir(parents=True, exist_ok=True)
    return d


def _bugscan_load():
    global _BUGSCAN_FINDINGS
    if _BUGSCAN_FINDINGS is not None:
        return _BUGSCAN_FINDINGS
    rows = {}
    try:
        with open(_bugscan_dir() / "findings.jsonl", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("hash"):
                    rows[r["hash"]] = r          # last row per hash wins
    except OSError:
        pass
    _BUGSCAN_FINDINGS = rows
    return rows


def _bugscan_append(row):
    try:
        with open(_bugscan_dir() / "findings.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError as e:
        _bslog.warning("findings.jsonl: %s", e)
    _bugscan_load()[row["hash"]] = row


def _bugscan_compact():
    """Rewrite the append log as one row per hash (newest BUGSCAN_KEEP) once it
    has grown well past that — findings survive restarts, not forever."""
    p = _bugscan_dir() / "findings.jsonl"
    try:
        if not p.is_file() or sum(1 for _ in open(p, errors="replace")) < BUGSCAN_KEEP * 4:
            return
        keep = sorted(_bugscan_load().values(), key=lambda r: r.get("ts") or 0)[-BUGSCAN_KEEP:]
        tmp = p.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in keep), encoding="utf-8")
        tmp.replace(p)
        _BUGSCAN_FINDINGS.clear()
        _BUGSCAN_FINDINGS.update({r["hash"]: r for r in keep})
    except OSError as e:
        _bslog.warning("findings compact: %s", e)


def _bugscan_state():
    global _BUGSCAN_STATE
    if _BUGSCAN_STATE is None:
        st = _read_json(_bugscan_dir() / "state.json", {})
        _BUGSCAN_STATE = st if isinstance(st, dict) else {}
        _BUGSCAN_STATE.setdefault("loci", {})
    return _BUGSCAN_STATE


def _bugscan_state_save():
    try:
        (_bugscan_dir() / "state.json").write_text(
            json.dumps(_bugscan_state(), indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        _bslog.warning("bugscan state: %s", e)


def _bugscan_locus_state(vm):
    """Per-locus scheduler row. A fresh install reviews THIS station out of the
    box (STATION_BUGSCAN); a named locus stays off until the operator flips it,
    so adding an ssh host never silently starts polling it."""
    loci = _bugscan_state().setdefault("loci", {})
    row = loci.get(vm or "")
    if row is None:
        row = loci[vm or ""] = {"on": bool(BUGSCAN_ON and not vm),
                                "interval": BUGSCAN_INTERVAL, "last": 0, "note": ""}
    row.setdefault("interval", BUGSCAN_INTERVAL)
    row.setdefault("last", 0)
    row.setdefault("note", "")
    return row


def _bugscan_on(row):
    """1.0.140: STATION_BUGSCAN=0 is a MASTER off for this station — it wins over
    the persisted per-locus ``on`` (state.json is left as the operator set it, so
    re-enabling the env restores the panel's choice)."""
    return bool(BUGSCAN_ON and (row or {}).get("on"))


BUGSCAN_OFF_NOTE = "disabled by STATION_BUGSCAN=0"


async def _bugscan_seat_tails(vm):
    """Cheap: the last error-ish lines of each seat pane on the keeper socket.
    Host locus only — a remote locus's panes are its own station's business."""
    if vm:
        return ""
    rc, out, _e = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "list-panes", "-a",
                             "-F", "#{session_name}")
    if rc != 0:
        return ""
    names, seen = [], set()
    for ln in (out or "").splitlines():
        s = ln.strip()
        if s and s not in seen:
            seen.add(s)
            names.append(s)
    chunks = []
    for s in names[:6]:
        rc, pane, _e = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "capture-pane", "-p",
                                  "-t", "=" + s + ":", "-S", "-40")
        if rc != 0:
            continue
        hits = [l.strip() for l in (pane or "").splitlines()
                if l.strip() and _BUGSCAN_ERR_RE.search(l)][-3:]
        if hits:
            chunks.append(s + ": " + " | ".join(h[:200] for h in hits))
    return "\n".join(chunks)


async def _bugscan_gather(vm):
    """The window handed to B: the locus journal since BUGSCAN_WINDOW, this
    backend's own WARNING+ ring, and the seat-pane error tails. (text, source)."""
    src = await _journal_source(vm)
    parts = []
    if src.get("ok"):
        cmd = " ".join(shlex.quote(a) for a in
                       _journal_argv(src, follow=False, since=BUGSCAN_WINDOW))
        rc, out, err = await _locus_run(vm, cmd + " | tail -n " + str(BUGSCAN_LINES), timeout=90)
        body = ((out or "").strip()
                or ((err or "").strip()[:400] if rc != 0 else "(no journal lines in the window)"))
        parts.append(f"--- journal ({src['user']}@{src['locus']}, since {BUGSCAN_WINDOW}) ---\n{body}")
    else:
        parts.append("--- journal ---\n(unavailable: " + (src.get("error") or "unresolved") + ")")
    ring = [d for d in list(_LOG_RING)[-800:]
            if d.get("level") in ("WARNING", "ERROR", "CRITICAL")][-80:]
    if ring:
        parts.append("--- station console backend log (warnings+) ---\n" + "\n".join(
            time.strftime("%H:%M:%S", time.localtime(d["ts"])) +
            f" {d['level']} {d['name']} {d['msg']}" for d in ring))
    tails = await _bugscan_seat_tails(vm)
    if tails:
        parts.append("--- seat panes (last error-ish lines) ---\n" + tails)
    return "\n\n".join(parts), src


def _bugscan_record(vm, locus, row, prev):
    """A log_findings row as a findings.jsonl record — the panel's fields
    (hash, severity, subject, evidence, suggested_action, first_ts, ts, runs,
    board) plus the structured finding (kind, source, count, first_seen,
    last_seen, sample_lines, signature)."""
    rec = dict(row)
    rec.pop("key", None)
    rec.pop("emit_reason", None)
    rec.update({"hash": row["key"], "locus": locus, "vm": vm or "",
                "subject": _logf.subject(row),
                "evidence": "\n".join(row.get("sample_lines") or [])[:1200],
                "first_ts": int(prev.get("first_ts") or row.get("first_seen") or row.get("ts") or 0),
                "ts": int(row.get("ts") or time.time()),
                "runs": int(row.get("count") or 1),
                "board": prev.get("board") or ""})
    return rec


async def _bugscan_run(app, vm):
    """One review pass for a locus. Never raises into the panel: every failure
    becomes the row's `note` and the next interval tries again."""
    vm = vm or ""
    row = _bugscan_locus_state(vm)
    if not BUGSCAN_ON:                                   # 1.0.140: the env switch is final
        return {"ok": False, "note": BUGSCAN_OFF_NOTE}
    if _BUGSCAN_BUSY.get(vm):
        return {"ok": False, "note": "a review is already running"}
    _BUGSCAN_BUSY[vm] = True
    try:
        logs, src = await _bugscan_gather(vm)
        locus = src.get("locus") or (vm or "this station")
        if _logf is None:
            raise RuntimeError("log_findings module missing (packaging)")
        now = time.time()
        found = _logf.scan(logs, now=now, locus=locus)
        if not vm and _token_meter() is not None:        # this station's own costly tasks
            found += _token_meter().findings(now, locus=locus)
        idx = _bugscan_load()
        emitted, new_high, pairs = [], [], []
        for f in found:
            prev = idx.get(f["key"]) or {}
            prow = dict(prev, key=prev.get("hash")) if prev.get("kind") else {}
            merged, reason = _logf.merge(prow, f, now)
            rec = _bugscan_record(vm, locus, merged, prev)
            if reason or rec != prev:
                if reason:
                    rec["emit_reason"] = reason
                _bugscan_append(rec)
            pairs.append((rec, reason))
            if reason:
                emitted.append(rec)
                if reason == "new" and rec["severity"] == "high" and not rec.get("board"):
                    new_high.append(rec)
        row["last"] = int(now)
        row["note"] = (f"{len(found)} finding(s)"
                       + (f" · {len(emitted)} new/jumped" if emitted else "")
                       + (f" · {len(new_high)} new high" if new_high else "")
                       if found else "no running problems found")
        _bugscan_state_save()
        await _notify_findings(app, vm, pairs, now)
        _b_findings_publish(emitted)
        _bugscan_compact()
        _bslog.info("bugscan %s: %s", locus, row["note"])
        return {"ok": True, "count": len(found), "emitted": len(emitted),
                "new_high": len(new_high), "note": row["note"]}
    except Exception as e:                       # noqa: BLE001 — a scan never errors the panel
        _bslog.warning("bugscan %s: %s", vm or "self", e)
        row["note"] = f"scan failed: {str(e)[:160]}"
        row["last"] = int(time.time())
        _bugscan_state_save()
        return {"ok": False, "note": row["note"]}
    finally:
        _BUGSCAN_BUSY.pop(vm, None)


async def _notify_findings(app, vm, pairs, now):
    """This scan's findings -> bug reports for the WORKER session of locus `vm`
    (operator 2026-10-01: never the keeper), the ⚠ strip, the dispositions read
    back from the board, and (STATION_B_PROPOSE=1 only) B's fix proposals.
    Never raises."""
    book = _notify_book()
    if book is None:
        return
    try:
        idx = _bugscan_load()
        live = {h: r.get("last_seen") for h, r in idx.items() if (r.get("vm") or "") == (vm or "")}
        ev = book.step(pairs, live, now)
        await _report_findings(app, vm, book, ev, now)
        book.save()
        await _bug_pump(app)
        _bug_link_board(book, idx)
        book.save()
        if not vm:
            await _notify_poll_dispositions(app, book, _BugBoard(app))
        if B_PROPOSE_ON and (ev.get("propose") or _propose_undelivered(book)):
            _propose_kick(app, book, [r for r in ev.get("propose") or [] if r.get("issue_fp")])
    except Exception as e:                               # noqa: BLE001 — the scan itself must finish
        _bslog.warning("notify: %s", e)


async def _notify_poll_dispositions(app, book, sink):
    """The keeper closes a finding / proposal with a disposition line in its
    note ("inert: <reason>", "reject: <reason>", "accept"; a plain close of a
    proposal = accepted). Read the closed items once per scan."""
    ids = {}
    for k, r in book.rows.items():
        if r.get("board_id") and r.get("active") and r.get("disp_read") != r["board_id"]:
            ids[str(r["board_id"])] = ("finding", k)
    for sk, sg in book.sigs.items():
        for pr in sg.get("proposals") or []:
            if pr.get("status") == "delivered" and pr.get("id") and pr.get("disposition") in (None, "open"):
                ids[str(pr["id"])] = ("proposal", sk)
    if not ids:
        return
    try:
        closed = await sink.closed(list(ids))
    except Exception:                                    # noqa: BLE001 — toolserver down: next scan
        return
    gate = _issue_gate(app)
    for bid, note in closed.items():
        what, ref = ids[bid]
        disp, reason = _kn.parse_disposition(note)
        if what == "proposal":
            book.set_disposition(ref, disp or "accepted", reason, by="keeper", proposal_id=bid)
        elif disp == "inert":
            book.set_disposition(ref, "inert", reason, by="keeper")
        final = disp or ("accepted" if what == "proposal" else "closed")
        # 1.0.140: the decision goes to the issue registry (fail open)
        if what == "proposal":
            fp = _book_issue_fp(book, ref)
        else:
            row = book.rows.get(ref) or {}
            row["disp_read"] = bid                      # one close = one disposition, not one per scan
            fp = row.get("issue_fp") or _book_issue_fp(book, row.get("sigkey"))
        if gate is not None and fp and not (disp is None and _ig.is_auto_resolve(note)):
            if what == "proposal":
                await gate.record_action(fp, "keeper %s B proposal %s" % (final, bid), by="keeper", note=reason,
                                         proposal_id=bid, disposition=final, board_id=bid)
            await gate.disposition(fp, final, reason or final, by="keeper", board_id=bid)
        _todo_hist({"event": "finding-disposition", "board": bid, "what": what, "ref": ref,
                    "disposition": final, "reason": reason, "fp": fp or ""})
    book.save()


def _book_issue_fp(book, sigkey):
    """The issue fp a NotifyBook signature was last gated under (or '')."""
    if not sigkey:
        return ""
    s = book.sigs.get(sigkey) or {}
    if s.get("issue_fp"):
        return s["issue_fp"]
    return next((r.get("issue_fp") for r in book.rows.values()
                 if r.get("sigkey") == sigkey and r.get("issue_fp")), "")


async def api_findings_disposition(request):
    """POST /api/findings/{sig}/disposition {"disposition": open|accepted|rejected|inert,
    "reason"?, "by"?} — set a signature's disposition (sig = sig key or finding key)."""
    book = _notify_book()
    if book is None:
        return web.json_response({"ok": False, "error": "keeper_notify module missing"}, status=503)
    try:
        body = await request.json()
    except Exception:                                    # noqa: BLE001
        body = {}
    disp = str(body.get("disposition") or "").strip().lower()
    if disp not in _kn.DISPOSITIONS:
        return web.json_response({"ok": False, "error": "disposition must be one of " + ", ".join(_kn.DISPOSITIONS)},
                                 status=400)
    s = book.set_disposition(request.match_info["sig"], disp, body.get("reason") or "",
                             by=str(body.get("by") or "keeper"))
    if s is None:
        return web.json_response({"ok": False, "error": "unknown signature"}, status=404)
    fp = _book_issue_fp(book, book.resolve_sig(request.match_info["sig"]))
    gate = _issue_gate(request.app)
    if gate is not None and fp:                          # 1.0.140: mirrored into the registry (fail open)
        await gate.disposition(fp, disp, body.get("reason") or disp, by=str(body.get("by") or "keeper"))
    audit(request, "finding-disposition", f"{request.match_info['sig']}={disp}", ok=True)
    return web.json_response({"ok": True, "sig": book.resolve_sig(request.match_info["sig"]),
                              **{k: v for k, v in s.items() if k not in ("hist",)}})


async def api_findings_notify(request):
    """GET /api/findings/notify — the notifier's view: active rows + per-signature
    dispositions and proposal history (the rate history omitted)."""
    book = _notify_book()
    if book is None:
        return web.json_response({"ok": False, "error": "keeper_notify module missing"}, status=503)
    return web.json_response({"ok": True, "keeper": KEEPER_TARGET, "origin": _notify_origin(),
                              "remote": _notify_origin() != KEEPER_TARGET,
                              "rows": book.strip_rows(),
                              "sigs": {k: {kk: vv for kk, vv in v.items() if kk != "hist"}
                                       for k, v in book.sigs.items()},
                              "token_burn": (_token_meter().rates() if _token_meter() else {}),
                              # 2026-10-01: bug reports go to the locus's WORKER session; the
                              # outbox shows every report's delivery state (nothing hidden)
                              "routing": "worker",
                              "outbox": ([{k: v for k, v in e.items() if k not in ("body",)}
                                          for e in _bug_outbox().items.values()] if _bug_outbox() else []),
                              "b_propose": B_PROPOSE_ON,
                              "inert_silences": bool(_kn.inert_silences())})


# ── B proposes fixes (1.0.124) ────────────────────────────────────────────────
_HA_DIR = None


def _hugpy_agent_dir():
    global _HA_DIR
    if _HA_DIR is None:
        _HA_DIR = ""
        py = _hugpy_python()
        if py:
            try:
                r = subprocess.run([py, "-c", "import hugpy_agent,os;print(os.path.dirname(hugpy_agent.__file__))"],
                                   capture_output=True, text=True, timeout=20)
                _HA_DIR = r.stdout.strip() if r.returncode == 0 else ""
            except Exception:                            # noqa: BLE001
                pass
    return _HA_DIR


def _propose_roots(row):
    """Where B's context is grepped: this backend, hugpy_agent, and the unit's
    own ExecStart script when the finding's source is a unit."""
    roots = [str(Path(__file__).resolve().parent)]
    if _hugpy_agent_dir():
        roots.append(_hugpy_agent_dir())
    src = str(row.get("source") or "")
    if re.match(r"^[\w@.:-]+\.service$", src):
        for scope in (["--user"], []):
            try:
                r = subprocess.run(["systemctl", *scope, "show", "-p", "ExecStart", "--value", src],
                                   capture_output=True, text=True, timeout=10)
                m = re.search(r"path=(\S+)", r.stdout or "")
                if m and os.path.isfile(m.group(1)):
                    roots.append(m.group(1))
                    for tok in re.findall(r"argv\[\]=([^;]+)", r.stdout or "")[:1]:
                        for a in tok.split():
                            if a.startswith("/") and os.path.isfile(a) and a != m.group(1):
                                roots.append(a)
                    break
            except Exception:                            # noqa: BLE001
                continue
    return roots


_B_PROPOSE_SCRIPT = r"""
import json, sys
from hugpy_agent.config import load_config
from hugpy_agent.gateway import Gateway
d = json.load(sys.stdin)
gw = Gateway.from_config(load_config())
_orig = gw.build_payload
def _bp(*a, **k):
    p = _orig(*a, **k)
    p["response_format"] = {"type": "json_object"}
    return p
gw.build_payload = _bp
r = gw.chat(d["messages"], temperature=0, max_tokens=d["max_tokens"])
print(json.dumps({"ok": bool(r.ok), "text": r.text or "", "error": r.error, "est": int(getattr(r, "est_tokens", 0) or 0)}))
"""


def _b_propose_call(msgs):
    """B's model through the same hugpy_agent Gateway as _b_answer: JSON
    response_format, temperature 0, max_tokens 600. -> (text, tokens, error)."""
    est_in = sum(len(m.get("content") or "") for m in msgs) // 4
    try:
        from hugpy_agent.config import load_config
        from hugpy_agent.gateway import Gateway
    except ImportError:                                  # packaged python: hugpy_agent's own interpreter
        py = _hugpy_python()
        if not py:
            return "", 0, "hugpy_agent not importable"
        try:
            r = subprocess.run([py, "-c", _B_PROPOSE_SCRIPT], capture_output=True, text=True, timeout=240,
                               input=json.dumps({"messages": msgs, "max_tokens": _bprop.MAX_TOKENS}))
            d = json.loads((r.stdout or "").strip().splitlines()[-1])
        except Exception as e:                           # noqa: BLE001
            return "", 0, ("gateway subprocess: %s" % e)[:200]
    else:
        try:
            gw = Gateway.from_config(load_config())
            orig = gw.build_payload

            def _bp(*a, **k):
                pl = orig(*a, **k)
                pl["response_format"] = {"type": "json_object"}
                return pl
            gw.build_payload = _bp
            res = gw.chat(msgs, temperature=0, max_tokens=_bprop.MAX_TOKENS)
            d = {"ok": bool(res.ok), "text": res.text or "", "error": res.error,
                 "est": int(getattr(res, "est_tokens", 0) or 0)}
        except Exception as e:                           # noqa: BLE001
            return "", 0, str(e)[:200]
    text = d.get("text") or ""
    tokens = int(d.get("est") or 0) or (est_in + len(text) // 4)
    return text, tokens, (None if d.get("ok") else (d.get("error") or "gateway error"))


def _propose_undelivered(book):
    return [(sk, pr) for sk, sg in book.sigs.items() for pr in sg.get("proposals") or []
            if pr.get("status") == "undelivered" and pr.get("payload")]


def _propose_kick(app, book, rows):
    """One B proposal pass in the background — one in flight per station."""
    if _bprop is None or not B_PROPOSE_ON or _PROPOSE_BUSY:
        return
    global _PROPOSE_TASK
    _PROPOSE_TASK = asyncio.create_task(_propose_run(app, book, rows))


async def _propose_deliver(app, book, sink, row, sk, p, entry=None):
    """A B proposal (STATION_B_PROPOSE=1 only) is a bug report too: a `finding`
    board item for the finding's locus, delivered to that locus's WORKER session
    (bug_route) — never a keeper ping or keeper mail (operator 2026-10-01)."""
    origin = _notify_origin()
    tb, nb = _bprop.fmt_board(p, row, origin)
    fp = row.get("issue_fp") or _book_issue_fp(book, sk)
    box = _bug_outbox()
    if box is None:
        raise RuntimeError("bug_route module missing")
    vm = row.get("vm") or ""
    key = "proposal:%s:%d" % (row.get("key"), int(time.time()))
    box.offer(key, vm=vm, locus=_bug_locus(vm), kind="b-proposal", title=tb, body=nb,
              severity=row.get("severity"), count=row.get("count"), source="b-propose",
              annotation=("issue_fp=" + fp) if fp else "", priority="medium")
    await _bug_pump(app)
    bid = (box.items.get(key) or {}).get("board_id")
    if not bid:
        raise RuntimeError("board add returned no id")
    gate = _issue_gate(app)
    if gate is not None and fp:
        await gate.record_b(fp, p, status="delivered", proposal_id=bid, model="b")
    sg = book.sig(sk)
    if fp:
        sg["issue_fp"] = fp
    if entry is None:
        entry = _bprop.record(sg, p, "delivered", bid=bid)
    else:
        entry.update(status="delivered", id=bid, disposition="open")
        entry.pop("payload", None)
        sg["disposition"] = "proposed"
    row["proposal_id"] = bid
    rec = _bugscan_load().get(row["key"])
    if rec:
        _bugscan_append(dict(rec, proposal=bid))
    book.save()
    _bslog.info("B proposal %s for %s", bid, row.get("identity"))
    return bid


async def _propose_run(app, book, rows):
    global _PROPOSE_BUSY
    _PROPOSE_BUSY = True
    try:
        sink = None                                      # proposals go through bug_route (worker)
        for sk, entry in _propose_undelivered(book):     # a proposal whose board post failed earlier
            row = next((r for r in book.rows.values() if r["sigkey"] == sk), None)
            if row is None:
                continue
            try:
                await _propose_deliver(app, book, sink, row, sk, entry["payload"], entry)
            except Exception:                            # noqa: BLE001
                return
        loop = asyncio.get_event_loop()
        calls = 0
        gate = _issue_gate(app)
        for row in rows:
            if calls >= B_PROPOSE_PER_SCAN:              # first scan after install: no burst
                break
            sk = row["sigkey"]
            sg = book.sig(sk)
            if not row.get("propose_due"):
                continue
            # 1.0.140: B is asked ONLY when the issue gate says so (raised, no B answer
            # for this raise yet, weekly cap) — never for benign/processed/self issues.
            fp = row.get("issue_fp") or ""
            ok = await gate.b_query_ok(fp) if (gate is not None and fp) else None
            if not ok:
                if ok is False:                          # an explicit no: stop asking for this row
                    row["propose_due"] = False
                continue
            try:                                         # the fleet is busy: retry on the next scan
                q = await _loops_get(app, "/api/llm/queue")
                waiting = int(((q or {}).get("counts") or {}).get("waiting") or 0)
            except Exception:                            # noqa: BLE001
                waiting = 0
            if waiting > 0:
                _bslog.info("B proposal deferred: central queue has %d waiting", waiting)
                break
            ctx = await loop.run_in_executor(None, lambda: _bprop.locate_source(row, _propose_roots(row)))
            # B's memory is THIS issue only (issue_memory(fp)); the NotifyBook signature
            # record is the read-through fallback when the toolserver does not answer
            mem = await gate.memory(fp) if gate is not None else None
            priors = _ig.memory_priors(mem) if mem else sg.get("proposals")
            msgs = _bprop.build_messages(row, ctx, sg, memory=mem)
            calls += 1
            text, tokens, err = await loop.run_in_executor(None, _b_propose_call, msgs)
            _meter_tokens(_bprop.TASK, tokens)
            row["propose_due"] = False
            if err:
                _bprop.record(sg, None, "error", why=err)
                book.save()
                continue
            p = _bprop.parse(text)
            sg["issue_fp"] = fp
            if p is None:                                # junk: the finding was already delivered
                _bprop.record(sg, None, "junk", why=(text or "")[:300])
                await gate.record_b(fp, {"why": (text or "")[:300]}, status="junk")
            elif p.get("no_new_fix"):
                _bprop.record(sg, None, "no_new_fix", why=p.get("why"))
                sg["note"] = "B: no new fix — " + (p.get("why") or "")[:300]
                await gate.record_b(fp, {"why": p.get("why")}, status="no_new_fix")
            else:
                dup = _bprop.novelty(p, priors)
                if dup:
                    _bprop.record(sg, p, "duplicate", dup_of=dup.split()[-1])
                    await gate.record_b(fp, dict(p, dup_of=dup.split()[-1]), status="duplicate")
                else:
                    try:
                        await _propose_deliver(app, book, sink, row, sk, p)
                    except Exception as e:               # noqa: BLE001 — keep it; deliver next scan
                        e_ = _bprop.record(sg, p, "undelivered", why=str(e)[:200])
                        e_["payload"] = p
            book.save()
    finally:
        _PROPOSE_BUSY = False


async def _bugscan_loop(app):
    try:
        await asyncio.sleep(90)          # let the station settle before the first review
        while True:
            now = time.time()
            for vm, row in list(_bugscan_state().get("loci", {}).items()):
                if not _bugscan_on(row):
                    continue
                if now - float(row.get("last") or 0) < max(60, int(row.get("interval")
                                                                   or BUGSCAN_INTERVAL)):
                    continue
                await _bugscan_run(app, vm)
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        pass


async def _start_bugscan(app):
    _bugscan_locus_state("")             # this station's own row exists from the first boot
    _bugscan_state_save()
    app["bugscan"] = asyncio.create_task(_bugscan_loop(app))


async def _stop_bugscan(app):
    t = app.get("bugscan")
    if t is not None:
        t.cancel()


def _bugscan_rows(vm):
    rows = [r for r in _bugscan_load().values() if (r.get("vm") or "") == (vm or "")]
    rows.sort(key=lambda r: ({"high": 0, "medium": 1, "low": 2}.get(r.get("severity"), 3),
                             -(r.get("ts") or 0)))
    return rows[:100]


# ── findings → B (1.0.124). B's lookup surface is its MCT state dir: the
# hugpy_agent b_lookup classifier reads <workspace>/.hugpy_agent/mct/
# findings.json next to mct.log, so "what's broken" is a LOOKUP (tokens 0,
# model "lookup") on every path — the /b REPL, /api/b/chat's one-shot, and the
# in-process fallback below. The file is the whole current set (bug scan rows
# from every locus this station scans + the loop detector's active loops) plus
# a short history of EMITTED findings (new / count jumped / returned).
B_FINDINGS_NAME = "findings.json"
B_FINDINGS_MAX_AGE = max(600, int(os.environ.get("STATION_B_FINDINGS_MAX_AGE", "21600")))  # 6 h
B_FINDINGS_EMITTED_KEEP = 50
_B_FINDINGS_ASK_RE = re.compile(
    r"what'?s (?:broken|failing|wrong|down|up with)|what is (?:broken|failing|wrong|down)"
    r"|anything (?:broken|failing|wrong|down)|\b(?:errors?|problems?|issues?|findings?|bugs?|bugscan"
    r"|crash(?:es|ing|-?loops?)?|429s?|rate.?limit\w*|tracebacks?|failures?|failing|oom|5xx|health)\b",
    re.I)


def _b_findings_path():
    return Path(_active_ws()) / ".hugpy_agent" / "mct" / B_FINDINGS_NAME


def _b_loop_finding(row):
    """A loop-detector row in the log_findings shape (kind crash_loop)."""
    return {"key": "loop:" + str(row.get("key") or ""), "kind": "crash_loop",
            "severity": "high" if row.get("severity") == "crit" else "medium",
            "source": "loop:" + str(row.get("source") or ""), "locus": _keeper_locus(),
            "count": int(row.get("count") or 0), "first_seen": row.get("first_seen"),
            "last_seen": row.get("last_seen"), "signature": str(row.get("identity") or "")[:160],
            "sample_lines": [str(row.get("detail") or "")[:240]] if row.get("detail") else [],
            "suggested_action": str(row.get("action") or "")[:400]}


def _b_findings_rows(now=None):
    now = now or time.time()
    out = []
    for r in _bugscan_load().values():
        if not r.get("kind") or now - float(r.get("last_seen") or 0) > B_FINDINGS_MAX_AGE:
            continue                           # pre-1.0.124 model rows / stale
        f = {k: r.get(k) for k in ("kind", "severity", "source", "locus", "count", "first_seen",
                                   "last_seen", "sample_lines", "signature", "suggested_action",
                                   "board")}
        f["key"] = r["hash"]
        out.append(f)
    det = _loops()
    if det is not None:
        with contextlib.suppress(Exception):
            out += [_b_loop_finding(r) for r in det.snapshot(now).get("active") or []]
    sev = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda f: (sev.get(f.get("severity"), 3), -float(f.get("last_seen") or 0)))
    return out


def _b_findings_publish(emitted=()):
    """Rewrite B's findings.json (atomic). ``emitted`` rows are appended to its
    short history so B can say what is NEW, not just what exists. Never raises."""
    try:
        p = _b_findings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        old = _read_json(p, {}) if p.exists() else {}
        hist = list((old or {}).get("emitted") or []) if isinstance(old, dict) else []
        now = time.time()
        for r in emitted or ():
            hist.append({"at": int(now), "key": r.get("hash") or r.get("key"),
                         "reason": r.get("emit_reason") or "new", "kind": r.get("kind"),
                         "severity": r.get("severity"), "locus": r.get("locus"),
                         "source": r.get("source"), "count": r.get("count"),
                         "signature": r.get("signature")})
        doc = {"schema": "station.findings.v1", "updated": int(now), "detector": "log_findings",
               "station": _station_id(), "findings": _b_findings_rows(now),
               "emitted": hist[-B_FINDINGS_EMITTED_KEEP:]}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        tmp.replace(p)
    except Exception as e:                        # noqa: BLE001 — B's surface is best-effort
        _bslog.warning("findings → B: %s", e)


def _b_findings_ask(text):
    return bool(_B_FINDINGS_ASK_RE.search(text or ""))


def _b_findings_reply(rows, started):
    """The deterministic answer to "what's broken" — no model, tokens 0."""
    if not rows:
        body = ("No running problems in my findings — the deterministic log scan "
                "(journal, station warnings, seat panes; crash loops, 429s, 5xx, "
                "tracebacks, OOM, refused, timeouts) and the loop detector have "
                "nothing active in the last %d h." % (B_FINDINGS_MAX_AGE // 3600))
    else:
        shown = rows[:12]
        body = "%d finding(s) from the deterministic log scan + loop detector%s:\n%s" % (
            len(rows), (" (showing %d)" % len(shown)) if len(rows) > len(shown) else "",
            "\n".join(_logf.fmt_finding(r, samples=1) if _logf else str(r) for r in shown))
    return f"{body.rstrip()}\n{_b_meta_line(_B_LOOKUP_MODEL, 0, started)}"


def _locus_scope(request):
    """1.0.141: (remote_vm, central_key) for a ?vm= that names ANOTHER locus,
    else ('', ''). Station-process sources (findings, loops, logs) are this
    station's own; for another locus they are filtered to rows that carry
    that locus, never shown as if they were its."""
    vm = (request.query.get("vm") or "").strip()
    if not vm or _is_host_vm(vm):
        return "", ""
    return vm, (_central_locus(vm) or vm)


def _rows_for_locus(rows, key):
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        loc = str(r.get("locus") or "")
        src = str(r.get("source") or "")
        if loc == key or src.endswith("@" + key):
            out.append(r)
    return out


async def api_b_findings(request):
    """GET /api/b/findings — the set B answers "what's broken" from.
    ?vm=<another locus> (1.0.141): only findings tagged with that locus."""
    vm, key = _locus_scope(request)
    rows = _b_findings_rows()
    if vm:
        return web.json_response({"ok": True, "findings": _rows_for_locus(rows, key),
                                  "locus": key, "scope": "this station's detector, filtered to " + key,
                                  "path": str(_b_findings_path())})
    return web.json_response({"ok": True, "findings": rows, "scope": "this station",
                              "path": str(_b_findings_path())})


async def api_bugscan(request):
    """GET /api/bugscan?vm=<locus> — the intermittent local-agent review:
    {on, interval, last, next, running, note, b, source, findings}.
    POST {"vm", "op": "on"|"off"|"now"|"interval"|"clear", "interval"?: seconds}.
    `now` only QUEUES the review (B takes a while) — the next poll shows it."""
    vm = _journal_vm(request)
    if request.method == "POST":
        try:
            b = await request.json()
            assert isinstance(b, dict)
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        vm = (b.get("vm") or "").strip()
        vm = "" if vm == "@keeper" else vm
        op = (b.get("op") or "").strip()
        row = _bugscan_locus_state(vm)
        if not BUGSCAN_ON and op in ("on", "now"):
            return web.json_response({"ok": False, "error": BUGSCAN_OFF_NOTE + " on this station (env wins)"},
                                     status=409)
        if op in ("on", "off"):
            row["on"] = (op == "on")
        elif op == "interval":
            try:
                row["interval"] = max(60, int(b.get("interval") or BUGSCAN_INTERVAL))
            except (TypeError, ValueError):
                return web.json_response({"error": "interval must be seconds"}, status=400)
        elif op == "now":
            row["note"] = "review queued " + time.strftime("%H:%M")
            asyncio.create_task(_bugscan_run(request.app, vm))
        elif op == "clear":
            for h in [h for h, r in _bugscan_load().items() if (r.get("vm") or "") == vm]:
                _BUGSCAN_FINDINGS.pop(h, None)
            with contextlib.suppress(OSError):
                (_bugscan_dir() / "findings.jsonl").write_text(
                    "".join(json.dumps(r) + "\n" for r in _bugscan_load().values()),
                    encoding="utf-8")
            row["note"] = "findings cleared"
        else:
            return web.json_response({"error": "op must be on|off|now|interval|clear"}, status=400)
        _bugscan_state_save()
        audit(request, "bugscan", f"{op} {vm or 'self'}", ok=True)
    row = _bugscan_locus_state(vm)
    src = await _journal_source(vm)
    interval = max(60, int(row.get("interval") or BUGSCAN_INTERVAL))
    return web.json_response({
        "ok": True, "vm": vm, "locus": src.get("locus") or (vm or "this station"),
        "on": _bugscan_on(row), "env_off": not BUGSCAN_ON, "interval": interval,
        "last": int(row.get("last") or 0),
        "next": (int(row["last"]) + interval) if (_bugscan_on(row) and row.get("last")) else 0,
        "running": bool(_BUGSCAN_BUSY.get(vm)),
        "note": (BUGSCAN_OFF_NOTE if not BUGSCAN_ON else "") or row.get("note") or "",
        # the scan is deterministic (no B call) since 1.0.124 — never "skipped"
        "b": True, "detector": "log_findings", "window": BUGSCAN_WINDOW, "board": BUGSCAN_BOARD,
        "source": src, "findings": _bugscan_rows(vm)})


async def _ssh_alive(h):
    """TCP probe only — cheap enough for the dropdown's 5s poll."""
    return await _tcp_open(h["host"], int(h.get("port") or 22), 1.5)


async def _known_names_safe():
    """LXD station names, or an empty set when lxc is unusable here (snap
    home-outside-/home as a service user, lxd absent) — never a 500."""
    try:
        return await known_names()
    except Exception:
        return set()


async def all_locus_names():
    """LXD stations + ssh hosts — what the dropdown may hold."""
    return set(await _known_names_safe()) | set(_ssh_hosts())


def _in_vm(cmd: str, vm: str) -> str:
    """Wrap a NON-seat surface command to run inside a station as the dev user
    — or on an ssh host as its login user. 1.0.141: tmux seats never come
    here (they go through _seat_launch, which writes the script on the
    target); this path is for short commands only."""
    if not vm:            # workbench mode: the models ARE local to this host
        return cmd
    t = _locus_target(vm)
    return LX.pty_wrap(t, ("bash -lc " + shlex.quote(cmd)) if t.kind == LX.KIND_SSH else cmd)


async def api_ssh_hosts(request):
    """GET /api/ssh-hosts — the list (+ live TCP state).
    POST {"op":"add","name","host","user","port","key"} | {"op":"delete","name"} | {"op":"test","name"}."""
    if request.method == "POST":
        try:
            b = await request.json(); assert isinstance(b, dict)
        except Exception:
            return web.json_response({"error": "bad body"}, status=400)
        op = b.get("op"); name = (b.get("name") or "").strip().lower()
        if not _SSH_NAME_RE.match(name):
            return web.json_response({"error": "name: a-z0-9- (lowercase)"}, status=400)
        hosts = _ssh_hosts_local()
        if op == "add":
            host = (b.get("host") or "").strip(); user = (b.get("user") or "").strip() or "root"
            try:
                port = int(b.get("port") or 22)
            except ValueError:
                return web.json_response({"error": "port must be a number"}, status=400)
            key = (b.get("key") or "").strip()
            if not _SSH_HOST_RE.match(host) or not _SSH_USER_RE.match(user) or not (0 < port < 65536):
                return web.json_response({"error": "bad host/user/port"}, status=400)
            if key and not os.path.isfile(os.path.expanduser(key)):
                return web.json_response({"error": f"key file not found on this host: {key}"}, status=400)
            if key and os.path.expanduser(key).endswith(".pub"):
                return web.json_response({"error": "key must be a private SSH key, not its .pub public half"}, status=400)
            if name in await known_names():
                return web.json_response({"error": f"'{name}' is an LXD station here — pick another name"}, status=409)
            hosts[name] = {"host": host, "user": user, "port": port,
                           "key": os.path.expanduser(key) if key else "", "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
            SSH_HOSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SSH_HOSTS_PATH.write_text(json.dumps(hosts, indent=2) + "\n")
            audit(request, "ssh-host-add", f"{name}={user}@{host}:{port}", ok=True)
            # SESSION-PULL-PATCH 2026-09-02: establish the locus centrally so every station sees it
            # (key path stays local — other stations hold their own keys)
            async def _register():
                try:
                    await _ts_call(request.app, "loci/register", {
                        "locus": name, "kind": "station", "endpoint": f"{user}@{host}:{port}",
                        "goal": f"ssh host added on {_station_id()}",
                        "pointer": {"ssh": {"host": host, "user": user, "port": port, "key": ""}}})
                    await _loci_sync_once(request.app)
                except Exception:
                    pass
            asyncio.create_task(_register())
            # Fleet Claude OAuth token (claude-oauth-sync): a new ssh host gets
            # it like a new VM does — best effort, never blocks the add.
            if CLAUDE_OAUTH_TOKEN_PATH.is_file() and CLAUDE_OAUTH_SYNC_BIN:
                asyncio.create_task(_run(CLAUDE_OAUTH_SYNC_BIN, "--ssh", name))
            # Install the seat CLIs on the new locus so both frontier backends
            # work (claude-code + mct); a hand-added ssh host is not provisioned
            # like a VM. Best effort, long timeout, never blocks the add.
            asyncio.create_task(_locus_run(name, _SEAT_CLI_PROVISION_SH, timeout=900))
        elif op == "delete":
            distributed = name in _TS_LOCI["hosts"]
            if hosts.pop(name, None) is None and not distributed:
                return web.json_response({"error": "no such ssh host"}, status=404)
            SSH_HOSTS_PATH.write_text(json.dumps(hosts, indent=2) + "\n")
            if distributed:
                # SESSION-PULL-PATCH 2026-09-02: retire it centrally too, else the next sync brings it back
                try:
                    await _ts_call(request.app, "loci/archive", {"locus": name})
                except Exception as e:
                    return web.json_response({"error": f"toolserver refused archive: {e}"}, status=502)
                _TS_LOCI["hosts"].pop(name, None)
            audit(request, "ssh-host-delete", name + (" (toolserver)" if distributed else ""), ok=True)
        elif op == "test":
            if name not in _ssh_hosts():   # SESSION-PULL-FIX1: local OR distributed
                return web.json_response({"error": "no such ssh host"}, status=404)
            rc, out, err = await _locus_run(name, "echo user=$(id -un); echo host=$(hostname); "
                                                "command -v claude >/dev/null 2>&1 && echo claude=1 || echo claude=0; "
                                                "command -v hugpy-agent >/dev/null 2>&1 && echo agent=1 || echo agent=0; "
                                                "sudo -n true 2>/dev/null && echo sudo=nopasswd || echo sudo=password-or-none")
            return web.json_response({"ok": rc == 0, "name": name, "rc": rc, "out": out.strip(), "err": err.strip()[:400],
                                      "hint": "" if rc == 0 else "key auth failed or host unreachable — select an already-authorized private key; first-time key enrollment needs console or other out-of-band access"})
        else:
            return web.json_response({"error": "op must be add|delete|test"}, status=400)
    hosts = _ssh_hosts()
    rows = []
    for n, h in sorted(hosts.items()):
        rows.append({"name": n, "kind": "ssh", "host": h["host"], "user": h.get("user", "root"),
                     "port": h.get("port", 22), "key": h.get("key", ""), "added": h.get("added", ""),
                     "source": h.get("source", "local"), "goal": h.get("goal", ""),
                     "state": "RUNNING" if await _ssh_alive(h) else "UNREACHABLE",
                     "command": _ssh_shell_cmd(h)})
    return web.json_response({"ok": True, "hosts": rows, "path": str(SSH_HOSTS_PATH),
                              "toolserver": {"url": TS_UPSTREAM, "ts": _TS_LOCI["ts"],
                                             "error": _TS_LOCI["error"],
                                             "distributed": sorted(_TS_LOCI["hosts"])}})


def _locus_home(user):
    """A locus user's home on THIS host (finder is host-native; these loci are
    local users here). Falls back to /home/<user> for an unknown/remote user."""
    import pwd
    try:
        return pwd.getpwnam((user or "").strip()).pw_dir
    except (KeyError, TypeError):
        return "/home/" + ((user or "op").strip() or "op")


async def api_vms_merged(request):
    """GET /api/vms — the sidecar's LXD rows + ssh hosts (kind=ssh), so the
    dropdown and tab strip hold every locus."""
    resp = await api_proxy(request)
    rows = []
    try:
        if resp.status == 200:
            rows = json.loads(resp.body.decode()).get("vms", [])
    except Exception:
        rows = []
    for n, h in sorted(_ssh_hosts().items()):
        rows.append({"name": n, "kind": "ssh", "host": h["host"], "user": h.get("user", "root"),
                     "home": _locus_home(h.get("user", "root")),
                     "source": h.get("source", "local"),
                     "state": "RUNNING" if await _ssh_alive(h) else "UNREACHABLE"})
    hidden = _hidden_loci()
    rows = [r for r in rows if r.get("name") not in hidden]     # ◌ hidden loci never reach the strip
    return web.json_response({"vms": rows, "hidden": sorted(hidden)})


# mct turn IDLE timeout, forwarded to `abstract-claude mct` so the station stays
# in control even if the package is reinstalled to its own default. This is a
# no-output (hung-turn) cap, NOT a wall-clock cap: a healthy streaming turn is
# never killed. Operator-tunable via $MCT_TURN_IDLE_TIMEOUT (seconds); default
# 1800s (30 min of silence).
try:
    _MCT_IDLE_TIMEOUT = int((os.environ.get("MCT_TURN_IDLE_TIMEOUT") or "").strip() or 0)
except ValueError:
    _MCT_IDLE_TIMEOUT = 0
MCT_TURN_IDLE_TIMEOUT = _MCT_IDLE_TIMEOUT if _MCT_IDLE_TIMEOUT > 0 else 1800

# Backend commands are stored RAW and wrapped per-request (_in_vm) with the
# grounding VM resolved from the SPA's active VM (else the static pin above).
TERM_SURFACES = {
    "local":    {"default": "opencode",
                 "backends": {"opencode":  "hugpy-agent console --frontend opencode",
                              "qwen-code": "hugpy-agent console --frontend qwen-code"}},
    # MCT is the default frontier backend. Claude Code, Codex, and the native
    # Hugpy agent remain independently selectable frontier backends. Hugpy is
    # A's direct fleet runtime, not B's local-agent wrapper.
    "frontier": {"default": "mct",
                 # Claude backends (ChatGPT/Codex added 2026-09-14):
                 # claude-code IS the frontier seat, and mct is the
                 # pointer-exchange method OVER it (PROMOTED from "mct2",
                 # 2026-08-27: plain-file C→B→A exchange). mct runs WITHIN the
                 # locus the seat is attributed to — the launch block pushes the
                 # script there and swaps the host path for the locus copy. Impl
                 # files/paths keep the mct2 name (mct2_repl.py,
                 # ~/.config/hugpy-station/mct2, /api/mct2/*); the BACKEND key is
                 # mct. The original broker MCT (mct-deprecated, `hugpy-agent
                 # mct`) and the clawd-code stub were retired from the picker.
                 "backends": {"mct":         f"abstract-claude mct {{ws}} --timeout {MCT_TURN_IDLE_TIMEOUT}",
                              "claude-code": "claude",
                              "codex": "codex",
                 "hugpy": "hugpy-agent harness"}},
    # The shell surface's transports: exec (lxc exec, the default — works
    # even where the network path doesn't) and ssh (a real sshd login as the
    # dev user; vm-new enables sshd and seeds the operator's key). These
    # commands are display/availability stubs — the shell branch below builds
    # the real command line.
    "shell":    {"default": "exec", "backends": {"exec": "lxc", "ssh": "ssh"}},
}

FV_SESSIONS = {}   # surface -> sid of that surface's persistent host session
FV_BACKENDS = {}   # sid -> backend key actually launched/attached for that sid

# ONE keeper per station (operator, 2026-08-21): the model surfaces do not run
# their own process any more — they ATTACH to the station's persistent tmux
# session on the `console` socket (`keeper` = frontier/A, `keeper-local` = B),
# creating it only when absent (new-session -A). The timer-started keeper and
# the console-started one are therefore the same process, a backend restart
# reattaches instead of respawning, and B and the "local agent" are one thing.
# EVERY backend is wired to its OWN tmux session, explicitly (operator,
# 2026-08-22). The old single `keeper` name meant `new-session -A` silently
# reattached whichever backend got there first (mct vs claude-code), and a
# backend switch killed the other backend's seat. This table is the single
# source of truth for the console; the packaged in-VM stack
# (station-stack/opt/station-keeper/keeper-ensure.sh, deployed by
# bin/station-stack-install) creates `keeper-mct` from the SAME naming — keep
# the two in sync. A backend with no row here runs unpersisted.
BACKEND_TMUX_SESSION = {
    # MCT is a communication method, so it deliberately has no tmux session.
    # It reuses whichever native frontier seat is selected (Claude Code/Codex).
    "frontier": {"claude-code": "keeper-claude",
                 "codex": "keeper-codex",
                 "hugpy": "keeper-hugpy"},
    "local":    {"opencode": "keeper-local-opencode",
                 "qwen-code": "keeper-local-qwen"},
}
KEEPER_TMUX_SOCK = "console"


def _tmux_session_for(surface: str, backend: str) -> str:
    """tmux session name for (surface, backend) — '' when not persisted."""
    spec = TERM_SURFACES.get(surface) or {}
    b = backend if backend in (spec.get("backends") or {}) else spec.get("default", "")
    return (BACKEND_TMUX_SESSION.get(surface) or {}).get(b, "")


def _frontier_live_session():
    """The live frontier TermSession (active mct session first, then any
    frontier seat), or None. Replaces the old bare FV_SESSIONS["frontier"]
    alias, which was set only on spawn and went stale on reattach/VM switch."""
    for prefix in (_surface_key("frontier", MODEL_VM, _active_session()),
                   _surface_key("frontier", "", _active_session())):
        sess = _live_surface_session(prefix)
        if sess is not None:
            return sess
    for k in list(FV_SESSIONS):
        if k.startswith("frontier:"):
            sess = SESSIONS.get(FV_SESSIONS.get(k) or "")
            if sess is not None and getattr(sess, "alive", False):
                return sess
    return None
_TMUX_OPTS = ("set -g status off \\; set -g prefix None \\; set -g prefix2 None \\; "
              "set -g escape-time 0 \\; set -g mouse off \\; "
              # t-scroll (2026-09-19): the seat TUIs (claude-code, opencode) hold
              # the ALTERNATE screen, where tmux keeps ZERO scrollback — copy-mode
              # opened at [0/0] and the wheel ctl ("scroll" above) was a no-op, so
              # the operator had NO scrollback path at all. Routing that output
              # through the MAIN screen accumulates it in history, which the
              # existing wheel -> copy-mode path then scrolls. mouse stays OFF:
              # browser-native selection and the right-click/chip paste need it.
              "set -g alternate-screen off \\; set -g history-limit 20000 \\; "
              "set -g destroy-unattached off \\; set -g window-size latest \\; "
              "setw -g aggressive-resize on \\; ")


_SCOPE_OK = None
_IO_DELEGATED = None


def _io_delegated():
    """True when the cgroup v2 `io` controller is delegated to this user manager.
    Without it systemd accepts IOWeight= but never applies it, so we only pass the
    property when it can actually bite (2026-09-16: this host delegates cpu/memory/
    pids only — the IO guard rides on ionice instead)."""
    global _IO_DELEGATED
    if _IO_DELEGATED is None:
        try:
            with open(f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/"
                      f"user@{os.getuid()}.service/cgroup.controllers") as fh:
                _IO_DELEGATED = "io" in fh.read().split()
        except Exception:
            _IO_DELEGATED = False
    return _IO_DELEGATED


# Every seat's tmux server (and therefore EVERY child a seat or its subagents
# spawn — ugrep/bfs/rg included) starts de-prioritised: nice 10 + ionice idle
# class. 2026-09-16: an orphaned `ugrep -rn / ` in a dead seat scope read ~854 GB
# over 10 h and drove load to ~30, stalling a build. Deny-globs in the seat
# settings are evadable; the scheduler class is not — it is inherited by fork.
_NICE_PREFIX = ["/usr/bin/nice", "-n", "10", "/usr/bin/ionice", "-c", "3"]


def _scope_prefix():
    """1.0.63: start the seats' tmux server in its OWN systemd user scope, outside
    this backend's cgroup — restarting the station unit used to kill every seat
    (KillMode=control-group took the tmux server with it; ae 2026-09-02). Probed
    once; empty when systemd-run --user is unavailable (plain tmux as before).
    1.0.90: the scope carries CPUWeight=50 (and IOWeight=20 where the io
    controller is delegated), and the command is always nice/ionice-wrapped."""
    global _SCOPE_OK
    if _SCOPE_OK is None:
        _SCOPE_OK = False
        if shutil.which("systemd-run") and (os.environ.get("XDG_RUNTIME_DIR")
                                            or os.path.isdir(f"/run/user/{os.getuid()}")):
            os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            try:
                r = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect",
                                    "true"], capture_output=True, timeout=10)
                _SCOPE_OK = r.returncode == 0
            except Exception:
                _SCOPE_OK = False
    if not _SCOPE_OK:
        return list(_NICE_PREFIX)
    pre = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", "CPUWeight=50"]
    if _io_delegated():
        pre += ["-p", "IOWeight=20"]
    return pre + _NICE_PREFIX


def _shell_persist(name: str, inner: str, scope: bool = False) -> str:
    """Run `inner` inside a persistent tmux session on the dedicated `shell`
    socket (survives detach + a station restart — an actual terminal in the
    locus, like the keeper seats), or run it bare when the locus has no tmux.
    `name` is the stable reattach id. `scope` wraps the tmux server in its own
    systemd scope (HOST only) so KillMode=control-group can't take it on a
    station restart; VM/ssh loci already live in their own cgroup/host."""
    pre = (" ".join(_scope_prefix()) + " ") if scope else ""
    t = (pre + "tmux -L shell " + _TMUX_OPTS + "new-session -A -s "
         + shlex.quote(name) + " " + shlex.quote(inner))
    return ("command -v tmux >/dev/null 2>&1 && exec " + t
            + " || exec bash -lc " + shlex.quote(inner))


def _locus_target(vm):
    """1.0.141: the ONE place a wire locus becomes an execution target.
    host tokens / this station's own locus / an ssh pointer at this very
    user@host -> self (no hop); a registered ssh host -> ssh over the mux; an
    LXD guest -> lxc exec as ubuntu. Every seat action (launch, tmux keys,
    config files, serve discovery) runs the SAME script through it."""
    v = (vm or "").strip()
    if not v or _is_host_vm(v):
        return LX.Target(LX.KIND_SELF, "", _station_locus())
    h = _ssh_host(v)
    if h:
        port = int(h.get("port") or 22)
        if _is_self_target(f"{h.get('user') or 'root'}@{h.get('host')}:{port}"):
            return LX.Target(LX.KIND_SELF, "", _station_locus())
        return LX.Target(LX.KIND_SSH, v, _central_locus(v) or v,
                         ssh_argv=_ssh_argv(h), ssh_tty=_ssh_shell_cmd(h))
    return LX.Target(LX.KIND_LXC, v, _central_locus(v) or v)


def _host_cfg_overrides():
    """The host's guidance lives in the ACTIVE mct workspace; tell the snapshot
    reader where (relative to the state dir) so the self target composes the
    same text the host always did."""
    try:
        rel = _bg_user_path().resolve().relative_to(FV_STATE_HOME.resolve())
        return {"guidance": str(rel)}
    except Exception:                                    # noqa: BLE001
        return {}


async def _seat_cfg(target, runner=None):
    """The target locus's seat-config snapshot, read ON the target by the same
    script for every kind (locus_exec.read_cfg). Raises LX.LocusError."""
    cfg = await LX.read_cfg(target, runner=runner,
                            overrides=_host_cfg_overrides() if not target.remote else None)
    cfg["remote"] = target.remote
    cfg["locus"] = target.locus
    return cfg


# The station-process env a seat needs, exported at the top of every host
# launch script (tmux's server env is whatever the FIRST session brought).
_SEAT_ENV_KEYS = ("HUGPY_URL", "HUGPY_BASE", "HUGPY_API_KEY", "HUGPY_OPERATOR_TOKEN",
                  "TOOLSERVER_URL", "TOOLSERVER_TOKEN", "TOOLSERVER_OPERATOR_TOKEN",
                  "STATION_CONSOLE_TOOLSERVER", "STATION_CONSOLE_TOOLSERVER_TOKEN",
                  "EXCHANGE_LOCUS", "HUGPY_STATION_STATE")
_LOCAL_HUGPY_FRONT = "http://127.0.0.1:7002"   # gunicorn front; nginx 500s on >~10 KB bodies


def _port_open(port, host="127.0.0.1", timeout=0.3):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _seat_env_block(surface="", target=None, cfg=None):
    """The env prelude at the top of EVERY seat script (1.0.141: identical on
    every target). On the target it resolves its own station state dir and
    imports the seat keys from ITS env files; the self target then pins the
    station process's own values on top (that IS the target's station)."""
    target = target or LX.Target(LX.KIND_SELF, "", _station_locus())
    keys = " ".join(_SEAT_ENV_KEYS + ("STATION_LOCUS",))
    lines = ["# seat env (hugpy-station 1.0.141 — same prelude on every locus)",
             LX.STATE_SH.rstrip("\n"),
             'export HUGPY_STATION_STATE="$S"',
             '__hs_env() { for __f in "$S/env/station.env" "$S/env/toolserver.env" "$S/toolserver.env"; do '
             '[ -r "$__f" ] || continue; ( set -a; . "$__f" >/dev/null 2>&1; '
             'for __k in ' + keys + '; do __v="${!__k:-}"; '
             '[ -n "$__v" ] && printf "export %s=%q\\n" "$__k" "$__v"; done ); done; }',
             'eval "$(__hs_env)"; unset -f __hs_env']
    if not target.remote:
        env = {k: os.environ[k] for k in _SEAT_ENV_KEYS if os.environ.get(k)}
        env["HUGPY_STATION_STATE"] = str(FV_STATE_HOME)
        if surface == "local" and _port_open(7002):
            env["HUGPY_BASE"] = _LOCAL_HUGPY_FRONT      # opencode's ~84 KB requests need the local front
        lines += [f"export {k}={shlex.quote(v)}" for k, v in env.items() if v]
    loc = target.locus or ""
    if loc:
        lines.append(f'export EXCHANGE_LOCUS="${{EXCHANGE_LOCUS:-{loc}}}" HUGPY_LOCUS="${{HUGPY_LOCUS:-{loc}}}"')
    lines += ['[ -z "${TOOLSERVER_URL:-}" ] && [ -n "${STATION_CONSOLE_TOOLSERVER:-}" ] && export TOOLSERVER_URL="$STATION_CONSOLE_TOOLSERVER"',
              '[ -z "${TOOLSERVER_TOKEN:-}" ] && [ -n "${STATION_CONSOLE_TOOLSERVER_TOKEN:-}" ] && export TOOLSERVER_TOKEN="$STATION_CONSOLE_TOOLSERVER_TOKEN"',
              'true']
    return "\n".join(lines) + "\n"


def _seat_launch_body(sess, cmd, target, cfg=None, surface=""):
    """env prelude + (claude-code seat: its seat-report sidecar) + the seat
    command — the text parked in the target's launch script."""
    if sess == "keeper-claude":
        # t240: the sidecar reports the REAL model the seat command resolved.
        mm = re.search(r" --model (\S+)", cmd)
        real_model = (mm.group(1).strip("'\"") if mm else "") or _frontier_models(cfg).get("claude-code", "")
        model_arg = (" --model " + shlex.quote(real_model)) if real_model else ""
        cmd = ("(command -v abstract-claude >/dev/null 2>&1 && abstract-claude seat-report "
               "--seat claude-code --watch-pid $$" + model_arg + " >/dev/null 2>&1 &); " + cmd)
    return _seat_env_block(surface, target, cfg) + cmd


async def _seat_launch(target, sess, cmd, cfg=None, surface="", runner=None):
    """The unified launcher: script ON the target, then the same short
    `tmux -L console new-session -A -s <sess> "bash <file>"` everywhere.
    -> (pty command, script path). Raises LX.LocusError."""
    body = _seat_launch_body(sess, cmd, target, cfg, surface)
    pre = " ".join(_scope_prefix()) if not target.remote else ""
    return await LX.seat_launch(target, sess, body, runner=runner, pre=pre, opts=_TMUX_OPTS)


async def _tmux_on(vm, *args, timeout=15):
    """`tmux -L console <args>` on the locus's own console socket (1.0.141:
    unstick / paste / show / live /model act on the SELECTED locus)."""
    return await LX.tmux(_locus_target(vm), list(args), timeout=timeout)


def _surface_key(surface: str, ground_vm: str, session: str = "", backend: str = "") -> str:
    """Key of a model surface's PTY record. Includes the BACKEND (#mct,
    #claude-code, …) so each backend owns its own record — without it a
    reattach silently handed the caller whichever backend was already up."""
    key = ("frontier:" + session) if surface == "frontier" else surface
    if surface != "shell" and ground_vm:
        key += "@" + ground_vm
    if surface != "shell" and backend:
        key += "#" + backend
    return key


def _live_surface_session(prefix: str):
    """First live TermSession whose key is `prefix` or `prefix#<backend>`."""
    for k, sid in list(FV_SESSIONS.items()):
        if k == prefix or k.startswith(prefix + "#"):
            sess = SESSIONS.get(sid or "")
            if sess is not None and getattr(sess, "alive", False):
                return sess
    return None


async def _vm_ip(name):
    """First ROUTABLE IPv4 of a station (lxc list -c4 csv), '' when it has
    none yet. A guest running docker reports its bridge addresses
    (172.17/172.18) first — those are routes INSIDE the guest, not to it,
    so prefer any other address before falling back."""
    rc, out, _err = await _run("lxc", "list", name, "-c", "4", "--format", "csv")
    if rc != 0:
        return ""
    cands = []
    for line in out.splitlines():
        for part in line.strip('"').split(","):
            tok = part.strip().split(" ")[0].strip()
            if tok:
                cands.append(tok)
    for ip in cands:
        if not ip.startswith(("172.17.", "172.18.")):
            return ip
    return cands[0] if cands else ""


async def _vm_probe(vm, names):
    """ONE exec in the locus as its seat user -> (reached, {name: bool}, who, err).
    `who` is `user@host` exactly as the locus reports itself (the ⌂ shell label);
    reached=False (rc != 0: ssh refused/unreachable, lxc unusable) is what
    disables the ⌂ shell button, with `err` as the reason."""
    probe = "; ".join(
        [f'if command -v {n} >/dev/null 2>&1 || [ -x "$HOME/.local/bin/{n}" ]; '
         f'then echo {n}=1; else echo {n}=0; fi' for n in names]
        + ['echo "__who=$(id -un)@$(hostname -s 2>/dev/null || hostname)"'])
    rc, out, err = await _locus_run(vm, probe, 20)
    if rc != 0:
        return False, {n: False for n in names}, "", (err or out or f"rc={rc}").strip()[-300:]
    got = dict(line.split("=", 1) for line in out.split() if "=" in line)
    return True, {n: got.get(n) == "1" for n in names}, got.get("__who", ""), ""


async def _vm_which(vm, names):
    """{name: bool} — which of these binaries exist in the VM for the dev user
    (PATH + ~/.local/bin, where vm-new seeds claude/hugpy-agent). One exec."""
    return (await _vm_probe(vm, names))[1]


def _self_who():
    """user@host of THIS station's service user (the host locus's shell user)."""
    return getpass.getuser() + "@" + (socket.gethostname().split(".")[0] or "localhost")


def _shell_plan(vm):
    """1.0.143 ⌂ shell: where the SHELL surface of locus `vm` runs and as whom.
    Resolved by the 1.0.141 transport (_locus_target), so the host tokens, this
    station's own locus name and an ssh pointer at this very user@host all give
    the HOST shell (the service user, no ssh hop). `vm` is the already-validated
    wire locus ('' / '@keeper' / a dropdown name). -> dict:
      kind  host | ssh | lxc
      key   the PTY record key (surf_key) — one persistent shell per locus
      who   static user@host label (the probe's own `id -un@hostname` wins)
      ssh   the interactive ssh login (kind ssh only)"""
    t = _locus_target("" if vm == "@keeper" else vm)
    if t.kind == LX.KIND_SELF:
        return {"kind": "host", "key": "shell:@keeper", "who": _self_who(), "ssh": ""}
    if t.kind == LX.KIND_SSH:
        h = _ssh_host(vm) or {}
        return {"kind": "ssh", "key": "shell-ssh:" + vm,
                "who": f"{h.get('user') or 'root'}@{h.get('host') or vm}", "ssh": t.ssh_tty}
    return {"kind": "lxc", "key": "shell:" + vm, "who": f"{t.lxc_user}@{vm}", "ssh": ""}


async def term_backends(request):
    """GET /api/term/backends[?vm=<name>] — the surface/backend whitelist plus
    whether each backend's binary is actually installed, so the UI can gray out
    the rest. With ?vm= (the SPA's active VM) availability is probed IN that VM
    — the grounding rule means that is where the backend would run. Also
    carries the Frontier Keeper toggle and which surfaces are live."""
    req_vm = (request.query.get("vm") or "").strip()
    # @keeper = the host seat: probe HOST availability (that is where its
    # backends actually run — the old fold to MODEL_VM probed the wrong box).
    loci = await all_locus_names()
    if req_vm == "@keeper":
        ground_vm = ""
    else:
        ground_vm = (req_vm if (req_vm and req_vm in loci)
                     else MODEL_VM)
    vm_avail = None
    vm_reached, vm_who, vm_err = True, "", ""
    if ground_vm:
        names = sorted({shlex.split(cmd)[0]
                        for s2, spec2 in TERM_SURFACES.items() if s2 != "shell"
                        for cmd in spec2["backends"].values()} | {"tmux"})
        vm_reached, vm_avail, vm_who, vm_err = await _vm_probe(ground_vm, names)
    out = {}
    for s, spec in TERM_SURFACES.items():
        sess = _live_surface_session(_surface_key(s, ground_vm, _active_session()))
        live = bool(sess is not None and sess.alive)
        out[s] = {"default": spec["default"], "backends": {}, "live": live,
                  "live_backend": FV_BACKENDS.get(sess.sid, "") if live else ""}
        for key, cmd in spec["backends"].items():
            argv0 = shlex.split(cmd)[0]
            # shell transports run FROM the host (lxc / ssh) — host which();
            # model backends run in the grounding VM — probe there.
            avail = (vm_avail[argv0] if vm_avail is not None and s != "shell"
                     else bool(shutil.which(argv0)))
            # MCT is a communication mode over the selected native frontier
            # terminal, so its availability follows that native terminal.
            if s == "frontier" and key == "mct":
                native_cmds = [spec["backends"].get(n, "") for n in ("claude-code", "codex")]
                native_argvs = [shlex.split(c)[0] for c in native_cmds if c]
                avail = any((vm_avail[a] if vm_avail is not None else bool(shutil.which(a)))
                            for a in native_argvs)
            out[s]["backends"][key] = {"available": avail}
    # 1.0.143 ⌂ shell: WHO the shell surface would be on the selected locus and
    # whether that locus is reachable — the same resolution ws_hostterm uses
    # (_shell_plan), so the button's label is the shell it opens. Reported inside
    # "shell" (a top-level key would become a phantom surface tab).
    if "shell" in out:
        shell_vm = req_vm if (req_vm == "@keeper" or req_vm in loci) else ""
        plan = _shell_plan(shell_vm)
        if plan["kind"] == "host":
            reached, who, why = True, plan["who"], ""
        else:
            if shell_vm == ground_vm:
                reached, who, why = vm_reached, vm_who, vm_err
            else:
                reached, _a, who, why = await _vm_probe(shell_vm, [])
            who = who or plan["who"]
            if not reached:
                why = f"cannot reach {shell_vm} as {plan['who']} ({plan['kind']}): {why or 'no answer'}"
        out["shell"].update({"available": bool(reached), "reason": why, "who": who,
                             "kind": plan["kind"], "locus": shell_vm or "@keeper"})
    out["frontier_enabled"] = _frontier_enabled()
    out["b_enabled"] = _b_enabled()
    # t-serve: which keeper surface LEADS. "serve" = abstract-claude serve at
    # /ac/ (the default, whenever it answers /api/state); the tmux backends stay
    # listed and selectable as explicit, non-default "terminal seat (tmux)"
    # choices — backend_labels is what the picker should show for them.
    # 1.0.101: ?vm=<locus> reports THAT locus's serve (ac-loci.json) as its
    # keeper surface; the host seat and unknown names keep this host's serve.
    surface_now, ac = await _keeper_surface_now(ground_vm if (req_vm and req_vm == ground_vm) else "")
    out["keeper_surface"] = surface_now
    out["ac_serve"] = ac
    if "frontier" in out:
        out["frontier"]["keeper_surface"] = surface_now
        out["frontier"]["serve"] = ac
        # 1.0.139: the tmux seat needs tmux on the box that runs it (this host, or
        # the grounding VM). Reported inside "frontier" — a top-level key would
        # become a phantom surface tab — so the UI disables the tmux button with
        # the reason instead of offering a seat that cannot start.
        t_ok = (vm_avail.get("tmux", False) if vm_avail is not None else _tmux_available())
        out["frontier"]["tmux"] = {"available": bool(t_ok),
                                   "reason": "" if t_ok else TMUX_MISSING_MSG}
        labels = dict(TMUX_SEAT_LABELS)
        # t296: the picker lists the serve console as its FIRST frontier entry.
        # It is a surface, not a TERM_SURFACES backend (nothing spawns it as a
        # PTY command), so it appears only as a label + the frontier.serve block
        # above — never in frontier.backends.
        labels["serve"] = ac.get("label") or "Serve console"
        out["frontier"]["backend_labels"] = labels
        # 1.0.144: `live` above is THIS station's attached PTY — not whether the
        # locus's seats exist. The seats themselves live on the locus's own
        # console socket (/tmp/tmux-<its uid>/console), so list them THERE, as
        # that user, through the locus transport: hugpy's keeper-claude /
        # keeper-codex / keeper-hugpy read live=False while running.
        s_ok, seats, s_err = await _locus_seats(ground_vm if (req_vm and req_vm == ground_vm) else "")
        names = {x["name"] for x in seats}
        sb = {b: bool(_tmux_session_for("frontier", b)) and _tmux_session_for("frontier", b) in names
              for b in out["frontier"]["backends"]}
        out["frontier"].update(seats=seats, seats_ok=s_ok, seats_error=s_err, seat_backends=sb,
                               seat_live=any(sb.values()))
    if ground_vm:
        out["grounded_in"] = ground_vm
    return web.json_response(out)


async def ws_hostterm(request):
    """A plain PTY on THIS host (not a VM). The client picks a surface
    (local | frontier | shell) and a backend key; TERM_SURFACES maps that to a
    command. Missing binaries fall back to a plain login shell ("basic first").
    Backs the in-app terminal page."""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    try:
        init_rows = int(request.query.get("rows", "")) or None
        init_cols = int(request.query.get("cols", "")) or None
    except ValueError:
        init_rows = init_cols = None
    # Resolve surface+backend against the TERM_SURFACES whitelist; unknown
    # values collapse to the surface default (or the shell). The frontier
    # default still honors $FLEETVIEW_TERM_CMD as an env override, preserving
    # the launcher's historic knob. If the command exits, we drop back to a
    # shell rather than closing the terminal out from under the user.
    fallback = os.environ.get("SHELL") or "/bin/bash"
    launch = f"exec {shlex.quote(fallback)} -i"
    surface = (request.query.get("surface") or "frontier").strip().lower()
    backend = (request.query.get("backend") or "").strip().lower()
    if surface not in TERM_SURFACES:
        surface = "frontier"
    spec = TERM_SURFACES[surface]
    if backend not in spec.get("backends", {}):
        backend = spec.get("default", "")
    # MCT is a communication method over the selected frontier model. Its
    # native terminal remains the same persistent seat (Claude Code or Codex).
    native = (request.query.get("native") or "claude-code").strip().lower()
    if native not in ("claude-code", "codex"):
        native = "claude-code"
    launch_backend = native if surface == "frontier" and backend == "mct" else backend
    # The SPA's active VM: every model surface follows it (grounding rule —
    # each VM's A/B/local are ITS OWN, living and grounding in that VM).
    # "@keeper" is the shell surface's host-keeper seat; unknown names blank.
    req_vm = (request.query.get("vm") or "").strip()
    if req_vm and req_vm != "@keeper" and req_vm not in await all_locus_names():
        req_vm = ""
    # Grounding: an ACTIVE VM wins — its own A/B/local, seated in that VM by
    # vm-new. No VM active grounds on the host, unless the site pinned a
    # static model VM (MODEL_VM, the fallback). The @keeper host seat is the
    # fleet's ONE deliberately host-grounded set — it must NOT fall through
    # to the static pin, or the keeper's frontier becomes the model VM's.
    ground_vm = "" if req_vm == "@keeper" else (req_vm or MODEL_VM)
    session = (request.query.get("session") or "").strip()
    if surface == "frontier":
        if not session or not _SESSION_RE.match(session):
            session = _active_session()
        mct_ws = _session_ws(session)
        _set_active_session(session)   # drawers (todo/guidance/chat) follow the terminal
        surf_key = "frontier:" + session
    else:
        session = ""
        surf_key = surface
    # A model surface grounded in a VM is its OWN persistent session — each
    # station keeps its A/local scrollback; the host seat keeps the bare key.
    if surface != "shell" and ground_vm:
        surf_key += "@" + ground_vm
    if surface != "shell":
        surf_key += "#" + (launch_backend or spec.get("default", ""))
    # shell instances (t5): ?inst=N (N>=2) is its OWN PTY record; inst 1 keeps
    # the bare key so existing sessions/URLs are unchanged.
    try:
        inst = int(request.query.get("inst") or 1)
    except ValueError:
        inst = 1
    inst = inst if surface == "shell" and 2 <= inst <= 32 else 1
    if surface == "shell":
        # HOST-LOCAL PATCH: the shell surface follows the SPA's active VM
        # (?vm=<name>) — it opens `station shell` INSIDE that VM as the dev
        # user, one persistent session per VM, so a "shell" is never
        # ambiguously the host. Only with no VM in context (or an unknown
        # name) does it remain the host keeper seat (vm_mgr, deliberate).
        term_cmd = ""
        vm = req_vm
        sh_inner = ""; sh_kind = ""; sh_ssh = ""   # persist: the shell to run IN the locus
        tmux_name = (request.query.get("tmux") or "").strip().lower()
        if tmux_name and _PULL_NAME_RE.match(tmux_name):
            # SESSION-PULL-PATCH 2026-09-02: a pulled / jump-in seat — ATTACH to its tmux session on
            # the keeper socket (the seat itself lives on, detached, when the
            # pane closes). Host-side by definition: the seat was spawned here.
            term_cmd = (f"tmux -L {KEEPER_TMUX_SOCK} " + _TMUX_OPTS
                        + "attach -t " + shlex.quote("=" + tmux_name))
            surf_key = "shell-tmux:" + tmux_name
        elif (plan := _shell_plan(vm))["kind"] == "host":
            # 1.0.41b (operator): the SHELL surface is a shell — a plain login
            # shell on THIS host as the service user (the host's own seat).
            # Claude on the host is the frontier surface's claude-code backend
            # (host-grounded), not something the shell tab silently starts.
            # 1.0.143 (⌂ shell): every host spelling — no vm, @keeper, @self,
            # this station's own locus name, an ssh pointer at this very
            # user@host — is THIS persistent tmux shell (_locus_target).
            sh_inner = "exec bash -l"; sh_kind = "host"
            surf_key = plan["key"]
        elif plan["kind"] == "ssh":
            # ⇅ ssh host: the shell IS ssh (interactive — password login allowed)
            sh_inner = "exec bash -l"; sh_kind = "ssh"; sh_ssh = plan["ssh"]
            surf_key = plan["key"]
        elif vm and vm in await known_names():
            if backend == "ssh":
                # A real sshd login as the dev user (vm-new enables sshd and
                # seeds the keys). Lab host-key policy: stations are rebuilt
                # freely and regenerate host keys — pinning would wedge every
                # rebuild, so known-hosts checking is off here. When the
                # FLEET KEY exists (minted by vm-new's first build), pin to
                # it (IdentitiesOnly): passphraseless by design, so the
                # in-app login never prompts and never trips a locked agent.
                ip = await _vm_ip(vm)
                if ip:
                    fleet_key = FV_STATE_HOME / "fleet_ssh_key"
                    ident = (f"-o IdentitiesOnly=yes -i {shlex.quote(str(fleet_key))} "
                             if fleet_key.is_file() else "")
                    sh_ssh = ("ssh -t -o StrictHostKeyChecking=no "
                              "-o UserKnownHostsFile=/dev/null "
                              f"-o LogLevel=ERROR {ident}ubuntu@{ip}")
                    sh_inner = "exec bash -l"; sh_kind = "ssh"
                    surf_key = "shell-ssh:" + vm
            if not sh_kind:
                sh_inner = ("command -v station >/dev/null 2>&1 && exec station shell"
                            "; exec bash -l")
                sh_kind = "lxc"
                surf_key = "shell:" + vm
        if inst > 1:
            surf_key += "#" + str(inst)
        # PERSIST (2026-09-18): back the shell with tmux IN THE LOCUS on a
        # dedicated `shell` socket so it survives detach AND a station restart
        # — an actual terminal in the locus, like the keeper seats. Bare-shell
        # fallback when the locus lacks tmux. The pulled `shell-tmux:` case is
        # already a tmux attach; left as-is. One shared session per (locus,
        # inst): reopening reattaches; extra shells come from inst (2..32).
        if not term_cmd and sh_inner:
            shname = "sh-" + (re.sub(r"[^A-Za-z0-9]+", "-",
                              surf_key.split(":", 1)[-1]).strip("-") or "x")
            persist = _shell_persist(shname, sh_inner, scope=(sh_kind == "host"))
            if sh_kind == "host":
                term_cmd = "bash -lc " + shlex.quote(persist)
            elif sh_kind == "lxc":
                term_cmd = (f"lxc exec {shlex.quote(vm)} -- sudo -u ubuntu -H bash -lc "
                            + shlex.quote(persist))
            elif sh_kind == "ssh":
                term_cmd = sh_ssh + " bash -lc " + shlex.quote(shlex.quote(persist))
    else:
        term_cmd = (spec["backends"].get(launch_backend)
                    or spec["backends"].get(spec["default"], ""))
        # $FLEETVIEW_TERM_CMD is the launcher's historic HOST knob — it only
        # applies to host-grounded frontier launches, never inside a VM.
        if (surface == "frontier" and not ground_vm
                and backend in ("", spec["default"])):
            # 1.0.85: the pre-1.0.85 launcher exported the legacy default
            # "hugpy-agent mct" (the DEPRECATED broker) unconditionally, which
            # replaced claude-code on every host seat while the UI still said
            # claude-code. That value is ignored here; an explicit operator
            # override ("claude" or any other command) is still honoured.
            _fv = os.environ.get("FLEETVIEW_TERM_CMD", "").strip()
            if _fv == "hugpy-agent mct":
                _fv = ""
            term_cmd = _fv or term_cmd
        # 1.0.141: ONE launch path for every locus. The target's OWN seat
        # config (directive, models, switches, template, handoff) is read ON
        # the target; the seat script is written ON the target; tmux only
        # ever sees `bash <file>` (no payload in any argv — the "command too
        # long" bug); local and remote differ only in the transport.
        target = _locus_target(ground_vm)
        cfg = None
        _live = SESSIONS.get(FV_SESSIONS.get(surf_key) or "")
        _reattach = _live is not None and getattr(_live, "alive", False)
        if surface == "frontier" and not _reattach:
            try:
                cfg = await _seat_cfg(target)
            except LX.LocusError as e:
                if target.remote:
                    cfg_err = str(e)
                    await ws.send_str(json.dumps({"t": "error", "msg":
                        f"seat config of {req_vm} unreadable — not launching with another locus's directive: {cfg_err[:300]}"}))
                    await ws.close()
                    return ws
                cfg = None          # host: its own python reads are the same files
            # 1.0.146: the handoff layer rides in from the toolserver, not a file
            await _handoff_layer_refresh(request.app, target.locus)
        if term_cmd == "claude":   # Claude Code = the fresh templated A seat
            # bash -lc so the seat prelude runs under bash wherever it lands.
            inner = "bash -lc " + shlex.quote(_claude_seat_cmd(
                "frontier", "mct" if (surface == "frontier" and backend == "mct") else "claude-code", cfg))
        elif term_cmd == "codex":
            # Keep the locus user's Codex auth/config and native model picker.
            # Pass standing guidance as configuration, without submitting a turn.
            # 1.0.143: the tmux-seat directive (+ guidance, overlay, handoff) —
            # the same composition as the claude-code seat, minus its switches.
            guidance = _session_directive("tmux", cfg, backend="codex", with_handoff=True)
            inner = _gpt_launch_cmd(guidance, remote=target.remote, cfg=cfg)
        elif term_cmd == "hugpy-agent harness":
            # A persistent OpenCode frontier seat backed by Hugpy. Its fleet
            # model is a launch setting, including a model served by a local
            # worker; the harness refreshes OpenCode's model map.
            model = _frontier_models(cfg).get("hugpy", "")
            inner = "hugpy-agent harness" + (" --model " + shlex.quote(model) if model else "")
        else:
            inner = term_cmd
        if surface == "frontier" and "abstract-claude mct " in inner:
            # mct pointer-exchange grounds (abstract-claude mct <ws>). Resolved
            # BEFORE the script is written (1.0.141: the old post-wrap textual
            # edits never reached the launch file). The REPL runs WITHIN the
            # locus the seat is attributed to.
            inner = "MCT_LOCUS=" + shlex.quote(target.locus or "") + " " + inner
            if target.remote:
                await _push_mct_to_locus(ground_vm)
                inner = inner.replace("{ws}", "~/.config/hugpy-station/mct2/repl")
            else:
                ws2 = str(_active_ws())
                inner = inner.replace("{ws}", ws2)
                # the directive reaches mct via <ws>/operator-guidance.md, composed
                # fresh at every launch — the init prompt rides in it exactly once.
                _render_guidance(Path(ws2))
            _m2 = _frontier_models(cfg).get("mct", "")
            if _m2 and "--model" not in inner:
                inner = inner.replace("abstract-claude mct ", "abstract-claude mct --model " + _m2 + " ", 1)
            # per-backend session dirs: label mct's per-launch config dir
            if "AC_SESSION_LABEL=" not in inner:
                inner = inner.replace("abstract-claude mct ", "AC_SESSION_LABEL=mct abstract-claude mct ", 1)
        # One keeper per station: attach to (or create) the persistent tmux
        # session instead of spawning a second process.
        tmux_sess = _tmux_session_for(surface, launch_backend)
        if tmux_sess and _reattach:
            term_cmd = ""            # a live PTY is reattached below: nothing to write anywhere
        elif tmux_sess:
            try:
                term_cmd, _launch_path = await _seat_launch(target, tmux_sess, inner, cfg, surface)
            except LX.LocusError as e:
                # never an inline payload: say what failed, then the confinement
                # fallback below (same locus) takes over
                term_cmd = ("printf '%s\\n' " + shlex.quote("[hugpy-station] " + str(e)[:400])
                            + "; sleep 5")
                term_cmd = "bash -c " + shlex.quote(term_cmd)
        else:
            # Seat the surface in its station (no-op when host-grounded).
            term_cmd = _in_vm(inner, ground_vm)
    if term_cmd:
        try:
            argv0 = shlex.split(term_cmd)[0]
        except ValueError:
            argv0 = ""
        if argv0 and shutil.which(argv0):
            cd = ""
            if surface == "local" and not ground_vm:
                # host grounding only: the standing-charge dir is a HOST path,
                # meaningless as a cwd for a command exec'd into a VM
                lk = _local_keeper_ws()   # AGENTS.md + ./docs standing charge
                if lk is not None:
                    cd = f"cd {shlex.quote(str(lk))} && "
            # HOST-LOCAL PATCH: a VM-confined agent must stay VM-confined when
            # it exits. The packaged fallback drops the PTY to a host shell —
            # as the service user that shell is lxd-capable (root-equivalent),
            # so a dead in-VM agent would quietly hand out a host prompt. If
            # the command execs into a VM, fall back into that same VM's shell.
            m = re.match(r"lxc exec (\S+) ", term_cmd)
            if m:
                fb = (f"exec lxc exec {m.group(1)} -- "
                      "sudo -u ubuntu -H bash -l")
            elif (req_vm and req_vm != "@keeper" and _ssh_host(req_vm)
                  and _locus_target(req_vm).remote):   # 1.0.143: a pointer at self is the host
                # a dead ssh host session reconnects to the SAME host, never a
                # host prompt (explicit confinement, same rule as VMs)
                fb = ("echo; echo '[hugpy-station] ssh session ended — reconnecting to "
                      + req_vm + " (Ctrl-C to stop)'; sleep 2; exec " + _ssh_shell_cmd(_ssh_host(req_vm)))
            elif (term_cmd.startswith("ssh ") and req_vm
                  and req_vm != "@keeper"):
                # a dead ssh login must NOT drop to a host prompt (the service
                # user is lxd-capable) — fall back into the same VM via exec
                fb = f"exec lxc exec {req_vm} -- sudo -u ubuntu -H bash -l"
            else:
                fb = f"exec {shlex.quote(fallback)} -i"
            launch = f"{cd}{term_cmd} ; {fb}"
    # Persistent per-surface session: reattach when one is alive (scrollback
    # replays), else spawn. A frontier SPAWN (not a reattach) is gated by the
    # Frontier Keeper toggle — Local Keeper and Shell are never gated, and a
    # running frontier session is not stopped by disabling (gate semantics).
    existing = SESSIONS.get(FV_SESSIONS.get(surf_key) or "")
    if existing is not None and not existing.alive:
        existing = None
    if existing is None and surface == "frontier" and not _frontier_enabled():
        await ws.send_str(json.dumps({"t": "error", "msg":
            "Frontier Keeper: Disabled — nothing will start it. "
            "Local Keeper and Shell are unaffected; re-enable to launch A."}))
        await ws.close()
        return ws
    if existing is not None:
        sess, sid = existing, existing.sid
    else:
        sid = secrets.token_hex(16)
        env = {**os.environ, "TERM": "xterm-256color"}
        # keeper=True marks the session stationary (reaper-exempt); station
        # "host" + slot "fv-<surface>" never collides with real VM keepers.
        sess = TermSession(sid, "host", cmd=["bash", "-lc", launch], env=env,
                           keeper=True, slot=f"fv-{surf_key.replace(':', '-')}",
                           init_rows=init_rows, init_cols=init_cols)
        SESSIONS[sid] = sess
        try:
            await sess.start()
        except Exception as e:
            SESSIONS.pop(sid, None)
            await ws.send_str(json.dumps({"t": "error", "msg": str(e)}))
            await ws.close()
            return ws
        FV_SESSIONS[surf_key] = sid
        FV_BACKENDS[sid] = launch_backend
    await ws.send_str(json.dumps({"t": "sid", "sid": sid}))
    mediated = surface == "frontier" and backend == "mct"
    if not mediated:
        sess.attach(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if not mediated:
                    sess.write(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    ctl = json.loads(msg.data)
                except ValueError:
                    if not mediated:
                        sess.write(msg.data.encode())
                    continue
                if ctl.get("t") == "size":
                    _set_winsize(sess.master, int(ctl["rows"]), int(ctl["cols"]))
                elif ctl.get("t") == "scroll":
                    # Wheel → tmux copy-mode on this backend's OWN session.
                    # Mouse stays OFF (browser-native selection/copy depends
                    # on it) and prefix is None (no keyboard copy-mode entry),
                    # so this ctl is the only scroll path into pane history.
                    # n>0 = back/up (enters copy-mode, -e auto-exits at the
                    # bottom), n<0 = forward/down, cancel = jump back live
                    # (sent when the operator types while scrolled).
                    tsess = _tmux_session_for(surface, launch_backend)
                    if tsess:
                        try:
                            n = max(-120, min(120, int(ctl.get("n") or 0)))
                        except (TypeError, ValueError):
                            n = 0
                        base = f"tmux -L {KEEPER_TMUX_SOCK} "
                        # "=name:" — exact session, its active pane (a bare
                        # "=name" only resolves in target-SESSION contexts;
                        # copy-mode/send-keys want a pane)
                        tgt = shlex.quote("=" + tsess + ":")
                        if ctl.get("cancel"):
                            script = (base + f"send-keys -X -t {tgt} cancel "
                                      "2>/dev/null; true")
                        elif n > 0:
                            script = (base + f"copy-mode -e -t {tgt} 2>/dev/null; "
                                      + base + f"send-keys -X -N {n} -t {tgt} "
                                      "scroll-up 2>/dev/null; true")
                        elif n < 0:
                            script = (base + f"send-keys -X -N {-n} -t {tgt} "
                                      "scroll-down 2>/dev/null; true")
                        else:
                            script = ""
                        if script:
                            try:
                                await _locus_run_fast(ground_vm, script, timeout=10)
                            except Exception:
                                pass
                elif ctl.get("t") == "detach":
                    # Backend switch: release THIS PTY only. The backend's own
                    # tmux session stays alive (switching back reattaches);
                    # no other backend's session is touched. Ack so the
                    # client reconnects only after the PTY is gone.
                    sess.close()
                    SESSIONS.pop(sid, None)
                    if FV_SESSIONS.get(surf_key) == sid:
                        FV_SESSIONS.pop(surf_key, None)
                    try:
                        await ws.send_str(json.dumps({"t": "detached"}))
                    except Exception:
                        pass
                    break
                elif ctl.get("t") == "kill":
                    # Explicit restart of THIS backend: kill only ITS OWN named
                    # tmux session (never the surface's other backends), then
                    # ack so the client reconnects only after the kill lands.
                    tsess = _tmux_session_for(surface, launch_backend)
                    if tsess:   # the attach client dying leaves tmux alive
                        await _locus_run(ground_vm, f"tmux -L {KEEPER_TMUX_SOCK} "
                                         f"kill-session -t ={tsess}", timeout=15)
                    sess.close()
                    SESSIONS.pop(sid, None)
                    if FV_SESSIONS.get(surf_key) == sid:
                        FV_SESSIONS.pop(surf_key, None)
                    try:
                        await ws.send_str(json.dumps({"t": "killed"}))
                    except Exception:
                        pass
                    break
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    finally:
        if sess.alive:
            if sess.ws is ws:
                sess.detach()      # surface persists; the client may come back
        else:
            SESSIONS.pop(sid, None)
            if FV_SESSIONS.get(surf_key) == sid:
                FV_SESSIONS.pop(surf_key, None)
    return ws


# --------------------------------------------------------------------------- #
# agent-sudo switch — GUI control over the temporary passwordless-sudo grant.
# Flipping the switch needs root (it writes /etc/sudoers.d), so it goes through
# pkexec: polkit shows an interactive auth dialog that only a human at the
# keyboard can satisfy. That preserves agent-sudo's core property — the agent,
# which can reach this backend, still cannot grant itself root because it cannot
# answer the polkit prompt. Reading state is just a file-existence check on the
# world-listable /etc/sudoers.d drop-in, so status needs no privilege.
# --------------------------------------------------------------------------- #
AGENT_SUDO_DROPIN = "/etc/sudoers.d/agent-temp"
AGENT_SUDO_BIN = os.environ.get("AGENT_SUDO_BIN", "/usr/bin/agent-sudo")


def _agent_sudo_on():
    return os.path.exists(AGENT_SUDO_DROPIN)


async def api_agent_sudo_status(request):
    return web.json_response({"ok": True, "on": _agent_sudo_on(),
                              "dropin": AGENT_SUDO_DROPIN})


async def api_agent_sudo_toggle(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    desired = body.get("desired")
    if desired not in ("on", "off"):
        desired = "off" if _agent_sudo_on() else "on"     # no explicit target: flip
    # pkexec -> polkit interactive auth -> agent-sudo on|off. rc 126/127 mean the
    # dialog was dismissed or no authority/agent was available: report as denied.
    rc, out, err = await _run("pkexec", AGENT_SUDO_BIN, desired)
    now_on = _agent_sudo_on()
    ok = (rc == 0)
    audit(request, "agent-sudo-" + desired, detail=((out or err) or "").strip()[:200], ok=ok)
    if not ok:
        denied = rc in (126, 127)
        reason = ("authorization dismissed or denied" if denied
                  else (err or out or ("pkexec rc=" + str(rc))).strip())
        return web.json_response({"ok": False, "on": now_on, "error": reason},
                                 status=(403 if denied else 500))
    return web.json_response({"ok": True, "on": now_on, "desired": desired})


# --------------------------------------------------------------------------- #
# frontier file-access switch — allow/disallow the console frontier model's
# filesystem access. This drives hugpy-agent's own per-workspace frontier FS
# policy (`hugpy-agent mct-fs`), so the grant is scoped to the frontier model
# itself (not the host user) and takes effect live for the running session.
# Reads/sets the policy in FV_MCT_WS; no privilege needed (runs as the frontier's
# own user, same as the mct-usage readout).
# --------------------------------------------------------------------------- #
async def _frontier_fs_policy():
    exe = shutil.which("hugpy-agent")
    if not exe:
        return None, "hugpy-agent not on PATH"
    rc, out, err = await _run(exe, "mct-fs", str(_active_ws()))
    if rc != 0:
        return None, (err or out).strip() or "mct-fs failed"
    try:
        return json.loads(out), None
    except Exception:
        return None, "mct-fs returned non-JSON"


async def api_frontier_fs_status(request):
    vm = await _sidecar_vm(request)
    if vm:
        return await _remote_fs(request, vm)            # 1.0.144: the locus's OWN policy
    doc, err = await _frontier_fs_policy()
    if err:
        return web.json_response({"ok": False, "error": err}, status=502)
    return web.json_response({"ok": True,
                              "allowed": bool(doc.get("allow_frontier_fs_requests")),
                              "mediated": _frontier_fs_mediated(),
                              "applies_to": ["frontier:mct", "frontier:claude-code"],
                              "never_applies_to": ["local", "shell"],
                              "roots": doc.get("granted_roots", []),
                              "policy": doc})


async def api_frontier_fs_toggle(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    vm = await _sidecar_vm(request)
    if vm:
        desired = body.get("desired")
        if desired not in ("on", "off"):
            return web.json_response({"ok": False, "error": 'remote toggle needs {"desired": "on"|"off"}'}, status=400)
        return await _remote_fs(request, vm, desired)
    desired = body.get("desired")
    if desired not in ("on", "off"):
        doc, err = await _frontier_fs_policy()
        if err:
            return web.json_response({"ok": False, "error": err}, status=502)
        desired = "off" if doc.get("allow_frontier_fs_requests") else "on"
    exe = shutil.which("hugpy-agent")
    if not exe:
        return web.json_response({"ok": False, "error": "hugpy-agent not on PATH"}, status=502)
    rc, out, err = await _run(exe, "mct-fs", str(_active_ws()), "--allow", desired)
    if rc != 0:
        audit(request, "frontier-fs-" + desired, detail=(err or out).strip()[:200], ok=False)
        return web.json_response({"ok": False, "error": (err or out).strip() or "mct-fs failed"},
                                 status=502)
    try:
        allowed = bool(json.loads(out).get("allow_frontier_fs_requests"))
    except Exception:
        allowed = (desired == "on")
    audit(request, "frontier-fs-" + desired, ok=True)
    # Mirror for the claude-code seat: "allowed" (direct access) == not mediated.
    try:
        FRONTIER_FS_FLAG.parent.mkdir(parents=True, exist_ok=True)
        FRONTIER_FS_FLAG.write_text(json.dumps({"mediated": not allowed}) + "\n")
    except OSError:
        pass
    _render_session_directives()
    return web.json_response({"ok": True, "allowed": allowed, "desired": desired,
                              "mediated": not allowed,
                              "applies_to": ["frontier:mct", "frontier:claude-code"],
                              "never_applies_to": ["local", "shell"]})


# --- ✉ fleet mail: stations <-> the keeper console (the host arm) -----------
# The keeper console is the fleet's host arm: every station can message IT,
# and it can message every station INDIVIDUALLY. Transport is files + lxc exec
# (macvlan blocks guest->host networking, so the host always reaches in; a
# VM's agents only ever touch their own home):
#   station -> keeper : agents append ~/.keeper-mail-out.jsonl (the fleet-msg
#       CLI vm-new seeds); the collector below drains every station's outbox
#       into the keeper inbox (FV_STATE_HOME/keeper-mail.jsonl).
#   keeper -> station : POST /api/fleet/message appends to that station's
#       ~/.bridge-mail.jsonl — the SAME file its ✉ tab already reads
#       (bugreport-api) and its seated A/B/local can read in-VM.
# GET /api/vm/keeper/messages serves the keeper inbox in the bugreport-api
# messages shape — exactly what lights up the keeper drawer's ✉ tab.
KEEPER_MAIL_PATH = FV_STATE_HOME / "keeper-mail.jsonl"
FLEET_MAIL_MAX = 200          # rows kept per mailbox (trimmed on append)
FLEET_MSG_MAX = 4000          # one message's text cap
_fleet_mail_lock = asyncio.Lock()
_fleet_mail_last = 0.0

# Drain a station's outbox: print-and-truncate under the same flock the
# fleet-msg writer takes, so a message is never torn or double-read.
_OUTBOX_DRAIN_SH = ('f="$HOME/.keeper-mail-out.jsonl"; [ -s "$f" ] || exit 0; '
                    'exec 9>>"$f.lock"; flock 9; cat "$f"; : > "$f"')
# (1.0.138: the lxc bridge-mail append + in-guest keeper-pane ping that lived
# here are gone — every non-host locus is messaged on the central comms lane.)


def _mail_row(frm, to, text, unread=False):
    row = {"id": f"m{int(time.time() * 1000)}-{secrets.token_hex(3)}",
           "ts": int(time.time()), "from": frm, "to": to,
           "text": str(text)[:FLEET_MSG_MAX], "via": "fleet-mail"}
    if unread:      # only rows a later serve can mark read — no phantom badge
        row["unread"] = True
    return row


def _keeper_mail_write(rows):
    KEEPER_MAIL_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEEPER_MAIL_PATH.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n"
                for r in rows[-FLEET_MAIL_MAX:]), encoding="utf-8")


def _keeper_mail_rows():
    rows = []
    try:
        for line in KEEPER_MAIL_PATH.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        pass
    return rows


async def _drain_fleet_mail(force=False):
    """Pull every station's outbox into the keeper inbox. One lock + a 15s
    freshness guard: the GET route and the background loop never double-drain,
    and a station's mail still lands while nobody is watching."""
    global _fleet_mail_last
    async with _fleet_mail_lock:
        if not force and time.time() - _fleet_mail_last < 15:
            return
        _fleet_mail_last = time.time()
        try:
            names = await known_names()
        except Exception:
            return
        fresh = []
        for vm in sorted(names):
            try:
                rc, out, _err = await _run(
                    "lxc", "exec", vm, "--", "sudo", "-u", "ubuntu", "-H",
                    "bash", "-c", _OUTBOX_DRAIN_SH)
            except OSError:
                continue
            if rc != 0 or not out.strip():
                continue
            for line in out.splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not (isinstance(row, dict) and str(row.get("text", "")).strip()):
                    continue
                r = _mail_row(vm, "keeper", str(row["text"]), unread=True)
                if isinstance(row.get("ts"), (int, float)):
                    r["ts"] = int(row["ts"])   # keep the sender's own clock
                fresh.append(r)
        if fresh:
            _keeper_mail_write(_keeper_mail_rows() + fresh)


async def keeper_messages_get(request):
    """GET /api/vm/keeper/messages — the keeper console's ✉ inbox (station
    mail, drained on read; bugreport-api shape {ok, messages}). Serving marks
    rows read: the unread badge means 'arrived since you last looked'."""
    await _drain_fleet_mail()
    rows = _keeper_mail_rows()
    if any(r.get("unread") for r in rows):
        _keeper_mail_write([{k: v for k, v in r.items() if k != "unread"}
                            for r in rows])
    return web.json_response({"ok": True, "messages": rows[-FLEET_MAIL_MAX:]})


async def fleet_message_post(request):
    """POST /api/fleet/message {"to": "<station>"|"keeper", "text": "..."} —
    the keeper console messaging one station (its ✉ tab + a file its seated
    agents read), or host-side tooling dropping mail in the keeper inbox."""
    try:
        body = await request.json()
        to = str(body.get("to") or "").strip()
        text = str(body.get("text") or "").strip()
        if not text or not to:
            raise ValueError
    except Exception:
        return web.json_response(
            {"error": 'body must be {"to": "<station>"|"keeper", "text": "..."}'},
            status=400)
    frm = (str(body.get("from") or "").strip() or "keeper")[:24]
    if _is_host_vm(to):
        row = _mail_row(frm, to, text, unread=True)
        _keeper_mail_write(_keeper_mail_rows() + [row])
        audit(request, "fleet-message", f"to=keeper from={frm}", ok=True)
        return web.json_response({"ok": True, "id": row["id"], "to": to})
    # 1.0.138: every non-host locus — ssh host, LXD guest, registered locus —
    # is messaged on the central comms lane: comms/ping files a [ping] request
    # on THAT locus's board (its central key); the locus's keeper reads it via
    # comms_inbox (or its own station sync, where one runs). No lxc exec into a
    # guest mail file, no station on the locus required.
    loc = await _known_locus_key(to)
    if not loc:
        return web.json_response({"error": f"unknown locus {to}"}, status=404)
    if loc == _keeper_locus():          # a dropdown pointer at THIS station
        row = _mail_row(frm, "keeper", text, unread=True)
        _keeper_mail_write(_keeper_mail_rows() + [row])
        audit(request, "fleet-message", f"to=keeper({to}) from={frm}", ok=True)
        return web.json_response({"ok": True, "id": row["id"], "to": "keeper"})
    sender = _station_locus() or re.sub(r"[^a-z0-9-]", "-", frm.lower()) or "keeper"
    try:
        res = await _ts_call(request.app, "comms/ping",
                             {"to": loc, "text": text, "from_": sender,
                              "kind": "message"}, timeout=15)
    except Exception as e:  # noqa: BLE001 — toolserver down / rejected id
        audit(request, "fleet-message", f"to={to}({loc}) via=board failed: {e}", ok=False)
        return web.json_response({"ok": False, "error": f"board ping failed: {e}"}, status=502)
    bid = (res or {}).get("id") if isinstance(res, dict) else None
    audit(request, "fleet-message", f"to={to}({loc}) from={sender} via=board id={bid}", ok=True)
    return web.json_response({"ok": True, "id": bid, "to": to, "locus": loc, "via": "board",
                              "pinged": False})


# ⧉ brief keeper: the board brief + a snapshot of what currently stands open,
# filed on the locus's central board (comms ping) — see _central_todo_brief.


def _todo_brief_summary(raw, cap=12):
    try:
        items = json.loads(raw or "{}").get("items") or []
    except ValueError:
        items = []
    live = [i for i in items if isinstance(i, dict)
            and i.get("status") in ("open", "doing")]
    if not live:
        return "The board has no open items right now."
    lines = []
    for i in live[:cap]:
        text = " ".join(str(i.get("text") or "").split())[:140]
        lines.append("- [%s/%s] %s%s" % (i.get("type") or "todo",
                                         i.get("status") or "open",
                                         i.get("id") or "?",
                                         (" " + text) if text else ""))
    more = len(live) - len(lines)
    if more > 0:
        lines.append("- … and %d more in the file" % more)
    return ("Currently standing (%d open/doing): " % len(live)) + " ".join(lines)


async def vm_todo_brief_post(request):
    """POST /api/vm/<locus>/todo/brief {"text": "<brief>"} -> {ok, via, open}.
    1.0.138: the brief + a snapshot of the locus's OPEN central items is filed
    as ONE comms ping on that locus's board (_central_todo_brief) — it used to
    lxc-exec ~/todo.json and a live keeper pane inside a guest."""
    vm = request.match_info.get("vm") or ""
    if _is_host_vm(vm):
        return web.json_response(
            {"error": "the host keeper is briefed by its charge — there is no "
                      "station pane to prompt"}, status=400)
    resp = await _central_vm_reroute(request)   # every non-host locus: central ping
    if resp is not None:
        return resp
    return web.json_response({"error": f"unknown locus {vm}"}, status=404)


async def _fleet_mail_loop(app):
    try:
        while True:
            await asyncio.sleep(30)
            try:
                await _drain_fleet_mail()
            except Exception:      # noqa: BLE001 — the collector must survive
                pass
    except asyncio.CancelledError:
        pass


async def _start_fleet_mail(app):
    if os.environ.get("STATION_DEV_QUIET") == "1":   # dev copy: never act on live seats
        return
    app["fleet_mail"] = asyncio.create_task(_fleet_mail_loop(app))


async def _stop_fleet_mail(app):
    app["fleet_mail"].cancel()


# --- ☑ canonical TodoDrawer host routes (/api/vm/keeper/*) -----------------
# The board panel renders the CANONICAL station-console TodoDrawer against
# vm="keeper" = THIS console's ACTIVE SESSION workspace: same file + same
# .todo.lock the agent's MCP todo tool uses. These handlers mirror the
# console-api / bugreport-api sidecar shapes the drawer expects, and are
# registered BEFORE the sidecar proxies so "keeper" board traffic never leaves
# the host process. messages stays unrouted (the drawer's honest
# "route not yet enabled" degrade) and bugreport falls through to the sidecar.

FV_TODO_TRIAGE = os.environ.get("FLEETVIEW_TODO_TRIAGE", "1") not in ("0", "off", "no")

_TRIAGE_SYS = (
    "You are B, the local keeper of an operator/agent shared to-do board "
    "(session %r). A new %s just landed on the board. Decide its routing:\n"
    "- 'frontier': it needs A, the frontier reasoning model (code work, builds, "
    "debugging, multi-file or multi-step judgment). Compose the prompt A should "
    "receive: self-contained and imperative, includes the item id and text, and "
    "tells A to keep this board item's status/comments updated (todo tool) as "
    "it works.\n"
    "- 'local': a short factual or organizational reply from you settles it.\n"
    "- 'none': nothing to route (a bookmark, a self-note, an operator-only "
    "step, or work already in motion).\n"
    "Reply ONLY JSON: {\"route\":\"frontier|local|none\",\"reason\":\"<one "
    "line>\",\"reply\":\"<local route only>\",\"prompt\":\"<frontier route "
    "only>\"}"
)


def _parse_json_obj(text):
    t = text or ""
    m = re.search(r"```[a-zA-Z]*\s*\n(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return {}
    try:
        d = json.loads(t[a:b + 1])
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _todo_b_comment(item_id, text):
    # flow: docs/flows/todo-push-triage.flow.json (B comments must not re-arm)
    """Append a by:'B' comment through the canonical engine (same lock as every
    other writer). B's own comments never re-trigger triage."""
    try:
        with _todo_locked():
            state, err = _mct_todo_read()
            if state is None:
                return
            it = next((i for i in state["items"]
                       if isinstance(i, dict) and i.get("id") == item_id), None)
            if it is None:
                return
            comments = [c for c in (it.get("comments") or []) if isinstance(c, dict)]
            comments.append({"by": "B", "ts": int(time.time()), "text": str(text)[:2000]})
            keep_ts = int(it.get("ts") or 0)
            state, err = todo_apply(state, {"op": "update", "id": item_id,
                                            "fields": {"comments": comments}})
            if state is not None:
                # B's own comment is NOT activity: restore the item's ts so the
                # reminder loop does not read it as a re-arm (the 2026-08-22
                # oscillator: B's "needs operator" re-armed itself every tick).
                it2 = next((i for i in state["items"]
                            if isinstance(i, dict) and i.get("id") == item_id), None)
                if it2 is not None and keep_ts:
                    it2["ts"] = keep_ts
                _mct_todo_write(state)
    except OSError:
        return
    _todo_hist({"event": "comment", "id": item_id, "type": it.get("type"),
                "detail": {"by": "B", "text": str(text)[:200]}})


# --- reminder/hand-off dispatch surface (1.0.92) -----------------------------
# Until 1.0.91 every composed reminder was TYPED into the live frontier pty as
# a pointer line. With the keeper surface on `serve` that is simply wrong (the
# pty is not the keeper), and on a handoff-roll the loop composed ONE line PER
# OPEN TODO, so the operator saw seventeen pointer lines land in the input in a
# single burst (2026-09-16 report). Delivery is now the toolserver prompt
# queue, collapsed to ONE item per cycle; the prompt-inbox dirs stay as the
# audit copy (capped, see _prompt_inbox_sweep). The pty write survives only for
# a tmux keeper surface with STATION_REMINDER_PTY=1 explicitly set — in serve
# mode it is never touched.
STATION_REMINDER_PTY = ((os.environ.get("STATION_REMINDER_PTY") or "").strip().lower()
                        in ("1", "on", "yes", "true"))
PROMPT_INBOX_KEEP = int(os.environ.get("STATION_PROMPT_INBOX_KEEP") or "50")
_DISPATCH_BATCH = []          # composed this cycle; flushed as one queued prompt
_DISPATCH_SURFACE = "serve"   # refreshed once per cycle by _todo_reminder_cycle
_DISPATCH_SERVE_OK = None     # serve answered /api/state this cycle (None = not probed)


def _dispatch_note(sent):
    """t360 (2026-09-16): the comment suffix after a hand-off names where it
    actually went. `sent` only ever means "typed into a tmux pty", so on the
    serve surface every reminder used to end in "saved to prompt-inbox (no
    frontier seat live)" although the batch reached the keeper through the
    toolserver prompt queue (its reminders panel + SessionStart hook)."""
    if sent:
        return " \u00b7 typed into the frontier terminal"
    if _DISPATCH_SURFACE == "serve":
        if _DISPATCH_SERVE_OK is False:
            return " \u00b7 queued for the keeper serve chat (serve is DOWN right now; it drains when it is back)"
        return " \u00b7 queued for the keeper serve chat (reminders panel)"
    return " \u00b7 saved to prompt-inbox (no frontier terminal running \u2014 it picks this up when opened)"


def _dispatch_state_path():
    return FV_STATE_HOME / "handoff-dispatch.json"


# 1.0.146 (t4260): the hand-off dedupe has an AGE FLOOR — an identical open set
# is re-queued after this long, so a lost prompt is never lost forever.
_HANDOFF_REQUEUE_S = max(600, int(os.environ.get("STATION_HANDOFF_REQUEUE_SECS") or str(6 * 3600)))


def _dispatch_is_dup(prev, sig, now, floor=None):
    """(dup, why): True while the last queued batch has the same signature AND
    is younger than the floor; past the floor the same set is re-queued."""
    floor = _HANDOFF_REQUEUE_S if floor is None else floor
    if not isinstance(prev, dict) or str(prev.get("sig")) != sig:
        return False, "new open set"
    age = now - float(prev.get("ts") or 0)
    if age < floor:
        return True, "same open set queued %d min ago (re-queued after %d h)" % (age // 60, floor // 3600)
    return False, "same open set but %d h old — re-queued (age floor)" % (age // 3600)


def _prompt_inbox_sweep(keep=None):
    """Retention for the audit copies: keep the newest `keep` dirs, drop older.
    Names are timestamp-prefixed, so a lexical sort is a chronological sort."""
    keep = PROMPT_INBOX_KEEP if keep is None else keep
    try:
        root = _active_ws() / "prompt-inbox"
        dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return 0
    n = 0
    for p in dirs[:max(0, len(dirs) - max(0, keep))]:
        try:
            shutil.rmtree(p)
            n += 1
        except OSError:
            pass
    return n


def _frontier_dispatch(tag, a_prompt, item=None, why=""):
    # flow: docs/flows/todo-push-triage.flow.json (keeper hand-off edge)
    """Compose one reminder/hand-off. The prompt.md under the session
    prompt-inbox is the AUDIT copy (and the pointer the queued item carries);
    actual delivery is the toolserver prompt queue — this only BUFFERS the
    entry and _flush_dispatch_batch() submits the whole cycle as one item.
    Returns (sent, dst); dst None = could not even save. `sent` now means
    "typed into the pty", which only happens on a tmux surface with
    STATION_REMINDER_PTY=1; the queue submission is reported separately."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = _active_ws() / "prompt-inbox" / f"{ts}-{tag}"
    try:
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "prompt.md").write_text(a_prompt, encoding="utf-8")
    except OSError:
        return False, None
    it = item if isinstance(item, dict) else {}
    _DISPATCH_BATCH.append({
        "tag": tag, "id": str(it.get("id") or tag.rsplit("-", 1)[-1]),
        "title": str(it.get("text") or "")[:160],
        "status": str(it.get("status") or ""),
        "ts": int(it.get("ts") or 0), "why": str(why or "")[:240],
        "path": str(dst / "prompt.md")})
    sent = False
    if STATION_REMINDER_PTY and _DISPATCH_SURFACE == "tmux":
        try:
            sess = _frontier_live_session()
            if sess is not None:
                pointer = ("Handle the operator prompt at " + str(dst / "prompt.md")
                           + " — read it via a pull, then respond.")
                sess.write((pointer + "\r").encode("utf-8"))
                sent = True
        except Exception:
            sent = False
    return sent, dst


async def _flush_dispatch_batch(app):
    """Collapse this cycle's composed reminders into ONE toolserver prompt.
    Dedup signature = sorted open-id set + newest item ts; an unchanged open
    set never requeues (state in <FV_STATE_HOME>/handoff-dispatch.json, plus a
    cross-check against prompts still pending on the locus, so a restart that
    loses the file still does not double-queue)."""
    batch, _DISPATCH_BATCH[:] = list(_DISPATCH_BATCH), []
    if not batch:
        return None
    ids = sorted({e["id"] for e in batch if e.get("id")})
    max_ts = max([int(e.get("ts") or 0) for e in batch] or [0])
    sig = hashlib.sha256(("|".join(ids) + "@%d" % max_ts).encode()).hexdigest()[:16]
    marker = "[station-reminder-batch %s]" % sig
    sp = _dispatch_state_path()
    try:
        prev = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    now_ts = int(time.time())
    dup, dup_why = _dispatch_is_dup(prev, sig, now_ts)
    if dup:
        return None                    # same open set, same newest ts, under the age floor
    if "age floor" in dup_why:
        _audit_line("handoff-requeue", "%s: %s" % (sig, dup_why))
        marker_age = marker + " requeue"
    else:
        marker_age = ""
    locus = _station_locus()
    if not locus or app is None:
        return None
    stale = []                         # older batches this one supersedes
    try:                               # a still-pending copy also counts as queued
        pend = await _ts_call(app, "prompt/list",
                              {"locus": locus, "status": "open", "limit": 50})
        for d in (pend or []):
            if not isinstance(d, dict):
                continue
            t = str(d.get("text") or "")
            if marker in t and not marker_age:
                return None            # a still-pending copy is queued, not lost
            if "[station-reminder-batch " in t and d.get("id"):
                stale.append(str(d["id"]))
    except Exception:
        pass
    lines = ["%d open items for keeper — queued by the station reminder cycle."
             % len(batch),
             "(These are NOT typed into your terminal. Pull each prompt file you "
             "act on, then update the board item.)", ""]
    if marker_age:
        lines.insert(1, "(re-queued — %s; the previous batch for this set was not acted on)" % dup_why)
    for e in batch:
        lines.append("- %s: %s" % (e["id"], e["title"] or "(no title)"))
        if e.get("why"):
            lines.append("    next: %s" % e["why"])
        lines.append("    prompt: %s" % e["path"])
    lines += ["", marker]
    body = "\n".join(lines)
    try:
        doc = await _ts_call(app, "prompt/submit",
                             {"locus": locus, "text": body, "files": [],
                              "by": _station_id(), "session": _active_session(),
                              "source": "station-reminder"}, timeout=30)
    except Exception:
        return None
    # 1.0.97 (t338): the new batch carries the whole open set, so the older
    # station-reminder prompts are superseded — close them (prompt/done also
    # closes their '[prompt] …' board rows) instead of letting them pile up.
    for pid in stale:
        try:
            await _ts_call(app, "prompt/done", {"id": pid, "status": "done"}, timeout=15)
        except Exception:
            pass
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({"sig": sig, "ids": ids, "max_ts": max_ts,
                                  "ts": now_ts, "requeues": int((prev or {}).get("requeues") or 0) + (1 if marker_age else 0),
                                  "prompt_id": (doc or {}).get("id")}, indent=2) + "\n",
                      encoding="utf-8")
    except OSError:
        pass
    _todo_hist({"event": "reminder", "id": "", "type": "batch",
                "detail": {"action": "queued", "count": len(batch), "ids": ids,
                           "prompt_id": (doc or {}).get("id")}})
    return doc


async def _todo_triage(item, trigger, comment_text=None):
    """Background board triage: B (the local gateway model) evaluates a fresh
    operator item/comment and routes it — 'frontier' composes a prompt and
    dispatches it to the running frontier terminal (prompt-inbox pointer, the
    same file-pointing path the ✍ prompt panel uses), 'local' answers as a
    by:'B' comment, 'none' just journals the verdict. Never blocks the POST;
    a gateway failure leaves the board exactly as it was."""
    name = _active_session()
    state, _err = _mct_todo_read()
    others = [{"id": i.get("id"), "type": i.get("type"), "status": i.get("status"),
               "text": str(i.get("text"))[:80]}
              for i in (state or {"items": []})["items"][:30]
              if i.get("id") != item.get("id")]
    payload = {"item": {k: item.get(k) for k in
                        ("id", "type", "text", "note", "priority", "status", "by")},
               **({"new_comment": comment_text} if comment_text else {}),
               "board": others}
    loop = asyncio.get_event_loop()
    try:
        reply = await loop.run_in_executor(
            None, _todo_keeper_chat,
            [{"role": "system", "content": _TRIAGE_SYS % (name, trigger)},
             {"role": "user", "content": json.dumps(payload)}], 700)
    except Exception:
        return
    verdict = _parse_json_obj(reply)
    route = str(verdict.get("route") or "").strip().lower()
    reason = str(verdict.get("reason") or "").strip()[:300]
    if route not in ("frontier", "local", "none"):
        return
    _todo_hist({"event": "triage", "id": item.get("id"), "type": item.get("type"),
                "detail": {"route": route, "reason": reason, "trigger": trigger}})
    if route == "local":
        note = str(verdict.get("reply") or "").strip()[:1800]
        if note:
            _todo_b_comment(item.get("id"), note)
        return
    if route == "frontier":
        a_prompt = (str(verdict.get("prompt") or "").strip()
                    or (f"Operator queued board item {item.get('id')} "
                        f"({item.get('type')}): {item.get('text')!r}. Handle it "
                        "and keep the board item's status and comments updated "
                        "via the todo tool as you work."))
        sent, dst = _frontier_dispatch(f"triage-{item.get('id')}", a_prompt,
                                       item=item, why=reason)
        if dst is None:
            return
        _todo_b_comment(item.get("id"),
                        "→ frontier: " + (reason or "needs the frontier model")
                        + _dispatch_note(sent))


# --- ⏱ board reminders — the vm_mgr push cadence, redundancy-corrected ----
# The vm_mgr keeper-relay re-delivered quiet board items on a timer REGARDLESS
# of why the last attempt stalled — the flaw this port fixes. Deterministic
# guards run first (attempt cap, dispatch spacing, standing withhold); only a
# plausible reminder consults B, whose verdict factors the item's LOGGED
# RESPONSE HISTORY (the comment thread — A's stated blockers live there) and is
# journaled. B's inference on A's reasons is fragile, so it is fenced: a hard
# attempt cap withholds regardless of B, a gateway failure fails CLOSED (no
# blind ping — the original bug), and any withhold clears on new non-B activity.
# Cadence = the canonical reserved `cfg-relay` item (note {"board_quiet_secs":N},
# 0 = no quiet gate) — the board drawer's \u23f1 push knob edits it; B mints it.
FV_TODO_REMIND = os.environ.get("FLEETVIEW_TODO_REMIND", "1") not in ("0", "off", "no")
# 1.0.144 (operator 2026-10-01: "take B out of it for now"): the per-item B
# verdict cycle below (_todo_keeper_chat verdicts, B comments, B-driven and
# attempt-cap withholding) is the reminder MITIGATOR, OFF by default. The keeper
# is reminded by the deterministic open-todos digest instead (_digest_loop,
# todo_digest.py). A redesigned mitigator slots in here — board item
# "[F2.5] Redesign a reminder mitigator"; it may annotate, never suppress.
REMIND_MITIGATOR = os.environ.get("STATION_REMIND_MITIGATOR", "0").strip().lower() in ("1", "on", "true", "yes")
_REMIND_TICK = int(os.environ.get("FLEETVIEW_TODO_REMIND_TICK", "60"))
# 1.0.146 (t4260): _REMIND_CAP (3 attempts, then withhold) is GONE — operator
# ruling d4254: no attempt caps; only the min-gap coalesce below remains.
_REMIND_MIN_GAP = int(os.environ.get("FLEETVIEW_TODO_REMIND_GAP", "600"))


def _station_switches():
    """1.0.146: every code default that can inert a loop, with its LIVE value —
    in the settings payload (/api/about, /api/loops config) and, when one is
    off, on the ⚠ strip. Never hidden in an env var nobody reads."""
    return {
        "remind_mitigator": {"on": bool(REMIND_MITIGATOR), "env": "STATION_REMIND_MITIGATOR", "default": "0",
                             "effect": "B reminder-verdict cycle (the digest reminds regardless)"},
        "todo_remind": {"on": bool(FV_TODO_REMIND), "env": "FLEETVIEW_TODO_REMIND", "default": "1",
                        "effect": "reminder cycle tick (the ⚠ loop detector rides it either way)"},
        "todo_digest": {"on": bool(DIGEST_ON), "env": "STATION_TODO_DIGEST", "default": "1",
                        "effect": "open-todos digest into every keeper session"},
        "nudge_tmux": {"on": bool(_knudge.tmux_enabled()) if _knudge else False, "env": "STATION_NUDGE_TMUX",
                       "default": "0", "effect": "tmux keeper pane as the nudge fallback"},
        "dev_quiet": {"on": os.environ.get("STATION_DEV_QUIET") == "1", "env": "STATION_DEV_QUIET", "default": "0",
                      "effect": "dev copy: no reminders, no host delivery target"},
        "handoff_requeue_s": _HANDOFF_REQUEUE_S, "remind_min_gap_s": _REMIND_MIN_GAP,
        "nudge_wait_max_s": _knudge.WAIT_MAX_S if _knudge else 900,
        "bugreport_inflight_max_s": int(os.environ.get("STATION_BUGREPORT_INFLIGHT_MAX") or 900),
        "prompt_pending_tick_s": PROMPT_PENDING_TICK,
    }


def _switch_strip_rows():
    """Strip rows for the inert-by-default switches (visible, settable)."""
    sw = _station_switches()
    for key, label in (("remind_mitigator", "reminder mitigator (B verdict cycle) OFF"),
                       ("todo_digest", "open-todos digest OFF"),
                       ("todo_remind", "reminder cycle OFF")):
        if not sw[key]["on"]:
            _hold_note("switch:" + key, _sh.SOURCE_SWITCH if _sh else "station:switch", label,
                       "%s=%s (default %s): %s" % (sw[key]["env"], "1" if sw[key]["on"] else "0",
                                                  sw[key]["default"], sw[key]["effect"]),
                       "set %s=1 in env/station.env and restart the station to turn it on" % sw[key]["env"])
        else:
            _hold_clear("switch:" + key)
_RELAY_ID = "cfg-relay"
_RELAY_DEFAULT_SECS = 1800

_REMIND_SYS = (
    "You are B, the local keeper of session %r's shared to-do board. Item %s "
    "has been quiet past the operator's push cadence and carries NO status "
    "flag yet. You see its logged history from the operator and 'A' (the "
    "frontier model); your own earlier comments are omitted — never treat "
    "them as evidence. Decide:\n"
    "- 'remind': another frontier (keeper) attempt is likely to progress it. "
    "Compose the prompt: item id/text, what prior attempts reported (quote "
    "the blocker), and demand concrete progress or a precise statement of "
    "what is missing, plus a board update.\n"
    "- 'withhold': re-pinging is redundant — a named blocker no retry can clear.\n"
    "- 'operator': the operator must act first; say exactly what is needed.\n"
    "Also ASSIGN the item's status flag so future pushes are deterministic: "
    "'in_process' (work can proceed; detail = what completion will look like), "
    "'operator_needed' (reason: ambiguous_request | operator_action), "
    "'fail' (reason: not_implementable), or 'completed' (needs review).\n"
    "Reply ONLY JSON: {\"action\":\"remind|withhold|operator\","
    "\"reason\":\"<one line>\",\"prompt\":\"<remind only>\","
    "\"flag\":{\"status\":\"in_process|operator_needed|fail|completed\","
    "\"reason\":\"<short>\",\"detail\":\"<short>\"}}"
)
TODO_FLAGS = ("completed", "operator_needed", "fail", "in_process")
_FLAG_REASONS = {"operator_needed": ("ambiguous_request", "operator_action"),
                 "fail": ("not_implementable",)}


def _flag_of(it):
    f = it.get("flag")
    if isinstance(f, dict) and f.get("status") in TODO_FLAGS:
        return f
    return None


def _set_flag(item_id, status, reason="", detail="", by="B"):
    """Write the item's status flag (extra field; survives update). None clears."""
    if status is not None and status not in TODO_FLAGS:
        return
    try:
        with _todo_locked():
            state, _err = _mct_todo_read()
            if state is None:
                return
            it = next((i for i in state["items"]
                       if isinstance(i, dict) and i.get("id") == item_id), None)
            if it is None:
                return
            if status is None:
                it.pop("flag", None)
            else:
                it["flag"] = {"status": status, "reason": str(reason or "")[:120],
                              "detail": str(detail or "")[:400], "by": by,
                              "ts": int(time.time())}
            _mct_todo_write(state)
    except OSError:
        return
    _todo_hist({"event": "flag", "id": item_id, "detail": {"status": status, "reason": reason, "by": by}})


def _relay_quiet_secs(items):
    for i in items:
        if isinstance(i, dict) and i.get("id") == _RELAY_ID:
            try:
                n = json.loads(i.get("note") or "")["board_quiet_secs"]
                if isinstance(n, (int, float)) and n >= 0:
                    return int(n)
            except (ValueError, KeyError, TypeError):
                pass
            return _RELAY_DEFAULT_SECS
    return _RELAY_DEFAULT_SECS


def _mint_cfg_relay():
    """The keeper mints the reserved cadence item (the console API never forges
    the id — the drawer's own contract); note carries the knob's JSON."""
    try:
        with _todo_locked():
            state, _err = _mct_todo_read()
            if state is None:
                return
            if any(isinstance(i, dict) and i.get("id") == _RELAY_ID
                   for i in state["items"]):
                return
            state["items"].append({
                "id": _RELAY_ID, "type": "todo",
                "text": "\u23f1 push cadence (reserved config — set via the "
                        "board's \u23f1 knob)",
                "note": json.dumps({"board_quiet_secs": _RELAY_DEFAULT_SECS}),
                "status": "done", "by": "B", "ts": int(time.time())})
            _mct_todo_write(state)
    except OSError:
        pass


def _watch_update(item_id, mut):
    """Persist per-item reminder state in the item's `watch` extra field —
    the keeper extra-field contract (update never strips it). None deletes."""
    try:
        with _todo_locked():
            state, _err = _mct_todo_read()
            if state is None:
                return
            it = next((i for i in state["items"]
                       if isinstance(i, dict) and i.get("id") == item_id), None)
            if it is None:
                return
            w = it.setdefault("watch", {})
            for k, v in mut.items():
                if v is None:
                    w.pop(k, None)
                    if k == "withheld":
                        w.pop("withheld_ts", None)
                else:
                    w[k] = v
            _mct_todo_write(state)
    except OSError:
        pass


async def _todo_reminder_cycle(app=None):
    # flow: docs/flows/todo-push-triage.flow.json
    global _DISPATCH_SURFACE, _DISPATCH_SERVE_OK
    state, _err = _mct_todo_read()
    if state is None:
        return
    items = [i for i in state["items"] if isinstance(i, dict)]
    quiet = _relay_quiet_secs(items)
    now = int(time.time())
    loop = asyncio.get_event_loop()
    name = _active_session()
    try:
        _DISPATCH_SURFACE, _ac = await _keeper_surface_now()
        _DISPATCH_SERVE_OK = bool((_ac or {}).get("ok"))
    except Exception:
        _DISPATCH_SURFACE = "serve"    # unknown surface: never type into a pty
        _DISPATCH_SERVE_OK = None
    # The board file is the UI's source (t186, file-primary) but it is NOT
    # updated when an item is closed through the toolserver's todo_done — which
    # is how the keeper actually closes things. That drift is why the
    # 2026-09-16 burst re-dispatched t130/t135 hours after they were closed.
    # Cross-check the central board and treat "done there" as done.
    closed = set()
    if app is not None:
        try:
            central = await _ts_call(app, "todo/list",
                                     {"locus": _station_locus(), "status": "done",
                                      "type": "", "limit": 500}, timeout=10)
            closed = {str(d.get("id")) for d in (central or [])
                      if isinstance(d, dict) and d.get("id")}
        except Exception:
            closed = set()
    for it in items:
        if (it.get("id") == _RELAY_ID
                or it.get("type") not in ("todo", "request", "operator")
                or it.get("status") not in ("open", "doing")
                or str(it.get("id")) in closed
                # 1.0.97 (t338): a '[prompt] …' row is the board mirror of a
                # queued prompt (prompt/submit) — a pointer, not work. Reminding
                # about it re-queued the reminder cycle's OWN batches (t317 ->
                # p20 -> t329 ...). prompt/done closes these rows.
                or str(it.get("source") or "") == "prompt.submit"
                or str(it.get("text") or "").startswith("[prompt]")):
            continue
        w = dict(it.get("watch") or {})
        comments = [c for c in (it.get("comments") or []) if isinstance(c, dict)]
        non_b = [c for c in comments if str(c.get("by")) != "B"]
        last_non_b = max([int(it.get("ts") or 0)]
                         + [int(c.get("ts") or 0) for c in non_b], default=0)
        # activity = operator/A/flag changes only; B's comments never count
        flag = _flag_of(it)
        flag_ts = int(flag.get("ts") or 0) if flag else 0
        last_act = max(last_non_b, int(w.get("last_dispatch") or 0), flag_ts)
        if w.get("withheld"):
            if last_non_b > int(w.get("withheld_ts") or 0) or flag_ts > int(w.get("withheld_ts") or 0):
                _watch_update(it["id"], {"withheld": None, "verdicts": 0})
                _todo_hist({"event": "reminder", "id": it["id"],
                            "type": it.get("type"),
                            "detail": {"action": "rearmed",
                                       "reason": "new non-B activity on the item"}})
            continue                     # (re-assess next cycle, clean state)
        gate = quiet if quiet > 0 else _REMIND_TICK
        if now - last_act < gate:
            continue
        if (int(w.get("last_dispatch") or 0)
                and now - int(w["last_dispatch"]) < max(quiet, _REMIND_MIN_GAP)):
            continue
        # ---- flag-driven branches (docs/flows/todo-push-triage) — deterministic,
        # no inference. Operator comment = by not in (A, B).
        op_cmts = [c for c in non_b if str(c.get("by")) != "A"]
        last_op = max([int(c.get("ts") or 0) for c in op_cmts], default=0)
        def _handoff(why, direction):
            hist = "\n".join(f"- {c.get('by')}: {str(c.get('text'))[:300]}"
                              for c in non_b[-4:])
            a_prompt = (f"Board item {it['id']} ({it.get('type')}): {it.get('text')!r}\n"
                        f"Flag: {json.dumps(flag) if flag else 'none'}\n"
                        f"Why you get it now: {why}\n"
                        f"Operator direction: {direction or '(see history)'}\n"
                        f"History (operator/A):\n{hist or '- none'}\n"
                        "Execute it, or set the flag precisely (completed / "
                        "in_process{detail, completion_indicator, log} / "
                        "operator_needed{reason} / fail{reason}) with a board comment.")
            sent, dst = _frontier_dispatch(f"handoff-{it['id']}", a_prompt,
                                           item=it, why=why)
            if dst is None:
                return
            _watch_update(it["id"], {"last_dispatch": now, "dispatches": int(w.get("dispatches") or 0) + 1})
            _todo_b_comment(it["id"], "→ keeper: " + why + _dispatch_note(sent))
            _todo_hist({"event": "reminder", "id": it["id"], "type": it.get("type"),
                        "detail": {"action": "handoff", "reason": why}})
        if flag:
            st = flag["status"]
            if st == "completed":
                if last_op > flag_ts:
                    continue             # operator has looked; closing is theirs
                if not w.get("review_ping"):
                    _watch_update(it["id"], {"review_ping": now})
                    _todo_b_comment(it["id"], "✔ flagged completed — operator review "
                                    "needed before closing (one ping; new activity re-arms)")
                continue
            if st in ("operator_needed", "fail"):
                if last_op > flag_ts:
                    direction = str(op_cmts[-1].get("text") or "")[:600]
                    _handoff(f"operator commented after the {st} flag", direction)
                    _set_flag(it["id"], "in_process", "keeper hand-off", direction[:200])
                continue                 # ignore silently until the operator speaks
            if st == "in_process":
                if last_non_b > int(w.get("last_dispatch") or 0):
                    continue             # progress since the last push: nothing to do
                _handoff("in_process but no operator/A activity since the last push — "
                         "confirm progress against the completion indicator",
                         flag.get("detail", ""))
                continue
        # A's OWN stated reason is the primary redundancy signal, applied
        # DETERMINISTICALLY: when the newest response since the last dispatch
        # is A's and it names an inability, withhold without consulting B —
        # B's reading of A's reasons is fragile (observed: it re-pinged over
        # an explicit credentials blocker), so B only judges ambiguous cases.
        last_c = next((c for c in reversed(comments)
                       if str(c.get("by")) != "B"), None)
        if last_c and str(last_c.get("by")) == "A":
            t = str(last_c.get("text") or "").lower()
            if any(m in t for m in (
                    "could not complete", "cannot complete", "can't complete",
                    "unable to", "no access", "not have access", "do not have",
                    "operator must", "requires the operator", "need the operator",
                    "needs the operator", "missing credential", "blocked on",
                    "waiting on", "waiting for the operator")):
                quote = str(last_c.get("text") or "")[:140]
                _watch_update(it["id"], {"withheld": "A-stated blocker: " + quote,
                                         "withheld_ts": now})
                _todo_b_comment(it["id"],
                                "⏱ withholding reminders — A's last response "
                                "reports a blocker: “" + quote + "” "
                                "(new activity re-arms)")
                _todo_hist({"event": "reminder", "id": it["id"],
                            "type": it.get("type"),
                            "detail": {"action": "withhold",
                                       "reason": "A-stated blocker (deterministic)",
                                       "attempt": int(w.get("dispatches") or 0)}})
                continue
        n = int(w.get("dispatches") or 0)
        nv = int(w.get("verdicts") or 0)
        # 1.0.146 (t4260 / d4254): NO attempt cap — only the _REMIND_MIN_GAP
        # coalesce above spaces reminders; the delivery count rides the comment
        payload = {"item": {k: it.get(k) for k in
                            ("id", "type", "text", "note", "priority",
                             "status", "by")},
                   "response_history": [{"by": c.get("by"), "ts": c.get("ts"),
                                         "text": str(c.get("text"))[:400]}
                                        for c in non_b[-8:]],
                   "dispatches_so_far": n, "quiet_secs": quiet}
        try:
            reply = await loop.run_in_executor(
                None, _todo_keeper_chat,
                [{"role": "system", "content": _REMIND_SYS % (name, it["id"])},
                 {"role": "user", "content": json.dumps(payload)}], 700)
        except Exception:
            break                        # gateway down: fail CLOSED, no blind ping
                                         # (break, not return, so what is already
                                         #  composed still reaches the queue below)
        v = _parse_json_obj(reply)
        action = str(v.get("action") or "").strip().lower()
        reason = str(v.get("reason") or "").strip()[:300]
        _watch_update(it["id"], {"verdicts": nv + 1})
        fl = v.get("flag") if isinstance(v.get("flag"), dict) else {}
        if fl.get("status") in TODO_FLAGS:
            _set_flag(it["id"], fl["status"], fl.get("reason", ""), fl.get("detail", ""))
        if action == "remind":
            hist = "\n".join(f"- {c.get('by')}: {str(c.get('text'))[:200]}"
                              for c in comments[-4:])
            a_prompt = (str(v.get("prompt") or "").strip()
                        or (f"Reminder (attempt {n + 1}) for board item "
                            f"{it['id']} ({it.get('type')}): {it.get('text')!r}.\n"
                            f"Prior responses:\n{hist or '- none'}\n"
                            "Either make concrete progress now or state "
                            "precisely what is missing; update the board item "
                            "either way."))
            sent, dst = _frontier_dispatch(f"remind-{it['id']}", a_prompt,
                                           item=it, why=reason)
            if dst is None:
                continue
            _watch_update(it["id"], {"dispatches": n + 1, "last_dispatch": now})
            _todo_b_comment(it["id"],
                            f"\u23f1 reminder \u2192 frontier (delivery #{n + 1}, no cap; "
                            f"next after {_REMIND_MIN_GAP // 60} min): "
                            + (reason or "still open past the push cadence")
                            + _dispatch_note(sent))
            _todo_hist({"event": "reminder", "id": it["id"], "type": it.get("type"),
                        "detail": {"action": "remind", "reason": reason,
                                   "attempt": n + 1}})
        elif action in ("withhold", "operator"):
            _watch_update(it["id"], {"withheld": (("operator: " if action == "operator" else "")
                                                 + (reason or "redundant to re-ping")),
                                     "withheld_ts": now})
            _todo_b_comment(it["id"],
                            ("\u23f1 needs operator: " if action == "operator"
                             else "\u23f1 withholding reminders: ")
                            + (reason or "the last response names a blocker a "
                               "retry cannot clear")
                            + " (new activity re-arms reminders)")
            _todo_hist({"event": "reminder", "id": it["id"], "type": it.get("type"),
                        "detail": {"action": action, "reason": reason,
                                   "attempt": n}})
    # one queued item for the whole cycle, then cap the audit copies
    try:
        await _flush_dispatch_batch(app)
    except Exception:
        _DISPATCH_BATCH[:] = []
    _prompt_inbox_sweep()


async def _todo_reminder_loop(app):
    # flow: docs/flows/todo-push-triage.flow.json (proposed redesign; see docs/FLOWS.md)
    while True:
        await asyncio.sleep(_REMIND_TICK)
        try:
            await _loops_cycle(app)          # ⚠ loop detector rides the same tick (1.0.118)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if not FV_TODO_REMIND or not REMIND_MITIGATOR:
            continue                     # 1.0.144: the digest (_digest_loop) reminds; B is out
        try:
            _mint_cfg_relay()
            await _todo_reminder_cycle(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


# ── 1.0.144 [F3.8]: the open-todos digest, delivered INTO each keeper session ──
# One deterministic DELTA digest per delivery target (_delivery_targets: the host
# + every managed locus) per cadence (STATION_TODO_DIGEST_SECS, default 1800, floor
# 300), for as long as anything is open — no B, no attempt cap. An unchanged board
# is skipped (recorded) only while the last delivery is under 2 h old. Never while the
# keeper session is mid-turn (deferred, retried every tick), never to a tmux seat,
# and every outcome (delivered / queued / NOT delivered + why) is recorded on the
# locus's board in ONE "[digest]" bookmark row (state changes only, no row spam).
DIGEST_ON = os.environ.get("STATION_TODO_DIGEST", "1").strip().lower() not in ("0", "off", "no", "false")
DIGEST_CADENCE = TD.cadence(os.environ.get("STATION_TODO_DIGEST_SECS") or TD.DEFAULT_CADENCE_S)
DIGEST_TICK = max(10, int(os.environ.get("STATION_TODO_DIGEST_TICK") or "60"))
DIGEST_TOP_N = max(1, int(os.environ.get("STATION_TODO_DIGEST_TOP") or TD.DEFAULT_TOP_N))
DIGEST_MAX_CHARS = max(400, int(os.environ.get("STATION_TODO_DIGEST_MAX_CHARS") or TD.DEFAULT_MAX_CHARS))


def _digest_state_path(locus):
    d = _locus_state_dir(locus)
    d.mkdir(parents=True, exist_ok=True)       # the state MUST persist: it is the flood guard
    return d / "todo-digest.json"


async def _digest_rows(app, locus):
    rows = []
    for st in TD.OPEN_STATUSES:
        got = await _ts_call(app, "todo/list", {"locus": locus, "status": st, "limit": 500}, timeout=15)
        rows.extend(r for r in (got or []) if isinstance(r, dict))
    return rows


async def _keeper_busy(app, base):
    """(sid, busy|None, why) of a serve's keeper session. busy None = unknown."""
    try:
        sid, why = await _keeper_console_head(app, base)
    except Exception as e:                               # noqa: BLE001 — serve down
        return "", None, "serve unreachable: %s" % str(e)[:120]
    if not sid:
        return "", None, why
    route = "/api/console/queue?id=" if sid.startswith("cs-") else "/api/session/queue?id="
    try:
        st, q = await _serve_get(app, route + sid, base=base)
    except Exception as e:                               # noqa: BLE001
        return sid, None, "queue unreadable: %s" % str(e)[:120]
    if st != 200 or not isinstance(q, dict):
        return sid, None, "queue HTTP %s" % st
    return sid, bool(q.get("busy")), why


async def _digest_board_record(app, locus, state, text_line, note):
    """Keep ONE '[digest]' bookmark row on the locus's board current."""
    rid = state.get("board_id") or ""
    try:
        if not rid:
            got = await _ts_call(app, "todo/list", {"locus": locus, "type": "bookmark", "limit": 200}, timeout=15)
            rid = next((str(r.get("id")) for r in (got or []) if isinstance(r, dict)
                        and str(r.get("text") or "").startswith("[digest]")
                        and str(r.get("status")) != "done"), "")
        if not rid:
            res = await _ts_call(app, "todo/add", {"text": text_line[:500], "type": "bookmark", "priority": "low",
                                                    "note": note[:2000], "by": "station", "source": "station.digest",
                                                    "locus": locus}, timeout=15)
            rid = str((res or {}).get("id") or "") if isinstance(res, dict) else ""
        else:
            await _ts_call(app, "todo/update", {"id": rid, "text": text_line[:500], "note": note[:2000]}, timeout=15)
        state["board_id"] = rid
    except Exception as e:                               # noqa: BLE001 — board down: the file keeps it
        _rlog.warning("digest board record %s: %s", locus, e)


async def _digest_once(app, target, now=None, send=None):
    """One digest decision + (maybe) delivery for one target. -> the state.
    send(text, base) -> (ok, detail) is injectable (tests). The digest is a
    DELTA against the snapshot the last DELIVERED digest carried."""
    now = time.time() if now is None else now
    locus = target["locus"]
    path = _digest_state_path(locus)
    state = _read_json(path, {}) or {}
    rows = await _digest_rows(app, locus)
    sig = TD.signature(rows)
    n = len(TD.digest_rows(rows))
    prev = state.get("last_state") or ""

    async def record(st, detail):
        changed = (st, detail) != (prev, state.get("last_detail_rec"))
        state.update(last_state=st, last_attempt=now, open=n, last_detail=detail)
        if st in ("delivered", "queued") or changed:
            state["last_detail_rec"] = detail
            nxt = time.strftime("%H:%M", time.localtime((state.get("last_delivered") or now) + DIGEST_CADENCE))
            line = ("[digest] open-todos → keeper session @%s: %s %s (%d open; next ≈%s)"
                    % (locus, st, time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), n, nxt))
            await _digest_board_record(app, locus, state, line,
                                       "station %s · %s · %s" % (_station_id(), st, detail))
            _audit_line("todo-digest", "%s: %s (%d open) %s" % (locus, st, n, str(detail)[:160]))
        _write_json_atomic(path, state)
        return state

    if n == 0:
        return (await record("idle", "nothing open — no digest")) if prev != "idle" else state
    last = float(state.get("last_delivered") or 0)
    if last and now - last < DIGEST_CADENCE:
        return state                                     # "wait": no network at all
    if TD.decide(state, n, now, DIGEST_CADENCE, False, sig) == "skip":
        return await record("skipped", "unchanged since the digest delivered %s (re-sent after %d h)"
                            % (time.strftime("%H:%M", time.localtime(last)), TD.UNCHANGED_FLOOR_S // 3600))
    base = AC_UPSTREAM if target.get("host") else (await _ac_resolve(target["vm"]))[0]
    sid, busy, why = ("", None, "no reachable serve") if not base else await _keeper_busy(app, base)
    if TD.decide(state, n, now, DIGEST_CADENCE, busy, sig) == "unreachable":
        return await record("NOT delivered", why)
    # 1.0.146 (t4260 / d4254): a BUSY keeper no longer "defers" the digest for an
    # unbounded number of ticks — it is submitted through the same path the comms
    # nudge uses (_nudge_serve → POST /api/console/chat), which QUEUES it behind
    # the running turn; serve sends it at the turn boundary. State "queued" with
    # the queue message id; the old deferred state is gone.
    text, _n = TD.build(rows, locus, now, DIGEST_CADENCE, prev_snap=state.get("snapshot"),
                        top_n=DIGEST_TOP_N, max_chars=DIGEST_MAX_CHARS)
    try:
        ok, detail = await (send or (lambda t, b: _nudge_serve(app, t, b)))(text, base)
    except Exception as e:                               # noqa: BLE001
        ok, detail = False, "serve error: %s" % str(e)[:160]
    if not ok:
        return await record("NOT delivered", detail)
    sub = (app.get("_rt") or {}).get("nudge_last_submit") or {}
    mids = [str(m) for m in (sub.get("message_ids") or [])] if str(sub.get("sid") or "") in (sid, "") or not sid else []
    queued = bool(sub.get("queued")) or "queued" in str(detail)
    state.update(last_delivered=now, last_sig=sig, snapshot=TD.snapshot(rows),
                 deliveries=int(state.get("deliveries") or 0) + 1, last_chars=len(text),
                 queue_message_ids=mids if queued else [], queued_behind=(sid if (queued and busy) else ""))
    if queued:
        detail = "%s%s" % (detail, (" — message %s queued behind the running turn of %s" % (",".join(mids), sid))
                           if mids else " — queued behind the running turn")
    return await record("queued" if queued else "delivered", detail)


async def _digest_loop(app):
    await asyncio.sleep(min(30, DIGEST_TICK))
    while True:
        for tgt in _delivery_targets("digest"):
            try:
                await _digest_once(app, tgt)
            except asyncio.CancelledError:
                raise
            except Exception as e:                       # noqa: BLE001
                _rlog.warning("todo digest %s: %s", tgt.get("locus"), e)
        await asyncio.sleep(DIGEST_TICK)


async def _start_digest(app):
    if DIGEST_ON:
        app["todo_digest"] = asyncio.create_task(_digest_loop(app))


async def _stop_digest(app):
    t = app.get("todo_digest")
    if t:
        t.cancel()


async def _start_reminders(app):
    if os.environ.get("STATION_DEV_QUIET") == "1":   # dev copy: never act on live seats
        return
    if FV_TODO_REMIND:
        _mint_cfg_relay()
    try:
        _switch_strip_rows()                 # 1.0.146: an inert code default is visible on the strip
    except Exception:                        # noqa: BLE001
        pass
    # the loop starts even with reminders off: the ⚠ loop detector needs the tick
    app["_todo_reminder"] = asyncio.create_task(_todo_reminder_loop(app))


async def _stop_reminders(app):
    t = app.get("_todo_reminder")
    if t is not None:
        t.cancel()


# --- ⚠ crash-loop / retry-loop detector (1.0.118) ---------------------------
# Operator, 2026-09-29: "I've never once seen the station inform me of any
# crash loops." Three loops ran silently that day (a PEFT adapter load retried
# forever on a permanent error, a health probe pinging Coder-Next on a timer,
# the serve reducer stacking JSON calls on the one Coder-Next slot). The pure
# logic lives in loop_detector.py (tested over fixtures); this block only
# FETCHES (systemctl show, central /llm/jobs?live=1, /api/llm/queue,
# /llm/calls, /llm/workers) and ACTS: one ✉ keeper mail per loop (30 min
# cooldown), one board todo per loop with the exact command, cleared when the
# loop stops. GET /api/loops serves the set to the ⚠ strip and other tools.
try:
    import loop_detector as _loopdet
except ImportError:                                  # pragma: no cover — packaging error
    _loopdet = None

LOOPS_STATE_PATH = FV_STATE_HOME / "loops.json"
_LOOP_UNITS_USER = [u.strip() for u in (os.environ.get("STATION_LOOP_UNITS_USER") or
                    "abstract-claude-serve@station.service,hugpy-station-web.service,"
                    "hugpy-station-board.service,7006_hugpy_station.service,"
                    "hugpy-agent-serve.service").split(",") if u.strip()]
_LOOP_UNITS_SYSTEM = [u.strip() for u in (os.environ.get("STATION_LOOP_UNITS_SYSTEM") or
                      "7002_hugpy_api.service,7004_hugpy_toolserver.service").split(",") if u.strip()]
# 1.0.123: PEER loci on this host — another station user's own units (tonight's
# 120-restart hugpy-station-web.service belonged to user hugpy and was invisible to
# a detector that only read its own user manager). Peers = STATION_LOOP_PEER_USERS
# (default "hugpy") ∪ ac-loci.json names that are local accounts served on loopback.
_LOOP_PEER_USERS_ENV = [u.strip() for u in (os.environ.get("STATION_LOOP_PEER_USERS", "hugpy")).split(",")
                        if u.strip()]
_LOOP_PEER_UNITS = [u.strip() for u in (os.environ.get("STATION_LOOP_PEER_UNITS") or
                    "hugpy-station-web.service,abstract-claude-serve-station.service,"
                    "hugpy-agent-serve.service").split(",") if u.strip()]
_LOOP_PEER_METHOD = {}                               # user -> "sudo-u" | "machine" | "unreachable"
_LOOP_CENTRAL = (os.environ.get("STATION_LOOP_CENTRAL") or _LOCAL_HUGPY_FRONT).rstrip("/")
_LOOP_CALLS_LIMIT = int(os.environ.get("STATION_LOOP_CALLS_LIMIT") or "200")
_LOOPS = None
_LOOPS_BUSY = False


def _loops():
    global _LOOPS
    if _LOOPS is None and _loopdet is not None:
        cfg = {}
        dc = os.environ.get("STATION_LOOP_DECLARED_CALLERS")
        if dc is not None:
            cfg["declared_callers"] = tuple(x.strip() for x in dc.split(",") if x.strip())
        if os.environ.get("STATION_LOOP_CALLER_BUDGET"):
            cfg["caller_token_budget_day"] = int(os.environ["STATION_LOOP_CALLER_BUDGET"])
        _LOOPS = _loopdet.LoopDetector(state_path=str(LOOPS_STATE_PATH), **cfg)
    return _LOOPS


def _loops_parse_show(text, scope):
    """`systemctl show -p Id,NRestarts,ActiveState,SubState,Result u1 u2 …`
    prints one KEY=VALUE block per unit, blank-line separated."""
    rows, cur = [], {}
    for ln in (text or "").splitlines() + [""]:
        ln = ln.strip()
        if not ln:
            if cur.get("Id"):
                if cur.get("LoadState") == "not-found":
                    cur = {}
                    continue
                rows.append({"unit": cur["Id"], "scope": scope,
                             "nrestarts": cur.get("NRestarts") or 0,
                             "active_state": cur.get("ActiveState") or "",
                             "sub_state": cur.get("SubState") or "",
                             "result": cur.get("Result") or ""})
            cur = {}
            continue
        k, _, v = ln.partition("=")
        cur[k] = v
    return rows


def _loops_peer_users():
    import pwd
    me = getpass.getuser()
    names = list(_LOOP_PEER_USERS_ENV)
    try:
        for name, v in (_ac_loci() or {}).items():
            url = str((v or {}).get("url") or "")
            if "127.0.0.1" in url or "localhost" in url:
                names.append(str(name))
    except Exception:                                    # noqa: BLE001
        pass
    out = []
    for n in names:
        if n == me or n in (u for u, _ in out):
            continue
        try:
            out.append((n, pwd.getpwnam(n).pw_uid))
        except KeyError:                                 # a remote locus, not an account here
            continue
    return out


async def _loops_peer_units():
    """Each peer user's station units, read without a password: `sudo -n -u <user>
    XDG_RUNTIME_DIR=/run/user/<uid> systemctl --user show` (works for vm_mgr on ae),
    else `systemctl --user -M <user>@ show` (needs polkit/root; kept as fallback)."""
    rows = []
    props = ["show", "-p", "Id,LoadState,NRestarts,ActiveState,SubState,Result"] + _LOOP_PEER_UNITS
    for user, uid in _loops_peer_users():
        tries = [("sudo-u", ["sudo", "-n", "-u", user, "env", "XDG_RUNTIME_DIR=/run/user/%d" % uid,
                             "systemctl", "--user"] + props),
                 ("machine", ["systemctl", "--user", "-M", user + "@"] + props)]
        if _LOOP_PEER_METHOD.get(user) == "machine":
            tries.reverse()
        got = None
        for method, argv in tries:
            try:
                rc, out, _err = await asyncio.wait_for(_run(*argv), timeout=10)
            except Exception:                            # noqa: BLE001
                continue
            parsed = [r for r in _loops_parse_show(out, "user") if r["unit"] in _LOOP_PEER_UNITS]
            if rc == 0 and "Id=" in (out or ""):
                got = (method, parsed)
                break
        _LOOP_PEER_METHOD[user] = got[0] if got else "unreachable"
        for r in (got[1] if got else []):
            r["owner"], r["uid"] = user, uid
            rows.append(r)
    return rows


async def _loops_units():
    rows = []
    for scope, units in (("user", _LOOP_UNITS_USER), ("system", _LOOP_UNITS_SYSTEM)):
        if not units:
            continue
        argv = ["systemctl"] + (["--user"] if scope == "user" else []) + \
               ["show", "-p", "Id,NRestarts,ActiveState,SubState,Result"] + units
        try:
            rc, out, _err = await asyncio.wait_for(_run(*argv), timeout=10)
        except Exception:                                    # noqa: BLE001 — no systemd / no bus
            continue
        # LoadState=not-found units still print a block (NRestarts=0) — harmless
        rows.extend(r for r in _loops_parse_show(out, scope) if r["unit"] in units)
    try:
        rows.extend(await _loops_peer_units())
    except Exception:                                    # noqa: BLE001
        pass
    return rows


async def _loops_get(app, path, timeout=6):
    sess = app.get("proxy_sess")
    if sess is None:
        sess = app["proxy_sess"] = aiohttp.ClientSession()
    async with sess.get(_LOOP_CENTRAL + path, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        if r.status != 200:
            return None
        return await r.json(content_type=None)


def _loops_central_watcher():
    """True on the ONE station that watches central's shared sources
    (STATION_LOOP_CENTRAL_WATCHER = a locus name; default the keeper locus)."""
    want = (os.environ.get("STATION_LOOP_CENTRAL_WATCHER") or KEEPER_TARGET).strip().lower()
    return _notify_origin() == want


async def _loops_cycle(app):
    """One detector pass. Every source is best-effort: a central that is down
    simply contributes nothing this tick (the systemd source still runs)."""
    global _LOOPS_BUSY
    det = _loops()
    if det is None or _LOOPS_BUSY:
        return None
    _LOOPS_BUSY = True
    try:
        now = time.time()
        try:
            det.observe_units(await _loops_units(), now)
        except Exception:                                    # noqa: BLE001
            pass
        # 1.0.125 fleet dedupe: central's calls/jobs/queue/workers are ONE shared
        # source — only the designated central-watcher station reads them (default:
        # the keeper station); every other station watches its own units/logs only.
        central = [] if not _loops_central_watcher() else [
                         ("/llm/jobs?live=1", lambda d: det.observe_jobs((d or {}).get("jobs") or [], now)),
                         ("/api/llm/queue", lambda d: det.observe_queue(d or {}, now)),
                         ("/llm/calls?limit=%d" % _LOOP_CALLS_LIMIT,
                          lambda d: det.observe_calls((d or {}).get("calls") or [], now)),
                         ("/llm/workers", lambda d: det.observe_workers(d if isinstance(d, list) else [], now))]
        for path, fn in central:
            try:
                fn(await _loops_get(app, path))
            except Exception:                                # noqa: BLE001 — central down / shape drift
                continue
        ev = det.finish_cycle(now)
        await _loops_act(app, det, ev, now)
        _b_findings_publish([dict(_b_loop_finding(r), emit_reason="new") for r in ev.get("new") or []])
        return ev
    finally:
        _LOOPS_BUSY = False


async def _loops_act(app, det, ev, now):
    """Loops are bug reports: they go to the WORKER serve session of this
    station's locus (bug_route, operator 2026-10-01) — never the keeper. One
    report per loop sighting due (the detector's 30 min cadence coalesces the
    rest, ×N), the board item resolved when the loop stops. Never raises: the
    outbox retries an undelivered report on every cycle."""
    for state in ("mail", "new", "cleared"):
        for row in ev.get(state) or []:
            _todo_hist({"event": "loop", "key": row["key"], "source": row["source"],
                        "identity": row["identity"], "count": row.get("count"), "state": state})
    if _kn is None:
        return
    book = _notify_book()
    if book is not None:
        await _loops_read_dispositions(book, det, _BugBoard(app), _issue_gate(app))
        # drops a decided loop ONLY under STATION_NOTIFY_INERT_SILENCES=1 (t4178)
        ev, quiet = _kn.gate_loops(book, ev, now)
        for row in quiet:
            det.mark_mailed(row, now)                    # keeps the 30 min cadence of the log line
            _todo_hist({"event": "loop-decided", "key": row["key"], "identity": row["identity"],
                        "count": row.get("count"),
                        "disposition": book.sig(_kn.loop_sigkey(row)).get("disposition")})
        if quiet:
            book.save()
            det.save()

    def resolve_note(row):
        return ("loop stopped %s — auto-resolved by the station loop detector (it saw no "
                "recurrence for one cycle); reopen if it returns.\n\n%s"
                % (time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), row.get("detail") or ""))[:2000]

    try:
        await _report_loops(app, det, book, ev, now, resolve_note)
        await _bug_pump(app)
        box = _bug_outbox()
        for row in det.loops.values():                   # board ids back onto the loop rows
            e = (box.items.get("loop:" + str(row.get("key"))) if box else None) or {}
            if e.get("board_id"):
                row["board_id"] = e["board_id"]
    except Exception as e:                               # noqa: BLE001
        _rlog.warning("loop reports: %s", e)
    if ev.get("new") or ev.get("cleared") or ev.get("mail") or ev.get("updated"):
        det.save()


def _loop_gate_spec(row, origin=""):
    """issue_observe kwargs for one loop sighting (1.0.140): the loop's identity
    is the subject (the gate strips job ids / prompt hashes), its caller /
    session the self-origin inputs."""
    meta = {"key": row.get("key"), "origin": origin, "action": str(row.get("action") or "")[:200]}
    if row.get("ids"):
        meta["ids"] = list(row["ids"])[:10]
    if row.get("session_id"):
        meta["session_id"] = row["session_id"]
    if row.get("unit"):
        meta["unit"] = row["unit"]
    return {"source": "station-loops", "kind": "loop:%s" % (row.get("source") or "other"),
            "subject": str(row.get("identity") or ""), "text": str(row.get("detail") or "")[:600],
            "severity": row.get("severity") or "warn", "caller": row.get("caller") or "",
            "meta": meta, "n": 1}


async def _report_loops(app, det, book, ev, now, resolve_note):
    """New / due loop rows -> bug reports (the outbox coalesces + retries); the
    issue registry annotates (fail open); cleared loops resolve their item."""
    box = _bug_outbox()
    if box is None:
        return 0
    gate, origin, seen, n = _issue_gate(app), _notify_origin(), set(), 0
    for row in list(ev.get("new") or []) + list(ev.get("mail") or []):
        if id(row) in seen:
            continue
        seen.add(id(row))
        res = await _bug_observe(gate, _loop_gate_spec(row, origin))
        if res:
            row["issue_fp"] = res.get("fp")
        sig = book.sig(_kn.loop_sigkey(row)) if book is not None else {}
        disp = sig.get("disposition") or "open"
        ann = " · ".join(x for x in (("%s%s" % (disp, (": " + sig["reason"]) if sig.get("reason") else ""))
                                     if disp != "open" else "", (res or {}).get("annotation") or "",
                                     ("issue_fp=" + row["issue_fp"]) if row.get("issue_fp") else "") if x)
        text, note = _loopdet.fmt_board(row)
        box.offer("loop:" + str(row["key"]), vm="", locus=_bug_locus(""), kind="loop:%s" % row.get("source"),
                  title=text, body=_loopdet.fmt_mail(row), severity="high" if row.get("severity") == "crit"
                  else "medium", count=row.get("count"), source=str(row.get("unit") or row.get("source") or ""),
                  caller=str(row.get("caller") or row.get("session_id") or ""), annotation=ann,
                  priority="low" if disp in ("inert", "accepted") else None)
        det.mark_mailed(row, now)
        n += 1
    for row in ev.get("cleared") or []:
        box.clear("loop:" + str(row["key"]), resolve_note(row))
    box.save()
    return n


async def _loops_read_dispositions(book, det, sink, gate=None):
    """A loop's board item closed with a disposition line (`inert: <reason>`,
    `accept`, `reject: <reason>`) records the decision on the loop's signature,
    once per board item. Never raises: toolserver down = next cycle. 1.0.140:
    every close (a plain close = processed) is forwarded to the issue registry."""
    ids = {str(r["board_id"]): r for r in det.loops.values()
           if r.get("board_id") and r.get("disp_read") != r["board_id"]}
    if not ids:
        return
    try:
        closed = await sink.closed(list(ids))
    except Exception:                                    # noqa: BLE001
        return
    for bid, note in (closed or {}).items():
        row = ids.get(str(bid))
        if row is None:
            continue
        row["disp_read"] = row["board_id"]
        disp, reason = _kn.parse_disposition(note)
        if disp:
            skey = _kn.loop_sigkey(row)
            book.sig(skey)
            book.set_disposition(skey, disp, reason, by="keeper")
            _todo_hist({"event": "loop-disposition", "board": bid, "key": row["key"],
                        "disposition": disp, "reason": reason})
        if gate is not None and row.get("issue_fp") and not (disp is None and _ig.is_auto_resolve(note)):
            final = disp or "closed"
            if final == "accepted":
                await gate.record_action(row["issue_fp"], "keeper accepted loop fix (board %s)" % bid,
                                         note=reason, fix=True, board_id=bid)
            await gate.disposition(row["issue_fp"], final, reason or final, by="keeper", board_id=bid)
    book.save()
    det.save()


async def api_loops(request):
    """GET /api/loops — the current crash/retry loop set: {ok, ts, active:[…],
    recent:[…] (cleared inside the last hour), config:{thresholds}}. ?refresh=1
    runs a detector pass first (rate-limited by the busy flag)."""
    det = _loops()
    if det is None:
        return web.json_response({"ok": False, "error": "loop_detector module missing"}, status=503)
    if request.query.get("refresh") == "1":
        try:
            await _loops_cycle(request.app)
        except Exception:                                    # noqa: BLE001
            pass
    snap = det.snapshot()
    snap["ok"] = True
    try:
        snap["findings"] = _notify_book().strip_rows() if _notify_book() is not None else []
    except Exception:                                    # noqa: BLE001
        snap["findings"] = []
    # 1.0.146 (t4260): every station hold / skip / coalesce / inert switch, visible
    snap["holds"] = _hold_rows()
    try:
        snap["config"]["switches"] = _station_switches()
    except Exception:                                    # noqa: BLE001
        pass
    vm, key = _locus_scope(request)          # 1.0.141: never this host's loops under another locus
    snap["scope"] = "this station"
    if vm:
        for k in ("active", "recent", "findings"):
            if isinstance(snap.get(k), list):
                snap[k] = _rows_for_locus(snap[k], key)
        snap["locus"] = key
        snap["scope"] = "this station's detector, filtered to " + key
        return web.json_response(snap)
    try:
        snap["config"]["units"] = {"user": _LOOP_UNITS_USER, "system": _LOOP_UNITS_SYSTEM}
        snap["config"]["central_watcher"] = _loops_central_watcher()
        snap["config"]["declared_callers"] = list(det.cfg.get("declared_callers") or ())
        snap["config"]["peers"] = [
            {"user": u, "uid": uid, "units": _LOOP_PEER_UNITS,
             "method": _LOOP_PEER_METHOD.get(u, "not-yet-read"),
             "seen": {k.split("/", 1)[1]: list(v)[-1][1] for k, v in det._units.items()
                      if k.startswith(u + "/") and v}}
            for u, uid in _loops_peer_users()]
    except Exception:                                    # noqa: BLE001
        pass
    return web.json_response(snap)


async def vm_keeper_alias(request):
    """1.0.42: /api/vm/<station>/{todo-history,todo/assist,todo-revision,messages}
    -> that station's OWN workbench, rewritten to its /api/vm/keeper/... route.
    The ☑ board now follows the station selection (index.html), and these
    keeper-only tabs 404'd for any VM because only the literal "keeper" name
    had them. The literal keeper routes are registered first and still win."""
    vm = request.match_info["vm"]
    if vm in _HOST_VMS:
        raise web.HTTPNotFound()          # never recurse into ourselves ("host" = explicit alias, 2026-08-27)
    resp = await _central_vm_reroute(request)   # 1.0.138: todo/assist on the locus's central rows
    if resp is not None:
        return resp
    tail = request.path.split(f"/api/vm/{vm}/", 1)[1]
    return _retired(tail, vm)


async def vm_host_alias(request):
    """/api/vm/host/* — the EXPLICIT address for the host locus
    (docs/NOMENCLATURE.md, 2026-08-27 consolidation: keeper names agents,
    never machines; the machine is "host"). 308 preserves the method and
    body; the literal "keeper" routes stay the canonical wire form."""
    raise web.HTTPPermanentRedirect("/api/vm/@self/" + request.match_info["tail"])


async def keeper_todo_history(request):
    """GET /api/vm/keeper/todo-history -> {ok, events} (bugreport-api shape):
    tail the session journal, skip malformed lines, oldest -> newest."""
    p = _todo_history_path()
    if not p.exists():
        return web.json_response({"ok": False, "error": "no history yet"})
    try:
        data = p.read_bytes()
    except OSError as e:
        return web.json_response({"ok": False, "error": str(e)})
    if len(data) > 1024 * 1024:
        data = data[-(1024 * 1024):].split(b"\n", 1)[-1]  # tail; drop partial line
    events = []
    for ln in data.decode("utf-8", "replace").splitlines():
        ln = ln.strip()
        if ln:
            try:
                events.append(json.loads(ln))
            except ValueError:
                continue
    return web.json_response({"ok": True, "events": events[-400:]})


def _todo_keeper_chat(messages, max_tokens):
    return _hugpy_chat(messages, max_tokens)


def _parse_items_reply(text):
    """Pull a JSON array of items out of a model reply (fences/prose tolerated).
    Verbatim console-api port."""
    t = text or ""
    m = re.search(r"```[a-zA-Z]*\s*\n(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1)
    a, b = t.find("["), t.rfind("]")
    if a < 0 or b <= a:
        return []
    t = t[a:b + 1]
    for candidate in (t, re.sub(r",\s*([}\]])", r"\1", t)):
        try:
            data = json.loads(candidate)
            return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
        except ValueError:
            continue
    return []


_TODO_FORMAT_PROMPTS = {
    "readability": "Restructure the text below for visual readability using minimal markdown: short paragraphs separated by blank lines; '- ' bullet lists where content enumerates; fenced code blocks (```bash or ```) around commands, paths-with-flags, code, or log excerpts; `inline code` for single identifiers. Preserve every fact, path, command, id, and number EXACTLY as written \u2014 never add, drop, or reword technical content. Return ONLY the restructured markdown, no preamble.",
    "listify": "Convert the text below into a markdown bullet list: one '- ' bullet per distinct point, sub-points indented two spaces under their parent. Preserve every fact, path, command, id, and number EXACTLY as written; add nothing. Return ONLY the list, no preamble.",
    "proposal": "Rewrite the text below as a concise decision brief with exactly three labelled sections: a line 'Pros:' then one '- ' bullet per pro, a line 'Cons:' then one '- ' bullet per con, and a line 'Recommendation:' then a single line naming the recommended choice and the one reason that decides it. Use only pros and cons the text states or directly implies; preserve every fact, path, command, id, and number EXACTLY as written. Return ONLY those three sections, no preamble.",
}


async def keeper_todo_assist(request):
    """POST /api/vm/keeper/todo/assist - the host board's ✨ assist."""
    return await _todo_assist(request)


async def _todo_assist(request, locus=None):
    """The canonical console-api keeper modes:
    mode=add (parse a free-text ask into items, append under the lock),
    mode=tidy (PROPOSE a revised list; applied only via op=replace),
    mode=format (text transform for the compose box / an item's text).
    locus=None is the host file board; a central locus key (1.0.138) reads and
    adds on THAT locus's central `todos` rows instead (the LLM runs here)."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    mode = body.get("mode", "add")
    name = locus or _active_session()
    loop = asyncio.get_event_loop()

    if mode == "add":
        instruction = str(body.get("instruction") or "").strip()[:4000]
        if not instruction:
            return web.json_response({"error": "instruction required"}, status=400)
        prompt = (
            f"You maintain the development to-do list for session '{name}'. "
            f"Convert the user's message into 1 to 6 to-do items. Each item is a "
            f"JSON object: {{\"type\":\"todo|request|bookmark\",\"text\":\"short "
            f"imperative summary\",\"note\":\"optional detail\"}}. Use type "
            f"'request' for asks directed at the keeper, 'bookmark' for "
            f"checkpoints/builds worth returning to, else 'todo'. Output ONLY a "
            f"JSON array of the new items, no prose, no code fences.\n\n"
            f"User message:\n{instruction}")
        try:
            reply = await loop.run_in_executor(
                None, _todo_keeper_chat, [{"role": "user", "content": prompt}], 1200)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=502)
        new = _parse_items_reply(reply)
        if not new:
            return web.json_response({"ok": False,
                                      "error": "model returned no usable items"}, status=502)
        if locus:                         # central board: one todo/add per parsed item
            added = 0
            try:
                for raw in new[:6]:
                    it = todo_norm(raw, "hugpy", 1)
                    if not it:
                        continue
                    await _ts_call(request.app, "todo/add", {
                        "text": it["text"], "type": it.get("type") or "todo",
                        "note": it.get("note") or "", "by": "hugpy",
                        "source": "station:" + _station_id(), "locus": locus}, timeout=15)
                    added += 1
                state = await _central_todo_state(request.app, locus)
            except Exception as e:
                return web.json_response({"ok": False, "error": f"{locus}: central board — {e}"}, status=502)
            return web.json_response({"ok": True, "vm": name, "added": added, "state": state,
                                      "central": True})
        try:
            with _todo_locked():          # o10 v4: never append onto a pre-LLM snapshot
                state, err = _mct_todo_read()
                if state is None:
                    return web.json_response({"ok": False, "error": err}, status=502)
                nid = todo_next_id(state["items"])
                added = 0
                for raw in new[:6]:
                    it = todo_norm(raw, "hugpy", nid)
                    if it:
                        state["items"].append(it)
                        nid += 1
                        added += 1
                _mct_todo_write(state)
        except OSError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=502)
        for it in state["items"][-added:] if added else []:
            _todo_hist({"event": "added", "id": it.get("id"), "type": it.get("type"),
                        "detail": {"text": it.get("text")}})
        return web.json_response({"ok": True, "vm": name, "added": added, "state": state})

    if mode == "tidy":
        instruction = str(body.get("instruction") or "").strip()[:4000]
        if locus:
            try:
                state, err = await _central_todo_state(request.app, locus), ""
            except Exception as e:
                state, err = None, f"{locus}: central board unreachable — {e}"
        else:
            state, err = _mct_todo_read()
        if state is None:
            return web.json_response({"ok": False, "error": err}, status=502)
        prompt = (
            f"You maintain the development to-do list for session '{name}'. Current "
            f"items as JSON:\n{json.dumps(state['items'])}\n\nRevise the list: "
            f"{instruction or 'deduplicate, merge related items, clarify wording, order by priority'}. "
            f"Rules: never drop an item whose status is not 'done' unless it "
            f"duplicates another; keep every bookmark; keep the same JSON item "
            f"shape (id,type,text,note,status,by,ts) and preserve ids of items "
            f"that survive. Output ONLY the full revised JSON array, no prose, "
            f"no code fences.")
        try:
            reply = await loop.run_in_executor(
                None, _todo_keeper_chat, [{"role": "user", "content": prompt}], 2000)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=502)
        proposal = _parse_items_reply(reply)
        if not proposal:
            return web.json_response({"ok": False,
                                      "error": "model returned no usable list"}, status=502)
        return web.json_response({"ok": True, "vm": name, "proposal": proposal})

    if mode == "format":
        kind = str(body.get("kind") or "").strip().lower()
        text = str(body.get("text") or "").strip()[:4000]
        if not text:
            return web.json_response({"error": "text required"}, status=400)
        p = _TODO_FORMAT_PROMPTS.get(kind)
        if not p:
            return web.json_response({"error": f"unknown format kind {kind!r}"}, status=400)
        try:
            reply = await loop.run_in_executor(
                None, _todo_keeper_chat,
                [{"role": "user", "content": p + "\n\nText:\n" + text}], 1500)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=502)
        out_text = (reply or "").strip()
        # replies may legitimately EMIT fences: unwrap only a whole-reply PROSE
        # wrapper (bare/markdown lang, no interior fence) - a ```bash reply IS
        # the answer, never packaging (console-api t99).
        fence = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```$", out_text, re.DOTALL)
        if fence and "```" not in fence.group(1):
            out_text = fence.group(1).strip()
        if not out_text:
            return web.json_response({"ok": False, "error": "model returned empty text"}, status=502)
        return web.json_response({"ok": True, "vm": name, "kind": kind, "result": out_text})

    return web.json_response({"error": f"unknown mode {mode!r}"}, status=400)


_REV_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_REV_TEXT_MAX = 64 * 1024


async def keeper_todo_revision(request):
    """POST /api/vm/keeper/todo-revision - long board-item revisions (o14/t49),
    session-scoped under <ws>/board-revisions/. write {op,id,text} -> {ok,path,
    bytes}; read {op,path} -> {ok,text}; strict per-op key whitelist."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad JSON"}, status=400)
    op = body.get("op")
    allowed = {"write": {"op", "id", "text"}, "read": {"op", "path"}}.get(op)
    if allowed is None:
        return web.json_response({"ok": False, "error": "op must be write|read"}, status=400)
    if set(body) - allowed:
        return web.json_response({"ok": False,
                                  "error": f"unexpected keys for {op}"}, status=400)
    base = (_active_ws() / "board-revisions").resolve()
    if op == "write":
        rid = str(body.get("id") or "")
        text = str(body.get("text") or "")
        if not _REV_ID_RE.match(rid):
            return web.json_response({"ok": False, "error": "bad id"}, status=400)
        if not text.strip():
            return web.json_response({"ok": False, "error": "text required"}, status=400)
        if len(text.encode("utf-8")) > _REV_TEXT_MAX:
            return web.json_response({"ok": False, "error": "text over 64KB"}, status=413)
        try:
            base.mkdir(parents=True, exist_ok=True)
            stem = f"{rid}-{time.strftime('%Y%m%d-%H%M%S')}"
            f = base / (stem + ".md")
            i = 1
            while f.exists():                 # collisions: suffix, never clobber
                f = base / f"{stem}-{i}.md"
                i += 1
            tmp = base / (".rev." + secrets.token_hex(4))
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(f)
        except OSError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response({"ok": True, "path": str(f),
                                  "bytes": len(text.encode("utf-8"))})
    # read
    try:
        p = Path(str(body.get("path") or "")).resolve()
        if not str(p).startswith(str(base) + os.sep):
            return web.json_response({"ok": False,
                                      "error": "path outside board-revisions"}, status=400)
        data = p.read_bytes()[:_REV_TEXT_MAX]
    except OSError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=404)
    return web.json_response({"ok": True, "text": data.decode("utf-8", "replace")})


async def keeper_todo_discord(request):
    """Discord bridge status/attach - not wired in the standalone (yet).
    GET answers honestly unconfigured; POST refuses with a plain reason."""
    if request.method == "GET":
        return web.json_response({"ok": True, "configured": False})
    return web.json_response(
        {"ok": False, "error": "discord bridge not wired in the standalone console yet"})


# =========================================================================== #
# hugpy Station 1.0.41 — EXPLICIT SEAT CONTROL (operator, 2026-08-20)
#
# Everything a frontier (A), local (B), or shell seat is handed at launch is
# operator-readable and operator-settable from the 🛡 steward tab — model,
# directive, filesystem switch, sudo grant, Claude OAuth state — per grounding
# locus (the @keeper host seat or the active VM). Nothing below grants an agent
# any reach it did not already have; it makes the existing reach observable and
# switchable. Rules of the road:
#   * The fs switch applies to the FRONTIER surfaces ONLY (mct + claude-code).
#     Local (B) and Shell are never touched by it.
#   * Sudo is one mechanism (agent-sudo drop-in) on both the host and every VM,
#     grantable and revocable from the same button, with the exact sudoers rule
#     shown. A and B share the seat's unix user, so a grant is per LOCUS, never
#     per model — the UI says so.
#   * 1.0.143 (operator 2026-10-01): directives are DISTINCT per (locus x
#     session kind) — the standing serve sessions keeper / chat / worker /
#     local and the tmux seats — generated by directives.py from the shipped
#     templates (resources/backend/directive-templates/) + the locus's live
#     facts + the operator layers. The tmux seats (claude-code: as
#     --append-system-prompt; mct: as the operator guidance B injects as a
#     governing_instruction; codex: as its launch guidance) get the `tmux`
#     kind; the serve sessions read <state>/directives/rendered/<kind>.md.
# =========================================================================== #
FRONTIER_DIRECTIVE_SHIPPED = DV.TEMPLATE_DIR          # 1.0.143: the templates are the shipped source
# The tmux seats' operator overlay (pre-1.0.143: THE directive file). A legacy
# FULL copy is preserved and reported, never composed (directives.is_legacy_full).
FRONTIER_DIRECTIVE_PATH = FV_STATE_HOME / "frontier-directive.md"
# The seat handoff: the letter a retiring/wiped A seat leaves for its reborn
# successor. Fixed path, never injected into any prompt — the shipped
# directive points A at it, and 🛡 steward → directive shows/edits it.
FRONTIER_HANDOFF_PATH = FV_STATE_HOME / "frontier-handoff.md"
FRONTIER_MODELS_PATH = FV_STATE_HOME / "frontier-models.json"
GPT_CONFIG_PATH = FV_STATE_HOME / "abstract-gpt" / "config.json"
GPT_SOURCE_ROOT = ROOT.parents[3] / "share" / "modules" / "abstract_gpt" / "src"
# B's model (the local keeper: todo triage, ✎ prompt-revise, and — at next
# launch — the mct seat / daemon). Empty = the hugpy package default
# (DEFAULT_AGENT_BRAIN). The console applies it as a load_config override so a
# change takes effect on the NEXT B call; it is also mirrored into agent.env's
# HUGPY_MODEL so the mct seat and daemon pick it up when they next launch.
B_MODEL_PATH = FV_STATE_HOME / "b-model.json"
B_AGENT_ENV = Path.home() / ".config" / "hugpy-agent" / "agent.env"


def _b_model():
    doc = _read_json(B_MODEL_PATH, {}) or {}
    v = doc.get("model")
    return v if isinstance(v, str) and (v == "" or _MODEL_RE.match(v)) else ""


def _b_overrides():
    m = _b_model()
    return {"model": m} if m else {}


def _b_agent_env_write(model):
    """Mirror HUGPY_MODEL into agent.env (preserve every other line). Best
    effort — the console's own B calls use the override regardless."""
    try:
        lines = B_AGENT_ENV.read_text(encoding="utf-8").splitlines() if B_AGENT_ENV.exists() else []
    except OSError:
        return
    out, done = [], False
    for ln in lines:
        if ln.startswith("HUGPY_MODEL=") and not done:
            out.append("HUGPY_MODEL=" + model); done = True
        else:
            out.append(ln)
    if not done:
        out.append("HUGPY_MODEL=" + model)
    try:
        B_AGENT_ENV.parent.mkdir(parents=True, exist_ok=True)
        B_AGENT_ENV.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError:
        pass
FRONTIER_FS_FLAG = FV_STATE_HOME / "frontier-fs.json"
# Claude Code model aliases the CLI accepts for --model, plus "" = the CLI's
# own default (whatever the account resolves). Free text is also accepted
# (full model ids), validated only by shape.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,80}$")
# Default frontier model for BOTH seats (operator, 2026-08-20): Fable. "" in
# frontier-models.json means "Claude Code's own account default" if wanted.
FRONTIER_MODEL_DEFAULTS = {"mct": "claude-fable-5",
                           "claude-code": "claude-fable-5",
                           "codex": "",
                           "hugpy": ""}
# Claude Code tools that touch the filesystem directly. When the frontier fs
# switch is MEDIATED these are hard-denied in the claude-code seat's settings
# (permissions.deny — native Claude Code policy, no hook needed), so A must
# route file work through B / the local keeper. Bash stays available for
# non-file work; the directive tells A to delegate file-touching commands.
FS_DENY_TOOLS = ["Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Grep", "Glob", "LS"]


def _frontier_directive_text(cfg=None):
    """(text, source, path) of the tmux seats' OPERATOR OVERLAY — the steward's
    frontier-directive.md (1.0.143: the generated templates are the base; this
    file only adds to them). cfg (1.0.141) = the TARGET locus's snapshot.
    source: "user" (composed), "legacy" (a pre-1.0.143 full copy: preserved,
    not composed) or "none"."""
    if cfg is not None:
        t = _cfg_text(cfg, "directive") or ""
        path = Path(str(cfg.get("state") or "")) / "frontier-directive.md"
    else:
        path = FRONTIER_DIRECTIVE_PATH
        try:
            t = path.read_text(encoding="utf-8")
        except OSError:
            t = ""
    if not t.strip():
        return "", "none", path
    return t, ("legacy" if DV.is_legacy_full(t) else "user"), path


def _dirv_snapshot(cfg=None):
    """The directive layers in locus_exec.read_cfg shape: the target's own
    snapshot (cfg) or this host's files read now (active guidance ws).
    1.0.146: the `handoff` layer is NEVER a file — it is the toolserver's
    launch pointer fetched by _handoff_layer_refresh() before the launch."""
    if cfg is not None:
        snap = {**cfg, "files": dict(cfg.get("files") or {})}
    else:
        snap = DV.read_state(FV_STATE_HOME, overrides={"guidance": str(_bg_user_path())})
    snap["files"]["handoff"] = _frontier_handoff_pending(cfg)
    return snap


def _dirv_facts(cfg=None):
    """Live facts of the locus a directive is for: a remote snapshot's own
    (user/host/home/state read ON the target), else this machine's now."""
    if cfg is not None and cfg.get("remote"):
        return DV.facts(cfg, locus=str(cfg.get("locus") or ""))
    loc = str((cfg or {}).get("locus") or "") or _station_locus()
    return DV.facts(_dirv_snapshot(cfg), locus=loc, station_port=str(PORT))


def _session_directive(kind, cfg=None, backend="", with_handoff=False):
    """The full directive for one session kind (keeper/chat/worker/local/tmux)."""
    return DV.compose(kind, _dirv_snapshot(cfg), fx=_dirv_facts(cfg), backend=backend,
                      with_handoff=with_handoff)


def _push_directive(vm):
    """The tmux directive a MANAGED guest (lxd / an ssh host without its own
    station) composes its mct guidance from: the generated tmux text with the
    guest's identity + the host's tmux overlay. Its own guidance stays its own."""
    h = _ssh_host(vm) or {}
    snap = {"files": {"directive": (_dirv_snapshot().get("files") or {}).get("directive")}}
    fx = DV.facts(snap, locus=_central_locus(vm) or vm, kind="ssh" if h else "lxd",
                  station_port=str(PORT))
    home = "/home/ubuntu" if not h else "~"
    fx.update(user=h.get("user") or "ubuntu", hostname=h.get("host") or vm,
              facts_source="by the managing station", home=home,
              state=home + "/.config/hugpy-station", ws=home + "/.config/hugpy-station/mct2/repl",
              local_ws=home + "/.config/hugpy-station/local-keeper",
              board_file=home + "/.hugpy/state/todo.json", ac_root=home + "/.config/hugpy-station/abstract-claude",
              app="(no station app on this locus)", docs="the managing station's docs (📖 docs tab)")
    return DV.compose("tmux", snap, fx=fx).rstrip("\n")


def _render_session_directives():
    """Re-derive <state>/directives/rendered/<kind>.md for THIS locus — what
    the serve sessions read every turn ($AC_SESSION_DIRECTIVES_DIR). Called at
    startup and on every directive / guidance / switch save; the serve runner
    re-renders at every serve start too. Best effort, never raises."""
    try:
        d = FV_STATE_HOME / "directives"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "station.json"
        cur = _read_json(p, None)
        st = dict(cur if isinstance(cur, dict) else {}, port=PORT, locus=_station_locus())   # keeps `nuances` (1.0.146)
        if cur != st:
            p.write_text(json.dumps(st) + "\n", encoding="utf-8")
        snap = _dirv_snapshot()
        fx = DV.facts(snap, locus=st["locus"], station_port=str(PORT))
        return DV.write_rendered(FV_STATE_HOME / DV.RENDERED_DIR, DV.render_all(snap, fx), fx)
    except Exception as e:                                # noqa: BLE001
        logging.getLogger("station.directives").warning("render failed: %s", e)
        return []


async def api_frontier_handoff(request):
    """GET/POST the seat handoff for the selected locus — the TOOLSERVER row
    (1.0.146; no file). GET → {text: the init prompt the next seat receives
    (exactly what handoff_pull / the SessionStart hook injects), exists, id,
    state, mtime (requested), layer, path: "toolserver handoff <id>"}.
    POST {"text"} → handoff_request on the locus (by operator-steward, no seat
    spawn); empty text → the open handoff is marked abandoned."""
    vm, bad = await _cfg_vm(request)          # 1.0.141: the SELECTED locus
    if bad is not None:
        return bad
    locus = (_central_locus(vm) or vm) if vm else _station_locus()
    if not locus:
        return _unconfigured_response("handoff")
    app = request.app
    try:
        if request.method == "POST":
            try:
                body = await request.json()
                text = body.get("text", "")
                if not isinstance(text, str):
                    raise ValueError
            except Exception:
                return web.json_response({"error": 'body must be {"text": string}'}, status=400)
            if text.strip():
                row = await _ts_call(app, "handoff/request",
                                     {"locus": locus, "text": text, "by": "operator-steward", "spawn": False},
                                     timeout=20)
                did = f"handoff {row.get('id')} filed on {locus} ({len(text)} chars)"
            else:
                st = await _ts_call(app, "handoff/station", {"locus": locus}, timeout=10)
                row = (st or {}).get("handoff")
                if row:
                    await _ts_call(app, "handoff/claim", {"id": row["id"], "status": "abandoned", "by": "operator-steward"},
                                   timeout=10)
                    did = f"handoff {row['id']} on {locus} abandoned"
                else:
                    did = f"no open handoff on {locus} to discard"
            audit(request, "frontier-handoff", did, ok=True)
        st = await _ts_call(app, "handoff/station", {"locus": locus}, timeout=10)
    except Exception as e:                                   # noqa: BLE001
        return web.json_response({"ok": False, "locus": locus, "error": f"toolserver: {str(e)[:200]}",
                                  "text": "", "exists": False, "mtime": None, "path": ""}, status=502)
    row = (st or {}).get("handoff") or None
    return web.json_response({"ok": True, "locus": locus, "remote": bool(vm),
                              "text": (st or {}).get("init_prompt") or "", "exists": row is not None,
                              "id": (row or {}).get("id", ""), "state": (row or {}).get("status", ""),
                              "by": (row or {}).get("by", ""), "source_session": (row or {}).get("source_session", ""),
                              "mtime": (row or {}).get("created"), "layer": (st or {}).get("layer") or "",
                              "path": (f"toolserver handoff {row['id']} (handoff_pull locus={locus} id={row['id']})"
                                       if row else f"toolserver (no open handoff on {locus})")})


def _frontier_models(cfg=None):
    doc = (_cfg_json(cfg, "models") if cfg is not None else _read_json(FRONTIER_MODELS_PATH, {})) or {}
    if not isinstance(doc, dict):
        doc = {}
    out = dict(FRONTIER_MODEL_DEFAULTS)
    for k in out:
        v = doc.get(k)
        if isinstance(v, str) and (v == "" or _MODEL_RE.match(v)):
            out[k] = v
    return out


def _gpt_config():
    from gpt_station import read_config
    return read_config(GPT_CONFIG_PATH)


def _gpt_launch_cmd(guidance="", remote=False, cfg=None):
    from gpt_station import launch_command
    config = _gpt_config()
    config["default_model"] = _frontier_models(cfg).get("codex", "") or config.get("default_model", "")
    return launch_command(config, GPT_CONFIG_PATH, guidance, remote=remote)


def _gpt_tracker():
    from gpt_station import tracker
    return tracker(KEEPER_TMUX_SOCK)


async def api_gpt(request):
    """Persist GPT launch defaults and report the exact live native session."""
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
    from gpt_station import settings, update_config, wrapper_binary, EFFORTS
    try:
        if request.method == "POST":
            body = await request.json()
            config = update_config(GPT_CONFIG_PATH, body)
            if "default_model" in body:
                models = _frontier_models()
                models["codex"] = config.get("default_model") or ""
                FRONTIER_MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
                FRONTIER_MODELS_PATH.write_text(json.dumps(models, indent=2) + "\n")
            audit(request, "gpt-settings", "updated", ok=True)
        config = _gpt_config()
        config["default_model"] = _frontier_models().get("codex", "") or config.get("default_model", "")
        tracker = await asyncio.to_thread(_gpt_tracker)
        return web.json_response({"settings": settings(config), "efforts": EFFORTS,
                                  "path": str(GPT_CONFIG_PATH), "session": tracker,
                                  "dangerous": tracker["unrestricted"],
                                  "configured_dangerous": settings(config)["dangerous"],
                                  "wrapper": wrapper_binary(), "launch": _gpt_launch_cmd(),
                                  "applies": "next launch"})
    except (ValueError, TypeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except OSError as exc:
        return web.json_response({"error": "GPT settings unavailable: " + str(exc)}, status=503)


def _frontier_fs_mediated(cfg=None):
    """Host-level mirror of the frontier fs switch: True = A's direct filesystem
    access is DENIED (mediated via B). Written by /api/frontier/fs/toggle
    alongside hugpy-agent's own per-workspace policy so BOTH frontier seats
    (mct and claude-code) obey one switch."""
    doc = _cfg_json(cfg, "fs") if cfg is not None else _read_json(FRONTIER_FS_FLAG, None)
    if isinstance(doc, dict) and "mediated" in doc:
        return bool(doc["mediated"])
    return False


# ── Delegation switch (board t7, operator 2026-09-10): per frontier backend, the
# seat acts as a ROUTER — it briefs subagents for every task instead of doing
# the work in its own context. Lets the frontier keeper be the newest model
# without draining tokens on task execution, and keeps it free to take new
# queries while work is in flight. Persisted in frontier-delegate.json; lands
# in the directive (claude-code: next launch; mct: next guidance render) and,
# when that seat is live, as a typed notice right away.
FRONTIER_DELEGATE_FLAG = FV_STATE_HOME / "frontier-delegate.json"
# 1.0.143: the section texts live with the generator (directives.py) — one source.
_DELEGATE_ONLY_TEXT = DV.DELEGATE_ONLY_TEXT
_DELEGATE_OFF_TEXT = DV.DELEGATE_OFF_TEXT
# 1.0.143: "serve" = the frontier serve sessions (keeper, chat): ON adds the
# DELEGATE-ONLY section to their rendered directives (OFF adds nothing).
DELEGATE_BACKENDS = ("mct", "claude-code", "serve")


def _frontier_delegate_doc(cfg=None, now=None):
    """{mct, claude-code, serve}: bool. 1.0.146: a switch ON past its `meta[be].expires`
    reads OFF (the toggle is owned, visible and expiring — d4254)."""
    doc = _cfg_json(cfg, "delegate") if cfg is not None else _read_json(FRONTIER_DELEGATE_FLAG, None)
    out = {k: False for k in DELEGATE_BACKENDS}
    if isinstance(doc, dict):
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        for k in out:
            out[k] = bool(doc.get(k)) and not _toggle_expired(meta.get(k) or {}, now)
    return out


def _frontier_delegate_meta(cfg=None):
    """{backend: {by, at, expires}} of the delegate switches (empty for a legacy file)."""
    doc = _cfg_json(cfg, "delegate") if cfg is not None else _read_json(FRONTIER_DELEGATE_FLAG, None)
    meta = doc.get("meta") if isinstance(doc, dict) and isinstance(doc.get("meta"), dict) else {}
    return {k: {"by": str((meta.get(k) or {}).get("by") or ""), "at": int((meta.get(k) or {}).get("at") or 0),
                "expires": int((meta.get(k) or {}).get("expires") or 0)} for k in DELEGATE_BACKENDS}


def _delegate_write_doc(doc, be, on, by, expires):
    """The file content for one flip: bools + meta {be: {by, at, expires}}."""
    out = {k: bool(doc.get(k)) for k in DELEGATE_BACKENDS}
    out[be] = bool(on)
    meta = dict(doc.get("meta") or {}) if isinstance(doc.get("meta"), dict) else {}
    meta[be] = {"by": by, "at": int(time.time()), "expires": int(expires or 0) if on else 0}
    out["meta"] = meta
    return out


def _delegate_section(backend, cfg=None):
    return _DELEGATE_ONLY_TEXT if _frontier_delegate_doc(cfg).get(backend) else _DELEGATE_OFF_TEXT


async def api_frontier_delegate(request):
    """GET/POST /api/frontier/delegate — the per-backend delegate-only switch.
    POST {"backend": "mct"|"claude-code", "on": bool}. Returns {delegate:{mct,
    claude-code}, notified: <tmux session typed into or "">}."""
    vm = await _sidecar_vm(request)
    if vm:
        return await _remote_delegate(request, vm)        # 1.0.144: the locus's OWN switch
    notified = ""
    if request.method == "POST":
        try:
            body = await request.json()
            be = body.get("backend")
            on = bool(body.get("on"))
            if be not in DELEGATE_BACKENDS:
                raise ValueError
        except Exception:
            return web.json_response({"error": 'body must be {"backend": "mct"|"claude-code"|"serve", "on": bool}'}, status=400)
        raw = _read_json(FRONTIER_DELEGATE_FLAG, None)
        doc = _delegate_write_doc(raw if isinstance(raw, dict) else {}, be, on, _toggle_owner(request),
                                  _toggle_expires(body))
        FRONTIER_DELEGATE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        FRONTIER_DELEGATE_FLAG.write_text(json.dumps(doc), encoding="utf-8")
        # re-render the mct seat's composed guidance where the pointer-exchange
        # REPL actually reads it (the mct2 workspaces — the station's own and,
        # when a live session runs elsewhere, that one), not the broker ws.
        seen = set()
        for ws in (FV_STATE_HOME / "mct2" / "repl", _mct2_live_workspace()):
            if ws and str(ws) not in seen:
                seen.add(str(ws))
                try:
                    _render_guidance(ws)
                except Exception:
                    pass
        _render_session_directives()          # the serve sessions read it on their next turn
        sess = "" if be == "serve" else (_tmux_session_for("frontier", be) or ("keeper-mct" if be == "mct" else "keeper-claude"))
        if sess and await _tmux_has(sess):
            line = ("📨 operator switch: DELEGATE-ONLY mode is now ON for this seat — from here on do not execute "
                    "tasks yourself: brief a subagent (Agent tool) for every task and keep your own turns to "
                    "routing and short summaries; several independent tasks → parallel subagents."
                    if on else
                    "📨 operator switch: delegate-only mode is now OFF for this seat — handle tasks yourself again "
                    "(delegate per the directive's token economy).")
            notified = sess if await _tmux_type_line(sess, line) else ""
        audit(request, "frontier-delegate", f"{be}={'on' if on else 'off'}" + (f" → {notified}" if notified else ""), ok=True)
    return web.json_response({"ok": True, "delegate": _frontier_delegate_doc(), "meta": _frontier_delegate_meta(),
                              "notified": notified,
                              "applies": "directive section at the next launch (claude-code) / next turn (mct); a live seat is also told right away"})


# ── 1.0.144: the delegate / fs switches of ANOTHER locus — its own files, set
# through the locus transport (locus_exec.read_cfg/write_cfg), then that locus's
# directives are re-rendered ON it (its serve reads them on the next turn).
_REMOTE_RENDER_SH = (LX.STATE_SH
    + 'A=""; for a in "$S/app/current" /opt/hugpy-station; do '
      '[ -f "$a/resources/backend/directives.py" ] && { A="$a"; break; }; done\n'
    + '[ -n "$A" ] || { echo "no station resources on this locus" >&2; exit 3; }\n'
    + 'L="${1:-}"; exec python3 "$A/resources/backend/directives.py" render --state "$S" '
      '--out "$S/directives/rendered" ${L:+--locus "$L"}\n')


async def _remote_render(t):
    rc, out, err = await LX.run(t, "set -- " + shlex.quote(t.locus or "") + "\n" + _REMOTE_RENDER_SH, timeout=60)
    return rc == 0, (err or out or "").strip()[-200:]


async def _remote_delegate(request, vm):
    t = _locus_target(vm)
    try:
        cfg = await _seat_cfg(t)
    except LX.LocusError as e:
        return web.json_response({"ok": False, "locus": vm, "error": str(e)}, status=502)
    if request.method == "POST":
        try:
            body = await request.json()
            be, on = body.get("backend"), bool(body.get("on"))
            if be not in DELEGATE_BACKENDS:
                raise ValueError
        except Exception:
            return web.json_response({"error": 'body must be {"backend": "mct"|"claude-code"|"serve", "on": bool}'}, status=400)
        raw = _cfg_json(cfg, "delegate")
        doc = _delegate_write_doc(raw if isinstance(raw, dict) else {}, be, on, _toggle_owner(request),
                                  _toggle_expires(body))
        try:
            path = await LX.write_cfg(t, "delegate", json.dumps(doc))
        except LX.LocusError as e:
            return web.json_response({"ok": False, "locus": vm, "error": str(e)}, status=502)
        ok, why = await _remote_render(t)
        audit(request, "frontier-delegate", "%s %s=%s (%s; render %s)" % (vm, be, "on" if on else "off", path,
                                                                        "ok" if ok else why), ok=True)
        return web.json_response({"ok": True, "locus": vm, "remote": True, "delegate": doc, "notified": "",
                                  "path": path, "rendered": ok, "render_error": "" if ok else why,
                                  "applies": "the locus's serve sessions read it on their next turn; its tmux seats at the next launch"})
    return web.json_response({"ok": True, "locus": vm, "remote": True, "delegate": _frontier_delegate_doc(cfg),
                              "notified": ""})


_REMOTE_FS_SH = (LX.STATE_SH
    + 'export PATH="$HOME/.local/bin:$S/serve-venv/bin:$PATH"\n'
    + 'command -v hugpy-agent >/dev/null 2>&1 || { echo "hugpy-agent not installed for $(id -un)@$(hostname)" >&2; exit 3; }\n'
    + 'W="$S/mct/repl"; mkdir -p "$W"\n'
    + 'if [ -n "${1:-}" ]; then exec hugpy-agent mct-fs "$W" --allow "$1"; else exec hugpy-agent mct-fs "$W"; fi\n')


async def _remote_fs(request, vm, desired=None):
    """GET/POST the fs switch of another locus: hugpy-agent mct-fs in ITS
    workspace, plus the frontier-fs.json mirror its tmux seat reads."""
    t = _locus_target(vm)
    rc, out, err = await LX.run(t, "set -- " + (shlex.quote(desired) if desired else "") + "\n" + _REMOTE_FS_SH,
                                timeout=60)
    try:
        txt = (out or "").strip()
        doc = json.loads(txt[txt.index("{"):]) if rc == 0 else None     # mct-fs prints indented JSON
        assert isinstance(doc, dict)
    except Exception:
        return web.json_response({"ok": False, "locus": vm, "error": (err or out or "rc=%s" % rc).strip()[-300:]},
                                 status=502)
    allowed = bool(doc.get("allow_frontier_fs_requests"))
    rendered = None
    if desired:
        try:
            await LX.write_cfg(t, "fs", json.dumps({"mediated": not allowed, "ts": int(time.time())}))
            rendered, _why = await _remote_render(t)
        except LX.LocusError as e:
            return web.json_response({"ok": False, "locus": vm, "error": str(e)}, status=502)
        audit(request, "frontier-fs-" + desired, "%s (remote)" % vm, ok=True)
    try:
        mediated = _frontier_fs_mediated(await _seat_cfg(t))
    except LX.LocusError:
        mediated = not allowed
    return web.json_response({"ok": True, "locus": vm, "remote": True, "allowed": allowed, "mediated": mediated,
                              "on": allowed, "desired": desired or "", "rendered": rendered,
                              "roots": doc.get("granted_roots", []), "policy": doc})


def _claude_seat_system_prompt(cfg=None):
    """The exact text the claude-code seat receives via --append-system-prompt.
    1.0.143: the claude-code seat is a TMUX seat (fallback / inference arm):
    the generated tmux directive for this locus, then the operator guidance,
    the tmux overlay, the fs switch (so A knows which of its tools are closed
    and why, instead of discovering it by denial), the delegation switch and
    the one-shot session init prompt.
    cfg (1.0.141) = compose from the TARGET locus's own files (seat launch on
    any locus, host included, passes the snapshot read on the target)."""
    return _session_directive("tmux", cfg, backend="claude-code", with_handoff=True)


def _claude_seat_settings_json(cfg=None):
    """The A settings template, plus the fs deny-list when mediated."""
    doc = dict(_a_template_doc(cfg)[0])
    if _frontier_fs_mediated(cfg):
        perms = dict(doc.get("permissions") or {})
        deny = list(perms.get("deny") or [])
        for t in FS_DENY_TOOLS:
            if t not in deny:
                deny.append(t)
        perms["deny"] = deny
        doc["permissions"] = perms
    return json.dumps(doc, separators=(",", ":"))


# ── 1.0.141: per-locus seat config ─────────────────────────────────────────────
# Every seat-config call from the UI carries ?vm=<selected locus>. The host
# (no vm / @keeper / this station's own name) keeps the handlers below as they
# always were; ANY other locus reads and writes ITS OWN files in ITS OWN station
# state dir, on the target, through locus_exec — exactly the files that locus's
# own station (and its seat launch) read. Never this host's files.
async def _cfg_vm(request):
    """'' = host; else the remote locus name (404 json for an unknown one)."""
    vm = (request.query.get("vm") or "").strip()
    if not vm or _is_host_vm(vm):
        return "", None
    await _loci_ready()
    if not (_ssh_host(vm) or vm in await _known_names_safe()):
        return vm, web.json_response({"ok": False, "error": f"unknown locus {vm}"}, status=404)
    return vm, None


def _cfg_err(vm, e, status=502):
    return web.json_response({"ok": False, "locus": vm, "remote": True,
                              "error": f"seat config of {vm} unavailable: {str(e)[:300]}"}, status=status)


def _dirv_session(request):
    """?session= — which session kind's operator overlay the steward edits.
    Default `tmux`: the pre-1.0.143 editor's target (frontier-directive.md)."""
    k = (request.query.get("session") or "tmux").strip().lower()
    return k if k in DV.KINDS else None


def _dirv_overlay_key(kind):
    return "directive" if kind == "tmux" else "dir_" + kind


def _dirv_doc(kind, cfg=None, locus_label=""):
    """The steward's view for one session kind: the operator overlay
    (verbatim), every kind's rendered directive length, the selected kind's
    full rendered text, and the legacy-file report."""
    snap = _dirv_snapshot(cfg)
    fx = _dirv_facts(cfg)
    key = _dirv_overlay_key(kind)
    text = (snap.get("files") or {}).get(key) or ""
    state = Path(str(snap.get("state") or FV_STATE_HOME))
    rendered = {k: DV.compose(k, snap, fx=fx, backend="claude-code" if k == "tmux" else "") for k in DV.KINDS}
    src = ("none" if not text.strip() else "legacy (not composed)" if kind == "tmux" and DV.is_legacy_full(text)
           else "operator overlay")
    return {
        "session": kind, "sessions": list(DV.KINDS), "titles": DV.TITLES,
        "text": text, "source": src, "path": str(state / DV.LAYER_FILES[key]),
        "shipped_path": str(FRONTIER_DIRECTIVE_SHIPPED), "templates": DV.templates_digest(),
        "rendered": rendered[kind], "rendered_chars": {k: len(v) for k, v in rendered.items()},
        "rendered_dir": str(state / DV.RENDERED_DIR),
        "legacy": DV.legacy_report(snap),
        "facts": {k: fx[k] for k in ("locus", "kind", "user", "hostname", "home", "state")},
        "how": ("serve session: $AC_SESSION_DIRECTIVES_DIR/" + kind + ".md, appended to the system prompt "
                "of every turn of the standing " + kind + " session" + (" on " + locus_label if locus_label else "")
                if kind != "tmux" else
                "tmux seats: --append-system-prompt (claude-code) / operator-guidance.md (mct) / launch "
                "guidance (codex), at every seat launch"),
    }


async def _remote_directive(request, vm):
    t = _locus_target(vm)
    kind = _dirv_session(request)
    if kind is None:
        return web.json_response({"error": "session must be one of " + ", ".join(DV.KINDS)}, status=400)
    try:
        if request.method == "POST":
            body = await request.json()
            text = body.get("text", "")
            if not isinstance(text, str):
                return web.json_response({"error": 'body must be {"text": string}'}, status=400)
            await LX.write_cfg(t, _dirv_overlay_key(kind), text if text.strip() else None)
            audit(request, "frontier-directive", f"{vm} {kind}: " + (f"overlay saved ({len(text)} chars)" if text.strip() else "overlay cleared"), ok=True)
        cfg = await _seat_cfg(t)
    except LX.LocusError as e:
        return _cfg_err(vm, e)
    doc = _dirv_doc(kind, cfg, vm)
    doc.update({
        "locus": vm, "remote": True, "state": cfg.get("state"),
        "composed": {
            "claude-code": {"how": "--append-system-prompt (verbatim, at every seat launch on " + vm + ")",
                            "text": _claude_seat_system_prompt(cfg),
                            "settings": json.loads(_claude_seat_settings_json(cfg))},
            "mct": {"how": "composed by " + vm + "'s own station into <ws>/operator-guidance.md",
                    "directive": doc["text"], "guidance": _cfg_text(cfg, "guidance") or "",
                    "guidance_path": str(Path(str(cfg.get("state") or "")) / LX.CFG_FILES["guidance"]),
                    "mct_system_prompt": ""},
        },
        "fs_mediated": _frontier_fs_mediated(cfg),
        "models": _frontier_models(cfg),
    })
    return web.json_response(doc)


async def _remote_models(request, vm):
    t = _locus_target(vm)
    applied_live = {}
    try:
        cfg = await LX.read_cfg(t, keys=["models"])
        cur = _frontier_models(cfg)
        if request.method == "POST":
            body = await request.json()
            if not isinstance(body, dict):
                return web.json_response({"error": "body must be a JSON object"}, status=400)
            for k in FRONTIER_MODEL_DEFAULTS:
                if k in body:
                    v = body[k]
                    if not isinstance(v, str) or (v and not _MODEL_RE.match(v)):
                        return web.json_response({"error": f"bad model for {k}"}, status=400)
                    cur[k] = v.strip()
            await LX.write_cfg(t, "models", json.dumps(cur, indent=2) + "\n")
            audit(request, "frontier-models", f"{vm}: " + json.dumps(cur), ok=True)
            # the live seat ON THAT LOCUS takes /model now (same keys as the host path)
            for k in body:
                sess = (BACKEND_TMUX_SESSION.get("frontier") or {}).get(k, "")
                if not sess or k not in cur or k == "hugpy":
                    continue
                rc, _o, _e = await _tmux_on(vm, "has-session", "-t", "=" + sess)
                if rc != 0:
                    applied_live[k] = "next launch (no live seat)"
                    continue
                rc2, _o2, err2 = await _tmux_on(vm, "send-keys", "-t", sess, "-l", "/model " + (cur[k] or "default"))
                if rc2 == 0:
                    await asyncio.sleep(1.2)
                    await _tmux_on(vm, "send-keys", "-t", sess, "Enter")
                applied_live[k] = "sent to live seat on " + vm if rc2 == 0 else f"failed: {(err2 or '').strip()[:80]}"
            cfg = await LX.read_cfg(t, keys=["models"])
    except LX.LocusError as e:
        return _cfg_err(vm, e)
    m = _frontier_models(cfg)
    return web.json_response({
        "applied_live": applied_live, "models": m, "locus": vm, "remote": True,
        "path": str(Path(str(cfg.get("state") or "")) / LX.CFG_FILES["models"]),
        "effective": {k: {"model": m.get(k, "") or "(default)", "launch": "next launch on " + vm}
                      for k in FRONTIER_MODEL_DEFAULTS},
        "choices": await _frontier_model_choices(request.app),
        "choices_source": _MODEL_CHOICES["source"], "custom_ok": True})


async def _remote_b_model(request, vm):
    t = _locus_target(vm)
    try:
        if request.method == "POST":
            body = await request.json()
            v = (body or {}).get("model", "")
            if not isinstance(v, str) or (v and not _MODEL_RE.match(v)):
                return web.json_response({"error": "bad model id"}, status=400)
            await LX.write_cfg(t, "b_model", json.dumps({"model": v.strip()}, indent=2) + "\n")
            audit(request, "b-model", f"{vm}: " + (v.strip() or "(package default)"), ok=True)
        cfg = await LX.read_cfg(t, keys=["b_model"])
    except LX.LocusError as e:
        return _cfg_err(vm, e)
    doc = _cfg_json(cfg, "b_model") or {}
    cur = doc.get("model") if isinstance(doc, dict) and isinstance(doc.get("model"), str) else ""
    return web.json_response({
        "model": cur, "effective": cur or "(" + vm + "'s package default)",
        "default": "", "locus": vm, "remote": True,
        "path": str(Path(str(cfg.get("state") or "")) / LX.CFG_FILES["b_model"]),
        "note": "written to " + vm + "'s own b-model.json; its station and seats pick it up at their next B call / launch",
        "candidates": await _b_model_candidates()})


async def api_frontier_directive(request):
    """GET/POST /api/frontier/directive?session=<kind> — 1.0.143: the
    directives are GENERATED per (locus x session kind) from the shipped
    templates; what the steward edits is the OPERATOR OVERLAY of one kind
    (keeper | chat | worker | local | tmux; default tmux = the pre-1.0.143
    frontier-directive.md). POST {"text"} replaces that overlay verbatim (empty
    deletes it). The response carries the kind's full rendered directive, the
    length of every kind's, and the COMPOSED tmux-seat texts as before. The
    serve sessions pick a change up on their next turn; the tmux seats at
    their next launch (claude-code) / next turn (mct)."""
    vm, bad = await _cfg_vm(request)          # 1.0.141: the SELECTED locus's own file
    if bad is not None:
        return bad
    if vm:
        return await _remote_directive(request, vm)
    kind = _dirv_session(request)
    if kind is None:
        return web.json_response({"error": "session must be one of " + ", ".join(DV.KINDS)}, status=400)
    if request.method == "POST":
        try:
            body = await request.json()
            text = body.get("text", "")
            if not isinstance(text, str):
                raise ValueError
        except Exception:
            return web.json_response({"error": 'body must be {"text": string}'}, status=400)
        path = FV_STATE_HOME / DV.LAYER_FILES[_dirv_overlay_key(kind)]
        path.parent.mkdir(parents=True, exist_ok=True)
        if text.strip():
            path.write_text(text, encoding="utf-8")       # operator text, verbatim — never rewritten
            did = f"{kind} overlay saved ({len(text)} chars)"
        else:
            if path.exists():
                path.unlink()
            did = f"{kind} overlay cleared — the generated directive alone"
        _render_guidance()
        _render_session_directives()
        audit(request, "frontier-directive", did, ok=True)
    try:
        _render_guidance()
        guidance = _bg_path().read_text(encoding="utf-8") if _bg_path().exists() else ""
    except OSError:
        guidance = ""
    mct_sys = ""
    try:   # best effort: show A's fixed MCT system prompt too (same package the seat runs)
        import importlib
        mod = importlib.import_module("hugpy_agent.mct.claude_adapter")
        mct_sys = getattr(mod, "_SYSTEM", "") or ""
    except Exception:
        mct_sys = ""
    doc = _dirv_doc(kind)
    doc.update({
        "composed": {
            "claude-code": {"how": "--append-system-prompt (verbatim, at every seat launch)",
                            "text": _claude_seat_system_prompt(),
                            "settings": json.loads(_claude_seat_settings_json())},
            "mct": {"how": "composed into <ws>/operator-guidance.md (tmux directive + 🧭 operator guidance) "
                           "-> B injects it as a governing_instruction (priority 100) into EVERY A turn, "
                           "after A's fixed MCT system prompt; re-rendered at every save and mct launch",
                    "directive": doc["text"], "guidance": guidance,
                    "guidance_path": str(_bg_path()),
                    "mct_system_prompt": mct_sys},
        },
        "fs_mediated": _frontier_fs_mediated(),
        "models": _frontier_models(),
    })
    return web.json_response(doc)


async def api_frontier_models(request):
    """GET/POST /api/frontier/models — the model each frontier seat launches
    with. POST {"mct": "sonnet", "claude-code": "opus"|""} ("" = Claude Code's
    own default). Applies at the next launch of that seat."""
    vm, bad = await _cfg_vm(request)          # 1.0.141: the SELECTED locus's own file
    if bad is not None:
        return bad
    if vm:
        return await _remote_models(request, vm)
    if request.method == "POST":
        try:
            body = await request.json()
            assert isinstance(body, dict)
        except Exception:
            return web.json_response({"error": "body must be a JSON object"}, status=400)
        cur = _frontier_models()
        for k in FRONTIER_MODEL_DEFAULTS:
            if k in body:
                v = body[k]
                if not isinstance(v, str) or (v and not _MODEL_RE.match(v)):
                    return web.json_response({"error": f"bad model for {k}"}, status=400)
                cur[k] = v.strip()
        FRONTIER_MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
        FRONTIER_MODELS_PATH.write_text(json.dumps(cur, indent=2) + "\n")
        audit(request, "frontier-models", json.dumps(cur), ok=True)
        # 1.0.65 (operator): a model pick must LITERALLY change the model. Both
        # frontier backends take `/model <id>` at their prompt (Claude Code's
        # slash command; the mct REPL's own) — type it into the live seat's
        # tmux session when one exists; next launch uses --model anyway.
        applied_live = {}
        for k in body:
            sess = (BACKEND_TMUX_SESSION.get("frontier") or {}).get(k, "")
            if not sess or k not in cur:
                continue
            # Hugpy's selected fleet model is supplied at launch; it has no
            # slash-model command for an already-running chat seat.
            if k == "hugpy":
                applied_live[k] = "next launch (Hugpy model is a launch setting)"
                continue
            rc, _o, _e = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "has-session", "-t", "=" + sess)
            if rc != 0:
                applied_live[k] = "next launch (no live seat)"
                continue
            line = "/model " + (cur[k] or "default")
            # Paced like a human at the TUI: close any menu, clear the input line,
            # type the command, let the slash-autocomplete settle BEFORE Enter (an
            # early Enter picks a suggestion instead), then confirm the "switch?
            # (history re-read)" dialog Claude Code shows on a cached conversation
            # — its default is Yes; on a fresh seat the extra Enter is an empty line.
            tk = ["tmux", "-L", KEEPER_TMUX_SOCK, "send-keys", "-t", sess]
            await _run(*tk, "Escape"); await asyncio.sleep(0.4)
            await _run(*tk, "C-u"); await asyncio.sleep(0.3)
            rc2, _o2, err2 = await _run(*tk, "-l", line)
            if rc2 == 0:
                await asyncio.sleep(1.2)
                await _run(*tk, "Enter")
                await asyncio.sleep(2.0)
                await _run(*tk, "Enter")
            applied_live[k] = "sent to live seat" if rc2 == 0 else f"failed: {(err2 or '').strip()[:80]}"
        request["applied_live"] = applied_live
    m = _frontier_models()
    return web.json_response({
        "applied_live": request.get("applied_live", {}),
        "models": m, "path": str(FRONTIER_MODELS_PATH),
        "effective": {
            "mct": {"model": m["mct"], "launch": f"abstract-claude mct <ws> --model {m['mct']}",
                    "note": "THE mct (pointer exchange, promoted 2026-08-27; impl files keep the mct2 name) — A is `claude -p --resume` per turn (chat history kept via <ws>/session.json; /new in the REPL resets); flight/queue state at /api/mct2/status; /model in the REPL changes it for that session only"},
            "claude-code": {"model": m["claude-code"] or "(Claude Code default for this account)",
                            "launch": "abstract-claude launch -- --dangerously-skip-permissions (fresh ~/.claude-sessions/<stamp>-<pid>-<label> per launch; legacy CLAUDE_CONFIG_DIR=<seat> claude only without abstract-claude)"
                                      + (f" --model {m['claude-code']}" if m["claude-code"] else "")
                                      + " --append-system-prompt <directive>"},
            "codex": {"model": m["codex"] or "(Codex default for this account)",
                      "launch": _gpt_launch_cmd()},
            "hugpy": {"model": m["hugpy"] or "(Hugpy agent default)",
                      "launch": "hugpy-agent harness" + (f" --model {m['hugpy']}" if m["hugpy"] else ""),
                      "note": "OpenCode terminal backed by Hugpy; set this to a fleet-worker model id to run A on that model."},
        },
        # 1.0.66 (operator): the LIVE model list — the toolserver asks the Models
        # API with the fleet token (claude/models, cached there 10 min) and this
        # station caches it too, so a page load costs one call. Static fallback
        # when the toolserver is unreachable. NOT a whitelist: "custom…" + POST
        # accept ANY id matching _MODEL_RE.
        "choices": await _frontier_model_choices(request.app),
        "choices_source": _MODEL_CHOICES["source"],
        "custom_ok": True,
    })


# ── 1.0.67: prompt-cache countdown for the frontier claude-code seat ─────────────
# Claude Code logs every API exchange to the seat's transcript (<CLAUDE_CONFIG_DIR>/
# projects/<cwd-slug>/<session>.jsonl). Each assistant row carries the API's usage:
# input_tokens, cache_read_input_tokens, cache_creation_input_tokens and the TTL
# split (cache_creation.ephemeral_5m/1h_input_tokens). The cached prefix = read +
# creation of the LAST request; it expires TTL after that request (refreshed by
# every request). Busy = the last row is an assistant tool_use with no result yet,
# or a user prompt with no assistant answer yet.
_CACHE_PROBE = r'''python3 - <<'__PY__'
import json, os, glob, time
home = os.path.expanduser("~")
# 1.0.85: prefer the KEEPER SEAT's own transcript. The broad ~/.claude/projects
# glob below picked the newest transcript of ANY Claude Code session of this
# account (an operator working in a terminal on the same box), so "busy" and
# the cache countdown described the wrong session and board nudges were
# deferred for as long as the operator was busy elsewhere. The seat's cwd is
# read from its tmux pane; Claude Code files transcripts under
# projects/<cwd with every non-alphanumeric char as "-">/ in whichever config
# dir the seat runs from (fresh per-launch ~/.claude-sessions/<stamp>, the
# legacy ~/.claude-seat/<label>, or the account's ~/.claude).
import re, subprocess
cands = []
try:
    cwd, pane_pid = subprocess.run(["tmux", "-L", "console", "display-message", "-p", "-t", "=keeper-claude:",
                          "#{pane_current_path}\t#{pane_pid}"], capture_output=True, text=True, timeout=3).stdout.strip().split("\t")[:2]
except Exception:
    cwd, pane_pid = "", ""
# 1.0.89 hotfix (keeper 2026-09-15): the glob ORDER below is not where the seat
# runs. abstract-claude's default fresh mode is "wipe" (rebuild ~/.claude in
# place, CLAUDE_CONFIG_DIR unset), so the live seat writes ~/.claude/projects/<slug>
# — but any leftover ~/.claude-seat/<label>/projects/<slug>/*.jsonl (a
# daemon-spawned bg session, an old launch) wins the cascade and the meter
# freezes on a foreign transcript that no wipe touches. Ask the pane's own
# claude process (same user: /proc/<pid>/environ + cmdline) which config dir
# and session it really uses; the cascade stays as the fallback.
seat_dir, seat_sid = "", ""
try:
    if pane_pid:
        out = subprocess.run(["ps", "-o", "pid=,cmd=", "--ppid", pane_pid], capture_output=True, text=True, timeout=3).stdout
        stack = [pane_pid] + [ln.split()[0] for ln in out.splitlines() if ln.strip()]
        seen = set()
        while stack:
            p = stack.pop(0)
            if p in seen: continue
            seen.add(p)
            try:
                cl = open("/proc/%s/cmdline" % p, "rb").read().split(b"\0")
            except OSError:
                continue
            argv = [a.decode("utf-8", "replace") for a in cl if a]
            if argv and (os.path.basename(argv[0]).startswith("claude") or ("claude" in (argv[1] if len(argv) > 1 else "") and "node" in argv[0]) or re.search(r"/claude/versions/", argv[0] or "")):
                try:
                    env = dict(kv.split("=", 1) for kv in open("/proc/%s/environ" % p, "rb").read().decode("utf-8", "replace").split("\0") if "=" in kv)
                except OSError:
                    env = {}
                seat_dir = env.get("CLAUDE_CONFIG_DIR") or os.path.join(env.get("HOME") or home, ".claude")
                for i, a in enumerate(argv):
                    if a == "--session-id" and i + 1 < len(argv): seat_sid = argv[i + 1]
                    elif a.startswith("--session-id="): seat_sid = a.split("=", 1)[1]
                    elif a == "--resume" and i + 1 < len(argv) and argv[i + 1].endswith(".jsonl") and not seat_sid:
                        seat_sid = os.path.basename(argv[i + 1])[:-6]
                break
            ch = subprocess.run(["ps", "-o", "pid=", "--ppid", p], capture_output=True, text=True, timeout=3).stdout.split()
            stack.extend(ch)
except Exception:
    seat_dir, seat_sid = "", ""
if cwd:
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    if seat_dir:
        cands = glob.glob(os.path.join(seat_dir, "projects", slug, "*.jsonl"))
        if seat_sid:
            hit = [c for c in cands if os.path.basename(c) == seat_sid + ".jsonl"]
            if hit: cands = hit
    if not cands:
        for pat in (os.path.join(home, ".claude-sessions", "*", "projects", slug, "*.jsonl"),
                    os.path.join(home, ".claude-seat", "*", "projects", slug, "*.jsonl"),
                    os.path.join(home, ".claude", "projects", slug, "*.jsonl")):
            cands = glob.glob(pat)
            if cands: break
if not cands:
    cands = glob.glob(os.path.join(home, ".claude-seat", "frontier", "projects", "*", "*.jsonl"))
if not cands:
    cands = glob.glob(os.path.join(home, ".claude", "projects", "*", "*.jsonl"))
if not cands:
    print(json.dumps({"ok": False, "error": "no transcript"})); raise SystemExit
f = max(cands, key=os.path.getmtime)
rows = []
try:
    with open(f, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln: continue
            try: rows.append(json.loads(ln))
            except ValueError: pass
except OSError as e:
    print(json.dumps({"ok": False, "error": str(e)})); raise SystemExit
def ts(r):
    t = r.get("timestamp") or ""
    try:
        import datetime
        return datetime.datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return 0
asst = [r for r in rows if r.get("type") == "assistant" and (r.get("message") or {}).get("usage")
        and any(int(((r.get("message") or {}).get("usage") or {}).get(k) or 0) for k in
                ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))]
if not asst:
    print(json.dumps({"ok": False, "error": "no requests yet", "transcript": f})); raise SystemExit
last = asst[-1]; m = last["message"]; u = m["usage"]
cc = u.get("cache_creation") or {}
ttl = 3600 if (cc.get("ephemeral_1h_input_tokens") or 0) > 0 else 300
read = u.get("cache_read_input_tokens") or 0; crt = u.get("cache_creation_input_tokens") or 0
last_ts = ts(last)
# busy: last transcript row is an assistant tool_use (result pending) or a user turn awaiting an answer
tail = rows[-1]
busy = False
if tail.get("type") == "assistant" and (tail.get("message") or {}).get("stop_reason") == "tool_use": busy = True
elif tail.get("type") == "user":
    c = (tail.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c): busy = True
    elif isinstance(c, (str, list)): busy = True
now = time.time()
hour = [r for r in asst if now - ts(r) <= 3600]
print(json.dumps({
    "ok": True, "transcript": f, "session_id": last.get("sessionId") or last.get("session_id") or "",
    "model": m.get("model") or "", "last_request_ts": int(last_ts), "age_s": int(now - last_ts),
    "cached_tokens": read + crt, "cache_read": read, "cache_creation": crt,
    "context_tokens": (u.get("input_tokens") or 0) + read + crt, "output_tokens": u.get("output_tokens") or 0,
    "ttl_s": ttl, "expires_at": int(last_ts + ttl), "remaining_s": int(last_ts + ttl - now),
    "busy": busy, "stop_reason": m.get("stop_reason") or "",
    "requests_last_hour": len(hour), "requests_total": len(asst),
    "cache_creation_split": {"5m": cc.get("ephemeral_5m_input_tokens") or 0, "1h": cc.get("ephemeral_1h_input_tokens") or 0},
}))
__PY__'''

# steward tabs (operator 2026-09-15): the GPT (codex) seat's OWN session +
# token/context meter — the twin of _CACHE_PROBE for keeper-codex. The MCT
# pointer exchange is merged into the native frontier session and its token
# tracker (`hugpy-agent mct-usage` / /api/a/usage?backend=mct) is retired, so
# each frontier backend's session meter is THE tracker for that backend.
# Codex CLI writes one rollout-<stamp>-<session>.jsonl per session under
# $CODEX_HOME/sessions/YYYY/MM/DD/ and appends an event_msg/token_count row
# (info.last_token_usage + model_context_window) after every response — the
# model's own accounting, not an estimate. The pane's codex process names the
# rollout it holds open (/proc/<pid>/fd), so a stale rollout of another
# session can never win; the newest rollout file is the fallback.
_CODEX_PROBE = r'''python3 - <<'__PY__'
import json, os, glob, time, re, subprocess, datetime
home = os.path.expanduser("~")
cwd, pane_pid = "", ""
try:
    cwd, pane_pid = subprocess.run(["tmux", "-L", "console", "display-message", "-p", "-t", "=keeper-codex:",
                          "#{pane_current_path}\t#{pane_pid}"], capture_output=True, text=True, timeout=3).stdout.strip().split("\t")[:2]
except Exception:
    cwd, pane_pid = "", ""
seat_live = bool(pane_pid)
f, codex_home, proc_pid = "", "", 0
try:
    if pane_pid:
        stack, seen = [pane_pid], set()
        while stack and not f:
            p = stack.pop(0)
            if p in seen: continue
            seen.add(p)
            try:
                argv = [a.decode("utf-8", "replace") for a in open("/proc/%s/cmdline" % p, "rb").read().split(b"\0") if a]
            except OSError:
                continue
            if argv and os.path.basename(argv[0]) == "codex":
                proc_pid = int(p)
                try:
                    env = dict(kv.split("=", 1) for kv in open("/proc/%s/environ" % p, "rb").read().decode("utf-8", "replace").split("\0") if "=" in kv)
                except OSError:
                    env = {}
                codex_home = env.get("CODEX_HOME") or os.path.join(env.get("HOME") or home, ".codex")
                try:
                    for fd in os.listdir("/proc/%s/fd" % p):
                        try: t = os.readlink("/proc/%s/fd/%s" % (p, fd))
                        except OSError: continue
                        b = os.path.basename(t)
                        if b.startswith("rollout-") and b.endswith(".jsonl") and os.path.exists(t):
                            if not f or os.path.getmtime(t) > os.path.getmtime(f): f = t
                except OSError:
                    pass
                break
            try:
                stack.extend(open("/proc/%s/task/%s/children" % (p, p)).read().split())
            except OSError:
                pass
except Exception:
    pass
if not f:
    print(json.dumps({"ok": False, "error": "no transcript", "backend": "codex", "seat": "keeper-codex", "seat_live": seat_live})); raise SystemExit
def ts(t):
    try:
        return datetime.datetime.strptime((t or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return 0
sid, model, cli, scwd = "", "", "", ""
counts, last, last_ts, busy, compactions = [], None, 0, False, 0
try:
    with open(f, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln: continue
            try: r = json.loads(ln)
            except ValueError: continue
            t = r.get("type"); p = r.get("payload") or {}
            if not isinstance(p, dict): continue
            if t == "session_meta":
                sid = p.get("session_id") or p.get("id") or sid; cli = p.get("cli_version") or cli; scwd = p.get("cwd") or scwd
            elif t == "turn_context":
                model = p.get("model") or model
            elif t == "compacted":
                compactions += 1
            elif t == "event_msg":
                et = p.get("type")
                if et == "token_count" and isinstance(p.get("info"), dict) and (p["info"].get("last_token_usage") or {}).get("total_tokens"):
                    last = p["info"]; last_ts = ts(r.get("timestamp")); counts.append(last_ts)
                elif et == "task_started": busy = True
                elif et in ("task_complete", "turn_aborted"): busy = False
except OSError as e:
    print(json.dumps({"ok": False, "error": str(e), "backend": "codex", "seat": "keeper-codex", "seat_live": seat_live})); raise SystemExit
if last is None:
    print(json.dumps({"ok": False, "error": "no requests yet", "transcript": f, "backend": "codex", "seat": "keeper-codex",
                      "seat_live": seat_live, "session_id": sid, "model": model})); raise SystemExit
u = last.get("last_token_usage") or {}; tot = last.get("total_token_usage") or {}
window = int(last.get("model_context_window") or 0)
inp = int(u.get("input_tokens") or 0); cached = int(u.get("cached_input_tokens") or 0); cw = int(u.get("cache_write_input_tokens") or 0)
now = time.time()
ttl = 600   # OpenAI prompt caching keeps a prefix warm ~5-10 min after its last use (an estimate: the API does not report it)
print(json.dumps({
    "ok": True, "backend": "codex", "provider": "openai", "seat": "keeper-codex", "seat_live": seat_live, "pid": proc_pid,
    "transcript": f, "session_id": sid, "model": model, "cli_version": cli, "cwd": scwd or cwd,
    "last_request_ts": int(last_ts), "age_s": int(now - last_ts),
    "cached_tokens": cached, "cache_read": cached, "cache_creation": cw,
    "context_tokens": inp, "output_tokens": int(u.get("output_tokens") or 0), "reasoning_tokens": int(u.get("reasoning_output_tokens") or 0),
    "context_window": window, "context_pct": (round(100.0 * inp / window, 1) if window else None),
    "totals": {"input": int(tot.get("input_tokens") or 0), "cached_input": int(tot.get("cached_input_tokens") or 0),
               "output": int(tot.get("output_tokens") or 0), "reasoning": int(tot.get("reasoning_output_tokens") or 0),
               "total": int(tot.get("total_tokens") or 0)},
    "compactions": compactions,
    "ttl_s": ttl, "ttl_basis": "estimate", "expires_at": int(last_ts + ttl), "remaining_s": int(last_ts + ttl - now),
    "busy": busy, "stop_reason": "",
    "requests_last_hour": len([x for x in counts if now - x <= 3600]), "requests_total": len(counts),
}))
__PY__'''

# steward tabs add-on (operator 2026-09-15): the LIVE SUBAGENTS the frontier
# seat spawned with Claude Code's Agent tool, from the seat's OWN transcript
# (no agent-side plumbing). The resolver is _CACHE_PROBE's own head (same
# pane → claude process → CLAUDE_CONFIG_DIR/--session-id → transcript, same
# fallback cascade), split off at its parse step, so both probes always read
# the same file. Per spawn: the assistant tool_use "Agent" block (input:
# description = purpose, prompt = task text → first line/sentence = title,
# subagent_type, model — explicit, else inherited = the session's model); a
# background agent's id comes from the tool_result text ("agentId: <id>") and
# its finish from the queued_command attachment carrying <task-notification>
# (task-id = agentId, status); a foreground Agent finishes with its own
# tool_result. Each subagent's own transcript
# (<config>/projects/<slug>/<session>/subagents/agent-<id>.jsonl — what the
# /tmp/claude-<uid>/…/tasks/<id>.output symlink points at) names the model
# it really ran on, its last usage, and (mtime) when it last wrote.
_AGENTS_TAIL = r'''
import datetime
def ts(t):
    try:
        return datetime.datetime.strptime((t or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return 0
sess_model, spawns, order, by_tool = "", {}, [], {}
sid = os.path.basename(f)[:-6]
sub_dir = os.path.join(os.path.dirname(f), sid, "subagents")
try:
    with open(f, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln: continue
            try: r = json.loads(ln)
            except ValueError: continue
            t = r.get("type"); m = r.get("message") if isinstance(r.get("message"), dict) else {}
            if t == "assistant" and m.get("model"): sess_model = m["model"]
            c = m.get("content")
            if t == "assistant" and isinstance(c, list):
                for x in c:
                    if isinstance(x, dict) and x.get("type") == "tool_use" and x.get("name") == "Agent":
                        inp = x.get("input") if isinstance(x.get("input"), dict) else {}
                        title = str(inp.get("prompt") or "").strip().split("\n", 1)[0].strip()
                        title = re.split(r"(?<=[.!?])\s", title, 1)[0].strip()[:160]
                        a = {"id": "", "tool_use_id": x.get("id") or "", "purpose": str(inp.get("description") or "")[:120],
                             "title": title, "subagent_type": str(inp.get("subagent_type") or "general-purpose"),
                             "model": str(inp.get("model") or ""), "model_explicit": bool(inp.get("model")),
                             "status": "running", "started_at": int(ts(r.get("timestamp"))), "ended_at": 0,
                             "tokens": None, "background": False, "last_write_ts": 0}
                        by_tool[a["tool_use_id"]] = a; order.append(a)
            elif t == "user" and isinstance(c, list):
                for x in c:
                    if isinstance(x, dict) and x.get("type") == "tool_result" and x.get("tool_use_id") in by_tool:
                        a = by_tool[x["tool_use_id"]]
                        txt = x.get("content")
                        if isinstance(txt, list): txt = "\n".join(str(y.get("text") or "") for y in txt if isinstance(y, dict))
                        mm = re.search(r"agentId:\s*([0-9A-Za-z_-]+)", str(txt or ""))
                        if mm:
                            a["id"] = mm.group(1); a["background"] = True; spawns[a["id"]] = a
                        else:
                            a["status"] = "failed" if x.get("is_error") else "completed"; a["ended_at"] = int(ts(r.get("timestamp")))
            elif t == "attachment" or (t == "user" and isinstance(c, str)):
                p = c if t == "user" else (str(((r.get("attachment") or {}).get("prompt")) or "") if isinstance(r.get("attachment"), dict) else "")
                if "<task-notification>" in p:
                    tid = re.search(r"<task-id>([^<]+)</task-id>", p); st = re.search(r"<status>([^<]+)</status>", p)
                    a = spawns.get(tid.group(1).strip() if tid else "")
                    if a is None:
                        tu = re.search(r"<tool-use-id>([^<]+)</tool-use-id>", p); a = by_tool.get(tu.group(1).strip() if tu else "")
                    if a is not None:
                        s = (st.group(1) if st else "completed").strip().lower()
                        a["status"] = "failed" if s in ("failed", "error", "errored", "killed", "cancelled") else "completed"
                        a["ended_at"] = int(ts(r.get("timestamp")))
except OSError as e:
    print(json.dumps({"ok": False, "error": str(e)})); raise SystemExit
now = time.time()
for a in order:
    if not a["model"]: a["model"] = sess_model
    if a["id"]:
        sp = os.path.join(sub_dir, "agent-%s.jsonl" % a["id"])
        try:
            st = os.stat(sp); a["last_write_ts"] = int(st.st_mtime)
            usage, mdl = None, ""
            with open(sp, "rb") as fh:
                fh.seek(max(0, st.st_size - 262144))
                for raw in fh.read().decode("utf-8", "replace").splitlines():
                    try: rr = json.loads(raw)
                    except ValueError: continue
                    mm = rr.get("message") if isinstance(rr.get("message"), dict) else {}
                    if rr.get("type") == "assistant":
                        if mm.get("model"): mdl = mm["model"]
                        if isinstance(mm.get("usage"), dict): usage = mm["usage"]
            if mdl: a["model"] = mdl
            if usage:
                a["tokens"] = {"input": int(usage.get("input_tokens") or 0), "output": int(usage.get("output_tokens") or 0),
                               "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                               "cache_write": int(usage.get("cache_creation_input_tokens") or 0)}
        except OSError:
            pass
    a["elapsed_s"] = max(0, int((a["ended_at"] or now) - a["started_at"])) if a["started_at"] else 0
order.reverse()
print(json.dumps({"ok": True, "transcript": f, "session_id": sid, "model": sess_model, "now": int(now),
                  "running": sum(1 for a in order if a["status"] == "running"), "agents": order[:50]}))
__PY__'''
_AGENTS_PROBE = ((_CACHE_PROBE.split("\nrows = []\n", 1)[0] + _AGENTS_TAIL)
                 if "\nrows = []\n" in _CACHE_PROBE else "")

# $/MTok input (Anthropic first-party, 2026-06): the rebuild estimate uses the
# cache WRITE rate (1.25x for the 5-minute TTL, 2x for the 1-hour TTL).
_MODEL_INPUT_PRICE = {"claude-fable-5-1": 10.0, "claude-fable-5": 10.0, "claude-opus-5": 5.0,
                      "claude-opus-4-8": 5.0, "claude-opus-4-7": 5.0, "claude-opus-4-6": 5.0,
                      "claude-sonnet-5": 2.0, "claude-sonnet-4-6": 3.0, "claude-haiku-4-5": 1.0}


async def api_frontier_cache(request):
    """GET /api/frontier/cache?vm=<locus|@keeper>[&backend=claude-code|codex] —
    the frontier seat's OWN session + token/context meter, read from that
    seat's transcript (the usage the model reported, not an estimate).
    backend (steward tabs, operator 2026-09-15): claude-code (default, so the
    old callers are unchanged — keeper-claude, the Claude Code transcript +
    prompt-cache countdown) or codex (keeper-codex, the Codex rollout's
    token_count rows); gpt/chatgpt alias codex. One shape for both:
    ok, backend, seat, seat_live, session_id, model, age_s, context_tokens,
    cached_tokens/cache_read/cache_creation, output_tokens, busy, state,
    ttl_s/expires_at/remaining_s, requests_last_hour/requests_total; codex
    adds context_window, context_pct, totals{input,cached_input,output,
    reasoning,total}, reasoning_tokens, compactions, ttl_basis="estimate"."""
    vm = (request.query.get("vm") or "").strip()
    if vm == "@keeper":
        vm = ""
    elif vm and vm not in await all_locus_names():
        return web.json_response({"ok": False, "error": f"unknown locus {vm}"}, status=404)
    backend = (request.query.get("backend") or "claude-code").strip().lower()
    if backend in ("gpt", "chatgpt", "openai"):
        backend = "codex"
    if backend in ("ac", "abstract-claude"):
        backend = "serve"
    # t-serve: backend=serve reads the keeper serve's OWN usage DB, so the token
    # meter keeps working now that serve is the keeper surface. On the host seat
    # in serve mode it is also the DEFAULT, and the tmux transcript probe is the
    # fallback (so the meter still reports while keeper-claude is the one live).
    # 1.0.141: the SELECTED locus's serve (its ssh forward) — never the host's
    # serve answering for another locus.
    if backend == "serve" or (backend == "claude-code"
                              and (await _keeper_surface_now(vm))[0] == "serve"):
        _sbase = (await _ac_resolve(vm))[0] if vm else None
        sdoc = await _ac_serve_cache_doc(base=_sbase) if (_sbase or not vm) else None
        if sdoc:
            sdoc["locus"] = vm or "host"
            sdoc["state"] = "warm" if sdoc.get("remaining_s", 0) > 0 else "lapsed"
            base = next((v for k, v in _MODEL_INPUT_PRICE.items()
                         if str(sdoc.get("model", "")).startswith(k)), None)
            mult = 2.0 if sdoc.get("ttl_s") == 3600 else 1.25
            sdoc["rebuild_cost_usd"] = (round(sdoc["cached_tokens"] / 1e6 * base * mult, 4) if base else None)
            sdoc["warm_turn_cost_usd"] = (round(sdoc["cached_tokens"] / 1e6 * base * 0.1, 4) if base else None)
            sdoc["price_basis"] = (f"${base}/MTok input, write x{mult}, read x0.1"
                                   if base else "unknown model price")
            return web.json_response(sdoc)
        if backend == "serve":
            return web.json_response({"ok": False, "backend": "serve", "surface": "serve",
                                      "locus": vm or "host",
                                      "error": "serve unreachable at " + ((_sbase if vm else None) or AC_UPSTREAM)},
                                     status=502)
        backend = "claude-code"      # serve had nothing: fall through to the tmux probe
    if backend not in ("claude-code", "codex"):
        return web.json_response({"ok": False, "error": f"unknown frontier backend {backend}", "backend": backend}, status=400)
    rc, out, err = await _locus_run_fast(vm, _CODEX_PROBE if backend == "codex" else _CACHE_PROBE, 20)
    try:
        doc = json.loads((out or "").strip().splitlines()[-1])
    except Exception:
        return web.json_response({"ok": False, "error": (err or out or "probe failed").strip()[:300],
                                  "backend": backend}, status=502)
    doc.setdefault("backend", backend)
    doc.setdefault("seat", "keeper-codex" if backend == "codex" else "keeper-claude")
    if doc.get("ok"):
        doc["state"] = "busy" if doc.get("busy") else ("warm" if doc.get("remaining_s", 0) > 0 else "lapsed")
        if backend == "codex":
            # no OpenAI price table here: cached input is billed at a discount,
            # but the rebuild/warm-turn dollar estimate is Anthropic-only.
            doc["rebuild_cost_usd"] = None
            doc["warm_turn_cost_usd"] = None
            doc["price_basis"] = "OpenAI prompt caching (cached input discounted); no price table"
        else:
            base = next((v for k, v in _MODEL_INPUT_PRICE.items() if str(doc.get("model", "")).startswith(k)), None)
            mult = 2.0 if doc.get("ttl_s") == 3600 else 1.25
            doc["rebuild_cost_usd"] = (round(doc["cached_tokens"] / 1e6 * base * mult, 4) if base else None)
            doc["warm_turn_cost_usd"] = (round(doc["cached_tokens"] / 1e6 * base * 0.1, 4) if base else None)
            doc["price_basis"] = f"${base}/MTok input, write x{mult}, read x0.1" if base else "unknown model price"
    doc["locus"] = vm or "host"
    return web.json_response(doc)


async def api_frontier_limits(request):
    """GET /api/frontier/limits — the frontier (claude-code) quota/limit state
    for the seat card's LimitsBadge (p515). Derived from the keeper serve with
    NO abstract-claude change: its configured default_model + quota_fallback_model
    (/api/state) vs the model the most recent run actually used (/api/usage/report).
    fallback_active = recent runs are on the fallback model, i.e. the default hit
    its limit and abstract-claude's launcher auto-switched (launch.py LIMIT_RE).
    Shape the frontend expects: {default_model, fallback_model, state:ok|hit,
    fallback_active, last_hit_ts, current_model}. "near" and a precise last_hit_ts
    need the serve-side quota event (not yet emitted), so state is ok|hit only and
    last_hit_ts stays null until that lands."""
    vm = (request.query.get("vm") or "").strip()     # 1.0.141: the SELECTED locus's serve
    vm = "" if _is_host_vm(vm) else vm
    st = await _ac_serve_state(vm)
    if not st.get("ok"):
        # Serve is the surface this signal is derived from. When it is not
        # answering (down, or a tmux-keeper host with no serve) return non-2xx
        # so the console's api() rejects and LimitsBadge hides (setLim(false))
        # rather than rendering a misleading green "ok" badge.
        return web.json_response({"ok": False, "error": st.get("error") or "keeper serve unavailable"},
                                 status=503)
    default_model = st.get("model") or ""
    fallback_model = st.get("fallback_model") or ""
    current_model = ""
    rep = await _ac_get("/api/usage/report?last=1", base=st.get("url") or None)
    if isinstance(rep, dict):
        recent = rep.get("recent") or []
        if recent:
            current_model = recent[0].get("model") or ""
    fallback_active = bool(fallback_model and current_model
                           and current_model == fallback_model
                           and current_model != default_model)
    state = "hit" if fallback_active else "ok"
    return web.json_response({"ok": True, "default_model": default_model,
                              "fallback_model": fallback_model, "current_model": current_model,
                              "state": state, "fallback_active": fallback_active,
                              "last_hit_ts": None, "locus": vm or "@keeper"})


async def api_frontier_agents(request):
    """GET /api/frontier/agents?vm=<locus|@keeper>[&backend=claude-code|codex]
    — the subagents the frontier seat spawned (steward tabs add-on, operator
    2026-09-15), newest first, last 50: {ok, backend, seat, session_id, model,
    now, running, agents: [{id, tool_use_id, purpose, title, subagent_type,
    model, model_explicit, status: running|completed|failed, started_at,
    ended_at, elapsed_s, tokens{input,output,cache_read,cache_write}|null,
    background, last_write_ts}]}. claude-code reads the seat's own transcript
    (_AGENTS_PROBE); codex has no spawn record in its rollout → empty list
    with a note. The elapsed timer ticks client-side from started_at."""
    vm = (request.query.get("vm") or "").strip()
    if vm == "@keeper":
        vm = ""
    elif vm and vm not in await all_locus_names():
        return web.json_response({"ok": False, "error": f"unknown locus {vm}"}, status=404)
    backend = (request.query.get("backend") or "claude-code").strip().lower()
    if backend in ("gpt", "chatgpt", "openai"):
        backend = "codex"
    if backend not in ("claude-code", "codex"):
        return web.json_response({"ok": False, "error": f"unknown frontier backend {backend}", "backend": backend}, status=400)
    if backend == "codex":
        return web.json_response({"ok": True, "backend": "codex", "seat": "keeper-codex", "agents": [], "running": 0,
                                  "now": int(time.time()), "locus": vm or "host",
                                  "note": "Codex CLI records no subagent spawns in its rollout — nothing to list"})
    if not _AGENTS_PROBE:
        return web.json_response({"ok": False, "error": "agents probe unavailable (cache probe drifted)", "backend": backend}, status=503)
    rc, out, err = await _locus_run_fast(vm, _AGENTS_PROBE, 25)
    try:
        doc = json.loads((out or "").strip().splitlines()[-1])
    except Exception:
        return web.json_response({"ok": False, "error": (err or out or "probe failed").strip()[:300],
                                  "backend": backend}, status=502)
    doc.setdefault("backend", backend)
    doc.setdefault("seat", "keeper-claude")
    doc.setdefault("agents", [])
    doc["locus"] = vm or "host"
    return web.json_response(doc)


# ── 1.0.68: rolling state (toolserver assess/state) + relaunch on the init prompt ──
# ROLLING-HANDOFF-SPEC.md: the fleet judge keeps a per-locus rolling state (objective,
# done, next steps, init prompt) in the toolserver DB. The station shows it and can
# hand it to a FRESH frontier seat: write init_prompt → frontier-handoff.md (the
# 1.0.65 launch consumes it), kill the seat's tmux session, start a new one. The
# idle-relaunch rule does that on its own when the seat sits at the prompt.
IDLE_RELAUNCH_MIN = int(os.environ.get("STATION_IDLE_RELAUNCH_MIN", "20") or 0)
_rlog = logging.getLogger("station.idle-relaunch")


def _station_locus():
    """This station's own locus name in the toolserver: STATION_LOCUS, else the
    distributed locus whose endpoint is <this user>@<one of this host's ips>."""
    env = _canon_locus(os.environ.get("STATION_LOCUS") or "")
    if env:
        return env
    if _TS_LOCI.get("self"):   # 1.0.85: loci-sync hides the self host from the dropdown, keeps its name
        return _canon_locus(_TS_LOCI["self"])
    me, ips = getpass.getuser(), _local_ips()
    hits = [name for name, h in (_TS_LOCI.get("hosts") or {}).items()
            if h.get("user") == me and h.get("host") in ips]
    return _canon_locus(_pick_endpoint_locus(hits))


def _locus_name_for(vm):
    """assess/state key: the host's own locus, else the dropdown locus's
    central key (1.0.138 — aliases/endpoint matches included)."""
    return _station_locus() if _is_host_vm(vm) else _central_locus(vm)

def _keeper_locus():
    """This station's OWN locus key for its central rows (board, comms identity,
    file<->DB sync). 1.0.141: it is exactly _station_locus() — there is NO
    fallback to the `keeper` slice any more (that silently pointed every
    unconfigured station at vm_mgr's board and its sync wrote into it). ''
    means "locus not configured": callers show that state instead."""
    return _station_locus()


LOCUS_UNCONFIGURED = ("this station's locus is not configured — set STATION_LOCUS in "
                      "<state>/env/station.env (or register this user@host with "
                      "loci_register) and restart the station")


def _unconfigured_response(what=""):
    return web.json_response({"ok": False, "unconfigured": True, "locus": "",
                              "error": (what + ": " if what else "") + LOCUS_UNCONFIGURED},
                             status=409)


async def _seat_session_created(sess):
    """Epoch seconds the tmux seat session was created (0 = no such session)."""
    rc, out, _ = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "list-sessions", "-F", "#{session_name} #{session_created}")
    if rc != 0:
        return 0
    for ln in (out or "").splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0] == sess and parts[1].isdigit():
            return int(parts[1])
    return 0


async def _seat_pass_dialogs(sess, tries=6):
    """A fresh claude-code seat may stop at Claude Code's one-time dialogs (bypass
    notice, folder trust). Answer them so an unattended relaunch never strands the
    seat: Down+Enter picks 'Yes' in both. Stops as soon as the prompt is up."""
    for _ in range(tries):
        await asyncio.sleep(3)
        rc, out, _ = await _run("tmux", "-L", KEEPER_TMUX_SOCK, "capture-pane", "-p", "-t", "=" + sess, "-S", "-30")
        if rc != 0:
            return False
        if "Bypass Permissions mode" in out or "trust this folder" in out:
            await _run("tmux", "-L", KEEPER_TMUX_SOCK, "send-keys", "-t", "=" + sess, "Down", "Enter")
            continue
        if "❯" in out:
            return True
    return False


async def _rolling_state(app, vm):
    if not _is_host_vm(vm):
        await _loci_ready()
    locus = _locus_name_for(vm)
    if not locus:
        return {"ok": False, "error": "this station has no locus in the toolserver yet (set STATION_LOCUS or register it)"}
    try:
        doc = await _ts_call(app, "assess/state", {"locus": locus}, timeout=20)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "locus": locus}
    out = dict(doc, ok=True, locus=locus)
    if _is_host_vm(vm):
        sess = _tmux_session_for("frontier", "claude-code") or "keeper-claude"
        created = await _seat_session_created(sess)
        out["seat_session"] = sess
        out["seat_created"] = created
        out["init_prompt_newer_than_seat"] = bool(created and int(doc.get("ts") or 0) > created)
        try:
            rc, cout, _ = await _locus_run_fast("", _CACHE_PROBE, 20)
            cd = json.loads((cout or "").strip().splitlines()[-1]) if rc == 0 else {}
        except Exception:
            cd = {}
        out["busy"] = bool(cd.get("busy"))
        out["idle_s"] = int(cd.get("age_s") or 0) if cd.get("ok") else None
        out["idle_relaunch"] = {"minutes": IDLE_RELAUNCH_MIN, "enabled": IDLE_RELAUNCH_MIN > 0,
                                "would": bool(IDLE_RELAUNCH_MIN > 0 and created and doc.get("init_prompt")
                                              and out["init_prompt_newer_than_seat"] and not out["busy"]
                                              and (out["idle_s"] or 0) >= IDLE_RELAUNCH_MIN * 60)}
    return out


async def api_frontier_state(request):
    """GET /api/frontier/state?vm= — the rolling state (objective, next steps, init
    prompt) for the locus, plus whether the host seat is due for an idle relaunch."""
    vm = (request.query.get("vm") or "").strip()
    if vm == "@keeper":
        vm = ""
    return web.json_response(await _rolling_state(request.app, vm))


async def _relaunch_host_seat(app, reason):
    """Hand the rolling init prompt to a FRESH host frontier claude-code seat."""
    if not _tmux_available():
        return {"ok": False, "error": TMUX_MISSING_MSG}
    st = await _rolling_state(app, "")
    if not st.get("ok"):
        return {"ok": False, "error": st.get("error", "no state")}
    if st.get("busy"):
        return {"ok": False, "error": "seat is working — not relaunching mid-turn"}
    ip = (st.get("init_prompt") or "").strip()
    if not ip:
        return {"ok": False, "error": "no init prompt in the rolling state"}
    # 1.0.146: the init prompt is filed as a toolserver handoff row (the seat's
    # SessionStart hook pulls it) unless one is already open for the locus.
    locus = _station_locus()
    try:
        open_now = await _ts_call(app, "handoff/station", {"locus": locus}, timeout=10)
        if not (open_now or {}).get("handoff"):
            await _ts_call(app, "handoff/request", {"locus": locus, "text": ip, "by": "rolling-state", "spawn": False},
                           timeout=20)
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": f"could not file the init prompt as a toolserver handoff: {str(e)[:200]}"}
    sess = st.get("seat_session") or "keeper-claude"
    await _run("tmux", "-L", KEEPER_TMUX_SOCK, "kill-session", "-t", "=" + sess)
    # 1.0.141: the SAME unified launcher the terminal uses (script on the
    # target, short tmux line), started detached.
    key = "mct" if TERM_SURFACES["frontier"].get("default") == "mct" else "claude-code"
    target = _locus_target("")
    try:
        cfg = await _seat_cfg(target)
    except LX.LocusError:
        cfg = None
    body = _seat_launch_body(sess, _claude_seat_cmd("frontier", key, cfg), target, cfg, "frontier")
    try:
        rc, out, err, _p = await LX.seat_start_detached(
            target, sess, body, pre=" ".join(_scope_prefix()), opts=_TMUX_OPTS,
            cwd=str(FV_STATE_HOME))
    except LX.LocusError as e:
        return {"ok": False, "error": str(e)[:200]}
    if rc != 0:
        return {"ok": False, "error": "tmux new-session failed: " + ((err or out) or "")[:200]}
    ready = await _seat_pass_dialogs(sess)
    _audit_line("frontier-relaunch", f"{reason} · init_prompt {len(ip)} chars · session {sess} · prompt_up={ready}")
    return {"ok": True, "session": sess, "init_prompt_chars": len(ip), "reason": reason, "prompt_up": ready}


async def api_ac_rollover(request):
    """GET/POST /api/ac/rollover — proxy the keeper serve's session-rollover
    status/controls onto the shape the console's rollover chip renders (p509).

    GET  -> serve GET /api/session/rollover, mapped to
            {policy:"auto|manual|off", pending:{session_id, grace_s|grace_until}|null,
             current:<current context tokens>, rolling:<threshold tokens>, due:bool}.
    POST -> forwards {action:"roll"|"cancel", session_id} to serve
            POST /api/session/rollover (reply passed through verbatim)."""
    # 1.0.141: ?vm= = the SELECTED locus's serve (its forward), not the host's.
    vm = (request.query.get("vm") or "").strip()
    vm = "" if _is_host_vm(vm) else vm
    base = None
    if vm:
        base, _u, _p, _k = await _ac_resolve(vm)
        if not base:
            return web.json_response(
                {"ok": False, "locus": vm, "error": "no reachable serve on " + vm + ": "
                 + ((_AC_DISCOVERED.get(vm) or {}).get("error") or "not discovered")}, status=502)
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:                                # noqa: BLE001
            body = {}
        payload = {"action": (body.get("action") or "").strip(),
                   "session_id": (body.get("session_id") or "").strip()}
        doc = await _ac_post("/api/session/rollover", payload, base=base)
        if not isinstance(doc, dict):
            return web.json_response(
                {"ok": False, "error": "serve unreachable at " + (base or AC_UPSTREAM)},
                status=502)
        return web.json_response(doc)
    # GET: map serve's rollover status onto the console's rollover-chip shape.
    doc = await _ac_get("/api/session/rollover", timeout=4, base=base)
    policy = (doc or {}).get("policy") or {}
    # newest per-session eval carries the live context size + due flag
    newest = None
    for ev in ((doc or {}).get("evals") or {}).values():
        if isinstance(ev, dict) and (newest is None
                                     or (ev.get("ts") or 0) > (newest.get("ts") or 0)):
            newest = ev
    pending = None
    pending_raw = (doc or {}).get("pending")
    if isinstance(pending_raw, dict):
        pending = {"session_id": pending_raw.get("session_id") or "",
                   "grace_s": pending_raw.get("grace_s"),
                   "grace_until": pending_raw.get("grace_until")}
    out = {"policy": policy.get("rollover_mode") or "auto",
           "pending": pending,
           "current": (newest or {}).get("context_tokens"),
           "rolling": policy.get("rollover_context_tokens"),
           "due": bool((doc or {}).get("due")) or bool((newest or {}).get("due"))}
    return web.json_response(out)


def _audit_line(action, detail):
    _alog.info("%s %s", action, detail)
    try:
        with open(str(FV_STATE_HOME / "audit.log"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "u": "station", "action": action, "detail": detail}) + "\n")
    except OSError:
        pass


async def api_frontier_relaunch(request):
    """POST /api/frontier/relaunch?vm=@keeper — operator-triggered relaunch on the init prompt."""
    vm = (request.query.get("vm") or "").strip()
    if vm and not _is_host_vm(vm):
        # 1.0.101: a locus with its own serve (ac-loci.json) relaunches THAT unit
        if (await _ac_resolve(vm))[0]:
            res = await _ac_serve_restart("operator", vm)
            audit(request, "frontier-relaunch", json.dumps(res)[:200], ok=bool(res.get("ok")))
            return web.json_response(res, status=200 if res.get("ok") else 409)
        return web.json_response({"ok": False, "error": "relaunch is host-seat only for now"}, status=400)
    # t-serve: in serve mode the keeper surface is the abstract-claude serve
    # console, so "relaunch" restarts ITS unit. ?surface=tmux (or
    # STATION_KEEPER_SURFACE=tmux) keeps the legacy tmux seat relaunch.
    # ?surface=serve|tmux is the canonical switch; ?backend= is accepted as an
    # alias because the frontier picker speaks in backend keys (t296).
    want = (request.query.get("surface") or request.query.get("backend")
            or "").strip().lower()
    surface_now, _ac = await _keeper_surface_now()
    if want != "tmux" and (want == "serve" or surface_now == "serve"):
        res = await _ac_serve_restart("operator")
        audit(request, "frontier-relaunch", json.dumps(res)[:200], ok=bool(res.get("ok")))
        return web.json_response(res, status=200 if res.get("ok") else 409)
    res = await _relaunch_host_seat(request.app, "operator")
    audit(request, "frontier-relaunch", json.dumps(res)[:200], ok=bool(res.get("ok")))
    return web.json_response(res, status=200 if res.get("ok") else 409)


async def _idle_relaunch_loop(app):
    """Every 60 s: if the host frontier seat has been AT THE PROMPT for
    STATION_IDLE_RELAUNCH_MIN minutes and the rolling init prompt is newer than
    the seat, relaunch it on that prompt. Never while busy. 0 disables."""
    await asyncio.sleep(90)
    while True:
        try:
            # t-serve: in serve mode there is no idle tmux prompt to roll, and
            # the loop must never kill the tmux keeper seat behind the
            # operator's back. Stay quiet and let serve be.
            if IDLE_RELAUNCH_MIN > 0 and (await _keeper_surface_now())[0] == "serve":
                pass
            elif IDLE_RELAUNCH_MIN > 0:
                st = await _rolling_state(app, "")
                if st.get("ok") and (st.get("idle_relaunch") or {}).get("would"):
                    res = await _relaunch_host_seat(app, f"idle {st.get('idle_s')} s ≥ {IDLE_RELAUNCH_MIN} min")
                    _rlog.info("idle relaunch: %s", res)
        except Exception as e:  # noqa: BLE001
            _rlog.warning("idle relaunch loop: %s", e)
        await asyncio.sleep(60)


async def _start_idle_relaunch(app):
    if os.environ.get("STATION_DEV_QUIET") == "1":   # dev copy: never act on live seats
        return
    app["idle_relaunch"] = asyncio.create_task(_idle_relaunch_loop(app))


async def _stop_idle_relaunch(app):
    t = app.get("idle_relaunch")
    if t:
        t.cancel()


_MODEL_CHOICES = {"ids": None, "ts": 0, "source": "static"}
_MODEL_CHOICES_STATIC = ["claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-opus-4-8",
                         "claude-sonnet-5", "claude-haiku-4-5-20251001", ""]


async def _frontier_model_choices(app):
    """Live Claude model ids (via the toolserver's claude/models) + "" (= the CLI
    default), cached 10 min per station process; the static list on failure."""
    now = time.time()
    if _MODEL_CHOICES["ids"] is not None and now - _MODEL_CHOICES["ts"] < 600:
        return _MODEL_CHOICES["ids"]
    try:
        doc = await _ts_call(app, "claude/models", {}, timeout=25)
        ids = [i for i in (doc.get("ids") or []) if isinstance(i, str) and _MODEL_RE.match(i)]
        if ids:
            _MODEL_CHOICES.update(ids=ids + [""], ts=now, source="models-api via toolserver")
            return _MODEL_CHOICES["ids"]
    except Exception:
        pass
    if _MODEL_CHOICES["ids"] is None:
        _MODEL_CHOICES.update(ids=list(_MODEL_CHOICES_STATIC), ts=now, source="static")
    return _MODEL_CHOICES["ids"]


# --- B's model (local keeper) — picker over the fleet's text-gen registry --- #
async def _b_model_candidates():
    """text-generation model_keys from the gateway registry (the same list the
    /api/models proxy serves), current/loaded-friendly. Best effort: [] on error."""
    try:
        import aiohttp
        base = os.environ.get("HUGPY_BASE") or "https://dev.hugpy.ai/api"
        async with aiohttp.ClientSession() as cs:
            async with cs.get(base.rstrip("/") + "/models",
                              timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json()
    except Exception:
        return []
    out = []
    for m in data if isinstance(data, list) else []:
        if m.get("primary_task") == "text-generation" and m.get("model_key"):
            out.append({"key": m["model_key"], "size": m.get("size_bytes") or m.get("effective_bytes"),
                        "status": m.get("status")})
    out.sort(key=lambda x: x["key"].lower())
    return out


async def api_b_model(request):
    """GET/POST /api/b/model — the local keeper's model.
    POST {"model": "<key>"|""} ("" = the hugpy package default)."""
    vm, bad = await _cfg_vm(request)          # 1.0.141: the SELECTED locus's own file
    if bad is not None:
        return bad
    if vm:
        return await _remote_b_model(request, vm)
    if request.method == "POST":
        try:
            body = await request.json(); assert isinstance(body, dict)
        except Exception:
            return web.json_response({"error": "body must be a JSON object"}, status=400)
        v = body.get("model", "")
        if not isinstance(v, str) or (v and not _MODEL_RE.match(v)):
            return web.json_response({"error": "bad model id"}, status=400)
        v = v.strip()
        B_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        B_MODEL_PATH.write_text(json.dumps({"model": v}, indent=2) + "\n")
        _b_agent_env_write(v or _DEFAULT_B_MODEL())
        audit(request, "b-model", v or "(package default)", ok=True)
    cur = _b_model()
    return web.json_response({
        "model": cur, "effective": cur or _DEFAULT_B_MODEL(),
        "default": _DEFAULT_B_MODEL(), "path": str(B_MODEL_PATH),
        "agent_env": str(B_AGENT_ENV),
        "note": "applies to B's console calls (todo triage, ✎ revise) immediately; "
                "the mct seat and daemon pick it up at their next launch",
        "candidates": await _b_model_candidates(),
    })


def _DEFAULT_B_MODEL():
    try:
        from hugpy_agent.config import DEFAULT_AGENT_BRAIN
        return DEFAULT_AGENT_BRAIN
    except Exception:
        return "Qwen~Qwen3-Coder-Next-GGUF"


# --- Claude OAuth / login state per locus ---------------------------------- #
_AUTH_PROBE = r'''
c="$HOME/.claude/.credentials.json"
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then echo installed=1; else echo installed=0; fi
v="$( (command -v claude >/dev/null 2>&1 && claude --version) || ("$HOME/.local/bin/claude" --version) 2>/dev/null | head -1)"; echo "version=$v"
echo "home=$HOME"; echo "user=$(id -un)"
if [ -f "$c" ]; then echo creds=1; python3 - "$c" <<'PY' 2>/dev/null || echo parse=0
import json,sys,time
d=json.load(open(sys.argv[1]))
o=d.get("claudeAiOauth") or {}
print("parse=1")
print("oauth=%d" % (1 if o.get("accessToken") else 0))
print("refresh=%d" % (1 if o.get("refreshToken") else 0))
print("expires_at=%s" % (int(o.get("expiresAt") or 0)//1000 if o.get("expiresAt") else ""))
print("subscription=%s" % (o.get("subscriptionType") or ""))
print("scopes=%s" % ",".join(o.get("scopes") or []))
print("apikey=%d" % (1 if (d.get("apiKey") or d.get("primaryApiKey")) else 0))
PY
else echo creds=0; fi
[ -n "${ANTHROPIC_API_KEY:-}" ] && echo envkey=1 || echo envkey=0
# 1.0.64: the fleet's durable OAuth token is served by the toolserver into login
# shells (CLAUDE_CODE_OAUTH_TOKEN) — that IS a login, credentials file or not.
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && echo envtoken=1 || echo envtoken=0
'''


async def _probe_claude_auth(vm):
    rc, out, err = await _locus_run(vm, _AUTH_PROBE, 30)
    kv = {}
    for ln in (out or "").splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            kv[k.strip()] = v.strip()
    now = int(time.time())
    exp = int(kv["expires_at"]) if kv.get("expires_at", "").isdigit() else None
    method = ("oauth-token" if kv.get("envtoken") == "1" else   # served by the toolserver (1.0.64)
              "oauth" if kv.get("oauth") == "1" and not (exp is not None and exp < now) else
              "api-key" if kv.get("apikey") == "1" else
              "env-api-key" if kv.get("envkey") == "1" else "none")
    return {
        "ok": rc == 0, "locus": vm or "host", "user": kv.get("user", ""),
        "home": kv.get("home", ""), "installed": kv.get("installed") == "1",
        "version": kv.get("version", ""), "credentials_file": kv.get("creds") == "1",
        "credentials_path": (kv.get("home", "~") + "/.claude/.credentials.json"),
        "method": method, "authed": method != "none",
        "has_refresh_token": kv.get("refresh") == "1",
        "expires_at": exp, "expired": (exp is not None and exp < now),
        "subscription": kv.get("subscription", ""),
        "error": (err or "").strip()[:300] if rc != 0 else "",
    }


async def api_claude_auth(request):
    """GET /api/claude/auth?vm=<name|@keeper> — does the seat that A would run
    in have a Claude login? Probes the locus (host, or the VM as the dev user),
    never the browser. Carries the EXACT steps to create one when absent."""
    vm = (request.query.get("vm") or "").strip()
    if vm == "@keeper":
        vm = ""
    elif vm and vm not in await all_locus_names():
        return web.json_response({"error": f"unknown station {vm}"}, status=404)
    elif not vm:
        vm = MODEL_VM
    st = await _probe_claude_auth(vm)
    st["login_steps"] = [
        "Open the frontier surface with backend 'claude-code' (or ⌂ keeper for the host seat). "
        "The seat prints this same notice, then starts Claude Code.",
        "Claude Code detects no login and shows its login picker: choose "
        "'Claude account with subscription' (OAuth) — or 'Anthropic Console account' for an API key.",
        "A URL is printed (and opened if a browser is reachable). Open it on ANY device, "
        "sign in, approve, and paste the code back into the terminal.",
        "Credentials are written to " + st["credentials_path"] + " for this locus "
        "(symlinked into every seat dir — refresh tokens rotate, so seats never copy them).",
        "Headless/offline alternative: on a machine with a browser run `claude setup-token`, "
        "then paste the token here as ANTHROPIC_API_KEY in the seat's environment — "
        "or copy .credentials.json into the path above.",
    ]
    return web.json_response(st)


# --- agent-sudo, per LOCUS (host or VM), one mechanism --------------------- #
AGENT_SUDO_SHIPPED = VM_BIN / "agent-sudo"
_VM_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


# One probe script for both loci. Reports: the station drop-in (on/rule),
# EVERY OTHER sudoers source that names the grantee (cloud-init's
# 90-cloud-init-users is the classic one — without this a "revoked" readout
# would be a lie), the drop-ins the station itself disabled, the EFFECTIVE
# answer (can the grantee `sudo -n true` right now?), the log, the tool.
_SUDO_STATE_SCRIPT = r"""
U=__U__
if [ -f /etc/sudoers.d/agent-temp ]; then echo on=1; grep -v '^#' /etc/sudoers.d/agent-temp | grep -v '^[[:space:]]*$' | sed 's/^/rule=/'; else echo on=0; fi
for f in /etc/sudoers.d/*; do [ -f "$f" ] || continue; case "$f" in */agent-temp|*/README|*~|*.disabled) continue;; esac
  if grep -Eq "^[[:space:]]*($U|%sudo|%admin|%wheel)[[:space:]]" "$f" 2>/dev/null; then
    if id -nG "$U" 2>/dev/null | tr ' ' '
' | grep -qx sudo || grep -Eq "^[[:space:]]*$U[[:space:]]" "$f"; then
      echo "other=$f: $(grep -Ev '^[[:space:]]*(#|$)' "$f" | grep -E "^[[:space:]]*($U|%sudo|%admin|%wheel)" | head -2 | tr '
' ' ')"
    fi
  fi
done
for f in /etc/sudoers.d.disabled-by-station/*; do [ -f "$f" ] && echo "disabled=$f"; done
if id -nG "$U" 2>/dev/null | tr ' ' '
' | grep -qx sudo; then echo group=sudo; fi
if [ "$(id -un)" = "$U" ]; then
  # 1.0.64: a host arm whose only root path is the hugpy-gate registry is
  # "effective" too — report it as gate, not as a failed blanket grant.
  if sudo -n true >/dev/null 2>&1; then echo effective=1
  elif [ -x /usr/local/sbin/hugpy-gate ] && sudo -n /usr/local/sbin/hugpy-gate list >/dev/null 2>&1; then echo effective=gate
  else echo effective=0; fi
elif [ "$(id -u)" = 0 ]; then su -s /bin/bash "$U" -c 'sudo -n true' >/dev/null 2>&1 && echo effective=1 || echo effective=0
else echo effective=?; fi
[ -f /var/log/agent-sudo.log ] && tail -n 12 /var/log/agent-sudo.log | sed 's/^/log=/'
[ -x /usr/bin/agent-sudo ] && echo tool=1 || echo tool=0
[ -r /etc/sudoers.d ] && echo readable=1 || echo readable=0
"""


def _parse_sudo_state(out):
    st = {"on": False, "rules": [], "others": [], "disabled": [], "log": [],
          "tool": False, "group_sudo": False, "effective": None, "sources_readable": True}
    for ln in (out or "").splitlines():
        if ln.startswith("on="):
            st["on"] = ln[3:] == "1"
        elif ln.startswith("rule="):
            st["rules"].append(ln[5:])
        elif ln.startswith("other="):
            st["others"].append(ln[6:])
        elif ln.startswith("disabled="):
            st["disabled"].append(ln[9:])
        elif ln.startswith("group=sudo"):
            st["group_sudo"] = True
        elif ln.startswith("effective="):
            v = ln[10:]
            st["effective"] = True if v in ("1", "gate") else False if v == "0" else None
            if v == "gate":
                st["gate"] = True      # 1.0.64: root only via the hugpy-gate registry
        elif ln.startswith("log="):
            st["log"].append(ln[4:])
        elif ln.startswith("tool="):
            st["tool"] = ln[5:] == "1"
        elif ln.startswith("readable="):
            st["sources_readable"] = ln[9:] == "1"
    return st


# Revoke must be REAL: besides removing agent-temp, move every other
# drop-in that grants the grantee aside (reversible by hand, listed in the
# readout as "disabled"). /etc/sudoers itself is never edited.
_SUDO_DISABLE_OTHERS = r"""
U=__U__
mkdir -p /etc/sudoers.d.disabled-by-station
for f in /etc/sudoers.d/*; do [ -f "$f" ] || continue; case "$f" in */agent-temp|*/README) continue;; esac
  if grep -Eq "^[[:space:]]*$U[[:space:]]" "$f" 2>/dev/null; then
    mv -f "$f" "/etc/sudoers.d.disabled-by-station/$(basename "$f")" && echo "disabled=$f"
  fi
done
"""


async def _vm_sudo_state(vm):
    """Status inside a VM: drop-in present? rule line? last log lines?"""
    script = _SUDO_STATE_SCRIPT.replace("__U__", "ubuntu")
    rc, out, err = await _run("lxc", "exec", vm, "--", "bash", "-c", script)
    st = _parse_sudo_state(out)
    st.update({"ok": rc == 0, "error": (err or "").strip()[:200] if rc != 0 else ""})
    return st


async def _host_sudo_state():
    rc, out, err = await _run("bash", "-c", _SUDO_STATE_SCRIPT.replace("__U__", getpass.getuser()))
    st = _parse_sudo_state(out)
    st["on"] = _agent_sudo_on()       # exists() is authoritative even when 0440 hides the rule
    st["tool"] = st["tool"] or AGENT_SUDO_SHIPPED.exists()
    st.update({"ok": True, "error": ""})
    return st


def _sudo_scope(request):
    """'' = host; else a VM name. ?vm=@keeper and ?scope=host both mean host."""
    vm = (request.query.get("vm") or request.query.get("scope") or "").strip()
    return "" if _is_host_vm(vm) else vm


async def _ssh_sudo_state(name):
    h = _ssh_host(name); user = h.get("user", "root")
    rc, out, err = await _ssh_run(name, _SUDO_STATE_SCRIPT.replace("__U__", user), 30)
    st = _parse_sudo_state(out)
    st.update({"ok": rc == 0, "error": (err or "").strip()[:200] if rc != 0 else ""})
    return st


async def api_sudo_status(request):
    """GET /api/sudo/status?vm=<locus> — explicit: grantee, mechanism, rule, log."""
    vm = _sudo_scope(request)
    if vm and _ssh_host(vm):
        h = _ssh_host(vm); user = h.get("user", "root")
        st = await _ssh_sudo_state(vm)
        st.update({"locus": vm, "grantee": user, "kind": "ssh",
                   "dropin": "/etc/sudoers.d/agent-temp (on " + h["host"] + ")",
                   "mechanism": "ssh " + user + "@" + h["host"] + " sudo -n agent-sudo on|off — works only if "
                                + user + " can already sudo WITHOUT a password there; otherwise run "
                                "`sudo ~/agent-sudo on|off` in its ⇥ terminal (the tool is pushed for you)",
                   "covers": "every seat of " + vm + " (frontier, local, shell) — all run as '" + user + "' over ssh"})
        return web.json_response(st)
    if vm:
        if vm not in await known_names():
            return web.json_response({"error": f"unknown station {vm}"}, status=404)
        st = await _vm_sudo_state(vm)
        st.update({"locus": vm, "grantee": "ubuntu",
                   "dropin": "/etc/sudoers.d/agent-temp (inside " + vm + ")",
                   "mechanism": "lxc exec " + vm + " -- agent-sudo on|off (root in the VM; "
                                "the host console is the only party that can reach it)",
                   "covers": "A (frontier: mct + claude-code), B (local), and the shell of "
                             + vm + " — they all run as the same unix user 'ubuntu', so the "
                             "grant is per STATION, not per model"})
    else:
        st = await _host_sudo_state()
        has_pk = bool(shutil.which("pkexec"))
        st.update({"locus": "host", "grantee": getpass.getuser(),
                   "dropin": AGENT_SUDO_DROPIN, "pkexec": has_pk,
                   "mechanism": ("pkexec " + AGENT_SUDO_BIN + " on|off — polkit asks a HUMAN "
                                 "at this desktop for a password; no agent can answer it") if has_pk
                                else ("no pkexec/polkit in this locus (a VM workbench): grant or "
                                      "revoke from the HOST station's 🛡 steward tab — dropdown → "
                                      "this station → sudo. That is the only path, by design."),
                   "covers": "the ⌂ keeper host seat (claude as '" + getpass.getuser()
                             + "'), the host-grounded frontier/local seats, and any host shell "
                             "— same unix user, so the grant is per HOST, not per model"})
    return web.json_response(st)


async def api_sudo_toggle(request):
    """POST /api/sudo/toggle?vm=<locus> {"desired":"on"|"off"} — grant or revoke
    passwordless sudo for that locus's agent user. Host: via pkexec (human
    auth). VM: via lxc exec as root, pushing the shipped agent-sudo first if
    the VM lacks it. Audited either way."""
    vm = _sudo_scope(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    desired = body.get("desired")
    if desired not in ("on", "off"):
        return web.json_response({"error": 'body must be {"desired":"on"|"off"}'}, status=400)
    if not vm:
        return await api_agent_sudo_toggle(request)   # the existing host path (pkexec)
    if _ssh_host(vm):
        h = _ssh_host(vm); user = h.get("user", "root")
        tool = AGENT_SUDO_SHIPPED.read_text() if AGENT_SUDO_SHIPPED.exists() else ""
        await _ssh_run(vm, "cat > ~/agent-sudo && chmod 755 ~/agent-sudo", 20, stdin=tool)
        rc, out, err = await _ssh_run(vm, f"sudo -n env AGENT_SUDO_USER={shlex.quote(user)} ~/agent-sudo {desired} 2>&1"
                                          + (f" && sudo -n bash -c {shlex.quote(_SUDO_DISABLE_OTHERS.replace('__U__', user))}" if desired == "off" else ""), 40)
        ok = rc == 0
        audit(request, f"agent-sudo-{desired}@ssh:{vm}", (out or err).strip()[:200], ok=ok)
        st = await _ssh_sudo_state(vm)
        if not ok:
            return web.json_response({"ok": False, "on": st["on"], "effective": st["effective"],
                                      "error": "remote sudo refused without a password — open " + vm + "'s ⇥ terminal and run: "
                                               f"sudo ~/agent-sudo {desired}   (" + (out or err).strip()[:120] + ")"}, status=403)
        return web.json_response({"ok": True, "on": st["on"], "desired": desired, "rules": st["rules"],
                                  "effective": st["effective"], "others": st["others"], "disabled": st["disabled"]})
    if vm not in await known_names():
        return web.json_response({"error": f"unknown station {vm}"}, status=404)
    st = await _vm_sudo_state(vm)
    if not st["tool"]:
        if not AGENT_SUDO_SHIPPED.exists():
            return web.json_response({"ok": False, "error": "agent-sudo not shipped"}, status=500)
        rc, out, err = await _run("lxc", "file", "push", "--mode", "0755",
                                  str(AGENT_SUDO_SHIPPED), f"{vm}/usr/bin/agent-sudo")
        if rc != 0:
            return web.json_response({"ok": False, "error": "push agent-sudo: " + (err or out).strip()},
                                     status=502)
    rc, out, err = await _run("lxc", "exec", vm, "--", "env", "AGENT_SUDO_USER=ubuntu",
                              "/usr/bin/agent-sudo", desired)
    ok = rc == 0
    disabled = []
    if ok and desired == "off":
        rc2, out2, _e2 = await _run("lxc", "exec", vm, "--", "bash", "-c",
                                    _SUDO_DISABLE_OTHERS.replace("__U__", "ubuntu"))
        disabled = [l[9:] for l in (out2 or "").splitlines() if l.startswith("disabled=")]
    audit(request, f"agent-sudo-{desired}@{vm}",
          (((out or err) or "").strip()[:160] + (" disabled=" + ",".join(disabled) if disabled else "")), ok=ok)
    st = await _vm_sudo_state(vm)
    if not ok:
        return web.json_response({"ok": False, "on": st["on"], "effective": st["effective"],
                                  "error": (err or out).strip() or f"agent-sudo rc={rc}"}, status=500)
    return web.json_response({"ok": True, "on": st["on"], "desired": desired, "rules": st["rules"],
                              "effective": st["effective"], "others": st["others"],
                              "disabled_now": disabled, "disabled": st["disabled"]})


# --- ssh as an explicit alternative transport into a station ----------------- #
async def api_vm_ssh_info(request):
    """GET /api/vm/{vm}/ssh-info — everything needed to ssh in, and whether it
    would work right now: ip, sshd state, the fleet key, the exact command."""
    vm = request.match_info["vm"]
    h = _ssh_host(vm)
    if h:
        up = await _ssh_alive(h)
        return web.json_response({"ok": True, "vm": vm, "kind": "ssh", "ip": h["host"], "user": h.get("user", "root"),
                                  "port": h.get("port", 22), "sshd": "active" if up else "unreachable",
                                  "fleet_key": h.get("key", ""), "ready": up, "command": _ssh_shell_cmd(h),
                                  "exec_command": "", "enable_hint": ""})
    if vm not in await known_names():
        return web.json_response({"error": f"unknown station {vm}"}, status=404)
    ip = await _vm_ip(vm)
    rc, out, _e = await _run("lxc", "exec", vm, "--", "bash", "-c",
                             "systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null || echo inactive")
    sshd = (out or "").strip().splitlines()[-1] if out else "unknown"
    fleet_key = FV_STATE_HOME / "fleet_ssh_key"
    ident = f" -o IdentitiesOnly=yes -i {fleet_key}" if fleet_key.is_file() else ""
    cmd = (f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null{ident} ubuntu@{ip}"
           if ip else "")
    return web.json_response({
        "ok": True, "vm": vm, "ip": ip, "sshd": sshd, "user": "ubuntu",
        "fleet_key": str(fleet_key) if fleet_key.is_file() else "",
        "ready": bool(ip) and sshd == "active",
        "command": cmd,
        "exec_command": f"lxc exec {vm} -- sudo -u ubuntu -H bash -l",
        "enable_hint": (f"lxc exec {vm} -- bash -c 'apt-get install -y openssh-server && "
                        "systemctl enable --now ssh'") if sshd != "active" else "",
    })


# --- a hugpy-station (workbench) INSIDE each station, installable from the UI -- #
INSTALL_VM_CONSOLE = VM_BIN / "install-vm-console"
INSTALL_SSH_CONSOLE = VM_BIN / "install-ssh-console"   # ssh-locus twin (adopt-or-install)
_INSTALL_JOBS = {}    # vm -> {"state": "running"|"done"|"failed", "rc", "log"}


def _console_installer(vm):
    """The right workbench installer for a locus: lxd → install-vm-console,
    ssh → install-ssh-console (which ssh'es into the host it is to be a
    console of)."""
    return INSTALL_SSH_CONSOLE if _ssh_host(vm) else INSTALL_VM_CONSOLE


def _vmconsole_token_dir():
    """Where the per-VM workbench tokens live: the site's /srv/vm_mgr/secrets
    when it exists and is writable, else the user's config dir. The same rule
    install-vm-console uses (via $VMCONSOLE_TOKEN_DIR)."""
    env = os.environ.get("VMCONSOLE_TOKEN_DIR")
    if env:
        return Path(env)
    p = Path("/srv/vm_mgr/secrets")
    if p.is_dir() and os.access(p, os.W_OK):
        return p
    return FV_STATE_HOME / "vmconsole"


async def _tcp_open(ip, port, timeout=1.5):
    try:
        fut = asyncio.open_connection(ip, int(port))
        r, w = await asyncio.wait_for(fut, timeout)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def api_vm_console_status(request):
    """GET /api/stations/{name}/console-status — is a hugpy-station installed
    and serving inside this locus (VM or ssh host)? token present, port
    answering, install log."""
    return _retired("workbench installer", request.match_info.get("name", ""))
    vm = request.match_info["name"]
    h = _ssh_host(vm)
    if vm not in await known_names() and not h:
        return web.json_response({"error": f"unknown locus {vm}"}, status=404)
    port = os.environ.get("VMCONSOLE_PORT", "8800")
    tokf = _vmconsole_token_dir() / f"vmconsole-{vm}.token"
    ip = h["host"] if h else await _vm_ip(vm)
    serving = bool(ip) and await _tcp_open(ip, port)
    job = _INSTALL_JOBS.get(vm, {})
    logp = FV_STATE_HOME / f"install-vm-console-{vm}.log"
    tail = ""
    try:
        tail = "\n".join(logp.read_text(errors="replace").splitlines()[-25:])
    except OSError:
        pass
    return web.json_response({
        "ok": True, "vm": vm, "ip": ip, "port": port,
        "token": tokf.is_file(), "token_path": str(tokf),
        "serving": serving, "installed": tokf.is_file() and serving,
        "installer": str(_console_installer(vm)), "installer_present": _console_installer(vm).is_file(),
        "job": job.get("state", ""), "rc": job.get("rc"), "log": tail, "log_path": str(logp),
        "url": (f"http://{ip}:{port}/" if ip else ""),
    })


async def _run_install_vm_console(vm):
    logp = FV_STATE_HOME / f"install-vm-console-{vm}.log"
    FV_STATE_HOME.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "VMCONSOLE_TOKEN_DIR": str(_vmconsole_token_dir()),
           "VMCONSOLE_PORT": os.environ.get("VMCONSOLE_PORT", "8800"),
           "HUGPY_STATION_ROOT": str(ROOT.parent)}
    with open(logp, "ab") as lf:
        lf.write(f"\n=== install-vm-console {vm} @ {time.strftime('%Y-%m-%dT%H:%M:%S')} ===\n".encode())
        lf.flush()
        proc = await asyncio.create_subprocess_exec(
            "bash", str(_console_installer(vm)), vm, stdout=lf, stderr=asyncio.subprocess.STDOUT, env=env)
        rc = await proc.wait()
    _INSTALL_JOBS[vm] = {"state": "done" if rc == 0 else "failed", "rc": rc}


async def api_vm_console_install(request):
    """POST /api/stations/{name}/console-install — install (or re-install /
    upgrade) a headless hugpy-station inside the VM from THIS host's own
    package files, serving on :8800 behind a per-VM token. Runs in the
    background; poll console-status. Provision-gated like create/delete."""
    return _retired("workbench installer", request.match_info.get("name", ""))
    vm = request.match_info["name"]
    if vm not in await known_names() and not _ssh_host(vm):
        return web.json_response({"error": f"unknown locus {vm}"}, status=404)
    if not _console_installer(vm).is_file():
        return web.json_response({"error": f"{_console_installer(vm).name} not shipped"}, status=500)
    if _INSTALL_JOBS.get(vm, {}).get("state") == "running":
        return web.json_response({"ok": True, "vm": vm, "job": "running"})
    _INSTALL_JOBS[vm] = {"state": "running"}
    audit(request, "install-vm-console", vm, ok=True)
    asyncio.ensure_future(_run_install_vm_console(vm))
    return web.json_response({"ok": True, "vm": vm, "job": "running"})


# --- ◳ canvas for the HOST seat: design (wireframe.json) + flow (flow.json) ---- #
# VMs keep theirs in ~ubuntu (console-api); the host seat keeps the same two
# files in the service user's home, where the host keeper reads/writes them.
# Same JSON contract as the VM routes, so the drawer is locus-agnostic.
_HOST_CANVAS = {"design": (Path(_hugpy_path("state", "wireframe.json", "wireframe.json")), "shapes", 300),
                "flow": (Path(_hugpy_path("state", "flow.json", "flow.json")), "nodes", 600)}


async def api_host_canvas(request, kind=None):
    kind = kind or request.match_info["kind"]
    own = _station_locus()
    if own:                       # the host seat IS a locus → one central copy
        return await _central_canvas_route(request, own, kind)
    path, key, cap = _HOST_CANVAS[kind]
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
        st = (body or {}).get("state")
        if not isinstance(st, dict) or not isinstance(st.get(key), list) or len(st[key]) > cap:
            return web.json_response({"error": f"body must carry a {kind} with {key}[] (≤{cap})"}, status=400)
        try:
            path.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        audit(request, kind + "_push@host", f"{len(st[key])} {key}", ok=True)
        return web.json_response({"ok": True, "vm": "@keeper", "path": str(path), "mtime": path.stat().st_mtime})
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return web.json_response({"ok": False, "error": f"no {path} on the host yet"}, status=404)
    except ValueError:
        return web.json_response({"ok": False, "error": f"{path} is not valid JSON"}, status=502)
    return web.json_response({"ok": True, "vm": "@keeper", "path": str(path),
                              "mtime": path.stat().st_mtime, "state": st})


# --- 👁 show a file IN a seat's terminal without it entering the model's
# context (operator, 2026-08-22): a tmux split in that backend's own session
# runs `less` on the file. The seat's model never sees the content — the
# operator reads it in the same pane. `q` (or close) removes the split.
_SHOW_PANE_MARK = "fv-show"


async def api_term_show(request):
    """POST /api/term/show {path, surface?, backend?, vm?, close?}"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    surface = (body.get("surface") or "frontier").strip()
    backend = (body.get("backend") or "").strip().lower()
    vm = (body.get("vm") or "").strip()
    ground_vm = "" if _is_host_vm(vm) and vm else (vm or MODEL_VM)
    tsess = _tmux_session_for(surface, backend)
    if not tsess:
        return web.json_response({"error": "surface/backend has no persistent tmux session"}, status=400)
    tgt = shlex.quote(tsess + ":0")
    base = f"tmux -L {KEEPER_TMUX_SOCK} "
    if body.get("close"):
        script = (base + f"list-panes -t {tgt} -F '#{{pane_id}} #{{@{_SHOW_PANE_MARK}}}' | "
                  "awk '$2==\"1\"{print $1}' | xargs -r -n1 " + base + "kill-pane -t")
        rc, out, err = await LX.run(_locus_target(ground_vm), script, timeout=15)
        return web.json_response({"ok": rc == 0, "closed": True, "err": err[-300:]})
    path = (body.get("path") or "").strip()
    if not path or "\n" in path:
        return web.json_response({"error": "path required"}, status=400)
    title = path.rsplit("/", 1)[-1]
    pane_cmd = ("less -R -P " + shlex.quote(title + "  (q closes · / searches)") + " -- "
                + shlex.quote(path))
    # one show pane at a time: close a previous one first, then split below
    script = (base + f"list-panes -t {tgt} -F '#{{pane_id}} #{{@{_SHOW_PANE_MARK}}}' | "
              "awk '$2==\"1\"{print $1}' | xargs -r -n1 " + base + "kill-pane -t; "
              + base + f"split-window -v -l 45% -t {tgt} -P -F '#{{pane_id}}' " + shlex.quote(pane_cmd)
              + " | xargs -r -I{} " + base + f"set-option -p -t {{}} @{_SHOW_PANE_MARK} 1")
    rc, out, err = await LX.run(_locus_target(ground_vm), script, timeout=15)
    if rc != 0:
        return web.json_response({"ok": False, "error": (err or out)[-300:]}, status=502)
    audit(request, "term_show", f"{surface}/{backend or 'default'}: {path}", ok=True)
    return web.json_response({"ok": True, "session": tsess, "path": path})


# --- ⋔ flow library: permanent flow.v1 documents beside the code ------------ #
# docs/flows/<id>.flow.json (contract: docs/FLOWS.md). The drawer's 📚 lists
# these; the working copy stays ~/flow.json (api_host_canvas) until a
# revision is deliberately saved back here.
FLOWS_DIR = STATIC / "docs" / "flows"
_FLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


async def api_flows_list(request):
    rows = []
    for f in sorted(FLOWS_DIR.glob("*.flow.json")):
        fid = f.name[:-len(".flow.json")]
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows.append({"id": fid, "error": "unreadable"}); continue
        rows.append({"id": fid, "title": d.get("title", ""), "status": d.get("status", ""),
                     "rev": d.get("rev"), "implements": d.get("implements", []),
                     "nodes": len(d.get("nodes") or []), "path": "docs/flows/" + f.name,
                     "mtime": f.stat().st_mtime})
    return web.json_response({"ok": True, "flows": rows, "dir": str(FLOWS_DIR)})


async def api_flow_save(request):
    """POST /api/flows/<id> {state} — make a drawer revision permanent."""
    fid = request.match_info["fid"]
    if not _FLOW_ID_RE.match(fid):
        return web.json_response({"error": "bad flow id"}, status=400)
    try:
        st = (await request.json() or {}).get("state")
    except Exception:
        st = None
    if (not isinstance(st, dict) or st.get("schema") != "flow.v1"
            or not isinstance(st.get("nodes"), list) or len(st["nodes"]) > 600):
        return web.json_response({"error": "body must carry a flow.v1 state with nodes[] (≤600)"}, status=400)
    st["id"] = fid
    FLOWS_DIR.mkdir(parents=True, exist_ok=True)
    path = FLOWS_DIR / (fid + ".flow.json")
    try:
        path.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    audit(request, "flow_save", fid, ok=True)
    return web.json_response({"ok": True, "id": fid, "path": str(path)})


# --- one readout for the whole seat (what the steward tab renders) ----------- #
async def api_mct2_status(request):
    """GET /api/mct2/status?vm=<locus> — the mct2 REPL's live flight/queue
    state, read from the status.json its Arbiter mirrors on every state change
    (atomic rename): whether A is working (state/turn/since), A's resumed
    session id (chat history), and every pending query (coalesced/appended/
    reconcile). Locus-aware: the REPL runs WITHIN the locus the seat is
    attributed to, so a vm= grounding reads the file THERE (one remote python
    one-liner that also does the /proc pid liveness check in place; age_s then
    mixes the locus's clock with ours — treat small values as noise). With no
    vm (or @keeper) the host copy is read directly. If the writer pid is gone
    the state degrades to "offline" regardless of what the file last said."""
    req_vm = (request.query.get("vm") or "").strip()
    ground = "" if req_vm in ("", "@keeper") else req_vm
    if ground and ground not in await all_locus_names():
        ground = ""
    if ground:
        await _loci_ready()
    central = _central_locus(ground) if ground else ""
    if central and central != _keeper_locus():
        # 1.0.138: every non-host locus kind reads the mct seat's REPORTED state
        # (central seat_state) — no python probe of a station file on the locus
        rows = await _seat_rows(request.app, central, "mct")
        if not rows:
            return web.json_response({"present": False, "locus": central,
                                      "source": "toolserver seat/state"})
        r = rows[0]
        doc = dict(r.get("status") or {})
        doc["alive"] = bool(r.get("alive"))
        if not doc["alive"] and doc.get("state") != "offline":
            doc["state"] = "offline"
        doc.update({"present": True, "locus": central, "source": "toolserver seat/state",
                    "age_s": r.get("reported_ago_s"), "model": doc.get("model") or r.get("model"),
                    "reporter_pid": r.get("pid")})
        return web.json_response(doc)
    p = FV_STATE_HOME / "mct2" / "repl" / "status.json"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(doc, dict)
    except (OSError, ValueError, AssertionError):
        return web.json_response({"present": False, "locus": "host", "path": str(p)})
    pid = doc.get("pid")
    alive = bool(pid) and Path("/proc/%s" % pid).exists()
    if not alive and doc.get("state") != "offline":
        doc["state"] = "offline"    # REPL died without writing its exit state
    doc.update({"present": True, "alive": alive, "locus": "host", "path": str(p),
                "age_s": round(time.time() - (doc.get("updated") or 0), 1)})
    return web.json_response(doc)


_EXCH_FILE_RE = re.compile(r"^turn-\d{4}-(prompt|response)\.md$")


async def api_mct2_exchange_file(request):
    """GET /api/mct2/exchange?file=turn-NNNN-{prompt|response}.md — that
    exchange file's content, for 📡 live's expandable turn rows. The name is
    shape-validated and resolved ONLY inside the host mct exchange dir (never
    an arbitrary path). With an active VM the read forwards to that VM's
    workbench (grounding rule), same as /api/mct/live itself."""
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
    name = (request.query.get("file") or "").strip()
    if not _EXCH_FILE_RE.match(name):
        return web.json_response({"error": "bad exchange file name"}, status=400)
    p = FV_STATE_HOME / "mct2" / "repl" / "exchange" / name
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return web.json_response({"error": name + ": not readable"}, status=404)
    return web.json_response({"ok": True, "file": name, "path": str(p),
                              "content": text[-200000:]})


async def _seat_rows(app, locus, seat=""):
    body = {"locus": locus}
    if seat:
        body["seat"] = seat
    try:
        rows = await _ts_call(app, "seat/state", body, timeout=8)
        return [r for r in (rows or []) if isinstance(r, dict)]
    except Exception:
        return None


async def _seat_auth_from_toolserver(app, locus):
    """claude_auth for an ssh locus, from what its seats REPORTED (seat/state)
    — the same document _probe_claude_auth built by ssh-ing in (1.0.77)."""
    rows = await _seat_rows(app, locus)
    h = _ssh_host(locus) or {}
    if rows is None:
        return {"ok": False, "locus": locus, "user": h.get("user", ""), "method": "unknown",
                "authed": False, "installed": False, "error": "toolserver seat/state unreachable"}
    live = [r for r in rows if r.get("alive")]
    src = live or rows
    auth_ok = any(r.get("auth_ok") for r in src) if src else False
    latest = max(src, key=lambda r: r.get("reported") or 0) if src else {}
    return {"ok": True, "locus": locus, "user": latest.get("login") or h.get("user", ""),
            "home": "", "installed": bool(rows), "version": "",
            "credentials_file": False, "credentials_path": "",
            "method": "oauth-token" if auth_ok else "none", "authed": auth_ok,
            "has_refresh_token": False, "expires_at": None, "expired": False,
            "subscription": "", "error": "" if rows else "no seat has reported yet",
            "source": "toolserver seat/state", "seats": [
                {"seat": r.get("seat"), "alive": r.get("alive"), "stale": r.get("stale"),
                 "model": r.get("model"), "reported_ago_s": r.get("reported_ago_s")} for r in rows]}


async def api_seat_status(request):
    """GET /api/seat?vm=<locus> — everything this locus's seats launch with,
    in one document: grounding, models, directive source, fs switch, sudo,
    claude auth, frontier toggle. If it affects a seat, it is here."""
    req_vm = (request.query.get("vm") or "").strip()
    if req_vm == "@keeper":
        ground = ""
    elif req_vm and req_vm in await all_locus_names():
        ground = req_vm
    else:
        ground = MODEL_VM
    if ground:
        await _loci_ready()
    central = _central_locus(ground) if ground else ""
    if central and central != _keeper_locus():
        # 1.0.138: what the locus's seats REPORTED (central seat_state) for every
        # kind — no ssh/lxc probe of ~/.claude on the locus
        auth = await _seat_auth_from_toolserver(request.app, central)
    else:
        auth = await _probe_claude_auth(ground if not central else "")
    models = _frontier_models()
    dtext, dsrc, dpath = _frontier_directive_text()
    return web.json_response({
        "requested": req_vm or "@keeper", "grounded_in": ground or "host",
        "locus": central or _keeper_locus(),
        "seat_user": ((_ssh_host(ground) or {}).get("user", "root") if _ssh_host(ground)
                      else "ubuntu" if ground else getpass.getuser()),
        "kind": ("ssh" if _ssh_host(ground) else "lxd" if ground else "host"),
        "frontier_enabled": _frontier_enabled(),
        "models": models,
        "directive": {"source": dsrc, "path": str(dpath), "chars": len(dtext)},
        "fs": {"mediated": _frontier_fs_mediated(),
               "applies_to": ["frontier:mct", "frontier:claude-code"],
               "never_applies_to": ["local", "shell"]},
        "claude_auth": auth,
        "launch": {
            "frontier:claude-code": _claude_seat_cmd("frontier"),
            "frontier:mct": "abstract-claude mct <ws> --model " + models["mct"],
            "local": TERM_SURFACES["local"]["backends"][TERM_SURFACES["local"]["default"]],
            "shell": (_ssh_shell_cmd(_ssh_host(ground)) if _ssh_host(ground)
                      else ("lxc exec " + ground + " -- sudo -u ubuntu -H bash -l") if ground
                      else "bash -l (this host, as " + getpass.getuser() + ")"),
        },
    })


async def api_firstrun(request):
    """GET /api/firstrun — is this box ready to run model seats? Reports which
    seat CLIs are present on the HOST and whether a durable token is stored, so a
    lay first-run pane can show a single 'Set up' button when something's missing."""
    import shutil
    need = {"abstract-claude": "frontier: mct", "claude": "frontier: claude-code",
            "hugpy-agent": "local"}
    seats = {exe: {"present": bool(shutil.which(exe)), "for": role}
             for exe, role in need.items()}
    token = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                 or (Path.home() / ".config/claude-auth/env").exists()
                 or (Path.home() / ".claude-oauth.env").exists()
                 or _fetch_toolserver_oauth_token())
    ready = all(s["present"] for s in seats.values()) and token
    return web.json_response({"ready": ready, "seats": seats, "token": token})


async def api_firstrun_provision(request):
    """POST /api/firstrun/provision — run the seat-provision bootstrap (python/
    pip/venv/upgrade + install abstract-claude/hugpy-agent/claude + store token).
    Best-effort; returns the log tail. Homebase/VPN gated like other actions."""
    require_provision(request)
    prov = ROOT.parent / "station-stack" / "opt" / "station-keeper" / "seat-provision.sh"
    if not prov.exists():
        return web.json_response({"ok": False, "error": "seat-provision.sh not found"}, status=500)
    body = await request.json() if request.can_read_body else {}
    env = dict(os.environ)
    tok = (body.get("token") or "").strip()
    if tok:
        env["HUGPY_STATION_OAUTH"] = tok
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(prov), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), 600)
        tail = "\n".join(out.decode("utf-8", "replace").splitlines()[-25:])
        return web.json_response({"ok": proc.returncode == 0, "log": tail})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


# backend → AC_SESSION_LABEL suffix on its per-launch ~/.claude-sessions dir.
# claude-code labels with "frontier" (_claude_seat_cmd: AC_SESSION_LABEL=frontier);
# mct labels with "mct" (ws_hostterm injects AC_SESSION_LABEL=mct). The scoped
# wipe uses this to delete ONLY the requested backend's dirs.
SEAT_WIPE_LABEL = {"claude-code": "frontier", "mct": "mct"}


async def api_seat_wipe(request):
    """POST /api/seat/wipe?vm=<locus>&backend=claude-code|mct — factory-reset
    ONE frontier backend on that locus (operator 2026-09-10: "wipe seat must
    wipe THAT backend's .claude — never destroy the other, never destroy
    login"). Kills only that backend's tmux session (keeper-claude / keeper-mct)
    and deletes only its per-launch config dirs under ~/.claude-sessions matched
    by the backend's AC_SESSION_LABEL suffix (*-frontier / *-mct — see
    SEAT_WIPE_LABEL). The SHARED login (~/.claude.json, ~/.claude-oauth.env, the
    durable OAuth token) and the OTHER backend's dirs are deliberately NOT
    touched, so both seats stay logged in and the sibling seat's state survives;
    the next launch of the wiped backend re-provisions a fresh labeled session
    dir from the surviving shared login. Defaults to claude-code when no backend
    is given (back-compat with the pre-per-backend button)."""
    req_vm = (request.query.get("vm") or "").strip()
    if req_vm == "@keeper":
        ground = ""
    elif req_vm and req_vm in await all_locus_names():
        ground = req_vm
    else:
        ground = MODEL_VM
    backend = (request.query.get("backend") or "claude-code").strip().lower()
    # t-serve: the DEFAULT wipe on the host seat is now the serve surface's own
    # reset — drop serve's labeled per-launch session dirs and restart its unit.
    # NO tmux session is killed (keeper-claude is never touched). ?surface=tmux
    # (or backend=mct) still runs the legacy per-backend tmux wipe below.
    want = (request.query.get("surface") or "").strip().lower()
    if not ground and want != "tmux" and backend in ("serve", "claude-code"):
        surface_now, _ac = await _keeper_surface_now()
        if want == "serve" or surface_now == "serve":
            label = os.environ.get("AC_SESSION_LABEL") or "keeper-serve"
            rc, out, err = await _locus_run(
                "", f'rm -rf "$HOME/.claude-sessions/"*-{label}; echo wiped', timeout=30)
            res = await _ac_serve_restart("seat wipe")
            ok = rc == 0 and "wiped" in (out or "") and bool(res.get("ok"))
            audit(request, "seat_wipe", f"locus=host surface=serve unit={AC_SERVE_UNIT} rc={rc}", ok=ok)
            return web.json_response({
                "ok": ok, "locus": "host", "surface": "serve", "backend": "serve",
                "unit": AC_SERVE_UNIT, "killed": None,
                "removed": [f"~/.claude-sessions/*-{label}"],
                "kept": ["~/.claude.json", "~/.claude-oauth.env (OAuth token)",
                         "every tmux seat (keeper-claude/keeper-codex untouched)"],
                "restart": res}, status=200 if ok else 502)
    if backend not in SEAT_WIPE_LABEL:
        return web.json_response(
            {"error": "backend must be claude-code|mct|serve"}, status=400)
    label = SEAT_WIPE_LABEL[backend]
    tsess = (_tmux_session_for("frontier", backend)
             or ("keeper-mct" if backend == "mct" else "keeper-claude"))
    # dir mode: each launch lands in ~/.claude-sessions/<stamp>-<pid>-<label>.
    # The quoted prefix + unquoted glob deletes only this backend's labeled dirs;
    # `rm -rf` with the literal (no match) is a silent no-op. Shared login and the
    # other backend's (differently-labeled / unlabeled) dirs are untouched.
    script = (
        f"tmux -L {KEEPER_TMUX_SOCK} kill-session -t ={tsess} 2>/dev/null; "
        f'rm -rf "$HOME/.claude-sessions/"*-{label}; echo wiped')
    rc, out, err = await _locus_run(ground, script, timeout=30)
    ok = rc == 0 and "wiped" in (out or "")
    audit(request, "seat_wipe",
          f"locus={ground or 'host'} backend={backend} sess={tsess} rc={rc}", ok=ok)
    if not ok:
        return web.json_response(
            {"error": (err or out or "wipe failed").strip(),
             "locus": ground or "host", "backend": backend}, status=502)
    other = "mct" if backend == "claude-code" else "claude-code"
    return web.json_response({
        "ok": True, "locus": ground or "host", "backend": backend, "killed": tsess,
        "removed": [f"~/.claude-sessions/*-{label}"],
        "kept": ["~/.claude.json", "~/.claude-oauth.env (OAuth token)",
                 f"the {other} backend's session dirs"],
        "relaunch": f"open the frontier {backend} seat — it re-provisions a fresh "
                    "labeled session dir from the surviving shared login"})


async def api_about(request):
    """GET /api/about — version + provenance for the Help→About popup and the
    shell title. Facts only the backend knows for sure: what is running, from
    where. The shell (main.js) adds its own app-root line."""
    try:
        version = (ROOT.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = "unknown"
    try:
        switches = _station_switches()
    except Exception:                                    # noqa: BLE001
        switches = {}
    return web.json_response({
        "name": "hugpy Station",
        "version": version,
        "backend_dir": str(ROOT),
        "python": sys.version.split()[0],
        "switches": switches,            # 1.0.146: live values of every inert-able default
        "toggles": {"frontier_keeper": _frontier_doc(), "delegate": _frontier_delegate_meta()},
        "author": "putkoff (hugpy.ai)",
        "homepage": "https://hugpy.ai",
        "source": "station-app monorepo (hugpy project) — ships as the "
                  "hugpy-station package",
        "attribution": "hugpy Station is part of the hugpy project "
                       "(https://hugpy.ai). Electron shell over a "
                       "self-contained aiohttp console backend; seats live in "
                       "tmux; loci via LXD / ssh. Seat backends: mct "
                       "(hugpy-agent), Claude Code (Anthropic), opencode, "
                       "qwen-code.",
    })


def _mct_sidecar_dirs():
    """Where the mct sidecars (mct_gateway.py, mct_http.py, bin/mct-pull,
    bin/mct-push, static/mct-renderer.js) may live besides this backend's own
    tree: the locus's per-version app tree under its state dir (how the keeper
    runs) — the /opt package may predate them (station.apply installs only
    server.py + two static files)."""
    return [ROOT.parent, FV_STATE_HOME / "app" / "current" / "resources"]


def _ensure_mct_cli_links():
    """~/.local/bin/mct-pull and mct-push → the shipped wrappers, so every
    native seat (bash -l PATH) can pull its context and push its reply. Only a
    missing link or a symlink is (re)pointed — a real file is never clobbered."""
    home_bin = Path(os.path.expanduser("~/.local/bin"))
    try:
        home_bin.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for name in ("mct-pull", "mct-push"):
        target = next((d / "bin" / name for d in _mct_sidecar_dirs() if (d / "bin" / name).is_file()), None)
        if target is None:
            continue
        link = home_bin / name
        try:
            if link.is_symlink():
                if os.readlink(link) != str(target):
                    link.unlink()
                    link.symlink_to(target)
            elif not link.exists():
                link.symlink_to(target)
        except OSError as e:
            print(f"mct: could not link {link} → {target}: {e}", file=sys.stderr)


async def _mct_static_fallback(request):
    """/static/mct-renderer.js when the packaged static dir predates it."""
    for d in _mct_sidecar_dirs():
        p = d / "backend" / "static" / "mct-renderer.js"
        if p.is_file():
            return web.FileResponse(p, headers={"Cache-Control": "no-store"})
    raise web.HTTPNotFound()


def make_app():
    app = web.Application(middlewares=[auth_mw, locus_vm_mw], client_max_size=MAX_UPLOAD_SIZE)
    # 2026-09-18: runtime-mutable state holder set BEFORE startup (pre-freeze)
    # so the hot-loop writes below do not trip aiohttp's "Changing state of
    # started application is deprecated" (spammed once per SSE event).
    app["_rt"] = {}
    # mct v2: the durable pointer-exchange ledger is the DEFAULT operator↔keeper
    # channel. Import its sidecars from this tree, else the locus's app tree.
    install_mct = None
    try:
        from mct_http import install as install_mct
    except ImportError:
        for d in _mct_sidecar_dirs():
            if (d / "backend" / "mct_http.py").is_file() and str(d / "backend") not in sys.path:
                sys.path.append(str(d / "backend"))
        try:
            from mct_http import install as install_mct
        except ImportError as e:
            print("mct: ledger broker NOT installed (mct_http/mct_gateway missing): " + str(e), file=sys.stderr)
    if install_mct is not None:
        install_mct(app, FV_STATE_HOME, _station_locus, _ts_call, _frontier_enabled,
                    collate=_mct_b_collate, capture=_mct_b_capture)
    _ensure_mct_cli_links()
    app.on_startup.append(_start_log_ring)
    app.on_startup.append(_start_directives)    # 1.0.143: re-derive the per-session directives
    app.on_startup.append(_start_handoff_retire) # 1.0.146: a leftover frontier-handoff.md is never delivered — say so
    app.on_startup.append(_start_reaper)
    app.on_cleanup.append(_stop_reaper)
    # console-api sidecar: the full fleet management API, same as the web console
    app.on_startup.append(_start_console_api)
    app.on_cleanup.append(_stop_console_api)
    app.on_startup.append(_start_bugreport_api)
    app.on_startup.append(_start_reminders)     # ⏱ board reminder cycle
    app.on_startup.append(_start_digest)        # 1.0.144 ⏱ open-todos digest -> every keeper session
    app.on_cleanup.append(_stop_digest)
    app.on_cleanup.append(_stop_reminders)
    app.on_startup.append(_start_bugscan)       # 🐞 intermittent local-agent log review
    app.on_cleanup.append(_stop_bugscan)
    app.on_cleanup.append(_stop_bugreport_api)
    app.router.add_get("/api/vms", api_vms_merged)     # sidecar rows + ⇅ ssh hosts
    app.router.add_get("/api/about", api_about)        # Help→About + shell title
    for _p in ("/api/health", "/api/models"):
        app.router.add_route("*", _p, api_proxy)
    # ☑ "keeper" board routes MUST register before the sidecar proxies: the
    # canonical TodoDrawer (vm="keeper") targets the ACTIVE SESSION's board —
    # same file + same flock as the agent's MCP todo tool — never a VM path.
    # messages stays unrouted (honest "route not yet enabled" degrade);
    # bugreport falls through to the sidecar.
    # 1.0.141: the host board lives at the RESERVED tokens (@self, and the
    # historic @keeper seat sentinel) — never at the literal `keeper`, which is
    # vm_mgr's registered locus key. /api/vm/<own locus name>/... is answered by
    # the same handlers in-process (_central_vm_reroute -> _host_vm_dispatch).
    for _ht in ("@self", "@keeper"):
        app.router.add_get(f"/api/vm/{_ht}/todo", mct_todo_get)
        app.router.add_post(f"/api/vm/{_ht}/todo", mct_todo_post)
        app.router.add_get(f"/api/vm/{_ht}/todo-history", keeper_todo_history)
        app.router.add_post(f"/api/vm/{_ht}/todo/assist", keeper_todo_assist)
        app.router.add_post(f"/api/vm/{_ht}/todo-revision", keeper_todo_revision)
        app.router.add_route("*", f"/api/vm/{_ht}/todo/discord", keeper_todo_discord)
        app.router.add_get(f"/api/vm/{_ht}/messages", keeper_messages_get)
    # ✉ fleet mail: the keeper console's inbox (before the {vm} proxy, so the
    # keeper drawer's messages tab lights up) + the one send endpoint.
    # explicit host-locus address (nomenclature 2026-08-27): /api/vm/host/* is
    # /api/vm/keeper/* — registered before every {vm} wildcard so it wins.
    app.router.add_route("*", "/api/vm/host/{tail:.*}", vm_host_alias)
    app.router.add_post("/api/fleet/message", fleet_message_post)
    app.router.add_post("/api/vm/{vm}/todo/brief", vm_todo_brief_post)   # ⧉ brief keeper
    app.on_startup.append(_start_fleet_mail)   # 30s station-outbox collector
    app.on_startup.append(_start_loci_sync)    # toolserver loci/pointers → ssh hosts + pulled sessions
    app.on_startup.append(_start_todo_ts_sync)  # 1.0.90: toolserver todos (own locus + '') → local board file
    app.on_cleanup.append(_stop_todo_ts_sync)
    app.on_startup.append(_start_local_seat_fit)  # 1.0.85: opencode/qwen-code get the toolserver bridge
    app.on_startup.append(_start_events_relay)  # 1.0.77: toolserver change bus → /api/events
    app.on_cleanup.append(_stop_events_relay)
    app.router.add_get("/api/events", api_events)
    app.router.add_get("/api/station/identity", api_station_identity)   # 1.0.141
    app.on_cleanup.append(_stop_loci_sync)
    app.on_startup.append(_start_idle_relaunch)  # 1.0.68: relaunch the idle host seat on a newer init prompt
    app.on_cleanup.append(_stop_idle_relaunch)
    app.on_cleanup.append(_stop_fleet_mail)
    # ◳ host-seat canvas BEFORE the bugreport suffix loop: "flow" is one of its
    # suffixes and used to swallow /api/vm/@keeper/flow ("unknown route").
    for _ht in ("@self", "@keeper"):
        app.router.add_get(f"/api/vm/{_ht}/{{kind:design|flow}}", api_host_canvas)
        app.router.add_post(f"/api/vm/{_ht}/{{kind:design|flow}}", api_host_canvas)
    for _suf in BUGREPORT_SUFFIXES:   # bugreport-api owns these per-VM
        app.router.add_route("*", "/api/vm/{vm}/" + _suf, bugreport_proxy)
    for _suf in ("todo-history", "todo/assist", "todo-revision", "messages"):
        app.router.add_route("*", "/api/vm/{vm}/" + _suf, vm_keeper_alias)   # 1.0.42: station board tabs
    app.router.add_get("/api/vm/{vm}/ssh-info", api_vm_ssh_info)   # 1.0.41: before the catch-all
    app.router.add_get("/api/ssh-hosts", api_ssh_hosts)
    app.router.add_post("/api/ssh-hosts", api_ssh_hosts)
    app.router.add_post("/api/term/show", api_term_show)
    app.router.add_get("/api/flows", api_flows_list)
    app.router.add_post("/api/flows/{fid}", api_flow_save)
    app.router.add_route("*", "/api/vm/{tail:.*}", api_proxy)
    app.router.add_route("*", "/api/console/{tail:.*}", api_proxy)
    app.router.add_route("*", "/api/discord/{tail:.*}", api_proxy)
    app.router.add_get("/", index)
    app.router.add_get("/login", login_get)
    app.router.add_post("/login", login_post)
    app.router.add_post("/logout", logout)
    app.router.add_get("/logout", logout)
    app.router.add_get("/api/stations", api_stations)
    app.router.add_get("/api/stations/{name}/console-url", api_console_url)
    app.router.add_post("/api/stations", api_create)          # provision (homebase-only)
    app.router.add_get("/api/jobs/{id}", api_job)             # build progress (poll)
    app.router.add_post("/api/stations/{name}/action/{action}", api_action)
    app.router.add_post("/api/stations/{name}/bridge", api_bridge)
    app.router.add_get("/api/llm/models", api_llm_models)
    app.router.add_post("/api/voice/transcribe", api_voice_transcribe)
    app.router.add_post("/api/llm/chat", api_llm_chat)
    app.router.add_post("/api/llm/agent", api_llm_agent)
    app.router.add_get("/api/wf/fetch", api_wf_fetch)         # Design drawer URL import
    # keeper (overseer) read-only surface
    app.router.add_get("/api/keeper/fleet", api_keeper_fleet)
    app.router.add_get("/api/keeper/logs/sessions", api_keeper_sessions)
    app.router.add_get("/api/keeper/logs/search", api_keeper_log_search)
    app.router.add_get("/api/keeper/logs/show", api_keeper_log_show)
    app.router.add_get("/api/keeper/backups", api_keeper_backups)
    app.router.add_get("/api/keeper/audit", api_keeper_audit)
    app.router.add_get("/api/keeper/directives", api_keeper_directives)
    app.router.add_get("/api/keeper/directives/show", api_keeper_dir_show)
    app.router.add_get("/api/keeper/directives/diff", api_keeper_dir_diff)
    app.router.add_get("/api/keeper/directives/resolve", api_keeper_dir_resolve)
    # file browser (read/write) — all act as the dev user inside the station
    app.router.add_get("/api/stations/{name}/fs/list", fs_list)
    app.router.add_get("/api/stations/{name}/fs/read", fs_read)
    app.router.add_get("/api/stations/{name}/fs/download", fs_download)
    app.router.add_post("/api/stations/{name}/fs/write", fs_write)
    app.router.add_post("/api/stations/{name}/fs/mkdir", fs_mkdir)
    app.router.add_post("/api/stations/{name}/fs/rename", fs_rename)
    app.router.add_post("/api/stations/{name}/fs/delete", fs_delete)
    app.router.add_post("/api/stations/{name}/fs/upload", fs_upload)
    app.router.add_get("/wsterm", ws_hostterm)
    # same-origin ttyd bridge: the SPA's per-VM panes (/term/?arg=<vm>) — the
    # route that puts a switched-to VM's shell IN that VM (term-wrapper).
    app.router.add_route("*", "/term/{tail:.*}", term_proxy)
    app.router.add_route("*", "/term", term_proxy)
    # session/focus subsystem surfaces (see SESSION-FOCUS-SUBSYSTEM.md on ae)
    app.router.add_route("*", "/ac/{tail:.*}", ac_proxy)
    app.router.add_route("*", "/ac", ac_proxy)
    app.router.add_route("*", "/ts/{tail:.*}", ts_proxy)
    app.router.add_post("/api/handoff/spawn", handoff_spawn)
    app.router.add_get("/api/handoff/spawn", handoff_spawn)        # capability probe
    app.router.add_get("/api/toolserver/config", api_toolserver_config_get)   # 1.0.63
    app.router.add_post("/api/toolserver/config", api_toolserver_config_post)
    app.router.add_get("/api/sessions/pulled", api_sessions_pulled)
    app.router.add_get("/api/term/backends", term_backends)
    app.router.add_get("/api/term/frontier", term_frontier)
    app.router.add_post("/api/term/frontier", term_frontier)
    app.router.add_get("/api/term/bgate", term_bgate)
    app.router.add_post("/api/term/bgate", term_bgate)
    app.router.add_post("/api/term/unstick", term_unstick)
    app.router.add_post("/api/term/paste", term_paste)
    app.router.add_get("/api/browser-access", browser_access_get)
    app.router.add_post("/api/browser-access", browser_access_post)
    app.router.add_get("/api/a/template", a_template_get)
    app.router.add_post("/api/a/template", a_template_post)
    app.router.add_post("/api/a/clear", a_clear)
    app.router.add_get("/api/b/guidance", b_guidance_get)
    app.router.add_post("/api/b/guidance", b_guidance_post)
    app.router.add_post("/api/b/chat", b_chat)
    app.router.add_post("/api/board/operator/{id}/draft-run", api_board_operator_draft_run)   # 1.0.144, unused by the UI
    app.router.add_get("/api/b/findings", api_b_findings)                # B's findings set (1.0.124)
    app.router.add_get("/api/mct/todo", mct_todo_get)
    app.router.add_post("/api/mct/prompt", mct_prompt)
    app.router.add_post("/api/prompt/send", api_prompt_send)          # 1.0.143 ✍ direct delivery
    app.router.add_get("/api/prompt/status", api_prompt_status)
    app.router.add_post("/api/prompt/status", api_prompt_status)      # {session_id, action:"release"} (1.0.146; "retry" accepted)
    app.router.add_get("/api/prompt/pending", api_prompt_pending)     # 1.0.146: re-pended ✍ prompts of a locus
    app.router.add_post("/api/prompt/pending", api_prompt_pending)    # {action:"retry"} = one delivery pass now
    app.on_startup.append(_start_prompt_pending)
    app.on_cleanup.append(_stop_prompt_pending)
    app.router.add_post("/api/locus/serve/standing", api_locus_serve_standing)  # 1.0.146 (t4250): standing roles + LOCUS NUANCES
    app.router.add_post("/api/mct/prompt/revise", mct_prompt_revise)
    app.router.add_get("/api/mct/live", mct_live)
    app.router.add_get("/api/mct/live/object", mct_live_object)   # mct v2: render a ledger object by pointer
    app.router.add_post("/api/mct/todo", mct_todo_post)
    app.router.add_get("/api/mct/todo/history", mct_todo_history)
    app.router.add_post("/api/mct/todo/assist", mct_todo_assist)
    app.router.add_post("/api/mct/todo/ping", mct_todo_ping)           # board t2: per-item keeper ping
    app.router.add_get("/api/frontier/delegate", api_frontier_delegate)    # board t7: delegate-only switch
    app.router.add_post("/api/frontier/delegate", api_frontier_delegate)
    app.router.add_post("/api/findings/{sig}/disposition", api_findings_disposition)  # 1.0.124
    app.router.add_get("/api/findings/notify", api_findings_notify)                     # 1.0.124
    app.router.add_get("/api/loops", api_loops)                        # ⚠ crash/retry loop set (1.0.118)
    app.router.add_get("/api/steward/log", api_steward_log)            # board t1: console log backlog
    app.router.add_get("/api/steward/log/stream", api_steward_log_stream)  # board t1: live SSE log
    app.router.add_get("/api/provenance", api_provenance)
    app.router.add_get("/api/provenance/stream", api_provenance_stream)
    app.router.add_post("/api/provenance/query", api_provenance_query)
    app.router.add_post("/api/provenance/ingest", api_provenance_ingest)
    app.router.add_post("/api/provenance/investigate", api_provenance_investigate)
    app.router.add_get("/api/locus/journal", api_locus_journal)             # 📜 resolved journal source
    app.router.add_get("/api/locus/journal/stream", api_locus_journal_stream)  # 📜 journalctl -f (SSE)
    app.router.add_get("/api/bugscan", api_bugscan)                  # 🐞 local-agent log review
    app.router.add_post("/api/bugscan", api_bugscan)
    app.router.add_post("/api/mct/finder", mct_finder)
    app.router.add_get("/api/mct/vm", mct_vm_get)
    app.router.add_post("/api/mct/vm", mct_vm_post)
    app.router.add_get("/api/mct/sessions", mct_sessions_get)
    app.router.add_post("/api/mct/sessions", mct_sessions_post)
    app.router.add_get("/api/a/usage", a_usage)
    app.router.add_get("/api/agent-sudo/status", api_agent_sudo_status)
    app.router.add_post("/api/agent-sudo/toggle", api_agent_sudo_toggle)
    # 1.0.41 explicit seat control (see the block above make_app)
    app.router.add_get("/api/mct2/status", api_mct2_status)
    app.router.add_get("/api/mct2/exchange", api_mct2_exchange_file)
    app.router.add_get("/api/seat", api_seat_status)
    app.router.add_post("/api/seat/wipe", api_seat_wipe)
    app.router.add_get("/api/firstrun", api_firstrun)
    app.router.add_post("/api/firstrun/provision", api_firstrun_provision)
    app.router.add_get("/api/frontier/directive", api_frontier_directive)
    app.router.add_post("/api/frontier/directive", api_frontier_directive)
    app.router.add_get("/api/frontier/handoff", api_frontier_handoff)
    app.router.add_post("/api/frontier/handoff", api_frontier_handoff)
    app.router.add_get("/api/frontier/models", api_frontier_models)
    app.router.add_post("/api/frontier/models", api_frontier_models)
    app.router.add_get("/api/gpt", api_gpt)
    app.router.add_post("/api/gpt", api_gpt)
    app.router.add_get("/api/frontier/cache", api_frontier_cache)       # 1.0.67 prompt-cache countdown; ?backend= per seat (steward tabs)
    app.router.add_get("/api/frontier/limits", api_frontier_limits)     # p515: frontier quota/limit state for the seat-card LimitsBadge
    app.router.add_get("/api/frontier/agents", api_frontier_agents)     # steward tabs: the seat's live subagents
    app.router.add_get("/api/frontier/state", api_frontier_state)       # 1.0.68 rolling state (toolserver assess/state)
    app.router.add_post("/api/frontier/relaunch", api_frontier_relaunch)  # 1.0.68 fresh seat on the init prompt
    app.router.add_get("/api/ac/rollover", api_ac_rollover)            # p509 serve session-rollover status
    app.router.add_get("/api/locus/serve", api_locus_serve)             # 1.0.144 a locus's serve + its tmux seats
    app.router.add_get("/api/locus/keeper", api_locus_keeper)           # t4262 the locus's serve Keeper role + catalog
    app.router.add_post("/api/locus/keeper", api_locus_keeper)          # t4262 set_model (+ set_provider) on that serve
    app.router.add_post("/api/locus/serve/provision", api_locus_serve_provision)  # 1.0.144 provision serve ON the locus
    app.router.add_post("/api/ac/rollover", api_ac_rollover)           # p509 roll/cancel forwarding
    app.router.add_get("/api/b/model", api_b_model)
    app.router.add_post("/api/b/model", api_b_model)
    app.router.add_get("/api/claude/auth", api_claude_auth)
    app.router.add_get("/api/sudo/status", api_sudo_status)
    app.router.add_post("/api/sudo/toggle", api_sudo_toggle)
    app.router.add_get("/api/stations/{name}/console-status", api_vm_console_status)
    app.router.add_post("/api/stations/{name}/console-install", api_vm_console_install)
    app.router.add_get("/api/frontier/fs/status", api_frontier_fs_status)
    app.router.add_post("/api/frontier/fs/toggle", api_frontier_fs_toggle)
    if not (STATIC / "mct-renderer.js").is_file():   # packaged static dir predates mct v2
        app.router.add_get("/static/mct-renderer.js", _mct_static_fallback)
    app.router.add_static("/static/", STATIC, show_index=False)
    # 📖 the shipped guides: the docs drawer fetches "/docs/<name>.md" (not
    # under /static/), which had no route here — the docs existed on disk but
    # were unretrievable. The same files are also readable by A/B through the
    # granted console root (they live under the backend's static/docs).
    app.router.add_static("/docs/", STATIC / "docs", show_index=False)
    return app


def _set_password_cli():
    pw = getpass.getpass("New console password: ")
    if not pw:
        print("empty password — aborted")
        return 1
    if pw != getpass.getpass("Confirm password: "):
        print("passwords did not match")
        return 1
    # Same dual path as the UI-set password: ROOT/.auth when writable (root /
    # standalone installs), else the user config dir — ROOT is root-owned once
    # installed, and 1.0.41 crashed here with PermissionError on Fedora.
    target = AUTH_FILE
    try:
        AUTH_FILE.write_text(hash_password(pw) + "\n")
    except OSError as exc:
        target = USER_AUTH_FILE
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
        try:
            target.write_text(hash_password(pw) + "\n")
        except OSError as exc2:
            print(f"could not write {AUTH_FILE} ({exc}) nor {target} ({exc2})")
            return 1
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    print(f"saved password hash to {target}  (restart the console to apply)")
    return 0


if __name__ == "__main__":
    if "--set-password" in sys.argv:
        sys.exit(_set_password_cli())

    ssl_ctx = None
    if USE_TLS:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(CERT, KEY)

    # 🌐 browser access: the operator's toggle widens a loopback bind to the
    # LAN — but ONLY with auth configured (the fail-closed guard below still
    # has the last word). The Electron shell always passes HOST=127.0.0.1;
    # this is where "serve on the LAN" actually happens.
    if _browser_access_enabled() and HOST in ("127.0.0.1", "::1", "localhost"):
        if AUTH_REQUIRED:
            HOST = "0.0.0.0"
            print("browser access: ON — serving the LAN (login required)")
        else:
            print("browser access: configured ON but no password is set — "
                  "staying loopback-only")

    # FAIL CLOSED, proxied flavor: a loopback bind fronted by a public proxy
    # (hugpy-station-web@ + nginx) looks local but reaches the world — the
    # unit sets REQUIRE_AUTH so an open console can never be published.
    if (os.environ.get("STATION_CONSOLE_REQUIRE_AUTH") == "1"
            and not AUTH_REQUIRED):
        print("REFUSING TO START: STATION_CONSOLE_REQUIRE_AUTH=1 but no "
              "password is set.\n  Set one in the console's 🌐 browser access "
              "panel (or: python3 server.py --set-password).", file=sys.stderr)
        sys.exit(1)

    # FAIL CLOSED: a provisioning-capable, host-root console must never run open
    # on a non-loopback address. (Loopback-only stays allowed for local dev.)
    if not AUTH_REQUIRED and HOST not in ("127.0.0.1", "::1", "localhost"):
        print(f"REFUSING TO START: no password set and bound to {HOST}.\n"
              "  Set one:  python3 server.py --set-password\n"
              "  (or bind HOST=127.0.0.1 for local-only use).", file=sys.stderr)
        sys.exit(1)

    _write_state_pointer()      # 1.0.141: peers' ssh scripts find this state dir
    scheme = "https" if USE_TLS else "http"
    mode = "session+token" if (AUTH_REQUIRED and TOKEN) else \
           "session" if AUTH_REQUIRED else "OPEN (loopback only — no auth)"
    print(f"station-console on {scheme}://{HOST}:{PORT}  (auth: {mode})")
    if not AUTH_REQUIRED:
        print("  !! no password set — OPEN on loopback only. Run --set-password.")
    # board t9 (operator 2026-09-10): a stop used to take the full 60 s aiohttp
    # shutdown grace because long-lived browser connections (the /api/events
    # SSE stream, terminal websockets, the 📜 log stream) never close on their
    # own — then systemd's 90 s stop timeout on top. 3 s is plenty: every
    # stream here is a hint the browser re-opens by itself.
    web.run_app(make_app(), host=HOST, port=PORT, ssl_context=ssl_ctx, print=None,
                shutdown_timeout=3.0)
