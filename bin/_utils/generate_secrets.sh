#!/usr/bin/env bash

# Creates the required secret files that do not exist yet, each with a random value
# (mode 600, existing files are never changed): the database password, the Flask secret
# key, the SMTP password and the key that encrypts the credential columns of the database
# (MOTUZ_DATA_KEYS, `<id>:<base64 of 32 random bytes>`; back it up apart from database
# dumps, README "Secrets and backups"). Used by bin/dev/init_dev.sh; bin/quickstart.sh does
# the same with a confirmation per file.
#
# Usage: bin/_utils/generate_secrets.sh [secrets directory]   (default $MOTUZ_DOCKER_ROOT/secrets)

set -e

SECRETS_DIR="${1:-${MOTUZ_DOCKER_ROOT:-/docker}/secrets}"

(umask 077 && mkdir -p "${SECRETS_DIR}")
chmod 700 "${SECRETS_DIR}"

for name in MOTUZ_DATABASE_PASSWORD MOTUZ_FLASK_SECRET_KEY MOTUZ_SMTP_PASSWORD MOTUZ_DATA_KEYS; do
    path="${SECRETS_DIR}/${name}"
    if [ -e "${path}" ]; then
        continue
    fi
    if [ "${name}" = "MOTUZ_DATA_KEYS" ]; then
        (umask 077 && printf 'k1:%s' "$(openssl rand -base64 32)" > "${path}")
    else
        (umask 077 && printf '%s' "$(openssl rand -hex 32)" > "${path}")
    fi
    echo "Created ${path}"
done
