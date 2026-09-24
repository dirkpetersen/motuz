#!/usr/bin/env bash

# Upgrades the PostgreSQL data directory ($MOTUZ_DOCKER_ROOT/volumes/postgres) to the
# major version of the `database` image in docker-compose.yml, using pg_dumpall with
# the old version and a restore into a freshly initialized cluster.
#
# - Does nothing when there is no data yet, the data is already current, or a
#   custom (external) database is configured.
# - Motuz must be stopped (bin/prod/start.sh calls this after `docker-compose down`).
# - The old data directory is never deleted: it is kept as postgres.pg<OLD>.<timestamp>
#   next to the new one, together with the SQL dump.

set -e

THIS_DIR=$(dirname "$0")
cd ${THIS_DIR}
cd ../..

source bin/_utils/load_env.sh

MOTUZ_DOCKER_ROOT=${MOTUZ_DOCKER_ROOT:-/docker}
VOLUMES="${MOTUZ_DOCKER_ROOT}/volumes"
DATA="${VOLUMES}/postgres"

NEW_IMAGE=$(sed -n 's/^ *image: *\(postgres:[^ ]*\).*/\1/p' docker-compose.yml | head -1)
NEW_MAJOR=$(echo "$NEW_IMAGE" | sed 's/^postgres:\([0-9]*\).*/\1/')
PG_DATA_IN_CONTAINER=/var/lib/postgresql/data

if [ "${MOTUZ_DATABASE_HOST}" != "0.0.0.0:5432" ]; then
    echo "Custom database ${MOTUZ_DATABASE_HOST} configured, not upgrading the local PostgreSQL"
    exit 0
fi

# Root inside a container can read the data directory regardless of its owner on the host
OLD_MAJOR=$(docker run --rm --entrypoint cat -v "${VOLUMES}":/volumes "$NEW_IMAGE" /volumes/postgres/PG_VERSION 2> /dev/null || true)
OLD_MAJOR=$(echo "$OLD_MAJOR" | tr -d '[:space:]')

if [ -z "$OLD_MAJOR" ]; then
    echo "No PostgreSQL data in ${DATA} yet, nothing to upgrade"
    exit 0
fi
if [ "$OLD_MAJOR" = "$NEW_MAJOR" ]; then
    echo "PostgreSQL data in ${DATA} is already at version ${NEW_MAJOR}"
    exit 0
fi
if [ "$OLD_MAJOR" -gt "$NEW_MAJOR" ]; then
    echo "PostgreSQL data is version ${OLD_MAJOR}, newer than ${NEW_IMAGE}. Aborting."
    exit 1
fi
if [ -n "$(docker ps -q -f name='^motuz_database$')" ]; then
    echo "motuz_database is running. Stop Motuz first (docker-compose down). Aborting."
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OLD_CONTAINER=motuz_pg_upgrade_old
NEW_CONTAINER=motuz_pg_upgrade_new
NEW_DATA_NAME="postgres.pg${NEW_MAJOR}.new"
BACKUP_NAME="postgres.pg${OLD_MAJOR}.${TIMESTAMP}"
DUMP="${VOLUMES}/postgres.pg${OLD_MAJOR}.${TIMESTAMP}.sql"
WORK_DIR=$(mktemp -d)

cleanup() {
    docker rm -f "$OLD_CONTAINER" "$NEW_CONTAINER" > /dev/null 2>&1 || true
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

# Runs a shell command as root with the volumes directory mounted at /volumes
in_volumes() {
    docker run --rm --entrypoint sh -v "${VOLUMES}":/volumes "$NEW_IMAGE" -c "$1"
}

wait_for_postgres() {
    local container=$1
    for _ in $(seq 1 120); do
        if docker exec "$container" psql -U postgres -tAc 'SELECT 1' > /dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    echo "PostgreSQL in $container did not become ready"
    docker logs "$container" | tail -20
    exit 1
}

# Row count of every table in the Motuz database, used to verify the restore
table_counts() {
    docker exec "$1" psql -U postgres -d "${MOTUZ_DATABASE_NAME}" -tAc "
        SELECT table_name, (xpath('/row/c/text()', query_to_xml(
            format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY 1"
}

echo "Upgrading PostgreSQL ${OLD_MAJOR} -> ${NEW_MAJOR} in ${DATA}"

# Local trust authentication only, over the unix socket, no network: the dump runs as postgres
cat > "${WORK_DIR}/pg_hba.conf" <<EOF
local   all             all                                     trust
EOF
chmod 644 "${WORK_DIR}/pg_hba.conf"
# The old server's unix socket is shared with the new image, whose pg_dumpall does the dump
mkdir "${WORK_DIR}/socket"
chmod 777 "${WORK_DIR}/socket"


echo "1/4 Dumping PostgreSQL ${OLD_MAJOR} data with the ${NEW_MAJOR} client..."
docker run -d --name "$OLD_CONTAINER" --network none \
    -e PGDATA="$PG_DATA_IN_CONTAINER" \
    -v "${DATA}":"$PG_DATA_IN_CONTAINER" \
    -v "${WORK_DIR}/pg_hba.conf":/etc/motuz_upgrade_hba.conf:ro \
    -v "${WORK_DIR}/socket":/var/run/postgresql \
    "postgres:${OLD_MAJOR}-alpine" \
    postgres -c hba_file=/etc/motuz_upgrade_hba.conf -c listen_addresses='' > /dev/null
wait_for_postgres "$OLD_CONTAINER"
table_counts "$OLD_CONTAINER" > "${WORK_DIR}/counts_old"
(umask 077 && docker run --rm --network none -v "${WORK_DIR}/socket":/var/run/postgresql \
    "$NEW_IMAGE" pg_dumpall -U postgres > "$DUMP")
docker stop "$OLD_CONTAINER" > /dev/null
echo "    Dump written to ${DUMP}"


echo "2/4 Restoring into a new postgres:${NEW_MAJOR} cluster..."
in_volumes "rm -rf /volumes/${NEW_DATA_NAME}"
docker run -d --name "$NEW_CONTAINER" --network none \
    -e POSTGRES_PASSWORD="$(head -c 32 /dev/urandom | base64)" \
    -e PGDATA="$PG_DATA_IN_CONTAINER" \
    -v "${VOLUMES}/${NEW_DATA_NAME}":"$PG_DATA_IN_CONTAINER" \
    "$NEW_IMAGE" -c listen_addresses='' > /dev/null
# The image initializes the cluster with a temporary server first; wait for the real one
for _ in $(seq 1 120); do
    if docker logs "$NEW_CONTAINER" 2>&1 | grep -q "PostgreSQL init process complete"; then
        break
    fi
    sleep 1
done
wait_for_postgres "$NEW_CONTAINER"

# Errors such as `role "postgres" already exists` are expected and harmless
docker exec -i "$NEW_CONTAINER" psql -U postgres -q -f - < "$DUMP" > "${WORK_DIR}/restore.log" 2>&1 || true
grep -i "error" "${WORK_DIR}/restore.log" | grep -v 'role "postgres" already exists' || true

# PostgreSQL 15+ no longer lets every role create tables in the public schema: the database
# owner can. Also re-hash the password as scram-sha-256 (the old cluster stored md5).
docker exec -i "$NEW_CONTAINER" psql -U postgres -v ON_ERROR_STOP=1 -q \
    -v db="${MOTUZ_DATABASE_NAME}" -v user="${MOTUZ_DATABASE_USER}" -v pw="${MOTUZ_DATABASE_PASSWORD}" <<'EOSQL'
ALTER DATABASE :"db" OWNER TO :"user";
ALTER ROLE :"user" WITH PASSWORD :'pw';
EOSQL


echo "3/4 Verifying row counts..."
table_counts "$NEW_CONTAINER" > "${WORK_DIR}/counts_new"
if ! diff "${WORK_DIR}/counts_old" "${WORK_DIR}/counts_new"; then
    echo "Row counts differ after restore. The original data in ${DATA} is untouched. Aborting."
    exit 1
fi
echo "    $(wc -l < "${WORK_DIR}/counts_new") tables, row counts match"
docker cp deployment/docker/database_init/pg_hba.conf "${NEW_CONTAINER}:${PG_DATA_IN_CONTAINER}/pg_hba.conf"
docker exec "$NEW_CONTAINER" chown postgres:postgres "${PG_DATA_IN_CONTAINER}/pg_hba.conf"
docker stop "$NEW_CONTAINER" > /dev/null


echo "4/4 Swapping data directories..."
in_volumes "mv /volumes/postgres /volumes/${BACKUP_NAME} && mv /volumes/${NEW_DATA_NAME} /volumes/postgres"

echo "
PostgreSQL upgraded to ${NEW_MAJOR}.
Previous data directory kept at ${VOLUMES}/${BACKUP_NAME}
SQL dump kept at ${DUMP}
Remove both once Motuz is confirmed to work."
