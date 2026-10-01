"""1.0.141 — any station views any locus exactly as that locus's own host does.

Exercises the REAL server.py functions (AST-extracted, like test_locus_central)
for: identity (keeper is a locus key, not "this station"; the ambiguous vm-mgr
endpoint; the ae-vm-mgr legacy alias; no silent keeper fallback), and the
unified seat launch end-to-end with a FAKE ssh runner — the remote seat gets
the TARGET's directive/models/env and the tmux argv stays < 1 KB with a 20 KB
directive. No process is started or killed; no network.
Run:  python3 -m pytest resources/backend/test_remote_locus_141.py
"""
import ast
import asyncio
import base64
import getpass
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

from aiohttp import web

HERE = Path(__file__).resolve().parent
SRC = HERE / "server.py"
sys.path.insert(0, str(HERE))
import locus_exec as LX  # noqa: E402
import directives as DV  # noqa: E402

FUNCS = {
    # identity
    "_canon_locus", "_is_host_vm", "_self_locus_names", "_pick_endpoint_locus", "_central_locus",
    "_station_locus", "_keeper_locus", "_unconfigured_response", "_locus_aliases", "_endpoint",
    "_endpoints_of", "_lxd_ips", "_ssh_hosts_local", "_ssh_hosts", "_ssh_host", "_ac_locus_key",
    # seat config composition + launch
    "_cfg_text", "_cfg_json", "_frontier_handoff_pending", "_frontier_directive_text",
    "_frontier_models", "_frontier_fs_mediated", "_frontier_delegate_doc", "_delegate_section",
    "_claude_seat_system_prompt", "_claude_seat_settings_json", "_a_template_doc",
    "_claude_seat_cmd", "_strip_directive", "_locus_target", "_seat_env_block",
    "_dirv_snapshot", "_dirv_facts", "_session_directive",          # 1.0.143 generator seam
    "_seat_launch_body", "_seat_launch", "_port_open", "_rows_for_locus",
}
CONSTS = {"_HOST_VMS", "HOST_TOKEN", "_LEGACY_LOCUS_ALIASES", "LOCUS_UNCONFIGURED", "_SSH_NAME_RE",
          "AC_MIN_VERSION", "_AC_ENSURE", "_SEAT_TRUST_B64", "_SEAT_ENV_KEYS", "_LOCAL_HUGPY_FRONT",
          "_TMUX_OPTS", "_MODEL_RE", "FRONTIER_MODEL_DEFAULTS", "FS_DENY_TOOLS",
          "_DELEGATE_ONLY_TEXT", "_DELEGATE_OFF_TEXT", "A_DEFAULT_SETTINGS",
          "FRONTIER_DIRECTIVE_PATH", "FRONTIER_HANDOFF_PATH", "FRONTIER_MODELS_PATH",
          "FRONTIER_FS_FLAG", "FRONTIER_DELEGATE_FLAG", "PORT", "DELEGATE_BACKENDS"}
ME = getpass.getuser()
BIG = "R" * 20_000
HOST_DIRECTIVE = "HOST-ONLY DIRECTIVE (must never reach a remote seat)"

# the toolserver feed as op sees it: keeper AND ae-mgr on vm_mgr's endpoint
REGISTRY = {
    "keeper": {"host": "192.168.1.100", "user": "vm_mgr", "port": 22, "kind": "station",
               "goal": "keeper - unified locus label for the vm_mgr keeper station on ae. Legacy alias: ae-vm-mgr."},
    "ae-mgr": {"host": "192.168.1.100", "user": "vm_mgr", "port": 22, "kind": "station",
               "goal": "ssh host added on vm_mgr@ae"},
    "hugpy": {"host": "192.168.1.100", "user": "hugpy", "port": 22, "kind": "station", "goal": "platform"},
    "twin-a": {"host": "10.9.9.9", "user": "x", "port": 22, "kind": "ssh", "goal": ""},
    "twin-b": {"host": "10.9.9.9", "user": "x", "port": 22, "kind": "ssh", "goal": ""},
}


def load(tmp, station_locus_env="", self_name=""):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    nodes = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FUNCS:
            nodes.append(n)
        elif isinstance(n, ast.Assign) and any(getattr(t, "id", "") in CONSTS for t in n.targets):
            nodes.append(n)
    tmp = Path(tmp)
    (tmp / "state").mkdir(exist_ok=True)
    (tmp / "state" / "frontier-directive.md").write_text(HOST_DIRECTIVE)
    (tmp / "state" / "frontier-models.json").write_text(json.dumps({"claude-code": "host-model"}))

    def _read_json(p, default):
        try:
            return json.loads(Path(p).read_text())
        except (OSError, ValueError):
            return default

    env = {"STATION_LOCUS": station_locus_env} if station_locus_env else {}
    fake_os = type("FakeOS", (), {})()
    for k in dir(os):
        if not k.startswith("__"):
            setattr(fake_os, k, getattr(os, k))
    fake_os.environ = env
    ns = dict(
        os=fake_os, re=re, json=json, time=time, getpass=getpass, asyncio=asyncio, shlex=shlex,
        web=web, Path=Path, LX=LX, DV=DV,
        FV_STATE_HOME=tmp / "state", ROOT=tmp, SSH_HOSTS_PATH=tmp / "ssh-hosts.json",
        LOCUS_ALIASES_PATH=tmp / "locus-aliases.json",
        FRONTIER_DIRECTIVE_SHIPPED=tmp / "shipped-directive.md",
        A_SETTINGS_TEMPLATE_PATH=tmp / "state" / "a-settings-template.json",
        A_SETTINGS_SHIPPED=tmp / "shipped-template.json",
        _TS_LOCI={"hosts": dict(REGISTRY), "self": self_name, "lxd": set(), "synced": True},
        _DISCOVER_CACHE={"stations": []}, _read_json=_read_json,
        _local_ips=lambda: {"127.0.0.1", "192.168.1.113"},
        _local_ips_cached=lambda: {"127.0.0.1", "192.168.1.113"},
        _is_self_target=lambda t: False,
        _ssh_argv=lambda h: ["ssh", "-o", "BatchMode=yes", f"{h['user']}@{h['host']}"],
        _ssh_shell_cmd=lambda h: f"ssh -t {h['user']}@{h['host']}",
        _scope_prefix=lambda: ["systemd-run", "--user", "--scope"],
        _ts_configured_url=lambda: "http://host-toolserver:7004",
        _bg_user_path=lambda: tmp / "state" / "mct" / "repl" / "operator-guidance.user.md",
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SRC), "exec"), ns)
    missing = FUNCS - set(ns)
    assert not missing, f"server.py lost {missing}"
    return ns


# ── identity ─────────────────────────────────────────────────────────────────
def test_keeper_is_a_locus_not_this_station(tmp_path):
    ns = load(tmp_path, station_locus_env="op")
    assert ns["_station_locus"]() == "op" and ns["_keeper_locus"]() == "op"
    assert not ns["_is_host_vm"]("keeper")              # vm_mgr's locus, seen from op
    for host in ("", "@self", "@keeper", "host", "op"):
        assert ns["_is_host_vm"](host)
    assert ns["_central_locus"]("keeper") == "keeper"
    assert ns["_central_locus"]("@self") == "op"
    assert ns["_ac_locus_key"]("keeper") == "keeper"     # its serve, not the host's


def test_keeper_is_host_only_on_vm_mgrs_own_station(tmp_path):
    ns = load(tmp_path, station_locus_env="keeper")
    assert ns["_is_host_vm"]("keeper") and ns["_is_host_vm"]("ae-vm-mgr")
    assert ns["_central_locus"]("keeper") == "keeper"


def test_legacy_alias_ae_vm_mgr(tmp_path):
    ns = load(tmp_path, station_locus_env="ae-vm-mgr")   # vm_mgr's old toolserver.env spelling
    assert ns["_station_locus"]() == "keeper"
    ns2 = load(tmp_path, station_locus_env="op")
    assert ns2["_central_locus"]("ae-vm-mgr") == "keeper"


def test_vm_mgr_pointer_resolves_deterministically(tmp_path):
    ns = load(tmp_path, station_locus_env="op")
    (tmp_path / "ssh-hosts.json").write_text(json.dumps({
        "vm-mgr": {"host": "192.168.1.100", "user": "vm_mgr", "port": 22},
        "twin": {"host": "10.9.9.9", "user": "x", "port": 22}}))
    for _ in range(5):                                   # never dict-order dependent
        assert ns["_central_locus"]("vm-mgr") == "keeper"
    pick = ns["_pick_endpoint_locus"]
    assert pick(["ae-mgr", "keeper"], REGISTRY) == "keeper"
    assert pick(["keeper", "ae-mgr"], REGISTRY) == "keeper"
    assert ns["_central_locus"]("twin") == "twin"        # a true tie: never guess


def test_unconfigured_station_never_falls_back_to_keeper(tmp_path):
    ns = load(tmp_path)                                  # no STATION_LOCUS, no self row
    ns["_TS_LOCI"]["hosts"] = {k: v for k, v in REGISTRY.items() if k != "hugpy"}
    assert ns["_station_locus"]() == ""
    assert ns["_keeper_locus"]() == ""
    assert ns["_central_locus"]("") == ""                # host board: not configured
    assert ns["_central_locus"]("keeper") == "keeper"    # keeper's board is keeper's, read as REMOTE
    r = ns["_unconfigured_response"]("board")
    assert r.status == 409 and json.loads(r.body)["unconfigured"] is True


def test_self_from_registry_is_deterministic(tmp_path):
    ns = load(tmp_path)
    ns["_local_ips"] = lambda: {"192.168.1.100"}
    ns["_TS_LOCI"]["hosts"] = {k: dict(v, user=ME) for k, v in REGISTRY.items() if k in ("keeper", "ae-mgr")}
    assert ns["_station_locus"]() == "keeper"


# ── unified seat launch (fake ssh runner) ────────────────────────────────────
class FakeSSH:
    def __init__(self, remote_cfg):
        self.calls, self.remote_cfg = [], remote_cfg

    async def __call__(self, argv, stdin=None, timeout=30):
        self.calls.append((list(argv), stdin or b""))
        text = (stdin or b"").decode()
        if "seat-launch" in text and "base64 -d" in text:
            return 0, "/home/vm_mgr/hugpy-station/seat-launch/keeper-claude-1.sh\n", ""
        if "operator-guidance" in text or "frontier-directive.md" in text:
            return 0, json.dumps(self.remote_cfg) + "\n", ""
        return 0, "", ""


def _remote_cfg():
    return {"state": "/srv/vm_mgr/hugpy-station", "home": "/srv/vm_mgr", "files": {
        "directive": "KEEPER OWN DIRECTIVE\n" + BIG, "handoff": "keeper handoff",
        "models": json.dumps({"claude-code": "keeper-model"}), "fs": None, "delegate": None,
        "b_model": None, "a_template": None, "guidance": "keeper guidance"},
        "remote": True, "locus": "keeper"}


def test_remote_seat_gets_target_config_and_short_argv(tmp_path):
    ns = load(tmp_path, station_locus_env="op")
    t = ns["_locus_target"]("keeper")
    assert t.kind == LX.KIND_SSH and t.locus == "keeper"
    fake = FakeSSH(_remote_cfg())
    cfg = asyncio.run(LX.read_cfg(t, runner=fake))
    cfg.update(remote=True, locus="keeper")
    cmd = "bash -lc " + shlex.quote(ns["_claude_seat_cmd"]("frontier", "claude-code", cfg))
    assert "KEEPER OWN DIRECTIVE" in cmd and BIG in cmd
    assert HOST_DIRECTIVE not in cmd and "host-model" not in cmd
    assert "keeper-model" in cmd and "keeper guidance" in cmd and "keeper handoff" in cmd
    assert "host-toolserver" not in cmd                  # the target's own toolserver env, not ours
    pty, path = asyncio.run(ns["_seat_launch"](t, "keeper-claude", cmd, cfg, "frontier", runner=fake))
    # the payload went to the target on stdin...
    argv, stdin = fake.calls[-1]
    assert argv[0] == "ssh" and argv[-2:] == ["bash", "-s"] and sum(map(len, argv)) < 1024
    sent = base64.b64decode("".join(stdin.decode().split("<<'__HUGPY_STATION_141__'\n", 1)[1]
                                    .split("\n__HUGPY_STATION_141__", 1)[0].split())).decode()
    assert "KEEPER OWN DIRECTIVE" in sent
    assert 'EXCHANGE_LOCUS="${EXCHANGE_LOCUS:-keeper}"' in sent
    assert "host-only" not in sent.lower() or HOST_DIRECTIVE not in sent
    # ...and NOT through tmux/ssh argv
    assert BIG[:100] not in pty
    inner = shlex.split(pty)[-1]
    tmux_line = inner.split("exec ", 1)[1].split("; else", 1)[0]
    assert len(tmux_line.encode()) < 1024 and len(pty.encode()) < 1024 + len(ns["_TMUX_OPTS"])
    assert "new-session -A -s keeper-claude" in tmux_line and path in tmux_line
    assert "systemd-run" not in pty                      # host scope never applied remotely


def test_remote_env_block_carries_no_host_process_env(tmp_path):
    ns = load(tmp_path, station_locus_env="op")
    ns["os"].environ.update(HUGPY_API_KEY="HOST-SECRET", TOOLSERVER_URL="http://host-ts")
    remote = ns["_seat_env_block"]("frontier", ns["_locus_target"]("keeper"))
    assert "HOST-SECRET" not in remote and "host-ts" not in remote
    assert '"$S/env/station.env"' in remote and "keeper" in remote
    local = ns["_seat_env_block"]("frontier", ns["_locus_target"](""))
    assert "HOST-SECRET" in local and "HUGPY_STATION_STATE=" in local
    # identical prelude: the remote block is a prefix-compatible subset of the local one
    assert local.startswith(remote.split("\nexport EXCHANGE_LOCUS")[0])


def test_host_composition_unchanged_without_cfg(tmp_path):
    ns = load(tmp_path, station_locus_env="op")
    sysp = ns["_claude_seat_system_prompt"]()
    # 1.0.143: the generated tmux directive comes first; the host's own
    # frontier-directive.md is that seat's operator overlay
    assert sysp.startswith("<!-- hugpy-station directive tmux@op ")
    assert HOST_DIRECTIVE in sysp and sysp.count(HOST_DIRECTIVE) == 1
    assert ns["_frontier_models"]()["claude-code"] == "host-model"


def test_findings_filter_by_locus(tmp_path):
    ns = load(tmp_path, station_locus_env="op")
    rows = [{"sig": 1, "locus": "keeper"}, {"sig": 2, "locus": "op"}, {"sig": 3, "source": "unit@keeper"}]
    assert [r["sig"] for r in ns["_rows_for_locus"](rows, "keeper")] == [1, 3]


if __name__ == "__main__":
    import tempfile
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d))
            print("ok", name)
