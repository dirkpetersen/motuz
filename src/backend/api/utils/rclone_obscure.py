"""
rclone's "obscure" for the passwords of SFTP and WebDAV remotes (fs/config/obscure in
rclone): AES-256-CTR with a fixed key published in rclone's source, a random 16 byte IV
in front, base64url without padding. It is no protection, rclone only needs the form.

Done here instead of running `rclone obscure <password>`: a password on the command line
is visible to every local user in ps and /proc/*/cmdline.
"""
import base64
import os

_KEY = bytes([
    0x9c, 0x93, 0x5b, 0x48, 0x73, 0x0a, 0x55, 0x4d,
    0x6b, 0xfd, 0x7c, 0x63, 0xc8, 0x86, 0xa9, 0x2b,
    0xd3, 0x90, 0x19, 0x8e, 0xb8, 0x12, 0x8a, 0xfb,
    0xf4, 0xde, 0x16, 0x2b, 0x8b, 0x95, 0xf6, 0x38,
])
_IV_BYTES = 16


def _ctr(iv):
    # Imported here: the remote worker agent (stdlib only) imports this package but
    # never obscures, the job tickets already carry the obscured value
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    return Cipher(algorithms.AES(_KEY), modes.CTR(iv)).encryptor()


def obscure(password):
    iv = os.urandom(_IV_BYTES)
    encryptor = _ctr(iv)
    sealed = encryptor.update(password.encode('utf-8')) + encryptor.finalize()
    return base64.urlsafe_b64encode(iv + sealed).rstrip(b'=').decode('ascii')


def reveal(obscured):
    raw = base64.urlsafe_b64decode(obscured + '=' * (-len(obscured) % 4))
    if len(raw) < _IV_BYTES:
        raise ValueError('input too short when revealing password - is it obscured?')
    decryptor = _ctr(raw[:_IV_BYTES])
    return (decryptor.update(raw[_IV_BYTES:]) + decryptor.finalize()).decode('utf-8')
