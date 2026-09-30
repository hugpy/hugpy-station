"""In-process B fallback answers lookups without the model — no network, no station boot.

The fallback in server.py (_b_answer, after the VM and host one-shot paths) used
to send EVERY message to the Gateway model. It now mirrors hugpy_agent.mct.b_lookup:
an ack / help / state question, or any message against an EMPTY state, is answered
deterministically with the "(ts · tokens 0 · dur · lookup)" metadata line.

Run:  /srv/vm_mgr/abstract-claude-serve/venv/bin/python -m pytest resources/backend/test_b_fallback_guard.py
"""
import ast
import re
import time
from pathlib import Path

SERVER = Path(__file__).with_name("server.py")
TREE = ast.parse(SERVER.read_text())
FUNCS = ("_b_meta_line", "_b_state_empty", "_b_lookup_kind", "_b_fallback_lookup")
BODY = [n for n in TREE.body
        if (isinstance(n, ast.FunctionDef) and n.name in FUNCS)
        or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith("_B_")
                                              and t.id != "_B_SYSMSG" for t in n.targets))]
NS = {"re": re, "time": time}
exec(compile(ast.Module(body=BODY, type_ignores=[]), str(SERVER), "exec"), NS)

META_RE = re.compile(r"\(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ · tokens (\d+) · \d+\.\d{3}s · ([^)]+)\)$")
GROUND = "policy: (none set)\ncatalog (1): notes.md\nderived memory (0):\nrolling log: /tmp/x.log"


class _Sess:
    session_id = "s1"

    def __init__(self, policy="", pointer=None, catalog=None):
        self._policy_text, self._policy_pointer, self._catalog = policy, pointer, dict(catalog or {})

    def _materialize_file_sources(self):
        pass


class _Srv:
    def __init__(self, facts=()):
        self.compaction = type("C", (), {"facts": staticmethod(lambda sid: list(facts))})()


def _meta(reply):
    m = META_RE.search(reply.strip().splitlines()[-1])
    assert m, f"no metadata line on: {reply!r}"
    return int(m.group(1)), m.group(2)


def test_classifier_is_deterministic():
    kind = NS["_b_lookup_kind"]
    assert kind("ok thanks") == "ack" and kind("") == "ack"
    assert kind("help") == "help" and kind("what can you do?") == "help"
    assert kind("status") == "state" and kind("what is in the catalog?") == "state"
    assert kind("show me the policy") == "state" and kind("how many tokens spent") == "state"
    assert kind("summarize the notes for a handoff") is None
    assert kind("rewrite the policy to be stricter") is None      # a verb, not a readout ask


def test_ack_help_state_answered_without_model():
    look = NS["_b_fallback_lookup"]
    t0 = time.monotonic()
    for text, expect in (("ok", "Noted."), ("help", NS["_B_HELP_TEXT"]), ("what's in the catalog", GROUND)):
        reply = look(text, GROUND, False, t0)
        assert reply is not None and reply.startswith(expect)
        assert _meta(reply) == (0, "lookup")


def test_empty_state_never_reaches_model_for_any_message():
    look = NS["_b_fallback_lookup"]
    for text in ("summarize everything for me", "what should A do next?", "judge the plan"):
        reply = look(text, "policy: (none set)\ncatalog (0): (empty)", True, time.monotonic())
        assert reply is not None and reply.startswith(NS["_B_EMPTY_TEXT"])
        assert _meta(reply) == (0, "lookup")


def test_synthesis_over_nonempty_state_keeps_model_path():
    look = NS["_b_fallback_lookup"]
    assert look("summarize everything for me", GROUND, False, time.monotonic()) is None
    assert look("what should A do next?", GROUND, False, time.monotonic()) is None


def test_state_empty_needs_no_policy_no_catalog_no_memory():
    empty = NS["_b_state_empty"]
    assert empty(_Sess(), _Srv()) is True
    assert empty(_Sess(policy="be terse"), _Srv()) is False
    assert empty(_Sess(pointer="sha256:abc"), _Srv()) is False
    assert empty(_Sess(catalog={"notes.md": "sha256:1"}), _Srv()) is False
    assert empty(_Sess(), _Srv(facts=[object()])) is False


def test_fallback_consults_lookup_before_gateway():
    fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "_b_answer")
    src = ast.get_source_segment(SERVER.read_text(), fn)
    i_lookup, i_gw = src.index("_b_fallback_lookup("), src.index("Gateway.from_config(")
    assert 0 < i_lookup < i_gw
    assert "_b_state_empty(sess, srv)" in src and "_B_LOOKUP_MODEL" in src
