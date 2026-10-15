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
# The packages' own services. Motuz uses them by default (DEFAULT_*: install.sh
# --system-postgres, --system-redis); with --private-postgres / --private-redis they
# stay masked and Motuz runs its own instances as user services of its account.
DISTRO_SERVICES="postgresql.service redis-server.service"
DISTRO_PG_SERVICE=postgresql.service
DISTRO_REDIS_SERVICE=redis-server.service
DEFAULT_POSTGRES=system
DEFAULT_REDIS=system
# The system Redis: socket, group of the socket (mode 770), unit and its main config
SYSTEM_REDIS_SOCKET=/run/redis/redis-server.sock
SYSTEM_REDIS_GROUP=redis
SYSTEM_REDIS_CONF=/etc/redis/redis.conf
SYSTEM_REDIS_BINARY=/usr/bin/redis-server

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
    mask_private_services
    if [ "$POSTGRES_MODE" = private ] && command -v pg_lsclusters >/dev/null && [ -n "$(pg_lsclusters -h 2>/dev/null)" ]; then
        warn "Ubuntu PostgreSQL clusters exist (pg_lsclusters); they are stopped (postgresql.service masked) but kept"
    fi
}

# --system-postgres: the distribution's cluster 18/main, created if missing, enabled and
# running (install.sh does the rest: role, database, pg_hba, listen_addresses)
distro_system_postgres_prepare() {
    systemctl unmask postgresql.service >/dev/null 2>&1 || true
    if ! pg_lsclusters -h 2>/dev/null | awk '$1 == 18 && $2 == "main" { found = 1 } END { exit !found }'; then
        log "pg_createcluster 18 main"
        pg_createcluster 18 main -e UTF8 --locale=C.UTF-8 >/dev/null || die "pg_createcluster 18 main failed"
    fi
    systemctl daemon-reload
    systemctl enable postgresql@18-main.service >/dev/null 2>&1 || true
    systemctl start postgresql@18-main.service || die "postgresql@18-main.service does not start (journalctl -u postgresql@18-main)"
    SYSTEM_PG_UNIT=postgresql@18-main.service
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
