"""add copy_job.performance and hashsum_job.performance

rclone performance overrides of a job (utils/rclone_tuning.py): parallel transfers,
checkers, multi-thread streams, S3 / Azure chunk size and upload concurrency. Existing
jobs stay NULL, i.e. the installation's defaults.

Revision ID: d5e1f7a3b9c4
Revises: c4a7e2b9d315
Create Date: 2026-09-27 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd5e1f7a3b9c4'
down_revision = 'c4a7e2b9d315'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('copy_job', sa.Column('performance', sa.JSON(), nullable=True))
    op.add_column('hashsum_job', sa.Column('performance', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('hashsum_job', 'performance')
    op.drop_column('copy_job', 'performance')
