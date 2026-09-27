import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from api.utils.local_connection import _parse_ls, _ls_with_impersonation


class TestListLocalFiles(unittest.TestCase):

    def test_parse_ls_base_case(self):
        result = _parse_ls("drwxr-xr-x 7 4096 1734673860 hello")
        self.assertEqual(result, [{
            'name': 'hello',
            'type': 'dir',
            'size': 4096,
            'modified': '2024-12-20T05:51:00Z',
        }])


    def test_parse_ls_skip_specials(self):
        result = _parse_ls("drwxr-xr-x 7 4096 1734673860 .")
        assert len(result) == 0

        result = _parse_ls("drwxr-xr-x 7 4096 1734673860 ..")
        assert len(result) == 0

        result = _parse_ls('\n'.join([
            "drwxr-xr-x 7 4096 1734673860 .",
            "drwxr-xr-x 7 4096 1734673860 ..",
        ]))
        assert len(result) == 0


    def test_parse_ls_broken_symlink(self):
        # -L cannot dereference it: only the name is known
        result = _parse_ls("l????????? ?    ? ? shared")
        self.assertEqual(result, [{'name': 'shared', 'type': 'symlink', 'size': None, 'modified': None}])


    def test_parse_ls_names(self):
        result = _parse_ls('\n'.join([
            "-rw-r--r-- 1    3 1790489744 with space",
            "-rw-r--r-- 1    0 1790489744  leading space",
            "-rw-r--r-- 1    0 1790489744 1790489744 digits",
            "-rw-r--r-- 1    0 1790489744 a -> b",
        ]))
        self.assertEqual([f['name'] for f in result], ['with space', ' leading space', '1790489744 digits', 'a -> b'])


    def test_parse_ls_device_and_old_files(self):
        result = _parse_ls('\n'.join([
            "crw-rw-rw- 1 1, 3 1790489744 null",
            "-rw-r--r-- 1    0 0 epoch",
            "-rw-r--r-- 1    0 -86400 before-epoch",
        ]))
        self.assertEqual(result[0], {'name': 'null', 'type': 'unknown', 'size': None, 'modified': '2026-09-27T06:15:44Z'})
        # 1970 is a placeholder, not a real modification time
        self.assertIsNone(result[1]['modified'])
        self.assertIsNone(result[2]['modified'])


    def test_parse_ls_many(self):
        input = [
            "-rw------- 1  573 1732750620 .bash_history",
            "-rw-r--r-- 1  220 1522800000 .bash_logout",
            "-rw-r--r-- 1 3771 1522800000 .bashrc",
            "drwx------ 2 4096 1732746000 .cache",
            "drwxrwxr-x 3 4096 1734673860 .config",
            "drwx------ 3 4096 1732746000 .gnupg",
            "-rw-r--r-- 1  807 1522800000 profile",
            "-rw------- 1    0 1732746000 python_history",
            "drwx------ 2 4096 1732746000 .ssh",
            "-rw-r--r-- 1    0 1732746000 .sudo_as_admin_successful",
            "-rw------- 1  867 1732746000 .viminfo",
            "l????????? ?    ? ? shared",
            "total 12"
        ]
        output = '\n'.join(input)
        result = _parse_ls(output)

        assert len(result) == len(input) - 1
        assert all(f['modified'] is None or f['modified'].endswith('Z') for f in result)


    def test_unparseable_line_is_skipped(self):
        self.assertEqual(_parse_ls("drwxr-xr-x 7 4096 Dec 20 05:51 hello"), [])


class TestListRealDirectory(unittest.TestCase):
    """GNU ls on a real directory, with sudo replaced by running as the current user"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, 'recent.txt'), 'w') as f:
            f.write('hello')
        os.utime(os.path.join(self.dir, 'recent.txt'), (1790489744, 1790489744))
        os.mkdir(os.path.join(self.dir, 'sub'))
        os.symlink('/nonexistent/motuz', os.path.join(self.dir, 'broken'))
        open(os.path.join(self.dir, ' spaced '), 'w').close()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def _ls(self, tz):
        real = subprocess.check_output

        def without_sudo(command, **kwargs):
            self.assertEqual(command[:4], ['sudo', '-n', '-u', 'someone'])
            self.assertEqual(command[-2:], ['--', self.dir])
            return real(command[4:], **kwargs)

        try:
            with mock.patch.dict(os.environ, {'TZ': tz}), \
                    mock.patch('api.utils.local_connection.check_output', side_effect=without_sudo):
                time.tzset()
                return {f['name']: f for f in _parse_ls(_ls_with_impersonation(self.dir, 'someone'))}
        finally:
            time.tzset() # TZ is restored by now

    @unittest.skipUnless(shutil.which('ls') and b'GNU' in subprocess.run(['ls', '--version'], capture_output=True).stdout,
                         'needs GNU ls')
    def test_modified_is_utc_whatever_tz(self):
        for tz in ('UTC', 'America/Los_Angeles', 'Asia/Kolkata'):
            files = self._ls(tz)
            self.assertEqual(files['recent.txt']['modified'], '2026-09-27T06:15:44Z', tz)
            self.assertEqual(files['recent.txt']['size'], 5)
            self.assertEqual(files['sub']['type'], 'dir')
            self.assertEqual(files['broken'], {'name': 'broken', 'type': 'symlink', 'size': None, 'modified': None})
            self.assertIn(' spaced ', files)
