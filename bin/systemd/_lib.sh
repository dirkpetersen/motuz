# Shared by bin/systemd/*.sh (sourced). Motuz without docker: README, "Install without
# Docker (Ubuntu 26.04)".

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[0;33mWARNING: %s\033[0m\n' "$*" >&2; }
die() { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# The account's configuration and data (the units use the same paths below %h)
motuz_paths() { # home
    CONFIG_DIR="$1/.config/motuz"
    SETTINGS="$CONFIG_DIR/motuz.env"
    SECRETS="$CONFIG_DIR/secrets.env"
    TRAEFIK_ENV="$CONFIG_DIR/traefik.env"
    BROKER_ENV="$CONFIG_DIR/broker.env"
    DATA_DIR="$1/data"
    PGDATA_DIR="$DATA_DIR/pg"
    CERTS_DIR="$DATA_DIR/certs"
    ACME_DIR="$DATA_DIR/traefik"
    CHECKOUT="$1/motuz"
    VENV="$CHECKOUT/venv"
}

# Secrets of a fresh install; optional ones (empty = not configured) are added to an
# existing secrets.env when missing, like bin/_utils/optional_secrets.sh does for docker
REQUIRED_SECRETS="MOTUZ_FLASK_SECRET_KEY MOTUZ_DATABASE_PASSWORD MOTUZ_SMTP_PASSWORD MOTUZ_REDIS_PASSWORD"
OPTIONAL_SECRETS="MOTUZ_ONEDRIVE_CLIENT_SECRET MOTUZ_GDRIVE_CLIENT_SECRET"

# Value of KEY in a systemd EnvironmentFile (the subset these scripts write: KEY=value,
# KEY="..." with \" \\ escapes, KEY='...'); empty if missing. Never `source` these files:
# bash would expand $ and backquotes in secrets.
env_get() { # file key
    python3 -I -S - "$1" "$2" <<'EOF'
import sys
path, key = sys.argv[1], sys.argv[2]
value = ''
try:
    lines = open(path, encoding='utf-8').read().splitlines()
except FileNotFoundError:
    lines = []
for line in lines:
    line = line.strip()
    if not line or line.startswith(('#', ';')) or '=' not in line:
        continue
    k, v = line.split('=', 1)
    if k.strip() != key:
        continue
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == "'":
        v = v[1:-1]
    elif len(v) >= 2 and v[0] == v[-1] == '"':
        out, i, s = [], 0, v[1:-1]
        while i < len(s):
            if s[i] == '\\' and i + 1 < len(s):
                i += 1
            out.append(s[i]); i += 1
        v = ''.join(out)
    value = v
sys.stdout.write(value)
EOF
}

# KEY="value" for a systemd EnvironmentFile (backslash and double quote escaped)
env_line() { # key value
    python3 -I -S -c 'import sys; k, v = sys.argv[1], sys.argv[2]; assert "\n" not in v and "\x00" not in v, "newline in " + k
print(k + "=\"" + v.replace("\\", "\\\\").replace("\"", "\\\"") + "\"")' "$1" "$2"
}

# Sets KEY in an EnvironmentFile (replaces the line or appends one); keeps the mode
env_set() { # file key value
    local file="$1" key="$2" line tmp
    line=$(env_line "$2" "$3") || die "invalid value for $2"
    tmp=$(mktemp "$file.XXXXXX")
    chmod --reference="$file" "$tmp" 2>/dev/null || chmod 600 "$tmp"
    if grep -q "^$key=" "$file" 2>/dev/null; then
        awk -v k="$key" -v l="$line" 'index($0, k "=") == 1 { print l; next } { print }' "$file" > "$tmp"
    else
        { cat "$file" 2>/dev/null; printf '%s\n' "$line"; } > "$tmp"
    fi
    mv "$tmp" "$file"
}

# Architecture names of the downloads
download_arch() {
    case "$(dpkg --print-architecture 2>/dev/null || uname -m)" in
        amd64|x86_64) echo amd64 ;;
        arm64|aarch64) echo arm64 ;;
        *) die "unsupported architecture $(uname -m) (amd64 and arm64 only)" ;;
    esac
}

# The distribution module bin/systemd/distro/<ID>.sh (ID from /etc/os-release): package
# installation, paths of PostgreSQL and Redis, the PAM stack, firewall hints
load_distro() {
    local module
    # shellcheck source=/dev/null
    . /etc/os-release
    module="$REPO_DIR/bin/systemd/distro/${ID:-unknown}.sh"
    [ -f "$module" ] || die "no support for ${PRETTY_NAME:-this distribution} (no $module); use the docker install"
    # shellcheck source=distro/ubuntu.sh
    source "$module"
    distro_supported || die "this install supports $DISTRO_NAME, found ${PRETTY_NAME:-unknown}; use the docker install"
}

# Copies the user units, with the distribution's paths (@PG_BINDIR@, @REDIS_SERVER@)
install_units() { # destination directory
    local unit
    for unit in "$REPO_DIR"/deployment/systemd/user/motuz*; do
        sed -e "s|@PG_BINDIR@|$PG_BINDIR|g" -e "s|@REDIS_SERVER@|$REDIS_SERVER|g" "$unit" > "$1/$(basename "$unit").new"
        chmod 644 "$1/$(basename "$unit").new"
        mv "$1/$(basename "$unit").new" "$1/$(basename "$unit")"
    done
}

# Pinned versions (deployment/systemd/versions.env; rclone from the app Dockerfile)
load_versions() {
    # shellcheck source=../../deployment/systemd/versions.env
    source "$REPO_DIR/deployment/systemd/versions.env"
    local dockerfile="$REPO_DIR/deployment/docker/app/Dockerfile"
    RCLONE_VERSION=$(sed -n 's/^ARG RCLONE_VERSION=//p' "$dockerfile")
    RCLONE_SHA256_AMD64=$(sed -n 's/^ARG RCLONE_SHA256_AMD64=//p' "$dockerfile")
    RCLONE_SHA256_ARM64=$(sed -n 's/^ARG RCLONE_SHA256_ARM64=//p' "$dockerfile")
    [ -n "$RCLONE_VERSION" ] && [ -n "$RCLONE_SHA256_AMD64" ] || die "no rclone version in $dockerfile"
}
