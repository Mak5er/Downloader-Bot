"""Add indexes for analytics event statistics queries.

Revision ID: 20260408_000003
Revises: 20260408_000002
Create Date: 2026-04-08 02:15:00
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260408_000003"
down_revision: Union[str, Sequence[str], None] = "20260408_000002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_analytics_events_created_at", "analytics_events", ["created_at"], unique=False)
    op.create_index(
        "ix_analytics_events_action_name_created_at",
        "analytics_events",
        ["action_name", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_analytics_events_action_name_created_at", table_name="analytics_events")
    op.drop_index("ix_analytics_events_created_at", table_name="analytics_events")
