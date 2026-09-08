import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.types import Chat, Message, User, ChatMemberUpdated
from aiogram.exceptions import TelegramBadRequest

from middlewares.chat_tracker import ChatTrackerMiddleware
import handlers.commands as cmd_mod
import handlers.user as user_mod
from services.storage.models import Base, User as UserModel, Group as GroupModel, GroupMember as GroupMemberModel
from services.storage.user_repository import UserRepositoryMixin
from sqlalchemy import create_engine, case, select


class MockDBWithTracking:
    """Mock DB implementing M1/M2 user repository interface with call inspection."""
    def __init__(self):
        self.upsert_user = AsyncMock()
        self.upsert_group = AsyncMock()
        self.record_group_member = AsyncMock()
        self.update_group_status = AsyncMock()
        self.update_group_member_count = AsyncMock()
        self.set_inactive = AsyncMock()
        self.upsert_chat = AsyncMock()


class StatefulInMemoryDB(UserRepositoryMixin):
    """Real SQLite-backed in-memory DB implementing UserRepositoryMixin SQL queries."""
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self._dialect_name = "sqlite"
        self._status_cache = {}

    def execute_upsert_user(self, user_id, user_name=None, user_username=None,
                            chat_type="private", language=None, has_dm=False,
                            status=None, referred_by=None, source=None):
        initial_status = status or "active"
        values = {
            "user_id": int(user_id),
            "user_name": user_name,
            "user_username": user_username,
            "chat_type": chat_type or "private",
            "language": language,
            "has_dm": has_dm,
            "status": initial_status,
        }
        if referred_by is not None:
            values["referred_by"] = referred_by
        if source is not None:
            values["source"] = source

        stmt = self._insert(UserModel).values(**values)
        set_dict = {
            "user_name": stmt.excluded.user_name,
            "user_username": stmt.excluded.user_username,
            "chat_type": stmt.excluded.chat_type,
            "has_dm": UserModel.has_dm | stmt.excluded.has_dm,
            "status": case(
                (UserModel.status == "ban", "ban"),
                (stmt.excluded.has_dm, "active"),
                else_=(stmt.excluded.status if status is not None else UserModel.status),
            ),
            "referred_by": case((UserModel.referred_by.is_(None), stmt.excluded.referred_by), else_=UserModel.referred_by),
            "source": case((UserModel.source.is_(None), stmt.excluded.source), else_=UserModel.source),
        }
        if language is not None:
            set_dict["language"] = stmt.excluded.language

        stmt = stmt.on_conflict_do_update(
            index_elements=[UserModel.user_id],
            set_=set_dict,
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def execute_upsert_group(self, chat_id, title=None, username=None,
                             chat_type=None, status="active", member_count=None):
        values = {
            "id": int(chat_id),
            "title": title,
            "username": username,
            "chat_type": chat_type or "group",
            "status": status,
            "member_count": member_count if member_count is not None else 0,
        }
        stmt = self._insert(GroupModel).values(**values)
        set_dict = {
            "title": stmt.excluded.title,
            "username": stmt.excluded.username,
            "chat_type": stmt.excluded.chat_type,
            "status": case(
                (GroupModel.status == "ban", "ban"),
                else_=stmt.excluded.status,
            ),
        }
        if member_count is not None:
            set_dict["member_count"] = stmt.excluded.member_count
        else:
            set_dict["member_count"] = GroupModel.member_count

        stmt = stmt.on_conflict_do_update(
            index_elements=[GroupModel.id],
            set_=set_dict,
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def execute_record_group_member(self, group_id, user_id):
        from sqlalchemy import func
        stmt = self._insert(GroupMemberModel).values(
            group_id=int(group_id),
            user_id=int(user_id),
            last_seen_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[GroupMemberModel.group_id, GroupMemberModel.user_id],
            set_={"last_seen_at": func.now()},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def get_user(self, user_id):
        with self.engine.connect() as conn:
            row = conn.execute(
                select(UserModel).where(UserModel.user_id == int(user_id))
            ).mappings().one_or_none()
            return dict(row) if row else None

    def get_group(self, group_id):
        with self.engine.connect() as conn:
            row = conn.execute(
                select(GroupModel).where(GroupModel.id == int(group_id))
            ).mappings().one_or_none()
            return dict(row) if row else None

    def get_group_member(self, group_id, user_id):
        with self.engine.connect() as conn:
            row = conn.execute(
                select(GroupMemberModel).where(
                    GroupMemberModel.group_id == int(group_id),
                    GroupMemberModel.user_id == int(user_id),
                )
            ).mappings().one_or_none()
            return dict(row) if row else None


# =========================================================================
# TEST 1: Group message from brand new user
# =========================================================================
@pytest.mark.asyncio
async def test_group_message_from_brand_new_user():
    db = MockDBWithTracking()
    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=75)
    middleware = ChatTrackerMiddleware(database=db, bot=bot)

    g_chat = MagicMock(spec=Chat, id=-1001234567890, type=ChatType.SUPERGROUP, title="Super Dev Group", username="superdev")
    g_user = MagicMock(spec=User, id=99999, full_name="New Contributor", username="newdev", language_code="ru", is_bot=False)
    msg = MagicMock(spec=Message, chat=g_chat, from_user=g_user, bot=bot)

    await middleware._process_message(msg)

    # 1. Group created in groups
    db.upsert_group.assert_awaited_once_with(
        chat_id=-1001234567890,
        title="Super Dev Group",
        username="superdev",
        chat_type="supergroup",
        status="active",
        member_count=75,
    )

    # 2. User created with has_dm=False and status=None
    db.upsert_user.assert_awaited_once_with(
        user_id=99999,
        user_name="New Contributor",
        user_username="newdev",
        chat_type="private",
        language="ru",
        has_dm=False,
        status=None,
    )

    # 3. Group member link recorded
    db.record_group_member.assert_awaited_once_with(
        group_id=-1001234567890,
        user_id=99999,
    )

    # Verify call arguments satisfy constraints: has_dm is False and status != "inactive"
    user_call_kwargs = db.upsert_user.await_args.kwargs
    assert user_call_kwargs["has_dm"] is False
    assert user_call_kwargs["status"] != "inactive"


# =========================================================================
# TEST 2 & 3: DM transition and sticky preservation in SQLite database
# =========================================================================
@pytest.mark.asyncio
async def test_has_dm_lifecycle_and_sticky_preservation_e2e():
    sqlite_db = StatefulInMemoryDB()
    mock_db = MockDBWithTracking()

    mock_db.upsert_user.side_effect = lambda **kwargs: sqlite_db.execute_upsert_user(**kwargs)
    mock_db.upsert_group.side_effect = lambda **kwargs: sqlite_db.execute_upsert_group(**kwargs)
    mock_db.record_group_member.side_effect = lambda **kwargs: sqlite_db.execute_record_group_member(**kwargs)

    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=120)
    middleware = ChatTrackerMiddleware(database=mock_db, bot=bot)

    g_chat = MagicMock(spec=Chat, id=-100999, type=ChatType.SUPERGROUP, title="Main Group", username="maingrp")
    p_chat = MagicMock(spec=Chat, id=55555, type=ChatType.PRIVATE)
    user = MagicMock(spec=User, id=55555, full_name="Charlie Root", username="charlieroot", language_code="en", is_bot=False)

    # --- STEP 1: Group message from brand new user ---
    msg1 = MagicMock(spec=Message, chat=g_chat, from_user=user, bot=bot)
    await middleware._process_message(msg1)

    db_user1 = sqlite_db.get_user(55555)
    db_group1 = sqlite_db.get_group(-100999)
    db_member1 = sqlite_db.get_group_member(-100999, 55555)

    assert db_user1 is not None
    assert not db_user1["has_dm"]
    assert db_user1["status"] != "inactive"
    assert db_user1["status"] == "active"  # default initial status

    assert db_group1 is not None
    assert db_group1["status"] == "active"
    assert db_group1["member_count"] == 120

    assert db_member1 is not None
    assert db_member1["group_id"] == -100999
    assert db_member1["user_id"] == 55555

    # --- STEP 2: Subsequent private message from same user ---
    msg2 = MagicMock(spec=Message, chat=p_chat, from_user=user, bot=bot)
    await middleware._process_message(msg2)

    db_user2 = sqlite_db.get_user(55555)
    assert db_user2["has_dm"]
    assert db_user2["status"] == "active"

    # --- STEP 3: Subsequent group message from same user (within cache TTL) ---
    mock_db.upsert_user.reset_mock()
    msg3 = MagicMock(spec=Message, chat=g_chat, from_user=user, bot=bot)
    await middleware._process_message(msg3)

    # Within 90s TTL, touch cache detects has_dm_bool=False is suppressed by cached_has_dm=True
    mock_db.upsert_user.assert_not_called()
    db_user3 = sqlite_db.get_user(55555)
    assert db_user3["has_dm"]
    assert db_user3["status"] == "active"

    # --- STEP 4: Subsequent group message from same user (AFTER cache TTL) ---
    # Advance monotonic clock by 120 seconds
    with patch("time.monotonic", return_value=time.monotonic() + 120.0):
        await middleware._process_message(msg3)

    # DB write called with has_dm=False, status=None
    mock_db.upsert_user.assert_called_once_with(
        user_id=55555,
        user_name="Charlie Root",
        user_username="charlieroot",
        chat_type="private",
        language="en",
        has_dm=False,
        status=None,
    )

    # Verify sticky preservation in SQLite database row!
    db_user4 = sqlite_db.get_user(55555)
    assert db_user4["has_dm"], "STICKY PRESERVATION FAILED: has_dm flipped from True to False!"
    assert db_user4["status"] == "active", "STICKY PRESERVATION FAILED: status overwritten!"


# =========================================================================
# TEST 4: Member count 1-hour caching and fallback on Telegram error
# =========================================================================
@pytest.mark.asyncio
async def test_member_count_caching_and_error_fallback():
    db = MockDBWithTracking()
    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=500)
    middleware = ChatTrackerMiddleware(database=db, bot=bot)

    g_chat = MagicMock(spec=Chat, id=-100888, type=ChatType.SUPERGROUP, title="Tech Chat", username="tech")
    user = MagicMock(spec=User, id=111, full_name="User1", username="u1", language_code="en", is_bot=False)
    msg = MagicMock(spec=Message, chat=g_chat, from_user=user, bot=bot)

    # 1. First call queries bot
    await middleware._process_message(msg)
    assert bot.get_chat_member_count.await_count == 1
    assert middleware._member_count_cache[-100888][1] == 500

    # 2. Second call 10 minutes later uses cache
    with patch("time.monotonic", return_value=time.monotonic() + 600.0):
        middleware._group_touch_cache.clear()  # clear group debounce to trigger ensure_group
        await middleware._process_message(msg)
        assert bot.get_chat_member_count.await_count == 1  # Not queried again!

    # 3. Third call 61 minutes later re-queries bot
    bot.get_chat_member_count.return_value = 505
    with patch("time.monotonic", return_value=time.monotonic() + 3700.0):
        middleware._group_touch_cache.clear()
        await middleware._process_message(msg)
        assert bot.get_chat_member_count.await_count == 2
        assert middleware._member_count_cache[-100888][1] == 505

    # 4. Telegram API error fallback: does not crash, falls back to cached count
    bot.get_chat_member_count.side_effect = TelegramBadRequest(method="getChatMemberCount", message="Chat not found")
    with patch("time.monotonic", return_value=time.monotonic() + 7500.0):
        middleware._group_touch_cache.clear()
        await middleware._process_message(msg)
        assert bot.get_chat_member_count.await_count == 3
        # Should fall back to last known count (505) without throwing exception
        last_group_call = db.upsert_group.await_args.kwargs
        assert last_group_call["member_count"] == 505


# =========================================================================
# TEST 5: Member touch 90s debouncing
# =========================================================================
@pytest.mark.asyncio
async def test_member_touch_debouncing():
    db = MockDBWithTracking()
    middleware = ChatTrackerMiddleware(database=db)

    # 10 rapid messages in group from same user
    for _ in range(10):
        await middleware._ensure_group_member(group_id=-100555, user_id=4242)

    assert db.record_group_member.await_count == 1

    # After 95 seconds, sends another message
    with patch("time.monotonic", return_value=time.monotonic() + 95.0):
        await middleware._ensure_group_member(group_id=-100555, user_id=4242)

    assert db.record_group_member.await_count == 2


# =========================================================================
# TEST 6: commands.py update_info private vs group behavior
# =========================================================================
@pytest.mark.asyncio
async def test_update_info_private_vs_group():
    mock_db = MockDBWithTracking()
    user_mod.db = mock_db
    user_mod._update_info_cache.clear()

    p_chat = MagicMock(spec=Chat, id=77, type=ChatType.PRIVATE)
    g_chat = MagicMock(spec=Chat, id=-88, type=ChatType.SUPERGROUP)
    user = MagicMock(spec=User, id=77, full_name="Sam", username="sam", language_code="en")

    # Command in group
    msg_group = MagicMock(spec=Message, chat=g_chat, from_user=user)
    await cmd_mod.update_info(msg_group)

    mock_db.upsert_user.assert_awaited_once_with(
        user_id=77,
        user_name="Sam",
        user_username="sam",
        chat_type="private",
        language="en",
        has_dm=False,
        status=None,
        referred_by=None,
        source=None,
    )

    # Subsequent /start in private chat within 120s must NOT be suppressed by group cache
    mock_db.upsert_user.reset_mock()
    msg_private = MagicMock(spec=Message, chat=p_chat, from_user=user)
    await cmd_mod.update_info(msg_private)

    mock_db.upsert_user.assert_awaited_once_with(
        user_id=77,
        user_name="Sam",
        user_username="sam",
        chat_type="private",
        language="en",
        has_dm=True,
        status="active",
        referred_by=None,
        source=None,
    )


# =========================================================================
# TEST 7: Bot membership join and kick handlers
# =========================================================================
@pytest.mark.asyncio
async def test_bot_membership_lifecycle():
    mock_db = MockDBWithTracking()
    mock_bot = MagicMock()
    mock_bot.get_chat_member_count = AsyncMock(return_value=300)
    mock_bot.send_message = AsyncMock()

    user_mod.db = mock_db
    user_mod.bot = mock_bot

    g_chat = MagicMock(spec=Chat, id=-100333, type=ChatType.SUPERGROUP, title="Omega Chat", username="omega")
    p_chat = MagicMock(spec=Chat, id=999, type=ChatType.PRIVATE, full_name="Solo User", username="solo")

    # 1. Bot joined group as MEMBER
    join_update = MagicMock(
        spec=ChatMemberUpdated,
        chat=g_chat,
        old_chat_member=MagicMock(status=ChatMemberStatus.LEFT),
        new_chat_member=MagicMock(status=ChatMemberStatus.MEMBER),
        bot=mock_bot,
    )
    await cmd_mod.handle_bot_membership(join_update)

    mock_db.upsert_group.assert_awaited_once_with(
        chat_id=-100333,
        title="Omega Chat",
        username="omega",
        chat_type="supergroup",
        status="active",
        member_count=300,
    )
    mock_bot.send_message.assert_awaited_once()

    # 2. Bot kicked from group
    mock_db.upsert_group.reset_mock()
    kick_update = MagicMock(
        spec=ChatMemberUpdated,
        chat=g_chat,
        old_chat_member=MagicMock(status=ChatMemberStatus.MEMBER),
        new_chat_member=MagicMock(status=ChatMemberStatus.KICKED),
        bot=mock_bot,
    )
    await cmd_mod.handle_bot_membership(kick_update)

    mock_db.update_group_status.assert_awaited_once_with(-100333, "kicked")
    mock_db.set_inactive.assert_not_awaited()  # NOT set_inactive on user!

    # 3. User blocks bot in private chat
    user_block_update = MagicMock(
        spec=ChatMemberUpdated,
        chat=p_chat,
        old_chat_member=MagicMock(status=ChatMemberStatus.MEMBER),
        new_chat_member=MagicMock(status=ChatMemberStatus.KICKED),
        bot=mock_bot,
    )
    await cmd_mod.handle_bot_membership(user_block_update)

    mock_db.set_inactive.assert_awaited_once_with(999)


# =========================================================================
# TEST 8: Inactive and Banned status preservation in DB
# =========================================================================
@pytest.mark.asyncio
async def test_banned_and_inactive_status_rules_in_db():
    sqlite_db = StatefulInMemoryDB()

    # Create a banned user
    sqlite_db.execute_upsert_user(
        user_id=888, user_name="Bad Actor", user_username="bad",
        chat_type="private", has_dm=True, status="ban"
    )
    assert sqlite_db.get_user(888)["status"] == "ban"

    # Banned user sends DM -> status MUST stay "ban"
    sqlite_db.execute_upsert_user(
        user_id=888, user_name="Bad Actor", user_username="bad",
        chat_type="private", has_dm=True, status="active"
    )
    assert sqlite_db.get_user(888)["status"] == "ban", "SECURITY FLAW: Banned user was unbanned by DM!"

    # Create an inactive user (blocked bot previously)
    sqlite_db.execute_upsert_user(
        user_id=777, user_name="Quiet User", user_username="quiet",
        chat_type="private", has_dm=True, status="inactive"
    )
    assert sqlite_db.get_user(777)["status"] == "inactive"

    # Inactive user sends message in a GROUP -> status MUST remain "inactive", NOT reactivated!
    sqlite_db.execute_upsert_user(
        user_id=777, user_name="Quiet User", user_username="quiet",
        chat_type="private", has_dm=False, status=None
    )
    assert sqlite_db.get_user(777)["status"] == "inactive", "FALSE REACTIVATION: Inactive user reactivated by group message!"

    # Inactive user sends message in DM -> status transitions to "active"
    sqlite_db.execute_upsert_user(
        user_id=777, user_name="Quiet User", user_username="quiet",
        chat_type="private", has_dm=True, status="active"
    )
    assert sqlite_db.get_user(777)["status"] == "active", "DM message failed to reactivate inactive user!"


# =========================================================================
# TEST 9: Bot users ignored
# =========================================================================
@pytest.mark.asyncio
async def test_bot_messages_ignored():
    mock_db = MockDBWithTracking()
    middleware = ChatTrackerMiddleware(database=mock_db)

    bot_user = MagicMock(spec=User, id=9999, is_bot=True, full_name="Robot", username="robot")
    g_chat = MagicMock(spec=Chat, id=-100111, type=ChatType.SUPERGROUP, title="Group")
    msg = MagicMock(spec=Message, chat=g_chat, from_user=bot_user)

    await middleware._process_message(msg)

    # Group is tracked, but bot user is NOT tracked in users or group_members
    mock_db.upsert_group.assert_awaited_once()
    mock_db.upsert_user.assert_not_awaited()
    mock_db.record_group_member.assert_not_awaited()


# =========================================================================
# TEST 10: Channel message without from_user
# =========================================================================
@pytest.mark.asyncio
async def test_channel_message_without_from_user():
    mock_db = MockDBWithTracking()
    bot = MagicMock(get_chat_member_count=AsyncMock(return_value=1500))
    middleware = ChatTrackerMiddleware(database=mock_db, bot=bot)

    channel = MagicMock(spec=Chat, id=-100777888, type=ChatType.CHANNEL, title="Official News", username="news")
    msg = MagicMock(spec=Message, chat=channel, from_user=None, bot=bot)

    await middleware._process_message(msg)

    # Channel should be upserted in groups without error, but no user upsert
    mock_db.upsert_group.assert_awaited_once_with(
        chat_id=-100777888,
        title="Official News",
        username="news",
        chat_type="channel",
        status="active",
        member_count=1500,
    )
    mock_db.upsert_user.assert_not_awaited()
    mock_db.record_group_member.assert_not_awaited()


# =========================================================================
# TEST 11: Group chat name fallbacks
# =========================================================================
@pytest.mark.asyncio
async def test_group_chat_name_fallbacks():
    mock_db = MockDBWithTracking()
    bot = MagicMock(get_chat_member_count=AsyncMock(return_value=10))
    middleware = ChatTrackerMiddleware(database=mock_db, bot=bot)

    # 1. Fallback to first_name + last_name
    chat1 = MagicMock(spec=Chat, id=-1001, type=ChatType.GROUP, title=None, full_name=None, first_name="Community", last_name="Hub", username=None)
    msg1 = MagicMock(spec=Message, chat=chat1, from_user=None, bot=bot)
    await middleware._process_message(msg1)
    assert mock_db.upsert_group.await_args.kwargs["title"] == "Community Hub"

    # 2. Fallback to "Chat <id>" when no names present
    mock_db.upsert_group.reset_mock()
    chat2 = MagicMock(spec=Chat, id=-1002, type=ChatType.GROUP, title=None, full_name=None, first_name=None, last_name=None, username=None)
    msg2 = MagicMock(spec=Message, chat=chat2, from_user=None, bot=bot)
    await middleware._process_message(msg2)
    assert mock_db.upsert_group.await_args.kwargs["title"] == "Chat -1002"


# =========================================================================
# TEST 12: User name change in group does not drop has_dm in DB
# =========================================================================
@pytest.mark.asyncio
async def test_user_name_change_in_group_preserves_has_dm_in_db():
    sqlite_db = StatefulInMemoryDB()
    mock_db = MockDBWithTracking()
    mock_db.upsert_user.side_effect = lambda **kwargs: sqlite_db.execute_upsert_user(**kwargs)
    mock_db.upsert_group.side_effect = lambda **kwargs: sqlite_db.execute_upsert_group(**kwargs)
    mock_db.record_group_member.side_effect = lambda **kwargs: sqlite_db.execute_record_group_member(**kwargs)

    bot = MagicMock(get_chat_member_count=AsyncMock(return_value=20))
    middleware = ChatTrackerMiddleware(database=mock_db, bot=bot)

    p_chat = MagicMock(spec=Chat, id=1234, type=ChatType.PRIVATE)
    g_chat = MagicMock(spec=Chat, id=-1004, type=ChatType.SUPERGROUP, title="Discussion", username=None)
    user_initial = MagicMock(spec=User, id=1234, full_name="Original Name", username="orig", language_code="en", is_bot=False)

    # User establishes DM
    await middleware._process_message(MagicMock(spec=Message, chat=p_chat, from_user=user_initial, bot=bot))
    assert sqlite_db.get_user(1234)["has_dm"]
    assert sqlite_db.get_user(1234)["user_name"] == "Original Name"

    # User changes name and speaks in group
    user_updated = MagicMock(spec=User, id=1234, full_name="Renamed User", username="renamed", language_code="en", is_bot=False)
    await middleware._process_message(MagicMock(spec=Message, chat=g_chat, from_user=user_updated, bot=bot))

    # Name is updated, but has_dm must remain True in DB!
    user_row = sqlite_db.get_user(1234)
    assert user_row["user_name"] == "Renamed User"
    assert user_row["has_dm"], "STICKY FAILURE: Name change in group erased has_dm flag in DB!"


# =========================================================================
# TEST 13: Legacy database fallback
# =========================================================================
@pytest.mark.asyncio
async def test_legacy_database_fallback():
    class LegacyDB:
        def __init__(self):
            self.upsert_chat = AsyncMock()
            self.set_inactive = AsyncMock()

    legacy_db = LegacyDB()
    bot = MagicMock(get_chat_member_count=AsyncMock(return_value=5))
    middleware = ChatTrackerMiddleware(database=legacy_db, bot=bot)

    # Private message with legacy DB
    p_chat = MagicMock(spec=Chat, id=555, type=ChatType.PRIVATE)
    user = MagicMock(spec=User, id=555, full_name="Legacy User", username="leg", language_code="en", is_bot=False)
    await middleware._process_message(MagicMock(spec=Message, chat=p_chat, from_user=user, bot=bot))

    legacy_db.upsert_chat.assert_awaited_once_with(
        user_id=555,
        user_name="Legacy User",
        user_username="leg",
        chat_type="private",
        language="en",
        status="active",
    )

    # Group message with legacy DB (new user)
    legacy_db.upsert_chat.reset_mock()
    user2 = MagicMock(spec=User, id=556, full_name="Group User", username="grp_usr", language_code="en", is_bot=False)
    g_chat = MagicMock(spec=Chat, id=-1005, type=ChatType.SUPERGROUP, title="Legacy Group", username="legacy_grp")
    await middleware._process_message(MagicMock(spec=Message, chat=g_chat, from_user=user2, bot=bot))

    # Group upserted as public chat, user upserted as private active
    assert legacy_db.upsert_chat.await_count == 2
    calls = legacy_db.upsert_chat.await_args_list
    assert calls[0].kwargs["user_id"] == -1005
    assert calls[0].kwargs["chat_type"] == "public"
    assert calls[1].kwargs["user_id"] == 556
    assert calls[1].kwargs["chat_type"] == "private"

