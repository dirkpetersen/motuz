"""
Uploads from the browser (managers/upload_manager.py, utils/upload_pipes.py,
views/copy_job_views.py): validation of names, conflicts, skip mode, chunks in order,
stop, the browser going away and the user's scope. rclone rcat is replaced by a script
that checks the size like rcat does; against SQLite.
"""
import datetime
import io
import os
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from flask import Flask
import flask_jwt_extended as flask_jwt

from api.application import db, jwt, register_api
from api.config import TestingConfig
from api.managers import overwrite_policy
from api.models import CloudConnection, CopyJob, MotuzDestination, UploadFile
from api.utils import upload_pipes
from api.utils.rclone_connection import RcloneConnection

# Writes stdin to argv[2] if it has argv[1] bytes, like rclone rcat --size; below a
# folder "full" it fails with what rclone rcat prints when the disk is full
FAKE_RCAT = textwrap.dedent('''
    import os, sys
    size, path = int(sys.argv[1]), sys.argv[2]
    data = sys.stdin.buffer.read()
    if len(data) != size:
        sys.exit('corrupted on transfer: sizes differ')
    if '/denied/' in path:
        sys.exit('permission denied')
    if '/full/' in path:
        for line in (
            'NOTICE: {0}: Removing partially written file on error: write {0}: no space left on device',
            'ERROR : {0}: Post request put error: write {0}: no space left on device',
            'NOTICE: Failed to rcat with 2 errors: last error was: write {0}: no space left on device',
        ):
            print('2026/10/18 08:52:27 ' + line.format(path), file=sys.stderr)
        sys.exit(7)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
''')


class UploadTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dst = os.path.join(self.directory.name, 'dst')
        os.mkdir(self.dst)
        rcat = os.path.join(self.directory.name, 'rcat.py')
        with open(rcat, 'w') as f:
            f.write(FAKE_RCAT)

        def upload_command(connection, dst_data, user, path, size):
            return [sys.executable, rcat, str(size), path], dict(os.environ)

        def rename_local(connection, user, src, dst):
            os.replace(src, dst)

        def remove_local(connection, user, path):
            os.remove(path)

        for patcher in (
            mock.patch.object(RcloneConnection, 'upload_command', upload_command),
            mock.patch.object(RcloneConnection, 'rename_local', rename_local),
            mock.patch.object(RcloneConnection, 'remove_local', remove_local),
            mock.patch.object(RcloneConnection, 'existing_names', self.existing_names),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.listing = {}  # (path, recursive) -> names

        self.app = Flask('uploads-test')
        self.app.config.from_object(TestingConfig)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(self.directory.name, 'test.sqlite3'),
            SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
            JWT_SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
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

    def existing_names(self, dst_data, user, path, recursive, timeout):
        return self.listing.get((path, recursive), [])

    def headers(self, user):
        with self.app.test_request_context():
            return {'Authorization': 'Bearer ' + flask_jwt.create_access_token(identity=user)}

    def create(self, files, mode='overwrite', headers=None, folder=None):
        return self.client.post('/api/copy-jobs/uploads/', headers=headers or self.alice, json={
            'dst_cloud_id': 0, 'dst_resource_path': folder or self.dst, 'mode': mode,
            'files': [{'path': path, 'size': len(data)} for path, data in files]})

    def put(self, job_id, index, offset, data, headers=None):
        return self.client.put('/api/copy-jobs/{}/uploads/{}?offset={}'.format(job_id, index, offset),
                               headers={**(headers or self.alice), 'Content-Type': 'application/octet-stream'},
                               data=data)

    def send(self, job_id, index, data, chunk=4):
        response = None
        for offset in range(0, max(len(data), 1), chunk):
            response = self.put(job_id, index, offset, data[offset:offset + chunk])
            self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def job(self, job_id, headers=None):
        return self.client.get('/api/copy-jobs/{}'.format(job_id), headers=headers or self.alice)

    def test_upload_in_chunks(self):
        files = [('photos/a.jpg', b'0123456789'), ('photos/sub/b.txt', b''), ('c.txt', b'abc')]
        response = self.create(files)
        self.assertEqual(response.status_code, 201, response.get_json())
        body = response.get_json()
        job_id = body['job']['id']
        self.assertEqual(body['pending'], [0, 1, 2])
        self.assertEqual(body['chunk_bytes'], 8 << 20)
        self.assertEqual(body['job']['upload'], {'files': 3, 'bytes': 13, 'mode': 'overwrite', 'name': '2 items'})
        self.assertEqual(body['job']['progress_state'], 'PROGRESS')

        result = self.send(job_id, 0, files[0][1])
        self.assertEqual(result, {'file_state': 'done', 'written': 10, 'error': None, 'job_state': 'PROGRESS'})
        self.assertEqual(self.send(job_id, 1, b'')['file_state'], 'done')
        self.assertEqual(self.send(job_id, 2, b'abc')['job_state'], 'SUCCESS')
        for path, data in files:
            with open(os.path.join(self.dst, path), 'rb') as f:
                self.assertEqual(f.read(), data)
        temporary = [name for _, _, names in os.walk(self.dst) for name in names if name.startswith('.motuz-upload')]
        self.assertEqual(temporary, [], 'renamed after rcat')

        job = self.job(job_id).get_json()
        self.assertEqual(job['progress_state'], 'SUCCESS')
        self.assertEqual(job['progress_current'], 100)
        self.assertEqual(job['src_resource_path'], '2 items')
        self.assertIsNone(job['src_cloud_id'])
        self.assertIn('Files:     3 of 3 done, 0 skipped, 0 failed', job['progress_text'])
        self.assertIn('your browser (overwrite)', job['progress_text'])
        self.assertEqual(job['progress_error_text'], '')
        # The job is in the user's list like any copy job
        listed = self.client.get('/api/copy-jobs/', headers=self.alice).get_json()['data']
        self.assertEqual([j['id'] for j in listed], [job_id])

    def test_one_folder_is_named_as_the_source(self):
        body = self.create([('photos/a.jpg', b'x'), ('photos/b.jpg', b'y')]).get_json()
        self.assertEqual(body['job']['src_resource_path'], 'photos/')

    def test_chunks_must_be_in_order(self):
        job_id = self.create([('a.bin', b'0123456789')]).get_json()['job']['id']
        self.assertEqual(self.put(job_id, 0, 0, b'0123').status_code, 200)
        response = self.put(job_id, 0, 8, b'89')  # skipped 4..8
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['written'], 4, 'where to go on')
        self.assertEqual(self.put(job_id, 0, 4, b'456789').get_json()['file_state'], 'done')
        # A chunk without a started file: start again at 0
        job_id = self.create([('b.bin', b'0123456789')]).get_json()['job']['id']
        response = self.put(job_id, 0, 4, b'4567')
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(response.get_json()['written'])
        # Past the end
        self.assertEqual(self.put(job_id, 0, 0, b'0123456789!').status_code, 409)

    def test_a_restarted_file_is_written_from_the_start(self):
        job_id = self.create([('a.bin', b'0123456789')]).get_json()['job']['id']
        self.assertEqual(self.put(job_id, 0, 0, b'xxxx').status_code, 200)
        self.send(job_id, 0, b'0123456789')
        with open(os.path.join(self.dst, 'a.bin'), 'rb') as f:
            self.assertEqual(f.read(), b'0123456789')

    def test_failed_file_fails_the_job_with_rclones_error(self):
        job_id = self.create([('ok.txt', b'ok'), ('denied/no.txt', b'no')]).get_json()['job']['id']
        self.send(job_id, 0, b'ok')
        result = self.send(job_id, 1, b'no')
        self.assertEqual(result['file_state'], 'failed')
        self.assertIn('permission denied', result['error'])
        self.assertEqual(result['job_state'], 'FAILED')
        job = self.job(job_id).get_json()
        self.assertEqual(job['progress_error'], '1 of 2 files could not be uploaded')
        self.assertIn('denied/no.txt: permission denied', job['progress_error_text'])
        # A finished file takes no more chunks
        self.assertEqual(self.put(job_id, 0, 0, b'ok').status_code, 409)

    def test_a_full_disk_fails_the_whole_upload_at_once(self):
        files = [('ok.txt', b'ok'), ('full/a.bin', b'0123456789'), ('full/b.bin', b'0123456789'), ('c.txt', b'c')]
        job_id = self.create(files).get_json()['job']['id']
        self.send(job_id, 0, b'ok')
        self.assertEqual(self.put(job_id, 2, 0, b'0123').status_code, 200) # b.bin is being sent
        result = self.send(job_id, 1, b'0123456789')
        self.assertEqual(result['file_state'], 'failed')
        self.assertEqual(result['job_state'], 'FAILED')
        self.assertIsNone(upload_pipes.written((job_id, 2)), "the job's other files are aborted")
        # The browser's next chunks are refused, the files that are left are not started
        for index, offset, data in ((2, 4, b'456789'), (3, 0, b'c')):
            response = self.put(job_id, index, offset, data)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()['job_state'], 'FAILED')
        job = self.job(job_id).get_json()
        self.assertTrue(job['progress_error'].startswith(
            'The destination filesystem is full (no space left on device) at {}: 1 file was uploaded before. '
            'Free space or choose another destination.\nrclone: NOTICE: Failed to rcat with 2 errors: '.format(self.dst)),
            job['progress_error'])
        self.assertTrue(job['progress_error_text'].startswith(job['progress_error']), 'shown first in the job')
        self.assertIn('full/a.bin: ', job['progress_error_text'])
        self.assertFalse(os.path.exists(os.path.join(self.dst, 'full', 'b.bin')))

    def test_conflicts(self):
        self.listing[(self.dst, False)] = ['photos/', 'c.txt', 'other']
        response = self.client.post('/api/copy-jobs/uploads/conflicts/', headers=self.alice, json={
            'dst_cloud_id': 0, 'dst_resource_path': self.dst, 'names': ['photos', 'c.txt', 'new.txt']})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json(), {
            'existing': ['c.txt', 'photos'], 'overwrite': False,
            'overwrite_message': overwrite_policy.MESSAGES[overwrite_policy.NOT_MOTUZ_DESTINATION]})
        response = self.client.post('/api/copy-jobs/uploads/conflicts/', headers=self.alice, json={
            'dst_cloud_id': 0, 'dst_resource_path': self.dst, 'names': ['a/b']})
        self.assertEqual(response.status_code, 400)

    def conflicts(self, names, headers=None):
        response = self.client.post('/api/copy-jobs/uploads/conflicts/', headers=headers or self.alice, json={
            'dst_cloud_id': 0, 'dst_resource_path': self.dst, 'names': names})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_overwrite_only_what_motuz_wrote(self):
        self.listing[(self.dst, False)] = ['c.txt']
        response = self.create([('c.txt', b'new')])
        self.assertEqual(response.status_code, 403)
        self.assertIn('Skip existing', response.get_json()['message'])
        self.assertEqual(CopyJob.query.count(), 0)
        # New names only: nothing to overwrite
        self.assertEqual(self.create([('d.txt', b'd'), ('photos/a.jpg', b'a')]).status_code, 201)
        self.assertEqual(sorted(d.path for d in MotuzDestination.query),
                         [os.path.join(self.dst, 'd.txt'), os.path.join(self.dst, 'photos')])
        # A skipped upload registers its items too: c.txt is written by Motuz from then on
        skipped = self.create([('c.txt', b'new')], mode='skip').get_json()['job']
        self.assertEqual((skipped['skipped_existing'], skipped['skipped_existing_sample']), (1, ['c.txt']))
        self.assertEqual(self.conflicts(['c.txt'])['overwrite'], True)
        self.listing[(self.dst, False)] = ['c.txt', 'photos/']
        bob = self.headers('bob') # another user may overwrite them too (the OS decides)
        self.assertEqual(self.conflicts(['photos', 'c.txt'], bob)['overwrite'], True)
        self.assertEqual(self.create([('photos/a.jpg', b'b'), ('c.txt', b'b')], headers=bob).status_code, 201)
        destination = MotuzDestination.query.filter_by(path=os.path.join(self.dst, 'photos')).one()
        self.assertEqual((destination.storage_key, destination.job_type, destination.owner), ('local:', 'upload', 'alice'))

    def test_general_overwrites(self):
        self.app.config['ALLOW_GENERAL_OVERWRITES'] = True
        self.addCleanup(self.app.config.update, ALLOW_GENERAL_OVERWRITES=False)
        self.listing[(self.dst, False)] = ['c.txt']
        self.assertEqual(self.conflicts(['c.txt']), {'existing': ['c.txt'], 'overwrite': True, 'overwrite_message': None})
        self.assertEqual(self.create([('c.txt', b'new')]).status_code, 201)

    def test_skip_mode_uploads_only_new_files(self):
        self.listing[(self.dst, False)] = ['photos/', 'c.txt']
        self.listing[(os.path.join(self.dst, 'photos'), True)] = ['a.jpg', 'sub/old.txt']
        files = [('photos/a.jpg', b'aa'), ('photos/new.jpg', b'nn'), ('photos/sub/old.txt', b'oo'), ('c.txt', b'c')]
        body = self.create(files, mode='skip').get_json()
        self.assertEqual(body['pending'], [1])
        job_id = body['job']['id']
        self.assertEqual(self.put(job_id, 0, 0, b'aa').status_code, 409, 'a skipped file takes no chunks')
        self.assertEqual(self.send(job_id, 1, b'nn')['job_state'], 'SUCCESS')
        self.assertIn('1 of 1 done, 3 skipped', self.job(job_id).get_json()['progress_text'])
        self.assertFalse(os.path.exists(os.path.join(self.dst, 'c.txt')))

    def test_everything_skipped_is_done_at_once(self):
        self.listing[(self.dst, False)] = ['c.txt']
        body = self.create([('c.txt', b'c')], mode='skip').get_json()
        self.assertEqual(body['pending'], [])
        self.assertEqual(body['job']['progress_state'], 'SUCCESS')

    def test_validation(self):
        for files in ([('../x', b'')], [('/abs', b'')], [('a//b', b'')], [('a/./b', b'')], [('a\x01', b'')],
                      [('a', b''), ('a', b'')], [('a', b''), ('a/b', b'')], [('x' * 256, b'')], []):
            self.assertEqual(self.create(files).status_code, 400, files)
        for size in (-1, 'x', True, 1.5):
            response = self.client.post('/api/copy-jobs/uploads/', headers=self.alice, json={
                'dst_cloud_id': 0, 'dst_resource_path': self.dst, 'mode': 'overwrite',
                'files': [{'path': 'a', 'size': size}]})
            self.assertEqual(response.status_code, 400, size)
        self.assertEqual(self.create([('a', b'')], mode='merge').status_code, 400)
        self.assertEqual(self.create([('a', b'')], folder='relative/dir').status_code, 400)
        job_id = self.create([('a', b'abc')]).get_json()['job']['id']
        self.assertEqual(self.put(job_id, 0, 'x', b'abc').status_code, 400)
        self.assertEqual(self.client.put('/api/copy-jobs/{}/uploads/0?offset=0'.format(job_id), headers=self.alice,
                                         data=b'').status_code, 200, 'no body is a chunk of 0 bytes')
        self.assertEqual(self.put(job_id, 7, 0, b'abc').status_code, 404)

    def test_other_users_get_404(self):
        job_id = self.create([('a', b'abc')]).get_json()['job']['id']
        bob = self.headers('bob')
        self.assertEqual(self.put(job_id, 0, 0, b'abc', headers=bob).status_code, 404)
        self.assertEqual(self.job(job_id, headers=bob).status_code, 404)
        self.assertEqual(self.client.put('/api/copy-jobs/{}/stop/'.format(job_id), headers=bob).status_code, 404)
        self.assertEqual(self.client.put('/api/copy-jobs/{}/uploads/0?offset=0'.format(job_id),
                                         data=b'abc').status_code, 401)

    def test_stop(self):
        job_id = self.create([('a.bin', b'0123456789')]).get_json()['job']['id']
        self.assertEqual(self.put(job_id, 0, 0, b'0123').status_code, 200)
        self.assertEqual(upload_pipes.written((job_id, 0)), 4)
        response = self.client.put('/api/copy-jobs/{}/stop/'.format(job_id), headers=self.alice)
        self.assertEqual(response.get_json()['progress_state'], 'STOPPED')
        self.assertIsNone(upload_pipes.written((job_id, 0)), 'rcat was aborted')
        response = self.put(job_id, 0, 4, b'456789')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['job_state'], 'STOPPED')
        self.assertFalse(os.path.exists(os.path.join(self.dst, 'a.bin')), 'rcat got too few bytes')

    def test_a_stopped_upload_keeps_the_file_it_replaces(self):
        with open(os.path.join(self.dst, 'a.bin'), 'wb') as f:
            f.write(b'old')
        job_id = self.create([('a.bin', b'0123456789')]).get_json()['job']['id']
        self.assertEqual(self.put(job_id, 0, 0, b'0123').status_code, 200)
        self.client.put('/api/copy-jobs/{}/stop/'.format(job_id), headers=self.alice)
        with open(os.path.join(self.dst, 'a.bin'), 'rb') as f:
            self.assertEqual(f.read(), b'old')
        self.assertEqual(os.listdir(self.dst), ['a.bin'], 'no temporary file left')

    def test_cloud_uploads_are_stopped_with_a_signal(self):
        # rcat writes the final name; on SIGTERM rclone aborts, end of file would complete it
        connection = CloudConnection(owner='alice', type='s3', name='s3')
        db.session.add(connection)
        db.session.commit()
        with open(os.path.join(self.dst, 'a.bin'), 'wb') as f:
            f.write(b'old')
        response = self.client.post('/api/copy-jobs/uploads/', headers=self.alice, json={
            'dst_cloud_id': connection.id, 'dst_resource_path': self.dst, 'mode': 'overwrite',
            'files': [{'path': 'a.bin', 'size': 10}, {'path': 'b.bin', 'size': 2}]})
        job_id = response.get_json()['job']['id']
        self.assertEqual(self.send(job_id, 1, b'bb')['file_state'], 'done')
        self.assertTrue(os.path.exists(os.path.join(self.dst, 'b.bin')), 'written under its own name')
        self.assertEqual(self.put(job_id, 0, 0, b'0123').status_code, 200)
        self.client.put('/api/copy-jobs/{}/stop/'.format(job_id), headers=self.alice)
        with open(os.path.join(self.dst, 'a.bin'), 'rb') as f:
            self.assertEqual(f.read(), b'old')
        bob = self.headers('bob')
        response = self.client.post('/api/copy-jobs/uploads/', headers=bob, json={
            'dst_cloud_id': connection.id, 'dst_resource_path': self.dst, 'mode': 'overwrite',
            'files': [{'path': 'c.bin', 'size': 1}]})
        self.assertEqual(response.status_code, 404, "bob cannot upload with alice's connection")

    def test_the_browser_going_away_fails_the_job(self):
        job_id = self.create([('a.bin', b'0123456789'), ('b.bin', b'b')]).get_json()['job']['id']
        self.send(job_id, 1, b'b')
        self.assertEqual(self.put(job_id, 0, 0, b'0123').status_code, 200)
        db.session.query(CopyJob).filter_by(id=job_id).update(
            {'updated_at': datetime.datetime.utcnow() - datetime.timedelta(seconds=upload_pipes.IDLE_SECONDS + 1)})
        db.session.commit()
        jobs = self.client.get('/api/copy-jobs/', headers=self.alice).get_json()['data']
        self.assertEqual(jobs[0]['progress_state'], 'FAILED')
        self.assertEqual(jobs[0]['progress_error'],
                         'The browser stopped sending the upload (the tab was closed or the connection was lost). '
                         '1 of 2 files were uploaded.')
        self.assertIsNone(upload_pipes.written((job_id, 0)))

    def test_an_old_pipe_cannot_finish_a_restarted_file(self):
        # A request still writing when the browser starts the file again
        key, command = ('test', 1), [sys.executable, '-c', 'import sys; sys.stdin.buffer.read()']
        upload_pipes.start(key, command, dict(os.environ), 3, by_signal=False)
        old = upload_pipes.write(key, 0, io.BytesIO(b'ab'), 2)
        upload_pipes.start(key, command, dict(os.environ), 3, by_signal=False)
        self.assertIsNone(upload_pipes.finish(key, old)[0])
        self.assertEqual(upload_pipes.written(key), 0, 'the new pipe is untouched')
        upload_pipes.abort(key)

    def test_idle_pipes_are_aborted(self):
        job_id = self.create([('a.bin', b'0123456789')]).get_json()['job']['id']
        self.assertEqual(self.put(job_id, 0, 0, b'0123').status_code, 200)
        upload_pipes.reap(now=upload_pipes.time.monotonic() + upload_pipes.IDLE_SECONDS + 1)
        self.assertIsNone(upload_pipes.written((job_id, 0)))
        self.assertEqual(self.put(job_id, 0, 4, b'456789').status_code, 409)

    def test_rows_are_removed_with_the_job(self):
        job_id = self.create([('a', b'abc'), ('b', b'')]).get_json()['job']['id']
        db.session.delete(db.session.get(CopyJob, job_id))
        db.session.commit()
        self.assertEqual(UploadFile.query.count(), 0)


class UploadCommandTest(unittest.TestCase):
    """The rclone command of one file: as the user, from stdin, never an option"""

    def test_local(self):
        command, env = RcloneConnection().upload_command(None, 'alice', '/home/alice/a.txt', 12)
        from api.utils.rclone_connection import RCLONE
        self.assertEqual(command[:2], ['sudo', '-n'])
        self.assertIn(RCLONE, command)
        self.assertEqual(command[command.index('-u') + 1], 'alice')
        self.assertEqual(command[-3:], ['rcat', '--size=12', '/home/alice/a.txt'])
        self.assertNotIn('MOTUZ_FLASK_SECRET_KEY', env)

    def test_relative_local_paths_are_refused(self):
        from api.utils.abstract_connection import RcloneException
        with self.assertRaises(RcloneException):
            RcloneConnection().upload_command(None, 'alice', '--config=x', 1)


if __name__ == '__main__':
    unittest.main()
