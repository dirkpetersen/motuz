#!/usr/bin/env bash
# Migration test: the docker e2e stack (test/e2e/run.sh --keep e2e: users, connections,
# copy and integrity-check jobs) is exported with bin/systemd/migrate_from_docker.sh and
# imported into the systemd install of the test VM (test/e2e/run_systemd.sh --keep).
# Then, in the VM: the data is there (row counts, alice's connections and jobs through
# the API), a login token issued by the docker install still works (the Flask key moved
# along), the settings moved (operator on /privacy), and a new copy job runs.
#
# Usage: test/e2e/systemd/migration_test.sh      (needs the VM of run_systemd.sh --keep,
#        the docker images are built from this checkout; the stack is removed at the end)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$E2E/../.." && pwd)"
VM="$HERE/vm.sh"
WORK="$E2E/.work-systemd-migration"
LOGS="$E2E/logs-systemd"
mkdir -p "$LOGS"
rm -rf "$WORK"; mkdir -p "$WORK"; chmod 700 "$WORK"
pass=0; fail=0
check() { if eval "$2"; then echo "PASS $1"; pass=$((pass+1)); else echo "FAIL $1"; fail=$((fail+1)); fi; }
die() { echo "ERROR: $*" >&2; exit 1; }

"$VM" status | grep -q running || die "no VM (run test/e2e/run_systemd.sh --keep first)"

echo "==> docker e2e stack with data, images of this checkout (run.sh --keep e2e)"
MOTUZ_E2E_UI=skip "$E2E/run.sh" --keep e2e > "$LOGS/migration-docker.log" 2>&1 \
    || { tail -5 "$LOGS/migration-docker.log"; die "the docker e2e stack did not start or its e2e suite failed"; }
grep -E '^e2e ' "$LOGS/migration-docker.log" | tail -1
trap '"$E2E/run.sh" --down >/dev/null 2>&1' EXIT

echo "==> a login on the docker install (its tokens must keep working after the move)"
python3 - "$WORK/tokens.json" <<'EOF' || die "login on the docker install failed"
import json, ssl, sys, urllib.request
ctx = ssl._create_unverified_context()
def call(path, body=None, token=None):
    r = urllib.request.Request('https://localhost' + path, data=json.dumps(body).encode() if body else None,
                               method='POST' if body is not None else 'GET')
    r.add_header('Content-Type', 'application/json')
    if token:
        r.add_header('Authorization', 'Bearer ' + token)
    with urllib.request.urlopen(r, context=ctx, timeout=60) as resp:
        return json.loads(resp.read())
tokens = call('/api/auth/login/', {'username': 'alice', 'password': 'AlicePass1'})
connections = sorted(c['name'] for c in call('/api/connections/', token=tokens['access']))
jobs = call('/api/copy-jobs/?page=1&page_size=100', token=tokens['access'])['total']
json.dump({'access': tokens['access'], 'refresh': tokens['refresh'], 'connections': connections, 'copy_jobs': jobs},
          open(sys.argv[1], 'w'))
print('    alice has', len(connections), 'connections and', jobs, 'copy jobs')
EOF

echo "==> export (migrate_from_docker.sh export)"
"$REPO/bin/systemd/migrate_from_docker.sh" export --out "$WORK/export.tar.gz" --repo "$REPO" \
    --env-file "$E2E/.env" --docker-root "$E2E/.work" \
    --db-container "${COMPOSE_PROJECT_NAME:-motuz_e2e}-database-1" --app-container "${COMPOSE_PROJECT_NAME:-motuz_e2e}-app-1" > "$LOGS/migration-export.log" 2>&1 \
    || { cat "$LOGS/migration-export.log"; die "export failed"; }
cat "$LOGS/migration-export.log"
check "export file is mode 600" '[ "$(stat -c %a "$WORK/export.tar.gz")" = 600 ]'
check "export holds dump, counts, secrets, settings, mounts" \
    'tar -tzf "$WORK/export.tar.gz" | grep -qx export/motuz.dump && tar -tzf "$WORK/export.tar.gz" | grep -qx export/counts.txt && tar -tzf "$WORK/export.tar.gz" | grep -qx export/secrets/MOTUZ_FLASK_SECRET_KEY && tar -tzf "$WORK/export.tar.gz" | grep -qx export/settings.env && tar -tzf "$WORK/export.tar.gz" | grep -qx export/mounts.txt'
check "no database password in the export" '! tar -tzf "$WORK/export.tar.gz" | grep -q MOTUZ_DATABASE_PASSWORD'

echo "==> the docker install stops (run.sh --down)"
"$E2E/run.sh" --down >/dev/null 2>&1
trap - EXIT

echo "==> import in the VM (as motuz)"
"$VM" ssh "sudo install -o motuz -g motuz -m 600 /dev/stdin /var/lib/motuz/motuz-export.tar.gz" < "$WORK/export.tar.gz"
"$VM" ssh "sudo install -m 600 /dev/stdin /root/motuz-migration-tokens.json" < "$WORK/tokens.json"
"$VM" ssh "sudo -iu motuz /var/lib/motuz/motuz/bin/systemd/migrate_from_docker.sh import /var/lib/motuz/motuz-export.tar.gz" \
    > "$LOGS/migration-import.log" 2>&1
rc=$?
grep -vE '^\s*$' "$LOGS/migration-import.log" | grep -vE 'webpack|WARNING in|lazy load|code-splitting|^\s+[a-z]' | tail -25
check "import succeeded (row counts match, deploy healthy)" '[ $rc = 0 ] && grep -q "row counts match" "$LOGS/migration-import.log" && grep -q "Motuz is up" "$LOGS/migration-import.log"'

echo "==> checks in the VM"
"$VM" root-script "$HERE/migration_check.py.sh" > "$LOGS/migration-check.log" 2>&1
cat "$LOGS/migration-check.log"
pass=$((pass + $(grep -c '^PASS' "$LOGS/migration-check.log"))); fail=$((fail + $(grep -c '^FAIL' "$LOGS/migration-check.log")))
"$VM" ssh "sudo rm -f /var/lib/motuz/motuz-export.tar.gz /root/motuz-migration-tokens.json"
rm -rf "$WORK"

echo; echo "$pass/$((pass + fail)) passed"
[ "$fail" = 0 ]
