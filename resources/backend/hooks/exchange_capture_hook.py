#!/usr/bin/env python3
"""Claude Code `Stop` hook — preserve this seat's conversation to the central
`exchanges` DB so it survives a ~/.claude wipe (the DB, not .claude, is the
durable record).

Reads the hook payload JSON on stdin ({transcript_path, cwd, session_id, ...}),
then ships the transcript BODY (not a path — a seat's transcript is 0600 and the
central server can't read another locus's file) to the toolserver's
`exchange/ingest_transcript`, which primes + records it idempotently per turn.

Design notes:
  - Best-effort and SILENT: a capture must never fail or slow a turn. All errors
    are swallowed; exit is always 0.
  - NON-BLOCKING: stdin + the transcript are read synchronously (fast), then the
    network POST + server-side prime run in a forked child so the turn returns
    immediately.
  - SELF-CONTAINED auth: URL + token come from the seat's own toolserver MCP
    config (~/.claude.json → mcpServers.toolserver.env), overridable by env.
    EXCHANGE_INGEST_URL is preferred (lets a same-host seat use 127.0.0.1:7004
    instead of the public TLS endpoint).
"""
import json
import os
import sys
import urllib.request


def _config():
    url = (os.environ.get("EXCHANGE_INGEST_URL")
           or os.environ.get("TOOLSERVER_URL") or "")
    tok = (os.environ.get("TOOLSERVER_TOKEN")
           or os.environ.get("TOOLSERVER_OPERATOR_TOKEN") or "")
    if not (url and tok):
        try:
            d = json.load(open(os.path.expanduser("~/.claude.json")))
            env = (((d.get("mcpServers") or {}).get("toolserver") or {}).get("env") or {})
            url = url or env.get("TOOLSERVER_URL", "")
            tok = tok or env.get("TOOLSERVER_TOKEN", "")
        except Exception:
            pass
    return url.rstrip("/"), tok


def _locus(payload):
    # The console injects EXCHANGE_LOCUS for the seat (authoritative — e.g. the
    # keeper is user vm_mgr but locus "keeper"). Fall back to the unix user.
    return (os.environ.get("EXCHANGE_LOCUS")
            or os.environ.get("USER") or "").strip().lower()


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    tp = payload.get("transcript_path") or ""
    if not tp or not os.path.isfile(tp):
        return
    locus = _locus(payload)
    url, tok = _config()
    if not (locus and url):
        return
    try:
        body = open(tp, encoding="utf-8", errors="replace").read()
    except Exception:
        return
    if not body.strip():
        return

    # Hand the slow part (network + server-side prime of a possibly-large
    # transcript) to a detached child so the turn is never blocked.
    try:
        if os.fork() > 0:
            return  # parent returns immediately
        os.setsid()
    except Exception:
        pass  # no fork (unlikely here) → fall through and do it inline

    data = json.dumps({"locus": locus, "body": body}).encode()
    req = urllib.request.Request(url + "/exchange/ingest_transcript", data=data,
                                 headers={"Content-Type": "application/json"})
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
