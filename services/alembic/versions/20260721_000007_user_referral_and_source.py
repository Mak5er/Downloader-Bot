"""add user referral and source columns

Revision ID: 20260721_000007
Revises: 20260618_000006
Create Date: 2026-07-21 19:12:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '20260721_000007'
down_revision = '000006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('referred_by', sa.BigInteger(), nullable=True))
    op.add_column('users', sa.Column('source', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'source')
    op.drop_column('users', 'referred_by')
