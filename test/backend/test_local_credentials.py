import datetime
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from api.utils import local_credentials as lc
from api.utils.rclone_connection import RcloneConnection


SECRET = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYSECRETKEY1'
SECRET2 = 'je7MtGbClwBF/2Zp9Utk/h3yCo8nvbSECRETKEY2'
TOKEN = 'IQoJb3JpZ2luX2VjSESSIONTOKEN'
AZURE_KEY = 'Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=='
SAS = 'https://myacct.blob.core.windows.net/?sv=2022-11-02&ss=b&sig=SASSIGNATURESECRET'

AWS_CREDENTIALS = """
[default]
aws_access_key_id = AKIADEFAULT000000001
aws_secret_access_key = {secret}

[research]
aws_access_key_id = AKIARESEARCH0000ABCD
aws_secret_access_key = {secret2}

[temp]
aws_access_key_id = ASIATEMP000000000XYZ
aws_secret_access_key = {secret}
aws_session_token = {token}
""".format(secret=SECRET, secret2=SECRET2, token=TOKEN)

AWS_CONFIG = """
[default]
region = us-west-2

[profile research]
region = us-east-1
s3 =
    max_concurrent_requests = 20

[profile admin]
role_arn = arn:aws:iam::123456789012:role/admin
source_profile = research
role_session_name = motuz
external_id = EXTERNALIDSECRET
duration_seconds = 7200

[profile mfa]
role_arn = arn:aws:iam::123456789012:role/admin
source_profile = research
mfa_serial = arn:aws:iam::123456789012:mfa/me

[profile instance]
role_arn = arn:aws:iam::123456789012:role/admin
credential_source = Ec2InstanceMetadata

[profile chained]
role_arn = arn:aws:iam::123456789012:role/other
source_profile = admin

[profile sso]
sso_session = fh
sso_account_id = 123456789012
sso_role_name = ReadOnly
region = us-west-2

[profile legacy-sso]
sso_start_url = https://d-123.awsapps.com/start
sso_region = us-east-1
sso_account_id = 123456789012
sso_role_name = ReadOnly

[profile proc]
credential_process = /bin/sh -c 'touch /tmp/pwned'

[profile regiononly]
region = eu-west-1

[sso-session fh]
sso_start_url = https://d-123.awsapps.com/start
sso_region = us-west-2
sso_registration_scopes = sso:account:access

[services foo]
s3 =
  endpoint_url = https://example.com
"""

RCLONE_CONF = """
[mys3]
type = s3
provider = Ceph
access_key_id = CEPHKEY000000000WXYZ
secret_access_key = {secret}
endpoint = https://s3.example.org
region = other-v2-signature

[envs3]
type = s3
provider = AWS
env_auth = true

[blobkey]
type = azureblob
account = myacct
key = {azure_key}

[blobsas]
type = azureblob
sas_url = {sas}

[blobmsi]
type = azureblob
account = myacct
use_msi = true

[azurite]
type = azureblob
account = devstoreaccount1
key = {azure_key}
endpoint = http://127.0.0.1:10000/devstoreaccount1

[drive]
type = drive
token = {{"access_token":"x"}}
""".format(secret=SECRET, azure_key=AZURE_KEY, sas=SAS)

TENANT = '11111111-2222-3333-4444-555555555555'
TENANT2 = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
MSI_TENANT = '99999999-8888-7777-6666-555555555555'
# As written by `az login` (with a BOM). There are no tokens in this file.
AZURE_PROFILE = '\ufeff' + json.dumps({
    'installationId': 'x',
    'subscriptions': [
        {'id': 's1', 'name': 'Research', 'state': 'Enabled', 'isDefault': True, 'tenantId': TENANT,
         'environmentName': 'AzureCloud', 'user': {'name': 'alice@example.org', 'type': 'user'}},
        {'id': 's2', 'name': 'Lab', 'state': 'Enabled', 'isDefault': False, 'tenantId': TENANT,
         'environmentName': 'AzureCloud', 'user': {'name': 'alice@example.org', 'type': 'user'}},
        {'id': TENANT2, 'name': 'N/A(tenant level account)', 'state': 'Enabled', 'tenantId': TENANT2,
         'environmentName': 'AzureCloud', 'user': {'name': 'alice@partner.org', 'type': 'user'}},
        {'id': 's3', 'name': 'VM', 'state': 'Enabled', 'tenantId': MSI_TENANT, 'environmentName': 'AzureCloud',
         'user': {'name': 'systemAssignedIdentity', 'type': 'servicePrincipal', 'assignedIdentityInfo': 'MSI'}},
        {'id': 's4', 'name': 'bad', 'tenantId': '$(touch /tmp/x)', 'user': {'name': 'x'}},
    ],
})

ALL_SECRETS = (SECRET, SECRET2, TOKEN, AZURE_KEY, 'SASSIGNATURESECRET', 'EXTERNALIDSECRET',
               'AKIARESEARCH0000', 'CEPHKEY000000000')


def future(hours=2):
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


def fake_reader(files):
    """read_home_files replacement: files maps relpath -> content (None = unreadable)"""
    def read(user, relpaths, home=None):
        out = {}
        for rel in relpaths:
            if rel not in files:
                out[rel] = {'error': 'missing'}
            elif files[rel] is None:
                out[rel] = {'error': 'permission denied'}
            else:
                out[rel] = {'content': files[rel]}
        return '/home/alice', out
    return read


class TestAwsParsing(unittest.TestCase):

    def setUp(self):
        self.profiles, self.sessions = lc.parse_aws(AWS_CREDENTIALS, AWS_CONFIG)

    def test_profiles_merged(self):
        self.assertEqual(self.profiles['research']['region'], 'us-east-1')
        self.assertEqual(self.profiles['research']['aws_access_key_id'], 'AKIARESEARCH0000ABCD')
        self.assertEqual(self.profiles['default']['region'], 'us-west-2')
        self.assertIn('fh', self.sessions)
        self.assertNotIn('foo', self.profiles)
        self.assertNotIn('services foo', self.profiles)

    def test_credentials_file_wins(self):
        profiles, _ = lc.parse_aws('[p]\naws_access_key_id = FROMCREDS\n', '[profile p]\naws_access_key_id = FROMCONFIG\n')
        self.assertEqual(profiles['p']['aws_access_key_id'], 'FROMCREDS')

    def test_static_and_session(self):
        kind, options, _ = lc.resolve_aws_profile('research', self.profiles, self.sessions)
        self.assertEqual(kind, 'static')
        self.assertEqual(options, {'access_key_id': 'AKIARESEARCH0000ABCD', 'secret_access_key': SECRET2})
        kind, options, _ = lc.resolve_aws_profile('temp', self.profiles, self.sessions)
        self.assertEqual(kind, 'session')
        self.assertEqual(options['session_token'], TOKEN)

    def test_role_with_static_source(self):
        kind, options, _ = lc.resolve_aws_profile('admin', self.profiles, self.sessions)
        self.assertEqual(kind, 'role')
        self.assertEqual(options['role_arn'], 'arn:aws:iam::123456789012:role/admin')
        self.assertEqual(options['access_key_id'], 'AKIARESEARCH0000ABCD')
        self.assertEqual(options['role_session_name'], 'motuz')
        self.assertEqual(options['role_external_id'], 'EXTERNALIDSECRET')
        self.assertEqual(options['role_session_duration'], '7200s')

    def test_unusable_roles(self):
        for name, text in (('mfa', 'MFA'), ('instance', 'own identity'), ('chained', 'must have access keys')):
            with self.assertRaises(lc._Unusable) as cm:
                lc.resolve_aws_profile(name, self.profiles, self.sessions)
            self.assertEqual(cm.exception.kind, 'role')
            self.assertIn(text, cm.exception.reason)

    def test_credential_process_never_used(self):
        with self.assertRaises(lc._Unusable) as cm:
            lc.resolve_aws_profile('proc', self.profiles, self.sessions)
        self.assertEqual(cm.exception.kind, 'process')

    def test_region_only_is_not_a_credential(self):
        self.assertIsNone(lc.resolve_aws_profile('regiononly', self.profiles, self.sessions))
        self.assertIsNone(lc.resolve_aws_profile('missing', self.profiles, self.sessions))

    def test_sso_session_config(self):
        kind, options, config = lc.resolve_aws_profile('sso', self.profiles, self.sessions)
        self.assertEqual(kind, 'sso')
        self.assertIsNone(options)
        parsed, sessions = lc.parse_aws(None, config)
        self.assertEqual(parsed['sso'], {'sso_account_id': '123456789012', 'sso_role_name': 'ReadOnly',
                                         'region': 'us-west-2', 'sso_session': 'fh'})
        self.assertEqual(sessions['fh']['sso_start_url'], 'https://d-123.awsapps.com/start')
        self.assertEqual(lc.sso_cache_path(self.profiles['sso']),
                         '.aws/sso/cache/{}.json'.format(hashlib.sha1(b'fh').hexdigest()))

    def test_legacy_sso_config(self):
        kind, _, config = lc.resolve_aws_profile('legacy-sso', self.profiles, self.sessions)
        self.assertEqual(kind, 'sso')
        self.assertIn('sso_start_url = https://d-123.awsapps.com/start', config)
        self.assertEqual(lc.sso_cache_path(self.profiles['legacy-sso']),
                         '.aws/sso/cache/{}.json'.format(hashlib.sha1(b'https://d-123.awsapps.com/start').hexdigest()))

    def test_sso_config_only_allowlisted_keys(self):
        profiles, sessions = lc.parse_aws(None, AWS_CONFIG.replace(
            'sso_role_name = ReadOnly\nregion = us-west-2',
            'sso_role_name = ReadOnly\nregion = us-west-2\ncredential_process = /bin/evil'))
        _, _, config = lc.resolve_aws_profile('sso', profiles, sessions)
        self.assertNotIn('credential_process', config)
        self.assertNotIn('evil', config)

    def test_sso_rejects_injection(self):
        profiles, sessions = lc.parse_aws(None, '[profile s]\nsso_start_url = https://x/start\nsso_region = us-east-1\n'
                                                 'sso_account_id = 1 x\nsso_role_name = r\n')
        with self.assertRaises(lc._Unusable):
            lc.resolve_aws_profile('s', profiles, sessions)

    def test_parse_error_does_not_quote_the_file(self):
        with self.assertRaises(lc._ParseError) as cm:
            lc.parse_aws('aws_secret_access_key = {}\n'.format(SECRET), None)
        self.assertNotIn(SECRET, str(cm.exception))


class TestSsoLoginState(unittest.TestCase):

    def test_valid(self):
        note = lc.sso_login_state({'content': json.dumps({'accessToken': 'x', 'expiresAt': future()})}, 'p')
        self.assertIn('valid until', note)

    def test_expired(self):
        with self.assertRaises(lc._Unusable) as cm:
            lc.sso_login_state({'content': json.dumps({'accessToken': 'x', 'expiresAt': future(-1)})}, 'p')
        self.assertIn('aws sso login --profile p', cm.exception.reason)
        self.assertIn('expired', cm.exception.reason)

    def test_expired_but_refreshable(self):
        note = lc.sso_login_state({'content': json.dumps({
            'accessToken': 'x', 'expiresAt': future(-1).replace('Z', 'UTC'), 'refreshToken': 'r',
            'clientId': 'c', 'registrationExpiresAt': future(24)})}, 'p')
        self.assertIn('renewed', note)

    def test_not_signed_in(self):
        with self.assertRaises(lc._Unusable) as cm:
            lc.sso_login_state({'error': 'missing'}, 'p')
        self.assertIn('Not signed in', cm.exception.reason)


class TestRclone(unittest.TestCase):

    def test_encrypted(self):
        with self.assertRaises(lc.LocalCredentialsError):
            lc.parse_rclone('# Encrypted rclone configuration File\n\nRCLONE_ENCRYPT_V0:\nabcdef')

    def test_classify(self):
        remotes = lc.parse_rclone(RCLONE_CONF)
        kind, options = lc.classify_rclone_remote('s3', remotes['mys3'])
        self.assertEqual(kind, 'keys')
        self.assertEqual(options['provider'], 'Ceph')
        self.assertEqual(options['endpoint'], 'https://s3.example.org')
        self.assertNotIn('type', options)
        with self.assertRaises(lc._Unusable):
            lc.classify_rclone_remote('s3', remotes['envs3'])
        self.assertIsNone(lc.classify_rclone_remote('s3', remotes['blobkey']))
        self.assertIsNone(lc.classify_rclone_remote('azureblob', remotes['drive']))
        self.assertEqual(lc.classify_rclone_remote('azureblob', remotes['blobkey'])[0], 'account_key')
        self.assertEqual(lc.classify_rclone_remote('azureblob', remotes['blobsas'])[0], 'sas_url')
        with self.assertRaises(lc._Unusable):
            lc.classify_rclone_remote('azureblob', remotes['blobmsi'])
        kind, options = lc.classify_rclone_remote('azureblob', remotes['azurite'])
        self.assertEqual(options['endpoint'], 'http://127.0.0.1:10000/devstoreaccount1')

    def test_use_az_remote(self):
        remotes = lc.parse_rclone('[az]\ntype = azureblob\naccount = myacct\nuse_az = true\n'
                                  'tenant = {}\nkey = {}\n'.format(TENANT, AZURE_KEY))
        with mock.patch.object(lc, 'azure_cli_installed', return_value=False):
            with self.assertRaises(lc._Unusable):
                lc.classify_rclone_remote('azureblob', remotes['az'])
        with mock.patch.object(lc, 'azure_cli_installed', return_value=True):
            kind, options = lc.classify_rclone_remote('azureblob', remotes['az'])
        self.assertEqual(kind, 'azure_cli')
        self.assertEqual(options, {'account': 'myacct', 'tenant': TENANT, 'use_az': 'true'})

    def test_env_auth_is_never_passed(self):
        remotes = lc.parse_rclone('[x]\ntype = s3\naccess_key_id = A\nsecret_access_key = B\nenv_auth = true\n')
        _, options = lc.classify_rclone_remote('s3', remotes['x'])
        self.assertNotIn('env_auth', options)

    def test_azure_account(self):
        self.assertEqual(lc._azure_account('sas_url', {'sas_url': SAS}), 'myacct')
        self.assertEqual(lc._azure_account('connection_string', {
            'connection_string': 'DefaultEndpointsProtocol=https;AccountName=acct2;AccountKey=xyz;EndpointSuffix=core.windows.net'}), 'acct2')


class TestDiscover(unittest.TestCase):

    def files(self, **extra):
        files = {
            lc.AWS_CREDENTIALS: AWS_CREDENTIALS,
            lc.AWS_CONFIG: AWS_CONFIG,
            lc.RCLONE_CONF: RCLONE_CONF,
            lc.sso_cache_path({'sso_session': 'fh'}): json.dumps({'accessToken': 'SSOACCESSTOKEN', 'expiresAt': future()}),
        }
        files.update(extra)
        return files

    def test_s3(self):
        with mock.patch.object(lc, 'read_home_files', fake_reader(self.files())):
            result = lc.discover('alice', 's3')
        raw = json.dumps(result)
        for secret in ALL_SECRETS + ('SSOACCESSTOKEN',):
            self.assertNotIn(secret, raw)
        by_name = {(p['source'], p['name']): p for p in result['profiles']}
        research = by_name[('aws', 'research')]
        self.assertEqual((research['kind'], research['usable'], research['region'], research['access_key_id']),
                         ('static', True, 'us-east-1', '****ABCD'))
        self.assertEqual(by_name[('aws', 'temp')]['kind'], 'session')
        self.assertTrue(by_name[('aws', 'admin')]['usable'])
        self.assertTrue(by_name[('aws', 'sso')]['usable'])
        self.assertIn('valid until', by_name[('aws', 'sso')]['note'])
        self.assertFalse(by_name[('aws', 'legacy-sso')]['usable'])
        self.assertIn('aws sso login', by_name[('aws', 'legacy-sso')]['reason'])
        self.assertFalse(by_name[('aws', 'proc')]['usable'])
        self.assertFalse(by_name[('aws', 'mfa')]['usable'])
        self.assertNotIn(('aws', 'regiononly'), by_name)
        self.assertEqual(by_name[('rclone', 'mys3')]['access_key_id'], '****WXYZ')
        self.assertFalse(by_name[('rclone', 'envs3')]['usable'])
        self.assertNotIn(('rclone', 'blobkey'), by_name)
        self.assertEqual(result['notes'], [])

    def test_azure(self):
        files = self.files(**{lc.AZURE_PROFILE: AZURE_PROFILE})
        with mock.patch.object(lc, 'read_home_files', fake_reader(files)), \
                mock.patch.object(lc, 'azure_cli_installed', return_value=True):
            result = lc.discover('alice', 'azureblob')
        raw = json.dumps(result)
        for secret in ALL_SECRETS:
            self.assertNotIn(secret, raw)
        by_name = {p['name']: p for p in result['profiles']}
        self.assertEqual(set(by_name), {'blobkey', 'blobsas', 'blobmsi', 'azurite', TENANT, TENANT2, MSI_TENANT})
        self.assertEqual((by_name['blobkey']['account'], by_name['blobkey']['kind']), ('myacct', 'account_key'))
        self.assertEqual(by_name['blobsas']['account'], 'myacct')
        self.assertFalse(by_name['blobmsi']['usable'])
        login = by_name[TENANT]
        self.assertEqual((login['source'], login['kind'], login['usable']), ('azure-cli', 'azure_cli', True))
        self.assertEqual(login['note'], 'signed in as alice@example.org; subscriptions: Lab, Research')
        self.assertEqual(by_name[TENANT2]['note'], 'signed in as alice@partner.org')
        self.assertFalse(by_name[MSI_TENANT]['usable'])
        self.assertIn('managed identity', by_name[MSI_TENANT]['reason'])

    def test_azure_cli_not_installed(self):
        files = {lc.AZURE_PROFILE: AZURE_PROFILE}
        with mock.patch.object(lc, 'read_home_files', fake_reader(files)), \
                mock.patch.object(lc, 'azure_cli_installed', return_value=False):
            result = lc.discover('alice', 'azureblob')
        login = {p['name']: p for p in result['profiles']}[TENANT]
        self.assertFalse(login['usable'])
        self.assertIn('INSTALL_AZURE_CLI', login['reason'])

    def test_azure_profile_unparseable(self):
        for content in ('not json', '[]', '{"subscriptions": 1}'):
            with mock.patch.object(lc, 'read_home_files', fake_reader({lc.AZURE_PROFILE: content})):
                result = lc.discover('alice', 'azureblob')
            self.assertEqual(result['profiles'], [])
            self.assertEqual(result['notes'], ['~/.azure/azureProfile.json could not be parsed'])

    def test_unreadable_and_encrypted(self):
        files = {lc.AWS_CREDENTIALS: None, lc.RCLONE_CONF: 'RCLONE_ENCRYPT_V0:\nxyz'}
        with mock.patch.object(lc, 'read_home_files', fake_reader(files)):
            result = lc.discover('alice', 's3')
        self.assertEqual(result['profiles'], [])
        self.assertIn('~/.aws/credentials: permission denied', result['notes'])
        self.assertIn('~/.config/rclone/rclone.conf is encrypted, Motuz cannot import it', result['notes'])

    def test_legacy_rclone_conf(self):
        files = {lc.RCLONE_CONF_LEGACY: RCLONE_CONF}
        with mock.patch.object(lc, 'read_home_files', fake_reader(files)):
            result = lc.discover('alice', 's3')
        self.assertEqual([p['file'] for p in result['profiles'] if p['name'] == 'mys3'], ['~/.rclone.conf'])

    def test_unparseable_file_is_not_quoted(self):
        files = {lc.AWS_CREDENTIALS: 'aws_secret_access_key = {}\n'.format(SECRET)}
        with mock.patch.object(lc, 'read_home_files', fake_reader(files)):
            result = lc.discover('alice', 's3')
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertTrue(result['notes'])


class TestResolve(unittest.TestCase):

    def resolve(self, files, *args, **kwargs):
        with mock.patch.object(lc, 'read_home_files', fake_reader(files)):
            return lc.resolve(*args, **kwargs)

    def test_static(self):
        options, env = self.resolve({lc.AWS_CREDENTIALS: AWS_CREDENTIALS, lc.AWS_CONFIG: AWS_CONFIG},
                                    'alice', 's3', 'aws', 'research')
        self.assertEqual(options, {'access_key_id': 'AKIARESEARCH0000ABCD', 'secret_access_key': SECRET2, 'region': 'us-east-1'})
        self.assertEqual(env, {})

    def test_errors(self):
        files = {lc.AWS_CREDENTIALS: AWS_CREDENTIALS, lc.AWS_CONFIG: AWS_CONFIG}
        cases = (
            (('s3', 'aws', 'nope'), 'not found'),
            (('s3', 'aws', 'proc'), 'credential_process'),
            (('azureblob', 'aws', 'research'), 'Unknown credential source'),
            (('s3', 'aws', '../x'), 'Invalid profile name'),
            (('swift', 'aws', 'research'), 'Only S3 and Azure'),
        )
        for args, text in cases:
            with self.assertRaises(lc.LocalCredentialsError) as cm:
                self.resolve(files, 'alice', *args)
            self.assertIn(text, str(cm.exception))
            for secret in ALL_SECRETS:
                self.assertNotIn(secret, str(cm.exception))

    def test_unreadable_file(self):
        with self.assertRaises(lc.LocalCredentialsError) as cm:
            self.resolve({lc.AWS_CREDENTIALS: None}, 'alice', 's3', 'aws', 'research')
        self.assertIn('permission denied', str(cm.exception))

    def test_rclone(self):
        options, env = self.resolve({lc.RCLONE_CONF: RCLONE_CONF}, 'alice', 'azureblob', 'rclone', 'blobsas')
        self.assertEqual(options, {'sas_url': SAS})
        with self.assertRaises(lc.LocalCredentialsError):
            self.resolve({lc.RCLONE_CONF: RCLONE_CONF}, 'alice', 's3', 'rclone', 'blobsas')
        with self.assertRaises(lc.LocalCredentialsError) as cm:
            self.resolve({lc.RCLONE_CONF: 'RCLONE_ENCRYPT_V0:\nx'}, 'alice', 's3', 'rclone', 'mys3')
        self.assertIn('encrypted', str(cm.exception))

    def test_azure_cli(self):
        files = {lc.AZURE_PROFILE: AZURE_PROFILE}
        with mock.patch.object(lc, 'azure_cli_installed', return_value=True):
            options, env = self.resolve(files, 'alice', 'azureblob', 'azure-cli', TENANT.upper())
            self.assertEqual(options, {'use_az': 'true', 'tenant': TENANT})
            self.assertEqual(env['HOME'], '/home/alice')
            self.assertEqual(env['AZURE_CONFIG_DIR'], '/home/alice/.azure')
            # Never the user's az extensions, never the server's managed identity
            self.assertEqual(env['AZURE_EXTENSION_DEV_SOURCES'], '')
            self.assertTrue(env['AZURE_EXTENSION_DIR'].startswith('/nonexistent/'))
            self.assertTrue(env['IDENTITY_ENDPOINT'].startswith('http://127.0.0.1:'))
            self.assertEqual((env['PYTHONNOUSERSITE'], env['AZURE_CORE_COLLECT_TELEMETRY']), ('1', 'false'))

            cases = (
                ((MSI_TENANT,), 'managed identity'),
                (('00000000-0000-0000-0000-000000000000',), 'az login --tenant'),
                (('not-a-tenant',), 'Invalid Azure tenant id'),
            )
            for args, text in cases:
                with self.assertRaises(lc.LocalCredentialsError) as cm:
                    self.resolve(files, 'alice', 'azureblob', 'azure-cli', *args)
                self.assertIn(text, str(cm.exception))
            with self.assertRaises(lc.LocalCredentialsError) as cm:
                self.resolve({}, 'alice', 'azureblob', 'azure-cli', TENANT)
            self.assertIn('run `az login`', str(cm.exception))
            with self.assertRaises(lc.LocalCredentialsError) as cm:
                self.resolve(files, 'alice', 's3', 'azure-cli', TENANT)
            self.assertIn('Unknown credential source', str(cm.exception))
        with mock.patch.object(lc, 'azure_cli_installed', return_value=False):
            with self.assertRaises(lc.LocalCredentialsError) as cm:
                self.resolve(files, 'alice', 'azureblob', 'azure-cli', TENANT)
            self.assertIn('not installed', str(cm.exception))

    def test_rclone_use_az_gets_the_az_environment(self):
        conf = '[az]\ntype = azureblob\naccount = myacct\nuse_az = true\n'
        with mock.patch.object(lc, 'azure_cli_installed', return_value=True):
            options, env = self.resolve({lc.RCLONE_CONF: conf}, 'alice', 'azureblob', 'rclone', 'az')
        self.assertEqual(options, {'account': 'myacct', 'use_az': 'true'})
        self.assertEqual(env['AZURE_CONFIG_DIR'], '/home/alice/.azure')

    def test_sso(self):
        files = {
            lc.AWS_CONFIG: AWS_CONFIG,
            lc.sso_cache_path({'sso_session': 'fh'}): json.dumps({'accessToken': 'SSOACCESSTOKEN', 'expiresAt': future()}),
        }
        with tempfile.TemporaryDirectory() as tmp:
            directory = os.path.join(tmp, 'sso')
            with mock.patch.object(lc, 'SSO_CONFIG_DIR', directory):
                options, env = self.resolve(files, 'alice', 's3', 'aws', 'sso')
                path = options['shared_credentials_file']
                self.assertEqual(os.path.dirname(path), directory)
                self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o711)
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
                with open(path) as f:
                    content = f.read()
                self.assertNotIn('SSOACCESSTOKEN', content)
                self.assertIn('[sso-session fh]', content)
                self.assertEqual(options['env_auth'], 'true')
                self.assertEqual(options['profile'], 'sso')
                self.assertEqual(env['HOME'], '/home/alice')
                self.assertEqual(env['AWS_SHARED_CREDENTIALS_FILE'], '/dev/null')
                # Same content, same file
                self.assertEqual(self.resolve(files, 'alice', 's3', 'aws', 'sso')[0]['shared_credentials_file'], path)

    def test_sso_expired(self):
        files = {
            lc.AWS_CONFIG: AWS_CONFIG,
            lc.sso_cache_path({'sso_session': 'fh'}): json.dumps({'accessToken': 'x', 'expiresAt': future(-3)}),
        }
        with self.assertRaises(lc.LocalCredentialsError) as cm:
            self.resolve(files, 'alice', 's3', 'aws', 'sso')
        self.assertIn('SSO login expired', str(cm.exception))


class TestReadHomeFiles(unittest.TestCase):

    def test_runs_as_the_user(self):
        completed = subprocess.CompletedProcess([], 0, stdout=b'{}', stderr=b'')
        with mock.patch.object(lc.subprocess, 'run', return_value=completed) as run:
            home, files = lc.read_home_files('alice', [lc.AWS_CREDENTIALS], home='/home/alice')
        command = run.call_args[0][0]
        self.assertEqual(command[:6], ['sudo', '-n', '-u', 'alice', '--', 'env'])
        self.assertEqual(command[-3:], [str(lc.MAX_FILE_SIZE), '/home/alice', lc.AWS_CREDENTIALS])
        self.assertIn('timeout', run.call_args[1])

    def test_only_relative_paths(self):
        for rel in ('/etc/shadow', '../bob/.aws/credentials', '.aws/../../x'):
            with self.assertRaises(ValueError):
                lc.read_home_files('alice', [rel], home='/home/alice')

    def test_reader_script(self):
        """The reader itself (run here as the current user)"""
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, '.aws'))
            with open(os.path.join(home, '.aws/credentials'), 'w') as f:
                f.write('[p]\n')
            with open(os.path.join(home, 'big'), 'w') as f:
                f.write('x' * 101)
            os.mkfifo(os.path.join(home, 'fifo'))
            os.symlink('/dev/zero', os.path.join(home, 'dev'))
            locked = os.path.join(home, 'locked')
            with open(locked, 'w') as f:
                f.write('secret')
            os.chmod(locked, 0)
            os.symlink(locked, os.path.join(home, 'link-to-locked'))
            rels = ['.aws/credentials', 'big', 'fifo', 'dev', 'locked', 'link-to-locked', 'missing', '.aws/credentials/x']
            result = subprocess.run([sys.executable, '-I', '-S', '-c', lc._READER, '100', home, *rels],
                                    capture_output=True, timeout=20, check=True)
            out = json.loads(result.stdout)
        self.assertEqual(out['.aws/credentials'], {'content': '[p]\n'})
        self.assertEqual(out['big'], {'error': 'too large'})
        self.assertEqual(out['fifo'], {'error': 'not a regular file'})
        self.assertEqual(out['dev'], {'error': 'not a regular file'})
        self.assertEqual(out['missing'], {'error': 'missing'})
        self.assertEqual(out['.aws/credentials/x'], {'error': 'missing'})
        if os.geteuid() != 0:
            self.assertEqual(out['locked'], {'error': 'permission denied'})
            self.assertEqual(out['link-to-locked'], {'error': 'permission denied'})


class TestRcloneFormatCredentials(unittest.TestCase):

    def connection(self, **fields):
        base = dict(type='s3', subtype='profile', owner='alice', profile_source='aws', profile_name='research',
                    s3_region=None, s3_endpoint=None, s3_v2_auth=None, kms_encryption_key_arn=None,
                    s3_access_key_id='IGNORED', s3_secret_access_key='IGNORED', s3_session_token=None,
                    azure_account=None, azure_key=None, azure_sas_url=None)
        base.update(fields)
        return SimpleNamespace(**base)

    def test_profile_s3(self):
        resolved = ({'access_key_id': 'AKIARESEARCH0000ABCD', 'secret_access_key': SECRET2, 'region': 'us-east-1'}, {})
        with mock.patch.object(lc, 'resolve', return_value=resolved) as resolve:
            credentials = RcloneConnection()._formatCredentials(self.connection(s3_region='us-west-2'), 'src')
        resolve.assert_called_once_with('alice', 's3', 'aws', 'research')
        self.assertEqual(credentials['RCLONE_CONFIG_SRC_ACCESS_KEY_ID'], 'AKIARESEARCH0000ABCD')
        self.assertEqual(credentials['RCLONE_CONFIG_SRC_SECRET_ACCESS_KEY'], SECRET2)
        self.assertEqual(credentials['RCLONE_CONFIG_SRC_REGION'], 'us-west-2') # connection wins
        self.assertEqual(credentials['RCLONE_CONFIG_SRC_PROVIDER'], 'AWS')

    def test_profile_rclone_provider(self):
        resolved = ({'provider': 'Ceph', 'endpoint': 'https://s3.example.org', 'access_key_id': 'A', 'secret_access_key': 'B'}, {})
        with mock.patch.object(lc, 'resolve', return_value=resolved):
            credentials = RcloneConnection()._formatCredentials(self.connection(profile_source='rclone'), 'dst')
        self.assertEqual(credentials['RCLONE_CONFIG_DST_PROVIDER'], 'Ceph')
        self.assertEqual(credentials['RCLONE_CONFIG_DST_ENDPOINT'], 'https://s3.example.org')

    def test_profile_azure(self):
        resolved = ({'sas_url': SAS}, {})
        with mock.patch.object(lc, 'resolve', return_value=resolved):
            credentials = RcloneConnection()._formatCredentials(self.connection(type='azureblob', profile_source='rclone'), 'current')
        self.assertEqual(credentials, {'RCLONE_CONFIG_CURRENT_TYPE': 'azureblob', 'RCLONE_CONFIG_CURRENT_SAS_URL': SAS})

    def test_profile_azure_cli_adds_the_storage_account(self):
        resolved = ({'use_az': 'true', 'tenant': TENANT}, {'HOME': '/home/alice', 'AZURE_CONFIG_DIR': '/home/alice/.azure'})
        connection = self.connection(type='azureblob', profile_source='azure-cli', profile_name=TENANT, azure_account='myacct')
        with mock.patch.object(lc, 'resolve', return_value=resolved):
            credentials = RcloneConnection()._formatCredentials(connection, 'src')
        self.assertEqual(credentials, {
            'RCLONE_CONFIG_SRC_TYPE': 'azureblob',
            'RCLONE_CONFIG_SRC_USE_AZ': 'true',
            'RCLONE_CONFIG_SRC_TENANT': TENANT,
            'RCLONE_CONFIG_SRC_ACCOUNT': 'myacct',
            'HOME': '/home/alice',
            'AZURE_CONFIG_DIR': '/home/alice/.azure',
        })

    def test_verify_reports_profile_errors(self):
        with mock.patch.object(lc, 'resolve', side_effect=lc.LocalCredentialsError('AWS profile "x" not found')):
            result = RcloneConnection().verify(self.connection())
        self.assertEqual(result, {'result': False, 'message': 'AWS profile "x" not found'})

    def test_log_masks_secrets(self):
        credentials = {
            'RCLONE_CONFIG_SRC_SAS_URL': SAS,
            'RCLONE_CONFIG_SRC_CONNECTION_STRING': 'AccountName=a;AccountKey=' + AZURE_KEY,
            'RCLONE_CONFIG_SRC_CLIENT_SECRET': 'CLIENTSECRET',
            'RCLONE_CONFIG_SRC_ROLE_EXTERNAL_ID': 'EXTERNALIDSECRET',
            'RCLONE_CONFIG_SRC_SESSION_TOKEN': TOKEN,
            'RCLONE_CONFIG_SRC_SECRET_ACCESS_KEY': SECRET,
            'RCLONE_CONFIG_SRC_ACCESS_KEY_ID': 'AKIARESEARCH0000ABCD',
            'RCLONE_CONFIG_SRC_ROLE_ARN': 'arn:aws:iam::1:role/r',
            'RCLONE_CONFIG_SRC_PROFILE': 'sso',
            'HOME': '/home/alice',
        }
        import logging
        logging.disable(logging.INFO)
        try:
            line = RcloneConnection()._log_command(['rclone'], credentials)
        finally:
            logging.disable(logging.NOTSET)
        for secret in ALL_SECRETS + ('CLIENTSECRET',):
            self.assertNotIn(secret, line)
        self.assertIn("RCLONE_CONFIG_SRC_ACCESS_KEY_ID='***ABCD'", line)
        self.assertIn("RCLONE_CONFIG_SRC_ROLE_ARN='arn:aws:iam::1:role/r'", line)
        self.assertIn("HOME='/home/alice'", line)


if __name__ == '__main__':
    unittest.main()


class TestProfileRules(unittest.TestCase):
    """cloud_connection_manager._apply_profile_rules"""

    def apply(self, **fields):
        from api.managers import cloud_connection_manager
        connection = SimpleNamespace(**dict(dict(
            type='azureblob', subtype='profile', owner='alice', profile_source='azure-cli', profile_name=TENANT,
            azure_account='myacct', azure_key='OLDKEY', azure_sas_url=None), **fields))
        with mock.patch.object(lc, 'resolve', return_value=({}, {})):
            cloud_connection_manager._apply_profile_rules(connection)
        return connection

    def test_azure_cli_keeps_the_account(self):
        connection = self.apply(azure_account=' myacct ')
        self.assertEqual((connection.azure_account, connection.azure_key), ('myacct', None))

    def test_azure_cli_needs_a_valid_account(self):
        from api.exceptions import HTTP_400_BAD_REQUEST
        for account in (None, '', 'My-Account', 'a' * 25):
            with self.assertRaises(HTTP_400_BAD_REQUEST):
                self.apply(azure_account=account)

    def test_other_sources_drop_the_account(self):
        connection = self.apply(profile_source='rclone', profile_name='blobsas')
        self.assertIsNone(connection.azure_account)
