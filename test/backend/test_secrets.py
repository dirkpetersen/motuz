"""
Secrets at rest and in transit: MOTUZ_DATA_KEYS encryption of the credential columns,
rclone obscure without the password on a command line, hidden database parameters,
generic 500 messages, purge of abandoned OAuth flows, URL user info in logs, JWT key.
"""
import base64
import datetime
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from flask import Flask
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from api.application import db, jwt
from api.config import Config, TestingConfig
from api.exceptions import INTERNAL_ERROR, error_text, public_error
from api.managers import oauth_manager
from api.models import CloudConnection, OauthFlow
from api.models import encrypted
from api.utils import rclone_obscure, secret_box
from api.utils.abstract_connection import RcloneException
from api.utils.rclone_connection import RcloneConnection, mask_url_secrets

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'backend'))

K1 = secret_box.parse_keys('k1:' + base64.b64encode(b'1' * 32).decode())
K2 = secret_box.parse_keys('k2:' + base64.b64encode(b'2' * 32).decode())
K2_K1 = K2 + K1


class TestSecretBox(unittest.TestCase):

    def test_round_trip(self):
        for value in ('', 'plain', 'üñíçødé ✓', 'x' * 100000, '{"refresh_token": "r"}'):
            sealed = secret_box.encrypt(K1, 'cloud_connection.azure_key', value)
            self.assertTrue(sealed.startswith('mzc1.k1.'))
            self.assertEqual(secret_box.decrypt(K1, 'cloud_connection.azure_key', sealed), value)

    def test_every_value_has_its_own_nonce(self):
        a = secret_box.encrypt(K1, 'c.c', 'same')
        b = secret_box.encrypt(K1, 'c.c', 'same')
        self.assertNotEqual(a, b)

    def test_key_material_is_strict_base64(self):
        good = base64.b64encode(b'7' * 32).decode()
        for bad in (good[:10] + '!' + good[11:], good + ' x', good.replace('A', '*')):
            if bad != good:
                with self.assertRaises(secret_box.SecretBoxError, msg=bad):
                    secret_box.parse_keys('k1:' + bad)
        self.assertEqual(secret_box.parse_keys('k1:' + base64.urlsafe_b64encode(b'7' * 32).decode())[0][0], 'k1')

    def test_wrong_key_fails(self):
        other = secret_box.parse_keys('k1:' + base64.b64encode(b'9' * 32).decode())
        sealed = secret_box.encrypt(K1, 'c.c', 'secret')
        with self.assertRaises(secret_box.SecretBoxError):
            secret_box.decrypt(other, 'c.c', sealed)

    def test_unknown_key_id_fails(self):
        sealed = secret_box.encrypt(K1, 'c.c', 'secret')
        with self.assertRaisesRegex(secret_box.SecretBoxError, 'k1'):
            secret_box.decrypt(K2, 'c.c', sealed)

    def test_tampered_value_fails(self):
        sealed = secret_box.encrypt(K1, 'c.c', 'secret value')
        head, _, body = sealed.rpartition('.')
        raw = bytearray(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        raw[-1] ^= 1
        tampered = head + '.' + base64.urlsafe_b64encode(bytes(raw)).rstrip(b'=').decode()
        with self.assertRaises(secret_box.SecretBoxError):
            secret_box.decrypt(K1, 'c.c', tampered)
        for broken in ('mzc1.k1.', 'mzc1.k1.AAAA', 'plaintext', 'mzc1.k1', 'mzc1.k1.!!!'):
            with self.assertRaises(secret_box.SecretBoxError):
                secret_box.decrypt(K1, 'c.c', broken)

    def test_value_of_another_column_fails(self):
        sealed = secret_box.encrypt(K1, 'cloud_connection.s3_secret_access_key', 'secret')
        with self.assertRaises(secret_box.SecretBoxError):
            secret_box.decrypt(K1, 'cloud_connection.azure_key', sealed)

    def test_old_key_still_decrypts_and_new_key_encrypts(self):
        old = secret_box.encrypt(K1, 'c.c', 'secret')
        self.assertEqual(secret_box.decrypt(K2_K1, 'c.c', old), 'secret')
        fresh = secret_box.encrypt(K2_K1, 'c.c', 'secret')
        self.assertEqual(secret_box.key_id_of(fresh), 'k2')
        self.assertEqual(secret_box.key_id_of(old), 'k1')

    def test_parse_keys(self):
        self.assertEqual([key_id for key_id, _ in K2_K1], ['k2', 'k1'])
        generated = secret_box.parse_keys(secret_box.generate_key('abc'))
        self.assertEqual(generated[0][0], 'abc')
        self.assertEqual(len(generated[0][1]), 32)
        for bad in ('', ' , ', 'nocolon', 'k1:', 'k1:short', 'k1:' + base64.b64encode(b'x' * 31).decode(),
                    'bad id:' + base64.b64encode(b'x' * 32).decode(), 'k1:!!!!',
                    secret_box.generate_key('a') + ',' + secret_box.generate_key('a')):
            with self.assertRaises(secret_box.SecretBoxError, msg=bad):
                secret_box.parse_keys(bad)

    def test_error_messages_do_not_contain_key_material(self):
        material = base64.b64encode(b'x' * 31).decode()
        with self.assertRaises(secret_box.SecretBoxError) as raised:
            secret_box.parse_keys('k1:' + material)
        self.assertNotIn(material, str(raised.exception))

    def test_derived_keys_depend_on_the_purpose_and_secret(self):
        a = secret_box.derive_secret('flask-secret', 'user jwt key v1')
        self.assertEqual(a, secret_box.derive_secret('flask-secret', 'user jwt key v1'))
        self.assertNotEqual(a, secret_box.derive_secret('flask-secret', 'other'))
        self.assertNotEqual(a, secret_box.derive_secret('other-secret', 'user jwt key v1'))
        self.assertNotIn('flask-secret', a)


class TestConfig(unittest.TestCase):

    def run_config(self, **env):
        base = {name: os.environ[name] for name in ('PATH', 'MOTUZ_FLASK_SECRET_KEY')}
        base.update(MOTUZ_DATABASE_PROTOCOL='postgresql', MOTUZ_DATABASE_USER='u', MOTUZ_DATABASE_PASSWORD='p',
                    MOTUZ_DATABASE_NAME='n', MOTUZ_DATABASE_HOST='h')
        base.update(env)
        code = 'from api.config import Config; print(Config.JWT_SECRET_KEY); print(Config.SQLALCHEMY_ENGINE_OPTIONS)'
        return subprocess.run([sys.executable, '-c', code], cwd=BACKEND, env=base, capture_output=True, text=True)

    def test_the_app_does_not_start_without_a_valid_key(self):
        self.assertNotEqual(self.run_config().returncode, 0)
        self.assertIn('MOTUZ_DATA_KEYS', self.run_config().stderr)
        invalid = self.run_config(MOTUZ_DATA_KEYS='k1:c2hvcnQ=')
        self.assertNotEqual(invalid.returncode, 0)
        self.assertNotIn('c2hvcnQ=', invalid.stderr)

    def test_jwt_key_is_derived_from_the_flask_secret(self):
        out = self.run_config(MOTUZ_DATA_KEYS=secret_box.generate_key())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotEqual(Config.JWT_SECRET_KEY, os.environ['MOTUZ_FLASK_SECRET_KEY'])
        self.assertEqual(Config.JWT_SECRET_KEY, secret_box.derive_secret(os.environ['MOTUZ_FLASK_SECRET_KEY'], 'user jwt key v1'))
        self.assertIn(Config.JWT_SECRET_KEY, out.stdout)

    def test_database_parameters_are_hidden_in_errors(self):
        self.assertTrue(Config.SQLALCHEMY_ENGINE_OPTIONS['hide_parameters'])


class DatabaseTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = Flask('secrets-test')
        self.app.config.from_object(TestingConfig)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(self.directory.name, 'test.sqlite3'),
            SQLALCHEMY_ENGINE_OPTIONS=Config.SQLALCHEMY_ENGINE_OPTIONS,
            SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        db.create_all()
        self.addCleanup(db.drop_all)
        patcher = mock.patch.object(encrypted, 'DATA_KEYS', K1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def raw(self, table, column, row_id):
        db.session.expire_all()
        return db.session.execute(text('SELECT {} FROM {} WHERE id = :id'.format(column, table)), {'id': row_id}).scalar()


class TestEncryptedColumns(DatabaseTest):

    SECRETS = {
        's3_secret_access_key': 'AKIA-secret-access-key-value', 's3_session_token': 'session-token-value',
        'azure_key': 'azure-account-key==', 'azure_sas_url': 'https://a.blob.core.windows.net/?sig=sas-signature',
        'swift_key': 'swift-key-value', 'gcp_service_account_credentials': '{"private_key": "gcp-private-key"}',
        'sftp_pass': 'sftp-password', 'webdav_pass': 'webdav-password',
        'dropbox_token': '{"refresh_token": "dropbox-refresh"}', 'onedrive_token': '{"refresh_token": "onedrive-refresh"}',
        'gdrive_token': '{"refresh_token": "gdrive-refresh"}',
    }

    def connection(self):
        connection = CloudConnection(name='c', owner='alice', type='s3', **self.SECRETS)
        db.session.add(connection)
        db.session.commit()
        return connection.id

    def test_every_context_matches_its_column(self):
        columns = encrypted.encrypted_columns()
        for table, column in columns:
            self.assertEqual(db.metadata.tables[table].columns[column].type.context, '{}.{}'.format(table, column))
        self.assertEqual({c for t, c in columns if t == 'cloud_connection'}, set(self.SECRETS))
        self.assertEqual({c for t, c in columns if t == 'oauth_flow'}, {'token', 'code_verifier'})

    def test_stored_values_are_not_plaintext(self):
        row_id = self.connection()
        for column, value in self.SECRETS.items():
            stored = self.raw('cloud_connection', column, row_id)
            self.assertTrue(stored.startswith('mzc1.k1.'), column)
            self.assertNotIn(value, stored)
            self.assertNotIn(value.split('"')[-2] if value.startswith('{') else value, stored)
        db.session.expire_all()
        connection = db.session.get(CloudConnection, row_id)
        for column, value in self.SECRETS.items():
            self.assertEqual(getattr(connection, column), value)

    def test_the_database_file_holds_no_plaintext(self):
        self.connection()
        db.session.close()
        with open(os.path.join(self.directory.name, 'test.sqlite3'), 'rb') as f:
            content = f.read()
        for value in self.SECRETS.values():
            self.assertNotIn(value.encode(), content)

    def test_null_stays_null_and_non_secrets_stay_plain(self):
        connection = CloudConnection(name='c', owner='alice', type='s3', bucket='my-bucket')
        db.session.add(connection)
        db.session.commit()
        self.assertIsNone(self.raw('cloud_connection', 'azure_key', connection.id))
        self.assertEqual(self.raw('cloud_connection', 'bucket', connection.id), 'my-bucket')

    def test_oauth_flow_columns_are_encrypted(self):
        flow = OauthFlow(state='s', owner='alice', provider='onedrive', code_verifier='pkce-verifier-value',
                         token='{"refresh_token": "flow-refresh"}')
        db.session.add(flow)
        db.session.commit()
        self.assertNotIn('pkce-verifier-value', self.raw('oauth_flow', 'code_verifier', flow.id))
        self.assertNotIn('flow-refresh', self.raw('oauth_flow', 'token', flow.id))
        db.session.expire_all()
        self.assertEqual(db.session.get(OauthFlow, flow.id).token, '{"refresh_token": "flow-refresh"}')

    def test_a_plaintext_value_is_refused_not_returned(self):
        row_id = self.connection()
        db.session.execute(text("UPDATE cloud_connection SET azure_key = 'plain' WHERE id = :id"), {'id': row_id})
        db.session.commit()
        db.session.expire_all()
        with self.assertRaises(secret_box.SecretBoxError):
            db.session.get(CloudConnection, row_id).azure_key

    def test_a_value_moved_to_another_column_is_refused(self):
        row_id = self.connection()
        db.session.execute(text('UPDATE cloud_connection SET azure_key = swift_key WHERE id = :id'), {'id': row_id})
        db.session.commit()
        db.session.expire_all()
        with self.assertRaises(secret_box.SecretBoxError):
            db.session.get(CloudConnection, row_id).azure_key

    def test_a_wrong_key_is_refused(self):
        row_id = self.connection()
        db.session.expire_all()
        with mock.patch.object(encrypted, 'DATA_KEYS', K2):
            with self.assertRaises(secret_box.SecretBoxError):
                db.session.get(CloudConnection, row_id).azure_key

    def test_rotation(self):
        row_id = self.connection()
        flow = OauthFlow(state='s', owner='alice', provider='onedrive', code_verifier='verifier', token='t')
        db.session.add(flow)
        db.session.commit()
        with mock.patch.object(encrypted, 'DATA_KEYS', K2_K1):
            db.session.expire_all()
            self.assertEqual(db.session.get(CloudConnection, row_id).azure_key, self.SECRETS['azure_key'])
            self.assertTrue(self.raw('cloud_connection', 'azure_key', row_id).startswith('mzc1.k1.'))
            counts = encrypted.rotate(db.session)
            self.assertEqual(counts['cloud_connection.azure_key'], 1)
            self.assertEqual(counts['oauth_flow.token'], 1)
            for column in self.SECRETS:
                self.assertTrue(self.raw('cloud_connection', column, row_id).startswith('mzc1.k2.'), column)
            self.assertEqual(encrypted.rotate(db.session)['cloud_connection.azure_key'], 0)
        with mock.patch.object(encrypted, 'DATA_KEYS', K2):  # the old key can go now
            db.session.expire_all()
            connection = db.session.get(CloudConnection, row_id)
            for column, value in self.SECRETS.items():
                self.assertEqual(getattr(connection, column), value)
            self.assertEqual(db.session.get(OauthFlow, flow.id).code_verifier, 'verifier')

    def test_rotation_keeps_a_value_the_app_changed_meanwhile(self):
        row_id = self.connection()
        real_execute = db.session.execute
        def execute(statement, params=None, **kwargs):
            # The token broker stores a refreshed token between rotate's read and write
            if params and params.get('old') is not None and str(statement).startswith('UPDATE cloud_connection SET azure_key'):
                connection = db.session.get(CloudConnection, row_id)
                connection.azure_key = 'refreshed'
                db.session.flush()
            return real_execute(statement, params, **kwargs)
        with mock.patch.object(encrypted, 'DATA_KEYS', K2_K1), mock.patch.object(db.session, 'execute', side_effect=execute):
            counts = encrypted.rotate(db.session)
        self.assertEqual(counts['cloud_connection.azure_key'], 0, 'not overwritten')
        with mock.patch.object(encrypted, 'DATA_KEYS', K2_K1):
            db.session.expire_all()
            self.assertEqual(db.session.get(CloudConnection, row_id).azure_key, 'refreshed')

    def test_check_finds_values_the_keys_cannot_decrypt(self):
        row_id = self.connection()
        self.assertEqual(encrypted.check(db.session), {})
        with mock.patch.object(encrypted, 'DATA_KEYS', K2):
            self.assertEqual(encrypted.check(db.session)['cloud_connection.azure_key'], 1, 'a lost key')
        db.session.execute(text("UPDATE cloud_connection SET sftp_pass = 'plaintext' WHERE id = :id"), {'id': row_id})
        self.assertEqual(encrypted.check(db.session), {'cloud_connection.sftp_pass': 1}, 'from before encryption')
        db.session.rollback()

    def test_rotation_stops_on_a_value_no_key_decrypts(self):
        self.connection()
        with mock.patch.object(encrypted, 'DATA_KEYS', K2):
            with self.assertRaises(secret_box.SecretBoxError):
                encrypted.rotate(db.session, K2)
            db.session.rollback()

    def test_a_database_error_does_not_echo_the_values(self):
        engine = db.engine
        self.assertTrue(engine.hide_parameters)
        db.session.add(CloudConnection(name='dup', owner='alice', type='s3', token_broker_handle='h', azure_key='the-azure-key-value'))
        db.session.commit()
        db.session.add(CloudConnection(name='dup', owner='alice', type='s3', token_broker_handle='h', azure_key='another-azure-key-value'))
        with self.assertRaises(IntegrityError) as raised:
            db.session.commit()
        db.session.rollback()
        for value in ('the-azure-key-value', 'another-azure-key-value'):
            self.assertNotIn(value, str(raised.exception))
        self.assertNotIn('mzc1', str(raised.exception))
        self.assertEqual(public_error(raised.exception), INTERNAL_ERROR)
        self.assertNotIn('IntegrityError', error_text(raised.exception).replace('(IntegrityError)', ''))
        self.assertNotIn('UNIQUE', error_text(raised.exception))


class TestErrorMessages(unittest.TestCase):

    def test_unexpected_errors_are_generic(self):
        self.assertEqual(public_error(ValueError('secret detail')), INTERNAL_ERROR)
        self.assertEqual(public_error(KeyError('x')), INTERNAL_ERROR)

    def test_rclone_errors_keep_their_text(self):
        self.assertEqual(public_error(RcloneException('directory not found')), 'directory not found')

    def test_error_text_hides_database_and_broker_errors_only(self):
        from sqlalchemy.exc import OperationalError
        import kombu.exceptions
        self.assertEqual(error_text(ValueError('bad input')), 'bad input')
        database = OperationalError('INSERT ... VALUES (:p)', {'p': 'password-value'}, Exception('boom'), hide_parameters=True)
        self.assertNotIn('INSERT', error_text(database))
        self.assertNotIn('password-value', error_text(database))
        self.assertNotIn('amqp://', error_text(kombu.exceptions.OperationalError('amqp://user:pw@host failed')))

    def test_views_do_not_return_exception_text(self):
        views = os.path.join(BACKEND, 'api', 'views')
        for name in os.listdir(views):
            if name.endswith('.py'):
                with open(os.path.join(views, name)) as f:
                    self.assertNotIn('abort(500, str(e))', f.read(), name)


class TestObscure(unittest.TestCase):

    def test_round_trip_and_form(self):
        for password in ('', 'pw', 'pässwörd ✓', 'a' * 500):
            obscured = rclone_obscure.obscure(password)
            self.assertEqual(rclone_obscure.reveal(obscured), password)
            self.assertRegex(obscured, r'^[A-Za-z0-9_-]*$')
        self.assertNotEqual(rclone_obscure.obscure('pw'), rclone_obscure.obscure('pw'))

    def test_rclone_reveals_it(self):
        rclone = os.environ.get('MOTUZ_RCLONE') or shutil.which('rclone')
        if not rclone or not os.access(rclone, os.X_OK):
            self.skipTest('no rclone binary')
        password = 'p4ss wörd/with "quotes" ✓'
        revealed = subprocess.run([rclone, 'reveal', rclone_obscure.obscure(password)], capture_output=True, text=True)
        self.assertEqual(revealed.stdout.rstrip('\n'), password)

    def test_we_reveal_what_rclone_obscured(self):
        rclone = os.environ.get('MOTUZ_RCLONE') or shutil.which('rclone')
        if not rclone or not os.access(rclone, os.X_OK):
            self.skipTest('no rclone binary')
        obscured = subprocess.run([rclone, 'obscure', '-'], input='from-rclone\n', capture_output=True, text=True)
        self.assertEqual(rclone_obscure.reveal(obscured.stdout.strip()), 'from-rclone')

    def test_the_password_never_appears_in_a_command_line(self):
        data = SimpleNamespace(type='sftp', owner='alice', sftp_host='h', sftp_port='22', sftp_user='u',
                               sftp_pass='sftp-argv-secret', sftp_key_file=None, subtype=None)
        webdav = SimpleNamespace(type='webdav', owner='alice', webdav_url='https://h/', webdav_user='u',
                                 webdav_pass='webdav-argv-secret', subtype=None)
        started = []
        real_popen = subprocess.Popen

        def spy(*args, **kwargs):
            started.append(args[0] if args else kwargs.get('args'))
            return real_popen(*args, **kwargs)

        with mock.patch.object(subprocess, 'Popen', spy):
            connection = RcloneConnection()
            for entry, secret in ((data, 'sftp-argv-secret'), (webdav, 'webdav-argv-secret')):
                credentials = connection._formatCredentials(entry, 'current')
                value = next(v for k, v in credentials.items() if k.endswith('_PASS'))
                self.assertNotEqual(value, secret)
                self.assertEqual(rclone_obscure.reveal(value), secret)
        self.assertEqual(started, [], 'no process is needed to obscure a password')


class TestUrlMasking(unittest.TestCase):

    def test_user_info_and_query_are_masked(self):
        self.assertEqual(mask_url_secrets('https://user:pass@host.example/dav/'), 'https://***@host.example/dav/')
        self.assertEqual(mask_url_secrets('https://tok@host/x?token=abc&y=1'), 'https://***@host/x?***')
        self.assertEqual(mask_url_secrets('https://host/dav/'), 'https://host/dav/')
        self.assertEqual(mask_url_secrets('eu-west-1'), 'eu-west-1')
        self.assertEqual(mask_url_secrets('http://a@b@host/'), 'http://***@host/')

    def test_log_command_masks_the_url_of_a_webdav_connection(self):
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        command = RcloneConnection()._log_command(['rclone', 'lsjson'], {
            'RCLONE_CONFIG_SRC_URL': 'https://alice:hunter2@dav.example.org/remote.php/',
            'RCLONE_CONFIG_SRC_ENDPOINT': 'https://AKID:SECRETKEY@s3.example.org',
            'RCLONE_CONFIG_SRC_USER': 'alice',
        })
        self.assertNotIn('hunter2', command)
        self.assertNotIn('SECRETKEY', command)
        self.assertIn("RCLONE_CONFIG_SRC_URL='https://***@dav.example.org/remote.php/'", command)
        self.assertIn("RCLONE_CONFIG_SRC_USER='alice'", command)


class TestOauthFlowPurge(DatabaseTest):

    def add_flow(self, state, age_minutes, token='{"refresh_token": "abandoned"}'):
        flow = OauthFlow(state=state, owner='alice', provider='onedrive', code_verifier='v', token=token,
                         created_at=datetime.datetime.utcnow() - datetime.timedelta(minutes=age_minutes))
        db.session.add(flow)
        db.session.commit()

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, oauth_manager, '_last_purge', 0.0)
        oauth_manager._last_purge = 0.0

    def test_expired_flows_are_deleted_and_fresh_ones_kept(self):
        self.add_flow('old', 16)
        self.add_flow('older', 600)
        self.add_flow('fresh', 5)
        self.assertEqual(oauth_manager.purge_expired_flows(), 2)
        self.assertEqual([f.state for f in OauthFlow.query.all()], ['fresh'])

    def test_rate_limited_unless_forced(self):
        self.add_flow('old', 16)
        self.assertEqual(oauth_manager.purge_expired_flows(), 1)
        self.add_flow('old2', 16)
        self.assertEqual(oauth_manager.purge_expired_flows(), 0)
        self.assertEqual(oauth_manager.purge_expired_flows(force=True), 1)

    def test_purge_runs_when_the_job_list_is_read(self):
        with open(os.path.join(BACKEND, 'api', 'managers', 'copy_job_manager.py')) as f:
            self.assertIn('oauth_manager.purge_expired_flows()', f.read())

    def test_connect_clears_the_token_of_the_flow(self):
        with open(os.path.join(BACKEND, 'api', 'managers', 'oauth_manager.py')) as f:
            source = f.read()
        self.assertIn('flow.token = None\n    db.session.delete(flow)', source)


class TestNoSecretInRepoDefaults(unittest.TestCase):

    def test_ci_and_dev_do_not_use_fixed_database_passwords(self):
        root = os.path.abspath(os.path.join(BACKEND, '..', '..'))
        for path in ('bin/dev/database_start.sh', '.github/workflows/ci.yml'):
            with open(os.path.join(root, path)) as f:
                text_ = f.read()
            self.assertNotIn('POSTGRES_PASSWORD=docker', text_, path)
            self.assertNotIn('MOTUZ_DATABASE_PASSWORD: ci\n', text_, path)


if __name__ == '__main__':
    unittest.main()
