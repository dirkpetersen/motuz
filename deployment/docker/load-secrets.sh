#!/usr/bin/env bash

# Exports every file in the secrets directory as an environment variable
# named after the file. Meant to be `source`d.

if [ -z "$1" ]; then
    SECRETS_DIRECTORY="/run/secrets"
else
    SECRETS_DIRECTORY="$1"
fi

if [ -z "$(ls -A "$SECRETS_DIRECTORY" 2> /dev/null)" ]; then
    echo "No secrets found at $SECRETS_DIRECTORY"
    return 0 2> /dev/null || exit 0
fi


echo "$SECRETS_DIRECTORY"

for secret_path in "$SECRETS_DIRECTORY"/*; do
    key=$(basename "$secret_path")
    export "$key=$(cat "$secret_path")"
done
