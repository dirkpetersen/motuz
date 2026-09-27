"""add temporary EC2 workers: ec2_worker, remote_job.source_bytes/source_files

ec2_worker records every EC2 instance the launcher starts for a job of the EC2 pool
(managers/ec2_launcher.py): job, attempt, instance type and id, the idempotency token
of RunInstances, the last seen state and errors. remote_job gets the size of the source
measured when the job was routed, from which the launcher picks the instance type.

Revision ID: e4b8c2d6f1a3
Revises: d9e2f4a6b8c1
Create Date: 2026-09-27 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e4b8c2d6f1a3'
down_revision = 'd9e2f4a6b8c1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ec2_worker',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('job_type', sa.String(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('instance_type', sa.String(), nullable=False),
        sa.Column('client_token', sa.String(), nullable=False),
        sa.Column('instance_id', sa.String(), nullable=True),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('bootstrap_token_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('launched_at', sa.DateTime(), nullable=True),
        sa.Column('terminated_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_type', 'job_id', 'attempt', name='uq_ec2_worker_job_attempt'),
        sa.UniqueConstraint('client_token'),
        sa.UniqueConstraint('instance_id'),
    )
    op.create_index('ix_ec2_worker_state', 'ec2_worker', ['state'])
    op.create_index('ix_ec2_worker_job', 'ec2_worker', ['job_type', 'job_id'])
    op.add_column('remote_job', sa.Column('source_bytes', sa.BigInteger(), nullable=True))
    op.add_column('remote_job', sa.Column('source_files', sa.BigInteger(), nullable=True))


def downgrade():
    op.drop_column('remote_job', 'source_files')
    op.drop_column('remote_job', 'source_bytes')
    op.drop_index('ix_ec2_worker_job', table_name='ec2_worker')
    op.drop_index('ix_ec2_worker_state', table_name='ec2_worker')
    op.drop_table('ec2_worker')
