"""add video_quality, as_document, and audio_format columns to settings

Revision ID: 20260722_000008
Revises: 20260721_000007
Create Date: 2026-07-22 02:07:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '20260722_000008'
down_revision = '20260721_000007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('settings', sa.Column('video_quality', sa.Text(), server_default='best', nullable=False))
    op.add_column('settings', sa.Column('as_document', sa.Text(), server_default='off', nullable=False))
    op.add_column('settings', sa.Column('audio_format', sa.Text(), server_default='mp3', nullable=False))


def downgrade() -> None:
    op.drop_column('settings', 'audio_format')
    op.drop_column('settings', 'as_document')
    op.drop_column('settings', 'video_quality')
