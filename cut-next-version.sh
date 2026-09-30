#!/usr/bin/env bash
# cut-next-version.sh — the one-command "next .deb version" flow for hugpy Station.
# Run as vm_mgr from SOURCE (/srv/hugpy/src/station-app). See RELEASE.md.
#
#   ./cut-next-version.sh              # patch bump (x.y.Z -> x.y.Z+1) + changelog stub + build deb
#   ./cut-next-version.sh 1.2.3        # set an explicit version
#   ./cut-next-version.sh --no-build   # bump + changelog only, do not build
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SRC"

# Guard: refuse to run inside a DEPLOYED install (must be SOURCE).
case "$SRC" in
  /opt/hugpy-station*|*/hugpy-station/app/current*)
    echo "REFUSING: $SRC looks like a DEPLOYED install, not SOURCE." >&2
    echo "Edit + release from solcatcher@ae:/srv/hugpy/src/station-app instead." >&2
    exit 2 ;;
esac
[ -f package.json ] && [ -f resources/VERSION ] || {
  echo "Not in SOURCE: package.json / resources/VERSION missing here." >&2; exit 2; }

DO_BUILD=1; WANT=""
for a in "$@"; do
  case "$a" in
    --no-build) DO_BUILD=0 ;;
    [0-9]*.[0-9]*.[0-9]*) WANT="$a" ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

CUR="$(node -p "require('./package.json').version")"
VF="$(tr -d '[:space:]' < resources/VERSION)"
[ "$CUR" = "$VF" ] || { echo "Version files already disagree ($CUR vs $VF) — fix by hand first." >&2; exit 1; }

if [ -n "$WANT" ]; then NEW="$WANT"
else IFS=. read -r MA MI PA <<<"$CUR"; NEW="$MA.$MI.$((PA+1))"; fi
echo ">> $CUR -> $NEW"

# Bump BOTH sources of truth.
node -e "const f='./package.json',j=require(f);j.version='$NEW';require('fs').writeFileSync(f,JSON.stringify(j,null,2)+'\n')"
printf '%s\n' "$NEW" > resources/VERSION

# CHANGELOG stub — insert before the FIRST "## " version heading so the title +
# preamble stay on top. Idempotent: skip if this version is already present.
if [ -f CHANGELOG.md ] && ! grep -q "^## $NEW " CHANGELOG.md; then
  TMP="$(mktemp)"
  STUB="$(printf '## %s — %s\n\n### TODO: summarise this release\n\n* \n' "$NEW" "$(date +%F)")"
  awk -v stub="$STUB" '
    !done && /^## / { print stub "\n"; done=1 }
    { print }
    END { if (!done) print "\n" stub }
  ' CHANGELOG.md > "$TMP"
  mv "$TMP" CHANGELOG.md
  echo ">> CHANGELOG.md: added stub for $NEW — EDIT IT before promoting."
fi

if [ "$DO_BUILD" = 1 ]; then
  echo ">> building deb (no-stage)…"
  ./build-release.sh --no-stage --targets deb
  echo ">> developing deb: $SRC/dist/hugpy-station_${NEW}_amd64.deb"
else
  echo ">> skipped build (--no-build). When ready: ./build-release.sh --no-stage --targets deb"
fi
