import datetime
import unittest

from api.managers.token_broker_manager import _parse_expiry, _parse_token
from api.utils.rclone_connection import _rate_limit_flags


class TestParseExpiry(unittest.TestCase):

    def test_go_nanoseconds_and_offset(self):
        expiry = _parse_expiry('2026-09-24T22:39:52.486512262+02:00')
        self.assertEqual(expiry, datetime.datetime(2026, 9, 24, 20, 39, 52, 486512, tzinfo=datetime.timezone.utc))

    def test_utc_z(self):
        self.assertEqual(_parse_expiry('2026-01-02T03:04:05Z').tzinfo, datetime.timezone.utc)

    def test_zero_and_garbage(self):
        self.assertIsNone(_parse_expiry('0001-01-01T00:00:00Z'))
        self.assertIsNone(_parse_expiry(''))
        self.assertIsNone(_parse_expiry('not a date'))


class TestParseToken(unittest.TestCase):

    def test_parse(self):
        self.assertEqual(_parse_token('{"refresh_token": "r"}'), {'refresh_token': 'r'})
        self.assertIsNone(_parse_token(''))
        self.assertIsNone(_parse_token('not json'))
        self.assertIsNone(_parse_token('[1, 2]'))


class TestRateLimitFlags(unittest.TestCase):

    def test_only_for_onedrive(self):
        self.assertEqual(_rate_limit_flags({'RCLONE_CONFIG_SRC_TYPE': 'onedrive'}), ['--tpslimit', '10'])
        self.assertEqual(_rate_limit_flags({'RCLONE_CONFIG_SRC_TYPE': 's3'}), [])
        self.assertEqual(_rate_limit_flags({}), [])
