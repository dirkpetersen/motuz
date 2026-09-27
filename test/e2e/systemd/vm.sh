#!/usr/bin/env bash
# A throwaway Ubuntu 26.04 VM for the systemd install tests (test/e2e/run_systemd.sh):
# the official cloud image (SHA256 checked against Ubuntu's SHA256SUMS), cloud-init
# with a user `tester` (passwordless sudo, ssh key), QEMU/KVM run inside a docker
# container (test/e2e/systemd/qemu.Dockerfile), so only docker and /dev/kvm are needed.
# Only ssh is forwarded, to 127.0.0.1:$MOTUZ_E2E_VM_SSH_PORT (default 2222).
#
# Usage: test/e2e/systemd/vm.sh up|down|status
#        test/e2e/systemd/vm.sh ssh [command...]
#        test/e2e/systemd/vm.sh root-script FILE [args...]   (runs a local script as root in the VM)
#        test/e2e/systemd/vm.sh push REPO DEST               (copies a working tree into the VM)
#
# Environment:
#   MOTUZ_E2E_VM_WORK      disk overlay, seed, ssh key, console log (default test/e2e/.work-systemd)
#   MOTUZ_E2E_IMAGE_CACHE  downloaded cloud image (default ~/.cache/motuz-e2e)
#   MOTUZ_E2E_VM_SSH_PORT  (2222), MOTUZ_E2E_VM_MEMORY (MiB, 6144), MOTUZ_E2E_VM_CPUS (4)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${MOTUZ_E2E_VM_WORK:-$HERE/../.work-systemd}"
CACHE="${MOTUZ_E2E_IMAGE_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/motuz-e2e}"
IMAGE_URL=https://cloud-images.ubuntu.com/releases/26.04/release
IMAGE=ubuntu-26.04-server-cloudimg-amd64.img
CONTAINER=motuz-e2e-vm
QEMU_IMAGE=motuz-e2e-qemu
SSH_PORT="${MOTUZ_E2E_VM_SSH_PORT:-2222}"
MEMORY="${MOTUZ_E2E_VM_MEMORY:-6144}"
CPUS="${MOTUZ_E2E_VM_CPUS:-4}"

die() { echo "ERROR: $*" >&2; exit 1; }

vm_ssh() {
    ssh -i "$WORK/id_ed25519" -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout=5 -o ServerAliveInterval=30 tester@127.0.0.1 "$@"
}

fetch_image() {
    mkdir -p "$CACHE"
    curl -fsSL -o "$CACHE/SHA256SUMS.new" "$IMAGE_URL/SHA256SUMS" || die "could not fetch $IMAGE_URL/SHA256SUMS"
    local want
    want=$(awk -v f="$IMAGE" '$2 == f || $2 == "*" f {print $1}' "$CACHE/SHA256SUMS.new")
    [ -n "$want" ] || die "$IMAGE is not listed in $IMAGE_URL/SHA256SUMS"
    if [ ! -f "$CACHE/$IMAGE" ] || [ "$(sha256sum < "$CACHE/$IMAGE" | cut -d' ' -f1)" != "$want" ]; then
        echo "downloading $IMAGE_URL/$IMAGE"
        curl -fSL -o "$CACHE/$IMAGE.part" "$IMAGE_URL/$IMAGE"
        [ "$(sha256sum < "$CACHE/$IMAGE.part" | cut -d' ' -f1)" = "$want" ] || die "SHA256 mismatch for $IMAGE"
        mv "$CACHE/$IMAGE.part" "$CACHE/$IMAGE"
    fi
    mv "$CACHE/SHA256SUMS.new" "$CACHE/SHA256SUMS"
}

in_qemu_image() { # runs a command in the qemu image as the calling user, with $WORK and the cache
    docker run --rm --user "$(id -u):$(id -g)" -v "$WORK:/work" -v "$CACHE:/images:ro" "$QEMU_IMAGE" "$@"
}

up() {
    [ -e /dev/kvm ] || die "/dev/kvm not found (no hardware virtualization)"
    docker inspect "$CONTAINER" >/dev/null 2>&1 && die "$CONTAINER exists already ($0 down)"
    fetch_image
    docker build -q -t "$QEMU_IMAGE" -f "$HERE/qemu.Dockerfile" "$HERE" >/dev/null
    rm -rf "$WORK"
    mkdir -p "$WORK"
    ssh-keygen -q -t ed25519 -N '' -C motuz-e2e -f "$WORK/id_ed25519"
    cat > "$WORK/user-data" <<EOF
#cloud-config
users:
  - name: tester
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys: ["$(cat "$WORK/id_ed25519.pub")"]
ssh_pwauth: false
EOF
    printf 'instance-id: motuz-e2e\nlocal-hostname: motuz-e2e\n' > "$WORK/meta-data"
    in_qemu_image cloud-localds /work/seed.img /work/user-data /work/meta-data
    in_qemu_image qemu-img create -q -f qcow2 -F qcow2 -b "/images/$IMAGE" /work/disk.qcow2 40G
    docker run -d --name "$CONTAINER" --user "$(id -u):$(id -g)" --group-add "$(stat -c %g /dev/kvm)" \
        --device /dev/kvm --network host -v "$WORK:/work" -v "$CACHE:/images:ro" "$QEMU_IMAGE" \
        qemu-system-x86_64 -enable-kvm -cpu host -smp "$CPUS" -m "$MEMORY" \
            -drive file=/work/disk.qcow2,if=virtio -drive file=/work/seed.img,if=virtio,format=raw \
            -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22" -device virtio-net-pci,netdev=net0 \
            -display none -serial file:/work/console.log >/dev/null
    for _ in $(seq 120); do
        vm_ssh true 2>/dev/null && break
        docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true || die "qemu exited: $(docker logs "$CONTAINER" 2>&1 | tail -5)"
        sleep 3
    done
    vm_ssh true || die "no ssh into the VM (console: $WORK/console.log)"
    vm_ssh cloud-init status --wait >/dev/null || true
    echo "VM up: $0 ssh"
}

down() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    ssh) shift; vm_ssh "$@" ;;
    root-script) # runs a local script file as root in the VM: root-script FILE [args...]
        shift; f="$1"; shift
        vm_ssh sudo bash -s -- "$@" < "$f" ;;
    push) # copies the repository's working tree (tracked and new files, as in the checkout,
          # without ignored ones) to DEST in the VM, replacing it: push REPO DEST
        shift; src="$1"; dest="$2"
        (cd "$src" && git ls-files -co --exclude-standard -z | tar --null -T - -czf -) \
            | vm_ssh "sudo rm -rf '$dest' && sudo mkdir -p '$dest' && sudo tar -xzf - -C '$dest' --no-same-owner" ;;
    status) docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo absent ;;
    *) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
