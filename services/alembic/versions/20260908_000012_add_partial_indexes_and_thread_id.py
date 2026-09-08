"""Add partial indexes and last_thread_id for groups.

Revision ID: 20260908_000012
Revises: 20260908_000011
Create Date: 2026-09-08 17:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260908_000012"
down_revision: Union[str, Sequence[str], None] = "20260908_000011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [col["name"] for col in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Add last_thread_id column to groups table if not present
    if not _has_column("groups", "last_thread_id"):
        op.add_column("groups", sa.Column("last_thread_id", sa.BigInteger(), nullable=True))

    # 2. Add PostgreSQL-specific partial indexes for fast active lookups
    if dialect == "postgresql":
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS ix_users_active_dm ON users (user_id) "
                "WHERE has_dm IS TRUE AND status = 'active'"
            )
        )
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS ix_groups_active ON groups (id) "
                "WHERE status = 'active'"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute(sa.text("DROP INDEX IF EXISTS ix_users_active_dm"))
        op.execute(sa.text("DROP INDEX IF EXISTS ix_groups_active"))

    if _has_column("groups", "last_thread_id"):
        op.drop_column("groups", "last_thread_id")
