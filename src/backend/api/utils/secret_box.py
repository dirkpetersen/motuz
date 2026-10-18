"""
Encryption of secrets at rest (AES-256-GCM) and derivation of purpose-bound keys.

Pure functions: this module never imports the Flask application or the configuration,
the keys come in as arguments (api/config.py parses MOTUZ_DATA_KEYS, api/models/encrypted.py
encrypts the credential columns).

Stored value: `mzc1.<key id>.<base64url(nonce || ciphertext || tag)>`, a random 96-bit
nonce per value, the table and column (`cloud_connection.azure_key`) as associated
data, so a value copied into another column does not decrypt. The AES key of a value is
derived with HKDF-SHA256 from the key material of its key id, so the raw material is
never used as a cipher key directly, and never shared with another purpose.

MOTUZ_DATA_KEYS: `<id>:<base64 of 32 or more random bytes>`, several separated by commas,
the first one encrypts, all of them decrypt (rotation: `manage.py secrets rotate`).
"""
import base64
import binascii
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIX = 'mzc1'
_KEY_ID = re.compile(r'^[A-Za-z0-9_-]{1,16}$')
_MIN_KEY_BYTES = 32
_NONCE_BYTES = 12


class SecretBoxError(Exception):
    """A key is invalid, or a stored value cannot be decrypted (wrong key, tampered, wrong column)"""


def _b64d(text):
    try:
        # Strict: a mistyped key or a changed value must not decode to something
        standard = text.replace('-', '+').replace('_', '/')
        return base64.b64decode(standard + '=' * (-len(standard) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise SecretBoxError('not valid base64') from None


def parse_keys(text):
    """
    Parses MOTUZ_DATA_KEYS into [(key id, key material)], the first entry being the active
    key. Raises SecretBoxError (never with key material in the message).
    """
    keys = []
    for entry in (text or '').split(','):
        entry = entry.strip()
        if not entry:
            continue
        key_id, separator, material = entry.partition(':')
        if not separator or not _KEY_ID.match(key_id):
            raise SecretBoxError('MOTUZ_DATA_KEYS: each key is <id>:<base64 key>, with an id of 1 to 16 letters, digits, - or _')
        if any(key_id == known for known, _ in keys):
            raise SecretBoxError('MOTUZ_DATA_KEYS: the key id {} appears twice'.format(key_id))
        try:
            raw = _b64d(material.strip())
        except SecretBoxError:
            raise SecretBoxError('MOTUZ_DATA_KEYS: the key {} is not valid base64'.format(key_id)) from None
        if len(raw) < _MIN_KEY_BYTES:
            raise SecretBoxError('MOTUZ_DATA_KEYS: the key {} must be at least {} random bytes'.format(key_id, _MIN_KEY_BYTES))
        keys.append((key_id, raw))
    if not keys:
        raise SecretBoxError('MOTUZ_DATA_KEYS is empty: it needs <id>:<base64 of 32 random bytes>, '
                             'e.g. k1:$(openssl rand -base64 32)')
    return keys


def generate_key(key_id='k1'):
    """A new `<id>:<base64>` entry for MOTUZ_DATA_KEYS"""
    return '{}:{}'.format(key_id, base64.b64encode(os.urandom(32)).decode('ascii'))


def derive(material, purpose, length=32):
    """HKDF-SHA256 of `material` bound to `purpose` (bytes of `length`)"""
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None,
                info=b'motuz ' + purpose.encode('ascii')).derive(material)


def derive_secret(secret, purpose):
    """A hex string derived from a text secret (e.g. the JWT signing key from the Flask secret)"""
    return derive(secret.encode('utf-8'), purpose).hex()


def _cipher(material, key_id):
    return AESGCM(derive(material, 'data key v1 ' + key_id))


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX + '.')


def key_id_of(value):
    """The id of the key that encrypted `value`, or None for something else"""
    return value.split('.', 2)[1] if is_encrypted(value) and value.count('.') >= 2 else None


def encrypt(keys, context, plaintext):
    key_id, material = keys[0]
    nonce = os.urandom(_NONCE_BYTES)
    sealed = _cipher(material, key_id).encrypt(nonce, plaintext.encode('utf-8'), context.encode('ascii'))
    return '{}.{}.{}'.format(PREFIX, key_id, base64.urlsafe_b64encode(nonce + sealed).rstrip(b'=').decode('ascii'))


def decrypt(keys, context, value):
    """Raises SecretBoxError for an unknown key id, a wrong key, tampering or another column"""
    parts = value.split('.')
    if len(parts) != 3 or parts[0] != PREFIX:
        raise SecretBoxError('a value of {} is not encrypted'.format(context))
    material = dict(keys).get(parts[1])
    if material is None:
        raise SecretBoxError('a value of {} was encrypted with the key {}, which is not in MOTUZ_DATA_KEYS'.format(context, parts[1]))
    blob = _b64d(parts[2])
    if len(blob) < _NONCE_BYTES + 16:
        raise SecretBoxError('a value of {} is truncated'.format(context))
    try:
        return _cipher(material, parts[1]).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:],
                                                    context.encode('ascii')).decode('utf-8')
    except InvalidTag:
        raise SecretBoxError('a value of {} cannot be decrypted: wrong key, or the value was changed'.format(context)) from None
