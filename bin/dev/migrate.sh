#!/usr/bin/env bash

set -e

THIS_DIR=$(dirname "$0")
cd ${THIS_DIR}
cd ../..

source venv/bin/activate
source bin/_utils/load_env.sh
cd src/backend/
python manage.py db migrate

echo -e "\n\nPress ENTER to execute migration. Ctrl+C to abort"
read
python manage.py db upgrade
