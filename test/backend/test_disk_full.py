"""
A full local destination ends a copy job at once (utils/disk_full.py,
utils/copy_job_queue.py, utils/job_runner.watch_copy, shared with remote workers).
rclone is replaced by a script that prints the lines rclone v1.76 printed for a copy to
/dev/full (captured: `rclone copyto --progress --stats 2s --inplace f.bin /dev/full`,
non-TTY), then waits as rclone would while it retries. With MOTUZ_TEST_RCLONE set to a
real rclone binary (and not root), the real rclone writes to /dev/full.
"""
import logging
import os
import signal
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from api.utils import disk_full, job_runner, rclone_connection
from api.utils.copy_job_queue import CopyJobQueue
from api.utils.rclone_connection import RcloneConnection


# rclone's stdout with --progress (not a terminal) when the destination is full
ENOSPC_STDOUT = (
    '\n'
    '2026/10/18 08:51:46 NOTICE: full: Removing partially written file on error: write /dev/full: no space left on device\n'
    'Transferred:   \t          0 B / 195.312 KiB, 0%, 0 B/s, ETA -\n'
    'Transferred:            0 / 1, 0%\n'
    'Elapsed time:         0.0s\n'
    'Transferring:\n'
    ' *                                        f1.bin:  0% / 195.312 KiB, 0 B/s, -\n'
    '2026/10/18 08:51:46 ERROR : f1.bin: Failed to copy: write /dev/full: no space left on device\n'
    'Transferred:   \t          0 B / 195.312 KiB, 0%, 0 B/s, ETA -\n'
    'Errors:                 1 (retrying may help)\n'
    'Transferred:            0 / 1, 0%\n'
    'Elapsed time:         0.0s\n'
    '2026/10/18 08:51:46 ERROR : Attempt 1/3 failed with 1 errors and: write /dev/full: no space left on device\n'
)
# rclone rcat's stderr when the destination is full
RCAT_STDERR = (
    '2026/10/18 08:52:27 NOTICE: full: Removing partially written file on error: write /dev/full: no space left on device\n'
    '2026/10/18 08:52:27 ERROR : full: Post request put error: write /dev/full: no space left on device\n'
    '2026/10/18 08:52:27 NOTICE: Failed to rcat with 2 errors: last error was: write /dev/full: no space left on device\n'
)

# Prints the files `stdout` and `stderr` next to it, then waits (with a child, like sudo
# and rclone) as rclone does while it retries; writes its environment to `env` and the
# child's pid to `child`. Ignores its arguments (rclone's command line).
FAKE_RCLONE = textwrap.dedent('''
    import json, os, subprocess, sys, time
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, 'env'), 'w') as f:
        json.dump(dict(os.environ), f)
    with open(os.path.join(here, 'child'), 'w') as f:
        f.write(str(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']).pid))
    stats = 'Transferred:   \\t  1.5 MiB / 3 MiB, 50%, 0 B/s, ETA -\\nTransferred:            2 / 5, 40%\\n'
    sys.stdout.write(stats)
    sys.stdout.flush()
    time.sleep(0.3)
    sys.stdout.write(open(os.path.join(here, 'stdout')).read())
    sys.stdout.flush()
    sys.stderr.write(open(os.path.join(here, 'stderr')).read())
    sys.stderr.flush()
    time.sleep(60)
''')


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try: # A zombie of the killed group: reaped by its parent, or init
        with open('/proc/{}/stat'.format(pid)) as f:
            return f.read().split(')')[-1].split()[0] != 'Z'
    except OSError:
        return False


class DiskFullLinesTest(unittest.TestCase):

    def test_rclones_lines(self):
        for line in ENOSPC_STDOUT.splitlines() + RCAT_STDERR.splitlines():
            if 'no space left on device' in line:
                self.assertEqual(disk_full.kind(line), disk_full.FULL, line)
        self.assertEqual(disk_full.kind('ERROR : big.bin: Failed to copy: preallocate: file too big for remaining disk space'),
                         disk_full.FULL)
        self.assertEqual(disk_full.kind('ERROR : a: Failed to copy: write /home/alice/a: disk quota exceeded'),
                         disk_full.QUOTA)

    def test_other_errors_and_file_names(self):
        for line in (
            'ERROR : a: Failed to copy: write /dst/a: file too large', # EFBIG (ulimit -f), not a full disk
            'ERROR : a: Failed to copy: open /dst/a: permission denied',
            'ERROR : no space left on device: Failed to copy: open /dst/x: permission denied',
            'INFO  : disk quota exceeded.txt: Copied (new)',
            'Transferred:            0 / 1, 0%',
        ):
            self.assertIsNone(disk_full.kind(line), line)

    def test_scan_takes_the_last_line_without_its_timestamp(self):
        self.assertEqual(disk_full.scan(RCAT_STDERR), (
            disk_full.FULL, 'NOTICE: Failed to rcat with 2 errors: last error was: write /dev/full: no space left on device'))
        self.assertIsNone(disk_full.scan('ERROR : a: permission denied\n'))

    def test_message(self):
        self.assertEqual(disk_full.message(disk_full.FULL, '/data/x', 3), (
            'The destination filesystem is full (no space left on device) at /data/x: 3 files were copied '
            'before. Free space or choose another destination.'))
        text = disk_full.message(disk_full.QUOTA, '/home/alice', 1, 'uploaded', 'ERROR : a: disk quota exceeded')
        self.assertIn('disk quota exceeded) at /home/alice: 1 file was uploaded before', text)
        self.assertTrue(text.endswith('\nrclone: ERROR : a: disk quota exceeded'))


class DiskFullCopyTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.script = self.path('rclone.py')
        with open(self.script, 'w') as f:
            f.write('#!' + sys.executable + '\n' + FAKE_RCLONE)
        os.chmod(self.script, 0o755)

    def path(self, name):
        return os.path.join(self.directory.name, name)

    def command(self, stdout='', stderr=''):
        for name, text in (('stdout', stdout), ('stderr', stderr)):
            with open(self.path(name), 'w') as f:
                f.write(text)
        return [self.script, '--config=/dev/null', 'copyto', '/data/in', '/data/out']

    def child(self):
        for _ in range(100):
            if os.path.exists(self.path('child')) and os.path.getsize(self.path('child')):
                with open(self.path('child')) as f:
                    pid = int(f.read())
                self.addCleanup(lambda: _alive(pid) and os.kill(pid, signal.SIGKILL))
                return pid
            time.sleep(0.05)
        self.fail('the fake rclone did not start')

    def wait(self, queue, job_id, seconds=10):
        deadline = time.monotonic() + seconds
        while not queue.copy_finished(job_id):
            if time.monotonic() > deadline:
                queue.terminate_all()
                self.fail('rclone was not stopped')
            time.sleep(0.05)

    def test_stopped_at_the_first_line_with_the_reason(self):
        queue = CopyJobQueue()
        start = time.monotonic()
        queue.push(self.command(stdout=ENOSPC_STDOUT), {}, 1, local_dst='/data/alice/out')
        child = self.child()
        self.wait(queue, 1)
        self.assertLess(time.monotonic() - start, 10)
        self.assertNotEqual(queue.copy_exitstatus(1), 0)
        self.assertEqual(job_runner.exit_state(queue.copy_exitstatus(1)), 'FAILED')
        self.assertEqual(queue.copy_disk_full(1), disk_full.FULL)
        error = queue.copy_error_text(1)
        self.assertTrue(error.startswith(
            # rclone's own stats in ENOSPC_STDOUT: 0 / 1 files
            'The destination filesystem is full (no space left on device) at /data/alice/out: 0 files were '
            'copied before. Free space or choose another destination.\nrclone: '), error)
        self.assertIn('no space left on device', error.split('\n')[1])
        self.assertIn('ERROR : f1.bin: Failed to copy: write /dev/full: no space left on device', error)
        time.sleep(0.2)
        self.assertFalse(_alive(child), "rclone's whole process group is stopped")

    def test_quota_on_stderr(self):
        queue = CopyJobQueue()
        queue.push(self.command(stderr='2026/10/18 08:51:46 ERROR : a: Failed to copy: write /home/alice/a: disk quota exceeded\n'),
                   {}, 2, local_dst='/home/alice')
        self.child()
        self.wait(queue, 2)
        self.assertEqual(queue.copy_disk_full(2), disk_full.QUOTA)
        self.assertTrue(queue.copy_error_text(2).startswith(
            'Your disk quota on the destination filesystem is exceeded (disk quota exceeded) at /home/alice: 2 files'))
        self.assertIn('\nrclone: ERROR : a: Failed to copy: write /home/alice/a: disk quota exceeded\n', queue.copy_error_text(2))

    def test_a_cloud_destination_is_left_to_rclone(self):
        queue = CopyJobQueue()
        queue.push(self.command(stdout=ENOSPC_STDOUT), {}, 3)
        self.child()
        time.sleep(1.5)
        self.assertFalse(queue.copy_finished(3))
        self.assertIsNone(queue.copy_disk_full(3))
        queue.terminate_all()
        self.wait(queue, 3)
        self.assertFalse(queue.copy_error_text(3).startswith('The destination'))

    def test_watch_copy_through_copy_with_credentials(self):
        # The path of Celery jobs and remote workers: rclone as the user, watched once per tick
        connection = RcloneConnection()
        command = self.command(stdout=ENOSPC_STDOUT)
        with mock.patch.object(rclone_connection, 'sudo_as', lambda user, extra=None: []), \
                mock.patch.object(rclone_connection, 'RCLONE', command[0]), \
                mock.patch.object(rclone_connection.RcloneConnection, '_log_command'):
            connection.copy_with_credentials({}, src_resource_path='/data/in', src_local=True,
                                             dst_resource_path='/data/out', dst_local=True, user='alice',
                                             copy_links=False, job_id=4)
        ticks = []
        exitstatus = job_runner.watch_copy(connection, 4, lambda *args: ticks.append(args), interval=0.05)
        self.assertEqual(job_runner.exit_state(exitstatus), 'FAILED')
        self.assertTrue(connection.copy_error_text(4).startswith('The destination filesystem is full'),
                        connection.copy_error_text(4))
        self.assertIn(' at /data/out: ', connection.copy_error_text(4))
        with open(self.path('env')) as f:
            self.assertIn('"RCLONE_LOCAL_FATAL_IF_NO_SPACE": "true"', f.read())

    def test_a_cloud_destination_gets_no_fatal_variable(self):
        connection = RcloneConnection()
        pushed = {}
        with mock.patch.object(connection._copy_job_queue, 'push',
                               side_effect=lambda command, env, job_id, local_dst=None, **kept: pushed.update(env=env, local_dst=local_dst)), \
                mock.patch.object(rclone_connection.RcloneConnection, '_log_command'):
            connection.copy_with_credentials({}, src_resource_path='/a', src_local=True, dst_resource_path='bucket/b',
                                             dst_local=False, user='alice', copy_links=False, job_id=5)
            self.assertNotIn('RCLONE_LOCAL_FATAL_IF_NO_SPACE', pushed['env'])
            self.assertIsNone(pushed['local_dst'])
            connection.copy_with_credentials({}, src_resource_path='bucket/b', src_local=False, dst_resource_path='/a',
                                             dst_local=True, user='alice', copy_links=False, job_id=6)
            self.assertEqual(pushed['env']['RCLONE_LOCAL_FATAL_IF_NO_SPACE'], 'true')
            self.assertEqual(pushed['local_dst'], '/a')

    @unittest.skipUnless(os.environ.get('MOTUZ_TEST_RCLONE') and os.path.exists('/dev/full') and os.geteuid() != 0,
                         'MOTUZ_TEST_RCLONE (a real rclone) not set, or root (rclone would remove /dev/full)')
    def test_real_rclone_writing_to_dev_full(self):
        source = self.path('in.bin')
        with open(source, 'wb') as f:
            f.write(os.urandom(300000))
        connection = RcloneConnection()
        with mock.patch.object(rclone_connection, 'sudo_as', lambda user, extra=None: []), \
                mock.patch.object(rclone_connection, 'RCLONE', os.environ['MOTUZ_TEST_RCLONE']), \
                mock.patch.object(rclone_connection.RcloneConnection, '_log_command'):
            # --inplace: write /dev/full itself, not a .partial file next to it
            connection.copy_with_credentials({}, src_resource_path=source, src_local=True,
                                             dst_resource_path='/dev/full', dst_local=True, user='alice',
                                             copy_links=False, job_id=7, extra_env={'RCLONE_INPLACE': 'true'})
        exitstatus = job_runner.watch_copy(connection, 7, lambda *args: None, interval=0.05)
        self.assertEqual(job_runner.exit_state(exitstatus), 'FAILED')
        self.assertTrue(connection.copy_error_text(7).startswith(
            'The destination filesystem is full (no space left on device) at /dev/full: 0 files were copied before.'),
            connection.copy_error_text(7))
        self.assertIn('write /dev/full: no space left on device', connection.copy_error_text(7))


if __name__ == '__main__':
    unittest.main()
