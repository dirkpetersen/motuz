#!/usr/bin/env bash
# Motuz end-to-end tests: builds the images, starts the production-like stack of
# test/e2e/compose.yml (Traefik on :80/:443, host networking, test users alice/bob),
# a fake Microsoft identity platform + Graph (fake_ms.py on 127.0.0.1:5999), runs the
# suites in order and tears everything down again, also when something fails.
#
# Usage: test/e2e/run.sh [--keep] [--no-build] [suite ...]
#        test/e2e/run.sh --down        (tear down a stack left by --keep)
#   --keep      leave the stack and the fake server running for debugging
#   --no-build  use the existing fredhutch/motuz_* images instead of bin/prod/build.sh
#   suite       any of: e2e broker oauth-paste traefik credentials ui oauth-callback
#               (default: all; they always run in this order on a fresh database;
#               ui also runs credentials, whose files in alice's home it uses)
#
# Environment:
#   MOTUZ_E2E_UI=auto|require|skip   UI suite (playwright): auto runs it when node and a
#                                    chromium for playwright are available (default)
#   MOTUZ_E2E_CHROMIUM               chromium executable for playwright (optional)
#   MOTUZ_E2E_AWS_PROFILE, MOTUZ_E2E_AWS_BUCKET, MOTUZ_E2E_AWS_REGION
#                                    credentials suite against real S3 (see cred_test.py);
#                                    without them those checks are skipped
# Needs docker with docker-compose or the compose plugin, python3, curl, openssl, ss and
# free ports 80, 443, 5000, 5001, 5432, 5672, 5999 and 10000. Generated secrets and certs
# live in test/e2e/.work and test/e2e/.env (ignored by git); logs go to test/e2e/logs.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export MOTUZ_E2E_WORK="${MOTUZ_E2E_WORK:-$HERE/.work}"
export MOTUZ_E2E_LOGS="${MOTUZ_E2E_LOGS:-$HERE/logs}"
WORK="$MOTUZ_E2E_WORK"
LOGS="$MOTUZ_E2E_LOGS"
BASE=https://localhost
ALL_SUITES="e2e broker oauth-paste traefik credentials ui oauth-callback"
OWN_APP_CLIENT_ID=motuz-own-app  # expected by oauth_test.py (PHASE=callback)
UI_MODE="${MOTUZ_E2E_UI:-auto}"

usage() { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; }

KEEP=0; BUILD=1; DOWN_ONLY=0; SELECTED=""
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1 ;;
        --no-build) BUILD=0 ;;
        --down) DOWN_ONLY=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown option $1" >&2; usage >&2; exit 2 ;;
        *)
            case " $ALL_SUITES " in
                *" $1 "*) SELECTED="$SELECTED $1" ;;
                *) echo "unknown suite $1 (suites: $ALL_SUITES)" >&2; exit 2 ;;
            esac ;;
    esac
    shift
done
[ -n "$SELECTED" ] || SELECTED="$ALL_SUITES"
case " $SELECTED " in *" ui "*) SELECTED="$SELECTED credentials" ;; esac
SUITES=""
for s in $ALL_SUITES; do
    case " $SELECTED " in *" $s "*) SUITES="$SUITES $s" ;; esac
done

if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
elif docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
else
    echo "ERROR: neither docker-compose nor the docker compose plugin found" >&2; exit 1
fi
export MOTUZ_E2E_COMPOSE="${COMPOSE[*]}"
# Run from test/e2e so compose reads the generated .env there
dc() { (cd "$HERE" && "${COMPOSE[@]}" -f compose.yml "$@"); }

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ------------------------------------------------------------------ fake Microsoft
FAKE_PID_FILE="$WORK/fake/pid"
stop_fake() {
    if [ -f "$FAKE_PID_FILE" ]; then
        local pid; pid=$(cat "$FAKE_PID_FILE")
        kill "$pid" 2>/dev/null && { wait "$pid" 2>/dev/null || true; }
        rm -f "$FAKE_PID_FILE"
    fi
}
start_fake() { # expected client secret; a fresh fake for every suite (refresh tokens restart at REAL-1)
    stop_fake
    mkdir -p "$WORK/fake"
    : > "$WORK/fake/requests.jsonl"
    rm -f "$WORK/fake/mode.txt"
    (umask 077 && printf '%s' "$1" > "$WORK/fake/secret")
    python3 "$HERE/fake_ms.py" "$1" 5999 >>"$LOGS/fake_ms.log" 2>&1 &
    echo $! > "$FAKE_PID_FILE"
    for _ in $(seq 50); do
        python3 -c 'import socket; socket.create_connection(("127.0.0.1", 5999), 0.2).close()' 2>/dev/null && return 0
        sleep 0.2
    done
    die "fake_ms.py did not start (see $LOGS/fake_ms.log)"
}

# ------------------------------------------------------------------ fixtures
write_env() { # OD_CLIENT_ID OD_REDIRECT_URI
    printf 'MOTUZ_DOCKER_ROOT=%s\nOD_CLIENT_ID=%s\nOD_REDIRECT_URI=%s\n' "$WORK" "$1" "$2" > "$HERE/.env"
}

make_fixtures() {
    rm -rf "$WORK"
    (
        umask 077
        mkdir -p "$WORK/secrets" "$WORK/certs" "$WORK/fake"
        printf '%s' "$(openssl rand -hex 16)" > "$WORK/secrets/MOTUZ_DATABASE_PASSWORD"
        printf '%s' "$(openssl rand -hex 32)" > "$WORK/secrets/MOTUZ_FLASK_SECRET_KEY"
        printf '%s' "unused" > "$WORK/secrets/MOTUZ_SMTP_PASSWORD"
        : > "$WORK/secrets/MOTUZ_ONEDRIVE_CLIENT_SECRET"  # empty = rclone's app
        openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj /CN=localhost \
            -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
            -keyout "$WORK/certs/cert.key" -out "$WORK/certs/cert.crt" 2>/dev/null
    ) || die "could not create the fixtures in $WORK"
    write_env "" ""
}

teardown() {
    stop_fake
    if [ -f "$HERE/.env" ]; then
        dc down -v --remove-orphans >/dev/null 2>&1 || true
    fi
    rm -rf "$WORK" "$HERE/.env"
}

wait_healthy() { # the API answers through Traefik and the celery worker is connected to RabbitMQ
    for _ in $(seq 150); do
        if curl -skf "$BASE/api/system/info/" 2>/dev/null | grep -q healthy \
                && dc logs celery 2>/dev/null | grep -q 'celery@.* ready\.'; then
            return 0
        fi
        sleep 2
    done
    dc ps
    die "the stack did not become ready: $BASE/api/system/info/ healthy and celery ready (logs: $LOGS/stack.log)"
}

check_ports() {
    command -v ss >/dev/null 2>&1 || return 0
    local busy
    busy=$(ss -ltnH | awk '{print $4}' | grep -oE ':(80|443|5000|5001|5432|5672|5999|10000)$' | sort -u | tr '\n' ' ')
    [ -z "$busy" ] || die "ports in use: $busy(another Motuz or a stack left by --keep: test/e2e/run.sh --down)"
}

ui_ready() {
    command -v node >/dev/null 2>&1 || { UI_WHY="node not found"; return 1; }
    if [ ! -d "$HERE/ui/node_modules/playwright" ]; then
        (cd "$HERE/ui" && npm ci --no-audit --no-fund) >"$LOGS/ui-npm.log" 2>&1 \
            || { UI_WHY="npm ci in test/e2e/ui failed (see $LOGS/ui-npm.log)"; return 1; }
    fi
    (cd "$HERE/ui" && node --input-type=module -e "
        import { chromium } from 'playwright';
        const b = await chromium.launch({ executablePath: process.env.MOTUZ_E2E_CHROMIUM || undefined });
        await b.close();") >"$LOGS/ui-launch.log" 2>&1 \
        || { UI_WHY="no chromium for playwright (cd test/e2e/ui && npx playwright install chromium)"; return 1; }
}

# ------------------------------------------------------------------ suites
SUMMARY=()
FAILED=()
record() { # suite, text, failed(0/1)
    SUMMARY+=("$(printf '%-15s %s' "$1" "$2")")
    [ "$3" = 0 ] || FAILED+=("$1")
}

run_suite() { # name, command...
    local name="$1"; shift
    log "suite $name"
    local t0=$SECONDS
    (cd "$HERE" && "$@") 2>&1 | tee "$LOGS/$name.log"
    local rc=${PIPESTATUS[0]}
    local line
    line=$(grep -E '^[0-9]+/[0-9]+ passed' "$LOGS/$name.log" | tail -n 1)
    if [ -z "$line" ]; then
        line="ABORTED (exit $rc) after $(grep -c '^PASS' "$LOGS/$name.log") passed, $(grep -c '^FAIL' "$LOGS/$name.log") failed checks"
        [ "$rc" != 0 ] || rc=1
    fi
    record "$name" "$line  ($((SECONDS - t0))s)" "$([ "$rc" = 0 ] && echo 0 || echo 1)"
}

configure_own_app() { # switches the app to an own app registration ("callback" redirect)
    OWN_SECRET="S3cr3t~own+app/=&%-$(openssl rand -hex 6)"
    (umask 077 && printf '%s' "$OWN_SECRET" > "$WORK/secrets/MOTUZ_ONEDRIVE_CLIENT_SECRET")
    write_env "$OWN_APP_CLIENT_ID" "$BASE/api/oauth/onedrive/callback"
    dc up -d --force-recreate app celery || die "could not restart app and celery"
    wait_healthy
}

# ------------------------------------------------------------------ main
STACK_STARTED=0
on_exit() {
    local rc=$?
    trap - EXIT INT TERM
    if [ "$STACK_STARTED" = 1 ]; then
        dc logs --no-color --timestamps > "$LOGS/stack.log" 2>&1 || true
    fi
    if [ "$KEEP" = 1 ] && [ "$STACK_STARTED" = 1 ]; then
        cat <<EOF

Stack left running (--keep). Motuz: $BASE (alice/AlicePass1, bob/BobPass1)
  compose:     cd test/e2e && ${COMPOSE[*]} -f compose.yml ps|logs|exec app bash
  a suite:     cd test/e2e && python3 oauth_test.py   (e2e_test.py needs a fresh database)
  fake_ms.py:  pid $(cat "$FAKE_PID_FILE" 2>/dev/null || echo -), log $LOGS/fake_ms.log; broker_test.py needs a
               fresh one: kill <pid>; python3 fake_ms.py "\$(cat .work/fake/secret)" &
  tear down:   test/e2e/run.sh --down
EOF
    else
        log "tearing down"
        teardown
    fi
    exit "$rc"
}

if [ "$DOWN_ONLY" = 1 ]; then
    teardown
    echo "e2e stack removed"
    exit 0
fi

trap on_exit EXIT
trap 'exit 130' INT TERM

mkdir -p "$LOGS"
rm -f "$LOGS"/*.log "$LOGS"/*.png
log "suites:$SUITES"

# A stack left by --keep or an interrupted run
teardown
make_fixtures
check_ports

if [ "$BUILD" = 1 ]; then
    log "building images (bin/prod/build.sh)"
    "$REPO/bin/prod/build.sh" || die "image build failed"
fi

# rclone's obscured OneDrive client secret, from the ONEDRIVE provider definition
OBSCURED=$(awk '/^ONEDRIVE = /{found=1} found && /rclone_obscured_client_secret=/{match($0, /\x27[^\x27]+\x27/); print substr($0, RSTART+1, RLENGTH-2); exit}' "$REPO/src/backend/api/managers/oauth_manager.py")
RCLONE_SECRET=$(docker run --rm --entrypoint rclone fredhutch/motuz_app:latest reveal "$OBSCURED") \
    && [ -n "$RCLONE_SECRET" ] || die "could not reveal rclone's OneDrive client secret"

log "initializing the database"
STACK_STARTED=1
dc run --rm database_init || die "database initialization failed"

log "starting the stack"
dc up -d database rabbitmq app celery traefik azurite || die "could not start the stack"
wait_healthy
echo "stack is up: $BASE"
start_fake "$RCLONE_SECRET"

for suite in $SUITES; do
    case "$suite" in
        e2e) run_suite e2e python3 -u e2e_test.py ;;
        broker) start_fake "$RCLONE_SECRET"; run_suite broker python3 -u broker_test.py ;;
        oauth-paste) start_fake "$RCLONE_SECRET"; run_suite oauth-paste env PHASE=paste python3 -u oauth_test.py ;;
        traefik) run_suite traefik bash traefik_test.sh ;;
        credentials) run_suite credentials python3 -u cred_test.py ;;
        ui)
            if [ "$UI_MODE" = skip ]; then
                record ui "SKIPPED (MOTUZ_E2E_UI=skip)" 0
            elif UI_WHY="" && ui_ready; then
                start_fake "$RCLONE_SECRET"
                run_suite ui node ui/ui_test.mjs
            elif [ "$UI_MODE" = require ]; then
                record ui "FAILED: $UI_WHY (MOTUZ_E2E_UI=require)" 1
            else
                record ui "SKIPPED: $UI_WHY" 0
            fi ;;
        oauth-callback)
            log "switching to an own OneDrive app registration"
            configure_own_app
            start_fake "$OWN_SECRET"
            run_suite oauth-callback env PHASE=callback python3 -u oauth_test.py ;;
    esac
done

log "summary"
printf '%s\n' "${SUMMARY[@]}"
if [ ${#FAILED[@]} -eq 0 ]; then
    printf '\nALL PASSED\n'
    exit 0
fi
printf '\nFAILED: %s (logs in %s)\n' "${FAILED[*]}" "$LOGS"
exit 1
