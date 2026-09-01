#!/usr/bin/env python3
"""bugreport-scanner — VM-side log scanner + local-LLM digest engine (lane #4).

Stdlib-only (Python 3.12). Deterministic-first triage ladder:
  collect -> filter/dedup -> (deterministic findings) -> optional LLM digest
  -> machine-enforced citation gate -> atomic bugreport.v1 write.

The LLM is strictly advisory: the scanner is fully useful with hugpy
unreachable (a deterministic summary is always present). Every LLM claim that
does not cite a real input line number [Ln] is discarded and counted.

Design grounding: /srv/share/projects/blackbird/LOCAL-LLM-OFFLOAD.md lane #4
and its dependability rules (section 3).
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Constants / paths
# --------------------------------------------------------------------------
HOME = os.path.expanduser("~")
CONFIG_PATH = os.path.join(HOME, ".config", "bugreport", "sources.json")
STATE_DIR = os.path.join(HOME, ".local", "state", "bugreport")
CURSOR_PATH = os.path.join(STATE_DIR, "cursors.json")
META_PATH = os.path.join(STATE_DIR, "meta.json")
AUDIT_PATH = os.path.join(STATE_DIR, "audit.jsonl")
# --- HUGPY_HOME: single base dir for hugpy runtime files ---------------------
# Byte-compatible with abstract_hugpy_dev/_platform/paths.py — keep in sync.
def _hugpy_path(sub, name, *legacy):
    base = os.environ.get("HUGPY_HOME") or os.path.join(os.path.expanduser("~"), ".hugpy")
    d = os.path.join(base, sub)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    new = os.path.join(d, name)
    try:
        if not os.path.exists(new):
            for lname in legacy:
                old = os.path.join(os.path.expanduser("~"), lname)
                if os.path.exists(old) and os.path.abspath(old) != os.path.abspath(new):
                    try:
                        os.replace(old, new)
                    except OSError:
                        import shutil
                        shutil.move(old, new)
                    break
    except OSError:
        pass
    return new


REPORT_PATH = _hugpy_path("logs", "bugreport.json", "bugreport.json")
REQUEST_PATH = _hugpy_path("logs", "bugreport-request.json", "bugreport-request.json")

# Activity/vitals (t46): the keeper-relay persists a `last_activity_ts` (refreshed
# whenever the keeper is observed WORKING) in its own state file. We READ that flat
# key defensively for the console's activity meter — we never write it, and never
# reimplement the relay's env/default quiet-window resolution.
KEEPER_RELAY_STATE_PATH = "/home/ubuntu/.local/state/keeper-relay/state.json"

# Watches (S1/S2): compiled per-group series live beside the triage state, in a
# SEPARATE cursor namespace so a watch reading a file never disturbs the triage
# source's own byte offset for that same file.
SERIES_DIR = os.path.join(STATE_DIR, "series")
WATCH_CURSOR_PATH = os.path.join(STATE_DIR, "watch_cursors.json")
SERIES_CAP = 5000        # ring the series file once it exceeds this many lines
SERIES_KEEP = 4000       # ...keeping the newest this-many lines
WATCH_GROUPS_CAP = 50    # payload caps groups at the N most-recently-active keys

# http watch source kind (S3): a remote endpoint (e.g. hugpy's rolling jobs
# window) is polled each cadence and its per-row (id,status) TRANSITIONS are
# accumulated into the SAME series/payload machinery as line watches. The window
# re-serves the same live rows every poll AND prunes terminal rows out, so the
# scanner keeps its OWN dedup: a per-watch (id -> last_status) map, persisted in
# a sibling state file (namespace-separated from cursors, like the series). It is
# recency-ordered and capped; the oldest-seen ids prune out first past the cap.
HTTP_STATE_PATH = os.path.join(STATE_DIR, "watch_http_state.json")
HTTP_STATE_CAP = 2000    # per-watch max remembered ids; prune oldest-seen beyond
HTTP_TIMEOUT = 20        # per-poll GET timeout (s); no retries — next cadence retries

LLM_URL = "https://dev.hugpy.ai/api/v1/chat/completions"
LLM_MODEL = "Qwen~Qwen3-Coder-Next-GGUF"
# The gateway enforces auth since 2026-07-23 (t12 closed hugpy-side): the digest
# needs the VM's real key. It lives in hugpy-agent's env file — read, not owned,
# here; the fallback tag keeps requests identifiable but will 401, which the
# digest path already degrades on (advisory-only, triage unaffected).
LLM_TOKEN_ENV_FILE = os.path.expanduser("~/.config/hugpy-agent/agent.env")


def _load_llm_token(path=None):
    """HUGPY_API_KEY from the hugpy-agent env file (shell KEY=value lines,
    optional quotes), else the legacy log-tag. Never raises."""
    try:
        with open(path or LLM_TOKEN_ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("HUGPY_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if val:
                        return val
    except OSError:
        pass
    return "bugreport-blackbird"


LLM_TOKEN = _load_llm_token()
DIGEST_MAX_TOKENS = 512
DIGEST_TIMEOUT = 300
DIGEST_MIN_INTERVAL = 3600  # at most hourly, unless forced or an err-level cluster is present

# The units whose is-active state rides along in report["status"].key_units, so
# the drawer shows liveness without a second call. These defaults are
# blackbird's; console-ui-dev is blackbird's UI dev server and exists on no
# other VM, so on any other station it reported a permanent "inactive" that read
# as a fault (seen on teststation, 2026-07-26). Overridable per VM via
# BUGREPORT_KEY_UNITS (comma-separated) rather than by forking this file --
# scanner.py is shared, and a per-VM edit would drift.
KEY_UNITS = [u.strip() for u in os.environ.get(
    "BUGREPORT_KEY_UNITS", "console-ui-dev,keeper-relay").split(",") if u.strip()]

CADENCE = 900          # cadence scan every 15 min
POLL = 5               # poll request file every ~5s
SAMPLE_CAP = 9000      # ~9KB LLM sample cap
MSG_TRUNC = 300        # per-message truncation inside the sample

PRIORITY_NAMES = {0: "emerg", 1: "alert", 2: "crit", 3: "err",
                  4: "warning", 5: "notice", 6: "info", 7: "debug"}
SEV_RANK = {"emerg": 0, "alert": 1, "crit": 2, "critical": 2, "err": 3,
            "error": 3, "fatal": 3, "warning": 4, "warn": 4, "notice": 5,
            "info": 6, "debug": 7}

# Known self-healing transients that journald logs at a scary PRIORITY but which
# are benign on this host. Each entry is (unit_pattern, message_pattern); a line
# is downgraded to "info" ONLY when BOTH compiled regexes match (unit/identifier
# and message). Downgraded lines are kept — not dropped — so they stay auditable.
# Keep this list narrow and conservative: only exact, well-understood signatures
# belong here, never broad patterns that could mask a genuine error.
BENIGN_JOURNAL_SIGNATURES = [
    # systemd-networkd-wait-online times out as the ExecStartPre of the
    # apt-daily* timers on this LXD/vsock VM (odd network setup), but the parent
    # apt unit still exits 0/SUCCESS ("Finished") — the wait-online precheck
    # timeout is expected here and self-heals. Match conservatively: the exact
    # message AND a unit/identifier tied to wait-online or the apt-daily units.
    (re.compile(r"systemd-networkd-wait-online|apt-daily(-upgrade)?|apt\.systemd\.daily", re.I),
     re.compile(r"Timeout occurred while waiting for network connectivity", re.I)),
    # At every host-side poweroff, systemd fails to unmount the two virtiofs
    # mounts (lxd agent + the project share) because they're still busy at
    # late shutdown. virtiofs data lives host-side, so a busy unmount at
    # poweroff loses nothing — and reboots happen every 1-3 days, so left as
    # `err` this re-fires a bugerr ping per reboot (seen 2026-07-22). Match
    # conservatively: exact unit mount names AND the Failed-unmounting message.
    (re.compile(r"^init\.scope$"),
     re.compile(r"Failed unmounting (run-lxd_agent|srv-share-projects-blackbird)\.mount", re.I)),
    # lxd-agent logs a `warning` every time a host-side vsock connection is torn
    # down without a TLS closeNotify. The message states the outcome itself —
    # "but connection was closed anyway" — so the teardown SUCCEEDED and only the
    # courtesy alert was skipped; nothing is lost and nothing retries. The
    # console polls this VM continuously, so each poll's close emits one line:
    # 3060 in a 3h window (2026-07-25), which crosses the warning budget and
    # fires a bugerr ping for pure cosmetic noise (bugerr:7@1784938150). Third
    # instance of this shape after bugerr:1 (boot-window URLError) and bugerr:2
    # (virtiofs unmount), and handled the same way.
    #
    # Match conservatively on the MESSAGE, not just the unit: lxd-agent also
    # carries genuinely distinct signatures that must stay visible — 10x prio-1
    # "unable to resolve host blackbird" and a prio-3 "pututline" in the same
    # window. Requiring both the closeNotify text and the vsock write target
    # keeps this to the one signature verified uniform across all 3060 lines.
    (re.compile(r"^lxd-agent(\.service)?$"),
     re.compile(r"failed to send closeNotify alert.*write vsock", re.I | re.S)),
]

# A keeper-relay poll failure (`fetch-failed:HTTPError`/`TimeoutError`) is
# self-healing when isolated: the relay re-polls the same `since` window on the
# next cycle and recovers, so a lone blip loses nothing. But file_severity()
# grades any "error"/"fail" audit row as `err`, so a single blip would burn the
# err-budget and fire a spurious alert. Only a SUSTAINED burst (dozens of
# consecutive fetch-failed rows, as in the 2026-07-17 upstream outage) indicates
# a real problem. We debounce at the cluster level: a keeper-relay-audit fetch-
# failed cluster with count < RELAY_TRANSIENT_MIN_BURST is downgraded err ->
# warning (kept visible, not dropped/info); a cluster at/above the threshold
# keeps its err severity so the real-outage burst still alerts.
# RECALIBRATED 2026-07-25 against the full audit history (972 fetch-failed rows,
# 49 clusters at a 5-min gap boundary). MIN_BURST=3 was set from a guess and was
# far too low: it paged the operator twice in six hours for clusters of 4 and 5
# that had already self-recovered before the report was even generated
# (bugerr:1@1784959951 URLError x40 -- itself a boot-window artifact -- and
# bugerr:2@1784983644 TimeoutError x4). The measured distribution is bimodal and
# the split is DURATION, not count:
#     transient, self-healing : 37 clusters, sizes 1-14, ALL over within <=6 min
#     genuine upstream outage : 12 clusters, 13 / 20 / 36 / 43 / 69 / 72 min
# So count alone cannot separate them -- a 14-row cluster resolved in 195s while
# a 34-row one ran 13 min. We now require BOTH conditions to alert as err:
# count >= MIN_BURST *and* span >= MIN_SPAN. A short-lived flap of any size is a
# warning (still visible in the report, never dropped); only a burst that is both
# large AND sustained pages. Threshold sits above the largest observed transient
# (14) with headroom, and the span floor is 8 min -- comfortably past the 6-min
# worst transient, comfortably under the 13-min shortest real outage.
RELAY_TRANSIENT_MIN_BURST = 15
RELAY_TRANSIENT_MIN_SPAN = 480
_RELAY_TRANSIENT_RE = re.compile(r"fetch-failed:(HTTPError|TimeoutError|URLError)")

# A host power-cycle (VM reboot) makes keeper-relay's ~15s host-endpoint poll
# fail with fetch-failed:URLError for about 90s until the host bridge comes
# back up, producing a burst of 6-8+ consecutive rows — well past
# RELAY_TRANSIENT_MIN_BURST, so debounce_relay_transients() alone lets it
# through and it fires a spurious bugerr ping every reboot (2026-07-22: 4
# boots at 11:58/12:52/13:10/13:13 UTC -> an 8x cluster -> bugerr:1@1784726019).
# This SEPARATE, more targeted debounce only downgrades a burst when it is
# BOTH tied to a boot AND already over:
#   - boot-window: every single failure ts in the cluster falls within
#     RELAY_BOOT_GRACE seconds of some recent boot. We source boot times from
#     `journalctl --list-boots`, whose first_entry is the first JOURNAL line
#     of that boot, not the kernel's own boot instant -- it lags true boot by
#     a few seconds (measured on this host: btime 1784726003 vs
#     first_entry 1784726010.46, ~7.5s lag). RELAY_BOOT_GRACE=180s comfortably
#     absorbs both that lag and the observed ~90s recovery window.
#   - recovered: the cluster's LAST failure is at least RELAY_RECOVERY_QUIET
#     seconds before the scan's `now`. A burst still producing rows at scan
#     time is either an ongoing reboot (bridge not up yet) or a real outage
#     (like 2026-07-17's) and must keep alerting as err either way.
# Multiple boots can each contribute their own burst within one scan window
# (today: 4) -- that's still fine as long as EVERY row lands in SOME boot's
# grace window; a row that falls in the gap between two boot windows means
# something else is going on and we keep err. If boot times can't be
# determined, or the cluster's per-row timestamps aren't available, we do NOT
# downgrade -- fail toward err, same posture as debounce_relay_transients.
RELAY_BOOT_GRACE = 180
RELAY_RECOVERY_QUIET = 120

log = logging.getLogger("bugreport")

DEFAULT_CONFIG = {
    "first_run_since": "-1h",
    "sources": [
        {"name": "journal-warnings", "kind": "journal",
         "priority": "warning", "enabled": True},
        {"name": "journal-key-units", "kind": "journal",
         "units": KEY_UNITS, "priority": "info", "enabled": True},
        {"name": "keeper-relay-audit", "kind": "file",
         "path": "/home/ubuntu/.local/state/keeper-relay/audit.jsonl",
         "enabled": True},
    ],
    # Operator-dictated watch rules (S1/S2). Empty by default — additive, so
    # existing installs pick up the field via config.get("watches", []) with no
    # migration. See README for the shape + a working example.
    "watches": [],
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def sev_rank(s: str) -> int:
    return SEV_RANK.get((s or "").lower(), 6)


def load_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True))


def atomic_write(path, text):
    """Write text to path atomically (tmp file in same dir + os.replace)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".bugreport-tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def audit_append(record):
    """Append one JSON line to the append-only audit log."""
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(AUDIT_PATH, "a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as e:
        log.warning("audit append failed: %s", e)


def ensure_config():
    """Load config, writing sensible defaults if the file is missing."""
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        atomic_write(CONFIG_PATH, json.dumps(DEFAULT_CONFIG, indent=2))
        log.info("wrote default config to %s", CONFIG_PATH)
    cfg = load_json(CONFIG_PATH, None)
    if not isinstance(cfg, dict) or "sources" not in cfg:
        log.warning("config malformed; falling back to defaults in-memory")
        return DEFAULT_CONFIG
    return cfg


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------
def file_severity(line: str) -> str:
    low = line.lower()
    if re.search(r"\b(error|fail(ed|ure)?|critical|fatal|traceback|exception)\b", low):
        return "err"
    if re.search(r"\bwarn(ing)?\b", low):
        return "warning"
    return "info"


def collect_file(path, offset):
    """Read new complete lines from `path` starting at byte `offset`.

    Handles rotation/truncation: if offset > current size the file was rotated,
    so we reset to 0. Only advances past the last complete (newline-terminated)
    line so a mid-write tail is re-read next time. Returns (lines, new_offset).
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if offset > size:  # rotation or truncation
        offset = 0
    if offset == size:
        return [], offset
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    nl = data.rfind(b"\n")
    if nl == -1:  # no complete line yet; wait for more
        return [], offset
    complete = data[:nl + 1]
    new_offset = offset + len(complete)
    text = complete.decode("utf-8", "replace")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return lines, new_offset


def collect_file_source(src, src_cursor):
    """Collect a kind:file source. `path` may be a literal path or a glob.

    Per-resolved-path byte offsets live in src_cursor (a dict path->offset).
    A missing path/empty glob is a per-source warning, never fatal.
    """
    pattern = src.get("path", "")
    if any(c in pattern for c in "*?["):
        paths = sorted(glob.glob(pattern))
    else:
        paths = [pattern]
    offsets = dict(src_cursor) if isinstance(src_cursor, dict) else {}
    tagged = []
    warnings = []
    matched = False
    for p in paths:
        if not os.path.isfile(p):
            continue
        matched = True
        off = offsets.get(p, 0)
        lines, new_off = collect_file(p, off)
        offsets[p] = new_off
        for ln in lines:
            # Best-effort: file sources are opaque text in general, but rows
            # that happen to be JSON objects carrying an `audit_ts` (as the
            # keeper-relay audit log does) get their real per-row timestamp
            # so cluster-level debounces (e.g. the boot-window one) can reason
            # about individual failure times, not just line order. Any
            # non-JSON or audit_ts-less line falls back to ts=None exactly as
            # before -- this is additive and changes nothing for other kinds
            # of file sources.
            ts = None
            try:
                rec = json.loads(ln)
                if isinstance(rec, dict) and rec.get("audit_ts") is not None:
                    ts = float(rec["audit_ts"])
            except (ValueError, TypeError):
                ts = None
            tagged.append({"source": src["name"], "severity": file_severity(ln),
                           "ts": ts, "message": ln})
    if not matched:
        warnings.append("%s: no file matched %r" % (src["name"], pattern))
    return tagged, offsets, warnings


def collect_journal_source(src, cursor, first_run_since):
    """Collect a kind:journal source via journalctl -o json.

    Supports a severity `priority` and/or a list of `units` (multiple -u).
    Uses --after-cursor for incremental reads; first run uses --since.
    Returns (tagged_lines, new_cursor, warnings).
    """
    cmd = ["journalctl", "--no-pager", "-o", "json", "-n", "10000"]
    pr = src.get("priority")
    if pr:
        cmd += ["-p", str(pr)]
    for u in (src.get("units") or []):
        cmd += ["-u", u]
    if cursor:
        cmd += ["--after-cursor", cursor]
    else:
        cmd += ["--since", first_run_since]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        return [], cursor, ["%s: journalctl failed: %s" % (src["name"], e)]
    tagged = []
    new_cursor = cursor
    for raw in proc.stdout.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        msg = e.get("MESSAGE", "")
        if isinstance(msg, list):
            try:
                msg = bytes(msg).decode("utf-8", "replace")
            except (ValueError, TypeError):
                msg = str(msg)
        prio = e.get("PRIORITY")
        try:
            sev = PRIORITY_NAMES.get(int(prio), "info")
        except (TypeError, ValueError):
            sev = "info"
        unit = e.get("_SYSTEMD_UNIT") or e.get("SYSLOG_IDENTIFIER") or ""
        cur = e.get("__CURSOR")
        if cur:
            new_cursor = cur
        tsr = e.get("__REALTIME_TIMESTAMP")
        try:
            ts = int(tsr) / 1e6 if tsr else None
        except (TypeError, ValueError):
            ts = None
        # Downgrade known-benign self-healing transients to info (kept, not
        # dropped) so they stay visible/auditable but don't burn the err-budget.
        if sev_rank(sev) < sev_rank("info"):
            for unit_re, msg_re in BENIGN_JOURNAL_SIGNATURES:
                if unit_re.search(unit) and msg_re.search(msg):
                    sev = "info"
                    break
        message = ("%s: %s" % (unit, msg)) if unit else msg
        tagged.append({"source": src["name"], "severity": sev,
                       "ts": ts, "message": message})
    return tagged, new_cursor, []


# --------------------------------------------------------------------------
# Dedup
# --------------------------------------------------------------------------
_NORM_RE_HEX = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
_NORM_RE_NUM = re.compile(r"\d+")
_NORM_RE_WS = re.compile(r"\s+")


def normalize_message(msg: str) -> str:
    """Collapse volatile bits (long hex ids, numbers, whitespace) to a signature."""
    s = _NORM_RE_HEX.sub("<HEX>", msg)
    s = _NORM_RE_NUM.sub("0", s)
    s = _NORM_RE_WS.sub(" ", s).strip()
    return s


def dedup(tagged_lines):
    """Collapse duplicate lines to clusters keyed by (source, signature).

    Returns a list of clusters: {source, severity, count, first_ts, last_ts,
    message}. `message` is the first raw representative; severity is the most
    severe seen in the cluster.
    """
    clusters = {}
    order = []
    for i, item in enumerate(tagged_lines):
        sig = (item["source"], normalize_message(item["message"]))
        c = clusters.get(sig)
        ts = item.get("ts")
        if c is None:
            c = {"source": item["source"], "severity": item["severity"],
                 "count": 0, "first_ts": ts, "last_ts": ts,
                 "message": item["message"], "_seq": i, "_all_ts": []}
            clusters[sig] = c
            order.append(sig)
        c["count"] += 1
        if sev_rank(item["severity"]) < sev_rank(c["severity"]):
            c["severity"] = item["severity"]
        if ts is not None:
            if c["first_ts"] is None or ts < c["first_ts"]:
                c["first_ts"] = ts
            if c["last_ts"] is None or ts > c["last_ts"]:
                c["last_ts"] = ts
            # Private, internal-only: the full per-row ts list (not just
            # first/last) so a debounce can check EVERY row individually
            # (e.g. against several boot windows) rather than assume the
            # whole [first_ts, last_ts] span is uniformly explained by one
            # cause. Not part of the cluster's public/report-facing shape.
            c["_all_ts"].append(ts)
    return [clusters[s] for s in order]


def debounce_relay_transients(clusters):
    """Downgrade short-lived keeper-relay poll-failure clusters err -> warning.

    Stateless, cluster-level debounce. Only touches the keeper-relay-audit
    source and only fetch-failed transients. A cluster keeps `err` ONLY when it
    is both large (count >= RELAY_TRANSIENT_MIN_BURST) and sustained
    (span >= RELAY_TRANSIENT_MIN_SPAN); see those constants for the measured
    distribution this encodes. Everything shorter is a flap -> warning, kept
    visible in the report rather than dropped.

    Span comes from the per-row timestamps dedup() stashes in `_all_ts`. If
    those are unavailable we cannot judge duration, so we fall back to the
    count-only test -- fail toward err, the same posture as the rest of this
    module. Never upgrades: a cluster already at/below warning is untouched.
    Mutates and returns the same list.
    """
    for c in clusters:
        if c["source"] != "keeper-relay-audit":
            continue
        if not _RELAY_TRANSIENT_RE.search(c["message"] or ""):
            continue
        big = c["count"] >= RELAY_TRANSIENT_MIN_BURST
        tss = c.get("_all_ts") or []
        if tss:
            sustained = (max(tss) - min(tss)) >= RELAY_TRANSIENT_MIN_SPAN
        else:
            sustained = True          # duration unknown -> don't rescue it
        if big and sustained:
            continue                  # large AND sustained -> a real outage, keep err
        # only downgrade (never upgrade something already <= warning)
        if sev_rank(c["severity"]) < sev_rank("warning"):
            c["severity"] = "warning"
    return clusters


def _recent_boot_times():
    """Return recent boot start timestamps (epoch seconds), newest first.

    Prefers `journalctl --list-boots -o json`, one row per boot with a
    first_entry (first journal line of that boot) that we use as a boot-time
    proxy -- this station reboots multiple times in a single day (2026-07-22:
    4 boots), so the CURRENT boot alone isn't enough to explain a burst that
    started under an earlier boot within the same scan window. Falls back to
    /proc/stat's btime (current boot only) if journalctl is unavailable or
    gives us nothing usable. Returns [] -- meaning "unknown" -- if neither
    source works; callers MUST treat that as fail-toward-err, never downgrade.
    """
    try:
        proc = subprocess.run(["journalctl", "--list-boots", "-o", "json"],
                               capture_output=True, text=True, timeout=20)
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            boots = data.get("boots", data) if isinstance(data, dict) else data
            times = []
            for b in boots or []:
                fe = b.get("first_entry")
                if fe is not None:
                    times.append(float(fe) / 1e6)
            if times:
                return sorted(times, reverse=True)
    except (subprocess.TimeoutExpired, OSError, ValueError, TypeError,
            AttributeError):
        pass
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return [float(line.split()[1])]
    except (OSError, ValueError, IndexError):
        pass
    return []


def _ts_in_some_boot_window(ts, boot_times, grace):
    """True if `ts` falls within `grace` seconds after any boot in boot_times."""
    return any(b <= ts <= b + grace for b in boot_times)


def debounce_relay_boot_bursts(clusters, now=None, boot_times=None):
    """Downgrade RECOVERED, boot-aligned keeper-relay fetch-failed bursts.

    Separate from (and more targeted than) debounce_relay_transients(), which
    only forgives isolated blips below RELAY_TRANSIENT_MIN_BURST. A host
    power-cycle produces a burst of 6-8+ fetch-failed rows in the ~90s before
    the host bridge comes back -- big enough to blow past that threshold and
    fire a spurious bugerr ping every reboot (see RELAY_BOOT_GRACE comment
    above). This downgrades err -> warning ONLY when BOTH hold:
      1. boot-window: EVERY row's ts in the cluster (not just first/last) is
         within RELAY_BOOT_GRACE seconds of SOME recent boot -- there can be
         several boots in one scan window.
      2. recovered: the cluster's last row is at least RELAY_RECOVERY_QUIET
         seconds before `now` -- a burst still arriving at scan time (host
         still down, or an unrelated sustained outage like 2026-07-17's) is
         NOT recovered and keeps its `err` severity.
    Conservative by construction: unknown boot times, or a cluster with no
    per-row timestamps available (see dedup()'s `_all_ts`), both mean we
    fail toward err and leave the cluster untouched. Never upgrades anything
    already <= warning. Stateless; mutates and returns the same list.
    """
    if now is None:
        now = time.time()
    boots = boot_times if boot_times is not None else _recent_boot_times()
    if not boots:
        return clusters  # boot times unknown -> fail toward err
    for c in clusters:
        if c["source"] != "keeper-relay-audit":
            continue
        if not _RELAY_TRANSIENT_RE.search(c["message"] or ""):
            continue
        tss = c.get("_all_ts") or []
        if not tss:
            continue  # no per-row timestamps -> fail toward err
        if not all(_ts_in_some_boot_window(t, boots, RELAY_BOOT_GRACE) for t in tss):
            continue  # some row outside every boot window -> real/unclear issue
        if now - max(tss) < RELAY_RECOVERY_QUIET:
            continue  # burst hasn't gone quiet yet -> may still be ongoing
        if sev_rank(c["severity"]) < sev_rank("warning"):
            c["severity"] = "warning"
    return clusters


# --------------------------------------------------------------------------
# Sample building + citation gate
# --------------------------------------------------------------------------
def build_sample(clusters, cap=SAMPLE_CAP):
    """Render clusters as a numbered [Ln] sample, capped to ~cap bytes.

    Orders by severity (most severe first) then count desc so the cap keeps the
    important clusters. Returns (sample_text, line_map, capped_overall) where
    line_map maps line number -> cluster.
    """
    ordered = sorted(clusters, key=lambda c: (sev_rank(c["severity"]), -c["count"]))
    lines = []
    line_map = {}
    used = 0
    n = 0
    for c in ordered:
        cand_no = n + 1
        msg = c["message"].replace("\n", " ")
        if len(msg) > MSG_TRUNC:
            msg = msg[:MSG_TRUNC] + "..."
        text = "[L%d] count=%d sev=%s src=%s :: %s" % (
            cand_no, c["count"], c["severity"], c["source"], msg)
        if lines and used + len(text) + 1 > cap:
            break
        n = cand_no
        lines.append(text)
        line_map[n] = c
        used += len(text) + 1
    capped_overall = len(line_map) < len(clusters)
    return "\n".join(lines), line_map, capped_overall


def parse_claims(reply: str):
    """Split an LLM reply into claim units (bullets or paragraphs)."""
    claims = []
    buf = []
    bullet = re.compile(r"^([-*•]|\d+[.)])\s+")
    for raw in reply.splitlines():
        s = raw.strip()
        if not s:
            if buf:
                claims.append(" ".join(buf))
                buf = []
            continue
        if bullet.match(s):
            if buf:
                claims.append(" ".join(buf))
                buf = []
            buf = [bullet.sub("", s)]
        else:
            buf.append(s)
    if buf:
        claims.append(" ".join(buf))
    return [c for c in claims if c.strip()]


_CITE_RE = re.compile(r"\[L(\d+)\]")

# Reasoning-scratchpad stripping. Reasoning models emit a hidden <think>...</think>
# scratchpad that is not part of the answer; if it leaks it must never become a
# claim. Model-agnostic: handles well-formed blocks plus the common malformed
# variants (trimmed opener, missing closer, stray tokens).
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)


def strip_reasoning(reply: str) -> str:
    """Remove reasoning-model scratchpad from a raw reply, before claim parsing.

    - Drops complete <think>...</think> blocks (any number, case-insensitive).
    - Lone closing </think> with no opener (reasoning-then-answer, opener
      trimmed): keeps only what follows the last </think> -- unless nothing
      meaningful follows (a stray trailing tag on a real line), in which case the
      preceding text is kept and the token stripped.
    - Unterminated <think> with no closer: drops from that tag to end of string.
    - Strips any residual stray <think>/</think> tokens.
    """
    if not reply:
        return ""
    # 1. Remove well-formed blocks.
    text = _THINK_BLOCK_RE.sub("", reply)
    # 2. Lone closer (reasoning-then-answer). After block removal any remaining
    #    </think> is unmatched; the real answer, if any, is what follows the last.
    closers = list(_THINK_CLOSE_RE.finditer(text))
    if closers:
        after = text[closers[-1].end():]
        if after.strip():
            text = after
        # else: stray trailing tag -> keep preceding text; token stripped in (4).
    # 3. Unterminated opener -> reasoning runs to end of string.
    opener = _THINK_OPEN_RE.search(text)
    if opener:
        text = text[:opener.start()]
    # 4. Strip any residual stray tokens.
    text = _THINK_OPEN_RE.sub("", text)
    text = _THINK_CLOSE_RE.sub("", text)
    return text.strip()


def gate_claims(reply, valid_lines):
    """Lane-4 gate: keep only claims citing a real input line number, and drop
    prompt-scaffolding echoes.

    Returns (kept, counts). `kept` is a list of (claim_text, [valid_line_numbers]).
    `counts` is {"uncited": n, "scaffolding": n}: claims dropped for citing no
    real input line, and claims dropped as echoes of the digest instructions.
    """
    kept = []
    counts = {"uncited": 0, "scaffolding": 0}
    for claim in parse_claims(reply):
        if is_scaffolding_echo(claim):
            counts["scaffolding"] += 1
            continue
        cited = [int(m) for m in _CITE_RE.findall(claim)]
        good = sorted({n for n in cited if n in valid_lines})
        if good:
            kept.append((claim, good))
        else:
            counts["uncited"] += 1
    return kept, counts


def citation_gate(reply, valid_lines):
    """Back-compat 2-tuple wrapper over gate_claims.

    Returns (kept, discarded) where discarded = uncited + scaffolding echoes.
    """
    kept, counts = gate_claims(reply, valid_lines)
    return kept, counts["uncited"] + counts["scaffolding"]


# --------------------------------------------------------------------------
# LLM call
# --------------------------------------------------------------------------
DIGEST_SYSTEM = (
    "You are a log-triage assistant for a single Linux host. You receive a "
    "numbered list of deduplicated log clusters, one per line, each tagged "
    "[Ln] (n = line number), with count, severity and source. Report only real "
    "problems worth a human's attention. RULES: (1) EVERY bullet MUST cite the "
    "exact input line(s) it draws from with [Ln] tags, e.g. [L3] or [L2][L7]. "
    "(2) Never assert anything not supported by a cited line. (3) One concise "
    "bullet per distinct issue, most severe first. (4) If nothing is "
    "actionable, reply with the single bullet: '- No actionable issues [L1]'. "
    "Output ONLY bullets, no preamble."
)

# Prompt-echo rejection. A weight-cliff / reasoning brain sometimes parrots its
# own instructions back (stapling an [Ln] on so they pass the citation gate) or
# leaks a stray reasoning token. These are never real findings. Signal is drawn
# from DIGEST_SYSTEM itself: `_SCAFFOLD_FRAMING` are instruction-framing
# fragments that all appear in DIGEST_SYSTEM (a drift test asserts this), and
# `_DIGEST_TOKENS` is the prompt's own vocabulary -- a framing fragment only
# rejects when the claim is *substantially* the prompt (high token overlap), so
# a real log line that merely contains an instruction-ish word survives.
#
# CRITICAL: the legitimate rule-4 reply "- No actionable issues [L1]" carries no
# framing verb, so it is never rejected; only the *framing* sentence ("If
# nothing ... reply with ...: '- No actionable issues [L1]'") is, via "if
# nothing" / "reply with".
_SCAFFOLD_FRAMING = (
    "must cite", "output only", "never assert", "no preamble", "reply with",
    "if nothing", "you receive", "log-triage", "one concise bullet",
    "most severe first",
)
_THINK_TOKEN_RE = re.compile(r"</?think", re.IGNORECASE)
# The literal instruction placeholder "[Ln]" (letter n) -- never a real cite,
# which is always "[L<digits>]".
_LITERAL_LN_RE = re.compile(r"\[ln\]", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+")
_CITE_LOWER_RE = re.compile(r"\[l\d+\]", re.IGNORECASE)
_DIGEST_TOKENS = set(_WORD_RE.findall(DIGEST_SYSTEM.lower()))


def is_scaffolding_echo(claim: str) -> bool:
    """True when a claim is an echo of the digest instructions or a leaked
    reasoning token rather than an observation about a cited log line."""
    low = claim.lower()
    # Residual reasoning token that survived strip_reasoning.
    if _THINK_TOKEN_RE.search(low):
        return True
    # The verbatim "[Ln]" instruction placeholder.
    if _LITERAL_LN_RE.search(low):
        return True
    # Instruction-framing fragment -- confirm it is genuinely a prompt echo (not
    # a real log line that happens to contain the word) by requiring the claim's
    # words to overlap heavily with the prompt's own vocabulary.
    if any(f in low for f in _SCAFFOLD_FRAMING):
        toks = _WORD_RE.findall(_CITE_LOWER_RE.sub("", low))
        if toks and sum(1 for t in toks if t in _DIGEST_TOKENS) / len(toks) >= 0.6:
            return True
    return False


def call_llm(user_content, system_content, max_tokens, timeout, retries=1):
    """POST to the hugpy chat endpoint. Returns (content, wall_s, error)."""
    body = json.dumps({
        "model": LLM_MODEL,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
    }).encode()
    last_err = None
    wall = 0.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            LLM_URL, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + LLM_TOKEN})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
            wall = time.time() - t0
            content = payload["choices"][0]["message"]["content"]
            return content, wall, None
        except (urllib.error.URLError, TimeoutError, OSError,
                ValueError, KeyError, IndexError) as e:
            wall = time.time() - t0
            last_err = "%s: %s" % (type(e).__name__, e)
            log.warning("LLM call attempt %d failed after %.1fs: %s",
                        attempt + 1, wall, last_err)
    return None, wall, last_err


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------
def get_status():
    failed = []
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--failed", "--no-legend", "--plain",
             "--no-pager"], capture_output=True, text=True, timeout=20).stdout
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0].endswith(".service"):
                failed.append(parts[0])
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("list-units --failed failed: %s", e)
    key = {}
    for u in KEY_UNITS:
        try:
            r = subprocess.run(["systemctl", "is-active", u],
                               capture_output=True, text=True, timeout=10)
            key[u] = (r.stdout.strip() or "unknown")
        except (subprocess.TimeoutExpired, OSError):
            key[u] = "unknown"
    return {"failed_units": failed, "key_units": key}


# --------------------------------------------------------------------------
# Activity / system vitals snapshot (t46) — for the console activity meter.
#
# A best-effort, fail-soft snapshot embedded in every report. Each probe is
# guarded independently: any single failure yields null for THAT field and never
# fails the scan. Nothing here writes state; the relay's activity clock is read
# from its own state file, never reimplemented.
# --------------------------------------------------------------------------
def read_relay_activity(path=None):
    """Read the keeper-relay state file for (last_activity_ts, board_quiet_secs).

    Fully defensive — a missing file, unparseable JSON, a non-dict body, or an
    absent/wrong-typed key each yields None for that field, never an exception.
    We read ONLY flat keys the relay already persists in its state; we do NOT
    reimplement its env/default quiet-window resolution, so `board_quiet_secs`
    reads None until/unless the relay itself persists it in state (forward-compat:
    both singular `board_quiet_sec` and plural `board_quiet_secs` are accepted).
    """
    if path is None:
        path = KEEPER_RELAY_STATE_PATH
    last_activity_ts = None
    board_quiet_secs = None
    st = load_json(path, None)
    if isinstance(st, dict):
        v = st.get("last_activity_ts")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            last_activity_ts = float(v)
        q = st.get("board_quiet_sec", st.get("board_quiet_secs"))
        if isinstance(q, (int, float)) and not isinstance(q, bool):
            board_quiet_secs = int(q)
    return last_activity_ts, board_quiet_secs


def read_meminfo(path="/proc/meminfo"):
    """Return (mem_available_kb, mem_total_kb) from /proc/meminfo.

    Either field is None if unreadable/unparseable; never raises.
    """
    avail = total = None
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    try:
                        avail = int(line.split()[1])
                    except (IndexError, ValueError):
                        avail = None
                elif line.startswith("MemTotal:"):
                    try:
                        total = int(line.split()[1])
                    except (IndexError, ValueError):
                        total = None
                if avail is not None and total is not None:
                    break
    except OSError:
        pass
    return avail, total


def collect_activity():
    """Collect a defensive activity + system-vitals snapshot (see README contract).

    Every probe is independently guarded so any one failing degrades to null for
    that field alone, never a scan failure.
    """
    last_activity_ts, board_quiet_secs = read_relay_activity()

    try:
        loadavg = list(os.getloadavg())
    except (OSError, ValueError):
        loadavg = None

    mem_available_kb, mem_total_kb = read_meminfo()

    disk_free_gb = disk_total_gb = None
    try:
        du = shutil.disk_usage("/")
        disk_free_gb = round(du.free / 1e9, 2)
        disk_total_gb = round(du.total / 1e9, 2)
    except OSError:
        pass

    return {
        "keeper_last_activity_ts": last_activity_ts,
        "board_quiet_secs": board_quiet_secs,
        "loadavg": loadavg,
        "mem_available_kb": mem_available_kb,
        "mem_total_kb": mem_total_kb,
        "disk_free_gb": disk_free_gb,
        "disk_total_gb": disk_total_gb,
        "collected_ts": time.time(),
    }


# --------------------------------------------------------------------------
# Watches (S1 config validation + S2 evaluation/series) — deterministic, no LLM.
#
# A watch matches a regex (with named captures) against a source's new lines
# each cadence, groups by one capture (`key`), optionally tracks a status
# capture, and compiles a per-group status-over-time series. Fail-closed: any
# invalid watch is SKIPPED (never evaluated) but still surfaced in the payload
# with its `error`, so a misconfigured watch is visible, not silent.
# --------------------------------------------------------------------------
_WATCH_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_watch_name(name) -> str:
    """Map a watch name to a safe series-file stem (no path separators).

    Runs of non-[A-Za-z0-9._-] collapse to '_'; leading dots are stripped so we
    never produce '', '.', '..' or a hidden file. Slashes (the traversal vector)
    are never in the allowed set, so a resolved series path always stays inside
    SERIES_DIR.
    """
    stem = _WATCH_NAME_RE.sub("_", str(name))
    stem = stem.lstrip(".")
    return stem or "watch"


def series_path(name) -> str:
    return os.path.join(SERIES_DIR, sanitize_watch_name(name) + ".jsonl")


def compile_watches(watches_cfg):
    """Validate + compile watch configs. PURE (no I/O) so it is trivially testable.

    Returns a list of compiled dicts, one per input watch (order preserved):
        {name, enabled, error, regex, kind, unit, path, key, status}
    `error` is None when valid; a non-empty reason string marks the watch
    invalid — it is kept in the list (for payload visibility) but never
    evaluated. Anything malformed becomes an error, never an exception.
    """
    out = []
    if not isinstance(watches_cfg, list):
        return out
    for i, w in enumerate(watches_cfg):
        e = {"name": "watch[%d]" % i, "enabled": True, "error": None,
             "regex": None, "kind": None, "unit": None, "path": None,
             "key": None, "status": None,
             # http-kind fields (None for line kinds):
             "url": None, "id_field": None, "key_field": None,
             "status_field": None, "capture": None}
        if not isinstance(w, dict):
            e["enabled"] = False
            e["error"] = "watch config is not an object"
            out.append(e)
            continue
        name = w.get("name")
        if isinstance(name, str) and name.strip():
            e["name"] = name
        e["enabled"] = bool(w.get("enabled", True))

        src = w.get("source")
        if not isinstance(src, dict):
            e["error"] = "missing/invalid 'source' object"
            out.append(e)
            continue
        kind = src.get("kind")
        e["kind"] = kind
        if kind == "journal":
            unit = src.get("unit")
            if not isinstance(unit, str) or not unit:
                e["error"] = "journal source requires a 'unit' string"
                out.append(e)
                continue
            e["unit"] = unit
        elif kind == "file":
            path = src.get("path")
            if not isinstance(path, str) or not path:
                e["error"] = "file source requires a 'path' string"
                out.append(e)
                continue
            e["path"] = path
        elif kind == "http":
            # http watches are field-mapped, NOT regex-mapped: they require a url
            # + three JSON field names, and MUST NOT require pattern/key/status.
            # Validated + appended here, so the line-kind block below never runs.
            url = src.get("url")
            if not isinstance(url, str) or not url:
                e["error"] = "http source requires a 'url' string"
                out.append(e)
                continue
            e["url"] = url
            missing = [fn for fn in ("id_field", "key_field", "status_field")
                       if not isinstance(w.get(fn), str) or not w.get(fn)]
            if missing:
                e["error"] = ("http watch requires %s (JSON field-name string%s)"
                              % (", ".join(missing), "s" if len(missing) > 1 else ""))
                out.append(e)
                continue
            e["id_field"] = w["id_field"]
            e["key_field"] = w["key_field"]
            e["status_field"] = w["status_field"]
            capture = w.get("capture", [])
            if capture is None:
                capture = []
            if not isinstance(capture, list) or not all(
                    isinstance(c, str) and c for c in capture):
                e["error"] = "'capture' must be a list of JSON field-name strings"
                out.append(e)
                continue
            e["capture"] = list(capture)
            out.append(e)
            continue
        else:
            e["error"] = ("unknown source kind %r (want 'journal', 'file' or 'http')"
                          % (kind,))
            out.append(e)
            continue

        pattern = w.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            e["error"] = "missing 'pattern' regex"
            out.append(e)
            continue
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            e["error"] = "bad regex: %s" % exc
            out.append(e)
            continue
        groups = rx.groupindex

        key = w.get("key")
        if not isinstance(key, str) or not key:
            e["error"] = "missing 'key' capture name"
            out.append(e)
            continue
        if key not in groups:
            e["error"] = "key %r is not a named capture in the pattern" % key
            out.append(e)
            continue

        status = w.get("status")
        if status is not None:
            if not isinstance(status, str) or not status:
                e["error"] = "'status' must be a capture-name string when present"
                out.append(e)
                continue
            if status not in groups:
                e["error"] = "status %r is not a named capture in the pattern" % status
                out.append(e)
                continue
            e["status"] = status

        e["regex"] = rx
        e["key"] = key
        out.append(e)
    return out


def collect_for_watch(w, src_cursor, first_run_since):
    """Collect a valid watch's new (message, ts) items incrementally.

    Reuses the triage collectors with the watch's PRIVATE cursor (a byte-offset
    dict for files, a journal cursor for journal). Returns (items, new_cursor,
    warnings) where each item is a tagged dict carrying 'message' and 'ts'.
    """
    if w["kind"] == "file":
        src = {"name": w["name"], "kind": "file", "path": w["path"]}
        return collect_file_source(src, src_cursor)
    if w["kind"] == "journal":
        src = {"name": w["name"], "kind": "journal", "units": [w["unit"]]}
        return collect_journal_source(src, src_cursor, first_run_since)
    return [], src_cursor, []


def _series_line_count(path) -> int:
    n = 0
    try:
        with open(path, "rb") as f:
            for _ in f:
                n += 1
    except OSError:
        return 0
    return n


def append_series(name, records):
    """Append match records to the per-watch series jsonl, then ring-cap it.

    Append uses the audit_append idiom (plain append); the ring-cap REWRITE is
    atomic (atomic_write), per the brief.
    """
    if not records:
        return
    path = series_path(name)
    os.makedirs(SERIES_DIR, exist_ok=True)
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    if _series_line_count(path) > SERIES_CAP:
        cap_series(path)


def cap_series(path):
    """Atomically rewrite a series file keeping only the newest SERIES_KEEP lines."""
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= SERIES_KEEP:
        return
    atomic_write(path, "".join(lines[-SERIES_KEEP:]))


def fold_series(name):
    """Fold a watch's series file into per-key groups.

    Returns (groups, matched_total, groups_truncated). Each group:
        {status: <latest by ts>, last_ts: <max ts>, count: C,
         statuses: {<status>: count, ...}}
    Groups are capped at the WATCH_GROUPS_CAP most-recently-active keys;
    groups_truncated is True when that cap dropped keys.
    """
    path = series_path(name)
    groups = {}
    total = 0
    try:
        f = open(path, "r")
    except OSError:
        return {}, 0, False
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            total += 1
            key = rec.get("key")
            if key is None:
                continue
            key = str(key)
            g = groups.get(key)
            if g is None:
                g = {"status": None, "last_ts": None, "count": 0, "statuses": {}}
                groups[key] = g
            g["count"] += 1
            status = rec.get("status")
            status = str(status) if status is not None else None
            if status is not None:
                g["statuses"][status] = g["statuses"].get(status, 0) + 1
            ts = rec.get("ts")
            try:
                ts = float(ts) if ts is not None else None
            except (TypeError, ValueError):
                ts = None
            if ts is not None:
                if g["last_ts"] is None or ts >= g["last_ts"]:
                    g["last_ts"] = ts
                    g["status"] = status
            elif g["count"] == 1:  # no ts anywhere yet -> first record's status
                g["status"] = status
    truncated = False
    if len(groups) > WATCH_GROUPS_CAP:
        ordered = sorted(
            groups.items(),
            key=lambda kv: (kv[1]["last_ts"] is not None, kv[1]["last_ts"] or 0.0),
            reverse=True)
        groups = {k: v for k, v in ordered[:WATCH_GROUPS_CAP]}
        truncated = True
    return groups, total, truncated


def build_watch_entry(w, matched_last_scan):
    """Build one payload row for a watch by folding its (possibly empty) series."""
    groups, total, truncated = fold_series(w["name"])
    return {
        "name": w["name"],
        "enabled": w["enabled"],
        "error": w["error"],
        "matched_total": total,
        "matched_last_scan": matched_last_scan,
        "groups": groups,
        "groups_truncated": truncated,
    }


# ----- http watch source kind (S3): poll + transition-dedup + accumulate ------
def parse_epoch_or_iso(value):
    """Parse a timestamp that may be an epoch number, a numeric string, or an
    ISO-8601 string, into a float epoch (or None; never raises). Naive ISO values
    are read as UTC so ordering stays stable across rows. (hugpy mixes forms:
    `progressed_at` is an epoch float, `updated_at`/`created_at` are ISO strings.)
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)                       # epoch encoded as a string
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


_HTTP_ROW_TS_FIELDS = ("progressed_at", "updated_at", "created_at")


def _http_row_ts(row, now):
    """Best ts for a jobs row: progressed_at > updated_at > created_at > scan time."""
    for f in _HTTP_ROW_TS_FIELDS:
        ts = parse_epoch_or_iso(row.get(f))
        if ts is not None:
            return ts
    return now


def _extract_rows(data):
    """Coerce a jobs response to a list of rows: a bare array, or a
    {jobs|data|results|items: [...]} wrapper. Returns None if neither shape.
    (dev.hugpy.ai wraps under `jobs`; the others are defensive for drift.)
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("jobs", "data", "results", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return None


def fetch_http_rows(url, timeout=HTTP_TIMEOUT):
    """GET `url` and return (rows, error). One shot, no retries (next cadence is
    the retry). A non-200 (HTTPError), timeout, transport error, non-JSON body,
    or an unrecognised JSON shape each yields (None, error-string), never raises.
    """
    req = urllib.request.Request(url, method="GET",
                                 headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, "%s: %s" % (type(e).__name__, e)
    try:
        data = json.loads(raw)
    except ValueError as e:
        return None, "non-JSON response: %s" % e
    rows = _extract_rows(data)
    if rows is None:
        return None, ("unexpected JSON shape "
                      "(want array or {jobs|data|results|items:[...]})")
    return rows, None


def poll_http_watch(w, seen, now):
    """Poll one http watch and transition-dedup its rows against `seen`, a
    recency-ordered {id: last_status} dict MUTATED IN PLACE.

    Returns (records, error, skipped, pruned_now):
      - records: new series records for (id,status) TRANSITIONS — a new id, OR a
        known id whose status changed. A row whose (id,status) is unchanged is a
        no-op (its recency is refreshed so live rows never prune out under it).
      - error: the fetch error string, or None. On error `seen` is left UNTOUCHED
        (a failed poll must never wipe state -> no false re-fires on recovery).
      - skipped: rows that are not objects or miss id/key/status — counted, never
        fatal.
      - pruned_now: True iff this poll pruned oldest-seen ids past HTTP_STATE_CAP.
    """
    rows, err = fetch_http_rows(w["url"])
    if err is not None:
        return [], err, 0, False
    records = []
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        rid = row.get(w["id_field"])
        keyval = row.get(w["key_field"])
        statusval = row.get(w["status_field"])
        if rid is None or keyval is None or statusval is None:
            skipped += 1
            continue
        rid = str(rid)
        statusval = str(statusval)
        prev = seen.get(rid)
        seen.pop(rid, None)          # delete+reinsert => id moves to newest end
        seen[rid] = statusval
        if prev == statusval:
            continue                  # unchanged (id,status): rolling re-read no-op
        fields = {"id": rid}
        for cap in (w["capture"] or []):
            v = row.get(cap)
            if v is not None:
                fields[cap] = v
        records.append({"ts": _http_row_ts(row, now), "key": str(keyval),
                        "status": statusval, "fields": fields})
    pruned_now = False
    if len(seen) > HTTP_STATE_CAP:
        pruned_now = True
        for k in list(seen.keys())[:len(seen) - HTTP_STATE_CAP]:
            del seen[k]
    return records, None, skipped, pruned_now


def _evaluate_http(w, http_state, now):
    """Evaluate one http watch: poll, append its transition records to the series,
    persist the per-watch state (seen-map + audit-once flags), and build the
    payload row. Returns (entry, matched_last_scan, warnings). A poll failure
    surfaces as the row's `error`, leaves state intact, and never raises.
    """
    st = http_state.get(w["name"])
    if not isinstance(st, dict):
        st = {}
    seen = st.get("seen")
    if not isinstance(seen, dict):
        seen = {}
    last_error = st.get("last_error")
    pruned_before = bool(st.get("pruned"))

    records, err, skipped, pruned_now = poll_http_watch(w, seen, now)
    warnings = []

    if err is not None:
        # audit ONCE per distinct error string (cfg-relay audit-once idiom); the
        # seen-map is preserved untouched so recovery does not re-fire history.
        if err != last_error:
            audit_append({"ts": now, "event": "watch-http-error",
                          "name": w["name"], "error": err})
        http_state[w["name"]] = {"seen": seen, "last_error": err,
                                 "pruned": pruned_before}
        warnings.append("%s: endpoint poll failed: %s" % (w["name"], err))
        entry = build_watch_entry(w, 0)
        entry["error"] = err          # surface the transient error in the row
        return entry, 0, warnings

    if records:
        append_series(w["name"], records)
    if skipped:
        warnings.append("%s: skipped %d malformed/incomplete row(s)"
                        % (w["name"], skipped))
    if pruned_now and not pruned_before:   # audit the FIRST prune only
        audit_append({"ts": now, "event": "watch-http-prune",
                      "name": w["name"], "cap": HTTP_STATE_CAP})
    http_state[w["name"]] = {"seen": seen, "last_error": None,
                             "pruned": pruned_before or pruned_now}
    return build_watch_entry(w, len(records)), len(records), warnings


def save_http_state(state):
    """Persist http transition state WITHOUT sort_keys — each per-watch seen-map is
    recency-ordered (insertion order IS the prune signal), which save_json's
    sort_keys=True would silently destroy. json preserves dict insertion order.
    """
    os.makedirs(os.path.dirname(HTTP_STATE_PATH), exist_ok=True)
    atomic_write(HTTP_STATE_PATH, json.dumps(state, indent=2))


def evaluate_watches(compiled, watch_cursors, first_run_since, now, http_state=None):
    """Evaluate compiled watches incrementally, append series, build payload rows.

    Returns (entries, watch_cursors, evaluated, total_matches, warnings).
    Invalid/disabled watches are NOT evaluated but STILL get a payload row
    (folded from any existing series) so the operator sees them armed, dry, or
    broken — never silently absent. One watch's error never kills the scan.
    `http_state` (default {}) carries per-http-watch transition state across scans.
    """
    entries = []
    warnings = []
    evaluated = 0
    total_matches = 0
    if http_state is None:
        http_state = {}
    for w in compiled:
        if w["error"] or not w["enabled"]:
            entries.append(build_watch_entry(w, 0))
            continue
        evaluated += 1
        try:
            if w["kind"] == "http":
                entry, matched, warns = _evaluate_http(w, http_state, now)
                warnings.extend(warns)
                total_matches += matched
                entries.append(entry)
                continue
            items, newcur, warns = collect_for_watch(
                w, watch_cursors.get(w["name"]), first_run_since)
            watch_cursors[w["name"]] = newcur
            warnings.extend(warns)
            records = []
            for it in items:
                m = w["regex"].search(it.get("message", ""))
                if not m:
                    continue
                gd = m.groupdict()
                keyval = gd.get(w["key"])
                if keyval is None:  # key group didn't participate in this match
                    continue
                statusval = gd.get(w["status"]) if w["status"] else None
                fields = {k: v for k, v in gd.items()
                          if v is not None and k != w["key"] and k != w["status"]}
                its = it.get("ts")
                records.append({
                    "ts": float(its) if its is not None else now,
                    "key": keyval,
                    "status": statusval,
                    "fields": fields,
                })
            append_series(w["name"], records)
            total_matches += len(records)
            entries.append(build_watch_entry(w, len(records)))
        except Exception as exc:  # never let one watch kill the scan
            warnings.append("%s: watch eval error: %s" % (w["name"], exc))
            entries.append(build_watch_entry(w, 0))
    return entries, watch_cursors, evaluated, total_matches, warnings


# --------------------------------------------------------------------------
# Scan orchestration
# --------------------------------------------------------------------------
def run_scan(config, trigger, subset=None, force_digest=False, do_llm=True):
    """Run one full scan, write bugreport.json atomically, audit, return report."""
    cursors = load_json(CURSOR_PATH, {})
    if not isinstance(cursors, dict):
        cursors = {}
    meta = load_json(META_PATH, {})
    if not isinstance(meta, dict):
        meta = {}
    first_run_since = config.get("first_run_since", "-1h")

    sources = [s for s in config.get("sources", []) if s.get("enabled", True)]
    if subset:
        sources = [s for s in sources if s.get("name") in subset]

    all_tagged = []
    per_source_raw = {}
    warnings = []
    for s in sources:
        name = s.get("name", "?")
        try:
            if s.get("kind") == "journal":
                lines, newc, w = collect_journal_source(
                    s, cursors.get(name), first_run_since)
                cursors[name] = newc
            elif s.get("kind") == "file":
                lines, newoff, w = collect_file_source(s, cursors.get(name))
                cursors[name] = newoff
            else:
                lines, w = [], ["%s: unknown kind %r" % (name, s.get("kind"))]
        except Exception as e:  # never let one source kill the scan
            lines, w = [], ["%s: collect error: %s" % (name, e)]
        per_source_raw[name] = len(lines)
        all_tagged.extend(lines)
        warnings.extend(w)

    for w in warnings:
        log.warning(w)

    clusters = dedup(all_tagged)
    debounce_relay_transients(clusters)
    debounce_relay_boot_bursts(clusters)
    sample, line_map, capped_overall = build_sample(clusters)
    valid_lines = set(line_map.keys())

    # Deterministic findings (always present) — one per sampled cluster.
    findings = []
    for n in sorted(line_map):
        c = line_map[n]
        findings.append({
            "severity": c["severity"],
            "claim": "%dx %s" % (c["count"], c["message"][:MSG_TRUNC]),
            "citations": [n],
            "source": c["source"],
        })

    # Digest gating: residue AND (forced OR err-level present OR hourly elapsed).
    now = time.time()
    residue = len(clusters) > 0
    error_present = any(sev_rank(c["severity"]) <= 3 for c in clusters)
    since_digest = now - meta.get("last_digest_ts", 0)
    digest_meta = {"ran": False, "model": LLM_MODEL, "wall_s": None,
                   "uncited_discarded": 0, "scaffolding_discarded": 0,
                   "error": None}

    # Failure backoff: while the LLM endpoint is down, the err-cluster force
    # would re-fail every 15-min cadence (each failure = relay noise). After 3
    # straight failures retry at most hourly; on-demand (force) always tries;
    # the first success resets the streak.
    fail_streak = meta.get("digest_fail_streak", 0)
    since_attempt = now - meta.get("digest_attempt_ts", 0)
    backed_off = (fail_streak >= 3 and since_attempt < DIGEST_MIN_INTERVAL
                  and not force_digest)
    digest_meta["fail_streak"] = fail_streak
    digest_meta["backed_off"] = backed_off

    want_digest = (do_llm and residue and sample and not backed_off and
                   (force_digest or error_present or since_digest >= DIGEST_MIN_INTERVAL))
    if want_digest:
        user = ("Deduplicated log clusters (count=occurrences, sev=severity, "
                "src=source):\n\n" + sample)
        reply, wall, err = call_llm(user, DIGEST_SYSTEM, DIGEST_MAX_TOKENS,
                                    DIGEST_TIMEOUT, retries=1)
        digest_meta["wall_s"] = round(wall, 1) if wall else None
        meta["digest_attempt_ts"] = now
        if err:
            digest_meta["error"] = err  # advisory only; deterministic summary stands
            meta["digest_fail_streak"] = fail_streak + 1
        else:
            meta["digest_fail_streak"] = 0
            # Strip any reasoning scratchpad before parsing/gating, then drop
            # uncited claims and prompt-scaffolding echoes.
            kept, gate_counts = gate_claims(strip_reasoning(reply), valid_lines)
            digest_meta["ran"] = True
            digest_meta["uncited_discarded"] = gate_counts["uncited"]
            digest_meta["scaffolding_discarded"] = gate_counts["scaffolding"]
            for claim, good in kept:
                clist = [line_map[n] for n in good]
                sev = min(clist, key=lambda c: sev_rank(c["severity"]))["severity"]
                findings.append({
                    "severity": sev,
                    "claim": claim[:500],
                    "citations": good,
                    "source": "digest",
                })
            meta["last_digest_ts"] = now

    # Per-source report rows.
    src_report = []
    for s in sources:
        name = s.get("name", "?")
        uniq = sum(1 for c in clusters if c["source"] == name)
        incl = sum(1 for n in line_map if line_map[n]["source"] == name)
        src_report.append({
            "name": name,
            "kind": s.get("kind"),
            "enabled": True,
            "lines_seen": per_source_raw.get(name, 0),
            "unique": uniq,
            "capped": incl < uniq,
        })

    # Watches (deterministic; wholly independent of the LLM/digest path).
    compiled_watches = compile_watches(config.get("watches", []))
    for w in compiled_watches:
        if w["error"]:
            log.warning("watch %s invalid: %s", w["name"], w["error"])
            audit_append({"ts": now, "event": "watch-invalid",
                          "name": w["name"], "error": w["error"]})
    watch_cursors = load_json(WATCH_CURSOR_PATH, {})
    if not isinstance(watch_cursors, dict):
        watch_cursors = {}
    http_state = load_json(HTTP_STATE_PATH, {})
    if not isinstance(http_state, dict):
        http_state = {}
    (watch_entries, watch_cursors, watches_evaluated,
     watch_matches, watch_warns) = evaluate_watches(
        compiled_watches, watch_cursors, first_run_since, now, http_state)
    for wmsg in watch_warns:
        log.warning(wmsg)
    warnings.extend(watch_warns)
    if compiled_watches:
        save_json(WATCH_CURSOR_PATH, watch_cursors)
    if any(w["kind"] == "http" for w in compiled_watches):
        save_http_state(http_state)

    report = {
        "schema": "bugreport.v1",
        "generated_ts": now,
        "status": get_status(),
        "sources": src_report,
        "findings": findings,
        "digest_meta": digest_meta,
        "watches": watch_entries,
        "activity": collect_activity(),
    }

    atomic_write(REPORT_PATH, json.dumps(report, indent=2))
    save_json(CURSOR_PATH, cursors)
    meta["last_scan_ts"] = now
    save_json(META_PATH, meta)

    audit_append({
        "ts": now,
        "event": "scan",
        "trigger": trigger,
        "sources": [s.get("name") for s in sources],
        "lines": len(all_tagged),
        "unique": len(clusters),
        "digest_ran": digest_meta["ran"],
        "digest_wall_s": digest_meta["wall_s"],
        "uncited_discarded": digest_meta["uncited_discarded"],
        "capped": capped_overall,
        "watches_evaluated": watches_evaluated,
        "watch_matches": watch_matches,
    })
    if digest_meta["ran"] or digest_meta["error"]:
        audit_append({
            "ts": now,
            "event": "digest",
            "trigger": trigger,
            "wall_s": digest_meta["wall_s"],
            "uncited_discarded": digest_meta["uncited_discarded"],
            "scaffolding_discarded": digest_meta["scaffolding_discarded"],
            "error": digest_meta["error"],
        })
    return report, warnings


# --------------------------------------------------------------------------
# Request file (the GUI contract)
# --------------------------------------------------------------------------
def consume_request(path):
    """At-most-once: read then unlink the request file. Returns dict|None.

    A malformed body yields {"_malformed": True} so the caller can log+ignore.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    try:
        os.unlink(path)  # consume before acting => at-most-once
    except OSError:
        pass
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {"_malformed": True}
    if not isinstance(d, dict):
        return {"_malformed": True}
    return d


def handle_request(config, req):
    if req.get("_malformed") or req.get("op") != "scan":
        log.warning("ignoring malformed/unknown request: %r",
                    {k: req.get(k) for k in ("op",)} if isinstance(req, dict) else req)
        audit_append({"ts": time.time(), "event": "request",
                      "decision": "ignored"})
        return
    subset = req.get("sources")
    if subset is not None and not isinstance(subset, list):
        subset = None
    audit_append({"ts": time.time(), "event": "request",
                  "decision": "accepted", "sources": subset})
    log.info("on-demand scan requested (sources=%s)", subset)
    run_scan(config, trigger="request", subset=subset, force_digest=True)


# --------------------------------------------------------------------------
# Service loop / CLI
# --------------------------------------------------------------------------
def serve():
    log.info("bugreport-scanner starting (cadence=%ds poll=%ds)", CADENCE, POLL)
    config = ensure_config()
    try:
        run_scan(config, trigger="startup")
    except Exception as e:
        log.exception("startup scan failed: %s", e)
    last_cadence = time.time()
    while True:
        try:
            req = consume_request(REQUEST_PATH)
            if req is not None:
                handle_request(ensure_config(), req)
            if time.time() - last_cadence >= CADENCE:
                run_scan(ensure_config(), trigger="cadence")
                last_cadence = time.time()
        except Exception as e:
            log.exception("loop iteration error: %s", e)
        time.sleep(POLL)


def main(argv=None):
    ap = argparse.ArgumentParser(description="bugreport-scanner (lane #4)")
    ap.add_argument("--serve", action="store_true", help="run the service loop")
    ap.add_argument("--once", action="store_true", help="run a single scan and exit")
    ap.add_argument("--trigger", default="manual", help="trigger label for --once")
    ap.add_argument("--force-digest", action="store_true",
                    help="force a digest on --once (bypass hourly gate)")
    ap.add_argument("--no-llm", action="store_true",
                    help="deterministic only, skip the LLM digest")
    ap.add_argument("--readiness", action="store_true",
                    help="tiny echo call to check the endpoint is warm")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.readiness:
        content, wall, err = call_llm("Reply with the single word: ready",
                                      "You are a health check. Reply with one word.",
                                      max_tokens=16, timeout=60, retries=1)
        print(json.dumps({"ready": err is None, "wall_s": round(wall, 1),
                          "content": content, "error": err}))
        return 0 if err is None else 1

    if args.once:
        cfg = ensure_config()
        report, warnings = run_scan(cfg, trigger=args.trigger,
                                    force_digest=args.force_digest,
                                    do_llm=not args.no_llm)
        print(json.dumps({"report_path": REPORT_PATH,
                          "findings": len(report["findings"]),
                          "digest_meta": report["digest_meta"],
                          "warnings": warnings}, indent=2))
        return 0

    # default: serve
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
