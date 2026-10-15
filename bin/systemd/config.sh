#!/usr/bin/env bash
# Edits the settings of the systemd install (~/.config/motuz/motuz.env and secrets.env)
# the safe way; used by the admin and by the Claude Code playbook (README, "Deploy with an
# AI assistant (Claude Code)"). Run as the motuz account.
#
# Usage: bin/systemd/config.sh show               the settings; for secrets only whether they are set
#        bin/systemd/config.sh set KEY VALUE      a MOTUZ_* setting in motuz.env (not a secret)
#        bin/systemd/config.sh ask-secret KEY     reads the value from the terminal without echo
#                                                 (/dev/tty, never from arguments or a pipe) and
#                                                 stores it in secrets.env
# After changes: bin/systemd/deploy.sh (restarts Motuz). Secrets are never printed.

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

[ "$(id -u)" != 0 ] || die "run as the motuz account, not as root"
motuz_paths "$HOME"
[ -f "$SETTINGS" ] && [ -f "$SECRETS" ] || die "run bin/systemd/deploy.sh once first (it creates $SETTINGS)"

# Secrets an operator may enter; the generated ones (database, Redis, Flask key) stay as they are
ASKABLE_SECRETS="MOTUZ_SMTP_PASSWORD MOTUZ_ONEDRIVE_CLIENT_SECRET MOTUZ_GDRIVE_CLIENT_SECRET"
# Settings that deploy.sh and install.sh manage; set them by hand in motuz.env if you really must
MANAGED="MOTUZ_DATABASE_MODE MOTUZ_BROKER_MODE MOTUZ_DATABASE_HOST MOTUZ_REDIS_SOCKET MOTUZ_AUTH_HELPER MOTUZ_PAM_SERVICE MOTUZ_SSO_CONFIG_DIR MOTUZ_FRONTEND_DIR"

case "${1:-}" in
    show)
        echo "# $SETTINGS"
        grep -vE '^[[:space:]]*(#|$)' "$SETTINGS" || true
        echo "# $SECRETS (names only)"
        sed -n 's/^\([A-Z0-9_]*\)=\(.*\)$/\1 \2/p' "$SECRETS" | while read -r key value; do
            if [ -n "$value" ] && [ "$value" != '""' ]; then echo "$key: set"; else echo "$key: empty"; fi
        done
        ;;
    set)
        [ $# = 3 ] || die "usage: config.sh set KEY VALUE"
        key="$2"
        [[ "$key" =~ ^MOTUZ_[A-Z0-9_]+$ ]] || die "$key is not a MOTUZ_* setting"
        case "$key" in
            *PASSWORD*|*SECRET*|*TOKEN*) die "$key is a secret: use config.sh ask-secret" ;;
        esac
        case " $MANAGED " in *" $key "*) die "$key is managed by install.sh/deploy.sh" ;; esac
        env_set "$SETTINGS" "$key" "$3"
        echo "$key set in $SETTINGS (run bin/systemd/deploy.sh to apply)"
        ;;
    ask-secret)
        [ $# = 2 ] || die "usage: config.sh ask-secret KEY"
        case " $ASKABLE_SECRETS " in *" $2 "*) ;; *) die "$2 is not one of: $ASKABLE_SECRETS" ;; esac
        [ -r /dev/tty ] && [ -w /dev/tty ] || die "no terminal: run this yourself in a terminal of the motuz account (in Claude Code: type ! and the command)"
        printf '%s (input hidden): ' "$2" > /dev/tty
        IFS= read -rs value < /dev/tty; echo > /dev/tty
        [ -n "$value" ] || die "empty value, nothing changed"
        env_set "$SECRETS" "$2" "$value"
        unset value
        echo "$2 stored in $SECRETS (run bin/systemd/deploy.sh to apply)"
        ;;
    *) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
