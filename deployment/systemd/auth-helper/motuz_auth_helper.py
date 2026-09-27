"""
motuz-auth: checks one Motuz login password with PAM, as root.

Why: Motuz installed with bin/systemd runs as the unprivileged account `motuz`. PAM in
that process can check SSSD/Kerberos accounts, but not local /etc/shadow accounts:
pam_unix then asks the setgid helper unix_chkpwd, which only checks the password of
the calling user. This helper does that one check as root.

How it runs: socket activated (deployment/systemd/system/motuz-auth.socket with
Accept=yes). systemd starts one instance per connection with the connection as stdin
and stdout; the socket is /run/motuz-auth.sock, root:motuz, mode 0660, and the peer
must also be the client account (SO_PEERCRED).

Protocol (the client is src/backend/api/utils/auth_helper.py): one JSON object
{"user": "...", "password": "..."} and a newline, at most 4096 bytes; the answer is
"OK\\n" or "NO\\n", then the connection is closed.

Refused without asking PAM: root, accounts with a uid below UID_MIN (system accounts,
from /etc/login.defs, else 1000), nobody (65534), the client account itself, unknown
users and malformed names. After MAX_FAILURES failed attempts of a user within WINDOW
seconds, that user is refused until the window has passed (an attempt counts as
failed until PAM accepts it, so concurrent guesses count too). The state file is in
the service's StateDirectory (root only).

Passwords are never logged. Only the Python standard library (ctypes PAM binding
pam.py, a root-owned copy of src/backend/api/utils/pam.py next to this file).
"""
import fcntl
import importlib.util
import json
import os
import pwd
import re
import socket
import struct
import sys
import time

MAX_REQUEST = 4096
READ_TIMEOUT = 10 # seconds for the client to send its request
REFUSAL_DELAY = 2 # seconds, about what PAM waits after a failure (pam_faildelay)
USER_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}\Z')
NOBODY_UID = 65534
MAX_TRACKED_USERS = 10000

OK = b'OK\n'
NO = b'NO\n'


def log(message):
    sys.stderr.write('motuz-auth: {}\n'.format(message))
    sys.stderr.flush()


def _env_int(name, default):
    try:
        return int(os.environ.get(name, ''))
    except ValueError:
        return default


def uid_min(login_defs='/etc/login.defs'):
    """UID_MIN of /etc/login.defs (the first uid of regular accounts), else 1000"""
    configured = _env_int('MOTUZ_AUTH_MIN_UID', 0)
    if configured > 0:
        return configured
    try:
        with open(login_defs) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == 'UID_MIN' and parts[1].isdigit():
                    return max(int(parts[1]), 1)
    except OSError:
        pass
    return 1000


def read_request(conn):
    """The request bytes (up to and without the newline), or None if too long or too slow"""
    conn.settimeout(READ_TIMEOUT)
    data = b''
    try:
        while b'\n' not in data:
            chunk = conn.recv(MAX_REQUEST + 1 - len(data))
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_REQUEST:
                return None
    except OSError: # includes socket.timeout
        return None
    line, newline, rest = data.partition(b'\n')
    if rest:
        return None # one request per connection
    return line


def parse_request(data):
    """(user, password) or None"""
    try:
        request = json.loads(data.decode('ascii'))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(request, dict) or set(request) != {'user', 'password'}:
        return None
    user, password = request['user'], request['password']
    if not isinstance(user, str) or not isinstance(password, str):
        return None
    if '\x00' in password or not password:
        return None
    return user, password


def refusal(user, client_user, min_uid):
    """Why `user` may not log in through this helper, or None"""
    if not USER_NAME.match(user):
        return 'malformed user name'
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        return 'unknown user'
    if entry.pw_uid == 0 or user == 'root':
        return 'root'
    if entry.pw_uid < min_uid:
        return 'system account (uid {} < {})'.format(entry.pw_uid, min_uid)
    if entry.pw_uid == NOBODY_UID:
        return 'nobody'
    if user == client_user:
        return 'the service account itself'
    return None


class FailureLog:
    """Failed (and pending) attempts per user in a JSON file, under an exclusive lock"""

    def __init__(self, path, max_failures, window):
        self.path = path
        self.max_failures = max_failures
        self.window = window

    def _update(self, change):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'r+') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                state = json.loads(f.read() or '{}')
                if not isinstance(state, dict):
                    state = {}
            except ValueError:
                state = {}
            now = time.time()
            state = {user: [t for t in times if isinstance(t, (int, float)) and now - t < self.window]
                     for user, times in state.items() if isinstance(times, list)}
            state = {user: times for user, times in state.items() if times}
            result = change(state, now)
            if len(state) > MAX_TRACKED_USERS: # bounded: only existing accounts are tracked
                for user in sorted(state, key=lambda u: max(state[u]))[:len(state) - MAX_TRACKED_USERS]:
                    del state[user]
            f.seek(0)
            f.truncate()
            f.write(json.dumps(state))
            return result

    def begin(self, user):
        """Records an attempt as failed; False if the user is locked out (nothing recorded)"""
        def change(state, now):
            times = state.get(user, [])
            if len(times) >= self.max_failures:
                return False
            state[user] = times + [now]
            return True
        return self._update(change)

    def succeeded(self, user):
        def change(state, now):
            state.pop(user, None)
        self._update(change)


def load_pam():
    """pam.py next to this file (never from a directory the client account can write)"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pam.py')
    spec = importlib.util.spec_from_file_location('motuz_auth_pam', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pam_check(user, password, service):
    """(accepted, reason) from pam_authenticate + pam_acct_mgmt; no credentials are set"""
    pam = load_pam().pam()
    accepted = pam.authenticate(user, password, service=service, resetcreds=False)
    return bool(accepted) and pam.code == 0, pam.reason


def handle(request_bytes, *, client_user, min_uid, failures, authenticate, service, sleep=time.sleep):
    """The answer (OK or NO) to one request; logs the outcome without the password"""
    parsed = parse_request(request_bytes) if request_bytes is not None else None
    if parsed is None:
        log('rejected a malformed request')
        return NO
    user, password = parsed
    reason = refusal(user, client_user, min_uid)
    if reason:
        log('refused {!r}: {}'.format(user[:64], reason))
        sleep(REFUSAL_DELAY)
        return NO
    if not failures.begin(user):
        log('refused {}: locked out after {} failures within {} s'.format(user, failures.max_failures, failures.window))
        sleep(REFUSAL_DELAY)
        return NO
    try:
        accepted, pam_reason = authenticate(user, password, service)
    except Exception as e: # PAM could not be loaded or crashed: never an OK
        log('PAM error for {}: {}'.format(user, type(e).__name__))
        return NO
    if accepted:
        failures.succeeded(user)
        log('accepted {}'.format(user))
        return OK
    log('rejected {}: {}'.format(user, pam_reason))
    return NO


def peer_uid(conn):
    pid, uid, gid = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    return uid


def main():
    client_user = os.environ.get('MOTUZ_AUTH_CLIENT_USER', 'motuz')
    state_dir = os.environ.get('STATE_DIRECTORY', '/var/lib/motuz-auth').split(':')[0]
    failures = FailureLog(os.path.join(state_dir, 'failures.json'),
                          max(_env_int('MOTUZ_AUTH_MAX_FAILURES', 5), 1),
                          max(_env_int('MOTUZ_AUTH_WINDOW', 900), 1))
    conn = socket.socket(fileno=0) # Accept=yes: the connection is stdin
    try:
        try:
            client_uid = pwd.getpwnam(client_user).pw_uid
        except KeyError:
            client_uid = None
        uid = peer_uid(conn)
        if client_uid is None or uid != client_uid:
            log('refused a connection from uid {} (only {} may ask)'.format(uid, client_user))
            answer = NO
        else:
            answer = handle(read_request(conn), client_user=client_user, min_uid=uid_min(),
                            failures=failures, authenticate=pam_check,
                            service=os.environ.get('MOTUZ_AUTH_PAM_SERVICE', 'motuz'))
        conn.sendall(answer)
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()


if __name__ == '__main__':
    main()
