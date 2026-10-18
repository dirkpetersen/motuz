"""
Options of S3 connections: rclone's provider, chunked uploads and skipping checksum
checks (utils/s3_providers.py, RcloneConnection._formatCredentials / destination_checks,
cloud_connection_manager._apply_s3_rules), and the frontend's copy of the provider list.
"""
import json
import logging
import os
import re
import types
import unittest
from unittest import mock

from api.exceptions import HTTP_400_BAD_REQUEST
from api.managers import cloud_connection_manager
from api.utils import rclone_tuning, s3_providers
from api.utils.abstract_connection import RcloneException
from api.utils.rclone_connection import RcloneConnection, checked_job_env, destination_checks

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def s3(**columns):
    values = dict(type='s3', subtype='key', owner='alice', s3_access_key_id='AKIDEXAMPLE1234',
                  s3_secret_access_key='secret', s3_region='us-east-1', s3_endpoint=None, s3_v2_auth=None,
                  kms_encryption_key_arn=None, skip_tls_verify=False, s3_provider=None, s3_chunked=True,
                  s3_skip_checks=None)
    values.update(columns)
    return types.SimpleNamespace(**values)


class TestProviderList(unittest.TestCase):

    def test_aws_first_ceph_second_other_last(self):
        values = [value for value, _ in s3_providers.PROVIDERS]
        self.assertEqual(values[:2], ['AWS', 'Ceph'])
        self.assertEqual(values[-1], 'Other')
        self.assertEqual(len(values), len(set(values)))
        self.assertIn('Minio', values)
        self.assertEqual(s3_providers.CHECK_SKIPPING_PROVIDERS, {'AWS', 'Ceph'})
        self.assertEqual(s3_providers.CRC32C_PROVIDERS, {'AWS'})
        with open(os.path.join(REPO, 'src', 'frontend', 'js', 'utils', 's3Providers.js')) as f:
            self.assertIn("export const CRC32C_PROVIDERS = ['AWS'];", f.read())

    def test_frontend_has_the_same_list(self):
        with open(os.path.join(REPO, 'src', 'frontend', 'js', 'utils', 's3Providers.js')) as f:
            js = f.read()
        block = js[js.index('export const S3_PROVIDERS'):js.index('];')]
        frontend = [json.loads(v) for v in re.findall(r'value: ("[^"]+")', block)]
        self.assertEqual(frontend, [value for value, _ in s3_providers.PROVIDERS])


class TestRemoteConfiguration(unittest.TestCase):

    def env(self, connection, name='dst'):
        return RcloneConnection()._formatCredentials(connection, name)

    def test_provider_as_chosen(self):
        self.assertEqual(self.env(s3(s3_provider='Ceph', s3_endpoint='https://s3.hpc.example'))['RCLONE_CONFIG_DST_PROVIDER'], 'Ceph')
        self.assertEqual(self.env(s3(s3_provider='Minio'))['RCLONE_CONFIG_DST_PROVIDER'], 'Minio')

    def test_legacy_connections_without_provider(self):
        self.assertEqual(self.env(s3())['RCLONE_CONFIG_DST_PROVIDER'], 'AWS')
        self.assertEqual(self.env(s3(s3_endpoint='https://x'))['RCLONE_CONFIG_DST_PROVIDER'], 'Other')
        # an unknown value never reaches rclone
        self.assertEqual(self.env(s3(s3_provider='Evil'))['RCLONE_CONFIG_DST_PROVIDER'], 'AWS')

    def test_chunked_uploads_per_remote(self):
        self.assertEqual(self.env(s3())['RCLONE_CONFIG_DST_UPLOAD_CUTOFF'], '32Mi')
        self.assertEqual(self.env(s3(s3_chunked=False))['RCLONE_CONFIG_DST_UPLOAD_CUTOFF'], '5Gi')
        # with skipped TLS verification the setting moves to the inner remote
        env = self.env(s3(s3_chunked=False, skip_tls_verify=True))
        self.assertEqual(env['RCLONE_CONFIG_DSTTLSOFF_UPLOAD_CUTOFF'], '5Gi')

    def test_cutoff_is_logged(self):
        rclone = RcloneConnection()
        line = rclone._log_command(['rclone'], self.env(s3(s3_provider='Ceph')))
        self.assertIn("RCLONE_CONFIG_DST_UPLOAD_CUTOFF='32Mi'", line)
        self.assertIn("RCLONE_CONFIG_DST_PROVIDER='Ceph'", line)


class TestDestinationChecks(unittest.TestCase):

    def test_post_copy_and_all(self):
        self.assertEqual(destination_checks(s3(s3_provider='AWS', s3_skip_checks='post_copy')), (['--ignore-checksum'], {}))
        self.assertEqual(destination_checks(s3(s3_provider='Ceph', s3_skip_checks='all')),
                         (['--ignore-checksum'], {'AWS_REQUEST_CHECKSUM_CALCULATION': 'when_required',
                                                  'RCLONE_S3_PART_CHECKSUM': 'none'}))

    def test_crc32c_only_on_aws(self):
        self.assertEqual(destination_checks(s3(s3_provider='AWS', s3_skip_checks='crc32c')),
                         (['--ignore-checksum'], {'RCLONE_S3_PART_CHECKSUM': 'crc32c'}))
        # stored before the provider changed: full checks, never a silent weaker one
        self.assertEqual(destination_checks(s3(s3_provider='Ceph', s3_skip_checks='crc32c')), ([], {}))

    def test_only_aws_and_ceph(self):
        for provider in ('Minio', 'Other', None):
            self.assertEqual(destination_checks(s3(s3_provider=provider, s3_skip_checks='all')), ([], {}), provider)
        self.assertEqual(destination_checks(types.SimpleNamespace(type='azureblob', s3_provider='AWS', s3_skip_checks='all')), ([], {}))
        self.assertEqual(destination_checks(None), ([], {}))
        self.assertEqual(destination_checks(s3(s3_provider='AWS')), ([], {}))

    def test_copy_command(self):
        connection = RcloneConnection()
        pushed = {}
        with mock.patch.object(rclone_tuning, '_settings', rclone_tuning.load_settings({})), \
                mock.patch.object(connection._copy_job_queue, 'push',
                                  side_effect=lambda command, env, job_id, **_: pushed.update(command=command, env=env)):
            logging.disable(logging.INFO)
            try:
                connection.copy(None, '/tmp', s3(s3_provider='AWS', s3_skip_checks='all'), 'bucket/x', 'alice', False, 1)
            finally:
                logging.disable(logging.NOTSET)
        command, env = pushed['command'], pushed['env']
        self.assertIn('--ignore-checksum', command[command.index('/usr/local/bin/rclone'):command.index('copyto')])
        self.assertEqual(env['AWS_REQUEST_CHECKSUM_CALCULATION'], 'when_required')
        preserve = [item for item in command if item.startswith('--preserve-env=')][0]
        self.assertIn('AWS_REQUEST_CHECKSUM_CALCULATION', preserve.split('=', 1)[1].split(','))

    def test_a_source_never_skips_checks_of_the_destination(self):
        connection = RcloneConnection()
        pushed = {}
        with mock.patch.object(rclone_tuning, '_settings', rclone_tuning.load_settings({})), \
                mock.patch.object(connection._copy_job_queue, 'push',
                                  side_effect=lambda command, env, job_id, **_: pushed.update(command=command, env=env)):
            logging.disable(logging.INFO)
            try:
                connection.copy(s3(s3_provider='AWS', s3_skip_checks='all'), 'bucket/x', s3(s3_provider='Minio'),
                                'other/y', 'alice', False, 1)
            finally:
                logging.disable(logging.NOTSET)
        self.assertNotIn('--ignore-checksum', pushed['command'])
        self.assertNotIn('AWS_REQUEST_CHECKSUM_CALCULATION', pushed['env'])

    def test_worker_accepts_only_known_job_env(self):
        self.assertEqual(checked_job_env(None), {})
        self.assertEqual(checked_job_env({'AWS_REQUEST_CHECKSUM_CALCULATION': 'when_required'}),
                         {'AWS_REQUEST_CHECKSUM_CALCULATION': 'when_required'})
        accepted = {'RCLONE_S3_PART_CHECKSUM': 'crc32c', 'RCLONE_MD5_SIMD': 'true', 'RCLONE_MD5_SIMD_SERVERS': '16'}
        self.assertEqual(checked_job_env(accepted), accepted)
        for bad in ({'AWS_REQUEST_CHECKSUM_CALCULATION': 'x'}, {'LD_PRELOAD': '/tmp/x.so'}, {'PATH': '/tmp'},
                    {'RCLONE_S3_PART_CHECKSUM': 'md5 --config=/x'}, {'RCLONE_MD5_SIMD_SERVERS': '0'},
                    {'RCLONE_MD5_SIMD_SERVERS': '17'}, {'RCLONE_CONFIG': '/etc/shadow'}):
            with self.assertRaises(RcloneException):
                checked_job_env(bad)


class TestValidation(unittest.TestCase):

    def apply(self, **columns):
        connection = s3(**columns)
        cloud_connection_manager._apply_s3_rules(connection)
        return connection

    def test_defaults(self):
        c = self.apply(s3_chunked=None)
        self.assertEqual((c.s3_provider, c.s3_chunked, c.s3_skip_checks), ('AWS', True, None))
        self.assertEqual(self.apply(s3_endpoint='https://x').s3_provider, 'Other')

    def test_skip_checks_only_for_aws_and_ceph(self):
        self.assertEqual(self.apply(s3_provider='Ceph', s3_skip_checks='post_copy').s3_skip_checks, 'post_copy')
        self.assertIsNone(self.apply(s3_provider='Minio', s3_skip_checks='all').s3_skip_checks)
        self.assertIsNone(self.apply(s3_provider='AWS', s3_skip_checks='').s3_skip_checks)

    def test_crc32c_only_for_aws(self):
        self.assertEqual(self.apply(s3_provider='AWS', s3_skip_checks='crc32c').s3_skip_checks, 'crc32c')
        with self.assertRaises(HTTP_400_BAD_REQUEST):
            self.apply(s3_provider='Ceph', s3_skip_checks='crc32c')
        # providers that skip no checks drop it like the others
        self.assertIsNone(self.apply(s3_provider='Minio', s3_skip_checks='crc32c').s3_skip_checks)

    def test_invalid_values(self):
        with self.assertRaises(HTTP_400_BAD_REQUEST):
            self.apply(s3_provider='Evil')
        with self.assertRaises(HTTP_400_BAD_REQUEST):
            self.apply(s3_provider='AWS', s3_skip_checks='everything')

    def test_other_types_keep_none(self):
        c = types.SimpleNamespace(type='azureblob', s3_provider='AWS', s3_skip_checks='all', s3_chunked=False)
        cloud_connection_manager._apply_s3_rules(c)
        self.assertEqual((c.s3_provider, c.s3_skip_checks, c.s3_chunked), (None, None, True))
