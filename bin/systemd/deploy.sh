#!/usr/bin/env bash
# Builds and (re)starts Motuz as `systemd --user` services of the motuz account (README,
# "Install without Docker (Ubuntu 26.04)"). Run as that account from its checkout
# ~/motuz, after bin/systemd/install.sh (as root), on the first deploy and after every
# update:
#     sudo -iu motuz ~/motuz/bin/systemd/deploy.sh [--pull]
#   --pull   git pull --ff-only first
#
# Steps: the Python venv (~/motuz/venv: Python and requirements.txt by uv), the frontend
# (npm ci, npm run build), ~/.config/motuz/{motuz.env,secrets.env} on the first deploy
# (never overwritten; secrets added by an update are generated), a self-signed
# certificate if there is none, Redis' and Traefik's generated configuration, the
# PostgreSQL cluster ~/data/pg with the role and database, the units in
# ~/.config/systemd/user, a restart of motuz.target and a health check.

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

PULL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --pull) PULL=1 ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1" ;;
    esac
    shift
done

[ "$(id -u)" != 0 ] || die "run as the motuz account (sudo -iu motuz), not as root"
motuz_paths "$HOME"
[ "$REPO_DIR" = "$CHECKOUT" ] || die "the checkout must be $CHECKOUT (the units use that path), not $REPO_DIR"
load_distro
[ -x /usr/local/bin/uv ] && [ -x "$PG_BINDIR/postgres" ] && [ -x "$REDIS_SERVER" ] \
    || die "run bin/systemd/install.sh as root first"
# sudo -iu does not start a login session: find this account's systemd user manager
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
systemctl --user show-environment >/dev/null 2>&1 \
    || die "no systemd user manager for $(id -un) (install.sh runs loginctl enable-linger)"
load_versions

if [ "$PULL" = 1 ]; then
    log "git pull"
    git -C "$CHECKOUT" pull --ff-only
fi

# Things install.sh (root) installed from an older checkout
if [ "$(/usr/local/bin/rclone version 2>/dev/null | head -1)" != "rclone v${RCLONE_VERSION}" ]; then
    warn "/usr/local/bin/rclone is not v${RCLONE_VERSION}: run bin/systemd/install.sh again as root"
fi
/usr/local/bin/traefik version 2>/dev/null | grep -qE "^Version:[[:space:]]+${TRAEFIK_VERSION}$" \
    || warn "/usr/local/bin/traefik is not ${TRAEFIK_VERSION}: run bin/systemd/install.sh again as root"
cmp -s "$PAM_TEMPLATE" /etc/pam.d/motuz \
    || warn "/etc/pam.d/motuz differs from the checkout: run bin/systemd/install.sh again as root"
if [ -f /usr/local/lib/motuz-auth/motuz_auth_helper.py ]; then
    cmp -s "$REPO_DIR/deployment/systemd/auth-helper/motuz_auth_helper.py" /usr/local/lib/motuz-auth/motuz_auth_helper.py \
        && cmp -s "$REPO_DIR/src/backend/api/utils/pam.py" /usr/local/lib/motuz-auth/pam.py \
        || warn "the login helper differs from the checkout: run bin/systemd/install.sh --local-accounts again as root"
fi

# ---------------------------------------------------------------- Python
# uv's Python $PYTHON_VERSION (versions.env), or the distribution's own interpreter when
# its module sets DISTRO_PYTHON (Amazon Linux 2027: /usr/bin/python3.14)
export UV_NO_PROGRESS=1
if [ -n "${DISTRO_PYTHON:-}" ]; then
    [ -x "$DISTRO_PYTHON" ] || die "$DISTRO_PYTHON not found: run bin/systemd/install.sh again as root"
    export UV_PYTHON_PREFERENCE=only-system
    PYTHON="$DISTRO_PYTHON"
    PYTHON_VERSION=$("$DISTRO_PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
else
    export UV_PYTHON_PREFERENCE=only-managed
    PYTHON="$PYTHON_VERSION"
    /usr/local/bin/uv python install --quiet "$PYTHON_VERSION"
fi
log "Python ${PYTHON_VERSION} venv and requirements.txt (uv ${UV_VERSION})"
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c "import sys; sys.exit(sys.version_info[:2] != tuple(map(int, '$PYTHON_VERSION'.split('.'))))" 2>/dev/null; then
    rm -rf "$VENV"
    /usr/local/bin/uv venv --quiet --python "$PYTHON" "$VENV"
fi
# uWSGI is built from source here, as in the docker image (build-essential)
/usr/local/bin/uv pip install --quiet --python "$VENV/bin/python" -r "$CHECKOUT/requirements.txt"

# ---------------------------------------------------------------- frontend
log "frontend (npm ci, npm run build)"
(cd "$CHECKOUT" && npm ci --no-audit --no-fund --loglevel=error && npm run build --silent) \
    || die "frontend build failed"
[ -f "$CHECKOUT/build/index.html" ] || die "no build/index.html after npm run build"

# ---------------------------------------------------------------- settings and secrets
log "settings and secrets in $CONFIG_DIR"
install -d -m 700 "$CONFIG_DIR" "$DATA_DIR" "$DATA_DIR/redis" "$ACME_DIR" "$CERTS_DIR"
if [ ! -f "$SETTINGS" ]; then
    (umask 077 && cp "$REPO_DIR/deployment/systemd/motuz.env.example" "$SETTINGS")
    if [ -S /run/motuz-auth.sock ]; then
        env_set "$SETTINGS" MOTUZ_AUTH_HELPER /run/motuz-auth.sock
    fi
    echo "    created $SETTINGS (edit it, then run deploy.sh again)"
fi
[ -f "$SECRETS" ] || (umask 077 && : > "$SECRETS")
chmod 600 "$SECRETS"
for key in $REQUIRED_SECRETS; do # generated once (also when an update adds one)
    [ -n "$(env_get "$SECRETS" "$key")" ] || env_set "$SECRETS" "$key" "$(openssl rand -hex 32)"
done
for key in $OPTIONAL_SECRETS; do
    grep -q "^$key=" "$SECRETS" || env_set "$SECRETS" "$key" ""
done

# Redis: the unix socket only (in the private runtime directory), with a password
REDIS_PASSWORD=$(env_get "$SECRETS" MOTUZ_REDIS_PASSWORD)
{
    echo "# Generated by bin/systemd/deploy.sh; do not edit"
    echo "supervised systemd"
    echo "daemonize no"
    echo 'logfile ""'
    echo "port 0"
    echo "unixsocket $XDG_RUNTIME_DIR/motuz-redis/redis.sock"
    echo "unixsocketperm 700"
    echo "requirepass $REDIS_PASSWORD"
    echo "dir $DATA_DIR/redis"
    echo "appendonly yes"
    echo 'save ""'
} > "$CONFIG_DIR/redis.conf.new"
chmod 600 "$CONFIG_DIR/redis.conf.new"
mv "$CONFIG_DIR/redis.conf.new" "$CONFIG_DIR/redis.conf"
(umask 077 && env_line MOTUZ_CELERY_BROKER_URL \
    "redis+socket://:${REDIS_PASSWORD}@${XDG_RUNTIME_DIR}/motuz-redis/redis.sock" > "$BROKER_ENV")

# Traefik's static configuration: the `command` of the traefik service in
# docker-compose.yml, with this install's paths and MOTUZ_ACME_* from motuz.env
python3 -I -S "$REPO_DIR/bin/systemd/traefik_args.py" "$REPO_DIR/docker-compose.yml" \
    "$REPO_DIR/deployment/docker/traefik/dynamic" "$ACME_DIR/acme.json" \
    "$(env_get "$SETTINGS" MOTUZ_ACME_EMAIL)" "$(env_get "$SETTINGS" MOTUZ_ACME_CA_SERVER)" > "$TRAEFIK_ENV.new"
mv "$TRAEFIK_ENV.new" "$TRAEFIK_ENV"

if [ -z "$(env_get "$SETTINGS" MOTUZ_ACME_DOMAIN)" ] && { [ ! -f "$CERTS_DIR/cert.crt" ] || [ ! -f "$CERTS_DIR/cert.key" ]; }; then
    log "self-signed certificate in $CERTS_DIR (replace cert.crt/cert.key, then: systemctl --user restart motuz-traefik)"
    host=$(hostname -f 2>/dev/null || hostname)
    (umask 077 && openssl req -x509 -newkey rsa:4096 -nodes -days 825 -subj "/CN=$host" \
        -addext "subjectAltName=DNS:$host,DNS:localhost,IP:127.0.0.1" \
        -keyout "$CERTS_DIR/cert.key" -out "$CERTS_DIR/cert.crt" 2>/dev/null)
fi

# ---------------------------------------------------------------- units
log "units in ~/.config/systemd/user"
install -d -m 755 "$HOME/.config/systemd/user"
install_units "$HOME/.config/systemd/user"
systemctl --user daemon-reload
systemctl --user enable --quiet motuz.target

# ---------------------------------------------------------------- database
DB_NAME=$(env_get "$SETTINGS" MOTUZ_DATABASE_NAME)
DB_USER=$(env_get "$SETTINGS" MOTUZ_DATABASE_USER)
DB_HOST=$(env_get "$SETTINGS" MOTUZ_DATABASE_HOST)
if [ "$DB_HOST" = 127.0.0.1:5432 ]; then
    if [ ! -f "$PGDATA_DIR/PG_VERSION" ]; then
        log "PostgreSQL cluster $PGDATA_DIR"
        # Peer logins of the superuser (this account) on the unix socket, passwords (scram)
        # on TCP. The locale only sorts text; a restore (migrate_from_docker.sh) rebuilds indexes.
        "$PG_BINDIR/initdb" -D "$PGDATA_DIR" --username="$(id -un)" --encoding=UTF8 \
            --locale=C.UTF-8 --auth-local=peer --auth-host=scram-sha-256 >/dev/null
    fi
    systemctl --user start motuz-postgres.service
    # The password goes through the environment, never the command line (ps)
    MOTUZ_DB_PASSWORD="$(env_get "$SECRETS" MOTUZ_DATABASE_PASSWORD)" \
        psql -X -q -v ON_ERROR_STOP=1 -h "$XDG_RUNTIME_DIR/motuz-postgres" -d postgres -v db="$DB_NAME" -v user="$DB_USER" <<'EOSQL'
\getenv pw MOTUZ_DB_PASSWORD
SELECT format('CREATE ROLE %I LOGIN', :'user') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'user') \gexec
ALTER ROLE :"user" WITH LOGIN PASSWORD :'pw';
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'user') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
ALTER DATABASE :"db" OWNER TO :"user";
EOSQL
else
    echo "    MOTUZ_DATABASE_HOST=$DB_HOST: an external database, no local cluster"
fi

# ---------------------------------------------------------------- start and check
log "restarting motuz.target"
systemctl --user restart motuz.target

log "health check"
healthy=0
for _ in $(seq 90); do
    if curl -skf --max-time 5 https://127.0.0.1/api/system/info/ 2>/dev/null | grep -q healthy; then
        healthy=1
        break
    fi
    sleep 2
done
systemctl --user --no-pager --plain list-units 'motuz*' || true
[ "$healthy" = 1 ] || die "https://127.0.0.1/api/system/info/ is not healthy; logs: sudo journalctl _SYSTEMD_USER_UNIT=motuz-app.service"
for unit in motuz-postgres motuz-redis motuz-app motuz-celery motuz-traefik; do
    systemctl --user is-active --quiet "$unit" || die "$unit is not running; logs: sudo journalctl _SYSTEMD_USER_UNIT=$unit.service"
done
echo "Motuz is up: https://$(hostname -f 2>/dev/null || hostname)/"
