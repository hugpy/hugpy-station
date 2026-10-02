"""~/.hugpy/.env — the shared hugpy settings home, as Station reads and edits it.

One file every hugpy arm reads (abstract_toolserver.hugpy_home): $HUGPY_HOME
(default ~/.hugpy)/.env, KEY=VALUE. The process environment always wins over the
file, so the file is the default and a shell export / unit Environment= the
override. Station's ⚙ Settings edits the file through /api/settings.

The hugpy fleet pointer (HUGPY_BASE) resolves through abstract_toolserver when
it is importable, else through the identical stdlib fallback below — Station
must not carry a fleet default of its own.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from abstract_toolserver import hugpy_home as _shared
except ImportError:          # pre-0.0.54 or not installed: same rules, stdlib only
    _shared = None

ENV_NAME = ".env"
BASE_KEYS = ("HUGPY_BASE", "HUGPY_URL")    # HUGPY_URL: legacy alias, read only
DEFAULT_BASE = "http://127.0.0.1:7002"     # a fresh install's own central

# The keys ⚙ Settings shows, by section. `secret` values are never returned in
# full (only whether they are set); writing "" removes the key from the file.
SCHEMA = [
    {"key": "HUGPY_BASE", "section": "hugpy fleet", "label": "Fleet URL",
     "help": "Central every hugpy arm talks to, e.g. https://dev.hugpy.ai",
     "default": DEFAULT_BASE},
    {"key": "HUGPY_API_KEY", "section": "hugpy fleet", "label": "Fleet API key",
     "secret": True},
    {"key": "HUGPY_FLEET_FALLBACK", "section": "hugpy fleet", "bool": True,
     "label": "Fall back to the fleet URL when the local central is down",
     "help": "Off: seats only ever use this machine's central. On: when it does not "
             "answer, seat traffic (prompts, files, tool output) goes to the Fleet URL "
             "— only a URL you set, never a default — and Station shows it.",
     "default": "0"},
    {"key": "HUGPY_AGENT_SERVE", "section": "serve", "label": "Serve URL",
     "help": "Console serve the TUI and Station join before spawning a local one"},
    {"key": "HUGPY_SERVE_TOKEN", "section": "serve", "label": "Serve token",
     "secret": True},
    {"key": "HUGPY_TOOLSERVER_URL", "section": "toolserver", "label": "Toolserver URL"},
    {"key": "HUGPY_OPERATOR_TOKEN", "section": "toolserver", "label": "Toolserver token",
     "secret": True},
]
_KNOWN = {row["key"]: row for row in SCHEMA}
URL_KEYS = ("HUGPY_BASE", "HUGPY_AGENT_SERVE", "HUGPY_TOOLSERVER_URL")
LOCAL_FRONT = "http://127.0.0.1:7002"      # this machine's central (gunicorn front)


def url_ok(url: str) -> str:
    """"" when `url` may carry credentials, else the reason it may not.
    https anywhere; plain http only to loopback or a private LAN address —
    a token never crosses the internet in clear text."""
    import ipaddress
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "must be an http(s):// URL"
    if parts.scheme == "https":
        return ""
    host = parts.hostname
    if host == "localhost":
        return ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "plain http is only allowed to loopback or a LAN address; use https://"
    if ip.is_loopback or ip.is_private:
        return ""
    return "plain http is only allowed to loopback or a LAN address; use https://"


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _configured_base() -> str:
    """The fleet URL the operator SET (env or file) — "" when only the default."""
    file_values = read_file()
    for source in (os.environ, file_values):
        for k in BASE_KEYS:
            v = (source.get(k) or "").strip().rstrip("/")
            if v:
                return v[:-4] if v.endswith("/api") else v
    return ""


_FRONT = {"at": 0.0, "url": LOCAL_FRONT, "fallback": False, "seen_local": False}


def local_front(ttl=30.0) -> str:
    """Where this machine's seats reach central.

    - Local central answers → the local front (big seat bodies skip nginx).
    - No local central has EVER answered here (a client box) → base(): the Fleet
      URL is this machine's primary target, not a fallback.
    - The local central answered before and is now down → stay on it (fail
      visibly) unless HUGPY_FLEET_FALLBACK is on AND the operator SET a Fleet URL
      that passes url_ok(); then that URL, flagged for the UI banner.
    Never a silent jump from this machine's central to somewhere else."""
    import socket
    import time
    now = time.monotonic()
    if now - _FRONT["at"] < ttl:
        return _FRONT["url"]
    url, fallback = LOCAL_FRONT, False
    try:
        socket.create_connection(("127.0.0.1", 7002), timeout=0.3).close()
        _FRONT["seen_local"] = True
    except OSError:
        target = _configured_base()
        if not _FRONT["seen_local"]:
            url = base()
        elif (_truthy(os.environ.get("HUGPY_FLEET_FALLBACK") or read_file().get("HUGPY_FLEET_FALLBACK"))
                and target and target != LOCAL_FRONT and not url_ok(target)):
            url, fallback = target, True
    _FRONT.update(at=now, url=url, fallback=fallback)
    return url


def front_status() -> dict:
    """{url, fallback} for the UI banner ("seats → … (local central down)")."""
    local_front()
    return {"url": _FRONT["url"], "fallback": _FRONT["fallback"]}


def home() -> Path:
    return Path(os.path.expanduser((os.environ.get("HUGPY_HOME") or "").strip() or "~/.hugpy"))


def env_path() -> Path:
    return home() / ENV_NAME


def read_file(path=None) -> dict:
    out = {}
    try:
        lines = Path(path or env_path()).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        out[k] = v.strip().strip('"').strip("'")
    return out


def load() -> list:
    """Fill os.environ from the file where unset; returns the keys set."""
    if _shared is not None:
        return list(_shared.load())
    added = []
    for k, v in read_file().items():
        if k and v and not (os.environ.get(k) or "").strip():
            os.environ[k] = v
            added.append(k)
    return added


def base() -> str:
    """The hugpy fleet base, bare host (no trailing /api)."""
    if _shared is not None and hasattr(_shared, "base"):
        return _shared.base()
    file_values = read_file()
    for source in (os.environ, file_values):
        for k in BASE_KEYS:
            v = (source.get(k) or "").strip().rstrip("/")
            if v:
                return v[:-4] if v.endswith("/api") else v
    return DEFAULT_BASE


def view() -> dict:
    """{path, settings:[{key, section, label, help, secret, value|set, source}]}.
    source: "env" (process environment overrides the file), "file", or "default"."""
    file_values = read_file()
    rows = []
    for row in SCHEMA:
        k = row["key"]
        in_file = file_values.get(k, "")
        in_env = (os.environ.get(k) or "").strip()
        effective = in_env or in_file
        source = ("env" if in_env and in_env != in_file else
                  "file" if in_file else "default")
        item = dict(row, source=source)
        if row.get("secret"):
            item["set"] = bool(effective)
        else:
            item["value"] = in_file
            item["effective"] = effective or row.get("default", "")
            if k in URL_KEYS and effective and url_ok(effective):
                item["error"] = url_ok(effective)
        rows.append(item)
    return {"path": str(env_path()), "settings": rows, "base": base(),
            "front": front_status()}


def update(changes: dict) -> dict:
    """Write known keys into the file (0600), preserving every other line.
    "" removes a key. Applies to this process too unless the shell overrides."""
    if not isinstance(changes, dict):
        raise ValueError("expected an object of KEY: value")
    for k, v in changes.items():
        if k not in _KNOWN:
            raise ValueError("unknown setting %r" % k)
        if not isinstance(v, str) or "\n" in v or len(v) > 4096:
            raise ValueError("invalid value for %s" % k)
        if k in URL_KEYS and v.strip():
            why = url_ok(v.strip())
            if why:
                raise ValueError("%s %s" % (k, why))
        if _KNOWN[k].get("bool") and v.strip() not in ("", "0", "1"):
            raise ValueError("%s must be 0 or 1" % k)
    path = env_path()
    before = read_file(path)
    # keys the shell/unit overrides keep their env value; the rest apply live
    overridden = {k for k in changes
                  if (os.environ.get(k) or "") and os.environ.get(k) != before.get(k, "")}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = ["# hugpy settings — shared by every hugpy package; edited by Station ⚙ Settings"]
    pending = {k: v.strip() for k, v in changes.items()}
    out = []
    for ln in lines:
        s = ln.strip()
        k = s.split("=", 1)[0].strip() if "=" in s and not s.startswith("#") else ""
        if k.startswith("export "):
            k = k[7:].strip()
        if k in pending:
            v = pending.pop(k)
            if v:
                out.append("%s=%s" % (k, v))
            continue
        out.append(ln)
    out.extend("%s=%s" % (k, v) for k, v in pending.items() if v)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(ENV_NAME + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    for k, v in changes.items():
        if k in overridden:
            continue
        if v.strip():
            os.environ[k] = v.strip()
        else:
            os.environ.pop(k, None)
    _FRONT["at"] = 0.0                     # re-decide the seat front on next use
    return view()


def audit_detail(changes: dict, before: dict) -> str:
    """Audit text: URL/flag keys as old -> new (where traffic was pointed is the
    record that matters); secrets only as changed/cleared."""
    out = []
    for k in sorted(changes):
        new = (changes[k] or "").strip()
        if _KNOWN.get(k, {}).get("secret"):
            out.append("%s %s" % (k, "set" if new else "cleared"))
        else:
            out.append("%s %s -> %s" % (k, before.get(k) or "(unset)", new or "(unset)"))
    return "; ".join(out)
