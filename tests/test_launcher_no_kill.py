"""1.0.139: the launcher never kills a running Station or the headless backend.

1.0.138 VM test: `hugpy-station --serve claude` (and serve-core's auto-open of it
from the Station's own serve) hit the launcher's port-8899 reaper —
`pkill -f hugpy-station/hugpy-station`, `pkill -f resources/backend/server.py`,
`fuser -k -9 8899/tcp` — which killed the running Station AND the headless
hugpy-station-web@ backend. These tests run the REAL launcher against fake
processes on real loopback ports (no Electron, no display, no network):

  * a fake Station = a process whose argv[0] is <app>/hugpy-station, with a fake
    backend child (<app>/resources/backend/server.py) listening on the port;
  * a fake headless backend = the same server.py, not parented by a Station;
  * the launcher's final exec target (<app>/hugpy-station) is a recorder.

Run:  python3 -m pytest tests/test_launcher_no_kill.py
"""
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / "resources" / "hugpy-station-launch"

pytestmark = pytest.mark.skipif(not (shutil.which("ss") and sys.platform.startswith("linux")),
                                reason="needs Linux + ss (iproute2)")

SERVER_PY = r'''
import socket, sys, time, pathlib
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(4)
pathlib.Path(sys.argv[2]).write_text("up")
while True:
    time.sleep(3600)
'''

STATION_SH = r'''
"$PYBIN" "$APP/resources/backend/server.py" "$PORT" "$READY" &
wait
'''


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:   # a zombie is dead for our purposes
        return Path(f"/proc/{pid}/status").read_text().split("State:")[1].split()[0] != "Z"
    except Exception:
        return False


def _wait_file(p, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if p.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{p} never appeared")


@pytest.fixture
def app(tmp_path):
    """A fake install root: the real launcher + a recording 'Electron'."""
    a = tmp_path / "app"
    (a / "resources" / "backend" / "vendor" / "aiohttp").mkdir(parents=True)
    (a / "resources" / "backend" / "vendor" / "aiohttp" / "__init__.py").write_text("")
    (a / "resources" / "backend" / "server.py").write_text(SERVER_PY)
    (a / "resources" / "VERSION").write_text("1.0.139\n")
    shutil.copy(LAUNCH, a / "hugpy-station-launch")
    rec = tmp_path / "exec.rec"
    (a / "hugpy-station").write_text(
        '#!/bin/sh\n{ echo "CONSOLE_PORT=${CONSOLE_PORT:-}"; for x in "$@"; do echo "$x"; done; } > %s\n' % rec)
    for f in ("hugpy-station-launch", "hugpy-station"):
        os.chmod(a / f, 0o755)
    procs = []

    class App:
        root = a
        tmp = tmp_path
        record = rec

        def station(self, port, chromium=False):
            """fake live Station (argv[0] = <app>/hugpy-station) + its backend child.
            chromium=True mimics Electron's rewritten /proc/<pid>/cmdline: ONE
            space-joined string ("<app>/hugpy-station --disable-gpu --no-sandbox")."""
            ready = tmp_path / f"ready-st-{port}"
            sh = tmp_path / "station.sh"
            sh.write_text(STATION_SH)
            argv0 = str(a / "hugpy-station") + (" --disable-gpu --no-sandbox" if chromium else "")
            p = subprocess.Popen([argv0, str(sh)], executable="/bin/bash",
                                 env=dict(os.environ, APP=str(a), PORT=str(port), READY=str(ready),
                                          PYBIN=sys.executable),
                                 start_new_session=True)
            procs.append(p)
            _wait_file(ready)
            kid = int(subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True,
                                     text=True).stdout.split()[0])
            return p.pid, kid

        def backend(self, port, env=None):
            """a station backend NOT parented by a Station (headless unit / orphan)."""
            ready = tmp_path / f"ready-be-{port}"
            p = subprocess.Popen([sys.executable, str(a / "resources" / "backend" / "server.py"),
                                  str(port), str(ready)],
                                 env=dict(os.environ, **(env or {})), start_new_session=True)
            procs.append(p)
            _wait_file(ready)
            return p.pid

        def launch(self, *args, port):
            env = {k: v for k, v in os.environ.items() if k not in ("CONSOLE_PORT", "APPDIR")}
            env.update(DISPLAY=":99", XDG_CACHE_HOME=str(tmp_path / "cache"),
                       HUGPY_STATION_PYTHON=sys.executable, CONSOLE_PORT=str(port))
            r = subprocess.run([str(a / "hugpy-station-launch"), *args], env=env,
                               capture_output=True, text=True, timeout=60)
            log = (tmp_path / "cache" / "hugpy-station-launch.log")
            r.log = log.read_text() if log.exists() else ""
            r.exec_lines = rec.read_text().splitlines() if rec.exists() else None
            return r

    yield App()
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGCONT)
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass


def test_serve_mode_never_touches_a_running_station_or_the_headless_backend(app):
    """The exact 1.0.138 VM failure: station on the port, headless backend beside it."""
    port, headless_port = _free_port(), _free_port()
    st, st_backend = app.station(port)
    headless = app.backend(headless_port)
    r = app.launch("--serve", "claude", port=port)
    assert r.returncode == 0, r.stderr + r.log
    assert _alive(st) and _alive(st_backend) and _alive(headless), r.log
    assert r.exec_lines is not None and "--serve" in r.exec_lines and "claude" in r.exec_lines
    assert r.exec_lines[0] == f"CONSOLE_PORT={port}"      # serve mode never moves the port
    assert "serve mode" in r.log and "reap" not in r.log


def test_station_mode_hands_off_to_a_live_station_without_killing_it(app):
    port = _free_port()
    st, st_backend = app.station(port)
    r = app.launch(port=port)
    assert r.returncode == 0, r.stderr + r.log
    assert _alive(st) and _alive(st_backend), r.log
    assert f"hugpy Station pid {st} is running" in r.log
    # Electron is still exec'd (its single-instance lock focuses the running window);
    # should an old lock-less Station hold the port, the new one cannot collide on it.
    assert r.exec_lines is not None and r.exec_lines[0] != f"CONSOLE_PORT={port}"


def test_chromium_rewritten_cmdline_is_still_recognised_as_the_live_station(app):
    """1.0.139 VM test (pre-release): Electron's main process shows its argv as one
    space-joined string; the launcher missed it and reaped the live Station's own
    backend as 'stale' on a second `hugpy-station`."""
    port = _free_port()
    st, st_backend = app.station(port, chromium=True)
    # main.js marks its backend with its own pid: the marker names a LIVE Station
    r = app.launch(port=port)
    assert r.returncode == 0, r.stderr + r.log
    time.sleep(0.3)
    assert _alive(st) and _alive(st_backend), r.log
    assert f"hugpy Station pid {st} is running" in r.log and "stale" not in r.log


def test_station_mode_leaves_the_headless_backend_alone_and_takes_another_port(app):
    port = _free_port()
    headless = app.backend(port)                   # no Station parent, no desktop marker
    r = app.launch(port=port)
    assert r.returncode == 0, r.stderr + r.log
    assert _alive(headless), r.log
    assert "NOT owned by a desktop Station" in r.log
    moved = int(r.exec_lines[0].split("=", 1)[1])
    assert moved != port and moved > port


def test_station_mode_reaps_only_a_stale_backend_whose_desktop_station_is_gone(app):
    port, other_port = _free_port(), _free_port()
    dead = subprocess.Popen(["true"]); dead.wait()
    stale = app.backend(port, env={"HUGPY_STATION_DESKTOP_PID": str(dead.pid)})
    bystander = app.backend(other_port)             # same script, other port: untouched
    r = app.launch(port=port)
    assert r.returncode == 0, r.stderr + r.log
    time.sleep(0.3)
    assert not _alive(stale), r.log
    assert _alive(bystander), r.log
    assert r.exec_lines[0] == f"CONSOLE_PORT={port}"


def test_a_marked_backend_whose_station_is_alive_is_never_reaped(app):
    """the marker names a LIVE Station -> not stale, whoever its parent is."""
    st, _kid = app.station(_free_port())
    port = _free_port()
    marked = app.backend(port, env={"HUGPY_STATION_DESKTOP_PID": str(st)})
    r = app.launch(port=port)
    assert r.returncode == 0, r.stderr + r.log
    assert _alive(st) and _alive(marked), r.log
    assert r.exec_lines[0] != f"CONSOLE_PORT={port}"


def test_a_stopped_station_gets_a_clear_message_not_a_kill(app):
    port = _free_port()
    st, st_backend = app.station(port)
    os.kill(st, signal.SIGSTOP)
    try:
        time.sleep(0.2)
        r = app.launch(port=port)
    finally:
        os.kill(st, signal.SIGCONT)
    assert r.returncode == 4
    assert "STOPPED" in r.stderr and str(st) in r.stderr
    assert _alive(st) and _alive(st_backend)
    assert r.exec_lines is None                     # no second Electron


def test_free_port_is_used_as_is(app):
    port = _free_port()
    r = app.launch(port=port)
    assert r.returncode == 0, r.stderr + r.log
    assert r.exec_lines[0] == f"CONSOLE_PORT={port}"


def test_launcher_has_no_name_based_or_port_based_kill():
    src = LAUNCH.read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "pkill" not in code and "killall" not in code and "fuser" not in code


def test_main_js_is_single_instance_and_marks_its_backend():
    js = (ROOT / "main.js").read_text()
    assert "requestSingleInstanceLock()" in js and "'second-instance'" in js
    assert "HUGPY_STATION_DESKTOP_PID: String(process.pid)" in js
    # the lock is taken only in Station mode: the --serve branch returns first
    assert js.index("--serve") < js.index("requestSingleInstanceLock()")
