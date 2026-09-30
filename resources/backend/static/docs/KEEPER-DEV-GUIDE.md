# Keeper dev guide — how to build console features safely

> Vocabulary: **locus**, **seat**, **backend**, **model** (and A / B / C) are used here in the fixed senses of [NOMENCLATURE.md](NOMENCLATURE.md).

This is the reference for the **development end** of the fleet console: how a
keeper adds or changes a feature without ever putting the live console at risk.
Every pattern here is one the station actually runs on — not aspiration.

The console has two ends, and this repo owns one of them:

- **Frontend (yours):** `index.html`, `wireframe.js`, `vendor/` — this git repo.
- **Backend (host-side, NOT yours):** `console-api` (lxc power, todo/design
  drawers, the Discord bridge), the ttyd terminals, nginx, `.htpasswd`. A VM
  can't run these — they need the host's `lxd` group. You build *against* the
  `/api/...` contract; you don't assume behavior that isn't in it.

The whole discipline exists because **editing and serving are decoupled**: a bad
edit can only ever break staging. The live console never flinches.

---

## 1. The loop: edit → verify → commit → promote

1. **Edit** the repo (`/srv/share/projects/blackbird/console-ui`, also `~/console-ui`).
2. **Verify on staging**: `https://console-dev.abstractendeavors.com` — a host
   vhost that proxies `/` to your dev server (`console-ui-dev.service`, :8790,
   serves this repo directory live, no restart needed) and `/api/` + `/term/`
   to the **real** host backend. So you test the actual UI against the actual
   backend. The keeper reaches staging without a password (LAN/bridge allow);
   it currently serves a **self-signed cert**, so `curl -sk` (a bare `curl -s`
   silently returns an empty body — a classic false "staging is down").
3. **Commit** every change — git history is the rollback.
4. **Promote** only when verified:
   `touch signals/deploy-ui.trigger`. A host poller (~1 min) validates the
   payload (index sanity + `node --check` on JS), backs up the live copy, and
   publishes — or parks `signals/deploy-ui.failed` with a reason and touches
   nothing live. Outcome lands in `signals/last-deploy.json`
   (`{status, ts, commit, files, backup}`).

**Never hand-edit the host's live console files.** Repo → staging → promote,
always. That decoupling is the entire point.

Only files in `deploy-manifest.txt` ever ship. That list is `index.html`,
`wireframe.js`, `vendor/*` — plus these two guides in `docs/`, which the 📖
docs drawer serves to users, so **edits to them are user-visible on the next
promote**. Everything else (`dev/`, `README.md`, other docs) stays a
development reference that never reaches the live docroot. Write freely; write
the shipped guides carefully.

---

## 2. Verification is mandatory, and specific

Three gates, in order. Do not promote until all three are green.

- **`node --check` on the extracted inline script.** The app is one big inline
  `<script>` (real React + htm tagged templates); extract it to a temp file and
  syntax-check. Catches parse errors htm would only reveal at runtime.
- **The browser harness: `node dev/verify-ui.js`.** Playwright + headless
  chromium drives the *real* UI against a stubbed backend across four
  environments (desktop, crowded drawer+dock, narrow, mobile). This exists
  because **`node --check` and DOM assertions structurally cannot see a CSS
  overlay or a dead click** — the design drawer was inert since the repo's birth
  (an unscoped `.hint{position:absolute;inset:0}` covering the canvas) and only
  a real browser caught it. Extend the harness with every interactive feature
  you add; keep every prior step green.
  - Two gotchas baked in: chromium needs `--no-sandbox` in this LXD VM;
    Playwright matches `page.route` patterns in **reverse** registration order,
    so register catch-alls first, specifics last.
- **md5 parity before promote:** `md5sum index.html` ==
  `curl -sk https://console-dev.abstractendeavors.com/index.html | md5sum`.
  Proves staging is serving exactly what you're about to publish.

After promotion, read `signals/last-deploy.json` and confirm `status:ok`.

---

## 3. The file-as-interface pattern

The console exchanges per-task work with the keeper through plain files in the
VM home dir, because the console backend runs host-side and the keeper runs in
the VM — a file both sides can read/write is the whole contract.

- **`~/todo.json`** (schema `todo.v1`) — the task board. `GET /api/vm/<vm>/todo`
  is transparent; `add`/`update` go through a **whitelist** (see §5).
- **◳ canvas** (`wireframe.v1` design, `flow.v1` flow) — NOT a file any more
  (2026-09-03): one row per (locus, kind) in the toolserver `canvas` table.
  Read `canvas/get {locus, kind}`, write `canvas/put {locus, kind, state[,
  by, note, notify]}` (MCP tools `canvas_get` / `canvas_put`); the console's
  ◳ tab shows the same row live. `~/wireframe.json` / `~/flow.json` are dead.
- **`~/bugreport.json`** + **`~/bugreport-request.json`** — the bug reporter's
  output and its scan-now trigger.

When you add a feature that needs the keeper and the console to share state, add
a file with a documented schema — don't invent a side channel. Keep writes
atomic (tmp + `os.replace`) and validate after writing; the console refuses a
malformed file rather than repairing it.

---

## 4. When a feature needs the host: the sidecar pattern

Some features need something only the host can do — `lxc file` into a VM, run a
VM's CLI, read live fleet state. You **cannot** edit `console-api` (you can't
see its source, and blind-patching a live backend is how consoles die). Instead:

**Additive sidecar + marked nginx block.** A small, localhost-only service does
the privileged bit; one clearly-marked `location` block per console vhost
proxies just that path to it. `console-api` is never touched. See
`bugreport/host/install-bugreport-route.sh` for the reference implementation:

- Localhost-bound stdlib HTTP service; **argv-only** subprocess calls (no shell
  — a search string may legally start with `-`); hard-pinned in-VM paths;
  regex-validated inputs; timeouts and output caps.
- The installer is **idempotent** (marker-guarded), **backs up** each vhost,
  **`nginx -t`-gates** with automatic restore on failure, and shows a **diff +
  y/N** before touching anything.
- Each route gets its own marker so features install independently.
- **Discovery caveat learned the hard way:** this host keeps real vhosts in a
  nested `sites-available/<domain>/ports/443/` tree with `sites-enabled` stubs;
  search all of `sites-enabled/`, `sites-available/`, `conf.d/`, skip backups,
  and insert only inside the `:443` server block (console-dev opens with an
  `:80` redirect block first).

The route contract stays identical to what a `console-api` route would expose,
so it can be folded into `console-api` later with no UI change.

---

## 5. Backend-contract discipline (this bites, repeatedly)

`console-api`'s todo ops enforce **field whitelists**, and they fail *silently*:

- `add` coerces an unknown `type` to `todo` and returns `ok:true`.
- `update` keeps `text`/`note`/`status` and **silently drops** everything else
  (`comments`, `query`, `type`, …) while still answering `ok:true`.

So a write can "succeed" and lose your data. Three rules:

1. **Probe before you build.** Don't assume — POST a probe item, read it back,
   see what survives. (Needs the `X-Console: 1` header.)
2. **Verify-after-write.** Re-read and compare; `ok:true` is not proof.
3. **Render honestly.** If the backend can't persist a field yet, the UI says so
   plainly ("comments aren't enabled on the backend yet") rather than showing a
   phantom that vanishes on refresh. When the whitelist widens, the same code
   just starts working — no UI change.

Widening a whitelist is a one-line host change you can't make — file it as an
**operator task** (`type:"operator"`) with the exact edit. Keeper-authored items
carry any field fine (the file write isn't whitelisted); only console-*written*
items hit the wall. That asymmetry is why most structured board items are
keeper-filed.

---

## 6. Dispatching implementers

Substantive features are built by a scoped subagent, then **verified by the
keeper** before promotion. The contract that works:

- **Scoped brief:** exact repo + HEAD, the guardrails ("touch only X"), the real
  API shapes (paste them — don't make the agent guess), the verification gates
  it must pass, and "report raw."
- **Serialize edits to a single-file app.** `index.html` is one file; two agents
  editing it collide. Run drawer builds one at a time.
- **Verify their claims yourself.** Re-run the harness, re-check md5, spot-check
  the behavior. An agent reporting "56/56 green" is a claim until you reproduce
  it. Promote only after your own re-run.
- **Behavior wins over the brief.** If the real engine/backend differs from what
  you told the agent, the truth wins and gets documented — don't normalize it
  away.

---

## 7. Board discipline

- **Stamp `via:"keeper"` on every board write.** The keeper-relay watcher pings
  the keeper terminal on new `~/todo.json` items; `by` records who *asked*
  (truthful — operator requests stay `by:"user"` even when keeper-filed), `via`
  records who *wrote*. Without the stamp, filing an item self-echoes into your
  own terminal.
- **Use the `todo` CLI** (`~/.local/bin/todo`, repo `todo-cli/`): it stamps
  `via` automatically, writes atomically, preserves unknown fields, and refuses
  a malformed board untouched. `todo add|done|doing|note|comment|bookmark|
  del|list|show`.
- Keep statuses current; add `bookmark` items at stable builds (commit in the
  note); keep the JSON valid — the console refuses a malformed file.
- **Write items to the templates.** UI-GUIDE.md's "Write items like these"
  section (visible to users in the 📖 docs drawer) is the canonical quality
  bar per type — operator-designated, with the proposal card as the standard
  the others follow. An item should carry its evidence and its lifecycle so
  the card alone tells the whole story.

---

## 8. The checklist

```
[ ] edit the repo (never the live docroot)
[ ] node --check the extracted inline script
[ ] extend dev/verify-ui.js for the new surface; full harness green (4 envs)
[ ] probe/verify any backend field you rely on; render honestly if unsupported
[ ] commit with a why-message
[ ] md5: repo == console-dev staging
[ ] touch signals/deploy-ui.trigger
[ ] read signals/last-deploy.json → status:ok
[ ] board: todo done <id> (via the CLI); bookmark the deploy
[ ] anything host-side you couldn't do → file an operator task with the command
```

Sibling repos on the same discipline: `bugreport/` (scanner + host route),
`finder/` (search CLI over the operator's `abstract_search`), `keeper-relay/`
(Discord + board injection), `todo-cli/`. The dev→verify→promote spirit is the
same everywhere; only console-ui has the signal-based promote.
