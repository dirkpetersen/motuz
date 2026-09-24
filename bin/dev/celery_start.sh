#!/usr/bin/env bash

set -e

THIS_DIR=$(dirname "$0")
cd ${THIS_DIR}
cd ../..

source venv/bin/activate
source bin/_utils/load_env.sh

cd src/backend

celery -A api.tasks worker -l info
