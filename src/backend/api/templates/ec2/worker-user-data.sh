#!/bin/bash
# Motuz temporary EC2 worker (README "Temporary EC2 workers", deployment/aws/README.md).
# Filled in for one job by src/backend/api/managers/ec2_launcher.py (build_user_data) and
# passed as the user data of an Amazon Linux 2027 instance from the launch template
# motuz-worker(-arm64): restrict instance metadata to root, schedule the maximum runtime
# shutdown, install rclone and Motuz, run motuz-worker for exactly this job, shut down.
# The template sets InstanceInitiatedShutdownBehavior=terminate, so every shutdown
# terminates the instance. The only secret is the single-use, job-bound, short-lived
# bootstrap token (near the end); it is never printed, and this script never uses set -x.
#
# Hook for a prebuilt AMI or the AL2027 installer's worker-only mode: when
# /usr/local/bin/rclone already is this rclone version and /opt/motuz/.motuz-source holds
# "<source url> <ref>", the download steps are skipped; everything else stays the same.
set -uo pipefail
umask 022
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8

CENTRAL_URL=@@CENTRAL_URL@@
POOL=@@POOL@@
JOB=@@JOB@@
SOURCE_URL=@@SOURCE_URL@@
SOURCE_REF=@@SOURCE_REF@@
MOTUZ_VERSION=@@MOTUZ_VERSION@@
RCLONE_VERSION=@@RCLONE_VERSION@@
RCLONE_SHA256_AMD64=@@RCLONE_SHA256_AMD64@@
RCLONE_SHA256_ARM64=@@RCLONE_SHA256_ARM64@@
MAX_RUNTIME_MINUTES=@@MAX_RUNTIME_MINUTES@@
ONCE_WAIT=@@ONCE_WAIT@@
HALT_DELAY_MINUTES=@@HALT_DELAY_MINUTES@@

say() { echo "motuz-boot: $*"; }
halt() {
    say "FAILED: $*"
    if [ "$HALT_DELAY_MINUTES" -gt 0 ]; then
        say "shutting down in $HALT_DELAY_MINUTES min (MOTUZ_EC2_HALT_DELAY)"
        shutdown -h "+$HALT_DELAY_MINUTES" "motuz: worker setup failed"
    else
        say "shutting down"
        shutdown -h now "motuz: worker setup failed"
    fi
    exit 1
}
say "job $JOB of $CENTRAL_URL (pool $POOL), Motuz $MOTUZ_VERSION at $SOURCE_REF"

# 1. Instance metadata (IMDS) for root only, before anything else: it serves the user
#    data (the bootstrap token) and the instance role's credentials, and the job's rclone
#    runs as an unprivileged account on this machine.
imds_root_only() {
    if command -v nft >/dev/null 2>&1; then
        nft -f - <<'NFT'
table inet motuz_imds {
    chain output {
        type filter hook output priority 0; policy accept;
        ip daddr 169.254.169.254 meta skuid != 0 reject
        ip6 daddr fd00:ec2::254 meta skuid != 0 reject
    }
}
NFT
    elif command -v iptables >/dev/null 2>&1; then
        iptables -I OUTPUT -d 169.254.169.254 -m owner ! --uid-owner 0 -j REJECT || return 1
        if command -v ip6tables >/dev/null 2>&1; then
            ip6tables -I OUTPUT -d fd00:ec2::254 -m owner ! --uid-owner 0 -j REJECT || return 1
        fi
    else
        return 1
    fi
}
imds_root_only || { dnf -y -q install nftables >/dev/null 2>&1 && imds_root_only; } \
    || halt "cannot restrict instance metadata to root (neither nft nor iptables)"
say "instance metadata firewall: $(command -v nft >/dev/null 2>&1 && echo nftables || echo iptables) rule for non-root users"

# 2. Backstop for the central node's MOTUZ_EC2_MAX_RUNTIME (its reaper acts first)
shutdown -h "+$MAX_RUNTIME_MINUTES" "motuz: maximum runtime reached" >/dev/null 2>&1 \
    || halt "cannot schedule the maximum runtime shutdown"
say "maximum runtime: shutdown in $MAX_RUNTIME_MINUTES min"

# 3. Accounts: motuz runs the agent; motuzjob runs rclone (the job's owner does not exist
#    here; only cloud-to-cloud jobs with stored credentials come to EC2 workers)
for account in motuz motuzjob; do
    id "$account" >/dev/null 2>&1 \
        || useradd --system --create-home --home-dir "/var/lib/$account" --shell /sbin/nologin "$account" \
        || halt "cannot create the account $account"
done
printf 'motuz ALL=(motuzjob) NOPASSWD:SETENV: /usr/local/bin/rclone\n' > /etc/sudoers.d/motuz-worker
chmod 440 /etc/sudoers.d/motuz-worker
visudo -cf /etc/sudoers.d/motuz-worker >/dev/null || halt "invalid sudoers rule"
if runuser -u motuzjob -- curl -s -m 5 -o /dev/null -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
        http://169.254.169.254/latest/api/token; then
    halt "instance metadata is reachable by users other than root"
fi
if curl -s -m 5 -o /dev/null -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token; then
    say "instance metadata: reachable by root, refused for motuzjob (checked)"
else
    say "instance metadata: refused for motuzjob (checked); root cannot reach it either"
fi

# 4. rclone, the release the central node pins, checksum verified
case "$(uname -m)" in
    aarch64) ARCH=arm64; RCLONE_SHA256=$RCLONE_SHA256_ARM64 ;;
    x86_64) ARCH=amd64; RCLONE_SHA256=$RCLONE_SHA256_AMD64 ;;
    *) halt "unsupported architecture $(uname -m)" ;;
esac
if [ "$(/usr/local/bin/rclone version 2>/dev/null | head -n 1)" != "rclone v$RCLONE_VERSION" ]; then
    W=$(mktemp -d) || halt "mktemp"
    NAME="rclone-v$RCLONE_VERSION-linux-$ARCH"
    curl -fsS --retry 5 --retry-all-errors --connect-timeout 20 -o "$W/rclone.zip" \
        "https://downloads.rclone.org/v$RCLONE_VERSION/$NAME.zip" || halt "rclone download failed"
    echo "$RCLONE_SHA256  $W/rclone.zip" | sha256sum -c --quiet - || halt "rclone checksum mismatch"
    python3 -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extract(sys.argv[2], sys.argv[3])' \
        "$W/rclone.zip" "$NAME/rclone" "$W" || halt "cannot unpack rclone"
    install -m 755 -o root -g root "$W/$NAME/rclone" /usr/local/bin/rclone || halt "cannot install rclone"
    rm -rf "$W"
    if command -v restorecon >/dev/null 2>&1; then restorecon /usr/local/bin/rclone; fi
fi
say "$(/usr/local/bin/rclone version 2>/dev/null | head -n 1) at /usr/local/bin/rclone"

# 5. Motuz at the central node's commit (MOTUZ_WORKER_SOURCE / MOTUZ_WORKER_SOURCE_REF)
if [ "$(cat /opt/motuz/.motuz-source 2>/dev/null)" != "$SOURCE_URL $SOURCE_REF" ]; then
    rm -rf /opt/motuz && install -d -m 755 /opt/motuz || halt "cannot create /opt/motuz"
    case "$SOURCE_URL" in
        https://github.com/*)
            # GitHub's archive of the ref: no git needed; the archive names its commit
            REPO=${SOURCE_URL#https://github.com/}; REPO=${REPO%.git}
            W=$(mktemp -d) || halt "mktemp"
            curl -fsS --retry 5 --retry-all-errors --connect-timeout 20 -o "$W/motuz.tar.gz" \
                "https://codeload.github.com/$REPO/tar.gz/$SOURCE_REF" || halt "Motuz download failed"
            COMMIT=$(python3 -I - "$W/motuz.tar.gz" /opt/motuz "$SOURCE_REF" <<'PY'
import re, sys, tarfile
archive, target, ref = sys.argv[1:]
with tarfile.open(archive) as tar:
    commit = tar.pax_headers.get('comment', '')
    if re.fullmatch(r'[0-9a-f]{40}', ref) and commit != ref:
        sys.exit('the archive is commit {}, not {}'.format(commit or '?', ref))
    members = []
    for member in tar.getmembers():
        top, _, rest = member.name.partition('/')
        if rest:
            member.name = rest
            members.append(member)
    kwargs = {'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}
    tar.extractall(target, members=members, **kwargs)
print(commit)
PY
            ) || halt "cannot unpack Motuz $SOURCE_REF"
            rm -rf "$W"
            ;;
        *)
            command -v git >/dev/null 2>&1 || dnf -y -q install git >/dev/null 2>&1 || halt "git is missing"
            git -C /opt/motuz init -q && git -C /opt/motuz fetch -q --depth 1 "$SOURCE_URL" "$SOURCE_REF" \
                && git -C /opt/motuz checkout -q --detach FETCH_HEAD || halt "cannot fetch Motuz $SOURCE_REF"
            COMMIT=$(git -C /opt/motuz rev-parse HEAD)
            case "$SOURCE_REF" in
                [0-9a-f]*) [ ${#SOURCE_REF} -ne 40 ] || [ "$COMMIT" = "$SOURCE_REF" ] || halt "fetched $COMMIT, not $SOURCE_REF" ;;
            esac
            rm -rf /opt/motuz/.git
            ;;
    esac
    echo "$SOURCE_URL $SOURCE_REF" > /opt/motuz/.motuz-source
    say "Motuz source: $SOURCE_URL commit ${COMMIT:-?}"
fi
grep -q "^VERSION = '$MOTUZ_VERSION'" /opt/motuz/src/backend/api/version.py \
    || halt "Motuz at $SOURCE_REF is not version $MOTUZ_VERSION like the central node"

# 6. The bootstrap token, readable only by motuz; motuz-worker exchanges it once
install -d -o motuz -g motuz -m 700 /run/motuz-worker || halt "cannot create /run/motuz-worker"
( umask 077; cat > /run/motuz-worker/bootstrap ) <<'MOTUZ_BOOTSTRAP_TOKEN'
@@TOKEN@@
MOTUZ_BOOTSTRAP_TOKEN
chown motuz:motuz /run/motuz-worker/bootstrap && chmod 400 /run/motuz-worker/bootstrap || halt "token file"

# 7. motuz-worker --once as motuz (rclone as motuzjob), then shut down whatever happened
cat > /usr/local/sbin/motuz-worker-once <<'RUN'
#!/bin/bash
# $1 central URL, $2 pool, $3 seconds to wait for the job, $4 minutes to wait before
# shutting down after a failure
runuser -u motuz -- env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/var/lib/motuz LANG=C.UTF-8 \
    MOTUZ_CENTRAL_URL="$1" MOTUZ_WORKER_POOL="$2" MOTUZ_WORKER_ONCE_WAIT="$3" MOTUZ_WORKER_RUN_AS=motuzjob \
    python3 -I /opt/motuz/src/worker/motuz_worker.py --bootstrap-token-file /run/motuz-worker/bootstrap --once
rc=$?
rm -f /run/motuz-worker/bootstrap
echo "motuz-boot: motuz-worker exited with status $rc"
if [ "$rc" != 0 ] && [ "$4" -gt 0 ]; then
    echo "motuz-boot: shutting down in $4 min (MOTUZ_EC2_HALT_DELAY)"
    shutdown -h "+$4" "motuz: worker failed"
else
    echo "motuz-boot: shutting down"
    shutdown -h now "motuz: worker done"
fi
RUN
chmod 755 /usr/local/sbin/motuz-worker-once
systemd-run --unit=motuz-worker --no-block -p StandardOutput=journal+console -p StandardError=journal+console \
    /usr/local/sbin/motuz-worker-once "$CENTRAL_URL" "$POOL" "$ONCE_WAIT" "$HALT_DELAY_MINUTES" \
    || halt "cannot start motuz-worker"
say "motuz-worker started (systemd unit motuz-worker)"
