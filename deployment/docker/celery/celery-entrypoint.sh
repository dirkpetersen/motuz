#!/usr/bin/env bash

set -e

# Wait for the database to be ready
./wait-for-it.sh "${MOTUZ_DATABASE_HOST:-0.0.0.0:5432}" -t 0

source ./load-secrets.sh

# Temporary EC2 workers (README, "Temporary EC2 workers"): the reaper, which also
# launches workers for queued jobs, every MOTUZ_EC2_REAP_INTERVAL. A loop next to the
# Celery worker rather than a Celery beat task: copy jobs occupy the worker's processes
# for hours, and the reaper must run anyway. Restarted if it exits.
case "${MOTUZ_EC2_WORKERS:-}" in
    [Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Oo][Nn])
        ( while true; do python3 manage.py ec2 reap --loop; sleep 30; done ) &
        ;;
esac

# Scheduled repeating copies (README, "Scheduled copies"): the runner, which starts the
# due copy jobs every minute. Next to the Celery worker for the same reason, and exactly
# one per installation (one celery container; claims are SKIP LOCKED anyway).
( while true; do python3 manage.py schedules run --loop; sleep 30; done ) &

exec celery -A api.tasks worker -l info
