"""Add index on analytics_events.user_id and downloaded_files.date_added.

Revision ID: 20260615_000005
Revises: 20260525_000004
Create Date: 2026-06-15 18:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260615_000005"
down_revision: Union[str, Sequence[str], None] = "20260525_000004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    index_names = {index["name"] for index in inspector.get_indexes(table_name)}
    return index_name in index_names


def upgrade() -> None:
    if not _has_index("analytics_events", "ix_analytics_events_user_id"):
        op.create_index("ix_analytics_events_user_id", "analytics_events", ["user_id"], unique=False)
    if not _has_index("downloaded_files", "ix_downloaded_files_date_added"):
        op.create_index("ix_downloaded_files_date_added", "downloaded_files", ["date_added"], unique=False)


def downgrade() -> None:
    if _has_index("downloaded_files", "ix_downloaded_files_date_added"):
        op.drop_index("ix_downloaded_files_date_added", table_name="downloaded_files")
    if _has_index("analytics_events", "ix_analytics_events_user_id"):
        op.drop_index("ix_analytics_events_user_id", table_name="analytics_events")
