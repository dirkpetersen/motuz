"""
AWS egress cost (utils/egress_cost.py, managers/egress_manager.py): which connections
are on AWS, the regions, the price table and the range, GET /copy-jobs/egress-estimate/
(rclone size replaced, timeout, other users' connections) and copy / hashsum job
creation that refuses a job out of AWS without the confirmation; against SQLite.
"""
import os
import tempfile
import types
import unittest
from unittest import mock

from flask import Flask
import flask_jwt_extended as flask_jwt

from api.application import db, jwt, register_api
from api.config import TestingConfig
from api.managers import copy_job_manager, hashsum_job_manager, job_routing
from api.models import CloudConnection, CopyJob, HashsumJob
from api.utils import egress_cost, file_view
from api.utils.rclone_connection import RcloneConnection, RcloneException

TB = 1000 ** 4


def s3(**columns):
    values = dict(type='s3', subtype='key', s3_provider=None, s3_endpoint=None, s3_region='us-west-2',
                  profile_source=None)
    values.update(columns)
    return types.SimpleNamespace(**values)


class TestDetection(unittest.TestCase):

    def test_aws_provider(self):
        self.assertTrue(egress_cost.is_aws(s3(s3_provider='AWS')))
        self.assertTrue(egress_cost.is_aws(s3(s3_provider='AWS', s3_endpoint='https://s3.us-west-2.amazonaws.com')))

    def test_legacy_rule(self):
        self.assertTrue(egress_cost.is_aws(s3()))
        self.assertTrue(egress_cost.is_aws(s3(s3_provider='')))
        self.assertFalse(egress_cost.is_aws(s3(s3_endpoint='https://s3.hpc.example')))

    def test_other_providers(self):
        for provider in ('Ceph', 'Minio', 'Wasabi', 'GCS', 'Other'):
            self.assertFalse(egress_cost.is_aws(s3(s3_provider=provider)), provider)

    def test_other_types_and_local(self):
        self.assertFalse(egress_cost.is_aws(None))
        self.assertFalse(egress_cost.is_aws(types.SimpleNamespace(type='azureblob', subtype=None)))
        self.assertFalse(egress_cost.is_aws(types.SimpleNamespace(type='drive', subtype=None)))

    def test_profiles(self):
        # ~/.aws: the stored provider (AWS without an endpoint) holds
        self.assertTrue(egress_cost.is_aws(s3(subtype='profile', profile_source='aws', s3_provider='AWS')))
        self.assertFalse(egress_cost.is_aws(s3(subtype='profile', profile_source='aws', s3_provider='Minio')))
        # An rclone remote has its own provider: unknown
        self.assertFalse(egress_cost.is_aws(s3(subtype='profile', profile_source='rclone', s3_provider='AWS')))

    def test_regions(self):
        self.assertEqual(egress_cost.region(s3(s3_region=' US-West-2 ')), 'us-west-2')
        self.assertEqual(egress_cost.region(s3(s3_region=None)), 'us-east-1')  # rclone's default
        self.assertIsNone(egress_cost.region(s3(subtype='profile', profile_source='aws', s3_region='')))

    def test_matrix(self):
        west, east = s3(s3_provider='AWS'), s3(s3_provider='AWS', s3_region='us-east-1')
        west2 = s3(s3_provider='AWS')
        minio = s3(s3_provider='Minio', s3_endpoint='https://minio.example')
        azure = types.SimpleNamespace(type='azureblob', subtype=None)
        unknown = s3(subtype='profile', profile_source='aws', s3_provider='AWS', s3_region=None)

        def assess(src, dst, runner):
            a = egress_cost.assess(src, dst, runner, 'X')
            return a and (a.kind, a.legs)
        # Runner not in AWS (on-prem server, other pools): internet, whatever the destination
        for dst in (None, west2, east, minio, azure):
            self.assertEqual(assess(west, dst, None), ('internet', 0))
        self.assertEqual(egress_cost.assess(west, west2, None, 'this server, which is not in AWS').reason,
                         'runs on this server, which is not in AWS, so the data leaves AWS')
        # Runner in the source's region
        self.assertEqual(assess(west, None, 'us-west-2'), ('same-region', 0))  # its local disk
        self.assertEqual(assess(west, west2, 'us-west-2'), ('same-region', 0))
        self.assertEqual(assess(west, east, 'us-west-2'), ('inter-region', 1))
        self.assertEqual(assess(west, azure, 'us-west-2'), ('internet', 0))
        self.assertEqual(assess(west, minio, 'us-west-2'), ('internet', 0))
        # Runner in another region: the read out of the source's region
        r = egress_cost.assess(west, None, 'us-east-1', 'an EC2 worker in us-east-1')
        self.assertEqual((r.kind, r.legs), ('inter-region', 1))
        self.assertEqual(r.reason, 'runs on an EC2 worker in us-east-1, so the data is read out of us-west-2')
        self.assertEqual(assess(west, east, 'us-east-1'), ('inter-region', 1))
        r = egress_cost.assess(west, s3(s3_provider='AWS', s3_region='eu-west-1'), 'us-east-1', 'X')
        self.assertEqual((r.kind, r.legs), ('inter-region', 2))
        self.assertIn('read out of us-west-2 and written to eu-west-1', r.reason)
        # Unknown regions count as another region
        self.assertEqual(assess(unknown, None, 'us-west-2'), ('inter-region', 1))
        self.assertEqual(assess(west, unknown, 'us-west-2'), ('inter-region', 1))
        # Not from AWS
        self.assertIsNone(assess(None, west, 'us-west-2'))
        self.assertIsNone(assess(minio, None, None))
        self.assertEqual(egress_cost.worst([None, egress_cost.assess(west, None, 'us-west-2', 'X'),
                                            egress_cost.assess(west, None, None, 'Y')]).kind, 'internet')

    def test_region_setting(self):
        self.assertIsNone(egress_cost.parse_region('MOTUZ_CENTRAL_AWS_REGION', ' '))
        self.assertEqual(egress_cost.parse_region('MOTUZ_CENTRAL_AWS_REGION', 'us-west-2'), 'us-west-2')
        with self.assertRaises(ValueError) as e:
            egress_cost.parse_region('MOTUZ_CENTRAL_AWS_REGION', 'mars')
        self.assertIn('MOTUZ_CENTRAL_AWS_REGION', str(e.exception))


INTERNET = egress_cost.Assessment('internet', 0, 'runs on X, so the data leaves AWS')
INTER_REGION = egress_cost.Assessment('inter-region', 1, 'runs on X, so the data is read out of us-west-2')
SAME_REGION = egress_cost.Assessment('same-region', 0, 'runs on X, in the region of the data')


class TestPrices(unittest.TestCase):

    def test_table(self):
        self.assertEqual(egress_cost.PRICING_DATE, '2026-10')
        self.assertEqual(egress_cost.GB, 10 ** 9)
        self.assertEqual(egress_cost.FREE_GB_PER_MONTH, 100)
        self.assertEqual(egress_cost.INTERNET_TIERS,
                         ((10000, 0.09), (50000, 0.085), (150000, 0.07), (None, 0.05)))
        self.assertEqual((egress_cost.RATE_LOW_PER_GB, egress_cost.RATE_HIGH_PER_GB), (0.05, 0.09))
        self.assertEqual(egress_cost.INTER_REGION_PER_GB, 0.02)

    def test_internet_range(self):
        e = egress_cost.estimate(INTERNET, 3.4 * TB, 12, runs_on='X')
        self.assertEqual((e['low_usd'], e['high_usd']), (170.0, 306.0))
        self.assertEqual((e['bytes'], e['files'], e['free_gb_per_month']), (3400000000000, 12, 100))
        self.assertEqual((e['runs_on'], e['reason']), ('X', INTERNET.reason))
        self.assertIn('$0.05-$0.09 per GB', e['note'])
        self.assertIn('egress waiver', e['note'])
        one_tb = egress_cost.estimate(INTERNET, TB)
        self.assertEqual((one_tb['low_usd'], one_tb['high_usd']), (50.0, 90.0))

    def test_inter_region_and_same_region(self):
        e = egress_cost.estimate(INTER_REGION, TB)
        self.assertEqual((e['low_usd'], e['high_usd'], e['rate_low_per_gb']), (20.0, 20.0, 0.02))
        twice = egress_cost.estimate(INTER_REGION._replace(legs=2), TB)
        self.assertEqual((twice['high_usd'], twice['rate_high_per_gb']), (40.0, 0.04))
        same = egress_cost.estimate(SAME_REGION, TB)
        self.assertEqual(same['high_usd'], 0.0)
        self.assertFalse(egress_cost.needs_confirmation(SAME_REGION))
        self.assertFalse(egress_cost.needs_confirmation(None))
        self.assertEqual(egress_cost.estimate(None), {'applies': False})

    def test_unknown_size(self):
        e = egress_cost.estimate(INTERNET, None)
        self.assertTrue(e['applies'])
        self.assertIsNone(e['bytes'])
        self.assertIsNone(e['low_usd'])
        self.assertEqual(egress_cost.estimate(INTERNET, float('nan'))['bytes'], None)
        self.assertEqual(egress_cost.estimate(INTERNET, -5)['bytes'], None)
        self.assertEqual(egress_cost.estimate(INTERNET, True)['bytes'], None)

    def test_tiered(self):
        GB = egress_cost.GB
        self.assertEqual(egress_cost.tiered_usd(50 * GB), 0)
        self.assertAlmostEqual(egress_cost.tiered_usd(1100 * GB), 90.0)
        self.assertAlmostEqual(egress_cost.tiered_usd(20100 * GB), 10000 * 0.09 + 10000 * 0.085)
        self.assertAlmostEqual(egress_cost.tiered_usd(200100 * GB),
                               10000 * 0.09 + 40000 * 0.085 + 100000 * 0.07 + 50000 * 0.05)

    def test_confirmed_recomputes(self):
        stored = egress_cost.confirmed(INTERNET, {
            'confirmed': True, 'bytes': TB, 'files': 3, 'low_usd': 0.01, 'kind': 'same-region', 'x': 1}, 'X')
        self.assertEqual((stored['kind'], stored['runs_on']), (egress_cost.INTERNET, 'X'))
        self.assertEqual((stored['low_usd'], stored['high_usd']), (50.0, 90.0))
        self.assertTrue(stored['confirmed'])
        self.assertNotIn('x', stored)
        self.assertIsNone(egress_cost.confirmed(INTERNET, {'confirmed': 'yes'}))
        self.assertIsNone(egress_cost.confirmed(INTERNET, None))
        self.assertIsNone(egress_cost.confirmed(INTERNET, {'bytes': TB}))

    def test_stdlib_only(self):
        # api.utils must not import the app or config (the remote worker agent)
        with open(egress_cost.__file__) as f:
            imports = [line for line in f if line.startswith(('import ', 'from '))]
        self.assertEqual(imports, ['import math\n', 'import re\n', 'from collections import namedtuple\n',
                                   'from . import s3_providers\n'])


class EgressApiTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = Flask('egress-test')
        self.app.config.from_object(TestingConfig)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(self.directory.name, 'test.sqlite3'),
            SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
            JWT_SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
            EGRESS_SIZE_TIMEOUT=7,
            CENTRAL_AWS_REGION=None,  # this installation is on-prem
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

        self.sizes = {}   # path -> (bytes, files) or an exception
        self.measured = []
        patcher = mock.patch.object(RcloneConnection, 'size', self.fake_size)
        patcher.start()
        self.addCleanup(patcher.stop)
        for target in (copy_job_manager.tasks.copy_job, hashsum_job_manager.tasks.hashsum_job):
            patcher = mock.patch.object(target, 'apply_async')
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(job_routing, 'estimate_size', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.aws = self.connection('alice', s3_provider='AWS', s3_region='us-west-2')
        self.aws_east = self.connection('alice', s3_provider='AWS', s3_region='us-east-1')
        self.aws_west2 = self.connection('alice', s3_provider='AWS', s3_region='us-west-2')
        self.minio = self.connection('alice', s3_provider='Minio', s3_endpoint='https://minio.example')

    def fake_size(self, data, path, timeout):  # bound to the test, not to RcloneConnection
        self.measured.append((data.id, path, timeout))
        value = self.sizes.get(path, (TB, 10))
        if isinstance(value, Exception):
            raise value
        return value

    def connection(self, owner, **fields):
        connection = CloudConnection(owner=owner, name='c', type='s3', subtype='key', bucket='b',
                                     s3_access_key_id='AKIDEXAMPLE', s3_secret_access_key='secret', **fields)
        db.session.add(connection)
        db.session.commit()
        return connection.id

    def headers(self, user):
        with self.app.test_request_context():
            return {'Authorization': 'Bearer ' + flask_jwt.create_access_token(identity=user)}

    def estimate(self, src, paths, dst=0, headers=None):
        query = '&'.join(['src_cloud_id={}'.format(src), 'dst_cloud_id={}'.format(dst)]
                         + ['src_resource_path={}'.format(p) for p in paths])
        return self.client.get('/api/copy-jobs/egress-estimate/?' + query, headers=headers or self.alice)

    def test_local_source_and_other_provider(self):
        for src in (0, self.minio):
            response = self.estimate(src, ['/b/x'])
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json(), {'applies': False})
        self.assertEqual(self.measured, [])

    def test_internet_sums_the_paths(self):
        self.sizes = {'/b/one': (TB, 3), '/b/two': (2 * TB, 4)}
        body = self.estimate(self.aws, ['/b/one', '/b/two']).get_json()
        self.assertEqual((body['applies'], body['kind']), (True, 'internet'))
        self.assertEqual((body['bytes'], body['files']), (3 * TB, 7))
        self.assertEqual((body['low_usd'], body['high_usd']), (150.0, 270.0))
        self.assertEqual([path for _, path, _ in self.measured], ['/b/one', '/b/two'])
        self.assertTrue(all(0 < timeout <= 7 for _, _, timeout in self.measured))

    def test_on_prem_server_pays_internet_egress_even_within_a_region(self):
        for dst in (0, self.aws_west2, self.aws_east, self.minio):
            body = self.estimate(self.aws, ['/b/x'], dst).get_json()
            self.assertEqual((body['kind'], body['high_usd']), ('internet', 90.0), dst)
            self.assertEqual(body['runs_on'], 'this server, which is not in AWS')
            self.assertEqual(body['reason'], 'runs on this server, which is not in AWS, so the data leaves AWS')

    def test_server_in_aws(self):
        self.app.config['CENTRAL_AWS_REGION'] = 'us-west-2'
        body = self.estimate(self.aws, ['/b/x'], self.aws_west2).get_json()
        self.assertEqual((body['kind'], body['runs_on']), ('same-region', 'this server (AWS us-west-2)'))
        self.assertEqual(self.measured, [])  # free: no rclone size
        self.assertEqual(self.estimate(self.aws, ['/b/x']).get_json()['kind'], 'same-region')  # its own disk
        body = self.estimate(self.aws, ['/b/x'], self.aws_east).get_json()
        self.assertEqual((body['kind'], body['high_usd']), ('inter-region', 20.0))
        self.assertIn('written to us-east-1', body['reason'])
        body = self.estimate(self.aws_east, ['/b/x']).get_json()
        self.assertEqual((body['kind'], body['high_usd']), ('inter-region', 20.0))
        self.assertIn('read out of us-east-1', body['reason'])
        self.assertEqual(self.estimate(self.aws, ['/b/x'], self.minio).get_json()['kind'], 'internet')

    def ec2(self):
        self.app.config.update(LARGE_JOB_POOL='aws', LARGE_JOB_BYTES=100 * 10 ** 9, LARGE_JOB_FILES=10 ** 9,
                               EC2=types.SimpleNamespace(enabled=True, pool='aws', region='us-east-1'))

    def test_large_job_on_an_ec2_worker(self):
        self.ec2()
        body = self.estimate(self.aws, ['/b/x'], self.aws_east).get_json()  # 1 TB: large
        self.assertEqual((body['kind'], body['high_usd'], body['runs_on']),
                         ('inter-region', 20.0, 'an EC2 worker in us-east-1'))
        self.assertEqual(body['reason'], 'runs on an EC2 worker in us-east-1, so the data is read out of us-west-2')
        self.assertEqual(len(self.measured), 1)  # measured once, for routing and the price
        self.sizes = {'/b/small': (10 ** 9, 1)}  # small: this (on-prem) server
        body = self.estimate(self.aws, ['/b/small'], self.aws_east).get_json()
        self.assertEqual((body['kind'], body['high_usd']), ('internet', 0.09))
        self.sizes = {'/b/x': file_view.ViewTimeoutError('slow')}  # unknown: large, like job_routing
        body = self.estimate(self.aws, ['/b/x'], self.aws_east).get_json()
        self.assertEqual((body['kind'], body['bytes'], body['runs_on']), ('inter-region', None, 'an EC2 worker in us-east-1'))

    def test_other_pool_is_not_known_to_be_in_aws(self):
        self.app.config.update(LOCAL_JOB_POOL='onprem', CENTRAL_AWS_REGION='us-west-2')
        body = self.estimate(self.aws, ['/b/x']).get_json()
        self.assertEqual((body['kind'], body['runs_on']),
                         ('internet', 'a worker of the pool onprem, which is not known to be in AWS'))

    def test_timeout_and_failure_leave_the_size_unknown(self):
        for error in (file_view.ViewTimeoutError('timed out'), RcloneException('AccessDenied')):
            self.sizes = {'/b/x': error}
            body = self.estimate(self.aws, ['/b/x']).get_json()
            self.assertEqual((body['applies'], body['bytes'], body['low_usd']), (True, None, None))
            self.assertEqual(body['rate_high_per_gb'], 0.09)

    def test_other_users_connections_are_404(self):
        self.assertEqual(self.estimate(self.aws, ['/b/x'], headers=self.bob).status_code, 404)
        self.assertEqual(self.estimate(0, ['/tmp'], self.aws, headers=self.bob).status_code, 404)
        self.assertEqual(self.measured, [])
        self.assertEqual(self.client.get('/api/copy-jobs/egress-estimate/?src_cloud_id=x&src_resource_path=/a',
                                         headers=self.alice).status_code, 400)
        self.assertEqual(self.estimate(self.aws, []).status_code, 400)

    def job(self, src, dst=0, **extra):
        body = {'description': '', 'src_cloud_id': src, 'src_resource_path': '/b/x',
                'dst_resource_path': '/tmp/x', 'copy_links': False, **extra}
        if dst:
            body['dst_cloud_id'] = dst
        return self.client.post('/api/copy-jobs/', headers=self.alice, json=body)

    def test_create_refuses_without_confirmation(self):
        for extra in ({}, {'egress_estimate': None}, {'egress_estimate': {'confirmed': False, 'bytes': TB}}):
            response = self.job(self.aws, **extra)
            self.assertEqual(response.status_code, 400, response.get_json())
            self.assertIn('Confirm the AWS egress cost', response.get_json()['message'])
        self.assertEqual(CopyJob.query.count(), 0)
        response = self.job(self.aws, self.aws_east)
        self.assertEqual(response.status_code, 400)

    def test_create_stores_the_confirmed_estimate(self):
        response = self.job(self.aws, egress_estimate={'confirmed': True, 'bytes': TB, 'files': 10,
                                                       'kind': 'same-region', 'high_usd': 1})
        self.assertEqual(response.status_code, 201, response.get_json())
        stored = db.session.get(CopyJob, response.get_json()['id']).egress_estimate
        self.assertEqual((stored['kind'], stored['low_usd'], stored['high_usd'], stored['confirmed']),
                         ('internet', 50.0, 90.0, True))
        self.assertEqual(response.get_json()['egress_estimate']['high_usd'], 90.0)
        # Size unknown (timeout): the confirmation of the per-TB range is enough
        response = self.job(self.aws, egress_estimate={'confirmed': True, 'bytes': None})
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.get_json()['egress_estimate']['bytes'])

    def test_create_without_aws_needs_nothing(self):
        for src, dst in ((0, 0), (self.minio, 0)):
            response = self.job(src, dst)
            self.assertEqual(response.status_code, 201, response.get_json())
            self.assertIsNone(response.get_json()['egress_estimate'])

    def test_create_depends_on_where_the_job_runs(self):
        # On-prem: AWS -> AWS in one region still leaves AWS
        self.assertEqual(self.job(self.aws, self.aws_west2).status_code, 400)
        self.app.config['CENTRAL_AWS_REGION'] = 'us-west-2'
        response = self.job(self.aws, self.aws_west2)
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertIsNone(response.get_json()['egress_estimate'])
        # A large job would run on an EC2 worker in us-east-1: confirmation needed
        self.ec2()
        self.assertEqual(self.job(self.aws, self.aws_west2).status_code, 400)
        response = self.job(self.aws, self.aws_west2, egress_estimate={'confirmed': True, 'bytes': TB, 'files': 1})
        self.assertEqual(response.status_code, 201, response.get_json())
        stored = response.get_json()['egress_estimate']
        self.assertEqual((stored['kind'], stored['high_usd'], stored['runs_on']),
                         ('inter-region', 40.0, 'an EC2 worker in us-east-1'))  # out of and back into us-west-2

    def test_hashsum_with_download(self):
        body = {'src_cloud_id': self.aws, 'src_resource_path': '/b/x', 'dst_resource_path': '/tmp/x',
                'option_download': True}
        response = self.client.post('/api/hashsum-jobs/', headers=self.alice, json=body)
        self.assertEqual(response.status_code, 400, response.get_json())
        response = self.client.post('/api/hashsum-jobs/', headers=self.alice,
                                    json={**body, 'egress_estimate': {'confirmed': True, 'bytes': TB}})
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(db.session.get(HashsumJob, int(response.get_json()['id'])).egress_estimate['high_usd'], 90.0)
        response = self.client.post('/api/hashsum-jobs/', headers=self.alice, json={**body, 'option_download': False})
        self.assertEqual(response.status_code, 201, response.get_json())

        query = 'src_cloud_id={}&src_resource_path=/b/x&dst_cloud_id={}&dst_resource_path=/b/y'.format(
            self.aws, self.aws_east)
        result = self.client.get('/api/hashsum-jobs/egress-estimate/?' + query, headers=self.alice).get_json()
        self.assertEqual((result['kind'], result['bytes']), ('internet', 2 * TB))  # both sides are read
        self.app.config['CENTRAL_AWS_REGION'] = 'us-west-2'
        result = self.client.get('/api/hashsum-jobs/egress-estimate/?' + query, headers=self.alice).get_json()
        self.assertEqual((result['kind'], result['bytes'], result['high_usd']), ('inter-region', TB, 20.0))  # us-east-1 only
        result = self.client.get('/api/hashsum-jobs/egress-estimate/?src_cloud_id={}&src_resource_path=/b/x'.format(
            self.minio), headers=self.alice).get_json()
        self.assertEqual(result, {'applies': False})
        self.assertEqual(self.client.get('/api/hashsum-jobs/egress-estimate/?src_cloud_id={}&src_resource_path=/b'.format(
            self.aws), headers=self.bob).status_code, 404)


if __name__ == '__main__':
    unittest.main()
