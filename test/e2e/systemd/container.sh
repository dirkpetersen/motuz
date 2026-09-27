#!/usr/bin/env bash
# A throwaway Amazon Linux 2027 machine for the systemd install tests
# (test/e2e/run_systemd.sh --distro=al2027): the official container image with systemd as
# PID 1 (test/e2e/systemd/al2027.Dockerfile), privileged, in its own network namespace, so
# the suites' ports (80, 443, 5432, 5999, 10000, ...) never touch the host's. Same
# commands as test/e2e/systemd/vm.sh. There is no AL2027 VM image outside EC2; SELinux
# (enforcing on EC2) cannot run in a container and is tested on a real instance (README).
#
# Usage: test/e2e/systemd/container.sh up|down|status
#        test/e2e/systemd/container.sh ssh [command...]        (a shell command, as root)
#        test/e2e/systemd/container.sh root-script FILE [args...]
#        test/e2e/systemd/container.sh push REPO DEST

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER="${MOTUZ_E2E_CONTAINER:-motuz-e2e-al2027}"
IMAGE=motuz-e2e-al2027

die() { echo "ERROR: $*" >&2; exit 1; }

up() {
    docker inspect "$CONTAINER" >/dev/null 2>&1 && die "$CONTAINER exists already ($0 down)"
    docker build -q -t "$IMAGE" -f "$HERE/al2027.Dockerfile" "$HERE" >/dev/null
    # --privileged: systemd, logind and the user managers need cgroups and mounts;
    # a private cgroup namespace and network, hostname motuz-e2e
    docker run -d --name "$CONTAINER" --hostname motuz-e2e --privileged --cgroupns=private \
        --tmpfs /run --tmpfs /run/lock --tmpfs /tmp:exec,mode=1777 --shm-size=1g "$IMAGE" >/dev/null
    for _ in $(seq 60); do
        state=$(docker exec "$CONTAINER" systemctl is-system-running 2>/dev/null || true)
        case "$state" in running|degraded) break ;; esac
        sleep 1
    done
    case "$state" in
        running) ;;
        degraded) echo "systemd degraded: $(docker exec "$CONTAINER" systemctl --failed --plain --no-legend | awk '{print $1}' | xargs)" ;;
        *) die "systemd did not start in $CONTAINER ($state)" ;;
    esac
    echo "container up: $0 ssh"
}

down() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    ssh) shift; docker exec -i "$CONTAINER" bash -c "$*" ;;
    root-script) shift; f="$1"; shift
        docker exec -i "$CONTAINER" bash -s -- "$@" < "$f" ;;
    push) # copies the working tree (tracked and new files, without ignored ones) to DEST
        shift; src="$1"; dest="$2"
        (cd "$src" && git ls-files -co --exclude-standard -z | tar --null -T - -czf -) \
            | docker exec -i "$CONTAINER" bash -c "rm -rf '$dest' && mkdir -p '$dest' && tar -xzf - -C '$dest' --no-same-owner" ;;
    status) docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo absent ;;
    *) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
