---
paths:
  - "src/backend/api/utils/local_credentials.py"
  - "src/frontend/js/views/Dialogs/CloudConnection/LocalCredentialPicker.jsx"
  - "test/backend/test_local_credentials.py"
---

# Credentials from the user's home directory

Detail for connections created from AWS profiles, rclone remotes and Azure CLI logins.

`utils/local_credentials.py`. The New Connection dialog (`LocalCredentialPicker.jsx`) calls `GET /api/connections/local-credentials/?type=s3|azureblob`, which lists AWS profiles (`~/.aws/credentials`, `~/.aws/config`, SSO login state from `~/.aws/sso/cache`) s3/azureblob remotes in `~/.config/rclone/rclone.conf` (or `~/.rclone.conf`), and Azure CLI logins (one per tenant, from `~/.azure/azureProfile.json`). It returns metadata only (name, kind, region, key id masked to the last 4, Azure account, `usable`/`reason`). A connection created from one has `subtype='profile'` plus `profile_source` (`aws`/`rclone`/`azure-cli`) and `profile_name` (the tenant id for `azure-cli`, which also keeps the non-secret `azure_account`: a login is not a storage account), and no stored credentials. `_formatCredentials` calls `local_credentials.resolve()`, which re-reads the files every time rclone runs (rotated keys are picked up).
- Files are read only as the user: `sudo -n -u <user> -- env python3 -I -S -c <reader>` with fixed relative paths below `_homepath_with_impersonation`, non-blocking open, regular files only, 1 MiB cap, timeout. Never read them as root.
- rclone never gets the user's AWS files: static keys, session tokens and roles (`role_arn` + a `source_profile` with keys, via rclone's `role_arn`) are passed as `RCLONE_CONFIG_*` values. With the raw files (env_auth) the AWS SDK would run `credential_process`, i.e. users could run programs in the containers. SSO profiles use env_auth with a config file Motuz writes from allowlisted SSO keys (`MOTUZ_SSO_CONFIG_DIR`, default `/tmp/motuz-aws-config/<sha256>.ini`, `/var/lib/motuz-aws-config` in the systemd install where `/tmp` is shared with every user; no secrets) and `HOME` set to the user's home explicitly. `credential_process`, MFA roles, `credential_source`, rclone `env_auth`/`use_msi` are reported as unusable.
- Azure CLI logins (and rclone remotes with `use_az`) need `az` in the image: build arg `INSTALL_AZURE_CLI=true` (`MOTUZ_INSTALL_AZURE_CLI` in `.env`, off by default, +590 MB); otherwise they are listed as unusable. rclone runs `az account get-access-token` as the user with `azure_cli_env()`: `HOME`/`AZURE_CONFIG_DIR` at the user's `~/.azure` (az refreshes its token cache there, as the user), az extensions and dynamic installs off (they are Python code from `~/.azure`), managed identity pointed at a closed port and managed-identity logins refused (they would be the server's identity). The az launcher runs Python with `-I`, so `~/.local` site-packages are ignored. Never run `az` as root with a user's `~/.azure`.
