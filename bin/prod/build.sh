#!/usr/bin/env bash

set -e

THIS_DIR=$(dirname "$0")
cd ${THIS_DIR}
cd ../..

COMPOSE="docker-compose -f docker-compose.yml -f docker-compose.override.yml -f deployment/docker-compose/docker-compose.build.yml"

# Pick up latest changes. Add `--no-cache` if this turns out to be unreliable.
# The celery image is built FROM fredhutch/motuz_app, so the app image must exist first
# (otherwise an old image is pulled from Docker Hub). The app image includes the frontend.
$COMPOSE build "$@" app
$COMPOSE build "$@" celery database_init
# Traefik is not built; fetch the pinned release so a changed tag is picked up
$COMPOSE pull traefik
