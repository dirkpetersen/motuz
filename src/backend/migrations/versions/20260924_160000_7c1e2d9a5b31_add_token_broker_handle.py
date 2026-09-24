"""add token_broker_handle

Revision ID: 7c1e2d9a5b31
Revises: 42a4b7401b23
Create Date: 2026-09-24 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7c1e2d9a5b31'
down_revision = '42a4b7401b23'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('cloud_connection', sa.Column('token_broker_handle', sa.String(), nullable=True))
    op.create_unique_constraint('uq_cloud_connection_token_broker_handle', 'cloud_connection', ['token_broker_handle'])


def downgrade():
    op.drop_constraint('uq_cloud_connection_token_broker_handle', 'cloud_connection', type_='unique')
    op.drop_column('cloud_connection', 'token_broker_handle')
