# Seat control — what every seat is handed, and where you see it (1.0.41)

> Vocabulary: **locus**, **seat**, **backend**, **model** (and A / B / C) are used here in the fixed senses of [NOMENCLATURE.md](NOMENCLATURE.md).

Rule: **if it affects a seat, it is visible and switchable in the 🛡 steward tab,
per locus.** A locus is the **⌂ keeper (host)** seat or one VM from the dropdown.

## The dropdown = the locus

- **⌂ keeper (host)** is the DEFAULT seat: this machine's own keeper console, the
  one bottleneck every host-level request passes through. No VM is ever
  auto-selected.
- Picking a VM makes THAT station the seat: its terminal (`lxc exec` as `ubuntu`,
  or ssh), its A/B/local, its steward — all **inside the VM**. A dead in-VM
  session falls back into the VM, never to a host prompt.
- **🖥 VMs → a VM → 🔌 access**: `⇥ terminal via exec`, `⇥ terminal via ssh`
  (ip, sshd state, key, the exact command), and **⧉ this station's own
  hugpy-station**: install/upgrade a headless station *inside* the VM from this
  host's package files (`install-vm-console`), token-gated on :8800, then `⧉ open`.

## 🛡 steward → 🪑 seat

| row | meaning |
|---|---|
| grounded in / as | where the seat's processes actually run, and as which unix user |
| frontier keeper | the Enabled/Disabled gate for launching A |
| claude login | OAuth / API-key state of **that locus's** `~/.claude/.credentials.json`: method, subscription, token expiry, refresh token. `✗ NONE` → **🔐 create login now (guided)** opens the claude seat, which prints the exact steps and runs Claude Code's own login picker; credentials persist for every later seat of that locus |
| frontier model · mct | the `--model` A launches with inside the MCT (`hugpy-agent mct … --model X`). Default: `claude-fable-5` |
| frontier model · claude-code | the `--model` the direct claude seat launches with. Default: `claude-fable-5` ("" = Claude Code's account default) |
| fs switch | DIRECT or MEDIATED — **frontier only** (mct + claude-code); local (B) and shell are never affected |
| 📜 frontier directive | the VERBATIM text A receives. claude-code: `--append-system-prompt` at every launch. mct: composed into `<ws>/operator-guidance.md` = directive + 🧭 operator guidance, which B injects as a `governing_instruction` every turn. `show composed text + launch lines` shows exactly what each seat gets and the full launch command |

The directive's job is **token economy, not restriction**: A is told to use the
local agent (B) for grep / fs / search / bulk reads first, and to act directly
when that is plainly better. Edit it, save, and the next launch/turn carries it.

## 🛡 steward → 🔑 sudo

One mechanism on the host and in every VM: the `agent-sudo` drop-in
`/etc/sudoers.d/agent-temp` (`NOPASSWD:ALL`, every command logged to
`/var/log/agent-sudo.log`). Grant and revoke from the same button, with a
confirm.

- **host**: `pkexec agent-sudo on|off` — polkit asks a human at this desktop.
  Other sudo sources for the user are *reported*, never touched.
- **VM**: `lxc exec <vm> -- agent-sudo on|off` as root — only the host station
  can reach it. **Revoke is real**: any other drop-in granting the user (e.g.
  cloud-init's `90-cloud-init-users`) is moved to
  `/etc/sudoers.d.disabled-by-station/` and listed; restore by hand if wanted.
- **effective now** is measured (`sudo -n true` as the grantee), so the readout
  can never say "revoked" while passwordless sudo still works.
- A (frontier) and B (local) share the seat's unix user, so a grant is **per
  locus**, never per model. The panel says so.

## Files (host side, `~/.config/hugpy-station/`)

`frontier-directive.md` (absent = shipped default), `frontier-models.json`,
`frontier-fs.json`, `vmconsole/vmconsole-<vm>.token`, `install-vm-console-<vm>.log`.
Per MCT workspace: `operator-guidance.user.md` (yours) → `operator-guidance.md` (composed, what B reads).

## API

`GET /api/seat?vm=` · `GET|POST /api/frontier/directive` · `GET|POST /api/frontier/models` ·
`GET /api/claude/auth?vm=` · `GET /api/sudo/status?vm=` · `POST /api/sudo/toggle?vm= {"desired":"on"|"off"}` ·
`GET /api/vm/<vm>/ssh-info` · `GET /api/stations/<vm>/console-status` · `POST /api/stations/<vm>/console-install`

## 1.0.41b additions

- **⇅ ssh hosts** — 🖥 → **＋ ssh host**: an existing machine (name, host, user, port,
  optional key) becomes a station. Its shell is an interactive ssh login; frontier
  (mct / claude-code), local, the seat probe, and sudo all run over ssh as that user.
  Stored in `~/.config/hugpy-station/ssh-hosts.json`; key default is
  `~/.config/hugpy-station/fleet_ssh_key` (seed it with `ssh-copy-id -i …fleet_ssh_key.pub user@host`).
- **◳ canvas always on, live both ways** — available at the host seat too
  (`~/wireframe.json`, `~/flow.json` of the station user). Local edits auto-push
  (1.2 s debounce); the locus file is polled every 3 s and loaded when it changed
  and you are not mid-edit. → VM / ← VM remain as manual overrides.
- **shell = shell** — the ⌂ keeper shell tab is a plain host login shell. Claude on
  the host is the frontier surface's `claude-code` backend.
- **B is the frontier default** (mct). A persisted `claude-code` choice from an
  earlier build is reset once; your later explicit pick sticks.
- **claude seat keeps its config dir** — `.credentials.json` and `.claude.json`
  (onboarding + account) are symlinked from the seat user's own; only `settings.json`
  is rewritten per launch. No more re-onboarding / re-login per open.
- **🔍 search** — restyled: card query area, pill filters, sticky per-file headers,
  badge counts, hover rows, and the matched term highlighted.
