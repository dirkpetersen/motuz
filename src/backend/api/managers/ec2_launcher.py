"""
Temporary EC2 workers (README "Temporary EC2 workers", deployment/aws/README.md).

With MOTUZ_EC2_WORKERS=true every job queued for the EC2 pool (MOTUZ_EC2_POOL, default
'aws'; job_routing sends large cloud-to-cloud jobs there) gets its own instance:

- dispatch() (Celery task ec2_dispatch right after the job is queued, and every reaper
  run) launches instances for queued jobs of the pool while fewer than
  MOTUZ_EC2_MAX_WORKERS are active; other jobs wait in the queue. Each launch creates a
  single-use bootstrap token bound to the job (worker_manager.create_bootstrap_token),
  puts it in the user data (api/templates/ec2/worker-user-data.sh) and calls RunInstances
  with exactly the parameters the central node's IAM policy allows: the launch template
  of the instance type's architecture, the instance type from MOTUZ_EC2_INSTANCE_TYPES
  (by the size of the source), one instance, an optional subnet, the user data, the
  worker tags and a ClientToken derived from the job and attempt, so that a repeated
  call never starts a second instance for the same attempt. One row per attempt in
  ec2_worker (models/ec2_worker.py).
- reap() (manage.py ec2 reap --loop, started by the Celery container) terminates
  instances whose job ended, that run longer than MOTUZ_EC2_MAX_RUNTIME, that did not
  start their job within MOTUZ_EC2_BOOT_TIMEOUT, that stopped instead of terminating, or
  that carry the worker tags but are not in ec2_worker; fails the jobs of instances that
  died (with the reason), and tries the next instance type of the table once when an
  instance could not be started for lack of capacity.

The worker on the instance runs `motuz_worker.py --bootstrap-token-file ... --once`
and shuts the instance down when it exits (the launch templates set
InstanceInitiatedShutdownBehavior=terminate), with `shutdown -h +<max runtime>` as a
backstop. It runs rclone as an unprivileged local account (MOTUZ_WORKER_RUN_AS=motuzjob):
the job's owner does not exist there, and only cloud-to-cloud jobs with stored
credentials come here (job_routing.ec2_eligible).

AWS credentials come from boto3's default chain (the central node's instance profile);
nothing here stores or logs the bootstrap token.
"""
import contextlib
import datetime
import hashlib
import hmac
import logging
import os
import re
import shlex
import threading

from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ..application import db
from ..models import Ec2Worker, RemoteJob
from ..models.worker import utcnow
from ..utils import ec2_config
from ..version import VERSION
from . import worker_manager


audit = worker_manager.audit
log = logging.getLogger('motuz.ec2')

# boto3/botocore log request parameters (the user data, i.e. the bootstrap token) at
# DEBUG, and application.py configures the root logger at DEBUG
for _name in ('boto3', 'botocore', 's3transfer', 'urllib3'):
    logging.getLogger(_name).setLevel(logging.INFO)

# Rows that count against MOTUZ_EC2_MAX_WORKERS (an instance that may still cost money)
ACTIVE_STATES = ('launching', 'pending', 'running', 'stopping', 'stopped')
# Rows the reaper still looks at
OPEN_STATES = ACTIVE_STATES + ('shutting-down',)
ALIVE_INSTANCE_STATES = ('pending', 'running', 'stopping', 'stopped')
ENDED_INSTANCE_STATES = ('shutting-down', 'terminated')

# RunInstances errors that mean "not this type now": try the next type of the table once
CAPACITY_ERRORS = ('InsufficientInstanceCapacity', 'InsufficientCapacity', 'InsufficientHostCapacity', 'Unsupported')
# ... and the state reason of an instance that went away for that reason
CAPACITY_STATE_REASONS = ('Server.InsufficientInstanceCapacity',)
# Worth another try on the next reaper run (the row stays 'launching')
TRANSIENT_ERRORS = ('RequestLimitExceeded', 'Throttling', 'InternalError', 'InternalFailure',
                    'ServiceUnavailable', 'Unavailable', 'RequestExpired')

RETRY_AFTER = datetime.timedelta(seconds=120) # a 'launching' row is looked up / retried after this
GONE_AFTER = datetime.timedelta(seconds=600) # an instance EC2 no longer lists counts as terminated
UNKNOWN_GRACE = datetime.timedelta(seconds=600) # unknown worker instances are terminated after this age
RUNTIME_GRACE_MINUTES = 15 # the instance's own shutdown timer fires this long after the reaper's limit
USER_DATA_LIMIT = 16 * 1024

WORKER_TAGS = (('Project', 'motuz'), ('Component', 'worker'), ('ManagedBy', 'motuz-central'))
TAG_FILTERS = [{'Name': 'tag:{}'.format(key), 'Values': [value]} for key, value in WORKER_TAGS]
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'templates', 'ec2', 'worker-user-data.sh')

TOKEN_RE = re.compile(r'^mzb1\.\d+\.[A-Za-z0-9_-]{20,}$')
CENTRAL_URL_RE = re.compile(r'^https://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?$')

_local_lock = threading.Lock()
_ADVISORY_LOCK = 0x6d6f74757a453243 # 'motuzE2C': one launcher/reaper at a time


class Ec2Error(Exception):
    pass


# ------------------------------------------------------------------ settings and AWS

def settings():
    return current_app.config['EC2']


def handles(pool):
    """True if jobs of `pool` get temporary EC2 workers"""
    s = settings()
    return bool(s.enabled and pool == s.pool)


def client():
    """EC2 client with the default credential chain (the instance profile on the central node)"""
    import boto3
    from botocore.config import Config as BotoConfig
    return boto3.session.Session(region_name=settings().region).client('ec2', config=BotoConfig(
        retries={'max_attempts': 5, 'mode': 'standard'}, connect_timeout=10, read_timeout=60))


def _client_error():
    from botocore.exceptions import BotoCoreError, ClientError
    return ClientError, BotoCoreError


def _error_code(error):
    response = getattr(error, 'response', None) or {}
    return response.get('Error', {}).get('Code', type(error).__name__)


def _error_text(error):
    response = getattr(error, 'response', None) or {}
    info = response.get('Error', {})
    if info:
        return '{}: {}'.format(info.get('Code', '?'), info.get('Message', ''))[:500]
    return str(error)[:500]


def installation_id():
    """Stable per installation (from the server secret), not secret itself"""
    key = current_app.config['SECRET_KEY'].encode('utf-8')
    return hmac.new(key, b'motuz ec2 client token v1', hashlib.sha256).hexdigest()[:10]


def client_token(job_type, job_id, attempt):
    """RunInstances idempotency token of one launch attempt of a job (at most 64 characters)"""
    return 'motuz-{}-{}-{}-{}'.format(installation_id(), job_type, job_id, attempt)


def job_tag(job_type, job_id):
    return '{}-{}'.format(job_type, job_id)


def _naive_utc(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value


# ------------------------------------------------------------------ launching

def launch_params(s, row, user_data):
    """
    RunInstances parameters: exactly what the central node's policy (deployment/aws/
    central-launch-workers-policy.json) allows. Never ImageId, SecurityGroupIds,
    NetworkInterfaces, IamInstanceProfile, KeyName, BlockDeviceMappings,
    MetadataOptions, Placement, InstanceMarketOptions, InstanceInitiatedShutdownBehavior
    or DisableApiStop: they come from the launch template.
    """
    template = s.launch_templates[ec2_config.architecture(row.instance_type)]
    launch_template = {'LaunchTemplateId': template} if template.startswith('lt-') else {'LaunchTemplateName': template}
    launch_template['Version'] = s.template_version
    tag = job_tag(row.job_type, row.job_id)
    tags = [{'Key': key, 'Value': value} for key, value in WORKER_TAGS]
    tags += [{'Key': 'Name', 'Value': 'motuz-worker-' + tag}, {'Key': 'MotuzJob', 'Value': tag}]
    params = {
        'LaunchTemplate': launch_template,
        'InstanceType': row.instance_type,
        'MinCount': 1,
        'MaxCount': 1,
        'ClientToken': row.client_token,
        'UserData': user_data,
        'TagSpecifications': [{'ResourceType': t, 'Tags': [dict(x) for x in tags]} for t in ('instance', 'volume')],
    }
    if s.subnet_id:
        params['SubnetId'] = s.subnet_id
    return params


def build_user_data(s, token, job_ref, central_url, template_path=TEMPLATE):
    """
    The instance's user data: the template with this job's values. The bootstrap token
    is its only secret (single use, bound to the job, valid for MOTUZ_EC2_BOOT_TIMEOUT).
    """
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        raise Ec2Error('malformed bootstrap token')
    if not central_url or not CENTRAL_URL_RE.match(central_url):
        raise Ec2Error('MOTUZ_PUBLIC_URL {!r} is not a plain https:// address'.format(central_url))
    if not re.match(r'^(copy|hashsum):\d+$', job_ref):
        raise Ec2Error('bad job reference')
    if not s.source_ref:
        raise Ec2Error('MOTUZ_WORKER_SOURCE_REF is not set')
    with open(template_path) as f:
        script = f.read()
    values = {
        'CENTRAL_URL': central_url.rstrip('/'),
        'POOL': s.pool,
        'JOB': job_ref,
        'SOURCE_URL': s.source_url,
        'SOURCE_REF': s.source_ref,
        'MOTUZ_VERSION': VERSION,
        'RCLONE_VERSION': ec2_config.RCLONE_VERSION,
        'RCLONE_SHA256_AMD64': ec2_config.RCLONE_SHA256['amd64'],
        'RCLONE_SHA256_ARM64': ec2_config.RCLONE_SHA256['arm64'],
        'MAX_RUNTIME_MINUTES': str(-(-s.max_runtime // 60) + RUNTIME_GRACE_MINUTES),
        'ONCE_WAIT': str(s.boot_timeout),
        'HALT_DELAY_MINUTES': str(-(-s.halt_delay // 60)),
    }
    for key, value in values.items():
        script = script.replace('@@{}@@'.format(key), shlex.quote(value))
    # In a quoted here-document of its own, never expanded or printed
    script = script.replace('@@TOKEN@@', token)
    leftover = re.search(r'@@[A-Z0-9_]+@@', script)
    if leftover:
        raise Ec2Error('user data template: {} not replaced'.format(leftover.group(0)))
    if len(script.encode('utf-8')) > USER_DATA_LIMIT:
        raise Ec2Error('user data is larger than 16 KB')
    return script


def central_url():
    return current_app.config.get('PUBLIC_URL')


def job_queued(pool):
    """Called when a job was queued for `pool`: launch its worker outside the request"""
    if not handles(pool):
        return
    try:
        from .. import tasks
        tasks.ec2_dispatch.apply_async()
    except Exception as e: # the reaper loop launches it within MOTUZ_EC2_REAP_INTERVAL
        log.warning("Could not queue the EC2 launcher task (%s); the reaper will launch the worker", e)


@contextlib.contextmanager
def _exclusive():
    """One launcher or reaper at a time (PostgreSQL advisory lock; in-process otherwise)"""
    if db.engine.dialect.name != 'postgresql':
        with _local_lock:
            yield
        return
    connection = db.engine.connect()
    try:
        connection.execute(text('SELECT pg_advisory_lock(:key)'), {'key': _ADVISORY_LOCK})
        connection.commit()
        try:
            yield
        finally:
            connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': _ADVISORY_LOCK})
            connection.commit()
    except Exception:
        connection.invalidate() # never return a connection that may still hold the lock
        raise
    finally:
        connection.close()


def dispatch(ec2=None):
    """Launches workers for queued jobs of the EC2 pool while slots are free; returns the new rows"""
    s = settings()
    if not s.enabled:
        return []
    with _exclusive():
        ec2 = ec2 or client()
        try:
            instances = _describe_workers(ec2)
        except _client_error() as e:
            log.error("EC2 workers: cannot list instances, not launching: %s", _error_text(e))
            return []
        launched = []
        queued = (db.session.query(RemoteJob)
                  .filter(RemoteJob.pool == s.pool, RemoteJob.state == 'QUEUED')
                  .order_by(RemoteJob.id).all())
        for remote_job in queued:
            if _active_count(instances) >= s.max_workers:
                break
            if db.session.query(Ec2Worker).filter_by(job_type=remote_job.job_type, job_id=remote_job.job_id).count():
                continue # has (or had) an instance: the reaper takes care of it
            row = launch(remote_job, ec2=ec2)
            if row is not None:
                launched.append(row)
        return launched


def _active_count(instances):
    """Rows that may have a running instance, plus running worker instances without a row"""
    rows = db.session.query(Ec2Worker).filter(Ec2Worker.state.in_(ACTIVE_STATES)).all()
    known = {row.instance_id for row in rows if row.instance_id}
    alive = {i['InstanceId'] for i in instances if i['State']['Name'] in ALIVE_INSTANCE_STATES}
    if alive:
        known |= {r[0] for r in db.session.query(Ec2Worker.instance_id).filter(Ec2Worker.instance_id.in_(alive))}
    return len(rows) + len(alive - known)


def launch(remote_job, attempt=1, instance_type=None, ec2=None):
    """
    Starts an instance for a queued job (attempt 2: capacity fallback). Returns the
    Ec2Worker row, or None if the job is no longer queued or another launcher got it.
    """
    s = settings()
    job = worker_manager._job(remote_job)
    if job is None or job.progress_state != 'PROGRESS' or remote_job.state != 'QUEUED':
        return None
    if job.src_cloud_id is None or job.dst_cloud_id is None: # job_routing never sends these here
        _fail(remote_job, 'Jobs with a local path cannot run on EC2 workers')
        return None
    instance_type = instance_type or ec2_config.choose_instance_type(s.instance_types, remote_job.source_bytes)
    row = Ec2Worker(
        job_type=remote_job.job_type, job_id=remote_job.job_id, attempt=attempt,
        instance_type=instance_type, state='launching',
        client_token=client_token(remote_job.job_type, remote_job.job_id, attempt),
    )
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError: # this attempt exists already (another launcher)
        db.session.rollback()
        return None
    _start(row, remote_job, ec2 or client())
    return row


def _start(row, remote_job, ec2):
    """RunInstances for a 'launching' row (again after a transient error: same ClientToken)"""
    s = settings()
    job_ref = '{}:{}'.format(row.job_type, row.job_id)
    ClientError, BotoCoreError = _client_error()
    worker_manager.expire_bootstrap_token(row.bootstrap_token_id) # of an earlier try
    try:
        token = worker_manager.create_bootstrap_token(s.pool, job=job_ref, ttl_seconds=s.boot_timeout)
    except ValueError as e: # no longer queued
        row.state = 'failed'
        row.last_error = str(e)[:1000]
        db.session.commit()
        return
    row.bootstrap_token_id = worker_manager.bootstrap_token_id(token)
    db.session.commit()
    try:
        params = launch_params(s, row, build_user_data(s, token, job_ref, central_url()))
    except (Ec2Error, OSError, KeyError) as e:
        _launch_failed(row, remote_job, 'Could not prepare an EC2 worker: {}'.format(e))
        return
    del token

    try:
        response = ec2.run_instances(**params)
    except ClientError as e:
        code = _error_code(e)
        if code == 'IdempotentParameterMismatch':
            # This attempt's ClientToken started an instance already (a lost response)
            instance = _find_by_client_token(ec2, row.client_token)
            if instance is not None:
                _adopt(row, instance)
                db.session.commit()
                return
        if code in CAPACITY_ERRORS:
            _launch_failed(row, remote_job, 'No EC2 capacity for {}: {}'.format(row.instance_type, _error_text(e)),
                           capacity=True)
        elif code in TRANSIENT_ERRORS or code == 'IdempotentParameterMismatch':
            row.last_error = _error_text(e)
            db.session.commit()
            log.warning("EC2 worker for %s: %s; the reaper tries again", job_ref, row.last_error)
        else:
            _launch_failed(row, remote_job, 'Could not start an EC2 worker: {}'.format(_error_text(e)))
        return
    except BotoCoreError as e: # network, credentials: try again until MOTUZ_EC2_BOOT_TIMEOUT
        row.last_error = _error_text(e)
        db.session.commit()
        log.warning("EC2 worker for %s: %s; the reaper tries again", job_ref, row.last_error)
        return
    _adopt(row, response['Instances'][0])
    db.session.commit()
    audit.info("EC2 worker %s (%s) launched for job %s, attempt %s", row.instance_id, row.instance_type,
               job_ref, row.attempt)


def _adopt(row, instance):
    row.instance_id = instance['InstanceId']
    row.state = instance.get('State', {}).get('Name') or 'pending'
    row.launched_at = _naive_utc(instance.get('LaunchTime')) or utcnow()


def _find_by_client_token(ec2, token):
    try:
        response = ec2.describe_instances(Filters=[{'Name': 'client-token', 'Values': [token]}])
    except _client_error() as e:
        log.error("EC2 workers: cannot look up client token: %s", _error_text(e))
        return None
    for reservation in response.get('Reservations', []):
        for instance in reservation.get('Instances', []):
            return instance
    return None


def _launch_failed(row, remote_job, reason, capacity=False, ec2=None):
    """No instance for this attempt: try the next type once (capacity) or fail the job"""
    s = settings()
    row.state = 'failed'
    row.last_error = reason[:1000]
    worker_manager.expire_bootstrap_token(row.bootstrap_token_id)
    db.session.commit()
    log.warning("EC2 worker for %s:%s: %s", row.job_type, row.job_id, reason)
    if capacity and row.attempt == 1:
        fallback = ec2_config.fallback_instance_type(s.instance_types, row.instance_type)
        if fallback is not None:
            audit.warning("EC2 worker for %s:%s: no capacity for %s, trying %s", row.job_type, row.job_id,
                          row.instance_type, fallback)
            if launch(remote_job, attempt=2, instance_type=fallback, ec2=ec2) is not None:
                return
    _fail(remote_job, reason)


def _fail(remote_job, reason):
    remote_job = (db.session.query(RemoteJob).filter_by(id=remote_job.id).with_for_update().one())
    notification = worker_manager.fail_remote_job(remote_job, reason)
    db.session.commit()
    worker_manager.send_notifications([notification])


# ------------------------------------------------------------------ reaper

def _describe_workers(ec2):
    instances = []
    for page in ec2.get_paginator('describe_instances').paginate(Filters=TAG_FILTERS):
        for reservation in page.get('Reservations', []):
            instances.extend(reservation.get('Instances', []))
    return instances


def _state_reason(instance):
    reason = (instance or {}).get('StateReason') or {}
    return reason.get('Code') or '', reason.get('Message') or ''


def _terminate(ec2, instance_id, reason):
    """TerminateInstances; returns the new state or None if it failed"""
    try:
        response = ec2.terminate_instances(InstanceIds=[instance_id])
    except _client_error() as e:
        if _error_code(e) == 'InvalidInstanceID.NotFound':
            return 'terminated'
        log.error("EC2 worker %s: could not terminate (%s): %s", instance_id, reason, _error_text(e))
        return None
    audit.warning("EC2 worker %s terminated: %s", instance_id, reason)
    for item in response.get('TerminatingInstances', []):
        return item.get('CurrentState', {}).get('Name') or 'shutting-down'
    return 'shutting-down'


def reap(ec2=None, now=None):
    """
    One reaper pass (manage.py ec2 reap). Returns a summary dict: terminated instance
    ids, failed jobs, relaunched and newly launched rows.
    """
    s = settings()
    summary = {'terminated': [], 'failed_jobs': [], 'relaunched': [], 'launched': []}
    if not s.enabled:
        return summary
    ec2 = ec2 or client()
    now = now or utcnow()
    with _exclusive():
        try:
            instances = _describe_workers(ec2)
        except _client_error() as e:
            log.error("EC2 workers: cannot list instances: %s", _error_text(e))
            return summary
        by_id = {i['InstanceId']: i for i in instances}
        by_token = {i['ClientToken']: i for i in instances if i.get('ClientToken')}
        rows = (db.session.query(Ec2Worker).filter(Ec2Worker.state.in_(OPEN_STATES))
                .order_by(Ec2Worker.id).all())
        for row in rows:
            try:
                _reap_row(ec2, s, row, by_id, by_token, now, summary)
            except Exception as e: # one broken row must not stop the others
                log.exception(e)
                db.session.rollback()

        known = set()
        if by_id:
            known = {r[0] for r in db.session.query(Ec2Worker.instance_id).filter(Ec2Worker.instance_id.in_(list(by_id)))}
        for instance in instances:
            launched = _naive_utc(instance.get('LaunchTime')) or now
            if (instance['InstanceId'] not in known and instance['State']['Name'] in ALIVE_INSTANCE_STATES
                    and now - launched > UNKNOWN_GRACE):
                if _terminate(ec2, instance['InstanceId'], 'tagged ManagedBy=motuz-central but not in ec2_worker'):
                    summary['terminated'].append(instance['InstanceId'])
        db.session.commit()
    summary['launched'] = dispatch(ec2)
    return summary


def _reap_row(ec2, s, row, by_id, by_token, now, summary):
    remote_job = (db.session.query(RemoteJob).filter_by(job_type=row.job_type, job_id=row.job_id)
                  .with_for_update().one_or_none())
    job = worker_manager._job(remote_job) if remote_job is not None else None
    job_open = (remote_job is not None and remote_job.state != 'DONE'
                and job is not None and job.progress_state == 'PROGRESS')
    name = 'The EC2 worker for this job'

    if row.state == 'launching':
        instance = by_token.get(row.client_token)
        if instance is None:
            if not job_open:
                row.state = 'failed'
                row.last_error = ((row.last_error or '') + ' (job ended before its instance started)').strip()
                worker_manager.expire_bootstrap_token(row.bootstrap_token_id)
                db.session.commit()
            elif now - row.created_at > datetime.timedelta(seconds=s.boot_timeout):
                _launch_failed(row, remote_job, 'Could not start an EC2 worker within {}: {}'.format(
                    ec2_config.format_duration(s.boot_timeout), row.last_error or 'no instance'), ec2=ec2)
                summary['failed_jobs'].append('{}:{}'.format(row.job_type, row.job_id))
            elif now - row.created_at > RETRY_AFTER:
                _start(row, remote_job, ec2)
                summary['relaunched'].append(row)
            else:
                db.session.commit()
            return
        _adopt(row, instance)

    instance = by_id.get(row.instance_id)
    if instance is not None:
        row.state = instance['State']['Name']
    elif row.launched_at is None or now - row.launched_at > GONE_AFTER:
        row.state = 'terminated' # no longer listed by EC2
    if row.state in ENDED_INSTANCE_STATES and row.terminated_at is None:
        row.terminated_at = now
    name = 'The EC2 worker {} ({})'.format(row.instance_id, row.instance_type)
    console = 'aws ec2 get-console-output --instance-id {}'.format(row.instance_id)

    if row.state in ALIVE_INSTANCE_STATES:
        runtime = now - (row.launched_at or row.created_at)
        reason = failure = None
        if not job_open:
            reason = 'its job ended'
        elif row.state in ('stopping', 'stopped'):
            reason = failure = '{} stopped instead of terminating'.format(name)
        elif runtime > datetime.timedelta(seconds=s.max_runtime):
            reason = failure = '{} reached the maximum runtime of {} (MOTUZ_EC2_MAX_RUNTIME) and was terminated'.format(
                name, ec2_config.format_duration(s.max_runtime))
        elif remote_job.state == 'QUEUED' and runtime > datetime.timedelta(seconds=s.boot_timeout):
            reason = failure = '{} did not start the job within {} (MOTUZ_EC2_BOOT_TIMEOUT); console output: {}'.format(
                name, ec2_config.format_duration(s.boot_timeout), console)
        if reason is not None:
            state = _terminate(ec2, row.instance_id, reason)
            if state is not None:
                row.state = state
                summary['terminated'].append(row.instance_id)
                if state in ENDED_INSTANCE_STATES and row.terminated_at is None:
                    row.terminated_at = now
            if failure is not None:
                row.last_error = failure[:1000]
                worker_manager.expire_bootstrap_token(row.bootstrap_token_id)
                notification = worker_manager.fail_remote_job(remote_job, failure)
                db.session.commit()
                worker_manager.send_notifications([notification])
                summary['failed_jobs'].append('{}:{}'.format(row.job_type, row.job_id))
        db.session.commit()
        return

    # shutting-down or terminated
    worker_manager.expire_bootstrap_token(row.bootstrap_token_id)
    code, message = _state_reason(instance)
    if not job_open:
        db.session.commit()
        return
    why = '{}: {}'.format(code, message) if code else 'no reason given'
    if remote_job.state == 'QUEUED':
        if code in CAPACITY_STATE_REASONS and row.attempt == 1:
            row.last_error = why[:1000]
            db.session.commit()
            fallback = ec2_config.fallback_instance_type(s.instance_types, row.instance_type)
            if fallback is not None:
                audit.warning("EC2 worker %s had no capacity for %s:%s, trying %s", row.instance_id,
                              row.job_type, row.job_id, fallback)
                new = launch(remote_job, attempt=2, instance_type=fallback, ec2=ec2)
                if new is not None:
                    summary['relaunched'].append(new)
                    return
        failure = '{} ended before it started the job ({}); console output: {}'.format(name, why, console)
    else:
        failure = '{} ended while the job was running ({})'.format(name, why)
    row.last_error = failure[:1000]
    notification = worker_manager.fail_remote_job(remote_job, failure)
    db.session.commit()
    worker_manager.send_notifications([notification])
    summary['failed_jobs'].append('{}:{}'.format(row.job_type, row.job_id))


def reap_loop(interval=None, stop=None):
    """manage.py ec2 reap --loop: reap() every MOTUZ_EC2_REAP_INTERVAL seconds, forever"""
    import time
    interval = interval or settings().reap_interval
    log.info("EC2 worker reaper: every %ss, pool %s, at most %s workers", interval, settings().pool,
             settings().max_workers)
    while stop is None or not stop():
        try:
            summary = reap()
            if any(summary.values()):
                log.info("EC2 worker reaper: %s", _summary_text(summary))
        except Exception as e:
            log.exception(e)
        finally:
            db.session.remove()
        time.sleep(interval)


def _summary_text(summary):
    return 'terminated {}, failed jobs {}, relaunched {}, launched {}'.format(
        summary['terminated'] or '-', summary['failed_jobs'] or '-',
        [r.instance_id or r.client_token for r in summary['relaunched']] or '-',
        [r.instance_id or r.client_token for r in summary['launched']] or '-')


# ------------------------------------------------------------------ status

def status_texts(job_type, remote_jobs):
    """{job id: text} where these EC2-pool jobs run, for the job's `pool_status`"""
    if not remote_jobs:
        return {}
    ids = [rj.job_id for rj in remote_jobs]
    latest = {}
    for row in (db.session.query(Ec2Worker)
                .filter(Ec2Worker.job_type == job_type, Ec2Worker.job_id.in_(ids))
                .order_by(Ec2Worker.attempt)):
        latest[row.job_id] = row
    texts = {}
    for rj in remote_jobs:
        row = latest.get(rj.job_id)
        if rj.state == 'QUEUED':
            if row is None:
                text_ = 'waiting for a free EC2 worker slot'
            elif row.state in ('launching', 'pending', 'running'):
                text_ = 'starting worker ({})'.format(row.instance_type)
            elif row.state == 'failed':
                text_ = 'worker launch failed'
            else:
                text_ = 'worker {} ({})'.format(row.state, row.instance_type)
        elif rj.state == 'RUNNING':
            text_ = 'running on {}'.format(row.instance_type) if row else 'running on an EC2 worker'
        else:
            text_ = 'ran on {}'.format(row.instance_type) if row and row.launched_at else None
        texts[rj.job_id] = text_
    return texts


def check_permissions(ec2=None):
    """
    manage.py ec2 check: RunInstances with DryRun for each architecture of the instance
    type table, with the launcher's parameters. Returns [(instance type, ok, message)].
    """
    s = settings()
    ec2 = ec2 or client()
    ClientError, BotoCoreError = _client_error()
    results = []
    types = []
    for rule in s.instance_types:
        if rule.instance_type not in types:
            types.append(rule.instance_type)
    for instance_type in types:
        row = Ec2Worker(job_type='copy', job_id=0, attempt=0, instance_type=instance_type,
                        client_token=client_token('check', 0, instance_type.replace('.', '-')))
        params = launch_params(s, row, '#!/bin/sh\nshutdown -h now\n')
        params['DryRun'] = True
        try:
            ec2.run_instances(**params)
            results.append((instance_type, False, 'unexpected: the dry run started nothing but returned no error'))
        except ClientError as e:
            code = _error_code(e)
            results.append((instance_type, code == 'DryRunOperation', _error_text(e)))
        except BotoCoreError as e:
            results.append((instance_type, False, _error_text(e)))
    return results
