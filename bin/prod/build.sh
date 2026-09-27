#!/usr/bin/env bash

set -e

THIS_DIR=$(dirname "$0")
cd ${THIS_DIR}
cd ../..

# docker-compose (standalone) or the docker compose plugin, whichever exists
if command -v docker-compose >/dev/null 2>&1; then
    DOCKER_COMPOSE="docker-compose"
else
    DOCKER_COMPOSE="docker compose"
fi
# The commit of this checkout, baked into the image: temporary EC2 workers fetch Motuz at
# this commit from MOTUZ_WORKER_SOURCE (README, "Temporary EC2 workers"), so it must be
# pushed there. Uncommitted changes are not in it.
if [ -z "${MOTUZ_SOURCE_COMMIT:-}" ] && git rev-parse --verify -q HEAD >/dev/null 2>&1; then
    MOTUZ_SOURCE_COMMIT=$(git rev-parse HEAD)
    git diff --quiet HEAD -- src 2>/dev/null \
        || echo "WARNING: uncommitted changes in src/: EC2 workers would run commit $MOTUZ_SOURCE_COMMIT without them" >&2
fi
export MOTUZ_SOURCE_COMMIT="${MOTUZ_SOURCE_COMMIT:-}"

COMPOSE="${DOCKER_COMPOSE} -f docker-compose.yml -f docker-compose.override.yml -f deployment/docker-compose/docker-compose.build.yml"

# Pick up latest changes. Add `--no-cache` if this turns out to be unreliable.
# The celery image is built FROM fredhutch/motuz_app, so the app image must exist first
# (otherwise an old image is pulled from Docker Hub). The app image includes the frontend.
$COMPOSE build "$@" app
$COMPOSE build "$@" celery database_init
# Traefik is not built; fetch the pinned release so a changed tag is picked up
$COMPOSE pull traefik
