# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Motuz is a web UI + REST API for large data transfers between on-prem filesystems and cloud storage (S3, Azure Blob, Swift, GCS, SFTP, Dropbox, OneDrive, WebDAV). All actual data movement is done by shelling out to `rclone`. Backend: Python 3.12, Flask 3 + flask-restx + Flask-SQLAlchemy 3 / SQLAlchemy 2 (psycopg 3) + Alembic + Celery 5 (RabbitMQ broker, results stored in the database). Frontend: React 18 + Redux + redux-api-middleware 3 + react-router 5, bundled with webpack 5 (Node >= 22.15).

## Commands

Dependencies: top-level Python deps live in `requirements.in` / `requirements-dev.in`; the pinned `requirements*.txt` are generated with `uv pip compile` (command in the `requirements.in` header). The Docker image installs only `requirements.txt`.

Dev setup (creates `venv/`, installs Python/npm deps, initializes the DB via docker, runs migrations):
```bash
./bin/dev/init_dev.sh
```

Dev run — each in its own terminal; UI at http://localhost:8080 (webpack-dev-server proxies `/api` and `/swaggerui` to Flask on :5000):
```bash
./bin/dev/database_start.sh   # postgres:11.3 in docker, data in $MOTUZ_DOCKER_ROOT/volumes/postgres
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

Lint: `pylint` using `.pylintrc` (no npm lint script / eslint config).

Frontend production build: `npm run build` (outputs to `./build/`).

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

### Jobs (copy + integrity check)
1. The manager inserts a `CopyJob`/`HashsumJob` row and dispatches the Celery task (`tasks/celery_tasks.py`) with the **Celery task id set to the DB id as a string**.
2. The worker starts rclone (`copyto` or `md5sum`) in its own process group, with background threads from `utils/copy_job_queue.py` / `utils/hashsum_job_queue.py` reading stdout (the `--progress` stats in non-TTY form, where the bytes and file-count lines are both named `Transferred:`) and stderr.
3. Once per second the task writes progress to the DB row and to the Celery task state `meta` (text/error text via RabbitMQ result backend).
4. `retrieve` merges `AsyncResult(id).info` into the DB object for live text; this needs a shared result backend (`CELERY_RESULT_BACKEND = 'db+' + database URI`), since Celery 5 removed the `amqp` backend. `stop` calls `revoke(terminate=True)`; the task's SIGTERM handler (`_terminate_rclone_on_sigterm`) kills the rclone process group before the worker process exits. Email notifications go through `utils/email_utils.py`.

Hashsum jobs run md5sum on src then dst, build file trees (`utils/file_utils.py`), and store only the differing branches (`remove_identical_branches`) as JSON.

### Adding a field to a cloud connection type
Follow the pattern of the `kms_encryption_key_arn` commits (`8a8988f`, `0a02274`):
1. Add a column in `models/cloud_connection.py` and an Alembic migration.
2. Add it to the DTO in `views/cloud_connection_views.py` (use `PrivateString` for secrets).
3. Map it to an rclone env var in `RcloneConnection._formatCredentials`, and add its suffix to the logging allowlist if it is safe to log.
4. Add the form field in `src/frontend/js/views/Dialogs/CloudConnection/CloudConnectionDialogFields.jsx`.

### Frontend (`src/frontend/js/`)
Webpack `resolve.modules` includes `src/frontend/js`, so imports are root-relative (`'reducers/reducers.jsx'`, `'actions/apiActions.jsx'`). API calls are `RSAA` actions (redux-api-middleware) in `actions/apiActions.jsx` with `withAuth` headers. `middleware/authMiddleware.jsx` handles token refresh. `redux-persist` keeps auth/settings in local storage. The main UI is a two-pane file browser (`views/App/Pane/`) with command bars and the job tables in `views/App/CopyJobSection/`.

### Deployment
All services in `docker-compose.yml` use `network_mode: host`: nginx (TLS, certs from `$MOTUZ_DOCKER_ROOT/certs`, security headers in `security_headers.conf` included in every `location` because `add_header` does not inherit), app (uWSGI on `127.0.0.1:5000` only; nginx is the only way in; `PYTHON_ENVIRONMENT=prod`), celery (image built `FROM fredhutch/motuz_app`, so rebuild app first), rabbitmq, postgres. Secrets are files in `$MOTUZ_DOCKER_ROOT/secrets` (default `/docker`), exposed as docker secrets and exported to env by `deployment/docker/load-secrets.sh`. The rclone version and its SHA256 are pinned in `deployment/docker/app/Dockerfile`. PostgreSQL stays on major version 11 because the data volume would need `pg_upgrade`/dump-restore to move to a newer major. The app runs `manage.py db upgrade` on each start. `.gitlab-ci.yml` is a placeholder; there is no real CI.
