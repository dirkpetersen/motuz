#!/usr/bin/env bash
# Installs the pinned rclone, Traefik and uv (deployment/systemd/versions.env; rclone from
# the app Dockerfile) into /var/lib/motuz-bin, SHA256 checked, if they are not those
# versions yet. Run as the service account, which owns that directory (install.sh makes
# it): deploy.sh runs it on every deploy, install.sh --worker-only (rclone only) as the
# account. Root never runs these binaries; the sudoers rule lets the account run this
# rclone as the users.
#
# Usage: bin/systemd/tools.sh [--rclone-only]

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

RCLONE_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --rclone-only) RCLONE_ONLY=1 ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1" ;;
    esac
    shift
done

[ "$(id -u)" != 0 ] || die "run as the service account, not as root"
[ -d "$TOOLS_DIR" ] && [ ! -L "$TOOLS_DIR" ] && [ -O "$TOOLS_DIR" ] \
    || die "$TOOLS_DIR is missing or not $(id -un)'s: run bin/systemd/install.sh as root first"
load_versions
ARCH=$(download_arch)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Renamed into place: a running rclone or Traefik keeps its old file. Mode 755: the users
# run rclone (through sudo).
put() { # file name
    install -m 755 "$1" "$TOOLS_DIR/.$2.new"
    mv -f "$TOOLS_DIR/.$2.new" "$TOOLS_DIR/$2"
}

if [ "$("$TOOLS_DIR/rclone" version 2>/dev/null | head -1)" != "rclone v${RCLONE_VERSION}" ]; then
    log "rclone ${RCLONE_VERSION} ($TOOLS_DIR)"
    sha="RCLONE_SHA256_${ARCH^^}"
    name="rclone-v${RCLONE_VERSION}-linux-${ARCH}"
    fetch_checked "${RCLONE_RELEASES}/v${RCLONE_VERSION}/${name}.zip" "${!sha}" "$TMP/rclone.zip"
    unzip -q "$TMP/rclone.zip" "$name/rclone" -d "$TMP"
    put "$TMP/$name/rclone" rclone
fi
[ "$RCLONE_ONLY" = 0 ] || exit 0

if ! "$TOOLS_DIR/traefik" version 2>/dev/null | grep -qE "^Version:[[:space:]]+${TRAEFIK_VERSION}$"; then
    log "Traefik ${TRAEFIK_VERSION} ($TOOLS_DIR)"
    sha="TRAEFIK_SHA256_${ARCH^^}"
    fetch_checked "https://github.com/traefik/traefik/releases/download/v${TRAEFIK_VERSION}/traefik_v${TRAEFIK_VERSION}_linux_${ARCH}.tar.gz" "${!sha}" "$TMP/traefik.tar.gz"
    tar -xzf "$TMP/traefik.tar.gz" -C "$TMP" traefik
    put "$TMP/traefik" traefik
fi
if [ "$("$TOOLS_DIR/uv" --version 2>/dev/null)" != "uv ${UV_VERSION}" ]; then
    log "uv ${UV_VERSION} ($TOOLS_DIR)"
    sha="UV_SHA256_${ARCH^^}"
    target=$([ "$ARCH" = arm64 ] && echo aarch64 || echo x86_64)-unknown-linux-gnu
    fetch_checked "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}.tar.gz" "${!sha}" "$TMP/uv.tar.gz"
    tar -xzf "$TMP/uv.tar.gz" -C "$TMP"
    put "$TMP/uv-${target}/uv" uv
fi
