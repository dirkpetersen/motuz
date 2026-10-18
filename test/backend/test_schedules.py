"""
Scheduled repeating copies (utils/schedule.py, managers/schedule_manager.py,
/api/copy-schedules/): the next run (hour, day, month and year boundaries), claiming
due schedules once, skipping a run while the previous job runs, add-only runs
(overwrite False unless MOTUZ_ALLOW_GENERAL_OVERWRITES), the AWS egress confirmation,
owner scoping and the API. Against SQLite.
"""
import datetime
import os
import tempfile
import unittest
from unittest import mock

from flask import Flask
import flask_jwt_extended as flask_jwt

from api.application import db, jwt, register_api
from api.config import TestingConfig
from api.exceptions import HTTP_409_CONFLICT
from api.managers import copy_job_manager, job_routing, overwrite_policy, schedule_manager, space_manager
from api.models import CloudConnection, CopyJob, CopySchedule
from api.utils import egress_cost, rclone_tuning, schedule
from api.utils.rclone_connection import RcloneConnection

D = datetime.datetime
TB = 1000 ** 4


def spec(frequency, **fields):
    return schedule.validate({'frequency': frequency, **fields})


class NextRunTest(unittest.TestCase):

    def test_hourly(self):
        s = spec('hourly', minute=15)
        self.assertEqual(schedule.next_run(s, D(2026, 10, 18, 9, 0)), D(2026, 10, 18, 9, 15))
        self.assertEqual(schedule.next_run(s, D(2026, 10, 18, 9, 15)), D(2026, 10, 18, 10, 15), 'strictly after')
        self.assertEqual(schedule.next_run(s, D(2026, 10, 18, 9, 14, 59, 999)), D(2026, 10, 18, 9, 15))
        self.assertEqual(schedule.next_run(s, D(2026, 12, 31, 23, 30)), D(2027, 1, 1, 0, 15), 'year boundary')

    def test_daily(self):
        s = spec('daily', minute=30, hour=2)
        self.assertEqual(schedule.next_run(s, D(2026, 10, 18, 1, 0)), D(2026, 10, 18, 2, 30))
        self.assertEqual(schedule.next_run(s, D(2026, 10, 18, 2, 30)), D(2026, 10, 19, 2, 30))
        self.assertEqual(schedule.next_run(s, D(2026, 10, 31, 3, 0)), D(2026, 11, 1, 2, 30), 'month boundary')
        self.assertEqual(schedule.next_run(s, D(2028, 2, 28, 3, 0)), D(2028, 2, 29, 2, 30), 'leap day')
        self.assertEqual(schedule.next_run(s, D(2026, 12, 31, 23, 59)), D(2027, 1, 1, 2, 30))

    def test_weekly(self):
        s = spec('weekly', minute=0, hour=6, weekday=0)  # Monday
        # 2026-10-18 is a Sunday
        self.assertEqual(schedule.next_run(s, D(2026, 10, 18, 12, 0)), D(2026, 10, 19, 6, 0))
        self.assertEqual(schedule.next_run(s, D(2026, 10, 19, 6, 0)), D(2026, 10, 26, 6, 0))
        self.assertEqual(schedule.next_run(s, D(2026, 10, 19, 5, 59)), D(2026, 10, 19, 6, 0))
        self.assertEqual(schedule.next_run(s, D(2026, 12, 29, 0, 0)), D(2027, 1, 4, 6, 0), 'year boundary')
        sunday = spec('weekly', minute=0, hour=23, weekday=6)
        self.assertEqual(schedule.next_run(sunday, D(2026, 10, 18, 23, 0)), D(2026, 10, 25, 23, 0))

    def test_monthly(self):
        s = spec('monthly', minute=5, hour=1, day_of_month=28)
        self.assertEqual(schedule.next_run(s, D(2026, 2, 1)), D(2026, 2, 28, 1, 5), 'every month has day 28')
        self.assertEqual(schedule.next_run(s, D(2026, 2, 28, 1, 5)), D(2026, 3, 28, 1, 5))
        self.assertEqual(schedule.next_run(s, D(2026, 12, 28, 2, 0)), D(2027, 1, 28, 1, 5), 'year boundary')
        first = spec('monthly', minute=0, hour=0, day_of_month=1)
        self.assertEqual(schedule.next_run(first, D(2026, 12, 31, 23, 59)), D(2027, 1, 1, 0, 0))
        self.assertEqual(schedule.next_run(first, D(2027, 1, 1, 0, 0)), D(2027, 2, 1, 0, 0))

    def test_aware_times_are_utc(self):
        s = spec('daily', minute=0, hour=12)
        pacific = datetime.timezone(datetime.timedelta(hours=-7))
        self.assertEqual(schedule.next_run(s, datetime.datetime(2026, 10, 18, 4, 0, tzinfo=pacific)),
                         D(2026, 10, 18, 12, 0))  # 11:00 UTC -> 12:00 UTC the same day
        self.assertEqual(schedule.next_run(s, datetime.datetime(2026, 10, 18, 4, 0, tzinfo=pacific)).tzinfo, None)

    def test_validate(self):
        self.assertEqual(spec('hourly', minute=3, hour=7, weekday=2),
                         {'frequency': 'hourly', 'minute': 3, 'hour': None, 'weekday': None, 'day_of_month': None})
        for raw in ({}, {'frequency': 'yearly', 'minute': 0}, {'frequency': 'hourly'},
                    {'frequency': 'hourly', 'minute': 60}, {'frequency': 'hourly', 'minute': True},
                    {'frequency': 'hourly', 'minute': '5'}, {'frequency': 'daily', 'minute': 0},
                    {'frequency': 'daily', 'minute': 0, 'hour': 24}, {'frequency': 'weekly', 'minute': 0, 'hour': 0, 'weekday': 7},
                    {'frequency': 'monthly', 'minute': 0, 'hour': 0, 'day_of_month': 29},
                    {'frequency': 'monthly', 'minute': 0, 'hour': 0, 'day_of_month': 0}, None):
            with self.assertRaises(schedule.ScheduleError, msg=raw):
                schedule.validate(raw)

    def test_describe_and_iso(self):
        self.assertEqual(schedule.describe(spec('hourly', minute=5)), 'every hour at minute 05 (UTC)')
        self.assertEqual(schedule.describe(spec('daily', minute=5, hour=2)), 'every day at 02:05 UTC')
        self.assertEqual(schedule.describe(spec('weekly', minute=0, hour=14, weekday=4)), 'every Friday at 14:00 UTC')
        self.assertEqual(schedule.describe(spec('monthly', minute=0, hour=0, day_of_month=1)),
                         'on day 1 of every month at 00:00 UTC')
        self.assertEqual(schedule.iso_utc(D(2026, 10, 18, 2, 5, 7, 123)), '2026-10-18T02:05:07Z')
        self.assertIsNone(schedule.iso_utc(None))

    def test_monthly_egress(self):
        stored = {'rate_low_per_gb': 0.05, 'rate_high_per_gb': 0.09}
        result = egress_cost.repeated(stored, 10 * 10 ** 9, schedule.RUNS_PER_MONTH['daily'])
        self.assertEqual(result, {'new_bytes_per_run': 10 ** 10, 'runs_per_month': 30.44,
                                  'monthly_low_usd': 15.22, 'monthly_high_usd': 27.4})
        self.assertIsNone(egress_cost.repeated(stored, None, 1)['monthly_low_usd'])
        self.assertIsNone(egress_cost.repeated(stored, -1, 1)['new_bytes_per_run'])


class ScheduleApiTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = Flask('schedule-test')
        self.app.config.from_object(TestingConfig)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(self.directory.name, 'test.sqlite3'),
            SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
            JWT_SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
            ALLOW_GENERAL_OVERWRITES=False,
            CENTRAL_AWS_REGION=None,
            LOCAL_JOB_POOL='central',
            LARGE_JOB_POOL='central',
            EC2=None,
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
        self.alice, self.bob = self.headers('alice'), self.headers('bob')
        patcher = mock.patch.object(copy_job_manager.tasks.copy_job, 'apply_async')
        self.apply_async = patcher.start()
        self.addCleanup(patcher.stop)
        for target, name, value in ((job_routing, 'estimate_size', None), (space_manager, 'check_copy', None)):
            patcher = mock.patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def headers(self, user):
        with self.app.test_request_context():
            return {'Authorization': 'Bearer ' + flask_jwt.create_access_token(identity=user)}

    def cloud(self, owner, **fields):
        cloud = CloudConnection(owner=owner, name='c', type='s3', subtype='key', bucket='b',
                                s3_access_key_id='AKIDEXAMPLE', s3_secret_access_key='secret', **fields)
        db.session.add(cloud)
        db.session.commit()
        return cloud.id

    def create(self, headers=None, **fields):
        body = {'description': 'nightly', 'src_resource_path': '/src/data', 'dst_resource_path': '/dst/data',
                'copy_links': False, 'frequency': 'daily', 'minute': 30, 'hour': 2, **fields}
        return self.client.post('/api/copy-schedules/', headers=headers or self.alice, json=body)

    def created(self, **fields):
        response = self.create(**fields)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()

    def make_due(self, schedule_id, when=None):
        row = db.session.get(CopySchedule, schedule_id)
        row.next_run_at = when or D(2026, 10, 18, 2, 30)
        db.session.commit()

    def tick(self, now=D(2026, 10, 18, 2, 31)):
        return schedule_manager.tick(now)

    # --- API

    def test_create_and_list(self):
        with mock.patch.object(schedule_manager, '_utcnow', return_value=D(2026, 10, 18, 1, 0)):
            body = self.created(hour=2, minute=30, weekday=3)
        self.assertEqual((body['frequency'], body['hour'], body['minute'], body['weekday']), ('daily', 2, 30, None))
        self.assertEqual(body['next_run_at'], '2026-10-18T02:30:00Z')
        self.assertEqual(body['schedule_text'], 'every day at 02:30 UTC')
        self.assertIs(body['enabled'], True)
        self.assertIsNone(body['last_job_id'])
        self.apply_async.assert_not_called()  # the first run at the next scheduled time
        self.assertEqual(CopyJob.query.count(), 0)
        listed = self.client.get('/api/copy-schedules/', headers=self.alice).get_json()
        self.assertEqual([s['id'] for s in listed], [body['id']])
        self.assertEqual(self.client.get('/api/copy-schedules/', headers=self.bob).get_json(), [])

    def test_run_now(self):
        body = self.created(run_now=True)
        job = CopyJob.query.one()
        self.assertEqual((body['last_job_id'], job.schedule_id, job.owner), (job.id, body['id'], 'alice'))
        self.assertIs(job.overwrite, False)
        self.assertEqual(self.apply_async.call_count, 1)
        jobs = self.client.get('/api/copy-jobs/', headers=self.alice).get_json()['data']
        self.assertEqual(jobs[0]['schedule_id'], body['id'])

    def test_run_now_refused_stores_nothing(self):
        with mock.patch.object(space_manager, 'check_copy', side_effect=HTTP_409_CONFLICT('too full')):
            response = self.create(run_now=True)
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual((CopySchedule.query.count(), CopyJob.query.count()), (0, 0))

    def test_invalid(self):
        for fields in ({'frequency': 'yearly'}, {'minute': 60}, {'frequency': 'monthly', 'day_of_month': 31},
                       {'frequency': 'weekly'}, {'dst_resource_path': ''}, {'performance': {'transfers': 10 ** 6}}):
            response = self.create(**fields)
            self.assertEqual(response.status_code, 400, (fields, response.get_json()))
        self.assertEqual(CopySchedule.query.count(), 0)
        self.assertEqual(self.client.get('/api/copy-schedules/').status_code, 401)

    def test_owner_scope(self):
        body = self.created()
        url = '/api/copy-schedules/{}'.format(body['id'])
        self.assertEqual(self.client.get(url, headers=self.alice).status_code, 200)
        for method in ('get', 'delete'):
            self.assertEqual(getattr(self.client, method)(url, headers=self.bob).status_code, 404, method)
        self.assertEqual(self.client.patch(url, headers=self.bob, json={'enabled': False}).status_code, 404)
        self.assertIs(db.session.get(CopySchedule, body['id']).enabled, True)
        # Nor can a schedule use another user's connection
        bobs = self.cloud('bob', s3_provider='Minio', s3_endpoint='https://minio.example')
        response = self.create(src_cloud_id=bobs)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn('minio.example', response.get_data(as_text=True))

    def test_pause_resume_delete(self):
        body = self.created()
        url = '/api/copy-schedules/{}'.format(body['id'])
        self.assertIs(self.client.patch(url, headers=self.alice, json={'enabled': False}).get_json()['enabled'], False)
        self.make_due(body['id'])
        self.assertEqual(self.tick(), [], 'paused schedules do not run')
        with mock.patch.object(schedule_manager, '_utcnow', return_value=D(2026, 10, 20, 5, 0)):
            resumed = self.client.patch(url, headers=self.alice, json={'enabled': True}).get_json()
        self.assertEqual(resumed['next_run_at'], '2026-10-21T02:30:00Z', 'no catch-up after a pause')
        self.assertEqual(self.client.patch(url, headers=self.alice, json={'enabled': 'yes'}).status_code, 400)
        self.assertEqual(self.client.delete(url, headers=self.alice).status_code, 200)
        self.assertEqual(self.client.get(url, headers=self.alice).status_code, 404)

    def test_deleting_the_connection_deletes_its_schedules(self):
        minio = self.cloud('alice', s3_provider='Minio', s3_endpoint='https://minio.example')
        self.created(src_cloud_id=minio)
        self.assertEqual(self.client.delete('/api/connections/{}'.format(minio), headers=self.alice).status_code, 200)
        self.assertEqual(CopySchedule.query.count(), 0)

    # --- runner

    def test_tick_runs_a_due_schedule_once(self):
        body = self.created()
        self.assertEqual(self.tick(D(2026, 10, 18, 2, 29)), [], 'not due yet')
        self.make_due(body['id'])
        self.assertEqual(self.tick(), [(body['id'], 'started')])
        self.assertEqual(self.tick(), [], 'the same due time never runs twice')
        row = db.session.get(CopySchedule, body['id'])
        job = CopyJob.query.one()
        self.assertEqual((row.last_job_id, row.next_run_at, row.last_run_at), (job.id, D(2026, 10, 19, 2, 30), D(2026, 10, 18, 2, 31)))
        self.assertEqual(row.last_status, 'Started job {} at 2026-10-18 02:31 UTC'.format(job.id))
        self.assertEqual((job.owner, job.schedule_id, job.src_resource_path, job.dst_resource_path, job.description),
                         ('alice', body['id'], '/src/data', '/dst/data', 'nightly'))
        self.apply_async.assert_called_once()

    def test_a_late_runner_runs_once_without_catch_up(self):
        body = self.created(frequency='hourly', minute=0)
        self.make_due(body['id'], D(2026, 10, 15, 0, 0))  # down for days
        self.assertEqual(self.tick(D(2026, 10, 18, 2, 31)), [(body['id'], 'started')])
        self.assertEqual(db.session.get(CopySchedule, body['id']).next_run_at, D(2026, 10, 18, 3, 0))

    def test_compare_and_set_against_a_second_runner(self):
        body = self.created()
        self.make_due(body['id'])
        original = schedule_manager.update

        def other_runner_first(*args, **kwargs):
            # Another runner moved next_run_at between our SELECT and our UPDATE
            db.session.execute(original(CopySchedule).where(CopySchedule.id == body['id'])
                               .values(next_run_at=D(2026, 10, 19, 2, 30)))
            return original(*args, **kwargs)
        with mock.patch.object(schedule_manager, 'update', side_effect=other_runner_first):
            self.assertEqual(self.tick(), [])
        self.assertEqual(CopyJob.query.count(), 0)

    def test_skips_while_the_previous_job_runs(self):
        body = self.created()
        self.make_due(body['id'])
        self.tick()
        first = CopyJob.query.one()
        self.assertEqual(first.progress_state, 'PROGRESS')
        self.make_due(body['id'], D(2026, 10, 19, 2, 30))
        self.assertEqual(self.tick(D(2026, 10, 19, 2, 31)), [(body['id'], 'skipped')])
        row = db.session.get(CopySchedule, body['id'])
        self.assertEqual((row.skipped_runs, row.last_job_id, CopyJob.query.count()), (1, first.id, 1))
        self.assertEqual(row.last_status, 'Skipped the run of 2026-10-19 02:31 UTC: job {} was still running'.format(first.id))
        self.assertEqual(row.next_run_at, D(2026, 10, 20, 2, 30), 'the next run stays scheduled')
        first.progress_state = 'SUCCESS'
        db.session.commit()
        self.assertEqual(self.tick(D(2026, 10, 20, 2, 31)), [(body['id'], 'started')])
        self.assertEqual(CopyJob.query.count(), 2)

    def test_a_refused_run_is_recorded(self):
        body = self.created()
        self.make_due(body['id'])
        with mock.patch.object(space_manager, 'check_copy', side_effect=HTTP_409_CONFLICT('Copying 2 GiB would fill it')):
            self.assertEqual(self.tick(), [(body['id'], 'refused')])
        row = db.session.get(CopySchedule, body['id'])
        self.assertEqual(row.last_status, 'Not run at 2026-10-18 02:31 UTC: Copying 2 GiB would fill it')
        self.assertIs(row.enabled, True)
        self.assertEqual(CopyJob.query.count(), 0)

    def test_a_failed_dispatch_is_recorded(self):
        body = self.created()
        self.make_due(body['id'])
        self.apply_async.side_effect = RuntimeError('broker down')
        self.assertEqual(self.tick(), [(body['id'], 'refused')])
        row = db.session.get(CopySchedule, body['id'])
        job = CopyJob.query.one()
        self.assertEqual((row.last_job_id, job.progress_state), (job.id, 'FAILED'))
        self.assertTrue(row.last_status.startswith('Failed at 2026-10-18 02:31 UTC'), row.last_status)

    # --- add-only runs

    def test_scheduled_runs_never_overwrite_in_a_motuz_destination(self):
        response = self.client.post('/api/copy-jobs/', headers=self.alice, json={
            'description': 'd', 'src_resource_path': '/src/data', 'dst_resource_path': '/dst/data', 'copy_links': False})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(overwrite_policy.is_motuz_destination(None, '/dst/data'))
        again = self.client.post('/api/copy-jobs/', headers=self.alice, json={
            'description': 'd', 'src_resource_path': '/src/data', 'dst_resource_path': '/dst/data', 'copy_links': False})
        self.assertIs(again.get_json()['overwrite'], True, 'a normal copy overwrites here')
        body = self.created()
        self.make_due(body['id'])
        self.tick()
        job = CopyJob.query.filter_by(schedule_id=body['id']).one()
        self.assertIs(job.overwrite, False)
        flags = job_routing.rclone_flags('copy', job)  # a remote worker's ticket
        self.assertIn('--ignore-existing', flags)
        with mock.patch.object(RcloneConnection, 'copy_with_credentials') as copy:
            RcloneConnection().copy(None, job.src_resource_path, None, job.dst_resource_path, 'alice', False, job.id,
                                    overwrite=job.overwrite)
        command = ' '.join(copy.call_args.args[0] if copy.call_args.args else [])
        flags = copy.call_args.kwargs['extra_flags']
        self.assertIn('--ignore-existing', flags)
        self.assertNotIn('sync', command)

    def test_general_overwrites_allow_overwriting(self):
        self.app.config['ALLOW_GENERAL_OVERWRITES'] = True
        body = self.created(run_now=True)
        self.assertIs(CopyJob.query.filter_by(schedule_id=body['id']).one().overwrite, True)

    def test_copy_command_never_syncs(self):
        # Scheduled runs are ordinary copy jobs: rclone copyto, which never deletes
        with mock.patch.object(RcloneConnection, '_copy_job_queue', create=True) as queue:
            connection = RcloneConnection()
            connection._copy_job_queue = queue
            connection.copy(None, '/src/a', None, '/dst/a', 'alice', False, 1, overwrite=False)
        command = queue.push.call_args.args[0]
        self.assertIn('copyto', command)
        for word in ('sync', 'move', 'moveto', 'delete', 'purge', '--delete-before', '--delete-after'):
            self.assertNotIn(word, command)
        self.assertEqual(rclone_tuning.KEEP_EXISTING_FLAGS[0], '--ignore-existing')

    # --- AWS egress

    def test_aws_source_needs_both_confirmations(self):
        aws = self.cloud('alice', s3_provider='AWS', s3_region='us-west-2')
        first = {'confirmed': True, 'bytes': TB, 'files': 10}
        for estimate, message in ((None, 'Confirm the AWS egress cost in the dialog'),
                                  ({'confirmed': False, 'repeat_confirmed': True}, 'Confirm the AWS egress cost in the dialog'),
                                  (first, schedule_manager.REPEAT_CONFIRM_MESSAGE),
                                  ({**first, 'repeat_confirmed': 'yes'}, schedule_manager.REPEAT_CONFIRM_MESSAGE)):
            response = self.create(src_cloud_id=aws, egress_estimate=estimate)
            self.assertEqual(response.status_code, 400, response.get_json())
            self.assertEqual(response.get_json()['message'], message)
        self.assertEqual(CopySchedule.query.count(), 0)
        body = self.created(src_cloud_id=aws, frequency='weekly', weekday=0,
                            egress_estimate={**first, 'repeat_confirmed': True, 'new_bytes_per_run': 10 ** 11})
        stored = body['egress_estimate']
        self.assertEqual((stored['kind'], stored['low_usd'], stored['high_usd'], stored['repeat_confirmed']),
                         ('internet', 50.0, 90.0, True))
        self.assertEqual((stored['new_bytes_per_run'], stored['runs_per_month'], stored['monthly_low_usd'],
                          stored['monthly_high_usd']), (10 ** 11, 4.35, 21.75, 39.15))
        # Each run stores the schedule's estimate, without a new confirmation
        self.make_due(body['id'])
        self.assertEqual(self.tick(), [(body['id'], 'started')])
        job = CopyJob.query.one()
        self.assertEqual((job.egress_estimate['schedule_id'], job.egress_estimate['confirmed'], job.egress_estimate['high_usd']),
                         (body['id'], True, 90.0))

    def test_no_confirmation_without_aws(self):
        minio = self.cloud('alice', s3_provider='Minio', s3_endpoint='https://minio.example')
        self.assertIsNone(self.created(src_cloud_id=minio)['egress_estimate'])

    def test_a_new_fee_pauses_the_schedule(self):
        aws = self.cloud('alice', s3_provider='AWS', s3_region='us-west-2')
        dst = self.cloud('alice', s3_provider='AWS', s3_region='us-west-2')
        self.app.config['CENTRAL_AWS_REGION'] = 'us-west-2'  # same region: no fee, no confirmation
        body = self.created(src_cloud_id=aws, dst_cloud_id=dst)
        self.assertIsNone(body['egress_estimate'])
        self.app.config['CENTRAL_AWS_REGION'] = None  # the server moved out of AWS
        self.make_due(body['id'])
        self.assertEqual(self.tick(), [(body['id'], 'refused')])
        row = db.session.get(CopySchedule, body['id'])
        self.assertIs(row.enabled, False)
        self.assertIn('not confirmed', row.last_status)
        self.assertEqual(CopyJob.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
