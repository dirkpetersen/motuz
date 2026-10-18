#!/usr/bin/env bash
# ExecStart of motuz-celery.service (systemd install): the Celery worker and, next to
# it, the runner of the scheduled repeating copies (README, "Scheduled copies";
# `manage.py schedules run --loop`, a tick every minute), as the Docker celery
# container does (deployment/docker/celery/celery-entrypoint.sh). One runner per
# installation: one motuz-celery service (claims are SKIP LOCKED anyway).
#
# exec keeps the Celery worker the service's main process, so stopping the service is
# still a warm shutdown (KillMode=mixed: SIGTERM to Celery only); the runner loop is
# killed with the rest of the unit afterwards.
set -euo pipefail

venv="$HOME/motuz/venv"
cd "$HOME/motuz/src/backend"

( while true; do "$venv/bin/python" manage.py schedules run --loop; sleep 30; done ) &

exec "$venv/bin/celery" -A api.tasks worker -l info
