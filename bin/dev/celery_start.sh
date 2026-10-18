#!/usr/bin/env bash

set -e

THIS_DIR=$(dirname "$0")
cd ${THIS_DIR}
cd ../..

source venv/bin/activate
source bin/_utils/load_env.sh

cd src/backend

# The runner of the scheduled copies, as in the production stacks (README, "Scheduled copies")
python manage.py schedules run --loop &
trap 'kill $! 2>/dev/null' EXIT

celery -A api.tasks worker -l info
