"""add oauth_flow

Revision ID: 3f6a8c2e4d17
Revises: 7c1e2d9a5b31
Create Date: 2026-09-25 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3f6a8c2e4d17'
down_revision = '7c1e2d9a5b31'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('oauth_flow',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('code_verifier', sa.String(), nullable=False),
        sa.Column('token', sa.String(), nullable=True),
        sa.Column('drives', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('state'),
    )
    op.create_index(op.f('ix_oauth_flow_owner'), 'oauth_flow', ['owner'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_oauth_flow_owner'), table_name='oauth_flow')
    op.drop_table('oauth_flow')
