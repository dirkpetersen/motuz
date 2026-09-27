#!/usr/bin/env bash
# A remote worker installed with `install.sh --worker-only` (README, "Remote workers on
# AL2027"), against a central systemd install left running by
#     test/e2e/run_systemd.sh --distro=al2027 --keep
# A second AL2027 container shares the central container's network namespace, so the
# worker reaches Traefik as https://localhost (its self-signed certificate in
# MOTUZ_CA_BUNDLE). Checks: the non-interactive install (twice: idempotent, then with
# --credential-file, which starts motuz-worker.service), a local copy job of alice run on
# the worker host (files owned by alice there, not on the central host), and a temporary
# worker: worker_once.sh with a bootstrap token bound to one job. Prints PASS/FAIL lines
# and "<passed>/<total> passed". The worker container is removed at the end (--keep keeps it).
# With --inside it runs as root on a machine with the central install (e.g. an EC2 instance
# after run_systemd.sh --inside): the worker is the same account on the same host, with
# MOTUZ_HOME = this checkout (the checks that need two hosts are left out).
#
# Usage: test/e2e/systemd/worker_host_test.sh [--keep | --inside]

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
CENTRAL=motuz-e2e-al2027
WORKER=motuz-e2e-al2027-worker
KEEP=0; INSIDE=0
case "${1:-}" in --keep) KEEP=1 ;; --inside) INSIDE=1 ;; esac

pass=0; fail=0
check() { # name, condition (a shell word list evaluated with eval), details
    if eval "$2"; then echo "PASS $1"; pass=$((pass+1)); else echo "FAIL $1 ${3:-}"; fail=$((fail+1)); fi
}
if [ "$INSIDE" = 1 ]; then
    central() { bash -c "$*"; }
    worker() { bash -c "$*"; }
    WORKER_HOME="$REPO"
else
    central() { MOTUZ_E2E_CONTAINER=$CENTRAL "$HERE/container.sh" ssh "$@"; }
    worker() { MOTUZ_E2E_CONTAINER=$WORKER "$HERE/container.sh" ssh "$@"; }
    WORKER_HOME=/opt/motuz
    cleanup() {
        [ "$KEEP" = 1 ] || MOTUZ_E2E_CONTAINER=$WORKER "$HERE/container.sh" down
    }
    trap cleanup EXIT
    [ "$(MOTUZ_E2E_CONTAINER=$CENTRAL "$HERE/container.sh" status)" = running ] \
        || { echo "ERROR: no $CENTRAL (run test/e2e/run_systemd.sh --distro=al2027 --keep first)" >&2; exit 1; }
fi

# ------------------------------------------------------------------ central: pool onprem
# manage.py and settings as the motuz account, with the units' environment
central 'cat > /root/manage.sh' <<'EOF'
uid=$(id -u motuz)
exec runuser -u motuz -- env XDG_RUNTIME_DIR=/run/user/$uid systemd-run --user --wait --pipe --quiet \
    -p WorkingDirectory=/var/lib/motuz/motuz/src/backend -E PYTHON_ENVIRONMENT=prod \
    -p EnvironmentFile=/var/lib/motuz/.config/motuz/motuz.env -p EnvironmentFile=/var/lib/motuz/.config/motuz/secrets.env \
    -p EnvironmentFile=/var/lib/motuz/.config/motuz/broker.env \
    /var/lib/motuz/motuz/venv/bin/python manage.py "$@"
EOF
central 'cat > /root/api.py' <<'EOF'
# api.py METHOD PATH [JSON]: as alice against https://localhost; prints the JSON answer
import json, ssl, sys, urllib.request, urllib.error
ctx = ssl._create_unverified_context()
def call(method, path, body=None, token=None):
    r = urllib.request.Request('https://localhost' + path, method=method,
                               data=json.dumps(body).encode() if body is not None else None)
    r.add_header('Content-Type', 'application/json')
    if token:
        r.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(r, context=ctx, timeout=60) as resp:
            return json.loads(resp.read() or b'null')
    except urllib.error.HTTPError as e:
        return {'status': e.code, 'body': e.read().decode()[:300]}
token = call('POST', '/api/auth/login/', {'username': 'alice', 'password': 'AlicePass1'})['access']
print(json.dumps(call(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else None, token)))
EOF
central "grep -q '^MOTUZ_LOCAL_JOB_POOL=' /var/lib/motuz/.config/motuz/motuz.env \
    || echo 'MOTUZ_LOCAL_JOB_POOL=onprem' >> /var/lib/motuz/.config/motuz/motuz.env
    runuser -u motuz -- env XDG_RUNTIME_DIR=/run/user/\$(id -u motuz) systemctl --user restart motuz-app
    for i in \$(seq 90); do curl -skf https://localhost/api/system/info/ | grep -q healthy && break; sleep 2; done"
SECRET=$(central 'bash /root/manage.sh workers add w1 --pool onprem 2>/dev/null' | tail -n 1)
check "manage.py workers add prints a worker secret" '[[ "$SECRET" == mzw1.* ]]' "$SECRET"

# ------------------------------------------------------------------ worker host
if [ "$INSIDE" = 0 ]; then
    MOTUZ_E2E_CONTAINER=$WORKER "$HERE/container.sh" down
    MOTUZ_E2E_CONTAINER=$WORKER MOTUZ_E2E_CONTAINER_NETWORK=container:$CENTRAL "$HERE/container.sh" up >/dev/null || exit 1
    MOTUZ_E2E_CONTAINER=$WORKER "$HERE/container.sh" push "$REPO" /opt/motuz || exit 1
    worker 'bash /opt/motuz/test/e2e/users.sh true && dnf -y -q install git >/dev/null'
fi
# The central certificate plus the system roots, readable by everyone (rclone runs as the owner)
central 'cat /var/lib/motuz/data/certs/cert.crt' > "$HERE/../.work-worker-central.crt"
worker 'install -d -m 755 /etc/motuz-worker && cat /etc/pki/tls/certs/ca-bundle.crt - > /etc/motuz-worker/ca.pem && chmod 644 /etc/motuz-worker/ca.pem' \
    < "$HERE/../.work-worker-central.crt"
rm -f "$HERE/../.work-worker-central.crt"

out=$(worker '$WORKER_HOME/bin/systemd/install.sh --worker-only --central-url=https://localhost --pool=onprem 2>&1')
as_motuz_systemctl='runuser -u motuz -- env XDG_RUNTIME_DIR=/run/user/$(id -u motuz) systemctl --user'
check "install.sh --worker-only (no credential): installed, not started" \
    '[[ "$out" == *"Worker installed, not started"* ]] && ! worker "$as_motuz_systemctl is-active --quiet motuz-worker"' "$(tail -3 <<<"$out")"
[ "$INSIDE" = 1 ] || check "no server pieces on the worker host (no Traefik, uv, PostgreSQL, Valkey, PAM service)" \
    'worker "! test -e /usr/local/bin/traefik && ! test -e /usr/local/bin/uv && ! rpm -q postgresql18-server valkey >/dev/null && ! test -e /etc/pam.d/motuz"'
env_file=$(worker 'cat ~motuz/.config/motuz-worker/worker.env; stat -c MODE=%a:%U ~motuz/.config/motuz-worker/worker.env')
check "worker.env: MOTUZ_HOME, central URL, pool, no required mounts, mode 600" \
    'grep -qx "MOTUZ_HOME=\"$WORKER_HOME\"" <<<"$env_file" && grep -qx "MOTUZ_CENTRAL_URL=\"https://localhost\"" <<<"$env_file" \
     && grep -qx "MOTUZ_WORKER_POOL=\"onprem\"" <<<"$env_file" && grep -qx "MOTUZ_REQUIRED_PATHS=\"\"" <<<"$env_file" \
     && grep -qx "MODE=600:motuz" <<<"$env_file"' "$env_file"
worker "sed -i 's|^#\\?MOTUZ_CA_BUNDLE=.*|MOTUZ_CA_BUNDLE=/etc/motuz-worker/ca.pem|' ~motuz/.config/motuz-worker/worker.env"
printf '%s\n' "$SECRET" | worker 'umask 077; cat > /root/credential'
out=$(worker '$WORKER_HOME/bin/systemd/install.sh --worker-only --central-url=https://localhost --pool=onprem --credential-file=/root/credential 2>&1')
check "install.sh --worker-only again with --credential-file starts motuz-worker" '[[ "$out" == *"starting motuz-worker"* ]]' "$(tail -3 <<<"$out")"
check "the credential is mode 600, owned by motuz" 'worker "[ \$(stat -c %a:%U ~motuz/.config/motuz-worker/credential) = 600:motuz ]"'
signed=""
for _ in $(seq 60); do
    signed=$(worker "journalctl --no-pager -o cat _SYSTEMD_USER_UNIT=motuz-worker.service | grep 'signed in as worker w1'")
    [ -z "$signed" ] || break
    sleep 2
done
check "motuz-worker signs in to https://localhost as w1 (pool onprem)" '[ -n "$signed" ]' \
    "$(worker 'journalctl --no-pager -o cat _SYSTEMD_USER_UNIT=motuz-worker.service | tail -5')"
check "motuz-worker runs as motuz (user service)" \
    'worker "ps -eo user=,args= | grep -v grep | grep motuz_worker.py | grep -q ^motuz"'

# ------------------------------------------------------------------ a local job on the worker
worker "runuser -u alice -- sh -c 'mkdir -p /home/alice/whost/sub && echo one > /home/alice/whost/a.txt && echo two > /home/alice/whost/sub/b.txt'"
wait_job() { # id -> final state
    local state=""
    for _ in $(seq 90); do
        state=$(central "python3 /root/api.py GET /api/copy-jobs/$1" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("progress_state"))')
        [ "$state" = PROGRESS ] || [ "$state" = None ] || break
        sleep 2
    done
    echo "$state"
}
JOB=$(central "python3 /root/api.py POST /api/copy-jobs/ '{\"description\": \"worker host\", \"src_resource_path\": \"/home/alice/whost\", \"dst_resource_path\": \"/home/alice/whost-copy\", \"copy_links\": true}'" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
state=$(wait_job "$JOB")
check "copy job $JOB SUCCESS on the worker host" '[ "$state" = SUCCESS ]' "$state"
check "copied on the worker host, owned by alice" \
    'worker "[ \"\$(stat -c %U /home/alice/whost-copy/a.txt /home/alice/whost-copy/sub/b.txt | sort -u)\" = alice ]"'
[ "$INSIDE" = 1 ] || check "not copied on the central host" 'central "! test -e /home/alice/whost-copy"'

# ------------------------------------------------------------------ a temporary worker: worker_once.sh
worker "$as_motuz_systemctl disable --now motuz-worker"
JOB2=$(central "python3 /root/api.py POST /api/copy-jobs/ '{\"description\": \"once\", \"src_resource_path\": \"/home/alice/whost\", \"dst_resource_path\": \"/home/alice/whost-once\", \"copy_links\": true}'" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
TOKEN=$(central "bash /root/manage.sh workers bootstrap --pool onprem --job copy:$JOB2 --ttl 10m 2>/dev/null" | tail -n 1)
check "manage.py workers bootstrap prints a token" '[[ "$TOKEN" == mzb1.* ]]' "$TOKEN"
printf '%s\n' "$TOKEN" | worker 'umask 077; cat > /root/bootstrap-token'
out=$(worker '$WORKER_HOME/bin/systemd/worker_once.sh --bootstrap-token-file=/root/bootstrap-token 2>&1'); rc=$?
check "worker_once.sh runs the job and exits 0" '[ "$rc" = 0 ]' "rc=$rc $(tail -5 <<<"$out")"
state=$(wait_job "$JOB2")
check "copy job $JOB2 SUCCESS through the bootstrap token" '[ "$state" = SUCCESS ]' "$state"
check "the token files are gone" 'worker "! test -e /root/bootstrap-token && ! ls /run/user/*/motuz-bootstrap-token >/dev/null 2>&1"'
check "the once-worker ran as a transient user service of motuz" \
    'worker "journalctl --no-pager -o cat _SYSTEMD_USER_UNIT=motuz-worker-once.service | grep -q signed"'
check "no secret in the worker's journal" \
    'worker "! journalctl --no-pager -o cat | grep -qF -e \"$SECRET\" -e \"$TOKEN\""'

echo; echo "$pass/$((pass + fail)) passed"
[ "$fail" = 0 ]
