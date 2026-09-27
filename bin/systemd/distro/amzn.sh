# Amazon Linux 2027 specifics of the systemd install (sourced by bin/systemd/_lib.sh
# load_distro, selected by ID=amzn in /etc/os-release); the same variables and functions
# as distro/ubuntu.sh. README, "Install on Amazon Linux 2027 (default on EC2)".
#
# AL2027 has no RabbitMQ and no Redis packages: the Celery broker is Valkey (Redis
# protocol, the same motuz-redis.service and redis.conf). Its PostgreSQL 18 and Valkey
# packages only provide the binaries here: their own services stay masked, both run as
# user services of the account, as on Ubuntu. SELinux stays enforcing; install.sh
# relabels what it installs (restorecon), no booleans or custom policy are needed.

DISTRO_NAME="Amazon Linux 2027"
# PostgreSQL 18 server binaries (postgresql18-server installs them in /usr/bin)
PG_BINDIR=/usr/bin
# The Celery broker (Redis protocol): Valkey 9, built with systemd notify support
REDIS_SERVER=/usr/bin/valkey-server
REDIS_CLI=/usr/bin/valkey-cli
# /etc/pam.d/motuz: authentication and account checks of the authselect stack
PAM_TEMPLATE="$REPO_DIR/deployment/systemd/pam.d/motuz.amzn"
# The packages' own services, masked: Motuz runs its own instances as user services
DISTRO_SERVICES="postgresql.service valkey.service valkey-sentinel.service"
# The venv's interpreter (bin/systemd/deploy.sh): AL2027's own Python 3.14 instead of uv's
# 3.12 (versions.env). The backend unit tests and the e2e suites pass on it with the
# pinned requirements.txt; it gets the distribution's security updates and SELinux's
# label for system binaries. python3-devel has the headers to build uWSGI.
DISTRO_PYTHON=/usr/bin/python3.14

distro_supported() { # after `. /etc/os-release`
    [ "${ID:-}" = amzn ] && [ "${VERSION_ID:-}" = 2027 ]
}

distro_install_packages() {
    # nodejs24 is Node.js 24 (node/npm via alternatives); python3 (3.14) runs the scripts,
    # the login helper and the venv (DISTRO_PYTHON). hostname and diffutils (cmp) are
    # used by deploy.sh, policycoreutils (restorecon) by install.sh. The distribution's
    # rclone package is never installed: install.sh puts the pinned rclone into
    # /usr/local/bin.
    local pkgs=(postgresql18-server postgresql18 valkey nodejs24 nodejs24-npm
        gcc make git tar gzip unzip openssl python3 python3-devel sudo iproute procps-ng shadow-utils
        util-linux pam policycoreutils hostname diffutils findutils gawk sed ca-certificates)
    # The AMI has curl-minimal, which conflicts with the full curl package; either works
    command -v curl >/dev/null || pkgs+=(curl)
    dnf -y -q install "${pkgs[@]}"
    local unit
    for unit in $DISTRO_SERVICES; do
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
        systemctl mask "$unit" >/dev/null
    done
    # Another nodejs package may own the `node` alternative
    if [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null)" != 24 ]; then
        alternatives --set node /usr/bin/node-24
    fi
    if [ -f /var/lib/pgsql/data/PG_VERSION ]; then
        warn "a PostgreSQL cluster exists in /var/lib/pgsql/data; it is stopped (postgresql.service masked) but kept"
    fi
}

# A remote worker (install.sh --worker-only): motuz_worker.py runs on the system python3
# with the standard library only; rclone is the pinned one
distro_install_worker_packages() {
    local pkgs=(python3 sudo unzip tar gzip shadow-utils util-linux policycoreutils ca-certificates)
    command -v curl >/dev/null || pkgs+=(curl)
    dnf -y -q install "${pkgs[@]}"
}

# Only Traefik listens beyond loopback. EC2 instances have no host firewall by default.
distro_firewall_hint() {
    echo "Firewall: only 80/tcp and 443/tcp need to be open: on EC2 in the instance's security group; with firewalld: firewall-cmd --permanent --add-service=http --add-service=https && firewall-cmd --reload"
}
