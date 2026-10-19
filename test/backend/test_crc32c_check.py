"""
CRC32C integrity checks of S3 (utils/job_runner.py hashsum_type / run_hashsum,
utils/rclone_connection.py supported_hashes / md5sum_with_credentials, utils/
hashsum_job_queue.py, the worker agent and worker_manager): the hash a check uses,
rclone's capability, the MD5 fallback for objects without a stored CRC32C and the
files that cannot be checked.
"""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from api.utils import job_runner
from api.utils import rclone_connection
from api.utils import rclone_tuning as tuning
from api.utils.abstract_connection import RcloneException
from api.utils.hashsum_job_queue import HashsumJobQueue, parse_line
from api.utils.rclone_connection import RcloneConnection

from test_rclone_tuning import PLAIN
from test_workers import WorkerTestBase
from api.managers import worker_manager
from api.models import CloudConnection, HashsumJob
from api.application import db


COMBO_HASHSUM = '''Supported hashes are:
- md5
- sha1
- whirlpool
- crc32
- sha256
- sha512
- blake3
- xxh3
- xxh128
- crc32c
- dropbox
- hidrive
- mailru
- quickxor
'''
OLD_HASHSUM = COMBO_HASHSUM.replace('- crc32c\n', '')

YES = lambda: True
NO = lambda: False


class TestHashType(unittest.TestCase):

    def test_kinds(self):
        self.assertEqual(job_runner.side_kind(True, 's3', 'AWS'), 'local')
        self.assertEqual(job_runner.side_kind(False, 's3', 'AWS'), 's3')
        # only Amazon S3 stores a CRC32C: other providers would answer a HEAD per object for nothing
        for provider in ('Ceph', 'Minio', 'Other', None):
            self.assertEqual(job_runner.side_kind(False, 's3', provider), 'other', provider)
        self.assertEqual(job_runner.side_kind(False, 'azureblob', 'AWS'), 'other')
        self.assertEqual(job_runner.side_kind(False, None), 'other')

    def test_effective_provider(self):
        from api.utils import s3_providers
        connection = mock.Mock(s3_provider='Wasabi', s3_endpoint='https://s3.wasabisys.com')
        self.assertEqual(s3_providers.effective_provider(connection), 'Wasabi')
        # connections from before s3_provider existed: AWS without an endpoint, else Other
        self.assertEqual(s3_providers.effective_provider(mock.Mock(s3_provider=None, s3_endpoint=None)), 'AWS')
        self.assertEqual(s3_providers.effective_provider(mock.Mock(s3_provider='', s3_endpoint='https://x')), 'Other')
        self.assertEqual(s3_providers.effective_provider(mock.Mock(s3_provider='nonsense', s3_endpoint=None)), 'AWS')

    def test_crc32c_with_s3_and_local_or_s3(self):
        for sides in (('local', 's3'), ('s3', 'local'), ('s3', 's3')):
            self.assertEqual(job_runner.hashsum_type(*sides, crc32c_supported=YES), 'crc32c', sides)
            self.assertEqual(job_runner.hashsum_type(*sides, crc32c_supported=NO), 'md5', sides)
            self.assertEqual(job_runner.hashsum_type(*sides), 'md5', sides)
            # with download, rclone hashes the data: MD5 as before (never --download for CRC32C)
            self.assertEqual(job_runner.hashsum_type(*sides, download=True, crc32c_supported=YES), 'md5', sides)

    def test_others_keep_md5_and_local_keeps_sha256(self):
        for sides in (('s3', 'other'), ('other', 's3'), ('local', 'other'), ('other', 'other')):
            self.assertEqual(job_runner.hashsum_type(*sides, crc32c_supported=YES), 'md5', sides)
        asked = mock.Mock(return_value=True)
        self.assertEqual(job_runner.hashsum_type('local', 'local', crc32c_supported=asked), 'sha256')
        asked.assert_not_called() # rclone is asked only when CRC32C could be used

    def test_outcome(self):
        self.assertEqual(job_runner.hashsum_outcome([], []), 'Files are IDENTICAL!')
        self.assertEqual(job_runner.hashsum_outcome([], [], 2), 'Files are IDENTICAL, but 2 could not be checked!')
        self.assertEqual(job_runner.hashsum_outcome([{'title': 'a'}], [], 2), 'Files are DIFFERENT!')


class TestCapability(unittest.TestCase):

    def setUp(self):
        rclone_connection._supported_hashes.clear()
        self.addCleanup(rclone_connection._supported_hashes.clear)

    def test_parse(self):
        self.assertIn('crc32c', rclone_connection.parse_supported_hashes(COMBO_HASHSUM))
        self.assertIn('sha256', rclone_connection.parse_supported_hashes(OLD_HASHSUM))
        self.assertNotIn('crc32c', rclone_connection.parse_supported_hashes(OLD_HASHSUM))
        self.assertEqual(rclone_connection.parse_supported_hashes('Error: unknown command'), frozenset())
        # the list ends at the first line that is not "- <hash>"
        self.assertEqual(rclone_connection.parse_supported_hashes('Supported hashes are:\n- md5\n\n- crc32c\n'),
                         frozenset({'md5'}))

    def test_asks_rclone_once_and_caches_only_an_answer(self):
        answer = subprocess.CompletedProcess([], 0, COMBO_HASHSUM, '')
        with mock.patch.object(rclone_connection.subprocess, 'run', return_value=answer) as run:
            self.assertTrue(rclone_connection.crc32c_supported('/usr/local/bin/rclone'))
            self.assertTrue(rclone_connection.crc32c_supported('/usr/local/bin/rclone'))
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertEqual(command, ['/usr/local/bin/rclone', '--config=/dev/null', 'hashsum'])
        self.assertNotIn('MOTUZ_FLASK_SECRET_KEY', run.call_args.kwargs['env'])
        with mock.patch.object(rclone_connection.subprocess, 'run', side_effect=subprocess.TimeoutExpired('r', 30)) as run:
            self.assertFalse(rclone_connection.crc32c_supported('/var/lib/motuz-runas/rclone'))
            self.assertFalse(rclone_connection.crc32c_supported('/var/lib/motuz-runas/rclone'))
        self.assertEqual(run.call_count, 2, 'a failure is not cached')
        old = subprocess.CompletedProcess([], 0, OLD_HASHSUM, '')
        with mock.patch.object(rclone_connection.subprocess, 'run', return_value=old):
            self.assertFalse(rclone_connection.crc32c_supported('/var/lib/motuz-runas/rclone'))


class TestParsing(unittest.TestCase):

    def test_crc32c_lines(self):
        self.assertEqual(parse_line('0b59a624  sub/small.bin\n', 'crc32c'), {'Name': 'sub/small.bin', 'md5chksum': '0b59a624'})
        # S3 object without a FULL_OBJECT CRC32C: rclone prints 8 spaces
        self.assertEqual(parse_line('          md5mode/big.bin\n', 'crc32c'), {'Name': 'md5mode/big.bin', 'md5chksum': None})
        self.assertEqual(parse_line('            leading space.bin\n', 'crc32c'), {'Name': '  leading space.bin', 'md5chksum': None})
        self.assertEqual(parse_line('UNSUPPORTED  x\n', 'crc32c'), {'Name': 'x', 'md5chksum': None})
        self.assertEqual(parse_line('0b59a624  a  b.txt', 'crc32c'), {'Name': 'a  b.txt', 'md5chksum': '0b59a624'})
        self.assertIsNone(parse_line('2026/10/18 NOTICE: something', 'crc32c'))

    def test_md5_lines_as_before(self):
        self.assertEqual(parse_line('{}  a.txt\n'.format('b' * 32)), {'Name': 'a.txt', 'md5chksum': 'b' * 32})
        self.assertEqual(parse_line('{}  a.txt\n'.format(' ' * 32)), {'Name': 'a.txt', 'md5chksum': None})
        # a CRC32C line is never taken for an MD5 one
        self.assertIsNone(parse_line('0b59a624  short.bin', 'md5'))


class TestCommands(unittest.TestCase):

    def pushed(self, **kwargs):
        connection = RcloneConnection()
        pushed = {}
        with mock.patch.object(tuning, '_settings', PLAIN), \
                mock.patch.object(connection._hashsum_job_queue, 'push',
                                  side_effect=lambda command, env, job_id, **kw: pushed.update(command=command, **kw)):
            connection.md5sum_with_credentials({'RCLONE_CONFIG_SRC_TYPE': 's3'}, resource_path='bucket/data',
                                               local=False, user='alice', job_id='1_src', **kwargs)
        return pushed

    def test_crc32c_command(self):
        pushed = self.pushed(hash_type='crc32c', extra_flags=['--checkers=64'])
        self.assertEqual(pushed['command'][-3:], ['hashsum', 'crc32c', 'src:bucket/data'])
        self.assertIn('--checkers=64', pushed['command'])
        self.assertNotIn('--download', pushed['command'])
        self.assertEqual((pushed['hash_type'], pushed['stdin']), ('crc32c', None))

    def test_md5_fallback_reads_the_names_from_stdin(self):
        pushed = self.pushed(files=['sub/a b.bin', '-rf', 'x.txt'])
        self.assertEqual(pushed['command'][-4:], ['md5sum', 'src:bucket/data', '--files-from-raw', '-'])
        self.assertNotIn('-rf', pushed['command'], 'names never become arguments')
        self.assertEqual(pushed['stdin'], b'sub/a b.bin\n-rf\nx.txt\n')
        self.assertEqual(pushed['hash_type'], 'md5')
        for bad in (['a\nb'], [''], ['a\rb'], [None]):
            with self.assertRaises(RcloneException):
                self.pushed(files=bad)
        with self.assertRaises(KeyError):
            self.pushed(hash_type='--config=/x')

    def test_a_local_list_has_no_other_filter(self):
        """rclone refuses --exclude next to --files-from-raw (found against AWS); the list
        comes from a run that excluded .snapshot already"""
        import tempfile
        connection = RcloneConnection()
        pushed = {}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(tuning, '_settings', PLAIN), \
                mock.patch.object(connection._hashsum_job_queue, 'push',
                                  side_effect=lambda command, env, job_id, **kw: pushed.update(command=command)):
            connection.md5sum(None, folder, 'alice', '1_src_md5', files=['a.txt'])
            self.assertFalse([a for a in pushed['command'] if a.startswith('--exclude')])
            connection.md5sum(None, folder, 'alice', '1_src', hash_type='crc32c')
            self.assertIn('--exclude=\\.snapshot/', pushed['command'])
            self.assertEqual(pushed['command'][-4:-1], ['hashsum', 'crc32c', folder])

    def test_queue_passes_stdin(self):
        queue = HashsumJobQueue()
        script = 'import sys\nfor n in sys.stdin.read().split():\n    print("{}  " + n)'.format('c' * 32)
        queue.push([sys.executable, '-c', script], dict(os.environ), 'p', stdin=b'a.txt\nb/c.txt\n')
        for _ in range(100):
            if queue.hashsum_finished('p'):
                break
            time.sleep(0.05)
        self.assertEqual([e['Name'] for e in queue.hashsum_text('p')], ['a.txt', 'b/c.txt'])

    def test_a_long_list_neither_blocks_nor_is_cut(self):
        """The list is written while the output is read (both longer than a pipe buffer):
        a child that answers every line as it reads it must not deadlock"""
        queue = HashsumJobQueue()
        names = ['dir {}/file {:06d}.bin'.format(i % 7, i) for i in range(60000)]
        script = 'import sys\nfor line in sys.stdin:\n    sys.stdout.write("{}  " + line)'.format('c' * 32)
        queue.push([sys.executable, '-c', script], dict(os.environ), 'p',
                   stdin=rclone_connection.files_from_list(names))
        for _ in range(600):
            if queue.hashsum_finished('p'):
                break
            time.sleep(0.05)
        self.assertTrue(queue.hashsum_finished('p'), 'the run did not finish: a pipe deadlock?')
        self.assertEqual(queue.hashsum_exitstatus('p'), 0)
        self.assertEqual([e['Name'] for e in queue.hashsum_text('p')], names)


class ScriptedConnection:
    """hashsum output of each run id: {run_id: [(name, hash)]}"""
    def __init__(self, outputs, exitstatus=None):
        self.outputs = outputs
        self.exitstatus = exitstatus or {}
        self.started = []
        self.polled = set()

    def start(self, side, run_id):
        self.started.append((side, run_id, 'main', None))

    def start_fallback(self, side, run_id, names):
        self.started.append((side, run_id, 'md5', names))

    def hashsum_finished(self, run_id):
        # one tick per run
        if run_id in self.polled:
            return True
        self.polled.add(run_id)
        return False

    def hashsum_text(self, run_id):
        return [{'Name': name, 'md5chksum': value} for name, value in self.outputs.get(run_id, [])]

    def hashsum_exitstatus(self, run_id):
        return self.exitstatus.get(run_id, 0)

    def hashsum_error_text(self, run_id):
        return ''

    def hashsum_percent(self, run_id):
        return 100

    def hashsum_delete(self, run_id):
        pass


def _names(tree, prefix=''):
    names = {}
    for node in tree:
        if node.get('isLeaf'):
            names[prefix + node['title']] = node.get('hash')
        else:
            names.update(_names(node.get('children', []), prefix + node['title'] + '/'))
    return names


class TestFallback(unittest.TestCase):

    def run_check(self, connection, hash_type='crc32c'):
        ticks = []
        result = job_runner.run_hashsum(connection, 7, connection.start, lambda *a: ticks.append(a[1]), interval=0,
                                        hash_type=hash_type, start_fallback=connection.start_fallback)
        return result, ticks

    def test_only_files_without_a_crc32c_are_compared_by_md5(self):
        connection = ScriptedConnection({
            '7_src': [('ok.bin', '0b59a624'), ('changed.bin', '11111111'), ('old.bin', 'aaaaaaaa'),
                      ('old-changed.bin', 'bbbbbbbb'), ('no-md5.bin', 'cccccccc'), ('d/only-src.bin', 'dddddddd')],
            '7_dst': [('ok.bin', '0b59a624'), ('changed.bin', '22222222'), ('old.bin', None),
                      ('old-changed.bin', None), ('no-md5.bin', None)],
            '7_src_md5': [('old.bin', 'e' * 32), ('old-changed.bin', 'f' * 32), ('no-md5.bin', '1' * 32)],
            '7_dst_md5': [('old.bin', 'e' * 32), ('old-changed.bin', '0' * 32), ('no-md5.bin', None)],
        })
        result, ticks = self.run_check(connection)
        fallback = ['no-md5.bin', 'old-changed.bin', 'old.bin']
        self.assertEqual(connection.started, [('src', '7_src', 'main', None), ('dst', '7_dst', 'main', None),
                                              ('src', '7_src_md5', 'md5', fallback), ('dst', '7_dst_md5', 'md5', fallback)])
        self.assertEqual(result['state'], 'SUCCESS')
        self.assertEqual(result['hash_type'], 'crc32c')
        self.assertEqual(result['md5_fallback'], 2)
        self.assertEqual(result['not_checkable'], {'count': 1, 'sample': ['no-md5.bin']})
        src, dst = _names(result['src_tree']), _names(result['dst_tree'])
        # differing: by CRC32C, by MD5, and the file only on one side; never the not checkable one
        self.assertEqual(set(src), {'changed.bin', 'old-changed.bin', 'd/only-src.bin'})
        self.assertEqual(set(dst), {'changed.bin', 'old-changed.bin'})
        self.assertEqual((src['old-changed.bin'], dst['old-changed.bin']), ('f' * 32, '0' * 32))
        self.assertEqual((max(ticks), sorted(ticks)), (100, ticks))

    def test_all_with_crc32c_needs_no_fallback(self):
        connection = ScriptedConnection({'7_src': [('a', '0b59a624')], '7_dst': [('a', '0b59a624')]})
        result, ticks = self.run_check(connection)
        self.assertEqual(len(connection.started), 2)
        self.assertEqual((result['src_tree'], result['dst_tree'], result['md5_fallback'], result['not_checkable']),
                         ([], [], 0, {'count': 0, 'sample': []}))
        self.assertLessEqual(max(ticks), 90)

    def test_a_single_file_is_checked_without_a_list(self):
        connection = ScriptedConnection({'7_src': [('big.bin', '0b59a624')], '7_dst': [('big.bin', None)],
                                         '7_src_md5': [('big.bin', 'e' * 32)], '7_dst_md5': [('big.bin', 'e' * 32)]})
        result, _ = self.run_check(connection)
        self.assertEqual([s[3] for s in connection.started[2:]], [None, None])
        self.assertEqual((result['src_tree'], result['dst_tree'], result['md5_fallback']), ([], [], 1))

    def test_md5_checks_have_no_fallback(self):
        connection = ScriptedConnection({'7_src': [('a', 'e' * 32)], '7_dst': [('a', None)]})
        result, _ = self.run_check(connection, hash_type='md5')
        self.assertEqual(len(connection.started), 2)
        self.assertEqual(set(_names(result['dst_tree'])), {'a'}, 'a missing MD5 is shown as before')

    def test_failed_fallback_fails_the_check(self):
        connection = ScriptedConnection({'7_src': [('a', '0b59a624'), ('b', '1')], '7_dst': [('a', None), ('b', '1')]},
                                        exitstatus={'7_dst_md5': 1})
        result, _ = self.run_check(connection)
        self.assertEqual((result['state'], result['failed_side']), ('FAILED', 'dst'))

    def test_record_hash_result_validates(self):
        job = mock.Mock(spec=['hash_type', 'md5_fallback', 'not_checkable', 'not_checkable_sample'])
        job_runner.record_hash_result(job, {'hash_type': 'crc32c', 'md5_fallback': 2,
                                            'not_checkable': {'count': 3, 'sample': ['a', 5, 'b' * 5000]}})
        self.assertEqual((job.hash_type, job.md5_fallback, job.not_checkable), ('crc32c', 2, 3))
        self.assertEqual(job.not_checkable_sample, ['a', 'b' * 4096])
        job = mock.Mock(spec=['hash_type', 'md5_fallback', 'not_checkable', 'not_checkable_sample'], hash_type=None,
                        md5_fallback=None, not_checkable=None)
        job_runner.record_hash_result(job, {'hash_type': '--x', 'md5_fallback': True, 'not_checkable': {'count': -1, 'sample': []}})
        self.assertEqual((job.hash_type, job.md5_fallback, job.not_checkable), (None, None, None))


class TestWorkerReports(WorkerTestBase):

    def test_finish_stores_the_hash_and_not_checkable_files(self):
        s3 = CloudConnection(owner='alice', name='c', type='s3', s3_access_key_id='AKIAALICE', s3_secret_access_key='x')
        db.session.add(s3)
        db.session.commit()
        job = HashsumJob(owner='alice', src_resource_path='/home/alice/a', dst_cloud_id=s3.id, dst_resource_path='b/a',
                         option_download=False, progress_state='PROGRESS', notification_email='a@example.com')
        db.session.add(job)
        db.session.commit()
        worker_manager.queue_job('hashsum', job, 'onprem')
        worker, _ = self.worker()
        ticket = self.claim(worker)
        # the worker decides from them (s3_provider: as rclone gets it, AWS for this old-style connection)
        self.assertEqual((ticket['job']['dst']['type'], ticket['job']['dst']['s3_provider']), ('s3', 'AWS'))
        self.assertNotIn('s3_provider', ticket['job']['src'])
        worker_manager.progress(worker, ticket['ticket_id'], ticket['ticket_token'], {'percent': 3, 'hash_type': 'crc32c'})
        self.assertEqual(db.session.get(HashsumJob, job.id).hash_type, 'crc32c')
        with mock.patch.object(worker_manager, '_send') as send:
            worker_manager.finish(worker, ticket['ticket_id'], ticket['ticket_token'], {
                'state': 'SUCCESS', 'src_tree': [], 'dst_tree': [], 'hash_type': 'crc32c', 'md5_fallback': 4,
                'not_checkable': {'count': 1, 'sample': ['big.bin']}})
        job = db.session.get(HashsumJob, job.id)
        self.assertEqual((job.hash_type, job.md5_fallback, job.not_checkable, job.not_checkable_sample),
                         ('crc32c', 4, 1, ['big.bin']))
        self.assertIn('1 could not be checked', send.call_args.args[0][0][1])


class TestWorkerAgentHashsum(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'worker'))
        import motuz_worker
        cls.agent = motuz_worker

    def run_job(self, supported, provider='AWS'):
        args = mock.Mock(central_url=None, bootstrap_token='mzb1.1.x', bootstrap_token_file=None, once=False)
        config = self.agent.Config({'MOTUZ_CENTRAL_URL': 'https://motuz.test'}, args)
        ticket = {'ticket_id': 1, 'ticket_token': 't', 'progress_interval': 1, 'job': {
            'type': 'hashsum', 'id': 3, 'owner': 'alice', 'options': {'download': False},
            'src': {'local': True, 'path': '/home/alice/a'},
            'dst': {'local': False, 'remote': 'src', 'type': 's3', 's3_provider': provider, 'path': 'b/a',
                    'rclone_env': {'RCLONE_CONFIG_SRC_TYPE': 's3'}}}}
        central = mock.Mock()
        central.call.return_value = (200, {'action': 'continue'})
        run = self.agent.JobRun(central, config, ticket, lambda: False)
        outputs = {'3_src': [('a', '0b59a624'), ('b', '11111111')], '3_dst': [('a', None), ('b', '11111111')],
                   '3_src_md5': [('a', 'e' * 32)], '3_dst_md5': [('a', 'e' * 32)]}
        calls = []

        def md5sum(credentials, **kwargs):
            calls.append((kwargs['job_id'], kwargs['hash_type'], kwargs.get('files')))
            run.connection._fake = kwargs['job_id']

        fake = ScriptedConnection(outputs)
        with mock.patch.object(self.agent, 'crc32c_supported', return_value=supported), \
                mock.patch.object(run.connection, 'md5sum_with_credentials', side_effect=md5sum), \
                mock.patch.multiple(run.connection, hashsum_finished=fake.hashsum_finished, hashsum_text=fake.hashsum_text,
                                    hashsum_exitstatus=fake.hashsum_exitstatus, hashsum_error_text=fake.hashsum_error_text,
                                    hashsum_percent=fake.hashsum_percent, hashsum_delete=fake.hashsum_delete), \
                mock.patch.object(run, 'finish') as finish:
            run.run()
        return calls, finish.call_args.args[0]

    def test_crc32c_with_fallback_on_the_worker(self):
        calls, body = self.run_job(True)
        self.assertEqual(calls, [('3_src', 'crc32c', None), ('3_dst', 'crc32c', None),
                                 ('3_src_md5', 'md5', ['a']), ('3_dst_md5', 'md5', ['a'])])
        self.assertEqual((body['state'], body['hash_type'], body['md5_fallback']), ('SUCCESS', 'crc32c', 1))
        self.assertEqual(body['not_checkable'], {'count': 0, 'sample': []})

    def test_an_older_rclone_keeps_md5(self):
        calls, body = self.run_job(False)
        self.assertEqual([c[1] for c in calls], ['md5', 'md5'])
        self.assertEqual(body['hash_type'], 'md5')

    def test_another_s3_provider_keeps_md5(self):
        calls, body = self.run_job(True, provider='Ceph')
        self.assertEqual([c[1] for c in calls], ['md5', 'md5'])
        self.assertEqual(body['hash_type'], 'md5')
