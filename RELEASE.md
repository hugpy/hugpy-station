# Releasing hugpy Station — where to edit, how to cut a version

**One rule: edit SOURCE, never a deployed install.** Changes reach users only by
building a new `.deb` from SOURCE and promoting it — never by hand-editing a live
tree (that is an emergency hot-patch, see bottom).

## The pipeline (vocabulary)

```
SOURCE  ─build-release.sh─▶  BUILD/dist  ─stage/promote─▶  ARCHIVE  ─apt/install─▶  LIVE
```

| Stage | Location | Notes |
|-------|----------|-------|
| **SOURCE** (edit here) | `solcatcher@ae:/srv/hugpy/src/station-app/` | canonical. `resources/backend/server.py` (backend), `resources/backend/static/fleetview-term.js` (frontend UI). Owned `vm_mgr` — edit via `sudo`. |
| **BUILD** | `…/station-app/dist/` | `build-release.sh` output: `hugpy-station_<version>_amd64.deb` (+ rpm/pacman/AppImage). **This is the "developing .deb".** |
| **ARCHIVE / shelf** | `/srv/hugpy/www/station/edit/` → promoted to `…/pub/` | `hugpy-station_latest.deb` symlink points at the promoted version. |
| **LIVE @client** | `/opt/hugpy-station/` (each client host) | deployed install. Do not edit. |
| **LIVE @server** | `/srv/vm_mgr/hugpy-station/app/current/` (ae) | deployed install (vm_mgr). Do not edit. |

## The version lives in TWO files — keep them in sync

`build-release.sh` **does not bump** and **aborts** if they disagree:

- `package.json` → `"version"`
- `resources/VERSION`

## Cut the next version (one command)

Run **as `vm_mgr`** on `ae` (owns `dist/`, has the build PATH, and its `$HOME` is
on `/srv` which has space — do NOT build as a user whose `$HOME` is on the
near-full `/` root):

```bash
sudo -u vm_mgr -i bash -lc 'cd /srv/hugpy/src/station-app && ./cut-next-version.sh'
```

`cut-next-version.sh` bumps the patch version in BOTH files, adds a CHANGELOG
stub for you to fill, then runs `./build-release.sh --no-stage --targets deb`.
The developing deb lands at `./dist/hugpy-station_<version>_amd64.deb`.

Manual equivalent, if you prefer: edit both version files by hand, add a
CHANGELOG entry, then `./build-release.sh --no-stage --targets deb`.

`build-release.sh` (no flags) also **stages** to the shelf's `edit/`; promote to
users with `release.sh promote` on the shelf. `--no-stage` builds+verifies only.
The build self-checks: secret-sanitize, asar-version == package.json version, and
layout — any failure aborts before staging.

## Emergency hot-patch (NOT a release)

`~solcatcher/apply-live-patch.sh` patches the two LIVE `server.py` copies in place
via `patch-hugpy-station.py`. Use only to unstick a running station; it does not
change SOURCE, so the fix is lost on the next install unless you also apply it to
SOURCE and cut a version.

## The drift gate (2026-09-19) — why a build can now REFUSE to run

`build-release.sh` runs `resources/bin/station-drift-check.sh` before it does
anything else (section `0b`) and **aborts** if the running system contains
anything this source tree does not. That is the mechanical version of the rule
at the top of this file; the rule alone never held.

The check is read-only and safe to run any time:

```bash
/srv/vm_mgr/bin/station-drift-check.sh        # -> resources/bin/station-drift-check.sh
```

It fails on: backend installed≠source; `abstract_claude` installed≠the staged
wheel; the staged wheel≠/srv/pyit/dev/abstract_claude; a live baked console≠what
`station-bake-webui` would produce; an open bake hatch; dirty or unpushed
`/srv/pyit/dev/abstract_claude` / `station-app` (warn only — git state is informational); and any outstanding entry in
`resources/unshipped-artifacts.tsv` — the inventory of live artifacts that no
shipping repo owns. Genuine runtime state (sessions, `console.sqlite3`,
`roster.json`, `env/`) is excluded by design and recorded as such.

Override, deliberately and loudly: `FORCE_DRIFT=1 ./build-release.sh …`.

### The console UI is DERIVED — never edit a deployed copy

`abstract_claude`'s console ships built for `base=/claude`; the station renders
it at `/ac`. `resources/bin/station-bake-webui` regenerates that `/ac` dist from
the INSTALLED package on **every serve start**, so a hand edit to a baked tree
does not survive a restart. The only place the console UI may be edited is
`/srv/pyit/dev/abstract_claude/src/abstract_claude/webui`, and it flows
`/srv/pyit/dev/abstract_claude -> PyPI (published version) -> resources/REQUIREMENTS.txt pin -> .deb -> pip install from PyPI into the venv -> baked dist`.
(1.0.112: bundled wheels are deprecated — the pin installs from PyPI.)

Emergency hotfix on a running console: `AC_UI_NO_BAKE=1`, or
`touch <dist>/.no-bake`. Both keep the live edit alive and both FAIL the drift
gate, so you can hotfix freely but cannot ship with one outstanding.
Every rebake archives the previous tree to
`<parent>/patches/webui-station.pre-bake-<timestamp>/` first — nothing is deleted.

## Ordered checklist for cutting the next version

1. `/srv/vm_mgr/bin/station-drift-check.sh` — read the table. Every `DRIFT` row
   is live work the next `.deb` would destroy. Do NOT proceed past a red row.
2. Land console-UI work in `/srv/pyit/dev/abstract_claude` (`src/abstract_claude/webui`), bump the `abstract_claude` version, and PUBLISH it to PyPI.
3. Update the `abstract-claude==` pin in `resources/REQUIREMENTS.txt` to that
   published version (1.0.112: no bundled wheels — the pin installs from PyPI;
   `build-release.sh` fails the build if any pin does not resolve on PyPI).
4. Let `resources/bin/station-provision-venv` upgrade the serve venvs from PyPI on
   the next serve start (or run it explicitly against each venv).
5. Remove `/srv/vm_mgr/abstract-claude-serve/webui-station/.no-bake` (and any
   other hatch the check reports), then restart serve so the dist re-derives.
6. Land backend work in `resources/backend/`, commit and push `dev`.
7. Clear every `SHIP-ME` row in `resources/unshipped-artifacts.tsv` — bring the
   file into this repo, or get an explicit operator decision and re-classify it.
8. Bump the version in BOTH `package.json` and `resources/VERSION`, add a
   CHANGELOG entry. (`./cut-next-version.sh` does 8 and 9 together.)
9. `./build-release.sh --no-stage --targets deb` — the gate runs first and must
   pass. Artifact lands in `dist/`.
10. `./ship-version.sh` — stages into the shelf's `edit/` and promotes to `pub/`.
11. Install on this box so `/srv/vm_mgr/hugpy-station/app/current` advances, and
    re-run the drift check: it must now be all green.
