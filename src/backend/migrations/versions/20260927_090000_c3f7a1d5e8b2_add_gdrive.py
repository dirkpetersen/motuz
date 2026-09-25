"""add Google Drive connection fields

gdrive_token (rclone token JSON), gdrive_root_folder_id, gdrive_team_drive (shared
drive id) and the server-controlled gdrive_client_id: the OAuth client that issued
the token, NULL for rclone's public app (pasted tokens).

Revision ID: c3f7a1d5e8b2
Revises: b8d4e6f1a902
Create Date: 2026-09-27 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3f7a1d5e8b2'
down_revision = 'b8d4e6f1a902'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('cloud_connection', sa.Column('gdrive_token', sa.String(), nullable=True))
    op.add_column('cloud_connection', sa.Column('gdrive_root_folder_id', sa.String(), nullable=True))
    op.add_column('cloud_connection', sa.Column('gdrive_team_drive', sa.String(), nullable=True))
    op.add_column('cloud_connection', sa.Column('gdrive_client_id', sa.String(), nullable=True))


def downgrade():
    op.drop_column('cloud_connection', 'gdrive_client_id')
    op.drop_column('cloud_connection', 'gdrive_team_drive')
    op.drop_column('cloud_connection', 'gdrive_root_folder_id')
    op.drop_column('cloud_connection', 'gdrive_token')
