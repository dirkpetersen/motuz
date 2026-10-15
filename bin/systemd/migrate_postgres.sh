#!/usr/bin/env bash
# Moves the database of an existing install from the account's private cluster (~/data/pg,
# motuz-postgres.service) to the distribution's PostgreSQL service (README, "Why a
# private PostgreSQL cluster?"). Run as the motuz account, after root prepared the system
# service with `sudo bin/systemd/install.sh --system-postgres` (which leaves the role, the
# empty database and /etc/motuz/handoff-<account>.env):
#     sudo -iu motuz ~/motuz/bin/systemd/migrate_postgres.sh [--force]
#     sudo -iu motuz ~/motuz/bin/systemd/migrate_postgres.sh --rollback
#
# It refuses while copy or integrity-check jobs run (--force stops the app anyway; those
# jobs fail), stops the app and worker, dumps the private cluster (pg_dump, custom
# format), replaces the contents of the system database with it (as the role motuz_user),
# compares the row count of every table with the private cluster's, and only then switches
# MOTUZ_DATABASE_MODE/HOST and the password in motuz.env / secrets.env, stops and
# disables motuz-postgres and runs deploy.sh. The private cluster ~/data/pg stays on disk,
# untouched, as the fallback: remove it yourself when the system database is trusted.
# On any failure before the switch nothing is changed and the app and worker are started
# again; run it again after fixing the cause.
#
# --rollback: back to the private cluster with its old settings (saved in
# ~/.config/motuz/postgres-rollback.env, mode 600). Changes made in the system database
# since the migration are NOT carried back.
#
# Backups afterwards: pg_dump (README, "Backups and upgrades").

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

FORCE=0
ROLLBACK=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --rollback) ROLLBACK=1 ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $arg" ;;
    esac
done

[ "$(id -u)" != 0 ] || die "run as the motuz account (sudo -iu motuz), not as root"
motuz_paths "$HOME"
load_distro
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
HANDOFF="/etc/motuz/handoff-$(id -un).env"
ROLLBACK_ENV="$CONFIG_DIR/postgres-rollback.env"

[ -f "$SETTINGS" ] && [ -f "$SECRETS" ] || die "run bin/systemd/deploy.sh once first"

if [ "$ROLLBACK" = 1 ]; then
    [ -f "$ROLLBACK_ENV" ] || die "no $ROLLBACK_ENV: this install was not migrated by migrate_postgres.sh"
    [ -f "$PGDATA_DIR/PG_VERSION" ] || die "the private cluster $PGDATA_DIR is gone"
    log "rolling back to the private cluster (changes in the system database since the migration are not carried back)"
    systemctl --user stop motuz-app.service motuz-celery.service
    env_set "$SETTINGS" MOTUZ_DATABASE_MODE private
    env_set "$SETTINGS" MOTUZ_DATABASE_HOST "$(env_get "$ROLLBACK_ENV" MOTUZ_DATABASE_HOST)"
    env_set "$SECRETS" MOTUZ_DATABASE_PASSWORD "$(env_get "$ROLLBACK_ENV" MOTUZ_DATABASE_PASSWORD)"
    "$REPO_DIR/bin/systemd/deploy.sh"
    exit 0
fi

[ "$(database_mode "$SETTINGS" "$PGDATA_DIR")" = private ] || die "this install does not use the private cluster (MOTUZ_DATABASE_MODE=$(env_get "$SETTINGS" MOTUZ_DATABASE_MODE))"
[ "$(env_get "$HANDOFF" MOTUZ_DATABASE_MODE)" = system ] \
    || die "no system PostgreSQL prepared: run as root: bin/systemd/install.sh --system-postgres (it writes $HANDOFF)"
NEW_HOST=$(env_get "$HANDOFF" MOTUZ_DATABASE_HOST)
NEW_PASSWORD=$(env_get "$HANDOFF" MOTUZ_DATABASE_PASSWORD)
valid_secret "$NEW_PASSWORD" && [[ "$NEW_HOST" =~ ^127\.0\.0\.1:[0-9]+$ ]] || die "unexpected contents of $HANDOFF"
DB_NAME=$(env_get "$SETTINGS" MOTUZ_DATABASE_NAME)
DB_USER=$(env_get "$SETTINGS" MOTUZ_DATABASE_USER)
[ "$DB_NAME" = motuz ] && [ "$DB_USER" = motuz_user ] || die "install.sh --system-postgres creates database motuz and role motuz_user; this install uses $DB_NAME / $DB_USER"
SOCK="$XDG_RUNTIME_DIR/motuz-postgres"

WORK_TMP=$(mktemp -d)
chmod 700 "$WORK_TMP"
SWITCHED=0
STOPPED=0
cleanup() {
    local rc=$?
    rm -rf "$WORK_TMP"
    # a failure before the switch: the old setup keeps working
    if [ "$rc" != 0 ] && [ "$SWITCHED" = 0 ] && [ "$STOPPED" = 1 ]; then
        echo "migration failed, starting the app and the worker again (still on the private cluster)" >&2
        systemctl --user start motuz-postgres.service motuz-app.service motuz-celery.service || true
    fi
}
trap cleanup EXIT

systemctl --user start motuz-postgres.service
for _ in $(seq 30); do psql -X -q -h "$SOCK" -d "$DB_NAME" -tAc 'SELECT 1' >/dev/null 2>&1 && break; sleep 1; done
running=$(psql -X -h "$SOCK" -d "$DB_NAME" -tAc "SELECT (SELECT count(*) FROM copy_job WHERE progress_state = 'PROGRESS')
    + (SELECT count(*) FROM hashsum_job WHERE progress_state = 'PROGRESS')
    + (SELECT count(*) FROM remote_job WHERE state IN ('QUEUED', 'RUNNING'))")
if [ "${running:-0}" != 0 ] && [ "$FORCE" = 0 ]; then
    die "$running copy/integrity-check jobs are running or queued: wait for them, or use --force (they fail)"
fi

log "stopping the app and the worker"
systemctl --user stop motuz-app.service motuz-celery.service
STOPPED=1
PGPASSWORD="$NEW_PASSWORD" psql -X -q -h "${NEW_HOST%:*}" -p "${NEW_HOST##*:}" -U "$DB_USER" -d "$DB_NAME" -tAc 'SELECT 1' >/dev/null \
    || die "cannot log in to the system PostgreSQL at $NEW_HOST as $DB_USER (install.sh --system-postgres again?)"

log "dumping the private cluster"
psql -X -h "$SOCK" -d "$DB_NAME" -tA -F '|' -c "$COUNT_SQL" > "$WORK_TMP/counts.old"
pg_dump -h "$SOCK" -d "$DB_NAME" -Fc --no-owner --no-privileges -f "$WORK_TMP/motuz.dump" || die "pg_dump failed"
echo "    $(wc -l < "$WORK_TMP/counts.old") tables, $(du -h "$WORK_TMP/motuz.dump" | cut -f1)"

log "restoring into the system PostgreSQL ($NEW_HOST)"
restore_dump_over_tcp "$NEW_HOST" "$DB_USER" "$NEW_PASSWORD" "$DB_NAME" "$WORK_TMP/motuz.dump" || die "pg_restore failed; nothing was switched"
PGPASSWORD="$NEW_PASSWORD" psql -X -h "${NEW_HOST%:*}" -p "${NEW_HOST##*:}" -U "$DB_USER" -d "$DB_NAME" -tA -F '|' -c "$COUNT_SQL" > "$WORK_TMP/counts.new"
if ! diff "$WORK_TMP/counts.old" "$WORK_TMP/counts.new"; then
    die "row counts differ after the restore (above: < private, > system); nothing was switched"
fi
echo "    row counts match"

log "switching to the system PostgreSQL"
# the old settings, for --rollback
(umask 077 && { env_line MOTUZ_DATABASE_HOST "$(env_get "$SETTINGS" MOTUZ_DATABASE_HOST)"
                env_line MOTUZ_DATABASE_PASSWORD "$(env_get "$SECRETS" MOTUZ_DATABASE_PASSWORD)"; } > "$ROLLBACK_ENV")
for key in MOTUZ_DATABASE_MODE MOTUZ_DATABASE_HOST; do env_set "$SETTINGS" "$key" "$(env_get "$HANDOFF" "$key")"; done
env_set "$SECRETS" MOTUZ_DATABASE_PASSWORD "$NEW_PASSWORD"
SWITCHED=1
systemctl --user disable --now motuz-postgres.service >/dev/null 2>&1 || systemctl --user stop motuz-postgres.service
echo "    the private cluster $PGDATA_DIR is kept (stopped); undo with $0 --rollback; remove it when you trust the new database"

"$REPO_DIR/bin/systemd/deploy.sh"
