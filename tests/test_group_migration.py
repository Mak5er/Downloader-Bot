import pytest
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
from aiogram.enums import ChatType
from aiogram.types import Chat, Message
from aiogram.exceptions import TelegramMigrateToChat
from sqlalchemy import create_engine, select

from middlewares.chat_tracker import ChatTrackerMiddleware
from handlers import admin
from services.storage.models import Base, Group, GroupMember, Settings
from services.storage.user_repository import UserRepositoryMixin


class InMemoryDB(UserRepositoryMixin):
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self._dialect_name = "sqlite"
        self._status_cache = {}

    class _SessionCtx:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    def SessionLocal(self):
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=self.engine)
        session = Session()

        class SyncToAsyncSession:
            def __init__(self, sync_sess):
                self._s = sync_sess

            async def execute(self, stmt):
                return self._s.execute(stmt)

            def begin(self):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                if exc_type:
                    self._s.rollback()
                else:
                    self._s.commit()
                self._s.close()

        return self._SessionCtx(SyncToAsyncSession(session))


@pytest.mark.asyncio
async def test_migrate_group_chat_db_logic():
    db = InMemoryDB()
    old_id = -1001
    new_id = -1001001

    # Seed old group, settings, and group members
    with db.engine.begin() as conn:
        conn.execute(
            db._insert(Group).values(
                id=old_id,
                title="Old Group",
                username="oldgroup",
                chat_type="group",
                status="active",
                member_count=10,
            )
        )
        conn.execute(
            db._insert(Settings).values(
                id=1,
                user_id=old_id,
                video_quality="720p",
            )
        )
        conn.execute(
            db._insert(GroupMember).values(
                group_id=old_id,
                user_id=42,
            )
        )
        conn.execute(
            db._insert(GroupMember).values(
                group_id=old_id,
                user_id=43,
            )
        )

    # Perform migration
    await db.migrate_group_chat(old_id, new_id, new_title="Super Group", new_username="supergroup")

    with db.engine.connect() as conn:
        # Old group must be deleted
        old_g = conn.execute(select(Group).where(Group.id == old_id)).mappings().one_or_none()
        assert old_g is None

        # New group must exist with supergroup type and updated title
        new_g = conn.execute(select(Group).where(Group.id == new_id)).mappings().one_or_none()
        assert new_g is not None
        assert new_g["title"] == "Super Group"
        assert new_g["username"] == "supergroup"
        assert new_g["chat_type"] == "supergroup"
        assert new_g["member_count"] == 10

        # Settings must be transferred to new_id
        old_s = conn.execute(select(Settings).where(Settings.user_id == old_id)).mappings().one_or_none()
        assert old_s is None
        new_s = conn.execute(select(Settings).where(Settings.user_id == new_id)).mappings().one_or_none()
        assert new_s is not None
        assert new_s["video_quality"] == "720p"

        # Group members must be transferred to new_id
        old_m = conn.execute(select(GroupMember.user_id).where(GroupMember.group_id == old_id)).scalars().all()
        assert len(old_m) == 0
        new_m = conn.execute(select(GroupMember.user_id).where(GroupMember.group_id == new_id)).scalars().all()
        assert set(new_m) == {42, 43}


@pytest.mark.asyncio
async def test_chat_tracker_handles_migrate_to_chat_id():
    fake_db = MagicMock()
    fake_db.migrate_group_chat = AsyncMock()
    fake_db.upsert_group = AsyncMock()
    fake_db.upsert_user = AsyncMock()
    fake_db.record_group_member = AsyncMock()

    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=15)
    middleware = ChatTrackerMiddleware(database=fake_db, bot=bot)

    old_chat = MagicMock(spec=Chat, id=-1005, type=ChatType.GROUP, title="Legacy Chat", username="legacy")
    msg = MagicMock(
        spec=Message,
        chat=old_chat,
        from_user=SimpleNamespace(id=1, full_name="User1", username="u1", language_code="en", is_bot=False),
        migrate_to_chat_id=-1009999,
        migrate_from_chat_id=None,
        bot=bot,
    )

    await middleware._process_message(msg)

    fake_db.migrate_group_chat.assert_awaited_once_with(
        -1005, -1009999, new_title="Legacy Chat", new_username="legacy"
    )


@pytest.mark.asyncio
async def test_chat_tracker_handles_migrate_from_chat_id():
    fake_db = MagicMock()
    fake_db.migrate_group_chat = AsyncMock()
    fake_db.upsert_group = AsyncMock()
    fake_db.upsert_user = AsyncMock()
    fake_db.record_group_member = AsyncMock()

    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=25)
    middleware = ChatTrackerMiddleware(database=fake_db, bot=bot)

    new_chat = MagicMock(spec=Chat, id=-1009999, type=ChatType.SUPERGROUP, title="New Super Chat", username="newsuper")
    msg = MagicMock(
        spec=Message,
        chat=new_chat,
        from_user=SimpleNamespace(id=1, full_name="User1", username="u1", language_code="en", is_bot=False),
        migrate_to_chat_id=None,
        migrate_from_chat_id=-1005,
        bot=bot,
    )

    await middleware._process_message(msg)

    fake_db.migrate_group_chat.assert_awaited_once_with(
        -1005, -1009999, new_title="New Super Chat", new_username="newsuper"
    )


@pytest.mark.asyncio
async def test_check_active_groups_handles_telegram_migrate_to_chat(monkeypatch):
    old_group = SimpleNamespace(id=-2001, title="Upgrading Group")
    fake_db = MagicMock()
    fake_db.get_active_groups = AsyncMock(return_value=[old_group])
    fake_db.migrate_group_chat = AsyncMock()
    fake_db.update_group_member_count = AsyncMock()
    fake_db.update_group_status = AsyncMock()

    bot = MagicMock()
    def fake_member_count(chat_id):
        if chat_id == -2001:
            raise TelegramMigrateToChat(method=MagicMock(chat_id=-2001), message="migrated", migrate_to_chat_id=-2001001)
        return 45

    bot.get_chat_member_count = AsyncMock(side_effect=fake_member_count)
    bot.send_chat_action = AsyncMock()

    call = MagicMock()
    call.answer = AsyncMock()
    call.from_user = SimpleNamespace(id=1)
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    call.message = MagicMock(edit_text=AsyncMock(return_value=status_msg))

    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "db", fake_db)
    monkeypatch.setattr(admin, "bot", bot)
    monkeypatch.setattr(admin, "_ADMIN_THROTTLE_SECONDS", 0.0)

    await admin.check_active_groups(call)

    fake_db.migrate_group_chat.assert_awaited_once_with(-2001, -2001001)
    fake_db.update_group_member_count.assert_awaited_once_with(-2001001, 45)
    fake_db.update_group_status.assert_awaited_once_with(-2001001, "active")
    completed_text = status_msg.edit_text.await_args[0][0]
    assert "Reachable (bot member): <b>1</b>" in completed_text
    assert "45" in completed_text


@pytest.mark.asyncio
async def test_deliver_mailing_handles_telegram_migrate_to_chat(monkeypatch):
    fake_db = MagicMock()
    fake_db.migrate_group_chat = AsyncMock()
    fake_db.update_group_status = AsyncMock()

    bot = MagicMock()
    calls = []
    async def fake_copy(*args, **kwargs):
        chat_id = kwargs.get("chat_id") or (args[0] if args else None)
        calls.append(chat_id)
        if chat_id == -3001:
            raise TelegramMigrateToChat(method=MagicMock(chat_id=-3001), message="migrated", migrate_to_chat_id=-3001001)
        return MagicMock()

    bot.copy_message = AsyncMock(side_effect=fake_copy)

    monkeypatch.setattr(admin, "db", fake_db)
    monkeypatch.setattr(admin, "bot", bot)
    monkeypatch.setattr(admin, "_ADMIN_THROTTLE_SECONDS", 0.0)

    target = SimpleNamespace(id=-3001, status="active")
    await admin._deliver_mailing_message(target, sender_id=10, message_id=50)

    fake_db.migrate_group_chat.assert_awaited_once_with(-3001, -3001001)
    fake_db.update_group_status.assert_awaited_once_with(-3001001, "active")
    assert calls == [-3001, -3001001]
