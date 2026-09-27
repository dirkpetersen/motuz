#!/usr/bin/env bash
# Moves a docker install of Motuz (docker-compose.yml) to the systemd install (README,
# "Migrating from the docker install"), on the same host or another one. Two steps:
#
# 1. On the docker host, as root (reads $MOTUZ_DOCKER_ROOT/secrets), with Motuz running:
#      bin/systemd/migrate_from_docker.sh export [--out FILE] [--repo DIR]
#          [--env-file FILE] [--docker-root DIR] [--db-container NAME] [--app-container NAME]
#    writes FILE (default ./motuz-export-<date>.tar.gz, mode 600; it holds secrets):
#      - the database as a pg_dump custom-format dump, and the row count of every table
#      - the secrets: the Flask key (signs the login tokens, so sessions stay valid), the
#        SMTP password and the OAuth client secrets (not the database password: the new
#        cluster has its own)
#      - the MOTUZ_* settings of the running app container (from .env, the compose files
#        and docker-compose.override.yml) and MOTUZ_ACME_* of .env
#      - Traefik's acme.json (Let's Encrypt account and certificates) and certs/cert.*
#      - the app container's volume mounts, as a list to recreate on the new host
#    Defaults: --repo is this checkout, --env-file <repo>/.env, --docker-root its
#    MOTUZ_DOCKER_ROOT (/docker), containers motuz_database and motuz_app.
#
# 2. On the new host, as the motuz account, after install.sh, the clone and a first
#    deploy.sh (stop the docker install first if it is the same host: ports 80/443):
#      ~/motuz/bin/systemd/migrate_from_docker.sh import FILE
#    stops the app and the worker, writes the secrets, settings, acme.json and
#    certificates, recreates the database from the dump (pg_restore into the new
#    cluster, owned by motuz_user), checks the row counts against the export, and runs
#    deploy.sh (migrations, restart, health check).
#
# Why a dump and not the data directory: the docker image is Alpine (musl libc), Ubuntu
# uses glibc; text sorts differently, so indexes on text columns copied as files would be
# corrupt. pg_restore rebuilds every index with the new cluster's collation.

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

# Settings of the docker install that are not carried over: docker only, or they belong
# to the new install (its database, login and paths)
NOT_CARRIED="MOTUZ_DOCKER_ROOT MOTUZ_HOST MOTUZ_FRONTEND_DIR MOTUZ_DATABASE_HOST MOTUZ_DATABASE_USER MOTUZ_DATABASE_NAME
MOTUZ_DATABASE_PROTOCOL MOTUZ_DATABASE_REQUIRE_SSL MOTUZ_DATABASE_PASSWORD MOTUZ_FLASK_SECRET_KEY MOTUZ_SMTP_PASSWORD
MOTUZ_ONEDRIVE_CLIENT_SECRET MOTUZ_GDRIVE_CLIENT_SECRET MOTUZ_PAM_SERVICE MOTUZ_AUTH_HELPER MOTUZ_SSO_CONFIG_DIR
MOTUZ_CELERY_BROKER_URL MOTUZ_TOKEN_BROKER_URL MOTUZ_INSTALL_AZURE_CLI"
CARRIED_SECRETS="MOTUZ_FLASK_SECRET_KEY MOTUZ_SMTP_PASSWORD MOTUZ_ONEDRIVE_CLIENT_SECRET MOTUZ_GDRIVE_CLIENT_SECRET"

# Row count of every table of the public schema, as "table|count" lines
COUNT_SQL="SELECT table_name, (xpath('/row/c/text()', query_to_xml(
    format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text
    FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1"

usage() { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

# ================================================================== export
export_docker() {
    local out="" repo="$REPO_DIR" env_file="" docker_root="" db_container=motuz_database app_container=motuz_app
    while [ $# -gt 0 ]; do
        case "$1" in
            --out) out="$2"; shift ;;
            --repo) repo="$2"; shift ;;
            --env-file) env_file="$2"; shift ;;
            --docker-root) docker_root="$2"; shift ;;
            --db-container) db_container="$2"; shift ;;
            --app-container) app_container="$2"; shift ;;
            *) usage ;;
        esac
        shift
    done
    env_file="${env_file:-$repo/.env}"
    [ -n "$docker_root" ] || docker_root=$(sed -n 's/^MOTUZ_DOCKER_ROOT=//p' "$env_file" 2>/dev/null | tail -n 1)
    docker_root="${docker_root:-/docker}"
    out="${out:-$PWD/motuz-export-$(date +%Y%m%d-%H%M%S).tar.gz}"
    command -v docker >/dev/null || die "docker not found"
    docker inspect "$db_container" "$app_container" >/dev/null || die "containers $db_container and $app_container must be running"
    [ -d "$docker_root/secrets" ] || die "no $docker_root/secrets (--docker-root)"

    local work; work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    chmod 700 "$work"
    mkdir -p "$work/export/secrets"

    log "settings of $app_container"
    local key value
    docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$app_container" | grep '^MOTUZ_' | sort > "$work/app.env"
    : > "$work/export/settings.env"
    while IFS='=' read -r key value; do
        case " $(echo $NOT_CARRIED) " in *" $key "*) continue ;; esac
        [ -n "$value" ] || continue
        env_line "$key" "$value" >> "$work/export/settings.env"
    done < "$work/app.env"
    for key in MOTUZ_ACME_DOMAIN MOTUZ_ACME_EMAIL MOTUZ_ACME_CA_SERVER; do
        value=$(sed -n "s/^$key=//p" "$env_file" 2>/dev/null | tail -n 1)
        [ -z "$value" ] || env_line "$key" "$value" >> "$work/export/settings.env"
    done
    sed 's/=.*//' "$work/export/settings.env" | sed 's/^/    /'

    log "secrets of $docker_root/secrets"
    for key in $CARRIED_SECRETS; do
        if [ -s "$docker_root/secrets/$key" ]; then
            # like load-secrets.sh: the file's content without trailing newlines
            printf '%s' "$(cat "$docker_root/secrets/$key")" > "$work/export/secrets/$key"
            echo "    $key"
        fi
    done
    [ -s "$work/export/secrets/MOTUZ_FLASK_SECRET_KEY" ] || die "no MOTUZ_FLASK_SECRET_KEY in $docker_root/secrets"

    log "Traefik certificates"
    if [ -s "$docker_root/traefik/acme.json" ]; then
        cp "$docker_root/traefik/acme.json" "$work/export/acme.json" && echo "    acme.json"
    fi
    if [ -s "$docker_root/certs/cert.crt" ] && [ -s "$docker_root/certs/cert.key" ]; then
        mkdir -p "$work/export/certs"
        cp "$docker_root/certs/cert.crt" "$docker_root/certs/cert.key" "$work/export/certs/" && echo "    certs/cert.crt, cert.key"
    fi

    log "database dump ($db_container)"
    local db_user db_name db_password
    db_user=$(sed -n 's/^MOTUZ_DATABASE_USER=//p' "$work/app.env"); db_user="${db_user:-motuz_user}"
    db_name=$(sed -n 's/^MOTUZ_DATABASE_NAME=//p' "$work/app.env"); db_name="${db_name:-motuz}"
    db_password=$(cat "$docker_root/secrets/MOTUZ_DATABASE_PASSWORD")
    # The password in the environment of `docker exec`, never on a command line
    PGPASSWORD="$db_password" docker exec -e PGPASSWORD "$db_container" \
        psql -X -h 127.0.0.1 -U "$db_user" -d "$db_name" -tA -F '|' -c "$COUNT_SQL" > "$work/export/counts.txt" \
        || die "could not count the rows"
    PGPASSWORD="$db_password" docker exec -e PGPASSWORD "$db_container" \
        pg_dump -h 127.0.0.1 -U "$db_user" -d "$db_name" -Fc --no-owner --no-privileges > "$work/export/motuz.dump" \
        || die "pg_dump failed"
    echo "    $(wc -l < "$work/export/counts.txt") tables, $(du -h "$work/export/motuz.dump" | cut -f1)"

    log "mounts of $app_container (recreate them on the new host, at the same paths)"
    docker inspect -f '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}' "$app_container" \
        | grep -v '^$' | tee "$work/export/mounts.txt" | sed 's/^/    /'
    printf 'motuz-export 1\ncreated %s\nhost %s\n' "$(date -Iseconds)" "$(hostname -f 2>/dev/null || hostname)" > "$work/export/manifest"

    (umask 077 && tar -C "$work" -czf "$out" export)
    chmod 600 "$out"
    log "wrote $out (SECRET: keep it private, delete it after the import)"
}

# ================================================================== import
import_systemd() {
    [ $# = 1 ] || usage
    local file="$1"
    [ "$(id -u)" != 0 ] || die "run import as the motuz account (sudo -iu motuz)"
    [ -f "$file" ] || die "no $file"
    motuz_paths "$HOME"
    load_distro
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
    [ -f "$SETTINGS" ] && [ -f "$SECRETS" ] && [ -f "$PGDATA_DIR/PG_VERSION" ] \
        || die "run bin/systemd/deploy.sh once before the import"
    [ "$(env_get "$SETTINGS" MOTUZ_DATABASE_HOST)" = 127.0.0.1:5432 ] || die "the import needs the local database (MOTUZ_DATABASE_HOST=127.0.0.1:5432)"

    local work; work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    chmod 700 "$work"
    tar -C "$work" -xzf "$file"
    grep -q '^motuz-export 1$' "$work/export/manifest" 2>/dev/null || die "$file is not an export of migrate_from_docker.sh"
    sed -n 's/^\(created\|host\) /    exported \1 /p' "$work/export/manifest"

    log "stopping the app and the worker"
    systemctl --user stop motuz-app.service motuz-celery.service
    systemctl --user start motuz-postgres.service

    log "database: new $(env_get "$SETTINGS" MOTUZ_DATABASE_NAME) from the dump"
    local db_name db_user sock="$XDG_RUNTIME_DIR/motuz-postgres"
    db_name=$(env_get "$SETTINGS" MOTUZ_DATABASE_NAME)
    db_user=$(env_get "$SETTINGS" MOTUZ_DATABASE_USER)
    psql -X -q -v ON_ERROR_STOP=1 -h "$sock" -d postgres -v db="$db_name" -v user="$db_user" <<'EOSQL'
DROP DATABASE IF EXISTS :"db" WITH (FORCE);
CREATE DATABASE :"db" OWNER :"user";
EOSQL
    # As the superuser (peer login), every object owned by the database owner
    pg_restore -h "$sock" -d "$db_name" --no-owner --no-privileges --role="$db_user" --exit-on-error "$work/export/motuz.dump" \
        || die "pg_restore failed"
    psql -X -h "$sock" -d "$db_name" -tA -F '|' -c "$COUNT_SQL" > "$work/counts.new"
    if ! diff "$work/export/counts.txt" "$work/counts.new"; then
        die "row counts differ after the restore (above: < docker, > new)"
    fi
    echo "    $(wc -l < "$work/counts.new") tables, row counts match"

    log "secrets and settings"
    local key value
    for key in $CARRIED_SECRETS; do
        if [ -f "$work/export/secrets/$key" ]; then
            env_set "$SECRETS" "$key" "$(cat "$work/export/secrets/$key")"
            echo "    $key"
        fi
    done
    while IFS= read -r line; do
        key="${line%%=*}"
        env_set "$SETTINGS" "$key" "$(env_get "$work/export/settings.env" "$key")"
        echo "    $key"
    done < "$work/export/settings.env"

    if [ -s "$work/export/acme.json" ]; then
        (umask 077 && cp "$work/export/acme.json" "$ACME_DIR/acme.json")
        chmod 600 "$ACME_DIR/acme.json"
        echo "    Traefik acme.json"
    fi
    if [ -f "$work/export/certs/cert.crt" ]; then
        (umask 077 && cp "$work/export/certs/cert.crt" "$work/export/certs/cert.key" "$CERTS_DIR/")
        echo "    certs/cert.crt, cert.key"
    fi
    if [ -s "$work/export/mounts.txt" ]; then
        echo "    Mounts of the docker app (Motuz needs the same paths here):"
        sed 's/^/        /' "$work/export/mounts.txt"
    fi

    log "deploy.sh"
    "$REPO_DIR/bin/systemd/deploy.sh"
}

case "${1:-}" in
    export) shift; export_docker "$@" ;;
    import) shift; import_systemd "$@" ;;
    *) usage ;;
esac
