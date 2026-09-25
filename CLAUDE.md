# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Motuz is a web UI + REST API for large data transfers between on-prem filesystems and cloud storage (S3, Azure Blob, Swift, GCS, SFTP, Dropbox, OneDrive, Google Drive, WebDAV). All actual data movement is done by shelling out to `rclone`. Backend: Python 3.12, Flask 3 + flask-restx + Flask-SQLAlchemy 3 / SQLAlchemy 2 (psycopg 3) + Alembic + Celery 5 (RabbitMQ broker, results stored in the database). Frontend: React 18 + Redux + redux-api-middleware 3 + react-router 5, bundled with webpack 5 (Node >= 22.15).

## Commands

Dependencies: top-level Python deps live in `requirements.in` / `requirements-dev.in`; the pinned `requirements*.txt` are generated with `uv pip compile` (command in the `requirements.in` header). The Docker image installs only `requirements.txt`.

Dev setup (creates `venv/`, installs Python/npm deps, initializes the DB via docker, runs migrations):
```bash
./bin/dev/init_dev.sh
```

Dev run — each in its own terminal; UI at http://localhost:8080 (webpack-dev-server proxies `/api` and `/swaggerui` to Flask on :5000):
```bash
./bin/dev/database_start.sh   # postgres:18 in docker, data in $MOTUZ_DOCKER_ROOT/volumes/postgres
./bin/dev/rabbitmq_start.sh
./bin/dev/celery_start.sh     # celery -A api.tasks worker (run from src/backend)
./bin/dev/backend_start.sh    # python manage.py run (from src/backend)
./bin/dev/frontend_start.sh   # npm start
```
Set `MOTUZ_HOST=0.0.0.0` to bind backend/frontend to all interfaces.

**`src/backend/api/config.py` reads `MOTUZ_FLASK_SECRET_KEY` and `MOTUZ_DATABASE_{PROTOCOL,USER,PASSWORD,NAME,HOST}` from the environment at import time and raises `KeyError` if any is missing.** Anything that imports `api` (the server, celery, manage.py, tests) needs them exported. The dev/CI scripts source `bin/_utils/load_env.sh`, which exports `.env` defaults plus the files in `$MOTUZ_DOCKER_ROOT/secrets`; the tests only need the variables set, not a reachable database.

Tests (unittest, discovered from `test/backend/`, run with cwd `src/backend` so `api` is importable):
```bash
./bin/ci/backend_unittest.sh                      # all backend tests (python manage.py test)
cd src/backend && python -m unittest discover -s ../../test/backend -p 'test_rclone_connection.py' -k test_allowlist   # single test
cd src/backend/api/utils && python -m unittest file_utils_test   # legacy test next to the code, not picked up by manage.py test
```

End-to-end tests (`test/e2e/`, needs docker and free ports 80, 443, 5000, 5001, 5432, 5672, 5999, 10000): `run.sh` builds the images with `bin/prod/build.sh`, starts `test/e2e/compose.yml` (the production stack with host networking and Traefik, test users alice/AlicePass1 and bob/BobPass1 from `users.sh`, Azurite, OneDrive/Graph pointed at `fake_ms.py` on 127.0.0.1:5999), runs the suites on a fresh database and tears everything down (also on failure). Secrets and the self-signed cert are generated into `test/e2e/.work/` and `test/e2e/.env` (git-ignored); logs go to `test/e2e/logs/`.
```bash
test/e2e/run.sh                          # all suites: e2e broker oauth-paste traefik credentials ui oauth-callback
test/e2e/run.sh --no-build broker        # one suite with the existing images (ui also runs credentials)
test/e2e/run.sh --keep e2e               # leave the stack running; then e.g. cd test/e2e && python3 e2e_test.py
test/e2e/run.sh --down                   # remove a stack left by --keep
```
Suites are plain scripts (`*_test.py`, `traefik_test.sh`, `ui/ui_test.mjs`, helpers in `common.py`) that print `PASS`/`FAIL`/`SKIP` lines and `<passed>/<total> passed`; `run.sh` prints one summary and exits non-zero if any failed. `e2e` and `broker` expect a fresh database and a fresh fake server (run.sh restarts `fake_ms.py` before each suite that uses it; `oauth-callback` first switches the app to an own OneDrive app). The UI suite (playwright, `cd test/e2e/ui && npm ci && npx playwright install chromium`) is skipped when node or chromium is missing unless `MOTUZ_E2E_UI=require`. The credentials suite uses fake AWS keys and skips the checks against real S3 unless `MOTUZ_E2E_AWS_PROFILE` (static keys in `~/.aws/credentials`) and `MOTUZ_E2E_AWS_BUCKET` are set.

CI: `.github/workflows/ci.yml` (push and pull_request) runs the backend unit tests, the frontend build and `test/e2e/run.sh` with `MOTUZ_E2E_UI=require`; no secrets are needed.

Lint: `pylint` using `.pylintrc` (no npm lint script / eslint config).

Frontend production build: `npm run build` (outputs to `./build/`). The app image builds it in a `node:22` stage and copies it to `/app/build` (`FRONTEND_DIR`).

Migrations (Flask-Migrate/Alembic; filenames are timestamp-prefixed via `alembic.ini` `file_template`):
```bash
./bin/dev/migrate.sh   # `manage.py db migrate` (autogenerate), then prompts before `db upgrade`
```
The production app container runs `manage.py db upgrade` on every start (`deployment/docker/app/app-entrypoint.sh`).

Production (docker-compose): `make quickstart` / `./bin/quickstart.sh` (first-time bootstrap of `/docker` certs/secrets/volumes), `./bin/prod/build.sh`, `./bin/redeploy.sh` (rebuild + restart). Production deploys from the `prod` branch, which is synced by `git reset --hard master` + force push rather than merging.

## Architecture

### Backend request flow (`src/backend/api/`)
`views/*_views.py` (flask-restx `Namespace` + `api.model` DTOs, mounted under `/api` in `application.register_api`; Swagger UI at `/api/`) → `managers/*_manager.py` (business logic, decorated with `@token_required`) → `models/` and `utils/`.

- **Auth**: `/api/auth/login/` checks username/password with PAM (`utils/pam.py`) against the host's users; the JWT identity is the Unix username. `JWT_IDENTITY_CLAIM = 'identity'` is part of the API contract (the frontend reads `state.access.identity`). Every token is checked against the `revoked_token` table (logout revokes the refresh token).
- **Per-user scoping**: every model has an `owner` string column. Managers filter by `owner == get_logged_in_user(request)` and return 404 (not 403) for other users' objects. Jobs resolve their `src_cloud_id`/`dst_cloud_id` through `cloud_connection_manager.owned_cloud_id` so a job can never use another user's credentials. Connection create/update only accept columns in `_WRITABLE_FIELDS` (never `id`/`owner`), and an empty value for a secret field on update keeps the stored secret.
- **DTO secrets**: credential fields use `PrivateString` in the view DTOs, so they can be written but are never returned by the API.

### Filesystem access as the logged-in user
Motuz runs as root in the containers but every filesystem/rclone operation runs as the logged-in user via `sudo -E -u <owner> ...`, so local permissions are enforced by the OS. That is why the containers mount host `/etc`, PAM/SSSD/Kerberos config, and the shared filesystems (the Fred Hutch-specific mounts in `docker-compose.override.yml`; these must be added to both `app` and `celery`).

- `utils/rclone_connection.py` (`RcloneConnection`) — cloud ops. It runs `rclone --config=/dev/null` and passes credentials as env vars `RCLONE_CONFIG_<REMOTE>_<KEY>` (remote names `src`, `dst`, `current`) built by `_formatCredentials()` from a `CloudConnection` row. `_log_command()` masks credentials in logs using suffix allowlists (`should_log_full_credential` / `should_log_partial_credential`).
- `utils/local_connection.py` (`LocalConnection`) — same interface for the local filesystem (`ls`/`mkdir`/home dir via sudo). A `connection_id` of `0` in the API, or a `None` src/dst cloud on a job, means local filesystem.
- User-supplied paths must never be parsed as options: local commands put `--` before the path, and local rclone paths must be absolute (`_local_path`).

### Credentials from the user's home directory
`utils/local_credentials.py`. The New Connection dialog (`LocalCredentialPicker.jsx`) calls `GET /api/connections/local-credentials/?type=s3|azureblob`, which lists AWS profiles (`~/.aws/credentials`, `~/.aws/config`, SSO login state from `~/.aws/sso/cache`) and s3/azureblob remotes in `~/.config/rclone/rclone.conf` (or `~/.rclone.conf`). It returns metadata only (name, kind, region, key id masked to the last 4, Azure account, `usable`/`reason`). A connection created from one has `subtype='profile'` plus `profile_source` (`aws`/`rclone`) and `profile_name`, and no stored credentials. `_formatCredentials` calls `local_credentials.resolve()`, which re-reads the files every time rclone runs (rotated keys are picked up).
- Files are read only as the user: `sudo -n -u <user> -- env python3 -I -S -c <reader>` with fixed relative paths below `_homepath_with_impersonation`, non-blocking open, regular files only, 1 MiB cap, timeout. Never read them as root.
- rclone never gets the user's AWS files: static keys, session tokens and roles (`role_arn` + a `source_profile` with keys, via rclone's `role_arn`) are passed as `RCLONE_CONFIG_*` values. With the raw files (env_auth) the AWS SDK would run `credential_process`, i.e. users could run programs in the containers. SSO profiles use env_auth with a config file Motuz writes from allowlisted SSO keys (`/tmp/motuz-aws-config/<sha256>.ini`, no secrets) and `HOME` set to the user's home, because `sudo -E` keeps root's `HOME`. `credential_process`, MFA roles, `credential_source`, rclone `env_auth`/`use_msi`/`use_az` are reported as unusable.

### Jobs (copy + integrity check)
1. The manager inserts a `CopyJob`/`HashsumJob` row and dispatches the Celery task (`tasks/celery_tasks.py`) with the **Celery task id set to the DB id as a string**.
2. The worker starts rclone (`copyto` or `md5sum`) in its own process group, with background threads from `utils/copy_job_queue.py` / `utils/hashsum_job_queue.py` reading stdout (the `--progress` stats in non-TTY form, where the bytes and file-count lines are both named `Transferred:`) and stderr.
3. Once per second the task writes progress to the DB row and to the Celery task state `meta` (text/error text via RabbitMQ result backend).
4. `retrieve` merges `AsyncResult(id).info` into the DB object for live text; this needs a shared result backend (`CELERY_RESULT_BACKEND = 'db+' + database URI`), since Celery 5 removed the `amqp` backend. `stop` calls `revoke(terminate=True)`; the task's SIGTERM handler (`_terminate_rclone_on_sigterm`) kills the rclone process group before the worker process exits. Email notifications go through `utils/email_utils.py`.

Hashsum jobs run md5sum on src then dst, build file trees (`utils/file_utils.py`), and store only the differing branches (`remove_identical_branches`) as JSON.

### OAuth token broker (OneDrive, Google Drive)
rclone gets OAuth tokens via env vars and `--config=/dev/null`, so it can never persist a refreshed token. For brokered types (`token_broker_manager.BROKERED_TYPES`: `onedrive`, `drive`, derived from the provider definitions in `oauth_manager.PROVIDERS`) `_formatCredentials` passes the current access token plus an opaque per-connection handle (`cloud_connection.token_broker_handle`) as the refresh token, and sets the remote's `token_url` to `TOKEN_BROKER_URL`. That is `/internal/oauth/token`, served on uWSGI's loopback `http-socket` 127.0.0.1:5001. It is exposed nowhere else: Traefik's router rule excludes `/internal`, and `internal_views.only_on_broker_socket` returns 404 unless `SERVER_PORT` (the accepting socket's port, not the Host header) is the port of `TOKEN_BROKER_URL`, i.e. 5001 in production and 5000 with the dev server. The broker locks the connection row, returns the cached access token if it has more than 5 minutes left, and otherwise refreshes upstream with the real refresh token, which never leaves the server. Concurrent jobs share a single refresh. A response without `refresh_token` (Google) keeps the stored one; a new one (Microsoft rotates) replaces it. Test it against a fake token endpoint by setting `MOTUZ_ONEDRIVE_TOKEN_URL` / `MOTUZ_GDRIVE_TOKEN_URL`.

Providers: `oauth_manager.Provider` holds everything provider specific (config prefix `ONEDRIVE`/`GDRIVE` for `*_CLIENT_ID/_CLIENT_SECRET/_REDIRECT_URI/_AUTH_URL/_TOKEN_URL`, rclone's public app id + obscured secret + loopback redirect, scopes, extra auth params, drive discovery, connection columns, token and client-id columns). Routes are `/api/oauth/<provider>/{start,finish,callback,flows/<state>,connect}` with provider `onedrive` or `gdrive`; OneDrive's callback lands on `/clouds?oauth_state=...`, others add `&oauth_provider=<name>`. Google Drive (connection type `drive`, columns `gdrive_*`) uses `access_type=offline&prompt=consent` (else Google returns no refresh token) and discovers My Drive (`about`) plus shared drives (`drives`, which set rclone's `team_drive`). rclone's shared Drive client is heavily rate limited and being retired by rclone during 2026; the README ("Google Drive") describes the own client and Google's restricted-scope limits. Frontend: `OauthSignIn.jsx` is the provider-parameterized sign-in (texts in `OAUTH_PROVIDERS`, used for Google; OneDrive still uses `OnedriveSignIn.jsx`), and `GdriveSection.jsx` is the Drive part of the connection form.

"Sign in with Microsoft/Google" (`oauth_manager`) uses rclone's public app, or an own app when `MOTUZ_ONEDRIVE_CLIENT_ID` / `MOTUZ_GDRIVE_CLIENT_ID` is set (secrets: docker secrets `MOTUZ_ONEDRIVE_CLIENT_SECRET` / `MOTUZ_GDRIVE_CLIENT_SECRET`, empty files by default; empty means unset). A refresh token only works with the app that issued it, so connections remember it in the server-controlled `cloud_connection.onedrive_client_id` / `gdrive_client_id` (NULL = rclone's app, e.g. pasted tokens; pasting a token resets it, `cloud_connection_manager._TOKEN_CLIENT_COLUMNS`). The broker's `connection_client_credentials` / `upstream_client_credentials` forwards rclone's own client credentials for rclone-app connections, substitutes the own app's id and secret (never given to rclone) for own-app connections, and refuses others with `invalid_grant`. Child processes get the server environment without the server secrets (`abstract_connection.subprocess_env`), because `sudo -E` would hand them to the user's rclone.

### Adding a field to a cloud connection type
Follow the pattern of the `kms_encryption_key_arn` commits (`8a8988f`, `0a02274`):
1. Add a column in `models/cloud_connection.py` and an Alembic migration.
2. Add it to the DTO in `views/cloud_connection_views.py` (use `PrivateString` for secrets).
3. Map it to an rclone env var in `RcloneConnection._formatCredentials`, and add its suffix to the logging allowlist if it is safe to log.
4. Add the form field in `src/frontend/js/views/Dialogs/CloudConnection/CloudConnectionDialogFields.jsx`.

### Frontend (`src/frontend/js/`)
Webpack `resolve.modules` includes `src/frontend/js`, so imports are root-relative (`'reducers/reducers.jsx'`, `'actions/apiActions.jsx'`). API calls are `RSAA` actions (redux-api-middleware) in `actions/apiActions.jsx` with `withAuth` headers. `middleware/authMiddleware.jsx` handles token refresh. `redux-persist` keeps auth/settings in local storage. The main UI is a two-pane file browser (`views/App/Pane/`) with command bars and the job tables in `views/App/CopyJobSection/`.

### Deployment
All services in `docker-compose.yml` use `network_mode: host`:
- **traefik** (`traefik:v3.7`, container `motuz_traefik`): the only thing listening beyond loopback (:80 redirects to :443). File provider only, no docker socket, no dashboard/API. Static config is the service's `command` flags (Traefik takes static config from exactly one source, and flags let compose substitute `MOTUZ_ACME_*` from `.env`); dynamic config is `deployment/docker/traefik/dynamic/motuz.yml`, a Go template: one router for everything except `/internal`, security-headers middleware, `responseHeaderTimeout: 600s` and no connection reuse to uWSGI, TLS 1.2+ with AEAD ciphers. TLS: `$MOTUZ_DOCKER_ROOT/certs/cert.{crt,key}` by default (reloaded only on `docker restart motuz_traefik`), or Let's Encrypt HTTP-01 when `MOTUZ_ACME_DOMAIN` is set (`acme.json` in `$MOTUZ_DOCKER_ROOT/traefik`).
- **app** (`PYTHON_ENVIRONMENT=prod`): uWSGI `http-socket`s on `127.0.0.1:5000` (for Traefik) and `127.0.0.1:5001` (token broker), `src/backend/wsgi.ini`. Besides the API it serves the built frontend (`views/frontend_views.py`, via `wsgi.file_wrapper`/sendfile): `/js`, `/css` cached 30 days, `/img` 7 days, anything else not under `/api`, `/swaggerui`, `/internal` gets `index.html` with `no-store` (SPA routes). Traefik only terminates TLS and routes because it cannot serve files, and this uWSGI build has no PCRE, so `static-map` could not set per-path Cache-Control. `wsgi.py` wraps the app in `ProxyFix(x_for=1, x_proto=1)` only (never host/port, see the broker check).
- **celery** (image built `FROM fredhutch/motuz_app`, so rebuild app first), **rabbitmq**, **database** (postgres).

Secrets are files in `$MOTUZ_DOCKER_ROOT/secrets` (default `/docker`), exposed as docker secrets and exported to env by `deployment/docker/load-secrets.sh`. Compose refuses to start when a secret file is missing, so optional secrets are created as empty files by `bin/_utils/optional_secrets.sh` (run by `bin/prod/start.sh` and `bin/quickstart.sh`). The rclone version and its SHA256 are pinned in `deployment/docker/app/Dockerfile`. PostgreSQL is 18 (`PGDATA` is set to `/var/lib/postgresql/data` because the 18 images changed the default). When the `database` image major version is bumped, `bin/_utils/upgrade_postgres.sh` (run by `bin/prod/start.sh` and `bin/dev/database_start.sh`) migrates `$MOTUZ_DOCKER_ROOT/volumes/postgres` with dump/restore, verifies row counts and keeps the old directory as `postgres.pg<old>.<timestamp>`. The database must be owned by `motuz_user` (PostgreSQL 15+ public schema rules). The app runs `manage.py db upgrade` on each start. `bin/prod/start.sh` runs `docker-compose down --remove-orphans`, which also removes the `motuz_nginx` container of pre-Traefik installs (README, "Migrating from nginx to Traefik"). `.gitlab-ci.yml` is a placeholder; CI runs on GitHub Actions (see Commands).
