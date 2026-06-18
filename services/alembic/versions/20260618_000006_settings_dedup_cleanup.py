"""settings dedup cleanup

Revision ID: 000006
Revises: 20260615_000005
Create Date: 2026-06-18
"""
from alembic import op
import sqlalchemy as sa

revision = "000006"
down_revision = "20260615_000005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DELETE FROM settings
        WHERE id NOT IN (
            SELECT MAX(id) FROM settings GROUP BY user_id
        )
    """)


def downgrade() -> None:
    pass
