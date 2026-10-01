"""1.0.141 — locus_exec: ONE code path to act on a locus.

The unified launcher is tested with a FAKE ssh runner (no ssh, no tmux is ever
started), plus real local `bash -s` runs of the generated scripts against a
temp state dir/HOME so the scripts are proven to do what they claim. Nothing
here launches the station, kills a process, or touches the network.
Run:  python3 -m pytest resources/backend/test_locus_exec.py
"""
import asyncio
import base64
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import locus_exec as LX  # noqa: E402

SSH_ARGV = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=8", "-p", "22", "-o", "ControlMaster=auto",
            "-o", "ControlPath=/home/st/.config/hugpy-station/ssh-mux/%C",
            "-o", "ControlPersist=120", "-o", "BatchMode=yes", "remote@10.0.0.9"]
SSH_TTY = "ssh -t -o StrictHostKeyChecking=accept-new -p 22 remote@10.0.0.9"
BIG = "D" * 20_000          # a 20 KB directive


class FakeRunner:
    """Records every (argv, stdin); answers the materialize script with a path."""

    def __init__(self, path="/home/remote/.config/hugpy-station/seat-launch/keeper-claude-1.sh"):
        self.calls, self.path = [], path

    async def __call__(self, argv, stdin=None, timeout=30):
        self.calls.append((list(argv), stdin or b""))
        return 0, self.path + "\n", ""


def _ssh_target():
    return LX.Target(LX.KIND_SSH, "keeper", "keeper", ssh_argv=SSH_ARGV, ssh_tty=SSH_TTY)


def _self_target():
    return LX.Target(LX.KIND_SELF, "", "op")


def _argv_bytes(cmd):
    return len(cmd.encode("utf-8"))


def test_remote_seat_launch_tmux_argv_under_1kb_with_20kb_directive():
    body = "__SYSP=" + shlex.quote(BIG) + "; exec claude --append-system-prompt \"$__SYSP\""
    fr = FakeRunner()
    cmd, path = asyncio.run(LX.seat_launch(_ssh_target(), "keeper-claude", body, runner=fr,
                                           opts="set -g status off \\; "))
    # the materialize script went over ssh STDIN, the argv is just `ssh ... bash -s`
    argv, stdin = fr.calls[0]
    assert argv == SSH_ARGV + ["bash", "-s"]
    assert base64.b64encode(LX.seat_script_text("keeper-claude", body).encode()).decode()[:200] in \
        stdin.decode().replace("\n", "")
    # the PTY command: ssh -t ... -- '<short tmux line>' — no payload anywhere in it
    assert cmd.startswith(SSH_TTY + " -- ")
    assert BIG[:64] not in cmd and "base64" not in cmd
    inner = shlex.split(cmd)[-1]                       # what the remote shell runs
    assert "new-session -A -s keeper-claude" in inner and "bash " + path in inner
    tmux_line = inner.split("exec ", 1)[1].split("; else", 1)[0]
    assert tmux_line.startswith("tmux -L console ")
    assert _argv_bytes(tmux_line) < 1024, _argv_bytes(tmux_line)
    assert _argv_bytes(inner) < 1024
    assert _argv_bytes(cmd) < 1024, _argv_bytes(cmd)
    assert path == fr.path


def test_self_and_remote_run_the_identical_script():
    """ONE code path: the script shipped to the target is byte-identical for the
    host and an ssh locus; only the transport argv differs."""
    body = "echo hi"
    a, b = FakeRunner(), FakeRunner()
    asyncio.run(LX.seat_launch(_self_target(), "keeper-claude", body, runner=a, stamp="S"))
    asyncio.run(LX.seat_launch(_ssh_target(), "keeper-claude", body, runner=b, stamp="S"))
    assert a.calls[0][1] == b.calls[0][1]
    assert a.calls[0][0] == ["bash", "-s"]
    assert b.calls[0][0][-2:] == ["bash", "-s"] and b.calls[0][0][0] == "ssh"


def test_self_launch_keeps_scope_prefix_and_is_bash_wrapped():
    fr = FakeRunner("/st/seat-launch/keeper-claude-1.sh")
    cmd, _p = asyncio.run(LX.seat_launch(_self_target(), "keeper-claude", "echo x", runner=fr,
                                         pre="systemd-run --user --scope"))
    assert shlex.split(cmd)[0] == "bash"               # argv0 resolvable by shutil.which
    assert "systemd-run --user --scope tmux -L console" in cmd
    # a remote target never gets the HOST's scope prefix
    fr2 = FakeRunner()
    cmd2, _ = asyncio.run(LX.seat_launch(_ssh_target(), "keeper-claude", "echo x", runner=fr2,
                                         pre="systemd-run --user --scope"))
    assert "systemd-run" not in cmd2


def test_materialize_failure_never_falls_back_inline():
    async def bad(argv, stdin=None, timeout=30):
        return 255, "", "Permission denied (publickey)"
    try:
        asyncio.run(LX.seat_launch(_ssh_target(), "keeper-claude", BIG, runner=bad))
    except LX.LocusError as e:
        assert "Permission denied" in str(e)
    else:
        raise AssertionError("expected LocusError")


def _local_runner(home, state=None):
    env = dict(os.environ, HOME=str(home))
    env.pop("HUGPY_STATION_STATE", None)
    if state:
        env["HUGPY_STATION_STATE"] = str(state)

    async def run(argv, stdin=None, timeout=30):
        return await LX.default_runner(argv, stdin, timeout, env=env)
    return run


def test_materialize_script_really_writes_the_file(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    body = "echo " + shlex.quote(BIG)
    path = asyncio.run(LX.seat_materialize(_self_target(), "keeper-claude", body,
                                           runner=_local_runner(home), stamp="T1"))
    p = Path(path)
    assert p.parent == home / ".config" / "hugpy-station" / "seat-launch"
    assert p.read_text() == LX.seat_script_text("keeper-claude", body)
    assert oct(p.stat().st_mode & 0o777) == "0o700"
    # the state pointer and $HUGPY_STATION_STATE are honoured the same way
    st = tmp_path / "state"
    path2 = asyncio.run(LX.seat_materialize(_self_target(), "s", "true",
                                            runner=_local_runner(home, st), stamp="T2"))
    assert Path(path2).parent == st / "seat-launch"
    ptr = home / ".config" / "hugpy-station" / "state-dir"
    alt = tmp_path / "alt"
    ptr.write_text(str(alt) + "\n")
    path3 = asyncio.run(LX.seat_materialize(_self_target(), "s", "true",
                                            runner=_local_runner(home), stamp="T3"))
    assert Path(path3).parent == alt / "seat-launch"


def test_seat_config_roundtrip_on_target(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    st = home / "hugpy-station"; (st / "env").mkdir(parents=True)   # the vm_mgr layout
    (st / "frontier-directive.md").write_text("REMOTE DIRECTIVE")
    (st / "frontier-models.json").write_text(json.dumps({"claude-code": "opus-x"}))
    r = _local_runner(home)
    cfg = asyncio.run(LX.read_cfg(_self_target(), runner=r))
    assert cfg["state"] == str(st)
    assert cfg["files"]["directive"] == "REMOTE DIRECTIVE"
    assert json.loads(cfg["files"]["models"])["claude-code"] == "opus-x"
    assert "handoff" not in cfg["files"]            # 1.0.146: the handoff is a toolserver row, never a file
    asyncio.run(LX.write_cfg(_self_target(), "directive", "next steps: " + BIG, runner=r))
    assert (st / "frontier-directive.md").read_text().startswith("next steps: DDD")
    asyncio.run(LX.write_cfg(_self_target(), "directive", None, runner=r))
    assert not (st / "frontier-directive.md").exists()
    # an override path (the host's active mct workspace guidance)
    (st / "mct" / "s2").mkdir(parents=True)
    (st / "mct" / "s2" / "operator-guidance.user.md").write_text("G2")
    cfg = asyncio.run(LX.read_cfg(_self_target(), runner=r,
                                  overrides={"guidance": "mct/s2/operator-guidance.user.md"}))
    assert cfg["files"]["guidance"] == "G2"


def test_tmux_payload_rides_stdin_not_argv():
    fr = FakeRunner()
    text = "x" * 5000
    asyncio.run(LX.tmux(_ssh_target(), ["send-keys", "-t", "=keeper-claude:", "-l", "--", text], runner=fr))
    argv, stdin = fr.calls[0]
    assert argv == SSH_ARGV + ["bash", "-s"]
    assert text in stdin.decode()
    assert sum(len(a) for a in argv) < 1024


def test_fs_is_fenced_to_home(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    (home / "a.txt").write_text("hello")
    r = _local_runner(home)
    t = _self_target()
    d = asyncio.run(LX.fs(t, "list", {"path": ""}, runner=r))
    assert d["path"] == str(home) and [e["name"] for e in d["entries"]] == ["a.txt"]
    assert asyncio.run(LX.fs(t, "read", {"path": "a.txt"}, runner=r))["text"] == "hello"
    assert "outside the locus home" in asyncio.run(LX.fs(t, "read", {"path": "/etc/passwd"}, runner=r))["error"]
    assert "outside" in asyncio.run(LX.fs(t, "read", {"path": "../x"}, runner=r))["error"]
    w = asyncio.run(LX.fs(t, "write", {"path": "d/b.bin"}, data=b"\x00\x01" * 50000, runner=r))
    assert w["ok"] and (home / "d" / "b.bin").read_bytes() == b"\x00\x01" * 50000
    assert "refusing" in asyncio.run(LX.fs(t, "delete", {"path": ""}, runner=r))["error"]


def test_probe_serve_finds_own_published_serve(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    st = tmp_path / "state"; st.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "abstract-claude", "serve"])
    try:
        (st / "ac-serve-station.json").write_text(json.dumps({"port": 9124, "pid": proc.pid}))
        d = asyncio.run(LX.probe_serve(_self_target(), runner=_local_runner(home, st)))
        assert d["ok"] and d["port"] == 9124 and d["pid"] == proc.pid
    finally:
        proc.terminate(); proc.wait(timeout=10)       # our own child only
    # a dead pid is not a serve
    d = asyncio.run(LX.probe_serve(_self_target(), runner=_local_runner(home, st)))
    assert d.get("port") != 9124 or not d.get("ok")


def test_serve_forward_over_the_mux():
    fr = FakeRunner()
    fw = LX.ServeForwards()
    opened = set()

    async def runner(argv, stdin=None, timeout=30):
        fr.calls.append((argv, stdin))
        if "-O" in argv:
            opened.add(int(argv[argv.index("-L") + 1].split(":")[1]))
        return 0, "", ""
    lport = asyncio.run(fw.ensure(_ssh_target(), 9124, runner=runner, is_open=lambda p: p in opened))
    fwd = [a for a, _ in fr.calls if "-O" in a][0]
    assert fwd[:len(SSH_ARGV) - 1] == SSH_ARGV[:-1] and fwd[-1] == SSH_ARGV[-1]
    assert fwd[fwd.index("-O") + 1] == "forward"
    assert fwd[fwd.index("-L") + 1] == f"127.0.0.1:{lport}:127.0.0.1:9124"
    n = len(fr.calls)
    assert asyncio.run(fw.ensure(_ssh_target(), 9124, runner=runner, is_open=lambda p: p in opened)) == lport
    assert len(fr.calls) == n                           # live forward reused, no new ssh
    assert asyncio.run(fw.ensure(_self_target(), 9124, runner=runner)) == 9124


if __name__ == "__main__":
    import tempfile
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print("ok", name)
