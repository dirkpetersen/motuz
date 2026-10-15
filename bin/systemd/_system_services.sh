# Sourced by bin/systemd/install.sh (root): the choice between the account's private
# PostgreSQL cluster / Redis and the distribution's own services, and the setup of the
# system ones (README, "Why a private PostgreSQL cluster?" and "Root steps without
# install.sh"). Needs _lib.sh, the distro module, and ACCOUNT, HOME_DIR, GROUP, TMP.
#
# What the system modes leave for bin/systemd/deploy.sh (run as the account) is the
# handoff file /etc/motuz/handoff-<account>.env: root:<account's group>, mode 640, written
# fresh by every install.sh run (so it only holds the modes that are system now): the
# generated passwords and the resulting settings. deploy.sh merges it into motuz.env and
# secrets.env, so the passwords never appear on a command line or in a log, and the
# account reads them without root. Root never writes, chowns or reads inside the account's
# home as root (the account controls the names there): it reads the account's files as the
# account (runuser) and validates what it takes from them.

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

# ---------------------------------------------------------------- the handoff file
HANDOFF=""        # /etc/motuz/handoff-<account>.env, set by handoff_init
HANDOFF_NEW=""
handoff_init() {
    HANDOFF="/etc/motuz/handoff-$ACCOUNT.env"
    HANDOFF_NEW="$TMP/handoff.new"
    : > "$HANDOFF_NEW"
}
handoff_add() { # key value
    env_line "$1" "$2" >> "$HANDOFF_NEW"
}
# Replaces the file with what this run added (nothing added: no file, no stale keys)
handoff_commit() {
    if [ -s "$HANDOFF_NEW" ]; then
        install -d -m 755 -o root -g root /etc/motuz
        install -m 640 -o root -g "$GROUP" "$HANDOFF_NEW" "$HANDOFF"
    else
        rm -f "$HANDOFF"
    fi
}

# The password of a system service: the previous handoff file's (root's), else the one in
# the account's secrets.env if that service already runs in system mode (read as the
# account), else a new random one. Only a generated-looking secret is accepted.
service_password() { # key, current mode of that service
    local pw
    pw=$(env_get "$HANDOFF" "$1")
    if ! valid_secret "$pw" && [ "$2" = system ]; then
        pw=$(account_env_get "$HOME_DIR/.config/motuz/secrets.env" "$1" || true)
    fi
    valid_secret "$pw" || pw=$(openssl rand -hex 32)
    printf '%s' "$pw"
}

# True when an address list (commas or spaces, quotes ignored) names anything but loopback
non_loopback() { # list
    local e
    for e in $(printf '%s' "$1" | tr ',' ' '); do
        e="${e//\'/}"
        case "$e" in localhost|127.0.0.1|::1|-127.0.0.1|-::1) ;; *) return 0 ;; esac
    done
    return 1
}

# SYSTEM_PG_PORT: the cluster's port when it is not the default (the unix socket is named after it)
SYSTEM_PG_PORT=""
PG_PORT_MARKER=/etc/motuz/system-postgres-port-moved
as_postgres() { # command... (the postgres superuser of the system cluster, peer login)
    local envargs=()
    [ -z "$SYSTEM_PG_PORT" ] || envargs=("PGPORT=$SYSTEM_PG_PORT")
    (cd / && runuser -u postgres -- env "${envargs[@]}" "$@")
}
wait_postgres() {
    for _ in $(seq 60); do
        as_postgres psql -X -q -d postgres -tAc 'SELECT 1' >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}

# ---------------------------------------------------------------- PostgreSQL
# The distribution's cluster (a distro_system_postgres_prepare), localhost only, role
# motuz_user (scram) and database motuz owned by it, pg_hba lines for that role on
# 127.0.0.1 and ::1 only (scram), and the handoff for deploy.sh. It refuses to take over a
# cluster that something else seems to use, unless --system-postgres was given.
setup_system_postgres() {
    local db=motuz dbuser=motuz_user port listen hba password others
    log "PostgreSQL: the distribution's service (system mode)"
    distro_system_postgres_prepare
    # the port of an earlier run (the service moves away from 5432 while the private cluster exists)
    SYSTEM_PG_PORT=$(env_get "$HANDOFF" MOTUZ_DATABASE_HOST)
    if [ "$EXISTING_PG" = system ]; then
        SYSTEM_PG_PORT=$(account_env_get "$HOME_DIR/.config/motuz/motuz.env" MOTUZ_DATABASE_HOST || true)
    fi
    SYSTEM_PG_PORT="${SYSTEM_PG_PORT##*:}"
    valid_port "$SYSTEM_PG_PORT" || SYSTEM_PG_PORT=""
    [ "$SYSTEM_PG_PORT" != 5432 ] || SYSTEM_PG_PORT=""
    wait_postgres || die "cannot connect to the system PostgreSQL as postgres"
    port=$(as_postgres psql -X -d postgres -tAc 'SHOW port')
    valid_port "$port" || die "unexpected port '$port' of the system PostgreSQL"
    listen=$(as_postgres psql -X -d postgres -tAc 'SHOW listen_addresses')

    # Something else may use this cluster already: do not take it over silently
    others=$(as_postgres psql -X -d postgres -tAc "SELECT string_agg(datname, ', ') FROM pg_database WHERE datname NOT IN ('postgres', 'template0', 'template1', '$db')")
    if [ "$PG_OPTION" != system ]; then
        if [ -n "$others" ] || non_loopback "$listen"; then
            die "the system PostgreSQL looks in use by something else (databases: ${others:-none besides the standard ones}; listen_addresses = '$listen'). Motuz would only add the role $dbuser, the database $db and pg_hba.conf lines for them and set listen_addresses to localhost; if that is fine run install.sh --system-postgres, otherwise --private-postgres"
        fi
    fi

    if [ "$EXISTING_PG" = private ] && [ "$port" = 5432 ]; then
        # the private cluster holds 127.0.0.1:5432 until migrate_postgres.sh has moved its data
        warn "the private cluster of $ACCOUNT uses 127.0.0.1:5432: the system PostgreSQL moves to port 5433 (install.sh moves it back after the migration)"
        distro_system_postgres_set_port 5433
        systemctl restart "$SYSTEM_PG_UNIT"
        install -d -m 755 /etc/motuz && : > "$PG_PORT_MARKER"
        port=5433
        SYSTEM_PG_PORT=5433
        wait_postgres || die "the system PostgreSQL does not answer on port 5433"
    elif [ "$EXISTING_PG" = system ] && [ -f "$PG_PORT_MARKER" ] && [ "$port" = 5433 ] && ! ss -ltnH | awk '{print $4}' | grep -qE ':5432$'; then
        # the private cluster is gone: back to the standard port
        log "the private cluster is stopped: the system PostgreSQL goes back to port 5432"
        distro_system_postgres_set_port 5432
        systemctl restart "$SYSTEM_PG_UNIT"
        rm -f "$PG_PORT_MARKER"
        port=5432
        SYSTEM_PG_PORT=""
        wait_postgres || die "the system PostgreSQL does not answer on port 5432"
    fi
    if non_loopback "$listen"; then
        warn "listen_addresses = '$listen': setting it to 'localhost' (ALTER SYSTEM)"
        as_postgres psql -X -q -d postgres -c "ALTER SYSTEM SET listen_addresses = 'localhost'"
        systemctl restart "$SYSTEM_PG_UNIT"
        wait_postgres || die "the system PostgreSQL does not restart"
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

    password=$(service_password MOTUZ_DATABASE_PASSWORD "$EXISTING_PG")
    valid_secret "$password" || die "invalid generated password"
    # The password (64 hex digits, validated) goes in through stdin, never through a command line
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
    handoff_add MOTUZ_DATABASE_MODE system
    handoff_add MOTUZ_DATABASE_HOST "127.0.0.1:$port"
    handoff_add MOTUZ_DATABASE_PASSWORD "$password"
    echo "    role $dbuser and database $db on 127.0.0.1:$port (service $SYSTEM_PG_UNIT); the password is in $HANDOFF (root:$GROUP, 640) until deploy.sh merges it into secrets.env"
}

# ---------------------------------------------------------------- Redis
# True when the distribution's redis-server seems to serve something else already: a
# configuration away from the package's defaults, keys, other clients. Our own
# /etc/redis/motuz.conf means it is ours.
redis_in_use() {
    local port=6379 out
    [ ! -f /etc/redis/motuz.conf ] || return 1
    if grep -qE '^[[:space:]]*(requirepass|aclfile|masterauth|replicaof|slaveof|cluster-enabled[[:space:]]+yes)' "$SYSTEM_REDIS_CONF" 2>/dev/null; then
        echo "its configuration sets requirepass/aclfile/replication/cluster"; return 0
    fi
    out=$(grep -E '^[[:space:]]*bind[[:space:]]' "$SYSTEM_REDIS_CONF" 2>/dev/null | tail -1 | sed -E 's/^[[:space:]]*bind[[:space:]]+//' || true)
    if [ -n "$out" ] && non_loopback "$out"; then
        echo "it listens on more than loopback"; return 0
    fi
    out=$(grep -E '^[[:space:]]*port[[:space:]]' "$SYSTEM_REDIS_CONF" 2>/dev/null | awk '{print $2}' | tail -1)
    if [ -n "$out" ] && [ "$out" != 6379 ]; then
        echo "it listens on port $out"; return 0
    fi
    if systemctl is-active --quiet "$DISTRO_REDIS_SERVICE" 2>/dev/null; then
        out=$(redis-cli -h 127.0.0.1 -p "$port" --no-auth-warning dbsize 2>&1 || true)
        case "$out" in
            *NOAUTH*|*Authentication*|*WRONGPASS*|*NOPERM*) echo "it requires authentication"; return 0 ;;
            *[!0-9]*|'') ;;
            0) ;;
            *) echo "it holds $out keys"; return 0 ;;
        esac
        out=$(redis-cli -h 127.0.0.1 -p "$port" --no-auth-warning client list 2>/dev/null | wc -l || true)
        if [ "${out:-0}" -gt 1 ]; then
            echo "$((out - 1)) other clients are connected"; return 0
        fi
    fi
    return 1
}

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
    local password hash conf=/etc/redis/motuz.conf reason
    log "Redis: the distribution's service (system mode, unix socket $SYSTEM_REDIS_SOCKET)"
    if [ "$REDIS_OPTION" != system ] && reason=$(redis_in_use); then
        die "the system Redis looks in use by something else ($reason). Motuz would switch it to a unix socket only (port 0) with the default user off; if that is fine run install.sh --system-redis, otherwise --private-redis"
    fi
    # Joining the group means restarting the account's user manager, which ends running jobs
    if ! id -nG "$ACCOUNT" | tr ' ' '\n' | grep -qx "$SYSTEM_REDIS_GROUP" && pgrep -x rclone >/dev/null; then
        die "rclone processes are running (jobs): the account's user manager must restart to join the group $SYSTEM_REDIS_GROUP; run install.sh again when no job runs"
    fi
    password=$(service_password MOTUZ_REDIS_PASSWORD "$EXISTING_REDIS")
    valid_secret "$password" || die "invalid generated password"
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
    handoff_add MOTUZ_BROKER_MODE system
    handoff_add MOTUZ_REDIS_SOCKET "$SYSTEM_REDIS_SOCKET"
    handoff_add MOTUZ_REDIS_PASSWORD "$password"
}
