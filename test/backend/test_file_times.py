import os
import time
import unittest
from unittest import mock

from api.utils.file_times import epoch_to_iso_utc, rfc3339_to_iso_utc
from api.utils.rclone_connection import _with_modified


class TestFileTimes(unittest.TestCase):

    def test_epoch(self):
        self.assertEqual(epoch_to_iso_utc(1790489744), '2026-09-27T06:15:44Z')
        self.assertEqual(epoch_to_iso_utc('1790489744'), '2026-09-27T06:15:44Z')
        self.assertEqual(epoch_to_iso_utc(1790489744.9), '2026-09-27T06:15:44Z')
        for unknown in (None, '', '?', 'abc', 0, -5, 10 ** 20):
            self.assertIsNone(epoch_to_iso_utc(unknown), unknown)

    def test_epoch_ignores_tz(self):
        for tz in ('UTC', 'America/Los_Angeles', 'Australia/Lord_Howe'):
            with mock.patch.dict(os.environ, {'TZ': tz}):
                time.tzset()
                self.assertEqual(epoch_to_iso_utc(1790489744), '2026-09-27T06:15:44Z', tz)
        time.tzset()

    def test_rfc3339(self):
        cases = {
            '2026-09-27T06:15:44Z': '2026-09-27T06:15:44Z',
            '2026-09-27T06:15:44.123456789Z': '2026-09-27T06:15:44Z',
            '2026-09-26T23:15:44.5-07:00': '2026-09-27T06:15:44Z',
            '2026-09-27T11:45:44+05:30': '2026-09-27T06:15:44Z',
            '2026-09-27T06:15:44+0000': '2026-09-27T06:15:44Z',
        }
        for value, expected in cases.items():
            self.assertEqual(rfc3339_to_iso_utc(value), expected, value)

    def test_rfc3339_unknown(self):
        for value in (None, '', 'yesterday', 12345, '0001-01-01T00:00:00Z', '1970-01-01T00:00:00Z',
                      '2026-09-27T06:15:44', '2026-13-40T04:15:44Z'):
            self.assertIsNone(rfc3339_to_iso_utc(value), value)

    def test_rfc3339_ignores_tz(self):
        with mock.patch.dict(os.environ, {'TZ': 'America/Los_Angeles'}):
            time.tzset()
            self.assertEqual(rfc3339_to_iso_utc('2026-09-26T23:15:44-07:00'), '2026-09-27T06:15:44Z')
        time.tzset()

    def test_rclone_entries_get_modified(self):
        files = _with_modified([
            {'Path': 'a', 'Name': 'a', 'Size': 1, 'ModTime': '2026-09-26T23:15:44.1-07:00', 'IsDir': False},
            {'Path': 'd', 'Name': 'd', 'Size': -1, 'ModTime': '0001-01-01T00:00:00Z', 'IsDir': True},
            {'Path': 'e', 'Name': 'e', 'Size': 0, 'IsDir': False},
        ])
        self.assertEqual([f['modified'] for f in files], ['2026-09-27T06:15:44Z', None, None])
        self.assertEqual(files[0]['ModTime'], '2026-09-26T23:15:44.1-07:00')  # passed through
