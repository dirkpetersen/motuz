#!/usr/bin/env bash
# Removes the systemd install of Motuz (bin/systemd/install.sh + deploy.sh). Run as root.
# Idempotent. By default it keeps the account, its home (the checkout, ~/.config/motuz with
# the settings and secrets, and ~/data with the database), the pinned binaries and the
# packages, so a later install.sh + deploy.sh continues with the same data; export the
# data first (README) if it should move elsewhere.
#
# Usage: sudo bin/systemd/uninstall.sh [--user=NAME] [--purge --yes]
#   --user=NAME  the service account (default motuz)
#   --purge      also delete the account with its home and all Motuz data (database,
#                secrets, certificates), /var/lib/motuz-aws-config, the login helper's
#                state and /usr/local/bin/{rclone,traefik,uv}; needs --yes
#
# Always: stops motuz.target and the user manager (loginctl disable-linger), removes the
# account's Motuz units, the sudoers rule, /etc/pam.d/motuz, the sysctl file, the login
# helper (motuz-auth.socket) and unmasks the distribution's PostgreSQL and Redis/Valkey
# services (bin/systemd/distro/<ID>.sh). Packages stay installed (dnf/apt remove them).

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

ACCOUNT=motuz
PURGE=0
YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --user=*) ACCOUNT="${1#*=}" ;;
        --user) ACCOUNT="${2:?--user needs a value}"; shift ;;
        --purge) PURGE=1 ;;
        --yes) YES=1 ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1" ;;
    esac
    shift
done

[ "$(id -u)" = 0 ] || die "run as root"
[[ "$ACCOUNT" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "invalid account name $ACCOUNT"
[ "$PURGE" = 0 ] || [ "$YES" = 1 ] || die "--purge deletes the database and all Motuz data of $ACCOUNT: add --yes"
load_distro

HOME_DIR=""
if id "$ACCOUNT" >/dev/null 2>&1; then
    uid=$(id -u "$ACCOUNT")
    HOME_DIR=$(getent passwd "$ACCOUNT" | cut -d: -f6)
    [ "$uid" != 0 ] || die "$ACCOUNT is root"
    log "stopping Motuz ($ACCOUNT's user services)"
    if [ -d "/run/user/$uid" ]; then
        runuser -u "$ACCOUNT" -- env XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user disable --now motuz.target >/dev/null 2>&1 || true
        runuser -u "$ACCOUNT" -- env XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user stop 'motuz-*.service' >/dev/null 2>&1 || true
    fi
    loginctl disable-linger "$ACCOUNT" 2>/dev/null || true
    systemctl stop "user@$uid.service" 2>/dev/null || true
    # Units only: the settings, secrets and data stay unless --purge
    rm -f "$HOME_DIR"/.config/systemd/user/motuz*
    rm -f "$HOME_DIR"/.config/systemd/user/default.target.wants/motuz.target
else
    warn "no account $ACCOUNT"
fi

log "sudoers rule, PAM service, sysctl"
rm -f /etc/sudoers.d/motuz /etc/pam.d/motuz /etc/sysctl.d/60-motuz.conf
visudo -c -q || warn "sudoers is invalid (not caused by /etc/sudoers.d/motuz, which is gone)"
# The kernel's defaults, then whatever other sysctl files set
sysctl -q -w net.ipv4.ip_unprivileged_port_start=1024 vm.overcommit_memory=0 >/dev/null 2>&1 || true
sysctl -q --system >/dev/null 2>&1 || true

if [ -f /etc/systemd/system/motuz-auth.socket ]; then
    log "login helper (motuz-auth.socket)"
    systemctl disable --now motuz-auth.socket >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/motuz-auth.socket /etc/systemd/system/motuz-auth@.service
    rm -rf /usr/local/lib/motuz-auth
    systemctl daemon-reload
fi

log "unmasking $DISTRO_SERVICES"
for unit in $DISTRO_SERVICES; do
    systemctl unmask "$unit" >/dev/null 2>&1 || true
done

if [ "$PURGE" = 1 ]; then
    log "purge: account $ACCOUNT and its home, Motuz data, pinned binaries"
    if [ -n "$HOME_DIR" ]; then
        pkill -KILL -u "$ACCOUNT" 2>/dev/null || true
        userdel --remove "$ACCOUNT" 2>/dev/null || userdel "$ACCOUNT"
        # userdel only removes the home if it is owned by the account
        case "$HOME_DIR" in /|/home|/var|/var/lib|/usr*|/etc*|/root) ;; *) rm -rf "$HOME_DIR" ;; esac
    fi
    rm -rf /var/lib/motuz-aws-config /var/lib/motuz-auth
    rm -f /usr/local/bin/rclone /usr/local/bin/traefik /usr/local/bin/uv
else
    [ -z "$HOME_DIR" ] || echo "    kept: the account $ACCOUNT and $HOME_DIR (checkout, ~/.config/motuz, ~/data), /usr/local/bin/{rclone,traefik,uv}"
fi
log "done"
