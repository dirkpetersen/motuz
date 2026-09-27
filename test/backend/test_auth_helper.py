"""
The login helper of the systemd install (deployment/systemd/auth-helper/
motuz_auth_helper.py, root, socket activated) and its client in the app
(api/utils/auth_helper.py): protocol, input limits, refusals, the per-user lockout,
and the peer check, without a real PAM check.
"""
import importlib.util
import json
import os
import pwd
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from api.utils import auth_helper

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
HELPER_PATH = os.path.join(REPO, 'deployment', 'systemd', 'auth-helper', 'motuz_auth_helper.py')


def load_helper():
    spec = importlib.util.spec_from_file_location('motuz_auth_helper', HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = load_helper()


def pw(name, uid):
    return pwd.struct_passwd((name, 'x', uid, uid, '', '/home/' + name, '/bin/sh'))


USERS = {'root': pw('root', 0), 'daemon': pw('daemon', 1), 'motuz': pw('motuz', 999), 'alice': pw('alice', 1501),
         'nobody': pw('nobody', 65534), 'ad.user@example.org': pw('ad.user@example.org', 123456789)}


def fake_getpwnam(name):
    return USERS[name]


class TestRequestParsing(unittest.TestCase):

    def test_valid(self):
        self.assertEqual(helper.parse_request(b'{"user": "alice", "password": "p\\u00e4ss w"}'), ('alice', 'päss w'))

    def test_invalid(self):
        for data in (b'', b'not json', b'[]', b'{"user": "alice"}', b'{"user": "alice", "password": "x", "extra": 1}',
                     b'{"user": 1, "password": "x"}', b'{"user": "alice", "password": null}',
                     b'{"user": "alice", "password": ""}', b'{"user": "alice", "password": "a\\u0000b"}',
                     '{"user": "alice", "password": "ä"}'.encode('utf-8')): # raw non-ASCII: the client escapes
            self.assertIsNone(helper.parse_request(data), data)

    def test_client_encoding_is_what_the_helper_parses(self):
        data = auth_helper.encode_request('alice', 'päss"\\ \n')
        self.assertTrue(data.endswith(b'\n') and data.count(b'\n') == 1)
        self.assertEqual(helper.parse_request(data[:-1]), ('alice', 'päss"\\ \n'))

    def test_client_refuses_oversized_requests(self):
        self.assertIsNone(auth_helper.encode_request('alice', 'x' * auth_helper.MAX_REQUEST))
        self.assertIsNone(auth_helper.encode_request('alice', None))
        self.assertEqual(auth_helper.MAX_REQUEST, helper.MAX_REQUEST)

    def read(self, chunks, close=True):
        a, b = socket.socketpair()
        with a, b:
            for chunk in chunks:
                b.sendall(chunk)
            if close:
                b.shutdown(socket.SHUT_WR)
            return helper.read_request(a)

    def test_read_request(self):
        self.assertEqual(self.read([b'{"a"', b': 1}\n']), b'{"a": 1}')
        self.assertEqual(self.read([b'{"a": 1}']), b'{"a": 1}') # EOF without newline
        self.assertIsNone(self.read([b'x' * (helper.MAX_REQUEST + 1)]))
        self.assertIsNone(self.read([b'{}\n{}\n'])) # one request per connection

    def test_read_request_times_out(self):
        with mock.patch.object(helper, 'READ_TIMEOUT', 0.2):
            start = time.time()
            self.assertIsNone(self.read([b'{"user"'], close=False))
            self.assertLess(time.time() - start, 5)


class TestRefusals(unittest.TestCase):

    def refusal(self, user):
        with mock.patch.object(helper.pwd, 'getpwnam', side_effect=fake_getpwnam):
            return helper.refusal(user, 'motuz', 1000)

    def test_refused(self):
        self.assertEqual(self.refusal('root'), 'root')
        self.assertIn('system account', self.refusal('daemon'))
        self.assertIn('system account', self.refusal('motuz'))
        self.assertEqual(self.refusal('nobody'), 'nobody')
        self.assertEqual(self.refusal('unknown'), 'unknown user')
        for name in ('', '-rf', 'a b', 'a\nb', '../x', 'x' * 65, 'al\\ice'):
            self.assertEqual(self.refusal(name), 'malformed user name', name)

    def test_the_client_account_is_refused_even_with_a_high_uid(self):
        with mock.patch.object(helper.pwd, 'getpwnam', side_effect=fake_getpwnam):
            self.assertEqual(helper.refusal('alice', 'alice', 1000), 'the service account itself')

    def test_allowed(self):
        self.assertIsNone(self.refusal('alice'))
        self.assertIsNone(self.refusal('ad.user@example.org'))

    def test_uid_min(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('# comment\nUID_MIN\t\t 2000\nUID_MAX 60000\n')
        self.addCleanup(os.unlink, f.name)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MOTUZ_AUTH_MIN_UID', None)
            self.assertEqual(helper.uid_min(f.name), 2000)
            self.assertEqual(helper.uid_min('/nonexistent'), 1000)
            os.environ['MOTUZ_AUTH_MIN_UID'] = '5000'
            self.assertEqual(helper.uid_min(f.name), 5000)


class TestFailureLog(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = helper.FailureLog(os.path.join(self.dir, 'failures.json'), 3, 60)

    def tearDown(self):
        subprocess.run(['rm', '-rf', self.dir])

    def test_lockout_after_max_failures(self):
        for _ in range(3):
            self.assertTrue(self.log.begin('alice'))
        self.assertFalse(self.log.begin('alice'))
        self.assertTrue(self.log.begin('bob')) # per user
        self.assertEqual(os.stat(self.log.path).st_mode & 0o777, 0o600)

    def test_success_clears(self):
        self.log.begin('alice')
        self.log.begin('alice')
        self.log.succeeded('alice')
        for _ in range(3):
            self.assertTrue(self.log.begin('alice'))

    def test_window_expires(self):
        with mock.patch.object(helper.time, 'time', return_value=1000.0):
            for _ in range(3):
                self.log.begin('alice')
            self.assertFalse(self.log.begin('alice'))
        with mock.patch.object(helper.time, 'time', return_value=1061.0):
            self.assertTrue(self.log.begin('alice'))

    def test_corrupt_state_is_reset(self):
        with open(self.log.path, 'w') as f:
            f.write('{not json')
        self.assertTrue(self.log.begin('alice'))

    def test_symlinked_state_file_is_refused(self):
        os.symlink('/etc/passwd', self.log.path)
        with self.assertRaises(OSError):
            self.log.begin('alice')


class TestHandle(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.failures = helper.FailureLog(os.path.join(self.dir, 'f.json'), 2, 900)
        self.calls = []
        patcher = mock.patch.object(helper.pwd, 'getpwnam', side_effect=fake_getpwnam)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        subprocess.run(['rm', '-rf', self.dir])

    def authenticate(self, user, password, service):
        self.calls.append((user, service))
        return password == 'right', 'Authentication failure'

    def handle(self, user, password, authenticate=None):
        request = json.dumps({'user': user, 'password': password}).encode()
        with mock.patch('sys.stderr') as stderr:
            answer = helper.handle(request, client_user='motuz', min_uid=1000, failures=self.failures,
                                   authenticate=authenticate or self.authenticate, service='motuz',
                                   sleep=lambda s: None)
        self.logged = ''.join(call.args[0] for call in stderr.write.call_args_list)
        return answer

    def test_accept_and_reject(self):
        self.assertEqual(self.handle('alice', 'right'), b'OK\n')
        self.assertIn('accepted alice', self.logged)
        self.assertEqual(self.handle('alice', 'wrong'), b'NO\n')
        self.assertEqual(self.calls, [('alice', 'motuz'), ('alice', 'motuz')])
        self.assertNotIn('wrong', self.logged) # never the password

    def test_refusals_never_reach_pam(self):
        for user in ('root', 'daemon', 'motuz', 'nobody', 'unknown', '-x'):
            self.assertEqual(self.handle(user, 'right'), b'NO\n')
        self.assertEqual(self.calls, [])
        self.assertEqual(helper.handle(None, client_user='motuz', min_uid=1000, failures=self.failures,
                                       authenticate=self.authenticate, service='motuz', sleep=lambda s: None), b'NO\n')

    def test_lockout(self):
        self.assertEqual(self.handle('alice', 'wrong'), b'NO\n')
        self.assertEqual(self.handle('alice', 'wrong'), b'NO\n')
        self.assertEqual(self.handle('alice', 'right'), b'NO\n') # locked, PAM not asked
        self.assertIn('locked out', self.logged)
        self.assertEqual(len(self.calls), 2)

    def test_pam_errors_are_a_no(self):
        def broken(user, password, service):
            raise OSError('libpam missing')
        self.assertEqual(self.handle('alice', 'right', broken), b'NO\n')


class TestHelperProcess(unittest.TestCase):
    """The helper as systemd runs it: the connection on stdin (Accept=yes)"""

    def run_helper(self, client_user, request):
        state = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ['rm', '-rf', state])
        ours, theirs = socket.socketpair()
        env = dict(os.environ, MOTUZ_AUTH_CLIENT_USER=client_user, STATE_DIRECTORY=state)
        process = subprocess.Popen([sys.executable, '-I', '-S', HELPER_PATH], stdin=theirs, stdout=theirs,
                                   stderr=subprocess.PIPE, env=env)
        theirs.close()
        with ours:
            ours.sendall(request)
            ours.shutdown(socket.SHUT_WR)
            answer = ours.recv(16)
        stderr = process.communicate(timeout=30)[1].decode()
        return answer, stderr

    def test_the_client_account_may_ask(self):
        me = pwd.getpwuid(os.getuid()).pw_name
        answer, stderr = self.run_helper(me, b'{"user": "root", "password": "x"}\n')
        self.assertEqual(answer, b'NO\n')
        self.assertIn("refused 'root': root", stderr)

    def test_other_peers_are_refused(self):
        answer, stderr = self.run_helper('root' if os.getuid() else 'nobody', b'{"user": "alice", "password": "x"}\n')
        self.assertEqual(answer, b'NO\n')
        self.assertIn('refused a connection from uid {}'.format(os.getuid()), stderr)


class TestClient(unittest.TestCase):

    def serve(self, answer):
        path = os.path.join(tempfile.mkdtemp(), 's')
        server = socket.socket(socket.AF_UNIX)
        server.bind(path)
        server.listen(1)
        received = []

        def run():
            conn, _ = server.accept()
            with conn:
                data = b''
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                received.append(data)
                if answer is not None:
                    conn.sendall(answer)
            server.close()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return path, received, thread

    def test_ok(self):
        path, received, thread = self.serve(b'OK\n')
        self.assertTrue(auth_helper.check_password(path, 'alice', 'secret'))
        thread.join(5)
        self.assertEqual(json.loads(received[0]), {'user': 'alice', 'password': 'secret'})

    def test_anything_else_is_no(self):
        for answer in (b'NO\n', b'OK', b'ok\n', b'', None, b'OKAY\n'):
            path, _, thread = self.serve(answer)
            self.assertFalse(auth_helper.check_password(path, 'alice', 'secret'), answer)
            thread.join(5)

    def test_no_helper(self):
        with mock.patch('api.utils.auth_helper.logging') as logging:
            self.assertFalse(auth_helper.check_password('/nonexistent/motuz-auth.sock', 'alice', 'secret'))
        self.assertNotIn('secret', str(logging.mock_calls))


if __name__ == '__main__':
    unittest.main()
