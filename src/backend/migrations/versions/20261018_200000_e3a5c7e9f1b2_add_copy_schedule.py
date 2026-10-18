"""add copy_schedule and copy_job.schedule_id

Scheduled repeating copies (managers/schedule_manager.py, README "Scheduled copies"):
each run is a copy job with schedule_id that only adds new files.

Revision ID: e3a5c7e9f1b2
Revises: d2f4a6c8e0b1
Create Date: 2026-10-18 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e3a5c7e9f1b2'
down_revision = 'd2f4a6c8e0b1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'copy_schedule',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('src_cloud_id', sa.Integer(), nullable=True),
        sa.Column('src_resource_path', sa.String(), nullable=False),
        sa.Column('dst_cloud_id', sa.Integer(), nullable=True),
        sa.Column('dst_resource_path', sa.String(), nullable=False),
        sa.Column('copy_links', sa.Boolean(), nullable=True),
        sa.Column('notification_email', sa.String(), nullable=True),
        sa.Column('performance', sa.JSON(), nullable=True),
        sa.Column('frequency', sa.String(), nullable=False),
        sa.Column('minute', sa.Integer(), nullable=False),
        sa.Column('hour', sa.Integer(), nullable=True),
        sa.Column('weekday', sa.Integer(), nullable=True),
        sa.Column('day_of_month', sa.Integer(), nullable=True),
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('next_run_at', sa.DateTime(), nullable=False),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('last_job_id', sa.Integer(), nullable=True),
        sa.Column('last_status', sa.String(), nullable=True),
        sa.Column('skipped_runs', sa.Integer(), server_default='0', nullable=False),
        sa.Column('egress_estimate', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['src_cloud_id'], ['cloud_connection.id']),
        sa.ForeignKeyConstraint(['dst_cloud_id'], ['cloud_connection.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_copy_schedule_owner'), 'copy_schedule', ['owner'], unique=False)
    op.create_index(op.f('ix_copy_schedule_next_run_at'), 'copy_schedule', ['next_run_at'], unique=False)
    op.add_column('copy_job', sa.Column('schedule_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_copy_job_schedule_id'), 'copy_job', ['schedule_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_copy_job_schedule_id'), table_name='copy_job')
    op.drop_column('copy_job', 'schedule_id')
    op.drop_index(op.f('ix_copy_schedule_next_run_at'), table_name='copy_schedule')
    op.drop_index(op.f('ix_copy_schedule_owner'), table_name='copy_schedule')
    op.drop_table('copy_schedule')
