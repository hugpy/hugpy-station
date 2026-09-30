# @hugpy/station — hugpy Station, the desktop cockpit (source)

The true source of the hugpy-station packages (formerly fleet-console;
Provides/Replaces/Conflicts handle upgrades), reconstructed into its proper home
2026-08-13 (it previously lived only as build roots on a dev drive; the
app.asar packs plain, unbundled files, so this IS the complete source).

## Layout → package mapping

| Here | Installed as |
|---|---|
| `main.js` + `package.json` | `<root>/resources/app.asar` (repacked by electron-builder on EVERY build) |
| `resources/backend/` | `<root>/resources/backend/` (server.py + console-api + bugreport-api sidecars + static UI) |
| `resources/bin/` | `<root>/resources/bin/` (hugpy-station-domain, vm-new, station-fix.sh) |
| `resources/systemd/` | `<root>/resources/systemd/` — after-install links `hugpy-station-web@.service` into `/etc/systemd/system` |
| `resources/hugpy-station-launch` | `<root>/hugpy-station-launch` (755) — the `/usr/bin/hugpy-station` alternative; source since 1.0.42. Also shipped as `<root>/AppRun`, the AppImage entry point |
| `resources/VERSION` | `<root>/resources/VERSION` (read by `hugpy-station --version`) |
| `build/icons/*.png` | `/usr/share/icons/hicolor/<size>/apps/hugpy-station.png` |
| `build/linux-after-install.tpl` | the deb postinst / rpm `%post` / pacman `post_install` — one script, three formats |
| `electron-builder.yml` | the whole package definition (deps, desktop entry, targets) |

`<root>` is `/opt/hugpy-station` for deb/rpm/pacman and `$APPDIR` inside the
AppImage; the launcher derives it from its own location rather than hardcoding.

The Electron runtime (the `<root>/hugpy-station` binary, locales, .pak files) is
**Electron 30.5.1**, downloaded by electron-builder — it is no longer copied out
of a previous build root. Confirm what a built or installed copy carries with:

```bash
strings -a /opt/hugpy-station/hugpy-station | grep -m1 -o 'Chrome/[0-9.]* Electron/[0-9.]*'
```

## Building a release

```bash
cd station-app
./build-release.sh                # deb + rpm + pacman + AppImage, verified, staged
npm run dist                      # the same command
./build-release.sh --no-stage     # …without copying to the keeper deploy dir
./build-release.sh --targets deb  # one format
```

To cut a new version, bump **`package.json` and `resources/VERSION` together**
(the script refuses to build if they disagree) and re-run. Artifacts land in
`station-app/dist/` and are staged to `/mnt/llm_storage/_keeper_deploy/console/`
with `sha256` sidecars; the console API serves the newest by version sort.

| Format | Artifact | Install |
|---|---|---|
| deb | `hugpy-station_X.Y.Z_amd64.deb` | `apt install ./…deb` |
| rpm | `hugpy-station-X.Y.Z.x86_64.rpm` | `dnf install ./…rpm` |
| pacman | `hugpy-station-X.Y.Z-x86_64.pacman` | rename to `.pkg.tar.xz`, then `pacman -U` (the installer does this for you) |
| AppImage | `hugpy-station-X.Y.Z-x86_64.AppImage` | `chmod +x`, run — no root, no FUSE |

Build-host prerequisites: **node + npm**, plus two things electron-builder's
bundled fpm shells out to and does not carry: **`rpmbuild`** for the rpm target
(`apt-get install rpm`) and **`bsdtar`** for the pacman target's `.MTREE`
(`apt-get install libarchive-tools`). The script checks both up front.
Everything else (Electron, fpm, the AppImage runtime) is downloaded on
first build and cached under `~/.cache/electron-builder`. With no network, point
`HUGPY_STATION_ELECTRON_DIST` at a prebuilt Electron dist; the script also falls
back to assembling one from `assets/fleet-console/build-1.0.41` automatically.

### What the script verifies before it will stage anything

Every check runs against the **finished artifact**, in that format's own terms,
and any failure aborts before staging:

1. **SANITIZE** — the full path list of each artifact must contain no
   `.secret`, `.auth`, `__pycache__` or `*.pyc`. server.py *generates*
   `resources/backend/.secret` at first run; a backend ever run inside the
   source tree plants a cookie-signing key that must NEVER ship (2026-08-13
   incident: every deb ≥1.0.12 shared one forgeable key). Listing is per format:
   `dpkg-deb --fsys-tarfile | tar -t` (deb), the RPM header's BASENAMES/DIRNAMES
   tags via `build/rpm-inspect.py` (rpm — no `rpm` or `bsdtar` needed on the
   build host), `tar -t` with a compression sniff (pacman), and
   `--appimage-offset` + `unsquashfs -l` (AppImage).
2. **asar version** — `resources/app.asar` is pulled back *out of each artifact*
   and its packed `package.json` version must equal this package.json's.
   1.0.39–1.0.41 shipped a stale 1.0.38 asar (the app said 1.0.38 while dpkg
   said 1.0.41) because the asar was copied from the previous build root.
3. **layout** — launcher, `resources/VERSION`, `chrome-sandbox`, `server.py` and
   `app.asar` present; the install scriptlet exists in deb/rpm/pacman *and*
   references the launcher (the 1.0.41 rpm was an `alien` conversion that
   dropped the postinst entirely); the declared aiohttp dependency is present.
4. **AppRun is our launcher** — inside the AppImage, `AppRun` must carry the
   python probe, the no-display branch and `--no-sandbox`, not electron-builder's
   stock stub.

### Known gap: pacman upgrades

fpm emits only `post_install` and `post_remove` into the pacman `.INSTALL`, so
`pacman -U` **over an existing install runs no scriptlet at all** (pacman calls
`post_upgrade`, which is not there). A pacman upgrade therefore restores the
package's own `.desktop` (whose `Exec` points at the raw Electron binary) and
drops the SUID bit on `chrome-sandbox`. Both are exactly what
`/api/agent/console/station-fix.sh` repairs, so on Arch: re-run the fix script
after an upgrade, or remove and reinstall. deb and rpm upgrades are unaffected —
their post-install scriptlets run on upgrade.

### Legacy flow (pre-2026-08-21, kept for reading old build roots)

<details><summary>The manual build-root flow that produced ≤1.0.42's first deb</summary>

1. Copy the previous `build-X.Y.(Z-1)` root from the project share
   (`assets/fleet-console/build-1.0.41`).
2. Sync this package's files into it; pack `app.asar` with `npx @electron/asar pack`.
3. Bump `debian/control` Version, package.json and `resources/VERSION` together.
4. Run `assets/fleet-console/SANITIZE.sh <build-root>`.
5. `dpkg-deb --root-owner-group -b <build-root> hugpy-station_X.Y.Z_amd64.deb`
6. Verify the asar was actually repacked, then `dpkg-deb -c | grep -E '\.secret|__pycache__'`.

`station-app/debian/` (control/postinst/postrm) is **deprecated** and kept only
as the reference the after-install template was derived from. It is not used by
any build. The rpm in this era was a deb conversion made elsewhere, which is how
the postinst got lost.
</details>

## Installing

One line, any distro — detects `apt`/`dnf`/`zypper`/`pacman` and falls back to
the AppImage:

```bash
curl -fsSL https://dev.hugpy.ai/api/agent/console/install.sh | HUGPY_TOKEN=<token> bash
```

Source: `abstract_hugpy_dev/.../routes/installer_assets/console-install.sh`,
served by the public route `GET /api/agent/console/install.sh` (no embedded
secret — the credential is read from the caller's environment at run time; the
artifacts themselves stay member-gated). To repair an already-installed copy,
see `STATION-FIX-README.md` and `/api/agent/console/station-fix.sh`.

### ONE install layout (1.0.90) — "install the .deb" is the whole story

| what | where | who writes it |
|---|---|---|
| payload | `/opt/hugpy-station` (the .deb) | the package |
| state (`HUGPY_STATION_STATE`) | `<home>/hugpy-station` — the ONE state dir; `~/.config/hugpy-station` becomes a symlink to it | `hugpy-station-firstrun` |
| instance env | `/etc/hugpy-station/<user>.env` (root 0600): `PORT` (first free from 8898, chosen once), `STATION_LOCUS`, `HUGPY_STATION_STATE`, `STATION_CONSOLE_TOKEN`, `HUGPY_URL`, `HUGPY_API_KEY`, `HUGPY_OPERATOR_TOKEN` | `hugpy-station-locus` + `hugpy-station-firstrun` (after-install) |
| headless unit | `hugpy-station-web@<user>` → `bash -l /opt/hugpy-station/resources/bin/hugpy-station-web-run` (pins state, `AC_ROOT`, re-runs first-run, exec's server.py) | the package (enabled for the installing user) |
| seat CLIs | `~/.local/bin/mct-pull`, `mct-push` (+ `/usr/local/bin/`) → the package | first-run |
| defaults seeded once | `frontier-models.json` (working model), `local-keeper/AGENTS.md` (+ `QWEN.md`, `docs/`), `abstract-claude/config.json` (`fresh_session_mode=dir`), `b-model.json`, `~/.config/hugpy-agent/agent.env`, `<state>/toolserver.env` | first-run |

The after-install runs first-run for EVERY station user on the host (the
installing user + every existing `/etc/hugpy-station/<user>.env`) and
`try-restart`s the instances, so `/opt` can never lag what a unit runs.
Installer knobs (env of the `sudo -E apt install ./hugpy-station_*.deb`):
`HUGPY_API_KEY`, `HUGPY_URL`, `HUGPY_OPERATOR_TOKEN`, `HUGPY_STATION_NO_HEADLESS=1`.
Re-runnable any time: `sudo hugpy-station-firstrun <user>`.

**User-unit derivative (a "sovereign" station run by the account's own systemd
user manager):** the SAME .deb, unpacked without root —
`hugpy-station-user-install <deb>` → `<state>/app/<version>/` + `app/current`,
`~/.config/systemd/user/hugpy-station-web.service` from
`resources/systemd/hugpy-station-web.user.service`, env in `<state>/env/`, the
same first-run. Rollback: `hugpy-station-user-install --rollback <version>`.

Post-install checks: `ls -l /usr/bin/hugpy-station` must resolve to
`/opt/hugpy-station/hugpy-station-launch` and `chrome-sandbox` must be
`-rwsr-xr-x root`. `hugpy-station` needs a graphical session — over plain ssh it
exits 2 with a message (1.0.41 and earlier segfaulted). The backend interpreter
is probed for `aiohttp`; a conda `python3` on PATH is skipped if it lacks it.

Licensed under the hugpy Source-Available License (see LICENSE).
