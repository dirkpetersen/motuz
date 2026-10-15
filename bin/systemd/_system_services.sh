# Sourced by bin/systemd/install.sh (root): the choice between the account's private
# PostgreSQL cluster / Redis and the distribution's own services, and the setup of the
# system ones (README, "Why a private PostgreSQL cluster?" and "Root steps without
# install.sh"). Needs _lib.sh, the distro module, and ACCOUNT, GROUP-less HOME_DIR.
#
# What the system modes leave for bin/systemd/deploy.sh (run as the account) is the
# handoff file ~/.config/motuz/handoff.env (mode 600, owned by the account): the
# generated passwords and the resulting settings. deploy.sh merges it into motuz.env and
# secrets.env and deletes it, so the passwords never appear on a command line or in a
# log, and the account reads them without root.

# Reads the mode of an existing install (or "unset"), the explicit options, and sets
# POSTGRES_MODE and REDIS_MODE; PG_OPTION / REDIS_OPTION are "", private or system.
resolve_service_modes() {
    local home settings existing_pg=unset existing_redis=unset
    home=$(getent passwd "$ACCOUNT" | cut -d: -f6 || true)
    home="${home:-$HOME_DIR}"
    settings="$home/.config/motuz/motuz.env"
    if [ -f "$settings" ]; then
        existing_pg=$(database_mode "$settings" "$home/data/pg")
        existing_redis=$(broker_mode "$settings" "$home/.config/motuz")
    else
        [ ! -f "$home/data/pg/PG_VERSION" ] || existing_pg=private
        [ ! -f "$home/.config/motuz/redis.conf" ] || existing_redis=private
    fi
    EXISTING_PG="$existing_pg"
    EXISTING_REDIS="$existing_redis"
    POSTGRES_MODE=$(pick_mode PostgreSQL MOTUZ_DATABASE_MODE "$PG_OPTION" "$existing_pg" "$DEFAULT_POSTGRES")
    REDIS_MODE=$(pick_mode Redis MOTUZ_BROKER_MODE "$REDIS_OPTION" "$existing_redis" "$DEFAULT_REDIS")
    if [ "$REDIS_MODE" = system ] && [ -z "$SYSTEM_REDIS_SOCKET" ]; then
        die "$DISTRO_NAME has no system Redis mode (--private-redis is the only one)"
    fi
}

pick_mode() { # name, setting, option, existing mode, distribution default
    local name="$1" setting="$2" option="$3" existing="$4" default="$5"
    if [ "$existing" = external ]; then
        [ -z "$option" ] || die "$name: this install uses an external database (MOTUZ_DATABASE_HOST), not the --$option option"
        echo external; return
    fi
    if [ "$existing" = unset ]; then echo "${option:-$default}"; return; fi
    if [ -z "$option" ] || [ "$option" = "$existing" ]; then echo "$existing"; return; fi
    if [ "$existing" = system ]; then
        die "$name already runs as a system service; to go back to a private instance set $setting=private in ~/.config/motuz/motuz.env (README, \"Why a private PostgreSQL cluster?\")"
    fi
    echo "$option" # private -> system, asked for explicitly
}

# Creates the handoff file on first use
handoff_init() {
    HANDOFF="$HOME_DIR/.config/motuz/handoff.env"
    install -d -m 755 -o "$ACCOUNT" -g "$GROUP" "$HOME_DIR/.config"
    install -d -m 700 -o "$ACCOUNT" -g "$GROUP" "$HOME_DIR/.config/motuz"
    [ -f "$HANDOFF" ] || install -m 600 -o "$ACCOUNT" -g "$GROUP" /dev/null "$HANDOFF"
}
handoff_set() { # key value
    env_set "$HANDOFF" "$1" "$2"
    chown "$ACCOUNT:$GROUP" "$HANDOFF"
}

# The password of a system service: the handoff file's, else the installed one if that
# service already runs in system mode, else a new random one
service_password() { # key, current mode of that service
    local pw
    pw=$(env_get "$HANDOFF" "$1")
    [ -n "$pw" ] || { [ "$2" != system ] || pw=$(env_get "$HOME_DIR/.config/motuz/secrets.env" "$1"); }
    [ -n "$pw" ] || pw=$(openssl rand -hex 32)
    printf '%s' "$pw"
}

# SYSTEM_PG_PORT: the cluster's port when it is not the default (the unix socket is named after it)
SYSTEM_PG_PORT=""
as_postgres() { # command... (the postgres superuser of the system cluster, peer login)
    (cd / && runuser -u postgres -- env ${SYSTEM_PG_PORT:+PGPORT=$SYSTEM_PG_PORT} "$@")
}

# ---------------------------------------------------------------- PostgreSQL
# The distribution's cluster (a distro_system_postgres_prepare), localhost only, role
# motuz_user (scram) and database motuz owned by it, pg_hba lines for that role on
# 127.0.0.1 and ::1 only (scram), and the handoff for deploy.sh.
setup_system_postgres() {
    local db=motuz dbuser=motuz_user port listen hba password
    log "PostgreSQL: the distribution's service (system mode)"
    distro_system_postgres_prepare
    # the port of an earlier run (the service moves from 5432 while the private cluster exists)
    SYSTEM_PG_PORT=$(env_get "$HOME_DIR/.config/motuz/handoff.env" MOTUZ_DATABASE_HOST)
    [ "$EXISTING_PG" != system ] || SYSTEM_PG_PORT=$(env_get "$HOME_DIR/.config/motuz/motuz.env" MOTUZ_DATABASE_HOST)
    SYSTEM_PG_PORT="${SYSTEM_PG_PORT##*:}"
    [ "$SYSTEM_PG_PORT" != 5432 ] || SYSTEM_PG_PORT=""
    for _ in $(seq 60); do
        as_postgres psql -X -q -d postgres -tAc 'SELECT 1' >/dev/null 2>&1 && break
        sleep 1
    done
    as_postgres psql -X -q -d postgres -tAc 'SELECT 1' >/dev/null 2>&1 || die "cannot connect to the system PostgreSQL as postgres"
    port=$(as_postgres psql -X -d postgres -tAc 'SHOW port')
    if [ "$EXISTING_PG" = private ] && [ "$port" = 5432 ]; then
        # the private cluster holds 127.0.0.1:5432 until migrate_postgres.sh has moved its data
        warn "the private cluster of $ACCOUNT uses 127.0.0.1:5432: the system PostgreSQL moves to port 5433"
        as_postgres psql -X -q -d postgres -c "ALTER SYSTEM SET port = 5433"
        systemctl restart "$SYSTEM_PG_UNIT"
        port=5433
        SYSTEM_PG_PORT=5433
        for _ in $(seq 60); do as_postgres psql -X -q -d postgres -tAc 'SELECT 1' >/dev/null 2>&1 && break; sleep 1; done
    fi
    listen=$(as_postgres psql -X -d postgres -tAc 'SHOW listen_addresses')
    if printf '%s' "$listen" | tr ',' '\n' | sed 's/ //g' | grep -qvE "^('?localhost'?|127\.0\.0\.1|::1)$"; then
        warn "listen_addresses = '$listen': setting it to 'localhost' (ALTER SYSTEM)"
        as_postgres psql -X -q -d postgres -c "ALTER SYSTEM SET listen_addresses = 'localhost'"
        systemctl restart "$SYSTEM_PG_UNIT"
        for _ in $(seq 60); do as_postgres psql -X -q -d postgres -tAc 'SELECT 1' >/dev/null 2>&1 && break; sleep 1; done
    fi

    # pg_hba.conf: first match wins, so our lines go to the top (once, between markers)
    hba=$(as_postgres psql -X -d postgres -tAc 'SHOW hba_file')
    if ! grep -q '^# BEGIN motuz ' "$hba"; then
        {
            echo "# BEGIN motuz (bin/systemd/install.sh --system-postgres): the Motuz role, loopback only, scram"
            echo "host $db $dbuser 127.0.0.1/32 scram-sha-256"
            echo "host $db $dbuser ::1/128 scram-sha-256"
            echo "host all $dbuser 0.0.0.0/0 reject"
            echo "host all $dbuser ::/0 reject"
            echo "# END motuz"
            cat "$hba"
        } > "$TMP/pg_hba.conf"
        cat "$TMP/pg_hba.conf" > "$hba" # keeps the owner, mode and SELinux label
    fi

    handoff_init
    password=$(service_password MOTUZ_DATABASE_PASSWORD "$EXISTING_PG")
    # The password (hex) goes in through stdin, never through a command line
    { printf '\\set pw %s\n' "'$password'"; cat <<'EOSQL'
SELECT format('CREATE ROLE %I LOGIN', :'user') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'user') \gexec
ALTER ROLE :"user" WITH LOGIN PASSWORD :'pw';
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'user') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
ALTER DATABASE :"db" OWNER TO :"user";
REVOKE CONNECT ON DATABASE :"db" FROM PUBLIC;
SELECT pg_reload_conf();
EOSQL
    } | as_postgres psql -X -q -v ON_ERROR_STOP=1 -d postgres -v db="$db" -v user="$dbuser" >/dev/null \
        || die "could not create the role $dbuser and the database $db"
    handoff_set MOTUZ_DATABASE_MODE system
    handoff_set MOTUZ_DATABASE_HOST "127.0.0.1:$port"
    handoff_set MOTUZ_DATABASE_PASSWORD "$password"
    echo "    role $dbuser and database $db on 127.0.0.1:$port (service $SYSTEM_PG_UNIT); the password is in $HANDOFF until deploy.sh moves it into secrets.env"
}

# ---------------------------------------------------------------- Redis
# The distribution's redis-server: a separate config (/etc/redis/motuz.conf) that
# includes the package's own and overrides it (no TCP: port 0; the unix socket, mode 770,
# group redis; append-only file; the default user off; ACL user motuz with a password
# hash and only the commands a Celery broker needs), selected by a systemd drop-in, so
# the package's redis.conf is never modified. The account joins the group redis.
# An ACL user rather than requirepass: the default user's password would give every
# command to whoever has it; the ACL denies @admin, @dangerous (CONFIG, SHUTDOWN, KEYS,
# FLUSHALL, DEBUG, ...) and scripting except EVAL, EVALSHA and SCRIPT LOAD, which
# kombu needs (its redis-py lock is released with a script; commands inside a script are
# still checked against this ACL), and the password is stored as a SHA-256 hash.
setup_system_redis() {
    local password hash conf=/etc/redis/motuz.conf
    log "Redis: the distribution's service (system mode, unix socket $SYSTEM_REDIS_SOCKET)"
    handoff_init
    password=$(service_password MOTUZ_REDIS_PASSWORD "$EXISTING_REDIS")
    hash=$(printf '%s' "$password" | sha256sum | cut -d' ' -f1)
    cat > "$TMP/motuz-redis.conf" <<EOF
# Generated by bin/systemd/install.sh --system-redis; do not edit (re-run install.sh)
include $SYSTEM_REDIS_CONF
port 0
unixsocket $SYSTEM_REDIS_SOCKET
unixsocketperm 770
appendonly yes
save ""
user default off
user motuz on #$hash ~* &* +@all -@admin -@dangerous -@scripting +eval +evalsha +script|load +info +client|setname +client|setinfo +client|id
EOF
    install -m 640 -o root -g "$SYSTEM_REDIS_GROUP" "$TMP/motuz-redis.conf" "$conf"
    install -d -m 755 /etc/systemd/system/redis-server.service.d
    cat > /etc/systemd/system/redis-server.service.d/motuz.conf <<EOF
# bin/systemd/install.sh --system-redis: Motuz's configuration, which includes the package's
[Service]
ExecStart=
ExecStart=$SYSTEM_REDIS_BINARY $conf --supervised systemd --daemonize no
EOF
    systemctl unmask "$DISTRO_REDIS_SERVICE" >/dev/null 2>&1 || true
    systemctl daemon-reload
    systemctl enable "$DISTRO_REDIS_SERVICE" >/dev/null 2>&1 || true
    systemctl restart "$DISTRO_REDIS_SERVICE" || die "$DISTRO_REDIS_SERVICE does not start (journalctl -u $DISTRO_REDIS_SERVICE)"
    if ! id -nG "$ACCOUNT" | tr ' ' '\n' | grep -qx "$SYSTEM_REDIS_GROUP"; then
        usermod -aG "$SYSTEM_REDIS_GROUP" "$ACCOUNT"
        # the account's systemd user manager must start again to have the new group
        systemctl restart "user@$(id -u "$ACCOUNT").service"
    fi
    for _ in $(seq 30); do [ -S "$SYSTEM_REDIS_SOCKET" ] && break; sleep 1; done
    # REDISCLI_AUTH: the password in the environment, not on the command line
    REDISCLI_AUTH="$password" redis-cli --user motuz -s "$SYSTEM_REDIS_SOCKET" ping 2>/dev/null | grep -qx PONG \
        || die "the system Redis does not answer PING on $SYSTEM_REDIS_SOCKET as the ACL user motuz"
    handoff_set MOTUZ_BROKER_MODE system
    handoff_set MOTUZ_REDIS_SOCKET "$SYSTEM_REDIS_SOCKET"
    handoff_set MOTUZ_REDIS_PASSWORD "$password"
}
