# Shared by bin/systemd/*.sh (sourced). Motuz without docker: README, "Install (default:
# without Docker)".

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[0;33mWARNING: %s\033[0m\n' "$*" >&2; }
die() { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# The account's pinned rclone (bin/systemd/tools.sh): its own directory, made by install.sh,
# outside its home, which stays closed to the users, who run rclone through sudo. The
# sudoers rule, the units and MOTUZ_RCLONE name this path. Traefik and uv, which only the
# account runs, are in ~/.local/bin of its home.
TOOLS_DIR=/var/lib/motuz-bin

fetch_checked() { # url sha256 file
    curl -fsSL --retry 3 -o "$3" "$1" || die "download failed: $1"
    echo "$2  $3" | sha256sum -c --quiet - || die "SHA256 mismatch: $1"
}

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
    ACCOUNT_BIN="$1/.local/bin"
}

# Secrets of a fresh install; optional ones (empty = not configured) are added to an
# existing secrets.env when missing, like bin/_utils/optional_secrets.sh does for docker
REQUIRED_SECRETS="MOTUZ_FLASK_SECRET_KEY MOTUZ_DATABASE_PASSWORD MOTUZ_SMTP_PASSWORD MOTUZ_REDIS_PASSWORD"
OPTIONAL_SECRETS="MOTUZ_ONEDRIVE_CLIENT_SECRET MOTUZ_GDRIVE_CLIENT_SECRET"

# Value of KEY in a systemd EnvironmentFile (the subset these scripts write: KEY=value,
# KEY="..." with \" \\ escapes, KEY='...'); empty if missing. Never `source` these files:
# bash would expand $ and backquotes in secrets.
ENV_GET_PY=$(cat <<'EOF'
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
)
env_get() { # file key
    python3 -I -S -c "$ENV_GET_PY" "$1" "$2"
}

# What root does with the account's files (install.sh): everything inside the account's
# tree is read and written AS THE ACCOUNT (runuser), never as root, because the account
# controls the names there: a symlink in ~/.config/motuz must not make root chown, read
# or overwrite a file elsewhere. The account's own privileges are all a symlink can reach.
account_env_get() { # file key (ACCOUNT)
    runuser -u "$ACCOUNT" -- python3 -I -S -c "$ENV_GET_PY" "$1" "$2"
}
ENV_SET_PY=$(cat <<'EOF'
import os, sys, tempfile
path, key, value = sys.argv[1:4]
if '\n' in value or '\x00' in value:
    sys.exit('newline in ' + key)
line = key + '="' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
try:
    lines = open(path, encoding='utf-8').read().split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    mode = os.stat(path).st_mode & 0o777
except FileNotFoundError:
    lines, mode = [], 0o600
for i, l in enumerate(lines):
    if l.startswith(key + '='):
        lines[i] = line
        break
else:
    lines.append(line)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.realpath(path)) or '.')
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines) + '\n')
os.chmod(tmp, mode)
os.replace(tmp, os.path.realpath(path))
EOF
)
account_env_set() { # file key value (ACCOUNT)
    runuser -u "$ACCOUNT" -- python3 -I -S -c "$ENV_SET_PY" "$1" "$2" "$3"
}
account_mkdir() { # mode dir... (ACCOUNT)
    local mode="$1"; shift
    runuser -u "$ACCOUNT" -- mkdir -p -m "$mode" "$@"
}
account_write() { # mode file < content (ACCOUNT): written by the account, mode applied to a new file
    runuser -u "$ACCOUNT" -- sh -c 'umask 077; cat > "$1" && chmod "$2" "$1"' _ "$2" "$1"
}

# A generated secret (openssl rand -hex 32) and a TCP port: the only shapes that may reach
# a psql meta-command, a PGPORT or a Redis ACL line made by root
valid_secret() { [[ "${1:-}" =~ ^[0-9a-f]{64}$ ]]; }
valid_port() { [[ "${1:-}" =~ ^[0-9]{1,5}$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }

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

# Where the database lives (README, "Why a private PostgreSQL cluster?"):
#   private   the account's own cluster ~/data/pg, run by the user unit motuz-postgres
#   system    the distribution's PostgreSQL service (install.sh --system-postgres)
#   external  another server (MOTUZ_DATABASE_HOST), nothing local
#   unset     a fresh install that has not chosen yet
# MOTUZ_DATABASE_MODE in motuz.env says it; installs made before that setting have none:
# a database host other than 127.0.0.1:5432 is external, an existing ~/data/pg private.
database_mode() { # settings file, pg data directory
    local mode host
    mode=$(env_get "$1" MOTUZ_DATABASE_MODE)
    case "$mode" in
        private|system|external) echo "$mode"; return ;;
        "") ;;
        *) die "MOTUZ_DATABASE_MODE=$mode in $1: use private, system or external" ;;
    esac
    host=$(env_get "$1" MOTUZ_DATABASE_HOST)
    if [ -n "$host" ] && [ "$host" != 127.0.0.1:5432 ]; then echo external
    elif [ -f "$2/PG_VERSION" ]; then echo private
    else echo unset
    fi
}

# Where the Celery broker lives: private (the user unit motuz-redis, config
# ~/.config/motuz/redis.conf) or system (the distribution's Redis service, install.sh
# --system-redis); MOTUZ_BROKER_MODE in motuz.env, else inferred like database_mode
broker_mode() { # settings file, config directory
    local mode
    mode=$(env_get "$1" MOTUZ_BROKER_MODE)
    case "$mode" in
        private|system) echo "$mode"; return ;;
        "") ;;
        *) die "MOTUZ_BROKER_MODE=$mode in $1: use private or system" ;;
    esac
    if [ -f "$2/redis.conf" ]; then echo private; else echo unset; fi
}

# Masks the distribution's own services that a private instance replaces (the
# distro modules' DISTRO_SERVICES); the PostgreSQL and Redis services of the system
# modes (DISTRO_PG_SERVICE, DISTRO_REDIS_SERVICE) are left alone. POSTGRES_MODE and
# REDIS_MODE are set by install.sh.
mask_private_services() {
    local unit
    for unit in $DISTRO_SERVICES; do
        [ "$unit" != "$DISTRO_PG_SERVICE" ] || [ "${POSTGRES_MODE:-private}" = private ] || continue
        [ "$unit" != "${DISTRO_REDIS_SERVICE:-}" ] || [ "${REDIS_MODE:-private}" = private ] || continue
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
        systemctl mask "$unit" >/dev/null
    done
}

# Copies the user units, with the distribution's paths (@PG_BINDIR@, @REDIS_SERVER@).
# motuz-postgres.service and motuz-redis.service only in their private modes (any other
# mode removes them), and only then does anything depend on them: drop-ins make the
# target want them and the app and the worker require/want them, so a system or
# external database or broker can never make a unit start or wait for a missing service.
install_units() { # destination directory, database mode, broker mode
    local unit name svc mode kind dir
    for unit in "$REPO_DIR"/deployment/systemd/user/motuz*; do
        name=$(basename "$unit")
        [ "$name" != motuz-postgres.service ] || [ "$2" = private ] || continue
        [ "$name" != motuz-redis.service ] || [ "$3" = private ] || continue
        sed -e "s|@PG_BINDIR@|$PG_BINDIR|g" -e "s|@REDIS_SERVER@|$REDIS_SERVER|g" "$unit" > "$1/$name.new"
        chmod 644 "$1/$name.new"
        mv "$1/$name.new" "$1/$name"
    done
    for svc in "postgres:$2:Requires" "redis:$3:Wants"; do
        IFS=: read -r svc mode kind <<< "$svc"
        for name in motuz.target motuz-app.service motuz-celery.service; do
            dir="$1/$name.d"
            if [ "$mode" = private ]; then
                mkdir -p "$dir"
                { echo "# Generated by bin/systemd/deploy.sh (private $svc)"; echo "[Unit]"
                  if [ "$name" = motuz.target ]; then echo "Wants=motuz-$svc.service"
                  else echo "$kind=motuz-$svc.service"; echo "After=motuz-$svc.service"; fi; } > "$dir/10-$svc.conf"
            else
                rm -f "$dir/10-$svc.conf"
                rmdir "$dir" 2>/dev/null || true
            fi
        done
        [ "$mode" = private ] || rm -f "$1/motuz-$svc.service"
    done
}

# Row count of every table of the public schema, as "table|count" lines (to compare a
# database before and after a dump and restore)
COUNT_SQL="SELECT table_name, (xpath('/row/c/text()', query_to_xml(
    format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text
    FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1"

# Replaces the contents of a database that motuz_user owns (the system or an external
# PostgreSQL) with a pg_dump custom-format dump, as that role: no superuser is needed
restore_dump_over_tcp() { # host:port, user, password, database, dump file
    local host="${1%:*}" port="${1##*:}"
    PGPASSWORD="$3" psql -X -q -v ON_ERROR_STOP=1 -h "$host" -p "$port" -U "$2" -d "$4" \
        -c 'DROP SCHEMA public CASCADE' -c 'CREATE SCHEMA public' || return 1
    PGPASSWORD="$3" pg_restore -h "$host" -p "$port" -U "$2" -d "$4" --no-owner --no-privileges --exit-on-error "$5"
}

# Pinned versions (deployment/systemd/versions.env; rclone from the app Dockerfile)
load_versions() {
    # shellcheck source=../../deployment/systemd/versions.env
    source "$REPO_DIR/deployment/systemd/versions.env"
    local dockerfile="$REPO_DIR/deployment/docker/app/Dockerfile"
    RCLONE_RELEASES=$(sed -n 's/^ARG RCLONE_RELEASES=//p' "$dockerfile")
    RCLONE_VERSION=$(sed -n 's/^ARG RCLONE_VERSION=//p' "$dockerfile")
    RCLONE_SHA256_AMD64=$(sed -n 's/^ARG RCLONE_SHA256_AMD64=//p' "$dockerfile")
    RCLONE_SHA256_ARM64=$(sed -n 's/^ARG RCLONE_SHA256_ARM64=//p' "$dockerfile")
    [ -n "$RCLONE_VERSION" ] && [ -n "$RCLONE_SHA256_AMD64" ] || die "no rclone version in $dockerfile"
    [[ "$RCLONE_RELEASES" =~ ^https://[A-Za-z0-9./_-]+$ ]] || die "no https RCLONE_RELEASES in $dockerfile"
}

# The data mover's TCP settings (install.sh, /etc/sysctl.d/61-motuz-network.conf): send and
# receive buffers of up to 128 MiB, so one connection can fill a fast link with a long round
# trip (Ubuntu's 4 MiB send buffer allows 4 MiB / 70 ms = 0.5 Gb/s per connection), and BBR
# with the fq qdisc when the kernel has it. Never lowers what the machine already has.
network_tuning_conf() { # [/proc/sys, for tests]
    local sys="${1:-/proc/sys}" target=134217728 name value min def max
    echo "# Motuz (bin/systemd/install.sh): TCP buffers for fast links with long round trips"
    for name in net.core.rmem_max net.core.wmem_max; do
        value=$(cat "$sys/${name//.//}" 2>/dev/null || echo 0)
        [ "$value" -ge "$target" ] 2>/dev/null || value=$target
        echo "$name = $value"
    done
    for name in net.ipv4.tcp_rmem net.ipv4.tcp_wmem; do
        min=4096; def=131072; max=0
        read -r min def max 2>/dev/null < "$sys/${name//.//}" || true
        [ "$max" -ge "$target" ] 2>/dev/null || max=$target
        echo "$name = $min $def $max"
    done
    if grep -qw bbr "$sys/net/ipv4/tcp_available_congestion_control" 2>/dev/null; then
        echo "net.core.default_qdisc = fq"
        echo "net.ipv4.tcp_congestion_control = bbr"
    fi
}
