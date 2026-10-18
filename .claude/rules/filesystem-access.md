---
paths:
  - "src/backend/api/utils/abstract_connection.py"
  - "src/backend/api/utils/local_connection.py"
  - "src/backend/api/utils/rclone_connection.py"
  - "src/backend/api/utils/file_times.py"
  - "src/backend/api/managers/system_manager.py"
  - "src/backend/api/views/system_views.py"
  - "src/backend/api/managers/cloud_connection_manager.py"
  - "test/backend/test_local_connection.py"
  - "test/backend/test_rclone_connection.py"
  - "test/backend/test_file_times.py"
---

# Filesystem access as the logged-in user, connections, listings

Detail for running filesystem and rclone commands as the user. Summary and invariants are in CLAUDE.md.

Motuz runs as root in the containers (and as the unprivileged account `motuz` in the systemd install) but every filesystem/rclone operation runs as the logged-in user via `sudo -n [--preserve-env=<names>] -u <owner> <absolute path>`, so local permissions are enforced by the OS. The systemd install's only sudoers rule is `motuz ALL=(ALL, !root, !postgres, !redis, !valkey) NOPASSWD:SETENV: /var/lib/motuz-bin/rclone, /usr/bin/ls, /usr/bin/mkdir, /usr/bin/env`: every `sudo` call must use exactly one of these absolute paths (`local_connection.LS/MKDIR/ENV`, `rclone_connection.RCLONE` = `rclone_path()`: `MOTUZ_RCLONE`, which the units set to the account's `/var/lib/motuz-bin/rclone`, else `/usr/local/bin/rclone` (Docker, EC2 workers); any other value is an error; readers run `python3` through `/usr/bin/env`), never `-i`/login shells (the home directory comes from `pwd.getpwnam`, `_homepath_with_impersonation`), and never `-E`: `abstract_connection.sudo_as(user, env)` builds `sudo -n --preserve-env=<the names of user_process_env(env) except PATH> -u <user>` (sudo-rs, Ubuntu 26.04's default sudo, ignores `-E`; `--preserve-env=list` needs the rule's `SETENV`). With the classic sudo (Amazon Linux) `install.sh` adds `Defaults:motuz !log_allowed`, because that sudo logs the `--preserve-env` variables (rclone's credentials) of every allowed command. `test_systemd_support.py` checks this. That is why the containers mount host `/etc`, PAM/SSSD/Kerberos config, and the shared filesystems (the Fred Hutch-specific mounts in `docker-compose.override.yml`; these must be added to both `app` and `celery`).

- `utils/rclone_connection.py` (`RcloneConnection`) — cloud ops. It runs `rclone --config=/dev/null` and passes credentials as env vars `RCLONE_CONFIG_<REMOTE>_<KEY>` (remote names `src`, `dst`, `current`) built by `_formatCredentials()` from a `CloudConnection` row. `_log_command()` masks credentials in logs using suffix allowlists (`should_log_full_credential` / `should_log_partial_credential`).
- `utils/local_connection.py` (`LocalConnection`) — same interface for the local filesystem (`ls`/`mkdir`/home dir via sudo). A `connection_id` of `0` in the API, or a `None` src/dst cloud on a job, means local filesystem.
- User-supplied paths must never be parsed as options: local commands put `--` before the path, and local rclone paths must be absolute (`_local_path`).
- **Listings** (`POST /api/system/files/`, `/files/home/`) return `{files, path}`. Local entries are `{name, type, size, modified}` (`type` `file`/`dir`/`symlink` = a broken link/`unknown`; `size` int or null) parsed by `_parse_ls` from `ls -a -l -L -g -o --time-style=+%s`; cloud entries are rclone's `lsjson` objects (`Name`, `Size`, `IsDir`, `ModTime`, ...) plus `modified` (`fileManager.convertRcloneFilesToMotuz` maps them). **`modified` is always UTC: ISO 8601 with `Z` and whole seconds (`2026-09-27T04:57:00Z`), or null** (`utils/file_times.py`). Local times come from epoch seconds, so the server's `TZ` never matters; rclone's `ModTime` (any offset) is converted to UTC; placeholder times before 1971 (Go's zero time) and rclone's 2000-01-01 for bucket directories (S3 prefixes, `_with_modified`) become null. Never return naive local times. The panes compute the age ("5 min ago") from that instant against `Date.now()` (`utils/fileAge.js`, re-rendered every 30 s without refetching) and show the exact time in the browser's time zone, with the zone's name, as the tooltip.
