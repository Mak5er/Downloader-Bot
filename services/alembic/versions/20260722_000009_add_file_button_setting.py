"""add file_button column to settings

Revision ID: 20260722_000009
Revises: 20260722_000008
Create Date: 2026-07-22 02:16:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '20260722_000009'
down_revision = '20260722_000008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('settings', sa.Column('file_button', sa.Text(), server_default='off', nullable=False))


def downgrade() -> None:
    op.drop_column('settings', 'file_button')
