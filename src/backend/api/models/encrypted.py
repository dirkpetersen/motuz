from sqlalchemy import text
from sqlalchemy.types import String, TypeDecorator

from ..config import DATA_KEYS
from ..utils import secret_box


class EncryptedString(TypeDecorator):
    """
    A text column stored encrypted with AES-256-GCM under the active key of MOTUZ_DATA_KEYS
    (utils/secret_box.py). `context` is `<table>.<column>` and is authenticated with
    the value, so a stored value does not decrypt in another column (the test
    test_secret_columns checks that every context matches its column). Decryption fails
    loudly (SecretBoxError) on a wrong key or a changed value; there is no plaintext
    fallback. NULL stays NULL. Encrypted columns cannot be searched or compared in SQL
    (the ciphertext differs on every write).
    """
    impl = String
    cache_ok = True

    def __init__(self, context):
        super().__init__()
        self.context = context

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return secret_box.encrypt(DATA_KEYS, self.context, value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return secret_box.decrypt(DATA_KEYS, self.context, value)


def encrypted_columns():
    """[(table name, column name)] of every EncryptedString column of the models"""
    from ..application import db
    return [(table.name, column.name)
            for table in db.metadata.sorted_tables
            for column in table.columns
            if isinstance(column.type, EncryptedString)]


def check(session, keys=None):
    """
    {'table.column': number of values that MOTUZ_DATA_KEYS (or `keys`) cannot decrypt}, only
    the columns with such values: plaintext from before encryption, a value of another
    key, or a changed value. Empty if every stored secret is readable.
    """
    keys = keys or DATA_KEYS
    quote = session.get_bind().dialect.identifier_preparer.quote
    unreadable = {}
    for table_name, column_name in encrypted_columns():
        context = '{}.{}'.format(table_name, column_name)
        select = text('SELECT {c} FROM {t} WHERE {c} IS NOT NULL'.format(c=quote(column_name), t=quote(table_name)))
        for (value,) in session.execute(select):
            try:
                secret_box.decrypt(keys, context, value)
            except secret_box.SecretBoxError:
                unreadable[context] = unreadable.get(context, 0) + 1
    return unreadable


def rotate(session, keys=None):
    """
    Re-encrypts every value that is not encrypted with the active key (the first of
    `keys`, default MOTUZ_DATA_KEYS) and returns {'table.column': values re-encrypted}.
    Reads and writes the raw column values, so a value of a retired key decrypts with
    the old key. Each write only replaces the value it read: a value the app changed
    meanwhile (e.g. the token broker storing a refreshed OAuth token) is kept, and the
    app writes it with the active key anyway. Raises SecretBoxError on a value no key
    decrypts (nothing is saved).
    """
    keys = keys or DATA_KEYS
    active = keys[0][0]
    quote = session.get_bind().dialect.identifier_preparer.quote
    counts = {}
    for table_name, column_name in encrypted_columns():
        context = '{}.{}'.format(table_name, column_name)
        select = text('SELECT id, {c} FROM {t} WHERE {c} IS NOT NULL'.format(c=quote(column_name), t=quote(table_name)))
        update = text('UPDATE {t} SET {c} = :value WHERE id = :id AND {c} = :old'.format(c=quote(column_name), t=quote(table_name)))
        counts[context] = 0
        for row_id, value in session.execute(select).fetchall():
            if secret_box.key_id_of(value) == active:
                continue
            result = session.execute(update, {'id': row_id, 'old': value,
                                              'value': secret_box.encrypt(keys, context, secret_box.decrypt(keys, context, value))})
            counts[context] += result.rowcount
    session.commit()
    return counts
