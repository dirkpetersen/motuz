#!/usr/bin/env bash
# Moves the database of an existing install from the account's private cluster (~/data/pg,
# motuz-postgres.service) to the distribution's PostgreSQL service (README, "Why a
# private PostgreSQL cluster?"). Run as the motuz account, after root prepared the system
# service with `sudo bin/systemd/install.sh --system-postgres` (which leaves the role,
# the empty database and ~/.config/motuz/handoff.env):
#     sudo -iu motuz ~/motuz/bin/systemd/migrate_postgres.sh
#
# It stops the app and the worker, dumps the private cluster (pg_dump, custom format),
# replaces the contents of the system database with it (as the role motuz_user), compares
# the row count of every table with the private cluster's, and only then switches
# MOTUZ_DATABASE_MODE/HOST and the password in motuz.env / secrets.env, stops and
# disables motuz-postgres and runs deploy.sh. The private cluster ~/data/pg stays on disk,
# untouched, as the fallback: remove it yourself when the system database is trusted.
# On any failure before the switch nothing is changed; run it again after fixing the cause.
#
# Backups afterwards: pg_dump (README, "Backups and upgrades").

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

[ "$(id -u)" != 0 ] || die "run as the motuz account (sudo -iu motuz), not as root"
motuz_paths "$HOME"
load_distro
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
HANDOFF="$CONFIG_DIR/handoff.env"

[ -f "$SETTINGS" ] && [ -f "$SECRETS" ] || die "run bin/systemd/deploy.sh once first"
[ "$(database_mode "$SETTINGS" "$PGDATA_DIR")" = private ] || die "this install does not use the private cluster (MOTUZ_DATABASE_MODE=$(env_get "$SETTINGS" MOTUZ_DATABASE_MODE))"
[ "$(env_get "$HANDOFF" MOTUZ_DATABASE_MODE)" = system ] \
    || die "no system PostgreSQL prepared: run as root: bin/systemd/install.sh --system-postgres (it writes $HANDOFF)"
NEW_HOST=$(env_get "$HANDOFF" MOTUZ_DATABASE_HOST)
NEW_PASSWORD=$(env_get "$HANDOFF" MOTUZ_DATABASE_PASSWORD)
DB_NAME=$(env_get "$SETTINGS" MOTUZ_DATABASE_NAME)
DB_USER=$(env_get "$SETTINGS" MOTUZ_DATABASE_USER)
[ "$DB_NAME" = motuz ] && [ "$DB_USER" = motuz_user ] || die "install.sh --system-postgres creates database motuz and role motuz_user; this install uses $DB_NAME / $DB_USER"
SOCK="$XDG_RUNTIME_DIR/motuz-postgres"

WORK_TMP=$(mktemp -d)
trap 'rm -rf "$WORK_TMP"' EXIT
chmod 700 "$WORK_TMP"

log "stopping the app and the worker, starting the private cluster"
systemctl --user stop motuz-app.service motuz-celery.service
systemctl --user start motuz-postgres.service
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
for key in MOTUZ_DATABASE_MODE MOTUZ_DATABASE_HOST; do env_set "$SETTINGS" "$key" "$(env_get "$HANDOFF" "$key")"; done
env_set "$SECRETS" MOTUZ_DATABASE_PASSWORD "$NEW_PASSWORD"
systemctl --user disable --now motuz-postgres.service >/dev/null 2>&1 || systemctl --user stop motuz-postgres.service
echo "    the private cluster $PGDATA_DIR is kept (stopped); remove it when you trust the new database"

"$REPO_DIR/bin/systemd/deploy.sh"
