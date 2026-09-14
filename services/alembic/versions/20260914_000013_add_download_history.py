"""Add download_history table.

Revision ID: 20260914_000013
Revises: 20260908_000012
Create Date: 2026-09-14 15:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260914_000013"
down_revision: Union[str, Sequence[str], None] = "20260908_000012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "download_history" not in tables:
        op.create_table(
            "download_history",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.user_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("chat_id", sa.BigInteger(), nullable=True),
            sa.Column("chat_type", sa.Text(), nullable=True),
            sa.Column("service", sa.Text(), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("file_type", sa.Text(), nullable=True),
            sa.Column("file_id", sa.Text(), nullable=True),
            sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("duration_seconds", sa.Float(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'success'")),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_download_history_user_id", "download_history", ["user_id"])
        op.create_index("ix_download_history_created_at", "download_history", ["created_at"])
        op.create_index("ix_download_history_service", "download_history", ["service"])
        op.create_index("ix_download_history_status", "download_history", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "download_history" in tables:
        op.drop_index("ix_download_history_status", table_name="download_history")
        op.drop_index("ix_download_history_service", table_name="download_history")
        op.drop_index("ix_download_history_created_at", table_name="download_history")
        op.drop_index("ix_download_history_user_id", table_name="download_history")
        op.drop_table("download_history")
