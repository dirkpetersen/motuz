---
paths:
  - "src/backend/api/managers/oauth_manager.py"
  - "src/backend/api/managers/token_broker_manager.py"
  - "src/backend/api/views/oauth_views.py"
  - "src/backend/api/views/internal_views.py"
  - "src/backend/api/models/oauth_flow.py"
  - "src/frontend/js/views/Dialogs/CloudConnection/OauthSignIn.jsx"
  - "src/frontend/js/views/Dialogs/CloudConnection/GdriveSection.jsx"
  - "test/backend/test_token_broker.py"
  - "test/backend/test_gdrive.py"
  - "test/backend/test_onedrive_app.py"
  - "test/e2e/oauth_test.py"
  - "test/e2e/broker_test.py"
  - "test/e2e/fake_ms.py"
---

# OAuth token broker and sign-in (OneDrive, Google Drive)

Detail for brokered OAuth connections.

rclone gets OAuth tokens via env vars and `--config=/dev/null`, so it can never persist a refreshed token. For brokered types (`token_broker_manager.BROKERED_TYPES`: `onedrive`, `drive`, derived from the provider definitions in `oauth_manager.PROVIDERS`) `_formatCredentials` passes the current access token plus an opaque per-connection handle (`cloud_connection.token_broker_handle`) as the refresh token, and sets the remote's `token_url` to `TOKEN_BROKER_URL`. That is `/internal/oauth/token`, served on uWSGI's loopback `http-socket` 127.0.0.1:5001. It is exposed nowhere else: Traefik's router rule excludes `/internal`, and `internal_views.only_on_broker_socket` returns 404 unless `SERVER_PORT` (the accepting socket's port, not the Host header) is the port of `TOKEN_BROKER_URL`, i.e. 5001 in production and 5000 with the dev server. The broker locks the connection row, returns the cached access token if it has more than 5 minutes left, and otherwise refreshes upstream with the real refresh token, which never leaves the server. Concurrent jobs share a single refresh. A response without `refresh_token` (Google) keeps the stored one; a new one (Microsoft rotates) replaces it. Test it against a fake token endpoint by setting `MOTUZ_ONEDRIVE_TOKEN_URL` / `MOTUZ_GDRIVE_TOKEN_URL`.

Providers: `oauth_manager.Provider` holds everything provider specific (config prefix `ONEDRIVE`/`GDRIVE` for `*_CLIENT_ID/_CLIENT_SECRET/_REDIRECT_URI/_AUTH_URL/_TOKEN_URL`, rclone's public app id + obscured secret + loopback redirect, scopes, extra auth params, drive discovery, connection columns, token and client-id columns). Routes are `/api/oauth/<provider>/{start,finish,callback,flows/<state>,connect}` with provider `onedrive` or `gdrive`; OneDrive's callback lands on `/clouds?oauth_state=...`, others add `&oauth_provider=<name>`. Google Drive (connection type `drive`, columns `gdrive_*`) uses `access_type=offline&prompt=consent` (else Google returns no refresh token) and discovers My Drive (`about`) plus shared drives (`drives`, which set rclone's `team_drive`). rclone's shared Drive client is heavily rate limited and being retired by rclone during 2026; the README ("Google Drive") describes the own client and Google's restricted-scope limits. Frontend: `OauthSignIn.jsx` is the one sign-in component for both providers (texts in `OAUTH_PROVIDERS`); `GdriveSection.jsx` holds the Drive fields for a pasted rclone token. The flow result includes the signed-in `account` (`discover(access_token, info)` sets `info['account']`; stored with the drives in `oauth_flow.drives`).

"Sign in with Microsoft/Google" (`oauth_manager`) uses rclone's public app, or an own app when `MOTUZ_ONEDRIVE_CLIENT_ID` / `MOTUZ_GDRIVE_CLIENT_ID` is set (secrets: docker secrets `MOTUZ_ONEDRIVE_CLIENT_SECRET` / `MOTUZ_GDRIVE_CLIENT_SECRET`, empty files by default; empty means unset). A refresh token only works with the app that issued it, so connections remember it in the server-controlled `cloud_connection.onedrive_client_id` / `gdrive_client_id` (NULL = rclone's app, e.g. pasted tokens; pasting a token resets it, `cloud_connection_manager._TOKEN_CLIENT_COLUMNS`). The broker's `connection_client_credentials` / `upstream_client_credentials` forwards rclone's own client credentials for rclone-app connections, substitutes the own app's id and secret (never given to rclone) for own-app connections, and refuses others with `invalid_grant`. Commands run as a user (rclone, via `sudo_as`) get only an allowlisted environment, `abstract_connection.user_process_env`: `PATH`/`LANG`/`LC_ALL`/`TZ` plus the explicit `RCLONE_CONFIG_*` values, never the server's secrets or any other `MOTUZ_*` variable, because the user can read their own process's environment (`/proc/<pid>/environ`).
