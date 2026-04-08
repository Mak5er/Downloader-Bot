"""Enforce one-row-per-user settings and cascade deletes.

Revision ID: 20260408_000002
Revises: 20260406_000001
Create Date: 2026-04-08 23:55:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260408_000002"
down_revision: Union[str, Sequence[str], None] = "20260406_000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _deduplicate_settings() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM settings
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id DESC) AS row_num
                    FROM settings
                    WHERE user_id IS NOT NULL
                ) ranked
                WHERE ranked.row_num > 1
            )
            """
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect_name = bind.dialect.name

    _deduplicate_settings()

    if dialect_name == "postgresql":
        for fk in inspector.get_foreign_keys("settings"):
            if fk.get("referred_table") == "users" and fk.get("constrained_columns") == ["user_id"]:
                op.drop_constraint(fk["name"], "settings", type_="foreignkey")
                break

        with op.batch_alter_table("settings") as batch_op:
            batch_op.create_unique_constraint("uq_settings_user_id", ["user_id"])
            batch_op.create_foreign_key(
                "fk_settings_user_id_users",
                "users",
                ["user_id"],
                ["user_id"],
                ondelete="CASCADE",
            )
        return

    with op.batch_alter_table("settings", recreate="always") as batch_op:
        batch_op.create_unique_constraint("uq_settings_user_id", ["user_id"])
        batch_op.create_foreign_key(
            "fk_settings_user_id_users",
            "users",
            ["user_id"],
            ["user_id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == "postgresql":
        with op.batch_alter_table("settings") as batch_op:
            batch_op.drop_constraint("uq_settings_user_id", type_="unique")
            batch_op.drop_constraint("fk_settings_user_id_users", type_="foreignkey")
            batch_op.create_foreign_key(
                "settings_user_id_fkey",
                "users",
                ["user_id"],
                ["user_id"],
            )
        return

    with op.batch_alter_table("settings", recreate="always") as batch_op:
        batch_op.drop_constraint("uq_settings_user_id", type_="unique")
        batch_op.drop_constraint("fk_settings_user_id_users", type_="foreignkey")
        batch_op.create_foreign_key(
            "settings_user_id_fkey",
            "users",
            ["user_id"],
            ["user_id"],
        )
