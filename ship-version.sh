#!/usr/bin/env bash
# ship-version.sh — ONE call to promote a built version to the fleet, correctly.
# Ensures the deb exists (builds if missing), stages it into the shelf's edit/,
# runs release.sh promote (verifies sha256, rotates edit→latest→previous→old,
# repoints every pub/ + shelf-root symlink), and prints what pub/ now serves.
# Run as vm_mgr from SOURCE. Default version = package.json. See RELEASE.md.
#
#   ./ship-version.sh            # promote the current source version
#   ./ship-version.sh 1.2.3      # promote an explicit version
#
# This PUBLISHES the version (makes it the latest downloadable/installable deb).
# It does NOT hot-swap already-running installs — each station updates on its own
# reinstall. Undo with:  <shelf>/release.sh rollback
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SRC"
SHELF="${HUGPY_STATION_SHELF:-/srv/hugpy/www/station}"
[ -x "$SHELF/release.sh" ] || { echo "no release.sh at $SHELF"; exit 2; }

VER="${1:-$(node -p "require('$SRC/package.json').version")}"
VF="$(tr -d '[:space:]' < "$SRC/resources/VERSION" 2>/dev/null || echo '')"
[ "$VF" = "$VER" ] || { echo "package.json ($VER) and resources/VERSION ($VF) disagree — run cut-next-version.sh"; exit 1; }

DEB="$SRC/dist/hugpy-station_${VER}_amd64.deb"
if [ ! -f "$DEB" ]; then
  echo ">> no dist deb for $VER — building (deb, no-stage)…"
  ./build-release.sh --no-stage --targets deb
fi
[ -f "$DEB" ] || { echo "build did not produce $DEB"; exit 1; }
if [ ! -f "$DEB.sha256" ]; then
  ( cd "$SRC/dist" && sha256sum "hugpy-station_${VER}_amd64.deb" > "hugpy-station_${VER}_amd64.deb.sha256" )
fi

echo ">> staging $VER into $SHELF/edit/"
cp -f "$DEB" "$DEB.sha256" "$SHELF/edit/"

echo ">> promoting $VER"
"$SHELF/release.sh" promote "$VER"

echo
echo ">> release status:"
"$SHELF/release.sh" status
echo ">> pub/ serves: $(readlink -f "$SHELF/pub/hugpy-station_latest.deb" 2>/dev/null || echo '?')"
