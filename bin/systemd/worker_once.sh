#!/usr/bin/env bash
# Runs one job on a temporary worker (README, "Remote workers (HTTPS only)", ephemeral
# workers), after `install.sh --worker-only --central-url=URL [--pool=NAME]`. As root,
# e.g. from EC2 user data:
#     sudo bin/systemd/worker_once.sh --bootstrap-token-file=FILE [--user=NAME]
# The token (from `manage.py workers bootstrap`) is moved into the account's private
# runtime directory (mode 600; FILE is deleted) and `motuz_worker.py --once` runs as a
# transient user service of the account (systemd-run --user --wait, unit
# motuz-worker-once, with ~/.config/motuz-worker/worker.env), i.e. in the same context
# as motuz-worker.service (on SELinux: unconfined_u, never the caller's, e.g.
# cloud-init's). Logs: journalctl _SYSTEMD_USER_UNIT=motuz-worker-once.service.
# Exit code: the worker's (0 done, 1 error, 3 no job, 78 configuration error); the
# token is deleted in any case.

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

ACCOUNT=motuz
TOKEN_FILE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --bootstrap-token-file=*) TOKEN_FILE="${1#*=}" ;;
        --user=*) ACCOUNT="${1#*=}" ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1" ;;
    esac
    shift
done
[ "$(id -u)" = 0 ] || die "run as root"
[ -n "$TOKEN_FILE" ] && [ -s "$TOKEN_FILE" ] || die "--bootstrap-token-file=FILE is missing or empty"
id "$ACCOUNT" >/dev/null 2>&1 || die "no account $ACCOUNT: run install.sh --worker-only first"
HOME_DIR=$(getent passwd "$ACCOUNT" | cut -d: -f6)
WORKER_ENV="$HOME_DIR/.config/motuz-worker/worker.env"
[ -f "$WORKER_ENV" ] || die "no $WORKER_ENV: run install.sh --worker-only first"
MOTUZ_HOME=$(env_get "$WORKER_ENV" MOTUZ_HOME)
[ -f "$MOTUZ_HOME/src/worker/motuz_worker.py" ] || die "MOTUZ_HOME=$MOTUZ_HOME has no src/worker/motuz_worker.py"

uid=$(id -u "$ACCOUNT")
RUNTIME="/run/user/$uid"
for _ in $(seq 30); do # the user manager of a lingering account starts at boot
    [ -S "$RUNTIME/bus" ] && break
    sleep 1
done
[ -S "$RUNTIME/bus" ] || systemctl start "user@$uid.service"
TOKEN="$RUNTIME/motuz-bootstrap-token"
install -m 600 -o "$ACCOUNT" -g "$(id -gn "$ACCOUNT")" "$TOKEN_FILE" "$TOKEN"
rm -f "$TOKEN_FILE"
trap 'rm -f "$TOKEN"' EXIT

log "motuz-worker --once (transient user service motuz-worker-once of $ACCOUNT)"
rc=0
runuser -u "$ACCOUNT" -- env -C "$HOME_DIR" XDG_RUNTIME_DIR="$RUNTIME" DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNTIME/bus" \
    systemd-run --user --wait --collect --quiet --unit=motuz-worker-once \
        --property=EnvironmentFile="$WORKER_ENV" --property=KillMode=mixed --property=TimeoutStopSec=90 \
        /usr/bin/python3 "$MOTUZ_HOME/src/worker/motuz_worker.py" --bootstrap-token-file "$TOKEN" --once \
    || rc=$?
echo "motuz-worker --once exited with $rc"
exit "$rc"
