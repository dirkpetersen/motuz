"""
The free space limit (utils/space_check.py, managers/space_manager.py, MOTUZ_MAX_FILL_PERCENT):
the setting, the fill arithmetic, the reader (nearest existing ancestor, statvfs, CephFS
quota xattrs) and its output, which limit is used, the messages, copy jobs and uploads
refused at create (409) or run with a note, and the check on a remote worker.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from flask import Flask
import flask_jwt_extended as flask_jwt

from api.application import db, jwt, register_api
from api.config import TestingConfig
from api.models import CloudConnection, CopyJob, RemoteJob
from api.utils import file_view, space_check
from api.utils.abstract_connection import RcloneException
from api.utils.rclone_connection import RcloneConnection
from api.utils.space_check import CEPH_QUOTA, FILESYSTEM, GROUP_QUOTA, USER_QUOTA, Limit

MiB = 1 << 20
GiB = 1 << 30
TiB = 1 << 40


class TestSetting(unittest.TestCase):

    def test_default_and_range(self):
        self.assertEqual(space_check.parse_max_fill(None), 90)
        self.assertEqual(space_check.parse_max_fill('  '), 90)
        self.assertEqual(space_check.parse_max_fill('85'), 85)
        self.assertEqual(space_check.parse_max_fill('85%'), 85)
        self.assertEqual(space_check.parse_max_fill('100'), 100)
        self.assertEqual(space_check.parse_max_fill('1'), 1)
        self.assertEqual(space_check.max_fill_percent({'MOTUZ_MAX_FILL_PERCENT': '75'}), 75)
        self.assertEqual(space_check.max_fill_percent({}), 90)

    def test_invalid_values_name_the_variable(self):
        for value in ('0', '101', '-5', '9.5', 'ninety', '90 --rc'):
            with self.assertRaises(space_check.SpaceConfigError, msg=value) as raised:
                space_check.parse_max_fill(value)
            self.assertIn('MOTUZ_MAX_FILL_PERCENT', str(raised.exception))

    def test_invalid_value_stops_the_app(self):
        env = dict(os.environ, MOTUZ_MAX_FILL_PERCENT='150')
        result = subprocess.run([sys.executable, '-c', 'import api.config'], env=env, capture_output=True, text=True,
                                cwd=os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'backend'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('MOTUZ_MAX_FILL_PERCENT must be a whole number from 1 to 100', result.stderr)

    def test_ticket_value(self):
        self.assertEqual(space_check.checked_percent(None), 100)
        self.assertEqual(space_check.checked_percent(90), 90)
        for bad in (0, 101, '90', True, 9.5):
            with self.assertRaises(RcloneException, msg=bad):
                space_check.checked_percent(bad)


class TestArithmetic(unittest.TestCase):

    def test_boundary_is_allowed(self):
        total = 1000
        self.assertFalse(space_check.exceeds(800, 100, total, 90)) # exactly 90%
        self.assertTrue(space_check.exceeds(800, 101, total, 90))
        self.assertFalse(space_check.exceeds(0, 0, total, 1))
        # Exact with large numbers (no floating point rounding at petabytes)
        total = 10 * 1000 ** 5
        self.assertFalse(space_check.exceeds(total * 9 // 10 - 1, 1, total, 90))
        self.assertTrue(space_check.exceeds(total * 9 // 10, 1, total, 90))

    def test_100_disables(self):
        self.assertFalse(space_check.exceeds(1000, 10 ** 12, 1000, 100))
        self.assertIsNone(space_check.check_upload('alice', '/x', 10 ** 12, 100, 60, read=self.fail_read))
        self.assertIsNone(space_check.check_copy(None, 'alice', src_credentials={}, src_path='/a', src_local=True,
                                                 dst_path='/b', copy_links=False, max_percent=100, timeout=60,
                                                 read=self.fail_read))

    def fail_read(self, *args):
        raise AssertionError('must not read the space')

    def test_human(self):
        self.assertEqual(space_check.human(0), '0 B')
        self.assertEqual(space_check.human(1023), '1023 B')
        self.assertEqual(space_check.human(int(1.2 * TiB)), '1.2 TiB')
        self.assertEqual(space_check.human(-5), '0 B')


class TestEvaluate(unittest.TestCase):

    def test_quota_message(self):
        limits = [Limit(FILESYSTEM, 9000 * TiB, 1000 * TiB, '/storage/t3/x'),
                  Limit(CEPH_QUOTA, 10 * TiB, int(9.6 * TiB), '/storage/t3/x')]
        with self.assertRaises(space_check.NotEnoughSpace) as raised:
            space_check.evaluate('Copying', '/storage/t3/x/new', limits, int(0.4 * TiB) + 1, 90)
        self.assertEqual(str(raised.exception),
                         'Copying 409.6 GiB to /storage/t3/x/new would fill its quota (CephFS quota of /storage/t3/x) '
                         'to 100% (limit 90%): 409.6 GiB of 10.0 TiB free. Free space, ask for a larger quota or '
                         'choose another destination.')

    def test_filesystem_message(self):
        limits = [Limit(FILESYSTEM, 10 * TiB, 8 * TiB, '/data')]
        with self.assertRaises(space_check.NotEnoughSpace) as raised:
            space_check.evaluate('Uploading', '/data/up', limits, int(1.6 * TiB), 90)
        self.assertEqual(str(raised.exception),
                         'Uploading 1.6 TiB to /data/up would fill its filesystem to 96% (limit 90%): 2.0 TiB of '
                         '10.0 TiB free. Free space or choose another destination.')

    def test_percent_just_above_the_limit_is_not_shown_as_the_limit(self):
        limits = [Limit(FILESYSTEM, 1000, 900, '/d')]
        with self.assertRaises(space_check.NotEnoughSpace) as raised:
            space_check.evaluate('Copying', '/d', limits, 4, 90)
        self.assertIn('to 90.4% (limit 90%)', str(raised.exception))

    def test_note_when_it_fits(self):
        note = space_check.evaluate('Copying', '/d/x', [Limit(FILESYSTEM, 10 * GiB, 2 * GiB, '/d')], GiB, 90)
        self.assertEqual(note, 'Free space checked: copying 1.0 GiB leaves its filesystem 30% full '
                               '(limit 90%, 8.0 GiB of 10.0 GiB free before).')

    def test_a_quota_is_the_limit_instead_of_the_filesystem(self):
        # The filesystem is fuller than the quota allows, but the quota applies
        limits = [Limit(FILESYSTEM, 100 * GiB, 95 * GiB, '/d'), Limit(CEPH_QUOTA, 10 * GiB, GiB, '/d/q')]
        note = space_check.evaluate('Copying', '/d/q/x', limits, GiB, 90)
        self.assertIn('its quota (CephFS quota of /d/q) 20% full', note)
        # A quota larger than the filesystem's free space: still the quota
        limits = [Limit(FILESYSTEM, 100 * GiB, 10 * GiB, '/d'), Limit(CEPH_QUOTA, 10 * GiB, 9 * GiB, '/d/q')]
        with self.assertRaises(space_check.NotEnoughSpace) as raised:
            space_check.evaluate('Copying', '/d/q/x', limits, GiB // 2, 90)
        self.assertIn('fill its quota (CephFS quota of /d/q) to 95%', str(raised.exception))

    def test_the_fullest_quota_decides(self):
        limits = [Limit(FILESYSTEM, 100 * GiB, 0, '/d'), Limit(USER_QUOTA, 10 * GiB, GiB, '/d'),
                  Limit(GROUP_QUOTA, 10 * GiB, 9 * GiB, '/d')]
        with self.assertRaises(space_check.NotEnoughSpace) as raised:
            space_check.evaluate('Copying', '/d', limits, GiB // 2, 90)
        self.assertIn("its group's disk quota to 95%", str(raised.exception))
        note = space_check.evaluate('Copying', '/d', limits[:2], GiB, 90)
        self.assertIn('your disk quota 20% full', note)


class FakeRun:
    """file_view.run_limited that runs READER as the current user, without sudo"""

    def __init__(self):
        self.commands = []

    def __call__(self, command, timeout, env=None):
        self.commands.append(command)
        script_at = command.index('-c') + 1
        result = subprocess.run([sys.executable, '-I', '-S', '-c'] + command[script_at:], capture_output=True,
                                timeout=timeout)
        return result.returncode, result.stdout, result.stderr


class TestReader(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)
        self.run_reader = FakeRun()

    def read(self, path):
        return space_check.read_limits('alice', path, 10, run=self.run_reader)

    def test_command_runs_python_through_env_as_the_user(self):
        self.read(self.root)
        command = self.run_reader.commands[0]
        self.assertEqual(command[:2], ['sudo', '-n'])
        self.assertEqual(command[command.index('-u') + 1], 'alice')
        self.assertEqual(command[command.index('-u') + 2], '/usr/bin/env')
        self.assertNotIn('-E', command)
        self.assertEqual(command[-1], self.root) # an argument of the script, never an option
        self.assertEqual(command[-6:-1], [command[-6], '-I', '-S', '-c', space_check.READER])

    def test_statvfs_of_the_nearest_existing_ancestor(self):
        limits = self.read(os.path.join(self.root, 'a', 'b', 'c'))
        st = os.statvfs(self.root)
        self.assertEqual(limits[0].kind, FILESYSTEM)
        self.assertEqual(limits[0].path, self.root)
        self.assertEqual(limits[0].total, st.f_blocks * st.f_frsize)
        self.assertGreaterEqual(limits[0].used, 0)
        os.makedirs(os.path.join(self.root, 'a'))
        self.assertEqual(self.read(os.path.join(self.root, 'a', 'b', 'c'))[0].path, os.path.join(self.root, 'a'))
        # A file on the way: the file is the nearest existing path (its filesystem)
        open(os.path.join(self.root, 'a', 'f'), 'w').close()
        self.assertEqual(self.read(os.path.join(self.root, 'a', 'f', 'x'))[0].path, os.path.join(self.root, 'a', 'f'))

    def test_relative_and_option_like_paths_are_refused(self):
        for path in ('relative', '--help', '', None, '/a\x00b'):
            with self.assertRaises(space_check.SpaceUnknown, msg=path):
                self.read(path)
        self.assertEqual(self.run_reader.commands, [])

    def test_a_folder_the_user_cannot_traverse(self):
        if os.geteuid() == 0:
            self.skipTest('root traverses every folder')
        closed = os.path.join(self.root, 'closed')
        os.makedirs(os.path.join(closed, 'in'))
        os.chmod(closed, 0)
        self.addCleanup(os.chmod, closed, 0o700)
        with self.assertRaises(space_check.SpaceUnknown) as raised:
            self.read(os.path.join(closed, 'in', 'x'))
        self.assertIn('cannot access', str(raised.exception))

    def test_timeout(self):
        def slow(command, timeout, env=None):
            raise file_view.ViewTimeoutError('slow')
        with self.assertRaises(space_check.SpaceUnknown) as raised:
            space_check.read_limits('alice', '/x', 5, run=slow)
        self.assertIn('took longer than 5 seconds', str(raised.exception))

    def run_in_process(self, path, xattrs):
        """READER in this process with os.getxattr answering from `xattrs` ({(dir, name): bytes})"""
        def getxattr(target, name, *args, **kwargs):
            if not name.startswith('ceph.'):
                raise OSError(95, 'Operation not supported')
            if not xattrs:
                raise OSError(95, 'Operation not supported') # not CephFS
            if (target, name) in xattrs:
                return xattrs[(target, name)]
            raise OSError(61, 'No data available')
        out = io.StringIO()
        with mock.patch.object(os, 'getxattr', getxattr), mock.patch.object(sys, 'argv', ['-c', path]), \
                contextlib.redirect_stdout(out):
            exec(compile(space_check.READER, 'reader', 'exec'), {'__name__': 'reader'})
        return space_check.parse_reader(0, out.getvalue().encode(), 'alice', path)

    def test_cephfs_quota_of_the_directory_or_an_ancestor(self):
        quota_dir = os.path.join(self.root, 'project')
        os.makedirs(os.path.join(quota_dir, 'sub'))
        limits = self.run_in_process(os.path.join(quota_dir, 'sub', 'new'), {
            (quota_dir, 'ceph.quota.max_bytes'): b'10995116277760',
            (quota_dir, 'ceph.dir.rbytes'): b'4398046511104',
            (os.path.join(quota_dir, 'sub'), 'ceph.dir.rbytes'): b'1',
        })
        self.assertEqual([l.kind for l in limits][:2], [FILESYSTEM, CEPH_QUOTA])
        self.assertEqual(limits[1], Limit(CEPH_QUOTA, 10 * TiB, 4 * TiB, quota_dir))
        # max_bytes 0 means no quota; then the filesystem only
        limits = self.run_in_process(os.path.join(quota_dir, 'sub'), {
            (quota_dir, 'ceph.quota.max_bytes'): b'0', (quota_dir, 'ceph.dir.rbytes'): b'5'})
        self.assertNotIn(CEPH_QUOTA, [l.kind for l in limits])

    def test_not_cephfs(self):
        limits = self.run_in_process(self.root, {})
        self.assertEqual(limits[0].kind, FILESYSTEM)
        self.assertNotIn(CEPH_QUOTA, [l.kind for l in limits])


class TestParseReader(unittest.TestCase):

    def parse(self, returncode, out):
        return space_check.parse_reader(returncode, out if isinstance(out, bytes) else json.dumps(out).encode(),
                                        'alice', '/d/x')

    def test_limits(self):
        limits = self.parse(0, {'path': '/d', 'limits': [
            {'kind': 'filesystem', 'path': '/d', 'total': 100, 'used': 50},
            {'kind': 'ceph-quota', 'path': '/d', 'total': 10, 'used': 5},
            {'kind': 'unknown', 'path': '/d', 'total': 10, 'used': 5},     # dropped
            {'kind': 'user-quota', 'path': '/d', 'total': 0, 'used': 5},   # no limit: dropped
            {'kind': 'group-quota', 'path': '/d', 'total': 'x', 'used': 5},
        ]})
        self.assertEqual(limits, [Limit(FILESYSTEM, 100, 50, '/d'), Limit(CEPH_QUOTA, 10, 5, '/d')])

    def test_errors(self):
        for returncode, out, text in ((3, {'error': 'denied'}, 'alice cannot access'),
                                      (3, {'error': 'missing'}, 'no part of'),
                                      (3, {'error': 'weird'}, 'could not be read'),
                                      (1, b'', 'could not be read'),
                                      (0, b'not json', 'could not be read'),
                                      (0, {'path': '/d', 'limits': []}, 'could not be read')):
            with self.assertRaises(space_check.SpaceUnknown, msg=out) as raised:
                self.parse(returncode, out)
            self.assertIn(text, str(raised.exception))


class FakeConnection:
    def __init__(self, sizes, error=None):
        self.sizes, self.error, self.calls = sizes, error, []

    def measure(self, credentials, path, **kwargs):
        self.calls.append((credentials, path, kwargs))
        if self.error:
            raise self.error
        return self.sizes.get(path, (0, 0))


def fs(total, used, path='/d'):
    return lambda user, path_, timeout: [Limit(FILESYSTEM, total, used, path)]


class TestCheckCopy(unittest.TestCase):

    def check(self, connection, read, **kwargs):
        args = dict(src_credentials={}, src_path='/src', src_local=True, dst_path='/d/dst', copy_links=True,
                    max_percent=90, timeout=60, read=read)
        args.update(kwargs)
        return space_check.check_copy(connection, 'alice', **args)

    def test_what_already_exists_counts_as_copied(self):
        # A retry of a half-done copy: 600 of 1000 bytes are there already
        connection = FakeConnection({'/src': (1000, 3), '/d/dst': (600, 2)})
        self.assertTrue(self.check(connection, fs(1000, 500)).startswith('Free space checked: copying 400 B')) # 90%
        with self.assertRaises(space_check.NotEnoughSpace):
            self.check(FakeConnection({'/src': (1000, 3)}), fs(1000, 500))
        # Never below 0
        connection = FakeConnection({'/src': (10, 1), '/d/dst': (600, 2)})
        self.assertIn('copying 0 B', self.check(connection, fs(1000, 900)))

    def test_measured_as_the_user(self):
        connection = FakeConnection({'/src': (1, 1)})
        self.check(connection, fs(1000, 0), src_credentials={'RCLONE_CONFIG_SRC_TYPE': 's3'}, src_path='b/k',
                   src_local=False, extra_env={'HTTPS_PROXY': 'http://p:3128'})
        (src_credentials, src_path, src_args), (dst_credentials, dst_path, dst_args) = connection.calls
        self.assertEqual((src_credentials, src_path), ({'RCLONE_CONFIG_SRC_TYPE': 's3'}, 'b/k'))
        self.assertEqual(src_args['user'], 'alice')
        self.assertFalse(src_args['local'])
        self.assertTrue(src_args['copy_links'])
        self.assertEqual(src_args['extra_env'], {'HTTPS_PROXY': 'http://p:3128'})
        self.assertEqual((dst_credentials, dst_path, dst_args['local'], dst_args['missing_ok']), ({}, '/d/dst', True, True))

    def test_unknown_or_slow_does_not_block(self):
        for error, text in ((file_view.ViewTimeoutError('slow'), 'took longer than 60 seconds'),
                            (RcloneException('directory not found'), 'directory not found'),
                            (OSError('sudo missing'), 'sudo missing')):
            note = self.check(FakeConnection({}, error), fs(1000, 999))
            self.assertTrue(note.startswith('Free space not checked: '), note)
            self.assertIn(text, note)

        def unreadable(user, path, timeout):
            raise space_check.SpaceUnknown("alice cannot access '/d/dst'")
        note = self.check(FakeConnection({'/src': (10 ** 15, 1)}), unreadable)
        self.assertEqual(note, "Free space not checked: alice cannot access '/d/dst'. A full disk still stops the job.")

    def test_one_deadline_for_everything(self):
        timeouts = []

        def read(user, path, timeout):
            timeouts.append(timeout)
            return [Limit(FILESYSTEM, 1000, 0, '/d')]
        connection = FakeConnection({'/src': (1, 1)})
        self.check(connection, read, timeout=45)
        self.assertLessEqual(timeouts[0], space_check.READER_TIMEOUT)
        for _, _, args in connection.calls:
            self.assertLessEqual(args['timeout'], 45)


class TestMeasure(unittest.TestCase):

    def run_measure(self, returncode, stdout, **kwargs):
        calls = []

        def run(command, timeout, env=None):
            calls.append((command, env))
            return returncode, stdout, b'2026/10/18 ERROR : error listing: directory not found'
        with mock.patch.object(file_view, 'run_limited', run):
            result = RcloneConnection().measure(kwargs.pop('credentials', {}), kwargs.pop('path', '/home/alice/src'),
                                                user='alice', timeout=5, **kwargs)
        return result, calls

    def test_local(self):
        result, calls = self.run_measure(0, b'{"count":3,"bytes":1000,"sizeless":0}', local=True, copy_links=True)
        self.assertEqual(result, (1000, 3))
        command = calls[0][0]
        self.assertEqual(command[-3:], ['size', '--json', '/home/alice/src'])
        self.assertIn('--copy-links', command)
        self.assertIn('--exclude=\\.snapshot/', command)
        self.assertEqual(command[command.index('-u') + 1], 'alice')

    def test_missing_destination(self):
        self.assertEqual(self.run_measure(3, b'', local=True, missing_ok=True)[0], (0, 0))
        with self.assertRaises(RcloneException):
            self.run_measure(3, b'', local=True)

    def test_cloud_source(self):
        result, calls = self.run_measure(0, b'{"count":1,"bytes":7}', local=False, path='bucket/key',
                                         credentials={'RCLONE_CONFIG_SRC_TYPE': 's3'})
        command, env = calls[0]
        self.assertEqual(command[-1], 'src:bucket/key')
        self.assertNotIn('--exclude=\\.snapshot/', command)
        self.assertEqual(env['RCLONE_CONFIG_SRC_TYPE'], 's3')

    def test_relative_local_path_is_never_an_option(self):
        with self.assertRaises(RcloneException):
            self.run_measure(0, b'{}', local=True, path='--config=/etc/shadow')


class ManagerTestBase(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = Flask('space-test')
        self.app.config.from_object(TestingConfig)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(self.directory.name, 'test.sqlite3'),
            SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
            JWT_SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
            MAX_FILL_PERCENT=90,
        )
        db.init_app(self.app)
        jwt.init_app(self.app)
        register_api(self.app)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        db.create_all()
        self.addCleanup(db.drop_all)
        self.client = self.app.test_client()
        with self.app.test_request_context():
            self.alice = {'Authorization': 'Bearer ' + flask_jwt.create_access_token(identity='alice')}

        self.limits = [Limit(FILESYSTEM, 10 * GiB, 8 * GiB, '/data')]
        self.sizes = {}
        self.read_calls = []

        def read(user, path, timeout):
            self.read_calls.append((user, path))
            if isinstance(self.limits, Exception):
                raise self.limits
            return self.limits

        def measure(connection, credentials, path, **kwargs):
            return self.sizes.get(path, (0, 0))

        from api import tasks
        for patcher in (mock.patch.object(space_check, 'read_limits', read),
                        mock.patch.object(RcloneConnection, 'measure', measure),
                        mock.patch.object(RcloneConnection, 'existing_names', lambda *a: []),
                        mock.patch.object(tasks.copy_job, 'apply_async')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def copy(self, src='/data/src', dst='/data/dst', **extra):
        return self.client.post('/api/copy-jobs/', headers=self.alice, json=dict(
            {'description': 'x', 'src_resource_path': src, 'dst_resource_path': dst, 'copy_links': False}, **extra))


class TestCopyCreate(ManagerTestBase):

    def test_refused_with_the_message_and_no_job(self):
        self.sizes['/data/src'] = (int(1.5 * GiB), 10)
        response = self.copy()
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()['message'],
                         'Copying 1.5 GiB to /data/dst would fill its filesystem to 95% (limit 90%): 2.0 GiB of '
                         '10.0 GiB free. Free space or choose another destination.')
        self.assertEqual(CopyJob.query.count(), 0)
        self.assertEqual(self.read_calls, [('alice', '/data/dst')])

    def test_accepted_with_a_note(self):
        self.sizes['/data/src'] = (GiB, 10)
        response = self.copy()
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()['space_check'], 'Free space checked: copying 1.0 GiB leaves its '
                         'filesystem 90% full (limit 90%, 2.0 GiB of 10.0 GiB free before).')
        self.assertEqual(db.session.get(CopyJob, response.get_json()['id']).space_check,
                         response.get_json()['space_check'])

    def test_unknown_space_does_not_block(self):
        self.limits = space_check.SpaceUnknown('reading the space of /data/dst took longer than 30 seconds')
        response = self.copy()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.get_json()['space_check'].startswith('Free space not checked: reading the space'))

    def test_limit_off(self):
        self.app.config['MAX_FILL_PERCENT'] = 100
        self.sizes['/data/src'] = (100 * GiB, 10)
        response = self.copy()
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.get_json()['space_check'])
        self.assertEqual(self.read_calls, [])

    def test_cloud_destination_is_not_checked(self):
        connection = CloudConnection(owner='alice', name='c', type='s3', s3_access_key_id='k', s3_secret_access_key='s')
        db.session.add(connection)
        db.session.commit()
        self.sizes['/data/src'] = (100 * GiB, 10)
        response = self.copy(dst='bucket/x', dst_cloud_id=connection.id)
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertIsNone(response.get_json()['space_check'])
        self.assertEqual(self.read_calls, [])

    def test_remote_worker_checks_instead_and_gets_the_limit(self):
        from api.managers import worker_manager
        self.app.config['LOCAL_JOB_POOL'] = 'onprem'
        self.sizes['/data/src'] = (100 * GiB, 10)
        response = self.copy()
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertIsNone(response.get_json()['space_check'])
        self.assertEqual(self.read_calls, [])
        job = db.session.get(CopyJob, response.get_json()['id'])
        remote_job = RemoteJob.query.one()
        with self.app.test_request_context(base_url='https://motuz.test'):
            ticket = worker_manager.build_ticket(remote_job, job, 'mzt1.x', 'secret')
        self.assertEqual(ticket['job']['options'], {'copy_links': False, 'max_fill_percent': 90,
                                                    'space_check_timeout': self.app.config['JOB_SIZE_TIMEOUT']})


class TestUploadCreate(ManagerTestBase):

    def upload(self, files, mode='overwrite'):
        return self.client.post('/api/copy-jobs/uploads/', headers=self.alice, json={
            'dst_cloud_id': 0, 'dst_resource_path': '/data/up', 'mode': mode,
            'files': [{'path': path, 'size': size} for path, size in files]})

    def test_refused(self):
        response = self.upload([('a.bin', GiB), ('b.bin', GiB)])
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertTrue(response.get_json()['message'].startswith(
            'Uploading 2.0 GiB to /data/up would fill its filesystem to 100% (limit 90%)'), response.get_json())
        self.assertEqual(CopyJob.query.count(), 0)

    def test_skipped_files_are_not_counted(self):
        with mock.patch('api.managers.upload_manager._existing_files', return_value={'a.bin'}):
            response = self.upload([('a.bin', GiB), ('b.bin', GiB // 2)], mode='skip')
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertIn('uploading 512.0 MiB', response.get_json()['job']['space_check'])

    def test_unknown_space_does_not_block(self):
        self.limits = space_check.SpaceUnknown("alice cannot access '/data/up'")
        response = self.upload([('a.bin', 100 * GiB)])
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.get_json()['job']['space_check'].startswith('Free space not checked'))


class TestWorker(unittest.TestCase):
    """The check on a remote worker (src/worker/motuz_worker.py), before rclone starts"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'worker'))
        import motuz_worker
        cls.agent = motuz_worker

    def run_job(self, options, limits, src_bytes):
        args = mock.Mock(central_url=None, bootstrap_token='mzb1.1.x', bootstrap_token_file=None, once=False)
        config = self.agent.Config({'MOTUZ_CENTRAL_URL': 'https://motuz.test'}, args)
        ticket = {'ticket_id': 1, 'ticket_token': 't', 'progress_interval': 1, 'job': {
            'type': 'copy', 'id': 3, 'owner': 'alice', 'options': options,
            'src': {'local': True, 'path': '/home/alice/src'}, 'dst': {'local': True, 'path': '/home/alice/dst'}}}
        run = self.agent.JobRun(mock.Mock(), config, ticket, lambda: False)
        sizes = {'/home/alice/src': (src_bytes, 1)}
        with mock.patch.object(space_check, 'read_limits', lambda user, path, timeout: limits), \
                mock.patch.object(run.connection, 'measure', lambda c, path, **kw: sizes.get(path, (0, 0))), \
                mock.patch.object(run.connection, 'copy_with_credentials') as copy, \
                mock.patch.object(self.agent.job_runner, 'watch_copy', return_value=0), \
                mock.patch.object(run.connection, 'copy_text', return_value=''), \
                mock.patch.object(run.connection, 'copy_error_text', return_value=''), \
                mock.patch.object(run, 'finish') as finish:
            run.run()
        return copy, finish.call_args.args[0]

    def test_refused_on_the_worker(self):
        copy, result = self.run_job({'copy_links': True, 'max_fill_percent': 90, 'space_check_timeout': 60},
                                    [Limit(FILESYSTEM, 10 * GiB, 8 * GiB, '/home')], int(1.5 * GiB))
        copy.assert_not_called()
        self.assertEqual(result['state'], 'FAILED')
        self.assertEqual(result['error_text'], 'Copying 1.5 GiB to /home/alice/dst would fill its filesystem to 95% '
                         '(limit 90%): 2.0 GiB of 10.0 GiB free. Free space or choose another destination.')

    def test_fits_or_no_limit_in_the_ticket(self):
        for options in ({'copy_links': True, 'max_fill_percent': 90}, {'copy_links': True}):
            copy, result = self.run_job(options, [Limit(FILESYSTEM, 10 * GiB, 8 * GiB, '/home')],
                                        GiB if 'max_fill_percent' in options else 100 * GiB)
            copy.assert_called_once()
            self.assertEqual(result['state'], 'SUCCESS')

    def test_invalid_limit_in_the_ticket_fails_the_job(self):
        copy, result = self.run_job({'max_fill_percent': '90; rm'}, [], 1)
        copy.assert_not_called()
        self.assertIn('Invalid max_fill_percent', result['error_text'])


if __name__ == '__main__':
    unittest.main()
