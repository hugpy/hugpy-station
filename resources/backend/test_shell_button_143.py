"""1.0.143 — the ⌂ shell button: a terminal on the CURRENT locus as that
locus's own user, resolved through the 1.0.141 locus transport.

Exercises the REAL server.py functions (AST-extracted with the 1.0.141 test's
loader): _shell_plan (where the shell runs + the PTY record key) and the
`shell` block of GET /api/term/backends (who / available / reason) with a fake
locus probe. No process is started; no network.
Run:  python3 -m pytest resources/backend/test_shell_button_143.py
"""
import asyncio
import getpass
import json
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_remote_locus_141 as T141  # noqa: E402

MINE = {"_shell_plan", "_self_who", "term_backends", "_vm_which"}
ME = getpass.getuser() + "@" + socket.gethostname().split(".")[0]
# the whitelist shape (the real dict needs half of server.py's constants)
TERM_SURFACES = {
    "local": {"default": "opencode", "backends": {"opencode": "opencode"}},
    "frontier": {"default": "mct", "backends": {"mct": "abstract-claude mct x",
                                                "claude-code": "claude", "codex": "codex"}},
    "shell": {"default": "exec", "backends": {"exec": "lxc", "ssh": "ssh"}},
}


def load(tmp, **kw):
    saved = set(T141.FUNCS)
    T141.FUNCS |= MINE                 # restored below: never leak into the 1.0.141 tests
    try:
        ns = T141.load(tmp, **kw)
    finally:
        T141.FUNCS.clear()
        T141.FUNCS.update(saved)
    ns["socket"] = socket
    ns["TERM_SURFACES"] = TERM_SURFACES
    return ns


def test_host_spellings_all_open_the_host_shell(tmp_path):
    ns = load(tmp_path, station_locus_env="op")
    for vm in ("", "@keeper", "@self", "host", "op"):
        p = ns["_shell_plan"](vm)
        assert p["kind"] == "host", vm
        assert p["key"] == "shell:@keeper", vm          # ONE persistent host shell
        assert p["who"] == ME


def test_ssh_pointer_at_this_very_user_host_is_the_host(tmp_path):
    ns = load(tmp_path, station_locus_env="op")
    ns["_is_self_target"] = lambda t: t == "vm_mgr@192.168.1.100:22"
    p = ns["_shell_plan"]("keeper")                     # vm_mgr@ae as seen from vm_mgr@ae
    assert p["kind"] == "host" and p["key"] == "shell:@keeper"


def test_remote_ssh_locus_and_lxd_guest(tmp_path):
    ns = load(tmp_path, station_locus_env="op")
    p = ns["_shell_plan"]("hugpy")
    assert p["kind"] == "ssh" and p["key"] == "shell-ssh:hugpy"
    assert p["who"] == "hugpy@192.168.1.100"
    assert p["ssh"] == "ssh -t hugpy@192.168.1.100"     # the interactive login (_ssh_shell_cmd)
    g = ns["_shell_plan"]("dev1")                       # not an ssh host -> LXD guest
    assert g["kind"] == "lxc" and g["key"] == "shell:dev1" and g["who"] == "ubuntu@dev1"


def _backends(ns, query, probe):
    class Req:
        pass
    r = Req()
    r.query = query
    ns.update(
        all_locus_names=lambda: _aw(set(T141.REGISTRY) | {"dev1"}),
        MODEL_VM="", FV_BACKENDS={}, _active_session=lambda: "repl",
        _live_surface_session=lambda k: None, _surface_key=lambda *a: "k",
        _frontier_enabled=lambda: True, _b_enabled=lambda: True,
        _keeper_surface_now=lambda vm: _aw(("serve", {"label": "Serve console"})),
        _tmux_available=lambda: True, TMUX_MISSING_MSG="no tmux", TMUX_SEAT_LABELS={},
        _vm_probe=probe, shutil=__import__("shutil"),
        # 1.0.144: term_backends also lists the locus's own tmux seats
        _locus_seats=lambda vm: _aw((True, [{"name": "keeper-claude", "attached": False, "created": 1}], "")),
        _tmux_session_for=lambda s, b: {"claude-code": "keeper-claude"}.get(b, ""))
    resp = asyncio.run(ns["term_backends"](r))
    return json.loads(resp.body)


async def _aw_impl(v):
    return v


def _aw(v):
    return _aw_impl(v)


def test_backends_reports_the_host_shell_user(tmp_path):
    ns = load(tmp_path, station_locus_env="op")
    calls = []

    async def probe(vm, names):
        calls.append(vm)
        return True, {n: True for n in names}, "nobody@x", ""
    doc = _backends(ns, {}, probe)
    assert doc["shell"]["who"] == ME and doc["shell"]["available"] is True
    assert doc["shell"]["kind"] == "host" and doc["shell"]["locus"] == "@keeper"
    assert calls == []                                  # the host needs no probe
    doc = _backends(ns, {"vm": "@keeper"}, probe)
    assert doc["shell"]["who"] == ME and calls == []


def test_backends_reports_the_remote_user_as_the_locus_says(tmp_path):
    ns = load(tmp_path, station_locus_env="op")

    async def probe(vm, names):
        return True, {n: True for n in names}, "hugpy@ae", ""
    doc = _backends(ns, {"vm": "hugpy"}, probe)
    assert doc["shell"] == {**doc["shell"], "who": "hugpy@ae", "available": True,
                            "kind": "ssh", "locus": "hugpy"}


def test_backends_lists_the_locus_own_running_seats(tmp_path):
    """1.0.144: frontier.live is THIS station's attached PTY; the locus's own
    running tmux seats (listed on its console socket, as its user) are
    reported separately so a running keeper-claude never reads as dead."""
    ns = load(tmp_path, station_locus_env="op")

    async def probe(vm, names):
        return True, {n: True for n in names}, "hugpy@ae", ""
    doc = _backends(ns, {"vm": "hugpy"}, probe)
    f = doc["frontier"]
    assert f["live"] is False and f["seat_live"] is True and f["seats_ok"] is True
    assert f["seats"][0]["name"] == "keeper-claude" and f["seat_backends"]["claude-code"] is True
    assert "seats" not in doc                           # inside "frontier", no phantom surface


def test_unreachable_locus_disables_with_a_reason(tmp_path):
    ns = load(tmp_path, station_locus_env="op")

    async def probe(vm, names):
        return False, {n: False for n in names}, "", "ssh: connect to host 10.9.9.9 port 22: No route to host"
    doc = _backends(ns, {"vm": "twin-a"}, probe)
    sh = doc["shell"]
    assert sh["available"] is False and sh["who"] == "x@10.9.9.9"
    assert "cannot reach twin-a" in sh["reason"] and "No route to host" in sh["reason"]
    # still a surface with its backends; no phantom top-level keys
    assert "backends" in sh and "who" not in doc


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
