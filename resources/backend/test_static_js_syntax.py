"""Every script the Station page runs must PARSE — no network, no station boot.

1.0.134-1.0.137 shipped `/^https?:\\/\\//i` (a double-escaped regex in the
🌐 browser panel) inside index.html's one big inline <script>. That is a
SyntaxError ("Invalid regular expression flags"), so the WHOLE inline script
never ran: React never mounted #root and the station UI vanished, leaving only
the frontier terminal column (fleetview-term.js is a separate <script>). The
static file served fine and no Python test touched it — so this one parses it.

Run:  python3 -m pytest resources/backend/test_static_js_syntax.py
  or: python3 resources/backend/test_static_js_syntax.py      (no pytest needed)
Needs `node` on PATH (the release build already requires it); skips without it.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"
# an HTML parser ends a classic script at the first "</script" — match that rule
_INLINE = re.compile(r"<script\b([^>]*)>(.*?)</script", re.S | re.I)


def _node():
    return shutil.which("node")


def _node_check(js, label):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        path = f.name
    try:
        r = subprocess.run([_node(), "--check", path], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(path)
    assert r.returncode == 0, f"{label} does not parse:\n{(r.stderr or r.stdout)[:1500]}"


def _inline_scripts(html):
    out = []
    for attrs, body in _INLINE.findall(html):
        if re.search(r"\bsrc\s*=", attrs):
            continue
        m = re.search(r"\btype\s*=\s*['\"]?([^'\" >]+)", attrs)
        if m and m.group(1).lower() not in ("text/javascript", "application/javascript"):
            continue                       # JSON/templates are not executed as JS
        out.append(body)
    return out


def _skip(msg):
    try:
        import pytest
        pytest.skip(msg)
    except ImportError:
        raise RuntimeError("SKIP: " + msg)


def test_index_inline_scripts_parse():
    if not _node():
        _skip("node not on PATH")
    # what the browser actually gets: server.py index() stamps "{v}" first
    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{v}", "1")
    blocks = _inline_scripts(html)
    assert blocks, "index.html has no inline <script> — the extractor is broken"
    for i, js in enumerate(blocks):
        _node_check(js, f"index.html inline <script> #{i}")


def test_static_js_files_parse():
    if not _node():
        _skip("node not on PATH")
    for p in sorted(STATIC.glob("*.js")):
        _node_check(p.read_text(encoding="utf-8"), f"static/{p.name}")


def test_extractor_catches_the_1_0_134_regex():
    """The regression itself must fail the check (guards the guard)."""
    if not _node():
        _skip("node not on PATH")
    bad = '<script>const ok = 1;\nif (!/^https?:\\\\/\\\\//i.test("x")) {}\n</script>'
    (js,) = _inline_scripts(bad)
    try:
        _node_check(js, "fixture")
    except AssertionError as e:
        assert "Invalid regular expression flags" in str(e)
    else:
        raise AssertionError("node --check accepted the double-escaped regex")


if __name__ == "__main__":          # plain-python runner when pytest is absent
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   ", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL ", name, "->", repr(e)[:1500])
    sys.exit(1 if failed else 0)
