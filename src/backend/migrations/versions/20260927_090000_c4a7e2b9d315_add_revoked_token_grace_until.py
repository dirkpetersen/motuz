"""add revoked_token.grace_until

Refresh tokens are rotated on every refresh: the old one is revoked with a short grace
window, so that browser tabs refreshing at the same moment both succeed. Existing rows
stay NULL (revoked without grace).

Revision ID: c4a7e2b9d315
Revises: b8d4e6f1a902
Create Date: 2026-09-27 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4a7e2b9d315'
down_revision = 'b8d4e6f1a902'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('revoked_token', sa.Column('grace_until', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('revoked_token', 'grace_until')
