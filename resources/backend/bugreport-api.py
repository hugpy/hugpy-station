#!/usr/bin/env python3
"""bugreport-api — host sidecar for /api/vm/<vm>/bugreport (board o5).
GET  -> lxc file pull <vm><VM_HOME>/bugreport.json
POST {"op":"scan","sources":[...]} -> lxc file push <vm><VM_HOME>/bugreport-request.json
Also serves (layered in, each with its own nginx location + per-op key whitelist):
  POST /api/vm/<vm>/finder         run the VM's finder CLI as the VM user (t21/t29)
  POST /api/vm/<vm>/keeper-ask     request bus: {type} -> {id,path,summary}, bytes spooled to VM, never inline (build-step 2)
  GET  /api/vm/<vm>/todo-history   tail the VM's board journal (t32)
  POST /api/vm/<vm>/todo-revision  write/read long board-item revisions (t49)
  GET  /api/vm/<vm>/steward        read the keeper steward state + config (t54)
  POST /api/vm/<vm>/steward        write steward config (mode/timeout + optional threshold_tokens (THE reset bar) / threshold_bytes (inert) / max_age_min, preserved when absent), an extend hold, or a manual reset request (t54, t89)
  GET  /api/vm/<vm>/keeper-env     read hugpy-keeper.env presence (never the key value) (p13)
  POST /api/vm/<vm>/keeper-env     set/rotate KEEPER_API_KEY / HUGPY_BASE_URL / HUGPY_KEEPER_MODEL (p13)
  GET  /api/vm/<vm>/flow           read the VM's ~/flow.json flow.v1 doc (o19, serving t84)
  POST /api/vm/<vm>/flow           validate + write a flow.v1 doc to ~/flow.json, echo the re-read (o19, serving t84)
Binds 127.0.0.1 ONLY by default — nginx fronts it inside the console vhosts
(their auth applies). Paths inside the VM are hard-pinned; vm names and
source names are whitelist-validated.

De-site-ified from the live ae/solcatcher deployment for @hugpy/vm-mgr
packaging (board t62 phase 2a). Nothing here reads a secret file — every
site-specific value below is an env var with a documented default; the
defaults intentionally match the conventional golden-image layout
(VM user "ubuntu", uid/gid 1000/1000, home /home/ubuntu) rather than any
one fleet's actual VM names or hostnames.
"""
import json, math, os, re, subprocess, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- host-side bind/port -----------------------------------------------------
# Loopback-only by design: this sidecar has NO auth of its own — the console
# vhost's nginx `location` blocks + basic-auth/allowlist are what actually gate
# access (see nginx-locations.conf.in in this same directory). Only override
# BUGREPORT_API_BIND if you understand that consequence.
BIND_HOST = os.environ.get("BUGREPORT_API_BIND", "127.0.0.1")
PORT = int(os.environ.get("BUGREPORT_API_PORT", "8793"))

# --- VM-side conventions ------------------------------------------------------
# The golden-image convention this sidecar assumes inside every target VM:
# a single non-root login user at this home dir/uid/gid. Override per-fleet
# via env if your golden image differs — these are NOT specific hostnames or
# fleet identities, just the standard single-user VM layout.
VM_HOME = os.environ.get("BUGREPORT_VM_HOME", "/home/ubuntu").rstrip("/")
VM_UID = os.environ.get("BUGREPORT_VM_UID", "1000")
VM_GID = os.environ.get("BUGREPORT_VM_GID", "1000")

VM_RE = re.compile(r"^/api/vm/([a-zA-Z0-9][a-zA-Z0-9-]{0,62})/bugreport$")
SRC_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# --- bridge mail route: the ✉ MESSAGES tab's long-missing backend (2026-08-12).
# Read-only tail of <VM_HOME>/.bridge-mail.jsonl — the durable per-station log
# the in-VM bridge-inbox-watcher appends every received vm-bridge message to
# (broker queues are in-memory; this file is the record). Rows share an id;
# the watcher appends an UPDATED row after triage (disposition/outcome), so we
# dedupe by id keeping the LAST row. Returned oldest->newest (the UI reverses).
MESSAGES_RE = re.compile(r"^/api/vm/([a-zA-Z0-9][a-zA-Z0-9-]{0,62})/messages$")
MESSAGES_MAX = 100          # newest messages returned

# --- board history route (t32): read-only tail of the VM's append-only journal ---
HISTORY_RE = re.compile(r"^/api/vm/([a-zA-Z0-9][a-zA-Z0-9-]{0,62})/todo-history$")
HISTORY_MAX = 1024 * 1024   # 1MB pull cap; if the journal is bigger keep the LAST bytes
HISTORY_EVENTS = 500        # return at most the newest this many events

# --- finder route (t21): run the VM's finder CLI as the VM user, argv-only ---
FINDER_RE = re.compile(r"^/api/vm/([a-zA-Z0-9][a-zA-Z0-9-]{0,62})/finder$")
# The finder CLI's install path inside the VM. Default matches the todo-cli /
# finder onboarding convention (~/.local/bin/<name>, symlinked in per-VM);
# override if your onboarding installs it elsewhere.
FINDER_BIN = os.environ.get("BUGREPORT_FINDER_BIN", VM_HOME + "/.local/bin/finder")
EXT_RE = re.compile(r"^\.[A-Za-z0-9_]{1,12}$")
VAL_RE = re.compile(r"^[^\x00]{1,120}$")
STR_RE = re.compile(r"^[^\x00]{1,200}$")
IMP_RE = re.compile(r"^[A-Za-z0-9_.]{1,80}$")
LINES_RE = re.compile(r"^\d{1,7}(-\d{1,7})?(,\d{1,7}(-\d{1,7})?)*$")  # view: 1-based line ranges
DATE_RE = re.compile(r"^[A-Za-z0-9 :/@,._+-]{1,40}$")   # collect: a find -newermt date / @epoch
SORT_RE = re.compile(r"^(name|mtime|size)$")             # collect: sort key
NUM_RE  = re.compile(r"^\d{1,15}$")                      # collect: size/limit/maxdepth
TYPE_RE = re.compile(r"^[fdl]$")                          # collect: entry type
# per-op whitelists: body key -> (cli flag, value regex); booleans separate
FINDER_OPS = {
    "collect": {"ext": ("--ext", EXT_RE), "exclude_ext": ("--exclude-ext", EXT_RE),
                "exclude_dir": ("--exclude-dir", VAL_RE), "after": ("--after", DATE_RE),
                "before": ("--before", DATE_RE), "sort": ("--sort", SORT_RE),
                "min_size": ("--min-size", NUM_RE), "max_size": ("--max-size", NUM_RE),
                "limit": ("--limit", NUM_RE), "maxdepth": ("--maxdepth", NUM_RE),
                "type": ("--type", TYPE_RE)},
    "dirs":    {},   # depth-1 subdir listing: no list-valued keys, only the no_add bool
    "grep":    {"ext": ("--ext", EXT_RE), "exclude_ext": ("--exclude-ext", EXT_RE),
                "dir": ("--dir", VAL_RE), "exclude_dir": ("--exclude-dir", VAL_RE),
                "pattern": ("--pattern", VAL_RE), "exclude_pattern": ("--exclude-pattern", VAL_RE),
                "string": ("--string", STR_RE)},
    "imports": {},
    "view":    {"lines": ("--lines", LINES_RE)},
}
FINDER_BOOLS = {"collect": {"no_add": "--no-add", "no_recursive": "--no-recursive",
                            "reverse": "--reverse", "meta": "--meta"},
                "dirs": {"no_add": "--no-add"},
                "grep": {"no_add": "--no-add", "no_recursive": "--no-recursive", "any": "--any"},
                "imports": {}, "view": {}}

# --- revision route (t49): store/fetch a long board-item revision as a VM file ---
# POST {op:"write", id, text} -> atomic write <VM_HOME>/board-revisions/<id>-<ts>.md, return path+bytes
# POST {op:"read", path}      -> pull that file's text back (path pinned under the dir)
# --- view-push route: a queue an agent / local-keeper appends to, the console
# View tab polls + renders via the `view` op. VM file <VM_HOME>/.view-pushes.jsonl
# (append-only JSON lines): agents inside a VM append it directly; the POST endpoint
# appends/clears from outside; GET drains it for the console poll.
VIEWPUSH_RE = re.compile(r"^/api/vm/([a-zA-Z0-9][a-zA-Z0-9-]{0,62})/view-push$")
VIEWPUSH_VM_PATH = "/.view-pushes.jsonl"          # under VM_HOME
VIEWPUSH_MAX = 200                                 # queue rows returned / kept
VIEWPUSH_LABEL_RE = re.compile(r"^[^\x00]{0,80}$")
VIEWPUSH_OPS = {"push", "clear"}
# append one JSON line ($1 = an argv element, never interpolated into the shell)
# and trim to the last VIEWPUSH_MAX lines so the queue can't grow unbounded.
VIEWPUSH_APPEND_SH = (
    'f="$HOME/.view-pushes.jsonl"; printf "%s\\n" "$1" >> "$f"; '
    'tail -n 200 "$f" > "$f.t" 2>/dev/null && mv "$f.t" "$f"'
)

# --- keeper-ask request bus (build-step 2): the pointer-discipline front door.
# POST /api/vm/<vm>/keeper-ask  {type, ...} -> ALWAYS {ok, id, path, summary?},
# NEVER an inline payload. The producer runs inside the VM as the VM user and
# writes its bytes to <VM_HOME>/.keeper-bus/<id>.json; the bus returns only the
# pointer + a small summary, so the LLM ingests spans (via a later `view`), not
# bulk. view/grep/collect are finder-backed (produced here, same validation as
# the /finder route, just persisted); compact/next-todo/bookmark are relayed to
# the VM responder (`vm-bridge serve`, run by the local-keeper) which produces,
# distils, and replies with its own {id,path,summary} pointer.
KEEPERASK_RE = re.compile(r"^/api/vm/([a-zA-Z0-9][a-zA-Z0-9-]{0,62})/keeper-ask$")
KEEPER_BUS_DIR = "/.keeper-bus"                          # spool dir under VM_HOME
KEEPERASK_PRODUCE = {"view", "grep", "collect"}          # finder-backed, produced host-side
KEEPERASK_RELAY = {"compact", "next-todo", "bookmark"}   # relayed to the VM responder
VM_BRIDGE_BIN = os.environ.get("BUGREPORT_VM_BRIDGE", "/srv/vm_mgr/share/scripts/vm-bridge")
KEEPERASK_RELAY_TIMEOUT = int(os.environ.get("BUGREPORT_KEEPERASK_TIMEOUT", "120"))  # seconds to wait on the responder

# console-plane bus reads (roadmap 2.2, Feed drawer): NOT keeper traffic — the
# browser lists / opens spool files so the operator reads them at zero LLM cost.
# Rides the existing /keeper-ask nginx location (no new route needed).
KEEPERASK_BUS = {"bus-list", "bus-get"}
KEEPERBUS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,90}$")
KEEPERBUS_ITEM_MAX = 256 * 1024                          # cap on one spool file returned
KEEPERBUS_LIST_MAX = 50                                  # newest spool rows returned
# Fixed listing script run IN the VM as the VM user (argv-only, nothing
# interpolated): newest-first rows of small metadata — never bulk content.
KEEPERBUS_LIST_PY = r"""
import json, os, sys
d = os.path.expanduser("~/.keeper-bus")
try:
    names = [n for n in os.listdir(d) if n.endswith(".json")]
except OSError:
    names = []
def mt(n):
    try:
        return os.stat(os.path.join(d, n)).st_mtime
    except OSError:
        return 0
names.sort(key=mt, reverse=True)
out = []
for n in names[:%d]:
    p = os.path.join(d, n)
    row = {"id": n[:-5]}
    try:
        row["size"] = os.path.getsize(p)
        with open(p) as fh:
            obj = json.load(fh)
        if isinstance(obj, dict):
            for k in ("type", "ts", "tags", "error"):
                if obj.get(k) is not None:
                    row[k] = obj[k]
            if isinstance(obj.get("prompt"), str):
                row["prompt"] = obj["prompt"][:120]
            f = obj.get("feed")
            if isinstance(f, dict):
                cats = [c for c in (f.get("categories") or []) if isinstance(c, dict)]
                row["categories"] = [str(c.get("label", ""))[:60] for c in cats][:8]
                row["n_items"] = sum(len(c.get("items") or []) for c in cats)
            pk = obj.get("picked")
            if isinstance(pk, dict):
                row["picked"] = {"id": pk.get("id"), "status": pk.get("status"),
                                 "text": str(pk.get("text", ""))[:120]}
            bm = obj.get("bookmark")
            if isinstance(bm, dict):
                row["text"] = str(bm.get("text", ""))[:120]
    except Exception as exc:
        row["error"] = str(exc)[:120]
    if isinstance(row.get("error"), str):
        row["error"] = row["error"][:200]
    out.append(row)
json.dump({"items": out}, sys.stdout)
""" % KEEPERBUS_LIST_MAX

REVISION_RE = re.compile(r"^/api/vm/([a-zA-Z0-9][a-zA-Z0-9-]{0,62})/todo-revision$")
REV_DIR = "board-revisions"                          # under VM_HOME in the VM
REV_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")     # board-item id (also the filename stem)
REV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")  # a basename we could have written
REV_TEXT_MAX = 64 * 1024                             # 64KB cap on write text / read result
REV_BODY_MAX = 256 * 1024                            # raw POST body cap (64KB text + JSON escaping)
# strict per-op key whitelist (the t29 `dirs` discipline): any other key -> 400
REV_OPS = {"write": {"op", "id", "text"}, "read": {"op", "path"}}
# Atomic write, run AS the VM user inside the VM (the finder exec-as-user pattern):
# mkdir -p the dir, write the stdin text to a temp file in the SAME dir, then
# rename (atomic on one fs). The timestamped name makes collisions vanishingly
# rare; if one somehow exists we add a numeric suffix rather than clobber. The
# name arrives as $1 (an argv element, never interpolated into the script), and
# the content arrives on stdin — so neither can inject shell. Prints the final
# absolute path on stdout for the handler to echo back.
REV_WRITE_SH = (
    'set -e; d="$HOME/board-revisions"; mkdir -p "$d"; b="$1"; f="$d/$b"; '
    'if [ -e "$f" ]; then s="${b%.md}"; i=1; '
    'while [ -e "$d/$s-$i.md" ]; do i=$((i+1)); done; f="$d/$s-$i.md"; fi; '
    't="$(mktemp "$d/.rev.XXXXXX")"; cat > "$t"; chmod 0644 "$t"; mv "$t" "$f"; '
    'printf %s "$f"'
)

# --- steward route (t54): the console's session-steward drawer. GET returns the
# keeper's read-only <VM_HOME>/steward-state.json + the console-writable
# <VM_HOME>/steward.json; POST writes the config (mode/timeout_min + optional
# threshold_bytes/max_age_min, preserve-if-absent), an extension hold, or a
# t89 manual reset request. This route can ARM a live keeper restart (mode:"on"), so
# writes are validated as strictly as the other write routes: exact op set,
# exact per-op key whitelist, mode in the fixed set, ints range-checked, bool
# rejected (it is an int subclass) — malformed input FAILS CLOSED (400, no
# write). Bounds MUST mirror keeper_relay.py (the VM-side counterpart) so the
# console can never write a value keeper-relay would silently drop.
STEWARD_RE = re.compile(r"^/api/vm/([a-zA-Z0-9][a-zA-Z0-9-]{0,62})/steward$")
STEWARD_STATE_VM_PATH = "/steward-state.json"  # keeper-relay WRITES (read-only here), under VM_HOME
STEWARD_CONFIG_VM_PATH = "/steward.json"       # the console WRITES this, under VM_HOME
STEWARD_HOLD_VM_PATH = "/handoff/hold"         # extend request (keeper-relay reads+consumes), under VM_HOME
STEWARD_REQUEST_VM_PATH = "/steward-request.json"  # manual reset request (keeper-relay consumes at-most-once, t89), under VM_HOME
STEWARD_REQUEST_SCHEMA = "steward-request.v1"      # == keeper_relay's expected request schema tag
STEWARD_CONFIG_SCHEMA = "steward.v1"                       # == keeper_relay STEWARD_CONFIG_SCHEMA
STEWARD_MODES = ("off", "dry", "on")                       # == keeper_relay STEWARD_MODES
STEWARD_TIMEOUT_MIN_LO = 1                                 # == keeper_relay STEWARD_TIMEOUT_MIN_LO
STEWARD_TIMEOUT_MIN_HI = 1440                              # == keeper_relay STEWARD_TIMEOUT_MIN_HI
STEWARD_THRESHOLD_BYTES_LO = 50000                        # == keeper_relay STEWARD_THRESHOLD_BYTES_LO (= floor default)
STEWARD_THRESHOLD_BYTES_HI = 4000000                      # == keeper_relay STEWARD_THRESHOLD_BYTES_HI (~1M tokens @ ~4 B/tok)
# THE RESET BAR since 2026-07-30 (operator: tokens, not mb). threshold_bytes above
# is reported-only on the VM side and triggers nothing; bounds MUST match
# keeper_relay's own threshold_tokens validation exactly.
STEWARD_THRESHOLD_TOKENS_LO = 20000                        # == keeper_relay STEWARD_THRESHOLD_TOKENS_LO
STEWARD_THRESHOLD_TOKENS_HI = 2000000                      # == keeper_relay STEWARD_THRESHOLD_TOKENS_HI
STEWARD_MAX_AGE_MIN_LO = 30                                # == keeper_relay STEWARD_MAX_AGE_MIN_LO (t89 time-ARM gate)
STEWARD_MAX_AGE_MIN_HI = 10080                             # == keeper_relay STEWARD_MAX_AGE_MIN_HI (7 days)
STEWARD_MAX_EXTEND_MIN = 60                                # == keeper_relay STEWARD_MAX_EXTEND_MIN (anti-dodge cap)
STEWARD_BODY_MAX = 4096                                    # raw POST body cap (tiny control messages)
# strict per-op key whitelist (the t49/t29 discipline): any other key -> 400
STEWARD_OPS = {"config": {"op", "mode", "timeout_min", "threshold_bytes",
                          "threshold_tokens", "max_age_min",
                          "fs_mediated", "lean_context", "zero_usage", "router_mode"},
               "extend": {"op", "minutes"},
               "reset": {"op"}}

# --- keeper-env route (board p13): set/rotate a VM keeper's gateway API key. GET
# never returns the key value in any form — only whether it is set. POST MERGES
# into the existing hugpy-keeper.env (replace-in-place per field, else append;
# every OTHER line preserved byte-identical) and chmods it 0600 after every
# write. The route refuses to CREATE the file (409) — half-provisioning a VM
# that never ran hugpy-keeper-ensure would be a silent lie about VM state. The
# target path is a fixed golden-image install location, not VM_HOME-relative —
# override via env only if your golden image installs hugpy-keeper elsewhere.
KEENV_RE = re.compile(r"^/api/vm/([a-zA-Z0-9][a-zA-Z0-9-]{0,62})/keeper-env$")
KEENV_VM_PATH = os.environ.get("BUGREPORT_KEEPER_ENV_PATH", "/opt/llm-station/hugpy-keeper.env")
KEENV_FIELDS = ("KEEPER_API_KEY", "HUGPY_BASE_URL", "HUGPY_KEEPER_MODEL")
KEENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")   # a recognizable KEY=VALUE line
KEENV_URL_RE = re.compile(r"^https?://\S+$")
KEENV_MODEL_RE = re.compile(r"^[A-Za-z0-9~._:-]{1,128}$")
KEENV_BODY_MAX = 4096                                             # tiny control message, generous cap
KEENV_OPS = {"set": {"op", "fields"}}

# --- flow route (o19, serving t84): the console's ⋔ flow drawer. A flow.v1 doc
# (console-ui docs/FLOW-SCHEMA.md) is a file-as-interface at <VM_HOME>/flow.json
# — the keeper derives it from source, the operator revises it in the console,
# both sides read+write the SAME file. GET pulls it (absence is normal — the
# keeper may not have derived one yet — so absent/unreadable/non-JSON degrades
# to state:null with ok:true, the _steward_pull_json idiom). POST validates
# fail-closed (400, no write) then pushes and RE-READS so the console reflects
# what the host actually wrote.
FLOW_RE = re.compile(r"^/api/vm/([a-zA-Z0-9][a-zA-Z0-9-]{0,62})/flow$")
FLOW_VM_PATH = "/flow.json"          # under VM_HOME, keeper- and console-writable
FLOW_SCHEMA = "flow.v1"              # == console-ui flow.js / docs/FLOW-SCHEMA.md
FLOW_BODY_MAX = 256 * 1024           # raw POST body cap — flows are small, this is generous


def _flow_validate(doc):
    """Validate a flow.v1 document; return an error string or None if valid.
    Fail-closed per FLOW-SCHEMA.md: schema tag exact, nodes a list of objects
    with string id/role/label and finite numeric x/y/w/h, ids unique, every
    edge endpoint resolving to a node id (a missing `edges` key reads as []).
    Deliberately UNLIKE the steward config write, this does NOT strip to a key
    whitelist: the spec REQUIRES unknown top-level (and node) keys survive a
    rewrite — either side may be OLDER than the document — so the doc is
    validated in place and later written through verbatim. Unknown node roles
    are likewise VALID (forward-compat: an older renderer shows `process` but
    preserves the raw role string)."""
    if not isinstance(doc, dict):
        return "state must be a JSON object"
    if doc.get("schema") != FLOW_SCHEMA:
        return "schema must be %r" % FLOW_SCHEMA
    nodes = doc.get("nodes")
    if not isinstance(nodes, list):
        return "nodes must be a list"
    ids = set()
    for i, nd in enumerate(nodes):
        if not isinstance(nd, dict):
            return "nodes[%d] must be an object" % i
        for k in ("id", "role", "label"):
            if not isinstance(nd.get(k), str):
                return "nodes[%d].%s must be a string" % (i, k)
        for k in ("x", "y", "w", "h"):
            v = nd.get(k)
            # bool is an int subclass -> reject it for numerics so a stray
            # true/false can never read as 1/0 (the steward timeout_min guard);
            # Python's json also parses NaN/Infinity, so require finite too.
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return "nodes[%d].%s must be a finite number" % (i, k)
        if nd["id"] in ids:
            return "duplicate node id: %s" % nd["id"]
        ids.add(nd["id"])
    edges = doc.get("edges", [])     # missing key reads as []; anything else must BE a list
    if not isinstance(edges, list):
        return "edges must be a list"
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            return "edges[%d] must be an object" % i
        for k in ("from", "to"):
            v = e.get(k)
            if not isinstance(v, str):
                return "edges[%d].%s must be a string" % (i, k)
            if v not in ids:
                return "edges[%d].%s does not resolve to a node id: %s" % (i, k, v)
        if "label" in e and not isinstance(e["label"], str):
            return "edges[%d].label must be a string" % i
    return None


def _keenv_valid_key(v):
    # KEEPER_API_KEY: <=128 chars, no whitespace/newline/control chars.
    if not isinstance(v, str) or len(v) > 128:
        return False
    return not any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in v)


def _keenv_validate(field, v):
    """Return (ok, error) for one field's value. Nothing is written unless
    EVERY field in the request validates (fail-the-whole-request, no partial apply)."""
    if not isinstance(v, str):
        return False, "%s must be a string" % field
    if field == "KEEPER_API_KEY":
        if not _keenv_valid_key(v):
            return False, "KEEPER_API_KEY must be <=128 chars with no whitespace/control characters"
        return True, None
    if field == "HUGPY_BASE_URL":
        if len(v) > 256 or not KEENV_URL_RE.match(v):
            return False, "HUGPY_BASE_URL must match ^https?://[^\\s]+$ (<=256 chars)"
        return True, None
    if field == "HUGPY_KEEPER_MODEL":
        if not KEENV_MODEL_RE.match(v):
            return False, "HUGPY_KEEPER_MODEL must match ^[A-Za-z0-9~._:-]{1,128}$"
        return True, None
    return False, "unsupported field: %s" % field   # unreachable — caller pre-filters KEENV_FIELDS


def _keenv_parse(data):
    """Parse hugpy-keeper.env bytes into {field: value} for the 3 known fields
    (last occurrence wins, matching shell-source semantics). Unknown lines
    (comments, blanks, other vars) are ignored here — _keenv_merge is what
    keeps them byte-identical on write."""
    parsed = {}
    for ln in data.decode("utf-8", "replace").split("\n"):
        m = KEENV_LINE_RE.match(ln.rstrip("\r"))
        if m and m.group(1) in KEENV_FIELDS:
            parsed[m.group(1)] = m.group(2)
    return parsed


def _keenv_merge(text, updates):
    """MERGE `updates` (field -> new value, already validated) into the existing
    env file `text`. Each updated field's FIRST matching `FIELD=...` line is
    replaced in place; a field with no existing line is appended. Every other
    line is preserved byte-identical (including comments/blanks/order)."""
    had_content = text != ""
    lines = text.split("\n")
    trailing_blank = had_content and lines and lines[-1] == ""
    if trailing_blank:
        lines = lines[:-1]
    remaining = dict(updates)
    out = []
    for ln in lines:
        m = KEENV_LINE_RE.match(ln)
        field = m.group(1) if m else None
        if field in remaining:
            out.append("%s=%s" % (field, remaining.pop(field)))
        else:
            out.append(ln)
    for field in KEENV_FIELDS:          # stable append order for any not already present
        if field in remaining:
            out.append("%s=%s" % (field, remaining[field]))
    return "\n".join(out) + "\n"        # always end the file on a newline


def _rev_timestamp():
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())

# --- keeper-mode: this machine IS the keeper console, addressable as "keeper" 
KEEPER_VM = (os.environ.get("FLEET_KEEPER_NAME")
             or os.environ.get("FLEET_SELF_NAME") or "keeper")
KEEPER_HOME = os.path.expanduser("~")


class _LocalResult:
    """CompletedProcess look-alike for locally-satisfied keeper-console ops."""
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _keeper_path(p):
    """'keeper/home/ubuntu/x' (or '/home/ubuntu/x') -> local-home path."""
    if p.startswith(KEEPER_VM + "/"):
        p = p[len(KEEPER_VM):]
    return p.replace("/home/ubuntu", KEEPER_HOME, 1)


def lxc(args, data=None):
    if args and args[0] == "exec" and len(args) > 1 and args[1] == KEEPER_VM:
        try:
            i = args.index("--")
        except ValueError:
            return _LocalResult(1, b"", b"keeper exec: no argv")
        argv = [a.replace("/home/ubuntu", KEEPER_HOME) for a in args[i + 1:]]
        env = dict(os.environ, HOME=KEEPER_HOME)
        try:
            return subprocess.run(argv, input=data, capture_output=True,
                                  timeout=20, env=env)
        except Exception as exc:
            return _LocalResult(1, b"", str(exc).encode())
    if args and args[0] == "file" and len(args) >= 3:
        if args[1] == "pull" and args[2].startswith(KEEPER_VM + "/"):
            try:
                with open(_keeper_path(args[2]), "rb") as f:
                    return _LocalResult(0, f.read())
            except OSError as exc:
                return _LocalResult(1, b"", str(exc).encode())
        if args[1] == "push" and args[2] == "-" and len(args) > 3 \
                and args[3].startswith(KEEPER_VM + "/"):
            try:
                target = _keeper_path(args[3])
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(data or b"")
                return _LocalResult(0)
            except OSError as exc:
                return _LocalResult(1, b"", str(exc).encode())
    try:
        return subprocess.run(["lxc"] + args, input=data, capture_output=True,
                              timeout=20)
    except Exception as exc:
        return _LocalResult(1, b"", str(exc).encode())

# --- ~/.hugpy migration compatibility --------------------------------------
# A station may keep these files at ~/ (legacy) or, once installed/upgraded, under
# ~/.hugpy/<sub>/ (organized — see debian/postinst + _platform/paths.py). We read
# organized-first-then-legacy and write to organized only when the VM already has
# that layout, so a host interoperates with old and new stations regardless of
# whether `lxc file pull/push` follows the compat symlink. Keep the map in sync.
_HUGPY_VM_MAP = {
    "/todo.json": "state/todo.json",
    "/todo-history.jsonl": "state/todo-history.jsonl",
    "/.todo.lock": "run/todo.lock",
    "/steward.json": "config/steward.json",
    "/steward-state.json": "state/steward-state.json",
    "/steward-request.json": "state/steward-request.json",
    "/bugreport.json": "logs/bugreport.json",
    "/bugreport-request.json": "logs/bugreport-request.json",
    "/flow.json": "state/flow.json",
    "/wireframe.json": "state/wireframe.json",
    "/.bridge-mail.jsonl": "logs/bridge-mail.jsonl",
}


def _hugpy_rel(rel):
    """VM_HOME-relative organized path ('/.hugpy/<sub>/<f>') for a legacy '/name',
    or None when the file is not part of the ~/.hugpy layout."""
    sub = _HUGPY_VM_MAP.get(rel)
    return ("/.hugpy/" + sub) if sub else None


def _pull_vm(vm, rel):
    """lxc file pull VM_HOME+rel, trying the organized ~/.hugpy path first and
    falling back to legacy. Returns the first non-empty success, else the last
    result (so the caller's normal missing/empty handling still applies)."""
    org = _hugpy_rel(rel)
    last = None
    for p in ([org, rel] if org else [rel]):
        r = lxc(["file", "pull", vm + VM_HOME + p, "-"])
        if getattr(r, "returncode", 1) == 0 and getattr(r, "stdout", b""):
            return r
        last = r
    return last


def _push_rel(vm, rel):
    """Destination for VM_HOME+rel: the organized ~/.hugpy path when the VM has
    that layout, else the legacy path. One destination — never a split write."""
    org = _hugpy_rel(rel)
    if org:
        chk = lxc(["exec", vm, "--", "test", "-d", VM_HOME + "/.hugpy"])
        if getattr(chk, "returncode", 1) == 0:
            return org
    return rel


class H(BaseHTTPRequestHandler):
    server_version = "bugreport-api/1"
    def log_message(self, fmt, *a):  # systemd captures stderr; log one line per request
        import sys; print(self.address_string(), self.command, self.path, file=sys.stderr)
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True, "service": "bugreport-api"})
        km = KEENV_RE.match(self.path)
        if km:
            return self._keeper_env_get(km.group(1))
        flm = FLOW_RE.match(self.path)
        if flm:
            return self._flow_get(flm.group(1))
        sm = STEWARD_RE.match(self.path)
        if sm:
            return self._steward_get(sm.group(1))
        hm = HISTORY_RE.match(self.path)
        if hm:
            return self._history(hm.group(1))
        mm = MESSAGES_RE.match(self.path)
        if mm:
            return self._messages(mm.group(1))
        vpm = VIEWPUSH_RE.match(self.path)
        if vpm:
            return self._view_push_get(vpm.group(1))
        m = VM_RE.match(self.path)
        if not m:
            return self._send(404, {"ok": False, "error": "unknown route"})
        vm = m.group(1)
        try:
            r = _pull_vm(vm, "/bugreport.json")
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            return self._send(200, {"ok": False, "error": "no bugreport yet"})
        try:
            return self._send(200, {"ok": True, "report": json.loads(r.stdout.decode())})
        except Exception:
            return self._send(200, {"ok": False, "error": "bugreport.json is not valid JSON"})
    def _messages(self, vm):
        # Bridge mail (see MESSAGES_RE above). Missing file = a station whose
        # watcher hasn't run yet: honestly EMPTY, not an error — the route
        # existing is what lights the tab up.
        try:
            r = _pull_vm(vm, "/.bridge-mail.jsonl")
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            return self._send(200, {"ok": True, "messages": []})
        raw = r.stdout[-HISTORY_MAX:]
        if len(r.stdout) > HISTORY_MAX:
            nl = raw.find(b"\n")
            raw = raw[nl + 1:] if nl >= 0 else raw
        by_id, order = {}, []
        for line in raw.decode(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or len(order))
            if rid not in by_id:
                order.append(rid)
            by_id[rid] = row               # last row per id wins (post-triage)
        msgs = [by_id[i] for i in order][-MESSAGES_MAX:]
        return self._send(200, {"ok": True, "messages": msgs})

    def _history(self, vm):
        # Read-only tail of the VM's <VM_HOME>/todo-history.jsonl (the keeper-relay
        # board journal). Missing file -> "no history yet"; malformed lines are
        # skipped; a journal over the 1MB cap is TAILED (keep the LAST bytes, drop
        # the now-partial first line); newest HISTORY_EVENTS returned, oldest -> newest.
        try:
            r = _pull_vm(vm, "/todo-history.jsonl")
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            return self._send(200, {"ok": False, "error": "no history yet"})
        data = r.stdout or b""
        clipped = len(data) > HISTORY_MAX
        if clipped:
            data = data[-HISTORY_MAX:]                 # tail: keep the LAST 1MB
        lines = data.decode("utf-8", "replace").split("\n")
        if clipped and lines:
            lines = lines[1:]                          # drop the now-partial first line
        events = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                events.append(json.loads(ln))          # skip malformed lines
            except ValueError:
                continue
        return self._send(200, {"ok": True, "events": events[-HISTORY_EVENTS:]})
    def _finder(self, vm):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 8192:
                return self._send(413, {"ok": False, "error": "body too large"})
            req = json.loads(self.rfile.read(n).decode())
        except Exception:
            return self._send(400, {"ok": False, "error": "bad JSON"})
        code, obj = self._finder_core(vm, req)
        return self._send(code, obj)

    def _finder_core(self, vm, req):
        # Validate `req` and run the VM's finder as the VM user. Returns
        # (status_code, payload) instead of sending, so both the /finder route
        # and the keeper-ask bus share one validated producer. payload is
        # {"ok":False,"error":...} on any rejection, else {"ok":True,"op","result"}.
        if not isinstance(req, dict):
            return (400, {"ok": False, "error": "body must be a JSON object"})
        op = req.get("op")
        if op not in FINDER_OPS:
            return (400, {"ok": False, "error": "op must be collect|dirs|grep|imports|view"})
        roots = req.get("roots")
        if (not isinstance(roots, list) or not 1 <= len(roots) <= 4
                or not all(isinstance(r, str) and r.startswith("/") and len(r) < 300
                           and "\x00" not in r and ".." not in r for r in roots)):
            return (400, {"ok": False, "error": "roots must be 1-4 absolute paths"})
        # Strict per-op key whitelist: reject any body key not valid for this op
        # (e.g. a `string` key sent to `dirs`). Makes FINDER_OPS/FINDER_BOOLS an
        # enforcing allow-list rather than a silent filter — so an op cannot be
        # smuggled flags meant for another op.
        allowed_keys = {"op", "roots"} | set(FINDER_OPS[op]) | set(FINDER_BOOLS[op])
        if op == "imports":
            allowed_keys.add("for")
        extra = [k for k in req if k not in allowed_keys]
        if extra:
            return (400, {"ok": False,
                          "error": "unsupported key(s) for %s: %s" % (op, ",".join(sorted(extra)))})
        flags = []
        for key, (flag, rx) in FINDER_OPS[op].items():
            vals = req.get(key)
            if vals is None:
                continue
            if not isinstance(vals, list) or len(vals) > 8 \
                    or not all(isinstance(v, str) and rx.match(v) for v in vals):
                return (400, {"ok": False, "error": key + ": list of up to 8 valid strings"})
            flags += [flag + "=" + v for v in vals]      # `=` form survives leading dashes
        if op == "grep" and not any(f.startswith("--string=") for f in flags):
            return (400, {"ok": False, "error": "grep needs string:[...]"})
        if op == "imports" and req.get("for") is not None:
            v = req["for"]
            if not isinstance(v, str) or not IMP_RE.match(v):
                return (400, {"ok": False, "error": "for: bad import name"})
            flags.append("--for=" + v)
        for key, flag in FINDER_BOOLS[op].items():
            if req.get(key) is True:
                flags.append(flag)
        argv = (["lxc", "exec", vm, "--user", VM_UID, "--group", VM_GID,
                 "--env", "HOME=" + VM_HOME, "--", FINDER_BIN, op] + roots + flags + ["--json"])
        try:
            r = subprocess.run(argv, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            return (504, {"ok": False, "error": "finder timed out (60s) — narrow the search"})
        if r.returncode != 0:
            return (200, {"ok": False, "error": (r.stderr.decode(errors="replace")[-6000:]
                                                 or "finder failed rc=%d" % r.returncode)})
        try:
            data = json.loads(r.stdout.decode())
        except Exception:
            return (200, {"ok": False, "error": "finder emitted non-JSON output"})
        # Don't refuse large result sets — cap them and say so, so the UI can
        # render what fits (and refine/page) instead of showing nothing.
        _MAX_SHOWN, _MAX_BYTES = 2000, 4 * 1024 * 1024
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            _total = data.get("count", len(data["results"]))
            _trim = False
            if len(data["results"]) > _MAX_SHOWN:
                data["results"] = data["results"][:_MAX_SHOWN]; _trim = True
            while len(data["results"]) > 1 and len(json.dumps(data)) > _MAX_BYTES:
                data["results"] = data["results"][:len(data["results"]) // 2]; _trim = True
            data["total"], data["shown"], data["truncated"] = _total, len(data["results"]), _trim
            return (200, {"ok": True, "op": op, "result": data})
        if len(r.stdout) > _MAX_BYTES:
            return (200, {"ok": False, "error": "result too large and not in the expected shape (%d bytes)" % len(r.stdout)})
        return (200, {"ok": True, "op": op, "result": data})

    # --- keeper-ask request bus (build-step 2) --------------------------------
    def _keeper_id(self):
        return "%s-%03d" % (_rev_timestamp(), int(time.time() * 1000) % 1000)

    def _keeper_ask(self, vm):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 16384:
                return self._send(413, {"ok": False, "error": "body too large"})
            req = json.loads(self.rfile.read(n).decode())
        except Exception:
            return self._send(400, {"ok": False, "error": "bad JSON"})
        if not isinstance(req, dict):
            return self._send(400, {"ok": False, "error": "body must be a JSON object"})
        typ = req.get("type")
        if typ in KEEPERASK_PRODUCE:
            return self._keeper_ask_produce(vm, typ, req)
        if typ in KEEPERASK_RELAY:
            return self._keeper_ask_relay(vm, typ, req)
        if typ in KEEPERASK_BUS:
            return self._keeper_ask_bus(vm, typ, req)
        return self._send(400, {"ok": False, "error": "type must be one of: %s"
                                % ", ".join(sorted(KEEPERASK_PRODUCE | KEEPERASK_RELAY | KEEPERASK_BUS))})

    def _keeper_ask_bus(self, vm, typ, req):
        # Console-plane spool reads (Feed drawer). bus-list -> capped newest-first
        # metadata rows; bus-get {id} -> that one spool file, parsed. These are
        # browser-render reads, not keeper traffic — the pointer discipline is
        # about what the LLM ingests, and nothing here goes near it.
        if typ == "bus-get":
            qid = req.get("id")
            if not (isinstance(qid, str) and KEEPERBUS_ID_RE.match(qid)
                    and ".." not in qid):
                return self._send(400, {"ok": False, "error": "bus-get needs a valid id"})
            try:
                r = lxc(["file", "pull", vm + VM_HOME + KEEPER_BUS_DIR + "/" + qid + ".json", "-"])
            except subprocess.TimeoutExpired:
                return self._send(504, {"ok": False, "error": "lxc timeout"})
            if r.returncode != 0:
                return self._send(200, {"ok": False, "error": "no such spool item"})
            if len(r.stdout) > KEEPERBUS_ITEM_MAX:
                return self._send(200, {"ok": False, "error":
                                        "spool item too large (%d bytes)" % len(r.stdout)})
            try:
                item = json.loads(r.stdout.decode("utf-8", "replace"))
            except ValueError:
                return self._send(200, {"ok": False, "error": "spool item is not JSON"})
            return self._send(200, {"ok": True, "item": item})
        try:
            r = lxc(["exec", vm, "--user", VM_UID, "--group", VM_GID,
                     "--env", "HOME=" + VM_HOME, "--", "python3", "-c", KEEPERBUS_LIST_PY])
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            return self._send(200, {"ok": False, "error":
                                    (r.stderr.decode(errors="replace")[-500:] or "list failed")})
        try:
            obj = json.loads(r.stdout.decode("utf-8", "replace"))
        except ValueError:
            return self._send(200, {"ok": False, "error": "bad listing output"})
        return self._send(200, {"ok": True, "items": obj.get("items") or []})

    def _keeper_ask_produce(self, vm, typ, req):
        # Finder-backed op. Reuse the /finder validation+producer (type -> op),
        # then — the whole point of the bus — persist the bytes to a VM-side
        # spool file and return ONLY {id, path, summary}, never the payload.
        freq = {k: v for k, v in req.items() if k != "type"}
        freq["op"] = typ
        code, obj = self._finder_core(vm, freq)
        if not obj.get("ok"):
            return self._send(code, obj)          # validation / producer error — pass through
        result = obj["result"]
        rid = self._keeper_id()
        vm_path = VM_HOME + KEEPER_BUS_DIR + "/" + rid + ".json"
        blob = json.dumps({"id": rid, "type": typ, "ts": int(time.time()), "result": result},
                          separators=(",", ":")).encode()
        # ensure the spool dir exists (as the VM user), then push the bytes as that user
        lxc(["exec", vm, "--user", VM_UID, "--group", VM_GID, "--env", "HOME=" + VM_HOME,
             "--", "sh", "-c", 'mkdir -p "$HOME%s"' % KEEPER_BUS_DIR])
        try:
            pr = lxc(["file", "push", "-", vm + vm_path,
                      "--uid", VM_UID, "--gid", VM_GID, "--mode", "0644"], data=blob)
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout writing spool"})
        if pr.returncode != 0:
            return self._send(502, {"ok": False, "error": "spool write failed: " + pr.stderr.decode()[:200]})
        summary = {"op": typ, "bytes": len(blob)}
        if isinstance(result, dict):
            for k in ("total", "shown", "truncated", "count"):
                if k in result:
                    summary[k] = result[k]
        return self._send(200, {"ok": True, "id": rid, "path": vm_path, "summary": summary})

    def _keeper_ask_relay(self, vm, typ, req):
        # compact / next-todo / bookmark: the local-keeper (the VM's
        # `vm-bridge serve` responder) produces + distils and writes its own
        # spool file. The host relays the typed request over the bridge and
        # normalizes whatever pointer it replies. Until a keeper-ask responder
        # is wired on the VM, this returns a clear 502 (the documented seam).
        payload = {"keeper_ask": {**{k: v for k, v in req.items() if k != "type"}, "type": typ}}
        try:
            pr = subprocess.run([VM_BRIDGE_BIN, "ask", vm, json.dumps(payload)],
                                capture_output=True, timeout=KEEPERASK_RELAY_TIMEOUT + 30,
                                env={**os.environ, "BRIDGE_TIMEOUT": str(KEEPERASK_RELAY_TIMEOUT)})
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False,
                                    "error": "responder timeout (%ds) for %s" % (KEEPERASK_RELAY_TIMEOUT, typ)})
        if pr.returncode != 0:
            err = (pr.stderr.decode(errors="replace")[-400:] or "vm-bridge ask failed")
            return self._send(502, {"ok": False, "error": err
                + " (gate open? is the VM running `vm-bridge serve` with a keeper-ask responder?)"})
        reply = pr.stdout.decode(errors="replace").strip()
        try:
            obj = json.loads(reply)
        except Exception:
            return self._send(502, {"ok": False,
                                    "error": "responder did not return a JSON pointer",
                                    "raw_head": reply[:200]})
        if not (isinstance(obj, dict) and obj.get("path")):
            return self._send(502, {"ok": False, "error": "responder reply missing {id,path}",
                                    "reply": obj if isinstance(obj, dict) else None})
        obj.setdefault("ok", True)
        return self._send(200, obj)

    # --- revision route (t49) -------------------------------------------------
    def _view_push_get(self, vm):
        # Poll the VM's view-push queue; absent/empty/bad -> [], never 500.
        try:
            r = lxc(["file", "pull", vm + VM_HOME + VIEWPUSH_VM_PATH, "-"])
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        pushes = []
        if r.returncode == 0 and r.stdout:
            for line in r.stdout.decode("utf-8", "replace").splitlines()[-VIEWPUSH_MAX:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    pushes.append(json.loads(line))
                except ValueError:
                    continue
        return self._send(200, {"ok": True, "pushes": pushes})

    def _view_push(self, vm):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 8192:
                return self._send(413, {"ok": False, "error": "body too large"})
            req = json.loads(self.rfile.read(n).decode())
        except Exception:
            return self._send(400, {"ok": False, "error": "bad JSON"})
        op = req.get("op")
        if op not in VIEWPUSH_OPS:
            return self._send(400, {"ok": False, "error": "op must be push|clear"})
        if op == "clear":
            lxc(["exec", vm, "--user", VM_UID, "--group", VM_GID, "--env", "HOME=" + VM_HOME,
                 "--", "sh", "-c", ': > "$HOME/.view-pushes.jsonl" 2>/dev/null || true'])
            return self._view_push_get(vm)
        path = req.get("path")
        if not (isinstance(path, str) and path.startswith("/") and ".." not in path
                and "\x00" not in path and len(path) < 300):
            return self._send(400, {"ok": False, "error": "path must be an absolute path"})
        lines = req.get("lines")
        if lines is not None and not (isinstance(lines, str) and LINES_RE.match(lines)):
            return self._send(400, {"ok": False, "error": "lines: e.g. 100-140,200-210"})
        label = req.get("label", "")
        if not (isinstance(label, str) and VIEWPUSH_LABEL_RE.match(label)):
            return self._send(400, {"ok": False, "error": "label must be <=80 chars"})
        rec = {"id": "%s-%03d" % (_rev_timestamp(), int(time.time() * 1000) % 1000),
               "ts": int(time.time()), "path": path, "lines": lines or "", "label": label}
        r = lxc(["exec", vm, "--user", VM_UID, "--group", VM_GID, "--env", "HOME=" + VM_HOME,
                 "--", "sh", "-c", VIEWPUSH_APPEND_SH, "sh", json.dumps(rec, separators=(",", ":"))])
        if r.returncode != 0:
            return self._send(200, {"ok": False, "error": (r.stderr.decode(errors="replace")[-2000:] or "append failed")})
        return self._view_push_get(vm)

    def _revision(self, vm):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > REV_BODY_MAX:
                return self._send(413, {"ok": False, "error": "body too large"})
            req = json.loads(self.rfile.read(n).decode())
        except Exception:
            return self._send(400, {"ok": False, "error": "bad JSON"})
        if not isinstance(req, dict):
            return self._send(400, {"ok": False, "error": "body must be a JSON object"})
        op = req.get("op")
        if op not in REV_OPS:
            return self._send(400, {"ok": False, "error": "op must be write|read"})
        # per-op key whitelist: an op cannot carry keys meant for the other op
        extra = [k for k in req if k not in REV_OPS[op]]
        if extra:
            return self._send(400, {"ok": False,
                                    "error": "unsupported key(s) for %s: %s" % (op, ",".join(sorted(extra)))})
        return self._rev_write(vm, req) if op == "write" else self._rev_read(vm, req)

    def _rev_write(self, vm, req):
        rid = req.get("id")
        if not isinstance(rid, str) or not REV_ID_RE.match(rid):
            return self._send(400, {"ok": False, "error": "id must match ^[A-Za-z0-9_-]{1,40}$"})
        text = req.get("text")
        if not isinstance(text, str):
            return self._send(400, {"ok": False, "error": "text must be a string"})
        body = text.encode("utf-8")
        if len(body) > REV_TEXT_MAX:
            return self._send(413, {"ok": False, "error": "text too large (64KB max)"})
        # rid is regex-pinned to [A-Za-z0-9_-] -> already filename-safe (no sanitize
        # transform needed); the timestamp keeps successive writes from colliding.
        name = "%s-%s.md" % (rid, _rev_timestamp())
        try:
            r = lxc(["exec", vm, "--user", VM_UID, "--group", VM_GID,
                     "--env", "HOME=" + VM_HOME, "--", "sh", "-c", REV_WRITE_SH, "sh", name],
                    data=body)
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            return self._send(502, {"ok": False, "error": "write failed: " +
                                    (r.stderr.decode(errors="replace")[-6000:] or "rc=%d" % r.returncode)})
        written = r.stdout.decode(errors="replace").strip()
        home_prefix = VM_HOME + "/"
        if written.startswith(home_prefix):
            path = "~/" + written[len(home_prefix):]     # ~/ form for the read op
        elif written:
            path = written
        else:
            path = "~/%s/%s" % (REV_DIR, name)               # fall back to the computed name
        return self._send(200, {"ok": True, "path": path, "bytes": len(body)})

    def _rev_read(self, vm, req):
        path = req.get("path")
        if not isinstance(path, str) or not path or len(path) > 300 or "\x00" in path:
            return self._send(400, {"ok": False, "error": "path must be a string"})
        if ".." in path:
            return self._send(400, {"ok": False, "error": "path must stay under ~/board-revisions/"})
        # accept ~/board-revisions/<name> or <VM_HOME>/board-revisions/<name>;
        # anything else absolute is out-of-bounds.
        p = path
        home_prefix = VM_HOME + "/"
        if p.startswith("~/"):
            p = p[2:]
        elif p.startswith(home_prefix):
            p = p[len(home_prefix):]
        elif p.startswith("/"):
            return self._send(400, {"ok": False, "error": "path must stay under ~/board-revisions/"})
        prefix = REV_DIR + "/"
        if not p.startswith(prefix):
            return self._send(400, {"ok": False, "error": "path must stay under ~/board-revisions/"})
        name = p[len(prefix):]
        if "/" in name or not REV_NAME_RE.match(name):
            return self._send(400, {"ok": False, "error": "bad revision filename"})
        try:
            r = lxc(["file", "pull", "%s%s/%s/%s" % (vm, VM_HOME, REV_DIR, name), "-"])
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            return self._send(200, {"ok": False, "error": "revision not found"})
        data = r.stdout or b""
        if len(data) > REV_TEXT_MAX:
            return self._send(200, {"ok": False, "error": "revision too large"})
        return self._send(200, {"ok": True, "text": data.decode("utf-8", "replace")})

    # --- steward route (t54) --------------------------------------------------
    def _steward_pull_json(self, vm, vm_path):
        """Pull a VM JSON file and parse it, or None on ANY problem (missing,
        empty, unreadable, lxc timeout, non-JSON). The steward may be OFF with no
        config/state written yet, so absence is normal — never a 500 here."""
        try:
            r = _pull_vm(vm, vm_path)
        except subprocess.TimeoutExpired:
            return None
        if r.returncode != 0 or not r.stdout:
            return None
        try:
            return json.loads(r.stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _steward_get(self, vm):
        # GET -> {"state": <steward-state.json|null>, "config": <steward.json|null>}.
        # Two independent reads; either file absent/bad -> that key null (never 500).
        return self._send(200, {
            "state": self._steward_pull_json(vm, STEWARD_STATE_VM_PATH),
            "config": self._steward_pull_json(vm, STEWARD_CONFIG_VM_PATH)})

    def _steward(self, vm):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > STEWARD_BODY_MAX:
                return self._send(413, {"ok": False, "error": "body too large"})
            req = json.loads(self.rfile.read(n).decode())
        except Exception:
            return self._send(400, {"ok": False, "error": "bad JSON"})
        if not isinstance(req, dict):
            return self._send(400, {"ok": False, "error": "body must be a JSON object"})
        op = req.get("op")
        if op not in STEWARD_OPS:
            return self._send(400, {"ok": False, "error": "op must be config|extend|reset"})
        # per-op key whitelist: an op cannot carry keys meant for another op,
        # and NO extra field can ride into steward.json (fail closed).
        extra = [k for k in req if k not in STEWARD_OPS[op]]
        if extra:
            return self._send(400, {"ok": False,
                                    "error": "unsupported key(s) for %s: %s" % (op, ",".join(sorted(extra)))})
        if op == "config":
            return self._steward_config(vm, req)
        if op == "reset":
            return self._steward_reset(vm)
        return self._steward_extend(vm, req)

    def _steward_config(self, vm, req):
        # mode: exactly one of the fixed set; anything else -> 400 (fail closed).
        mode = req.get("mode")
        if not isinstance(mode, str) or mode not in STEWARD_MODES:
            return self._send(400, {"ok": False, "error": "mode must be off|dry|on"})
        # timeout_min: an int in [LO,HI]. bool is an int subclass -> reject it so a
        # stray true/false can never read as 1 (mirrors keeper_relay's own guard).
        tmo = req.get("timeout_min")
        if (not isinstance(tmo, int) or isinstance(tmo, bool)
                or not (STEWARD_TIMEOUT_MIN_LO <= tmo <= STEWARD_TIMEOUT_MIN_HI)):
            return self._send(400, {"ok": False,
                                    "error": "timeout_min must be an int in [%d,%d]"
                                             % (STEWARD_TIMEOUT_MIN_LO, STEWARD_TIMEOUT_MIN_HI)})
        # threshold_bytes: OPTIONAL — the growth-ARM bar. Present: an int in
        # [LO,HI], same guard as timeout_min (bool rejected as an int subclass);
        # invalid -> 400 (fail closed). ABSENT: preserve the VM's currently-
        # configured value by re-reading steward.json; if that yields no valid
        # value, OMIT the field entirely so keeper_relay's documented env/default
        # fail-safe applies — never write a guessed value. Legacy-shape console
        # writes (mode/timeout only) therefore keep working and can never
        # clobber a configured threshold. Bounds MUST match keeper_relay's own
        # threshold_bytes validation exactly so the console can never write a
        # value the VM-side would silently drop.
        if "threshold_bytes" in req:
            thr = req.get("threshold_bytes")
            if (not isinstance(thr, int) or isinstance(thr, bool)
                    or not (STEWARD_THRESHOLD_BYTES_LO <= thr <= STEWARD_THRESHOLD_BYTES_HI)):
                return self._send(400, {"ok": False,
                                        "error": "threshold_bytes must be an int in [%d,%d]"
                                                 % (STEWARD_THRESHOLD_BYTES_LO, STEWARD_THRESHOLD_BYTES_HI)})
        else:
            thr = None
            cur = self._steward_pull_json(vm, STEWARD_CONFIG_VM_PATH)
            if isinstance(cur, dict):
                prev = cur.get("threshold_bytes")
                if (isinstance(prev, int) and not isinstance(prev, bool)
                        and STEWARD_THRESHOLD_BYTES_LO <= prev <= STEWARD_THRESHOLD_BYTES_HI):
                    thr = prev
        # threshold_tokens: OPTIONAL — THE RESET BAR (operator 2026-07-29/30:
        # tokens, not mb). Identical shape to threshold_bytes above — present:
        # an int in [LO,HI], bool rejected as an int subclass, invalid -> 400;
        # absent: preserve the VM's configured value, else omit so keeper_relay's
        # env/default (400000) applies. Kept SEPARATE from threshold_bytes rather
        # than replacing it: there is no honest byte->token conversion (measured
        # 126-512 tok/KB, a 4x spread), so a legacy console write that carries
        # only threshold_bytes must never be silently reinterpreted as a token
        # bar — it preserves the inert byte value and leaves the token bar alone.
        if "threshold_tokens" in req:
            tht = req.get("threshold_tokens")
            if (not isinstance(tht, int) or isinstance(tht, bool)
                    or not (STEWARD_THRESHOLD_TOKENS_LO <= tht <= STEWARD_THRESHOLD_TOKENS_HI)):
                return self._send(400, {"ok": False,
                                        "error": "threshold_tokens must be an int in [%d,%d]"
                                                 % (STEWARD_THRESHOLD_TOKENS_LO, STEWARD_THRESHOLD_TOKENS_HI)})
        else:
            tht = None
            cur = self._steward_pull_json(vm, STEWARD_CONFIG_VM_PATH)
            if isinstance(cur, dict):
                prev = cur.get("threshold_tokens")
                if (isinstance(prev, int) and not isinstance(prev, bool)
                        and STEWARD_THRESHOLD_TOKENS_LO <= prev <= STEWARD_THRESHOLD_TOKENS_HI):
                    tht = prev
        # max_age_min: OPTIONAL — the time-ARM gate (t89). Present: an int that
        # is 0 (VALID: "explicitly off", so the console can disable an env-set
        # gate) or in [LO,HI]; bool rejected as an int subclass, same guard as
        # every other field; invalid -> 400 (fail closed). ABSENT: preserve the
        # VM's currently-configured value by re-reading steward.json (the
        # threshold_bytes preserve-if-absent idiom above); if that yields no
        # valid value, OMIT the field entirely so keeper_relay's documented
        # env/default fail-safe applies — never write a guessed value. Bounds
        # MUST match keeper_relay's own max_age_min validation exactly so the
        # console can never write a value the VM-side would silently drop.
        if "max_age_min" in req:
            ma = req.get("max_age_min")
            if (not isinstance(ma, int) or isinstance(ma, bool)
                    or not (ma == 0 or STEWARD_MAX_AGE_MIN_LO <= ma <= STEWARD_MAX_AGE_MIN_HI)):
                return self._send(400, {"ok": False,
                                        "error": "max_age_min must be 0 or an int in [%d,%d]"
                                                 % (STEWARD_MAX_AGE_MIN_LO, STEWARD_MAX_AGE_MIN_HI)})
        else:
            ma = None
            cur = self._steward_pull_json(vm, STEWARD_CONFIG_VM_PATH)
            if isinstance(cur, dict):
                prev = cur.get("max_age_min")
                if (isinstance(prev, int) and not isinstance(prev, bool)
                        and (prev == 0 or STEWARD_MAX_AGE_MIN_LO <= prev <= STEWARD_MAX_AGE_MIN_HI)):
                    ma = prev
        # fs_mediated / lean_context: OPTIONAL bools (Method E steward switches).
        # Present -> must be a real bool; absent -> preserve the VM's configured
        # value, else omit (default off). fs_mediated also drives a flag file on
        # the VM (written after the push below) that a PreToolUse hook reads to
        # deny the keeper's raw Read/Grep/Glob (finder-mediated mode).
        def _opt_bool(key):
            if key in req:
                v = req.get(key)
                if not isinstance(v, bool):
                    return ("err", None)
                return ("ok", v)
            cur2 = self._steward_pull_json(vm, STEWARD_CONFIG_VM_PATH)
            if isinstance(cur2, dict) and isinstance(cur2.get(key), bool):
                return ("ok", cur2.get(key))
            return ("ok", None)
        _s, fsm = _opt_bool("fs_mediated")
        if _s == "err":
            return self._send(400, {"ok": False, "error": "fs_mediated must be a bool"})
        _s, lean = _opt_bool("lean_context")
        if _s == "err":
            return self._send(400, {"ok": False, "error": "lean_context must be a bool"})
        _s, zu = _opt_bool("zero_usage")
        if _s == "err":
            return self._send(400, {"ok": False, "error": "zero_usage must be a bool"})
        _s, rm = _opt_bool("router_mode")
        if _s == "err":
            return self._send(400, {"ok": False, "error": "router_mode must be a bool"})
        # ONLY the schema tag + the validated fields — the request object is
        # never passed through, so no arbitrary key can land in steward.json;
        # threshold_bytes/max_age_min go in only when present-and-valid or
        # carried over (max_age_min:0 is a real value and IS written).
        doc = {"schema": STEWARD_CONFIG_SCHEMA, "mode": mode, "timeout_min": tmo}
        if thr is not None:
            doc["threshold_bytes"] = thr
        if tht is not None:
            doc["threshold_tokens"] = tht
        if ma is not None:
            doc["max_age_min"] = ma
        if fsm is not None:
            doc["fs_mediated"] = fsm
        if lean is not None:
            doc["lean_context"] = lean
        if zu is not None:
            doc["zero_usage"] = zu
        if rm is not None:
            doc["router_mode"] = rm
        try:
            r = lxc(["file", "push", "-", vm + VM_HOME + _push_rel(vm, STEWARD_CONFIG_VM_PATH),
                     "--uid", VM_UID, "--gid", VM_GID, "--mode", "0644"],
                    data=json.dumps(doc).encode())
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            return self._send(502, {"ok": False,
                                    "error": "lxc push failed: " + r.stderr.decode(errors="replace")[-6000:]})
        # fs_mediated -> reconcile the PreToolUse deny flag on the VM now (the
        # enforcement the hook reads). Best-effort: the config already landed, so
        # a flag hiccup is not a 500 — the returned state shows the truth.
        if fsm is not None:
            _fcmd = ('touch "$HOME/.keeper-fs-mediated"' if fsm
                     else 'rm -f "$HOME/.keeper-fs-mediated"')
            try:
                lxc(["exec", vm, "--user", VM_UID, "--group", VM_GID,
                     "--env", "HOME=" + VM_HOME, "--", "sh", "-c", _fcmd])
            except Exception:
                pass
        # re-read both files so the drawer reflects what the host ACTUALLY wrote
        return self._steward_get(vm)

    def _steward_extend(self, vm, req):
        # minutes: an int in [1, MAX_EXTEND]; bool rejected; >cap rejected (the
        # steward's anti-dodge 60-min ceiling). The console sends 30.
        mins = req.get("minutes")
        if (not isinstance(mins, int) or isinstance(mins, bool)
                or not (1 <= mins <= STEWARD_MAX_EXTEND_MIN)):
            return self._send(400, {"ok": False,
                                    "error": "minutes must be an int in [1,%d]" % STEWARD_MAX_EXTEND_MIN})
        # Hold-file FORMAT = keeper_relay._steward_parse_hold's relative token
        # "+Nm": that parser does `_STEWARD_HOLD_RE.fullmatch(raw.strip())` with
        # _STEWARD_HOLD_RE = re.compile(r"\+(\d+)([mh])"), then target = now + N*60.
        # A RELATIVE token (not a host-computed epoch) is chosen so the extension
        # is measured from when keeper-relay READS it inside the VM — immune to any
        # host<->VM clock skew a raw epoch would suffer — and it is the keeper's own
        # documented idiom (`echo +45m > <VM_HOME>/handoff/hold`). The trailing
        # newline is fine: the parser strips it. --create-dirs so a not-yet-created
        # handoff dir never fails the write.
        hold = ("+%dm\n" % mins).encode()
        try:
            r = lxc(["file", "push", "-", vm + VM_HOME + STEWARD_HOLD_VM_PATH,
                     "--uid", VM_UID, "--gid", VM_GID, "--mode", "0644",
                     "--create-dirs"],
                    data=hold)
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            return self._send(502, {"ok": False,
                                    "error": "lxc push failed: " + r.stderr.decode(errors="replace")[-6000:]})
        # re-read state so the drawer shows the (soon-to-be-consumed) hold
        return self._steward_get(vm)

    def _steward_reset(self, vm):
        # Manual reset request (t89): push EXACTLY the steward-request.v1 body
        # keeper_relay._steward_consume_reset_request accepts
        # ({"schema":"steward-request.v1","op":"reset"}) to
        # <VM_HOME>/steward-request.json. The relay consumes the file at-most-
        # once (read-then-unlink) and fires a reset at the first lull; a
        # malformed body would be consumed-and-ignored, so ONLY this fixed doc
        # is ever written — no request field can ride into it (op:"reset"
        # accepts no other keys, enforced by the STEWARD_OPS whitelist, the
        # extend op's fail-closed discipline).
        body = {"schema": STEWARD_REQUEST_SCHEMA, "op": "reset"}
        try:
            r = lxc(["file", "push", "-", vm + VM_HOME + _push_rel(vm, STEWARD_REQUEST_VM_PATH),
                     "--uid", VM_UID, "--gid", VM_GID, "--mode", "0644"],
                    data=json.dumps(body).encode())
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            return self._send(502, {"ok": False,
                                    "error": "lxc push failed: " + r.stderr.decode(errors="replace")[-6000:]})
        # re-read both files so the drawer reflects current state (the request
        # itself is invisible here by design — it lives only until consumed)
        return self._steward_get(vm)

    # --- flow route (o19, serving t84) ----------------------------------------
    def _flow_get(self, vm):
        # GET -> {"ok": true, "state": <flow.v1 doc | null>}. Reuses the steward
        # pull helper: absent/unreadable/non-JSON flow.json -> state null with
        # ok true — absence is NOT an error, the keeper simply hasn't derived a
        # flow yet (same degrade as _steward_pull_json's own callers). A stopped
        # VM reads the same way (pull fails -> null), matching the steward GET.
        return self._send(200, {"ok": True, "state": self._steward_pull_json(vm, FLOW_VM_PATH)})

    def _flow(self, vm):
        # POST {"state": <flow.v1 doc>} -> validate fail-closed, push, re-read.
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > FLOW_BODY_MAX:
                return self._send(413, {"ok": False, "error": "body too large"})
            req = json.loads(self.rfile.read(n).decode())
        except Exception:
            return self._send(400, {"ok": False, "error": "bad JSON"})
        if not isinstance(req, dict):
            return self._send(400, {"ok": False, "error": "body must be a JSON object"})
        # The WRAPPER is strict (the t49/t54 body-key discipline): only "state"
        # may appear. The unknown-key tolerance mandated by FLOW-SCHEMA.md
        # applies INSIDE the document, not to the request envelope.
        extra = [k for k in req if k != "state"]
        if extra:
            return self._send(400, {"ok": False,
                                    "error": "unsupported key(s): %s" % ",".join(sorted(extra))})
        doc = req.get("state")
        err = _flow_validate(doc)
        if err:
            return self._send(400, {"ok": False, "error": err})
        # Written THROUGH verbatim (one json round-trip, no field rebuild) —
        # deliberately unlike _steward_config's strip-to-whitelist: flow.v1
        # REQUIRES unknown top-level keys be preserved on rewrite (either side
        # may be older than the document — see FLOW-SCHEMA.md). _flow_validate
        # already ran fail-closed on everything the schema constrains.
        try:
            r = lxc(["file", "push", "-", vm + VM_HOME + _push_rel(vm, FLOW_VM_PATH),
                     "--uid", VM_UID, "--gid", VM_GID, "--mode", "0644"],
                    data=json.dumps(doc).encode())
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            return self._send(502, {"ok": False,
                                    "error": "lxc push failed: " + r.stderr.decode(errors="replace")[-6000:]})
        # re-read the file so the console reflects what the host ACTUALLY wrote
        # (the steward config write's verify-after-write echo)
        return self._flow_get(vm)

    # --- keeper-env route (p13) -------------------------------------------
    def _keeper_env_get(self, vm):
        # GET never returns the key VALUE — set:true|false only. Missing file
        # -> env_present:false, every field at its "unset" shape (never a 500;
        # the VM simply hasn't been provisioned yet).
        try:
            r = lxc(["file", "pull", vm + KEENV_VM_PATH, "-"])
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            return self._send(200, {"vm": vm, "env_present": False, "fields": {
                "KEEPER_API_KEY": {"set": False},
                "HUGPY_BASE_URL": {"value": None},
                "HUGPY_KEEPER_MODEL": {"value": None}}})
        parsed = _keenv_parse(r.stdout or b"")
        key = parsed.get("KEEPER_API_KEY")
        return self._send(200, {"vm": vm, "env_present": True, "fields": {
            "KEEPER_API_KEY": {"set": bool(key)},
            "HUGPY_BASE_URL": {"value": parsed.get("HUGPY_BASE_URL")},
            "HUGPY_KEEPER_MODEL": {"value": parsed.get("HUGPY_KEEPER_MODEL")}}})

    def _keeper_env(self, vm):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > KEENV_BODY_MAX:
                return self._send(413, {"ok": False, "error": "body too large"})
            req = json.loads(self.rfile.read(n).decode())
        except Exception:
            return self._send(400, {"ok": False, "error": "bad JSON"})
        if not isinstance(req, dict):
            return self._send(400, {"ok": False, "error": "body must be a JSON object"})
        op = req.get("op")
        if op not in KEENV_OPS:
            return self._send(400, {"ok": False, "error": "op must be 'set'"})
        extra = [k for k in req if k not in KEENV_OPS[op]]
        if extra:
            return self._send(400, {"ok": False,
                                    "error": "unsupported key(s) for %s: %s" % (op, ",".join(sorted(extra)))})
        fields = req.get("fields")
        if not isinstance(fields, dict) or not fields:
            return self._send(400, {"ok": False, "error": "fields must be a non-empty object"})
        bad = [k for k in fields if k not in KEENV_FIELDS]
        if bad:
            return self._send(400, {"ok": False,
                                    "error": "unsupported field(s): %s" % ",".join(sorted(bad))})
        # Validate EVERY field before writing anything — fail the whole request,
        # no partial apply, on the FIRST bad value (deterministic: field order).
        validated = {}
        for field, v in fields.items():
            ok, err = _keenv_validate(field, v)
            if not ok:
                return self._send(400, {"ok": False, "error": err})
            validated[field] = v
        try:
            r = lxc(["file", "pull", vm + KEENV_VM_PATH, "-"])
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            # Honest 409 — never half-provision a VM that never ran hugpy-keeper-ensure.
            return self._send(409, {"error": "hugpy keeper not provisioned in this VM"})
        new_text = _keenv_merge((r.stdout or b"").decode("utf-8", "replace"), validated)
        try:
            rp = lxc(["file", "push", "-", vm + KEENV_VM_PATH,
                     "--uid", VM_UID, "--gid", VM_GID, "--mode", "0600"],
                    data=new_text.encode("utf-8"))
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if rp.returncode != 0:
            return self._send(502, {"ok": False,
                                    "error": "lxc push failed: " + rp.stderr.decode(errors="replace")[-6000:]})
        # belt-and-braces: `file push --mode` sets the mode on write, but chmod
        # explicitly too — the secret must never end up world/group readable.
        try:
            rc = lxc(["exec", vm, "--", "chmod", "0600", KEENV_VM_PATH])
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if rc.returncode != 0:
            return self._send(502, {"ok": False,
                                    "error": "chmod 0600 failed: " + rc.stderr.decode(errors="replace")[-6000:]})
        updated = list(validated.keys())
        # Audit line -> stderr (systemd/journald captures it, same as every other
        # request via log_message): vm + field NAMES only, NEVER the value.
        import sys
        print("keeper-env: vm=%s updated=%s ts=%d" % (vm, ",".join(updated), int(time.time())),
              file=sys.stderr)
        return self._send(200, {"ok": True, "updated": updated})

    def do_POST(self):
        km = KEENV_RE.match(self.path)
        if km:
            return self._keeper_env(km.group(1))
        flm = FLOW_RE.match(self.path)
        if flm:
            return self._flow(flm.group(1))
        sm = STEWARD_RE.match(self.path)
        if sm:
            return self._steward(sm.group(1))
        rm = REVISION_RE.match(self.path)
        if rm:
            return self._revision(rm.group(1))
        vpm = VIEWPUSH_RE.match(self.path)
        if vpm:
            return self._view_push(vpm.group(1))
        fm = FINDER_RE.match(self.path)
        if fm:
            return self._finder(fm.group(1))
        kam = KEEPERASK_RE.match(self.path)
        if kam:
            return self._keeper_ask(kam.group(1))
        m = VM_RE.match(self.path)
        if not m:
            return self._send(404, {"ok": False, "error": "unknown route"})
        vm = m.group(1)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 4096:
                return self._send(413, {"ok": False, "error": "body too large"})
            req = json.loads(self.rfile.read(n).decode())
        except Exception:
            return self._send(400, {"ok": False, "error": "bad JSON"})
        if not isinstance(req, dict) or req.get("op") != "scan":
            return self._send(400, {"ok": False, "error": "op must be 'scan'"})
        out = {"op": "scan", "ts": int(time.time())}
        src = req.get("sources")
        if src is not None:
            if (not isinstance(src, list) or not src
                    or not all(isinstance(s, str) and SRC_RE.match(s) for s in src)):
                return self._send(400, {"ok": False, "error": "sources must be a list of source names"})
            out["sources"] = src
        try:
            r = lxc(["file", "push", "-", vm + VM_HOME + _push_rel(vm, "/bugreport-request.json"),
                     "--uid", VM_UID, "--gid", VM_GID, "--mode", "0644"],
                    data=json.dumps(out).encode())
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": "lxc timeout"})
        if r.returncode != 0:
            return self._send(502, {"ok": False, "error": "lxc push failed: " + r.stderr.decode()[:200]})
        return self._send(200, {"ok": True, "queued": out})

if __name__ == "__main__":
    ThreadingHTTPServer((BIND_HOST, PORT), H).serve_forever()
