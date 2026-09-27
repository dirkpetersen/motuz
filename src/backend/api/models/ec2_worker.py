"""
Temporary EC2 workers (managers/ec2_launcher.py): one row per launch attempt of an
instance for one job of the EC2 pool. Nothing secret is stored: the instance's
bootstrap token is only referenced by id (worker_bootstrap_token), so that it can be
invalidated when the instance goes away unused.
"""
from ..application import db
from .worker import utcnow


class Ec2Worker(db.Model):
    """
    state: 'launching' (RunInstances not confirmed yet, no instance id), then the EC2
    instance state as last seen (pending, running, stopping, stopped, shutting-down,
    terminated), or 'failed' (no instance could be started for this attempt).
    """
    __tablename__ = 'ec2_worker'
    __table_args__ = (
        db.UniqueConstraint('job_type', 'job_id', 'attempt', name='uq_ec2_worker_job_attempt'),
        db.Index('ix_ec2_worker_state', 'state'),
        db.Index('ix_ec2_worker_job', 'job_type', 'job_id'),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    job_type = db.Column(db.String, nullable=False) # 'copy' or 'hashsum'
    job_id = db.Column(db.Integer, nullable=False)
    attempt = db.Column(db.Integer, nullable=False, default=1) # 2: capacity fallback or relaunch
    instance_type = db.Column(db.String, nullable=False)
    # RunInstances idempotency token: the same job attempt never starts two instances
    client_token = db.Column(db.String, nullable=False, unique=True)
    instance_id = db.Column(db.String, nullable=True, unique=True)
    state = db.Column(db.String, nullable=False, default='launching')
    bootstrap_token_id = db.Column(db.Integer, nullable=True) # worker_bootstrap_token.id, never the token

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    launched_at = db.Column(db.DateTime, nullable=True)
    terminated_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.String, nullable=True)

    def __repr__(self):
        return '<Ec2Worker {} {}:{} #{} {} ({})>'.format(
            self.instance_id or '-', self.job_type, self.job_id, self.attempt, self.instance_type, self.state)
