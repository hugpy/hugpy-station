"""The station resolves the toolserver via abstract_toolserver discovery (no
hardcoded URL) and calls through the shared client. Standalone:
    python3 test_toolserver_discovery.py   (needs abstract-toolserver>=0.0.31)
"""
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "hooks"))

from abstract_toolserver import discovery as D  # noqa: E402


def _server_helpers():
    """Exec only the toolserver-endpoint helpers out of server.py (importing the
    whole backend has side effects)."""
    src = (HERE / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    want = {"_ts_configured_url", "_ts_discovered_url", "_ts_resolve_url", "_ts_shared_client"}
    nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in want)
             or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in
                 ("_TS_URL_KEYS", "_TS_LOCAL_DEFAULT") for t in n.targets))]
    ns = {"os": os, "Path": Path, "TS_UPSTREAM": ""}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "server.py", "exec"), ns)
    return ns


class _Resp:
    def __init__(self, doc):
        self._b = json.dumps(doc).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = {"HOME": self.tmp, "PATH": "/usr/bin:/bin"}
        self.p_env = mock.patch.dict(os.environ, self.env, clear=True)
        self.p_sys = mock.patch.object(D, "SYSTEM_DIRS", ())
        self.p_env.start(); self.p_sys.start()
        tf = Path(self.tmp) / "ts.env"
        tf.write_text("TOOLSERVER_OPERATOR_TOKEN=tok-xyz\n")
        D.write_advertisement(D.make_advertisement("http://127.0.0.1:7911", "127.0.0.1", 7911,
                                                   os.getpid(), token_file=str(tf)))

    def tearDown(self):
        self.p_sys.stop(); self.p_env.stop()

    def test_backend_resolves_advertised_then_configured(self):
        ns = _server_helpers()
        self.assertEqual(ns["_ts_configured_url"](), "")
        self.assertEqual(ns["_ts_resolve_url"](), "http://127.0.0.1:7911")
        os.environ["STATION_CONSOLE_TOOLSERVER"] = "https://dev.hugpy.ai/toolserver/"
        self.assertEqual(ns["_ts_resolve_url"](), "https://dev.hugpy.ai/toolserver")

    def test_backend_calls_through_shared_client(self):
        ns = _server_helpers()
        c = ns["_ts_shared_client"](None, None)
        self.assertEqual((c.url, c.token, c.token_source), ("http://127.0.0.1:7911", "tok-xyz", "file"))
        seen = []

        def opener(req, timeout=None):
            seen.append((req.full_url, req.get_header("X-operator-token")))
            return _Resp({"result": {"CLAUDE_CODE_OAUTH_TOKEN": "x"}})
        c._opener = opener
        self.assertEqual(c.post("/claude/oauth_token", {}), {"CLAUDE_CODE_OAUTH_TOKEN": "x"})
        self.assertEqual(seen, [("http://127.0.0.1:7911/claude/oauth_token", "tok-xyz")])

    def test_hooks_discover_without_hardcoded_url(self):
        import ledger_hook
        import exchange_capture_hook
        self.assertEqual(ledger_hook._url_tok()[0], "http://127.0.0.1:7911")
        self.assertEqual(exchange_capture_hook._config()[0], "http://127.0.0.1:7911")
        os.environ["EXCHANGE_INGEST_URL"] = "http://127.0.0.1:1"
        self.assertEqual(ledger_hook._url_tok()[0], "http://127.0.0.1:1")

    def test_seat_mcp_entry_has_no_url_unless_configured(self):
        import base64
        import re
        import subprocess
        src = (HERE / "server.py").read_text(encoding="utf-8")
        script = base64.b64decode(re.search(r'_SEAT_TRUST_B64 = "([^"]+)"', src).group(1)).decode()
        cfg = Path(self.tmp) / ".config" / "hugpy-station"
        cfg.mkdir(parents=True)
        (cfg / "toolserver.env").write_text("HUGPY_OPERATOR_TOKEN=tok-xyz\n")
        for url, want in (("", None), ("https://dev.hugpy.ai/toolserver", "https://dev.hugpy.ai/toolserver")):
            cj = Path(self.tmp) / ("c%s.json" % bool(url))
            subprocess.run([sys.executable, "-c", script, str(cj), "/ws", url], check=True,
                           env={"HOME": self.tmp})
            env = json.load(open(cj))["mcpServers"]["toolserver"]["env"]
            self.assertEqual(env.get("TOOLSERVER_URL"), want)
            self.assertEqual(env["TOOLSERVER_TOKEN"], "tok-xyz")


if __name__ == "__main__":
    unittest.main()
