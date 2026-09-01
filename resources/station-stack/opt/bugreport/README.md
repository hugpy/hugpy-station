# bugreport-scanner

VM-side log scanner + local-LLM digest engine. Accepted-p5 pilot of **lane #4**
(log/journal triage) from `/srv/share/projects/blackbird/LOCAL-LLM-OFFLOAD.md`.

Stdlib-only Python 3.12. Runs as a systemd **system** unit on this VM
(`bugreport-scanner.service`, `User=ubuntu`). The output file
`~/bugreport.json` is the **console-drawer contract** — schema `bugreport.v1`,
kept stable and documented below.

## The triage ladder (deterministic first, LLM last)

1. **Collect** since the last scan — journal via `journalctl --after-cursor`
   (first run `--since`), files by byte offset (rotation-safe: `offset > size`
   resets to 0; only advances past the last complete line).
2. **Filter + dedup** — collapse duplicates to `(count, first_ts, last_ts,
   message)` keyed by `(source, normalized-signature)`. Normalization folds
   long hex ids, numbers, and whitespace. On this box a typical hour of
   warnings dedups ~900 raw lines to 1 unique cluster.
3. **Digest (LLM, advisory)** — only when there is residue **and** at most
   hourly, **or** an `err`-level cluster is present, **or** an on-demand request
   forces it. POSTs a ~9KB numbered sample to hugpy
   (`Qwen~Qwen3-Coder-Next-GGUF`, `max_tokens=512`, 300s timeout, one retry).
   The sample is capped; `sources[].capped` and the deterministic summary flag
   when clusters were dropped.
4. **Citation gate (machine-enforced, lane-4)** — every claim bullet/paragraph
   must cite `[Ln]` mapping to a real input line. Uncited claims are
   **discarded** and counted in `digest_meta.uncited_discarded`. The LLM is
   strictly advisory: **the deterministic summary is always present**, so the
   scanner is fully useful with hugpy unreachable.
5. **Write** `~/bugreport.json` atomically (tmp + `os.replace`).

## Files

| Path | Role |
|------|------|
| `scanner.py` | scanner + digest engine + service loop (importable helpers) |
| `test_scanner.py` | offline unit tests (dedup, rotation, citation gate, request, atomic write) |
| `bugreport-scanner.service` | systemd system unit |
| `install.sh` | installs the unit (sudo: daemon-reload + enable --now) |
| `~/.config/bugreport/sources.json` | source config (auto-created with defaults) |
| `~/.local/state/bugreport/cursors.json` | per-source cursors / byte offsets (triage) |
| `~/.local/state/bugreport/watch_cursors.json` | per-watch cursors — **separate namespace** so a watch never disturbs a triage read of the same file |
| `~/.local/state/bugreport/watch_http_state.json` | per-`http`-watch transition state (`{id → last_status}`, recency-ordered, capped) — the rolling-window dedup memory |
| `~/.local/state/bugreport/series/<watch>.jsonl` | compiled per-watch match series (ring-capped) |
| `~/.local/state/bugreport/meta.json` | last_scan_ts / last_digest_ts |
| `~/.local/state/bugreport/audit.jsonl` | append-only audit (one line per scan/digest/request/watch-invalid) |
| `~/bugreport.json` | **output — the console-drawer contract** |
| `~/bugreport-request.json` | **input — the on-demand scan trigger** |

## Sources config — `~/.config/bugreport/sources.json`

A named, first-class list. Each scan reads only **enabled** sources; every
extracted line is tagged with its source name.

```json
{
  "first_run_since": "-1h",
  "sources": [
    {"name": "journal-warnings",  "kind": "journal", "priority": "warning", "enabled": true},
    {"name": "journal-key-units", "kind": "journal", "units": ["console-ui-dev", "keeper-relay"], "priority": "info", "enabled": true},
    {"name": "keeper-relay-audit","kind": "file", "path": "/home/ubuntu/.local/state/keeper-relay/audit.jsonl", "enabled": true}
  ]
}
```

- **`kind: "journal"`** — whole journal at a `priority`, and/or specific
  `units` (list → multiple `-u`). Severity comes from the journal `PRIORITY`.
- **`kind: "file"`** — `path` is a literal path **or** a glob (`*?[`). A
  missing path / empty glob is a per-source warning, never fatal. Severity is
  pattern-inferred (`error|fail|critical|fatal|traceback|exception` → `err`,
  `warn` → `warning`, else `info`).

## Watches — `watches` (operator-dictated, deterministic, no LLM)

Beyond one-shot triage, the scanner **watches + compiles**: an operator-dictated
list of regex rules, evaluated **incrementally each cadence** over their own
source, compiling a per-group **status-over-time series**. There is **no LLM
anywhere** in this path — it is fully deterministic. The `watches` array is
optional and empty by default (additive; existing installs need no migration).

```json
{
  "watches": [
    {"name": "relay-events", "enabled": true,
     "source": {"kind": "file", "path": "/home/ubuntu/.local/state/keeper-relay/audit.jsonl"},
     "pattern": "\"event\"\\s*:\\s*\"(?P<event>[^\"]+)\"",
     "key": "event"},

    {"name": "svc-health", "enabled": true,
     "source": {"kind": "journal", "unit": "my-service"},
     "pattern": "svc=(?P<svc>\\S+) status=(?P<st>\\S+)",
     "key": "svc", "status": "st"}
  ]
}
```

- **`name`** — identifies the watch; sanitized to a safe series filename
  (`[^A-Za-z0-9._-]`→`_`, leading dots stripped, never a path separator).
- **`source`** — `{"kind":"file","path":"<path|glob>"}`,
  `{"kind":"journal","unit":"<unit>"}`, or `{"kind":"http","url":"<url>"}` (the
  endpoint kind — see below). The **line kinds** (`file`/`journal`) are read
  incrementally via a **private cursor** in `watch_cursors.json`, so a watch and
  a triage source may read the same file without disturbing each other's offset.
  (Journal watch lines are matched against the rendered `"<unit>: <MESSAGE>"`
  string; use `re.search`, not an anchored pattern.)
- **`pattern`** — a Python regex with **named captures** (`(?P<name>…)`),
  matched with `re.search` against each new line.
- **`key`** (required) — the capture to **group by**. Must be a named group in
  the pattern.
- **`status`** (optional) — a capture holding a status value. Omit it for
  **count mode** (matches are counted per key, no per-group status). Must be a
  named group in the pattern when present.

**Fail-closed validation** (on every load): a missing/invalid `source`, unknown
`kind`, missing/absent unit or path, missing or **uncompilable** `pattern`, or a
`key`/`status` that does **not** name a capture in the pattern → the watch is
marked **invalid and SKIPPED** (never evaluated, never a crash, never a partial
result). For an `http` watch the required fields differ (a `url` + the three
JSON field names below, **not** `pattern`/`key`/`status`) — validation is
**per-kind**, so a line watch and an endpoint watch each enforce only their own
shape. The reason is carried into the payload as the watch's `error` field and
recorded once per scan as an `event:"watch-invalid"` audit line.

**Series** — every match appends one compact record to
`~/.local/state/bugreport/series/<watch>.jsonl`:

```json
{"ts": 1784328822.79, "key": "todo-notify", "status": null, "fields": {"code": "200"}}
```

`ts` is the line's timestamp (journal) or the scan time (file, which carries no
per-line ts). `fields` holds every **other** named capture (not `key`/`status`).
Each series file is ring-capped: once it exceeds **5000** lines it is
**atomically rewritten** keeping the newest **4000**.

### `http` source kind — endpoint watches (rolling-window accumulation)

Beyond files and the journal, a watch can poll a **remote JSON endpoint** and
accumulate a **models × generations status history** into the exact same
series/payload machinery (so the console's watches tab renders it with **zero UI
changes**). The pilot is hugpy's jobs endpoint:

```json
{
  "watches": [
    {"name": "hugpy-generations", "enabled": true,
     "source": {"kind": "http", "url": "https://dev.hugpy.ai/api/llm/jobs"},
     "id_field": "id", "key_field": "model_key", "status_field": "status",
     "capture": ["kind", "worker", "error"]}
  ]
}
```

- **`source.url`** (required) — the endpoint. GET each cadence (and on-demand)
  with a **20 s timeout and no retries** — the next cadence *is* the retry. The
  response may be a **bare JSON array** or a wrapper (`{"jobs":[…]}` /
  `data`/`results`/`items`); both are parsed. hugpy wraps under `jobs`.
- **`id_field`** / **`key_field`** / **`status_field`** (all required) — the JSON
  field names on each row for the **row id** (dedup identity), the **group key**
  (→ series `key`, e.g. `model_key`), and the **status** (→ series `status`). A
  row missing any of the three is **skipped and counted**, never fatal.
- **`capture`** (optional list of field names) — extra fields copied into the
  series record's `fields` (alongside `id`). A `null`/absent captured field is
  simply omitted.
- **`ts`** per row is chosen defensively: `progressed_at` → `updated_at` →
  `created_at` → scan time. Each is parsed as an **epoch number *or* an ISO-8601
  string** (hugpy mixes both: `progressed_at` is an epoch float, the others are
  ISO); naive ISO is read as UTC.

**Rolling-window accumulation contract** (the reason this kind exists): the
endpoint is a **rolling ~10-row window** — terminal rows (`done`/`failed`/
`expired`/…) prune out of it over time. So the scanner keeps its **own** history.
It persists a per-watch `{id → last_status}` map (`watch_http_state.json`,
recency-ordered, capped at **2000** ids, **oldest-seen pruned first**) and, on
each poll, appends **one series record per (id, status) *transition*** — a **new
id**, or a **known id whose status changed**. A row whose `(id,status)` is
**unchanged** since last poll is a **no-op**, so re-reading the same window every
cadence **never duplicates**. A **missed** transition between two polls (a status
the window pruned before we saw it) is **acceptable and expected**; a
**duplicated** one is not. An id that was pruned from our own state and later
**reappears** re-fires once — accepted, and cheap given the 2000-id cap. The
first prune is audited once (`event:"watch-http-prune"`).

**Endpoint failure is not a scan failure.** A timeout, non-200, non-JSON body, or
unrecognised shape surfaces as that watch's `error` in the payload row for that
scan (the row still folds its **accumulated** series — history is never wiped),
is logged, and is **audited once per distinct error string**
(`event:"watch-http-error"`). Crucially, a failed poll **does not touch the
transition state**, so recovery does **not** re-fire the whole window. Every
other watch and the whole triage/digest path are unaffected.

**Boundary note (remote-only).** This watch reaches hugpy **purely over HTTP from
this VM** — there is **nothing installed on the hugpy VM**. Observing the jobs
window from outside is the hugpy keeper's explicit boundary; the scanner treats
the endpoint as a read-only external surface and owns its own accumulation.

## Output schema — `bugreport.v1` (the console-drawer contract)

```jsonc
{
  "schema": "bugreport.v1",
  "generated_ts": 1784279700.1,
  "status": {
    "failed_units": ["systemd-networkd-wait-online.service"],
    "key_units": {"console-ui-dev": "active", "keeper-relay": "active"}
  },
  "sources": [
    {"name": "journal-warnings", "kind": "journal", "enabled": true,
     "lines_seen": 903, "unique": 1, "capped": false}
  ],
  "findings": [
    // deterministic (always present): source is the log source name
    {"severity": "err", "claim": "8x {...fetch-failed:HTTPError...}",
     "citations": [1], "source": "keeper-relay-audit"},
    // digest (advisory, citation-gated): source == "digest"
    {"severity": "err", "claim": "HTTP fetch failures in keeper-relay-audit [L1]",
     "citations": [1], "source": "digest"}
  ],
  "digest_meta": {"ran": true, "model": "Qwen~Qwen3-Coder-Next-GGUF",
                  "wall_s": 15.9, "uncited_discarded": 0, "error": null},
  "watches": [
    {"name": "relay-events", "enabled": true, "error": null,
     "matched_total": 11, "matched_last_scan": 0, "groups_truncated": false,
     "groups": {
       "todo-notify": {"status": null, "last_ts": 1784328822.79,
                       "count": 11, "statuses": {}}
     }},
    // status-mode watch: per-group latest status + a status histogram
    {"name": "svc-health", "enabled": true, "error": null,
     "matched_total": 3, "matched_last_scan": 3, "groups_truncated": false,
     "groups": {
       "api": {"status": "degraded", "last_ts": 1784328789.26,
               "count": 2, "statuses": {"ok": 1, "degraded": 1}}
     }}
  ],
  "activity": {
    "keeper_last_activity_ts": 1784353925.12,
    "board_quiet_secs": null,
    "loadavg": [0.05, 0.16, 0.08],
    "mem_available_kb": 4958156,
    "mem_total_kb": 6041656,
    "disk_free_gb": 57.69,
    "disk_total_gb": 102.89,
    "collected_ts": 1784354046.49
  }
}
```

### `watches[]` — the drawer-tab contract (consumed verbatim by the console follow-up)

Deterministic; grows the payload additively (absent/empty `watches` leaves all
prior fields byte-identical). One row **per configured watch**, in config order:

- `name` — watch name. `enabled` — its config flag. `error` — `null` when valid,
  else the fail-closed reason string (**render the row, greyed, showing the
  error** — an invalid watch is visible, never silently absent). For an `http`
  watch this same field also carries a **transient poll error** for a scan whose
  GET failed (the row still shows its accumulated `groups`); it clears back to
  `null` on the next successful poll.
- `matched_total` — total records in the series file (all history, post ring-cap).
- `matched_last_scan` — matches appended in the most recent scan (0 for
  disabled/invalid watches, and for a valid watch that saw no new matching lines).
- `groups` — map of **key value → group**, each:
  - `status` — the **latest** status (by `ts`); `null` in count mode.
  - `last_ts` — epoch of the most recent match for this key.
  - `count` — total matches for this key.
  - `statuses` — `{status: count}` histogram (empty `{}` in count mode).
- `groups` is capped at the **50 most-recently-active** keys; `groups_truncated`
  is `true` when that cap dropped keys (`matched_total` still counts **all**
  records).
- A valid watch with **no matches yet** still appears with `groups: {}` and
  `matched_total: 0` — **armed but dry**, so the operator sees it is live, not
  missing.

- `findings[].citations` are integer line numbers into the digest sample. Every
  deterministic finding cites its own sample line; every `source:"digest"`
  finding cites the line(s) it drew from (guaranteed valid by the gate).
- `digest_meta.error` is set (and `ran:false`) when the LLM was unreachable /
  timed out — findings are still fully populated deterministically.

### `activity` — the activity-meter contract (t46, deterministic, no LLM)

A best-effort **activity + system-vitals** snapshot, collected fresh on **every**
scan (cadence and on-demand) for the console's activity meter. Grows the payload
additively; wholly independent of the LLM/digest path. **Every field is
independently fail-soft**: any single probe failing yields `null` for that field
alone and never fails the scan (a scan with every probe broken still writes a
full report with a nulls-filled `activity`). `collected_ts` is the only field
always present as a float.

- `keeper_last_activity_ts` — the keeper-relay's `last_activity_ts` (epoch
  float), read from `~/.local/state/keeper-relay/state.json`. The relay refreshes
  it whenever the keeper is observed **working**, so it is the freshest "keeper is
  alive" signal — drive the meter's staleness from `now - keeper_last_activity_ts`.
  `null` if the state file is missing / unparseable / lacks the key. **Read-only**:
  the scanner never writes the relay's state.
- `board_quiet_secs` — the relay's effective board-quiet window (seconds), **only**
  if it is cheaply present as a flat key in that same state file; otherwise `null`.
  It is currently `null` because the relay resolves this window from its own
  env/default config, which the scanner deliberately does **not** reimplement.
  (Forward-compatible: if the relay ever persists `board_quiet_sec`/`board_quiet_secs`
  in state, it is surfaced here automatically.)
- `loadavg` — `[1m, 5m, 15m]` from `os.getloadavg()`; `null` if unavailable.
- `mem_available_kb` / `mem_total_kb` — from `/proc/meminfo`
  (`MemAvailable` / `MemTotal`, in kB); either is `null` if unreadable.
- `disk_free_gb` / `disk_total_gb` — `shutil.disk_usage("/")` free/total bytes ÷
  1e9 (GB = 10⁹ bytes), rounded to 2 dp; `null` on error.
- `collected_ts` — epoch float when this snapshot was taken (always present).

## On-demand trigger — `~/bugreport-request.json` (the GUI contract)

The console's *scan-now* button (host route, later) writes:

```json
{"op": "scan", "sources": ["journal-warnings"], "ts": 1784279700}
```

- `op` must be `"scan"`; `sources` is **optional** (default: all enabled).
- Consumed **at-most-once**: the file is read then deleted before the scan
  runs. Malformed / unknown content is logged and ignored (audited
  `decision:"ignored"`). A request forces a digest (bypasses the hourly gate).

## Service cadence

- Poll the request file every ~5s; cadence scan every 15 min.
- Digest at most hourly, **except** an on-demand request or any `err`-or-higher
  unique cluster triggers one inside the hour.
- `Restart=on-failure`. Audit every scan/digest/request to `audit.jsonl`.

## Run / test / install

```bash
python3 -m unittest -v test_scanner      # offline suite
python3 scanner.py --readiness           # tiny echo call to check the endpoint
python3 scanner.py --once --no-llm        # deterministic-only scan
python3 scanner.py --once --force-digest  # full scan + forced digest
./install.sh                              # install + enable the system unit
```
