---
paths:
  - "src/backend/api/views/auth_views.py"
  - "src/backend/api/managers/auth_manager.py"
  - "src/backend/api/utils/pam.py"
  - "src/backend/api/utils/auth_helper.py"
  - "src/backend/api/config.py"
  - "deployment/systemd/auth-helper/**"
  - "src/frontend/js/middleware/authMiddleware.jsx"
  - "src/frontend/js/reducers/authReducer.jsx"
  - "src/frontend/js/actions/authActions.jsx"
  - "test/backend/test_auth_*.py"
---

# Authentication, tokens, login helper

Detail for authentication. Summary is in CLAUDE.md ("Backend request flow").

- **Auth**: `/api/auth/login/` checks username/password with PAM (`utils/pam.py`, service `MOTUZ_PAM_SERVICE`, default `login`; `motuz` in the systemd install) against the host's users, or, when `MOTUZ_AUTH_HELPER` is set (systemd install with `install.sh --local-accounts`), through the root login helper on that unix socket (`utils/auth_helper.py` client; `deployment/systemd/auth-helper/motuz_auth_helper.py`, socket activated `motuz-auth.socket`/`motuz-auth@.service`, one JSON request `{"user","password"}` of at most 4 KiB, answer `OK`/`NO`; it refuses root, uids below `UID_MIN`, `nobody` and its client account, locks a user out after 5 failures in 15 minutes, checks the peer with `SO_PEERCRED`, never logs passwords; tests in `test_auth_helper.py`), because an unprivileged `pam_unix` can only check its own account's password. `auth_manager._login_refused` refuses root and the account the server runs as. The JWT identity is the Unix username. `JWT_IDENTITY_CLAIM = 'identity'` is part of the API contract (the frontend reads `state.access.identity`). Every token is checked against the `revoked_token` table, by its `jti` and by its session id (claim `sid`, shared by all tokens of one login). Access tokens live 15 minutes, refresh tokens 30 days (`JWT_*_TOKEN_EXPIRES` in `config.py`). `/auth/refresh/` rotates the refresh token: the old one keeps working for `REFRESH_GRACE_SECONDS` (60, `revoked_token.grace_until`) so tabs refreshing at the same moment both succeed, and reusing it later revokes the whole session. Logout revokes the refresh token and the session, i.e. every access token of that login. Tabs share tokens through localStorage; `middleware/authMiddleware.jsx` adopts tokens other tabs write (`storage` event) and re-reads them before refreshing and when a refresh fails, so a tab never uses a rotated refresh token. Postponed API calls resolve with their real result after a refresh.
