#!/usr/bin/env bash
# Prepares a machine (a VM, or a privileged LXC) with Ubuntu 26.04 LTS (the supported
# distributions are the modules in bin/systemd/distro) for Motuz without docker: `systemd --user` services of one dedicated account (README, "Install without
# Docker (Ubuntu 26.04)"). Run as root, once; it is idempotent, so run it again after
# updates that change what it installs (bin/systemd/deploy.sh warns about that).
#
# Usage: sudo bin/systemd/install.sh [--local-accounts] [--sudo-group=GROUP] [--user=NAME] [--home=DIR]
#        sudo bin/systemd/install.sh --worker-only [--sudo-group=GROUP] [--user=NAME] [--home=DIR]
#   --worker-only       a remote worker host (README, "Remote workers (HTTPS only)"): only
#                       the pinned rclone, python3, the account with linger, its sudoers
#                       rule and /var/lib/motuz-aws-config; no database, broker, web server
#                       or login helper. Then configure and start motuz-worker.service
#                       (src/worker) as the account, as the README describes.
#   --local-accounts    install the login helper (motuz-auth.socket), needed when users
#                       log in with local /etc/shadow accounts (not for SSSD/Kerberos)
#   --sudo-group=GROUP  Motuz may act only as members of GROUP (default: any user but root)
#   --user=NAME         the service account (default motuz)
#   --home=DIR          its home directory, on local disk (default /var/lib/motuz)
#
# What it does:
#   - packages (bin/systemd/distro/<ID>.sh): PostgreSQL 18 (the distribution's own
#     cluster and service never run), Redis (its service masked), Node.js/npm, build
#     tools, git, curl, openssl
#   - pinned rclone, Traefik and uv in /usr/local/bin, SHA256 checked
#   - the account, `loginctl enable-linger`, its sudoers rule (checked with visudo -c)
#   - sysctl: net.ipv4.ip_unprivileged_port_start=80 (Traefik is a user service on
#     :80/:443), vm.overcommit_memory=1 (Redis)
#   - /etc/pam.d/motuz, /var/lib/motuz-aws-config (AWS SSO configs for rclone)
# Then, as the account: clone the repository to ~/motuz and run bin/systemd/deploy.sh.

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

LOCAL_ACCOUNTS=0
WORKER_ONLY=0
SUDO_GROUP=""
ACCOUNT=motuz
HOME_DIR=/var/lib/motuz
while [ $# -gt 0 ]; do
    opt="$1"; value=""
    case "$opt" in
        --*=*) value="${opt#*=}"; opt="${opt%%=*}" ;;
        --sudo-group|--user|--home) value="${2:?$1 needs a value}"; shift ;;
    esac
    case "$opt" in
        --local-accounts) LOCAL_ACCOUNTS=1 ;;
        --worker-only) WORKER_ONLY=1 ;;
        --sudo-group) SUDO_GROUP="$value" ;;
        --user) ACCOUNT="$value" ;;
        --home) HOME_DIR="$value" ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1" ;;
    esac
    shift
done

[ "$(id -u)" = 0 ] || die "run as root"
[ "$WORKER_ONLY" = 0 ] || [ "$LOCAL_ACCOUNTS" = 0 ] || die "--local-accounts is not for workers (they do not log users in)"
[[ "$ACCOUNT" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "invalid account name $ACCOUNT"
[[ "$HOME_DIR" = /* ]] || die "--home must be absolute"
[ -z "$SUDO_GROUP" ] || getent group "$SUDO_GROUP" >/dev/null || die "group $SUDO_GROUP does not exist"
load_distro
[ -d /run/systemd/system ] || die "systemd is not running (a container without systemd?)"

load_versions
ARCH=$(download_arch)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------- packages
log "packages ($DISTRO_NAME)"
if [ "$WORKER_ONLY" = 1 ]; then
    distro_install_worker_packages
else
    distro_install_packages
fi

# ---------------------------------------------------------------- pinned binaries
fetch_checked() { # url sha256 file
    curl -fsSL --retry 3 -o "$3" "$1" || die "download failed: $1"
    echo "$2  $3" | sha256sum -c --quiet - || die "SHA256 mismatch: $1"
}

if [ "$(/usr/local/bin/rclone version 2>/dev/null | head -1)" != "rclone v${RCLONE_VERSION}" ]; then
    log "rclone ${RCLONE_VERSION}"
    sha="RCLONE_SHA256_${ARCH^^}"
    name="rclone-v${RCLONE_VERSION}-linux-${ARCH}"
    fetch_checked "https://downloads.rclone.org/v${RCLONE_VERSION}/${name}.zip" "${!sha}" "$TMP/rclone.zip"
    unzip -q "$TMP/rclone.zip" -d "$TMP"
    install -m 755 -o root -g root "$TMP/$name/rclone" /usr/local/bin/rclone
fi
if [ "$WORKER_ONLY" = 0 ] && ! /usr/local/bin/traefik version 2>/dev/null | grep -qE "^Version:[[:space:]]+${TRAEFIK_VERSION}$"; then
    log "Traefik ${TRAEFIK_VERSION}"
    sha="TRAEFIK_SHA256_${ARCH^^}"
    fetch_checked "https://github.com/traefik/traefik/releases/download/v${TRAEFIK_VERSION}/traefik_v${TRAEFIK_VERSION}_linux_${ARCH}.tar.gz" "${!sha}" "$TMP/traefik.tar.gz"
    tar -xzf "$TMP/traefik.tar.gz" -C "$TMP" traefik
    install -m 755 -o root -g root "$TMP/traefik" /usr/local/bin/traefik
fi
if [ "$WORKER_ONLY" = 0 ] && [ "$(/usr/local/bin/uv --version 2>/dev/null)" != "uv ${UV_VERSION}" ]; then
    log "uv ${UV_VERSION}"
    sha="UV_SHA256_${ARCH^^}"
    target=$([ "$ARCH" = arm64 ] && echo aarch64 || echo x86_64)-unknown-linux-gnu
    fetch_checked "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}.tar.gz" "${!sha}" "$TMP/uv.tar.gz"
    tar -xzf "$TMP/uv.tar.gz" -C "$TMP"
    install -m 755 -o root -g root "$TMP/uv-${target}/uv" /usr/local/bin/uv
fi

# ---------------------------------------------------------------- the account
if ! id "$ACCOUNT" >/dev/null 2>&1; then
    log "account $ACCOUNT ($HOME_DIR)"
    # A system account: no password (locked), uid below UID_MIN, so the login helper and
    # Motuz itself refuse it as a Motuz user. The home must be on local disk.
    useradd --system --user-group --create-home --home-dir "$HOME_DIR" --shell /bin/bash \
        --comment "Motuz service account" "$ACCOUNT"
fi
HOME_DIR=$(getent passwd "$ACCOUNT" | cut -d: -f6)
chmod 750 "$HOME_DIR"
loginctl enable-linger "$ACCOUNT"
systemctl start "user@$(id -u "$ACCOUNT").service"

# ---------------------------------------------------------------- sudoers
# The only privilege of the account: run rclone, ls, mkdir and env (the file viewer and the
# credential readers run python3 through env) as the logged-in user, never as root.
# SETENV: rclone gets the connection's credentials as RCLONE_CONFIG_* variables
# (sudo --preserve-env=<names>).
log "sudoers rule /etc/sudoers.d/motuz"
RUNAS="ALL, !root"
[ -z "$SUDO_GROUP" ] || RUNAS="%${SUDO_GROUP}, !root"
cat > "$TMP/sudoers" <<EOF
# Motuz (bin/systemd/install.sh): file and rclone operations as the logged-in user
$ACCOUNT ALL=($RUNAS) NOPASSWD:SETENV: /usr/local/bin/rclone, /usr/bin/ls, /usr/bin/mkdir, /usr/bin/env
EOF
visudo -c -q -f "$TMP/sudoers" || die "the sudoers rule does not parse"
install -m 440 -o root -g root "$TMP/sudoers" /etc/sudoers.d/motuz
visudo -c -q || die "sudoers is invalid after installing /etc/sudoers.d/motuz"

# Secret-free AWS SSO configs that the app or worker writes and rclone reads as the user
# (MOTUZ_SSO_CONFIG_DIR): traversable, not listable, owned by the account
install -d -m 711 -o "$ACCOUNT" -g "$(id -gn "$ACCOUNT")" /var/lib/motuz-aws-config

if [ "$WORKER_ONLY" = 1 ]; then
    log "done (worker only)"
    cat <<EOF

Next, as $ACCOUNT (sudo -iu $ACCOUNT): the same Motuz release as the central node in
~/motuz, then ~/.config/motuz-worker/worker.env and the worker credential, and
motuz-worker.service (src/worker/motuz-worker.service): README, "Remote workers (HTTPS only)".
EOF
    exit 0
fi

# ---------------------------------------------------------------- sysctl, PAM
log "sysctl net.ipv4.ip_unprivileged_port_start=80, vm.overcommit_memory=1"
# Redis rewrites its append-only file in a forked child (Redis' own recommendation)
printf 'net.ipv4.ip_unprivileged_port_start = 80\nvm.overcommit_memory = 1\n' > /etc/sysctl.d/60-motuz.conf
sysctl -q -p /etc/sysctl.d/60-motuz.conf

log "/etc/pam.d/motuz"
install -m 644 -o root -g root "$PAM_TEMPLATE" /etc/pam.d/motuz


# ---------------------------------------------------------------- login helper
if [ "$LOCAL_ACCOUNTS" = 1 ]; then
    log "login helper (motuz-auth.socket)"
    install -d -m 755 -o root -g root /usr/local/lib/motuz-auth
    install -m 644 -o root -g root "$REPO_DIR/deployment/systemd/auth-helper/motuz_auth_helper.py" /usr/local/lib/motuz-auth/
    install -m 644 -o root -g root "$REPO_DIR/src/backend/api/utils/pam.py" /usr/local/lib/motuz-auth/pam.py
    GROUP=$(id -gn "$ACCOUNT")
    sed "s/^SocketGroup=motuz$/SocketGroup=$GROUP/" "$REPO_DIR/deployment/systemd/system/motuz-auth.socket" > "$TMP/motuz-auth.socket"
    sed "s/^Environment=MOTUZ_AUTH_CLIENT_USER=motuz$/Environment=MOTUZ_AUTH_CLIENT_USER=$ACCOUNT/" \
        "$REPO_DIR/deployment/systemd/system/motuz-auth@.service" > "$TMP/motuz-auth@.service"
    install -m 644 -o root -g root "$TMP/motuz-auth.socket" "$TMP/motuz-auth@.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable motuz-auth.socket >/dev/null 2>&1
    systemctl restart motuz-auth.socket
elif systemctl is-enabled motuz-auth.socket >/dev/null 2>&1; then
    warn "motuz-auth.socket (from an earlier --local-accounts) stays installed"
fi

log "done"
distro_firewall_hint
cat <<EOF

Next, as $ACCOUNT (sudo -iu $ACCOUNT):
    git clone https://github.com/FredHutch/motuz.git ~/motuz    # or your fork / branch
    ~/motuz/bin/systemd/deploy.sh
EOF
