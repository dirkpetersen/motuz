#!/usr/bin/env bash

# Source from the repository root. Exports the defaults from .env (without
# overriding variables already set) and the secrets in $MOTUZ_DOCKER_ROOT/secrets,
# which the backend requires at import time (see src/backend/api/config.py).

while IFS='=' read -r key value; do
    case "$key" in
        ''|\#*) continue ;;
    esac
    if [ -z "${!key+x}" ]; then
        export "$key=$value"
    fi
done < .env

source ./deployment/docker/load-secrets.sh "${MOTUZ_DOCKER_ROOT:-/docker}/secrets" > /dev/null
