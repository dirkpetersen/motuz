#!/usr/bin/env bash

set -e

THIS_DIR=$(dirname "$0")
cd ${THIS_DIR}
cd ../..

# Existing installs get empty files for secrets added later (e.g. the OneDrive client
# secret), otherwise docker-compose refuses to start
./bin/_utils/optional_secrets.sh

# Shut down anything that might still be running. --remove-orphans also removes the
# motuz_nginx container of installs from before Traefik, which would hold ports 80/443.
docker-compose down --remove-orphans

# Migrate the database files to a new PostgreSQL major version if needed (no-op otherwise)
./bin/_utils/upgrade_postgres.sh

# Initialize Database
./bin/_utils/database_install.sh

# Start the application
docker-compose up -d

echo "
Application is building, initializing and starting...
This can take up to 5 minutes

Once complete, you should be able to visit your service using
curl -k https://0.0.0.0/

Use \`docker ps -a\` to check the status
You should see the following services running:
    - motuz_traefik
    - motuz_app
    - motuz_celery
    - motuz_database
    - motuz_rabbitmq
"
