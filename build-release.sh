#!/usr/bin/env bash
# hugpy Station — ONE command, four Linux artifacts, verified, then staged.
#
#   ./build-release.sh                 build + verify + stage
#   ./build-release.sh --no-stage      build + verify only
#   ./build-release.sh --targets deb   just one target (deb|rpm|pacman|AppImage)
#
# Replaces the manual build-root flow in README.md ("Building a release",
# now LEGACY). What it does, in order:
#
#   1. copy the source into a scratch workspace, EXCLUDING .secret/.auth/
#      __pycache__/*.pyc so the sanitizer's job is already done before packing
#      (electron-builder.yml filters them too — belt and braces, because the
#      2026-08-13 incident shipped one forgeable cookie key to every install);
#   2. electron-builder -> deb + rpm + pacman + AppImage into ./dist;
#   3. SANITIZE check   — list every path in every artifact, in its own native
#      format, and fail on .secret / .auth / __pycache__ / *.pyc;
#   4. asar version check — pull resources/app.asar back OUT of each artifact
#      and assert its packed package.json version == this package.json version.
#      1.0.39-1.0.41 shipped a stale 1.0.38 asar; this is the check that makes
#      that impossible to repeat;
#   5. layout check     — launcher, VERSION, chrome-sandbox, backend present;
#      install scriptlets present in deb/rpm/pacman; AppRun is OUR launcher;
#   6. sha256 sidecars, then stage into the shelf's edit/ (/srv/hugpy/www/station/edit);
#      serving it is `release.sh promote` on the shelf; dist/ keeps one symlink.
#
# ANY check failure aborts before staging. Re-running is safe and idempotent:
# artifacts are rebuilt in place and staging overwrites only same-named files.
#
# Env:
#   HUGPY_STATION_BUILD_DIR      scratch workspace   (default ~/.cache/hugpy-station-build)
#   HUGPY_STATION_ELECTRON_DIST  prebuilt Electron dist dir; forces the offline path
#   HUGPY_STATION_STAGE_DIR      staging dir         (default /mnt/llm_storage/_keeper_deploy/console)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$SRC/dist"
WORK="${HUGPY_STATION_BUILD_DIR:-$HOME/.cache/hugpy-station-build}"
APPDIR_SRC="$WORK/app"
STAGE_DIR="${HUGPY_STATION_STAGE_DIR:-/srv/hugpy/www/station/edit}"   # the shelf's edit/ (2026-09-02): promote with release.sh
ELECTRON_BUILDER_SPEC="electron-builder@26"

TARGETS="deb rpm pacman AppImage"
DO_STAGE=1
while [ $# -gt 0 ]; do
    case "$1" in
        --no-stage) DO_STAGE=0; shift ;;
        --targets)  TARGETS="$2"; shift 2 ;;
        -h|--help)  sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

FAILURES=0
bad() { printf '   \033[31mBAD \033[0m %s\n' "$*" >&2; FAILURES=$((FAILURES + 1)); }
ok()  { printf '   \033[32mok  \033[0m %s\n' "$*"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── 0. version ───────────────────────────────────────────────────────────────
command -v node >/dev/null || fail "node is required"
command -v npm  >/dev/null || fail "npm is required"
# electron-builder downloads fpm, but fpm shells out to the host for two things
# it does not bundle: rpmbuild (rpm) and bsdtar (the pacman .MTREE). Say so up
# front instead of dying several GB into a build.
if [[ " $TARGETS " == *" rpm "* ]] && ! command -v rpmbuild >/dev/null; then
    fail "the rpm target needs rpmbuild — apt-get install rpm"
fi
if [[ " $TARGETS " == *" pacman "* ]] && ! command -v bsdtar >/dev/null; then
    fail "the pacman target needs bsdtar — apt-get install libarchive-tools"
fi
VERSION="$(node -p "require('$SRC/package.json').version")"
VERSION_FILE="$(tr -d '[:space:]' < "$SRC/resources/VERSION")"
[ "$VERSION" = "$VERSION_FILE" ] \
    || fail "package.json ($VERSION) and resources/VERSION ($VERSION_FILE) disagree"
say "hugpy Station $VERSION -> $TARGETS"

# ── 0b. DRIFT GATE ───────────────────────────────────────────────────────────
# Refuse to build while the running system contains anything the artifact would
# not. This is the mechanical half of a rule that convention never held: a live
# fix that exists only in a deployed tree is a fix that the next install deletes.
# station-drift-check.sh compares installed-vs-source for the backend, the
# abstract_claude package, the staged wheel and the baked consoles, checks for
# dirty/unpushed source of record, and fails on any unshipped authored artifact
# in resources/unshipped-artifacts.tsv.
#
# FORCE_DRIFT=1 overrides it. That is the emergency valve, and it is loud and
# recorded in the build log on purpose — it is not the normal path.
DRIFT_CHECK="$SRC/resources/bin/station-drift-check.sh"
if [ -x "$DRIFT_CHECK" ]; then
    say "drift gate — is the running system what this build will ship?"
    if "$DRIFT_CHECK"; then
        :
    elif [ "${FORCE_DRIFT:-}" = "1" ]; then
        printf '\n\033[33m!! FORCE_DRIFT=1 — building over the drift listed above.\n'
        printf '   Everything marked DRIFT is live work that this .deb does NOT contain\n'
        printf '   and that installing it will overwrite. %s by %s.\033[0m\n\n' "$(date -Is)" "${SUDO_USER:-$USER}"
    else
        fail "drift gate: the live system is ahead of this source tree (see the table above).
   Reconcile each DRIFT row, then re-run. To override deliberately:  FORCE_DRIFT=1 $0 $*"
    fi
else
    fail "drift gate missing: $DRIFT_CHECK is not executable — it must run before every build"
fi

# ── 0c. PyPI PIN GATE ────────────────────────────────────────────────────────
# 1.0.112: bundled wheels are deprecated — resources/REQUIREMENTS.txt installs its
# pins from PyPI. The old build gate ("every pin has a matching bundled wheel") is
# replaced by THIS one: every Tier-1 pin must resolve on PyPI at its exact version,
# or the build fails. Otherwise a fresh box would get nothing (or a lagging PyPI
# floor) at provision time. Tier-2 '# @service' lines are parsed by firstrun, not
# pip, and are skipped here.
REQ_FILE="$SRC/resources/REQUIREMENTS.txt"
[ -f "$REQ_FILE" ] || fail "PyPI pin gate: $REQ_FILE is missing — it is the single pin source"
command -v python3 >/dev/null || fail "PyPI pin gate needs python3 (for 'pip download')"
say "PyPI pin gate — every pin in resources/REQUIREMENTS.txt resolves on PyPI?"
PINGATE_TMP="$TMP/pypi-gate"; mkdir -p "$PINGATE_TMP"
pin_fail=0
while IFS= read -r pin; do
    [ -n "$pin" ] || continue
    if python3 -m pip download --no-deps --dest "$PINGATE_TMP" "$pin" >/dev/null 2>&1; then
        ok "PyPI has $pin"
    else
        printf '   \033[31mBAD \033[0m PyPI cannot resolve pin: %s\n' "$pin" >&2
        pin_fail=$((pin_fail + 1))
    fi
done < <(grep -vE '^\s*#|^\s*$' "$REQ_FILE")
[ "$pin_fail" = 0 ] || fail "PyPI pin gate: $pin_fail pin(s) do not resolve on PyPI (see above) — publish them, then rebuild"

# ── 1. scratch workspace ─────────────────────────────────────────────────────
# rsync --delete keeps the workspace a faithful mirror of source (a file deleted
# here must not survive into the next package), minus node_modules and minus
# everything the sanitizer would otherwise have to catch later.
say "syncing source -> $APPDIR_SRC"
mkdir -p "$APPDIR_SRC"
rsync -a --delete --delete-excluded \
    --exclude '.secret' --exclude '.auth' \
    --exclude '__pycache__/' --exclude '*.pyc' \
    --exclude '*.bak-*' --exclude '*.bak' --exclude '*.orig' --exclude '*.rej' \
    "$SRC/resources/" "$APPDIR_SRC/resources/"
rsync -a --delete "$SRC/build/" "$APPDIR_SRC/build/"
cp -f "$SRC/main.js" "$SRC/preload.js" "$SRC/package.json" "$SRC/electron-builder.yml" \
      "$SRC/LICENSE" "$APPDIR_SRC/"
# observability/ is required by main.js (toggle.js at startup) — without it the
# packaged app would crash on launch. test/ never enters the workspace.
rsync -a --delete --delete-excluded --exclude 'test/' \
    "$SRC/observability/" "$APPDIR_SRC/observability/"
info "$(find "$APPDIR_SRC" -type f | wc -l) files"

# ── 2. toolchain ─────────────────────────────────────────────────────────────
if [ ! -x "$APPDIR_SRC/node_modules/.bin/electron-builder" ]; then
    say "installing $ELECTRON_BUILDER_SPEC into the workspace"
    ( cd "$APPDIR_SRC" && npm install --no-save --no-audit --no-fund "$ELECTRON_BUILDER_SPEC" )
fi
info "electron-builder $("$APPDIR_SRC/node_modules/.bin/electron-builder" --version 2>/dev/null | tail -1)"

# ── 2b. Electron runtime ─────────────────────────────────────────────────────
# Normal path: electron-builder downloads the electronVersion pinned in
# electron-builder.yml. Offline path: assemble a dist dir out of the Electron
# runtime we already ship — the canonical build root, or this box's live
# install — and hand it over as electronDist. Both give Electron 30.5.1; the
# offline one is byte-identical to what 1.0.41 shipped.
EB_EXTRA=()
ELECTRON_DIST="${HUGPY_STATION_ELECTRON_DIST:-}"
if [ -z "$ELECTRON_DIST" ] && ! curl -fsI -m 10 https://github.com >/dev/null 2>&1; then
    info "github.com unreachable — assembling a local Electron dist"
    for cand in /srv/share/projects/hugpy/assets/fleet-console/build-1.0.41/opt/hugpy-station \
                /opt/hugpy-station; do
        if [ -x "$cand/hugpy-station" ]; then ELECTRON_SRC="$cand"; break; fi
    done
    [ -n "${ELECTRON_SRC:-}" ] || fail "offline and no prebuilt Electron runtime found"
    ELECTRON_DIST="$WORK/electron-dist"
    rm -rf "$ELECTRON_DIST"
    # An electron dist is the zip's contents: the runtime at the top level with
    # the executable named `electron`, plus an EMPTY resources/ (electron-builder
    # writes app.asar into it). Skipping the source resources/ is the point —
    # it carries the PREVIOUS build's app.asar and backend, and copying those is
    # precisely how 1.0.41 came to ship a 1.0.38 asar. The launcher and AppRun
    # are skipped too; extraFiles re-adds them from source.
    mkdir -p "$ELECTRON_DIST/resources"
    ( cd "$ELECTRON_SRC" && find . -mindepth 1 -maxdepth 1 \
        ! -name resources ! -name hugpy-station ! -name hugpy-station-launch \
        ! -name AppRun -exec cp -a {} "$ELECTRON_DIST/" \; )
    cp -a "$ELECTRON_SRC/hugpy-station" "$ELECTRON_DIST/electron"
fi
if [ -n "$ELECTRON_DIST" ]; then
    info "electronDist: $ELECTRON_DIST"
    EB_EXTRA+=("-c.electronDist=$ELECTRON_DIST")
fi

# ── 3. build ─────────────────────────────────────────────────────────────────
say "electron-builder"
mkdir -p "$DIST"
EB_TARGETS=()
for t in $TARGETS; do EB_TARGETS+=("$t"); done
( cd "$APPDIR_SRC" && ./node_modules/.bin/electron-builder \
    --linux "${EB_TARGETS[@]}" --x64 \
    "-c.directories.output=$DIST" \
    "${EB_EXTRA[@]}" )

# ── verification helpers ─────────────────────────────────────────────────────
SANITIZE_RE='(^|/)\.secret$|(^|/)\.auth$|(^|/)__pycache__(/|$)|\.pyc$|\.bak(-|\.|$)|resources/backend/test_[^/]*\.py$'

# GNU tar auto-detects gzip/xz/zstd from a real file; the explicit fallbacks are
# for the pacman package, whose compression is fpm's choice, not ours.
tar_list() {
    local f="$1"
    tar -tf "$f" 2>/dev/null && return 0
    case "$(od -An -tx1 -N4 "$f" | tr -d ' \n')" in
        28b52ffd*) zstd -dc "$f" | tar -tf - ;;
        fd377a58*) xz   -dc "$f" | tar -tf - ;;
        1f8b*)     gzip -dc "$f" | tar -tf - ;;
        *) return 1 ;;
    esac
}
tar_extract() {   # tar_extract <archive> <member> -> stdout
    # --occurrence=1 stops the scan at the first hit; without it tar decompresses
    # the whole 75 MB pacman package three times over during verification.
    local f="$1" m="$2"
    tar --occurrence=1 -xOf "$f" "$m" 2>/dev/null && return 0
    case "$(od -An -tx1 -N4 "$f" | tr -d ' \n')" in
        28b52ffd*) zstd -dc "$f" | tar --occurrence=1 -xOf - "$m" ;;
        fd377a58*) xz   -dc "$f" | tar --occurrence=1 -xOf - "$m" ;;
        1f8b*)     gzip -dc "$f" | tar --occurrence=1 -xOf - "$m" ;;
        *) return 1 ;;
    esac
}
appimage_offset() {
    local off
    # head -1 + a length sanity gate: the 20251108 static runtime does NOT
    # answer --appimage-offset — it mounts and runs AppRun, whose preflight
    # output tr used to concatenate into a garbage mega-number (2026-08-27).
    off="$("$1" --appimage-offset 2>/dev/null | head -1 | tr -dc '0-9')"
    if [ -n "$off" ] && [ "${#off}" -le 10 ]; then echo "$off"; return 0; fi
    # Compute it from the runtime's ELF header: the squashfs payload starts
    # where the section headers end. Verified against the superblock magic;
    # a bare "first hsqs" scan is WRONG — the runtime stub contains one
    # (false hit at 194183 vs true 944632, measured 2026-08-27).
    python3 -c 'import struct, sys
b = open(sys.argv[1], "rb").read()
off = struct.unpack_from("<Q", b, 0x28)[0] +       struct.unpack_from("<H", b, 0x3a)[0] * struct.unpack_from("<H", b, 0x3c)[0]
if b[off:off+4] == b"hsqs":
    print(off)
else:
    hits, i = [], 0
    while True:
        i = b.find(b"hsqs", i)
        if i < 0: break
        hits.append(i); i += 1
    print(hits[-1] if hits else "")' "$1"
}

asar_version() {  # asar_version <file> — the packed package.json's version
    grep -ao '"version": *"[0-9][0-9A-Za-z.+-]*"' "$1" | head -1 \
        | sed -E 's/.*"([^"]+)"$/\1/'
}

check_listing() { # check_listing <label> <listing-file>
    local label="$1" lst="$2" hits
    hits="$(grep -E "$SANITIZE_RE" "$lst" || true)"
    if [ -n "$hits" ]; then
        bad "$label SANITIZE: $(echo "$hits" | tr '\n' ' ')"
    else
        ok "$label SANITIZE clean ($(wc -l < "$lst") paths)"
    fi
    for want in hugpy-station-launch resources/VERSION chrome-sandbox \
                resources/backend/server.py resources/app.asar \
                resources/backend/mct_gateway.py resources/backend/mct_http.py \
                resources/backend/static/mct-renderer.js \
                resources/backend/prompt_send.py resources/backend/static/prompt-tools.js \
                resources/backend/static/board-md.js \
                resources/backend/frontier-models.default.json \
                resources/backend/abstract-claude.default.json \
                resources/backend/local-keeper/AGENTS.md \
                resources/backend/directives.py resources/backend/directive-templates/core.md \
                resources/backend/todo_digest.py resources/backend/locus_exec.py \
                resources/bin/station-serve-provision resources/systemd/abstract-claude-serve@.service \
                resources/bin/mct-pull resources/bin/mct-push \
                resources/bin/hugpy-station-firstrun resources/bin/hugpy-station-web-run \
                resources/bin/abstract-claude-console resources/abstract-claude-console.desktop \
                resources/bin/hugpy-station-user-install \
                resources/bin/station-provision-venv \
                resources/systemd/hugpy-station-web@.service \
                resources/systemd/hugpy-station-web.user.service \
                resources/REQUIREMENTS.txt; do
        grep -q "$want" "$lst" || bad "$label missing $want"
    done
    # 1.0.112: bundled wheels are DEPRECATED — the pins install from PyPI. The
    # "every pin has a matching bundled wheel" gate is gone; PyPI-resolvability of
    # every pin is enforced at build time by the PyPI pin gate (section 0c), and
    # nothing under resources/wheels/ should ship any more.
    if grep -qE 'resources/wheels/' "$lst"; then
        bad "$label ships a resources/wheels/ artifact — bundled wheels were retired in 1.0.112"
    fi
}

check_asar() {    # check_asar <label> <extracted-app.asar>
    local label="$1" asar="$2" got
    [ -s "$asar" ] || { bad "$label: could not extract resources/app.asar"; return; }
    got="$(asar_version "$asar")"
    if [ "$got" = "$VERSION" ]; then
        ok "$label app.asar version $got"
    else
        bad "$label app.asar version '$got' != package.json '$VERSION'"
    fi
    # main.js requires ./observability/toggle at startup: an asar without it
    # cannot launch. The asar header is compact JSON, so the dir entry is greppable.
    if grep -aq '"observability":{"files":{' "$asar" && grep -aq '"toggle.js":{' "$asar"; then
        ok "$label app.asar ships observability/"
    else
        bad "$label app.asar is missing observability/ (main.js would crash on launch)"
    fi
}

# ── 4. verify each artifact ──────────────────────────────────────────────────
DEB="$DIST/hugpy-station_${VERSION}_amd64.deb"
RPM="$DIST/hugpy-station-${VERSION}.x86_64.rpm"
PACMAN="$DIST/hugpy-station-${VERSION}-x86_64.pacman"
APPIMAGE="$DIST/hugpy-station-${VERSION}-x86_64.AppImage"

ARTIFACTS=()

for t in $TARGETS; do
case "$t" in
deb)
    say "verify deb"
    [ -f "$DEB" ] || fail "expected $DEB"
    dpkg-deb --fsys-tarfile "$DEB" | tar -tf - > "$TMP/deb.list"
    check_listing deb "$TMP/deb.list"
    dpkg-deb --fsys-tarfile "$DEB" \
        | tar -xO ./opt/hugpy-station/resources/app.asar > "$TMP/deb.asar" || true
    check_asar deb "$TMP/deb.asar"
    dpkg-deb -I "$DEB" > "$TMP/deb.control"
    dpkg-deb -I "$DEB" postinst > "$TMP/deb.postinst" 2>/dev/null || true
    grep -q 'hugpy-station-launch' "$TMP/deb.postinst" \
        && ok "deb postinst wires the launcher" || bad "deb postinst missing/!launcher"
    grep -q 'fix_seat_settings' "$TMP/deb.postinst" \
        && ok "deb postinst migrates seat settings" || bad "deb postinst missing fix_seat_settings"
    grep -q 'provision_seats' "$TMP/deb.postinst" \
        && ok "deb postinst provisions seat CLIs" || bad "deb postinst missing provision_seats"
    # 1.0.90: one-shot first-run — every station user provisioned, CLIs on PATH,
    # instances restarted onto this payload.
    grep -q 'hugpy-station-firstrun' "$TMP/deb.postinst" \
        && ok "deb postinst runs hugpy-station-firstrun" || bad "deb postinst missing hugpy-station-firstrun"
    grep -q 'try-restart' "$TMP/deb.postinst" \
        && ok "deb postinst restarts instances onto this payload" || bad "deb postinst missing try-restart"
    dpkg-deb --fsys-tarfile "$DEB" \
        | tar -xO ./opt/hugpy-station/resources/systemd/hugpy-station-web@.service > "$TMP/deb.unit" 2>/dev/null || true
    grep -q 'hugpy-station-web-run' "$TMP/deb.unit" \
        && ok "deb unit runs the layout-agnostic wrapper (explicit HUGPY_STATION_STATE)" \
        || bad "deb unit does not use hugpy-station-web-run"
    dpkg-deb --fsys-tarfile "$DEB" \
        | tar -xO ./opt/hugpy-station/resources/backend/server.py > "$TMP/deb.server.py" 2>/dev/null || true
    python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$TMP/deb.server.py" 2>/dev/null \
        && ok "deb server.py parses" || bad "deb server.py does not parse"
    grep -q 'from mct_http import' "$TMP/deb.server.py" \
        && ok "deb server.py wires mct_http (durable MCT ledger)" || bad "deb server.py lacks the mct_http wiring"
    # Since 1.0.47 the deb must be single-file offline-installable: aiohttp is
    # vendored (resources/backend/vendor) and lxd is only Recommended, so both
    # must be OUT of Depends and IN Recommends.
    grep '^ Depends:' "$TMP/deb.control" | grep -q 'python3-aiohttp' \
        && bad "deb Depends still lists python3-aiohttp (breaks offline dpkg -i)" \
        || ok "deb Depends: no python3-aiohttp (vendored)"
    grep '^ Depends:' "$TMP/deb.control" | grep -q 'lxd' \
        && bad "deb Depends still lists lxd (breaks offline dpkg -i)" \
        || ok "deb Depends: no lxd"
    grep '^ Recommends:' "$TMP/deb.control" | grep -q 'lxd | lxd-installer' \
        && ok "deb Recommends: lxd | lxd-installer" || bad "deb Recommends missing lxd"
    grep -q 'vendor/aiohttp/' "$TMP/deb.list" \
        && ok "deb ships vendored aiohttp" || bad "deb missing resources/backend/vendor"
    # 1.0.139: seat provisioning needs curl + python3-venv + python3-pip on a bare
    # Ubuntu (Depends: apt resolves them online); tmux backs the terminal seat
    # (Recommends: absent = button disabled, never a traceback).
    for dep in curl python3-venv python3-pip; do
        grep '^ Depends:' "$TMP/deb.control" | grep -qE "(^|[ ,])$dep(\$|[ ,(])" \
            && ok "deb Depends: $dep" || bad "deb Depends missing $dep"
    done
    grep '^ Recommends:' "$TMP/deb.control" | grep -qE '(^|[ ,])tmux($|[ ,(])' \
        && ok "deb Recommends: tmux" || bad "deb Recommends missing tmux"
    ARTIFACTS+=("$DEB")
    ;;
rpm)
    say "verify rpm"
    [ -f "$RPM" ] || fail "expected $RPM"
    python3 "$SRC/build/rpm-inspect.py" --files "$RPM" > "$TMP/rpm.list"
    check_listing rpm "$TMP/rpm.list"
    python3 "$SRC/build/rpm-inspect.py" --payload "$RPM" \
        | cpio -i --quiet --to-stdout '*opt/hugpy-station/resources/app.asar' \
        > "$TMP/rpm.asar" 2>/dev/null || true
    check_asar rpm "$TMP/rpm.asar"
    python3 "$SRC/build/rpm-inspect.py" --scripts "$RPM" > "$TMP/rpm.scripts"
    grep -q 'hugpy-station-launch' "$TMP/rpm.scripts" \
        && ok "rpm %post wires the launcher (the alien conversion dropped this)" \
        || bad "rpm %post missing/!launcher"
    python3 "$SRC/build/rpm-inspect.py" --meta "$RPM" > "$TMP/rpm.meta"
    grep -q 'python3-aiohttp' "$TMP/rpm.meta" \
        && ok "rpm Requires: python3-aiohttp" || bad "rpm missing python3-aiohttp"
    ARTIFACTS+=("$RPM")
    ;;
pacman)
    say "verify pacman"
    [ -f "$PACMAN" ] || fail "expected $PACMAN"
    tar_list "$PACMAN" > "$TMP/pac.list"
    check_listing pacman "$TMP/pac.list"
    tar_extract "$PACMAN" opt/hugpy-station/resources/app.asar > "$TMP/pac.asar" || true
    check_asar pacman "$TMP/pac.asar"
    tar_extract "$PACMAN" .INSTALL > "$TMP/pac.install" || true
    grep -q 'hugpy-station-launch' "$TMP/pac.install" \
        && ok "pacman .INSTALL wires the launcher" || bad "pacman .INSTALL missing/!launcher"
    tar_extract "$PACMAN" .PKGINFO > "$TMP/pac.pkginfo" || true
    grep -q 'python-aiohttp' "$TMP/pac.pkginfo" \
        && ok "pacman depend = python-aiohttp" || bad "pacman missing python-aiohttp"
    ARTIFACTS+=("$PACMAN")
    ;;
AppImage)
    say "verify AppImage"
    [ -f "$APPIMAGE" ] || fail "expected $APPIMAGE"
    OFF="$(appimage_offset "$APPIMAGE")"
    [ -n "$OFF" ] || fail "AppImage --appimage-offset produced nothing"
    info "squashfs offset $OFF"
    unsquashfs -l -o "$OFF" "$APPIMAGE" | sed 's|^squashfs-root/||' > "$TMP/app.list"
    check_listing AppImage "$TMP/app.list"
    rm -rf "$TMP/sq"
    unsquashfs -n -o "$OFF" -d "$TMP/sq" "$APPIMAGE" \
        resources/app.asar AppRun hugpy-station-launch resources/VERSION >/dev/null
    check_asar AppImage "$TMP/sq/resources/app.asar"
    # The whole point of the AppImage work: AppRun must be OUR launcher, so the
    # no-display branch, --version and the aiohttp probe all run inside.
    if grep -q 'HUGPY_STATION_PYTHON' "$TMP/sq/AppRun" \
       && grep -q 'no graphical display' "$TMP/sq/AppRun"; then
        ok "AppRun is the hugpy launcher (python probe + no-display branch present)"
    else
        bad "AppRun is NOT the hugpy launcher — electron-builder's stock AppRun won"
    fi
    grep -q -- '--no-sandbox' "$TMP/sq/AppRun" \
        && ok "AppRun preserves --no-sandbox" || bad "AppRun lost --no-sandbox"
    ARTIFACTS+=("$APPIMAGE")
    ;;
esac
done

# ── 5. gate ──────────────────────────────────────────────────────────────────
say "results"
for a in "${ARTIFACTS[@]}"; do
    printf '   %-46s %8s KiB  %s\n' "$(basename "$a")" \
        "$(( $(stat -c%s "$a") / 1024 ))" "$(sha256sum "$a" | cut -c1-16)…"
done
[ "$FAILURES" -eq 0 ] || fail "$FAILURES check(s) failed — NOT staging"

# ── 6. sha256 sidecars + stage ───────────────────────────────────────────────
say "sha256 sidecars"
( cd "$DIST" && for a in "${ARTIFACTS[@]}"; do
      b="$(basename "$a")"; sha256sum "$b" > "$b.sha256"; cat "$b.sha256"
  done )

if [ "$DO_STAGE" = 1 ]; then
    say "staging -> $STAGE_DIR"
    [ -d "$STAGE_DIR" ] || fail "stage dir $STAGE_DIR does not exist"
    for a in "${ARTIFACTS[@]}"; do
        b="$(basename "$a")"
        cp -f "$a" "$STAGE_DIR/$b"
        cp -f "$DIST/$b.sha256" "$STAGE_DIR/$b.sha256"
        info "staged $b"
    done
    for a in "${ARTIFACTS[@]}"; do
        b="$(basename "$a")"
        ( cd "$STAGE_DIR" && sha256sum -c "$b.sha256" >/dev/null ) \
            || fail "staged $b does not match its sidecar"
    done
    ok "staged copies verify against their sidecars"
    # The shelf is the ONLY home of releases (operator, 2026-09-02). A build lands
    # in edit/; serving it is a deliberate step:  <shelf>/release.sh promote
    # (edit/ → latest/, latest/ → previous/, previous/ → old/). dist/ keeps
    # nothing but a symlink to what the shelf serves.
    for a in "${ARTIFACTS[@]}"; do
        b="$(basename "$a")"
        cmp -s "$a" "$STAGE_DIR/$b" && rm -f "$a" "$DIST/$b.sha256"
    done
    rm -rf "$DIST/linux-unpacked" "$DIST/builder-debug.yml"
    ln -sfn "$(dirname "$STAGE_DIR")/pub/hugpy-station_latest.deb" "$DIST/hugpy-station_latest.deb"
    info "dist/ holds only hugpy-station_latest.deb -> served release; now: $(dirname "$STAGE_DIR")/release.sh promote $VERSION"
else
    info "--no-stage: artifacts left in $DIST"
fi

say "done — hugpy Station $VERSION"
