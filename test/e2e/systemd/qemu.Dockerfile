# QEMU/KVM for test/e2e/run_systemd.sh: runs the Ubuntu 26.04 cloud image as a real VM.
# The user running the tests needs docker, but no access to /dev/kvm or qemu itself.
FROM ubuntu:26.04
RUN apt-get update -y \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        qemu-system-x86 qemu-utils cloud-image-utils openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
