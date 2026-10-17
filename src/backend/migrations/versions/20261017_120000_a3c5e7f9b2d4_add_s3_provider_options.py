"""add cloud_connection.s3_provider, s3_chunked, s3_skip_checks

s3_provider is rclone's provider of an S3 connection (utils/s3_providers.py). Existing S3
connections get what rclone used for them so far: AWS without an endpoint, Other with
one. s3_chunked (default true: multipart from 32 MiB) and s3_skip_checks (AWS and Ceph
only) are new options of the connection.

Revision ID: a3c5e7f9b2d4
Revises: f2a7c9d4e6b1
Create Date: 2026-10-17 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3c5e7f9b2d4'
down_revision = 'f2a7c9d4e6b1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('cloud_connection', sa.Column('s3_provider', sa.String(), nullable=True))
    op.add_column('cloud_connection', sa.Column(
        's3_chunked', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('cloud_connection', sa.Column('s3_skip_checks', sa.String(), nullable=True))
    op.execute("""
        UPDATE cloud_connection
        SET s3_provider = CASE WHEN COALESCE(TRIM(s3_endpoint), '') = '' THEN 'AWS' ELSE 'Other' END
        WHERE type = 's3'
    """)


def downgrade():
    op.drop_column('cloud_connection', 's3_skip_checks')
    op.drop_column('cloud_connection', 's3_chunked')
    op.drop_column('cloud_connection', 's3_provider')
