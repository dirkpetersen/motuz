"""add copy_job.egress_estimate and hashsum_job.egress_estimate

The AWS egress cost the user confirmed for a job that reads out of AWS
(utils/egress_cost.py, managers/egress_manager.py); NULL when no AWS fee applies.

Revision ID: d2f4a6c8e0b1
Revises: c8e1a3f5d7b9
Create Date: 2026-10-18 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd2f4a6c8e0b1'
down_revision = 'c8e1a3f5d7b9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('copy_job', sa.Column('egress_estimate', sa.JSON(), nullable=True))
    op.add_column('hashsum_job', sa.Column('egress_estimate', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('hashsum_job', 'egress_estimate')
    op.drop_column('copy_job', 'egress_estimate')
