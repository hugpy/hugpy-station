#!/bin/bash
# hugpy Station — after-remove scriptlet for deb, rpm AND pacman.
# debian/postrm (deprecated/reference-only) plus electron-builder's stock
# AppArmor teardown. See linux-after-install.tpl for the ${...} macro warning:
# only ${executable} and ${sanitizedProductName} may appear in braces.

APP_ROOT='/opt/${sanitizedProductName}'

# --- /usr/bin/${executable} ---------------------------------------------------
# update-alternatives --remove takes the REGISTERED path (the launcher), not the
# generic symlink. Also drop the raw-binary registration older packages made.
if type update-alternatives >/dev/null 2>&1; then
    update-alternatives --remove '${executable}' "$APP_ROOT/hugpy-station-launch" 2>/dev/null || true
    update-alternatives --remove '${executable}' "$APP_ROOT/${executable}" 2>/dev/null || true
else
    rm -f '/usr/bin/${executable}'
fi

# Is this a real removal or the teardown half of an upgrade? The three package
# managers disagree about $1, so the test is "assume removal, recognise the
# upgrade cases" — the shapes that are NOT a removal:
#   deb    : upgrade | failed-upgrade | deconfigure | disappear
#            (a real removal is "remove" or "purge")
#   rpm    : 1 = another copy remains, i.e. an upgrade (0 = last copy going away)
#   pacman : post_remove runs ONLY on real removal — pacman calls post_upgrade
#            for upgrades — and it is passed the VERSION ("1.0.42-1"), which is
#            why this cannot be a whitelist of removal words.
STATION_PURGE=1
case "${1:-}" in
    upgrade|failed-upgrade|deconfigure|disappear|1) STATION_PURGE=0 ;;
esac

if [ "$STATION_PURGE" = 1 ]; then
    systemctl disable --now 'hugpy-station-web@*' 2>/dev/null || true
    rm -f /etc/systemd/system/hugpy-station-web@.service
    rm -f /usr/bin/hugpy-station-domain /usr/bin/fleet-vm-new
    systemctl daemon-reload 2>/dev/null || true
    if hash update-desktop-database 2>/dev/null; then
        update-desktop-database /usr/share/applications || true
    fi
fi

# --- AppArmor profile ---------------------------------------------------------
APPARMOR_PROFILE_DEST='/etc/apparmor.d/${executable}'
if [ -f "$APPARMOR_PROFILE_DEST" ] && [ "$STATION_PURGE" = 1 ]; then
  if apparmor_status --enabled > /dev/null 2>&1; then
    if ! { [ -x '/usr/bin/ischroot' ] && /usr/bin/ischroot; } && hash apparmor_parser 2>/dev/null; then
      apparmor_parser --remove "$APPARMOR_PROFILE_DEST" || true
    fi
  fi
  rm -f "$APPARMOR_PROFILE_DEST"
fi

exit 0
