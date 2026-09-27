"""add remote workers: worker, worker_bootstrap_token, remote_job

Remote workers (motuz-worker) claim jobs over HTTPS. worker holds their credentials
(SHA-256 of the secret), worker_bootstrap_token single-use tokens for ephemeral
workers, and remote_job the queue of jobs outside the 'central' pool with lease,
ticket (hashes) and live progress text.

Revision ID: d9e2f4a6b8c1
Revises: d5e1f7a3b9c4
Create Date: 2026-09-27 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd9e2f4a6b8c1'
down_revision = 'd5e1f7a3b9c4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'worker',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('pool', sa.String(), nullable=False),
        sa.Column('secret_hash', sa.String(), nullable=True),
        sa.Column('ephemeral', sa.Boolean(), server_default='f', nullable=False),
        sa.Column('bound_job', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('last_seen_ip', sa.String(), nullable=True),
        sa.Column('version', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'worker_bootstrap_token',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('token_hash', sa.String(), nullable=False),
        sa.Column('pool', sa.String(), nullable=False),
        sa.Column('bound_job', sa.String(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('worker_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['worker_id'], ['worker.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_table(
        'remote_job',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('job_type', sa.String(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('pool', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('worker_id', sa.Integer(), nullable=True),
        sa.Column('claimed_at', sa.DateTime(), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(), nullable=True),
        sa.Column('ticket_expires_at', sa.DateTime(), nullable=True),
        sa.Column('ticket_hash', sa.String(), nullable=True),
        sa.Column('broker_hash', sa.String(), nullable=True),
        sa.Column('stop_requested', sa.Boolean(), server_default='f', nullable=False),
        sa.Column('progress_text', sa.String(), nullable=True),
        sa.Column('progress_error_text', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['worker_id'], ['worker.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_type', 'job_id', name='uq_remote_job_job'),
        sa.UniqueConstraint('ticket_hash'),
        sa.UniqueConstraint('broker_hash'),
    )
    op.create_index('ix_remote_job_pool_state', 'remote_job', ['pool', 'state'])


def downgrade():
    op.drop_index('ix_remote_job_pool_state', table_name='remote_job')
    op.drop_table('remote_job')
    op.drop_table('worker_bootstrap_token')
    op.drop_table('worker')
