#!/usr/bin/env bash
# station-drift-check.sh — READ-ONLY. Prove that what is RUNNING is what the next
# .deb will SHIP. Exits non-zero on any drift. (2026-09-19)
#
# The standing rule this enforces — asked for repeatedly, since before this
# station existed:
#
#     *** NOTHING THE SYSTEM SERVES OR EXECUTES MAY BE HAND-AUTHORED
#         SOMEWHERE THAT DOES NOT SHIP. ***
#
# Convention did not hold it. This does, mechanically, because build-release.sh
# runs it FIRST and refuses to build when it fails. A live hotfix is still
# allowed; shipping while one is outstanding is not.
#
#   station-drift-check.sh              full check, table + non-zero on drift
#   station-drift-check.sh --quiet      table only on failure
#   station-drift-check.sh --register   print an updated unshipped-artifacts.tsv
#                                       (still read-only; it only prints)
#
# Sections, each FAILING (not warning):
#   A  installed station backend   vs  station-app source of record
#   B  installed abstract_claude   vs  the pin in resources/REQUIREMENTS.txt
#   C  the abstract_claude pin      vs  /srv/pyit/dev/abstract_claude version
#   D  every live baked console    vs  what station-bake-webui would produce
#   E  emergency bake hatches      (.no-bake / AC_UI_NO_BAKE) must be closed
#   F  git                         no dirty tracked files, no unpushed commits
#   G  unshipped-authored registry every entry byte-identical to its recorded
#                                  md5, and no SHIP-ME entry left outstanding
#
# Genuine runtime STATE is deliberately NOT checked and never will be: session
# stores, console.sqlite3, roster.json, rollover.json, queue.json, *.log, env/.
# State is expected to differ between the running system and any build; only
# AUTHORED and DERIVED artifacts are drift.
set -uo pipefail

QUIET=0; REGISTER=0
for a in "$@"; do
    case "$a" in
        --quiet)    QUIET=1 ;;
        --register) REGISTER=1 ;;
        -h|--help)  sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $a" >&2; exit 2 ;;
    esac
done

# ── where things live (all overridable) ─────────────────────────────────────
STATION_SRC="${STATION_SRC:-/srv/hugpy/src/station-app}"
STATION_APP="${STATION_APP:-/srv/vm_mgr/hugpy-station/app/current}"
# abstract_claude's ONE source is /srv/pyit/dev/abstract_claude (2026-09-24
# sweep: the old ac-wt/c144 worktree was a strict subset of it and is archived).
AC_WT="${AC_WT:-/srv/pyit/dev/abstract_claude}"
SERVE_VENV="${SERVE_VENV:-/srv/vm_mgr/abstract-claude-serve/venv}"
BAKE="${BAKE:-$STATION_SRC/resources/bin/station-bake-webui}"
# the registry ships next to resources/bin as resources/unshipped-artifacts.tsv,
# so prefer the copy beside THIS script (works from an install) and fall back to
# the source tree (works at build time from a checkout).
_self_res="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." 2>/dev/null && pwd)"
REGISTRY="${REGISTRY:-}"
[ -n "$REGISTRY" ] || for _r in "$_self_res/unshipped-artifacts.tsv" "$STATION_SRC/resources/unshipped-artifacts.tsv"; do
    [ -f "$_r" ] && { REGISTRY="$_r"; break; }
done
REGISTRY="${REGISTRY:-$STATION_SRC/resources/unshipped-artifacts.tsv}"
# "live dist dir" tab "the venv that owns its package" — every baked console on the box
LIVE_DISTS="${LIVE_DISTS:-/srv/vm_mgr/abstract-claude-serve/webui-station:$SERVE_VENV
/srv/hugpy/abstract-claude-serve/webui-station:/srv/hugpy/abstract-claude-serve/venv}"

ROWS=(); FAILS=0; WARNS=0
add() { ROWS+=("$(printf '%s\t%s\t%s' "$1" "$2" "$3")"); }
pass() { add "$1" "IN SYNC" "$2"; }
fail() { add "$1" "DRIFT"   "$2"; FAILS=$((FAILS+1)); }
warn() { add "$1" "warn"    "$2"; WARNS=$((WARNS+1)); }
skip() { add "$1" "skip"    "$2"; }

md5of() { [ -f "$1" ] && md5sum < "$1" | cut -d' ' -f1 || echo MISSING; }
git_q() { git -c safe.directory='*' -C "$1" "${@:2}" 2>/dev/null; }

# ── A. station backend: installed vs source of record ───────────────────────
# test_*.py is excluded on purpose: build-release.sh's SANITIZE_RE strips it
# from every artifact, so it is *correctly* absent from the install.
A_SRC="$STATION_SRC/resources/backend"
A_INS="$STATION_APP/resources/backend"
# 2026-10-02: DIRECTION-AWARE. The danger is LIVE ahead of source (a hand edit
# the next install deletes), not SOURCE ahead of live (the release itself). A
# file that differs from source passes when the installed copy is byte-identical
# to what the installed version SHIPPED — the commit that set resources/VERSION
# to the installed version. Anything else (a live edit, or no shipped commit to
# prove it) still fails.
GIT_PREFIX="$(git_q "$STATION_SRC" rev-parse --show-prefix)"
INS_VER="$(cat "$STATION_APP/resources/VERSION" 2>/dev/null | tr -d '[:space:]')"
SHIPPED_REV=""
if [ -n "$INS_VER" ]; then   # newest commit whose resources/VERSION *is* the installed version
    for _rev in $(git_q "$STATION_SRC" log --format=%H -- resources/VERSION); do
        [ "$(git_q "$STATION_SRC" show "$_rev:${GIT_PREFIX}resources/VERSION" | tr -d '[:space:]')" = "$INS_VER" ] \
            && { SHIPPED_REV="$_rev"; break; }
    done
fi
shipped_md5() { [ -n "$SHIPPED_REV" ] || { echo NOREV; return; }
                git_q "$STATION_SRC" cat-file -e "$SHIPPED_REV:$GIT_PREFIX$1" || { echo MISSING; return; }
                git_q "$STATION_SRC" show "$SHIPPED_REV:$GIT_PREFIX$1" | md5sum | cut -d' ' -f1; }
if [ -d "$A_SRC" ] && [ -d "$A_INS" ]; then
    a_bad=""; a_ahead=0
    while IFS= read -r rel; do
        [ -n "$rel" ] || continue
        ins="$(md5of "$A_INS/$rel")"
        [ "$(md5of "$A_SRC/$rel")" = "$ins" ] && continue
        if [ "$(shipped_md5 "resources/backend/$rel")" = "$ins" ]; then
            a_ahead=$((a_ahead+1))         # live == shipped: source is simply ahead
        else
            a_bad="$a_bad $rel"            # live differs from what shipped: a hand edit
        fi
    done < <(cd "$A_SRC" && find . -type f \( -name '*.py' -o -name '*.js' -o -name '*.css' -o -name '*.html' -o -name '*.json' \) \
                 ! -path './__pycache__/*' ! -name '*.pyc' ! -name 'test_*.py' -printf '%P\n' 2>/dev/null)
    if [ -n "$a_bad" ]; then
        fail "A backend installed vs source" "$(echo $a_bad | tr ' ' '\n' | head -6 | tr '\n' ' ')($(echo $a_bad | wc -w) files differ from shipped ${INS_VER:-?}${SHIPPED_REV:+ @ ${SHIPPED_REV:0:8}})"
    elif [ "$a_ahead" -gt 0 ]; then
        pass "A backend installed vs source" "live == shipped $INS_VER @ ${SHIPPED_REV:0:8}; source ahead by $a_ahead file(s)"
    else
        pass "A backend installed vs source" "$STATION_APP -> $STATION_SRC"
    fi
else
    skip "A backend installed vs source" "missing tree ($A_SRC / $A_INS)"
fi

# ── B. installed abstract_claude vs the pin ─────────────────────────────────
# 1.0.112: bundled wheels retired. resources/REQUIREMENTS.txt is the authority —
# assert the serve venv's installed abstract_claude matches the pinned version.
REQ_FILE="${REQ_FILE:-$STATION_SRC/resources/REQUIREMENTS.txt}"
ac_pin() { sed -n 's/^abstract-claude==\([0-9][0-9.]*\).*/\1/p' "$1" 2>/dev/null | head -1; }
PYBIN="$SERVE_VENV/bin/python3"; [ -x "$PYBIN" ] || PYBIN="$SERVE_VENV/bin/python3.12"
[ -x "$PYBIN" ] || PYBIN="$SERVE_VENV/bin/python"
PIN="$(ac_pin "$REQ_FILE")"
IVER="$("$PYBIN" -c 'import abstract_claude;print(getattr(abstract_claude,"__version__","?"))' 2>/dev/null || echo '?')"
if [ -z "$PIN" ]; then
    skip "B abstract_claude installed vs pin" "no abstract-claude pin in $REQ_FILE"
elif [ -z "$IVER" ] || [ "$IVER" = '?' ]; then
    skip "B abstract_claude installed vs pin" "abstract_claude not importable in $SERVE_VENV"
elif [ "$IVER" = "$PIN" ]; then
    pass "B abstract_claude installed vs pin" "$IVER == pin $PIN ($SERVE_VENV)"
elif [ -n "$SHIPPED_REV" ] && [ "$IVER" = "$(git_q "$STATION_SRC" show "$SHIPPED_REV:${GIT_PREFIX}resources/REQUIREMENTS.txt" \
                                             | sed -n 's/^abstract-claude==\([0-9][0-9.]*\).*/\1/p' | head -1)" ]; then
    # 2026-10-02: installed == the pin the installed version SHIPPED; the pin is
    # simply ahead (this release bumps it) — installing the build closes it.
    pass "B abstract_claude installed vs pin" "installed $IVER == shipped pin ($INS_VER); pin ahead -> $PIN"
else
    fail "B abstract_claude installed vs pin" "installed $IVER != pinned $PIN — re-run station-provision-venv $SERVE_VENV"
fi

# ── C. the pin vs the abstract_claude source of record ───────────────────────
# The pin must name the version currently in /srv/pyit/dev/abstract_claude
# (pyproject.toml). Catches "bumped the source, forgot to bump the pin" and the
# reverse. PyPI-resolvability of the pin is gated at build time by build-release.sh.
C_PYPROJECT="$AC_WT/pyproject.toml"
if [ -n "$PIN" ] && [ -f "$C_PYPROJECT" ]; then
    SRCVER="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' "$C_PYPROJECT" | head -1)"
    if [ -z "$SRCVER" ]; then
        skip "C pin vs abstract_claude source" "no version in $C_PYPROJECT"
    elif [ "$SRCVER" = "$PIN" ]; then
        pass "C pin vs abstract_claude source" "pin $PIN == source $SRCVER"
    elif [ "$(GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*' \
              git -C "$AC_WT" show "abstract-claude-v$PIN:pyproject.toml" 2>/dev/null \
              | sed -n 's/^version[[:space:]]*=[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' | head -1)" = "$PIN" ]; then
        # 1.0.128: the published release is a tagged tree (abstract-claude-v<pin>),
        # which may sit on a branch ahead of the checked-out one
        pass "C pin vs abstract_claude source" "pin $PIN == release tag abstract-claude-v$PIN in $AC_WT"
    else
        fail "C pin vs abstract_claude source" "pin $PIN != source $SRCVER ($C_PYPROJECT) — bump the pin or the source"
    fi
else
    skip "C pin vs abstract_claude source" "pin='${PIN:-none}' src='$C_PYPROJECT'"
fi

# ── D/E. every live baked console vs what the bake would produce ────────────
while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    dist="${spec%%:*}"; venv="${spec##*:}"
    [ -d "$dist" ] || { skip "D bake $dist" "no such dist"; continue; }
    acbin="$venv/bin/abstract-claude"
    if [ ! -x "$BAKE" ]; then skip "D bake $dist" "station-bake-webui missing at $BAKE"; continue; fi
    out="$(ABSTRACT_CLAUDE_BIN="$acbin" AC_UI_NO_BAKE= "$BAKE" --check "$dist" 2>&1)"; rc=$?
    case "$rc" in
        0) pass "D bake $dist" "derived from $venv" ;;
        3) fail "D bake $dist" "${out#*DIFFERS from the package bake: } — live differs from the package; port it into /srv/pyit/dev/abstract_claude" ;;
        4) fail "E bake hatch $dist" ".no-bake / AC_UI_NO_BAKE open — hand-held tree, cannot ship" ;;
        *) fail "D bake $dist" "check failed rc=$rc: $out" ;;
    esac
done <<< "$LIVE_DISTS"

# ── F. git: dirty tracked files / unpushed commits ──────────────────────────
# Informational only (warn, never DRIFT): operator ruling 2026-09-23 — git
# commit/push state is not the measure of integration; content vs live is
# (checks A-E, G). A dirty tree still shows here so it is not forgotten.
for repo_spec in "abstract_claude:$AC_WT:src/abstract_claude" "station-app:$STATION_SRC:."; do
    name="${repo_spec%%:*}"; rest="${repo_spec#*:}"; dir="${rest%%:*}"; scope="${rest##*:}"
    if [ ! -d "$dir/.git" ] && [ ! -f "$dir/.git" ]; then
        # a worktree's .git is a file; a subdir of a repo has neither
        git_q "$dir" rev-parse --git-dir >/dev/null || { skip "F git $name" "not a git repo: $dir"; continue; }
    fi
    dirty="$(git_q "$dir" status --porcelain -- "$scope" | sed 's/^/   /')"
    ahead="$(git_q "$dir" rev-list --count '@{u}..HEAD' || echo '?')"
    br="$(git_q "$dir" rev-parse --abbrev-ref HEAD)"
    msg=""
    [ -n "$dirty" ] && msg="dirty: $(git_q "$dir" status --porcelain -- "$scope" | awk '{print $NF}' | head -4 | tr '\n' ' ')"
    if [ "$ahead" != "0" ] && [ "$ahead" != "?" ]; then msg="$msg unpushed: $ahead commit(s) on $br"; fi
    if [ -n "$msg" ]; then warn "F git $name" "$msg"; else pass "F git $name" "$br clean and pushed ($scope)"; fi
done

# ── G. unshipped-authored registry ──────────────────────────────────────────
# Every artifact on this box that the system executes/serves but that no
# shipping repo owns is listed in resources/unshipped-artifacts.tsv with its
# md5 at the time it was acknowledged. Columns:
#   path <TAB> class <TAB> md5 <TAB> disposition <TAB> note
#   class:       authored | derived | state
#   disposition: SHIP-ME   must be brought into a shipping repo — always FAILS
#                ACK       acknowledged, pinned: FAILS if it changes
#                STATE     runtime state, never checked (recorded for the record)
# A changed ACK entry fails because an unshipped file that is being EDITED is,
# by definition, live work that will be lost on the next install.
if [ -f "$REGISTRY" ]; then
    while IFS=$'\t' read -r rpath rclass rmd5 rdisp rnote; do
        case "${rpath:-}" in ''|'#'*) continue ;; esac
        case "$rdisp" in
            STATE) skip "G state $rpath" "${rnote:-runtime state, excluded by design}" ;;
            SHIP-ME)
                fail "G unshipped $rpath" "${rnote:-authored but in no shipping repo} — bring it into a source of record" ;;
            ACK)
                now="$(md5of "$rpath")"
                if [ "$now" = "MISSING" ]; then warn "G unshipped $rpath" "registered but absent"
                elif [ "$now" != "$rmd5" ]; then fail "G unshipped $rpath" "CHANGED since acknowledgment ($rmd5 -> $now) — this edit ships nowhere"
                else pass "G unshipped $rpath" "pinned, unchanged${rnote:+ — $rnote}"; fi ;;
            *) warn "G unshipped $rpath" "unknown disposition '${rdisp:-}'" ;;
        esac
    done < "$REGISTRY"
else
    fail "G unshipped registry" "$REGISTRY missing — the inventory of unshipped authored artifacts is the whole point; recreate it"
fi

# ── report ──────────────────────────────────────────────────────────────────
if [ "$REGISTER" = 1 ]; then
    printf '# regenerate md5s for ACK entries (review before committing)\n'
    [ -f "$REGISTRY" ] && while IFS=$'\t' read -r rpath rclass rmd5 rdisp rnote; do
        case "${rpath:-}" in ''|'#'*) printf '%s\n' "${rpath:-}"; continue ;; esac
        [ "$rdisp" = ACK ] && rmd5="$(md5of "$rpath")"
        printf '%s\t%s\t%s\t%s\t%s\n' "$rpath" "$rclass" "$rmd5" "$rdisp" "$rnote"
    done < "$REGISTRY"
    exit 0
fi

if [ "$QUIET" = 1 ] && [ "$FAILS" = 0 ]; then exit 0; fi

printf '\n\033[1m== station drift check — %s ==\033[0m\n' "$(date -Is)"
printf '%-46s  %-8s  %s\n' "CHECK" "STATUS" "DETAIL"
printf '%-46s  %-8s  %s\n' "$(printf '%.0s-' {1..46})" "--------" "$(printf '%.0s-' {1..40})"
for r in "${ROWS[@]}"; do
    IFS=$'\t' read -r c s d <<< "$r"
    case "$s" in
        "IN SYNC") col=32 ;; "DRIFT") col=31 ;; "warn") col=33 ;; *) col=90 ;;
    esac
    printf '%-46s  \033[%sm%-8s\033[0m  %s\n' "$c" "$col" "$s" "$d"
done
printf '\n'
if [ "$FAILS" -gt 0 ]; then
    printf '\033[31m%s drift item(s), %s warning(s) — THE NEXT RELEASE WOULD NOT CONTAIN THE LIVE SYSTEM.\033[0m\n' "$FAILS" "$WARNS"
    printf 'Fix the DRIFT rows, or build with FORCE_DRIFT=1 (recorded, and you own the consequences).\n\n'
    exit 1
fi
printf '\033[32mno drift: live == source of record == what the next .deb will ship (%s warning(s)).\033[0m\n\n' "$WARNS"
exit 0
