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


<!--  The TOC below is created/maintained by
      the `Markdown TOC` extension to the
      Visual Studio Code editor.
 -->

<!-- TOC -->

1. [Quickstart](#quickstart)
2. [Beyond quickstart](#beyond-quickstart)
3. [Customizing your deployment](#customizing-your-deployment)
4. [Setting up production](#setting-up-production)
    1. [Authentication](#authentication)
        1. [Local Authentication](#local-authentication)
        2. [Authenticate against Active Directory](#authenticate-against-active-directory)
    2. [Requirements](#requirements)
    3. [Docker and docker-compose](#docker-and-docker-compose)
    4. [Shared Filesystems (optional)](#shared-filesystems-optional)
    5. [Cloning the Motuz repository](#cloning-the-motuz-repository)
    6. [Set up HTTPS certificate](#set-up-https-certificate)
        1. [Option A: certificate files (default)](#option-a-certificate-files-default)
        2. [Option B: Let's Encrypt](#option-b-lets-encrypt)
    7. [Running Motuz the first time](#running-motuz-the-first-time)
    8. [Redeploying](#redeploying)
    9. [Migrating from nginx to Traefik](#migrating-from-nginx-to-traefik)
    10. [OneDrive: own app registration](#onedrive-own-app-registration)
    11. [Google Drive](#google-drive)
    12. [Performance tuning](#performance-tuning)
5. [Developer Installation](#developer-installation)
    1. [Initialize](#initialize)
    2. [Start](#start)
    3. [Tests](#tests)
6. [Development Options](#development-options)
7. [Examples](#examples)
    1. [How to use the API](#how-to-use-the-api)
        1. [API Endpoint](#api-endpoint)
    2. [Authentication](#authentication-1)
8. [Folder structure](#folder-structure)
    1. [Overview](#overview)
    2. [Frontend folder structure (inside `/src/frontend/`)](#frontend-folder-structure-inside-srcfrontend)
    3. [Backend folder structure (inside `/src/backend/`)](#backend-folder-structure-inside-srcbackend)
    4. [Temp folders](#temp-folders)
9. [Other resources](#other-resources)

<!-- /TOC -->


## Quickstart

- Install [`docker` and `docker-compose`](https://docs.docker.com/install/linux/docker-ce/ubuntu/)
- Run the following command

```bash
git clone https://github.com/FredHutch/motuz.git
cd motuz
./bin/quickstart.sh
```

- Open browser at http://localhost/ and accept self-signed certificates

---

![Motuz main page ](docs/img/image_root.png)

---


## Beyond quickstart

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


## Customizing your deployment

- Add your SSL certificates - `/docker/certs/`, or set `MOTUZ_ACME_DOMAIN` for [Let's Encrypt](#option-b-lets-encrypt)
- Change the environment variables - `.env`
- Change passwords by editing the files - `/docker/secrets`
- Change the files that are visible to motuz - `docker-compose.override.yml`
- Use a different root (instead of `/docker`) by changing the MOTUZ_DOCKER_ROOT variable in `.env`


## Setting up production

The following instructions could be used on
an on-premises system or an EC2 instance.
For on-premises installations, we have found
that bare metal hardware has faster networking
than virtual machines.


We recommend using a machine running Ubuntu 24.04 (Noble).

To run Motuz without Docker, as `systemd --user` services on Ubuntu 26.04 LTS (for
example in a Proxmox VM), see [Install without Docker (Ubuntu 26.04)](#install-without-docker-ubuntu-2604).
On EC2 the default is the same install on Amazon Linux 2027, see
[Install on Amazon Linux 2027 (default on EC2)](#install-on-amazon-linux-2027-default-on-ec2).

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
git clone https://github.com/FredHutch/motuz.git
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

First, initialize the database as described
the "Initializing the Database" [section](README.md#initializing-the-database) of the README.
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
   unset values mean "not configured" (rclone's app).

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

Set who runs the installation in `.env` (all optional; without them the pages
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
  `docker-compose.yml`). Unset or empty means rclone's default, i.e. no flag at all, so
  existing installations behave as before.
- **Per job**, in the collapsed "Performance" section of the New Copy Job dialog: a
  preset or custom values for the destination's type, within the server's caps and
  memory budget. The job detail shows them, Retry keeps them, and "Check Integrity"
  passes the checkers on.

| Setting (`.env`) | rclone flag | rclone default | Per job |
|---|---|---|---|
| `MOTUZ_RCLONE_TRANSFERS` | `--transfers` | 4 | yes |
| `MOTUZ_RCLONE_CHECKERS` | `--checkers` (also for integrity checks) | 8 | yes |
| `MOTUZ_RCLONE_MULTI_THREAD_STREAMS` | `--multi-thread-streams` (0: off) | 4 | yes |
| `MOTUZ_RCLONE_MULTI_THREAD_CUTOFF` | `--multi-thread-cutoff` | 256M | yes |
| `MOTUZ_RCLONE_BUFFER_SIZE` | `--buffer-size` | 16M | no |
| `MOTUZ_RCLONE_S3_UPLOAD_CONCURRENCY` | `--s3-upload-concurrency` | 4 | yes (S3 destinations) |
| `MOTUZ_RCLONE_S3_CHUNK_SIZE` | `--s3-chunk-size` (5M to 5G) | 5M | yes (S3 destinations) |
| `MOTUZ_RCLONE_AZUREBLOB_UPLOAD_CONCURRENCY` | `--azureblob-upload-concurrency` | 16 | yes (Azure destinations) |
| `MOTUZ_RCLONE_AZUREBLOB_CHUNK_SIZE` | `--azureblob-chunk-size` | 4M | yes (Azure destinations) |

Caps for the per-job values: `MOTUZ_RCLONE_MAX_TRANSFERS` (default 64),
`MOTUZ_RCLONE_MAX_CHECKERS` (128), `MOTUZ_RCLONE_MAX_MULTI_THREAD_STREAMS` (32),
`MOTUZ_RCLONE_MAX_UPLOAD_CONCURRENCY` (64, S3 and Azure), and the memory budget per job
`MOTUZ_RCLONE_MEMORY_BUDGET` (default `8G`). Numbers are whole numbers, sizes use
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


### Remote workers (HTTPS only)

Jobs can run on other machines than the Motuz server: **remote workers** run
`motuz-worker` (`src/worker/motuz_worker.py`), which needs nothing but outbound HTTPS to
the Motuz server (port 443, optionally through an HTTP proxy). Workers have no database
access, open no ports and need no VPN. Typical setups:

- Motuz in the cloud (e.g. a small EC2 instance), workers on-prem next to the file systems
  (Proxmox VMs with the same mounts, users and versions as each other).
- Later: temporary cloud workers for large cloud-to-cloud jobs (see "Ephemeral workers").

A worker runs the same job code as the Motuz server's Celery worker: rclone as the job's
owner (`sudo -E -u <owner>`), the same output parsing, Stop, progress and final state in
the UI. Local paths are the worker's own mounts.

**Which jobs go where** (`.env` of the Motuz server, passed to the `app` container):

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

**On the Motuz server**

```bash
# .env: send jobs with local paths to the on-prem workers, then ./bin/prod/start.sh
MOTUZ_LOCAL_JOB_POOL=onprem

# One credential per worker; the secret is printed once
docker exec motuz_app bash -c 'source ./load-secrets.sh >/dev/null && python3 manage.py workers add proxmox-1 --pool onprem'
docker exec motuz_app bash -c 'source ./load-secrets.sh >/dev/null && python3 manage.py workers list'
docker exec motuz_app bash -c 'source ./load-secrets.sh >/dev/null && python3 manage.py workers revoke proxmox-1'
```

**Installing a worker** (Ubuntu; the same Motuz release and rclone version as the server,
the same users (SSSD/LDAP) and mounts as the other workers):

```bash
sudo apt-get install -y python3 sudo unzip curl
# rclone: the version pinned in deployment/docker/app/Dockerfile, at /usr/local/bin/rclone
sudo git clone https://github.com/FredHutch/motuz /opt/motuz   # check out the server's release

# An unprivileged account that may run commands as any user except root (rclone as the
# job's owner, like the server's containers do). Keep sudo's environment (sudo -E):
# the ALL command implies SETENV.
sudo useradd --system --create-home --home-dir /var/lib/motuz --shell /usr/sbin/nologin motuz
echo 'motuz ALL=(ALL,!root) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/motuz-worker
sudo chmod 440 /etc/sudoers.d/motuz-worker
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

**Ephemeral workers** (for the later EC2 launcher): a single-use bootstrap token, valid
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



## Install without Docker (Ubuntu 26.04)

A second way to run Motuz: `systemd --user` services of one dedicated account, `motuz`,
on Ubuntu 26.04 LTS. Meant for a VM (for example on Proxmox) or a privileged LXC. The
Docker install above stays the same; both use the same code, the same Traefik flags and
dynamic configuration, the same rclone and the same PostgreSQL major version.

| Service | Unit (in `~motuz/.config/systemd/user`) | Listens on |
| --- | --- | --- |
| PostgreSQL 18 (Ubuntu's binaries, data in `~/data/pg`) | `motuz-postgres` | 127.0.0.1:5432 |
| Redis 8, the Celery broker (password, append-only file in `~/data/redis`) | `motuz-redis` | a unix socket in `/run/user/<uid>/motuz-redis` only |
| uWSGI: API, frontend, OAuth token broker (`~/motuz/venv`, `src/backend/wsgi.ini`) | `motuz-app` | 127.0.0.1:5000, 127.0.0.1:5001 |
| Celery worker | `motuz-celery` | nothing |
| Traefik 3.7 (the flags of `docker-compose.yml`, `deployment/docker/traefik/dynamic/motuz.yml`) | `motuz-traefik` | :80, :443 |

All five are grouped by `motuz.target`, which starts at boot (`loginctl enable-linger
motuz`). Only Traefik listens beyond loopback: the sysctl
`net.ipv4.ip_unprivileged_port_start=80` lets a user service bind ports 80 and 443
(user services cannot get `CAP_NET_BIND_SERVICE`).

### Install

As root, from any clone of the repository:

```bash
sudo bin/systemd/install.sh                    # SSSD/Kerberos accounts (Active Directory)
sudo bin/systemd/install.sh --local-accounts   # also local /etc/shadow accounts (login helper)
```

`install.sh` checks that it runs on Ubuntu 26.04 and is idempotent (run it again after
updates; `deploy.sh` warns when it should). It installs the apt packages (PostgreSQL 18
without Ubuntu's own `main` cluster, whose service is masked like `redis-server`'s,
Node.js, build tools), the pinned rclone (version and SHA256 from
`deployment/docker/app/Dockerfile`), Traefik and uv (`deployment/systemd/versions.env`,
SHA256 checked), the account `motuz` (a system account with a locked password, home
`/var/lib/motuz`: keep it on local disk), linger, the sudoers rule, the sysctl,
`/etc/pam.d/motuz` and `/var/lib/motuz-aws-config`. Then, as `motuz`:

```bash
sudo -iu motuz
git clone https://github.com/FredHutch/motuz.git ~/motuz
~/motuz/bin/systemd/deploy.sh            # every update: ~/motuz/bin/systemd/deploy.sh --pull
```

`deploy.sh` creates `~/motuz/venv` with uv (Python 3.12, the Python of the Docker image,
installed by uv; Ubuntu's own 3.14 only runs the scripts), installs `requirements.txt`
(uWSGI is built from source, as in the image), builds the frontend with Ubuntu's Node.js
22 (`npm ci && npm run build`), creates `~/.config/motuz/motuz.env` (settings, from
`deployment/systemd/motuz.env.example`) and `secrets.env` (mode 600: the Flask key, the
database and Redis passwords, empty OAuth client secrets) on the first run and never
overwrites them, a self-signed certificate in `~/data/certs` if there is none, the
database cluster, the units, restarts `motuz.target` and checks
`https://127.0.0.1/api/system/info/`. Settings are the `MOTUZ_*` variables of `.env`
and `docker-compose.yml`; after editing `motuz.env` run `deploy.sh` again (or
`systemctl --user restart motuz.target`). Certificates: replace
`~/data/certs/cert.crt` and `cert.key` and run `systemctl --user restart motuz-traefik`,
or set `MOTUZ_ACME_DOMAIN` (and `MOTUZ_ACME_EMAIL`) for Let's Encrypt. Open 80/tcp and
443/tcp in the firewall.

Status and logs (as `motuz`; `sudo -iu motuz` sets no session, `deploy.sh` finds the user
manager itself):

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user status 'motuz*'
sudo journalctl _SYSTEMD_USER_UNIT=motuz-app.service -f   # as an admin: motuz is a system
                                                          # account, its logs are in the system journal
```

### Why a sudoers rule

Every file and rclone operation runs as the logged-in user, so the operating system
checks the permissions (in Docker, Motuz runs as root for this). Without Docker, the
account `motuz` gets exactly one rule, `/etc/sudoers.d/motuz`, checked with `visudo -c`:

```
motuz ALL=(ALL, !root) NOPASSWD:SETENV: /usr/local/bin/rclone, /usr/bin/ls, /usr/bin/mkdir, /usr/bin/env
```

- `rclone` copies, lists and reads cloud storage; `ls` and `mkdir` browse the local
  filesystem; `env` starts the small Python readers of the file viewer and of the
  credentials in the home directory (`python3 -I -S -c ...`).
- `!root`: never as root. `SETENV`: rclone gets a connection's credentials as
  `RCLONE_CONFIG_*` variables, passed with `sudo --preserve-env=<names>` (Ubuntu 26.04's
  default `sudo` is sudo-rs, which ignores `-E`; the classic `sudo` works as well).
- With the classic `sudo` (Amazon Linux, or Ubuntu's `sudo.ws`) the file also gets
  `Defaults:motuz !log_allowed`: that `sudo` logs the variables of `--preserve-env` with
  every allowed command (`ENV=RCLONE_CONFIG_..._SECRET_ACCESS_KEY=...`), which would put
  the connections' secrets into the journal. Refused commands are still logged; Motuz
  logs the commands it runs itself, with the credentials masked.
- `install.sh --sudo-group=GROUP` allows only the members of a group
  (`(%GROUP, !root)`).
- Motuz refuses logins as root and as the account it runs as.

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

# 2. On the new host: install.sh, the clone and a first deploy.sh (above), then
sudo install -o motuz -m 600 /root/motuz-export.tar.gz /var/lib/motuz/
sudo -iu motuz ~/motuz/bin/systemd/migrate_from_docker.sh import ~/motuz-export.tar.gz
```

The export holds the database as a `pg_dump` dump with the row count of every table, the
Flask secret key (it signs the login tokens: sessions stay valid), the SMTP password, the
OAuth client secrets, the `MOTUZ_*` settings of the running app container, Traefik's
`acme.json` and `certs/cert.*`, and the list of the app container's mounts (recreate them
at the same paths). It is secret: delete it after the import. The import recreates the
database with `pg_restore` (owned by `motuz_user`), checks the row counts, writes the
secrets and settings and runs `deploy.sh`.

The data directory is never copied: the Docker image is Alpine (musl libc) and Ubuntu
uses glibc, so text sorts differently and copied indexes on text columns would be
corrupt. A dump and restore rebuilds every index.

### Tests of this install

`test/e2e/run_systemd.sh` starts an Ubuntu 26.04 cloud image as a VM (QEMU/KVM in a
container, `test/e2e/systemd/vm.sh`; needs docker and `/dev/kvm`), runs `install.sh
--local-accounts` and `deploy.sh` in it, and runs the same end-to-end suites as
`test/e2e/run.sh` against it (`MOTUZ_E2E_TARGET=systemd`), plus security checks
(`test/e2e/systemd/security_test.sh`). `test/e2e/systemd/migration_test.sh` migrates
the Docker e2e stack into such a VM.


## Install on Amazon Linux 2027 (default on EC2)

On EC2, install Motuz on Amazon Linux 2027 (AL2027, a public preview since September 2026)
with the same scripts as [Install without Docker (Ubuntu 26.04)](#install-without-docker-ubuntu-2604):
`systemd --user` services of the account `motuz`, the same units, settings
(`~/.config/motuz/motuz.env`, including the `MOTUZ_RCLONE_*` defaults), sudoers rule,
login helper, migration and update steps. They start faster than containers, run as fast,
and work with SELinux enforcing. What differs is in `bin/systemd/distro/amzn.sh`:

| | Amazon Linux 2027 | Ubuntu 26.04 |
| --- | --- | --- |
| PostgreSQL 18 | `postgresql18-server` (binaries in `/usr/bin`) | `postgresql-18` |
| Celery broker (`motuz-redis`) | **Valkey 9** (`valkey-server`; AL2027 has neither RabbitMQ nor Redis) | Redis 8 |
| Python of `~/motuz/venv` | the system's **Python 3.14** (`python3`, `python3-devel` for uWSGI) | Python 3.12 from uv |
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
AL2027 all backend unit tests and the end-to-end suites pass on its Python 3.14 with these
pins (uWSGI 2.0.31 builds), so `deploy.sh` uses `/usr/bin/python3.14`
(`DISTRO_PYTHON`): it gets the distribution's security updates and needs no download. On
Ubuntu, `deploy.sh` keeps uv's 3.12.

**SELinux** stays enforcing; no booleans, file contexts or policy modules are needed.
`install.sh` runs `restorecon` on everything it writes. The account's user services
(uWSGI, Celery, rclone, Traefik, PostgreSQL, Valkey) run as `unconfined_u` like any login
of the account, `sudo` to the logged-in user keeps that context, and the login helper is a
system service (`unconfined_service_t`) that may read `/etc/shadow`. Traefik binds 80 and
443 through the same sysctl as on Ubuntu. Check with `sudo ausearch -m avc -ts boot`
(empty). PostgreSQL and Valkey stay user services of `motuz`, as on Ubuntu, rather than
the packages' system services (which would run confined as `postgresql_t` / `redis_t`):
then `deploy.sh` manages everything as the account without root, both distributions
share one design, and the database and the broker are reachable only by that account.

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
`deploy.sh`), or only `sudo -iu motuz ~/motuz/bin/systemd/deploy.sh --pull` when the
installed pieces did not change (`deploy.sh` warns otherwise). `dnf upgrade` keeps
PostgreSQL 18 and Python 3.14; after a Python minor change `deploy.sh` rebuilds the venv.

**Uninstall**: `sudo bin/systemd/uninstall.sh` stops Motuz and removes the units, the
sudoers rule, the PAM service, the sysctl file and the login helper, and unmasks the
distribution's PostgreSQL and Valkey services; the account and its data stay.
`--purge --yes` also deletes the account, its home (database, secrets, certificates) and
the pinned binaries. Packages stay installed.

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
* `https://localhost/api/` if you followed the [Quickstart](#quickstart) instructions.
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

- The `access_token` is only valid for a limited amount of time (1 day). Upon expiration, a new `access_token` can be obtained by issuing a POST request to `/api/auth/refresh/` using the `refresh_token` in the Authorization field. Example:
    - Authorization: Bearer `refresh_token`

- The `refresh_token` has longer validity, say T days. Please note that the `POST /api/auth/refresh/` endpoint issues a new `refresh_token` as well, so if the users login at least once every T days, they will never be logged out.



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
