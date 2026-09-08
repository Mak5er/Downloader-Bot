"""Separate users and groups, add has_dm, create group_members, decouple settings FK.

Revision ID: 20260908_000011
Revises: 20260726_000010
Create Date: 2026-09-08 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260908_000011"
down_revision: Union[str, Sequence[str], None] = "20260726_000010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [col["name"] for col in inspector.get_columns(table_name)]
    return column_name in columns


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = [idx["name"] for idx in inspector.get_indexes(table_name)]
    return index_name in indexes


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect_name = bind.dialect.name

    # 1. Add has_dm column to users
    if not _has_column("users", "has_dm"):
        op.add_column(
            "users",
            sa.Column("has_dm", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        )

    # 2. Backfill has_dm = true for existing positive private users
    op.execute(
        sa.text(
            """
            UPDATE users
            SET has_dm = true
            WHERE user_id > 0 AND chat_type = 'private'
            """
        )
    )

    # 3. Create index on users.has_dm
    if not _has_index("users", "ix_users_has_dm"):
        op.create_index("ix_users_has_dm", "users", ["has_dm"], unique=False)

    # 4. Create groups table
    if not _has_table("groups"):
        op.create_table(
            "groups",
            sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("username", sa.Text(), nullable=True),
            sa.Column("chat_type", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
            sa.Column("member_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=True,
            ),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=True,
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index("groups", "ix_groups_status"):
        op.create_index("ix_groups_status", "groups", ["status"], unique=False)

    # 5. Migrate negative user_id rows from users to groups
    op.execute(
        sa.text(
            """
            INSERT INTO groups (id, title, username, chat_type, status, member_count, created_at, updated_at)
            SELECT
                user_id AS id,
                user_name AS title,
                user_username AS username,
                'group' AS chat_type,
                COALESCE(status, 'active') AS status,
                0 AS member_count,
                CURRENT_TIMESTAMP AS created_at,
                CURRENT_TIMESTAMP AS updated_at
            FROM users
            WHERE user_id < 0
            ON CONFLICT (id) DO NOTHING
            """
        )
    )

    # 6. Drop foreign key constraint on settings.user_id referencing users.user_id
    # Must drop BEFORE deleting negative users to avoid CASCADE deletion of group settings!
    if dialect_name == "postgresql":
        for fk in inspector.get_foreign_keys("settings"):
            if fk.get("referred_table") == "users" and fk.get("constrained_columns") == ["user_id"]:
                op.drop_constraint(fk["name"], "settings", type_="foreignkey")
                break
    else:
        with op.batch_alter_table("settings", recreate="always") as batch_op:
            batch_op.drop_constraint("fk_settings_user_id_users", type_="foreignkey")

    # 7. Delete migrated negative user_id rows from users
    op.execute(
        sa.text(
            """
            DELETE FROM users
            WHERE user_id < 0
            """
        )
    )

    # 8. Create group_members table
    if not _has_table("group_members"):
        op.create_table(
            "group_members",
            sa.Column("group_id", sa.BigInteger(), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column(
                "last_seen_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=True,
            ),
            sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("group_id", "user_id"),
        )
    if not _has_index("group_members", "ix_group_members_group_id"):
        op.create_index("ix_group_members_group_id", "group_members", ["group_id"], unique=False)
    if not _has_index("group_members", "ix_group_members_user_id"):
        op.create_index("ix_group_members_user_id", "group_members", ["user_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    # 1. Drop group_members table
    if _has_table("group_members"):
        if _has_index("group_members", "ix_group_members_user_id"):
            op.drop_index("ix_group_members_user_id", table_name="group_members")
        if _has_index("group_members", "ix_group_members_group_id"):
            op.drop_index("ix_group_members_group_id", table_name="group_members")
        op.drop_table("group_members")

    # 2. Re-insert groups back into users
    op.execute(
        sa.text(
            """
            INSERT INTO users (user_id, user_name, user_username, chat_type, status, has_dm)
            SELECT
                id AS user_id,
                title AS user_name,
                username AS user_username,
                'public' AS chat_type,
                status,
                false AS has_dm
            FROM groups
            WHERE id < 0
            ON CONFLICT (user_id) DO NOTHING
            """
        )
    )

    # 3. Recreate foreign key on settings.user_id referencing users.user_id
    if dialect_name == "postgresql":
        op.create_foreign_key(
            "fk_settings_user_id_users",
            "settings",
            "users",
            ["user_id"],
            ["user_id"],
            ondelete="CASCADE",
        )
    else:
        with op.batch_alter_table("settings", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                "fk_settings_user_id_users",
                "users",
                ["user_id"],
                ["user_id"],
                ondelete="CASCADE",
            )

    # 4. Drop groups table
    if _has_table("groups"):
        if _has_index("groups", "ix_groups_status"):
            op.drop_index("ix_groups_status", table_name="groups")
        op.drop_table("groups")

    # 5. Drop ix_users_has_dm index and has_dm column
    if _has_index("users", "ix_users_has_dm"):
        op.drop_index("ix_users_has_dm", table_name="users")

    if dialect_name == "postgresql":
        if _has_column("users", "has_dm"):
            op.drop_column("users", "has_dm")
    else:
        with op.batch_alter_table("users", recreate="always") as batch_op:
            if _has_column("users", "has_dm"):
                batch_op.drop_column("has_dm")
