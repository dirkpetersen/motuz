import json
import logging
import os
import types
import unittest
from unittest import mock

from api import create_app
from api.exceptions import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND
from api.managers import oauth_manager, token_broker_manager
from api.managers.cloud_connection_manager import _SECRET_FIELDS, _WRITABLE_FIELDS, _writable
from api.managers.token_broker_manager import (
    BROKERED_TYPES, AppRegistrationMismatch, connection_client_credentials,
)
from api.utils.abstract_connection import user_process_env
from api.utils.rclone_connection import RcloneConnection, _rate_limit_flags


GOOGLE_RCLONE_ID = '202264815644.apps.googleusercontent.com'
MS_RCLONE_ID = 'b15665d9-eda6-4092-8539-0eec376afd59'
CALLBACK = 'https://motuz.example.org/api/oauth/gdrive/callback'


class AppContextTest(unittest.TestCase):

    def setUp(self):
        self.app = create_app('test')
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.app.config.update(
            ONEDRIVE_CLIENT_ID=None, ONEDRIVE_CLIENT_SECRET=None, ONEDRIVE_REDIRECT_URI=None,
            GDRIVE_CLIENT_ID=None, GDRIVE_CLIENT_SECRET=None, GDRIVE_REDIRECT_URI=None,
        )

    def tearDown(self):
        self.ctx.pop()


class TestProviderDefinitions(AppContextTest):

    def test_registry(self):
        self.assertIs(oauth_manager.provider('onedrive'), oauth_manager.ONEDRIVE)
        self.assertIs(oauth_manager.provider('gdrive'), oauth_manager.GDRIVE)
        self.assertIs(oauth_manager.PROVIDERS_BY_CONNECTION_TYPE['drive'], oauth_manager.GDRIVE)
        with self.assertRaises(HTTP_404_NOT_FOUND):
            oauth_manager.provider('dropbox')

    def test_onedrive_names_unchanged(self):
        # Module level names the rest of the code and older tests use
        self.assertEqual(oauth_manager.PROVIDER, 'onedrive')
        self.assertEqual(oauth_manager.CALLBACK_PATH, '/api/oauth/onedrive/callback')
        self.assertEqual(oauth_manager.RCLONE_CLIENT_ID, MS_RCLONE_ID)
        self.assertEqual(oauth_manager.RCLONE_REDIRECT_URI, 'http://localhost:53682/')

    def test_gdrive_rclone_app(self):
        p = oauth_manager.GDRIVE
        self.assertFalse(p.uses_own_app())
        self.assertEqual(p.callback_path, '/api/oauth/gdrive/callback')
        self.assertEqual(p.connection_type, 'drive')
        self.assertEqual(p.redirect_uri(), 'http://127.0.0.1:53682/')
        self.assertEqual(p.redirect_mode(), 'paste')
        self.assertEqual(p.scopes(), 'https://www.googleapis.com/auth/drive')
        self.assertEqual(p.token_url(), 'https://oauth2.googleapis.com/token')
        self.assertTrue(p.auth_url().startswith('https://accounts.google.com/'))
        with mock.patch.object(oauth_manager, '_reveal', return_value='revealed') as reveal:
            self.assertEqual(p.client_credentials(), (GOOGLE_RCLONE_ID, 'revealed'))
        reveal.assert_called_once_with(p.rclone_obscured_client_secret)

    def test_gdrive_offline_consent(self):
        # Google returns a refresh token only with these
        params = dict(oauth_manager.GDRIVE.auth_params)
        self.assertEqual(params['access_type'], 'offline')
        self.assertEqual(params['prompt'], 'consent')
        self.assertFalse(oauth_manager.GDRIVE.exchange_sends_scope)
        self.assertEqual(dict(oauth_manager.ONEDRIVE.auth_params)['response_mode'], 'query')

    def test_gdrive_own_app(self):
        self.app.config.update(GDRIVE_CLIENT_ID='own.apps.googleusercontent.com', GDRIVE_CLIENT_SECRET='s',
                               GDRIVE_REDIRECT_URI=CALLBACK)
        p = oauth_manager.GDRIVE
        self.assertEqual(p.client_credentials(), ('own.apps.googleusercontent.com', 's'))
        self.assertEqual(p.redirect_mode(), 'callback')
        # Configuring Google does not change OneDrive
        self.assertFalse(oauth_manager.uses_own_app())
        self.assertFalse(oauth_manager.ONEDRIVE.uses_own_app())

    def test_gdrive_own_app_without_redirect_pastes(self):
        self.app.config.update(GDRIVE_CLIENT_ID='own', GDRIVE_REDIRECT_URI=None)
        self.assertEqual(oauth_manager.GDRIVE.redirect_uri(), 'http://127.0.0.1:53682/')
        self.assertEqual(oauth_manager.GDRIVE.redirect_mode(), 'paste')

    def test_redirect_uri_without_client_id_ignored(self):
        self.app.config.update(GDRIVE_CLIENT_ID='', GDRIVE_CLIENT_SECRET='s', GDRIVE_REDIRECT_URI=CALLBACK)
        self.assertEqual(oauth_manager.GDRIVE.redirect_mode(), 'paste')

    def test_connection_fields(self):
        flow = types.SimpleNamespace(token='{"refresh_token": "r"}', client_id='cid')
        shared = oauth_manager.GDRIVE.connection_fields({'id': '0ABC', 'name': 'x', 'drive_type': 'shared_drive'}, flow)
        self.assertEqual(shared['gdrive_team_drive'], '0ABC')
        self.assertEqual(shared['gdrive_client_id'], 'cid')
        self.assertEqual(shared['gdrive_token'], flow.token)
        mine = oauth_manager.GDRIVE.connection_fields({'id': 'root', 'name': 'x', 'drive_type': 'my_drive'}, flow)
        self.assertIsNone(mine['gdrive_team_drive'])


class TestGdriveDiscovery(AppContextTest):

    def fake_request(self, responses):
        calls = []

        def request(method, url, form=None, access_token=None):
            calls.append(url)
            for prefix, response in responses:
                if url.startswith(prefix):
                    return response.pop(0) if isinstance(response, list) else response
            return 404, {}
        return request, calls

    def test_my_drive_and_shared_drives(self):
        api = self.app.config['GDRIVE_API_URL']
        request, calls = self.fake_request([
            (api + '/about', (200, {'user': {'displayName': 'Alice', 'emailAddress': 'alice@example.org'}})),
            (api + '/drives', [
                (200, {'drives': [{'id': '0AAA', 'name': 'Lab'}], 'nextPageToken': 'p2'}),
                (200, {'drives': [{'id': '0BBB', 'name': 'Core'}]}),
            ]),
        ])
        with mock.patch.object(oauth_manager, '_request', request):
            drives = oauth_manager._discover_gdrive('tok')
        self.assertEqual(drives, [
            {'id': 'root', 'name': 'My Drive (alice@example.org)', 'drive_type': 'my_drive'},
            {'id': '0AAA', 'name': 'Shared drive: Lab', 'drive_type': 'shared_drive'},
            {'id': '0BBB', 'name': 'Shared drive: Core', 'drive_type': 'shared_drive'},
        ])
        self.assertIn('pageToken=p2', calls[-1])

    def test_shared_drives_failure_keeps_my_drive(self):
        api = self.app.config['GDRIVE_API_URL']
        request, _ = self.fake_request([
            (api + '/about', (200, {'user': {}})),
            (api + '/drives', (403, {'error': {'message': 'nope'}})),
        ])
        with mock.patch.object(oauth_manager, '_request', request):
            drives = oauth_manager._discover_gdrive('tok')
        self.assertEqual(drives, [{'id': 'root', 'name': 'My Drive', 'drive_type': 'my_drive'}])

    def test_drive_api_disabled(self):
        request, _ = self.fake_request([
            (self.app.config['GDRIVE_API_URL'] + '/about', (403, {'error': {'code': 403, 'message': 'Google Drive API has not been used in project 1'}})),
        ])
        with mock.patch.object(oauth_manager, '_request', request):
            with self.assertRaises(HTTP_400_BAD_REQUEST) as e:
                oauth_manager._discover_gdrive('tok')
        self.assertIn('Google Drive API has not been used', e.exception.payload)


class TestBrokerClientSelection(AppContextTest):
    """The broker picks the client per connection and provider"""

    def connection(self, conn_type, **columns):
        return types.SimpleNamespace(type=conn_type, **columns)

    def test_brokered_types(self):
        self.assertEqual(BROKERED_TYPES['onedrive'], ('onedrive_token', 'ONEDRIVE_TOKEN_URL'))
        self.assertEqual(BROKERED_TYPES['drive'], ('gdrive_token', 'GDRIVE_TOKEN_URL'))

    def test_pasted_gdrive_forwards_rclone(self):
        forwarded = (GOOGLE_RCLONE_ID, 'rclone-secret')
        conn = self.connection('drive', gdrive_client_id=None)
        self.assertEqual(connection_client_credentials(conn, forwarded), forwarded)
        self.app.config.update(GDRIVE_CLIENT_ID='own', GDRIVE_CLIENT_SECRET='own-secret')
        self.assertEqual(connection_client_credentials(conn, forwarded), forwarded)
        conn.gdrive_client_id = GOOGLE_RCLONE_ID
        self.assertEqual(connection_client_credentials(conn, forwarded), forwarded)

    def test_gdrive_own_app(self):
        self.app.config.update(GDRIVE_CLIENT_ID='own', GDRIVE_CLIENT_SECRET='own-secret')
        conn = self.connection('drive', gdrive_client_id='own')
        self.assertEqual(connection_client_credentials(conn, (GOOGLE_RCLONE_ID, 'x')), ('own', 'own-secret'))

    def test_gdrive_switch_refused(self):
        conn = self.connection('drive', gdrive_client_id='old')
        self.app.config.update(GDRIVE_CLIENT_ID='new', GDRIVE_CLIENT_SECRET='s')
        with self.assertRaises(AppRegistrationMismatch):
            connection_client_credentials(conn, (GOOGLE_RCLONE_ID, 'x'))
        self.app.config.update(GDRIVE_CLIENT_ID=None)
        with self.assertRaises(AppRegistrationMismatch):
            connection_client_credentials(conn, (GOOGLE_RCLONE_ID, 'x'))

    def test_providers_do_not_mix(self):
        # A Google client id is not OneDrive's own app, and vice versa
        self.app.config.update(GDRIVE_CLIENT_ID='g-own', GDRIVE_CLIENT_SECRET='g', ONEDRIVE_CLIENT_ID='m-own', ONEDRIVE_CLIENT_SECRET='m')
        with self.assertRaises(AppRegistrationMismatch):
            connection_client_credentials(self.connection('onedrive', onedrive_client_id='g-own'), (MS_RCLONE_ID, 'x'))
        with self.assertRaises(AppRegistrationMismatch):
            connection_client_credentials(self.connection('drive', gdrive_client_id='m-own'), (GOOGLE_RCLONE_ID, 'x'))
        # A OneDrive connection with Google's rclone id is not rclone's OneDrive app either
        with self.assertRaises(AppRegistrationMismatch):
            connection_client_credentials(self.connection('onedrive', onedrive_client_id=GOOGLE_RCLONE_ID), (MS_RCLONE_ID, 'x'))
        self.assertEqual(
            connection_client_credentials(self.connection('onedrive', onedrive_client_id='m-own'), (MS_RCLONE_ID, 'x')),
            ('m-own', 'm'),
        )

    def test_mismatch_description_per_provider(self):
        self.assertIn('Sign in with Google', oauth_manager.GDRIVE.app_mismatch_description)
        self.assertEqual(token_broker_manager.APP_MISMATCH_DESCRIPTION, oauth_manager.ONEDRIVE.app_mismatch_description)


class TestGdriveConnectionFields(unittest.TestCase):

    def test_server_controlled(self):
        self.assertNotIn('gdrive_client_id', _WRITABLE_FIELDS)
        self.assertIn('gdrive_token', _WRITABLE_FIELDS)
        self.assertIn('gdrive_team_drive', _WRITABLE_FIELDS)
        self.assertIn('gdrive_root_folder_id', _WRITABLE_FIELDS)
        self.assertIn('gdrive_token', _SECRET_FIELDS)
        self.assertEqual(_writable({'name': 'x', 'gdrive_client_id': 'evil'}), {'name': 'x'})

    def test_secret_not_passed_to_user_processes(self):
        with mock.patch.dict(os.environ, {'MOTUZ_GDRIVE_CLIENT_SECRET': 'g', 'PATH': '/bin'}):
            env = user_process_env({'RCLONE_CONFIG_SRC_TYPE': 'drive'})
        self.assertNotIn('MOTUZ_GDRIVE_CLIENT_SECRET', env)


TOKEN = json.dumps({'access_token': 'ya29.ACCESS', 'token_type': 'Bearer', 'refresh_token': '1//REFRESH', 'expiry': '2026-01-01T00:00:00Z'})


class TestDriveCredentials(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.INFO)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def data(self, **columns):
        values = dict(type='drive', owner='alice', subtype=None, gdrive_token=TOKEN,
                      gdrive_root_folder_id=None, gdrive_team_drive=None)
        values.update(columns)
        return types.SimpleNamespace(**values)

    def test_pasted_token_unbrokered(self):
        with mock.patch.object(token_broker_manager, 'broker_token', return_value=None):
            credentials = RcloneConnection()._formatCredentials(self.data(gdrive_team_drive='0ABCDEFGHIJ'), 'src')
        self.assertEqual(credentials, {
            'RCLONE_CONFIG_SRC_TYPE': 'drive',
            'RCLONE_CONFIG_SRC_TOKEN': TOKEN,
            'RCLONE_CONFIG_SRC_SCOPE': 'drive',
            'RCLONE_CONFIG_SRC_TEAM_DRIVE': '0ABCDEFGHIJ',
            'RCLONE_CONFIG_SRC_PACER_MIN_SLEEP': '100ms',
            'RCLONE_CONFIG_SRC_CHUNK_SIZE': '64Mi',
            'RCLONE_CONFIG_SRC_STOP_ON_UPLOAD_LIMIT': 'true',
        })

    def test_brokered(self):
        brokered = ('{"access_token":"ya29.ACCESS","refresh_token":"HANDLE"}', 'http://127.0.0.1:5001/internal/oauth/token')
        with mock.patch.object(token_broker_manager, 'broker_token', return_value=brokered):
            credentials = RcloneConnection()._formatCredentials(self.data(gdrive_root_folder_id='1FolderId'), 'dst')
        self.assertEqual(credentials['RCLONE_CONFIG_DST_TOKEN'], brokered[0])
        self.assertEqual(credentials['RCLONE_CONFIG_DST_TOKEN_URL'], brokered[1])
        self.assertEqual(credentials['RCLONE_CONFIG_DST_ROOT_FOLDER_ID'], '1FolderId')
        self.assertNotIn('RCLONE_CONFIG_DST_TEAM_DRIVE', credentials)
        # rclone must never get a client secret
        self.assertFalse([key for key in credentials if 'CLIENT' in key])

    def test_log_masking(self):
        rclone = RcloneConnection()
        brokered = ('{"access_token":"ya29.ACCESS","refresh_token":"HANDLE"}', 'http://127.0.0.1:5001/internal/oauth/token')
        with mock.patch.object(token_broker_manager, 'broker_token', return_value=brokered):
            credentials = rclone._formatCredentials(self.data(gdrive_team_drive='0ABCDEFGHIJ', gdrive_root_folder_id='1FolderXYZW'), 'src')
        line = rclone._log_command(['rclone', 'lsjson', 'src:'], credentials)
        self.assertNotIn('ya29', line)
        self.assertNotIn('HANDLE', line)
        self.assertIn("RCLONE_CONFIG_SRC_TOKEN='***'", line)
        self.assertIn("RCLONE_CONFIG_SRC_TOKEN_URL='http://127.0.0.1:5001/internal/oauth/token'", line)
        self.assertIn("RCLONE_CONFIG_SRC_TEAM_DRIVE='***GHIJ'", line)
        self.assertIn("RCLONE_CONFIG_SRC_ROOT_FOLDER_ID='***XYZW'", line)
        self.assertIn("RCLONE_CONFIG_SRC_SCOPE='drive'", line)
        self.assertIn("RCLONE_CONFIG_SRC_CHUNK_SIZE='64Mi'", line)

    def test_rate_limit(self):
        self.assertEqual(_rate_limit_flags({'RCLONE_CONFIG_SRC_TYPE': 'drive'}), ['--tpslimit', '10'])
        self.assertEqual(_rate_limit_flags({'RCLONE_CONFIG_SRC_TYPE': 'drive', 'RCLONE_CONFIG_DST_TYPE': 'onedrive'}), ['--tpslimit', '10'])
        self.assertEqual(_rate_limit_flags({'RCLONE_CONFIG_SRC_TYPE': 'google cloud storage'}), [])
