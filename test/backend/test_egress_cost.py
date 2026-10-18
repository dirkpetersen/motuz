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

    def test_kind(self):
        aws_west, aws_east = s3(s3_provider='AWS'), s3(s3_provider='AWS', s3_region='us-east-1')
        minio = s3(s3_provider='Minio', s3_endpoint='https://minio.example')
        azure = types.SimpleNamespace(type='azureblob', subtype=None)
        self.assertEqual(egress_cost.kind(aws_west, None), egress_cost.INTERNET)
        self.assertEqual(egress_cost.kind(aws_west, azure), egress_cost.INTERNET)
        self.assertEqual(egress_cost.kind(aws_west, minio), egress_cost.INTERNET)
        self.assertEqual(egress_cost.kind(aws_west, s3(s3_provider='AWS')), egress_cost.SAME_REGION)
        self.assertEqual(egress_cost.kind(aws_west, aws_east), egress_cost.INTER_REGION)
        self.assertEqual(egress_cost.kind(s3(s3_region=None), aws_east), egress_cost.SAME_REGION)
        unknown = s3(subtype='profile', profile_source='aws', s3_provider='AWS', s3_region=None)
        self.assertEqual(egress_cost.kind(aws_west, unknown), egress_cost.INTER_REGION)
        self.assertIsNone(egress_cost.kind(None, aws_west))
        self.assertIsNone(egress_cost.kind(minio, None))


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
        e = egress_cost.estimate(egress_cost.INTERNET, 3.4 * TB, 12)
        self.assertEqual((e['low_usd'], e['high_usd']), (170.0, 306.0))
        self.assertEqual((e['bytes'], e['files'], e['free_gb_per_month']), (3400000000000, 12, 100))
        self.assertIn('$0.05-$0.09 per GB', e['note'])
        self.assertIn('egress waiver', e['note'])
        one_tb = egress_cost.estimate(egress_cost.INTERNET, TB)
        self.assertEqual((one_tb['low_usd'], one_tb['high_usd']), (50.0, 90.0))

    def test_inter_region_and_same_region(self):
        e = egress_cost.estimate(egress_cost.INTER_REGION, TB)
        self.assertEqual((e['low_usd'], e['high_usd'], e['rate_low_per_gb']), (20.0, 20.0, 0.02))
        self.assertIn('not known', egress_cost.estimate(egress_cost.INTER_REGION, TB, region_known=False)['note'])
        same = egress_cost.estimate(egress_cost.SAME_REGION, TB)
        self.assertEqual(same['high_usd'], 0.0)
        self.assertFalse(egress_cost.needs_confirmation(egress_cost.SAME_REGION))
        self.assertEqual(egress_cost.estimate(None), {'applies': False})

    def test_unknown_size(self):
        e = egress_cost.estimate(egress_cost.INTERNET, None)
        self.assertTrue(e['applies'])
        self.assertIsNone(e['bytes'])
        self.assertIsNone(e['low_usd'])
        self.assertEqual(egress_cost.estimate(egress_cost.INTERNET, float('nan'))['bytes'], None)
        self.assertEqual(egress_cost.estimate(egress_cost.INTERNET, -5)['bytes'], None)
        self.assertEqual(egress_cost.estimate(egress_cost.INTERNET, True)['bytes'], None)

    def test_tiered(self):
        GB = egress_cost.GB
        self.assertEqual(egress_cost.tiered_usd(50 * GB), 0)
        self.assertAlmostEqual(egress_cost.tiered_usd(1100 * GB), 90.0)
        self.assertAlmostEqual(egress_cost.tiered_usd(20100 * GB), 10000 * 0.09 + 10000 * 0.085)
        self.assertAlmostEqual(egress_cost.tiered_usd(200100 * GB),
                               10000 * 0.09 + 40000 * 0.085 + 100000 * 0.07 + 50000 * 0.05)

    def test_confirmed_recomputes(self):
        stored = egress_cost.confirmed(egress_cost.INTERNET, {
            'confirmed': True, 'bytes': TB, 'files': 3, 'low_usd': 0.01, 'kind': 'same-region', 'x': 1})
        self.assertEqual(stored['kind'], egress_cost.INTERNET)
        self.assertEqual((stored['low_usd'], stored['high_usd']), (50.0, 90.0))
        self.assertTrue(stored['confirmed'])
        self.assertNotIn('x', stored)
        self.assertIsNone(egress_cost.confirmed(egress_cost.INTERNET, {'confirmed': 'yes'}))
        self.assertIsNone(egress_cost.confirmed(egress_cost.INTERNET, None))
        self.assertIsNone(egress_cost.confirmed(egress_cost.INTERNET, {'bytes': TB}))

    def test_stdlib_only(self):
        # api.utils must not import the app or config (the remote worker agent)
        with open(egress_cost.__file__) as f:
            imports = [line for line in f if line.startswith(('import ', 'from '))]
        self.assertEqual(imports, ['import math\n', 'from . import s3_providers\n'])


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

    def test_regions(self):
        body = self.estimate(self.aws, ['/b/x'], self.aws_west2).get_json()
        self.assertEqual(body['kind'], 'same-region')
        self.assertEqual(self.measured, [])  # free: no rclone size
        body = self.estimate(self.aws, ['/b/x'], self.aws_east).get_json()
        self.assertEqual((body['kind'], body['high_usd']), ('inter-region', 20.0))
        body = self.estimate(self.aws, ['/b/x'], self.minio).get_json()
        self.assertEqual(body['kind'], 'internet')

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
        for src, dst in ((0, 0), (self.minio, 0), (self.aws, self.aws_west2)):
            response = self.job(src, dst)
            self.assertEqual(response.status_code, 201, response.get_json())
            self.assertIsNone(response.get_json()['egress_estimate'])

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
        result = self.client.get('/api/hashsum-jobs/egress-estimate/?src_cloud_id={}&src_resource_path=/b/x'.format(
            self.minio), headers=self.alice).get_json()
        self.assertEqual(result, {'applies': False})
        self.assertEqual(self.client.get('/api/hashsum-jobs/egress-estimate/?src_cloud_id={}&src_resource_path=/b'.format(
            self.aws), headers=self.bob).status_code, 404)


if __name__ == '__main__':
    unittest.main()
