import os
import unittest
from unittest import mock

from api import create_app
from api.config import _optional_env
from api.managers import oauth_manager
from api.managers.cloud_connection_manager import _WRITABLE_FIELDS, _writable
from api.managers.token_broker_manager import AppRegistrationMismatch, upstream_client_credentials
from api.utils.abstract_connection import user_process_env


RCLONE_ID = oauth_manager.RCLONE_CLIENT_ID
FORWARDED = (RCLONE_ID, 'rclone-secret')
OWN_APP = ('own-app-id', 'own-app-secret')


class TestUpstreamClientCredentials(unittest.TestCase):
    """Which client credentials the broker refreshes a OneDrive token with"""

    def test_null_forwards_rclone_credentials(self):
        # Pasted rclone tokens and all connections from before the column existed
        self.assertEqual(upstream_client_credentials(None, FORWARDED, None, RCLONE_ID), FORWARDED)
        self.assertEqual(upstream_client_credentials(None, FORWARDED, OWN_APP, RCLONE_ID), FORWARDED)
        self.assertEqual(upstream_client_credentials('', FORWARDED, OWN_APP, RCLONE_ID), FORWARDED)

    def test_rclone_id_forwards_rclone_credentials(self):
        self.assertEqual(upstream_client_credentials(RCLONE_ID, FORWARDED, None, RCLONE_ID), FORWARDED)
        self.assertEqual(upstream_client_credentials(RCLONE_ID, FORWARDED, OWN_APP, RCLONE_ID), FORWARDED)

    def test_forwards_nothing_if_rclone_sent_nothing(self):
        self.assertEqual(upstream_client_credentials(None, (None, None), OWN_APP, RCLONE_ID), (None, None))

    def test_own_app_replaces_rclone_credentials(self):
        self.assertEqual(upstream_client_credentials('own-app-id', FORWARDED, OWN_APP, RCLONE_ID), OWN_APP)

    def test_own_app_without_secret(self):
        self.assertEqual(
            upstream_client_credentials('own-app-id', FORWARDED, ('own-app-id', ''), RCLONE_ID),
            ('own-app-id', ''),
        )

    def test_other_app_is_refused(self):
        # The own app changed ...
        with self.assertRaises(AppRegistrationMismatch):
            upstream_client_credentials('old-app-id', FORWARDED, OWN_APP, RCLONE_ID)
        # ... or was removed from the configuration
        with self.assertRaises(AppRegistrationMismatch):
            upstream_client_credentials('own-app-id', FORWARDED, None, RCLONE_ID)
        with self.assertRaises(AppRegistrationMismatch):
            upstream_client_credentials('own-app-id', FORWARDED, ('', ''), RCLONE_ID)


class TestOptionalEnv(unittest.TestCase):

    def test_empty_means_unset(self):
        with mock.patch.dict(os.environ, {'MOTUZ_TEST_OPTIONAL': ''}):
            self.assertIsNone(_optional_env('MOTUZ_TEST_OPTIONAL'))
            self.assertEqual(_optional_env('MOTUZ_TEST_OPTIONAL', 'default'), 'default')
        with mock.patch.dict(os.environ, {'MOTUZ_TEST_OPTIONAL': ' \n'}):
            self.assertIsNone(_optional_env('MOTUZ_TEST_OPTIONAL'))
        with mock.patch.dict(os.environ, {'MOTUZ_TEST_OPTIONAL': 'value\n'}):
            self.assertEqual(_optional_env('MOTUZ_TEST_OPTIONAL'), 'value')
        os.environ.pop('MOTUZ_TEST_OPTIONAL', None)
        self.assertIsNone(_optional_env('MOTUZ_TEST_OPTIONAL'))


class TestOauthManagerConfig(unittest.TestCase):

    def setUp(self):
        self.app = create_app('test')
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def configure(self, client_id, secret, redirect_uri):
        self.app.config.update(
            ONEDRIVE_CLIENT_ID=client_id,
            ONEDRIVE_CLIENT_SECRET=secret,
            ONEDRIVE_REDIRECT_URI=redirect_uri,
        )

    def test_not_configured(self):
        for client_id in (None, ''):
            # A redirect URI or secret without a client id is ignored
            self.configure(client_id, 's', 'https://motuz.example.org/api/oauth/onedrive/callback')
            self.assertFalse(oauth_manager.uses_own_app())
            self.assertIsNone(oauth_manager.own_client_id())
            self.assertEqual(oauth_manager.redirect_uri(), 'http://localhost:53682/')
            self.assertEqual(oauth_manager.redirect_mode(), 'paste')
            self.assertEqual(oauth_manager.scopes(), oauth_manager.RCLONE_SCOPES)
            with mock.patch.object(oauth_manager, '_reveal', return_value='revealed'):
                self.assertEqual(oauth_manager.client_credentials(), (RCLONE_ID, 'revealed'))

    def test_own_app_callback(self):
        self.configure('own-app-id', 'own-secret', 'https://motuz.example.org/api/oauth/onedrive/callback')
        self.assertTrue(oauth_manager.uses_own_app())
        self.assertEqual(oauth_manager.client_credentials(), ('own-app-id', 'own-secret'))
        self.assertEqual(oauth_manager.redirect_mode(), 'callback')
        self.assertEqual(oauth_manager.scopes(), oauth_manager.OWN_APP_SCOPES)

    def test_own_app_without_redirect_uri_pastes(self):
        self.configure('own-app-id', '', None)
        self.assertEqual(oauth_manager.client_credentials(), ('own-app-id', ''))
        self.assertEqual(oauth_manager.redirect_uri(), 'http://localhost:53682/')
        self.assertEqual(oauth_manager.redirect_mode(), 'paste')


class TestServerControlledFields(unittest.TestCase):

    def test_onedrive_client_id_not_writable(self):
        self.assertNotIn('onedrive_client_id', _WRITABLE_FIELDS)
        self.assertNotIn('token_broker_handle', _WRITABLE_FIELDS)
        self.assertEqual(_writable({'name': 'x', 'onedrive_client_id': 'evil'}), {'name': 'x'})


class TestSubprocessEnv(unittest.TestCase):

    def test_server_secrets_removed(self):
        secrets = {
            'MOTUZ_FLASK_SECRET_KEY': 'k',
            'MOTUZ_DATABASE_PASSWORD': 'p',
            'MOTUZ_SMTP_PASSWORD': 's',
            'MOTUZ_ONEDRIVE_CLIENT_SECRET': 'o',
        }
        with mock.patch.dict(os.environ, {**secrets, 'MOTUZ_ONEDRIVE_CLIENT_ID': 'id', 'PATH': '/bin'}):
            env = user_process_env({'RCLONE_CONFIG_SRC_TYPE': 'onedrive'})
        for key in secrets:
            self.assertNotIn(key, env)
        # Allowlist: nothing of the server's configuration reaches user processes
        self.assertFalse([key for key in env if key.startswith('MOTUZ_')])
        self.assertEqual(env['PATH'], '/bin')
        self.assertEqual(env['RCLONE_CONFIG_SRC_TYPE'], 'onedrive')


class TestGoogleTransient(unittest.TestCase):

    def test_classification(self):
        from api.managers.oauth_manager import _google_transient
        quota = {'error': {'code': 403, 'message': "Quota exceeded for quota metric 'Queries'", 'errors': [{'reason': 'rateLimitExceeded'}]}}
        not_enabled = {'error': {'code': 403, 'message': 'Google Drive API has not been used in project 1 before or it is disabled', 'errors': [{'reason': 'accessNotConfigured'}]}}
        self.assertTrue(_google_transient(403, quota))
        self.assertTrue(_google_transient(429, {}))
        self.assertTrue(_google_transient(503, {}))
        self.assertFalse(_google_transient(403, not_enabled))
        self.assertFalse(_google_transient(401, {}))
