import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from api.utils import file_view
from api.utils.file_view import MAX_VIEW_BYTES, NotTextError, decode_text, view_result
from api.utils.local_connection import LocalConnection
from api.utils.rclone_connection import RcloneConnection


class TestDecodeText(unittest.TestCase):

    def test_utf8(self):
        self.assertEqual(decode_text('hello\nwörld ✓\n'.encode(), False), ('hello\nwörld ✓\n', 'utf-8'))
        self.assertEqual(decode_text(b'', False), ('', 'utf-8'))
        self.assertEqual(decode_text(b'\xef\xbb\xbfbom', False), ('bom', 'utf-8'))
        self.assertEqual(decode_text(b'tab\tcr\r\nesc\x1b[0m', False)[1], 'utf-8')

    def test_binary(self):
        for data in (b'\x00', b'PK\x03\x04\x00\x00', b'text then \x00 nul', bytes(range(256)) * 4,
                     b'\x89PNG\r\n\x1a\n' + bytes(range(128, 256)) * 10, b'\x01\x02\x03\x04\x05abc'):
            with self.assertRaises(NotTextError, msg=data[:20]):
                decode_text(data, False)

    def test_mostly_valid_utf8_is_shown_with_replacements(self):
        data = ('x' * 500).encode() + b'\xff' + ('y' * 500).encode()
        content, encoding = decode_text(data, False)
        self.assertEqual(content, 'x' * 500 + '�' + 'y' * 500)
        self.assertEqual(encoding, 'utf-8 (invalid bytes replaced)')

    def test_latin1_is_refused(self):
        with self.assertRaises(NotTextError):
            decode_text('Größe über Maß'.encode('latin-1') * 3, False)

    def test_genuine_replacement_characters_are_not_invalid(self):
        data = ('�' * 50).encode()
        self.assertEqual(decode_text(data, False), ('�' * 50, 'utf-8'))

    def test_truncated_in_the_middle_of_a_character(self):
        data = ('a' * 10 + 'é').encode()[:-1] # cut after the first byte of é
        self.assertEqual(decode_text(data, True), ('a' * 10, 'utf-8'))
        # ... but not at the end of a complete file
        self.assertEqual(decode_text(data, False)[1], 'utf-8 (invalid bytes replaced)')


class TestViewResult(unittest.TestCase):

    def test_small(self):
        self.assertEqual(view_result('/a/b.txt', b'hi\n', 3),
                         {'path': '/a/b.txt', 'content': 'hi\n', 'truncated': False, 'size': 3, 'encoding': 'utf-8'})

    def test_truncated(self):
        result = view_result('/big.log', b'x' * (MAX_VIEW_BYTES + 1), 5 * MAX_VIEW_BYTES)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['content']), MAX_VIEW_BYTES)
        self.assertEqual(result['size'], 5 * MAX_VIEW_BYTES)

    def test_exactly_the_cap(self):
        result = view_result('/cap', b'x' * MAX_VIEW_BYTES, MAX_VIEW_BYTES)
        self.assertFalse(result['truncated'])


class TestRunLimited(unittest.TestCase):

    def test_output_and_status(self):
        self.assertEqual(file_view.run_limited([sys.executable, '-c', 'import sys; sys.stdout.write("x"); sys.exit(3)'], 10),
                         (3, b'x', b''))

    def test_timeout_kills_the_process_group(self):
        with self.assertRaises(file_view.ViewTimeoutError):
            file_view.run_limited(['sh', '-c', 'sleep 30 & sleep 30'], 0.5)


def _without_sudo(test):
    """Runs the reader as the current user: drops `sudo -n -u <user> --`"""
    real = file_view.run_limited

    def run(command, timeout, env=None):
        test.assertEqual(command[:5], ['sudo', '-n', '-u', 'alice', '--'])
        test.assertIn('-I', command) # isolated python: no user site-packages, no PYTHON* variables
        return real(command[5:], timeout, env)
    return mock.patch('api.utils.local_connection.file_view.run_limited', side_effect=run)


class Owner:
    owner = 'alice'


class TestLocalView(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def view(self, path):
        with _without_sudo(self):
            return LocalConnection().view(Owner(), path)

    def test_text(self):
        path = self.write('notes.txt', 'line 1\nlïne 2\n'.encode())
        self.assertEqual(self.view(path), {'path': path, 'content': 'line 1\nlïne 2\n', 'truncated': False,
                                           'size': 15, 'encoding': 'utf-8'})

    def test_large_file_is_truncated(self):
        path = self.write('big.log', b'0123456789\n' * 200000)
        result = self.view(path)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['size'], 2200000)
        self.assertEqual(len(result['content']), MAX_VIEW_BYTES)

    def test_binary(self):
        path = self.write('image.png', b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR')
        with self.assertRaisesRegex(file_view.NotTextError, "'image.png' is not a text file"):
            self.view(path)

    def test_errors(self):
        with self.assertRaises(file_view.NotFoundError):
            self.view(os.path.join(self.dir, 'missing'))
        with self.assertRaisesRegex(file_view.ViewError, 'is a folder'):
            self.view(self.dir)
        os.mkfifo(os.path.join(self.dir, 'fifo'))
        with self.assertRaisesRegex(file_view.ViewError, 'not a regular file'):
            self.view(os.path.join(self.dir, 'fifo'))
        with self.assertRaisesRegex(file_view.ViewError, 'not a regular file'):
            self.view('/dev/zero')
        for relative in ('notes.txt', '-la', '', '--help'):
            with self.assertRaisesRegex(file_view.ViewError, 'must be absolute'):
                self.view(relative)

    @unittest.skipIf(os.geteuid() == 0, 'root can read everything')
    def test_permission_denied(self):
        path = self.write('secret', b'secret')
        os.chmod(path, 0)
        with self.assertRaises(file_view.ForbiddenError):
            self.view(path)

    def test_symlink_is_followed_as_the_user(self):
        target = self.write('target.txt', b'target')
        os.symlink(target, os.path.join(self.dir, 'link'))
        self.assertEqual(self.view(os.path.join(self.dir, 'link'))['content'], 'target')

    def test_sudo_failure(self):
        with mock.patch('api.utils.local_connection.file_view.run_limited', return_value=(1, b'', b'sudo: a password is required')):
            with self.assertRaises(file_view.ForbiddenError):
                LocalConnection().view(Owner(), '/etc/hostname')


class TestCloudView(unittest.TestCase):

    def data(self):
        conn = mock.Mock()
        conn.owner = 'alice'
        conn.type = 'webdav'
        conn.webdav_url = 'http://127.0.0.1:9/'
        conn.webdav_user = None
        conn.webdav_pass = None
        return conn

    def run_view(self, outputs):
        calls = []

        def run(command, timeout, env=None):
            calls.append((command, env))
            return outputs.pop(0)

        with mock.patch('api.utils.rclone_connection.file_view.run_limited', side_effect=run):
            try:
                return RcloneConnection().view(self.data(), '/dir/notes.txt'), calls
            except Exception as e:
                return e, calls

    def test_text(self):
        result, calls = self.run_view([(0, b'{"Path":"notes.txt","Size":6,"IsDir":false}', b''), (0, b'hello\n', b'')])
        self.assertEqual(result, {'path': '/dir/notes.txt', 'content': 'hello\n', 'truncated': False, 'size': 6, 'encoding': 'utf-8'})
        stat, cat = calls[0][0], calls[1][0]
        self.assertEqual(stat[:4], ['sudo', '-E', '-u', 'alice'])
        self.assertEqual(stat[-3:], ['lsjson', '--stat', 'current:/dir/notes.txt'])
        self.assertEqual(cat[-4:], ['cat', '--count', str(MAX_VIEW_BYTES + 1), 'current:/dir/notes.txt'])
        env = calls[1][1]
        self.assertEqual(env['RCLONE_CONFIG_CURRENT_TYPE'], 'webdav')
        self.assertFalse(any(key.startswith('MOTUZ_') for key in env))

    def test_folder_is_never_catted(self):
        result, calls = self.run_view([(0, b'{"Path":"dir","IsDir":true}', b'')])
        self.assertIsInstance(result, file_view.ViewError)
        self.assertIn('folder or does not exist', str(result))
        self.assertEqual(len(calls), 1)
        result, calls = self.run_view([(0, b'null', b'')])
        self.assertIsInstance(result, file_view.NotFoundError)

    def test_binary_and_truncated(self):
        result, _ = self.run_view([(0, b'{"Size":9,"IsDir":false}', b''), (0, b'\x00\x01bin', b'')])
        self.assertIsInstance(result, file_view.NotTextError)
        result, _ = self.run_view([(0, b'{"Size":3000000,"IsDir":false}', b''), (0, b'a' * (MAX_VIEW_BYTES + 1), b'')])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['size'], 3000000)

    def test_not_found(self):
        result, _ = self.run_view([(3, b'', b'ERROR : error listing: object not found')])
        self.assertIsInstance(result, file_view.NotFoundError)
