"""add cloud_connection.skip_tls_verify

Lets a connection to S3 compatible storage with a certificate that does not match its
host name (or is self-signed) skip TLS certificate verification, for that connection's
remote only (RcloneConnection._formatCredentials).

Revision ID: f2a7c9d4e6b1
Revises: e4b8c2d6f1a3
Create Date: 2026-10-15 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f2a7c9d4e6b1'
down_revision = 'e4b8c2d6f1a3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('cloud_connection', sa.Column(
        'skip_tls_verify', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('cloud_connection', 'skip_tls_verify')
