#!/usr/bin/env bash

set -e

THIS_DIR=$(dirname "$0")
cd ${THIS_DIR}
cd ../..


MOTUZ_DOCKER_ROOT=${MOTUZ_DOCKER_ROOT:-/docker}

# In order to check that it all works well
# docker run -it --entrypoint='bash' --net='host' postgres:18-alpine
# PGPASSWORD="$MOTUZ_DATABASE_PASSWORD" psql -h 127.0.0.1 -U "$MOTUZ_DATABASE_USER" "$MOTUZ_DATABASE_NAME"
# (after `source bin/_utils/load_env.sh`)

./bin/_utils/upgrade_postgres.sh

# The image needs a superuser password to initialize an empty data directory. A random
# one that nobody keeps: the cluster is initialized by bin/_utils/database_install.sh,
# whose pg_hba.conf lets only the application role in. Passed by name, so it is not on
# the command line.
export POSTGRES_PASSWORD="$(openssl rand -hex 32)"

docker run --rm \
    --name local_motuz_database \
    -e POSTGRES_PASSWORD \
    -e PGDATA=/var/lib/postgresql/data \
    -p 127.0.0.1:5432:5432 \
    -v ${MOTUZ_DOCKER_ROOT}/volumes/postgres:/var/lib/postgresql/data \
    postgres:18-alpine
