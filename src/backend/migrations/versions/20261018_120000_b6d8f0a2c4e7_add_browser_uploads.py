"""add copy_job.upload and upload_file

Uploads from the user's browser (drag and drop from the desktop): a copy job with
`upload` set and one upload_file row per file (managers/upload_manager.py).

Revision ID: b6d8f0a2c4e7
Revises: a3c5e7f9b2d4
Create Date: 2026-10-18 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b6d8f0a2c4e7'
down_revision = 'a3c5e7f9b2d4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('copy_job', sa.Column('upload', sa.JSON(), nullable=True))
    op.create_table(
        'upload_file',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('copy_job_id', sa.Integer(), nullable=False),
        sa.Column('index', sa.Integer(), nullable=False),
        sa.Column('path', sa.String(), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('bytes_done', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('state', sa.String(), server_default='pending', nullable=False),
        sa.Column('error', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['copy_job_id'], ['copy_job.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('copy_job_id', 'index'),
    )
    op.create_index(op.f('ix_upload_file_copy_job_id'), 'upload_file', ['copy_job_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_upload_file_copy_job_id'), table_name='upload_file')
    op.drop_table('upload_file')
    op.drop_column('copy_job', 'upload')
