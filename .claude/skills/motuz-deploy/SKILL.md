---
name: motuz-deploy
description: Deploy or update Motuz (systemd install, no Docker) as the motuz account, step by step with the administrator. Use only when the administrator runs /motuz-deploy on a machine where bin/systemd/install.sh was already run as root.
disable-model-invocation: true
---

# Deploy Motuz with the systemd install (playbook)

You are running as the unprivileged service account `motuz` on the machine that will run
Motuz, in its checkout `~/motuz`. An administrator is watching and answers your questions.
Follow this playbook in order. README sections "Install" and "Deploy with an AI assistant
(Claude Code)" are the reference.

## Ground rules (read first)

- **This account is powerful.** Its sudoers rule lets it run `rclone`, `ls`, `mkdir` and
  `env` as any user except root. Never use `sudo`, `su`, `machinectl`, `systemd-run` or any
  other way to become another user, and never try to work around a denied command or
  permission. If something needs root, **stop and tell the administrator exactly what to run
  as root**, then wait.
- **Secrets never pass through you.** Do not read, print, echo, log or commit
  `~/.config/motuz/secrets.env`, `broker.env`, `handoff.env`, `redis.conf`, private keys, API
  keys or tokens. For a secret, tell the administrator to run in this session
  `! bin/systemd/config.sh ask-secret MOTUZ_<NAME>` (the `!` runs it in their terminal; the
  value is typed hidden and goes straight to `secrets.env`). Never ask them to paste a
  secret into the chat. Non-secret settings: `bin/systemd/config.sh set KEY VALUE`;
  `bin/systemd/config.sh show` lists the settings and only says whether secrets are set.
- Do not edit files outside `~/motuz` and `~/.config/motuz`, do not change git remotes, do
  not run installers (`curl ... | bash`), do not open ports, do not touch other users' files.
- Ask before each step that changes something and explain it in one sentence. One question
  at a time, with the default. Never invent values (host names, client ids, mount paths).
- Tell the administrator when you are done and that Claude Code should now be closed (and
  `claude logout` run if the organization prefers) because this account can act as every
  user.

## Step 0. Check the starting point

Run (read-only): `id`, `pwd`, `git -C ~/motuz status --short`, `git -C ~/motuz log -1 --oneline`,
`ls /etc/os-release /var/lib/motuz-bin`,
`systemctl --user status 'motuz*' --no-pager` and `bin/systemd/config.sh show` (if
`~/.config/motuz` exists). Report: distribution, Motuz version (git commit), whether
Motuz already runs (update) or not (first deployment), and the database and broker modes
(`MOTUZ_DATABASE_MODE`, `MOTUZ_BROKER_MODE`: `system` = the distribution's services,
`private` = this account's own, from `install.sh`). If rclone, Traefik or uv are missing:
**stop**, root must run `sudo <clone of Motuz>/bin/systemd/install.sh` (README, "Root steps
without install.sh"). Never run `install.sh` yourself.

## Step 1. Ask for what you cannot know

Ask these one by one; skip what the administrator says is not wanted. Write the answers
with `config.sh set` (settings) and `config.sh ask-secret` (secrets, by the administrator).

1. **Public host name** users will open (for example `motuz.example.org`). Needed for
   Let's Encrypt and OAuth redirect URIs. DNS is the administrator's job.
2. **HTTPS certificate**:
   - Let's Encrypt (port 80 must be reachable from the internet and DNS must point here):
     `config.sh set MOTUZ_ACME_DOMAIN <host>` and `config.sh set MOTUZ_ACME_EMAIL <mail>`.
   - Their own certificate: tell them to put `cert.crt` and `cert.key` into
     `~/data/certs/` (mode 600 for the key) themselves; you do not handle the key.
   - Neither: the self-signed certificate that `deploy.sh` makes (browsers warn).
3. **Accounts**: SSSD/Kerberos (Active Directory; nothing to do here, but the machine must be
   joined by root) or local `/etc/shadow` accounts (needs the login helper from
   `install.sh --local-accounts`; `deploy.sh` enables it when `/run/motuz-auth.sock` exists).
   Check `ls /run/motuz-auth.sock` and tell the administrator if root must re-run
   `install.sh --local-accounts`.
4. **Shared filesystems**: which mount points users should see
   (`config.sh set MOTUZ_REQUIRED_PATHS /fh/fast,/fh/scratch`, commas). They must be
   mounted at the same paths on this host (root's job); the worker refuses to start while
   one is missing, which is the point.
5. **OneDrive / Google Drive sign-in** with an own app (optional; rclone's shared app works
   but is rate limited): client id `MOTUZ_ONEDRIVE_CLIENT_ID` / `MOTUZ_GDRIVE_CLIENT_ID`,
   redirect URI `MOTUZ_ONEDRIVE_REDIRECT_URI` / `MOTUZ_GDRIVE_REDIRECT_URI`
   (`https://<host>/api/oauth/onedrive/callback`, `.../gdrive/callback`) with `config.sh set`;
   the client secret by the administrator: `! bin/systemd/config.sh ask-secret MOTUZ_ONEDRIVE_CLIENT_SECRET`
   (or `MOTUZ_GDRIVE_CLIENT_SECRET`). README "OneDrive: own app registration" and "Google Drive".
6. **Operator for /privacy and /terms**: `MOTUZ_OPERATOR_NAME`, `MOTUZ_CONTACT_EMAIL`,
   `MOTUZ_OPERATOR_URL`. Required for Google's OAuth consent screen.
7. **Job emails** (optional): `MOTUZ_SMTP_SERVER` (host:port), `MOTUZ_SMTP_USER`,
   `MOTUZ_SMTP_REQUIRE_SSL`, `MOTUZ_ALERT_ADDRESS`; password by
   `! bin/systemd/config.sh ask-secret MOTUZ_SMTP_PASSWORD`.
8. **Central node of remote or EC2 workers?** If yes, ask for `MOTUZ_PUBLIC_URL`
   (`https://<host>`), `MOTUZ_LOCAL_JOB_POOL` / `MOTUZ_LARGE_JOB_POOL`, and for EC2 workers
   the `MOTUZ_EC2_*` settings; point to README "Remote workers (HTTPS only)" and "Temporary
   EC2 workers" and `deployment/aws/README.md` (IAM and launch templates are the AWS
   administrator's job, not yours). Do not enable them without explicit answers.
9. **Performance defaults** (optional): `MOTUZ_RCLONE_TRANSFERS` etc. only if they have
   numbers; otherwise rclone's defaults.

Show the administrator `bin/systemd/config.sh show` afterwards and ask them to confirm.

## Step 2. Deploy

Run `bin/systemd/deploy.sh` (add `--pull` for an update; it does `git pull --ff-only`).
It builds the venv and the frontend (several minutes), writes the configuration, prepares
the database and the broker according to their modes, installs the units and starts
`motuz.target`, then checks `https://127.0.0.1/api/system/info/`. If it warns that
something installed by root differs from the checkout (rclone, Traefik, `/etc/pam.d/motuz`,
the login helper) or says "run install.sh as root", **stop** and give the administrator the
exact root command. If it fails, read its last lines and `systemctl --user status 'motuz*'`;
fix only what is in `~/.config/motuz/motuz.env` (via `config.sh`) or the checkout, then run it
again. Do not change units or scripts to make an error go away.

## Step 3. Verify (report each result as pass or fail)

1. `systemctl --user status 'motuz*' --no-pager`: app, celery, traefik active; postgres and
   redis units active only in the private modes.
2. `curl -sk https://127.0.0.1/api/system/info/` contains `healthy`.
3. Listeners: `ss -ltn` shows only 80 and 443 beyond loopback (22 is the administrator's
   sshd); PostgreSQL (5432) and the app (5000, 5001) are on 127.0.0.1 only; no 6379.
4. `MOTUZ_DATABASE_MODE=system` or `private` as expected, and `deploy.sh` ended with
   "Motuz is up".
5. Login and a small copy job through the API, with a test user the administrator names.
   You cannot create files as another user (no sudo): ask for a path that user can read and
   write, e.g. `/home/<user>/motuz-test-src` (a folder with a file; the administrator
   creates it) and a destination `/home/<user>/motuz-test-dst`. The administrator types the
   user's password, hidden, with `! bin/systemd/config.sh ask-test-password` (it needs a
   terminal and says so if there is none; then they run it in a terminal of the account
   instead). Then run `bin/systemd/smoke_test.py <user> <src> <dst>` (it reads only
   `~/.motuz-test-pw`, talks only to this host, copies with `copy_links`, waits for SUCCESS
   and prints PASS/FAIL, never the password) and ask the administrator to look at the copied
   files. Afterwards the administrator removes the password file: `! rm ~/.motuz-test-pw` (the
   settings deny you that path).
6. Logs without secrets: `systemctl --user status motuz-app motuz-celery --no-pager -n 30`
   shows no passwords, tokens or `RCLONE_CONFIG_` values. The system journal needs an
   administrator: ask them to run, as root,
   `journalctl _SYSTEMD_USER_UNIT=motuz-app.service -n 200 | grep -ciE 'password|secret|token='`
   (expected: only lines about settings names, no values) and tell you the outcome.

## Step 4. Hand over

Report: what you set (names, never secret values), what runs, the URL, and **what root
still has to do** (do not try yourself): open 80/tcp and 443/tcp in the firewall or
security group; DNS for the host name; join the machine to the directory (`realm join`) or
create local users; mount the shared filesystems at the required paths; a CA-signed
certificate if wanted; for central nodes of EC2 workers the IAM policies and launch
templates; backups (README "Backups and upgrades"); re-running
`sudo bin/systemd/install.sh` when `deploy.sh` warns after an update. Remind the administrator to
exit Claude Code now and to run `claude logout` / delete `~/.claude` if the organization
prefers, and to re-enable the sudoers rule if they disabled it for the session.
