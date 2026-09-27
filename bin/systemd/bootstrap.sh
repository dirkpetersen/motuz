#!/usr/bin/env bash
# Installs or updates Motuz on this machine in one step, as root: install.sh, the
# service account's checkout ~/motuz (cloned from --repo/--branch, or fast-forwarded if
# it exists) and deploy.sh as the account. Used by EC2 user-data
# (deployment/aws/user-data-al2027.sh) and fine for any fresh machine; idempotent, so it
# also works as the upgrade command. README, "Install on Amazon Linux 2027 (default on
# EC2)".
#
# Usage: sudo bin/systemd/bootstrap.sh [--repo=URL] [--branch=NAME] [install.sh options]
#   --repo=URL      the repository the account clones (default https://github.com/FredHutch/motuz.git),
#                   or the absolute path of a local clone or mirror the account can read
#   --branch=NAME   its branch (default: the remote's default branch)
#   other options (--local-accounts, --sudo-group=GROUP, --user=NAME, --home=DIR) go to
#   install.sh

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

REPO_URL=https://github.com/FredHutch/motuz.git
BRANCH=""
ACCOUNT=motuz
INSTALL_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --repo=*) REPO_URL="${1#*=}" ;;
        --branch=*) BRANCH="${1#*=}" ;;
        --repo|--branch) die "use $1=VALUE" ;;
        --user=*) ACCOUNT="${1#*=}"; INSTALL_ARGS+=("$1") ;;
        --user) die "use --user=NAME" ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) INSTALL_ARGS+=("$1") ;;
    esac
    shift
done
[ "$(id -u)" = 0 ] || die "run as root"
[[ "$REPO_URL" =~ ^(https://|/)[A-Za-z0-9._/:@-]+$ ]] || die "--repo must be an https URL or an absolute path"
[ -z "$BRANCH" ] || [[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || die "invalid branch name $BRANCH"

"$REPO_DIR/bin/systemd/install.sh" "${INSTALL_ARGS[@]}"

HOME_DIR=$(getent passwd "$ACCOUNT" | cut -d: -f6)
as_account() { # command... (with the account's systemd user manager)
    runuser -u "$ACCOUNT" -- env -C "$HOME_DIR" HOME="$HOME_DIR" XDG_RUNTIME_DIR="/run/user/$(id -u "$ACCOUNT")" "$@"
}
if [ -d "$HOME_DIR/motuz/.git" ]; then
    log "updating $HOME_DIR/motuz"
    as_account git -C "$HOME_DIR/motuz" pull --ff-only
else
    log "cloning $REPO_URL${BRANCH:+ ($BRANCH)} to $HOME_DIR/motuz"
    # A local repository belongs to another user: git's ownership check (of the work
    # tree and of its .git) needs safe.directory
    as_account git -c safe.directory="$REPO_URL" -c safe.directory="$REPO_URL/.git" \
        clone ${BRANCH:+--branch "$BRANCH"} "$REPO_URL" "$HOME_DIR/motuz"
fi
as_account "$HOME_DIR/motuz/bin/systemd/deploy.sh"
