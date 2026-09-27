"""
Remote workers (managers/worker_manager.py, views/worker_views.py, managers/
job_routing.py): worker authentication and its separation from user tokens, bootstrap
tokens, atomic claims, pool isolation, ticket scope, leases, the job-scoped HTTPS token
broker and rate limits. Against SQLite; the e2e suite `worker` runs the same against
PostgreSQL with a real worker behind a proxy.
"""
import datetime
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from flask import Flask
import flask_jwt_extended as flask_jwt
import jwt as pyjwt

from api.application import db, jwt
from api.config import TestingConfig
from api.exceptions import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN, HTTP_410_GONE
from api.managers import auth_manager, job_routing, worker_manager
from api.models import CloudConnection, CopyJob, HashsumJob, RemoteJob, Worker
from api.models.worker import utcnow
from api.utils import job_runner
from api.utils.rate_limit import RateLimiter
from api.views import worker_views


BASE_URL = 'https://motuz.test'
REAL_REFRESH = 'REAL-REFRESH-TOKEN'


def make_app(database_uri):
    app = Flask('workers-test')
    app.config.from_object(TestingConfig)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=database_uri,
        SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
        JWT_SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
        WORKER_LEASE_SECONDS=60,
        LOCAL_JOB_POOL='onprem',
        LARGE_JOB_POOL='central',
        PUBLIC_URL=None,
    )
    db.init_app(app)
    jwt.init_app(app)
    app.register_blueprint(worker_views.bp)
    return app


class WorkerTestBase(unittest.TestCase):
    database_uri = 'sqlite://'

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.WARNING)
        cls.app = make_app(cls.database_uri)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def setUp(self):
        self.context = self.app.test_request_context(base_url=BASE_URL)
        self.context.push()
        db.create_all()
        worker_views.AUTH_LIMIT.reset()
        worker_views.CLAIM_LIMIT.reset()
        worker_views.BROKER_LIMIT.reset()
        worker_manager._last_sweep[0] = 0.0
        patcher = mock.patch.object(worker_manager, 'rclone_version', return_value='rclone v1.75.1')
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    # ------------------------------------------------------------ fixtures
    def worker(self, name='w1', pool='onprem'):
        worker, secret = worker_manager.add_worker(name, pool)
        return worker, secret

    def token(self, worker):
        return worker_manager.issue_access_token(worker)[0]

    def connection(self, owner, **fields):
        connection = CloudConnection(owner=owner, name='c', **fields)
        db.session.add(connection)
        db.session.commit()
        return connection

    def copy_job(self, owner='alice', src=None, dst=None, pool='onprem', src_path='/home/alice/src', dst_path='/home/alice/dst'):
        job = CopyJob(owner=owner, description='x', src_cloud_id=src.id if src else None, src_resource_path=src_path,
                      dst_cloud_id=dst.id if dst else None, dst_resource_path=dst_path, copy_links=True,
                      progress_state='PROGRESS', progress_current=0, progress_total=100)
        db.session.add(job)
        db.session.commit()
        worker_manager.queue_job('copy', job, pool)
        return job

    def claim(self, worker, pool=None, **body):
        return worker_manager.claim(worker, {'pool': pool or worker.pool, **body})


class TestWorkerAuthentication(WorkerTestBase):

    def test_sign_in_with_secret(self):
        worker, secret = self.worker()
        answer = worker_manager.sign_in({'secret': secret, 'version': '0.3.0'})
        self.assertEqual(answer['worker']['name'], 'w1')
        self.assertEqual(answer['server']['protocol'], worker_manager.WORKER_PROTOCOL)
        self.assertEqual(worker_manager.authenticate('Bearer ' + answer['access_token']).id, worker.id)
        self.assertNotIn(secret, json.dumps(answer))
        stored = db.session.get(Worker, worker.id)
        self.assertNotEqual(stored.secret_hash, secret) # only the hash is stored
        self.assertIsNotNone(stored.last_seen_at)

    def test_bad_secrets_are_refused(self):
        worker, secret = self.worker()
        for bad in (secret[:-1] + ('A' if secret[-1] != 'A' else 'B'), 'mzw1.999.x', 'garbage', '',
                    secret.replace('mzw1.{}.'.format(worker.id), 'mzw1.{}.'.format(worker.id + 1))):
            with self.assertRaises((HTTP_401_UNAUTHORIZED, Exception)):
                worker_manager.sign_in({'secret': bad})

    def test_secret_comparison_is_constant_time(self):
        worker, secret = self.worker()
        with mock.patch.object(worker_manager.hmac, 'compare_digest', wraps=worker_manager.hmac.compare_digest) as compare:
            worker_manager.sign_in({'secret': secret})
            with self.assertRaises(HTTP_401_UNAUTHORIZED):
                worker_manager.sign_in({'secret': secret + 'x'})
        self.assertGreaterEqual(compare.call_count, 2)

    def test_revoked_worker_is_refused(self):
        worker, secret = self.worker()
        token = self.token(worker)
        worker_manager.revoke_worker('w1')
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            worker_manager.sign_in({'secret': secret})
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            worker_manager.authenticate('Bearer ' + token)

    def test_expired_worker_token_is_refused(self):
        worker, _ = self.worker()
        with mock.patch.object(worker_manager.time, 'time', return_value=time.time() - 3600):
            token = self.token(worker)
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            worker_manager.authenticate('Bearer ' + token)

    def test_user_tokens_are_not_worker_tokens(self):
        self.worker()
        user_token = flask_jwt.create_access_token(identity='alice')
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            worker_manager.authenticate('Bearer ' + user_token)
        # Even a user-token-shaped JWT for worker 1 signed with the users' key
        forged = pyjwt.encode({'aud': 'motuz-worker', 'iss': 'motuz', 'typ': 'worker', 'sub': '1', 'pool': 'onprem',
                               'iat': int(time.time()), 'exp': int(time.time()) + 60},
                              self.app.config['JWT_SECRET_KEY'], algorithm='HS256')
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            worker_manager.authenticate('Bearer ' + forged)

    def test_worker_tokens_are_not_user_tokens(self):
        worker, _ = self.worker()
        token = self.token(worker)
        with self.app.test_request_context(headers={'Authorization': 'Bearer ' + token}):
            with self.assertRaises(HTTP_401_UNAUTHORIZED):
                auth_manager.get_logged_in_user()

    def test_worker_token_claims_are_checked(self):
        worker, _ = self.worker()
        key = worker_manager._signing_key()
        now = int(time.time())
        base = {'aud': 'motuz-worker', 'iss': 'motuz', 'typ': 'worker', 'sub': str(worker.id), 'pool': 'onprem',
                'iat': now, 'exp': now + 60}
        self.assertEqual(worker_manager.authenticate('Bearer ' + pyjwt.encode(base, key, algorithm='HS256')).id, worker.id)
        for change in ({'aud': 'other'}, {'typ': 'access'}, {'pool': 'aws'}, {'sub': 'x'}):
            with self.assertRaises(HTTP_401_UNAUTHORIZED):
                worker_manager.authenticate('Bearer ' + pyjwt.encode({**base, **change}, key, algorithm='HS256'))
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            worker_manager.authenticate('Bearer ' + pyjwt.encode(base, key, algorithm='HS512'))

    def test_http_separation(self):
        client = self.app.test_client()
        user_token = flask_jwt.create_access_token(identity='alice')
        response = client.post('/api/workers/claim', json={'pool': 'onprem'},
                               headers={'Authorization': 'Bearer ' + user_token}, base_url=BASE_URL)
        self.assertEqual(response.status_code, 401)
        response = client.post('/api/workers/claim', json={'pool': 'onprem'}, base_url=BASE_URL)
        self.assertEqual(response.status_code, 401)


class TestBootstrapTokens(WorkerTestBase):

    def test_single_use_and_bound_to_its_job(self):
        job = self.copy_job(pool='aws')
        other = self.copy_job(pool='aws')
        token = worker_manager.create_bootstrap_token('aws', job='copy:{}'.format(job.id), ttl_seconds=900)
        answer = worker_manager.sign_in({'bootstrap_token': token})
        self.assertTrue(answer['worker']['ephemeral'])
        self.assertEqual(answer['worker']['bound_job'], 'copy:{}'.format(job.id))
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            worker_manager.sign_in({'bootstrap_token': token}) # single use
        worker = worker_manager.authenticate('Bearer ' + answer['access_token'])
        ticket = self.claim(worker)
        self.assertEqual(ticket['job']['id'], job.id)
        self.assertIsNone(self.claim(worker)) # never the other job
        self.assertEqual(db.session.get(RemoteJob, other.id).state, 'QUEUED')
        # Its job ends: the ephemeral worker is revoked
        worker_manager.finish(worker, ticket['ticket_id'], ticket['ticket_token'], {'state': 'SUCCESS'})
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            worker_manager.authenticate('Bearer ' + answer['access_token'])

    def test_expired_token_is_refused(self):
        token = worker_manager.create_bootstrap_token('aws', ttl_seconds=60)
        later = utcnow() + datetime.timedelta(seconds=61)
        with mock.patch.object(worker_manager, 'utcnow', return_value=later):
            with self.assertRaises(HTTP_401_UNAUTHORIZED):
                worker_manager.sign_in({'bootstrap_token': token})

    def test_job_must_be_queued_for_the_pool(self):
        job = self.copy_job(pool='onprem')
        with self.assertRaises(ValueError):
            worker_manager.create_bootstrap_token('aws', job=str(job.id))
        with self.assertRaises(ValueError):
            worker_manager.create_bootstrap_token('central')


class TestClaims(WorkerTestBase):

    def test_pool_isolation(self):
        job = self.copy_job(pool='onprem')
        aws, _ = self.worker('a1', 'aws')
        with self.assertRaises(HTTP_403_FORBIDDEN):
            self.claim(aws, pool='onprem')
        self.assertIsNone(self.claim(aws, pool='aws'))
        onprem, _ = self.worker('o1', 'onprem')
        with self.assertRaises(HTTP_403_FORBIDDEN):
            self.claim(onprem, pool='aws')
        self.assertEqual(self.claim(onprem)['job']['id'], job.id)

    def test_a_job_is_claimed_once(self):
        self.copy_job()
        w1, _ = self.worker('w1')
        w2, _ = self.worker('w2')
        self.assertIsNotNone(self.claim(w1))
        self.assertIsNone(self.claim(w2))
        self.assertIsNone(self.claim(w1))

    def test_compare_and_set_loses_a_race(self):
        # Another claimer takes the job between this claimer's SELECT and its UPDATE
        job = self.copy_job()
        worker, _ = self.worker()
        real = worker_manager._new_secret

        def race(*args):
            db.session.execute(RemoteJob.__table__.update().values(state='RUNNING'))
            return real(*args)
        with mock.patch.object(worker_manager, '_new_secret', side_effect=race):
            self.assertIsNone(worker_manager._try_claim(worker, ['copy', 'hashsum']))

    def test_job_types(self):
        self.copy_job()
        worker, _ = self.worker()
        self.assertIsNone(self.claim(worker, capabilities={'job_types': ['hashsum']}))
        self.assertIsNotNone(self.claim(worker, capabilities={'job_types': ['copy']}))

    def test_stopped_before_claim(self):
        job = self.copy_job()
        worker_manager.request_stop('copy', job.id)
        worker, _ = self.worker()
        self.assertIsNone(self.claim(worker))


class TestConcurrentClaims(WorkerTestBase):
    """Threads claiming one job at the same time (a SQLite file: separate connections)"""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.database_uri = 'sqlite:///' + os.path.join(cls.directory.name, 'claims.sqlite3')
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.directory.cleanup()

    def test_exactly_one_winner(self):
        self.copy_job()
        workers = [self.worker('w{}'.format(i))[0].id for i in range(8)]
        results = []
        barrier = threading.Barrier(len(workers))

        def run(worker_id):
            with self.app.test_request_context(base_url=BASE_URL):
                worker = db.session.get(Worker, worker_id)
                barrier.wait()
                try:
                    results.append(self.claim(worker))
                except Exception as e: # e.g. "database is locked": not a claim either
                    results.append(e)
                finally:
                    db.session.remove()

        threads = [threading.Thread(target=run, args=(w,)) for w in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        tickets = [r for r in results if isinstance(r, dict)]
        self.assertEqual(len(tickets), 1, results)
        self.assertEqual(db.session.query(RemoteJob).one().state, 'RUNNING')


class TestTicketScope(WorkerTestBase):

    def test_only_the_jobs_own_connections(self):
        s3 = self.connection('alice', type='s3', s3_access_key_id='AKIAALICE', s3_secret_access_key='alice-secret', s3_region='us-west-2')
        self.connection('alice', type='s3', s3_access_key_id='AKIAOTHER', s3_secret_access_key='other-secret')
        self.connection('bob', type='s3', s3_access_key_id='AKIABOB', s3_secret_access_key='bob-secret')
        job = self.copy_job(src=s3, src_path='bucket/data')
        worker, _ = self.worker()
        ticket = self.claim(worker)
        text = json.dumps(ticket)
        self.assertIn('alice-secret', text)
        for foreign in ('other-secret', 'bob-secret', 'AKIAOTHER', 'AKIABOB'):
            self.assertNotIn(foreign, text)
        self.assertEqual(ticket['job']['src']['rclone_env']['RCLONE_CONFIG_SRC_ACCESS_KEY_ID'], 'AKIAALICE')
        self.assertTrue(all(key.startswith('RCLONE_CONFIG_SRC_') for key in ticket['job']['src']['rclone_env']))
        self.assertEqual(ticket['job']['dst'], {'path': '/home/alice/dst', 'local': True})
        self.assertEqual(set(ticket['job']), {'type', 'id', 'owner', 'options', 'src', 'dst'})
        self.assertEqual(ticket['job']['owner'], 'alice')
        self.assertTrue(ticket['ticket_token'].startswith('mzt1.{}.'.format(ticket['ticket_id'])))
        remote_job = db.session.get(RemoteJob, ticket['ticket_id'])
        self.assertNotIn(ticket['ticket_token'], (remote_job.ticket_hash, remote_job.broker_hash))

    def test_oauth_connections_get_the_job_scoped_https_broker(self):
        token = {'access_token': 'ACCESS', 'refresh_token': REAL_REFRESH, 'expiry': '2020-01-01T00:00:00Z'}
        onedrive = self.connection('alice', type='onedrive', onedrive_token=json.dumps(token),
                                   onedrive_drive_id='b!x', onedrive_drive_type='business', token_broker_handle='LOOPBACK-HANDLE')
        self.copy_job(src=onedrive, src_path='/Documents')
        worker, _ = self.worker()
        ticket = self.claim(worker)
        env = ticket['job']['src']['rclone_env']
        rclone_token = json.loads(env['RCLONE_CONFIG_SRC_TOKEN'])
        self.assertTrue(rclone_token['refresh_token'].startswith('mzr1.{}.src.'.format(ticket['ticket_id'])))
        self.assertEqual(env['RCLONE_CONFIG_SRC_TOKEN_URL'], BASE_URL + '/api/workers/oauth/token')
        text = json.dumps(ticket)
        self.assertNotIn(REAL_REFRESH, text)
        self.assertNotIn('LOOPBACK-HANDLE', text)

    def test_performance_flags_are_computed_centrally(self):
        job = self.copy_job()
        job.performance = {'transfers': 7}
        db.session.commit()
        worker, _ = self.worker()
        tuning = worker_manager.job_routing.rclone_tuning.load_settings({'MOTUZ_RCLONE_CHECKERS': '16'})
        with mock.patch.object(worker_manager.job_routing.rclone_tuning, 'settings', return_value=tuning):
            ticket = self.claim(worker)
        self.assertEqual(ticket['rclone_flags'], ['--transfers=7', '--checkers=16'])

    def test_profile_connections_are_resolved_by_the_worker(self):
        profile = self.connection('alice', type='s3', subtype='profile', profile_source='aws', profile_name='lab', s3_region='eu-west-1')
        self.copy_job(src=profile, src_path='bucket')
        worker, _ = self.worker()
        with mock.patch('api.utils.local_credentials.resolve') as resolve:
            ticket = self.claim(worker)
        resolve.assert_not_called() # never read on the central node for a remote job
        self.assertEqual(ticket['job']['src']['profile_connection']['profile_name'], 'lab')
        self.assertNotIn('rclone_env', ticket['job']['src'])

    def test_hashsum_reads_both_sides_as_remote_src(self):
        s3 = self.connection('alice', type='s3', s3_access_key_id='AKIAALICE', s3_secret_access_key='x')
        job = HashsumJob(owner='alice', src_cloud_id=None, src_resource_path='/home/alice/a', dst_cloud_id=s3.id,
                         dst_resource_path='bucket/a', option_download=True, progress_state='PROGRESS')
        db.session.add(job)
        db.session.commit()
        worker_manager.queue_job('hashsum', job, 'onprem')
        worker, _ = self.worker()
        ticket = self.claim(worker)
        self.assertEqual(ticket['job']['type'], 'hashsum')
        self.assertEqual(ticket['job']['dst']['remote'], 'src')
        self.assertIn('RCLONE_CONFIG_SRC_ACCESS_KEY_ID', ticket['job']['dst']['rclone_env'])
        self.assertEqual(ticket['job']['options'], {'download': True})


class TestTicketCalls(WorkerTestBase):

    def setUp(self):
        super().setUp()
        self.job1 = self.copy_job()
        self.job2 = self.copy_job()
        self.w1, _ = self.worker('w1')
        self.w2, _ = self.worker('w2')
        self.t1 = self.claim(self.w1)
        self.t2 = self.claim(self.w2)

    def progress(self, worker, ticket, token=None, **body):
        return worker_manager.progress(worker, ticket['ticket_id'], token or ticket['ticket_token'], body)

    def test_progress_renews_the_lease_and_shows_in_the_job(self):
        answer = self.progress(self.w1, self.t1, percent=42, text='Transferred: 1 / 2, 50%', error_text='')
        self.assertEqual(answer['action'], 'continue')
        job = db.session.get(CopyJob, self.t1['job']['id'])
        self.assertEqual(job.progress_current, 42)
        self.assertTrue(worker_manager.apply_remote_progress('copy', job))
        self.assertEqual(job.progress_text, 'Transferred: 1 / 2, 50%')

    def test_another_workers_or_jobs_ticket_is_refused(self):
        with self.assertRaises(HTTP_403_FORBIDDEN): # the other worker's ticket
            self.progress(self.w1, self.t2)
        with self.assertRaises(HTTP_403_FORBIDDEN): # own token, other job id
            worker_manager.progress(self.w1, self.t2['ticket_id'], self.t1['ticket_token'], {})
        with self.assertRaises(HTTP_403_FORBIDDEN): # other job's token, own job id
            worker_manager.progress(self.w1, self.t1['ticket_id'], self.t2['ticket_token'], {})
        with self.assertRaises(HTTP_403_FORBIDDEN):
            self.progress(self.w1, self.t1, token=self.t1['ticket_token'][:-2] + 'xx')
        self.assertEqual(self.progress(self.w1, self.t1)['action'], 'continue')

    def test_stop(self):
        worker_manager.request_stop('copy', self.t1['job']['id'])
        self.assertEqual(self.progress(self.w1, self.t1)['action'], 'stop')
        answer = worker_manager.finish(self.w1, self.t1['ticket_id'], self.t1['ticket_token'], {'state': 'FAILED', 'exit_status': 143})
        self.assertEqual(answer['state'], 'STOPPED')

    def test_ticket_is_invalid_after_finish(self):
        worker_manager.finish(self.w1, self.t1['ticket_id'], self.t1['ticket_token'],
                              {'state': 'SUCCESS', 'exit_status': 0, 'text': 'done', 'error_text': ''})
        self.assertEqual(db.session.get(CopyJob, self.t1['job']['id']).progress_state, 'SUCCESS')
        with self.assertRaises(HTTP_410_GONE):
            self.progress(self.w1, self.t1)
        with self.assertRaises(HTTP_410_GONE):
            worker_manager.finish(self.w1, self.t1['ticket_id'], self.t1['ticket_token'], {'state': 'SUCCESS'})

    def test_lease_expiry_fails_the_job(self):
        remote_job = db.session.get(RemoteJob, self.t1['ticket_id'])
        remote_job.lease_expires_at = utcnow() - datetime.timedelta(seconds=1)
        db.session.commit()
        worker_manager.expire_leases(force=True)
        job = db.session.get(CopyJob, self.t1['job']['id'])
        self.assertEqual(job.progress_state, 'FAILED')
        self.assertIn('lease expired', job.progress_error)
        self.assertEqual(db.session.get(RemoteJob, self.t1['ticket_id']).state, 'DONE')
        with self.assertRaises(HTTP_410_GONE):
            self.progress(self.w1, self.t1)
        # The other job is unaffected
        self.assertEqual(self.progress(self.w2, self.t2)['action'], 'continue')

    def test_late_report_after_lease_expiry(self):
        remote_job = db.session.get(RemoteJob, self.t1['ticket_id'])
        remote_job.lease_expires_at = utcnow() - datetime.timedelta(seconds=1)
        db.session.commit()
        with self.assertRaises(HTTP_410_GONE): # before any sweep ran
            self.progress(self.w1, self.t1)
        self.assertEqual(db.session.get(CopyJob, self.t1['job']['id']).progress_state, 'FAILED')

    def test_revoking_a_worker_fails_its_jobs(self):
        worker_manager.revoke_worker('w1')
        self.assertEqual(db.session.get(CopyJob, self.t1['job']['id']).progress_state, 'FAILED')
        self.assertEqual(db.session.get(CopyJob, self.t2['job']['id']).progress_state, 'PROGRESS')

    def test_hashsum_finish(self):
        job = HashsumJob(owner='alice', src_resource_path='/a', dst_resource_path='/b', option_download=False, progress_state='PROGRESS')
        db.session.add(job)
        db.session.commit()
        worker_manager.queue_job('hashsum', job, 'onprem')
        ticket = self.claim(self.w1)
        tree = [{'title': 'f', 'hash': 'x', 'isLeaf': True}]
        worker_manager.finish(self.w1, ticket['ticket_id'], ticket['ticket_token'],
                              {'state': 'SUCCESS', 'src_tree': tree, 'dst_tree': [], 'src_error_text': None, 'dst_error_text': ''})
        job = db.session.get(HashsumJob, job.id)
        self.assertEqual((job.progress_state, json.loads(job.progress_src_tree), json.loads(job.progress_dst_tree)), ('SUCCESS', tree, []))


class TestWorkerTokenBroker(WorkerTestBase):

    def setUp(self):
        super().setUp()
        token = {'access_token': 'ACCESS', 'refresh_token': REAL_REFRESH, 'expiry': '2020-01-01T00:00:00Z'}
        self.onedrive = self.connection('alice', type='onedrive', onedrive_token=json.dumps(token),
                                        onedrive_drive_id='b!x', onedrive_drive_type='business', token_broker_handle='HANDLE')
        self.job = self.copy_job(src=self.onedrive, src_path='/Documents')
        self.worker_row, _ = self.worker()
        self.ticket = self.claim(self.worker_row)
        self.refresh_token = json.loads(self.ticket['job']['src']['rclone_env']['RCLONE_CONFIG_SRC_TOKEN'])['refresh_token']
        patcher = mock.patch.object(worker_manager.token_broker_manager, 'serve_locked_connection',
                                    return_value=(200, {'access_token': 'NEW'}))
        self.serve = patcher.start()
        self.addCleanup(patcher.stop)

    def broker(self, refresh_token):
        return worker_manager.handle_worker_token_request({'grant_type': 'refresh_token', 'refresh_token': refresh_token}, None)

    def test_the_jobs_connection_is_refreshed(self):
        self.assertEqual(self.broker(self.refresh_token), (200, {'access_token': 'NEW'}))
        connection = self.serve.call_args[0][0]
        self.assertEqual(connection.id, self.onedrive.id)

    def test_scope(self):
        prefix, ticket_id, side, secret = self.refresh_token.split('.')
        for bad in (
            'HANDLE',                                          # the loopback handle
            REAL_REFRESH,
            '.'.join((prefix, ticket_id, 'dst', secret)),       # the job's other side (local)
            '.'.join((prefix, str(int(ticket_id) + 1), side, secret)), # another ticket
            '.'.join((prefix, ticket_id, side, secret[:-1])),
            self.ticket['ticket_token'],                        # the worker's ticket token
        ):
            self.assertEqual(self.broker(bad)[0], 400, bad)
        self.serve.assert_not_called()

    def test_dead_after_the_job_ends(self):
        worker_manager.finish(self.worker_row, self.ticket['ticket_id'], self.ticket['ticket_token'], {'state': 'FAILED'})
        self.assertEqual(self.broker(self.refresh_token)[0], 400)

    def test_dead_when_the_worker_is_revoked(self):
        worker_manager.revoke_worker('w1')
        self.assertEqual(self.broker(self.refresh_token)[0], 400)

    def test_http_endpoint(self):
        response = self.app.test_client().post('/api/workers/oauth/token', base_url=BASE_URL,
                                               data={'grant_type': 'refresh_token', 'refresh_token': self.refresh_token})
        self.assertEqual((response.status_code, response.get_json()), (200, {'access_token': 'NEW'}))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')


class TestHttpApi(WorkerTestBase):

    def test_auth_is_rate_limited(self):
        client = self.app.test_client()
        codes = [client.post('/api/workers/auth', json={'secret': 'mzw1.1.wrong'}, base_url=BASE_URL).status_code
                 for _ in range(worker_views.AUTH_LIMIT.limit + 1)]
        self.assertEqual(codes[:-1], [401] * worker_views.AUTH_LIMIT.limit)
        self.assertEqual(codes[-1], 429)

    def test_claim_flow(self):
        worker, secret = self.worker()
        client = self.app.test_client()
        token = client.post('/api/workers/auth', json={'secret': secret}, base_url=BASE_URL).get_json()['access_token']
        headers = {'Authorization': 'Bearer ' + token}
        # A long poll that finds nothing (the session is released while it waits)
        response = client.post('/api/workers/claim', json={'pool': 'onprem', 'wait': 1.5}, headers=headers, base_url=BASE_URL)
        self.assertEqual(response.status_code, 204)
        job = self.copy_job()
        ticket = client.post('/api/workers/claim', json={'pool': 'onprem'}, headers=headers, base_url=BASE_URL).get_json()
        self.assertEqual(ticket['job']['id'], job.id)
        response = client.post('/api/workers/jobs/{}/progress'.format(ticket['ticket_id']), json={'percent': 5},
                               headers=headers, base_url=BASE_URL)
        self.assertEqual(response.status_code, 403) # no ticket token
        response = client.post('/api/workers/jobs/{}/progress'.format(ticket['ticket_id']), json={'percent': 5},
                               headers={**headers, 'X-Motuz-Ticket': ticket['ticket_token']}, base_url=BASE_URL)
        self.assertEqual(response.get_json()['action'], 'continue')


class TestRouting(unittest.TestCase):
    CONFIG = {'LOCAL_JOB_POOL': 'onprem', 'LARGE_JOB_POOL': 'aws', 'LARGE_JOB_BYTES': 300 * 1000 ** 3,
              'LARGE_JOB_FILES': 50000, 'JOB_SIZE_TIMEOUT': 60}

    def test_local_paths_go_to_the_local_pool(self):
        estimate = mock.Mock()
        self.assertEqual(job_routing.choose_pool(None, '/a', object(), self.CONFIG, estimate), 'onprem')
        self.assertEqual(job_routing.choose_pool(object(), 'b', None, self.CONFIG, estimate), 'onprem')
        estimate.assert_not_called()

    def test_cloud_to_cloud(self):
        small = mock.Mock(return_value=(10, 10))
        self.assertEqual(job_routing.choose_pool(object(), 'b', object(), self.CONFIG, small), 'central')
        for size in ((300 * 1000 ** 3, 1), (1, 50000), (float('inf'), float('inf'))):
            self.assertEqual(job_routing.choose_pool(object(), 'b', object(), self.CONFIG, mock.Mock(return_value=size)), 'aws')
        self.assertEqual(job_routing.choose_pool(object(), 'b', object(), self.CONFIG, mock.Mock(return_value=None)), 'central')

    def test_defaults_keep_everything_central(self):
        config = dict(self.CONFIG, LOCAL_JOB_POOL='central', LARGE_JOB_POOL='central')
        estimate = mock.Mock()
        self.assertEqual(job_routing.choose_pool(None, '/a', None, config, estimate), 'central')
        self.assertEqual(job_routing.choose_pool(object(), 'b', object(), config, estimate), 'central')
        estimate.assert_not_called()

    def test_invalid_pool(self):
        with self.assertRaises(ValueError):
            job_routing.choose_pool(None, '/a', None, dict(self.CONFIG, LOCAL_JOB_POOL='On Prem!'))


class TestRateLimiter(unittest.TestCase):

    def test_sliding_window(self):
        limiter = RateLimiter(limit=2, window_seconds=10)
        self.assertTrue(limiter.allow('a', now=0))
        self.assertTrue(limiter.allow('a', now=1))
        self.assertFalse(limiter.allow('a', now=2))
        self.assertTrue(limiter.allow('b', now=2))
        self.assertTrue(limiter.allow('a', now=10.5))

    def test_bounded_keys(self):
        limiter = RateLimiter(limit=1, window_seconds=10, max_keys=3)
        for key in range(10):
            limiter.allow(key, now=0)
        self.assertEqual(len(limiter._hits), 3)


class FakeHashsumConnection:
    def __init__(self, exitstatus):
        self.exitstatus = exitstatus
        self.started = []
        self.deleted = []

    def start(self, side, run_id):
        self.started.append((side, run_id))

    def hashsum_finished(self, run_id):
        return True

    def hashsum_text(self, run_id):
        return [{'Name': 'f.txt', 'md5chksum': 'a' * 32}]

    def hashsum_exitstatus(self, run_id):
        return self.exitstatus[run_id.split('_')[1]]

    def hashsum_error_text(self, run_id):
        return ''

    def hashsum_percent(self, run_id):
        return 100

    def hashsum_delete(self, run_id):
        self.deleted.append(run_id)


class TestJobRunner(unittest.TestCase):

    def test_exit_state(self):
        self.assertEqual([job_runner.exit_state(s) for s in (-1, 0, 1, 143)], ['UNSET', 'SUCCESS', 'FAILED', 'FAILED'])

    def test_identical_sides(self):
        connection = FakeHashsumConnection({'src': 0, 'dst': 0})
        result = job_runner.run_hashsum(connection, 7, connection.start, lambda *a: None, interval=0)
        self.assertEqual((result['state'], result['src_tree'], result['dst_tree']), ('SUCCESS', [], []))
        self.assertEqual(connection.started, [('src', '7_src'), ('dst', '7_dst')])
        self.assertEqual(connection.deleted, ['7_src', '7_dst'])

    def test_failed_source_skips_the_destination(self):
        connection = FakeHashsumConnection({'src': 1, 'dst': 0})
        result = job_runner.run_hashsum(connection, 7, connection.start, lambda *a: None, interval=0)
        self.assertEqual((result['state'], result['failed_side']), ('FAILED', 'src'))
        self.assertEqual(connection.started, [('src', '7_src')])

    def test_stop_before_a_side(self):
        connection = FakeHashsumConnection({'src': 0, 'dst': 0})
        result = job_runner.run_hashsum(connection, 7, connection.start, lambda *a: None, interval=0,
                                        should_stop=lambda: bool(connection.started))
        self.assertEqual(result['state'], 'STOPPED')


class TestWorkerAgent(unittest.TestCase):
    """src/worker/motuz_worker.py (imports only api.utils, never the Flask app)"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'worker'))
        import motuz_worker
        cls.agent = motuz_worker

    def test_ticket_configuration_is_checked(self):
        side = self.agent.side_credentials
        self.assertEqual(side({'local': True, 'path': '/a'}), {})
        self.assertEqual(side({'local': False, 'remote': 'src', 'rclone_env': {'RCLONE_CONFIG_SRC_TYPE': 's3'}}),
                         {'RCLONE_CONFIG_SRC_TYPE': 's3'})
        for env in ({'LD_PRELOAD': '/x.so'}, {'RCLONE_CONFIG_DST_TYPE': 's3'}, {'RCLONE_CONFIG_SRC_TYPE': 1}):
            with self.assertRaises(self.agent.RcloneException):
                side({'local': False, 'remote': 'src', 'rclone_env': env})

    def test_credential_file_must_be_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'credential')
            with open(path, 'w') as f:
                f.write('mzw1.1.secret\n')
            args = mock.Mock(central_url=None, bootstrap_token=None, bootstrap_token_file=None, once=False)
            config = self.agent.Config({'MOTUZ_CENTRAL_URL': 'https://motuz.test', 'MOTUZ_WORKER_CREDENTIAL_FILE': path}, args)
            os.chmod(path, 0o644)
            with self.assertRaises(self.agent.ConfigError):
                config.secret()
            os.chmod(path, 0o600)
            self.assertEqual(config.secret(), 'mzw1.1.secret')

    def test_https_only_and_rclone_environment(self):
        args = mock.Mock(central_url=None, bootstrap_token=None, bootstrap_token_file=None, once=False)
        with self.assertRaises(self.agent.ConfigError):
            self.agent.Config({'MOTUZ_CENTRAL_URL': 'http://motuz.test'}, args)
        config = self.agent.Config({'MOTUZ_CENTRAL_URL': 'https://motuz.test', 'HTTPS_PROXY': 'http://proxy:3128',
                                    'MOTUZ_FLASK_SECRET_KEY': 'x'}, args)
        self.assertEqual(config.rclone_env, {'HTTPS_PROXY': 'http://proxy:3128'})

    def test_ca_bundle_must_be_readable_by_the_job_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o755)
            path = os.path.join(directory, 'ca.pem')
            open(path, 'w').close()
            os.chmod(path, 0o644)
            self.assertTrue(self.agent.readable_by_everyone(path))
            os.chmod(path, 0o600)
            self.assertFalse(self.agent.readable_by_everyone(path))
            os.chmod(path, 0o644)
            os.chmod(directory, 0o700)
            self.assertFalse(self.agent.readable_by_everyone(path))

    def test_required_paths(self):
        args = mock.Mock(central_url=None, bootstrap_token=None, bootstrap_token_file=None, once=False)
        config = self.agent.Config({'MOTUZ_CENTRAL_URL': 'https://motuz.test', 'MOTUZ_REQUIRED_PATHS': '/,/nonexistent-motuz'}, args)
        problems = self.agent.check_host(config)
        self.assertTrue(any('/nonexistent-motuz does not exist' in p for p in problems), problems)
        self.assertFalse(any(p.startswith('required path / ') for p in problems), problems)


if __name__ == '__main__':
    unittest.main()
