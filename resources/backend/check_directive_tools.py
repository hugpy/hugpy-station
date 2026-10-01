"""Every toolserver tool a rendered directive names must exist (operator
2026-10-01: the station toolkit's facts are generated or verified, never
hand-typed guesses).

    python3 check_directive_tools.py                  # check against the LIVE toolserver
    python3 check_directive_tools.py --snapshot       # check against the shipped snapshot
    python3 check_directive_tools.py --write-snapshot # refresh the snapshot from the live list

Live mode reads TOOLSERVER_URL + TOOLSERVER_TOKEN and asks the toolserver's MCP
endpoint (tools/list, then ts_categories + ts_list per category: the meta-mode
toolserver lists only its three meta tools at tools/list). Exit 1 if any named tool is missing.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "toolserver-tools.snapshot.json"
# Tools the MCP bridge adds next to the toolserver's own (abstract-serve-core:
# the serve-owned session relay) — real, but not in the toolserver's list.
BRIDGE_EXTRAS = frozenset({"session_message"})


def snapshot_tools() -> set:
    return set(json.loads(SNAPSHOT.read_text(encoding="utf-8"))["tools"])


def mentioned(text: str, known: set) -> set:
    """Tool-shaped names in `text`: <category>_<name> where <category> is a
    prefix of some known tool. Wildcards (`issue_*`) are not names."""
    prefixes = sorted({t.split("_", 1)[0] for t in known}, key=len, reverse=True)
    rx = re.compile(r"(?<![\w/.-])((?:%s)_[a-z][a-z0-9_]*)(?![\w*@/-])" % "|".join(map(re.escape, prefixes)))
    return set(rx.findall(text or ""))


def missing(text: str, known: set) -> set:
    return {n for n in mentioned(text, known | BRIDGE_EXTRAS) if n not in known and n not in BRIDGE_EXTRAS}


def live_tools(url: str, token: str, timeout: float = 30) -> set:
    def rpc(method, params):
        req = urllib.request.Request(
            url.rstrip("/") + "/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)["result"]

    def call(name, args):
        return json.loads(rpc("tools/call", {"name": name, "arguments": args})["content"][0]["text"])

    names = {t["name"] for t in rpc("tools/list", {})["tools"]}
    if "ts_categories" in names:
        for c in call("ts_categories", {}):
            names |= {t["name"] for t in call("ts_list", {"category": c["category"]})["tools"]}
    return names


def rendered_texts() -> dict:
    import directives as DV
    fx = DV.facts({"files": {}}, locus="keeper", env={})
    return DV.render_all({"files": {}}, fx)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sys.path.insert(0, str(HERE))
    if "--snapshot" in argv:
        known = snapshot_tools()
    else:
        url, tok = os.environ.get("TOOLSERVER_URL", ""), os.environ.get("TOOLSERVER_TOKEN", "")
        if not url or not tok:
            print("TOOLSERVER_URL / TOOLSERVER_TOKEN not set (use --snapshot for the shipped list)")
            return 2
        known = live_tools(url, tok)
        if "--write-snapshot" in argv:
            SNAPSHOT.write_text(json.dumps({"source": url + " MCP tools/list + ts_list", "tools": sorted(known)},
                                           indent=0) + "\n", encoding="utf-8")
            print(f"snapshot: {len(known)} tools -> {SNAPSHOT}")
    bad = {}
    for kind, text in rendered_texts().items():
        m = missing(text, known)
        if m:
            bad[kind] = sorted(m)
    named = sorted(set().union(*(mentioned(t, known | BRIDGE_EXTRAS) for t in rendered_texts().values())))
    print(f"directives name {len(named)} tools; toolserver lists {len(known)}")
    for kind, names in bad.items():
        print(f"MISSING in {kind}: {', '.join(names)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
