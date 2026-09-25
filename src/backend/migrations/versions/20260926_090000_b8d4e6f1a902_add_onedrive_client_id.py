"""add onedrive_client_id

Remembers which app registration issued a OneDrive token, so that the token broker
refreshes it with the same app. Existing rows stay NULL, which means rclone's public
app: every connection before this revision was created with it.

Revision ID: b8d4e6f1a902
Revises: 3f6a8c2e4d17
Create Date: 2026-09-26 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8d4e6f1a902'
down_revision = '3f6a8c2e4d17'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('cloud_connection', sa.Column('onedrive_client_id', sa.String(), nullable=True))
    op.add_column('oauth_flow', sa.Column('client_id', sa.String(), nullable=True))


def downgrade():
    op.drop_column('oauth_flow', 'client_id')
    op.drop_column('cloud_connection', 'onedrive_client_id')
