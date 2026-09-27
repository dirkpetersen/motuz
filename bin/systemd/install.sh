#!/usr/bin/env bash
# Prepares a machine (a VM, an EC2 instance or a privileged LXC) with Ubuntu 26.04 LTS or
# Amazon Linux 2027 (the modules in bin/systemd/distro) for Motuz without docker:
# `systemd --user` services of one dedicated account (README, "Install without Docker
# (Ubuntu 26.04)" and "Install on Amazon Linux 2027 (default on EC2)"). Run as root,
# once; it is idempotent, so run it again after updates that change what it installs
# (bin/systemd/deploy.sh warns about that). bin/systemd/uninstall.sh undoes it.
#
# Usage: sudo bin/systemd/install.sh [--local-accounts] [--sudo-group=GROUP] [--user=NAME] [--home=DIR]
#        sudo bin/systemd/install.sh --worker-only [--central-url=URL] [--pool=NAME]
#                                    [--credential-file=FILE] [--sudo-group=GROUP] [--user=NAME] [--home=DIR]
#   --local-accounts    install the login helper (motuz-auth.socket), needed when users
#                       log in with local /etc/shadow accounts (not for SSSD/Kerberos)
#   --sudo-group=GROUP  Motuz may act only as members of GROUP (default: any user but root)
#   --user=NAME         the service account (default motuz)
#   --home=DIR          its home directory, on local disk (default /var/lib/motuz)
#   --worker-only       a remote worker (README, "Remote workers (HTTPS only)"), no server:
#                       python3, sudo, rclone, the account and its sudoers rule, and the
#                       user unit motuz-worker.service with ~/.config/motuz-worker/worker.env
#                       (MOTUZ_HOME = this checkout, which must stay readable by the
#                       account, e.g. a root-owned clone in /opt/motuz)
#   --central-url=URL   worker: MOTUZ_CENTRAL_URL (https://...) in worker.env
#   --pool=NAME         worker: MOTUZ_WORKER_POOL in worker.env
#   --credential-file=FILE  worker: the secret of `manage.py workers add`; installed as
#                       ~/.config/motuz-worker/credential (mode 600), then motuz-worker is
#                       enabled and started. Without it: nothing runs (a temporary worker
#                       runs one job with bin/systemd/worker_once.sh)
#
# What it does:
#   - packages (bin/systemd/distro/<ID>.sh): PostgreSQL 18 (the distribution's own
#     cluster and service never run), Redis or Valkey (its service masked), Node.js/npm,
#     build tools, git, curl, openssl (--worker-only: python3, sudo, curl, unzip)
#   - pinned rclone, Traefik and uv in /usr/local/bin, SHA256 checked (--worker-only: rclone)
#   - the account, `loginctl enable-linger`, its sudoers rule (checked with visudo -c)
#   - sysctl: net.ipv4.ip_unprivileged_port_start=80 (Traefik is a user service on
#     :80/:443), vm.overcommit_memory=1 (Redis) (not with --worker-only)
#   - /etc/pam.d/motuz (not with --worker-only), /var/lib/motuz-aws-config (AWS SSO
#     configs for rclone)
#   - with SELinux (Amazon Linux: enforcing): restorecon of all of the above, so every
#     file has its default label; no booleans or policy modules
# Then, as the account: clone the repository to ~/motuz and run bin/systemd/deploy.sh
# (bin/systemd/bootstrap.sh does all of it in one step).

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

LOCAL_ACCOUNTS=0
SUDO_GROUP=""
ACCOUNT=motuz
HOME_DIR=/var/lib/motuz
WORKER_ONLY=0
CENTRAL_URL=""
POOL=""
CREDENTIAL_FILE=""
while [ $# -gt 0 ]; do
    opt="$1"; value=""
    case "$opt" in
        --*=*) value="${opt#*=}"; opt="${opt%%=*}" ;;
        --sudo-group|--user|--home|--central-url|--pool|--credential-file) value="${2:?$1 needs a value}"; shift ;;
    esac
    case "$opt" in
        --local-accounts) LOCAL_ACCOUNTS=1 ;;
        --sudo-group) SUDO_GROUP="$value" ;;
        --user) ACCOUNT="$value" ;;
        --home) HOME_DIR="$value" ;;
        --worker-only) WORKER_ONLY=1 ;;
        --central-url) CENTRAL_URL="$value" ;;
        --pool) POOL="$value" ;;
        --credential-file) CREDENTIAL_FILE="$value" ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1" ;;
    esac
    shift
done

[ "$(id -u)" = 0 ] || die "run as root"
[[ "$ACCOUNT" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "invalid account name $ACCOUNT"
[[ "$HOME_DIR" = /* ]] || die "--home must be absolute"
[ -z "$SUDO_GROUP" ] || getent group "$SUDO_GROUP" >/dev/null || die "group $SUDO_GROUP does not exist"
if [ "$WORKER_ONLY" = 1 ]; then
    [ "$LOCAL_ACCOUNTS" = 0 ] || die "--local-accounts is for the server, not --worker-only"
    [ -z "$CENTRAL_URL" ] || [[ "$CENTRAL_URL" =~ ^https://[A-Za-z0-9.:/_-]+$ ]] || die "--central-url must be an https:// address"
    [ -z "$POOL" ] || [[ "$POOL" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid pool name $POOL"
    [ -z "$CREDENTIAL_FILE" ] || [ -s "$CREDENTIAL_FILE" ] || die "$CREDENTIAL_FILE is missing or empty"
    [ -f "$REPO_DIR/src/worker/motuz_worker.py" ] || die "no src/worker/motuz_worker.py in $REPO_DIR"
elif [ -n "$CENTRAL_URL$POOL$CREDENTIAL_FILE" ]; then
    die "--central-url, --pool and --credential-file need --worker-only"
fi
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
GROUP=$(id -gn "$ACCOUNT")
chmod 750 "$HOME_DIR"
loginctl enable-linger "$ACCOUNT"
systemctl start "user@$(id -u "$ACCOUNT").service"

# ---------------------------------------------------------------- sudoers
# The only privilege of the account: run rclone, ls, mkdir and env (the file viewer and the
# credential readers run python3 through env) as the logged-in user, never as root.
# SETENV: rclone gets the connection's credentials as RCLONE_CONFIG_* variables
# (sudo --preserve-env=<names>). A worker uses the same rule (rclone as the job's owner).
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

# ---------------------------------------------------------------- sysctl, PAM, SSO dir
if [ "$WORKER_ONLY" = 0 ]; then
    log "sysctl net.ipv4.ip_unprivileged_port_start=80, vm.overcommit_memory=1"
    # Redis rewrites its append-only file in a forked child (Redis' own recommendation)
    printf 'net.ipv4.ip_unprivileged_port_start = 80\nvm.overcommit_memory = 1\n' > /etc/sysctl.d/60-motuz.conf
    sysctl -q -p /etc/sysctl.d/60-motuz.conf

    log "/etc/pam.d/motuz"
    install -m 644 -o root -g root "$PAM_TEMPLATE" /etc/pam.d/motuz
fi

# Secret-free AWS SSO configs that the app writes and rclone reads as the user
# (MOTUZ_SSO_CONFIG_DIR): traversable, not listable, owned by the account
install -d -m 711 -o "$ACCOUNT" -g "$GROUP" /var/lib/motuz-aws-config

# ---------------------------------------------------------------- login helper
if [ "$LOCAL_ACCOUNTS" = 1 ]; then
    log "login helper (motuz-auth.socket)"
    install -d -m 755 -o root -g root /usr/local/lib/motuz-auth
    install -m 644 -o root -g root "$REPO_DIR/deployment/systemd/auth-helper/motuz_auth_helper.py" /usr/local/lib/motuz-auth/
    install -m 644 -o root -g root "$REPO_DIR/src/backend/api/utils/pam.py" /usr/local/lib/motuz-auth/pam.py
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

# ---------------------------------------------------------------- worker
if [ "$WORKER_ONLY" = 1 ]; then
    log "worker: ~/.config/motuz-worker, motuz-worker.service (MOTUZ_HOME=$REPO_DIR)"
    # The code runs from this checkout (root's clone): the account reads it, never writes it
    runuser -u "$ACCOUNT" -- test -r "$REPO_DIR/src/worker/motuz_worker.py" \
        || die "$ACCOUNT cannot read $REPO_DIR: clone the repository to a readable place, e.g. /opt/motuz"
    WORKER_DIR="$HOME_DIR/.config/motuz-worker"
    WORKER_ENV="$WORKER_DIR/worker.env"
    install -d -m 755 -o "$ACCOUNT" -g "$GROUP" "$HOME_DIR/.config" "$HOME_DIR/.config/systemd" "$HOME_DIR/.config/systemd/user"
    install -d -m 700 -o "$ACCOUNT" -g "$GROUP" "$WORKER_DIR"
    if [ ! -f "$WORKER_ENV" ]; then
        install -m 600 -o "$ACCOUNT" -g "$GROUP" "$REPO_DIR/src/worker/worker.env.example" "$WORKER_ENV"
        # No mounts are required unless configured (a cloud worker has none)
        env_set "$WORKER_ENV" MOTUZ_REQUIRED_PATHS ""
    fi
    env_set "$WORKER_ENV" MOTUZ_HOME "$REPO_DIR"
    env_set "$WORKER_ENV" MOTUZ_WORKER_CREDENTIAL_FILE "$WORKER_DIR/credential"
    env_set "$WORKER_ENV" MOTUZ_SSO_CONFIG_DIR /var/lib/motuz-aws-config
    [ -z "$CENTRAL_URL" ] || env_set "$WORKER_ENV" MOTUZ_CENTRAL_URL "$CENTRAL_URL"
    [ -z "$POOL" ] || env_set "$WORKER_ENV" MOTUZ_WORKER_POOL "$POOL"
    chown "$ACCOUNT:$GROUP" "$WORKER_ENV"
    install -m 644 -o "$ACCOUNT" -g "$GROUP" "$REPO_DIR/src/worker/motuz-worker.service" "$HOME_DIR/.config/systemd/user/motuz-worker.service"
    if [ -n "$CREDENTIAL_FILE" ]; then
        install -m 600 -o "$ACCOUNT" -g "$GROUP" "$CREDENTIAL_FILE" "$WORKER_DIR/credential"
    fi
fi

# ---------------------------------------------------------------- SELinux
# Default labels for what this script wrote. The account's services are unconfined user
# services (unconfined_u, like a login of the account), PostgreSQL and Valkey included:
# they need no booleans or policy modules.
if command -v selinuxenabled >/dev/null && selinuxenabled; then
    log "SELinux ($(getenforce)): restorecon"
    for f in /usr/local/bin/rclone /usr/local/bin/traefik /usr/local/bin/uv /etc/sudoers.d/motuz \
            /etc/sysctl.d/60-motuz.conf /etc/pam.d/motuz /etc/systemd/system/motuz-auth.socket \
            /etc/systemd/system/motuz-auth@.service; do
        [ ! -e "$f" ] || restorecon -F "$f"
    done
    restorecon -RF /var/lib/motuz-aws-config "$HOME_DIR"
    [ ! -d /usr/local/lib/motuz-auth ] || restorecon -RF /usr/local/lib/motuz-auth
fi

# ---------------------------------------------------------------- next steps
as_account_systemctl() {
    runuser -u "$ACCOUNT" -- env XDG_RUNTIME_DIR="/run/user/$(id -u "$ACCOUNT")" systemctl --user "$@"
}
if [ "$WORKER_ONLY" = 1 ]; then
    as_account_systemctl daemon-reload
    if [ -f "$WORKER_DIR/credential" ] && [ -n "$(env_get "$WORKER_ENV" MOTUZ_CENTRAL_URL | grep -v example.org)" ]; then
        log "starting motuz-worker"
        as_account_systemctl enable --quiet motuz-worker.service
        as_account_systemctl restart motuz-worker.service
        echo "    logs: journalctl _SYSTEMD_USER_UNIT=motuz-worker.service -f"
    else
        cat <<EOF
Worker installed, not started. Set MOTUZ_CENTRAL_URL (and the mounts) in $WORKER_ENV, then
  - a permanent worker: run this again with --credential-file=FILE (the secret printed by
    \`manage.py workers add NAME --pool POOL\` on the server), or
  - a temporary worker: sudo $REPO_DIR/bin/systemd/worker_once.sh --bootstrap-token-file=FILE
EOF
    fi
    log "done"
    exit 0
fi

log "done"
distro_firewall_hint
cat <<EOF

Next, as $ACCOUNT (sudo -iu $ACCOUNT):
    git clone https://github.com/FredHutch/motuz.git ~/motuz    # or your fork / branch
    ~/motuz/bin/systemd/deploy.sh
EOF
