#!/usr/bin/env bash
# Motuz end-to-end tests against the systemd install (bin/systemd, README "Install
# without Docker (Ubuntu 26.04)") in a real Ubuntu 26.04 VM (test/e2e/systemd/vm.sh:
# the cloud image in QEMU/KVM). It copies this working tree into the VM, runs
# `install.sh --local-accounts` as root, creates the test users alice and bob
# (test/e2e/users.sh), runs `deploy.sh` as motuz with the e2e settings (fake
# Microsoft/Google on 127.0.0.1:5999, Azurite on :10000, both inside the VM), and then
# the SAME suites as test/e2e/run.sh inside the VM with MOTUZ_E2E_TARGET=systemd, plus
# `security` (test/e2e/systemd/security_test.sh: processes, sudo, ports, login helper).
# Logs are copied to test/e2e/logs-systemd; the VM is removed at the end.
# With --distro=al2027 the machine is an Amazon Linux 2027 container with systemd as PID 1
# instead (test/e2e/systemd/container.sh, own network namespace, no SELinux; logs in
# test/e2e/logs-systemd-al2027).
#
# Usage: test/e2e/run_systemd.sh [--distro=ubuntu|al2027] [--keep] [--reuse] [suite ...]
#   --keep    leave the VM running (test/e2e/systemd/vm.sh ssh; vm.sh down removes it)
#   --reuse   use a VM left by --keep: copy the tree, install and deploy again, run suites
#   suite     any of: e2e broker oauth-paste traefik credentials ui oauth-callback
#             ui-callback security (default: all; e2e and broker need a fresh database,
#             i.e. a new VM)
# Environment: MOTUZ_E2E_UI=auto|require|skip as for run.sh (chromium for playwright is
# installed in the VM); vm.sh's MOTUZ_E2E_VM_* variables.
#
# Inside the machine (as root; used by the above, and usable on any test machine with
# the systemd install; it creates the users alice and bob):
#   test/e2e/run_systemd.sh --inside-setup           install, users, deploy with e2e settings
#   test/e2e/run_systemd.sh --inside [suite ...]     the suites

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
UI_MODE="${MOTUZ_E2E_UI:-auto}"
VM="$HERE/systemd/vm.sh"
SRC=/opt/motuz-src   # the copied working tree in the VM
INSIDE_ROOT=/root/motuz-e2e
ACCOUNT=motuz

MODE=host; KEEP=0; REUSE=0; ARGS=(); DISTRO=ubuntu
while [ $# -gt 0 ]; do
    case "$1" in
        --distro=*) DISTRO="${1#*=}" ;;
        --keep) KEEP=1 ;;
        --reuse) REUSE=1 ;;
        --inside) MODE=inside ;;
        --inside-setup) MODE=setup ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option $1" >&2; exit 2 ;;
        *) ARGS+=("$1") ;;
    esac
    shift
done

# ================================================================== host
if [ "$MODE" = host ]; then
    LOGS="$HERE/logs-systemd"
    MACHINE="an Ubuntu 26.04 VM"
    case "$DISTRO" in
        ubuntu) ;;
        al2027) VM="$HERE/systemd/container.sh"; LOGS="$HERE/logs-systemd-al2027"
                MACHINE="an Amazon Linux 2027 container" ;;
        *) echo "unknown --distro=$DISTRO (ubuntu, al2027)" >&2; exit 2 ;;
    esac
    log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
    on_exit() {
        local rc=$?
        trap - EXIT INT TERM
        if "$VM" status 2>/dev/null | grep -q running; then
            mkdir -p "$LOGS"
            "$VM" ssh "sudo tar -C $INSIDE_ROOT -czf - logs 2>/dev/null" 2>/dev/null | tar -C "$LOGS" -xzf - --strip-components=1 2>/dev/null || true
            "$VM" ssh "sudo journalctl --no-pager -o short-iso _UID=\$(id -u $ACCOUNT) 2>/dev/null; sudo journalctl --no-pager -o short-iso -u motuz-auth.socket -u 'motuz-auth@*' 2>/dev/null" > "$LOGS/journal.log" 2>/dev/null || true
            if [ "$KEEP" = 1 ]; then
                echo "VM left running (--keep): $VM ssh; remove it with $VM down"
            else
                log "removing the VM"
                "$VM" down
            fi
        fi
        exit "$rc"
    }
    rm -rf "$LOGS"; mkdir -p "$LOGS"
    trap on_exit EXIT
    trap 'exit 130' INT TERM
    if [ "$REUSE" = 1 ] && "$VM" status | grep -q running; then
        log "reusing the VM"
    else
        log "starting $MACHINE"
        "$VM" up || exit 1
    fi
    log "copying the working tree to $SRC"
    "$VM" push "$REPO" "$SRC" || exit 1
    log "install.sh, users, deploy.sh (in the VM)"
    "$VM" ssh "sudo MOTUZ_E2E_UI=$UI_MODE $SRC/test/e2e/run_systemd.sh --inside-setup" 2>&1 | tee "$LOGS/setup.log"
    [ "${PIPESTATUS[0]}" = 0 ] || { echo "ERROR: setup failed (see $LOGS/setup.log)" >&2; exit 1; }
    log "suites (in the VM)"
    "$VM" ssh "sudo MOTUZ_E2E_UI=$UI_MODE $SRC/test/e2e/run_systemd.sh --inside ${ARGS[*]:-}"
    exit $?
fi

# ================================================================== inside the machine
[ "$(id -u)" = 0 ] || { echo "ERROR: --inside and --inside-setup run as root" >&2; exit 1; }
export MOTUZ_E2E_WORK="$INSIDE_ROOT/work"
export MOTUZ_E2E_LOGS="$INSIDE_ROOT/logs"
WORK="$MOTUZ_E2E_WORK"
LOGS="$MOTUZ_E2E_LOGS"
BASE=https://localhost
OWN_APP_CLIENT_ID=motuz-own-app  # expected by oauth_test.py (PHASE=callback)
source "$HERE/_suites.sh"
mkdir -p "$WORK" "$LOGS"
# Playwright 1.58 has no build for Ubuntu 26.04; its 24.04 chromium works there
. /etc/os-release
if [ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 26.04 ]; then
    export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE="${PLAYWRIGHT_HOST_PLATFORM_OVERRIDE:-ubuntu24.04-x64}"
fi
HOME_DIR=$(getent passwd "$ACCOUNT" | cut -d: -f6 2>/dev/null || true)
CONFIG="$HOME_DIR/.config/motuz"

as_motuz() { # a command as the service account, with its systemd user manager
    local uid; uid=$(id -u "$ACCOUNT")
    runuser -u "$ACCOUNT" -- env -C "$HOME_DIR" HOME="$HOME_DIR" XDG_RUNTIME_DIR="/run/user/$uid" "$@"
}
set_setting() { # file key value (the files are the account's, mode 600)
    local file="$1" key="$2" value="$3"
    python3 -I -S - "$file" "$key" "$value" <<'EOF'
import os, re, sys
path, key, value = sys.argv[1:4]
line = '{}="{}"'.format(key, value.replace('\\', '\\\\').replace('"', '\\"'))
lines = open(path).read().splitlines()
new = [line if re.match(r'^\s*{}\s*='.format(key), l) else l for l in lines]
if new == lines:
    new.append(line)
open(path, 'w').write('\n'.join(new) + '\n')
EOF
}
wait_healthy() {
    for _ in $(seq 120); do
        if curl -skf "$BASE/api/system/info/" 2>/dev/null | grep -q healthy \
                && journalctl --no-pager -o cat _SYSTEMD_USER_UNIT=motuz-celery.service --since "-10min" 2>/dev/null | grep -q 'celery@.* ready\.'; then
            return 0
        fi
        sleep 2
    done
    die "Motuz did not become healthy (journalctl _SYSTEMD_USER_UNIT=motuz-app.service)"
}
restart_motuz() { # units...
    as_motuz systemctl --user restart "$@" || die "could not restart $*"
    wait_healthy
}

if [ "$MODE" = setup ]; then
    log "install.sh --local-accounts"
    "$REPO/bin/systemd/install.sh" --local-accounts > "$LOGS/install.log" 2>&1 \
        || { tail -30 "$LOGS/install.log"; die "install.sh failed (see $LOGS/install.log)"; }
    tail -5 "$LOGS/install.log"
    HOME_DIR=$(getent passwd "$ACCOUNT" | cut -d: -f6)
    CONFIG="$HOME_DIR/.config/motuz"

    log "test users alice and bob (test/e2e/users.sh)"
    bash "$HERE/users.sh" true || die "users.sh failed"

    log "checkout $HOME_DIR/motuz (copy of $REPO; venv, node_modules and build are kept)"
    mkdir -p "$HOME_DIR/motuz"
    tar -C "$REPO" --exclude=./node_modules --exclude=./venv --exclude=./build -cf - . | tar -C "$HOME_DIR/motuz" -xf -
    chown -R "$ACCOUNT:" "$HOME_DIR/motuz"

    log "e2e settings (fake Microsoft/Google, operator) and certificate (CN=localhost)"
    install -d -m 700 -o "$ACCOUNT" -g "$ACCOUNT" "$HOME_DIR/.config" "$CONFIG" "$HOME_DIR/data" "$HOME_DIR/data/certs"
    if [ ! -f "$CONFIG/motuz.env" ]; then
        install -m 600 -o "$ACCOUNT" -g "$ACCOUNT" "$REPO/deployment/systemd/motuz.env.example" "$CONFIG/motuz.env"
        set_setting "$CONFIG/motuz.env" MOTUZ_AUTH_HELPER /run/motuz-auth.sock
    fi
    while IFS='=' read -r key value; do
        set_setting "$CONFIG/motuz.env" "$key" "$value"
    done <<'EOF'
MOTUZ_SMTP_SERVER=127.0.0.1:2525
MOTUZ_SMTP_REQUIRE_SSL=false
MOTUZ_ALERT_ADDRESS=admin@example.com
MOTUZ_ONEDRIVE_TOKEN_URL=http://127.0.0.1:5999/token
MOTUZ_ONEDRIVE_AUTH_URL=http://127.0.0.1:5999/authorize
MOTUZ_GRAPH_URL=http://127.0.0.1:5999/graph
MOTUZ_GDRIVE_AUTH_URL=http://127.0.0.1:5999/google/authorize
MOTUZ_GDRIVE_TOKEN_URL=http://127.0.0.1:5999/google/token
MOTUZ_GDRIVE_API_URL=http://127.0.0.1:5999/google/drive/v3
MOTUZ_OPERATOR_NAME=E2E Lab <R&D>
MOTUZ_CONTACT_EMAIL=motuz-admin@example.org
MOTUZ_OPERATOR_URL=https://example.org/?a=1&b=2
MOTUZ_ONEDRIVE_CLIENT_ID=
MOTUZ_ONEDRIVE_REDIRECT_URI=
MOTUZ_RCLONE_CHECKERS=16
MOTUZ_RCLONE_MAX_TRANSFERS=48
EOF
    if [ ! -f "$HOME_DIR/data/certs/cert.crt" ]; then
        (umask 077 && openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj /CN=localhost \
            -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
            -keyout "$HOME_DIR/data/certs/cert.key" -out "$HOME_DIR/data/certs/cert.crt" 2>/dev/null)
    fi
    chown -R "$ACCOUNT:" "$CONFIG" "$HOME_DIR/data"

    log "deploy.sh (as $ACCOUNT)"
    as_motuz "$HOME_DIR/motuz/bin/systemd/deploy.sh" > "$LOGS/deploy.log" 2>&1 \
        || { tail -30 "$LOGS/deploy.log"; die "deploy.sh failed (see $LOGS/deploy.log)"; }
    tail -12 "$LOGS/deploy.log"
    wait_healthy

    log "test tools: Azurite, playwright + chromium"
    if [ ! -x "$INSIDE_ROOT/azurite/node_modules/.bin/azurite-blob" ]; then
        npm install --silent --no-audit --no-fund --prefix "$INSIDE_ROOT/azurite" azurite@3.37.0 > "$LOGS/azurite-npm.log" 2>&1 \
            || die "could not install Azurite (see $LOGS/azurite-npm.log)"
    fi
    if [ "$UI_MODE" != skip ]; then
        # playwright installs chromium's libraries with apt only; on dnf distributions
        # (Amazon Linux) they come from these packages
        with_deps=--with-deps
        if ! command -v apt-get >/dev/null && command -v dnf >/dev/null; then
            with_deps=""
            dnf -y -q install nss nspr atk at-spi2-atk at-spi2-core cups-libs libdrm libxkbcommon \
                libXcomposite libXdamage libXext libXfixes libXrandr libxshmfence mesa-libgbm pango \
                cairo alsa-lib dbus-libs expat > "$LOGS/ui-deps.log" 2>&1 \
                || echo "WARNING: chromium's libraries did not install (see $LOGS/ui-deps.log)"
        fi
        (cd "$HERE/ui" && npm ci --silent --no-audit --no-fund && npx playwright install $with_deps chromium) \
            > "$LOGS/ui-install.log" 2>&1 || echo "WARNING: playwright/chromium install failed (see $LOGS/ui-install.log)"
    fi
    echo "setup done"
    exit 0
fi

# ------------------------------------------------------------------ suites (inside)
export MOTUZ_E2E_TARGET=systemd
export MOTUZ_E2E_SYSTEMD_CONFIG="$CONFIG"
ALL_SUITES="$ALL_SUITES security"
select_suites "${ARGS[@]:-}"

# rclone's OneDrive and Google Drive client secrets (fake_ms.py checks them)
reveal() { # provider constant in oauth_manager.py
    local obscured
    obscured=$(awk "/^$1 = /{found=1} found && /rclone_obscured_client_secret=/{match(\$0, /\x27[^\x27]+\x27/); print substr(\$0, RSTART+1, RLENGTH-2); exit}" \
        "$REPO/src/backend/api/managers/oauth_manager.py")
    /usr/local/bin/rclone reveal "$obscured"
}
RCLONE_SECRET=$(reveal ONEDRIVE) && [ -n "$RCLONE_SECRET" ] || die "could not reveal rclone's OneDrive client secret"
GDRIVE_SECRET=$(reveal GDRIVE) && [ -n "$GDRIVE_SECRET" ] || die "could not reveal rclone's Google Drive client secret"

AZURITE_PID=""
cleanup_inside() {
    stop_fake
    [ -z "$AZURITE_PID" ] || kill "$AZURITE_PID" 2>/dev/null || true
}
trap cleanup_inside EXIT
# In memory, like compose.yml's (--location is refused together with --inMemoryPersistence)
mkdir -p "$WORK/azurite"
(cd "$WORK/azurite" && exec "$INSIDE_ROOT/azurite/node_modules/.bin/azurite-blob" --blobHost 127.0.0.1 --blobPort 10000 \
    --skipApiVersionCheck --loose --inMemoryPersistence) > "$LOGS/azurite.log" 2>&1 &
AZURITE_PID=$!

OWN_APP=0
configure_own_app() { # switches the app to an own app registration ("callback" redirect), once
    [ "$OWN_APP" = 0 ] || return 0
    OWN_APP=1
    log "switching to an own OneDrive app registration"
    OWN_SECRET="S3cr3t~own+app/=&%-$(openssl rand -hex 6)"
    set_setting "$CONFIG/secrets.env" MOTUZ_ONEDRIVE_CLIENT_SECRET "$OWN_SECRET"
    set_setting "$CONFIG/motuz.env" MOTUZ_ONEDRIVE_CLIENT_ID "$OWN_APP_CLIENT_ID"
    set_setting "$CONFIG/motuz.env" MOTUZ_ONEDRIVE_REDIRECT_URI "$BASE/api/oauth/onedrive/callback"
    restart_motuz motuz-app.service motuz-celery.service
}

wait_healthy
start_fake "$RCLONE_SECRET"
for suite in $SUITES; do
    case "$suite" in
        security) run_suite security bash systemd/security_test.sh ;;
        *) SUITES="$suite" run_suites "$RCLONE_SECRET" ;;
    esac
done
print_summary
