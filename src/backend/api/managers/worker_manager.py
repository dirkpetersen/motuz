"""
Remote workers: motuz-worker agents (src/worker) that run jobs behind firewalls which
allow only outbound HTTPS. They talk to /api/workers/... (views/worker_views.py) and
never get database access.

Credentials and tokens (none of them is accepted anywhere else):
- worker secret `mzw1.<worker id>.<random>`: created by `manage.py workers add`, shown
  once, stored as SHA-256. Exchanged at /api/workers/auth for an access token.
- bootstrap token `mzb1.<token id>.<random>`: single use, expires after minutes
  (`manage.py workers bootstrap`, create_bootstrap_token). Exchanging it creates an
  ephemeral worker, optionally bound to one job, for temporary cloud workers.
- worker access token: a JWT signed with a key derived from the server secret (not
  the key of the users' tokens) with audience 'motuz-worker' and type 'worker'. User
  endpoints never accept it (other key), and worker endpoints never accept user
  tokens (other key, no audience). The worker must also still exist unrevoked.
- ticket token `mzt1.<remote job id>.<random>`: returned with a claimed job, required
  (with the worker's access token) for that job's progress and finish calls.
- broker token `mzr1.<remote job id>.<side>.<random>`: the refresh token rclone gets
  for an OAuth connection of the job (instead of the connection's handle); the HTTPS
  token broker (handle_worker_token_request) accepts it only for that job's
  connection on that side, while the job runs. rclone runs as the job's owner, who
  can read its environment, so it gets this narrower token and never the ticket.
Tickets and broker tokens die when the job ends, its lease expires, or its worker is
revoked. All comparisons of secrets are constant time (hmac.compare_digest).

Jobs: job_routing picks the pool; queue_job adds a QUEUED RemoteJob. claim() hands out
one job atomically (SELECT ... FOR UPDATE SKIP LOCKED, then a compare-and-set on the
state) with a lease of WORKER_LEASE_SECONDS, renewed by every progress call. A job
whose lease expires is marked FAILED (never requeued: the worker may still be running
rclone somewhere); expire_leases() runs on claims and whenever users look at jobs.
"""
import datetime
import hashlib
import hmac
import json
import logging
import secrets
import subprocess
import time
import uuid

import jwt as pyjwt
from flask import current_app, request
from sqlalchemy import select, update

from ..application import db
from ..exceptions import *
from ..models import CloudConnection, CopyJob, HashsumJob, RemoteJob, Worker, WorkerBootstrapToken
from ..models.worker import utcnow
from ..utils.email_utils import Email
from ..utils.rclone_connection import RcloneConnection
from ..version import VERSION, WORKER_PROTOCOL
from . import job_routing
from . import token_broker_manager


audit = logging.getLogger('motuz.audit')

JOB_MODELS = {'copy': CopyJob, 'hashsum': HashsumJob}
FINAL_STATES = ('SUCCESS', 'FAILED', 'UNSET', 'STOPPED')
SIDES = ('src', 'dst')
# rclone remote name of each side in the ticket's configuration: md5sum always reads
# the remote `src` (RcloneConnection.md5sum_with_credentials)
REMOTE_NAMES = {'copy': {'src': 'src', 'dst': 'dst'}, 'hashsum': {'src': 'src', 'dst': 'src'}}
# Everything a worker needs to read a 'profile' connection's credentials from the
# owner's home directory itself (no secrets are stored for those)
PROFILE_FIELDS = (
    'type', 'subtype', 'owner', 'profile_source', 'profile_name',
    's3_region', 's3_endpoint', 's3_v2_auth', 'kms_encryption_key_arn', 'azure_account',
)

TOKEN_AUDIENCE = 'motuz-worker'
TOKEN_TYPE = 'worker'
MAX_CLAIM_WAIT = 25 # seconds; below Traefik's and uWSGI's timeouts
MAX_TEXT = 20000
MAX_ERROR_TEXT = 10000
_LAST_SEEN_INTERVAL = datetime.timedelta(seconds=30)
_SWEEP_INTERVAL = 5 # seconds between lease sweeps triggered by user requests
_last_sweep = [0.0]


# ------------------------------------------------------------------ secrets

def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _hash_matches(value, stored_hash):
    """Constant-time comparison of a presented secret with a stored hash"""
    if not isinstance(value, str) or not stored_hash:
        return False
    return hmac.compare_digest(_hash(value), stored_hash)


def _parse(value, prefix, parts):
    """Splits `<prefix>.<id>[.<more>].<random>`; None if malformed. The id is an int."""
    if not isinstance(value, str) or len(value) > 512:
        return None
    fields = value.split('.')
    if len(fields) != parts or fields[0] != prefix or not fields[1].isdigit():
        return None
    return fields


def _new_secret(prefix, *ids):
    return '.'.join([prefix, *[str(i) for i in ids], secrets.token_urlsafe(32)])


def _iso(value):
    return value.replace(microsecond=0).isoformat() + 'Z' if value else None


# ------------------------------------------------------------------ worker access tokens

def _signing_key():
    # Derived from the server secret, but never the key of the users' JWTs
    return hmac.new(current_app.config['SECRET_KEY'].encode('utf-8'),
                    b'motuz worker access token v1', hashlib.sha256).digest()


def issue_access_token(worker):
    now = int(time.time())
    lifetime = current_app.config['WORKER_TOKEN_SECONDS']
    token = pyjwt.encode({
        'iss': 'motuz',
        'aud': TOKEN_AUDIENCE,
        'typ': TOKEN_TYPE,
        'sub': str(worker.id),
        'pool': worker.pool,
        'iat': now,
        'exp': now + lifetime,
        'jti': str(uuid.uuid4()),
    }, _signing_key(), algorithm='HS256')
    return token, lifetime


def authenticate(authorization_header):
    """The Worker of a request's `Authorization: Bearer <worker access token>`; 401 otherwise"""
    if not authorization_header or not authorization_header.startswith('Bearer '):
        raise HTTP_401_UNAUTHORIZED('Worker access token required')
    try:
        claims = pyjwt.decode(
            authorization_header[len('Bearer '):].strip(), _signing_key(),
            algorithms=['HS256'], audience=TOKEN_AUDIENCE, issuer='motuz',
            options={'require': ['exp', 'iat', 'sub', 'aud', 'iss']},
        )
    except pyjwt.PyJWTError:
        raise HTTP_401_UNAUTHORIZED('Invalid or expired worker access token')
    if claims.get('typ') != TOKEN_TYPE or not str(claims.get('sub', '')).isdigit():
        raise HTTP_401_UNAUTHORIZED('Invalid or expired worker access token')

    worker = db.session.get(Worker, int(claims['sub']))
    if worker is None or worker.revoked_at is not None:
        audit.warning("worker request refused: worker id %s is revoked or unknown, ip=%s",
                      claims['sub'], _client_ip())
        raise HTTP_401_UNAUTHORIZED('Worker revoked')
    if claims.get('pool') != worker.pool:
        raise HTTP_401_UNAUTHORIZED('Invalid or expired worker access token')
    _seen(worker)
    return worker


def _client_ip():
    try:
        return request.remote_addr
    except RuntimeError: # no request (CLI)
        return None


def _seen(worker, version=None):
    now = utcnow()
    if version is not None:
        worker.version = str(version)[:64]
    if version is not None or worker.last_seen_at is None or now - worker.last_seen_at > _LAST_SEEN_INTERVAL:
        worker.last_seen_at = now
        worker.last_seen_ip = _client_ip()
        db.session.commit()


def server_info():
    return {'version': VERSION, 'protocol': WORKER_PROTOCOL, 'rclone_version': rclone_version()}


_rclone_version = []


def rclone_version():
    if not _rclone_version:
        try:
            output = subprocess.run(['rclone', 'version'], capture_output=True, text=True, timeout=10).stdout
            _rclone_version.append(output.split('\n')[0].strip() or None)
        except (OSError, subprocess.SubprocessError):
            _rclone_version.append(None)
    return _rclone_version[0]


def sign_in(body):
    """
    Exchanges a worker secret or a bootstrap token for an access token.
    @return: {access_token, expires_in, worker, server, lease_seconds}
    """
    body = body if isinstance(body, dict) else {}
    ip = _client_ip()
    if body.get('secret'):
        worker = _worker_by_secret(body['secret'], ip)
    elif body.get('bootstrap_token'):
        worker = _exchange_bootstrap_token(body['bootstrap_token'], ip)
    else:
        raise HTTP_400_BAD_REQUEST('secret or bootstrap_token required')

    _seen(worker, version=body.get('version') or '')
    audit.info("worker sign-in: worker=%s id=%s pool=%s ephemeral=%s version=%s ip=%s",
               worker.name, worker.id, worker.pool, worker.ephemeral, worker.version, ip)
    return _token_response(worker)


def refresh(worker):
    """A new access token for a signed-in worker (ephemeral workers have no secret)"""
    return _token_response(worker)


def _token_response(worker):
    token, lifetime = issue_access_token(worker)
    return {
        'access_token': token,
        'token_type': 'Bearer',
        'expires_in': lifetime,
        'worker': {
            'id': worker.id,
            'name': worker.name,
            'pool': worker.pool,
            'ephemeral': worker.ephemeral,
            'bound_job': worker.bound_job,
        },
        'server': server_info(),
        'lease_seconds': current_app.config['WORKER_LEASE_SECONDS'],
    }


def _worker_by_secret(secret, ip):
    fields = _parse(secret, 'mzw1', 3)
    worker = db.session.get(Worker, int(fields[1])) if fields else None
    # Compare even for unknown workers, so that both take the same time
    if not _hash_matches(secret, worker.secret_hash if worker is not None else _hash('-')) or worker is None:
        audit.warning("worker sign-in refused: bad secret (worker id %s), ip=%s", fields[1] if fields else '?', ip)
        raise HTTP_401_UNAUTHORIZED('Invalid worker credential')
    if worker.revoked_at is not None or worker.ephemeral:
        audit.warning("worker sign-in refused: worker %s is revoked, ip=%s", worker.name, ip)
        raise HTTP_401_UNAUTHORIZED('Worker revoked')
    return worker


def _exchange_bootstrap_token(value, ip):
    fields = _parse(value, 'mzb1', 3)
    row = None
    if fields:
        row = (db.session.query(WorkerBootstrapToken)
               .filter_by(id=int(fields[1])).with_for_update().one_or_none())
    if not _hash_matches(value, row.token_hash if row is not None else _hash('-')) or row is None:
        db.session.rollback()
        audit.warning("bootstrap refused: bad token (id %s), ip=%s", fields[1] if fields else '?', ip)
        raise HTTP_401_UNAUTHORIZED('Invalid bootstrap token')
    if row.used_at is not None or row.expires_at <= utcnow():
        db.session.rollback()
        audit.warning("bootstrap refused: token %s is used or expired, ip=%s", row.id, ip)
        raise HTTP_401_UNAUTHORIZED('Bootstrap token used or expired')

    worker = Worker(
        name='{}-ephemeral-{}'.format(row.pool, row.id),
        pool=row.pool,
        secret_hash=None,
        ephemeral=True,
        bound_job=row.bound_job,
    )
    db.session.add(worker)
    db.session.flush()
    row.used_at = utcnow()
    row.worker_id = worker.id
    db.session.commit()
    audit.info("bootstrap token %s exchanged: ephemeral worker %s, pool %s, job %s, ip=%s",
               row.id, worker.name, worker.pool, worker.bound_job or 'any', ip)
    return worker


# ------------------------------------------------------------------ administration (manage.py workers)

def add_worker(name, pool):
    """Returns (worker, secret); the secret is not stored and cannot be shown again"""
    if not job_routing.valid_pool(pool) or pool == job_routing.CENTRAL:
        raise ValueError("Pool must be a name like 'onprem' or 'aws' (not 'central')")
    if not name or len(name) > 64 or db.session.query(Worker).filter_by(name=name).count():
        raise ValueError("Worker name must be new and 1 to 64 characters")
    worker = Worker(name=name, pool=pool, secret_hash=_hash('pending'))
    db.session.add(worker)
    db.session.flush()
    secret = _new_secret('mzw1', worker.id)
    worker.secret_hash = _hash(secret)
    db.session.commit()
    audit.info("worker added: %s id=%s pool=%s", name, worker.id, pool)
    return worker, secret


def list_workers():
    return db.session.query(Worker).order_by(Worker.id).all()


def revoke_worker(name):
    """Revokes a worker: its tokens stop working, and its running jobs fail now"""
    worker = db.session.query(Worker).filter_by(name=name).one_or_none()
    if worker is None:
        raise ValueError("No worker named {!r}".format(name))
    if worker.revoked_at is None:
        worker.revoked_at = utcnow()
    running = (db.session.query(RemoteJob)
               .filter_by(worker_id=worker.id, state='RUNNING').with_for_update().all())
    notifications = [_expire(rj, 'Worker {} was revoked'.format(worker.name)) for rj in running]
    db.session.commit()
    _send(notifications)
    audit.info("worker revoked: %s id=%s, %d running job(s) failed", worker.name, worker.id, len(running))
    return worker


def parse_job_ref(value):
    """'copy:12', 'hashsum:3' or '12' (a copy job) -> ('copy', 12)"""
    job_type, _, job_id = str(value).rpartition(':')
    job_type = job_type or 'copy'
    if job_type not in JOB_MODELS or not job_id.isdigit():
        raise ValueError("Job must be <id>, copy:<id> or hashsum:<id>")
    return job_type, int(job_id)


def create_bootstrap_token(pool, job=None, ttl_seconds=900):
    """
    A single-use bootstrap token for an ephemeral worker of `pool` (e.g. an EC2
    instance started for one job, which gets the token in its user data). With `job`
    ('copy:<id>' / 'hashsum:<id>') the worker can claim only that job, which must be
    queued for the same pool. Returns the token; only its hash is stored.
    """
    if not job_routing.valid_pool(pool) or pool == job_routing.CENTRAL:
        raise ValueError("Pool must be a name like 'aws' (not 'central')")
    if not 30 <= ttl_seconds <= 24 * 3600:
        raise ValueError("TTL must be between 30 seconds and 24 hours")
    bound_job = None
    if job is not None:
        job_type, job_id = parse_job_ref(job)
        remote_job = db.session.query(RemoteJob).filter_by(job_type=job_type, job_id=job_id).one_or_none()
        if remote_job is None or remote_job.pool != pool or remote_job.state != 'QUEUED':
            raise ValueError("{} job {} is not queued for pool {}".format(job_type, job_id, pool))
        bound_job = '{}:{}'.format(job_type, job_id)
    row = WorkerBootstrapToken(
        token_hash=_hash(secrets.token_urlsafe(32)), # replaced below, once the id is known
        pool=pool,
        bound_job=bound_job,
        expires_at=utcnow() + datetime.timedelta(seconds=ttl_seconds),
    )
    db.session.add(row)
    db.session.flush()
    token = _new_secret('mzb1', row.id)
    row.token_hash = _hash(token)
    db.session.commit()
    audit.info("bootstrap token %s created: pool %s, job %s, ttl %ss", row.id, pool, bound_job or 'any', ttl_seconds)
    return token


# ------------------------------------------------------------------ queue

def queue_job(job_type, job, pool):
    """Queues a committed job for the remote workers of `pool`"""
    db.session.add(RemoteJob(job_type=job_type, job_id=job.id, owner=job.owner, pool=pool, state='QUEUED'))
    db.session.commit()
    audit.info("job queued: %s:%s owner=%s pool=%s", job_type, job.id, job.owner, pool)


def remote_job_for(job_type, job_id):
    return db.session.query(RemoteJob).filter_by(job_type=job_type, job_id=job_id).one_or_none()


def apply_remote_progress(job_type, job):
    """
    For a remote job, sets the live output a Celery job gets from its task result
    (progress_text / progress_error_text, or the hashsum error text of the running
    side). Returns False for jobs that run with Celery.
    """
    remote_job = remote_job_for(job_type, job.id)
    if remote_job is None:
        return False
    if job_type == 'copy':
        text = remote_job.progress_text or ''
        if remote_job.state == 'QUEUED':
            text = 'Waiting for a worker of pool "{}"'.format(remote_job.pool)
        job.progress_text = text
        job.progress_error_text = remote_job.progress_error_text or ''
    else:
        if remote_job.state == 'RUNNING' and remote_job.progress_text in SIDES:
            setattr(job, 'progress_{}_error_text'.format(remote_job.progress_text), remote_job.progress_error_text)
    return True


def request_stop(job_type, job_id):
    """
    Stop pressed by the user: a queued job is dropped from the queue; a running one is
    told to stop on its worker's next progress call. False if the job runs with Celery.
    """
    remote_job = (db.session.query(RemoteJob)
                  .filter_by(job_type=job_type, job_id=job_id).with_for_update().one_or_none())
    if remote_job is None:
        db.session.rollback()
        return False
    if remote_job.state == 'QUEUED':
        remote_job.state = 'DONE'
        remote_job.finished_at = utcnow()
    elif remote_job.state == 'RUNNING':
        remote_job.stop_requested = True
    db.session.commit()
    audit.info("stop requested: %s:%s (%s)", job_type, job_id, remote_job.state)
    return True


def _job(remote_job):
    return db.session.get(JOB_MODELS[remote_job.job_type], remote_job.job_id)


def _end(remote_job):
    remote_job.state = 'DONE'
    remote_job.finished_at = utcnow()
    # The ticket hash stays, so that a late call of the worker learns that the job
    # ended (410) and stops its rclone; the state refuses everything else
    remote_job.broker_hash = None
    worker = db.session.get(Worker, remote_job.worker_id) if remote_job.worker_id else None
    # An ephemeral worker exists for its one job
    if (worker is not None and worker.ephemeral and worker.revoked_at is None
            and worker.bound_job == '{}:{}'.format(remote_job.job_type, remote_job.job_id)):
        worker.revoked_at = utcnow()


def _expire(remote_job, reason):
    """Fails a running job whose worker is gone; returns the notification to send"""
    job = _job(remote_job)
    notification = None
    if job is not None and job.progress_state == 'PROGRESS':
        job.progress_state = 'FAILED'
        job.progress_current = 100
        job.progress_error = reason
        if remote_job.claimed_at:
            job.progress_execution_time = int((utcnow() - remote_job.claimed_at).total_seconds())
        notification = _notification(remote_job.job_type, job, 'FAILED')
    remote_job.progress_error_text = ((remote_job.progress_error_text or '') + '\n' + reason).strip()[-MAX_ERROR_TEXT:]
    _end(remote_job)
    audit.warning("job %s:%s failed: %s", remote_job.job_type, remote_job.job_id, reason)
    return notification


def expire_leases(force=False):
    """Fails running jobs whose worker stopped reporting (lease or ticket expired)"""
    if not force and time.monotonic() - _last_sweep[0] < _SWEEP_INTERVAL:
        return
    _last_sweep[0] = time.monotonic()
    now = utcnow()
    rows = (db.session.query(RemoteJob)
            .filter(RemoteJob.state == 'RUNNING')
            .filter((RemoteJob.lease_expires_at < now) | (RemoteJob.ticket_expires_at < now))
            .with_for_update(skip_locked=True)
            .all())
    notifications = []
    for remote_job in rows:
        worker = db.session.get(Worker, remote_job.worker_id) if remote_job.worker_id else None
        if remote_job.ticket_expires_at < now:
            reason = 'The job ticket expired (MOTUZ_WORKER_TICKET_MAX_HOURS)'
        else:
            reason = 'Worker {} stopped reporting: its lease expired at {}'.format(
                worker.name if worker else '?', _iso(remote_job.lease_expires_at))
        notifications.append(_expire(remote_job, reason))
    db.session.commit()
    _send(notifications)


def claim(worker, body):
    """
    Long poll for one job of the worker's pool: returns a ticket, or None after up to
    `wait` seconds (at most MAX_CLAIM_WAIT).
    """
    body = body if isinstance(body, dict) else {}
    pool = body.get('pool')
    if pool != worker.pool:
        audit.warning("claim refused: worker %s (pool %s) asked for pool %r", worker.name, worker.pool, pool)
        raise HTTP_403_FORBIDDEN('Worker {} belongs to pool {}'.format(worker.name, worker.pool))
    capabilities = body.get('capabilities') if isinstance(body.get('capabilities'), dict) else {}
    job_types = capabilities.get('job_types') or list(JOB_MODELS)
    job_types = [t for t in job_types if t in JOB_MODELS] if isinstance(job_types, list) else []
    if not job_types:
        raise HTTP_400_BAD_REQUEST('capabilities.job_types: copy and/or hashsum')
    try:
        wait = min(max(float(body.get('wait', 0)), 0), MAX_CLAIM_WAIT)
    except (TypeError, ValueError):
        raise HTTP_400_BAD_REQUEST('wait must be a number of seconds')

    worker_id = worker.id
    deadline = time.monotonic() + wait
    while True:
        expire_leases()
        ticket = _try_claim(worker, job_types)
        if ticket is not None:
            return ticket
        if time.monotonic() >= deadline:
            return None
        db.session.remove() # no connection held while waiting
        time.sleep(1)
        worker = db.session.get(Worker, worker_id)
        if worker is None or worker.revoked_at is not None:
            raise HTTP_401_UNAUTHORIZED('Worker revoked')


def _try_claim(worker, job_types):
    query = (select(RemoteJob.id)
             .where(RemoteJob.pool == worker.pool)
             .where(RemoteJob.state == 'QUEUED')
             .where(RemoteJob.job_type.in_(job_types)))
    if worker.bound_job:
        job_type, job_id = parse_job_ref(worker.bound_job)
        query = query.where(RemoteJob.job_type == job_type).where(RemoteJob.job_id == job_id)
    query = query.order_by(RemoteJob.id).limit(1).with_for_update(skip_locked=True)
    remote_job_id = db.session.execute(query).scalar()
    if remote_job_id is None:
        db.session.rollback()
        return None

    now = utcnow()
    lease = datetime.timedelta(seconds=current_app.config['WORKER_LEASE_SECONDS'])
    ticket_token = _new_secret('mzt1', remote_job_id)
    broker_secret = secrets.token_urlsafe(32)
    # Compare-and-set: exactly one claimer moves the job from QUEUED to RUNNING, also
    # where the database has no row locks
    result = db.session.execute(
        update(RemoteJob)
        .where(RemoteJob.id == remote_job_id)
        .where(RemoteJob.state == 'QUEUED')
        .values(
            state='RUNNING',
            worker_id=worker.id,
            claimed_at=now,
            lease_expires_at=now + lease,
            ticket_expires_at=now + datetime.timedelta(hours=current_app.config['WORKER_TICKET_MAX_HOURS']),
            ticket_hash=_hash(ticket_token),
            broker_hash=_hash(broker_secret),
            stop_requested=False,
        )
    )
    if result.rowcount != 1:
        db.session.rollback()
        return None

    remote_job = db.session.get(RemoteJob, remote_job_id)
    db.session.refresh(remote_job)
    job = _job(remote_job)
    if job is None or job.progress_state != 'PROGRESS':
        _end(remote_job) # deleted or stopped meanwhile
        db.session.commit()
        return None

    try:
        ticket = build_ticket(remote_job, job, ticket_token, broker_secret)
    except Exception as e:
        logging.exception(e)
        notification = _expire(remote_job, 'Could not prepare the job for its worker: {}'.format(e))
        db.session.commit()
        _send([notification])
        return None

    db.session.commit()
    audit.info("job claimed: %s:%s owner=%s by worker %s (pool %s), ticket %s",
               remote_job.job_type, remote_job.job_id, remote_job.owner, worker.name, worker.pool, remote_job.id)
    return ticket


def public_broker_url():
    base = current_app.config.get('PUBLIC_URL') or request.host_url
    return base.rstrip('/') + '/api/workers/oauth/token'


def broker_refresh_token(remote_job_id, side, broker_secret):
    return 'mzr1.{}.{}.{}'.format(remote_job_id, side, broker_secret)


def build_ticket(remote_job, job, ticket_token, broker_secret):
    """
    Everything the worker needs for this one job: its parameters, the rclone
    configuration of the job's own connections (by _formatCredentials, with the
    job-scoped HTTPS token broker for OAuth connections), extra rclone flags, the
    ticket token and its expiry. Nothing about other jobs, connections or users.
    """
    sides = {}
    broker_url = public_broker_url()
    for side in SIDES:
        connection = getattr(job, '{}_cloud'.format(side))
        entry = {'path': getattr(job, '{}_resource_path'.format(side)), 'local': connection is None}
        if connection is not None:
            if connection.owner != job.owner: # never, see cloud_connection_manager.owned_cloud_id
                raise ValueError('connection of another user')
            remote = REMOTE_NAMES[remote_job.job_type][side]
            entry['remote'] = remote
            entry['type'] = connection.type
            if connection.subtype == 'profile':
                # No stored credentials: the worker reads them from the owner's home
                # directory, as the owner, like this node would (local_credentials)
                entry['profile_connection'] = {key: getattr(connection, key) for key in PROFILE_FIELDS}
            else:
                entry['rclone_env'] = RcloneConnection()._formatCredentials(
                    connection, remote,
                    token_broker=lambda _, side=side: (broker_refresh_token(remote_job.id, side, broker_secret), broker_url),
                )
        sides[side] = entry

    if remote_job.job_type == 'copy':
        options = {'copy_links': bool(job.copy_links)}
    else:
        options = {'download': bool(job.option_download)}

    return {
        'protocol': WORKER_PROTOCOL,
        'ticket_id': remote_job.id,
        'ticket_token': ticket_token,
        'expires_at': _iso(remote_job.ticket_expires_at),
        'lease_seconds': current_app.config['WORKER_LEASE_SECONDS'],
        'lease_expires_at': _iso(remote_job.lease_expires_at),
        'progress_interval': max(1, min(5, current_app.config['WORKER_LEASE_SECONDS'] // 6)),
        'job': {
            'type': remote_job.job_type,
            'id': job.id,
            'owner': job.owner,
            'options': options,
            'src': sides['src'],
            'dst': sides['dst'],
        },
        'rclone_flags': job_routing.rclone_flags(remote_job.job_type, job),
    }


# ------------------------------------------------------------------ ticket calls

def _ticket(worker, ticket_id, ticket_token):
    """The locked RemoteJob of a valid ticket of this worker; 403/410 otherwise"""
    fields = _parse(ticket_token, 'mzt1', 3)
    remote_job = (db.session.query(RemoteJob).filter_by(id=ticket_id).with_for_update().one_or_none()
                  if fields and int(fields[1]) == ticket_id else None)
    if (remote_job is None or remote_job.worker_id != worker.id
            or not _hash_matches(ticket_token, remote_job.ticket_hash)):
        db.session.rollback()
        audit.warning("ticket refused: worker %s presented an invalid ticket for %s, ip=%s",
                      worker.name, ticket_id, _client_ip())
        raise HTTP_403_FORBIDDEN('Invalid ticket')
    if remote_job.state != 'RUNNING':
        db.session.rollback()
        raise HTTP_410_GONE('The job has ended')
    now = utcnow()
    if remote_job.lease_expires_at < now or remote_job.ticket_expires_at < now:
        notification = _expire(remote_job, 'Worker {} reported after its lease expired'.format(worker.name))
        db.session.commit()
        _send([notification])
        raise HTTP_410_GONE('The lease of this job expired')
    return remote_job


def _text(value, limit, tail=False):
    if not isinstance(value, str):
        return None
    return value[-limit:] if tail else value[:limit]


def progress(worker, ticket_id, ticket_token, body):
    """
    Progress of a running job: renews the lease. Returns {action: continue|stop, ...};
    'stop' after the user pressed Stop.
    """
    body = body if isinstance(body, dict) else {}
    remote_job = _ticket(worker, ticket_id, ticket_token)
    job = _job(remote_job)
    now = utcnow()

    if job is not None and job.progress_state == 'PROGRESS':
        try:
            job.progress_current = min(max(int(body.get('percent') or 0), 0), 100)
        except (TypeError, ValueError):
            pass
        job.progress_execution_time = int((now - remote_job.claimed_at).total_seconds())
    if remote_job.job_type == 'hashsum':
        if body.get('side') in SIDES:
            remote_job.progress_text = body['side']
    elif body.get('text') is not None:
        remote_job.progress_text = _text(body.get('text'), MAX_TEXT)
    if body.get('error_text') is not None:
        remote_job.progress_error_text = _text(body.get('error_text'), MAX_ERROR_TEXT, tail=True)

    lease = current_app.config['WORKER_LEASE_SECONDS']
    remote_job.lease_expires_at = now + datetime.timedelta(seconds=lease)
    # Stop pressed, or the job is gone (deleting a connection deletes its jobs)
    stop = remote_job.stop_requested or job is None or job.progress_state == 'STOPPED'
    action = 'stop' if stop else 'continue'
    db.session.commit()
    return {'action': action, 'lease_seconds': lease, 'lease_expires_at': _iso(remote_job.lease_expires_at)}


def finish(worker, ticket_id, ticket_token, body):
    """The end of a job on its worker: final state, output, and for hashsum the trees"""
    body = body if isinstance(body, dict) else {}
    state = body.get('state')
    if state not in FINAL_STATES:
        raise HTTP_400_BAD_REQUEST('state must be one of {}'.format(', '.join(FINAL_STATES)))
    remote_job = _ticket(worker, ticket_id, ticket_token)
    job = _job(remote_job)
    now = utcnow()
    notification = None

    if remote_job.job_type == 'hashsum':
        trees = {side: body.get('{}_tree'.format(side)) for side in SIDES}
        if any(tree is not None and not isinstance(tree, list) for tree in trees.values()):
            db.session.rollback()
            raise HTTP_400_BAD_REQUEST('src_tree and dst_tree must be lists')

    if job is not None:
        stopped = remote_job.stop_requested or job.progress_state == 'STOPPED'
        if stopped:
            final = 'STOPPED'
        elif state == 'STOPPED': # the worker stopped without being asked (shut down)
            final = 'FAILED'
        else:
            final = state
        job.progress_state = final
        job.progress_current = 100
        job.progress_execution_time = int((now - remote_job.claimed_at).total_seconds())

        if remote_job.job_type == 'copy':
            if body.get('text') is not None:
                remote_job.progress_text = _text(body['text'], MAX_TEXT)
            if body.get('error_text') is not None:
                remote_job.progress_error_text = _text(body['error_text'], MAX_ERROR_TEXT, tail=True)
        else:
            for side in SIDES:
                error_text = _text(body.get('{}_error_text'.format(side)), MAX_ERROR_TEXT, tail=True)
                if error_text is not None:
                    setattr(job, 'progress_{}_error'.format(side), error_text or None)
                if final == 'SUCCESS' and trees[side] is not None:
                    setattr(job, 'progress_{}_tree'.format(side), json.dumps(trees[side]))
            if body.get('error_text') and not body.get('src_error_text') and not body.get('dst_error_text'):
                job.progress_error = _text(body['error_text'], MAX_ERROR_TEXT, tail=True)
        if final != 'STOPPED':
            notification = _notification(remote_job.job_type, job, final, trees if remote_job.job_type == 'hashsum' else None)
    else:
        final = state

    _end(remote_job)
    db.session.commit()
    _send([notification])
    audit.info("job finished: %s:%s on worker %s: %s (exit status %s)", remote_job.job_type,
               remote_job.job_id, worker.name, final, body.get('exit_status'))
    return {'state': final}


# ------------------------------------------------------------------ HTTPS token broker

def handle_worker_token_request(form, authorization):
    """
    OAuth refresh_token grant for rclone on a remote worker (/api/workers/oauth/token).
    The refresh token is a broker token of a running job (mzr1.<ticket>.<side>.<random>);
    it only refreshes the connection on that side of that job. Everything else
    (locking, caching, rotation, own-app credentials) is the loopback broker's code.
    """
    if form.get('grant_type') != 'refresh_token' or not form.get('refresh_token'):
        return 400, {'error': 'unsupported_grant_type'}

    value = form['refresh_token']
    fields = _parse(value, 'mzr1', 4)
    refused = (400, {'error': 'invalid_grant', 'error_description': 'Invalid or expired job token'})
    if not fields or fields[2] not in SIDES:
        audit.warning("broker refused: malformed token, ip=%s", _client_ip())
        return refused
    remote_job_id, side, secret = int(fields[1]), fields[2], fields[3]
    remote_job = db.session.get(RemoteJob, remote_job_id)
    now = utcnow()
    worker = db.session.get(Worker, remote_job.worker_id) if remote_job is not None and remote_job.worker_id else None
    if (remote_job is None or not _hash_matches(secret, remote_job.broker_hash)
            or remote_job.state != 'RUNNING' or remote_job.lease_expires_at < now
            or remote_job.ticket_expires_at < now or worker is None or worker.revoked_at is not None):
        db.session.rollback()
        audit.warning("broker refused: invalid or ended ticket %s (%s), ip=%s", remote_job_id, side, _client_ip())
        return refused

    job = _job(remote_job)
    connection_id = getattr(job, '{}_cloud_id'.format(side), None) if job is not None else None
    connection = None
    if connection_id is not None:
        connection = (db.session.query(CloudConnection)
                      .filter_by(id=connection_id).with_for_update().one_or_none())
    if (connection is None or connection.owner != remote_job.owner
            or connection.type not in token_broker_manager.BROKERED_TYPES):
        db.session.rollback()
        audit.warning("broker refused: ticket %s has no OAuth connection on side %s, ip=%s",
                      remote_job_id, side, _client_ip())
        return refused

    audit.info("broker refresh: ticket %s (%s:%s) side %s, connection %s, worker %s, ip=%s",
               remote_job_id, remote_job.job_type, remote_job.job_id, side, connection.id, worker.name, _client_ip())
    return token_broker_manager.serve_locked_connection(connection, form, authorization)


# ------------------------------------------------------------------ notifications

def _notification(job_type, job, state, trees=None):
    if job_type == 'copy':
        outcome = 'COMPLETED successfully' if state == 'SUCCESS' else 'FAILED'
        return job.notification_email, 'Motuz Copy Job with ID {} {}!'.format(job.id, outcome)
    if state == 'SUCCESS':
        identical = trees is not None and not trees.get('src') and not trees.get('dst')
        outcome = 'Files are IDENTICAL!' if identical else 'Files are DIFFERENT!'
        return job.notification_email, 'Motuz Integrity Check Job with ID {} completed! {}'.format(job.id, outcome)
    return job.notification_email, 'Motuz Integrity Check Job with ID {} FAILED!'.format(job.id)


def _send(notifications):
    for notification in notifications:
        if notification is None:
            continue
        try:
            Email.send_notification(to=notification[0], subject=notification[1])
        except Exception as e:
            logging.exception(e)
