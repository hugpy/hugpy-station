"""source_guard_hook: refuses copying a source out of place, passes normal work."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import source_guard_hook as sg  # noqa: E402


def _srcs(tmp_path):
    src = tmp_path / "dev" / "abstract_gpt"
    (src / "src" / "abstract_gpt").mkdir(parents=True)
    (src / "dist").mkdir()
    (src / "dist" / "abstract_gpt-0.1.13-py3-none-any.whl").write_text("w")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "x.whl").write_text("w")
    return str(src), [os.path.realpath(src)]


def test_blocks_the_ways_a_source_got_forked(tmp_path):
    src, srcs = _srcs(tmp_path)
    blocked = [
        "cd %s && git worktree add -b toolserver-central /tmp/tc-gpt HEAD" % src,
        "git -c safe.directory=* -C %s worktree add /tmp/x main" % src,
        "git clone %s /tmp/copy" % src,
        "git clone https://github.com/AbstractEndeavors/abstract-gpt.git /tmp/g",
        "cp -a %s /srv/vm_mgr/tmp/serve-release/" % src,
        "rsync -a %s/src/ /tmp/snap/" % src,
        "twine upload %s/x.whl" % (tmp_path / "elsewhere"),
        "cd %s && python -m twine upload --repository pypi dist/*" % src,
    ]
    for cmd in blocked:
        why = sg.check(cmd, str(tmp_path), srcs)
        assert why and "source guard" in why, cmd


def test_passes_normal_work(tmp_path):
    src, srcs = _srcs(tmp_path)
    ok = [
        "cd %s && git status && python -m pytest -q" % src,
        "cp %s/src/abstract_gpt/x.py /tmp/x.py" % src,           # a single file, not a tree
        "cp -a %s/src %s/src.bak" % (src, src),                  # stays inside the source
        "git clone https://github.com/someone/unrelated.git /tmp/u",
        "cd /srv/hugpy/src/hugpy && git worktree add -b x /tmp/hugpy-wt-x main",   # sanctioned hugpy landing
        "ls /tmp && grep -rn foo %s" % src,
    ]
    for cmd in ok:
        assert sg.check(cmd, str(tmp_path), srcs) is None, cmd


def test_symlinked_source_is_a_source(tmp_path):
    real = tmp_path / "hugpy" / "py" / "cinema" / "hugpy_video"
    real.mkdir(parents=True)
    (real / "pyproject.toml").write_text("[project]\nname='hugpy-video'\n")
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "hugpy_video").symlink_to(real)
    srcs = sg.sources(registry_glob=str(tmp_path / "none" / "*"), dev_root=str(dev))
    assert srcs == [str(real.resolve())]
    assert sg.check("cp -a %s /tmp/snap" % (dev / "hugpy_video"), str(tmp_path), srcs)
    assert sg.check("cp -a %s /tmp/snap" % real, str(tmp_path), srcs)


def test_ufw_writes_refused_reads_pass():
    for cmd in ["sudo sed -i '/coturn/a -A ufw-before-input -p udp' /etc/ufw/before.rules",
                "echo '-A x' | sudo tee -a /etc/ufw/before.rules",
                "sudo cp /tmp/new.rules /etc/ufw/before.rules",
                "cat new >> /etc/ufw/before6.rules",
                "sudo mv x /etc/ufw/user.rules",
                "sudo install -m 640 new /etc/ufw/before.rules",
                "sudo rm /etc/ufw/before.rules.bak", "sudo nano /etc/ufw/before.rules"]:
        assert sg.check_ufw(cmd), cmd
    for cmd in ["sudo cat /etc/ufw/before.rules", "grep coturn /etc/ufw/before.rules",
                "sudo ufw status verbose", "cp /etc/ufw/before.rules /srv/vm_mgr/gate/ufw/before.rules",
                "diff /etc/ufw/before.rules /srv/vm_mgr/gate/ufw/before.rules", "sudo ufw allow 3478/udp"]:
        assert sg.check_ufw(cmd) is None, cmd


def test_write_edit_tools_on_ufw_refused(monkeypatch, capsys):
    import io, json
    for tool in ("Write", "Edit"):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": tool, "tool_input": {"file_path": "/etc/ufw/before.rules"}})))
        assert sg.main() == 2
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": tool, "tool_input": {"file_path": "/srv/vm_mgr/x.txt"}})))
        assert sg.main() == 0
