#!/usr/bin/env bash
# Security checks of the systemd install (run by test/e2e/run_systemd.sh inside the VM,
# as root, after the other suites): which processes run as root, the service account's
# sudo rights, the listening ports, file modes, and the login helper's refusals and rate
# limit. Prints PASS/FAIL lines and "<passed>/<total> passed" like the other suites.
set -u
pass=0; fail=0
check() { # name, command...
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then echo "PASS $name"; pass=$((pass+1)); else echo "FAIL $name"; fail=$((fail+1)); fi
}
ACCOUNT=motuz
UID_M=$(id -u "$ACCOUNT")
HOME_M=$(getent passwd "$ACCOUNT" | cut -d: -f6)
as_motuz() { runuser -u "$ACCOUNT" -- "$@"; }
# a value of motuz.env / secrets.env (KEY="value" or KEY=value)
envval() { sed -n "s/^$2=\"\{0,1\}\([^\"]*\)\"\{0,1\}\$/\1/p" "$HOME_M/.config/motuz/$1" | tail -1; }
DB_MODE=$(envval motuz.env MOTUZ_DATABASE_MODE); BROKER_MODE=$(envval motuz.env MOTUZ_BROKER_MODE)
DB_PORT=$(envval motuz.env MOTUZ_DATABASE_HOST); DB_PORT=${DB_PORT##*:}
REDIS_SOCK=$(envval motuz.env MOTUZ_REDIS_SOCKET)
export DB_PW=$(envval secrets.env MOTUZ_DATABASE_PASSWORD) REDIS_PW=$(envval secrets.env MOTUZ_REDIS_PASSWORD) DB_PORT REDIS_SOCK
echo "    database: $DB_MODE, broker: $BROKER_MODE"

# --- processes
echo "    root processes outside system services (informational):"
ps -eo uid=,pid=,comm=,cgroup= | awk '$1 == 0 && $4 !~ /(system\.slice|init\.scope)/ && $4 != "-" && $4 != "" {print "      " $0}'
check "no root process in $ACCOUNT's user manager (user@$UID_M.service)" \
    bash -c "! ps -eo uid=,cgroup= | awk '\$1 == 0' | grep -q 'user@$UID_M.service'"
check "the Motuz services run as $ACCOUNT" bash -c "
    for c in traefik uwsgi celery; do
        ps -eo user=,args= | grep -v grep | grep -qE \"^$ACCOUNT .*\$c\" || exit 1
    done
    [ $DB_MODE != private ] || ps -eo user=,args= | grep -v grep | grep -qE \"^$ACCOUNT .*postgres\" || exit 1
    [ $BROKER_MODE != private ] || ps -eo user=,args= | grep -v grep | grep -qE \"^$ACCOUNT .*(redis|valkey)-server\" || exit 1
    ! ps -eo user=,args= | grep -v grep | grep -E '^root ' | grep -qE \"(/var/lib/motuz-bin/traefik|(redis|valkey)-server|postgres -D|uwsgi|celery)\""
check "the login helper only runs as root on demand (socket activated, no daemon)" \
    bash -c "systemctl is-active --quiet motuz-auth.socket && ! ps -eo args= | grep -q '^/usr/bin/python3 -I -S /usr/local/lib/motuz-auth/'"

# --- sudo
RULE=$(sudo -l -U "$ACCOUNT" 2>/dev/null | sed -n '/may run the following commands/,$p' | tail -n +2 | sed 's/^ *//')
echo "    sudo -l -U $ACCOUNT: $RULE"
check "sudo -l -U $ACCOUNT shows exactly the one rule" \
    bash -c "[ \"\$(printf '%s\n' \"$RULE\" | grep -c .)\" = 1 ] && printf '%s' \"$RULE\" | grep -qE '^\(ALL, !root, !postgres, !redis, !valkey\) (SETENV: NOPASSWD:|NOPASSWD: ?SETENV:) /var/lib/motuz-bin/rclone, /usr/bin/ls, /usr/bin/mkdir, /usr/bin/env$'"
check "rclone, Traefik and uv are the account's (/var/lib/motuz-bin, root's directory entry), runnable by everyone" \
    bash -c "[ \"\$(stat -c %U:%a /var/lib/motuz-bin)\" = $ACCOUNT:755 ] && for f in rclone traefik uv; do [ \"\$(stat -c %U:%a /var/lib/motuz-bin/\$f)\" = $ACCOUNT:755 ] || exit 1; done && [ \"\$(stat -c %U /var/lib)\" = root ]"
check "$ACCOUNT cannot sudo -u postgres, redis or valkey (the superusers of the system services)" \
    bash -c "! runuser -u $ACCOUNT -- sudo -n -u postgres /usr/bin/env true && ! runuser -u $ACCOUNT -- sudo -n -u redis /usr/bin/env true && ! runuser -u $ACCOUNT -- sudo -n -u valkey /usr/bin/env true"
check "$ACCOUNT cannot sudo -u root" bash -c "! runuser -u $ACCOUNT -- sudo -n -u root /usr/bin/ls /root"
check "$ACCOUNT cannot sudo -u '#0'" bash -c "! runuser -u $ACCOUNT -- sudo -n -u '#0' /usr/bin/ls /root"
check "$ACCOUNT cannot sudo -u '#-1' (CVE-2019-14287)" bash -c "! runuser -u $ACCOUNT -- sudo -n -u '#-1' /usr/bin/id"
check "$ACCOUNT cannot sudo -u '#4294967295'" bash -c "! runuser -u $ACCOUNT -- sudo -n -u '#4294967295' /usr/bin/id"
check "$ACCOUNT cannot sudo without -u (root)" bash -c "! runuser -u $ACCOUNT -- sudo -n /usr/bin/id"
check "$ACCOUNT cannot run other commands as a user" bash -c "! runuser -u $ACCOUNT -- sudo -n -u alice /usr/bin/id"
check "$ACCOUNT cannot get a shell as a user" bash -c "! runuser -u $ACCOUNT -- sudo -n -i -u alice true"
check "$ACCOUNT runs the allowed commands as alice" \
    bash -c "[ \"\$(runuser -u $ACCOUNT -- sudo -n -u alice /usr/bin/env id -un)\" = alice ]"
check "sudo never logged rclone's credentials (ENV=RCLONE_CONFIG_...)" \
    bash -c "! journalctl --no-pager -o cat _COMM=sudo | grep -q 'RCLONE_CONFIG_'"
check "$ACCOUNT has no password (locked)" bash -c "passwd -S $ACCOUNT | awk '{print \$2}' | grep -qE '^(L|LK)$'"

# --- root never follows symlinks the account controls (install.sh --worker-only makes the
# account's worker files; the system PostgreSQL / Redis handoff is root's own file)
SYM=/var/lib/motuz-symtest
REPO_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
rm -rf "$SYM"; mkdir -m 755 "$SYM"
mv "$HOME_M/.config" "$HOME_M/.config.real"
ln -s "$SYM" "$HOME_M/.config"
"$REPO_ROOT/bin/systemd/install.sh" --worker-only > /tmp/symlink-install.log 2>&1; rc=$?
rm -f "$HOME_M/.config"; mv "$HOME_M/.config.real" "$HOME_M/.config"
check "install.sh does not follow a symlinked ~/.config (the target stays root:root 755 and empty)" \
    bash -c "[ \"\$(stat -c '%U:%G %a' $SYM)\" = 'root:root 755' ] && [ -z \"\$(ls -A $SYM)\" ] && [ $rc != 0 ]"
rm -rf "$SYM"
if [ -e /etc/motuz/handoff-$ACCOUNT.env ]; then
    check "the handoff file is root's, group $ACCOUNT, mode 640" \
        bash -c "[ \"\$(stat -c '%U:%G %a' /etc/motuz/handoff-$ACCOUNT.env)\" = 'root:$ACCOUNT 640' ]"
    check "install.sh left no handoff file inside the account's home" bash -c "! ls $HOME_M/.config/motuz | grep -q handoff"
fi

# --- ports and files
echo "    listening TCP sockets:"
ss -ltnpH | awk '{print "      " $4, $6}'
check "only 80 and 443 listen beyond loopback (besides the VM's sshd on 22)" \
    bash -c "! ss -ltnH | awk '{print \$4}' | grep -vE '^(127\.|\[::1\]|\[::ffff:127\.)' | grep -vE ':(80|443|22)$' | grep -q ."
check "Traefik ($ACCOUNT) listens on 80 and 443" bash -c "ss -ltnpH | grep -E ':(80|443) ' | grep -c traefik | grep -qx 2"
check "no UDP listeners of $ACCOUNT" bash -c "! ss -lunpH | grep -qE 'users:.*(redis|valkey|postgres|uwsgi|celery|traefik)'"
check "Redis/Valkey has no TCP port (unix socket only)" bash -c "! ss -ltnpH | grep -qE 'redis|valkey'"
if [ "$BROKER_MODE" = system ]; then
    check "system Redis: the socket is 770 redis:redis and $ACCOUNT is in the group redis" \
        bash -c "[ \"\$(stat -c '%a %U:%G' \$REDIS_SOCK)\" = '770 redis:redis' ] && id -nG $ACCOUNT | tr ' ' '\n' | grep -qx redis"
    check "system Redis: another local user cannot connect to the socket (EACCES)" \
        bash -c "runuser -u alice -- python3 -I -S -c 'import socket,os; s=socket.socket(socket.AF_UNIX); s.connect(os.environ[\"REDIS_SOCK\"])' 2>&1 | grep -q 'Permission denied'"
    check "system Redis: no unauthenticated access (the default user is off)" \
        bash -c "redis-cli -s \$REDIS_SOCK ping 2>&1 | grep -qE 'NOAUTH|WRONGPASS|Authentication required|NOPERM'"
    check "system Redis: the ACL user motuz works but cannot CONFIG, FLUSHALL, KEYS or SHUTDOWN" \
        bash -c "set -f; export REDISCLI_AUTH=\$REDIS_PW; for c in 'CONFIG GET dir' FLUSHALL 'KEYS *' SHUTDOWN; do redis-cli --user motuz -s \$REDIS_SOCK \$c 2>&1 | grep -q NOPERM || exit 1; done; [ \"\$(redis-cli --user motuz -s \$REDIS_SOCK ping)\" = PONG ]"
    check "system Redis: the password is neither in /etc/redis/motuz.conf (a hash) nor on a command line" \
        bash -c "pat=\"[\${REDIS_PW:0:1}]\${REDIS_PW:1}\"; ! grep -q \"\$pat\" /etc/redis/motuz.conf && ! ps -eo args= | grep -q \"\$pat\""
    check "system Redis: the append-only file is on" bash -c "grep -q '^appendonly yes' /etc/redis/motuz.conf"
fi
check "~/.config/motuz is 700, secrets.env and broker.env 600" \
    bash -c "[ \$(stat -c %a $HOME_M/.config/motuz) = 700 ] && [ \$(stat -c %a $HOME_M/.config/motuz/secrets.env) = 600 ] && [ \$(stat -c %a $HOME_M/.config/motuz/broker.env) = 600 ]"
check "the home is not world readable" bash -c "[ \$(( 0\$(stat -c %a $HOME_M) & 7 )) = 0 ]"
[ "$DB_MODE" != private ] || check "~/data/pg is 700" bash -c "[ \$(stat -c %a $HOME_M/data/pg) = 700 ]"
if [ "$DB_MODE" = system ]; then
    check "system PostgreSQL: listens on loopback only" \
        bash -c "! ss -ltnH | awk '{print \$4}' | grep -E ':\$DB_PORT\$' | grep -vE '^(127\.|\[::1\])' | grep -q ."
    check "system PostgreSQL: pg_hba has our scram lines and nothing weaker for motuz_user" \
        bash -c "hba=\$(cd /; runuser -u postgres -- psql -X -p \$DB_PORT -d postgres -tAc 'SHOW hba_file'); grep -q '^host motuz motuz_user 127.0.0.1/32 scram-sha-256' \$hba && ! grep -E '^host .*motuz_user.* (trust|password|md5)\$' \$hba"
    check "system PostgreSQL: motuz_user logs in on TCP, is no superuser, owns the database" \
        bash -c "[ \"\$(PGPASSWORD=\$DB_PW psql -X -h 127.0.0.1 -p \$DB_PORT -U motuz_user -d motuz -tAc \"select rolsuper::text || (select pg_get_userbyid(datdba) from pg_database where datname='motuz') from pg_roles where rolname='motuz_user'\")\" = falsemotuz_user ]"
    check "system PostgreSQL: a wrong password and a local user without a role are refused" \
        bash -c "! PGPASSWORD=wrong psql -X -h 127.0.0.1 -p \$DB_PORT -U motuz_user -d motuz -tAc 'select 1' && ! PGPASSWORD=x runuser -u alice -- env PGPASSWORD=x psql -X -h 127.0.0.1 -p \$DB_PORT -U postgres -d postgres -tAc 'select 1'"
    check "system PostgreSQL: the account has no motuz-postgres unit" bash -c "! ls $HOME_M/.config/systemd/user | grep -q motuz-postgres"
fi
check "/etc/sudoers.d/motuz is root 440" bash -c "[ \"\$(stat -c '%U %a' /etc/sudoers.d/motuz)\" = 'root 440' ]"

# --- login helper (/run/motuz-auth.sock)
ask() { # as-user user password -> OK/NO/ERROR
    runuser -u "$1" -- python3 -I -S -c '
import json, socket, sys
s = socket.socket(socket.AF_UNIX)
s.settimeout(30)
try:
    s.connect("/run/motuz-auth.sock")
    s.sendall(json.dumps({"user": sys.argv[1], "password": sys.argv[2]}).encode() + b"\n")
    s.shutdown(socket.SHUT_WR)
    print(s.recv(16).decode().strip() or "EMPTY")
except OSError as e:
    print("ERROR", e.errno)' "$2" "$3"
}
export -f ask # the checks run it in `bash -c`
export ACCOUNT
check "socket is root:$ACCOUNT 660" bash -c "[ \"\$(stat -c '%U:%G %a' /run/motuz-auth.sock)\" = 'root:$ACCOUNT 660' ]"
check "helper accepts alice with her password" bash -c "[ \"\$(ask $ACCOUNT alice AlicePass1)\" = OK ]"
check "helper rejects a wrong password" bash -c "[ \"\$(ask $ACCOUNT alice wrong)\" = NO ]"
check "helper refuses root" bash -c "[ \"\$(ask $ACCOUNT root anything)\" = NO ] && journalctl -u 'motuz-auth@*' --since -2min -o cat | grep -q \"refused 'root': root\""
check "helper refuses system accounts (daemon, uid 1)" bash -c "[ \"\$(ask $ACCOUNT daemon x)\" = NO ] && journalctl -u 'motuz-auth@*' --since -2min -o cat | grep -q \"refused 'daemon': system account\""
check "helper refuses the service account itself" bash -c "[ \"\$(ask $ACCOUNT $ACCOUNT x)\" = NO ]"
check "other users cannot connect to the helper (EACCES)" bash -c "ask alice alice AlicePass1 | grep -q '^ERROR 13'"
check "root peers are refused by the helper" bash -c "[ \"\$(runuser -u root -- python3 -I -S -c '
import json, socket
s = socket.socket(socket.AF_UNIX); s.connect(\"/run/motuz-auth.sock\")
s.sendall(json.dumps({\"user\": \"alice\", \"password\": \"AlicePass1\"}).encode() + b\"\n\"); s.shutdown(socket.SHUT_WR)
print(s.recv(16).decode().strip())')\" = NO ]"
check "passwords never appear in the helper's log" bash -c "! journalctl -u 'motuz-auth@*' -o cat | grep -qE 'AlicePass1|anything'"

# Rate limit with a user of its own (the lockout lasts 15 minutes)
id carol >/dev/null 2>&1 || useradd -M -u 1503 -p "$(openssl passwd -6 CarolPass1)" carol
check "carol logs in before the lockout" bash -c "[ \"\$(ask $ACCOUNT carol CarolPass1)\" = OK ]"
for i in 1 2 3 4 5; do ask "$ACCOUNT" carol "wrong$i" >/dev/null; done
check "after 5 failures carol is locked out, even with her password" bash -c "[ \"\$(ask $ACCOUNT carol CarolPass1)\" = NO ] && journalctl -u 'motuz-auth@*' --since -2min -o cat | grep -q 'refused carol: locked out'"
check "the lockout is per user (alice still logs in)" bash -c "[ \"\$(ask $ACCOUNT alice AlicePass1)\" = OK ]"
check "a login through Motuz (HTTPS) is refused for carol while locked out" \
    bash -c "[ \"\$(curl -sk -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{\"username\":\"carol\",\"password\":\"CarolPass1\"}' https://localhost/api/auth/login/)\" = 401 ]"
check "Motuz refuses a login as root (HTTPS)" \
    bash -c "[ \"\$(curl -sk -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{\"username\":\"root\",\"password\":\"x\"}' https://localhost/api/auth/login/)\" = 401 ]"
rm -f /var/lib/motuz-auth/failures.json # carol's lockout

echo; echo "$pass/$((pass + fail)) passed"
[ "$fail" = 0 ]
