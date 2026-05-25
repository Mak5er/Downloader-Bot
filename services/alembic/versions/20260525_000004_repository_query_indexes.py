"""Add indexes for repository query paths.

Revision ID: 20260525_000004
Revises: 20260408_000003
Create Date: 2026-05-25 17:45:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260525_000004"
down_revision: Union[str, Sequence[str], None] = "20260408_000003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_users_user_username", "users", ["user_username"]),
    ("ix_users_chat_type", "users", ["chat_type"]),
    ("ix_users_status", "users", ["status"]),
    ("ix_settings_user_id", "settings", ["user_id"]),
    ("ix_analytics_events_action_name", "analytics_events", ["action_name"]),
    ("ix_analytics_events_created_action", "analytics_events", ["created_at", "action_name"]),
)


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    index_names = {index["name"] for index in inspector.get_indexes(table_name)}
    return index_name in index_names


def upgrade() -> None:
    for index_name, table_name, columns in INDEXES:
        if not _has_index(table_name, index_name):
            op.create_index(index_name, table_name, columns, unique=False)


def downgrade() -> None:
    for index_name, table_name, _columns in reversed(INDEXES):
        if _has_index(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)
