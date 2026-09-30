"""1.0.128: station-serve-update — the station release owns the abstract-claude serve.

No network, no real systemd: systemctl, curl and the serve venv are fakes on PATH.
"""
import importlib.util
import os
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "resources" / "bin"
UPDATE = BIN / "station-serve-update"
FIX = Path(__file__).resolve().parent / "fixtures"

_loader = SourceFileLoader("ssu_parse", str(BIN / "station-serve-update-parse"))
_spec = importlib.util.spec_from_loader("ssu_parse", _loader)
parse_mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(parse_mod)


def test_parse_keeper_launcher():
    v = parse_mod.parse((FIX / "keeper-run-station.sh").read_text())
    assert v["AC_PORT"] == "9124" and v["AC_UI_BASE"] == "/ac" and v["EXCHANGE_LOCUS"] == "keeper"
    assert v["ABSTRACT_CLAUDE_BIN"] == "/srv/vm_mgr/abstract-claude-serve/venv/bin/abstract-claude"
    assert v["AC_UI_DIST"] == "/srv/vm_mgr/abstract-claude-serve/webui-station" and "AC_UI_BAKE" not in v
    assert not any("TOKEN" in k or "KEY" in k for k in v)


def test_parse_hugpy_launcher():
    v = parse_mod.parse((FIX / "hugpy-run-station.sh").read_text())
    assert v["AC_PORT"] == "9125" and v["AC_UI_BASE"] == "" and v["AC_UI_BAKE"] == "0"
    assert v["AC_ROOT"] == "/srv/hugpy/hugpy-station/abstract-claude"          # $STATE expanded
    assert v["AC_B_GATEWAY"] == "https://dev.hugpy.ai/api" and v["EXCHANGE_LOCUS"] == "hugpy"


def _world(tmp, legacy=True, pins_ok=False, busy_polls=2, marker=False):
    """A fake locus: systemctl/curl on PATH, a serve venv, a state dir."""
    t = Path(tmp)
    fb, state, units = t / "fakebin", t / "state", t / "units"
    for d in (fb, state / "env", units, state / "abstract-claude"):
        d.mkdir(parents=True, exist_ok=True)
    calls = t / "calls"
    live = "abstract-claude-serve-station.service" if legacy else "abstract-claude-serve@station.service"
    run = FIX / "keeper-run-station.sh"
    (fb / "systemctl").write_text(f'''#!/bin/bash
echo "$@" >> {calls}
shift  # --user
case "$1" in
  is-enabled|is-active) u="${{@: -1}}"; [ "$u" = "{live}" ] && exit 0; exit 1 ;;
  show) echo "{{ path={run} ; argv[]={run} }}"; exit 0 ;;
esac
exit 0
''')
    cnt = t / "curl-count"
    (fb / "curl").write_text(f'''#!/bin/bash
n=$(cat {cnt} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt}
case "$*" in *"-w"*) echo -n 200; exit 0 ;; esac
if [ $n -le {busy_polls} ]; then echo '{{"busy": {{"busy": true}}}}'; else echo '{{"busy": {{"busy": false}}}}'; fi
''')
    (fb / "systemd-run").write_text(f'#!/bin/bash\necho systemd-run "$@" >> {calls}\n')
    venv = t / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "abstract-claude").write_text("#!/bin/sh\n")
    (venv / "python").write_text("#!/bin/sh\ncat >/dev/null\n" + ("exit 0\n" if pins_ok else "echo 'abstract-claude 0.1.61!=0.1.64'; exit 1\n"))
    (venv / "pip").write_text(f'#!/bin/sh\necho pip "$@" >> {calls}\n')
    for f in list(fb.iterdir()) + list(venv.iterdir()):
        f.chmod(0o755)
    (state / "env" / "serve-station.env").write_text(f'ABSTRACT_CLAUDE_BIN="{venv}/abstract-claude"\nAC_PORT="9999"\n')
    if marker:
        import hashlib
        sha = hashlib.sha256((BIN / "abstract-claude-serve-run").read_bytes()).hexdigest()[:16]
        (state / "abstract-claude" / ".serve-run-station").write_text("runner=%s\napp=x\n" % sha)
    if not legacy:
        (units / "abstract-claude-serve@.service").write_bytes(
            (ROOT / "resources" / "systemd" / "abstract-claude-serve@.service").read_bytes())
    env = dict(os.environ, PATH="%s:%s" % (fb, os.environ["PATH"]), HUGPY_STATION_STATE=str(state),
               STATION_UNIT_DIR=str(units), STATION_SERVE_POLL_S="1", STATION_SERVE_IDLE_S="2",
               STATION_SERVE_VERIFY_S="0", STATION_SERVE_MAX_WAIT_S="30", STATION_SERVE_UPDATE_INLINE="1",
               STATION_CONSOLE_TOOLSERVER="", STATION_CONSOLE_TOOLSERVER_TOKEN="", HUGPY_OPERATOR_TOKEN="")
    return t, env, calls, state, units


def _run(env):
    return subprocess.run(["bash", str(UPDATE), "--instance", "station"], capture_output=True, text=True,
                          env=env, timeout=120)


def test_up_to_date_serve_is_left_alone(tmp_path):
    t, env, calls, state, units = _world(tmp_path, legacy=False, pins_ok=True, marker=True)
    r = _run(env)
    assert "serve up to date" in r.stderr
    assert "restart" not in calls.read_text() and "pip" not in calls.read_text()


def test_legacy_locus_is_migrated_then_switched_at_idle(tmp_path):
    t, env, calls, state, units = _world(tmp_path, legacy=True, busy_polls=2)
    (state / "env" / "serve-station.env").unlink()                   # force the migration
    r = _run(env)
    senv = (state / "env" / "serve-station.env").read_text()
    assert 'AC_PORT="9124"' in senv and (units / "abstract-claude-serve@.service").exists()
    c = calls.read_text()
    assert "restart due: legacy unit abstract-claude-serve-station.service is live" in r.stderr
    assert "waiting for an idle serve" in r.stderr
    assert c.index("disable --now abstract-claude-serve-station.service") < c.index("restart abstract-claude-serve@station.service")
    assert int((t / "curl-count").read_text()) >= 4                  # it polled busy twice, then idle


def test_module_drift_provisions_then_restarts_never_mid_turn(tmp_path):
    t, env, calls, state, units = _world(tmp_path, legacy=False, pins_ok=False, marker=True, busy_polls=3)
    r = _run(env)
    c = calls.read_text()
    assert "modules differ from pins" in r.stderr
    assert c.index("pip install -U -r") < c.index("restart abstract-claude-serve@station.service")
    assert "FORCED" not in r.stderr


def test_defer_flag_migrates_files_only(tmp_path):
    t, env, calls, state, units = _world(tmp_path, legacy=False, pins_ok=False)
    (state / "env" / "serve-station.defer").write_text("keeper: every agent runs inside this serve\n")
    r = _run(env)
    assert "DEFERRED" in r.stderr
    c = calls.read_text()
    assert "restart" not in c and "pip" not in c and "systemd-run" not in c


def test_max_wait_restarts_anyway_and_says_so(tmp_path):
    t, env, calls, state, units = _world(tmp_path, legacy=False, pins_ok=False, marker=True, busy_polls=10_000)
    env["STATION_SERVE_MAX_WAIT_S"] = "3"
    r = _run(env)
    assert "FORCED after 3s busy" in r.stderr and "restart abstract-claude-serve@station.service" in calls.read_text()
    assert "keeper not mailed" in r.stderr                           # no creds in the test: logged, not silent


def test_scheduled_waiter_goes_through_systemd_run(tmp_path):
    t, env, calls, state, units = _world(tmp_path, legacy=False, pins_ok=False, marker=True)
    env.pop("STATION_SERVE_UPDATE_INLINE")
    _run(env)
    c = calls.read_text()
    assert "systemd-run --user --unit=station-serve-update-station --collect" in c and "--waiter" in c
    assert "restart abstract-claude-serve@station.service" not in c   # the caller never restarts inline
