# Fleet ops guide — the keeper, its stations, and every way in

> Vocabulary: **locus**, **seat**, **backend**, **model** (and A / B / C) are used here in the fixed senses of [NOMENCLATURE.md](NOMENCLATURE.md).

How the fleet fits together and how to drive it: creating stations, where the
models live, the mail bus between them, and the four ways to reach the console
(app, ssh, LAN browser, your own domain). This is the **operations end** — the
UI guide covers the page itself; this covers the fleet.

---

## The shape of the fleet

- **The keeper console** is this machine — the fleet's **host arm**. It is the
  ONE console that exists on first launch, and the only place host-grounded
  models run. Its name is `keeper` everywhere: the station list, the board
  routes, the mail bus. (A real VM can never take that name — it's reserved.)
- **Stations** are LXD virtual machines the console builds and manages. Each
  station is self-contained: **its own A, B, and local keeper live INSIDE it**,
  its shell is a terminal inside it, and it has a mail line to the keeper
  console. Stations appear in the tab strip the moment they exist.
- **The grounding rule** (the one idea that explains most of this page): a
  model surface always grounds in the ACTIVE console. Select a station → its
  A/B/local/shell are that station's own, seated in that VM. Select `keeper`
  (or nothing) → you get the host seats. The fleet never mixes them up.

---

## Creating a station

### From the app

**+VM** in the actions bar. Name it, pick resources, go. The build streams
into the panel; when it finishes the station is a tab like any other.

### From any ssh session

The button and the CLI are the same builder:

```
ssh op@fleet-host
fleet-vm-new mystation --yes
```

`--yes` skips the confirm prompt (required non-interactively). The useful
options, with their defaults:

```
fleet-vm-new mystation --yes \
    --cpu 4 --mem 8GiB --disk 100GiB    # resources (these are the defaults)
    --from goldenvm                     # CoW-clone an existing instance
                                        #   (default: clone 'golden' if present,
                                        #    else fresh ubuntu:24.04)
    --snapshot                          # take a 'baseline' snapshot when done
    --no-seed                           # bare VM: skip every seat below
```

Requirements: an account in the `lxd` group (the installer was added
automatically; others: `sudo usermod -aG lxd <user>`, re-login). No sudo.

### What a new station comes with (the seats)

Every build seeds these, best-effort — a miss logs a WARNING, never a failed
build, and the console grays out whatever is absent. The build summary prints
one `seat <name> : ok` line per seat:

- **claude** — A, Claude Code, installed for the dev user at
  `~/.local/bin/claude` (exactly where the keeper launcher looks). It asks
  you to log in on its first launch in each station — credentials are never
  copied into a VM.
- **hugpy-agent** — B and the local keeper (`hugpy-agent mct`,
  `hugpy-agent console`), plus `~/.mct` session workspaces.
- **opencode** — the local seat's TUI; `hugpy-agent console` execs it, wired
  to the fleet's models.
- **qwen** — the local seat's alternate frontend
  (`hugpy-agent console --frontend qwen-code`); Node 22 comes with it.
- **clawd** — Clawd-Codex, the frontier's third-party A CLI, seeded from
  your source checkout (auto-detected from this host's own install, or
  `FLEET_CLAWD_SRC=/path/to/checkout`). No source around → skipped with a
  note.
- **tmux** — what keeps a station's keeper session alive across console
  restarts.
- **fleet-msg** — the station's mail line to the keeper console (below).
- **sshd + keys** — the station accepts `ssh ubuntu@<ip>`: the fleet key
  (below) and your own `~/.ssh/id_*.pub` when you have one.

---

## Terminals: surfaces, grounding, and the two gates

The ⌨ terminal pane has three persistent surfaces; each keeps its own
server-side session per grounding, so switching never loses scrollback:

- **local** — talk to B directly (OpenCode by default; qwen-code in the
  backend dropdown). Works without A.
- **frontier** — the A seat. The backend dropdown picks what runs it:
  `mct` (the Mediated Context Terminal, you → B → A), `claude-code`
  (Claude Code direct, exactly as it runs normally), or `clawd-code`.
  Changing the backend restarts only this surface.
- **shell** — a real shell. Its backend dropdown picks the transport:
  - **exec** (default) — `lxc exec`, works even with no network path;
  - **ssh** — a real sshd login at the station's IP (see SSH below).

> **Deprecated 2026-09-17: the A: on/off and B: on/off gate chips, the
> ⧉ copy chip and the ▤ /cmds palette are gone from the terminal bar — use
> the abstract-claude serve console.** The tmux seats are emergency-only and
> both gates behave as always-on (their old default). Copy still works from
> every terminal pane: right-click a selection, or Ctrl/Cmd+Shift+C.

The status line always tells you the truth about where you are:
`frontier:mct@mystation`, `ssh@mystation`, `shell@host`.

Selecting a VM tab regrounds all three surfaces to that station; selecting
`keeper` grounds them on the host. The backend availability in the dropdowns
is probed in whatever the surfaces are currently grounded in.

---

## ✉ Fleet mail — stations ↔ the keeper console

Every station can message the keeper console; the keeper console can message
every station individually. Transport is files + `lxc exec` (no network
dependency), so it works everywhere the console does.

### From inside a station (agents or you)

```
fleet-msg the deploy finished — service is back and healthy
fleet-msg --inbox            # read mail the keeper console sent this station
```

Messages are collected within ~30 seconds and land in the **keeper console's
✉ messages tab** (board drawer on the `keeper` tab) with an unread badge.

### From the keeper console

Open a station's board drawer → **✉ messages** → compose box at the bottom:
type and send. On the `keeper` drawer the compose has a station dropdown
instead. Delivery does two things:

1. the message lands in the station's inbox file (`~/.bridge-mail.jsonl` —
   its ✉ tab and `fleet-msg --inbox` both read it);
2. **the station's A gets pinged**: a `✉ fleet mail from …` line is typed
   into its live keeper session (Claude Code queues input that arrives
   mid-turn, so nothing is interrupted). No live keeper → the mail just
   waits in the inbox, and the compose status says which happened.

Scripted sending (any logged-in session):

```
curl -s -X POST https://your.console/api/fleet/message \
  -H 'Content-Type: application/json' -b "$COOKIES" -H "X-Console-CSRF: $CSRF" \
  -d '{"to": "mystation", "text": "nightly build is green"}'
```

---

## SSH — into stations, without ceremony

Every station is reachable as `ssh ubuntu@<ip>` (the build summary prints the
exact line). Two keys are seeded:

- **The fleet key** — minted automatically on your very first station build
  (`~/.config/hugpy-station/fleet_ssh_key`, ed25519, passphraseless, never
  leaves this machine). The console's in-app **ssh transport pins to it**, so
  those logins never prompt and never depend on your desktop key agent.
- **Your own key** — the first of `~/.ssh/id_ed25519.pub / id_rsa.pub /
  id_ecdsa.pub`, for logging in from your other LAN machines. Override with:

```
FLEET_SSH_PUBKEY="ssh-ed25519 AAAA… you@laptop" fleet-vm-new mystation --yes
```

In the app: shell surface → backend dropdown → **ssh**. Host keys are not
pinned (stations are rebuilt freely and regenerate them) — treat the LAN
bridge as trusted, or pin manually if your threat model needs it.

---

## ▤ /cmds — the frontier command palette (deprecated 2026-09-17)

The palette (a form over the mct REPL's slash commands: `/model`, `/bmodel`,
`/log`, `/frontier`, `/native`, …) was removed from the terminal bar — use
the abstract-claude serve console. In an emergency tmux seat, `/help` at the
REPL prompt still lists the authoritative commands.

---

## 🌐 Browser access — the console from any LAN machine

The console IS a web app; the desktop shell is just a window onto it.

- **This machine, right now**: `http://127.0.0.1:8899/` (menu: Help → Open
  in browser). Always works, no setup.
- **LAN browsers**: steward drawer → **🌐 browser access**:
  1. **Set a password** (8+ chars; stored pbkdf2-hashed in your config dir).
     From that moment every new page load — the app window too — logs in as
     `admin` once per day.
  2. **Enable** — takes effect on the next console launch, which then
     listens LAN-wide. The panel lists your per-interface URLs with ⧉ copy.
- Fail-closed by design: enabling without a password is refused, and a
  wide bind without auth refuses to start. LAN traffic is plain http unless
  you configure TLS — or use the domain flow below, which handles TLS for
  you.

---

## Publishing at a domain

One command, after two prerequisites:

1. DNS: point `console.your.tld`'s A record at this machine (port 80
   reachable — Let's Encrypt validates over it);
2. Password: set it in 🌐 browser access (the command refuses without one).

```
sudo hugpy-station-domain console.your.tld
```

That wires, idempotently:

- **`hugpy-station-web@you`** — a headless systemd service running the same
  backend on `127.0.0.1:8898`, always-on (the desktop app can stay closed),
  and hard-refusing to start without a password;
- **nginx** — reverse proxy with websocket upgrades (terminals stay open for
  days), `X-Real-IP` (audit + login rate-limits see the true client), big
  uploads, unbuffered streams;
- **certbot** — Let's Encrypt certificate + http→https redirect.

Then: `https://console.your.tld`, log in as `admin`. Variants:

```
hugpy-station-domain --print-nginx console.your.tld   # preview the site config
sudo hugpy-station-domain console.your.tld --no-cert  # http only (testing)
sudo hugpy-station-domain console.your.tld --port 8901 --user someone
systemctl status hugpy-station-web@op                 # the headless console
```

---

## Reference

### Commands (on the host)

- `fleet-vm-new <name> --yes [opts]` — build a station (identical to +VM).
- `hugpy-station-domain <domain>` — publish the console at a domain.
- `hugpy-station` — launch the desktop app.

### Commands (inside a station)

- `fleet-msg <text…>` / `fleet-msg --inbox [N]` — the mail line.
- `claude`, `hugpy-agent mct|console`, `opencode`, `qwen`, `clawd` — the seats.

### Environment knobs (set before running the builder / console)

- `FLEET_KEEPER_NAME` — rename the keeper seat (default `keeper`).
- `FLEET_SSH_PUBKEY` — the public key to seed instead of `~/.ssh/id_*.pub`.
- `FLEET_CLAWD_SRC` — path to a Clawd-Codex checkout to seed.
- `FLEET_VM_IMAGE` — base image for fresh builds (default `ubuntu:24.04`).
- `FLEET_CONSOLE_STATE` — the console's state dir (default
  `~/.config/hugpy-station`).
- `STATION_CONSOLE_MODEL_VM` — site pin: fallback grounding VM when no
  station is active.

### Files that matter

- `~/.config/hugpy-station/` — `.auth` (login password, hashed),
  `fleet_ssh_key(.pub)`, `keeper-mail.jsonl` (the keeper's ✉ inbox),
  `browser-access.json`, `frontier-keeper.json`, `a-settings-template.json`.
- In each station: `~/.bridge-mail.jsonl` (inbox), `~/.keeper-mail-out.jsonl`
  (outbox), `~/.mct/` (B's session workspaces), `~/todo.json` (the board).

### Troubleshooting

- **A surface opens a bare shell instead of its agent** — that seat is
  missing in the station (the fallback drops you to a shell *in the same
  VM*, never the host). Check the build log's seat summary; re-seed by hand
  or rebuild.
- **"no live keeper" when mailing a station** — its A isn't running; open
  the station's keeper tab once (or the mail simply waits in the inbox).
- **certbot failed** — the domain's A record isn't pointing here yet, or
  port 80 is blocked. Fix DNS, re-run the same command.
- **A station's ssh prompts for a passphrase** — that's your personal key;
  the in-app ssh transport uses the passphraseless fleet key and never
  prompts. For prompt-free logins from other machines, carry the fleet key
  or seed a dedicated one via `FLEET_SSH_PUBKEY`.
