import unittest
import sys
import os
import logging

from api.utils.rclone_connection import RcloneConnection


class TestLogCommand(unittest.TestCase):

    def setUp(self):
        self.rclone_connection = RcloneConnection()


    @classmethod
    def setUpClass(cls):
        logging.disable(logging.INFO)


    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)


    def test_allowlist(self):
        credentials = {
            "RCLONE_CONFIG_SRC_REGION": "one"
        }
        expected = "RCLONE_CONFIG_SRC_REGION='one' foo"
        self.assertEqual(self.rclone_connection._log_command(["foo"], credentials), expected)


    def test_blocklist(self):
        credentials = {
            "SECRET_ACCESS_KEY": "one two three"
        }
        expected = "SECRET_ACCESS_KEY='***' foo"
        self.assertEqual(self.rclone_connection._log_command(["foo"], credentials), expected)


    def test_no_list(self):
        credentials = {
            "SECRET_ACCESS_KEY": "one two three"
        }
        expected = "SECRET_ACCESS_KEY='***' foo"
        self.assertEqual(self.rclone_connection._log_command(["foo"], credentials), expected)


    def test_partial_allow(self):
        credentials = {
            "RCLONE_CONFIG_SRC_ACCESS_KEY_ID": "KHSNRYQPOFYN"
        }
        expected = "RCLONE_CONFIG_SRC_ACCESS_KEY_ID='***OFYN' foo"
        self.assertEqual(self.rclone_connection._log_command(["foo"], credentials), expected)


class TestSkipTlsVerify(unittest.TestCase):
    """Ignore certificate errors: only the connection's own remote stops checking"""

    def _connection(self, **columns):
        import types
        values = dict(type='s3', subtype='key', owner='alice', s3_access_key_id='AKIDEXAMPLE1234',
                      s3_secret_access_key='secret', s3_region='us-east-1',
                      s3_endpoint='s3.example.edu', s3_v2_auth=None, kms_encryption_key_arn=None)
        values.update(columns)
        return types.SimpleNamespace(**values)

    def test_off_keeps_plain_remote(self):
        credentials = RcloneConnection()._formatCredentials(self._connection(skip_tls_verify=False), 'src')
        self.assertEqual(credentials['RCLONE_CONFIG_SRC_TYPE'], 's3')
        self.assertFalse(any('TLSOFF' in key for key in credentials))

    def test_on_wraps_remote_in_alias_with_override(self):
        credentials = RcloneConnection()._formatCredentials(self._connection(skip_tls_verify=True), 'src')
        self.assertEqual(credentials['RCLONE_CONFIG_SRC_TYPE'], 'alias')
        self.assertEqual(credentials['RCLONE_CONFIG_SRC_REMOTE'], 'srctlsoff,override.no_check_certificate=true:')
        self.assertEqual(credentials['RCLONE_CONFIG_SRCTLSOFF_TYPE'], 's3')
        self.assertEqual(credentials['RCLONE_CONFIG_SRCTLSOFF_SECRET_ACCESS_KEY'], 'secret')
        self.assertEqual(credentials['RCLONE_CONFIG_SRCTLSOFF_ENDPOINT'], 's3.example.edu')
        self.assertEqual(sorted(k for k in credentials if k.startswith('RCLONE_CONFIG_SRC_')),
                         ['RCLONE_CONFIG_SRC_REMOTE', 'RCLONE_CONFIG_SRC_TYPE'])
        # every name stays a plain environment variable name (sudo --preserve-env)
        self.assertTrue(all(key.replace('_', '').isalnum() for key in credentials))

    def test_alias_is_logged_secret_is_not(self):
        rclone = RcloneConnection()
        credentials = rclone._formatCredentials(self._connection(skip_tls_verify=True), 'dst')
        line = rclone._log_command(['rclone'], credentials)
        self.assertIn("RCLONE_CONFIG_DST_REMOTE='dsttlsoff,override.no_check_certificate=true:'", line)
        self.assertNotIn('secret', line.replace('SECRET_ACCESS_KEY', ''))


class TestVerifyErrors(unittest.TestCase):

    def test_permanent(self):
        from api.utils.rclone_connection import is_permanent_error
        for message in (
            'tls: failed to verify certificate: x509: certificate is not valid for any names',
            'api error InvalidAccessKeyId: The AWS Access Key Id you provided does not exist',
            'https response error StatusCode: 403',
            'dial tcp: lookup s3.nowhere.example: no such host',
        ):
            self.assertTrue(is_permanent_error(message), message)

    def test_transient(self):
        from api.utils.rclone_connection import is_permanent_error
        for message in ('i/o timeout', 'StatusCode: 503, SlowDown', 'connection reset by peer'):
            self.assertFalse(is_permanent_error(message), message)
