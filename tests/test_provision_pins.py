"""1.0.128: the station's module pins are plain PyPI pins; provisioning is `pip install -U -r`.

No network: pip is a fake that records its arguments. The one integration check
(the pins resolve on PyPI) is run by hand before a cut:
    python3 -m pip download --no-deps -d /tmp/pin-check -r resources/REQUIREMENTS.txt
"""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQ = ROOT / "resources" / "REQUIREMENTS.txt"
PROVISION = ROOT / "resources" / "bin" / "station-provision-venv"


def _pins():
    out = {}
    for ln in REQ.read_text().splitlines():
        if ln.lstrip().startswith("#") or not ln.strip():
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^\s;#]+)$", ln.strip())
        assert m, "every Tier-1 line is an exact == pin: %r" % ln
        out[m.group(1)] = m.group(2)
    return out


def test_serve_modules_are_pinned_to_the_published_versions():
    p = _pins()
    assert p["abstract-claude"] == "0.1.70" and p["abstract-serve-core"] == "0.1.10"


def test_no_local_wheels_ship():
    assert not (ROOT / "resources" / "wheels").exists()
    assert not (ROOT / "resources" / "wheels.lock").exists()
    assert "--find-links" not in PROVISION.read_text()


def test_provision_runs_plain_pip_install_of_the_pins(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    rec = tmp_path / "pip-args"
    pip = venv / "bin" / "pip"
    pip.write_text('#!/bin/sh\necho "$@" >> %s\n' % rec)
    pip.chmod(0o755)
    r = subprocess.run(["bash", str(PROVISION), str(venv)], capture_output=True, text=True,
                       env=dict(os.environ, STATION_REQUIREMENTS=str(REQ)), timeout=60)
    assert r.returncode == 0, r.stderr
    assert rec.read_text().strip().splitlines()[0] == "install -U -r %s" % REQ
