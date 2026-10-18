"""
Overwrite policy (managers/overwrite_policy.py, utils/motuz_destinations.py): storage
keys, path normalization and segment-wise matching, registration of copy job
destinations, --ignore-existing on the command (Celery and worker tickets), the setting
MOTUZ_ALLOW_GENERAL_OVERWRITES and GET /api/copy-jobs/overwrite-policy/. Uploads are in
test_uploads.py. Against SQLite.
"""
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from flask import Flask
import flask_jwt_extended as flask_jwt

from api.application import db, jwt, register_api
from api.config import TestingConfig
from api.managers import copy_job_manager, job_routing, overwrite_policy
from api.models import CloudConnection, CopyJob, MotuzDestination
from api.utils import motuz_destinations, rclone_tuning
from api.utils.motuz_destinations import ancestors, normalized_path, storage_key
from api.utils.rclone_connection import RcloneConnection


def connection(**fields):
    defaults = {name: None for name in (
        'type', 'subtype', 'profile_source', 's3_endpoint', 's3_provider', 's3_region', 'azure_account',
        'azure_sas_url', 'swift_auth', 'swift_tenant', 'swift_user', 'sftp_host', 'sftp_port',
        'onedrive_drive_id', 'gdrive_team_drive', 'gdrive_root_folder_id', 'webdav_url')}
    return SimpleNamespace(**{**defaults, **fields})


class StorageKeyTest(unittest.TestCase):

    def test_local(self):
        self.assertEqual(storage_key(None), 'local:')

    def test_s3(self):
        self.assertEqual(storage_key(connection(type='s3')), 's3:aws')
        self.assertEqual(storage_key(connection(type='s3', s3_region='us-west-2', s3_provider='AWS')), 's3:aws')
        self.assertEqual(storage_key(connection(type='s3', s3_region='cn-north-1')), 's3:aws-cn')
        for endpoint in ('https://S3.Lab.example/', 's3.lab.example', 'https://s3.lab.example:443'):
            self.assertEqual(storage_key(connection(type='s3', s3_endpoint=endpoint)), 's3:https://s3.lab.example')
        self.assertEqual(storage_key(connection(type='s3', s3_endpoint='http://minio:9000')), 's3:http://minio:9000')
        self.assertIsNone(storage_key(connection(type='s3', s3_provider='Ceph')), 'not AWS, but where?')

    def test_independent_of_owner_and_credentials(self):
        a = connection(type='s3', s3_endpoint='https://s3.lab.example', owner='alice', s3_access_key_id='A')
        b = connection(type='s3', s3_endpoint='https://s3.lab.example/', owner='bob', s3_access_key_id='B')
        self.assertEqual(storage_key(a), storage_key(b))

    def test_profiles(self):
        self.assertIsNone(storage_key(connection(type='s3', subtype='profile', profile_source='aws')),
                          '~/.aws may name an endpoint')
        self.assertEqual(storage_key(connection(type='s3', subtype='profile', s3_endpoint='https://s3.lab.example')),
                         's3:https://s3.lab.example', "the connection's endpoint wins over the profile's")
        self.assertEqual(storage_key(connection(type='azureblob', subtype='profile', profile_source='azure-cli',
                                                azure_account='Acct')), 'azureblob:acct')
        self.assertIsNone(storage_key(connection(type='azureblob', subtype='profile', profile_source='rclone',
                                                 azure_account='acct')))

    def test_other_types(self):
        self.assertEqual(storage_key(connection(type='azureblob', azure_account='acct')), 'azureblob:acct')
        self.assertEqual(storage_key(connection(type='azureblob', subtype='sas',
                                                azure_sas_url='https://acct.blob.core.windows.net/box?sv=1&sig=SECRET')),
                         'azureblob:https://acct.blob.core.windows.net/box')
        self.assertEqual(storage_key(connection(type='google cloud storage')), 'gcs:')
        self.assertEqual(storage_key(connection(type='sftp', sftp_host='Host.example')), 'sftp:host.example:22')
        self.assertEqual(storage_key(connection(type='sftp', sftp_host='h', sftp_port='2222')), 'sftp:h:2222')
        self.assertEqual(storage_key(connection(type='swift', swift_auth='https://auth/v1.0', swift_user='u')),
                         'swift:https://auth/v1.0|u')
        self.assertEqual(storage_key(connection(type='onedrive', onedrive_drive_id='b!x')), 'onedrive:b!x')
        self.assertEqual(storage_key(connection(type='drive', gdrive_team_drive='T')), 'drive:T|')
        self.assertEqual(storage_key(connection(type='webdav', webdav_url='https://dav.example/files/')),
                         'webdav:https://dav.example/files')
        for unknown in (connection(type='dropbox'), connection(type='drive'), connection(type='onedrive'),
                        connection(type='sftp'), connection(type='azureblob'), connection(type='new-type')):
            self.assertIsNone(storage_key(unknown), unknown.type)

    def test_sas_secret_is_not_part_of_the_key(self):
        key = storage_key(connection(type='azureblob', subtype='sas', azure_sas_url='https://a.example/c?sig=SECRET'))
        self.assertNotIn('SECRET', key)


class PathTest(unittest.TestCase):

    def test_normalized(self):
        self.assertEqual(normalized_path('/data//a/', True), '/data/a')
        self.assertEqual(normalized_path('bucket/dir/', False), '/bucket/dir')
        self.assertEqual(normalized_path('/bucket/dir', False), '/bucket/dir')
        self.assertEqual(normalized_path('/', True), '/')

    def test_refused(self):
        for path in ('/data/../etc', '/data/./a', '..', 'relative', '', None, '/a\x00b'):
            self.assertIsNone(normalized_path(path, True), path)
        self.assertIsNone(normalized_path('bucket/../other', False))

    def test_ancestors(self):
        self.assertEqual(ancestors('/a/b/c'), ['/a/b/c', '/a/b', '/a', '/'])
        self.assertEqual(ancestors('/'), ['/'])


class SettingTest(unittest.TestCase):

    def test_values(self):
        for raw, value in (('true', True), (' TRUE ', True), ('1', True), ('yes', True),
                           ('false', False), ('0', False), ('', False), ('off', False)):
            self.assertEqual(motuz_destinations.allow_general_overwrites({'MOTUZ_ALLOW_GENERAL_OVERWRITES': raw}), value)
        self.assertFalse(motuz_destinations.allow_general_overwrites({}))

    def test_bad_value_stops_the_app(self):
        with self.assertRaisesRegex(ValueError, 'MOTUZ_ALLOW_GENERAL_OVERWRITES'):
            motuz_destinations.allow_general_overwrites({'MOTUZ_ALLOW_GENERAL_OVERWRITES': 'maybe'})

    def test_default_in_config(self):
        self.assertIs(TestingConfig.ALLOW_GENERAL_OVERWRITES, False)


class FlagTest(unittest.TestCase):
    PLAIN = rclone_tuning.load_settings({'MOTUZ_RCLONE_TRANSFERS': '4', 'MOTUZ_RCLONE_CHECKERS': '32'})

    def test_copy_flags(self):
        self.assertEqual(rclone_tuning.copy_flags(None, None, self.PLAIN, overwrite=False),
                         ['--transfers=4', '--checkers=32', '--ignore-existing', '--combined=-'])
        self.assertNotIn('--ignore-existing', rclone_tuning.copy_flags(None, None, self.PLAIN))
        self.assertNotIn('--ignore-existing', rclone_tuning.copy_flags(None, None, self.PLAIN, overwrite=True))

    def test_central_command(self):
        with mock.patch.object(RcloneConnection, 'copy_with_credentials') as copy:
            RcloneConnection().copy(None, '/data/a', None, '/data/b', 'alice', False, 1, overwrite=False)
            self.assertIn('--ignore-existing', copy.call_args.kwargs['extra_flags'])
            RcloneConnection().copy(None, '/data/a', None, '/data/b', 'alice', False, 2)
            self.assertNotIn('--ignore-existing', copy.call_args.kwargs['extra_flags'])

    def test_command_line_position(self):
        with mock.patch.object(RcloneConnection, '_copy_job_queue', create=True) as queue:
            connection = RcloneConnection()
            connection._copy_job_queue = queue
            connection.copy(None, '/data/a', None, '/data/b', 'alice', False, 1, overwrite=False)
        command = queue.push.call_args.args[0]
        self.assertLess(command.index('--ignore-existing'), command.index('copyto'))


# stdout of rclone v1.76.0 `--ignore-existing --combined=- copyto src/data dst/data
# --progress --stats 2s` (non-TTY) where dst/data had x.txt and "sub dir/y: z.txt"
RCLONE_STDOUT = '''
Transferred:   \t          0 B / 0 B, -, 0 B/s, ETA -
Checks:                 0 / 0, -, Listed 5
Elapsed time:         0.0s
+ z.txt
= x.txt
Transferred:   \t          0 B / 3 B, 0%, 0 B/s, ETA -
Checks:                 1 / 2, 50%, Listed 7
Transferred:            0 / 1, 0%
Elapsed time:         0.0s
Checking:
 *                              sub dir/y: z.txt: checking
Transferring:
 *                                         z.txt:  0% / 3 B, 0 B/s, -
= sub dir/y: z.txt
= = eq
= new␊line
Transferred:   \t          3 B / 3 B, 100%, 0 B/s, ETA -
Checks:                 2 / 2, 100%, Listed 7
Transferred:            1 / 1, 100%
Elapsed time:         0.0s
'''


class KeptFilesTest(unittest.TestCase):

    def run_queue(self, stdout):
        from api.utils.copy_job_queue import CopyJobQueue
        queue = CopyJobQueue()
        queue.push([sys.executable, '-c', 'import sys; sys.stdout.write(sys.argv[1])', stdout], {}, 1)
        for _ in range(100):
            if queue.copy_finished(1):
                break
            time.sleep(0.05)
        self.assertTrue(queue.copy_finished(1))
        return queue

    def test_kept_files_from_rclone_output(self):
        queue = self.run_queue(RCLONE_STDOUT)
        self.assertEqual(queue.copy_exitstatus(1), 0)
        self.assertEqual(queue.copy_kept(1), {'count': 4, 'sample': ['x.txt', 'sub dir/y: z.txt', '= eq', 'new␊line']})
        text = queue.copy_text(1)
        self.assertIn('Kept: 4 existing files (not overwritten)', text)
        self.assertIn('Transferred: 1 / 1, 100%', text)
        self.assertNotIn('z.txt', text, 'report lines are not status lines (a name with ": " would be one)')
        self.assertEqual(queue.copy_error_text(1), '')

    def test_sample_is_bounded(self):
        from api.utils import copy_job_queue
        queue = self.run_queue(''.join('= f{}\n'.format(i) for i in range(copy_job_queue.KEPT_SAMPLE + 5)))
        kept = queue.copy_kept(1)
        self.assertEqual(kept['count'], copy_job_queue.KEPT_SAMPLE + 5)
        self.assertEqual(len(kept['sample']), copy_job_queue.KEPT_SAMPLE)

    def test_nothing_kept(self):
        queue = self.run_queue('Transferred:            1 / 1, 100%\n+ a.txt\n')
        self.assertEqual(queue.copy_kept(1), {'count': 0, 'sample': []})
        self.assertNotIn('Kept', queue.copy_text(1))

    def test_record_kept_validates(self):
        job = SimpleNamespace(skipped_existing=None, skipped_existing_sample=None)
        for count, sample in ((0, []), (-1, []), (True, []), ('3', []), (3, 'x'), (None, None)):
            overwrite_policy.record_kept(job, count, sample)
            self.assertIsNone(job.skipped_existing, (count, sample))
        overwrite_policy.record_kept(job, 300, ['a', 7, 'x' * 5000] + ['b'] * 200)
        self.assertEqual(job.skipped_existing, 300)
        self.assertEqual(len(job.skipped_existing_sample), overwrite_policy.KEPT_SAMPLE - 1)
        self.assertEqual(job.skipped_existing_sample[:2], ['a', 'x' * 4096])


class PolicyTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = Flask('overwrite-policy-test')
        self.app.config.from_object(TestingConfig)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(self.directory.name, 'test.sqlite3'),
            SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
            JWT_SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
            ALLOW_GENERAL_OVERWRITES=False,
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
        self.alice = self.headers('alice')
        self.bob = self.headers('bob')
        patcher = mock.patch.object(copy_job_manager.tasks.copy_job, 'apply_async')
        self.apply_async = patcher.start()
        self.addCleanup(patcher.stop)

    def headers(self, user):
        with self.app.test_request_context():
            return {'Authorization': 'Bearer ' + flask_jwt.create_access_token(identity=user)}

    def cloud(self, owner, **fields):
        cloud = CloudConnection(owner=owner, name='c', **fields)
        db.session.add(cloud)
        db.session.commit()
        return cloud

    def copy(self, dst_path, headers=None, dst_cloud=None, src_path='/src/data'):
        body = {'description': 'd', 'src_resource_path': src_path, 'dst_resource_path': dst_path, 'copy_links': False}
        if dst_cloud is not None:
            body['dst_cloud_id'] = dst_cloud.id
        response = self.client.post('/api/copy-jobs/', headers=headers or self.alice, json=body)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()

    def policy(self, dst_path, headers=None, dst_cloud_id=0):
        return self.client.get('/api/copy-jobs/overwrite-policy/', headers=headers or self.alice,
                               query_string={'dst_cloud_id': dst_cloud_id, 'dst_resource_path': dst_path})

    def register(self, path, cloud=None):
        overwrite_policy.register(cloud, path, 'copy', 1, 'alice')
        db.session.commit()

    def test_segment_prefix_match(self):
        self.register('/data/a')
        covered = lambda path: overwrite_policy.is_motuz_destination(None, path)
        for path in ('/data/a', '/data/a/', '/data/a/x/y.txt', '//data//a/x'):
            self.assertTrue(covered(path), path)
        for path in ('/data/ab', '/data', '/', '/data/a/../b', '/data/b/../a', 'data/a', '/other/data/a'):
            self.assertFalse(covered(path), path)

    def test_storages_are_separate(self):
        lab = self.cloud('alice', type='s3', s3_endpoint='https://s3.lab.example')
        same = self.cloud('bob', type='s3', s3_endpoint='https://s3.lab.example/')
        other = self.cloud('alice', type='s3', s3_endpoint='https://s3.other.example')
        self.register('/bucket/dir', lab)
        self.assertTrue(overwrite_policy.is_motuz_destination(same, 'bucket/dir/x'))
        self.assertFalse(overwrite_policy.is_motuz_destination(other, '/bucket/dir/x'))
        self.assertFalse(overwrite_policy.is_motuz_destination(None, '/bucket/dir/x'))

    def test_register_skips_covered_paths_and_unknowns(self):
        self.register('/data/a')
        self.register('/data/a/b')
        self.register('/data/a')
        self.register('/')
        self.register('/data/../etc')
        self.register('/x', self.cloud('alice', type='dropbox'))
        self.assertEqual([d.path for d in MotuzDestination.query], ['/data/a'])

    def test_first_copy_keeps_existing_files(self):
        job = self.copy('/dst/data')
        self.assertIs(job['overwrite'], False)
        self.assertEqual(self.apply_async.call_count, 1)
        destination = MotuzDestination.query.one()
        self.assertEqual((destination.storage_key, destination.path, destination.job_type, destination.job_id),
                         ('local:', '/dst/data', 'copy', job['id']))
        # Second copy, also by another user and to something below: overwrites
        self.assertIs(self.copy('/dst/data')['overwrite'], True)
        self.assertIs(self.copy('/dst/data/sub', headers=self.bob, src_path='/src/sub')['overwrite'], True)
        # Not the parent nor a sibling with the same prefix
        self.assertIs(self.copy('/dst/database')['overwrite'], False)
        self.assertIs(self.copy('/dst')['overwrite'], False)

    def test_failed_dispatch_still_registers(self):
        self.apply_async.side_effect = RuntimeError('broker down')
        response = self.client.post('/api/copy-jobs/', headers=self.alice, json={
            'description': 'd', 'src_resource_path': '/src/x', 'dst_resource_path': '/dst/x', 'copy_links': False})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(CopyJob.query.one().progress_state, 'FAILED')
        self.assertTrue(overwrite_policy.is_motuz_destination(None, '/dst/x'))

    def test_general_overwrites(self):
        self.app.config['ALLOW_GENERAL_OVERWRITES'] = True
        self.assertIs(self.copy('/dst/data')['overwrite'], True)
        self.assertEqual(self.policy('/anywhere').get_json()['reason'], overwrite_policy.GENERAL)

    def test_unknown_storage_never_overwrites(self):
        dropbox = self.cloud('alice', type='dropbox', dropbox_token='{}')
        self.assertIs(self.copy('/a', dst_cloud=dropbox)['overwrite'], False)
        self.assertIs(self.copy('/a', dst_cloud=dropbox)['overwrite'], False)
        self.assertEqual(MotuzDestination.query.count(), 0)

    def test_the_decision_reaches_the_command(self):
        job = db.session.get(CopyJob, self.copy('/dst/data')['id'])
        flags = job_routing.rclone_flags('copy', job) # what a remote worker's ticket gets
        self.assertIn('--ignore-existing', flags)
        again = db.session.get(CopyJob, self.copy('/dst/data')['id'])
        self.assertNotIn('--ignore-existing', job_routing.rclone_flags('copy', again))
        legacy = CopyJob(owner='alice', src_resource_path='/a', dst_resource_path='/b', overwrite=None)
        self.assertNotIn('--ignore-existing', job_routing.rclone_flags('copy', legacy), 'jobs from before the policy')
        self.assertNotIn('--ignore-existing', job_routing.rclone_flags('hashsum', job))

        from api.tasks import celery_tasks
        connection = mock.Mock()
        connection.copy_kept.return_value = {'count': 3, 'sample': ['a', 'b', 'c']}
        with mock.patch.object(celery_tasks.job_runner, 'watch_copy', return_value=0):
            celery_tasks._copy_job_run(mock.Mock(), job, connection, job.id, 0)
            self.assertIs(connection.copy.call_args.kwargs['overwrite'], False)
            self.assertEqual((job.skipped_existing, job.skipped_existing_sample), (3, ['a', 'b', 'c']))
            celery_tasks._copy_job_run(mock.Mock(), again, connection, again.id, 0)
            self.assertIs(connection.copy.call_args.kwargs['overwrite'], True)

    def test_remote_worker_ticket(self):
        self.app.config['LOCAL_JOB_POOL'] = 'onprem'
        job = self.copy('/dst/data')
        self.apply_async.assert_not_called()
        from api.managers import worker_manager
        worker, _ = worker_manager.add_worker('w1', 'onprem')
        with mock.patch.object(worker_manager, 'rclone_version', return_value='rclone v1.75.1'), \
                self.app.test_request_context(base_url='https://motuz.test'):
            ticket = worker_manager.claim(worker, {'pool': 'onprem'})
        self.assertEqual(ticket['job']['id'], job['id'])
        self.assertIn('--ignore-existing', ticket['rclone_flags'])
        self.assertIn('--combined=-', ticket['rclone_flags'])
        # The worker reports the kept files with its progress and its result
        with self.app.test_request_context(base_url='https://motuz.test'):
            worker_manager.progress(worker, ticket['ticket_id'], ticket['ticket_token'],
                                    {'percent': 50, 'kept': {'count': 'many', 'sample': None}})
            self.assertIsNone(db.session.get(CopyJob, job['id']).skipped_existing)
            worker_manager.progress(worker, ticket['ticket_id'], ticket['ticket_token'],
                                    {'percent': 50, 'kept': {'count': 1, 'sample': ['x.txt']}})
            worker_manager.finish(worker, ticket['ticket_id'], ticket['ticket_token'],
                                  {'state': 'SUCCESS', 'text': 't', 'kept': {'count': 2, 'sample': ['x.txt', 'y.txt']}})
        response = self.client.get('/api/copy-jobs/{}'.format(job['id']), headers=self.alice).get_json()
        self.assertEqual((response['skipped_existing'], response['skipped_existing_sample']), (2, ['x.txt', 'y.txt']))

    def test_policy_api(self):
        response = self.policy('/dst/data')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            'overwrite': False, 'reason': 'not_motuz_destination',
            'message': overwrite_policy.MESSAGES[overwrite_policy.NOT_MOTUZ_DESTINATION]})
        self.copy('/dst/data', headers=self.bob)
        body = self.policy('/dst/data/x').get_json()
        self.assertEqual((body['overwrite'], body['reason']), (True, 'motuz_destination'))
        self.assertNotIn('bob', str(body), 'never who wrote it')
        self.assertEqual(self.policy('').status_code, 400)
        self.assertEqual(self.client.get('/api/copy-jobs/overwrite-policy/?dst_resource_path=/a').status_code, 401)

    def test_policy_api_scope(self):
        bobs = self.cloud('bob', type='s3', s3_endpoint='https://s3.lab.example', s3_secret_access_key='BOB-SECRET')
        response = self.policy('/bucket/x', dst_cloud_id=bobs.id)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn('BOB-SECRET', response.get_data(as_text=True))
        self.assertNotIn('s3.lab.example', response.get_data(as_text=True))
        self.assertEqual(self.policy('/bucket/x', headers=self.bob, dst_cloud_id=bobs.id).status_code, 200)
        # Nor can a job use another user's connection to find out
        response = self.client.post('/api/copy-jobs/', headers=self.alice, json={
            'description': 'd', 'src_resource_path': '/a', 'dst_cloud_id': bobs.id,
            'dst_resource_path': '/bucket/x', 'copy_links': False})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(MotuzDestination.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
