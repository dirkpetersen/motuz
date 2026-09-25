"""add profile_source and profile_name (credentials from the user's home directory)

Revision ID: 5b9e0f3c7a42
Revises: 3f6a8c2e4d17
Create Date: 2026-09-26 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5b9e0f3c7a42'
down_revision = '3f6a8c2e4d17'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('cloud_connection', sa.Column('profile_source', sa.String(), nullable=True))
    op.add_column('cloud_connection', sa.Column('profile_name', sa.String(), nullable=True))


def downgrade():
    op.drop_column('cloud_connection', 'profile_name')
    op.drop_column('cloud_connection', 'profile_source')
