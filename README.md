<div align="center">
    <img src="src/frontend/img/logo.png" width="100" height="100">
    <h1>Motuz</h1>
    <p>
        <b>A web based infrastructure for large scale data movements between on-premise and cloud</b>
    </p>
    <br>
</div>

![Motuz transfer and checksum validation](docs/img/mov-copy-check.gif)

![Motuz settings and features](docs/img/mov-settings.gif)

## Quick start

Motuz runs without Docker as `systemd --user` services of one dedicated account, `motuz`, on
**Ubuntu 26.04 LTS** or **Amazon Linux 2027** (a VM, bare metal or an EC2 instance with at
least 4 GiB of memory and 16 GiB of disk: the frontend build and the uWSGI compile run on the
machine). The install is split in two so that the part that needs root is short and can be
reviewed:

| | Who | How often | What |
| --- | --- | --- | --- |
| 1 | **root** | once (again only when `deploy.sh` says so) | `sudo bin/systemd/install.sh`: packages, the account and its directory `/var/lib/motuz-runas` for rclone, its sudoers rule, PAM, PostgreSQL and Redis |
| 2 | **the account `motuz`** | on the first deploy and on every update | `~/motuz/bin/systemd/deploy.sh`: the pinned rclone, Traefik and uv, Python environment, frontend, settings, units, database, start |

Before you start, the two things people most often get stuck on:

- **Firewall.** Only Traefik listens beyond the machine, on **TCP 80 and 443** (80 redirects to
  443 and is needed for Let's Encrypt). Both must be open. Ubuntu: `sudo ufw allow 22/tcp &&
  sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable` (22 first, or you lock
  yourself out of SSH). Amazon Linux 2027 / EC2: the instance's security group, or with
  firewalld `sudo firewall-cmd --permanent --add-service=http --add-service=https &&
  sudo firewall-cmd --reload`. `install.sh` prints this hint at the end; it never changes
  the firewall.
- **DNS.** For a first try you can open `https://<ip or host name>/` and accept the
  self-signed certificate. For a real name and a trusted certificate, a DNS record for
  the name must point at this machine's public address *before* you ask for a Let's Encrypt
  certificate (see [HTTPS certificate](#https-certificate-tls-lets-encrypt)); the Let's
  Encrypt servers connect to port 80 of that name.

### Very quick start

On a fresh machine, from `git clone` to the login page (every line says who runs it):

```bash
# 1. As root (via sudo), once: root's own clone of the repository, then the root half
sudo apt-get update && sudo apt-get install -y git      # Amazon Linux 2027: sudo dnf -y install git
sudo git clone https://github.com/dirkpetersen/motuz.git /root/motuz-install    # or your fork: add --branch NAME
sudo /root/motuz-install/bin/systemd/install.sh --local-accounts                # drop --local-accounts if users come from Active Directory/SSSD

# 2. As the account motuz: its own clone, then the deploy (a few minutes: it builds the frontend)
sudo -iu motuz                                           # you are now motuz
git clone https://github.com/dirkpetersen/motuz.git ~/motuz    # the same repository and branch as above
~/motuz/bin/systemd/deploy.sh                            # ends with "Motuz is up: https://<host>/"
exit                                                     # back to your own account

# 3. As an admin (root): open TCP 80 and 443 (Ubuntu shown, see the firewall bullet above)
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable

# 4. Check, and create a user to log in with (--local-accounts: any local, non-root account)
curl -sk https://127.0.0.1/api/system/info/              # answers with "healthy"
sudo useradd -m alice && sudo passwd alice
```

Then open `https://<host name or address>/`, accept the self-signed certificate, and sign in
as `alice`. Nothing has to be configured for this first start: `deploy.sh` generates the
secrets and a self-signed certificate. Do these next when you are ready (all as `motuz`,
[Settings and secrets](#settings-and-secrets)):

- a real certificate: `~/motuz/bin/systemd/config.sh set MOTUZ_ACME_DOMAIN motuz.example.org`
  (Let's Encrypt), or your own files in `~/data/certs`
  ([HTTPS certificate](#https-certificate-tls-lets-encrypt));
- shared filesystems that must be mounted before jobs run: `MOTUZ_REQUIRED_PATHS`
  ([Storage](#storage));
- own OneDrive / Google Drive app registrations, performance defaults, notification emails
  (see [Configuration and features](#configuration-and-features)).

Apply changes with `~/motuz/bin/systemd/deploy.sh` (or `systemctl --user restart
motuz.target` for settings that only the app reads).

### What runs as root

`sudo bin/systemd/install.sh [options]`, from a clone that only root can change (never from the
account's `~/motuz`). It is idempotent, supports only Ubuntu 26.04 and Amazon Linux 2027, and
does exactly this (every command by hand: [Root steps without
install.sh](#root-steps-without-installsh)):

- installs packages: PostgreSQL 18, Redis (Ubuntu) or Valkey 9 (Amazon Linux), Node.js and npm,
  build tools, `git`, `curl`, `openssl`, `python3`, `sudo`;
- installs the HEIC/HEIF converter (`pi-heif` and Pillow) into `/opt/motuz-heic`;
- makes `/var/lib/motuz-runas`, owned by the account: the account itself puts the pinned
  **rclone** there, and **Traefik and uv** into its `~/.local/bin` (SHA256 checked,
  `bin/systemd/tools.sh`, run by `deploy.sh`), so new versions need no root, and root never
  runs them. rclone is outside the home because the users run it (through sudo), and the
  home stays closed to them;
- creates the account **`motuz`** (system account, locked password, home `/var/lib/motuz`) and
  turns on `loginctl enable-linger`, so its services start at boot;
- writes the account's only privilege, the sudoers rule `/etc/sudoers.d/motuz` (run `rclone`,
  `ls`, `mkdir`, `env` as the logged-in user, never as root:
  [Why a sudoers rule](#why-a-sudoers-rule));
- sets two sysctls (`net.ipv4.ip_unprivileged_port_start=80`, so a user service can bind 80 and
  443; `vm.overcommit_memory=1`), installs the PAM service `/etc/pam.d/motuz` and
  `/var/lib/motuz-aws-config`;
- tunes the network for the data mover in `/etc/sysctl.d/61-motuz-network.conf`, also on worker
  hosts: TCP buffers of up to 128 MiB (Ubuntu's 4 MiB send buffer limits one connection to
  0.5 Gb/s at 70 ms round trip) and BBR with the `fq` qdisc if the kernel has it; larger
  values the machine already has are kept. `--no-network-tuning` leaves this out;
- prepares **PostgreSQL and the Celery broker in the distribution's default mode**: Ubuntu uses
  the distribution's own `postgresql@18-main` and `redis-server` services (role `motuz_user`,
  database `motuz`, localhost only; a Redis unix socket with its own ACL user), Amazon Linux
  uses private instances run as user services of `motuz`
  ([Defaults per distribution](#install-default-without-docker));
- with `--local-accounts`, installs the root login helper for local `/etc/shadow` accounts
  ([Logins (PAM)](#logins-pam)); on Amazon Linux (SELinux enforcing) runs `restorecon` on what
  it wrote.

It does **not** start Motuz, open the firewall, set up DNS or touch the certificate, and it
writes nothing inside the account's home. Options (details in [Root steps
(install.sh)](#root-steps-installsh)): `--local-accounts`, `--system-postgres` /
`--private-postgres`, `--system-redis` / `--private-redis`, `--sudo-group=GROUP`,
`--user=NAME`, `--home=DIR`, `--worker-only` (a [remote worker](#remote-workers-https-only)
host). Root is needed again only when `deploy.sh` prints "run bin/systemd/install.sh again as
root", for the firewall and DNS, for joining a directory (`realm join`), for shared mounts and
for OS updates.

### What runs as the account `motuz`

`~/motuz/bin/systemd/deploy.sh [--pull]`, run as `motuz` (`sudo -iu motuz`) from its own
clone `~/motuz`. No root, now or for updates. It:

- creates the Python environment `~/motuz/venv` with uv and installs `requirements.txt`
  (the system's Python 3.14 on Ubuntu and Amazon Linux);
- builds the frontend (`npm ci && npm run build`);
- creates `~/.config/motuz/motuz.env` (settings, from `deployment/systemd/motuz.env.example`) and
  `secrets.env` (mode 600; Flask key, database and Redis passwords are generated) on the first run
  and never overwrites them, and merges the passwords `install.sh` left in
  `/etc/motuz/handoff-motuz.env`;
- makes a self-signed certificate in `~/data/certs` if there is none, and writes Traefik's and
  (in the private mode) Redis' configuration;
- installs the `motuz-*` user units in `~/.config/systemd/user`, prepares the database (initializes
  the private cluster, or checks the login to the system / external one), restarts
  `motuz.target` (the app runs the migrations on start) and checks
  `https://127.0.0.1/api/system/info/`.

**Required settings: none.** Optional ones you will probably want are listed above; edit them with
`bin/systemd/config.sh` (`show`, `set KEY VALUE`, `ask-secret KEY`; secrets are typed hidden and never
printed), then run `deploy.sh` again.

### One command instead: bootstrap.sh

On a fresh machine, as root, `bootstrap.sh` does both halves: `install.sh`, the account's clone
`~/motuz` (cloned from `--repo` / `--branch`, or fast-forwarded if it exists) and `deploy.sh` as the
account. It is idempotent, so it is also the update command; any other option goes to `install.sh`.

```bash
sudo apt-get update && sudo apt-get install -y git      # Amazon Linux 2027: sudo dnf -y install git
sudo git clone --branch BRANCH https://github.com/dirkpetersen/motuz.git /root/motuz-install
sudo /root/motuz-install/bin/systemd/bootstrap.sh --repo=https://github.com/dirkpetersen/motuz.git --branch=BRANCH --local-accounts
```

(Leave `--branch` out for the default branch.) EC2 user data does the same:
[Install on an instance](#install-on-an-instance), `deployment/aws/user-data-al2027.sh`.

### Check that it works

```bash
curl -sk https://127.0.0.1/api/system/info/        # "healthy"
sudo -iu motuz env XDG_RUNTIME_DIR=/run/user/$(id -u motuz) systemctl --user status motuz.target 'motuz*'
```

Then open `https://<host>/` and sign in with a Unix account (root and `motuz` are refused). A
login plus a small local copy job through the API, as the account `motuz`
(`bin/systemd/smoke_test.py USER SRC_PATH DST_PATH`; both paths must be local paths that user can
access, `SRC_PATH` must exist; the password is typed hidden into `~/.motuz-test-pw` and never goes on a
command line):

```bash
# as motuz
~/motuz/bin/systemd/config.sh ask-test-password
~/motuz/bin/systemd/smoke_test.py alice /home/alice/some-folder /home/alice/copy-of-it
rm ~/.motuz-test-pw
```

If something is wrong: [Status and logs](#status-and-logs), and the checklist of the usual causes:
the page does not open (firewall, DNS, the security group), the browser warns about the certificate
(self-signed until you [configure one](#https-certificate-tls-lets-encrypt)), sign-in fails for a
local account (installed without `--local-accounts`, or the user has no password), or `deploy.sh`
stops with a message naming the missing piece (it says when to run `install.sh` again).

## Contents

- [Quick start](#quick-start)
    - [Very quick start](#very-quick-start)
    - [What runs as root](#what-runs-as-root)
    - [What runs as the account `motuz`](#what-runs-as-the-account-motuz)
    - [One command instead: bootstrap.sh](#one-command-instead-bootstrapsh)
    - [Check that it works](#check-that-it-works)
- [Install (default: without Docker)](#install-default-without-docker)
    - [Root steps (install.sh)](#root-steps-installsh)
    - [Root steps without install.sh](#root-steps-without-installsh)
    - [Steps as the motuz account](#steps-as-the-motuz-account)
    - [Settings and secrets](#settings-and-secrets)
    - [HTTPS certificate (TLS, Let's Encrypt)](#https-certificate-tls-lets-encrypt)
    - [Status and logs](#status-and-logs)
    - [Updating](#updating)
    - [Deploy with an AI assistant (Claude Code)](#deploy-with-an-ai-assistant-claude-code)
    - [Why a private PostgreSQL cluster?](#why-a-private-postgresql-cluster)
    - [Backups and upgrades](#backups-and-upgrades)
    - [Why a sudoers rule](#why-a-sudoers-rule)
    - [Logins (PAM)](#logins-pam)
    - [Storage](#storage)
    - [VM or LXC](#vm-or-lxc)
    - [Migrating from the docker install](#migrating-from-the-docker-install)
    - [Tests of this install](#tests-of-this-install)
    - [Uninstall](#uninstall)
- [Install on Amazon Linux 2027 (default on EC2)](#install-on-amazon-linux-2027-default-on-ec2)
    - [Install on an instance](#install-on-an-instance)
    - [Remote workers on AL2027](#remote-workers-on-al2027)
    - [Tests on AL2027](#tests-on-al2027)
- [Configuration and features](#configuration-and-features)
    - [S3 connections](#s3-connections)
    - [OneDrive: own app registration](#onedrive-own-app-registration)
    - [Google Drive](#google-drive)
    - [Privacy policy and terms](#privacy-policy-and-terms)
    - [Performance tuning](#performance-tuning)
    - [File viewer](#file-viewer)
    - [Uploads from the desktop](#uploads-from-the-desktop)
    - [Remote workers (HTTPS only)](#remote-workers-https-only)
    - [Temporary EC2 workers](#temporary-ec2-workers)
- [Alternative: Docker](#alternative-docker)
    - [Docker quickstart](#docker-quickstart)
    - [Beyond quickstart](#beyond-quickstart)
    - [Customizing your deployment](#customizing-your-deployment)
    - [Setting up production](#setting-up-production)
    - [Authentication](#authentication)
    - [Requirements](#requirements)
    - [Docker and docker-compose](#docker-and-docker-compose)
    - [Shared Filesystems (optional)](#shared-filesystems-optional)
    - [Cloning the Motuz repository](#cloning-the-motuz-repository)
    - [Set up HTTPS certificate](#set-up-https-certificate)
    - [Running Motuz the first time](#running-motuz-the-first-time)
    - [Redeploying](#redeploying)
    - [Migrating from nginx to Traefik](#migrating-from-nginx-to-traefik)
    - [Using a custom database](#using-a-custom-database)
- [Developer Installation](#developer-installation)
    - [Initialize](#initialize)
    - [Start](#start)
    - [Tests](#tests)
- [Development Options](#development-options)
- [Examples](#examples)
    - [How to use the API](#how-to-use-the-api)
    - [Authentication](#authentication-1)
- [Folder structure](#folder-structure)
    - [Overview](#overview)
    - [Frontend folder structure (inside `/src/frontend/`)](#frontend-folder-structure-inside-srcfrontend)
    - [Backend folder structure (inside `/src/backend/`)](#backend-folder-structure-inside-srcbackend)
    - [Temp folders](#temp-folders)
- [Other resources](#other-resources)


## Install (default: without Docker)

Motuz runs as `systemd --user` services of one dedicated account, `motuz`, on **Ubuntu 26.04
LTS** (a VM, for example on Proxmox, a privileged LXC or bare metal) or **Amazon Linux 2027**
(EC2, see [Install on Amazon Linux 2027](#install-on-amazon-linux-2027-default-on-ec2)).
This is the default way to install Motuz; Docker is the
[alternative](#alternative-docker). Both use the same code, the same Traefik flags and
dynamic configuration, the same rclone and the same PostgreSQL major version, and
[move into each other](#migrating-from-the-docker-install).

The install is split in two, root once and the account `motuz` for everything else (the
[Quick start](#quick-start) has the copy-and-paste version and says what each half does). The
rest of this chapter is the reference: every root command by hand ([Root steps without
install.sh](#root-steps-without-installsh)), settings, certificates, updates, the database
modes, and what the install changes on the machine. The account's half can also be done by an AI
assistant ([Deploy with an AI assistant (Claude Code)](#deploy-with-an-ai-assistant-claude-code)).

| Service | Unit | Listens on |
| --- | --- | --- |
| PostgreSQL 18 | the distribution's service (default on Ubuntu), or `motuz-postgres`, a user service with the distribution's binaries and data in `~/data/pg` | 127.0.0.1:5432 |
| Celery broker: Redis 8 (Ubuntu) or Valkey 9 (Amazon Linux; AL2027 has no Redis), append-only file | the distribution's `redis-server` (default on Ubuntu), or `motuz-redis`, a user service | a unix socket only, no TCP |
| uWSGI: API, frontend, OAuth token broker (`~/motuz/venv`, `src/backend/wsgi.ini`) | `motuz-app` | 127.0.0.1:5000, 127.0.0.1:5001 |
| Celery worker | `motuz-celery` | nothing |
| Traefik 3.7 (the flags of `docker-compose.yml`, `deployment/docker/traefik/dynamic/motuz.yml`) | `motuz-traefik` | :80, :443 |

The `motuz-*` units are grouped by `motuz.target`, which starts at boot (`loginctl
enable-linger motuz`). Only Traefik listens beyond loopback: the sysctl
`net.ipv4.ip_unprivileged_port_start=80` lets a user service bind ports 80 and 443
(user services cannot get `CAP_NET_BIND_SERVICE`).

**Defaults per distribution** (each with an explicit option; an existing install keeps the
mode it has and is never switched silently):

| | Ubuntu 26.04 | Amazon Linux 2027 |
| --- | --- | --- |
| PostgreSQL | the distribution's service (`postgresql@18-main`): `--system-postgres`, default | private cluster `~/data/pg`: `--private-postgres`, default; `--system-postgres` uses `postgresql.service` |
| Broker | the distribution's `redis-server` on its socket: `--system-redis`, default | private Valkey (`motuz-redis`): `--private-redis`, default; no system mode |
| Why | standard tooling (`pg_lsclusters`, `pg_upgradecluster`, `pg_dump` as `postgres`, apt updates) and the packages' own units | the AL2027 packages are a preview with one `postgresql.service` and a Valkey whose `valkey.conf` has no include directory (a system mode would rewrite the package's configuration); one design as before |

### Root steps (install.sh)

As root, from a clone of the repository that only root can change (never from the
account's `~/motuz`):

```bash
sudo bin/systemd/install.sh                    # SSSD/Kerberos accounts (Active Directory)
sudo bin/systemd/install.sh --local-accounts   # also local /etc/shadow accounts (login helper)
sudo bin/systemd/install.sh --private-postgres --private-redis   # the account's own instances (Ubuntu)
sudo bin/systemd/install.sh --system-postgres  # the distribution's PostgreSQL (Amazon Linux)
```

Other options: `--sudo-group=GROUP` (Motuz may act only as members of that group, default:
every user but root), `--user=NAME` (the service account, default `motuz`), `--home=DIR` (its
home, absolute, on local disk, default `/var/lib/motuz`), `--worker-only` (a [remote
worker](#remote-workers-https-only) host, with `--central-url`, `--pool`, `--credential-file`,
`--run-as`), `-h` (all options). The README says `motuz` and `/var/lib/motuz` throughout.

`install.sh` checks that it runs on a supported distribution, is idempotent, and
prints the PostgreSQL/Redis mode it uses. It installs the packages, the pinned rclone
(version and SHA256 from `deployment/docker/app/Dockerfile`), Traefik and uv
(`deployment/systemd/versions.env`, SHA256 checked), the account `motuz` (a system
account with a locked password, home `/var/lib/motuz`, shell `/bin/bash`: keep it on local
disk), linger, the sudoers rule, the sysctl files, `/etc/pam.d/motuz`,
`/var/lib/motuz-aws-config`, the optional login helper, and, in the system modes, the
database role and the Redis configuration below. It leaves the account usable for the
next step: `sudo -iu motuz` (or `sudo machinectl shell motuz@`) gives an interactive shell
with `git` and `curl`; there is no login password and no SSH key, and the account never
gets a `nologin` shell because the setup is done interactively as that account.

**System PostgreSQL** (`--system-postgres`). Ubuntu: cluster `18/main` of
`postgresql-common`, created with `pg_createcluster 18 main` if missing, service
`postgresql@18-main`. Amazon Linux: `postgresql-setup --initdb` and `postgresql.service`.
In both: the service is enabled and running, listens on `localhost` only (`ALTER SYSTEM`
if it did not), the role `motuz_user` (scram-sha-256) and the database `motuz` owned by it
(PostgreSQL 15+ public-schema ownership; `CONNECT` revoked from `PUBLIC`), and lines at the
top of `pg_hba.conf` (between `# BEGIN motuz` and `# END motuz`) that allow scram for
`motuz_user` on `127.0.0.1` and `::1` and reject it from anywhere else. The password is
generated, never on a command line, and left in `/etc/motuz/handoff-motuz.env` (root-owned,
group `motuz`, mode 640) with the mode and address; `deploy.sh` merges it into
`motuz.env`/`secrets.env`. Root never writes inside the account's home (a symlink there
could point anywhere): the file is root's, and everything `install.sh` does in the home, for
the worker, it does as the account. It refuses to take over a PostgreSQL or Redis that
something else seems to use (other databases, a listen address beyond localhost; keys,
clients, authentication or another port in Redis) unless `--system-postgres` /
`--system-redis` is given explicitly, and never rewrites a service silently.

**System Redis** (`--system-redis`, Ubuntu). `/etc/redis/motuz.conf` includes the package's
`redis.conf` and overrides it: `port 0` (no TCP), the unix socket
`/run/redis/redis-server.sock` with mode 770 (group `redis`; `motuz` is added to the group),
`appendonly yes`, the `default` user off and the ACL user `motuz` with the password's
SHA-256 (not requirepass: the password gives Celery what it needs and no more, `-@admin
-@dangerous -@scripting` denies `CONFIG`, `SHUTDOWN`, `FLUSHALL`, `KEYS`, `DEBUG`, ...;
`+eval +evalsha +script|load` gives back what kombu's lock needs, and commands inside a
script are still checked against the same ACL).
A systemd drop-in `/etc/systemd/system/redis-server.service.d/motuz.conf` starts
`redis-server` with that file; the package's own `redis.conf` is not modified. The Celery
`visibility_timeout` of 30 days and `vm.overcommit_memory=1` are unchanged.
`MOTUZ_CELERY_BROKER_URL` becomes `redis+socket://motuz:<password>@/run/redis/redis-server.sock`.

### Root steps without install.sh

What `install.sh` does as root, in order, for an admin who wants to run the commands
(replace the variables first; each step is idempotent). Versions and checksums are not
repeated here: they are in `deployment/systemd/versions.env` (Traefik, uv) and in the `ARG
RCLONE_*` lines of `deployment/docker/app/Dockerfile` (rclone), which the commands read.
Work from root's own clone: `git clone https://github.com/dirkpetersen/motuz.git /root/motuz-install; cd /root/motuz-install`.

**1. Packages.**

Ubuntu 26.04:

```bash
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends postgresql-common
# so that no `main` cluster is created behind your back (--private-postgres; harmless otherwise)
grep -q '^create_main_cluster = false' /etc/postgresql-common/createcluster.conf \
  || echo 'create_main_cluster = false' >> /etc/postgresql-common/createcluster.conf
apt-get install -y --no-install-recommends postgresql-18 postgresql-client-18 redis-server \
  nodejs npm build-essential git curl ca-certificates unzip openssl python3 sudo iproute2 libpam-modules
# only with --private-postgres / --private-redis: the package's services must not run
#   systemctl disable --now postgresql.service redis-server.service; systemctl mask postgresql.service redis-server.service
```

Amazon Linux 2027:

```bash
dnf -y install postgresql18-server postgresql18 valkey nodejs24 nodejs24-npm gcc make git tar gzip \
  unzip openssl python3 python3-devel sudo iproute procps-ng shadow-utils util-linux pam \
  policycoreutils hostname diffutils findutils gawk sed ca-certificates
command -v curl >/dev/null || dnf -y install curl     # the AMI has curl-minimal
systemctl disable --now valkey.service valkey-sentinel.service; systemctl mask valkey.service valkey-sentinel.service
# private PostgreSQL (the default there): also  systemctl disable --now postgresql.service; systemctl mask postgresql.service
[ "$(node -p 'process.versions.node.split(".")[0]')" = 24 ] || alternatives --set node /usr/bin/node-24
```

**2. Pinned rclone, Traefik and uv**: nothing as root. The account downloads rclone into
`/var/lib/motuz-runas` and Traefik and uv into `~/.local/bin` (`bin/systemd/tools.sh`, SHA256
checked, run by `deploy.sh`); root only makes `/var/lib/motuz-runas` (step 3). The HEIC/HEIF converter in `/opt/motuz-heic` is optional
(`install.sh` installs it with a uv of its own, downloaded for that step).

**3. The account**, linger and its systemd user manager:

```bash
useradd --system --user-group --create-home --home-dir /var/lib/motuz --shell /bin/bash \
  --comment "Motuz service account" motuz        # locked password; a uid below UID_MIN
chmod 750 /var/lib/motuz
install -d -m 755 -o motuz -g motuz /var/lib/motuz-runas   # the account's rclone
loginctl enable-linger motuz
systemctl start user@$(id -u motuz).service
```

**4. The sudoers rule** (`--sudo-group=GROUP`: `(%GROUP, !root, !postgres, !redis, !valkey)` instead of `(ALL, !root, !postgres, !redis, !valkey)`):

```bash
cat > /tmp/motuz.sudoers <<'EOF'
# Motuz (bin/systemd/install.sh): file and rclone operations as the logged-in user
motuz ALL=(ALL, !root, !postgres, !redis, !valkey) NOPASSWD:SETENV: /var/lib/motuz-runas/rclone, /usr/bin/ls, /usr/bin/mkdir, /usr/bin/env
EOF
# the classic sudo (Amazon Linux; not Ubuntu's sudo-rs, `sudo -V | head -1` says "Sudo version" for it) would log
# rclone's credentials with every allowed command:
sudo -V | head -1 | grep -q '^Sudo version' && echo 'Defaults:motuz !log_allowed' >> /tmp/motuz.sudoers
visudo -c -q -f /tmp/motuz.sudoers && install -m 440 -o root -g root /tmp/motuz.sudoers /etc/sudoers.d/motuz
visudo -c -q
```

**5. Sysctl, PAM and the AWS SSO directory:**

```bash
printf 'net.ipv4.ip_unprivileged_port_start = 80\nvm.overcommit_memory = 1\n' > /etc/sysctl.d/60-motuz.conf
sysctl -q -p /etc/sysctl.d/60-motuz.conf
# network tuning (optional; install.sh only raises values, never lowers them)
modprobe tcp_bbr && echo tcp_bbr > /etc/modules-load.d/motuz-bbr.conf
cat > /etc/sysctl.d/61-motuz-network.conf <<'EOF'
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.ipv4.tcp_rmem = 4096 131072 134217728
net.ipv4.tcp_wmem = 4096 16384 134217728
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
EOF
sysctl -q -p /etc/sysctl.d/61-motuz-network.conf
install -m 644 deployment/systemd/pam.d/motuz.ubuntu /etc/pam.d/motuz      # Amazon Linux: motuz.amzn
install -d -m 711 -o motuz -g motuz /var/lib/motuz-aws-config
```

**6. The login helper** (only for local `/etc/shadow` accounts, `--local-accounts`):

```bash
install -d -m 755 /usr/local/lib/motuz-auth
install -m 644 deployment/systemd/auth-helper/motuz_auth_helper.py src/backend/api/utils/pam.py /usr/local/lib/motuz-auth/
install -m 644 deployment/systemd/system/motuz-auth.socket deployment/systemd/system/motuz-auth@.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now motuz-auth.socket
```

(`install.sh` also replaces `SocketGroup=motuz` and `MOTUZ_AUTH_CLIENT_USER=motuz` when `--user` names another account.)

**7. System PostgreSQL** (Ubuntu and Amazon Linux default to different modes, see above):

```bash
# Ubuntu: the cluster 18/main
systemctl unmask postgresql.service
pg_lsclusters -h | grep -q '^18 main' || pg_createcluster 18 main -e UTF8 --locale=C.UTF-8
systemctl enable --now postgresql@18-main.service
# Amazon Linux instead:
#   systemctl unmask postgresql.service
#   [ -f /var/lib/pgsql/data/PG_VERSION ] || PGSETUP_INITDB_OPTIONS="-E UTF8 --locale=C.UTF-8" postgresql-setup --initdb
#   systemctl enable --now postgresql.service

# Is it used by something else? (other databases, listen addresses beyond localhost: then stop here)
sudo -u postgres psql -tAc "SELECT datname FROM pg_database WHERE datname NOT IN ('postgres','template0','template1','motuz')"
sudo -u postgres psql -tAc 'SHOW listen_addresses'            # must be localhost (else ALTER SYSTEM SET listen_addresses='localhost' and restart)
sudo -u postgres psql -tAc 'SHOW port'

# role and database, idempotent (the password, 64 hex digits, goes in through stdin, not on a command line)
PW=$(openssl rand -hex 32)
{ printf '\\set pw %s\n' "'$PW'"; cat <<'EOSQL'
SELECT format('CREATE ROLE %I LOGIN', :'user') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'user') \gexec
ALTER ROLE :"user" WITH LOGIN PASSWORD :'pw';
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'user') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
ALTER DATABASE :"db" OWNER TO :"user";
REVOKE CONNECT ON DATABASE :"db" FROM PUBLIC;
EOSQL
} | sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -d postgres -v db=motuz -v user=motuz_user
# pg_hba.conf (sudo -u postgres psql -tAc 'SHOW hba_file'): put these lines at the top, then reload
#   host motuz motuz_user 127.0.0.1/32 scram-sha-256
#   host motuz motuz_user ::1/128 scram-sha-256
#   host all motuz_user 0.0.0.0/0 reject
#   host all motuz_user ::/0 reject
sudo -u postgres psql -c 'SELECT pg_reload_conf()'
```

Hand the values to the account the way `install.sh` does, as the file
`/etc/motuz/handoff-motuz.env` (root-owned, group `motuz`, mode 640), which `deploy.sh`
merges into `motuz.env` and `secrets.env` on its first run (so `motuz.env` still comes from
`motuz.env.example` and has all its other settings); use the port of `SHOW port`:

```bash
install -d -m 755 /etc/motuz
( umask 027; { echo 'MOTUZ_DATABASE_MODE="system"'; echo 'MOTUZ_DATABASE_HOST="127.0.0.1:5432"'
  echo "MOTUZ_DATABASE_PASSWORD=\"$PW\""; } > /etc/motuz/handoff-motuz.env )
chown root:motuz /etc/motuz/handoff-motuz.env
```

**8. System Redis** (Ubuntu):

```bash
PW=$(openssl rand -hex 32); HASH=$(printf '%s' "$PW" | sha256sum | cut -d' ' -f1)
install -m 640 -o root -g redis /dev/stdin /etc/redis/motuz.conf <<EOF
include /etc/redis/redis.conf
port 0
unixsocket /run/redis/redis-server.sock
unixsocketperm 770
appendonly yes
save ""
user default off
user motuz on #$HASH ~* &* +@all -@admin -@dangerous -@scripting +eval +evalsha +script|load +info +client|setname +client|setinfo +client|id
EOF
install -d /etc/systemd/system/redis-server.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/bin/redis-server /etc/redis/motuz.conf --supervised systemd --daemonize no\n' \
  > /etc/systemd/system/redis-server.service.d/motuz.conf
systemctl unmask redis-server.service; systemctl daemon-reload; systemctl enable redis-server.service; systemctl restart redis-server.service
usermod -aG redis motuz; systemctl restart user@$(id -u motuz).service    # the user manager needs the new group
REDISCLI_AUTH="$PW" redis-cli --user motuz -s /run/redis/redis-server.sock ping    # PONG
```

Check first that nothing else uses this Redis (`redis-cli dbsize`, `redis-cli client list`, the
`requirepass`/`bind`/`port` lines of `/etc/redis/redis.conf`): the configuration above turns TCP
off and the default user off. Then add to the handoff file (append, same permissions):

```bash
( umask 027; { echo 'MOTUZ_BROKER_MODE="system"'; echo 'MOTUZ_REDIS_SOCKET="/run/redis/redis-server.sock"'
  echo "MOTUZ_REDIS_PASSWORD=\"$PW\""; } >> /etc/motuz/handoff-motuz.env )
chown root:motuz /etc/motuz/handoff-motuz.env
```

**9. SELinux (Amazon Linux, enforcing)**: default labels for what was written, no booleans or
modules:

```bash
restorecon -F /etc/sudoers.d/motuz /etc/sysctl.d/60-motuz.conf /etc/pam.d/motuz
restorecon -RF /var/lib/motuz-aws-config /var/lib/motuz-runas /var/lib/motuz
# with the login helper: restorecon -F /etc/systemd/system/motuz-auth.socket /etc/systemd/system/motuz-auth@.service; restorecon -RF /usr/local/lib/motuz-auth
```

**10. Firewall**: only 80/tcp and 443/tcp (Ubuntu: `ufw allow 80/tcp && ufw allow 443/tcp && ufw enable`;
Amazon Linux: the security group, or `firewall-cmd --permanent --add-service=http --add-service=https && firewall-cmd --reload`).

Then the [steps as the `motuz` account](#steps-as-the-motuz-account).

**What root is needed for later.** Updates of Motuz itself (`deploy.sh --pull`) need no
root, also when they pin a new rclone, Traefik or uv (the account's own: `/var/lib/motuz-runas`, `~/.local/bin`).
Run `install.sh` (or the steps above) again when `deploy.sh` says so: it does when
`/var/lib/motuz-runas` is missing (installs from before it), `/etc/pam.d/motuz`
or the login helper in the checkout differ from what is installed, or a system service was
prepared that the account has not picked up. Root is also needed for the firewall and DNS,
joining a directory (`realm join`), mounting shared filesystems, and OS updates (`apt`/`dnf`).
`bin/systemd/uninstall.sh` undoes the root steps ([Uninstall](#uninstall)).

### Steps as the motuz account

```bash
sudo -iu motuz          # or: sudo machinectl shell motuz@
git clone https://github.com/dirkpetersen/motuz.git ~/motuz        # or your fork / branch
~/motuz/bin/systemd/deploy.sh            # every update: ~/motuz/bin/systemd/deploy.sh --pull
```

(From an admin shell, one command without an interactive session: `sudo -iu motuz
motuz/bin/systemd/deploy.sh --pull`. The path is relative to the account's home on purpose: a
`~` would be expanded by your own shell before `sudo` runs.)

`deploy.sh` creates `~/motuz/venv` with uv (the system's own Python 3.14 on Ubuntu and
Amazon Linux, so nothing is downloaded; Docker uses Python 3.12), installs `requirements.txt`
(uWSGI is built from source, as in the image), builds the frontend with the distribution's
Node.js (`npm ci && npm run build`), creates `~/.config/motuz/motuz.env` (settings, from
`deployment/systemd/motuz.env.example`) and `secrets.env` (mode 600: the Flask key, the
database and Redis passwords, empty OAuth client secrets) on the first run and never
overwrites them, merges the passwords and modes `install.sh` left in
`/etc/motuz/handoff-motuz.env`, creates
a self-signed certificate in `~/data/certs` if there is none, writes Redis' and Traefik's
generated configuration and the units, prepares the database (the private cluster, or
checks the login to the system or external one), restarts `motuz.target` and checks
`https://127.0.0.1/api/system/info/`. It warns when something root installed is older than
the checkout ("run bin/systemd/install.sh again as root").

### Settings and secrets

Settings are the `MOTUZ_*` variables of `.env` and `docker-compose.yml`, here in
`~/.config/motuz/motuz.env`; secrets are in `secrets.env` (mode 600). Both use systemd's
EnvironmentFile syntax (`KEY=value` or `KEY="value"`, no `$` expansion). Use
`bin/systemd/config.sh` (as `motuz`; it never prints a secret):

```bash
~/motuz/bin/systemd/config.sh show                        # settings; for secrets only "set" or "empty"
~/motuz/bin/systemd/config.sh set MOTUZ_ACME_DOMAIN motuz.example.org    # a MOTUZ_* setting that is not a secret
~/motuz/bin/systemd/config.sh ask-secret MOTUZ_ONEDRIVE_CLIENT_SECRET    # typed hidden; also MOTUZ_GDRIVE_CLIENT_SECRET, MOTUZ_SMTP_PASSWORD
~/motuz/bin/systemd/deploy.sh                             # apply (or: systemctl --user restart motuz.target)
```

`config.sh set` refuses secrets and the settings `install.sh` and `deploy.sh` manage (the
database and broker modes and addresses, `MOTUZ_AUTH_HELPER`, `MOTUZ_PAM_SERVICE`,
`MOTUZ_SSO_CONFIG_DIR`, `MOTUZ_FRONTEND_DIR`); editing `motuz.env` by hand works for
everything else. Run `deploy.sh` (not just a restart) after changing the `MOTUZ_ACME_*` settings,
because it regenerates Traefik's flags. Nothing is required for a first start.

**An external database.** Set `MOTUZ_DATABASE_MODE=external`, `MOTUZ_DATABASE_HOST` (`host:port`),
`MOTUZ_DATABASE_USER`, `MOTUZ_DATABASE_NAME` in `motuz.env` and `MOTUZ_DATABASE_PASSWORD` in
`secrets.env` (and `MOTUZ_DATABASE_REQUIRE_SSL=true` for a remote server), create the role
and the database owned by it on that server, and run `deploy.sh`: it checks the login and
never installs, enables or waits for `motuz-postgres`.

### HTTPS certificate (TLS, Let's Encrypt)

Traefik (`motuz-traefik`) is the only service that listens beyond loopback: it terminates TLS on
443 (TLS 1.2 and 1.3), redirects 80 to 443 and forwards everything except `/internal/*` to uWSGI
on `127.0.0.1:5000`. It gets its certificate in one of two ways (the same two as in the Docker
install, [Set up HTTPS certificate](#set-up-https-certificate)):

- **Certificate files (default).** `deploy.sh` makes a self-signed certificate in `~/data/certs`
  (`cert.crt`, `cert.key`) if there is none. Replace both with your certificate (full chain in
  `cert.crt`, key mode 0400) and run `systemctl --user restart motuz-traefik`; Traefik reads the
  files only when it starts.
- **Let's Encrypt.** A public DNS name must point at this host and 80/tcp and 443/tcp must be
  reachable from the internet (Let's Encrypt connects to port 80). As `motuz`:

  ```bash
  ~/motuz/bin/systemd/config.sh set MOTUZ_ACME_DOMAIN motuz.example.org
  ~/motuz/bin/systemd/config.sh set MOTUZ_ACME_EMAIL admin@example.org    # optional, contact of the ACME account
  ~/motuz/bin/systemd/deploy.sh                                           # writes Traefik's flags and restarts
  ```

  Traefik requests the certificate on start and renews it about 30 days before it expires. The
  account and the certificates, with their private keys, are stored in `~/data/traefik/acme.json`
  (keep it private, include it in backups); `~/data/certs` is not used in this mode.
  To try without Let's Encrypt's rate limits, first set `MOTUZ_ACME_CA_SERVER` (with `config.sh set`) to the staging CA
  (`https://acme-staging-v02.api.letsencrypt.org/directory`), which browsers do not trust, run
  `deploy.sh`, and when it works set it to an empty value (`config.sh set MOTUZ_ACME_CA_SERVER ''`), delete `~/data/traefik/acme.json` and run `deploy.sh` again. To go
  back to certificate files, set `MOTUZ_ACME_DOMAIN` to an empty value (`config.sh set MOTUZ_ACME_DOMAIN ''`) and run `deploy.sh`.

### Status and logs

Status and logs (as `motuz`; `sudo -iu motuz` sets no session, `deploy.sh` finds the user
manager itself):

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user status 'motuz*'
sudo journalctl _SYSTEMD_USER_UNIT=motuz-app.service -f   # as an admin: motuz is a system
                                                          # account, its logs are in the system journal
```

### Updating

As `motuz`: `~/motuz/bin/systemd/deploy.sh --pull` (a `git pull --ff-only`, then the same steps as
the first deploy; the migrations run when the app starts, a short downtime). It needs no root.
When it prints "run bin/systemd/install.sh again as root" (the pinned rclone or Traefik,
`/etc/pam.d/motuz`, the HEIC converter or the login helper in the checkout differ from what is
installed), update root's clone and run `install.sh` again with the options you used, or do both
halves in one step:

```bash
sudo git -C /root/motuz-install pull --ff-only
sudo /root/motuz-install/bin/systemd/install.sh --local-accounts      # the options of the first run
sudo -iu motuz motuz/bin/systemd/deploy.sh --pull
# or in one step: sudo /root/motuz-install/bin/systemd/bootstrap.sh --local-accounts
```

OS packages (`apt`/`dnf`) are updated as usual; see [Backups and upgrades](#backups-and-upgrades)
for PostgreSQL and Redis.

### Deploy with an AI assistant (Claude Code)

The second half of the install can be done by [Claude Code](https://code.claude.com/docs)
running as `motuz`, guided by a playbook in the repository
(`.claude/skills/motuz-deploy/SKILL.md`). It asks you for what only you know (public host
name, Let's Encrypt or your own certificate, shared mounts, SSSD or local accounts, OAuth
client ids, operator name for `/privacy`, remote or EC2 workers), writes the settings with
`bin/systemd/config.sh`, runs `deploy.sh`, verifies (health check, units, listeners, a
login and a small copy job through `bin/systemd/smoke_test.py`) and reports what root
still has to do. **Read the safety notes below first.**

> **Safety.** The account `motuz` can act as every user except root (its sudoers rule), so
> an AI assistant running as `motuz` is as powerful as a root-like service. Therefore:
> - Run it **only for setup and updates** and close it afterwards; never leave it running.
> - Keep permission prompts **on**. Never use `--dangerously-skip-permissions`, auto-accept
>   or `bypassPermissions`; the settings file below disables bypass mode.
> - Install `deployment/systemd/claude/settings.json` as `~motuz/.claude/settings.json`
>   *before* the first start (command below). It denies `sudo`, `su`, `doas`, `pkexec`,
>   `machinectl`, reading or editing `secrets.env`, `broker.env`, `handoff.env`, `redis.conf`
>   and the TLS key, pipes into a shell, and editing Claude's own settings. This is a
>   guardrail, not a sandbox: you stay the boundary by reading every prompt. As root you can
>   install the same content as Claude Code *managed settings* (see Anthropic's "Settings" documentation
>   for the file location on Linux), which the account cannot weaken.
> - For the strongest setting, take the sudoers rule away while the assistant works
>   (`sudo mv /etc/sudoers.d/motuz /root/motuz.sudoers`) and put it back (`sudo mv
>   /root/motuz.sudoers /etc/sudoers.d/motuz; sudo visudo -c`) before the playbook's
>   login/copy test (the app needs it to run jobs, `deploy.sh` does not).
> - Secrets: the assistant never sees them. It asks you to run `! bin/systemd/config.sh
>   ask-secret MOTUZ_ONEDRIVE_CLIENT_SECRET` (the value is typed hidden). Do not paste secrets
>   into the chat. The playbook's test login uses a password file you create the same way (`! bin/systemd/config.sh ask-test-password`).
> - The sign-in (or API key) is yours, kept only in `~motuz/.claude` (mode 600). Afterwards run
>   `claude logout` or delete `~motuz/.claude` and `~motuz/.claude.json` if your organization
>   prefers, and remove `~motuz/.local/bin/claude`.
> - Nothing in this repository contains credentials, and root installs nothing for Claude Code.

As root, after `install.sh` (nothing else is needed; `git` and `curl` are installed by it):

```bash
sudo -iu motuz            # or: sudo machinectl shell motuz@
```

As `motuz`, install Claude Code with Anthropic's official per-user installer (it installs into
`~/.local/bin` and updates itself, no root; the current instructions are at
https://code.claude.com/docs/en/setup, which also lists `npm install -g @anthropic-ai/claude-code` with a
user-level npm prefix, never `sudo`, as an alternative; Amazon Linux 2027 is not on the page's list of
tested distributions, Ubuntu is), put the guardrails in
place, clone the repository and start it there:

```bash
curl -fsSL https://claude.ai/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"; echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile
git clone https://github.com/dirkpetersen/motuz.git ~/motuz            # or your fork / branch
install -d -m 700 ~/.claude && install -m 600 ~/motuz/deployment/systemd/claude/settings.json ~/.claude/settings.json
cd ~/motuz && claude        # sign in when asked (browser on another machine: use the URL it prints)
```

Inside Claude Code type `/motuz-deploy`. When it is done: `/exit`, then `claude logout` if you
want to remove the sign-in.

### Why a private PostgreSQL cluster?

On Ubuntu the default is the distribution's own PostgreSQL 18 and Redis services, managed
with the standard tools (`pg_lsclusters`, `systemctl status postgresql@18-main`, `apt`
updates, `pg_upgradecluster`, backups as `postgres`). The private mode is still available
(`--private-postgres`, `--private-redis`) and is the default on Amazon Linux. What the
**private** cluster is and why you might choose it:

- It uses the distribution's PostgreSQL 18 **binaries** but runs `postgres` as a user
  service of `motuz` with the data in `~motuz/data/pg` (`motuz-postgres.service`, TCP on
  127.0.0.1:5432, the unix socket in the account's private runtime directory), and keeps the
  distribution's service masked. Start, stop, backups, `psql` as the superuser (peer login of
  the account) and the upgrade to a new major version need **no root**.
- It is isolated: only the account can reach it, nothing else on the machine shares its
  `pg_hba.conf`, `postgresql.conf` or major version, and removing Motuz removes it.
- It is the **same layout on both distributions**, whatever their packaging of services.
- The system mode is what you want if the machine already runs, or will run, other databases
  you manage with the distribution's tools, if you back up with a system-wide tool, or if you
  prefer standard tooling and root-managed services.

Choose with `install.sh --system-postgres` / `--private-postgres` (and `--system-redis` /
`--private-redis`); `MOTUZ_DATABASE_MODE` and `MOTUZ_BROKER_MODE` in `motuz.env` record it.
An existing install keeps its mode: `install.sh` and `deploy.sh` never switch it silently.
To move an existing private cluster to the system service run `sudo bin/systemd/install.sh
--system-postgres` (prepares the empty database and prints the next step), then as `motuz`:

```bash
~/motuz/bin/systemd/migrate_postgres.sh
```

It stops the app and worker, dumps the private cluster, restores it into the system
database as `motuz_user`, **compares the row count of every table**, only then switches
the settings, stops `motuz-postgres` and runs `deploy.sh`. It refuses while copy or
integrity-check jobs run (`--force` stops the app anyway; those jobs fail), starts the app
again if anything fails before the switch, and keeps the old settings:
`migrate_postgres.sh --rollback` returns to the private cluster (changes made in the system
database since are not carried back). `~/data/pg` stays as a fallback until you delete it.
While the private cluster holds port 5432 the system PostgreSQL runs on 5433 (on Ubuntu set
with `pg_conftool 18 main set port 5433`, so `pg_lsclusters` shows it); run `sudo
bin/systemd/install.sh` again after the migration and it moves back to 5432 and updates the
address for `deploy.sh`. Redis can be switched with `install.sh --system-redis` and `deploy.sh`
(the queue is transient; `install.sh` refuses while rclone processes run, because joining the group `redis` restarts the account's user manager). An external database is `MOTUZ_DATABASE_MODE=external`
(see above); `uninstall.sh` never drops a system or external cluster (with `--purge` it drops
only the database `motuz` and the role `motuz_user` of the system service, after printing
what it does).

### Backups and upgrades

The database is the only state worth backing up (the broker holds queued jobs only; secrets
and settings are in `~motuz/.config/motuz`, certificates in `~motuz/data/certs`). A dump,
whichever mode:

```bash
# system PostgreSQL (as root, or any account that may become postgres)
sudo -u postgres pg_dump -Fc motuz > motuz-$(date +%F).dump
# private cluster (as motuz; the socket is in the runtime directory)
pg_dump -h /run/user/$(id -u)/motuz-postgres -Fc motuz > motuz-$(date +%F).dump
# external: pg_dump -h HOST -U motuz_user -Fc motuz  (the password in PGPASSWORD or ~/.pgpass)
```

Restore into an installation at the same Motuz release (stop `motuz-app` and `motuz-celery`
first; the dump holds the schema, so a newer checkout migrates it with `deploy.sh`):

```bash
sudo -u postgres pg_restore --clean --if-exists --no-owner --role=motuz_user -d motuz motuz-2026-10-14.dump   # system
pg_restore -h /run/user/$(id -u)/motuz-postgres --clean --if-exists --no-owner --role=motuz_user -d motuz motuz-2026-10-14.dump   # private
```

Upgrades: Motuz itself (`deploy.sh --pull`, migrations run on every start); minor PostgreSQL
and Redis versions with `apt`/`dnf` (system mode: the service restarts; private mode:
restart `motuz-postgres` afterwards); a new PostgreSQL **major** version: system mode,
`pg_upgradecluster 18 main` (Ubuntu) or the distribution's upgrade procedure, then update
`MOTUZ_DATABASE_HOST` if the port changed; private mode, dump with the old binaries,
install the new `postgresql-N` package, point `PG_BINDIR` in `bin/systemd/distro/<ID>.sh`
and restore into a new `~/data/pg` (the same dump and restore as `migrate_from_docker.sh`).

### Why a sudoers rule

Every file and rclone operation runs as the logged-in user, so the operating system
checks the permissions (in Docker, Motuz runs as root for this). Without Docker, the
account `motuz` gets exactly one rule, `/etc/sudoers.d/motuz`, checked with `visudo -c`:

```
motuz ALL=(ALL, !root, !postgres, !redis, !valkey) NOPASSWD:SETENV: /var/lib/motuz-runas/rclone, /usr/bin/ls, /usr/bin/mkdir, /usr/bin/env
```

- `rclone` copies, lists and reads cloud storage; `ls` and `mkdir` browse the local
  filesystem; `env` starts the small Python readers of the file viewer and of the
  credentials in the home directory (`python3 -I -S -c ...`).
- `!root`: never as root; `!postgres`, `!redis`, `!valkey`: never as the superuser of the
  distribution's PostgreSQL or Redis/Valkey either (with the system modes `motuz` could
  otherwise run `rclone` or `env` as `postgres` and own the whole cluster). `SETENV`: rclone gets a connection's credentials as
  `RCLONE_CONFIG_*` variables, passed with `sudo --preserve-env=<names>` (Ubuntu 26.04's
  default `sudo` is sudo-rs, which ignores `-E`; the classic `sudo` works as well).
- With the classic `sudo` (Amazon Linux, or Ubuntu's `sudo.ws`) the file also gets
  `Defaults:motuz !log_allowed`: that `sudo` logs the variables of `--preserve-env` with
  every allowed command (`ENV=RCLONE_CONFIG_..._SECRET_ACCESS_KEY=...`), which would put
  the connections' secrets into the journal. Refused commands are still logged; Motuz
  logs the commands it runs itself, with the credentials masked.
- `install.sh --sudo-group=GROUP` allows only the members of a group
  (`(%GROUP, !root, !postgres, !redis, !valkey)`).
- Motuz refuses logins as root and as the account it runs as.
- `/var/lib/motuz-runas/rclone` is the account's own file (it updates it without root). That
  adds nothing to the rule: through `env` the account may already run any program as the
  users, and rclone can run commands itself (`--password-command`). Root never runs it.

This rule lets `motuz` act as any other user, as the root of the Docker install does, so
treat the account like root: no password (locked), no other use, its home only readable
by itself.

### Logins (PAM)

Motuz checks passwords with the PAM service `motuz` (`MOTUZ_PAM_SERVICE`,
`/etc/pam.d/motuz`: Ubuntu's `common-auth` and `common-account`; the Docker install
keeps using `login`).

- **SSSD / Kerberos (Active Directory)**: join the machine to the directory as usual
  (`realm join` or your SSSD configuration). `pam_sss` and `pam_krb5` work in the
  unprivileged app itself.
- **Local accounts** (`/etc/shadow`): an unprivileged `pam_unix` can only check the
  password of the account it runs as. `install.sh --local-accounts` installs a small login
  helper, `motuz-auth.socket` / `motuz-auth@.service`: a socket-activated root service
  (Python standard library, a root-owned copy in `/usr/local/lib/motuz-auth`) that runs
  one PAM check per connection on `/run/motuz-auth.sock` (root:motuz, mode 0660; the
  peer must also be the `motuz` account) and answers yes or no. It refuses root,
  accounts below `UID_MIN` (system accounts), `nobody` and `motuz` itself, limits requests
  to 4 KiB, locks a user out for 15 minutes after 5 failed attempts, and never logs
  passwords. `deploy.sh` sets `MOTUZ_AUTH_HELPER=/run/motuz-auth.sock` when it finds the
  socket on the first deploy; without it Motuz uses PAM in-process.

### Storage

Mount the shared filesystems on the host (the `/fh/...` mounts of
`docker-compose.override.yml`) at the same paths and list them in `MOTUZ_REQUIRED_PATHS`
(commas or colons) in `motuz.env`, as for a remote worker: the worker does not start
while one is not mounted (`systemctl --user status motuz-celery` says
which; it retries every 30 seconds), and a job fails with the reason instead of running
on a half-mounted tree.

### VM or LXC

A VM is preferred. A privileged LXC works too; leave nesting off (nesting in a privileged
container exposes more of the host's `/proc` and `/sys`); if `systemd --user` services do
not work there without it, use a VM. In an unprivileged LXC `nesting=1` is fine, but uids
on shared storage need a 1:1 `lxc.idmap`. Hardening a privileged LXC: keep the default
AppArmor and seccomp profiles; no nesting, fuse, mount or keyctl features; add
`lxc.cap.drop: sys_ptrace sys_pacct mknod net_raw syslog wake_alarm block_suspend`;
mount data with `mountoptions=nosuid;nodev` and `backup=0`; set `lxc.cgroup2.pids.max`;
use the Proxmox firewall; let only root@pam edit the container's configuration; and set
`protection: 1`.

More worker machines connect through the HTTPS worker API ("Remote workers (HTTPS
only)"). On such a host, `sudo bin/systemd/install.sh --worker-only` prepares only what
the worker needs (the pinned rclone, python3, the `motuz` account with linger, the same
sudoers rule, `/var/lib/motuz-aws-config`); then configure `worker.env` and start
`motuz-worker.service` as that section describes. Workers never reach the database or
the broker, and the central install needs no change for them.

Other distributions: everything distribution specific (packages, the paths of the
PostgreSQL and Redis binaries, how the distribution's own database service is kept from
running, the PAM stack, the firewall hint) is in `bin/systemd/distro/<ID>.sh`, chosen by
`ID` in `/etc/os-release`: `ubuntu.sh` and `amzn.sh` ([Amazon Linux 2027](#install-on-amazon-linux-2027-default-on-ec2)).

### Migrating from the docker install

`bin/systemd/migrate_from_docker.sh` moves the data, the secrets and the settings:

```bash
# 1. On the docker host, as root, with Motuz running:
sudo bin/systemd/migrate_from_docker.sh export --out /root/motuz-export.tar.gz
docker-compose down            # if the new install is on the same host (ports 80/443)

# 2. On the new host: install.sh, the clone and a first deploy.sh (above); the import goes into the
#    database mode that is configured (system, private or external), then
sudo install -o motuz -m 600 /root/motuz-export.tar.gz /var/lib/motuz/
sudo -iu motuz motuz/bin/systemd/migrate_from_docker.sh import motuz-export.tar.gz   # relative to the home of motuz
```

The export holds the database as a `pg_dump` dump with the row count of every table, the
Flask secret key (it signs the login tokens: sessions stay valid), the SMTP password, the
OAuth client secrets, the `MOTUZ_*` settings of the running app container, Traefik's
`acme.json` and `certs/cert.*`, and the list of the app container's mounts (recreate them
at the same paths). It is secret: delete it after the import. The import refuses a
database at a migration the new checkout does not know (check out the Docker install's
release, or a newer one), recreates the database with `pg_restore` (the private cluster: dropped and
created again; the system or an external PostgreSQL: the `public` schema of the database
is replaced, as `motuz_user`), checks the row counts, writes the secrets and settings and runs
`deploy.sh`, which applies newer migrations.

The data directory is never copied: the Docker image is Alpine (musl libc) and Ubuntu
uses glibc, so text sorts differently and copied indexes on text columns would be
corrupt. A dump and restore rebuilds every index.

The other direction has no script: dump the database ([Backups and upgrades](#backups-and-upgrades)),
restore it into the Docker stack's `database` container (`docker exec -i motuz_database pg_restore ...`
as `motuz_user`), copy `MOTUZ_FLASK_SECRET_KEY` (so logins stay valid) and the other secrets into
`/docker/secrets`, and start the stack.

### Tests of this install

`test/e2e/run_systemd.sh` starts an Ubuntu 26.04 cloud image as a VM (QEMU/KVM in a
container, `test/e2e/systemd/vm.sh`; needs docker and `/dev/kvm`), runs `install.sh
--local-accounts` and `deploy.sh` in it, and runs the same end-to-end suites as
`test/e2e/run.sh` against it (`MOTUZ_E2E_TARGET=systemd`), plus security checks
(`test/e2e/systemd/security_test.sh`: processes, sudo, ports, the system PostgreSQL's and Redis' access). `MOTUZ_E2E_INSTALL_OPTS` passes more options to `install.sh`, e.g.
`--private-postgres --private-redis` to test the private mode on Ubuntu. `test/e2e/systemd/migration_test.sh` migrates
the Docker e2e stack into such a VM.

### Uninstall

`sudo bin/systemd/uninstall.sh` stops Motuz and removes the units (and the drop-ins), the
sudoers rule, the PAM service, the sysctl file and the login helper, and unmasks the
distribution's PostgreSQL and Redis/Valkey services; the account and its data stay.
`--purge --yes` also deletes the account, its home (a private database, secrets,
certificates) and the pinned binaries. With the distribution's services it never touches
them: `--purge` drops only the database `motuz` and the role `motuz_user` (and our `pg_hba.conf`
lines) of the system PostgreSQL, and removes `/etc/redis/motuz.conf` and the drop-in. Packages
stay installed.

### The account's home: /home or /var/lib

The account's home holds the checkout, the settings and secrets, and in the private modes the
database (`~/data`), so it needs space. Without `--home`, `install.sh` creates it as
`/home/motuz` if `/home` is a local filesystem (not NFS, CIFS, FUSE, autofs, Ceph, Lustre,
GPFS, ...) other than the one of `/var/lib` with more free space, and as `/var/lib/motuz`
otherwise; a home on a network filesystem is refused. `/var/lib/motuz-runas` (the rclone the
users run) stays in `/var/lib`. An existing account keeps its home (`install.sh` warns).

**Moving the account's home** starts from an empty database (the stored connections, jobs and
logins are lost; the users create their connections again):

1. As `motuz`, save what you want to keep: `~/.config/motuz/motuz.env` (settings, e.g. the
   OneDrive/Google app ids) and `~/data/certs/` (your TLS certificate, if not Let's Encrypt),
   e.g. `(umask 077 && tar -C ~ -czf /tmp/motuz-keep.tgz .config/motuz/motuz.env data/certs)`.
   Keep the secrets of `secrets.env` you set yourself (`config.sh show` lists their names),
   such as an OAuth client secret; the database password and the keys are created again.
2. As root, from a root-only checkout: `bin/systemd/uninstall.sh --purge --yes`.
3. As root: `bin/systemd/install.sh` with the options of the first install; it prints the
   new home (`account motuz (/home/motuz)`).
4. As `motuz`: clone the repository into `~/motuz`, restore `motuz.env` and `data/certs/`
   (only the settings you changed: the database settings are written by `deploy.sh`), set
   your secrets with `bin/systemd/config.sh ask-secret NAME`, then run
   `~/motuz/bin/systemd/deploy.sh`. `bin/systemd/bootstrap.sh` does steps 3 and 4 in one.

## Install on Amazon Linux 2027 (default on EC2)

On EC2, install Motuz on Amazon Linux 2027 (AL2027, a public preview since September 2026)
with the same scripts as [Install (default: without Docker)](#install-default-without-docker):
`systemd --user` services of the account `motuz`, the same units, settings
(`~/.config/motuz/motuz.env`, including the `MOTUZ_RCLONE_*` defaults), sudoers rule,
login helper, migration and update steps. They start faster than containers, run as fast,
and work with SELinux enforcing. What differs is in `bin/systemd/distro/amzn.sh`:

| | Amazon Linux 2027 | Ubuntu 26.04 |
| --- | --- | --- |
| PostgreSQL 18 | `postgresql18-server` (binaries in `/usr/bin`) | `postgresql-18` |
| Celery broker (`motuz-redis`) | **Valkey 9** (`valkey-server`; AL2027 has neither RabbitMQ nor Redis) | Redis 8 |
| Python of `~/motuz/venv` | the system's **Python 3.14** (`python3`, `python3-devel` for uWSGI) | the system's **Python 3.14** (`python3`, `python3-dev` for uWSGI) |
| Node.js (frontend build) | `nodejs24`, `nodejs24-npm` | `nodejs` 22 |
| `/etc/pam.d/motuz` | `password-auth` (authselect) | `common-auth`, `common-account` |
| SELinux | enforcing, nothing to change | (AppArmor, nothing to change) |

rclone, Traefik and uv are the pinned downloads with their SHA256 in both cases (never
AL2027's own `rclone` package, which is another version).

**Broker.** Valkey speaks the Redis protocol, so Celery uses its Redis transport
(`MOTUZ_CELERY_BROKER_URL=redis+socket://...`, written to `broker.env` by `deploy.sh`; the
Docker install keeps `amqp://` RabbitMQ). The Redis transport redelivers a task that a
worker has fetched but not acknowledged within `visibility_timeout`. Motuz acknowledges a
task when it starts (Celery's default, `acks_late` off), so only jobs that wait behind
other long jobs in the worker's prefetch are affected; their timeout is 30 days
(`MOTUZ_CELERY_VISIBILITY_TIMEOUT`, seconds), far above any job. Tasks are not acknowledged
late on purpose: a job whose worker dies is failed, not silently run again days later.
Stopping a job (`revoke(terminate=True)`) goes through Celery's remote control, which the
Redis transport supports (pub/sub on the same socket).

**Python.** `requirements.txt` is compiled for 3.12, the Python of the Docker image. On
AL2027 and Ubuntu 26.04 all backend unit tests (and on AL2027 the end-to-end suites) pass on
their Python 3.14 with these pins (uWSGI 2.0.31 builds), so `deploy.sh` uses
`/usr/bin/python3.14` (`DISTRO_PYTHON`): it gets the distribution's security updates and
needs no download. Run `install.sh` again after an update so `python3-dev` (headers for
uWSGI) is installed before `deploy.sh`.

**SELinux** stays enforcing; no booleans, file contexts or policy modules are needed.
`install.sh` runs `restorecon` on everything it writes. The account's user services
(uWSGI, Celery, rclone, Traefik, PostgreSQL, Valkey) run as `unconfined_u` like any login
of the account, `sudo` to the logged-in user keeps that context, and the login helper is a
system service (`unconfined_service_t`) that may read `/etc/shadow`. Traefik binds 80 and
443 through the same sysctl as on Ubuntu. Check with `sudo ausearch -m avc -ts boot`
(empty). PostgreSQL and Valkey are user services of `motuz` by default
(`motuz-postgres`, `motuz-redis`) rather than the packages' system services (which would
run confined as `postgresql_t` / `valkey_t`): `deploy.sh` manages everything as the account
without root, and the database and the broker are reachable only by that account.
`install.sh --system-postgres` uses the package's `postgresql.service` instead
(`postgresql-setup --initdb`, the role and database as described under
[Root steps](#root-steps-installsh)); there is no system Valkey mode, because `valkey.conf`
has no include directory and a drop-in for the unit would still mean rewriting the package's
configuration. The system PostgreSQL service on AL2027 is as simple as on Ubuntu (it works the same way and is
tested with `MOTUZ_E2E_INSTALL_OPTS=--system-postgres`), so the default could change; it is
private for now to keep one design with the single Valkey.


### Install on an instance

Launch AL2027 (the AMI of the SSM parameter
`/aws/service/ami-amazon-linux-latest/al2027-preview-ami-kernel-default-arm64`, or
`-x86_64`), at least 4 GiB of memory (for example c7g.large) and a 16 GiB root volume,
IMDSv2 only, an instance profile with `AmazonSSMManagedInstanceCore` for Session Manager
(no SSH), and a security group with 443 (and 80, for the redirect and Let's Encrypt) open
to your users only. As user data, `deployment/aws/user-data-al2027.sh` (edit `REPO`,
`BRANCH` and `INSTALL_ARGS` first) clones the repository to `/root/motuz-install` and
runs `bin/systemd/bootstrap.sh`, which runs `install.sh`, clones the account's
`~/motuz` and runs `deploy.sh`; the output is in `/var/log/cloud-init-output.log`:

```bash
aws ec2 run-instances --image-id resolve:ssm:/aws/service/ami-amazon-linux-latest/al2027-preview-ami-kernel-default-arm64 \
    --instance-type c7g.large --iam-instance-profile Name=<profile with AmazonSSMManagedInstanceCore> \
    --security-group-ids <sg> --user-data file://deployment/aws/user-data-al2027.sh \
    --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled \
    --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3,Encrypted=true}' \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=motuz}]'
```

On an existing instance, as root (the same as the user data does):

```bash
sudo dnf -y install git
sudo git clone --branch <branch> https://github.com/<you>/motuz.git /root/motuz-install
sudo /root/motuz-install/bin/systemd/bootstrap.sh --repo=https://github.com/<you>/motuz.git --branch=<branch> --local-accounts
```

`install.sh` and `bootstrap.sh` run from root's own clone, never from the account's
`~/motuz` (the account must not be able to change what root runs). Then add users
(`useradd` + `passwd` with `--local-accounts`, or join the directory with
`realm join` / SSSD and `authselect select sssd`, without `--local-accounts`), set
`MOTUZ_ACME_DOMAIN` for a Let's Encrypt certificate (port 80 must be reachable) and
connect with `aws ssm start-session --target <instance id>`.

**Updates**: `sudo git -C /root/motuz-install pull && sudo /root/motuz-install/bin/systemd/bootstrap.sh
--local-accounts` (idempotent: `install.sh` again, `git pull --ff-only` of `~/motuz`,
`deploy.sh`), or only `sudo -iu motuz motuz/bin/systemd/deploy.sh --pull` when the
installed pieces did not change (`deploy.sh` warns otherwise). `dnf upgrade` keeps
PostgreSQL 18 and Python 3.14; after a Python minor change `deploy.sh` rebuilds the venv.

**Uninstall**: see [Uninstall](#uninstall).

### Remote workers on AL2027

`install.sh --worker-only` installs a [remote worker](#remote-workers-https-only) without
the server: `python3`, `sudo`, the pinned rclone, the account with the same sudoers rule,
and the user unit `motuz-worker.service` with `~/.config/motuz-worker/worker.env`
(`MOTUZ_HOME` = the checkout it runs from, which must be readable by the account, for
example a root-owned clone in `/opt/motuz`). Non-interactive, for example from user data:

```bash
git clone --branch <release> https://github.com/<you>/motuz.git /opt/motuz
# a permanent worker: starts motuz-worker with the secret of `manage.py workers add`
/opt/motuz/bin/systemd/install.sh --worker-only --central-url=https://motuz.example.org --pool=onprem --credential-file=/root/credential
# a temporary worker: one job with a bootstrap token, then e.g. shutdown
/opt/motuz/bin/systemd/install.sh --worker-only --central-url=https://motuz.example.org --pool=aws
/opt/motuz/bin/systemd/worker_once.sh --bootstrap-token-file=/run/motuz-bootstrap
```

`worker_once.sh` moves the token into the account's private runtime directory and runs
`motuz_worker.py --bootstrap-token-file ... --once` as the transient user service
`motuz-worker-once` (so it runs as the account, in its user manager, not in cloud-init's
context), deletes the token and exits with the worker's exit code (0 done, 1 error, 3 no
job, 78 configuration error).

`--run-as=NAME` makes a cloud-to-cloud worker like the [temporary EC2
workers](#temporary-ec2-workers): rclone runs as the system account NAME (created, no
shell) instead of the jobs' owners, the sudoers rule allows only `/var/lib/motuz-runas/rclone`
as NAME, and `worker.env` gets `MOTUZ_WORKER_RUN_AS` (local jobs and credentials from a
home directory are refused). A prebuilt AMI for those workers: `install.sh --worker-only
--run-as=motuzjob` from a checkout in `/opt/motuz` at the server's release, plus
`/opt/motuz/.motuz-source` with `<source url> <ref>`; the launcher's user data then skips
its downloads.

### Tests on AL2027

`test/e2e/run_systemd.sh --distro=al2027` runs the same suites as for Ubuntu against an
AL2027 container with systemd as PID 1 (`test/e2e/systemd/container.sh`,
`al2027.Dockerfile`: privileged, its own network namespace, so it needs no free ports on
the host). There is no AL2027 VM image outside EC2 and a container cannot enforce SELinux,
so SELinux is tested on an instance: the same `run_systemd.sh --inside-setup` and
`--inside` run there as root, then `ausearch -m avc` must be empty.
`test/e2e/systemd/worker_host_test.sh` (after `run_systemd.sh --distro=al2027 --keep`)
installs a worker with `install.sh --worker-only` in a second container that shares the
first one's network, runs a job there and one with `worker_once.sh`; `--inside` does the
same on the server itself (e.g. on the instance).

## Configuration and features

These apply to both installs. The settings are the same `MOTUZ_*` variables; where they live
differs:

| | Install without Docker (default) | Docker |
| --- | --- | --- |
| Settings | `~/.config/motuz/motuz.env`: `bin/systemd/config.sh set KEY VALUE` | `.env` (or the environment that runs `./start.sh`) |
| Secrets | `~/.config/motuz/secrets.env`: `bin/systemd/config.sh ask-secret KEY` (typed hidden) | files in `/docker/secrets` |
| Apply | `bin/systemd/deploy.sh` (or `systemctl --user restart motuz.target`) | `bin/redeploy.sh` (or `./start.sh`) |
| `manage.py` commands | the `motuz_manage` function below | `docker exec motuz_app bash -c 'source ./load-secrets.sh >/dev/null && python3 manage.py ...'` |

`manage.py` commands on the install without Docker (as `motuz`; the same environment as the
units, one transient user service per call):

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
motuz_manage() { systemd-run --user --wait --pipe --quiet \
    -p WorkingDirectory=$HOME/motuz/src/backend -E PYTHON_ENVIRONMENT=prod \
    -p EnvironmentFile=$HOME/.config/motuz/motuz.env -p EnvironmentFile=$HOME/.config/motuz/secrets.env \
    -p EnvironmentFile=$HOME/.config/motuz/broker.env \
    $HOME/motuz/venv/bin/python manage.py "$@"; }
motuz_manage workers list
```

The docker-specific lines in the sections below (`.env`, `/docker/secrets`, `bin/redeploy.sh`)
are the Docker column of this table; use the other column for the default install.

### S3 connections

A connection of type **S3** has a **Provider** right below the type: rclone's list of S3
implementations, Amazon S3 first, then Ceph (RADOS Gateway), then MinIO, Wasabi, Cloudflare R2
and the others rclone 1.75 knows, and "Any other S3 compatible provider" last. For anything but
Amazon S3 the **Endpoint URL** appears right below the provider and the region starts as
`us-east-1`, which most S3 compatible servers accept; for Amazon S3 the endpoint stays in the
advanced options (VPC or FIPS endpoints only). Connections created before the provider existed
count as Amazon S3 without an endpoint and "Other" with one; switch an on-prem Ceph connection to
Ceph to get the options below.

Advanced options:

- **Ignore certificate errors**: for storage you trust whose certificate does not match its name
  or is self-signed; only this connection skips the check (the other side of a copy still checks).
- **Chunked uploads** (on by default): files from 32 MiB on go up in parts, in parallel, which is
  the fastest. S3 stores no MD5 for objects uploaded in parts, so **Check Integrity is not
  available** for copies to such a connection (the button is greyed out). Off: files up to 5 GiB
  go up in one piece and keep their MD5 (S3's limit for one upload; larger files are still
  uploaded in parts and cannot be checked).
- **Checksum checks** (Amazon S3 and Ceph only, which verify the `Content-MD5` of every upload
  request themselves):
  - *Full checks* (default): rclone also compares the MD5 of every file after the copy.
  - *Skip rclone's check after the copy* (recommended for speed, `--ignore-checksum` for copies to
    this connection): S3 still verifies every request. Measured on 8 vCPUs, AWS us-west-2,
    100 MB files: 1.1 GB/s with rclone's defaults, 2.2 GB/s with chunked uploads and this option,
    because about two thirds of rclone's CPU time went into MD5.
  - *CRC32C instead of MD5 for parts* (Amazon S3 only): like the previous option, and each part of
    a chunked upload carries its CRC32C instead of its MD5 (`RCLONE_S3_PART_CHECKSUM=crc32c`). S3
    verifies it just the same and stores a composite CRC32C of the object; it costs almost no CPU.
  - *Skip all checks* (not recommended): additionally no checksum with the parts
    (`RCLONE_S3_PART_CHECKSUM=none`) and `AWS_REQUEST_CHECKSUM_CALCULATION=when_required`, so S3
    no longer verifies uploads sent in one piece either.

  The part checksums need the rclone build with `--s3-part-checksum` (below); a stock rclone
  ignores the variable and sends each part's MD5, i.e. behaves like the option before.

**Faster MD5 and part checksums (rclone build).** Two options of the rclone branch
[`s3-md5-simd`](https://github.com/dirkpetersen/rclone/tree/s3-md5-simd) (rclone master plus
these options, proposed upstream): `--md5-simd` computes MD5s with
[md5-simd](https://github.com/minio/md5-simd), up to 16 streams at once per core (local files,
Check Integrity, S3 upload parts; x86-64 with AVX2 or AVX512 only, other CPUs such as arm64 use
the standard MD5), and `--s3-part-checksum md5|crc32c|none` chooses the checksum
of each upload part. Motuz passes both as rclone variables (`RCLONE_MD5_SIMD*`,
`RCLONE_S3_PART_CHECKSUM`), which a stock rclone ignores, so they are safe with either. Measured
on 8 vCPUs, AWS us-west-2, 33 GB of 100 MB files, 32 transfers, upload concurrency 16, chunked
uploads, rclone's check after the copy skipped (release
[v1.76.0-md5simd.2](https://github.com/dirkpetersen/rclone/releases/tag/v1.76.0-md5simd.2)):

| Part checksum | Upload | CPU per GB |
|---|---|---|
| MD5, Go's crypto/md5 (stock rclone) | 2.6 GB/s | 2.7 core-s |
| MD5 with md5-simd (3 servers) | 3.1 GB/s | 2.0 core-s |
| CRC32C | 3.8 GB/s | 1.7 core-s |
| CRC32C and `MOTUZ_RCLONE_BUFFER_SIZE=0` | 4.2-4.5 GB/s (34-36 Gb/s) | 1.4-1.5 core-s |
| none and `MOTUZ_RCLONE_BUFFER_SIZE=0` | 4.3-4.4 GB/s | 1.4 core-s |

With CRC32C the SDK computes the checksum while it sends each part (an `aws-chunked` trailer), so it
costs as little as no checksum while S3 still verifies every part: the recommended choice for
Amazon S3. A buffer size of 0 saves the copy through rclone's read-ahead buffer, about 10% for
local sources served from the page cache, so copy jobs from a local source get `--buffer-size=0`
unless `MOTUZ_RCLONE_BUFFER_SIZE` is set (cloud sources keep rclone's 16M). For slow or network
file systems, where the read-ahead may help, set e.g. `MOTUZ_RCLONE_BUFFER_SIZE=16M`. At that point the 8 vCPUs are busy (28% user, 29% system, 27% softirq):
the rest is the kernel's sending and file reading, and TLS. `rclone md5sum` of the same files:
8.7 instead of 14.2 CPU-seconds with md5-simd.

**Local to local: SHA-256 instead of MD5.** When both sides of a job are local paths, the copy is
checked with SHA-256 (`RCLONE_LOCAL_HASHES=sha256`) and Check Integrity runs `rclone hashsum
sha256` instead of `md5sum`. Go's SHA-256 uses the CPU's SHA instructions (x86 SHA-NI, ARMv8
SHA2): 1.8 GB/s per stream against 0.8 GB/s for MD5 on an AMD EPYC 9534. Copy of 3 GB (30 files
of 100 MB) between two local folders, 16 transfers, median of 3 runs:

| Check after the copy | Time | User CPU |
|---|---|---|
| MD5 (rclone's default) | 1.68 s | 7.8 s |
| SHA-256 | 1.07 s | 3.5 s |
| none | 0.99 s | 0.1 s |

Whenever a cloud is involved, both stay MD5: clouds record MD5 (the S3 ETag), not SHA-256.

### OneDrive: own app registration

"Sign in with Microsoft" (Clouds > New connection > OneDrive) uses rclone's
public OneDrive app by default. Its only redirect address is
`http://localhost:53682/`, so after signing in, users copy the address of the
error page the browser shows and paste it into Motuz. All Motuz installations
and rclone users share that app's Microsoft Graph throttling quota.

With your own app registration in Microsoft Entra ID, Microsoft redirects back
to Motuz, which completes the sign-in by itself, and Motuz gets its own
throttling quota.

1. In the [Microsoft Entra admin center](https://entra.microsoft.com/), open
   *App registrations* > *New registration*:
   - Name: e.g. `Motuz`
   - Supported account types: *Accounts in any organizational directory
     (Multitenant)*. Motuz signs in through Microsoft's `common` endpoints, like
     rclone, which single-tenant apps cannot use. The app still only works in
     tenants whose administrator consents to it (step 3).
   - Redirect URI: platform *Web*,
     `https://motuz.example.org/api/oauth/onedrive/callback` (your Motuz host
     name; it must match `MOTUZ_ONEDRIVE_REDIRECT_URI` exactly).
2. Note the *Application (client) ID* on the app's *Overview* page.
3. *API permissions* > *Add a permission* > *Microsoft Graph* > *Delegated
   permissions*: `Files.ReadWrite.All`, `Sites.Read.All`, `offline_access` and
   `User.Read` (usually already there). Then *Grant admin consent for
   <tenant>*: users cannot consent to `Files.ReadWrite.All` and
   `Sites.Read.All` themselves, so without the tenant's admin consent they see
   "Need admin approval".
4. *Certificates & secrets* > *Client secrets* > *New client secret*. Copy its
   *Value* (not the *Secret ID*); it is shown only once. Note the expiry date:
   Motuz cannot refresh tokens once the secret expires.
5. Configure Motuz. The id and redirect URI go in `.env` (or the environment
   that runs `./start.sh`, which takes precedence), the secret in a docker
   secret file, never in `.env`:
   ```bash
   MOTUZ_ONEDRIVE_CLIENT_ID=<Application (client) ID>
   MOTUZ_ONEDRIVE_REDIRECT_URI=https://motuz.example.org/api/oauth/onedrive/callback
   ```
   ```bash
   # $MOTUZ_DOCKER_ROOT/secrets, /docker by default
   (umask 077 && printf '%s' '<secret value>' > /docker/secrets/MOTUZ_ONEDRIVE_CLIENT_SECRET)
   ```
6. Redeploy with `bin/redeploy.sh` (or `./start.sh`).

Without Docker, as `motuz` (instead of steps 5 and 6):

```bash
~/motuz/bin/systemd/config.sh set MOTUZ_ONEDRIVE_CLIENT_ID <Application (client) ID>
~/motuz/bin/systemd/config.sh set MOTUZ_ONEDRIVE_REDIRECT_URI https://motuz.example.org/api/oauth/onedrive/callback
~/motuz/bin/systemd/config.sh ask-secret MOTUZ_ONEDRIVE_CLIENT_SECRET     # typed hidden
~/motuz/bin/systemd/deploy.sh
```

`bin/prod/start.sh` creates `/docker/secrets/MOTUZ_ONEDRIVE_CLIENT_SECRET` as
an empty file (mode 600) if it does not exist, because docker-compose does not
start with a missing secret file. Empty or unset values mean "not configured":
without `MOTUZ_ONEDRIVE_CLIENT_ID` Motuz uses rclone's app and ignores the
secret and the redirect URI.

Each connection remembers which app issued its token and is always refreshed
with that app:

- Connections created before the own app was configured, and connections
  created by pasting a token from `rclone config`, keep using rclone's app.
- Rotating the client secret (same client id) keeps all connections working:
  replace the file and redeploy before the old secret expires.
- Connections created with an own app stop working when its client id changes
  or is removed from the configuration. Their jobs fail with "This OneDrive
  connection was created with a different app registration..."; the owner
  signs in with Microsoft again (or pastes an rclone token into the
  connection).

The edit dialog of a OneDrive connection shows which app it uses. The client
secret is only used by the `app` container (sign-in and token refresh); rclone
never receives it.

### Google Drive

Clouds > New connection > *Google Drive (beta)* creates an rclone `drive`
remote with the `drive` scope (full access to the user's files). My Drive is the
default; a shared drive (formerly Team Drive) sets rclone's `team_drive`, and an
optional root folder id starts the connection in a folder. As with OneDrive,
the token broker keeps the token fresh and rclone never sees the refresh token.
Jobs use rclone's recommendations for Drive: API calls paced at 10 per second
(`--tpslimit 10`, which applies to the whole rclone process, and a 100ms pacer),
64 MiB upload chunks (buffered in memory per transfer), and the job fails when
Google's daily upload limit (about 750 GiB per user) is reached.

There are three ways to connect:

1. **Paste a token from `rclone config`** (*Advanced* in the dialog, which
   shows the steps): `rclone config`, new remote, storage `drive`, empty
   `client_id` and `client_secret`, scope `1`, confirm that you want to use
   rclone's shared client, sign in, then `rclone config show <name>` and paste
   the `token` JSON (and `team_drive`, if any). Only tokens of rclone's own
   client work: Motuz cannot refresh a token issued to a client id whose secret
   it does not have.
2. **Sign in with Google, rclone's app** (default): Motuz opens Google's sign-in
   page. rclone's app only redirects to `http://127.0.0.1:53682/`, so the
   browser shows an error page afterwards and the user pastes its address into
   Motuz, then picks My Drive or a shared drive.
3. **Sign in with Google, own OAuth client**: Google redirects back to Motuz,
   which completes the sign-in by itself.

**rclone's shared client is not a long-term option.** It is shared by all
rclone users worldwide and heavily rate limited (Google's default quota is per
client id), and rclone announced that it is being retired and will stop
working during 2026 (`rclone config` now warns about it). When that happens,
connections created with it (pasted tokens and option 2) stop refreshing and
users must sign in again with an own client. Configure an own client for
production.

#### Own OAuth client

1. In the [Google Cloud Console](https://console.cloud.google.com/), create or
   select a project, then *APIs & Services* > *Library* > *Google Drive API* >
   *Enable*. Without it, sign-in succeeds but Motuz reports "Google Drive
   cannot be accessed: Google Drive API has not been used in project ...".
2. *Google Auth Platform* (formerly *OAuth consent screen*): app name, support
   email, contact email.
   - *Audience*: *Internal* if all users are in your Google Workspace domain
     (no verification needed, only your domain's accounts can sign in);
     *External* otherwise.
   - *Data access*: add the scope `https://www.googleapis.com/auth/drive`.
3. *Clients* > *Create client* > type *Web application*, authorized redirect
   URI `https://motuz.example.org/api/oauth/gdrive/callback` (your Motuz host
   name; it must match `MOTUZ_GDRIVE_REDIRECT_URI` exactly). Copy the client id
   and the client secret.
4. Configure Motuz: the id and redirect URI in `.env` (or the environment that
   runs `./start.sh`), the secret in the docker secret file, never in `.env`:
   ```bash
   MOTUZ_GDRIVE_CLIENT_ID=<client id>.apps.googleusercontent.com
   MOTUZ_GDRIVE_REDIRECT_URI=https://motuz.example.org/api/oauth/gdrive/callback
   ```
   ```bash
   (umask 077 && printf '%s' '<client secret>' > /docker/secrets/MOTUZ_GDRIVE_CLIENT_SECRET)
   ```
5. Redeploy with `bin/redeploy.sh` (or `./start.sh`). `bin/prod/start.sh`
   creates an empty `MOTUZ_GDRIVE_CLIENT_SECRET` if it is missing; empty or
   unset values mean "not configured" (rclone's app). Without Docker, as `motuz`:
   `bin/systemd/config.sh set MOTUZ_GDRIVE_CLIENT_ID <client id>.apps.googleusercontent.com`,
   `config.sh set MOTUZ_GDRIVE_REDIRECT_URI https://motuz.example.org/api/oauth/gdrive/callback`,
   `config.sh ask-secret MOTUZ_GDRIVE_CLIENT_SECRET`, then `bin/systemd/deploy.sh`.

Google's restrictions on the `drive` scope, which Google classifies as
*restricted*:

- An *External* app that is not verified is in *Testing* mode: only the test
  users listed under *Audience* (up to 100) can sign in, they see a "Google
  hasn't verified this app" warning, and Google expires their refresh tokens
  after 7 days, so those connections need a new sign-in every week. Publishing
  the app for everyone requires Google's verification of the restricted scope,
  which includes a security assessment by a third party.
- An *Internal* app avoids verification, but only works for accounts of the
  Workspace organization that owns the Cloud project.
- Google Workspace administrators can block third-party apps or unconfigured
  apps (*Admin console* > *Security* > *API controls* > *App access
  control*). Users then see "Access blocked" or "admin_policy_enforced", the
  same situation as a OneDrive tenant that requires admin approval: the
  administrator has to trust the client id (Motuz's own client, or rclone's
  `202264815644.apps.googleusercontent.com`).
- Google sends a refresh token only with `access_type=offline` and
  `prompt=consent`, which Motuz always requests. Refresh tokens stop working
  when the user revokes access (myaccount.google.com > *Security* >
  *Third-party apps*), when an administrator removes the app's access, or
  after 6 months without use.

Each connection remembers the client that issued its token
(`gdrive_client_id`) and is always refreshed with it, exactly like OneDrive:
pasted tokens and connections created before the own client was configured
keep using rclone's app; changing or removing `MOTUZ_GDRIVE_CLIENT_ID` breaks
connections of the old client ("This Google Drive connection was created with
a different OAuth client ... Sign in with Google again"); rotating only the
secret keeps them working. The client secret is used only by the `app`
container.

### Privacy policy and terms

Motuz serves a privacy policy at `https://<host>/privacy` and terms of service
at `https://<host>/terms`: plain HTML without login or JavaScript
(`src/backend/api/views/legal_views.py`, templates in
`src/backend/api/templates/legal/`). The login page and the page shown without
JavaScript (`src/frontend/index.html`) describe the app and link to both.
Google's consent-screen branding (*Google Auth Platform* > *Branding*) needs
exactly these: the home page `https://<host>/`, the privacy policy link and the
terms of service link, all on a domain you have verified in Google Search
Console.

Set who runs the installation in `.env` (without Docker: `motuz.env`, e.g. `bin/systemd/config.sh set MOTUZ_OPERATOR_NAME "Example Research Institute"`, then `deploy.sh`; all optional; without them the pages
refer to "the administrator of this Motuz installation"):
```bash
MOTUZ_OPERATOR_NAME=Example Research Institute
MOTUZ_CONTACT_EMAIL=motuz-admin@example.org
MOTUZ_OPERATOR_URL=https://www.example.org/
```
The text describes what Motuz itself does. Review it with your institution
(e.g. log retention, backups, who handles data protection requests), edit the
templates if needed, and change `EFFECTIVE_DATE` in `legal_views.py` whenever
the text changes.

### Performance tuning

Motuz copies with rclone. With rclone's defaults (4 parallel transfers, 8 checkers, 4
streams per file above 256 MiB, S3 parts of 5 MiB with 4 in flight) one job rarely goes
beyond a few Gb/s, however fast the link. Two levels of settings change that:

- **Installation defaults** in `.env` (passed to `app` and `celery` by
  `docker-compose.yml`) or in `motuz.env` (the commented `MOTUZ_RCLONE_*` lines of
  `deployment/systemd/motuz.env.example`; run `deploy.sh` after changing them). Unset or
  empty means Motuz's default of 16 transfers and 32 checkers (lowered to the `MAX_*`
  caps, and left out if they would not fit the memory budget), and rclone's own default,
  i.e. no flag, for everything else.
- **Per job**, in the collapsed "Performance" section of the New Copy Job dialog: a
  preset or custom values for the destination's type, within the server's caps and
  memory budget. The job detail shows them, Retry keeps them, and "Check Integrity"
  passes the checkers on.

| Setting (`.env`, or `motuz.env` without Docker) | rclone flag | rclone default | Per job |
|---|---|---|---|
| `MOTUZ_RCLONE_TRANSFERS` | `--transfers` | 4 | yes |
| `MOTUZ_RCLONE_CHECKERS` | `--checkers` (also for integrity checks) | 8 | yes |
| `MOTUZ_RCLONE_MULTI_THREAD_STREAMS` | `--multi-thread-streams` (0: off) | 4 | yes |
| `MOTUZ_RCLONE_MULTI_THREAD_CUTOFF` | `--multi-thread-cutoff` | 256M | yes |
| `MOTUZ_RCLONE_BUFFER_SIZE` | `--buffer-size` (unset: 0 for local sources) | 16M | no |
| `MOTUZ_RCLONE_S3_UPLOAD_CONCURRENCY` | `--s3-upload-concurrency` | 4 | yes (S3 destinations) |
| `MOTUZ_RCLONE_S3_CHUNK_SIZE` | `--s3-chunk-size` (5M to 5G) | 5M | yes (S3 destinations) |
| `MOTUZ_RCLONE_AZUREBLOB_UPLOAD_CONCURRENCY` | `--azureblob-upload-concurrency` | 16 | yes (Azure destinations) |
| `MOTUZ_RCLONE_AZUREBLOB_CHUNK_SIZE` | `--azureblob-chunk-size` | 4M | yes (Azure destinations) |

Caps for the per-job values: `MOTUZ_RCLONE_MAX_TRANSFERS` (default 64),
`MOTUZ_RCLONE_MAX_CHECKERS` (128), `MOTUZ_RCLONE_MAX_MULTI_THREAD_STREAMS` (32),
`MOTUZ_RCLONE_MAX_UPLOAD_CONCURRENCY` (64, S3 and Azure), and the memory budget per job
`MOTUZ_RCLONE_MEMORY_BUDGET` (default `8G`). `MOTUZ_RCLONE_MD5_SIMD_SERVERS` (default 2, 0 to
16, 0 = off) sets the md5-simd servers of copy and Check Integrity jobs, each using up to
one core (README "S3 connections"; ignored by a stock rclone). Numbers are whole numbers, sizes use
rclone's binary units and need a unit (`64M`, `1.5G`, `64Mi`). An invalid value, an
installation default above its cap or defaults above the memory budget stop the app and
the worker at startup, with a message that names the variable.

**What matters when**

- *Many small files* (below ~100 MB): per-file overhead dominates. Raise
  `--transfers` (16 to 64) and `--checkers` (32 to 128); streams and chunk sizes hardly
  matter. On S3 the request rate of the bucket (prefix) may become the limit.
- *Few large files*: a single file must be split. Raise `--multi-thread-streams`
  (8 to 16), lower `--multi-thread-cutoff` (64M to 128M), and for S3/Azure raise the chunk
  size (32M to 128M) and the upload concurrency (8 to 32). Transfers can stay small.
- *Mixed data* needs both, which is what costs memory (see below).

**Suggested installation defaults**, per link speed (one job; several jobs share the
link and the node's memory):

| Link | transfers | checkers | streams | cutoff | S3/Azure chunk | upload concurrency | memory estimate (S3) | budget |
|---|---|---|---|---|---|---|---|---|
| 10 Gb/s | 8 | 16 | 4 | 256M | 16M | 8 | 1.5 GiB | 8G (default) |
| 100 Gb/s | 32 | 64 | 8 | 128M | 32M | 16 | 20 GiB | 32G |
| 200 Gb/s | 64 | 128 | 16 | 64M | 64M | 16 | 80 GiB | 96G |

Beyond about 20 Gb/s a single rclone process is usually limited by CPU (TLS, MD5 of
every part for S3) and by the storage on both sides: run several jobs in parallel, e.g.
one per top-level folder, and make sure the source filesystem (e.g. CephFS) can read
at that rate with that many parallel streams. The presets below are fitted to the budget
per destination, so raise the budget together with the caps.

**Presets** (New Copy Job dialog; values are clamped to the caps and reduced until the
estimate fits the budget, then marked "reduced"):

| Preset | transfers | checkers | streams | cutoff | S3/Azure chunk × concurrency |
|---|---|---|---|---|---|
| Default | server defaults | | | | |
| Many small files | 32 | 64 | | | |
| Few large files | 4 | | 16 | 64M | 64M × 16 |
| Maximum | 64 | 128 | 16 | 64M | 64M × 16 |

With the default budget of 8 GiB, "Maximum" becomes 10 transfers with 32M chunks for S3
and Azure destinations and 32 transfers for local ones.

**Memory estimate.** Each transfer buffers what it reads, per stream, and each upload to
S3 or Azure holds its chunks in memory while they are sent. Motuz estimates the memory
of one job as

    transfers × ( max(1, streams) × buffer_size
                + max(upload_concurrency, streams) × chunk_size )

where the second term only counts for S3 and Azure Blob destinations (with that
backend's chunk size and upload concurrency; rclone uses the larger of the upload
concurrency and the streams for multi-thread uploads), and unset values count with
rclone's defaults. Example: 32 transfers × (8 × 16 MiB + 16 × 32 MiB) = 20 GiB. Jobs
whose estimate is above `MOTUZ_RCLONE_MEMORY_BUDGET` are refused (the dialog shows the
estimate as you type). It is a rough upper bound: it is reached only when every
transfer is a large file at the same time. For a hard limit inside rclone, add
`--max-buffer-memory` (below).

**Extra flags.** `MOTUZ_RCLONE_EXTRA_FLAGS` (admin only, e.g.
`--max-buffer-memory=64G --use-mmap`) accepts only these flags, each with a validated
value: `--fast-list`, `--use-mmap`, `--no-traverse`, `--disable-http2`,
`--s3-disable-http2`, `--max-buffer-memory=SIZE`, `--multi-thread-chunk-size=SIZE`,
`--multi-thread-write-buffer-size=SIZE`, `--s3-upload-cutoff=SIZE`,
`--s3-copy-cutoff=SIZE`, `--low-level-retries=N`, `--retries=N`. There is deliberately
no free-form option: rclone runs as the user, and other flags could start a remote
control server (`--rc`), run programs (`--password-command`, `--metadata-mapper`),
write files or credentials anywhere (`--log-file`, `--dump`), or weaken what is copied
and checked (`--ignore-checksum`, `--size-only`).

Users can never pass flags: per-job settings are a fixed set of names whose values are
parsed as numbers or sizes and formatted by Motuz, one argv item per flag
(`--transfers=32`), without a shell. The flags appear in the rclone command in the
celery log (credentials stay masked).

### File viewer

A double-click on a file in a pane opens a read-only viewer; the file is read as the
logged-in user (locally with their permissions, in the cloud with their connection):

- `.png`, `.jpg`/`.jpeg`, `.gif`, `.webp`, `.heic`/`.heif`, `.avif`: the image, fit to
  the window (a click shows 100%), with Previous/Next buttons (also `←`/`→`, PageUp/
  PageDown) to step through the other images in the same folder, in the order shown,
  without wrapping at the ends. The server decides the type from the file's first
  bytes, not its name (an ISO-BMFF `ftyp` box for HEIC/HEIF/AVIF); SVG files open as
  text instead (an SVG can contain scripts). Images above `MOTUZ_VIEW_IMAGE_MAX_BYTES`
  (default `25M`, at most `256M`, set in `.env`) are refused before they are read.
  AVIF (the AV1 codec) decodes in every current major browser. HEIC/HEIF (what iPhones
  take, HEVC coded) is shown by Safari itself; for Chrome, Firefox and Edge the server
  converts it to a JPEG (quality 85, the primary image only, upright, without EXIF/GPS/XMP;
  at most 100 megapixels, the long side capped at 8192 px) and the viewer says "HEIC,
  converted to JPEG". The converter is C code reading an untrusted file, so it runs in a
  separate process **as the logged-in user** (the same `sudo` rule as every file
  operation; never as root and never as the service account) with limits on memory
  (2 GiB), CPU time, open files and no files written (it decodes nothing if one of them
  cannot be set), and a 60 s time limit; at most two conversions run at once
  (`MOTUZ_HEIC_MAX_CONCURRENT`, further requests wait 5 s and are then answered "busy, try
  again"). It uses
  [pi-heif](https://github.com/bigcat88/pillow_heif) (the decode-only build of
  pillow-heif) and Pillow, pinned in `requirements.in` and installed with hash checking from `requirements-heic.txt`. Their wheels bundle **libheif and
  libde265, licensed LGPL-3.0**, as separate shared libraries that Python loads at run
  time (`pi_heif.libs/` next to the package: replaceable, nothing is linked into Motuz's
  MIT-licensed code); no x265 or other GPL encoder is included. HEVC is covered by patent
  pools (MPEG-LA, Access Advance, Velos Media): decoding happens on your server, so if
  that matters to your organization decide before you leave this on; delete
  `/opt/motuz-heic` (systemd) or build the image without the `--target` install and
  HEIC/HEIF files then answer "not installed" instead. The docker image installs the
  packages into `/opt/motuz-heic/site`; `install.sh` of the systemd install does the same
  for the distribution's `python3` (`MOTUZ_HEIC_PYTHON`, `MOTUZ_HEIC_SITE`, set by
  `deploy.sh`). A HEIC file with several images (bursts, live photos) shows its primary
  image only. Markdown files show relative `.heic` pictures the same way (converted).
- `.md`, `.markdown`: rendered in the browser from the first 1 MiB, without raw HTML
  (shown as text). Links open in a new tab and only for http, https and mailto;
  remote images are never loaded, images next to the file (relative paths) are.
  *Source* shows the text.
- `.pdf`; `.docx`/`.dotx`/`.docm`/`.dotm`; `.xlsx`/`.xlsm`/`.xltx`/`.xltm`/`.xlsb`/`.xls`/
  `.ods`; `.pptx`/`.pptm`/`.ppsx`/`.ppsm`/`.potx`/`.potm`: a read-only preview, rendered
  entirely in the browser (pdf.js, docx-preview, SheetJS, a Motuz-written PPTX outline
  reader) — there is no LibreOffice or other server-side conversion, and files are never
  sent to an external viewer service. PDFs open a page at a time with find, page
  navigation, zoom and safe (http/https/mailto only) links; PDF JavaScript never runs.
  Word documents keep headings, text, tables and pictures; unsafe links (`javascript:`,
  etc.) are shown struck through instead of working, and remote pictures are never
  fetched. Spreadsheets show sheet tabs and formatted values only — formulas keep their
  last calculated value and are never recalculated, and a sheet is capped at 5,000 rows
  by 200 columns with a notice when cut off. PowerPoint files show an outline (titles,
  text, tables, pictures, speaker notes) rather than rendered slides. Files above
  `MOTUZ_VIEW_DOCUMENT_MAX_BYTES` (default `50M`) are refused, except PDFs, which are
  read in small ranges regardless of size. Old binary Office files (`.doc`, `.ppt`, ...)
  and other formats without a browser renderer show "No preview for this file type".
- anything else: a text pager like `less`, with *Follow* (like `tail -f`).

### Uploads from the desktop

Drag files or folders from the desktop (or a file manager) onto a pane: they are uploaded
into the folder the pane shows, or into the folder row you drop them on, also on cloud
connections. The upload is a copy job in the job table, with the progress bar of any
copy (the source shows as `desktop://`); its details show the files done, skipped and
failed, with rclone's message for each failed file.

- If some of the dropped names already exist there, one question per drop offers
  **Overwrite all**, **Skip existing** (only files that do not exist yet are uploaded, also
  inside existing folders) or **Cancel**.
- The browser sends each file in chunks of 8 MiB, three files at a time. Each chunk goes
  to an `rclone rcat` that runs **as the logged-in user** (the same `sudo` rule as every
  file operation) and writes the file at its destination; nothing is stored on the
  server. A chunk that is lost on the way is sent again; a file that cannot be continued
  starts again from its beginning.
- The data comes from the tab: the page asks before it closes while an upload runs, and a
  job whose browser sends nothing for 5 minutes fails ("the browser stopped sending the
  upload"). Stop works like for any job; the file that was being written is removed.
- Empty folders are not uploaded, at most 100,000 files per drop. Uploads always run on
  the central node, never on remote workers, and have no Retry or Check Integrity (the
  source was in the browser).
- When a local destination is full (or the user's disk quota is exceeded), the whole
  upload fails at once with that reason instead of failing file by file.

### Remote workers (HTTPS only)

Jobs can run on other machines than the Motuz server: **remote workers** run
`motuz-worker` (`src/worker/motuz_worker.py`), which needs nothing but outbound HTTPS to
the Motuz server (port 443, optionally through an HTTP proxy). Workers have no database
access, open no ports and need no VPN. Typical setups:

- Motuz in the cloud (e.g. a small EC2 instance), workers on-prem next to the file systems
  (Proxmox VMs with the same mounts, users and versions as each other).
- Temporary EC2 workers, one per large cloud-to-cloud job (see "Temporary EC2 workers").

A worker runs the same job code as the Motuz server's Celery worker: rclone as the job's
owner (`sudo -n --preserve-env=<names> -u <owner>`), the same output parsing, Stop, progress and final state in
the UI. Local paths are the worker's own mounts.

A copy job whose local destination is full (no space left on device, or the user's disk
quota is exceeded) stops at once, on the Motuz server and on workers alike: rclone is
stopped instead of failing and retrying every remaining file, and the job's error text
starts with the reason, the destination and how many files were copied before.

**Which jobs go where** (`.env` of the Motuz server, passed to the `app` container; `motuz.env` without Docker):

| Variable | Default | Meaning |
|---|---|---|
| `MOTUZ_LOCAL_JOB_POOL` | `central` | Pool of jobs with a local path: `central` runs them on the Motuz server (Celery), e.g. `onprem` queues them for on-prem workers |
| `MOTUZ_LARGE_JOB_POOL` | `central` | Pool of large cloud-to-cloud jobs, e.g. `aws`; other cloud-to-cloud jobs run centrally |
| `MOTUZ_LARGE_JOB_BYTES`, `MOTUZ_LARGE_JOB_FILES` | 300 GB, 50000 | "Large": the source has at least this many bytes or files (`rclone size`, at most `MOTUZ_JOB_SIZE_TIMEOUT` = 60 s; slower counts as large) |
| `MOTUZ_WORKER_LEASE_SECONDS` | 120 | A claimed job fails when its worker has not reported for this long |
| `MOTUZ_PUBLIC_URL` | the address the worker used | `https://` address of the Motuz server for the token broker URL in job tickets |

With the defaults nothing changes: every job runs on the Motuz server. A job queued for a
pool without workers waits ("Waiting for a worker of pool ..." in its details).

**Security model**

- Each worker has a credential: `manage.py workers add <name> --pool onprem` prints a
  secret once; the server stores only its SHA-256. The worker exchanges it at
  `POST /api/workers/auth` for an access token (10 minutes) that is signed with a key of
  its own and has the audience `motuz-worker`: user endpoints never accept it, and worker
  endpoints never accept user tokens. Secrets are compared in constant time.
- A worker claims jobs of its own pool only (`POST /api/workers/claim`, long poll). A claim
  hands out one job atomically (`SELECT ... FOR UPDATE SKIP LOCKED` plus a compare-and-set)
  with a **job ticket**: the job's parameters (type, owner, paths, options), the rclone
  configuration of that job's own connections (the same `_formatCredentials` as the
  server; connections using credentials from the home directory are read on the worker,
  as the owner), the job's rclone performance flags (from the server's `MOTUZ_RCLONE_*`
  settings and the job's overrides, see "Performance tuning"; workers need no such
  settings), an expiry and a ticket token. Nothing about other
  jobs, connections or users. A ticket is returned once and never again.
- Progress (`POST /api/workers/jobs/<ticket>/progress`, every few seconds) and the result
  (`.../finish`) need the worker's access token and the ticket token (`X-Motuz-Ticket`) of
  a job that worker claimed. Progress renews the **lease**; its answer is `continue` or
  `stop` (the user pressed Stop: the worker kills rclone). A job whose lease expires (the
  worker died or lost its connection) is marked FAILED with the reason; it is not
  requeued, because the worker might still be running rclone. A worker that reports after
  that gets `410 Gone` and stops rclone.
- OneDrive and Google Drive: rclone on the worker gets a **job-scoped broker token**
  instead of the connection's broker handle, and the token URL
  `https://<Motuz server>/api/workers/oauth/token`. That endpoint refreshes only the
  connection on that side of that running job, with the same broker code as the loopback
  broker (locking, caching, rotation, own-app credentials); the real refresh token never
  leaves the server. `/internal` stays unreachable from outside.
- Tickets and broker tokens die when the job ends, its lease expires or its worker is
  revoked (`manage.py workers revoke <name>`, which also fails the worker's running jobs).
- Sign-in, claims and the broker are rate limited; sign-ins, claims, ticket and broker use
  are logged (logger `motuz.audit`) without secrets.

**On the Motuz server** (without Docker: `bin/systemd/config.sh set MOTUZ_LOCAL_JOB_POOL onprem`,
`deploy.sh`, and `motuz_manage workers add|list|revoke|bootstrap ...` with the function from
[Configuration and features](#configuration-and-features))

```bash
# .env: send jobs with local paths to the on-prem workers, then ./bin/prod/start.sh
MOTUZ_LOCAL_JOB_POOL=onprem

# One credential per worker; the secret is printed once
docker exec motuz_app bash -c 'source ./load-secrets.sh >/dev/null && python3 manage.py workers add proxmox-1 --pool onprem'
docker exec motuz_app bash -c 'source ./load-secrets.sh >/dev/null && python3 manage.py workers list'
docker exec motuz_app bash -c 'source ./load-secrets.sh >/dev/null && python3 manage.py workers revoke proxmox-1'
```

**Installing a worker** (Ubuntu or Amazon Linux; the same Motuz release and rclone version as the
server, the same users (SSSD/LDAP) and mounts as the other workers). The short way is
`install.sh --worker-only` from a readable clone such as `/opt/motuz` (see [Remote workers on
AL2027](#remote-workers-on-al2027) for the options and a temporary worker):

```bash
sudo git clone https://github.com/dirkpetersen/motuz /opt/motuz        # check out the server's release
sudo /opt/motuz/bin/systemd/install.sh --worker-only --central-url=https://motuz.example.org --pool=onprem --credential-file=/root/credential
```

The same by hand (Ubuntu):

```bash
sudo apt-get install -y python3 sudo unzip curl
sudo git clone https://github.com/dirkpetersen/motuz /opt/motuz   # check out the server's release

# An unprivileged account that may run rclone, ls, mkdir and env as any user except root
# (rclone as the job's owner, like the server's containers do); SETENV: rclone gets its
# configuration, proxy and CA bundle as variables (sudo --preserve-env=<names>).
# On Ubuntu 26.04, `sudo bin/systemd/install.sh --worker-only` does this and the rclone
# download (README, "Install (default: without Docker)").
sudo useradd --system --create-home --home-dir /var/lib/motuz --shell /usr/sbin/nologin motuz
# rclone: the version pinned in deployment/docker/app/Dockerfile, downloaded by the account
sudo install -d -m 755 -o motuz -g motuz /var/lib/motuz-runas
sudo -u motuz /opt/motuz/bin/systemd/tools.sh --rclone-only     # again as motuz after updates
echo 'motuz ALL=(ALL, !root, !postgres, !redis, !valkey) NOPASSWD:SETENV: /var/lib/motuz-runas/rclone, /usr/bin/ls, /usr/bin/mkdir, /usr/bin/env' \
    | sudo tee /etc/sudoers.d/motuz-worker
sudo chmod 440 /etc/sudoers.d/motuz-worker
sudo visudo -c
sudo loginctl enable-linger motuz          # user services without a login

# Configuration and the secret from `manage.py workers add` (mode 600)
sudo -u motuz install -d -m 700 /var/lib/motuz/.config/motuz-worker /var/lib/motuz/.config/systemd/user
sudo -u motuz sh -c 'umask 077; cat > ~/.config/motuz-worker/credential'   # paste, Ctrl-D
sudo -u motuz install -m 600 /opt/motuz/src/worker/worker.env.example /var/lib/motuz/.config/motuz-worker/worker.env
sudo -u motuz editor /var/lib/motuz/.config/motuz-worker/worker.env   # MOTUZ_CENTRAL_URL, proxy, mounts
sudo -u motuz install -m 644 /opt/motuz/src/worker/motuz-worker.service /var/lib/motuz/.config/systemd/user/

# Check (mounts, sign-in, version), then start
sudo -u motuz bash -c 'set -a; . ~/.config/motuz-worker/worker.env; python3 $MOTUZ_HOME/src/worker/motuz_worker.py --check'
sudo -u motuz XDG_RUNTIME_DIR=/run/user/$(id -u motuz) systemctl --user enable --now motuz-worker
sudo -u motuz XDG_RUNTIME_DIR=/run/user/$(id -u motuz) journalctl --user -u motuz-worker -f
```

Worker settings (`worker.env`, see `src/worker/worker.env.example`): `MOTUZ_CENTRAL_URL`
(https only), `MOTUZ_WORKER_CREDENTIAL_FILE` (must be mode 600), `MOTUZ_WORKER_POOL`
(optional check), `MOTUZ_REQUIRED_PATHS` (mount points that must be mounted before the
worker claims jobs), `HTTPS_PROXY`/`NO_PROXY` (used by the worker and passed to rclone)
and `MOTUZ_CA_BUNDLE` (a PEM bundle for the server's certificate, also given to rclone as
`SSL_CERT_FILE`; it replaces the system roots, so it must contain them, and it must be
readable by every user, e.g. `/etc/motuz-worker/ca.pem`, because rclone runs as the job's
owner). Before claiming,
the worker checks its mounts and that its release (`src/backend/api/version.py`: `VERSION`,
`WORKER_PROTOCOL`) equals the server's; otherwise it logs why and waits. On SIGTERM it
stops rclone and reports the job as failed. Exit code 78 (configuration error, revoked
credential) stops systemd from restarting it.

**Ephemeral workers** (what the EC2 launcher uses): a single-use bootstrap token, valid
for minutes and optionally bound to one job queued for the same pool, replaces the
credential:

```bash
python3 manage.py workers bootstrap --pool aws --job copy:123 --ttl 15m   # prints mzb1....
# on the new instance (token e.g. from its user data; a file keeps it out of `ps`):
python3 /opt/motuz/src/worker/motuz_worker.py --bootstrap-token-file /run/motuz-bootstrap --once
```

The worker exchanges the token (it cannot be used again), claims exactly its job, runs it,
reports the result and exits; the server then revokes the ephemeral worker. Code can call
`worker_manager.create_bootstrap_token(pool, job='copy:123', ttl_seconds=900)`.

### Temporary EC2 workers

When the Motuz server runs in AWS, each large cloud-to-cloud job can get its own
short-lived EC2 instance: a remote worker of the pool `aws` that boots, runs exactly this
job and terminates. The Motuz server itself stays small; the copy runs on a
network-optimized instance sized for the job. Off by default (`MOTUZ_EC2_WORKERS=false`).

**How it works**

1. A new copy or integrity-check job whose source is large (`MOTUZ_LARGE_JOB_BYTES` /
   `_FILES`, see "Remote workers") is queued for the pool `MOTUZ_LARGE_JOB_POOL=aws`,
   with the source size that `rclone size` measured.
2. Right after (a Celery task, so creating the job does not wait for AWS), the launcher
   (`src/backend/api/managers/ec2_launcher.py`) starts an instance if fewer than
   `MOTUZ_EC2_MAX_WORKERS` are running; otherwise the job waits in the queue ("waiting for
   a free EC2 worker slot") until one ends. It creates a single-use bootstrap token bound
   to the job (valid for `MOTUZ_EC2_BOOT_TIMEOUT`), puts it into the instance's user data,
   and calls `RunInstances` with the launch template of the instance type's architecture
   (`deployment/aws/README.md`), the type from `MOTUZ_EC2_INSTANCE_TYPES`, the worker tags
   and a `ClientToken` derived from the job, so that a repeated call never starts a
   second instance for the same job.
3. The instance (Amazon Linux 2027) restricts instance metadata to root, installs rclone
   (the version pinned for the server, checksum verified) and Motuz at the server's commit,
   and runs `motuz_worker.py --bootstrap-token-file ... --once`. The worker exchanges the
   token, claims its job, reports progress over HTTPS like any remote worker, and exits;
   then the instance shuts down, which terminates it.
4. A reaper on the Motuz server (`manage.py ec2 reap --loop`, started by the Celery
   container every `MOTUZ_EC2_REAP_INTERVAL`) cleans up and starts queued jobs whose
   turn has come.

The job table shows where a job runs, e.g. "aws · starting worker (c7gn.large)",
"aws · running on c7gn.2xlarge"; the job dialog shows it as "Runs on".

**Which jobs.** Only cloud-to-cloud jobs whose two connections are HTTPS APIs with
credentials stored in Motuz: S3 (AWS or an `https://` endpoint on port 443), Azure Blob,
Google Cloud Storage, Dropbox, OneDrive, Google Drive. The workers reach nothing but port
443 and have none of the users' files, so large jobs with SFTP, WebDAV, Swift, an S3
endpoint on another port, or "Credentials from your home directory" run on the Motuz
server instead. Jobs with a local path never go to EC2 workers: the server refuses to
start with `MOTUZ_LOCAL_JOB_POOL` set to the EC2 pool.

**Settings** (`.env` of the Motuz server; passed to `app` and `celery`):

| Variable | Default | Meaning |
|---|---|---|
| `MOTUZ_EC2_WORKERS` | `false` | `true` launches workers for the jobs of the EC2 pool |
| `MOTUZ_EC2_POOL` | `aws` | The pool; must equal `MOTUZ_LARGE_JOB_POOL` |
| `MOTUZ_AWS_REGION` | (required) | Region of the launch templates, e.g. `us-west-2` |
| `MOTUZ_PUBLIC_URL` | (required) | `https://` address of this server, reachable from the internet on 443 |
| `MOTUZ_EC2_INSTANCE_TYPES` | `1T:c7gn.large,10T:c7gn.2xlarge,*:c7gn.4xlarge` | Instance type by source size (decimal: `1T` = 10^12 bytes): below 1 TB `c7gn.large`, below 10 TB `c7gn.2xlarge`, else (and when the listing timed out) `c7gn.4xlarge`. Graviton types (`c7gn`, `c8gn`) use the arm64 template, others (`c6in`, `c7i`) the x86_64 one; only types the IAM policy allows can start |
| `MOTUZ_EC2_MAX_WORKERS` | 2 | Instances at the same time (worker instances without a record count too) |
| `MOTUZ_EC2_MAX_RUNTIME` | `24h` | An instance running longer is terminated and its job failed with that reason |
| `MOTUZ_EC2_BOOT_TIMEOUT` | `20m` | Lifetime of the bootstrap token; an instance that has not started its job by then is terminated and the job failed |
| `MOTUZ_EC2_LAUNCH_TEMPLATE_ARM64`, `_AMD64` | `motuz-worker-arm64`, `motuz-worker` | Launch template names or ids (`lt-...`) |
| `MOTUZ_EC2_LAUNCH_TEMPLATE_VERSION` | `$Default` | Or `$Latest` or a number (`1` = the Ubuntu 26.04 fallback, also tested end to end 2026-09-28: real copy, hashsum and Google Drive -> S3 broker jobs, no code changes needed) |
| `MOTUZ_EC2_SUBNET_ID` | (none) | Subnet; unset: EC2 picks a default subnet |
| `MOTUZ_EC2_REAP_INTERVAL` | `60s` | Reaper period |
| `MOTUZ_EC2_HALT_DELAY` | `0` | For debugging: minutes a worker waits before shutting down after a failure |
| `MOTUZ_WORKER_SOURCE` | `https://github.com/dirkpetersen/motuz` | Where workers get Motuz (a GitHub repository is fetched as an archive, anything else with git) |
| `MOTUZ_WORKER_SOURCE_REF` | the image's commit | Commit, tag or branch; `bin/prod/build.sh` records the commit it builds (`MOTUZ_SOURCE_COMMIT`), which must be pushed to `MOTUZ_WORKER_SOURCE` |

An invalid or inconsistent value stops the app and the Celery worker at startup with a
message naming the variable. AWS credentials come from the server's instance profile
(IMDSv2), never from a file.

**Limits and cleanup.** IAM cannot limit the number of instances, and a stop could be
requested instead of a termination, so Motuz enforces both itself:

- `MOTUZ_EC2_MAX_WORKERS` concurrent instances; further jobs wait queued.
- On the instance: `shutdown -h +<max runtime + 15 min>` right after boot, and a shutdown
  when `motuz-worker --once` exits, successful or not. The worker gives up when it has no
  job after `MOTUZ_EC2_BOOT_TIMEOUT` (also while the server is unreachable). The launch
  templates set `InstanceInitiatedShutdownBehavior=terminate`; the launcher never passes
  that parameter (nor any other that would override the template: image, security group,
  instance profile, key pair, metadata options).
- The reaper terminates instances whose job ended (finished, failed or stopped), that
  run longer than `MOTUZ_EC2_MAX_RUNTIME`, that did not start their job within
  `MOTUZ_EC2_BOOT_TIMEOUT`, that are stopped, and instances with the worker tags
  (`Project=motuz`, `Component=worker`, `ManagedBy=motuz-central`) that have no record in
  the `ec2_worker` table and are older than 10 minutes. So one account and region must
  have at most one Motuz server with EC2 workers. It fails the job of an instance that
  ended before or while running it, with the reason and the command for its console
  output (`aws ec2 get-console-output --latest --instance-id ...`; EC2 keeps it only while
  the instance exists, see `MOTUZ_EC2_HALT_DELAY`).
- No capacity for a type (`InsufficientInstanceCapacity`, when launching or as the
  reason an instance ended before claiming its job): the next type of the table is tried
  once (the last type falls back to the one before it); then the job fails.

**On the instance.** Before anything else a firewall rule (nftables, or iptables with the
owner match; the Amazon Linux 2027 preview has neither, so nftables is installed with dnf
first; Ubuntu 26.04's AMI ships `iptables`/`nft` already, so this dnf fallback is AL2027
only) lets only root reach the instance metadata service (169.254.169.254), which
serves the user data with the bootstrap token and the (permissionless) instance role; the
script checks it as an unprivileged account. `motuz-worker` runs as the account `motuz`,
rclone as `motuzjob` (`MOTUZ_WORKER_RUN_AS`; `motuz` may run only `/usr/local/bin/rclone`
as `motuzjob` through sudo): the job's owner does not exist on the instance, and the
instance runs one job of one user. The bootstrap token is the only secret in the user
data (single use, bound to the job, valid for minutes) and is never printed; the job's
credentials arrive in the job ticket over HTTPS, as for any remote worker. The user data
(`src/backend/api/templates/ec2/worker-user-data.sh`, about 10 KB of the 16 KB limit)
skips the downloads when rclone and Motuz at the right version are already installed, the
hook for a prebuilt AMI or the Amazon Linux 2027 installer's worker-only mode. Booting
to a running worker takes about 20 seconds.

**Commands** (in the `motuz_app` or `motuz_celery` container; without Docker `motuz_manage ec2 check|status|reap`.
The reaper loop (`manage.py ec2 reap --loop`) is started only by the Docker `celery` container's
entrypoint: the systemd units do not start it, so on the install without Docker run it yourself
(for example as a user service) before enabling `MOTUZ_EC2_WORKERS`):

```bash
docker exec motuz_celery bash -c 'source ./load-secrets.sh >/dev/null && python3 manage.py ec2 check'   # IAM: RunInstances dry run per type
docker exec motuz_celery bash -c 'source ./load-secrets.sh >/dev/null && python3 manage.py ec2 status'  # launches and live worker instances
docker exec motuz_celery bash -c 'source ./load-secrets.sh >/dev/null && python3 manage.py ec2 reap'    # one reaper pass now
```

To debug a worker, set `MOTUZ_EC2_HALT_DELAY=30m`: a worker whose setup or job failed
then stays up that long, and `aws ec2 get-console-output --latest --instance-id <id>` shows
the boot steps (`motuz-boot: ...`) and the worker's log. The `motuz-worker-ssm-debug`
policy (`deployment/aws/README.md`) would allow a Session Manager shell, but the Amazon
Linux 2027 preview instances did not register with Systems Manager in a test; detach the
policy again afterwards in any case.

**Costs.** Only while instances run: e.g. `c7gn.large` about 0.12 USD/h, `c7gn.4xlarge`
about 1 USD/h in us-west-2, plus 16 GiB gp3 and a public IPv4 address. With the defaults
at most two workers of at most 24 hours each run at a time. Keep an AWS budget alert.

## Alternative: Docker

The same Motuz in containers with `docker-compose` (Traefik, the app, Celery, RabbitMQ and
PostgreSQL, all with host networking), for machines where you do not want a service
account with systemd user services, or to try Motuz quickly. Everything in the
[configuration and features](#configuration-and-features) below applies to both installs; the
settings are the same `MOTUZ_*` variables (`.env` and `/docker/secrets` here, `motuz.env` and
`secrets.env` there). To move to the default install, or to see what differs, read
[Migrating from the docker install](#migrating-from-the-docker-install); `make quickstart`,
`bin/quickstart.sh` and `bin/redeploy.sh` keep working. The default install, which needs
no Docker, is the [Quick start](#quick-start) and [Install (default: without Docker)](#install-default-without-docker)
(`sudo bin/systemd/bootstrap.sh` does all of it on a fresh machine).

### Docker quickstart

- Install [`docker` and `docker-compose`](https://docs.docker.com/install/linux/docker-ce/ubuntu/)
- Run the following command

```bash
git clone https://github.com/dirkpetersen/motuz.git
cd motuz
./bin/quickstart.sh
```

- Open browser at http://localhost/ and accept self-signed certificates

---

![Motuz main page ](docs/img/image_root.png)

---

### Beyond quickstart

In this section we will explain what each step of quickstart does. We will also see how to customize some of these steps.

1. Create a folder called `docker` in your root directory using `sudo install -d -o $USER -m 755 /docker`. Inside the folder, create the following subfolders

- `mkdir -p /docker/certs`
- `mkdir -p /docker/secrets`
- `mkdir -p /docker/volumes/postgres`
- `mkdir -p -m 700 /docker/traefik` (only used for [Let's Encrypt](#option-b-lets-encrypt))

2. Add SSL certificates inside `/docker/certs` (with names `cert.crt` and `cert.key`). If you don't have SSL certificates, you can temporarily use [self-signed certificates](https://stackoverflow.com/questions/10175812/how-to-create-a-self-signed-certificate-with-openssl#10176685), or let Traefik get one from [Let's Encrypt](#option-b-lets-encrypt).

3. Create the following secret files and remember the passwords

```bash
mkdir -p /docker/secrets
head /dev/urandom | md5sum | awk '{print $1}' > /docker/secrets/MOTUZ_DATABASE_PASSWORD
head /dev/urandom | md5sum | awk '{print $1}' > /docker/secrets/MOTUZ_FLASK_SECRET_KEY
head /dev/urandom | md5sum | awk '{print $1}' > /docker/secrets/MOTUZ_SMTP_PASSWORD
# Optional, empty unless you use an own OneDrive app registration
install -m 600 /dev/null /docker/secrets/MOTUZ_ONEDRIVE_CLIENT_SECRET
# Optional, empty unless you use an own Google OAuth client
install -m 600 /dev/null /docker/secrets/MOTUZ_GDRIVE_CLIENT_SECRET
```

4. Initialize the database

```bash
./bin/_utils/database_install.sh
```

5. Pull and Start all containers

```bash
docker-compose up -d
```

6. See result at http://localhost/.

### Customizing your deployment

- Add your SSL certificates - `/docker/certs/`, or set `MOTUZ_ACME_DOMAIN` for [Let's Encrypt](#option-b-lets-encrypt)
- Change the environment variables - `.env`
- Change passwords by editing the files - `/docker/secrets`
- Change the files that are visible to motuz - `docker-compose.override.yml`
- Use a different root (instead of `/docker`) by changing the MOTUZ_DOCKER_ROOT variable in `.env`

### Setting up production

The following instructions could be used on
an on-premises system or an EC2 instance.
For on-premises installations, we have found
that bare metal hardware has faster networking
than virtual machines.


We recommend using a machine running Ubuntu 24.04 (Noble).

The following sections are the Docker install; the default install without Docker is
[Install (default: without Docker)](#install-default-without-docker).


### Authentication

There are two options for authentication:

#### Local Authentication

Motuz will let you log in as any (non-root) user who is in
`/etc/passwd` on the local system.

To use this option, you should make sure the
users you want to use exist in `/etc/passwd`.

If your system does not
have an appropriate user, create one
with `useradd` and set its password
with `passwd`.

#### Authenticate against Active Directory

You can authenticate against Active Directory.
To do this we use PAM and Kerberos (or SSSD on
a CentOS system).

**NOTE**: If you want to use this option, and also
be able to log in as users in `/etc/passwd`,
you must create those users and set their passwords *before* installing Kerberos and PAM.

To authenticate against Active Directory, install the required packages
as follows:

```bash
sudo apt-get update -y
sudo apt-get install -y krb5-user libpam-krb5
```

### Requirements

Git and rsyslog are required regardless of authentication method.
Install them as follows:

```bash
sudo apt-get update -y
sudo apt-get install -y git rsyslog
```

### Docker and docker-compose

Docker is also required. Install it according to
[these instructions](https://docs.docker.com/install/linux/docker-ce/ubuntu/). Do NOT simply run `apt-get install` to install Docker. You must follow these
instructions to install Docker from Docker's own
repositories.

Then, install the latest release of `docker-compose` from
the [releases page](https://github.com/docker/compose/releases) on GitHub. Instructions are provided on that page.

### Shared Filesystems (optional)

If you want to expose shared filesystems to Motuz
users, you'll need to install NFS and mount all the
filesystems that you want to expose. For production
machines these mounts should be added to `/etc/fstab`.

You will need to add the directories you want to expose
to the `docker-compose.yml` file, under the `app`
and `celery` services.

### Cloning the Motuz repository

Clone the repository as follows:

```bash
git clone https://github.com/dirkpetersen/motuz.git
cd motuz
```

### Set up HTTPS certificate

Traefik (container `motuz_traefik`) is the only service reachable from other
hosts. It terminates TLS on port 443 (TLS 1.2 and 1.3), redirects port 80 to
HTTPS, adds the security headers and forwards everything except `/internal/*`
to uWSGI on `127.0.0.1:5000`, which serves both the API and the web UI. Its
configuration is the `traefik` service in `docker-compose.yml` (static
settings) and `deployment/docker/traefik/dynamic/motuz.yml` (routing, headers,
TLS). The request log goes to `docker logs motuz_traefik`.

Traefik gets its certificate in one of two ways.

#### Option A: certificate files (default)

Obtain an SSL certificate for your domain.
This will consist of a `.key` file and
a `.crt` file.

These files must be placed in the directory `/docker/certs` (`$MOTUZ_DOCKER_ROOT/certs`). Create that
directory if it doesn't already exist.

Copy the certificate (.crt) file to
`/docker/certs/cert.crt`. If there are intermediate certificates, append them
to the same file after your certificate (full chain).

Copy the key (.key) file to
`/docker/certs/cert.key`.

The `.key` file should have permission 0400.

If you do not have SSL certificates, you
can temporarily (but not in a production context!)
use [self-signed certificates](https://stackoverflow.com/questions/10175812/how-to-create-a-self-signed-certificate-with-openssl#10176685).
`bin/quickstart.sh` creates them for you.

Traefik reads the files when it starts. After replacing them, run
`docker restart motuz_traefik`.

#### Option B: Let's Encrypt

Traefik can get and renew a free certificate from Let's Encrypt by itself
(ACME HTTP-01 challenge), so no certbot or cron job is needed. Requirements:

- a public DNS name that points to this host
- ports 80 and 443 reachable from the internet (Let's Encrypt connects to port 80)

Set the name in `.env`, or export it in the environment that runs
`docker-compose` / `./start.sh` (the environment takes precedence over `.env`):

```bash
MOTUZ_ACME_DOMAIN=motuz.example.org
MOTUZ_ACME_EMAIL=admin@example.org   # optional, contact address of the ACME account
```

Then create the directory for Traefik's certificate store and (re)start Motuz:

```bash
sudo install -d -m 700 /docker/traefik   # $MOTUZ_DOCKER_ROOT/traefik
./start.sh                               # or: docker-compose up -d traefik
```

Traefik requests the certificate on start and renews it automatically about 30
days before it expires. The account and the certificates, including their
private keys, are stored in `/docker/traefik/acme.json` (mode 600); keep it
private and include it in backups. `/docker/certs` is not used in this mode.
Clients that connect by IP address also get the Let's Encrypt certificate.

To try this without hitting Let's Encrypt's rate limits, first use the staging
CA, whose certificates browsers do not trust:

```bash
MOTUZ_ACME_CA_SERVER=https://acme-staging-v02.api.letsencrypt.org/directory
```

When that works, remove `MOTUZ_ACME_CA_SERVER` and `/docker/traefik/acme.json`
and restart.

To go back to certificate files, unset `MOTUZ_ACME_DOMAIN` and restart.

### Running Motuz the first time

First, initialize the database as described in step 4 of
[Beyond quickstart](#beyond-quickstart) (`./bin/_utils/database_install.sh`).
(At some point, we expect this step to go away.)


If you haven't already, change to
the directory where you cloned the
Motuz repository.

Then run the `start.sh` script. This script
is *only* meant to be run the first time
Motuz is started on a machine.

```bash
./start.sh
```

This will take a few moments.

After the stack comes up, assuming your machine
is set up in DNS as hostname.example.com, you should be able to access Motuz at the following URL:

`https://hostname.example.com/`

If you used a self-signed SSL certificate, your
browser will warn you that proceeding is not secure.

### Redeploying

We typically deploy on the `prod` branch. If everything in `master` is ok to 
be synced with `prod`, we do not merge master into prod, but rather sync it up as follows:

```
git pull
git checkout prod
git reset --hard master # or the commit that you prefer
git push -f
```

If you want to stop and restart motuz, for example
to deploy new code, use the `bin/redeploy.sh` script.

For example, if you want to deploy recent changes
to the Motuz code base, sync the code as above.


Then run the redeployment script:

```bash
bin/redeploy.sh
```

This will stop Motuz, rebuild Docker images, run
database migrations if necessary, and bring Motuz
back up. It will result in short (-2min) downtime.

### Migrating from nginx to Traefik

Installations from before Traefik ran nginx in a container named `motuz_nginx`.
To migrate:

1. Update the code and run `bin/redeploy.sh` as above. `start.sh` runs
   `docker-compose down --remove-orphans`, which also removes the old
   `motuz_nginx` container. If you start the containers some other way, run
   `docker rm -f motuz_nginx` first, otherwise it keeps ports 80 and 443 and
   Traefik cannot start. The `fredhutch/motuz_nginx` image is no longer used.
2. Certificate files in `/docker/certs` keep working unchanged (Option A).
3. If certbot renews the certificate (as on the EC2 instance, where it writes
   into `/docker/certs` and its renewal hooks run `docker stop motuz_nginx` /
   `docker start motuz_nginx`), do one of the following:
   - **Switch to Traefik's Let's Encrypt support (recommended).** Set
     `MOTUZ_ACME_DOMAIN` (and optionally `MOTUZ_ACME_EMAIL`) as described in
     [Option B](#option-b-lets-encrypt), run
     `sudo install -d -m 700 /docker/traefik` and `./start.sh`, and check the
     certificate with
     `echo | openssl s_client -connect motuz.example.org:443 2>/dev/null | openssl x509 -noout -issuer -dates`.
     Then stop certbot, whose standalone renewal would stop the proxy:
     `sudo systemctl disable --now certbot.timer` (or remove its cron entry) and
     `sudo certbot delete --cert-name motuz.example.org`.
     If the deployment script replaces the checkout (like `bin/update.sh`),
     `.env` changes are lost; export the variables in the environment that runs
     it instead (e.g. the sourced `secrets.sh`).
   - **Keep certbot.** Point its hooks at the new container, then test them:
     ```bash
     sudo grep -rl motuz_nginx /etc/letsencrypt/renewal /etc/letsencrypt/renewal-hooks \
         | xargs -r sudo sed -i 's/motuz_nginx/motuz_traefik/g'
     sudo certbot renew --dry-run
     ```
     Traefik must be restarted after new files are copied to `/docker/certs`;
     the stop/start hook pair around a standalone renewal does that. A hook that
     only copies files needs a `docker restart motuz_traefik` after the copy.

### Using a custom database

The [.env](/.env) file provides a set of default variables that can be overwritten with environment variables. This can be leveraged to use a custom database.

```bash
export MOTUZ_DATABASE_PROTOCOL=postgresql
export MOTUZ_DATABASE_NAME=your_database_name
export MOTUZ_DATABASE_HOST=your_host.com:5432
export MOTUZ_DATABASE_USER=your_user
echo -n "your_password" > /docker/secrets/MOTUZ_DATABASE_PASSWORD

export MOTUZ_SMTP_SERVER=0.0.0.0:25
export MOTUZ_SMTP_USER=admin
echo -n "your_password" > /docker/secrets/MOTUZ_SMTP_PASSWORD

./start.sh
```

The config above is the equivalent of connecting to

```
postgresql://your_user:your_password@your_host.com:5432/your_database_name
```

The install without Docker takes the same settings in `motuz.env` and `secrets.env` with
`MOTUZ_DATABASE_MODE=external` (see [Steps as the motuz account](#steps-as-the-motuz-account)).

## Developer Installation

### Initialize

1. Install system dependencies

- [Docker](https://docs.docker.com/install/linux/docker-ce/ubuntu/)
- Python 3.12
- Node >= 22.15


2. Initialize app

```bash
./bin/dev/init_dev.sh
```

### Start

1. Start Database

```bash
./bin/dev/database_start.sh
```

2. Start RabbitMQ (in a new terminal window/tab)

```bash
./bin/dev/rabbitmq_start.sh
```

3. Start Celery (in a new terminal window/tab)

```bash
./bin/dev/celery_start.sh
```

4. Start Backend (in a new terminal window/tab)

```bash
./bin/dev/backend_start.sh
```

5. Start Frontend (in a new terminal window/tab)

```bash
./bin/dev/frontend_start.sh
```

6. See result at http://localhost:8080/

### Tests

Backend unit tests (no database needed):

```bash
./bin/ci/backend_unittest.sh
```

End-to-end tests build the Docker images, start a production-like stack (Traefik on ports
80/443, test users `alice`/`bob`, a fake Microsoft sign-in and Graph, Azurite), run all
suites (API, token broker, OneDrive sign-in, Traefik, credentials from the home directory,
UI with playwright, a remote worker behind an HTTP proxy) and remove the stack again. Nothing else may use ports 80, 443, 5000,
5001, 5432, 5672, 5999 or 10000, so do not run them on a Motuz server.

```bash
test/e2e/run.sh                     # everything
test/e2e/run.sh --no-build broker   # a single suite with the existing images
test/e2e/run.sh --keep              # leave the stack running for debugging (--down removes it)
```

The UI suite runs when Node and a playwright chromium are available
(`cd test/e2e/ui && npm ci && npx playwright install chromium`). Checks against a real S3
bucket run only with `MOTUZ_E2E_AWS_PROFILE` and `MOTUZ_E2E_AWS_BUCKET` set. GitHub Actions
(`.github/workflows/ci.yml`) runs the unit tests, the frontend build and the end-to-end
tests on every push and pull request.

## Development Options

1. Changing the host

```bash
MOTUZ_HOST='0.0.0.0' ./bin/dev/frontend_start.sh
MOTUZ_HOST='0.0.0.0' ./bin/dev/backend_start.sh
```

## Examples

### How to use the API

#### API Endpoint

* `http://localhost:5000/api/` if you followed the [developer installation](#developer-installation) instructions.
* `https://<host>/api/` if you followed the [Quick start](#quick-start) (the install without Docker).
* `https://localhost/api/` if you followed the [Docker quickstart](#docker-quickstart) instructions.
* `https://example.com/api/` if you followed the [Setting up production](#setting-up-production) instructions. Replace `example.com` with your actual domain name.

In the following examples, we'll use `https://example.com/api/` as the API endpoint.

If you visit the endpoint in a web browser, you will see a Swagger front end that makes the API easy to explore.

### Authentication

- POST requests to `/api/auth/login/` with correct credentials will issue an `access_token` and a `refresh_token` (called `access` and `refresh` in the JSON response)

cURL example (replace `myusername` and `mypassword` with your username and password):

```
curl -X POST "https://example.com/api/auth/login/" \
  -H  "accept: application/json" -H  "Content-Type: application/json" \
  -d "{  \"username\": \"myusername\",  \"password\": \"mypassword\"}"
```

- Protected API points can then be accessed by providing the `access_token` as an Authorization header. Example header:
    - Authorization: Bearer `access_token`

- The `access_token` can be provided to Swagger using the "Authorize" button an the top-right and inserting `Bearer $access_token` in the box, where $access_token is the value returned for key "access" in step 1

- The `access_token` is only valid for a limited amount of time (15 minutes). Upon expiration, a new `access_token` can be obtained by issuing a POST request to `/api/auth/refresh/` using the `refresh_token` in the Authorization field. Example:
    - Authorization: Bearer `refresh_token`

- The `refresh_token` has longer validity (30 days). Please note that the `POST /api/auth/refresh/` endpoint issues a new `refresh_token` as well, so if the users login at least once every 30 days, they will never be logged out.

## Folder structure

### Overview

| Folder | Description |
| --- | --- |
| `bin/` | Scripts for starting / installing / testing the application |
| `deployment/` | Container definition for production |
| `docs/` | Documentation |
| `src/` | All source code in one place |
| `src/frontend/` | Frontend code |
| `src/backend/` | Backend code |
| `test/` | All test code in one place |
| `test/frontend/` | Frontend testing |
| `test/backend/` | Backend testing |

### Frontend folder structure (inside `/src/frontend/`)

| Folder | Description |
| --- | --- |
| `css/` | Styling |
| `img/` | Images |
| `js/` | ReactJS Code |
| `js/actions/` | Redux Actions |
| `js/components/` | Reusable React Components |
| `js/managers/` | Reusable React Utilities |
| `js/middleware/` | React middleware |
| `js/reducers/` | Redux Reducers |
| `js/utils/` | Independent JavaScript Utilities |
| `js/views/` | Motuz-Specific view and business logic |
| `webpack/` | Webpack configurations (For JS bundling) |

### Backend folder structure (inside `/src/backend/`)

| Folder | Description |
| --- | --- |
| `api/` | Code for the API (Swagger) Module |
| `api/managers/` | Utilities that the views call to perform actions (Also called services in Flask) |
| `api/mixins/` | Flask Mixins for Database Models |
| `api/models/` | Database Models |
| `api/utils/` | Standalone helper code. Could be inside its own repository |
| `api/views/` | API Endpoints for Swagger |
| `migrations/` | Database Migrations |

### Temp folders

Additional temporary folders - ignore and do not commit

| Folder | Description |
| --- | --- |
| `__pycache__/` | Python Bytecode |
| `node_modules/` | JavaScript dependencies |
| `venv/` | Python dependencies |

## Other resources

- [Server Recipes](docs/server-recipes.md)
- [Homepage of Rclone](https://rclone.org/) - the workhorse behind Motuz
