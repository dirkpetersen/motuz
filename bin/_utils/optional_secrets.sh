#!/usr/bin/env bash

# Creates an empty file (mode 600) for each optional docker secret that does not exist
# yet. docker-compose refuses to start a service whose secret file is missing, and an
# empty secret means "not configured" (src/backend/api/config.py). Existing files are
# never changed, so this is safe to run on every deploy: bin/prod/start.sh (also run by
# bin/redeploy.sh) and bin/quickstart.sh call it.
#
# Usage: bin/_utils/optional_secrets.sh [secrets directory]
# The directory defaults to $MOTUZ_DOCKER_ROOT/secrets, where MOTUZ_DOCKER_ROOT comes
# from the environment, else from .env, else /docker (the same precedence as compose).

set -e

OPTIONAL_SECRETS="MOTUZ_ONEDRIVE_CLIENT_SECRET"

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

if [ -n "$1" ]; then
    SECRETS_DIR="$1"
else
    if [ -z "${MOTUZ_DOCKER_ROOT}" ] && [ -f "${REPO_DIR}/.env" ]; then
        MOTUZ_DOCKER_ROOT="$(sed -n 's/^MOTUZ_DOCKER_ROOT=//p' "${REPO_DIR}/.env" | tail -n 1)"
    fi
    SECRETS_DIR="${MOTUZ_DOCKER_ROOT:-/docker}/secrets"
fi

if [ ! -d "${SECRETS_DIR}" ]; then
    echo "WARNING: ${SECRETS_DIR} does not exist (run bin/quickstart.sh); not creating optional secrets" >&2
    exit 0
fi

for name in ${OPTIONAL_SECRETS}; do
    path="${SECRETS_DIR}/${name}"
    if [ ! -e "${path}" ]; then
        (umask 077 && : > "${path}")
        echo "Created empty optional secret ${path}"
    fi
done
