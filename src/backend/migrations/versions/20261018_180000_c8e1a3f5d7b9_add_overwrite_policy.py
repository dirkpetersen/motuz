"""add motuz_destination and copy_job.overwrite, skipped_existing

Overwrite policy (managers/overwrite_policy.py): the paths Motuz copy jobs and uploads
wrote to, whether a copy job may overwrite existing files, and the existing files it
kept.

Revision ID: c8e1a3f5d7b9
Revises: b6d8f0a2c4e7
Create Date: 2026-10-18 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8e1a3f5d7b9'
down_revision = 'b6d8f0a2c4e7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('copy_job', sa.Column('overwrite', sa.Boolean(), nullable=True))
    op.add_column('copy_job', sa.Column('skipped_existing', sa.Integer(), nullable=True))
    op.add_column('copy_job', sa.Column('skipped_existing_sample', sa.JSON(), nullable=True))
    op.create_table(
        'motuz_destination',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('storage_key', sa.String(), nullable=False),
        sa.Column('path', sa.String(), nullable=False),
        sa.Column('job_type', sa.String(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('storage_key', 'path'), # also the index of the lookups
    )


def downgrade():
    op.drop_table('motuz_destination')
    op.drop_column('copy_job', 'skipped_existing_sample')
    op.drop_column('copy_job', 'skipped_existing')
    op.drop_column('copy_job', 'overwrite')
