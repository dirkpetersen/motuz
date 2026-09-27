"""
Remote workers (managers/worker_manager.py): their credentials, single-use bootstrap
tokens, and the queue of jobs they run. Secrets and tokens are stored as SHA-256
hashes only (they are 256-bit random values, so no slow hash is needed).
"""
import datetime

from ..application import db


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class Worker(db.Model):
    """A worker credential (`manage.py workers add`), or an ephemeral worker created
    by exchanging a bootstrap token (bound to one job)"""
    __tablename__ = 'worker'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String, nullable=False, unique=True)
    pool = db.Column(db.String, nullable=False)
    secret_hash = db.Column(db.String, nullable=True) # NULL for ephemeral workers
    ephemeral = db.Column(db.Boolean, nullable=False, default=False, server_default='f')
    # Ephemeral workers claim only this job: 'copy:<id>' or 'hashsum:<id>'
    bound_job = db.Column(db.String, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    revoked_at = db.Column(db.DateTime, nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    last_seen_ip = db.Column(db.String, nullable=True)
    version = db.Column(db.String, nullable=True)

    def __repr__(self):
        return '<Worker {} ({})>'.format(self.name, self.pool)


class WorkerBootstrapToken(db.Model):
    """Single-use, short-lived: exchanged once for an ephemeral worker's access token"""
    __tablename__ = 'worker_bootstrap_token'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    token_hash = db.Column(db.String, nullable=False, unique=True)
    pool = db.Column(db.String, nullable=False)
    bound_job = db.Column(db.String, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    worker_id = db.Column(db.Integer, db.ForeignKey('worker.id', ondelete='SET NULL'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class RemoteJob(db.Model):
    """
    A copy or hashsum job queued for a pool of remote workers (every job outside the
    'central' pool has one). QUEUED -> RUNNING (claimed: worker, lease, ticket) -> DONE.
    The row stays after the job ends: it records which worker ran the job.
    """
    __tablename__ = 'remote_job'
    __table_args__ = (
        db.UniqueConstraint('job_type', 'job_id', name='uq_remote_job_job'),
        db.Index('ix_remote_job_pool_state', 'pool', 'state'),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    job_type = db.Column(db.String, nullable=False) # 'copy' or 'hashsum'
    job_id = db.Column(db.Integer, nullable=False)
    owner = db.Column(db.String, nullable=False)
    pool = db.Column(db.String, nullable=False)
    state = db.Column(db.String, nullable=False, default='QUEUED')

    worker_id = db.Column(db.Integer, db.ForeignKey('worker.id', ondelete='SET NULL'), nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    lease_expires_at = db.Column(db.DateTime, nullable=True)
    ticket_expires_at = db.Column(db.DateTime, nullable=True)
    # Ticket token (progress and finish, sent by the worker agent) and broker token
    # (OAuth token refreshes, sent by rclone); valid only while RUNNING
    ticket_hash = db.Column(db.String, nullable=True, unique=True)
    broker_hash = db.Column(db.String, nullable=True, unique=True)
    stop_requested = db.Column(db.Boolean, nullable=False, default=False, server_default='f')

    # Live output of the job (a Celery job keeps it in its task result)
    progress_text = db.Column(db.String, nullable=True)
    progress_error_text = db.Column(db.String, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return '<Remote job {}:{} ({}, {})>'.format(self.job_type, self.job_id, self.pool, self.state)
