#!/usr/bin/env bash
# Entrypoint of the e2e remote worker container (suite `worker`): the test users (users.sh,
# same uids, homes on the shared `homes` volume), an unprivileged `motuz` account with the
# sudoers rule a real worker has, its credential (mode 600) and a CA bundle with the
# stack's self-signed certificate; then motuz-worker as `motuz`, with the arguments given.
set -e
id motuz >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/motuz --shell /usr/sbin/nologin motuz
# rclone runs as the job's owner: `sudo -E -u <owner>` (ALL implies SETENV), never as root
echo 'motuz ALL=(ALL,!root) NOPASSWD: ALL' > /etc/sudoers.d/motuz-worker
chmod 440 /etc/sudoers.d/motuz-worker
CONF=/var/lib/motuz/.config/motuz-worker
install -d -o motuz -g motuz -m 700 /var/lib/motuz/.config "$CONF"
if [ -s /run/motuz-worker/credential ]; then
    install -o motuz -g motuz -m 600 /run/motuz-worker/credential "$CONF/credential"
fi
# World-readable: rclone reads it as the job's owner (SSL_CERT_FILE)
install -d -m 755 /etc/motuz-worker
cat /etc/ssl/certs/ca-certificates.crt /run/motuz-worker/central.crt > /etc/motuz-worker/ca.pem
chmod 644 /etc/motuz-worker/ca.pem
exec /users.sh runuser -u motuz -- python3 -u /app/src/worker/motuz_worker.py "$@"
