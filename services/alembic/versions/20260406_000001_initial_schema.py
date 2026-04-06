"""Initial schema for Downloader-Bot.

Revision ID: 20260406_000001
Revises:
Create Date: 2026-04-06 23:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260406_000001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "downloaded_files",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("file_id", sa.Text(), nullable=False),
        sa.Column("date_added", sa.TIMESTAMP(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("file_type", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )

    op.create_table(
        "users",
        sa.Column("user_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_name", sa.Text(), nullable=True),
        sa.Column("user_username", sa.Text(), nullable=True),
        sa.Column("chat_type", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "analytics_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_type", sa.Text(), nullable=True),
        sa.Column("action_name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "settings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("captions", sa.Text(), server_default=sa.text("'off'"), nullable=False),
        sa.Column("delete_message", sa.Text(), server_default=sa.text("'off'"), nullable=False),
        sa.Column("info_buttons", sa.Text(), server_default=sa.text("'off'"), nullable=False),
        sa.Column("url_button", sa.Text(), server_default=sa.text("'off'"), nullable=False),
        sa.Column("audio_button", sa.Text(), server_default=sa.text("'off'"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("analytics_events")
    op.drop_table("users")
    op.drop_table("downloaded_files")
