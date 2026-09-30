#!/bin/bash
# hugpy Station — after-install scriptlet for deb, rpm AND pacman.
#
# This is debian/postinst (now deprecated/reference-only) plus electron-builder's
# stock AppArmor handling, generalised so the SAME semantics land on every
# package format. The 1.0.41 Fedora rpm was an `alien` conversion of the deb and
# silently dropped the postinst, which is why /usr/bin/hugpy-station pointed
# nowhere and chrome-sandbox was not SUID (STATION-FIX-README.md).
#
# TEMPLATING WARNING: electron-builder substitutes /\$\{([a-zA-Z]+)\}/ here and
# THROWS on an unknown macro. Only ${executable} and ${sanitizedProductName} are
# defined. Never brace a shell variable in this file — use $NAME (unbraced).

APP_ROOT='/opt/${sanitizedProductName}'
LAUNCHER="$APP_ROOT/hugpy-station-launch"
DESKTOP_FILE='/usr/share/applications/${executable}.desktop'

# --- /usr/bin/${executable} -> the LAUNCHER, never the raw Electron binary ----
# The raw binary crashes when started from a shell (zygote sandbox); the
# launcher supplies --no-sandbox, probes for a Python that can import aiohttp,
# answers --version/--help without a display, and forwards "$@".
if type update-alternatives >/dev/null 2>&1; then
    if [ -L '/usr/bin/${executable}' ] && [ -e '/usr/bin/${executable}' ] \
       && [ "$(readlink '/usr/bin/${executable}')" != '/etc/alternatives/${executable}' ]; then
        rm -f '/usr/bin/${executable}'
    fi
    # Drop stale alternatives that pointed at the raw binary (<=1.0.12 and any
    # package built by electron-builder's stock after-install template).
    update-alternatives --remove '${executable}' "$APP_ROOT/${executable}" 2>/dev/null || true
    update-alternatives --install '/usr/bin/${executable}' '${executable}' "$LAUNCHER" 100 \
        || ln -sf "$LAUNCHER" '/usr/bin/${executable}'
else
    ln -sf "$LAUNCHER" '/usr/bin/${executable}'
fi

# --- SUID chrome-sandbox (Electron 5+) ---------------------------------------
# Unconditional 4755, as debian/postinst has always done and as station-fix.sh
# verifies: -rwsr-xr-x root root.
chmod 4755 "$APP_ROOT/chrome-sandbox" 2>/dev/null || true

# --- desktop entry Exec -> the launcher --------------------------------------
# electron-builder refuses to let linux.desktop.entry.Exec be set (it throws in
# computeDesktopEntry), so the generated entry points at the raw binary. Rewrite
# it here — the same repair station-fix.sh performs on already-installed copies.
if [ -f "$DESKTOP_FILE" ]; then
    sed -i "s|^Exec=.*|Exec=\"$LAUNCHER\" %U|" "$DESKTOP_FILE" || true
fi

# The companion native window is source-owned by the bundled abstract-claude
# wheel; install its menu entry only when this Station payload carries it.
if [ -f "$APP_ROOT/resources/abstract-claude-console.desktop" ]; then
    cp -f "$APP_ROOT/resources/abstract-claude-console.desktop" \
        /usr/share/applications/abstract-claude-console.desktop 2>/dev/null || true
fi

# --- supersede previous hugpy Station desktop entries (1.0.45) ----------------
# ONE "hugpy Station" in the menu: the entry this package just installed.
# Previous incarnations leave entries that either duplicate it or SHADOW it
# (a user-local ${executable}.desktop outranks /usr/share/applications):
#   * appimagekit_*hugpy*tation*.desktop — AppImageLauncher integrations of
#     old AppImage builds ("hugpy Station (1.0.43)", "... (1)", ...)
#   * ~/.local/share/applications/${executable}.desktop — hand-made / dev
#     entries pointing at older or copied installs
#   * fleet-console.desktop — the pre-1.0.37 package name, both scopes
# Same best-effort user resolution as the lxd-group block below: the human
# who ran the install (SUDO_USER), else uid 1000. Never fatal.
rm -f /usr/share/applications/fleet-console.desktop 2>/dev/null || true
clean_stale_desktop_entries() {
    stale_user="$1"
    stale_home="$(getent passwd "$stale_user" 2>/dev/null | cut -d: -f6)"
    [ -n "$stale_home" ] && [ -d "$stale_home/.local/share/applications" ] || return 0
    for f in "$stale_home"/.local/share/applications/appimagekit_*ugpy*tation*.desktop \
             "$stale_home"/.local/share/applications/'${executable}'.desktop \
             "$stale_home"/.local/share/applications/fleet-console.desktop; do
        [ -f "$f" ] || continue
        rm -f "$f" 2>/dev/null || true
        echo "hugpy-station: removed stale desktop entry $f (superseded by $DESKTOP_FILE)"
    done
    if hash update-desktop-database 2>/dev/null; then
        update-desktop-database "$stale_home/.local/share/applications" 2>/dev/null || true
    fi
}
if [ -n "$SUDO_USER" ]; then
    clean_stale_desktop_entries "$SUDO_USER"
else
    clean_stale_desktop_entries "$(getent passwd 1000 2>/dev/null | cut -d: -f1)"
fi

# --- CLI sidecars ------------------------------------------------------------
# Domain hosting CLI (see: hugpy-station-domain --help). Station builder under
# its OWN name so a site layer's richer `vm-new` is never shadowed.
ln -sf "$APP_ROOT/resources/bin/hugpy-station-domain" /usr/bin/hugpy-station-domain 2>/dev/null || true
ln -sf "$APP_ROOT/resources/bin/vm-new" /usr/bin/fleet-vm-new 2>/dev/null || true

# --- headless unit -----------------------------------------------------------
# hugpy-station-web@<user>.service — the packaged backend without the desktop
# app, for domain/proxy access. Linked, not copied, so an upgrade cannot leave a
# stale unit behind. Never enabled here; `hugpy-station-domain` does that.
if [ -d /etc/systemd/system ] && [ -f "$APP_ROOT/resources/systemd/hugpy-station-web@.service" ]; then
    ln -sf "$APP_ROOT/resources/systemd/hugpy-station-web@.service" \
        /etc/systemd/system/hugpy-station-web@.service 2>/dev/null || true
    ln -sf "$APP_ROOT/resources/systemd/hugpy-station-board@.service" \
        /etc/systemd/system/hugpy-station-board@.service 2>/dev/null || true
fi
systemctl daemon-reload 2>/dev/null || true

if hash update-mime-database 2>/dev/null; then
    update-mime-database /usr/share/mime || true
fi
if hash update-desktop-database 2>/dev/null; then
    update-desktop-database /usr/share/applications || true
fi

# --- AppArmor profile (Ubuntu 24+) -------------------------------------------
# Verbatim from electron-builder's stock template: only install the profile if
# the running AppArmor can parse it (22.04 has no abi/4.0 and runs fine without).
if apparmor_status --enabled > /dev/null 2>&1; then
  APPARMOR_PROFILE_SOURCE="$APP_ROOT/resources/apparmor-profile"
  APPARMOR_PROFILE_TARGET='/etc/apparmor.d/${executable}'
  if apparmor_parser --skip-kernel-load --debug "$APPARMOR_PROFILE_SOURCE" > /dev/null 2>&1; then
    cp -f "$APPARMOR_PROFILE_SOURCE" "$APPARMOR_PROFILE_TARGET"
    if ! { [ -x '/usr/bin/ischroot' ] && /usr/bin/ischroot; } && hash apparmor_parser 2>/dev/null; then
      apparmor_parser --replace --write-cache --skip-read-cache "$APPARMOR_PROFILE_TARGET"
    fi
  else
    echo "Skipping the installation of the AppArmor profile as this version of AppArmor does not seem to support the bundled profile"
  fi
fi

# --- lxd group ---------------------------------------------------------------
# The console talks to LXD over its unix socket, which requires membership in
# the 'lxd' group; without it the launcher warns "fleet VMs will be invisible".
# Add the human who ran the install. Best-effort, idempotent, never fatal.
add_to_lxd_group() {
    lxd_user="$1"
    getent group lxd >/dev/null 2>&1 || return 0
    [ -n "$lxd_user" ] && [ "$lxd_user" != 'root' ] || return 0
    id "$lxd_user" >/dev/null 2>&1 || return 0
    if ! id -nG "$lxd_user" 2>/dev/null | tr ' ' '\n' | grep -qx lxd; then
        if usermod -aG lxd "$lxd_user" 2>/dev/null; then
            echo "hugpy-station: added '$lxd_user' to the 'lxd' group (log out/in once for it to take effect)"
        fi
    fi
}
if [ -n "$SUDO_USER" ]; then
    add_to_lxd_group "$SUDO_USER"
else
    add_to_lxd_group "$(getent passwd 1000 2>/dev/null | cut -d: -f1)"
fi

# --- seat settings hygiene ----------------------------------------------------
# Config screens before 1.0.46 wrote string values ("prompt"/"auto"/"pause")
# into switchModelsOnFlag, a key Claude Code validates as boolean — the seat
# then warns "Expected boolean, but received string" on every start. One-time
# cleanup of the installing user's seat settings. Best-effort, never fatal.
# (No brace expansions here — see the TEMPLATING WARNING at the top.)
fix_seat_settings() {
    fix_home="$(getent passwd "$1" 2>/dev/null | cut -d: -f6)"
    [ -n "$fix_home" ] && [ -d "$fix_home/.claude-seat" ] || return 0
    for f in "$fix_home"/.claude-seat/*/settings.json; do
        [ -f "$f" ] || continue
        python3 - "$f" <<'PYEOF' 2>/dev/null || true
import json, os, shutil, sys
p = sys.argv[1]
try:
    d = json.load(open(p))
except Exception:
    sys.exit(0)
v = d.get("switchModelsOnFlag")
if isinstance(v, str):
    if v.lower() in ("true", "false"):
        d["switchModelsOnFlag"] = v.lower() == "true"
    else:
        del d["switchModelsOnFlag"]
    tmp = p + ".postinst-tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=2)
        fh.write("\n")
    shutil.copystat(p, tmp)
    st = os.stat(p)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, p)
    print("hugpy-station: fixed switchModelsOnFlag in " + p)
PYEOF
    done
}
if [ -n "$SUDO_USER" ]; then
    fix_seat_settings "$SUDO_USER"
else
    fix_seat_settings "$(getent passwd 1000 2>/dev/null | cut -d: -f1)"
fi

# --- hugpy Station 1.0.90: ONE-SHOT first-run for every station user -----------
# "Install the .deb" is the whole story. This block (a) puts the seat CLIs and
# the install tools on the system PATH, (b) establishes the headless station
# instance for the installing user (hugpy-station-locus: instance env with a
# free PORT, STATION_LOCUS, HUGPY_STATION_STATE, console token; unit enabled),
# (c) runs hugpy-station-firstrun for EVERY station user on the host — the
# installing user plus every existing /etc/hugpy-station/<user>.env instance —
# which seeds the state dir, the working model default, the local-keeper
# charge, the abstract-claude config, ~/.local/bin/mct-pull|mct-push, and
# HUGPY_URL/HUGPY_API_KEY (found on the host or minted), and (d) restarts the
# running instances so they run exactly THIS package. Idempotent; never fatal.
# Set HUGPY_STATION_NO_HEADLESS=1 in the installer env to skip (b) and (d).
ln -sf "$APP_ROOT/resources/bin/hugpy-station-locus" /usr/bin/hugpy-station-locus 2>/dev/null || true
for cli in hugpy-station-firstrun hugpy-station-user-install mct-pull mct-push; do
    ln -sf "$APP_ROOT/resources/bin/$cli" "/usr/local/bin/$cli" 2>/dev/null || true
done
station_users=""
for f in /etc/hugpy-station/*.env; do
    [ -f "$f" ] || continue
    n="$(basename "$f" .env)"
    case "$n" in toolserver|hugpy-api|*.bak*) continue ;; esac
    id "$n" >/dev/null 2>&1 && station_users="$station_users $n"
done
install_user="$SUDO_USER"
[ -n "$install_user" ] || install_user="$(getent passwd 1000 2>/dev/null | cut -d: -f1)"
if [ -n "$install_user" ] && [ "$install_user" != root ]; then
    station_users="$station_users $install_user"
    # SOVEREIGN USER LAYOUT (1.0.90): an account that already runs the station
    # from its OWN systemd user manager (hugpy-station-user-install: unit in
    # ~/.config/systemd/user, payload under <state>/app/current) must NOT also
    # get a root-owned system instance. Minting one would enable a SECOND
    # station for that user on a freshly picked port, with its own env file and
    # console token, competing with the sovereign one. This matters on the
    # keeper, where the .deb is installed into /opt by an operator whose
    # $SUDO_USER is exactly that sovereign account.
    install_home="$(getent passwd "$install_user" 2>/dev/null | cut -d: -f6)"
    if [ -n "$install_home" ] \
       && [ -f "$install_home/.config/systemd/user/hugpy-station-web.service" ]; then
        echo "hugpy-station: '$install_user' runs a sovereign user-manager station — not minting a system instance."
        echo "               update it with:  hugpy-station-user-install <deb>   (as $install_user)"
    elif [ -z "$HUGPY_STATION_NO_HEADLESS" ] && [ -x "$APP_ROOT/resources/bin/hugpy-station-locus" ]; then
        "$APP_ROOT/resources/bin/hugpy-station-locus" enable "$install_user" --quiet \
            || echo "hugpy-station: headless instance for '$install_user' not enabled (see above); the desktop app still works"
    fi
fi
for u in $(printf '%s\n' $station_users | sort -u); do
    "$APP_ROOT/resources/bin/hugpy-station-firstrun" "$u" --app-root "$APP_ROOT" --quiet || true
done
if [ -z "$HUGPY_STATION_NO_HEADLESS" ]; then
    systemctl daemon-reload 2>/dev/null || true
    # an upgrade must leave every instance running THIS payload (not the old one in memory)
    systemctl try-restart 'hugpy-station-web@*' 2>/dev/null || true
    # ONE board file per locus: the shipped mirror watches the file the station
    # UI reads. Enabled per station user unless the host already runs its own
    # board-mirror@<user> (that one stays authoritative — never two mirrors), or
    # the operator retired that locus instance (/etc/hugpy-station/<user>.retired).
    for u in $(printf '%s\n' $station_users | sort -u); do
        [ -e "/etc/hugpy-station/$u.retired" ] && continue
        systemctl is-enabled "board-mirror@$u.service" >/dev/null 2>&1 && continue
        systemctl is-active "board-mirror@$u.service" >/dev/null 2>&1 && continue
        systemctl enable --now "hugpy-station-board@$u.service" >/dev/null 2>&1 || true
    done
fi

# --- hugpy Station: provision the model-seat CLIs (best-effort, never fatal) --
# A fresh box has none of the seat backends, so frontier (claude / abstract-
# claude mct) and local (hugpy-agent) report unavailable and only the shell
# surface shows. seat-provision.sh runs the ordered bootstrap as the seat user
# (pip -> venv -> pip upgrade -> install abstract-claude/hugpy-agent/claude),
# linking them onto ~/.local/bin where the console's PATH patch looks. Runs in
# the background so a package install never blocks on network pip fetches.
provision_seats() {
    seat_user="$1"; seat_home=""
    [ -n "$seat_user" ] && [ "$seat_user" != 'root' ] || return 0
    seat_home="$(getent passwd "$seat_user" 2>/dev/null | cut -d: -f6)"
    [ -n "$seat_home" ] && [ -d "$seat_home" ] || return 0
    seat_prov='/opt/hugpy-station/resources/station-stack/opt/station-keeper/seat-provision.sh'
    [ -r "$seat_prov" ] || return 0
    echo "hugpy-station: provisioning seat CLIs for '$seat_user' in the background (log: $seat_home/.local/share/station-seats/provision.log)"
    for d in "$seat_home/.local" "$seat_home/.local/share" "$seat_home/.local/bin" "$seat_home/.local/share/station-seats"; do
        install -d -o "$seat_user" -g "$seat_user" -m 0755 "$d" 2>/dev/null || true
    done
    # Forward an install-time OAuth token (if the installer set one) so the seat
    # user's provision authenticates with the same durable token as the toolserver.
    setsid sudo -u "$seat_user" -H \
        HUGPY_STATION_OAUTH="${HUGPY_STATION_OAUTH:-}" \
        bash "$seat_prov" \
        >"$seat_home/.local/share/station-seats/provision.log" 2>&1 &
}
if [ -n "$SUDO_USER" ]; then
    provision_seats "$SUDO_USER"
else
    provision_seats "$(getent passwd 1000 2>/dev/null | cut -d: -f1)"
fi

# --- hugpy Station: toolserver credential, BUILT INTO THE INSTALL (1.0.63) ------
# The station carries its own operator token in <user>/.config/hugpy-station/
# toolserver.env (0600). The backend loads it at startup, authenticates every
# toolserver call with it (loci-sync, handoffs, exchanges) and exports it to
# every seat it spawns (mct uploads, MCP bridge). Provisioned here from the
# installer's environment:   HUGPY_OPERATOR_TOKEN=… [HUGPY_TOOLSERVER_URL=…] sudo -E dpkg -i …
# Never overwrites an existing file. Without a token: the CLI below sets it later.
ln -sf "$APP_ROOT/resources/bin/hugpy-station-toolserver" /usr/local/bin/hugpy-station-toolserver 2>/dev/null || true
provision_toolserver_cred() {
    cred_user="$1"; cred_home=""
    [ -n "$cred_user" ] && [ "$cred_user" != 'root' ] || return 0
    cred_home="$(getent passwd "$cred_user" 2>/dev/null | cut -d: -f6)"
    [ -n "$cred_home" ] && [ -d "$cred_home" ] || return 0
    cred_dir="$cred_home/.config/hugpy-station"; cred_file="$cred_dir/toolserver.env"
    if [ -s "$cred_file" ]; then
        echo "hugpy-station: toolserver credential present ($cred_file)"; return 0
    fi
    if [ -z "$HUGPY_OPERATOR_TOKEN" ]; then
        echo "hugpy-station: NO toolserver credential — the station cannot read/write the fleet DB until one is set:"
        echo "               hugpy-station-toolserver set <operator token>     (as $cred_user)"
        echo "               or reinstall with: HUGPY_OPERATOR_TOKEN=... sudo -E dpkg -i <deb>"
        return 0
    fi
    install -d -o "$cred_user" -g "$cred_user" -m 0700 "$cred_dir" 2>/dev/null || true
    {
        echo "# hugpy-station toolserver credential — written by the package install; 0600"
        [ -n "$HUGPY_TOOLSERVER_URL" ] && echo "STATION_CONSOLE_TOOLSERVER=$HUGPY_TOOLSERVER_URL"
        echo "HUGPY_OPERATOR_TOKEN=$HUGPY_OPERATOR_TOKEN"
    } > "$cred_file.tmp" && chown "$cred_user:$cred_user" "$cred_file.tmp" && chmod 0600 "$cred_file.tmp" \
        && mv -f "$cred_file.tmp" "$cred_file" \
        && echo "hugpy-station: toolserver credential installed for '$cred_user' -> $cred_file"
}
if [ -n "$SUDO_USER" ]; then
    provision_toolserver_cred "$SUDO_USER"
else
    provision_toolserver_cred "$(getent passwd 1000 2>/dev/null | cut -d: -f1)"
fi

exit 0
