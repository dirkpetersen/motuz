---
paths:
  - "docker-compose*.yml"
  - "deployment/docker/**"
  - "bin/prod/**"
  - "bin/_utils/**"
  - "bin/quickstart.sh"
  - "bin/redeploy.sh"
  - "Makefile"
---

# Docker deployment

Detail for the Docker stack (the alternative to the systemd install).

Alternative, Docker (docker-compose): `make quickstart` / `./bin/quickstart.sh` (first-time bootstrap of `/docker` certs/secrets/volumes), `./bin/prod/build.sh`, `./bin/redeploy.sh` (rebuild + restart). Production deploys from the `prod` branch, which is synced by `git reset --hard master` + force push rather than merging.

The production app container runs `manage.py db upgrade` on every start (`deployment/docker/app/app-entrypoint.sh`).

The default is the systemd install (below); the Docker stack is the alternative. All services in `docker-compose.yml` use `network_mode: host`:
- **traefik** (`traefik:v3.7`, container `motuz_traefik`): the only thing listening beyond loopback (:80 redirects to :443). File provider only, no docker socket, no dashboard/API. Static config is the service's `command` flags (Traefik takes static config from exactly one source, and flags let compose substitute `MOTUZ_ACME_*` from `.env`); dynamic config is `deployment/docker/traefik/dynamic/motuz.yml`, a Go template: one router for everything except `/internal`, security-headers middleware, `responseHeaderTimeout: 600s` and no connection reuse to uWSGI, TLS 1.2+ with AEAD ciphers. TLS: `$MOTUZ_DOCKER_ROOT/certs/cert.{crt,key}` by default (reloaded only on `docker restart motuz_traefik`), or Let's Encrypt HTTP-01 when `MOTUZ_ACME_DOMAIN` is set (`acme.json` in `$MOTUZ_DOCKER_ROOT/traefik`).
- **app** (`PYTHON_ENVIRONMENT=prod`): uWSGI `http-socket`s on `127.0.0.1:5000` (for Traefik) and `127.0.0.1:5001` (token broker), `src/backend/wsgi.ini`. Besides the API it serves the built frontend (`views/frontend_views.py`, via `wsgi.file_wrapper`/sendfile): `/js`, `/css` cached 30 days, `/img` 7 days, `/about` (public homepage for OAuth reviewers), `/privacy` and `/terms` are server-rendered public pages (`views/legal_views.py`, templates in `api/templates/legal/`, operator from `MOTUZ_OPERATOR_NAME` / `MOTUZ_CONTACT_EMAIL` / `MOTUZ_OPERATOR_URL`; required for Google's OAuth consent-screen branding), anything else not under `/api`, `/swaggerui`, `/internal` gets `index.html` with `no-store` (SPA routes). Traefik only terminates TLS and routes because it cannot serve files, and this uWSGI build has no PCRE, so `static-map` could not set per-path Cache-Control. `wsgi.py` wraps the app in `ProxyFix(x_for=1, x_proto=1)` only (never host/port, see the broker check).
- **celery** (image built `FROM fredhutch/motuz_app`, so rebuild app first; with `MOTUZ_EC2_WORKERS=true` its entrypoint also runs the EC2 reaper loop), **rabbitmq**, **database** (postgres).

Secrets are files in `$MOTUZ_DOCKER_ROOT/secrets` (default `/docker`), exposed as docker secrets and exported to env by `deployment/docker/load-secrets.sh`. Compose refuses to start when a secret file is missing, so optional secrets are created as empty files by `bin/_utils/optional_secrets.sh` (run by `bin/prod/start.sh` and `bin/quickstart.sh`). The rclone version and its SHA256 are pinned in `deployment/docker/app/Dockerfile`. PostgreSQL is 18 (`PGDATA` is set to `/var/lib/postgresql/data` because the 18 images changed the default). When the `database` image major version is bumped, `bin/_utils/upgrade_postgres.sh` (run by `bin/prod/start.sh` and `bin/dev/database_start.sh`) migrates `$MOTUZ_DOCKER_ROOT/volumes/postgres` with dump/restore, verifies row counts and keeps the old directory as `postgres.pg<old>.<timestamp>`. The database must be owned by `motuz_user` (PostgreSQL 15+ public schema rules). The app runs `manage.py db upgrade` on each start. `bin/prod/start.sh` runs `docker-compose down --remove-orphans`, which also removes the `motuz_nginx` container of pre-Traefik installs (README, "Migrating from nginx to Traefik"). `.gitlab-ci.yml` is a placeholder; CI runs on GitHub Actions (see Commands).
