# Shared by test/e2e/run.sh (the docker stack) and test/e2e/run_systemd.sh (the systemd
# install): the fake Microsoft/Google server, running and recording the suites, and the
# summary. Sourced; the caller sets HERE, WORK, LOGS, UI_MODE, GDRIVE_SECRET and defines
# configure_own_app (which sets OWN_SECRET).

ALL_SUITES="e2e broker oauth-paste traefik credentials ui oauth-callback ui-callback"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# The selected suites in their fixed order; ui also runs credentials (fixtures)
select_suites() { # suite...
    local selected="$*" s
    [ -n "$selected" ] || selected="$ALL_SUITES"
    case " $selected " in *" ui "*) selected="$selected credentials" ;; esac
    SUITES=""
    for s in $ALL_SUITES; do
        case " $selected " in *" $s "*) SUITES="$SUITES $s" ;; esac
    done
}

# ------------------------------------------------------------------ fake Microsoft
FAKE_PID_FILE="$WORK/fake/pid"
stop_fake() {
    if [ -f "$FAKE_PID_FILE" ]; then
        local pid; pid=$(cat "$FAKE_PID_FILE")
        kill "$pid" 2>/dev/null && { wait "$pid" 2>/dev/null || true; }
        rm -f "$FAKE_PID_FILE"
    fi
}
start_fake() { # expected OneDrive client secret; a fresh fake for every suite (refresh tokens restart at REAL-1)
    stop_fake
    mkdir -p "$WORK/fake"
    : > "$WORK/fake/requests.jsonl"
    rm -f "$WORK/fake/mode.txt"
    (umask 077 && printf '%s' "$1" > "$WORK/fake/secret")
    python3 "$HERE/fake_ms.py" "$1" 5999 "$GDRIVE_SECRET" >>"$LOGS/fake_ms.log" 2>&1 &
    echo $! > "$FAKE_PID_FILE"
    for _ in $(seq 50); do
        python3 -c 'import socket; socket.create_connection(("127.0.0.1", 5999), 0.2).close()' 2>/dev/null && return 0
        sleep 0.2
    done
    die "fake_ms.py did not start (see $LOGS/fake_ms.log)"
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

run_ui() { # suite name, PHASE of ui_test.mjs, OneDrive client secret for the fake
    if [ "$UI_MODE" = skip ]; then
        record "$1" "SKIPPED (MOTUZ_E2E_UI=skip)" 0
    elif UI_WHY="" && ui_ready; then
        start_fake "$3"
        run_suite "$1" env PHASE="$2" node ui/ui_test.mjs
    elif [ "$UI_MODE" = require ]; then
        record "$1" "FAILED: $UI_WHY (MOTUZ_E2E_UI=require)" 1
    else
        record "$1" "SKIPPED: $UI_WHY" 0
    fi
}

run_suites() { # rclone's OneDrive client secret; runs $SUITES
    local suite
    for suite in $SUITES; do
        case "$suite" in
            e2e) run_suite e2e python3 -u e2e_test.py ;;
            broker) start_fake "$1"; run_suite broker python3 -u broker_test.py ;;
            oauth-paste) start_fake "$1"; run_suite oauth-paste env PHASE=paste python3 -u oauth_test.py ;;
            traefik) run_suite traefik bash traefik_test.sh ;;
            credentials) run_suite credentials python3 -u cred_test.py ;;
            ui) run_ui ui paste "$1" ;;
            oauth-callback)
                configure_own_app
                start_fake "$OWN_SECRET"
                run_suite oauth-callback env PHASE=callback python3 -u oauth_test.py ;;
            ui-callback)
                configure_own_app
                run_ui ui-callback callback "$OWN_SECRET" ;;
        esac
    done
}

print_summary() { # returns non-zero if a suite failed
    log "summary"
    printf '%s\n' "${SUMMARY[@]}"
    if [ ${#FAILED[@]} -eq 0 ]; then
        printf '\nALL PASSED\n'
        return 0
    fi
    printf '\nFAILED: %s (logs in %s)\n' "${FAILED[*]}" "$LOGS"
    return 1
}
