"""add copy_job.space_check

The note of the free space check (MOTUZ_MAX_FILL_PERCENT) when a copy job or upload to
the local filesystem is created (managers/space_manager.py): checked, or not checked
and why.

Revision ID: c7e9a1b3d5f8
Revises: b6d8f0a2c4e7
Create Date: 2026-10-18 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7e9a1b3d5f8'
down_revision = 'b6d8f0a2c4e7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('copy_job', sa.Column('space_check', sa.String(), nullable=True))


def downgrade():
    op.drop_column('copy_job', 'space_check')
