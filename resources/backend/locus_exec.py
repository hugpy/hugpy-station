"""locus_exec — ONE code path to act on a locus (station 1.0.141).

Operator principle: every station is perfect as host of its own locus; only the
REMOTE path broke. So there is one primitive, and the host is just the target
whose transport is "no hop":

    Target(kind=self)  -> bash -s                       (this station's user)
    Target(kind=ssh)   -> ssh <opts> user@host bash -s  (over the ssh-mux)
    Target(kind=lxc)   -> lxc exec <guest> -- sudo -u ubuntu -H bash -s

Every action is a bash SCRIPT shipped on stdin (never in argv), so nothing
here is bounded by tmux's ~16 KB client message or the ssh command line:

  * seat_launch()  — write the generated seat script ON THE TARGET (heredoc on
    stdin), then run the same short `tmux -L console new-session -A -s <sess>
    "bash <file>"` on every target. tmux argv stays < 1 KB whatever the
    directive size (the 1.0.139 "command too long" bug).
  * tmux()         — send-keys / has-session / capture-pane on the target's
    console socket (unstick, paste, show, live /model).
  * read_cfg() / write_cfg() — the seat-config files (directive,
    models, fs/delegate switches, B model, A template, guidance) in the
    TARGET's own station state dir, resolved on the target by STATE_SH.
  * probe_serve() + ServeForwards — find the target's own abstract-claude
    serve port and reach it through `ssh -O forward -L` on the existing mux.

Pure module (no server.py import) so it can be unit-tested with a fake runner:
runner(argv, stdin_bytes, timeout) -> (rc, out_str, err_str).
"""
import asyncio
import base64
import contextlib
import json
import os
import re
import shlex
import socket
import time

KIND_SELF, KIND_SSH, KIND_LXC = "self", "ssh", "lxc"
TMUX_SOCK = "console"
ARGV_BUDGET = 1024          # bytes: the tmux/ssh argv must stay under this
_HEREDOC = "__HUGPY_STATION_141__"

# Seat-config files, relative to the TARGET's station state dir. The same names
# the host station uses under FV_STATE_HOME — so a remote locus is read exactly
# where its own station reads it.
CFG_FILES = {
    "directive": "frontier-directive.md",
    "models": "frontier-models.json",
    "fs": "frontier-fs.json",
    "delegate": "frontier-delegate.json",
    "b_model": "b-model.json",
    "a_template": "a-settings-template.json",
    "guidance": "mct/repl/operator-guidance.user.md",
    # 1.0.143: per-session operator overlays + the facts the directive
    # generator needs (directives.LAYER_FILES — same keys, same paths)
    "dir_keeper": "directives/keeper.operator.md",
    "dir_chat": "directives/chat.operator.md",
    "dir_worker": "directives/worker.operator.md",
    "dir_local": "directives/local.operator.md",
    "serve": "ac-serve-station.json",
    "station": "directives/station.json",
}
CFG_MAX_BYTES = 512 * 1024

# The target's station state dir, resolved ON the target, identically for
# every kind: $HUGPY_STATION_STATE (set for the host station process and in
# every seat script) -> the pointer the station writes at startup
# (~/.config/hugpy-station/state-dir) -> a ~/hugpy-station layout that carries
# its env dir (the vm_mgr keeper) -> ~/.config/hugpy-station (the default).
STATE_SH = (
    'S="${HUGPY_STATION_STATE:-}"\n'
    'if [ -z "$S" ] && [ -r "$HOME/.config/hugpy-station/state-dir" ]; then\n'
    '  S="$(head -n1 "$HOME/.config/hugpy-station/state-dir" 2>/dev/null)"; fi\n'
    'if [ -z "$S" ] && [ -d "$HOME/hugpy-station/env" ]; then S="$HOME/hugpy-station"; fi\n'
    'S="${S:-$HOME/.config/hugpy-station}"\n'
)


class LocusError(RuntimeError):
    pass


class Target:
    """Where an action runs. name = the dropdown name ('' for self); locus =
    the central key; ssh_argv = the non-interactive ssh argv ending in the
    destination; ssh_tty = the interactive `ssh -t ...` string."""
    __slots__ = ("kind", "name", "locus", "ssh_argv", "ssh_tty", "lxc_user", "env")

    def __init__(self, kind, name="", locus="", ssh_argv=None, ssh_tty="",
                 lxc_user="ubuntu", env=None):
        if kind not in (KIND_SELF, KIND_SSH, KIND_LXC):
            raise ValueError("bad target kind " + repr(kind))
        if kind == KIND_SSH and not ssh_argv:
            raise ValueError("ssh target needs ssh_argv")
        self.kind, self.name, self.locus = kind, name, locus
        self.ssh_argv = list(ssh_argv or [])
        self.ssh_tty, self.lxc_user, self.env = ssh_tty, lxc_user, dict(env or {})

    @property
    def remote(self):
        return self.kind != KIND_SELF

    def __repr__(self):
        return f"Target({self.kind}, {self.name or '@self'}, locus={self.locus!r})"


def script_argv(t):
    """argv that runs a bash script read from STDIN on the target."""
    if t.kind == KIND_SELF:
        return ["bash", "-s"]
    if t.kind == KIND_SSH:
        return list(t.ssh_argv) + ["bash", "-s"]
    return ["lxc", "exec", t.name, "--", "sudo", "-u", t.lxc_user, "-H", "bash", "-s"]


def pty_wrap(t, inner):
    """The interactive PTY command that runs the SHORT shell string `inner` on
    the target (a terminal is the one thing that needs a tty)."""
    if t.kind == KIND_SELF:
        return "bash -c " + shlex.quote(inner)
    if t.kind == KIND_SSH:
        return t.ssh_tty + " -- " + shlex.quote(inner)
    return (f"lxc exec {shlex.quote(t.name)} -- sudo -u {t.lxc_user} -H bash -lc "
            + shlex.quote(inner))


async def default_runner(argv, stdin=None, timeout=30, env=None):
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
    except OSError as e:
        return 127, "", str(e)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return 124, "", f"timed out after {timeout}s"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def run(t, script, runner=None, timeout=30):
    """Run a bash script on the target (script on stdin). (rc, out, err)."""
    runner = runner or default_runner
    return await runner(script_argv(t), script.encode("utf-8"), timeout)


def heredoc_b64(dest_expr, data):
    """Shell that writes `data` (bytes) to the file named by the shell
    expression dest_expr — base64 in a quoted heredoc, i.e. on stdin."""
    b64 = base64.b64encode(data).decode("ascii")
    lines = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))
    return f"base64 -d > {dest_expr} <<'{_HEREDOC}'\n{lines}\n{_HEREDOC}\n"


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name or "").strip("-") or "seat"


# ── seat launch ────────────────────────────────────────────────────────────────
LAUNCH_KEEP_MIN = 24 * 60


def materialize_script(sess, text, stamp):
    """The bash (run ON the target) that parks the seat script under the
    target's <state>/seat-launch (0700) and prints its absolute path."""
    name = f"{_safe(sess)}-{stamp}.sh"
    return (
        "set -e\n" + STATE_SH
        + 'd="$S/seat-launch"; mkdir -p "$d"; chmod 700 "$d"\n'
        + f'f="$d/{name}"\n'
        + heredoc_b64('"$f.tmp"', text.encode("utf-8"))
        + 'chmod 700 "$f.tmp"; mv -f "$f.tmp" "$f"\n'
        + f'find "$d" -maxdepth 1 -name {shlex.quote(_safe(sess) + "-*.sh")} '
          f'-mmin +{LAUNCH_KEEP_MIN} -delete 2>/dev/null || true\n'
        + 'printf "%s\\n" "$f"\n')


def tmux_attach(sess, path, pre="", opts="", detached=False):
    """The short attach-or-create command: identical on every target.
    detached=True starts the session without attaching (relaunch)."""
    verb = "new-session -d -s " if detached else "new-session -A -s "
    t = (pre + " " if pre else "") + f"tmux -L {TMUX_SOCK} " + opts \
        + verb + shlex.quote(sess) + " " + shlex.quote("bash " + shlex.quote(path))
    if detached:
        return "exec " + t
    return ("if command -v tmux >/dev/null 2>&1; then exec " + t + "; else "
            "echo '[hugpy-station] tmux is not installed on '\"$(hostname)\"' — "
            "running the seat without persistence'; exec bash " + shlex.quote(path) + "; fi")


def seat_script_text(sess, body):
    return ("#!/bin/bash\n# generated by hugpy-station for tmux session " + sess
            + " — safe to delete once the seat is up\n" + body + "\n")


async def seat_materialize(t, sess, body, runner=None, stamp=None):
    """Write the seat script ON the target; -> its absolute path there.
    Raises LocusError when it cannot be written — there is no inline fallback."""
    stamp = stamp or f"{time.time_ns()}-{os.getpid()}"
    rc, out, err = await run(t, materialize_script(sess, seat_script_text(sess, body), stamp),
                             runner, timeout=45)
    path = ""
    for ln in reversed((out or "").strip().splitlines()):
        ln = ln.strip()
        if ln.startswith("/") and ln.endswith(".sh"):
            path = ln
            break
    if rc != 0 or not path:
        raise LocusError(f"seat script not written on {t.name or 'this host'}: "
                         + ((err or out or f"rc={rc}").strip()[-300:]))
    return path


async def seat_launch(t, sess, body, runner=None, pre="", opts="", stamp=None):
    """Write the seat script on the target and return (pty_command, path).
    The returned command (and the tmux argv inside it) never carries `body`."""
    path = await seat_materialize(t, sess, body, runner, stamp)
    attach = tmux_attach(sess, path, pre if t.kind == KIND_SELF else "", opts)
    return pty_wrap(t, attach), path


async def seat_start_detached(t, sess, body, runner=None, pre="", opts="", stamp=None, cwd=""):
    """Same script, same tmux line, started detached (the relaunch path).
    -> (rc, out, err, path)."""
    path = await seat_materialize(t, sess, body, runner, stamp)
    attach = tmux_attach(sess, path, pre if t.kind == KIND_SELF else "", opts, detached=True)
    cd = f"cd {shlex.quote(cwd)} 2>/dev/null || true\n" if cwd else ""
    rc, out, err = await run(t, cd + attach + "\n", runner, timeout=30)
    return rc, out, err, path


def tmux_shell(*args):
    return f"tmux -L {TMUX_SOCK} " + " ".join(shlex.quote(str(a)) for a in args)


async def tmux(t, args, runner=None, timeout=15):
    """Run `tmux -L console <args>` on the target (args may be long: they ride
    the stdin script, not an argv)."""
    return await run(t, "exec " + tmux_shell(*args) + "\n", runner, timeout)


# ── seat config (the target's own files) ──────────────────────────────────────
_CFG_READ_PY = r'''
import getpass, json, os, socket, sys
S = sys.argv[1]
names = json.loads(sys.argv[2])
cap = int(sys.argv[3])
out = {"state": S, "home": os.path.expanduser("~"), "files": {}, "mtimes": {}}
# 1.0.143: the locus's live identity for its directives, resolved ON the target
out["user"], out["host"] = getpass.getuser(), socket.gethostname()
for a in (os.path.join(S, "app", "current", "resources", "backend"), "/opt/hugpy-station/resources/backend"):
    if os.path.isfile(os.path.join(a, "server.py")):
        out["app"] = a
        break
for key, rel in names.items():
    p = os.path.join(S, rel)
    try:
        with open(p, "rb") as fh:
            data = fh.read(cap + 1)
        out["files"][key] = data[:cap].decode("utf-8", "replace")
        out["mtimes"][key] = int(os.stat(p).st_mtime)
    except OSError:
        out["files"][key] = None
print(json.dumps(out))
'''


def read_cfg_script(keys=None, overrides=None):
    """overrides: {key: relpath under the state dir} (the host passes its
    active mct workspace's guidance file)."""
    names = {k: CFG_FILES[k] for k in (keys or CFG_FILES) if k in CFG_FILES}
    for k, rel in (overrides or {}).items():
        if k in names and rel and not rel.startswith("/") and ".." not in rel.split("/"):
            names[k] = rel
    return (STATE_SH + "exec python3 - \"$S\" " + shlex.quote(json.dumps(names))
            + f" {CFG_MAX_BYTES} <<'{_HEREDOC}'\n" + _CFG_READ_PY + f"\n{_HEREDOC}\n")


async def read_cfg(t, keys=None, runner=None, timeout=25, overrides=None):
    rc, out, err = await run(t, read_cfg_script(keys, overrides), runner, timeout)
    try:
        doc = json.loads((out or "").strip().splitlines()[-1])
        assert isinstance(doc, dict) and isinstance(doc.get("files"), dict)
        return doc
    except Exception:
        raise LocusError(f"seat config unreadable on {t.name or 'this host'}: "
                         + ((err or out or f"rc={rc}").strip()[-300:]))


def write_cfg_script(key, text):
    """text None = delete the file."""
    rel = CFG_FILES[key]
    q = shlex.quote(rel)
    if text is None:
        return "set -e\n" + STATE_SH + f'rm -f "$S"/{q}\nprintf "%s\\n" "$S"/{q}\n'
    return ("set -e\n" + STATE_SH + f'p="$S"/{q}; mkdir -p "$(dirname "$p")"\n'
            + heredoc_b64('"$p.tmp"', text.encode("utf-8"))
            + 'mv -f "$p.tmp" "$p"\nprintf "%s\\n" "$p"\n')


async def write_cfg(t, key, text, runner=None, timeout=25):
    rc, out, err = await run(t, write_cfg_script(key, text), runner, timeout)
    if rc != 0:
        raise LocusError(f"{CFG_FILES[key]} not written on {t.name or 'this host'}: "
                         + ((err or out or f"rc={rc}").strip()[-300:]))
    return (out or "").strip().splitlines()[-1] if (out or "").strip() else ""


# ── remote files (the 📁 drawer API) — fenced under the target user's $HOME ────
_FS_PY = r'''
import base64, json, os, shutil, stat, sys
op = sys.argv[1]; arg = json.loads(sys.argv[2]); cap = int(sys.argv[3])
home = os.path.realpath(os.path.expanduser("~"))
def fence(p):
    p = (p or "").strip() or home
    if not p.startswith("/"):
        p = os.path.join(home, p)
    r = os.path.realpath(p)
    if r != home and not r.startswith(home + "/"):
        raise PermissionError("outside the locus home " + home)
    return r
def done(d):
    print(json.dumps(d)); sys.exit(0)
try:
    if op == "list":
        p = fence(arg.get("path")); ents = []
        for n in sorted(os.listdir(p)):
            fp = os.path.join(p, n)
            try:
                ls = os.lstat(fp); isl = stat.S_ISLNK(ls.st_mode)
                isd = os.path.isdir(fp)
                ents.append({"name": n, "path": fp, "dir": isd, "link": isl,
                             "size": ls.st_size, "mtime": ls.st_mtime})
            except OSError:
                continue
        ents.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        done({"path": p, "parent": os.path.dirname(p.rstrip("/")) or "/", "home": home, "entries": ents})
    if op == "read":
        p = fence(arg.get("path")); size = os.path.getsize(p)
        if size > cap:
            done({"too_large": True, "size": size})
        data = open(p, "rb").read()
        if b"\x00" in data[:8192]:
            done({"binary": True, "size": size})
        done({"text": data.decode("utf-8", "replace"), "size": size})
    if op == "download":
        p = fence(arg.get("path")); size = os.path.getsize(p)
        if size > cap:
            done({"too_large": True, "size": size})
        done({"b64": base64.b64encode(open(p, "rb").read()).decode(), "size": size})
    if op == "write":
        p = fence(arg.get("path")); os.makedirs(os.path.dirname(p), exist_ok=True)
        data = base64.b64decode(sys.stdin.buffer.read()) if arg.get("stdin") else b""
        tmp = p + ".hs-tmp"; open(tmp, "wb").write(data); os.replace(tmp, p)
        done({"ok": True, "path": p})
    if op == "mkdir":
        p = fence(arg.get("path")); os.makedirs(p, exist_ok=True); done({"ok": True, "path": p})
    if op == "rename":
        s, d = fence(arg.get("src")), fence(arg.get("dst"))
        if os.path.exists(d):
            raise FileExistsError(d)
        os.rename(s, d); done({"ok": True, "src": s, "dst": d})
    if op == "delete":
        p = fence(arg.get("path"))
        if p == home:
            raise PermissionError("refusing to delete the locus home")
        (shutil.rmtree if os.path.isdir(p) and not os.path.islink(p) else os.unlink)(p)
        done({"ok": True, "path": p})
    done({"error": "unknown op " + op})
except Exception as e:
    done({"error": type(e).__name__ + ": " + str(e)[:300]})
'''


def fs_script(op, arg, data=None, cap=8 * 1024 * 1024):
    a = dict(arg or {})
    if data is not None:
        a["stdin"] = True
    py = (f"python3 -c {shlex.quote(_FS_PY)} {shlex.quote(op)} "
          f"{shlex.quote(json.dumps(a))} {int(cap)}")
    if data is None:
        return "exec " + py + " </dev/null\n"
    b64 = base64.b64encode(data).decode("ascii")
    lines = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))
    return f"exec {py} <<'{_HEREDOC}'\n{lines}\n{_HEREDOC}\n"


async def fs(t, op, arg, data=None, runner=None, timeout=60, cap=8 * 1024 * 1024):
    rc, out, err = await run(t, fs_script(op, arg, data, cap), runner, timeout)
    try:
        return json.loads((out or "").strip().splitlines()[-1])
    except Exception:
        return {"error": (err or out or f"rc={rc}").strip()[-300:]}


# ── serve: discovery + ssh -L forward over the existing mux ───────────────────
_SERVE_PY = r'''
import glob, json, os, re, subprocess, sys
S = sys.argv[1]
def own(pid):
    try:
        return os.stat("/proc/%d" % int(pid)).st_uid == os.getuid()
    except Exception:
        return False
def cmd(pid):
    try:
        return open("/proc/%d/cmdline" % int(pid), "rb").read().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:
        return ""
files = sorted(glob.glob(os.path.join(S, "ac-serve-*.json")),
               key=lambda p: (not p.endswith("-station.json"), p))
for f in files:
    try:
        d = json.load(open(f))
        pid, port = int(d.get("pid") or 0), int(d.get("port") or 0)
    except Exception:
        continue
    if port and pid and own(pid) and "serve" in cmd(pid):
        print(json.dumps({"ok": True, "port": port, "pid": pid, "source": f})); sys.exit(0)
try:
    out = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, timeout=5).stdout
except Exception:
    out = ""
for ln in out.splitlines():
    m = re.search(r"(?:127\.0\.0\.1|\[::1\]|\*|0\.0\.0\.0):(\d+)\s.*pid=(\d+)", ln)
    if not m:
        continue
    port, pid = int(m.group(1)), int(m.group(2))
    c = cmd(pid)
    if own(pid) and "abstract-claude" in c and " serve" in c:
        print(json.dumps({"ok": True, "port": port, "pid": pid, "source": "ss"})); sys.exit(0)
print(json.dumps({"ok": False, "error": "no abstract-claude serve owned by " + os.environ.get("USER", "this user") + " is listening"}))
'''


def serve_probe_script():
    return (STATE_SH + f"exec python3 - \"$S\" <<'{_HEREDOC}'\n" + _SERVE_PY + f"\n{_HEREDOC}\n")


async def probe_serve(t, runner=None, timeout=20):
    rc, out, err = await run(t, serve_probe_script(), runner, timeout)
    try:
        return json.loads((out or "").strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "error": (err or out or f"rc={rc}").strip()[-300:]}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_open(port, host="127.0.0.1", timeout=0.3):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def forward_argv(t, lport, rport):
    """ssh -O forward on the target's mux master: the forward lives in the
    existing connection (no second login)."""
    if t.kind != KIND_SSH:
        raise ValueError("forward needs an ssh target")
    base, dest = t.ssh_argv[:-1], t.ssh_argv[-1]
    return base + ["-O", "forward", "-L", f"127.0.0.1:{int(lport)}:127.0.0.1:{int(rport)}", dest]


class ServeForwards:
    """locus -> local forwarded port to that locus's serve. Re-established on
    demand (the mux master idles out with ControlPersist)."""

    def __init__(self):
        self.fw = {}        # name -> {"rport", "lport", "ts"}

    async def ensure(self, t, rport, runner=None, is_open=port_open, timeout=20):
        """-> local port (int) reaching 127.0.0.1:<rport> on the target."""
        if t.kind == KIND_SELF:
            return int(rport)
        if t.kind != KIND_SSH:
            raise LocusError("serve forward needs an ssh locus (lxd guests are reached directly)")
        runner = runner or default_runner
        cur = self.fw.get(t.name)
        if cur and cur["rport"] == int(rport) and is_open(cur["lport"]):
            return cur["lport"]
        # bring up (or reuse) the mux master, then add the forward to it
        rc, out, err = await run(t, "true\n", runner, timeout)
        if rc != 0:
            raise LocusError(f"ssh to {t.name} failed: " + (err or out or f"rc={rc}").strip()[-200:])
        lport = cur["lport"] if cur and cur["rport"] == int(rport) else free_port()
        rc, out, err = await runner(forward_argv(t, lport, rport), None, timeout)
        if rc != 0:
            lport = free_port()
            rc, out, err = await runner(forward_argv(t, lport, rport), None, timeout)
        if rc != 0:
            raise LocusError(f"ssh -L forward to {t.name}:{rport} failed: "
                             + (err or out or f"rc={rc}").strip()[-200:])
        self.fw[t.name] = {"rport": int(rport), "lport": int(lport), "ts": time.time()}
        return int(lport)

    def get(self, name):
        cur = self.fw.get(name)
        return cur["lport"] if cur else None


# ── 1.0.144: a serve on ANY locus kind — stdio bridge forward (lxd guests) ─────
# An ssh locus gets `ssh -O forward -L` on its mux. An LXD guest has no ssh
# leg, and its serve listens on the guest's own loopback; the bridge is a local
# listener on this host whose every accepted connection runs ONE short python
# relay inside the guest as the seat user (`lxc exec … python3 -c BRIDGE port`)
# and pumps bytes both ways. Same contract as ServeForwards.ensure -> local port.
BRIDGE_PY = (
    "import os,socket,sys,threading\n"
    "s=socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=10);s.settimeout(None)\n"
    "def up():\n"
    " while True:\n"
    "  b=os.read(0,65536)\n"
    "  if not b:break\n"
    "  s.sendall(b)\n"
    " try:s.shutdown(socket.SHUT_WR)\n"
    " except OSError:pass\n"
    "threading.Thread(target=up,daemon=True).start()\n"
    "o=sys.stdout.buffer\n"
    "while True:\n"
    " b=s.recv(65536)\n"
    " if not b:break\n"
    " o.write(b);o.flush()\n")


def bridge_argv(t, rport):
    """argv that connects stdio to 127.0.0.1:<rport> ON the target."""
    py = ["python3", "-c", BRIDGE_PY, str(int(rport))]
    if t.kind == KIND_LXC:
        return ["lxc", "exec", t.name, "--", "sudo", "-u", t.lxc_user, "-H"] + py
    if t.kind == KIND_SSH:
        return list(t.ssh_argv) + [" ".join(shlex.quote(a) for a in py)]
    return py


class BridgeForwards:
    """locus -> a local listener whose connections are bridged into the target
    (bridge_argv). Used for LXD guests; lives as long as the station."""

    def __init__(self, spawn=None):
        self.fw = {}        # name -> {"rport", "lport", "server"}
        self._spawn = spawn or asyncio.create_subprocess_exec

    async def _pipe(self, t, rport, reader, writer):
        try:
            proc = await self._spawn(*bridge_argv(t, rport), stdin=asyncio.subprocess.PIPE,
                                     stdout=asyncio.subprocess.PIPE,
                                     stderr=asyncio.subprocess.DEVNULL)
        except OSError:
            writer.close()
            return

        async def a2b():
            try:
                while True:
                    b = await reader.read(65536)
                    if not b:
                        break
                    proc.stdin.write(b)
                    await proc.stdin.drain()
            except (ConnectionError, OSError):
                pass
            with contextlib.suppress(Exception):
                proc.stdin.close()

        async def b2a():
            try:
                while True:
                    b = await proc.stdout.read(65536)
                    if not b:
                        break
                    writer.write(b)
                    await writer.drain()
            except (ConnectionError, OSError):
                pass
            with contextlib.suppress(Exception):
                writer.close()

        try:
            await asyncio.gather(a2b(), b2a())
        finally:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    async def ensure(self, t, rport, runner=None, is_open=port_open, timeout=20):
        if t.kind == KIND_SELF:
            return int(rport)
        cur = self.fw.get(t.name)
        if cur and cur["rport"] == int(rport) and cur["server"].is_serving():
            return cur["lport"]
        if cur:
            with contextlib.suppress(Exception):
                cur["server"].close()
        server = await asyncio.start_server(
            lambda r, w: self._pipe(t, rport, r, w), "127.0.0.1", 0)
        lport = server.sockets[0].getsockname()[1]
        self.fw[t.name] = {"rport": int(rport), "lport": int(lport), "server": server}
        return int(lport)

    def get(self, name):
        cur = self.fw.get(name)
        return cur["lport"] if cur else None


# ── 1.0.144: the target's own tmux seats (the ⌂ seat dot is THEIR state) ──────
def seats_script():
    """List the target's sessions on ITS console socket — as the target user,
    so `tmux -L console` resolves that user's own /tmp/tmux-<uid>/console."""
    return ("command -v tmux >/dev/null 2>&1 || { echo '__notmux'; exit 0; }\n"
            "tmux -L " + TMUX_SOCK + " list-sessions -F "
            "'#{session_name}\t#{session_attached}\t#{session_created}' 2>/dev/null || true\n")


def parse_seats(out):
    """-> [{name, attached, created}] ('__notmux' -> [])."""
    rows = []
    for ln in (out or "").splitlines():
        parts = ln.split("\t")
        if len(parts) != 3 or not parts[0]:
            continue
        try:
            att, created = int(parts[1] or 0), int(parts[2] or 0)
        except ValueError:
            att, created = 0, 0
        rows.append({"name": parts[0], "attached": att > 0, "created": created})
    return rows


async def list_seats(t, runner=None, timeout=15):
    """-> (ok, [seat...], err)."""
    rc, out, err = await run(t, seats_script(), runner, timeout)
    if rc != 0:
        return False, [], (err or out or f"rc={rc}").strip()[-200:]
    return True, parse_seats(out), ""


# ── 1.0.144: provision a standing serve ON the target (its own user) ─────────
def provision_script(text, instance="station", locus="", port=None, linger_user=""):
    """The shipped resources/bin/station-serve-provision, parameterised by env
    at the top and shipped on stdin. linger_user (lxd guests only — the lxc
    transport is root in the guest) enables lingering first."""
    head = ["export PROVISION_INSTANCE=" + shlex.quote(instance or "station")]
    if locus:
        head.append("export PROVISION_LOCUS=" + shlex.quote(locus))
    if port:
        head.append("export PROVISION_PORT=" + str(int(port)))
    return "\n".join(head) + "\n" + text


async def serve_provision(t, text, instance="station", locus="", port=None, runner=None,
                          timeout=900):
    """Run station-serve-provision on the target as its seat user.
    -> the script's JSON result (+ log = its stderr tail)."""
    runner = runner or default_runner
    if t.kind == KIND_LXC:
        # the guest's seat user needs a user manager for the USER unit; only
        # the guest's root can grant it, and the lxc transport IS guest root.
        await runner(["lxc", "exec", t.name, "--", "loginctl", "enable-linger", t.lxc_user],
                     None, 30)
    rc, out, err = await run(t, provision_script(text, instance, locus or t.locus, port),
                             runner, timeout)
    doc = None
    for ln in reversed((out or "").strip().splitlines()):
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                doc = json.loads(ln)
                break
            except ValueError:
                continue
    if not isinstance(doc, dict):
        doc = {"ok": False, "error": (err or out or f"rc={rc}").strip()[-400:]}
    doc["log"] = (err or "").strip()[-3000:]
    return doc


# ── 1.0.146 (t4250 / t4260): the standing roles' serve config on a locus ───────
# "Every locus has a keeper that knows its locus": the station ships, into the
# locus's AC_ROOT/config.json, the permission mode + cwd its STANDING sessions
# (keeper / chat / worker) run with — hugpy's serve had none, so every Bash
# call waited 30 min for a permission answer nobody could give (audit S4).
# Only MISSING keys are added (an operator's values are never overwritten); the
# script also reports the facts the keeper directive's LOCUS NUANCES block needs
# (home, gate, user units, serve env). "~" in a value is the TARGET user's $HOME.
_STANDING_PY = r'''
import glob, json, os, shutil, subprocess, sys
S = sys.argv[1]
ADD = json.loads(sys.argv[2])
home = os.path.expanduser("~")
root = ""
for f in sorted(glob.glob(os.path.join(S, "env", "serve-*.env"))):
    try:
        for ln in open(f, encoding="utf-8"):
            if ln.startswith("AC_ROOT="):
                root = ln.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
# never the CALLING process's AC_ROOT (a station running the script on itself
# would otherwise write its own serve root): the target's serve env, else <state>/abstract-claude
root = root or os.path.join(S, "abstract-claude")
path = os.path.join(root, "config.json")
try:
    cfg = json.load(open(path, encoding="utf-8"))
    assert isinstance(cfg, dict)
except Exception:
    cfg = {}
def fill(v):
    return home if v == "~" else (v.replace("~/", home + "/", 1) if isinstance(v, str) and v.startswith("~/") else v)
added = []
for k, v in ADD.items():
    if isinstance(v, dict):
        cur = cfg.get(k) if isinstance(cfg.get(k), dict) else {}
        for sk, sv in v.items():
            if sk not in cur:
                cur[sk] = fill(sv); added.append("%s.%s" % (k, sk))
        cfg[k] = cur
    elif k not in cfg:
        cfg[k] = fill(v); added.append(k)
if added:
    os.makedirs(root, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True); fh.write("\n")
    os.replace(tmp, path)
def sh(*a):
    try:
        return subprocess.run(list(a), capture_output=True, text=True, timeout=8).stdout.strip()
    except Exception:
        return ""
units = [ln.split()[0] for ln in sh("systemctl", "--user", "list-units", "--all", "--no-legend", "--plain",
                                     "abstract-claude-serve@*", "hugpy-station*", "hugpy-gate*").splitlines() if ln.split()]
gate = shutil.which("hugpy-gate") or ""
sudo_n = sh("sudo", "-n", "-l") != "" if shutil.which("sudo") else False
print(json.dumps({"ok": True, "path": path, "added": added, "present": sorted(cfg.keys()), "home": home,
                  "user": os.environ.get("USER") or "", "state": S, "root": root, "gate": gate,
                  "sudo_n": bool(sudo_n), "units": units,
                  "trees": [p for p in (os.path.join(home, "src"), "/srv/pyit/dev", "/srv/hugpy/src") if os.path.isdir(p)]}))
'''


def standing_config_script(add):
    """Merge-only write of ADD into the target's AC_ROOT/config.json + the locus facts."""
    return (STATE_SH + "exec python3 - \"$S\" " + shlex.quote(json.dumps(add))
            + f" <<'{_HEREDOC}'\n" + _STANDING_PY + f"\n{_HEREDOC}\n")


async def standing_config(t, add, runner=None, timeout=30):
    """-> the script's JSON ({ok, path, added, home, gate, units, ...}); raises LocusError."""
    rc, out, err = await run(t, standing_config_script(add), runner, timeout)
    try:
        doc = json.loads((out or "").strip().splitlines()[-1])
        assert isinstance(doc, dict) and doc.get("ok")
        return doc
    except Exception:
        raise LocusError(f"standing config not written on {t.name or 'this host'}: "
                         + ((err or out or f"rc={rc}").strip()[-300:]))
