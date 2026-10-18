"""add hashsum_job.hash_type, md5_fallback, not_checkable, not_checkable_sample

CRC32C integrity checks of S3 (utils/job_runner.py, README "Check Integrity"): the
hash a check compared, the files it compared by MD5 and those it could not check.

Revision ID: f4b6d8e0a2c3
Revises: e3a5c7e9f1b2
Create Date: 2026-10-19 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4b6d8e0a2c3'
down_revision = 'e3a5c7e9f1b2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('hashsum_job', sa.Column('hash_type', sa.String(), nullable=True))
    op.add_column('hashsum_job', sa.Column('md5_fallback', sa.Integer(), nullable=True))
    op.add_column('hashsum_job', sa.Column('not_checkable', sa.Integer(), nullable=True))
    op.add_column('hashsum_job', sa.Column('not_checkable_sample', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('hashsum_job', 'not_checkable_sample')
    op.drop_column('hashsum_job', 'not_checkable')
    op.drop_column('hashsum_job', 'md5_fallback')
    op.drop_column('hashsum_job', 'hash_type')
