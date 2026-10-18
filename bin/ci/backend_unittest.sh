#!/usr/bin/env bash

set -e

THIS_DIR=$(dirname "$0")
cd ${THIS_DIR}
cd ../..


source venv/bin/activate
source bin/_utils/load_env.sh

# The key for the encrypted columns (the tests never use a real one)
export MOTUZ_DATA_KEYS="${MOTUZ_DATA_KEYS:-k1:$(openssl rand -base64 32)}"

cd src/backend
python manage.py test
