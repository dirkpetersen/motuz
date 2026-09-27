#!/bin/bash
# EC2 user-data: installs Motuz on a fresh Amazon Linux 2027 instance (the systemd install,
# README "Install on Amazon Linux 2027 (default on EC2)"). Launch with IMDSv2 required
# (--metadata-options HttpTokens=required,HttpPutResponseHopLimit=1), 80/443 open in the
# security group only where users need them, and at least 4 GiB of memory and a 16 GiB
# root volume (the frontend build and uWSGI's compile run on the instance). The output
# goes to /var/log/cloud-init-output.log; about 10 minutes on a c7g.large.
#
# Edit REPO/BRANCH/INSTALL_ARGS below. --local-accounts: users log in with local accounts
# (useradd + passwd); for Active Directory join the instance (realm join / SSSD) and drop it.
set -euo pipefail
REPO=https://github.com/dirkpetersen/motuz.git
BRANCH=dirk
INSTALL_ARGS="--local-accounts"
SRC=/root/motuz-install   # root's own clone: install.sh runs as root, never from the account's checkout

dnf -y -q install git
if [ -d "$SRC/.git" ]; then
    git -C "$SRC" pull -q --ff-only
else
    git clone -q --branch "$BRANCH" "$REPO" "$SRC"
fi
# shellcheck disable=SC2086
"$SRC/bin/systemd/bootstrap.sh" --repo="$REPO" --branch="$BRANCH" $INSTALL_ARGS

# The address to open (instance metadata with an IMDSv2 session token; IMDSv1 is off)
TOKEN=$(curl -fsS -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)
HOST=$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-hostname || true)
[ -n "$HOST" ] || HOST=$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/local-ipv4)
echo "Motuz is installed: https://$HOST/ (self-signed certificate until MOTUZ_ACME_DOMAIN is set in ~motuz/.config/motuz/motuz.env)"
