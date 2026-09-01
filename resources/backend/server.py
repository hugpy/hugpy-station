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


def audit(request, action, detail="", ok=True):
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
async def _run(*args):
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


def _project(inst):
    dev = (inst.get("devices") or {}).get("project") or {}
    src = dev.get("source", "")
    return os.path.basename(src) if src else ""


def _bridge_open(name):
    return (BRIDGE_STATE / f"{name}.gate").exists()


async def discover():
    # Workbench mode (the console installed INSIDE a station, no LXD of its
    # own): no stations is a normal answer, not a crash — the terminal
    # surfaces, board, feed, and B chat are the point of such an install.
    try:
        rc, out, err = await _run("lxc", "list", "--format", "json")
    except OSError:
        return []
    if rc != 0:
        raise RuntimeError(f"lxc list failed: {err.strip()}")
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
            "project": _project(inst) or ("(base image)" if is_base else ""),
            "base": is_base,
            "bridge": _bridge_open(name),
        })
    stations.sort(key=lambda s: (s["base"], s["name"]))
    return stations


async def known_names():
    return {s["name"] for s in await discover()}


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
    rc, out, err = await _run(str(VM_BIN / "keeper-backup"), "status")
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
    return web.json_response({"current": cur, "directives": by_name})


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


async def _exec_in_station(name, command):
    rc, out, err = await _lxc(name, "bash", "-lc", command)
    output = (out or "")
    if err and err.strip():
        output += "\n[stderr]\n" + err
    output = output.strip()
    if len(output) > AGENT_OBS_LIMIT:
        output = output[:AGENT_OBS_LIMIT] + f"\n…[truncated, {len(output)} chars total]"
    return rc, output or "(no output)"


async def _sse(resp, obj):
    await resp.write(("data: " + json.dumps(obj) + "\n\n").encode())


async def _agent_stream_step(session, url, payload, headers, resp):
    """One model turn: stream tokens to the browser, return the full text."""
    text = ""
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
    return text


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
                text = await _agent_stream_step(s, url, payload, headers, resp)
                convo.append({"role": "assistant", "content": text})
                action = _parse_action(text)
                if not action:
                    stopped = False
                    break
                await _sse(resp, {"type": "action", "command": action})
                rc, output = await _exec_in_station(station, action)
                await _sse(resp, {"type": "observation", "rc": rc, "output": output})
                convo.append({"role": "user", "content": f"Observation (exit {rc}):\n{output}"})
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
A_SETTINGS_TEMPLATE_PATH = (Path(os.environ["A_SETTINGS_TEMPLATE"])
                            if os.environ.get("A_SETTINGS_TEMPLATE")
                            else FV_STATE_HOME / "a-settings-template.json")
A_SETTINGS_SHIPPED = ROOT / "a-settings-template.json"


def _a_template_doc():
    """(doc, source, path) — the user template, else the shipped file, else
    the built-in defaults."""
    for path, src in ((A_SETTINGS_TEMPLATE_PATH, "user"),
                      (A_SETTINGS_SHIPPED, "shipped")):
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


def _frontier_enabled() -> bool:
    """The Frontier Keeper: Enabled/Disabled toggle (default enabled). Gates
    only A-surface launches; Local Keeper and Shell are never gated."""
    try:
        doc = json.loads((FV_STATE_HOME / "frontier-keeper.json").read_text())
        return bool(doc.get("enabled", True))
    except (OSError, ValueError):
        return True


async def term_frontier(request):
    """GET/POST /api/term/frontier — flip or read the Frontier Keeper toggle.
    Disabling does not stop a running frontier session (keeper-gate semantics);
    it only prevents starting a new one."""
    if request.method == "POST":
        try:
            body = await request.json()
            enabled = bool(body["enabled"])
        except Exception:
            return web.json_response(
                {"error": 'body must be {"enabled": <bool>}'}, status=400)
        FV_STATE_HOME.mkdir(parents=True, exist_ok=True)
        (FV_STATE_HOME / "frontier-keeper.json").write_text(
            json.dumps({"enabled": enabled}) + "\n")
        return web.json_response({"ok": True, "enabled": enabled})
    return web.json_response({"enabled": _frontier_enabled()})


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


async def a_usage(request):
    """GET /api/a/usage — precise MCT token/cost accounting + cache-shadow
    timing, via `hugpy-agent mct-usage` (source of truth: A's own stored
    transcripts). 502s cleanly when hugpy-agent is unavailable. With an
    active VM the readout comes from THAT VM's workbench (grounding rule)."""
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
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
    if not up.exists() and cp.exists():
        try:
            up.write_text(cp.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    return up


def _compose_guidance(user_text):
    """What B injects as the governing_instruction: the frontier directive,
    then the operator's guidance. The steward tab shows exactly this."""
    d = _frontier_directive_text()[0].rstrip()
    u = (user_text or "").strip()
    return d + ("\n\n# Operator guidance\n" + u if u else "") + "\n"


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
    d = _frontier_directive_text()[0]
    ws = f"$HOME/.mct/{session}"
    script = (f'mkdir -p "$HOME/.config/hugpy-station" {ws}; '
              f'cat > "$HOME/.config/hugpy-station/frontier-directive.md" <<\'__HSDIR__\'\n{d}\n__HSDIR__\n'
              f'u={ws}/operator-guidance.user.md; c={ws}/operator-guidance.md; '
              f'[ -f "$u" ] || {{ [ -f "$c" ] && cp "$c" "$u" || : ; }}; '
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
    d = _frontier_directive_text()[0]
    ws = "$HOME/.config/hugpy-station/mct2/repl"
    script = ('command -v abstract-claude >/dev/null 2>&1 || '
              'pip install --user -q -U abstract-claude >/dev/null 2>&1 || '
              'pipx install abstract-claude >/dev/null 2>&1 || true; '
              f'mkdir -p {ws}; '
              f'cat > "$HOME/.config/hugpy-station/frontier-directive.md" <<\'__HSDIR__\'\n{d}\n__HSDIR__\n'
              f'u={ws}/operator-guidance.user.md; c={ws}/operator-guidance.md; '
              f'[ -f "$u" ] || {{ [ -f "$c" ] && cp "$c" "$u" || : ; }}; '
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
    d = _frontier_directive_text()[0]
    ws = f"{VM_MCT_ROOT}/{session}"
    script = (f'mkdir -p "$HOME/.config/hugpy-station" {shlex.quote(ws)}; '
              f'cat > "$HOME/.config/hugpy-station/frontier-directive.md"; '
              f'u={shlex.quote(ws)}/operator-guidance.user.md; c={shlex.quote(ws)}/operator-guidance.md; '
              f'[ -f "$u" ] || {{ [ -f "$c" ] && cp "$c" "$u" || : ; }}; '
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
        did += " — composed with the frontier directive into operator-guidance.md"
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
VMCONSOLE_TOKEN_DIR = Path(os.environ.get("VMCONSOLE_TOKEN_DIR") or (
    "/srv/vm_mgr/secrets" if os.path.isdir("/srv/vm_mgr/secrets") else str(Path(
        os.environ.get("HUGPY_STATION_STATE") or os.path.join(
            os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"),
            "hugpy-station")) / "vmconsole")))
_WB_SESS_SYNCED = {}   # vm -> session name last selected on its workbench


async def _sidecar_vm(request):
    """The locus a sidecar request grounds in: ?vm= (the SPA's active locus)
    when it names a real station OR an ssh host (both run their own workbench
    console — install-vm-console / install-ssh-console), else the static
    model-VM pin. '' = host-native."""
    if request.path.startswith("/api/vm/keeper/"):
        return ""              # the host keeper's own board/aliases stay host
    vm = (request.query.get("vm") or "").strip()
    if vm == "@keeper":
        return ""
    if vm and (vm in await known_names() or _ssh_host(vm)):
        return vm
    return MODEL_VM


def _wb_token(vm):
    try:
        return (VMCONSOLE_TOKEN_DIR / f"vmconsole-{vm}.token").read_text(
            encoding="utf-8").strip()
    except OSError:
        return ""


async def _wb_request(request, vm, body=None, path=None):
    """Forward this request to the VM's own workbench station. Returns the
    proxied web.Response, or None when the workbench is unreachable."""
    tok = _wb_token(vm)
    sess = request.app.get("proxy_sess")
    if not tok or sess is None:
        return None
    h = _ssh_host(vm)          # ssh locus: its workbench serves on its address
    ip = h["host"] if h else await _vm_ip(vm)
    if not ip:
        return None
    base = f"http://{ip}:{os.environ.get('VMCONSOLE_PORT', '8800')}"
    hdrs = {"X-Console-Token": tok, "Content-Type": "application/json"}
    to = aiohttp.ClientTimeout(total=60)
    try:
        want = _active_session()
        if _WB_SESS_SYNCED.get(vm) != want:
            async with sess.post(base + "/api/mct/sessions", headers=hdrs,
                                 json={"op": "select", "name": want},
                                 timeout=to) as r0:
                await r0.read()
                if r0.status == 200:
                    _WB_SESS_SYNCED[vm] = want
        data = body if body is not None else (await request.read() or None)
        async with sess.request(request.method, base + (path or request.path),
                                headers=hdrs, data=data, timeout=to,
                                allow_redirects=False) as r:
            payload = await r.read()
            ct = (r.headers.get("Content-Type") or "application/json")
            return web.Response(status=r.status, body=payload,
                                content_type=ct.split(";")[0].strip())
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return None


async def _sidecar_proxy(request):
    """None = stay host-native; else the workbench's response (or the 502
    install hint when the VM's workbench cannot be reached)."""
    vm = await _sidecar_vm(request)
    if not vm:
        return None
    resp = await _wb_request(request, vm)
    if resp is not None:
        return resp
    return web.json_response(
        {"ok": False, "error": f"{vm}: workbench console unreachable — "
                               f"install/start it: install-vm-console {vm}"},
        status=502)


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


def _b_answer(text, history, vm=""):
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
        payload = json.dumps({"workspace": str(_active_ws()), "text": text,
                              "history": (history or [])[-20:]})
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
    srv = _console_b_server()
    sess = srv.session(_latest_session_id(srv))
    ground = _b_state_text(sess, srv, {"last": None})
    msgs = [{"role": "system", "content": _B_SYSMSG + ground}]
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
        return {"reply": (reply or "").strip() or "(no reply)", "offline": False}
    except Exception as exc:
        return {"reply": "[B offline - deterministic state readout]\n"
                         f"(gateway unavailable: {type(exc).__name__}: {exc})\n\n" + ground,
                "offline": True}


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
    vm = str(body.get("vm") or "").strip()
    if vm != "@keeper" and (not vm or vm not in await known_names()):
        vm = ""                   # unknown name → static pin / host fallback
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
MCT_TODO_TYPES = ("todo", "request", "bookmark", "operator", "proposal")
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


def todo_next_id(items):
    n = 0
    for it in items:
        m = re.match(r"^t(\d+)$", str(it.get("id", "")))
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
    return {"id": f"t{nid}",
            "type": typ if typ in TODO_TYPES else "todo",
            "text": text,
            "note": str(raw.get("note", "") or "").strip()[:2000],
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
            it["note"] = str(f["note"] or "").strip()[:2000]
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
                ctext = str(c.get("text", "")).strip()[:2000]
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
                it["id"] = f"t{nid}"
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


# --- Host-native finder (POST /api/mct/finder). Searches THIS host directly — no
# VM, no lxc, no per-VM vernacular. grep -> abstract_search.findContent (exact
# {file_path, lines:[{line,content}]} shape); collect/dirs/view -> stdlib. Roots
# are absolute host paths from the body. --------------------------------------
def _finder_roots(body):
    roots = body.get("roots")
    if not isinstance(roots, list) or not (1 <= len(roots) <= 8):
        return None
    out = []
    for r in roots:
        if not isinstance(r, str) or "\x00" in r or not r.startswith("/"):
            return None
        out.append(os.path.abspath(r))
    return out


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
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)
    op = (body.get("op") or "").strip()
    roots = _finder_roots(body)
    if roots is None:
        return web.json_response({"ok": False, "error": "roots must be 1-8 absolute host paths"}, status=400)

    def _work():
        if op == "grep":
            from abstract_search.find_content import findContent
            strings = _fv_list(body, "string")
            if not strings:
                return {"ok": False, "error": "grep needs at least one string"}
            results = findContent(*roots, strings=strings,
                                  total_strings=not body.get("any"),
                                  get_lines=True, **_finder_filters(body))
            norm = [r for r in results if isinstance(r, dict) and r.get("lines")]
            return {"ok": True, "result": {"results": norm}}
        if op in ("collect", "dirs"):
            import fnmatch
            recursive = not body.get("no_recursive")
            maxdepth = _fv_int(body, "maxdepth")
            allowed_ext = {e.lstrip(".").lower() for e in _fv_list(body, "ext")}
            exclude_ext = {e.lstrip(".").lower() for e in _fv_list(body, "exclude_ext")}
            exclude_dir = {d.lower() for d in _fv_list(body, "exclude_dir")}
            pats = [p.lower() for p in _fv_list(body, "pattern")]
            xpats = [p.lower() for p in _fv_list(body, "exclude_pattern")]
            hidden = bool(body.get("no_add"))
            ftype = (_fv_list(body, "type") or [None])[0]
            minsz, maxsz = _fv_int(body, "min_size"), _fv_int(body, "max_size")
            after, before = _fv_date(body, "after"), _fv_date(body, "before")
            limit = _fv_int(body, "limit")
            files, dirs = [], []
            for base in roots:
                base_depth = base.rstrip("/").count("/")
                for dp, dns, fns in os.walk(base):
                    depth = dp.rstrip("/").count("/") - base_depth
                    dns[:] = [d for d in dns if d.lower() not in exclude_dir
                              and (hidden or not d.startswith("."))]
                    if op == "dirs":
                        dirs += [os.path.join(dp, d) for d in dns]
                        dns[:] = []          # depth-1 for the browse picker
                        continue
                    if not recursive and depth >= 1:
                        dns[:] = []
                    if maxdepth is not None and depth >= maxdepth:
                        dns[:] = []
                    if ftype == "d":
                        continue
                    for fn in fns:
                        if not hidden and fn.startswith("."):
                            continue
                        if allowed_ext and fn.rsplit(".", 1)[-1].lower() not in allowed_ext:
                            continue
                        if exclude_ext and fn.rsplit(".", 1)[-1].lower() in exclude_ext:
                            continue
                        low = fn.lower()
                        if pats and not any(fnmatch.fnmatch(low, p) for p in pats):
                            continue
                        if xpats and any(fnmatch.fnmatch(low, p) for p in xpats):
                            continue
                        fp = os.path.join(dp, fn)
                        try:
                            st = os.stat(fp)
                        except OSError:
                            continue
                        if minsz is not None and st.st_size < minsz:  continue
                        if maxsz is not None and st.st_size > maxsz:  continue
                        if after is not None and st.st_mtime < after:  continue
                        if before is not None and st.st_mtime > before: continue
                        files.append(fp)
            if op == "dirs":
                return {"ok": True, "result": {"dirs": sorted(set(dirs))}}
            sort = (_fv_list(body, "sort") or ["name"])[0]
            key = {"size": lambda p: _safe_stat(p, "st_size"),
                   "mtime": lambda p: _safe_stat(p, "st_mtime")}.get(sort)
            files = sorted(set(files), key=key) if key else sorted(set(files))
            if body.get("reverse"):
                files = files[::-1]
            if limit:
                files = files[:limit]
            return {"ok": True, "result": {"files": files}}
        if op == "view":
            path = roots[0]
            spec = (_fv_list(body, "lines") or [""])[0]
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    all_lines = fh.read().split("\n")
            except OSError as e:
                return {"ok": False, "error": str(e)}
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
            return {"ok": True, "result": {"spans": spans}}
        return {"ok": False, "error": "op must be grep|collect|dirs|view"}

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _work)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


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
    try:
        rc, out, err = await _run("lxc", "list", "--format", "csv", "-c", "ns")
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=502)
    if rc != 0:
        return web.json_response({"ok": False, "error": (err or out).strip() or "lxc list failed"},
                                 status=502)
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
    return web.json_response({"ok": True, "vms": vms, "vm_new": bool(VM_NEW_BIN and os.path.exists(VM_NEW_BIN))})


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
    base = FV_STATE_HOME / "mct2" / "repl"
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


async def mct_prompt(request):
    """POST /api/mct/prompt {text, files:[{name,type,dataUrl}]} - save a composed
    prompt + attached media into the active session workspace's prompt-inbox, for the
    agent (B/A) to pick up. Returns the saved dir. With an active VM the
    prompt is SAVED by that VM's workbench (files land in the VM workspace —
    the only place its A can read them), then the pointer line is typed into
    the HARNESS's frontier PTY for that VM, which is the live seat here."""
    vm = await _sidecar_vm(request)
    if vm:
        resp = await _wb_request(request, vm)
        if resp is None:
            return web.json_response(
                {"ok": False, "error": f"{vm}: workbench console unreachable — "
                                       f"install/start it: install-vm-console {vm}"},
                status=502)
        try:
            doc = json.loads(resp.body)
        except Exception:
            doc = None
        if (resp.status == 200 and isinstance(doc, dict) and doc.get("path")
                and not doc.get("submitted")):
            key = "frontier:" + _active_session() + "@" + vm
            s2 = SESSIONS.get(FV_SESSIONS.get(key) or "")
            if s2 is not None and getattr(s2, "alive", False):
                pointer = ("Handle the operator prompt at " + str(doc["path"])
                           + "/prompt.md — read it via a pull, then respond.")
                try:
                    s2.write((pointer + "\r").encode("utf-8"))
                    doc["submitted"] = True
                    doc["note"] = "sent → frontier (A) @" + vm
                except Exception:
                    pass
            return web.json_response(doc, status=resp.status)
        return resp
    import base64, re as _re
    try:
        body = await request.json()
        text = (body.get("text") or "").strip()
        files = body.get("files") or []
    except Exception:
        return web.json_response({"error": "bad body"}, status=400)
    if not text and not files:
        return web.json_response({"error": "empty prompt"}, status=400)
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
    # Actually reach the agent: type a ONE-LINE pointer into the running frontier
    # terminal (the MCT REPL), so B points A at the saved prompt/media — file-pointing
    # keeps A's context lean. Falls back to "saved only" if no frontier is running.
    submitted = False
    try:
        sess = _frontier_live_session()
        if sess is not None:
            pointer = ("Handle the operator prompt at " + str(dst / "prompt.md")
                       + (" (with " + str(len(saved)) + " attached file(s) in " + str(dst)
                          + ": " + ", ".join(saved) + ")" if saved else "")
                       + " — read it via a pull, then respond.")
            sess.write((pointer + "\r").encode("utf-8"))
            submitted = True
    except Exception:
        submitted = False
    note = (("sent \u2192 frontier (A)" if submitted else "saved \u2192 prompt-inbox/" + ts + " (no frontier running)")
            + (" \u00b7 " + str(len(saved)) + " file(s)" if saved else ""))
    return web.json_response({"ok": True, "note": note, "submitted": submitted, "path": str(dst)})


async def mct_todo_get(request):
    """GET /api/mct/todo - the current session's todo.v1 board. With an
    active VM the board is THAT VM's own (via its workbench); the
    /api/vm/keeper/todo alias always stays the host keeper's board."""
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
    state, err = _mct_todo_read()
    if state is None:
        return web.json_response({"ok": False, "error": err}, status=502)
    return web.json_response({"ok": True, "state": state, "path": str(_mct_todo_path())})


async def mct_todo_post(request):
    """POST /api/mct/todo (and /api/vm/keeper/todo) - the canonical vm_mgr todo
    op set, verbatim: add{item:{...}} | update{id, fields:{...}} | del{id} |
    replace{items:[...]}. The UI's legacy verbs (add{text,...}, set, edit,
    remove, resolve, comment) are translated onto those ops so both dialects
    run through ONE engine (todo_apply). fresh read -> apply -> write is
    serialized under the board's flock (o10 v4); a decision rides in on note +
    status (the canonical proposal contract). Mutations journal CANONICAL
    events ({event, id, type, detail}) that the drawer's history tab renders
    natively (added/status/text/note/comment/removed)."""
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
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
    return web.json_response({"ok": True, "state": state,
                              "path": str(_mct_todo_path())})


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


def _todo_assist_call(mode, text):
    from hugpy_agent.config import load_config
    from hugpy_agent.gateway import Gateway
    sysmsg = _TODO_ASSIST_PROMPTS.get(mode, _TODO_ASSIST_PROMPTS["reword"])
    gw = Gateway.from_config(load_config(overrides=_b_overrides()))
    res = gw.chat([{"role": "system", "content": sysmsg},
                   {"role": "user", "content": text}], max_tokens=500)
    return (getattr(res, "text", None) or getattr(res, "content", None) or str(res) or "").strip()


async def mct_todo_assist(request):
    """POST /api/mct/todo/assist {mode, text} - LLM reword/split/tidy via the hugpy
    gateway. Returns {result} (a preview the operator applies); never mutates the board."""
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
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
    from hugpy_agent.config import load_config
    from hugpy_agent.gateway import Gateway
    try:
        glossary = (STATIC / "docs" / "NOMENCLATURE.md").read_text(encoding="utf-8")
    except OSError:
        glossary = "(glossary missing)"
    gw = Gateway.from_config(load_config(overrides=_b_overrides()))
    res = gw.chat([{"role": "system", "content": _PROMPT_REVISE_SYS + glossary},
                   {"role": "user", "content": text}], max_tokens=1200)
    raw = (getattr(res, "text", None) or getattr(res, "content", None) or str(res) or "").strip()
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
LOCAL_KEEPER_AGENTS = """\
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

## Design & flow
- Design (wireframe) and flow (flow.v1) documents are exchanged through the
  console's ⬚ design and ⋔ flow drawers; pasted blocks land in keeper
  terminals. Formats and etiquette live in ./docs/UI-GUIDE.md.

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


def _local_keeper_ws() -> "Path | None":
    """Ensure FV_STATE_HOME/local-keeper exists with AGENTS.md + ./docs."""
    lk = FV_STATE_HOME / "local-keeper"
    try:
        lk.mkdir(parents=True, exist_ok=True)
        agents = lk / "AGENTS.md"
        if not agents.exists():
            agents.write_text(LOCAL_KEEPER_AGENTS)
        docs = lk / "docs"
        want = ROOT / "static" / "docs"
        if docs.is_symlink() and docs.resolve() != want.resolve():
            docs.unlink()          # stale link (e.g. pre-upgrade install path)
        if not docs.exists():
            docs.symlink_to(want)
        return lk
    except OSError:
        return None


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


async def fs_list(request):
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
    name = await _require_station(request)
    body = await request.json()
    path = _safe_path(body.get("path", ""))
    content = body.get("content", "")
    rc, out = await _push_bytes(name, path, content.encode("utf-8"))
    if rc != 0:
        return web.json_response({"error": out.strip() or "write failed"}, status=400)
    return web.json_response({"ok": True, "path": path})


async def fs_mkdir(request):
    name = await _require_station(request)
    path = _safe_path((await request.json()).get("path", ""))
    uid, gid = _fs_owner(name)
    rc, out, err = await _run("lxc", "exec", name, "--user", uid, "--group", gid,
                              "--", "mkdir", "-p", path)
    if rc != 0:
        return web.json_response({"error": err.strip() or "mkdir failed"}, status=400)
    return web.json_response({"ok": True, "path": path})


async def fs_rename(request):
    name = await _require_station(request)
    body = await request.json()
    src, dst = _safe_path(body.get("src", "")), _safe_path(body.get("dst", ""))
    rc, out, err = await _lxc(name, "mv", "-n", src, dst)
    if rc != 0:
        return web.json_response({"error": err.strip() or "rename failed"}, status=400)
    return web.json_response({"ok": True, "src": src, "dst": dst})


async def fs_delete(request):
    name = await _require_station(request)
    path = _safe_path((await request.json()).get("path", ""))
    if path in ("/", SH_HOME):
        return web.json_response({"error": "refusing to delete that path"}, status=400)
    rc, out, err = await _lxc(name, "rm", "-rf", path)
    if rc != 0:
        return web.json_response({"error": err.strip() or "delete failed"}, status=400)
    return web.json_response({"ok": True, "path": path})


async def fs_download(request):
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


async def _ssh_vm_reroute(request):
    """ssh-host loci must never fall through to the lxc-only sidecars
    (console-api / bugreport-api answer "unknown vm" for them — operator hit
    this queueing a todo on hugpy, 2026-08-27): /api/vm/<ssh-host>/<tail> is
    rewritten to that locus's OWN workbench as /api/vm/keeper/<tail> — the
    same rewrite vm_keeper_alias does for stations. None = not an ssh host,
    caller proceeds to its sidecar."""
    m = re.match(r"^/api/vm/([^/]+)/(.+)$", request.path)
    if not m or not _ssh_host(m.group(1)):
        return None
    resp = await _wb_request(request, m.group(1),
                             path="/api/vm/keeper/" + m.group(2))
    if resp is not None:
        return resp
    return web.json_response(
        {"ok": False, "error": f"{m.group(1)}: workbench console unreachable — "
                               f"install/start it: install-ssh-console {m.group(1)}"},
        status=502)


async def api_proxy(request):
    """Forward a request to the console-api sidecar, verbatim (ssh-host loci
    reroute to their own workbench first)."""
    resp = await _ssh_vm_reroute(request)
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
    """Forward a request to the bugreport-api sidecar, verbatim (ssh-host
    loci reroute to their own workbench first)."""
    resp = await _ssh_vm_reroute(request)
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
_SEAT_TRUST_B64 = "aW1wb3J0IGpzb24sc3lzCnAsd3M9c3lzLmFyZ3ZbMV0sc3lzLmFyZ3ZbMl0KdHJ5OgogICAgZD1qc29uLmxvYWQob3BlbihwKSkKZXhjZXB0IEV4Y2VwdGlvbjoKICAgIGQ9e30KZC5zZXRkZWZhdWx0KCdoYXNDb21wbGV0ZWRPbmJvYXJkaW5nJyxUcnVlKQpkLnNldGRlZmF1bHQoJ3Byb2plY3RzJyx7fSkuc2V0ZGVmYXVsdCh3cyx7fSlbJ2hhc1RydXN0RGlhbG9nQWNjZXB0ZWQnXT1UcnVlCmpzb24uZHVtcChkLG9wZW4ocCwndycpKQo="


def _claude_seat_cmd(label: str) -> str:
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
    tmpl = shlex.quote(_claude_seat_settings_json())
    sysp = shlex.quote(_claude_seat_system_prompt())
    model = _frontier_models()["claude-code"]
    mflag = (" --model " + shlex.quote(model)) if model else ""
    fs = "MEDIATED (file tools denied; route via B)" if _frontier_fs_mediated() else "DIRECT"
    banner = (
        'echo "── hugpy-station claude seat ─────────────────────────────────"; '
        'echo "  seat      : ' + label + '"; '
        'echo "  user@host : $(id -un)@$(hostname)"; '
        'echo "  model     : ' + (model or "Claude Code default") + '"; '
        'echo "  fs switch : ' + fs + '"; '
        'echo "  directive : ' + str(len(_frontier_directive_text()[0])) + ' chars via --append-system-prompt (see 🛡 steward → frontier directive)"; '
        'echo "  config    : $c  (credentials + .claude.json symlinked from the seat user\'s own — login once)"; ')
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
    return ('c="$HOME/.claude-seat/' + label + '"; '
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
            f'printf %s {_SEAT_TRUST_B64} | base64 -d | python3 - "$j" "$(pwd)" >/dev/null 2>&1 || true; '
            f'printf %s {tmpl} > "$c/settings.json"; '
            'export PATH="$HOME/.local/bin:$PATH"; '
            + banner + noclaude + nologin +
            f'CLAUDE_CONFIG_DIR="$c" claude --dangerously-skip-permissions{mflag} '
            f'--append-system-prompt {sysp}')


# --- ⇅ SSH HOSTS (1.0.41b, operator): an EXISTING machine, added by ssh, is a
# station like any LXD VM — it sits in the dropdown, and every surface for it
# (shell, frontier mct/claude-code, local, seat probe, sudo) runs over ssh the
# way a VM's runs over `lxc exec`. Stored host-side, never in the browser. ----
SSH_HOSTS_PATH = FV_STATE_HOME / "ssh-hosts.json"

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
    if tok and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return bool(tok)


_export_fleet_oauth_token()
_SSH_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,253}$")
_SSH_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")


def _ssh_hosts():
    doc = _read_json(SSH_HOSTS_PATH, {}) or {}
    return {k: v for k, v in doc.items() if isinstance(v, dict) and _SSH_NAME_RE.match(k)}


def _ssh_host(name):
    return _ssh_hosts().get(name or "")


def _ssh_opts(h, interactive=False):
    key = h.get("key") or ""
    if not key:
        fk = FV_STATE_HOME / "fleet_ssh_key"
        key = str(fk) if fk.is_file() else ""
    o = ["-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR",
         "-o", "ConnectTimeout=8", "-p", str(int(h.get("port") or 22))]
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
    """Run a bash script in a locus as its seat user: '' host, VM (ubuntu), or ssh host."""
    if not vm:
        return await _run("bash", "-lc", script)
    if _ssh_host(vm):
        return await _ssh_run(vm, "bash -lc " + shlex.quote(script), timeout)
    return await _run("lxc", "exec", vm, "--", "sudo", "-u", "ubuntu", "-H", "bash", "-lc", script)


async def _locus_run_fast(vm, script, timeout=10):
    """_locus_run minus the host login shell: `bash -lc` sources the
    operator's full profile (~1.2s here) — fine for kill/wipe, hopeless for
    interactive paths like wheel scrolling. Host runs plain `bash -c`;
    VM/ssh grounding keeps the normal path (the transport dominates there)."""
    if not vm:
        return await _run("bash", "-c", script)
    return await _locus_run(vm, script, timeout)


async def _ssh_alive(h):
    """TCP probe only — cheap enough for the dropdown's 5s poll."""
    return await _tcp_open(h["host"], int(h.get("port") or 22), 1.5)


async def all_locus_names():
    """LXD stations + ssh hosts — what the dropdown may hold."""
    return set(await known_names()) | set(_ssh_hosts())


def _in_vm(cmd: str, vm: str) -> str:
    """Wrap a surface command to run inside a station as the dev user — or on
    an ssh host as its login user."""
    if not vm:            # workbench mode: the models ARE local to this host
        return cmd
    h = _ssh_host(vm)
    if h:
        return _ssh_shell_cmd(h) + " -- bash -lc " + shlex.quote(shlex.quote(cmd))
    return (f"lxc exec {shlex.quote(vm)} -- sudo -u ubuntu -H bash -lc "
            + shlex.quote(cmd))


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
        hosts = _ssh_hosts()
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
            if name in await known_names():
                return web.json_response({"error": f"'{name}' is an LXD station here — pick another name"}, status=409)
            hosts[name] = {"host": host, "user": user, "port": port,
                           "key": os.path.expanduser(key) if key else "", "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
            SSH_HOSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SSH_HOSTS_PATH.write_text(json.dumps(hosts, indent=2) + "\n")
            audit(request, "ssh-host-add", f"{name}={user}@{host}:{port}", ok=True)
            # Fleet Claude OAuth token (claude-oauth-sync): a new ssh host gets
            # it like a new VM does — best effort, never blocks the add.
            if CLAUDE_OAUTH_TOKEN_PATH.is_file() and CLAUDE_OAUTH_SYNC_BIN:
                asyncio.create_task(_run(CLAUDE_OAUTH_SYNC_BIN, "--ssh", name))
            # Install the seat CLIs on the new locus so both frontier backends
            # work (claude-code + mct); a hand-added ssh host is not provisioned
            # like a VM. Best effort, long timeout, never blocks the add.
            asyncio.create_task(_locus_run(name, _SEAT_CLI_PROVISION_SH, timeout=900))
        elif op == "delete":
            if hosts.pop(name, None) is None:
                return web.json_response({"error": "no such ssh host"}, status=404)
            SSH_HOSTS_PATH.write_text(json.dumps(hosts, indent=2) + "\n")
            audit(request, "ssh-host-delete", name, ok=True)
        elif op == "test":
            if name not in hosts:
                return web.json_response({"error": "no such ssh host"}, status=404)
            rc, out, err = await _locus_run(name, "echo user=$(id -un); echo host=$(hostname); "
                                                "command -v claude >/dev/null 2>&1 && echo claude=1 || echo claude=0; "
                                                "command -v hugpy-agent >/dev/null 2>&1 && echo agent=1 || echo agent=0; "
                                                "sudo -n true 2>/dev/null && echo sudo=nopasswd || echo sudo=password-or-none")
            return web.json_response({"ok": rc == 0, "name": name, "rc": rc, "out": out.strip(), "err": err.strip()[:400],
                                      "hint": "" if rc == 0 else "key auth failed or host unreachable — open its ⇥ terminal (password login works there), or add the key path"})
        else:
            return web.json_response({"error": "op must be add|delete|test"}, status=400)
    hosts = _ssh_hosts()
    rows = []
    for n, h in sorted(hosts.items()):
        rows.append({"name": n, "kind": "ssh", "host": h["host"], "user": h.get("user", "root"),
                     "port": h.get("port", 22), "key": h.get("key", ""), "added": h.get("added", ""),
                     "state": "RUNNING" if await _ssh_alive(h) else "UNREACHABLE",
                     "command": _ssh_shell_cmd(h)})
    return web.json_response({"ok": True, "hosts": rows, "path": str(SSH_HOSTS_PATH)})


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
                     "state": "RUNNING" if await _ssh_alive(h) else "UNREACHABLE"})
    return web.json_response({"vms": rows})


# Backend commands are stored RAW and wrapped per-request (_in_vm) with the
# grounding VM resolved from the SPA's active VM (else the static pin above).
TERM_SURFACES = {
    "local":    {"default": "opencode",
                 "backends": {"opencode":  "hugpy-agent console --frontend opencode",
                              "qwen-code": "hugpy-agent console --frontend qwen-code"}},
    # claude-code = the BASE frontier terminal (operator 2026-08-27): the
    # direct interactive seat is the default; mct is on course to become a
    # COMMUNICATION METHOD over it (file-pointer exchange into the claude-code
    # PTY — /api/mct/prompt already types pointer lines there) rather than a
    # separate program. The standalone arbiter below is the transitional form.
    "frontier": {"default": "claude-code",
                 # Two frontier backends, and only two (operator 2026-08-28):
                 # claude-code IS the frontier seat, and mct is the
                 # pointer-exchange method OVER it (PROMOTED from "mct2",
                 # 2026-08-27: plain-file C→B→A exchange). mct runs WITHIN the
                 # locus the seat is attributed to — the launch block pushes the
                 # script there and swaps the host path for the locus copy. Impl
                 # files/paths keep the mct2 name (mct2_repl.py,
                 # ~/.config/hugpy-station/mct2, /api/mct2/*); the BACKEND key is
                 # mct. The original broker MCT (mct-deprecated, `hugpy-agent
                 # mct`) and the clawd-code stub were retired from the picker.
                 "backends": {"mct":         "abstract-claude mct {ws}",
                              "claude-code": "claude"}},
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
    # mct promotion (2026-08-27): keeper-mct hosts the pointer-exchange arbiter
    # (mct2_repl). The in-VM keeper-ensure.sh still creates keeper-mct running
    # the OLD broker until station-stack is re-synced (station-stack-install) —
    # on such a VM, new-session -A would attach the wrong program; re-sync
    # before grounding the mct backend in an LXD guest.
    "frontier": {"mct": "keeper-mct", "claude-code": "keeper-claude"},
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
              "set -g destroy-unattached off \\; set -g window-size latest \\; "
              "setw -g aggressive-resize on \\; ")


def _tmux_persist(sess: str, cmd: str) -> str:
    """Shell command that attaches to tmux session `sess` (console socket),
    starting `cmd` in it only when the session does not exist yet."""
    return (f"tmux -L {KEEPER_TMUX_SOCK} " + _TMUX_OPTS
            + "new-session -A -s " + shlex.quote(sess) + " " + shlex.quote(cmd))


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


async def _vm_which(vm, names):
    """{name: bool} — which of these binaries exist in the VM for the dev user
    (PATH + ~/.local/bin, where vm-new seeds claude/hugpy-agent). One exec."""
    probe = "; ".join(
        f'if command -v {n} >/dev/null 2>&1 || [ -x "$HOME/.local/bin/{n}" ]; '
        f'then echo {n}=1; else echo {n}=0; fi' for n in names)
    rc, out, _err = await _locus_run(vm, probe, 20)
    if rc != 0:
        return {n: False for n in names}
    got = dict(line.split("=", 1) for line in out.split() if "=" in line)
    return {n: got.get(n) == "1" for n in names}


async def term_backends(request):
    """GET /api/term/backends[?vm=<name>] — the surface/backend whitelist plus
    whether each backend's binary is actually installed, so the UI can gray out
    the rest. With ?vm= (the SPA's active VM) availability is probed IN that VM
    — the grounding rule means that is where the backend would run. Also
    carries the Frontier Keeper toggle and which surfaces are live."""
    req_vm = (request.query.get("vm") or "").strip()
    # @keeper = the host seat: probe HOST availability (that is where its
    # backends actually run — the old fold to MODEL_VM probed the wrong box).
    if req_vm == "@keeper":
        ground_vm = ""
    else:
        ground_vm = (req_vm if (req_vm and req_vm in await all_locus_names())
                     else MODEL_VM)
    vm_avail = None
    if ground_vm:
        names = sorted({shlex.split(cmd)[0]
                        for s2, spec2 in TERM_SURFACES.items() if s2 != "shell"
                        for cmd in spec2["backends"].values()})
        vm_avail = await _vm_which(ground_vm, names)
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
            out[s]["backends"][key] = {"available": avail}
    out["frontier_enabled"] = _frontier_enabled()
    out["b_enabled"] = _b_enabled()
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
        surf_key += "#" + (backend or spec.get("default", ""))
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
        if vm == "@keeper":
            # 1.0.41b (operator): the SHELL surface is a shell — a plain login
            # shell on THIS host as the service user (the host's own seat).
            # Claude on the host is the frontier surface's claude-code backend
            # (host-grounded), not something the shell tab silently starts.
            term_cmd = "bash -l"
            surf_key = "shell:@keeper"
        elif vm and _ssh_host(vm):
            # ⇅ ssh host: the shell IS ssh (interactive — password login allowed)
            term_cmd = _ssh_shell_cmd(_ssh_host(vm))
            surf_key = "shell-ssh:" + vm
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
                    term_cmd = ("ssh -o StrictHostKeyChecking=no "
                                "-o UserKnownHostsFile=/dev/null "
                                f"-o LogLevel=ERROR {ident}ubuntu@{ip}")
                    surf_key = "shell-ssh:" + vm
            if not term_cmd:
                inner = ("command -v station >/dev/null 2>&1 && exec station shell"
                         "; exec bash -l")
                term_cmd = (f"lxc exec {vm} -- sudo -u ubuntu -H bash -lc "
                            + shlex.quote(inner))
                surf_key = "shell:" + vm
        if inst > 1:
            surf_key += "#" + str(inst)
    else:
        term_cmd = (spec["backends"].get(backend)
                    or spec["backends"].get(spec["default"], ""))
        # $FLEETVIEW_TERM_CMD is the launcher's historic HOST knob — it only
        # applies to host-grounded frontier launches, never inside a VM.
        if (surface == "frontier" and not ground_vm
                and backend in ("", spec["default"])):
            term_cmd = os.environ.get("FLEETVIEW_TERM_CMD", "").strip() or term_cmd
        if term_cmd == "claude":   # Claude Code = the fresh templated A seat
            # bash -lc so the seat prelude runs under bash wherever it lands.
            inner = "bash -lc " + shlex.quote(_claude_seat_cmd("frontier"))
        else:
            inner = term_cmd
        # One keeper per station: attach to (or create) the persistent tmux
        # session instead of spawning a second process. The mct workspace is
        # still injected below, textually, inside this wrapper's quoting.
        tmux_sess = _tmux_session_for(surface, backend)
        if tmux_sess:
            inner = _tmux_persist(tmux_sess, inner)
        # Seat the surface in its station (no-op when host-grounded).
        term_cmd = _in_vm(inner, ground_vm)
    if surface == "frontier" and "abstract-claude mct" in term_cmd:
        # mct pointer-exchange grounds. The bundled mct2_repl.py was RETIRED
        # 2026-08-31: the REPL now lives in the `abstract-claude` package
        # (single source of truth; `abstract-claude mct <ws>`), so there's no
        # host script path to swap — only the {ws} to point at the locus/host
        # workspace. Grounding rule (operator 2026-08-27) unchanged: the REPL
        # runs WITHIN the locus the seat is attributed to; ensure the package +
        # push guidance there. Workspace paths are kept (mct2 dirname) so the
        # console's status tab reads the same <ws> as before.
        if ground_vm:
            await _push_mct_to_locus(ground_vm)
            term_cmd = term_cmd.replace("{ws}", "~/.config/hugpy-station/mct2/repl")
        else:
            ws2 = str(FV_STATE_HOME / "mct2" / "repl")
            term_cmd = term_cmd.replace("{ws}", ws2)
            # the directive reaches mct via <ws>/operator-guidance.md, composed
            # fresh at every launch
            _render_guidance(Path(ws2))
        _m2 = _frontier_models().get("mct", "")
        if _m2 and "--model" not in term_cmd:
            term_cmd = term_cmd.replace("abstract-claude mct ",
                                        "abstract-claude mct --model " + _m2 + " ", 1)
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
            elif (term_cmd.startswith("ssh ") and req_vm
                  and req_vm != "@keeper" and _ssh_host(req_vm)):
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
        FV_BACKENDS[sid] = backend or spec.get("default", "") if surface != "shell" else backend
    await ws.send_str(json.dumps({"t": "sid", "sid": sid}))
    sess.attach(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                sess.write(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    ctl = json.loads(msg.data)
                except ValueError:
                    sess.write(msg.data.encode()); continue
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
                    tsess = _tmux_session_for(surface, backend)
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
                    tsess = _tmux_session_for(surface, backend)
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
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
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
    resp = await _sidecar_proxy(request)
    if resp is not None:
        return resp
    try:
        body = await request.json()
    except Exception:
        body = {}
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
# Append one JSON row ($1 — an argv element, never interpolated) to the
# station's bridge-mail inbox, trimmed like view-push so it can't grow forever.
_INBOX_APPEND_SH = ('f="$HOME/.bridge-mail.jsonl"; exec 9>>"$f.lock"; flock 9; '
                    'printf "%s\\n" "$1" >> "$f"; '
                    f'tail -n {FLEET_MAIL_MAX} "$f" > "$f.t" 2>/dev/null '
                    '&& mv "$f.t" "$f"')
# Ping the station's A: type the notice ($1) into its live keeper pane. The
# session is ANCHORED exactly as console-api probes it — unanchored, tmux
# prefix-matches and B's `keeper-local` session would swallow A's ping. NB
# send-keys takes a target-PANE, where a bare '=keeper' does not parse — the
# trailing ':' ('=keeper:') means "that exact session, its active pane".
# rc 3 = no live keeper, a normal answer (the mail still sits in the inbox).
_KEEPER_PING_SH = ('tmux -L console has-session -t =keeper 2>/dev/null || exit 3; '
                   'tmux -L console send-keys -t =keeper: -l "$1"; sleep 0.3; '
                   'tmux -L console send-keys -t =keeper: Enter')


async def _ping_station_keeper(vm, frm, text):
    """Type a one-line ✉ notice into the station's live A (the keeper tmux
    session). Claude Code queues input that arrives mid-turn, so a turn in
    flight is never corrupted — the ping simply lands as the next message.
    No live keeper is a silent no-op: the mail waits in the inbox for
    `fleet-msg --inbox`."""
    note = "✉ fleet mail from %s: %s" % (frm, " ".join(str(text).split()))
    if len(note) > 280:
        note = note[:280] + " … (read the full message: fleet-msg --inbox)"
    try:
        rc, _out, _err = await _run(
            "lxc", "exec", vm, "--", "sudo", "-u", "ubuntu", "-H",
            "bash", "-c", _KEEPER_PING_SH, "fleet-mail-ping", note)
        return rc == 0
    except OSError:
        return False


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
    if to == "keeper":
        row = _mail_row(frm, to, text, unread=True)
        _keeper_mail_write(_keeper_mail_rows() + [row])
        audit(request, "fleet-message", f"to=keeper from={frm}", ok=True)
        return web.json_response({"ok": True, "id": row["id"], "to": to})
    if to not in await known_names():
        return web.json_response({"error": f"unknown station {to}"}, status=404)
    row = _mail_row(frm, to, text)      # no unread: nothing in-VM marks read
    rc, out, err = await _run(
        "lxc", "exec", to, "--", "sudo", "-u", "ubuntu", "-H",
        "bash", "-c", _INBOX_APPEND_SH, "fleet-mail",
        json.dumps(row, separators=(",", ":")))
    if rc != 0:
        audit(request, "fleet-message", f"to={to} failed", ok=False)
        return web.json_response(
            {"ok": False, "error": (err or out).strip() or "delivery failed"},
            status=502)
    pinged = await _ping_station_keeper(to, frm, text)
    audit(request, "fleet-message",
          f"to={to} from={frm} pinged={'yes' if pinged else 'no'}", ok=True)
    return web.json_response({"ok": True, "id": row["id"], "to": to,
                              "pinged": pinged})


# ⧉ brief keeper: type the board brief INTO the station's live keeper (A) so the
# model is prompted about the to-dos directly — no clipboard round-trip. Same
# anchored pane as the ✉ ping, but the full text (no 280-char trim: the brief
# IS the instruction) plus a snapshot of what currently stands open.
_TODO_SNAPSHOT_SH = 'cat "$HOME/todo.json" 2>/dev/null || true'


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
    """POST /api/vm/<station>/todo/brief {"text": "<brief>"} -> {ok, pinged,
    open}. Reads the station's ~/todo.json, appends a one-line snapshot of the
    open items to the brief, and types the whole thing into the live keeper
    pane (Claude Code queues mid-turn input, so it lands as the next message).
    No live keeper: 409 — the operator starts the keeper first, nothing is
    silently dropped."""
    vm = request.match_info.get("vm") or ""
    if vm == "keeper":
        return web.json_response(
            {"error": "the host keeper is briefed by its charge — there is no "
                      "station pane to prompt"}, status=400)
    resp = await _ssh_vm_reroute(request)   # ssh locus: ITS workbench briefs
    if resp is not None:
        return resp
    if vm not in await known_names():
        return web.json_response({"error": f"unknown station {vm}"}, status=404)
    try:
        body = await request.json()
        text = " ".join(str(body.get("text") or "").split())
        if not text:
            raise ValueError
    except Exception:
        return web.json_response(
            {"error": 'body must be {"text": "<brief>"}'}, status=400)
    rc, out, _err = await _run(
        "lxc", "exec", vm, "--", "sudo", "-u", "ubuntu", "-H",
        "bash", "-c", _TODO_SNAPSHOT_SH)
    summary = _todo_brief_summary(out if rc == 0 else "")
    prompt = ("☑ board brief from the operator's console: " + text[:FLEET_MSG_MAX]
              + " " + summary
              + " Please read ~/todo.json now and act on the open items.")
    try:
        rc, out, err = await _run(
            "lxc", "exec", vm, "--", "sudo", "-u", "ubuntu", "-H",
            "bash", "-c", _KEEPER_PING_SH, "todo-brief", prompt)
    except OSError as e:
        rc, out, err = 1, "", str(e)
    if rc == 3:
        audit(request, "todo-brief", f"vm={vm} no live keeper", ok=False)
        return web.json_response(
            {"error": f"no live keeper in {vm} — start the keeper, then brief it"},
            status=409)
    if rc != 0:
        audit(request, "todo-brief", f"vm={vm} failed", ok=False)
        return web.json_response(
            {"ok": False, "error": (err or out).strip() or "delivery failed"},
            status=502)
    audit(request, "todo-brief", f"vm={vm}", ok=True)
    return web.json_response({"ok": True, "pinged": True, "to": vm})


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


def _frontier_dispatch(tag, a_prompt):
    # flow: docs/flows/todo-push-triage.flow.json (keeper hand-off edge)
    """Save a composed prompt to the session prompt-inbox and type its pointer
    into the running frontier terminal (the \u270d-panel file-pointing path).
    Returns (sent, dst); dst None = could not even save."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = _active_ws() / "prompt-inbox" / f"{ts}-{tag}"
    try:
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "prompt.md").write_text(a_prompt, encoding="utf-8")
    except OSError:
        return False, None
    sent = False
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
        sent, dst = _frontier_dispatch(f"triage-{item.get('id')}", a_prompt)
        if dst is None:
            return
        _todo_b_comment(item.get("id"),
                        "→ frontier: " + (reason or "needs the frontier model")
                        + (" · dispatched to the running frontier terminal"
                           if sent else
                           " · saved to prompt-inbox (no frontier terminal running"
                           " — it picks this up when opened)"))


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
_REMIND_TICK = int(os.environ.get("FLEETVIEW_TODO_REMIND_TICK", "60"))
_REMIND_CAP = 3
_REMIND_MIN_GAP = int(os.environ.get("FLEETVIEW_TODO_REMIND_GAP", "600"))
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


async def _todo_reminder_cycle():
    # flow: docs/flows/todo-push-triage.flow.json
    state, _err = _mct_todo_read()
    if state is None:
        return
    items = [i for i in state["items"] if isinstance(i, dict)]
    quiet = _relay_quiet_secs(items)
    now = int(time.time())
    loop = asyncio.get_event_loop()
    name = _active_session()
    for it in items:
        if (it.get("id") == _RELAY_ID
                or it.get("type") not in ("todo", "request", "operator")
                or it.get("status") not in ("open", "doing")):
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
            sent, dst = _frontier_dispatch(f"handoff-{it['id']}", a_prompt)
            if dst is None:
                return
            _watch_update(it["id"], {"last_dispatch": now, "dispatches": int(w.get("dispatches") or 0) + 1})
            _todo_b_comment(it["id"], "→ keeper: " + why
                            + ("" if sent else " · saved to prompt-inbox (no frontier seat live)"))
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
        if n >= _REMIND_CAP or nv >= _REMIND_CAP:   # hard fence: EVERY verdict counts
            _watch_update(it["id"], {"withheld": f"attempt cap ({_REMIND_CAP})",
                                     "withheld_ts": now})
            _todo_b_comment(it["id"],
                            f"\u23f1 withholding reminders: {max(n, nv)} pushes/verdicts "
                            "without completion — operator review needed "
                            "(new activity re-arms)")
            _todo_hist({"event": "reminder", "id": it["id"], "type": it.get("type"),
                        "detail": {"action": "withhold", "reason": "attempt cap",
                                   "attempt": n}})
            continue
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
            return                       # gateway down: fail CLOSED, no blind ping
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
            sent, dst = _frontier_dispatch(f"remind-{it['id']}", a_prompt)
            if dst is None:
                continue
            _watch_update(it["id"], {"dispatches": n + 1, "last_dispatch": now})
            _todo_b_comment(it["id"],
                            f"\u23f1 reminder \u2192 frontier (attempt {n + 1}): "
                            + (reason or "still open past the push cadence")
                            + ("" if sent else " · saved to prompt-inbox "
                               "(no frontier terminal running)"))
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


async def _todo_reminder_loop(app):
    # flow: docs/flows/todo-push-triage.flow.json (proposed redesign; see docs/FLOWS.md)
    while True:
        await asyncio.sleep(_REMIND_TICK)
        try:
            _mint_cfg_relay()
            await _todo_reminder_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


async def _start_reminders(app):
    if FV_TODO_REMIND:
        _mint_cfg_relay()
        app["_todo_reminder"] = asyncio.create_task(_todo_reminder_loop(app))


async def _stop_reminders(app):
    t = app.get("_todo_reminder")
    if t is not None:
        t.cancel()


async def vm_keeper_alias(request):
    """1.0.42: /api/vm/<station>/{todo-history,todo/assist,todo-revision,messages}
    -> that station's OWN workbench, rewritten to its /api/vm/keeper/... route.
    The ☑ board now follows the station selection (index.html), and these
    keeper-only tabs 404'd for any VM because only the literal "keeper" name
    had them. The literal keeper routes are registered first and still win."""
    vm = request.match_info["vm"]
    if vm in ("keeper", "@keeper", "host"):
        raise web.HTTPNotFound()          # never recurse into ourselves ("host" = explicit alias, 2026-08-27)
    if vm not in await known_names() and not _ssh_host(vm):
        return web.json_response({"ok": False, "error": f"unknown locus: {vm}"}, status=404)
    tail = request.path.split(f"/api/vm/{vm}/", 1)[1]
    resp = await _wb_request(request, vm, path=f"/api/vm/keeper/{tail}")
    if resp is not None:
        return resp
    return web.json_response(
        {"ok": False, "error": f"{vm}: workbench console unreachable"}, status=502)


async def vm_host_alias(request):
    """/api/vm/host/* — the EXPLICIT address for the host locus
    (docs/NOMENCLATURE.md, 2026-08-27 consolidation: keeper names agents,
    never machines; the machine is "host"). 308 preserves the method and
    body; the literal "keeper" routes stay the canonical wire form."""
    raise web.HTTPPermanentRedirect("/api/vm/keeper/" + request.match_info["tail"])


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
    from hugpy_agent.config import load_config
    from hugpy_agent.gateway import Gateway
    gw = Gateway.from_config(load_config(overrides=_b_overrides()))
    res = gw.chat(messages, max_tokens=max_tokens)
    return (getattr(res, "text", None) or getattr(res, "content", None) or str(res) or "").strip()


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
    """POST /api/vm/keeper/todo/assist - the canonical console-api keeper modes:
    mode=add (parse a free-text ask into items, append under the lock),
    mode=tidy (PROPOSE a revised list; applied only via op=replace),
    mode=format (text transform for the compose box / an item's text)."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    mode = body.get("mode", "add")
    name = _active_session()
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
#   * The directive is one file. Its verbatim text is what A receives: as
#     --append-system-prompt for the claude-code seat, and as the operator
#     guidance B injects as a governing_instruction for the mct seat.
# =========================================================================== #
FRONTIER_DIRECTIVE_SHIPPED = ROOT / "frontier-directive.md"
FRONTIER_DIRECTIVE_PATH = FV_STATE_HOME / "frontier-directive.md"
# The seat handoff: the letter a retiring/wiped A seat leaves for its reborn
# successor. Fixed path, never injected into any prompt — the shipped
# directive points A at it, and 🛡 steward → directive shows/edits it.
FRONTIER_HANDOFF_PATH = FV_STATE_HOME / "frontier-handoff.md"
FRONTIER_MODELS_PATH = FV_STATE_HOME / "frontier-models.json"
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
                           "claude-code": "claude-fable-5"}
# Claude Code tools that touch the filesystem directly. When the frontier fs
# switch is MEDIATED these are hard-denied in the claude-code seat's settings
# (permissions.deny — native Claude Code policy, no hook needed), so A must
# route file work through B / the local keeper. Bash stays available for
# non-file work; the directive tells A to delegate file-touching commands.
FS_DENY_TOOLS = ["Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Grep", "Glob", "LS"]


def _frontier_directive_text():
    """(text, source, path): the operator's directive file, else the shipped
    default, else a one-line fallback so A is never launched blind."""
    for p, src in ((FRONTIER_DIRECTIVE_PATH, "user"), (FRONTIER_DIRECTIVE_SHIPPED, "shipped")):
        try:
            t = p.read_text(encoding="utf-8")
            if t.strip():
                return t, src, p
        except OSError:
            continue
    return ("Use the local agent (B) for search, grep, and file reads; act "
            "directly only when delegating is plainly worse.", "builtin",
            FRONTIER_DIRECTIVE_PATH)


async def api_frontier_handoff(request):
    """GET/POST the frontier seat handoff at FRONTIER_HANDOFF_PATH. POST
    {"text"} replaces the file (empty text deletes it, meaning: no handoff
    on file)."""
    if request.method == "POST":
        try:
            body = await request.json()
            text = body.get("text", "")
            if not isinstance(text, str):
                raise ValueError
        except Exception:
            return web.json_response({"error": 'body must be {"text": string}'}, status=400)
        FRONTIER_HANDOFF_PATH.parent.mkdir(parents=True, exist_ok=True)
        if text.strip():
            FRONTIER_HANDOFF_PATH.write_text(text, encoding="utf-8")
            did = f"handoff saved ({len(text)} chars)"
        else:
            if FRONTIER_HANDOFF_PATH.exists():
                FRONTIER_HANDOFF_PATH.unlink()
            did = "handoff deleted"
        audit(request, "frontier-handoff", did, ok=True)
    text, exists, mtime = "", False, None
    try:
        text = FRONTIER_HANDOFF_PATH.read_text(encoding="utf-8")
        exists = True
        mtime = int(FRONTIER_HANDOFF_PATH.stat().st_mtime)
    except OSError:
        pass
    return web.json_response({"text": text, "path": str(FRONTIER_HANDOFF_PATH),
                              "exists": exists, "mtime": mtime})


def _frontier_models():
    doc = _read_json(FRONTIER_MODELS_PATH, {}) or {}
    out = dict(FRONTIER_MODEL_DEFAULTS)
    for k in out:
        v = doc.get(k)
        if isinstance(v, str) and (v == "" or _MODEL_RE.match(v)):
            out[k] = v
    return out


def _frontier_fs_mediated():
    """Host-level mirror of the frontier fs switch: True = A's direct filesystem
    access is DENIED (mediated via B). Written by /api/frontier/fs/toggle
    alongside hugpy-agent's own per-workspace policy so BOTH frontier seats
    (mct and claude-code) obey one switch."""
    doc = _read_json(FRONTIER_FS_FLAG, None)
    if isinstance(doc, dict) and "mediated" in doc:
        return bool(doc["mediated"])
    return False


def _claude_seat_system_prompt():
    """The exact text the claude-code seat receives via --append-system-prompt:
    the frontier directive, then the operator's standing B-guidance (if any),
    then a one-line statement of the current fs switch — so A knows which of
    its tools are closed and why, instead of discovering it by denial."""
    parts = [_frontier_directive_text()[0].rstrip()]
    try:
        up = _bg_user_path()
        g = up.read_text(encoding="utf-8").strip() if up.exists() else ""
    except OSError:
        g = ""
    if g:
        parts.append("# Operator guidance\n" + g)
    if _frontier_fs_mediated():
        parts.append("# Filesystem switch: MEDIATED\nYour direct file tools ("
                     + ", ".join(FS_DENY_TOOLS) + ") are denied by the operator for "
                     "this seat. Route every file read/search/edit through the local "
                     "agent (B) or a local shell; do not retry the denied tools.")
    else:
        parts.append("# Filesystem switch: DIRECT\nYour file tools are open. Prefer "
                     "delegating search and bulk reads to B anyway (token economy).")
    return "\n\n".join(parts) + "\n"


def _claude_seat_settings_json():
    """The A settings template, plus the fs deny-list when mediated."""
    doc = dict(_a_template_doc()[0])
    if _frontier_fs_mediated():
        perms = dict(doc.get("permissions") or {})
        deny = list(perms.get("deny") or [])
        for t in FS_DENY_TOOLS:
            if t not in deny:
                deny.append(t)
        perms["deny"] = deny
        doc["permissions"] = perms
    return json.dumps(doc, separators=(",", ":"))


async def api_frontier_directive(request):
    """GET/POST /api/frontier/directive — the directive file (verbatim) and the
    COMPOSED text each frontier seat actually receives. POST {"text"} replaces
    it (empty restores the shipped default). Takes effect at the next A launch
    (claude-code) and the next A turn (mct, via guidance composition)."""
    if request.method == "POST":
        try:
            body = await request.json()
            text = body.get("text", "")
            if not isinstance(text, str):
                raise ValueError
        except Exception:
            return web.json_response({"error": 'body must be {"text": string}'}, status=400)
        FRONTIER_DIRECTIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if text.strip():
            FRONTIER_DIRECTIVE_PATH.write_text(text, encoding="utf-8")
            did = f"directive saved ({len(text)} chars)"
        else:
            if FRONTIER_DIRECTIVE_PATH.exists():
                FRONTIER_DIRECTIVE_PATH.unlink()
            did = "directive reset to the shipped default"
        _render_guidance()
        audit(request, "frontier-directive", did, ok=True)
    text, src, path = _frontier_directive_text()
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
    return web.json_response({
        "text": text, "source": src, "path": str(path),
        "shipped_path": str(FRONTIER_DIRECTIVE_SHIPPED),
        "composed": {
            "claude-code": {"how": "--append-system-prompt (verbatim, at every seat launch)",
                            "text": _claude_seat_system_prompt(),
                            "settings": json.loads(_claude_seat_settings_json())},
            "mct": {"how": "composed into <ws>/operator-guidance.md (directive + 🧭 operator guidance) "
                           "-> B injects it as a governing_instruction (priority 100) into EVERY A turn, "
                           "after A's fixed MCT system prompt; re-rendered at every save and mct launch",
                    "directive": text, "guidance": guidance,
                    "guidance_path": str(_bg_path()),
                    "mct_system_prompt": mct_sys},
        },
        "fs_mediated": _frontier_fs_mediated(),
        "models": _frontier_models(),
    })


async def api_frontier_models(request):
    """GET/POST /api/frontier/models — the model each frontier seat launches
    with. POST {"mct": "sonnet", "claude-code": "opus"|""} ("" = Claude Code's
    own default). Applies at the next launch of that seat."""
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
    m = _frontier_models()
    return web.json_response({
        "models": m, "path": str(FRONTIER_MODELS_PATH),
        "effective": {
            "mct": {"model": m["mct"], "launch": f"abstract-claude mct <ws> --model {m['mct']}",
                    "note": "THE mct (pointer exchange, promoted 2026-08-27; impl files keep the mct2 name) — A is `claude -p --resume` per turn (chat history kept via <ws>/session.json; /new in the REPL resets); flight/queue state at /api/mct2/status; /model in the REPL changes it for that session only"},
            "claude-code": {"model": m["claude-code"] or "(Claude Code default for this account)",
                            "launch": "CLAUDE_CONFIG_DIR=<seat> claude --dangerously-skip-permissions"
                                      + (f" --model {m['claude-code']}" if m["claude-code"] else "")
                                      + " --append-system-prompt <directive>"},
        },
        # A CURATED shortcut list — NOT a whitelist. The dropdown also offers a
        # free-text "custom…" entry, and POST accepts ANY id matching _MODEL_RE,
        # so the operator is never limited to these (2026-08-22: the old list
        # omitted opus-4-8 and had no custom path, so a go-to model could not be
        # picked at all). Keep the operator's current go-tos near the top.
        "choices": ["claude-fable-5", "claude-opus-5", "claude-opus-4-8",
                    "claude-sonnet-5", "claude-haiku-4-5-20251001",
                    "opus", "sonnet", "haiku", ""],
        "custom_ok": True,
    })


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
    method = ("oauth" if kv.get("oauth") == "1" else
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
if [ "$(id -un)" = "$U" ]; then sudo -n true >/dev/null 2>&1 && echo effective=1 || echo effective=0
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
            st["effective"] = True if v == "1" else False if v == "0" else None
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
    return "" if vm in ("", "@keeper", "host") else vm


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


async def api_host_canvas(request):
    kind = request.match_info["kind"]
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
    ground_vm = "" if vm == "@keeper" else (vm or MODEL_VM)
    tsess = _tmux_session_for(surface, backend)
    if not tsess:
        return web.json_response({"error": "surface/backend has no persistent tmux session"}, status=400)
    tgt = shlex.quote(tsess + ":0")
    base = f"tmux -L {KEEPER_TMUX_SOCK} "
    if body.get("close"):
        script = (base + f"list-panes -t {tgt} -F '#{{pane_id}} #{{@{_SHOW_PANE_MARK}}}' | "
                  "awk '$2==\"1\"{print $1}' | xargs -r -n1 " + base + "kill-pane -t")
        rc, out, err = await _locus_run(ground_vm, script, timeout=15)
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
    rc, out, err = await _locus_run(ground_vm, script, timeout=15)
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
        py = ("import json,os; "
              "p=os.path.expanduser('~/.config/hugpy-station/mct2/repl/status.json'); "
              "d=json.load(open(p)); "
              "d['alive']=os.path.exists('/proc/%s'%d.get('pid')); "
              "print(json.dumps(d))")
        rc, out, err = await _locus_run(ground, "python3 -c " + shlex.quote(py), 10)
        try:
            assert rc == 0
            doc = json.loads(out.strip().splitlines()[-1])
            assert isinstance(doc, dict)
        except (AssertionError, ValueError, IndexError):
            return web.json_response({"present": False, "locus": ground})
        if not doc.get("alive") and doc.get("state") != "offline":
            doc["state"] = "offline"
        doc.update({"present": True, "locus": ground,
                    "age_s": round(time.time() - (doc.get("updated") or 0), 1)})
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
    auth = await _probe_claude_auth(ground)
    models = _frontier_models()
    dtext, dsrc, dpath = _frontier_directive_text()
    return web.json_response({
        "requested": req_vm or "@keeper", "grounded_in": ground or "host",
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
                 or (Path.home() / ".claude-oauth.env").exists())
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


async def api_seat_wipe(request):
    """POST /api/seat/wipe?vm=<locus> — factory-reset the frontier claude seat
    on that locus. Kills its tmux session, deletes the seat dir
    (~/.claude-seat/frontier — sessions, projects incl. A's memory, todos,
    caches) plus A_STATE_DIRS under ~/.claude. ~/.claude/.credentials.json and
    ~/.claude/.claude.json deliberately survive (see _claude_seat_cmd: wiping
    them re-runs onboarding/login), so the next launch re-creates the seat
    OAuth-authenticated with settings.json freshly stamped from the template."""
    req_vm = (request.query.get("vm") or "").strip()
    if req_vm == "@keeper":
        ground = ""
    elif req_vm and req_vm in await all_locus_names():
        ground = req_vm
    else:
        ground = MODEL_VM
    tsess = _tmux_session_for("frontier", "claude-code") or "keeper-claude"
    state = " ".join('"$HOME/.claude/' + d + '"' for d in A_STATE_DIRS)
    script = (
        f"tmux -L {KEEPER_TMUX_SOCK} kill-session -t ={tsess} 2>/dev/null; "
        f'rm -rf "$HOME/.claude-seat/frontier" {state}; echo wiped')
    rc, out, err = await _locus_run(ground, script, timeout=30)
    ok = rc == 0 and "wiped" in (out or "")
    audit(request, "seat_wipe",
          f"locus={ground or 'host'} sess={tsess} rc={rc}", ok=ok)
    if not ok:
        return web.json_response(
            {"error": (err or out or "wipe failed").strip(),
             "locus": ground or "host"}, status=502)
    return web.json_response({
        "ok": True, "locus": ground or "host", "killed": tsess,
        "removed": ["~/.claude-seat/frontier"] +
                   ["~/.claude/" + d for d in A_STATE_DIRS],
        "kept": ["~/.claude/.credentials.json", "~/.claude/.claude.json"],
        "relaunch": "open the frontier claude-code seat — it re-provisions "
                    "from the template and the surviving OAuth credentials"})


async def api_about(request):
    """GET /api/about — version + provenance for the Help→About popup and the
    shell title. Facts only the backend knows for sure: what is running, from
    where. The shell (main.js) adds its own app-root line."""
    try:
        version = (ROOT.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = "unknown"
    return web.json_response({
        "name": "hugpy Station",
        "version": version,
        "backend_dir": str(ROOT),
        "python": sys.version.split()[0],
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


def make_app():
    app = web.Application(middlewares=[auth_mw], client_max_size=MAX_UPLOAD_SIZE)
    app.on_startup.append(_start_reaper)
    app.on_cleanup.append(_stop_reaper)
    # console-api sidecar: the full fleet management API, same as the web console
    app.on_startup.append(_start_console_api)
    app.on_cleanup.append(_stop_console_api)
    app.on_startup.append(_start_bugreport_api)
    app.on_startup.append(_start_reminders)     # ⏱ board reminder cycle
    app.on_cleanup.append(_stop_reminders)
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
    app.router.add_get("/api/vm/keeper/todo", mct_todo_get)
    app.router.add_post("/api/vm/keeper/todo", mct_todo_post)
    app.router.add_get("/api/vm/keeper/todo-history", keeper_todo_history)
    app.router.add_post("/api/vm/keeper/todo/assist", keeper_todo_assist)
    app.router.add_post("/api/vm/keeper/todo-revision", keeper_todo_revision)
    app.router.add_route("*", "/api/vm/keeper/todo/discord", keeper_todo_discord)
    # ✉ fleet mail: the keeper console's inbox (before the {vm} proxy, so the
    # keeper drawer's messages tab lights up) + the one send endpoint.
    app.router.add_get("/api/vm/keeper/messages", keeper_messages_get)
    # explicit host-locus address (nomenclature 2026-08-27): /api/vm/host/* is
    # /api/vm/keeper/* — registered before every {vm} wildcard so it wins.
    app.router.add_route("*", "/api/vm/host/{tail:.*}", vm_host_alias)
    app.router.add_post("/api/fleet/message", fleet_message_post)
    app.router.add_post("/api/vm/{vm}/todo/brief", vm_todo_brief_post)   # ⧉ brief keeper
    app.on_startup.append(_start_fleet_mail)   # 30s station-outbox collector
    app.on_cleanup.append(_stop_fleet_mail)
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
    app.router.add_get("/api/vm/@keeper/{kind:design|flow}", api_host_canvas)
    app.router.add_post("/api/vm/@keeper/{kind:design|flow}", api_host_canvas)
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
    app.router.add_get("/api/term/backends", term_backends)
    app.router.add_get("/api/term/frontier", term_frontier)
    app.router.add_post("/api/term/frontier", term_frontier)
    app.router.add_get("/api/term/bgate", term_bgate)
    app.router.add_post("/api/term/bgate", term_bgate)
    app.router.add_get("/api/browser-access", browser_access_get)
    app.router.add_post("/api/browser-access", browser_access_post)
    app.router.add_get("/api/a/template", a_template_get)
    app.router.add_post("/api/a/template", a_template_post)
    app.router.add_post("/api/a/clear", a_clear)
    app.router.add_get("/api/b/guidance", b_guidance_get)
    app.router.add_post("/api/b/guidance", b_guidance_post)
    app.router.add_post("/api/b/chat", b_chat)
    app.router.add_get("/api/mct/todo", mct_todo_get)
    app.router.add_post("/api/mct/prompt", mct_prompt)
    app.router.add_post("/api/mct/prompt/revise", mct_prompt_revise)
    app.router.add_get("/api/mct/live", mct_live)
    app.router.add_post("/api/mct/todo", mct_todo_post)
    app.router.add_get("/api/mct/todo/history", mct_todo_history)
    app.router.add_post("/api/mct/todo/assist", mct_todo_assist)
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
    app.router.add_get("/api/b/model", api_b_model)
    app.router.add_post("/api/b/model", api_b_model)
    app.router.add_get("/api/claude/auth", api_claude_auth)
    app.router.add_get("/api/sudo/status", api_sudo_status)
    app.router.add_post("/api/sudo/toggle", api_sudo_toggle)
    app.router.add_get("/api/stations/{name}/console-status", api_vm_console_status)
    app.router.add_post("/api/stations/{name}/console-install", api_vm_console_install)
    app.router.add_get("/api/frontier/fs/status", api_frontier_fs_status)
    app.router.add_post("/api/frontier/fs/toggle", api_frontier_fs_toggle)
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

    scheme = "https" if USE_TLS else "http"
    mode = "session+token" if (AUTH_REQUIRED and TOKEN) else \
           "session" if AUTH_REQUIRED else "OPEN (loopback only — no auth)"
    print(f"station-console on {scheme}://{HOST}:{PORT}  (auth: {mode})")
    if not AUTH_REQUIRED:
        print("  !! no password set — OPEN on loopback only. Run --set-password.")
    web.run_app(make_app(), host=HOST, port=PORT, ssl_context=ssl_ctx, print=None)
