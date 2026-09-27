# Ubuntu 26.04 LTS specifics of the systemd install (sourced by bin/systemd/_lib.sh
# load_distro, selected by ID in /etc/os-release). Another distribution gets its own
# module with the same variables and functions (distro/amzn.sh: Amazon Linux 2027).
# Everything else in bin/systemd must stay distribution neutral.

DISTRO_NAME="Ubuntu 26.04 LTS"
# PostgreSQL 18 server binaries (postgres, initdb) of the distribution's packages
PG_BINDIR=/usr/lib/postgresql/18/bin
# The Celery broker (Redis protocol): server and client
REDIS_SERVER=/usr/bin/redis-server
REDIS_CLI=/usr/bin/redis-cli
# /etc/pam.d/motuz: authentication and account checks of the distribution's stack
PAM_TEMPLATE="$REPO_DIR/deployment/systemd/pam.d/motuz.ubuntu"
# The packages' own services, masked: Motuz runs its own instances as user services
DISTRO_SERVICES="postgresql.service redis-server.service"

distro_supported() { # after `. /etc/os-release`
    [ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 26.04 ]
}

# Installs the packages. The distribution's own PostgreSQL cluster and Redis service must
# never run: Motuz runs both as user services of its account.
distro_install_packages() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y -q
    # postgresql-common creates and starts a `main` cluster unless told not to, before
    # the server package is installed
    apt-get install -y -q --no-install-recommends postgresql-common
    sed -i -E 's/^#?[[:space:]]*create_main_cluster[[:space:]]*=.*/create_main_cluster = false/' \
        /etc/postgresql-common/createcluster.conf
    grep -q '^create_main_cluster = false' /etc/postgresql-common/createcluster.conf \
        || echo 'create_main_cluster = false' >> /etc/postgresql-common/createcluster.conf
    apt-get install -y -q --no-install-recommends \
        postgresql-18 postgresql-client-18 redis-server \
        nodejs npm build-essential git curl ca-certificates unzip openssl python3 \
        sudo iproute2 libpam-modules
    local unit
    for unit in $DISTRO_SERVICES; do
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
        systemctl mask "$unit" >/dev/null
    done
    if command -v pg_lsclusters >/dev/null && [ -n "$(pg_lsclusters -h 2>/dev/null)" ]; then
        warn "Ubuntu PostgreSQL clusters exist (pg_lsclusters); they are stopped (postgresql.service masked) but kept"
    fi
}

# A remote worker host (install.sh --worker-only): rclone runs as the users, the worker
# (src/worker/motuz_worker.py) with the distribution's python3
distro_install_worker_packages() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y -q
    apt-get install -y -q --no-install-recommends git curl ca-certificates unzip python3 sudo
}

# Only Traefik listens beyond loopback
distro_firewall_hint() {
    echo "Firewall: only 80/tcp and 443/tcp need to be open, e.g. ufw allow 80/tcp && ufw allow 443/tcp && ufw enable"
}
