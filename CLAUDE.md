# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Motuz is a web UI + REST API for large data transfers between on-prem filesystems and cloud storage (S3, Azure Blob, Swift, GCS, SFTP, Dropbox, OneDrive, Google Drive, WebDAV). All actual data movement is done by shelling out to `rclone`. Backend: Python 3.12 (Docker image) or the distribution's 3.14 (systemd install), Flask 3 + flask-restx + Flask-SQLAlchemy 3 / SQLAlchemy 2 (psycopg 3) + Alembic + Celery 5 (RabbitMQ broker in docker, Redis/Valkey in the systemd install; results stored in the database). The **default install is without Docker** (`systemd --user` services of one account on Ubuntu 26.04 or Amazon Linux 2027, `bin/systemd/`); Docker (`docker-compose.yml`) is the alternative. Frontend: React 18 + Redux + redux-api-middleware 3 + react-router 5, bundled with webpack 5 (Node >= 22.15).

## Rules

Invariants that hold everywhere (the details are in the sections below and in `.claude/rules/`, which load when you work on matching files).

- Run commands as the logged-in user only through `abstract_connection.sudo_as(user, env)` with exactly one absolute path of the sudoers rule (`/var/lib/motuz-runas/rclone, /usr/bin/ls, /usr/bin/mkdir, /usr/bin/env`; rclone is `rclone_connection.RCLONE`, from `MOTUZ_RCLONE`: `/usr/local/bin/rclone` in Docker and on EC2 workers); never `-E`, `-i` or a login shell.
- Systemd install: rclone, Traefik and uv are the account's own (`bin/systemd/tools.sh`, run by `deploy.sh`): rclone in `/var/lib/motuz-runas` (the users run it through sudo, so it is outside the closed home), Traefik and uv in `~/.local/bin`; root never runs them and never downloads them for the account.
- Commands run as a user get only the allowlisted environment of `abstract_connection.user_process_env` (never the server's secrets or other `MOTUZ_*` variables).
- User-supplied paths never become options: `--` before local paths, local rclone paths absolute (`_local_path`).
- Every model has an `owner`; managers filter by it and return 404 (not 403) for other users' objects.
- Secret DTO fields are `PrivateString`; connection create/update accept only `_WRITABLE_FIELDS`.
- `modified` in listings is always UTC ISO 8601 with `Z` and whole seconds, or null; never naive local times.
- Celery task ids are `copy-<id>` / `hashsum-<id>` (`_celery_task_id`).
- `api.utils` and `api.version` must never import the Flask app or config (the remote worker agent imports them without `MOTUZ_*` variables).
- Bump `VERSION` / `WORKER_PROTOCOL` in `api/version.py` when the job code or the worker API changes.
- Never build rclone flags from user text: only validated values through `rclone_tuning` (`copy_flags` / `hashsum_flags`).
- Systemd install: root never writes, chowns or follows names inside the service account's home; everything root reads from it is validated.
- Change Traefik flags only in the `command:` of `docker-compose.yml` (`bin/systemd/traefik_args.py` derives the systemd flags from it).
- Never log secrets or file contents.
- `src/backend/api/config.py` needs the `MOTUZ_*` variables below at import time.

## Commands

Dependencies: top-level Python deps live in `requirements.in` / `requirements-dev.in`; the pinned `requirements*.txt` are generated with `uv pip compile` (command in the `requirements.in` header). The Docker image installs only `requirements.txt`.

Dev setup (creates `venv/`, installs Python/npm deps, initializes the DB via docker, runs migrations):
```bash
./bin/dev/init_dev.sh
```

Dev run — each in its own terminal; UI at http://localhost:8080 (webpack-dev-server proxies `/api`, `/swaggerui`, `/privacy` and `/terms` to Flask on :5000):
```bash
./bin/dev/database_start.sh   # postgres:18 in docker, data in $MOTUZ_DOCKER_ROOT/volumes/postgres
./bin/dev/rabbitmq_start.sh
./bin/dev/celery_start.sh     # celery -A api.tasks worker (run from src/backend)
./bin/dev/backend_start.sh    # python manage.py run (from src/backend)
./bin/dev/frontend_start.sh   # npm start
```
Set `MOTUZ_HOST=0.0.0.0` to bind backend/frontend to all interfaces.

Default install without Docker (Ubuntu 26.04 or Amazon Linux 2027, `systemd --user` services of the account `motuz`; details, options and the Claude-assisted deploy: `.claude/rules/systemd-install.md`, README "Install (default: without Docker)"):

- Install, once, as root: `sudo bin/systemd/install.sh [--local-accounts] [--system-postgres|--private-postgres] [--system-redis|--private-redis]`.
- Deploy, as `motuz`, on every update: `~/motuz/bin/systemd/deploy.sh [--pull]`.
- Both in one step (also EC2 user data `deployment/aws/user-data-al2027.sh`): `sudo bin/systemd/bootstrap.sh --repo=URL --branch=B [install.sh options]`.
- Uninstall: `bin/systemd/uninstall.sh [--purge --yes]`.
- Move from Docker: `bin/systemd/migrate_from_docker.sh export|import`; private -> system PostgreSQL: `bin/systemd/migrate_postgres.sh`.
- Settings and secrets without printing them: `bin/systemd/config.sh show|set|ask-secret` (edits `motuz.env` / `secrets.env`).
- Remote worker host: `install.sh --worker-only [--central-url=URL] [--pool=NAME] [--credential-file=FILE] [--run-as=NAME]`; one temporary job: `sudo bin/systemd/worker_once.sh --bootstrap-token-file=FILE`.
- AI assistant playbook for the account's half: `.claude/skills/motuz-deploy/SKILL.md` (`/motuz-deploy`), `bin/systemd/smoke_test.py`.
- Tests: `test/e2e/run_systemd.sh [--distro=ubuntu|al2027] [--keep] [--reuse] [suite ...]` (`MOTUZ_E2E_INSTALL_OPTS` passes options to `install.sh`; VM: `test/e2e/systemd/vm.sh`, AL2027 container: `test/e2e/systemd/container.sh`), `test/e2e/systemd/worker_host_test.sh`, `test/e2e/systemd/migration_test.sh`.

**`src/backend/api/config.py` reads `MOTUZ_FLASK_SECRET_KEY` and `MOTUZ_DATABASE_{PROTOCOL,USER,PASSWORD,NAME,HOST}` from the environment at import time and raises `KeyError` if any is missing.** Anything that imports the application or config (the server, celery, manage.py, tests) needs them exported; the package `api` itself loads the application lazily, so `api.utils` and `api.version` import without them (the remote worker agent relies on that). The dev/CI scripts source `bin/_utils/load_env.sh`, which exports `.env` defaults plus the files in `$MOTUZ_DOCKER_ROOT/secrets`; the tests only need the variables set, not a reachable database.

Tests (unittest, discovered from `test/backend/`, run with cwd `src/backend` so `api` is importable):
```bash
./bin/ci/backend_unittest.sh                      # all backend tests (python manage.py test)
cd src/backend && python -m unittest discover -s ../../test/backend -p 'test_rclone_connection.py' -k test_allowlist   # single test
cd src/backend/api/utils && python -m unittest file_utils_test   # legacy test next to the code, not picked up by manage.py test
./bin/ci/frontend_unittest.sh                     # node --test test/frontend/*.mjs, with TZ=UTC and TZ=America/New_York: plain-JS helpers only (e.g. utils/parentDirectory.js, fileSort.js, fileAge.js, viewerChunks.js, viewerFollow.js, viewerKind.js, copyPerformance.js), not JSX
```
When setting the variables by hand, use a realistic secret (`MOTUZ_FLASK_SECRET_KEY=$(openssl rand -hex 32)`): with a one-character value `test_ec2_launcher` fails because that character occurs in the user-data script. `test_heic_convert ... test_timeout_is_504_and_kills_the_process_group` is timing-sensitive and can fail once under load; rerun it alone before chasing it.

End-to-end tests (`test/e2e/`, needs docker and free ports 80, 443, 5000, 5001, 5432, 5672, 5999, 10000): `run.sh` builds the images, starts the production stack (`test/e2e/compose.yml`), runs the suites on a fresh database and tears everything down. Suites print `PASS`/`FAIL`/`SKIP` lines and `<passed>/<total> passed`. Suite internals, the UI suites and the worker suite: `.claude/rules/e2e-tests.md`.
```bash
test/e2e/run.sh                          # all suites: e2e broker oauth-paste traefik credentials ui oauth-callback ui-callback worker
test/e2e/run.sh --no-build broker        # one suite with the existing images (ui also runs credentials)
test/e2e/run.sh --keep e2e               # leave the stack running; then e.g. cd test/e2e && python3 e2e_test.py
test/e2e/run.sh --down                   # remove a stack left by --keep
```

`run.sh` and `run_systemd.sh` share `test/e2e/_suites.sh`; suites reach the installation only through `common.py`'s `sh()`, `psql()`, `pg_dump()` and `service_logs()` (and `appShell()` in `ui/ui_test.mjs`), switching on `MOTUZ_E2E_TARGET=docker|systemd`.

CI: `.github/workflows/ci.yml` (push and pull_request) runs the backend unit tests, the frontend build, `test/e2e/run.sh` (Docker) and the systemd install twice (`run_systemd.sh --distro=al2027` in a container and `run_systemd.sh` in an Ubuntu 26.04 VM), all with `MOTUZ_E2E_UI=require`; no secrets are needed (details: `.claude/rules/e2e-tests.md`).

Lint: `pylint` (in `requirements-dev.in`) with `.pylintrc`; no script or CI job runs it and there is no npm lint script / eslint config. Run it by hand from the repository root: `pylint --rcfile=.pylintrc src/backend/api`.

Frontend production build: `npm run build` (outputs to `./build/`). The app image builds it in a `node:22` stage and copies it to `/app/build` (`FRONTEND_DIR`).

Migrations (Flask-Migrate/Alembic; filenames are timestamp-prefixed via `alembic.ini` `file_template`):
```bash
./bin/dev/migrate.sh   # `manage.py db migrate` (autogenerate), then prompts before `db upgrade`
```
The production app container runs `manage.py db upgrade` on every start (`deployment/docker/app/app-entrypoint.sh`).

Docker alternative (`make quickstart`, `./bin/quickstart.sh`, `./bin/prod/build.sh`, `./bin/redeploy.sh`; `prod` branch is synced with `git reset --hard master` + force push): `.claude/rules/docker-deployment.md`.

## Architecture

### Backend request flow (`src/backend/api/`)
`views/*_views.py` (flask-restx `Namespace` + `api.model` DTOs, mounted under `/api` in `application.register_api`; Swagger UI at `/api/`) → `managers/*_manager.py` (business logic, decorated with `@token_required`) → `models/` and `utils/`.

- **Auth**: PAM login (`utils/pam.py`, or the root login helper with `MOTUZ_AUTH_HELPER`), JWT identity = Unix username (`JWT_IDENTITY_CLAIM = 'identity'` is API contract), every token checked against `revoked_token` (by `jti` and session id `sid`), access tokens 15 minutes, refresh tokens 30 days and rotated with a 60 s grace, tabs share tokens through localStorage. Details: `.claude/rules/auth.md`.
- **Per-user scoping**: every model has an `owner` string column. Managers filter by `owner == get_logged_in_user(request)` and return 404 (not 403) for other users' objects. Jobs resolve their `src_cloud_id`/`dst_cloud_id` through `cloud_connection_manager.owned_cloud_id` so a job can never use another user's credentials. Connection create/update only accept columns in `_WRITABLE_FIELDS` (never `id`/`owner`), and an empty value for a secret field on update keeps the stored secret.
- **DTO secrets**: credential fields use `PrivateString` in the view DTOs, so they can be written but are never returned by the API.

### Filesystem access as the logged-in user
Motuz runs as root in the containers (and as the unprivileged account `motuz` in the systemd install) but every filesystem/rclone operation runs as the logged-in user via `sudo -n [--preserve-env=<names>] -u <owner> <absolute path>`, so local permissions are enforced by the OS. The systemd install's only sudoers rule is `motuz ALL=(ALL, !root, !postgres, !redis, !valkey) NOPASSWD:SETENV: /var/lib/motuz-runas/rclone, /usr/bin/ls, /usr/bin/mkdir, /usr/bin/env`; see "Rules". Full text (sudo-rs vs classic sudo, `!log_allowed`, mounts for the containers) and the listing format: `.claude/rules/filesystem-access.md`.
- `utils/rclone_connection.py` (`RcloneConnection`): cloud ops via `rclone --config=/dev/null` and `RCLONE_CONFIG_<REMOTE>_<KEY>` env vars (remotes `src`, `dst`, `current`) from `_formatCredentials()`; `_log_command()` masks credentials.
- `utils/local_connection.py` (`LocalConnection`): same interface for the local filesystem; `connection_id` `0` (or a `None` src/dst cloud on a job) means local.
- Listings (`POST /api/system/files/`, `/files/home/`) return `{files, path}`; `modified` is always UTC (see "Rules").

### File viewers
Double-click on a file in a pane opens a read-only viewer: pager (text, with follow mode), image, Markdown, or document (PDF, DOCX, XLSX/ODS, PPTX). Full text: `.claude/rules/file-viewers.md`.
- Text is rendered, never HTML; the viewer type comes from the extension (`utils/viewerKind.js`) but the file type is decided from magic bytes, never from names or MIME types.
- Files are read as the user (`_VIEW_READER` via sudo, or rclone as the user); other users' connections are 404. Size caps: `MOTUZ_VIEW_IMAGE_MAX_BYTES`, `MOTUZ_VIEW_DOCUMENT_MAX_BYTES`.
- Binary responses carry safe headers (`nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`, `no-store`) and the app's own pages carry `frontend_views.APP_CSP`, so a renderer bug cannot fetch from another site.
- HEIC/HEIF conversion runs in a sudo'd child process (`utils/heic_convert.py`), never in the server.
- Renderers (pdf.js, docx-preview, SheetJS, markdown) are lazy webpack chunks, never in the main bundle; untrusted ZIPs are parsed in a Web Worker with limits (`utils/zipSafe.js`).
- Never log file contents.

### Credentials from the user's home directory
`utils/local_credentials.py`: the New Connection dialog can use AWS profiles, rclone remotes and Azure CLI logins found in the user's home (`GET /api/connections/local-credentials/`); a connection created from one has `subtype='profile'` and no stored credentials, and `_formatCredentials` re-reads the files each time. Files are read only as the user, never as root; rclone never gets the user's AWS files (no `credential_process`). Details: `.claude/rules/credentials-from-home.md`.

### Jobs (copy + integrity check)
1. The manager inserts a `CopyJob`/`HashsumJob` row, picks a pool (`job_routing.route`, which also returns the measured source size; remote pools are queued for workers, see "Remote workers") and, for `central`, dispatches the Celery task (`tasks/celery_tasks.py`) with the **Celery task id `copy-<id>` / `hashsum-<id>`** (`_celery_task_id`; the two job types have separate id sequences, so a bare id would let revoking copy job N discard hashsum job N). `_async_result` falls back to the bare id for jobs created before the prefix existed.
2. The worker starts rclone (`copyto` or `md5sum`) in its own process group, with background threads from `utils/copy_job_queue.py` / `utils/hashsum_job_queue.py` reading stdout (the `--progress` stats in non-TTY form, where the bytes and file-count lines are both named `Transferred:`) and stderr. The polling loops (`watch_copy`, `run_hashsum`) are in `utils/job_runner.py`, shared with the remote worker agent. A full local destination (`utils/disk_full.py`: rclone lines ending in `no space left on device`, `disk quota exceeded` or `preallocate: file too big for remaining disk space`) kills the copy's process group at once and the error text starts with the reason; rclone also gets `RCLONE_LOCAL_FATAL_IF_NO_SPACE=true` (a variable, never a flag: older rclone ignores it). An upload file failing that way fails its whole upload job and aborts the job's other files.
3. Once per second the task writes progress to the DB row and to the Celery task state `meta` (text/error text via the database result backend). Before a job starts, `_ensure_node_ready` fails it (with the reason as error text) when a mount point of `MOTUZ_REQUIRED_PATHS` (commas or colons, as for remote workers; `utils/node_check.py`) is missing; the systemd install's `motuz-celery.service` runs the same check as `ExecStartPre` and does not start without them. `node_check` also computes a node's version (backend code hash, git commit, rclone) for the future HTTPS worker API.
4. `retrieve` merges `AsyncResult(id).info` into the DB object for live text; this needs a shared result backend (`CELERY_RESULT_BACKEND = 'db+' + database URI`), since Celery 5 removed the `amqp` backend. `stop` calls `revoke(terminate=True)`; the task's SIGTERM handler (`_terminate_rclone_on_sigterm`) kills the rclone process group before the worker process exits. Email notifications go through `utils/email_utils.py`.

**Uploads from the desktop** (`managers/upload_manager.py`, `utils/upload_pipes.py`, `actions/uploadActions.jsx`, `utils/desktopDrop.js`, `utils/uploadRunner.js`): files dropped from the desktop onto a pane are a copy job with `upload` set (no Celery task, central node only) and one `upload_file` row per file; the browser sends each file in chunks (`PUT /api/copy-jobs/<id>/uploads/<index>?offset=N`, in order) into a per-file `rclone rcat --size` run as the user, whose stdin lives in the uWSGI process (one process, `wsgi.ini`), so nothing is staged on the server. A job whose browser sends nothing for `upload_pipes.IDLE_SECONDS` fails (`upload_manager.expire`); Stop closes rcat's stdin early, so rclone removes the partial file.

**AWS egress cost** (`utils/egress_cost.py`, the one price table, stdlib only; `managers/egress_manager.py`; `GET /api/copy-jobs/egress-estimate/`, `/api/hashsum-jobs/egress-estimate/`; `views/Dialogs/CopyJobEgress.jsx`, `utils/egressCost.js`): a copy out of AWS S3 (or a check with download) needs `egress_estimate: {confirmed: true, ...}` on create (else 400) and stores it; the charge depends on where the job runs (`egress_manager.runner`: `MOTUZ_CENTRAL_AWS_REGION` for this node, empty = not in AWS, so on-prem every copy out of AWS is internet egress); details: `.claude/rules/rclone-tuning.md`, README "AWS egress cost".

Hashsum jobs run md5sum on src then dst, build file trees (`utils/file_utils.py`), and store only the differing branches (`remove_identical_branches`) as JSON.

**rclone performance** (`utils/rclone_tuning.py`, stdlib only): installation defaults `MOTUZ_RCLONE_*` (unset transfers/checkers mean Motuz's 16/32, `MOTUZ_DEFAULTS`, so every copy carries `--transfers=16 --checkers=32` unless configured otherwise), caps `MOTUZ_RCLONE_MAX_*`, a memory budget, and validated per-job overrides in `copy_job.performance` / `hashsum_job.performance`; an invalid setting stops the app with a `TuningConfigError`. Details: `.claude/rules/rclone-tuning.md`.

### Remote workers (HTTPS only)
Jobs can run on remote workers that reach the central node only by outbound HTTPS (optionally via an HTTP proxy); no database access, no inbound ports. README "Remote workers (HTTPS only)" is the operator documentation.
Pools: `job_routing.route` sends jobs with a local path to `MOTUZ_LOCAL_JOB_POOL`, large cloud-to-cloud jobs to `MOTUZ_LARGE_JOB_POOL`, the rest to `central` (Celery); other pools get a `remote_job` row. Workers authenticate with their own tokens (never user JWTs), claim with `FOR UPDATE SKIP LOCKED`, hold a lease renewed by progress, and get OAuth tokens through the HTTPS broker. The agent (`src/worker/motuz_worker.py`) is stdlib only. Details: `.claude/rules/remote-workers.md`.

### Temporary EC2 workers
Off unless `MOTUZ_EC2_WORKERS=true`: each job queued for `MOTUZ_EC2_POOL` gets its own instance (`managers/ec2_launcher.py`, `manage.py ec2 reap --loop`) that runs the worker with `--once` and terminates. Details: `.claude/rules/ec2-workers.md`, README "Temporary EC2 workers", `deployment/aws/README.md`.

### OAuth token broker (OneDrive, Google Drive)
rclone cannot persist refreshed tokens, so for brokered types (`token_broker_manager.BROKERED_TYPES`: `onedrive`, `drive`) it gets the current access token plus an opaque handle as refresh token, and `token_url` points at the broker `/internal/oauth/token` on loopback port 5001 (never exposed by Traefik; 404 on any other socket). The real refresh token never leaves the server. Providers are defined in `oauth_manager.PROVIDERS`; routes `/api/oauth/<provider>/...`. Details, own apps (`MOTUZ_ONEDRIVE_CLIENT_ID` / `MOTUZ_GDRIVE_CLIENT_ID`): `.claude/rules/oauth-broker.md`.

### New Cloud Connection dialog
Top to bottom: Type, the only "Connection Name" field, the type's own part, advanced options collapsed; exactly one primary button "Create connection" in the footer; all errors once in `DialogError` above the footer. Layout rules (OneDrive/Google Drive sign-in, callbacks): `.claude/rules/connection-dialog.md`.

### Adding a field to a cloud connection type
Follow the pattern of the `kms_encryption_key_arn` commits (`8a8988f`, `0a02274`):
1. Add a column in `models/cloud_connection.py` and an Alembic migration.
2. Add it to the DTO in `views/cloud_connection_views.py` (use `PrivateString` for secrets).
3. Map it to an rclone env var in `RcloneConnection._formatCredentials`, and add its suffix to the logging allowlist if it is safe to log. A global rclone flag for one connection only (e.g. `skip_tls_verify` → `no_check_certificate`) goes through `_without_tls_verification`'s pattern: the configuration moves to `<remote>tlsoff` and the remote becomes an `alias` with `override.<flag>=...`, so the other side of a copy is unaffected and everything stays plain `RCLONE_CONFIG_*` variables (also in worker tickets).
4. Add the form field in `src/frontend/js/views/Dialogs/CloudConnection/CloudConnectionDialogFields.jsx`.

### Frontend (`src/frontend/js/`)
Webpack `resolve.modules` includes `src/frontend/js`, so imports are root-relative (`'reducers/reducers.jsx'`, `'actions/apiActions.jsx'`). API calls are `RSAA` actions (redux-api-middleware) in `actions/apiActions.jsx` with `withAuth` headers. `middleware/authMiddleware.jsx` handles token refresh. `redux-persist` keeps auth/settings in local storage. The main UI is a two-pane file browser (`views/App/Pane/`) with command bars and the job tables in `views/App/CopyJobSection/`. Each pane has Name / Age / Size columns with sortable headers. Selection, copy (arrow button, `dialogActions.buildCopyJobData`) and drag and drop work with row indexes, so `state.pane.files[side]` is always kept in the displayed (sorted) order: listings are sorted in `paneReducer` with `utils/fileSort.js` (`..` first, folders before files, names case-insensitive and natural), and a header click (`SORT_CHANGE`) re-sorts the array and moves the selection with it (`resortWithSelection`). The sort of each pane is persisted in `settings.paneSort` and copied to `state.pane.sort` on `REHYDRATE`.

### Deployment
The default is the systemd install (`bin/systemd/`, `deployment/systemd/`, user units of `motuz` grouped by `motuz.target`: `motuz-app` uWSGI, `motuz-celery`, `motuz-traefik`, and in private modes `motuz-postgres` / `motuz-redis`; PostgreSQL and the broker are private, system or external per `MOTUZ_DATABASE_MODE` / `MOTUZ_BROKER_MODE`); settings in `~/.config/motuz/motuz.env`, secrets in `secrets.env` (mode 600, read with `env_get`, never `source`d). Distribution specifics live in `bin/systemd/distro/<ID>.sh`. Details: `.claude/rules/systemd-install.md`.

The Docker stack (`docker-compose.yml`, all services `network_mode: host`: traefik, app, celery, rabbitmq, database) is the alternative: Traefik is the only thing listening beyond loopback, uWSGI serves the API and the built frontend on 127.0.0.1:5000 and the token broker on 5001. Secrets are files in `$MOTUZ_DOCKER_ROOT/secrets`. Details: `.claude/rules/docker-deployment.md`.

Rule files (`.claude/rules/`, loaded by path): `file-viewers`, `filesystem-access`, `auth`, `credentials-from-home`, `rclone-tuning`, `remote-workers`, `ec2-workers`, `oauth-broker`, `connection-dialog`, `systemd-install`, `docker-deployment`, `e2e-tests`.
