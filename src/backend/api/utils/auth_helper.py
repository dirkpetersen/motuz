"""
Client of the Motuz login helper (deployment/systemd/auth-helper/motuz_auth_helper.py).

A Motuz that does not run as root (the systemd install, bin/systemd) cannot check the
passwords of local /etc/shadow accounts with PAM itself: pam_unix then asks the
setgid helper unix_chkpwd, which only checks the caller's own password. The login
helper is a socket-activated root service that runs one PAM check per connection.

Protocol: the client sends one JSON object {"user": ..., "password": ...} followed by a
newline, at most MAX_REQUEST bytes in all, and reads the answer "OK\\n" or "NO\\n".
Anything else, including a closed connection, means no.
"""
import json
import logging
import socket

MAX_REQUEST = 4096
TIMEOUT = 20 # seconds; PAM delays failures (pam_faildelay) by a few seconds


def encode_request(user, password):
    """The request bytes, or None if it cannot be sent (too long, not a string)"""
    if not isinstance(user, str) or not isinstance(password, str):
        return None
    data = json.dumps({'user': user, 'password': password}, ensure_ascii=True).encode('ascii') + b'\n'
    return data if len(data) <= MAX_REQUEST else None


def check_password(socket_path, user, password, timeout=TIMEOUT):
    """True only if the helper at `socket_path` answered OK for this user and password"""
    data = encode_request(user, password)
    if data is None:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path)
            sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
            answer = b''
            while len(answer) < 3:
                chunk = sock.recv(3 - len(answer))
                if not chunk:
                    break
                answer += chunk
    except OSError as e:
        logging.error("Login helper %s failed: %s", socket_path, e)
        return False
    return answer == b'OK\n'
