"""
Temporary EC2 workers (managers/ec2_launcher.py, utils/ec2_config.py, the user data
template, routing to the EC2 pool). AWS is a botocore Stubber: no network. The worker
agent's MOTUZ_WORKER_RUN_AS and --once deadline are tested in test_workers.py. The parameters are checked against the IAM contract in
deployment/aws (central-launch-workers-policy.json, README "What the launcher must pass").
"""
import base64
import datetime
import io
import json
import logging
import os
import re
import subprocess
import unittest
from unittest import mock

import boto3
from botocore.stub import ANY, Stubber

from api.managers import ec2_launcher, job_routing, worker_manager
from api.models import CloudConnection, CopyJob, Ec2Worker, HashsumJob, RemoteJob, WorkerBootstrapToken
from api.models.worker import utcnow
from api.application import db
from api.utils import ec2_config

import test_workers as tw


REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
POLICY = os.path.join(REPO, 'deployment', 'aws', 'central-launch-workers-policy.json')
PUBLIC_URL = 'https://motuz.example.org'
TB = 1000 ** 4
GB = 1000 ** 3
# Parameters RunInstances must never get from the launcher (deployment/aws/README.md)
FORBIDDEN = ('ImageId', 'SecurityGroupIds', 'SecurityGroups', 'NetworkInterfaces', 'IamInstanceProfile', 'KeyName',
             'BlockDeviceMappings', 'MetadataOptions', 'Placement', 'InstanceMarketOptions',
             'InstanceInitiatedShutdownBehavior', 'DisableApiStop', 'DisableApiTermination')
TAG_FILTERS = [{'Name': 'tag:Project', 'Values': ['motuz']}, {'Name': 'tag:Component', 'Values': ['worker']},
               {'Name': 'tag:ManagedBy', 'Values': ['motuz-central']}]


def ec2_settings(**env):
    base = {'MOTUZ_EC2_WORKERS': 'true', 'MOTUZ_AWS_REGION': 'us-west-2', 'MOTUZ_WORKER_SOURCE_REF': 'a' * 40}
    base.update(env)
    return ec2_config.load_settings(base, local_job_pool='onprem', large_job_pool='aws', public_url=PUBLIC_URL)


class Capture:
    """Matches any value in Stubber's expected parameters and keeps it"""
    def __init__(self):
        self.values = []

    def __eq__(self, other):
        self.values.append(other)
        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return '<Capture>'


def instance(instance_id, state='running', instance_type='c7gn.large', launched=None, token=None, tags=None,
             reason=None):
    item = {
        'InstanceId': instance_id,
        'InstanceType': instance_type,
        'State': {'Name': state, 'Code': 16},
        'LaunchTime': launched or datetime.datetime.now(datetime.timezone.utc),
        'Tags': [{'Key': k, 'Value': v} for k, v in (tags or {'Project': 'motuz', 'Component': 'worker',
                                                                'ManagedBy': 'motuz-central'}).items()],
    }
    if token:
        item['ClientToken'] = token
    if reason:
        item['StateReason'] = {'Code': reason[0], 'Message': reason[1]}
    return item


def reservations(*instances):
    return {'Reservations': [{'Instances': list(instances)}] if instances else []}


class Ec2TestBase(tw.WorkerTestBase):

    def setUp(self):
        super().setUp()
        self.app.config.update(EC2=ec2_settings(), LARGE_JOB_POOL='aws', PUBLIC_URL=PUBLIC_URL)
        self.addCleanup(self.app.config.update, EC2=ec2_config.load_settings({}), LARGE_JOB_POOL='central',
                        PUBLIC_URL=None)
        self.ec2 = boto3.session.Session(region_name='us-west-2', aws_access_key_id='AKIATEST',
                                         aws_secret_access_key='test').client('ec2')
        self.stub = Stubber(self.ec2)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)
        patcher = mock.patch.object(ec2_launcher, 'client', return_value=self.ec2)
        patcher.start()
        self.addCleanup(patcher.stop)
        dispatch = mock.patch('api.tasks.ec2_dispatch.apply_async')
        self.dispatch_task = dispatch.start()
        self.addCleanup(dispatch.stop)
        self.sent = []
        send = mock.patch.object(worker_manager.Email, 'send_notification',
                                 side_effect=lambda **kw: self.sent.append(kw))
        send.start()
        self.addCleanup(send.stop)
        self.s3 = self.connection('alice', type='s3', s3_access_key_id='AKIAALICE', s3_secret_access_key='alice-secret')
        self.s3b = self.connection('alice', type='s3', s3_access_key_id='AKIAALICE2', s3_secret_access_key='alice-secret2')
        self.counter = 0

    def settings(self, **env):
        self.app.config['EC2'] = ec2_settings(**env)

    def big_job(self, size=500 * GB, job_type='copy'):
        if job_type == 'copy':
            job = CopyJob(owner='alice', description='big', src_cloud_id=self.s3.id, src_resource_path='bucket/a',
                          dst_cloud_id=self.s3b.id, dst_resource_path='bucket/b', copy_links=False,
                          notification_email='alice@example.org', progress_state='PROGRESS', progress_current=0,
                          progress_total=100)
        else:
            job = HashsumJob(owner='alice', src_cloud_id=self.s3.id, src_resource_path='bucket/a',
                             dst_cloud_id=self.s3b.id, dst_resource_path='bucket/b', option_download=False,
                             progress_state='PROGRESS')
        db.session.add(job)
        db.session.commit()
        worker_manager.queue_job(job_type, job, 'aws', job_routing.Route('aws', size, 10))
        return job

    def remote(self, job, job_type='copy'):
        return db.session.query(RemoteJob).filter_by(job_type=job_type, job_id=job.id).one()

    def next_id(self):
        self.counter += 1
        return 'i-{:017x}'.format(self.counter)

    def expect_describe(self, *instances):
        self.stub.add_response('describe_instances', reservations(*instances), {'Filters': TAG_FILTERS})

    def expect_launch(self, job, instance_type='c7gn.large', attempt=1, user_data=None, template='motuz-worker-arm64',
                      subnet=None, job_type='copy', instance_id=None):
        instance_id = instance_id or self.next_id()
        tag = '{}-{}'.format(job_type, job.id)
        tags = [{'Key': 'Project', 'Value': 'motuz'}, {'Key': 'Component', 'Value': 'worker'},
                {'Key': 'ManagedBy', 'Value': 'motuz-central'}, {'Key': 'Name', 'Value': 'motuz-worker-' + tag},
                {'Key': 'MotuzJob', 'Value': tag}]
        key = 'LaunchTemplateId' if template.startswith('lt-') else 'LaunchTemplateName'
        expected = {
            'LaunchTemplate': {key: template, 'Version': '$Default'},
            'InstanceType': instance_type,
            'MinCount': 1,
            'MaxCount': 1,
            'ClientToken': ec2_launcher.client_token(job_type, job.id, attempt),
            'UserData': user_data if user_data is not None else ANY,
            'TagSpecifications': [{'ResourceType': 'instance', 'Tags': tags}, {'ResourceType': 'volume', 'Tags': tags}],
        }
        if subnet:
            expected['SubnetId'] = subnet
        self.stub.add_response('run_instances', {'Instances': [
            instance(instance_id, 'pending', instance_type, token=expected['ClientToken'])]}, expected)
        return instance_id

    def row(self, job, attempt=1, job_type='copy'):
        return db.session.query(Ec2Worker).filter_by(job_type=job_type, job_id=job.id, attempt=attempt).one()

    def job_state(self, job, model=CopyJob):
        db.session.expire_all()
        return db.session.get(model, job.id)


class TestLaunch(Ec2TestBase):

    def test_parameters_are_exactly_the_iam_contract(self):
        job = self.big_job()
        self.expect_describe()
        user_data = Capture()
        self.expect_launch(job, user_data=user_data)
        launched = ec2_launcher.dispatch()
        self.stub.assert_no_pending_responses()
        self.assertEqual(len(launched), 1)
        row = self.row(job)
        self.assertEqual((row.state, row.instance_type, row.attempt), ('pending', 'c7gn.large', 1))
        self.assertTrue(row.instance_id.startswith('i-'))
        self.assertIsNotNone(row.launched_at)
        # The user data holds the job's bootstrap token, and only its hash is stored
        token = re.search(r'mzb1\.\d+\.[A-Za-z0-9_-]+', user_data.values[0]).group(0)
        stored = db.session.get(WorkerBootstrapToken, row.bootstrap_token_id)
        self.assertEqual(stored.bound_job, 'copy:{}'.format(job.id))
        self.assertEqual(stored.pool, 'aws')
        self.assertNotIn(token, json.dumps([stored.token_hash, row.client_token, row.last_error]))
        # The worker can exchange it for exactly this job
        answer = worker_manager.sign_in({'bootstrap_token': token})
        self.assertEqual(answer['worker']['bound_job'], 'copy:{}'.format(job.id))

    def test_parameters_against_the_policy_document(self):
        with open(POLICY) as f:
            policy = json.load(f)
        statements = {s['Sid']: s for s in policy['Statement']}
        arm = statements['RunInstancesWorkerInstanceArm64']['Condition']
        amd = statements['RunInstancesWorkerInstanceAmd64']['Condition']
        s = ec2_settings()
        row = Ec2Worker(job_type='copy', job_id=5, attempt=1, instance_type='c7gn.2xlarge', client_token='t')
        params = ec2_launcher.launch_params(s, row, '#!/bin/sh\n')
        for key in FORBIDDEN:
            self.assertNotIn(key, params)
        self.assertEqual(set(params), {'LaunchTemplate', 'InstanceType', 'MinCount', 'MaxCount', 'ClientToken',
                                       'UserData', 'TagSpecifications'})
        for spec in params['TagSpecifications']:
            tags = {t['Key']: t['Value'] for t in spec['Tags']}
            self.assertLessEqual(set(tags), set(arm['ForAllValues:StringEquals']['aws:TagKeys']))
            self.assertEqual(tags['Project'], arm['StringEquals']['aws:RequestTag/Project'])
            self.assertEqual(tags['Component'], arm['StringEquals']['aws:RequestTag/Component'])
        # Every default type is allowed by the policy for its template's architecture
        for rule in s.instance_types:
            allowed = arm if ec2_config.architecture(rule.instance_type) == 'arm64' else amd
            self.assertIn(rule.instance_type, allowed['StringEquals']['ec2:InstanceType'])
        # amd64 types use the other template; ids and a subnet are passed as given
        s = ec2_settings(MOTUZ_EC2_LAUNCH_TEMPLATE_AMD64='lt-052b24ff601c3130b', MOTUZ_EC2_SUBNET_ID='subnet-0123456789abcdef0')
        row.instance_type = 'c6in.xlarge'
        params = ec2_launcher.launch_params(s, row, '#!/bin/sh\n')
        self.assertEqual(params['LaunchTemplate'], {'LaunchTemplateId': 'lt-052b24ff601c3130b', 'Version': '$Default'})
        self.assertEqual(params['SubnetId'], 'subnet-0123456789abcdef0')
        self.assertIn('c6in.xlarge', amd['StringEquals']['ec2:InstanceType'])

    def test_instance_type_by_size(self):
        rules = ec2_settings().instance_types
        choose = ec2_config.choose_instance_type
        self.assertEqual(choose(rules, 300 * GB), 'c7gn.large')
        self.assertEqual(choose(rules, TB - 1), 'c7gn.large')
        self.assertEqual(choose(rules, TB), 'c7gn.2xlarge')
        self.assertEqual(choose(rules, 9 * TB), 'c7gn.2xlarge')
        self.assertEqual(choose(rules, 10 * TB), 'c7gn.4xlarge')
        self.assertEqual(choose(rules, 500 * TB), 'c7gn.4xlarge')
        self.assertEqual(choose(rules, None), 'c7gn.4xlarge') # the listing timed out: huge
        job = self.big_job(size=5 * TB)
        self.expect_describe()
        self.expect_launch(job, 'c7gn.2xlarge')
        ec2_launcher.dispatch()
        self.stub.assert_no_pending_responses()

    def test_hashsum_jobs_get_workers_too(self):
        job = self.big_job(job_type='hashsum')
        self.expect_describe()
        self.expect_launch(job, job_type='hashsum')
        ec2_launcher.dispatch()
        self.stub.assert_no_pending_responses()
        self.assertEqual(self.row(job, job_type='hashsum').state, 'pending')

    def test_client_token_is_per_job_attempt_and_installation(self):
        token = ec2_launcher.client_token('hashsum', 123456789, 2)
        self.assertLessEqual(len(token), 64)
        self.assertEqual(token, ec2_launcher.client_token('hashsum', 123456789, 2))
        self.assertNotEqual(token, ec2_launcher.client_token('copy', 123456789, 2))
        self.assertNotEqual(token, ec2_launcher.client_token('hashsum', 123456789, 1))
        with mock.patch.dict(self.app.config, SECRET_KEY='another-installation-secret-key-32b'):
            self.assertNotEqual(token, ec2_launcher.client_token('hashsum', 123456789, 2))
        self.assertNotIn(self.app.config['SECRET_KEY'], token)

    def test_a_job_attempt_is_launched_once(self):
        job = self.big_job()
        self.expect_describe()
        self.expect_launch(job)
        ec2_launcher.dispatch()
        # Another dispatcher (Celery task and reaper loop) finds the row: no second call
        self.expect_describe()
        self.assertEqual(ec2_launcher.dispatch(), [])
        self.assertIsNone(ec2_launcher.launch(self.remote(job))) # attempt 1 exists (unique row)
        self.stub.assert_no_pending_responses()
        self.assertEqual(db.session.query(Ec2Worker).count(), 1)

    def test_lost_response_is_adopted_by_client_token(self):
        # RunInstances succeeded but the answer was lost; the retry (same ClientToken,
        # new user data) gets IdempotentParameterMismatch and adopts that instance
        job = self.big_job()
        self.expect_describe()
        token = ec2_launcher.client_token('copy', job.id, 1)
        self.stub.add_client_error('run_instances', 'RequestLimitExceeded', 'slow down', 503)
        ec2_launcher.dispatch()
        row = self.row(job)
        self.assertEqual(row.state, 'launching')
        self.assertIn('RequestLimitExceeded', row.last_error)
        first_token = row.bootstrap_token_id
        # The reaper retries after RETRY_AFTER with the same ClientToken
        later = row.created_at + ec2_launcher.RETRY_AFTER + datetime.timedelta(seconds=1)
        self.expect_describe() # reaper: nothing with this token yet
        self.stub.add_client_error('run_instances', 'IdempotentParameterMismatch', 'token reused', 400,
                                   expected_params={'LaunchTemplate': ANY, 'InstanceType': 'c7gn.large', 'MinCount': 1,
                                                    'MaxCount': 1, 'ClientToken': token, 'UserData': ANY,
                                                    'TagSpecifications': ANY})
        self.stub.add_response('describe_instances', reservations(instance('i-0adopted0000000001', 'pending', token=token)),
                               {'Filters': [{'Name': 'client-token', 'Values': [token]}]})
        self.expect_describe(instance('i-0adopted0000000001', 'pending', token=token)) # dispatch after the reap
        summary = ec2_launcher.reap(now=later)
        self.stub.assert_no_pending_responses()
        row = self.row(job)
        self.assertEqual((row.instance_id, row.state), ('i-0adopted0000000001', 'pending'))
        self.assertEqual(len(summary['relaunched']), 1)
        # The first try's bootstrap token was invalidated
        self.assertLessEqual(db.session.get(WorkerBootstrapToken, first_token).expires_at, utcnow())

    def test_local_jobs_are_never_launched(self):
        job = CopyJob(owner='alice', src_cloud_id=None, src_resource_path='/home/alice/a', dst_cloud_id=self.s3.id,
                      dst_resource_path='bucket/b', progress_state='PROGRESS')
        db.session.add(job)
        db.session.commit()
        db.session.add(RemoteJob(job_type='copy', job_id=job.id, owner='alice', pool='aws', state='QUEUED'))
        db.session.commit()
        self.expect_describe()
        self.assertEqual(ec2_launcher.dispatch(), [])
        self.stub.assert_no_pending_responses()
        self.assertEqual(self.job_state(job).progress_state, 'FAILED')
        self.assertIn('local path', self.job_state(job).progress_error)

    def test_permanent_error_fails_the_job(self):
        job = self.big_job()
        self.expect_describe()
        self.stub.add_client_error('run_instances', 'UnauthorizedOperation', 'You are not authorized', 403)
        ec2_launcher.dispatch()
        self.assertEqual(self.row(job).state, 'failed')
        stored = self.job_state(job)
        self.assertEqual(stored.progress_state, 'FAILED')
        self.assertIn('Could not start an EC2 worker: UnauthorizedOperation', stored.progress_error)
        self.assertEqual(self.remote(job).state, 'DONE')
        self.assertTrue(any('FAILED' in m['subject'] for m in self.sent))


class TestCapacityFallback(Ec2TestBase):

    def test_next_type_once_on_insufficient_capacity(self):
        job = self.big_job(size=5 * TB) # c7gn.2xlarge, then the next entry c7gn.4xlarge
        self.expect_describe()
        self.stub.add_client_error('run_instances', 'InsufficientInstanceCapacity', 'no capacity', 500)
        self.expect_launch(job, 'c7gn.4xlarge', attempt=2)
        ec2_launcher.dispatch()
        self.stub.assert_no_pending_responses()
        self.assertEqual(self.row(job, 1).state, 'failed')
        self.assertEqual((self.row(job, 2).state, self.row(job, 2).instance_type), ('pending', 'c7gn.4xlarge'))
        self.assertEqual(self.job_state(job).progress_state, 'PROGRESS')

    def test_second_failure_fails_the_job(self):
        job = self.big_job(size=50 * TB) # the last type: fallback is the one before it
        self.expect_describe()
        self.stub.add_client_error('run_instances', 'InsufficientInstanceCapacity', 'no capacity', 500)
        self.stub.add_client_error('run_instances', 'InsufficientInstanceCapacity', 'no capacity either', 500,
                                   expected_params={'LaunchTemplate': ANY, 'InstanceType': 'c7gn.2xlarge', 'MinCount': 1,
                                                    'MaxCount': 1, 'ClientToken': ANY, 'UserData': ANY,
                                                    'TagSpecifications': ANY})
        ec2_launcher.dispatch()
        self.stub.assert_no_pending_responses()
        stored = self.job_state(job)
        self.assertEqual(stored.progress_state, 'FAILED')
        self.assertIn('No EC2 capacity for c7gn.2xlarge', stored.progress_error)
        self.assertEqual(db.session.query(Ec2Worker).filter_by(state='failed').count(), 2)

    def test_instance_that_died_of_capacity_before_the_claim(self):
        job = self.big_job()
        self.expect_describe()
        instance_id = self.expect_launch(job)
        ec2_launcher.dispatch()
        row = self.row(job)
        dead = instance(instance_id, 'terminated', token=row.client_token,
                        reason=('Server.InsufficientInstanceCapacity', 'Insufficient capacity.'))
        self.expect_describe(dead)
        new_id = self.expect_launch(job, 'c7gn.2xlarge', attempt=2)
        self.expect_describe(dead, instance(new_id, 'pending'))
        summary = ec2_launcher.reap(now=utcnow() + datetime.timedelta(minutes=2))
        self.stub.assert_no_pending_responses()
        self.assertEqual(self.row(job, 1).state, 'terminated')
        self.assertEqual(self.row(job, 2).instance_id, new_id)
        self.assertEqual(len(summary['relaunched']), 1)
        self.assertEqual(self.job_state(job).progress_state, 'PROGRESS')
        # The second one dies too: the job fails, with the reason and where to look
        dead2 = instance(new_id, 'terminated', 'c7gn.2xlarge', reason=('Server.InsufficientInstanceCapacity', 'again'))
        self.expect_describe(dead, dead2)
        self.expect_describe(dead, dead2)
        ec2_launcher.reap(now=utcnow() + datetime.timedelta(minutes=4))
        stored = self.job_state(job)
        self.assertEqual(stored.progress_state, 'FAILED')
        self.assertIn('ended before it started the job (Server.InsufficientInstanceCapacity', stored.progress_error)
        self.assertIn('get-console-output --instance-id ' + new_id, stored.progress_error)

    def test_fallback_order(self):
        rules = ec2_config.parse_instance_types('1T:c7gn.large,10T:c7gn.2xlarge,*:c7gn.4xlarge')
        self.assertEqual(ec2_config.fallback_instance_type(rules, 'c7gn.large'), 'c7gn.2xlarge')
        self.assertEqual(ec2_config.fallback_instance_type(rules, 'c7gn.2xlarge'), 'c7gn.4xlarge')
        self.assertEqual(ec2_config.fallback_instance_type(rules, 'c7gn.4xlarge'), 'c7gn.2xlarge')
        self.assertIsNone(ec2_config.fallback_instance_type(ec2_config.parse_instance_types('*:c7gn.large'), 'c7gn.large'))


class TestLimits(Ec2TestBase):

    def test_at_most_max_workers(self):
        jobs = [self.big_job() for _ in range(3)]
        self.expect_describe()
        ids = [self.expect_launch(job) for job in jobs[:2]]
        self.assertEqual(len(ec2_launcher.dispatch()), 2)
        self.stub.assert_no_pending_responses()
        self.assertEqual(self.remote(jobs[2]).state, 'QUEUED')
        self.assertEqual(db.session.query(Ec2Worker).filter_by(job_id=jobs[2].id).count(), 0)
        worker_manager.annotate_location('copy', jobs)
        self.assertEqual([j.pool for j in jobs], ['aws'] * 3)
        self.assertEqual(jobs[0].pool_status, 'starting worker (c7gn.large)')
        self.assertEqual(jobs[2].pool_status, 'waiting for a free EC2 worker slot')

        # The first job ends; its worker shuts itself down; the reaper records that and
        # the third job gets the free slot
        self.remote(jobs[0]).state = 'DONE'
        db.session.get(CopyJob, jobs[0].id).progress_state = 'SUCCESS'
        db.session.commit()
        running = [instance(ids[0], 'terminated'), instance(ids[1], 'running')]
        self.expect_describe(*running) # reaper
        self.expect_describe(*running) # launcher
        third = self.expect_launch(jobs[2])
        summary = ec2_launcher.reap()
        self.stub.assert_no_pending_responses()
        self.assertEqual([row.instance_id for row in summary['launched']], [third])
        self.assertEqual(self.job_state(jobs[0]).progress_state, 'SUCCESS') # not touched
        self.assertIsNotNone(self.row(jobs[0]).terminated_at)

    def test_unknown_running_workers_count(self):
        self.settings(MOTUZ_EC2_MAX_WORKERS='1')
        self.big_job()
        self.expect_describe(instance('i-0f00000000000000f', 'running'))
        self.assertEqual(ec2_launcher.dispatch(), [])
        self.stub.assert_no_pending_responses()

    def test_no_launch_when_ec2_cannot_be_listed(self):
        self.big_job()
        self.stub.add_client_error('describe_instances', 'UnauthorizedOperation', 'no', 403)
        self.assertEqual(ec2_launcher.dispatch(), [])
        self.stub.assert_no_pending_responses()


class TestReaper(Ec2TestBase):

    def launched(self, **kw):
        job = self.big_job(**kw)
        self.expect_describe()
        instance_id = self.expect_launch(job)
        ec2_launcher.dispatch()
        return job, instance_id

    def claim(self, job):
        token_row = db.session.get(WorkerBootstrapToken, self.row(job).bootstrap_token_id)
        worker = tw.Worker(name='aws-ephemeral-x', pool='aws', ephemeral=True, bound_job='copy:{}'.format(job.id))
        db.session.add(worker)
        db.session.commit()
        token_row.used_at = utcnow()
        db.session.commit()
        return self.claim_as(worker)

    def claim_as(self, worker):
        return worker_manager.claim(worker, {'pool': 'aws'})

    def expect_terminate(self, instance_id):
        self.stub.add_response('terminate_instances', {'TerminatingInstances': [{
            'InstanceId': instance_id, 'CurrentState': {'Name': 'shutting-down', 'Code': 32},
            'PreviousState': {'Name': 'running', 'Code': 16}}]}, {'InstanceIds': [instance_id]})

    def test_worker_of_a_finished_job_is_terminated(self):
        job, instance_id = self.launched()
        ticket = self.claim(job)
        remote = self.remote(job)
        remote_worker = db.session.get(tw.Worker, remote.worker_id)
        worker_manager.finish(remote_worker, ticket['ticket_id'], ticket['ticket_token'], {'state': 'SUCCESS'})
        self.expect_describe(instance(instance_id, 'running'))
        self.expect_terminate(instance_id)
        self.expect_describe(instance(instance_id, 'shutting-down'))
        summary = ec2_launcher.reap()
        self.stub.assert_no_pending_responses()
        self.assertEqual(summary['terminated'], [instance_id])
        self.assertEqual(self.job_state(job).progress_state, 'SUCCESS')
        self.assertEqual(self.row(job).state, 'shutting-down')
        worker_manager.annotate_location('copy', [job])
        self.assertEqual((job.pool, job.pool_status), ('aws', 'ran on c7gn.large'))

    def test_running_status(self):
        job, instance_id = self.launched()
        self.claim(job)
        stored = db.session.get(CopyJob, job.id)
        worker_manager.annotate_location('copy', [stored])
        self.assertEqual(stored.pool_status, 'running on c7gn.large')

    def test_max_runtime(self):
        job, instance_id = self.launched()
        self.claim(job)
        later = self.row(job).launched_at + datetime.timedelta(hours=24, minutes=1)
        self.expect_describe(instance(instance_id, 'running'))
        self.expect_terminate(instance_id)
        self.expect_describe(instance(instance_id, 'shutting-down'))
        with mock.patch.object(worker_manager, 'utcnow', return_value=later):
            summary = ec2_launcher.reap(now=later)
        self.stub.assert_no_pending_responses()
        self.assertEqual(summary['failed_jobs'], ['copy:{}'.format(job.id)])
        stored = self.job_state(job)
        self.assertEqual(stored.progress_state, 'FAILED')
        self.assertIn('reached the maximum runtime of 24h (MOTUZ_EC2_MAX_RUNTIME) and was terminated', stored.progress_error)
        self.assertEqual(self.remote(job).state, 'DONE')
        # The ephemeral worker is revoked with its job
        self.assertIsNotNone(db.session.get(tw.Worker, self.remote(job).worker_id).revoked_at)

    def test_boot_timeout(self):
        job, instance_id = self.launched()
        later = self.row(job).launched_at + datetime.timedelta(minutes=21)
        self.expect_describe(instance(instance_id, 'running'))
        self.expect_terminate(instance_id)
        self.expect_describe(instance(instance_id, 'shutting-down'))
        ec2_launcher.reap(now=later)
        self.stub.assert_no_pending_responses()
        stored = self.job_state(job)
        self.assertEqual(stored.progress_state, 'FAILED')
        self.assertIn('did not start the job within 20m', stored.progress_error)
        # Its bootstrap token no longer works
        self.assertLessEqual(db.session.get(WorkerBootstrapToken, self.row(job).bootstrap_token_id).expires_at, utcnow())

    def test_booting_worker_is_left_alone(self):
        job, instance_id = self.launched()
        self.expect_describe(instance(instance_id, 'running'))
        self.expect_describe(instance(instance_id, 'running'))
        summary = ec2_launcher.reap(now=utcnow() + datetime.timedelta(minutes=5))
        self.stub.assert_no_pending_responses()
        self.assertEqual(summary['terminated'], [])
        self.assertEqual(self.row(job).state, 'running')
        self.assertEqual(self.job_state(job).progress_state, 'PROGRESS')

    def test_stopped_worker_is_terminated(self):
        job, instance_id = self.launched()
        self.expect_describe(instance(instance_id, 'stopped'))
        self.expect_terminate(instance_id)
        self.expect_describe(instance(instance_id, 'shutting-down'))
        ec2_launcher.reap()
        self.stub.assert_no_pending_responses()
        self.assertIn('stopped instead of terminating', self.job_state(job).progress_error)

    def test_worker_died_while_running(self):
        job, instance_id = self.launched()
        self.claim(job)
        gone = instance(instance_id, 'terminated', reason=('Server.SpotInstanceTermination', 'x'))
        self.expect_describe(gone)
        self.expect_describe(gone)
        ec2_launcher.reap()
        stored = self.job_state(job)
        self.assertEqual(stored.progress_state, 'FAILED')
        self.assertIn('ended while the job was running', stored.progress_error)

    def test_worker_shut_down_before_its_claim(self):
        job, instance_id = self.launched()
        gone = instance(instance_id, 'terminated', reason=('Client.InstanceInitiatedShutdown', 'shutdown'))
        self.expect_describe(gone)
        self.expect_describe(gone)
        ec2_launcher.reap()
        stored = self.job_state(job)
        self.assertEqual(stored.progress_state, 'FAILED')
        self.assertIn('ended before it started the job (Client.InstanceInitiatedShutdown', stored.progress_error)
        self.assertEqual(db.session.query(Ec2Worker).count(), 1) # no fallback: not a capacity problem

    def test_unknown_workers_are_terminated_after_a_grace_period(self):
        old = instance('i-0unknown00000000a', 'running',
                       launched=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30))
        young = instance('i-0unknown00000000b', 'running')
        gone = instance('i-0unknown00000000c', 'terminated',
                        launched=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30))
        self.expect_describe(old, young, gone)
        self.expect_terminate('i-0unknown00000000a')
        self.expect_describe(old, young, gone)
        summary = ec2_launcher.reap()
        self.stub.assert_no_pending_responses()
        self.assertEqual(summary['terminated'], ['i-0unknown00000000a'])

    def test_stopping_a_queued_job_terminates_its_booting_worker(self):
        job, instance_id = self.launched()
        worker_manager.request_stop('copy', job.id)
        self.expect_describe(instance(instance_id, 'running'))
        self.expect_terminate(instance_id)
        self.expect_describe(instance(instance_id, 'shutting-down'))
        ec2_launcher.reap()
        self.stub.assert_no_pending_responses()

    def test_a_failing_terminate_is_retried_next_time(self):
        job, instance_id = self.launched()
        worker_manager.request_stop('copy', job.id)
        self.expect_describe(instance(instance_id, 'running'))
        self.stub.add_client_error('terminate_instances', 'RequestLimitExceeded', 'slow', 503)
        self.expect_describe(instance(instance_id, 'running'))
        ec2_launcher.reap()
        self.assertEqual(self.row(job).state, 'running')
        self.expect_describe(instance(instance_id, 'running'))
        self.expect_terminate(instance_id)
        self.expect_describe(instance(instance_id, 'shutting-down'))
        ec2_launcher.reap()
        self.stub.assert_no_pending_responses()


class TestDisabled(Ec2TestBase):

    def test_nothing_happens_when_off(self):
        self.app.config['EC2'] = ec2_config.load_settings({})
        self.big_job()
        self.dispatch_task.assert_not_called()
        self.assertEqual(ec2_launcher.dispatch(), [])
        self.assertEqual(ec2_launcher.reap(), {'terminated': [], 'failed_jobs': [], 'relaunched': [], 'launched': []})
        ec2_launcher.client.assert_not_called()

    def test_queueing_starts_the_launcher_task(self):
        self.big_job()
        self.dispatch_task.assert_called_once_with()
        # Other pools never do
        self.dispatch_task.reset_mock()
        worker_manager.queue_job('copy', self.big_job_without_queue(), 'onprem')
        self.dispatch_task.assert_not_called()

    def big_job_without_queue(self):
        job = CopyJob(owner='alice', src_cloud_id=None, src_resource_path='/a', dst_cloud_id=None,
                      dst_resource_path='/b', progress_state='PROGRESS')
        db.session.add(job)
        db.session.commit()
        return job


class TestUserData(Ec2TestBase):

    def build(self, **env):
        s = ec2_settings(**env)
        token = 'mzb1.42.' + 'T' * 43
        return token, ec2_launcher.build_user_data(s, token, 'copy:7', PUBLIC_URL)

    def test_size_secrets_and_order(self):
        token, script = self.build()
        self.assertLess(len(script.encode()), 16 * 1024)
        self.assertLess(len(base64.b64encode(script.encode())), 22 * 1024) # < 16 KB before encoding
        self.assertEqual(script.count(token), 1)
        self.assertEqual(len(re.findall(r'mzb1\.', script)), 1)
        for secret in (self.app.config['SECRET_KEY'], os.environ['MOTUZ_FLASK_SECRET_KEY'], 'alice-secret',
                       'AKIAALICE', self.app.config['SQLALCHEMY_DATABASE_URI']):
            self.assertNotIn(secret, script)
        self.assertNotRegex(script, r'(?m)^\s*set -[a-z]*x')
        self.assertNotIn('@@', script)
        # The token is in a quoted here-document of its own, never echoed
        self.assertIn("<<'MOTUZ_BOOTSTRAP_TOKEN'\n{}\nMOTUZ_BOOTSTRAP_TOKEN\n".format(token), script)
        # IMDS is restricted before anything is downloaded or any account created
        lines = script.split('\n')
        first = lambda pattern: next(i for i, line in enumerate(lines) if re.search(pattern, line))
        imds = first(r'^imds_root_only \|\|')
        self.assertLess(imds, first(r'curl -fsS'))
        self.assertLess(imds, first(r'useradd'))
        self.assertLess(imds, first(r'shutdown -h "\+\$MAX_RUNTIME_MINUTES"'))
        self.assertIn('meta skuid != 0 reject', script)
        self.assertIn('-m owner ! --uid-owner 0 -j REJECT', script)
        # Values
        self.assertIn("CENTRAL_URL={}\n".format(PUBLIC_URL), script)
        self.assertIn("SOURCE_REF={}\n".format('a' * 40), script)
        self.assertIn("MAX_RUNTIME_MINUTES={}\n".format(24 * 60 + ec2_launcher.RUNTIME_GRACE_MINUTES), script)
        self.assertIn("ONCE_WAIT=1200\n", script)
        self.assertIn("RCLONE_VERSION=1.75.1\n", script)
        self.assertIn('MOTUZ_WORKER_RUN_AS=motuzjob', script)
        self.assertIn('motuz ALL=(motuzjob) NOPASSWD:SETENV: /usr/local/bin/rclone', script)
        self.assertIn('--once', script)

    def test_shell_syntax(self):
        _, script = self.build()
        result = subprocess.run(['bash', '-n'], input=script, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_values_are_validated(self):
        s = ec2_settings()
        for token in ('mzb1.1.short', 'x; rm -rf /', "mzb1.1.{}'".format('a' * 30)):
            with self.assertRaises(ec2_launcher.Ec2Error):
                ec2_launcher.build_user_data(s, token, 'copy:7', PUBLIC_URL)
        for url in ('http://motuz.example.org', 'https://motuz.example.org/$(id)', "https://a'b"):
            with self.assertRaises(ec2_launcher.Ec2Error):
                ec2_launcher.build_user_data(s, 'mzb1.1.' + 'a' * 43, 'copy:7', url)

    def test_rclone_pin_matches_the_dockerfile(self):
        with open(os.path.join(REPO, 'deployment', 'docker', 'app', 'Dockerfile')) as f:
            dockerfile = f.read()
        self.assertIn('ARG RCLONE_VERSION={}\n'.format(ec2_config.RCLONE_VERSION), dockerfile)
        self.assertIn('ARG RCLONE_SHA256_AMD64={}\n'.format(ec2_config.RCLONE_SHA256['amd64']), dockerfile)
        self.assertIn('ARG RCLONE_SHA256_ARM64={}\n'.format(ec2_config.RCLONE_SHA256['arm64']), dockerfile)

    def test_the_token_is_never_logged(self):
        job = self.big_job()
        self.expect_describe()
        user_data = Capture()
        self.expect_launch(job, user_data=user_data)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        logging.disable(logging.NOTSET)
        try:
            ec2_launcher.dispatch()
        finally:
            logging.disable(logging.WARNING)
            root.removeHandler(handler)
            root.setLevel(previous)
        token = re.search(r'mzb1\.\d+\.[A-Za-z0-9_-]+', user_data.values[0]).group(0)
        output = stream.getvalue()
        self.assertIn('launched for job copy:{}'.format(job.id), output)
        self.assertNotIn(token, output)
        self.assertNotIn(base64.b64encode(user_data.values[0].encode()).decode()[:200], output)
        for name in ('botocore', 'boto3', 'urllib3'):
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.INFO)


class TestSettings(unittest.TestCase):

    def test_off_by_default(self):
        s = ec2_config.load_settings({})
        self.assertFalse(s.enabled)
        self.assertEqual((s.pool, s.max_workers, s.max_runtime, s.boot_timeout), ('aws', 2, 24 * 3600, 1200))
        self.assertEqual([r.instance_type for r in s.instance_types], ['c7gn.large', 'c7gn.2xlarge', 'c7gn.4xlarge'])
        self.assertEqual(s.source_url, 'https://github.com/dirkpetersen/motuz')

    def test_enabled_needs_a_consistent_configuration(self):
        good = {'MOTUZ_EC2_WORKERS': 'true', 'MOTUZ_AWS_REGION': 'us-west-2', 'MOTUZ_SOURCE_COMMIT': 'b' * 40}
        s = ec2_config.load_settings(good, large_job_pool='aws', public_url=PUBLIC_URL)
        self.assertEqual(s.source_ref, 'b' * 40) # the image's commit
        cases = [
            (dict(good, MOTUZ_AWS_REGION=''), {}, 'MOTUZ_AWS_REGION'),
            (good, {'large_job_pool': 'central'}, 'MOTUZ_LARGE_JOB_POOL'),
            (good, {'local_job_pool': 'aws'}, 'MOTUZ_LOCAL_JOB_POOL'),
            (good, {'public_url': None}, 'MOTUZ_PUBLIC_URL'),
            (good, {'public_url': 'http://motuz.example.org'}, 'MOTUZ_PUBLIC_URL'),
            (dict(good, MOTUZ_SOURCE_COMMIT=''), {}, 'MOTUZ_WORKER_SOURCE_REF'),
        ]
        for env, kwargs, variable in cases:
            args = dict({'large_job_pool': 'aws', 'public_url': PUBLIC_URL}, **kwargs)
            with self.assertRaisesRegex(ec2_config.Ec2ConfigError, variable):
                ec2_config.load_settings(env, **args)

    def test_invalid_values_name_the_variable(self):
        for variable, value in (('MOTUZ_EC2_WORKERS', 'maybe'), ('MOTUZ_EC2_MAX_WORKERS', '0'),
                                ('MOTUZ_EC2_MAX_WORKERS', 'two'), ('MOTUZ_EC2_MAX_RUNTIME', '1m'),
                                ('MOTUZ_EC2_MAX_RUNTIME', 'forever'), ('MOTUZ_EC2_INSTANCE_TYPES', '1T:c7gn.large,1T:x.y'),
                                ('MOTUZ_EC2_INSTANCE_TYPES', '*:c7gn.large,1T:c7gn.xlarge'),
                                ('MOTUZ_EC2_INSTANCE_TYPES', 'c7gn.large'), ('MOTUZ_EC2_SUBNET_ID', 'sn-1'),
                                ('MOTUZ_EC2_LAUNCH_TEMPLATE_ARM64', 'a b'), ('MOTUZ_AWS_REGION', 'mars'),
                                ('MOTUZ_WORKER_SOURCE', 'http://github.com/x/y'),
                                ('MOTUZ_WORKER_SOURCE', 'https://github.com/x/y?ref=1'),
                                ('MOTUZ_WORKER_SOURCE_REF', '$(id)'), ('MOTUZ_EC2_POOL', 'central'),
                                ('MOTUZ_EC2_LAUNCH_TEMPLATE_VERSION', 'latest')):
            with self.assertRaisesRegex(ec2_config.Ec2ConfigError, variable):
                ec2_config.load_settings({variable: value})
        with self.assertRaisesRegex(ec2_config.Ec2ConfigError, 'MOTUZ_EC2_BOOT_TIMEOUT'):
            ec2_config.load_settings({'MOTUZ_EC2_MAX_RUNTIME': '20m', 'MOTUZ_EC2_BOOT_TIMEOUT': '30m'})

    def test_instance_type_table(self):
        rules = ec2_config.parse_instance_types('500G:c7gn.large, 2T:c6in.xlarge, 20T:c8gn.4xlarge')
        self.assertEqual(rules[0], ec2_config.InstanceTypeRule(500 * GB, 'c7gn.large'))
        self.assertEqual(rules[-1], ec2_config.InstanceTypeRule(None, 'c8gn.4xlarge')) # also larger jobs
        self.assertEqual([ec2_config.architecture(t) for t in ('c7gn.large', 'c8gn.xlarge', 't4g.small', 'm7gd.large',
                                                               'c6in.large', 'c7i.2xlarge', 'm5.large')],
                         ['arm64', 'arm64', 'arm64', 'arm64', 'amd64', 'amd64', 'amd64'])


class TestRouting(unittest.TestCase):
    CONFIG = {'LOCAL_JOB_POOL': 'onprem', 'LARGE_JOB_POOL': 'aws', 'LARGE_JOB_BYTES': 300 * GB,
              'LARGE_JOB_FILES': 50000, 'JOB_SIZE_TIMEOUT': 60, 'EC2': ec2_settings()}

    def connection(self, **fields):
        return CloudConnection(owner='alice', **fields)

    def test_size_is_kept(self):
        s3 = self.connection(type='s3')
        route = job_routing.route(s3, 'b', s3, self.CONFIG, mock.Mock(return_value=(5 * TB, 7)))
        self.assertEqual(route, job_routing.Route('aws', 5 * TB, 7))
        route = job_routing.route(s3, 'b', s3, self.CONFIG, mock.Mock(return_value=(float('inf'), float('inf'))))
        self.assertEqual(route, job_routing.Route('aws', None, None)) # the listing timed out

    def test_only_https_apis_with_stored_credentials(self):
        big = mock.Mock(return_value=(TB, 1))
        s3 = self.connection(type='s3')
        for other, pool in ((self.connection(type='azureblob'), 'aws'),
                            (self.connection(type='drive'), 'aws'),
                            (self.connection(type='s3', s3_endpoint='https://s3.wasabisys.com'), 'aws'),
                            (self.connection(type='s3', s3_endpoint='s3.wasabisys.com'), 'aws'),
                            (self.connection(type='s3', s3_endpoint='http://minio.local'), 'central'),
                            (self.connection(type='s3', s3_endpoint='https://minio.local:9000'), 'central'),
                            (self.connection(type='sftp'), 'central'),
                            (self.connection(type='webdav'), 'central'),
                            (self.connection(type='swift'), 'central'),
                            (self.connection(type='s3', subtype='profile'), 'central')):
            self.assertEqual(job_routing.choose_pool(s3, 'b', other, self.CONFIG, big), pool, (other.type, other.s3_endpoint))
            self.assertEqual(job_routing.choose_pool(other, 'b', s3, self.CONFIG, big), pool)

    def test_without_ec2_workers_the_pool_is_plain(self):
        config = dict(self.CONFIG, EC2=ec2_config.load_settings({}))
        self.assertEqual(job_routing.choose_pool(self.connection(type='sftp'), 'b', self.connection(type='s3'), config,
                                                 mock.Mock(return_value=(TB, 1))), 'aws')

    def test_local_jobs_never_go_to_the_ec2_pool(self):
        config = dict(self.CONFIG, LOCAL_JOB_POOL='aws')
        with self.assertRaises(ValueError):
            job_routing.choose_pool(None, '/a', self.connection(type='s3'), config)


if __name__ == '__main__':
    unittest.main()
