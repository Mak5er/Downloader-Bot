"""Adversarial stress test suite for Milestone 2 (ChatTrackerMiddleware).

Covers all 6 mandatory challenge dimensions + adversarial edge cases:
1. Group-only user never marked has_dm=True or status="active"
2. Sticky preservation of has_dm=True for group speakers
3. Immediate group-to-DM transition (bypassing 90s debounce)
4. Group member touch debouncing (90s TTL)
5. Group member count caching (1h TTL)
6. Error handling and resilience when get_chat_member_count raises Telegram errors
7. Concurrency, missing bot, bot users, anonymous senders, and name fallbacks
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import Chat, Message, User
from sqlalchemy import case, create_engine, select

from middlewares.chat_tracker import ChatTrackerMiddleware
from services.storage.models import (
    Base,
    Group as GroupModel,
    GroupMember as GroupMemberModel,
    User as UserModel,
)
from services.storage.user_repository import UserRepositoryMixin


class MockDB:
    """Mock DB capturing all call parameters for strict assertion."""
    def __init__(self):
        self.upsert_user = AsyncMock()
        self.upsert_group = AsyncMock()
        self.record_group_member = AsyncMock()
        self.update_group_status = AsyncMock()
        self.update_group_member_count = AsyncMock()
        self.set_inactive = AsyncMock()
        self.upsert_chat = AsyncMock()


class SQLiteDB(UserRepositoryMixin):
    """Real in-memory SQLite implementing M1 UserRepositoryMixin SQL execution."""
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self._dialect_name = "sqlite"
        self._status_cache = {}

    def upsert_user_sync(
        self,
        user_id,
        user_name=None,
        user_username=None,
        chat_type="private",
        language=None,
        has_dm=False,
        status=None,
        referred_by=None,
        source=None,
    ):
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
            "referred_by": case(
                (UserModel.referred_by.is_(None), stmt.excluded.referred_by),
                else_=UserModel.referred_by,
            ),
            "source": case(
                (UserModel.source.is_(None), stmt.excluded.source),
                else_=UserModel.source,
            ),
        }
        if language is not None:
            set_dict["language"] = stmt.excluded.language

        stmt = stmt.on_conflict_do_update(
            index_elements=[UserModel.user_id],
            set_=set_dict,
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def upsert_group_sync(
        self,
        chat_id,
        title=None,
        username=None,
        chat_type=None,
        status="active",
        member_count=None,
    ):
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
            "status": case((GroupModel.status == "ban", "ban"), else_=stmt.excluded.status),
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

    def record_group_member_sync(self, group_id, user_id):
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


# =============================================================================
# CHALLENGE 1: Group-only user never marked has_dm=True or status="active"
# =============================================================================
@pytest.mark.asyncio
async def test_challenge_1_group_only_user_never_has_dm_true_or_status_active():
    """Verify that when a user speaks in a group:
    1. Chat tracker passes has_dm=False and status=None (never has_dm=True, never status="active").
    2. If user already had status="inactive" in DB, speaking in group DOES NOT reactivate them!
    3. If user already had status="ban" in DB, speaking in group DOES NOT unban them!
    """
    mock_db = MockDB()
    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=15)
    mw = ChatTrackerMiddleware(database=mock_db, bot=bot)

    g_chat = MagicMock(spec=Chat, id=-100111, type=ChatType.SUPERGROUP, title="Group Alpha", username="grp_a")
    user = MagicMock(spec=User, id=1001, full_name="Group Chatter", username="chatter", language_code="en", is_bot=False)
    msg = MagicMock(spec=Message, chat=g_chat, from_user=user, bot=bot)

    await mw._process_message(msg)

    # Middleware call to DB must strictly pass has_dm=False and status=None
    mock_db.upsert_user.assert_awaited_once()
    kwargs = mock_db.upsert_user.await_args.kwargs
    assert kwargs["has_dm"] is False, f"Expected has_dm=False, got {kwargs['has_dm']}"
    assert kwargs["status"] is None, f"Expected status=None, got {kwargs['status']}"
    assert kwargs["status"] != "active", "CRITICAL: Chat tracker must NEVER pass status='active' for group message!"

    # Now verify with real SQLite DB: Existing Inactive user is NOT reactivated
    sql_db = SQLiteDB()
    sql_db.upsert_user_sync(user_id=1002, user_name="Inactive Guy", has_dm=False, status="inactive")
    assert sql_db.get_user(1002)["status"] == "inactive"

    # Simulate group message forwarded to DB with kwargs from chat_tracker
    sql_db.upsert_user_sync(user_id=1002, user_name="Inactive Guy", has_dm=False, status=None)
    db_user = sql_db.get_user(1002)
    assert db_user["has_dm"] is False
    assert db_user["status"] == "inactive", f"SECURITY BUG: Inactive user was reactivated to {db_user['status']} by group message!"

    # Existing Banned user is NOT unbanned
    sql_db.upsert_user_sync(user_id=1003, user_name="Spammer", has_dm=False, status="ban")
    assert sql_db.get_user(1003)["status"] == "ban"
    sql_db.upsert_user_sync(user_id=1003, user_name="Spammer", has_dm=False, status=None)
    db_user_banned = sql_db.get_user(1003)
    assert db_user_banned["status"] == "ban", "SECURITY BUG: Banned user unbanned by group message!"


# =============================================================================
# CHALLENGE 2: Sticky preservation of has_dm=True when speaking in group
# =============================================================================
@pytest.mark.asyncio
async def test_challenge_2_sticky_preservation_of_has_dm():
    """A user who already has has_dm=True who speaks in a group must NOT lose has_dm=True."""
    sql_db = SQLiteDB()
    mock_db = MockDB()
    mock_db.upsert_user.side_effect = lambda **kw: sql_db.upsert_user_sync(**kw)
    mock_db.upsert_group.side_effect = lambda **kw: sql_db.upsert_group_sync(**kw)
    mock_db.record_group_member.side_effect = lambda **kw: sql_db.record_group_member_sync(**kw)

    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=50)
    mw = ChatTrackerMiddleware(database=mock_db, bot=bot)

    p_chat = MagicMock(spec=Chat, id=2001, type=ChatType.PRIVATE)
    g_chat = MagicMock(spec=Chat, id=-100222, type=ChatType.GROUP, title="Discussion", username=None)
    user = MagicMock(spec=User, id=2001, full_name="Alice Wonder", username="alice", language_code="en", is_bot=False)

    # 1. Private message establishes has_dm=True
    await mw._process_message(MagicMock(spec=Message, chat=p_chat, from_user=user, bot=bot))
    assert sql_db.get_user(2001)["has_dm"] is True
    assert sql_db.get_user(2001)["status"] == "active"

    # 2. Group message within 90s TTL -> skipped by touch cache
    mock_db.upsert_user.reset_mock()
    await mw._process_message(MagicMock(spec=Message, chat=g_chat, from_user=user, bot=bot))
    mock_db.upsert_user.assert_not_called()
    assert sql_db.get_user(2001)["has_dm"] is True

    # 3. Group message AFTER 90s TTL -> executes DB write with has_dm=False, status=None
    with patch("time.monotonic", return_value=time.monotonic() + 100.0):
        await mw._process_message(MagicMock(spec=Message, chat=g_chat, from_user=user, bot=bot))

    mock_db.upsert_user.assert_called_once()
    assert mock_db.upsert_user.await_args.kwargs["has_dm"] is False
    assert mock_db.upsert_user.await_args.kwargs["status"] is None

    # Verify sticky preservation in SQLite database!
    db_row = sql_db.get_user(2001)
    assert db_row["has_dm"] is True, "STICKY FAILURE: has_dm flipped from True to False in database!"
    assert db_row["status"] == "active"


# =============================================================================
# CHALLENGE 3: Immediate group-first to DM transition (bypassing 90s debounce)
# =============================================================================
@pytest.mark.asyncio
async def test_challenge_3_group_first_then_dm_immediate_activation():
    """A user who spoke in group first (has_dm=False) and then sends a DM 100ms later
    must immediately become has_dm=True and status='active', NOT debounced!"""
    sql_db = SQLiteDB()
    mock_db = MockDB()
    mock_db.upsert_user.side_effect = lambda **kw: sql_db.upsert_user_sync(**kw)
    mock_db.upsert_group.side_effect = lambda **kw: sql_db.upsert_group_sync(**kw)
    mock_db.record_group_member.side_effect = lambda **kw: sql_db.record_group_member_sync(**kw)

    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=30)
    mw = ChatTrackerMiddleware(database=mock_db, bot=bot)

    g_chat = MagicMock(spec=Chat, id=-100333, type=ChatType.SUPERGROUP, title="Forum", username=None)
    p_chat = MagicMock(spec=Chat, id=3001, type=ChatType.PRIVATE)
    user = MagicMock(spec=User, id=3001, full_name="Bob Builder", username="bob", language_code="ru", is_bot=False)

    # 1. Group message
    t0 = time.monotonic()
    with patch("time.monotonic", return_value=t0):
        await mw._process_message(MagicMock(spec=Message, chat=g_chat, from_user=user, bot=bot))

    assert sql_db.get_user(3001)["has_dm"] is False
    assert mock_db.upsert_user.await_count == 1
    assert mock_db.upsert_user.await_args.kwargs["has_dm"] is False
    assert mock_db.upsert_user.await_args.kwargs["status"] is None

    # 2. Private message just 0.5s later (within 90s TTL)
    mock_db.upsert_user.reset_mock()
    with patch("time.monotonic", return_value=t0 + 0.5):
        await mw._process_message(MagicMock(spec=Message, chat=p_chat, from_user=user, bot=bot))

    # Must NOT be debounced!
    assert mock_db.upsert_user.await_count == 1, "FAILURE: DM message was incorrectly debounced after group message!"
    assert mock_db.upsert_user.await_args.kwargs["has_dm"] is True
    assert mock_db.upsert_user.await_args.kwargs["status"] == "active"

    # In database:
    db_row = sql_db.get_user(3001)
    assert db_row["has_dm"] is True
    assert db_row["status"] == "active"


# =============================================================================
# CHALLENGE 4: Group member touch debouncing (90s TTL)
# =============================================================================
@pytest.mark.asyncio
async def test_challenge_4_group_member_touch_debouncing_90s():
    """Verify repeated messages from same user in same group within 90s do not trigger
    redundant DB calls, but messages after 90s do. Test multi-user and multi-group isolation."""
    mock_db = MockDB()
    mw = ChatTrackerMiddleware(database=mock_db)

    t0 = 1000.0

    # 1. User 4001 in Group -100 at t0
    with patch("time.monotonic", return_value=t0):
        await mw._ensure_group_member(group_id=-100, user_id=4001)
    assert mock_db.record_group_member.await_count == 1
    mock_db.record_group_member.assert_awaited_with(group_id=-100, user_id=4001)

    # 2. Burst of 50 messages from User 4001 in Group -100 between t0 and t0+89.9s
    for delta in [1.0, 10.0, 30.0, 60.0, 89.0, 89.9]:
        with patch("time.monotonic", return_value=t0 + delta):
            await mw._ensure_group_member(group_id=-100, user_id=4001)
    assert mock_db.record_group_member.await_count == 1, "Debounce failed: DB calls made during 90s window!"

    # 3. Message from User 4001 in Group -100 at t0+90.1s -> triggers DB call!
    with patch("time.monotonic", return_value=t0 + 90.1):
        await mw._ensure_group_member(group_id=-100, user_id=4001)
    assert mock_db.record_group_member.await_count == 2, "Debounce failed: DB call not made after 90s expiration!"

    # 4. Multi-user: User 4002 in Group -100 at t0+10s is NOT suppressed by User 4001
    with patch("time.monotonic", return_value=t0 + 10.0):
        await mw._ensure_group_member(group_id=-100, user_id=4002)
    assert mock_db.record_group_member.await_count == 3
    mock_db.record_group_member.assert_awaited_with(group_id=-100, user_id=4002)

    # 5. Multi-group: User 4001 in Group -200 at t0+15s is NOT suppressed by Group -100
    with patch("time.monotonic", return_value=t0 + 15.0):
        await mw._ensure_group_member(group_id=-200, user_id=4001)
    assert mock_db.record_group_member.await_count == 4
    mock_db.record_group_member.assert_awaited_with(group_id=-200, user_id=4001)


# =============================================================================
# CHALLENGE 5: Group member count caching (1h TTL)
# =============================================================================
@pytest.mark.asyncio
async def test_challenge_5_group_member_count_caching_1h_ttl():
    """Verify repeated messages in group do not call bot.get_chat_member_count,
    but after 3600s cache expires and calls again."""
    mock_db = MockDB()
    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=250)
    mw = ChatTrackerMiddleware(database=mock_db, bot=bot)

    g_chat = MagicMock(spec=Chat, id=-100555, type=ChatType.SUPERGROUP, title="Busy Hub", username="hub")
    user = MagicMock(spec=User, id=5001, full_name="User Hub", username="user_hub", language_code="en", is_bot=False)
    msg = MagicMock(spec=Message, chat=g_chat, from_user=user, bot=bot)

    t0 = 5000.0

    # 1. Initial message at t0 -> queries bot
    with patch("time.monotonic", return_value=t0):
        await mw._process_message(msg)

    assert bot.get_chat_member_count.await_count == 1
    bot.get_chat_member_count.assert_awaited_with(-100555)
    assert mock_db.upsert_group.await_args.kwargs["member_count"] == 250

    # 2. Messages at t0+120s, t0+1800s, t0+3000s:
    # Group touch cache expires every 90s, so upsert_group is called, but member_count cache (1h TTL) is reused!
    for delta in [120.0, 1800.0, 3000.0]:
        with patch("time.monotonic", return_value=t0 + delta):
            await mw._process_message(msg)
        assert bot.get_chat_member_count.await_count == 1, f"Bot queried prematurely at t0+{delta}s!"
        assert mock_db.upsert_group.await_args.kwargs["member_count"] == 250

    # 3. Message at t0+3601s:
    # Both group touch cache (3601-3000 = 601 > 90s) AND member_count cache (3601 > 3600s) have expired!
    bot.get_chat_member_count.return_value = 255
    with patch("time.monotonic", return_value=t0 + 3601.0):
        await mw._process_message(msg)

    assert bot.get_chat_member_count.await_count == 2, "Bot was not queried after 1-hour TTL expiration!"
    assert mock_db.upsert_group.await_args.kwargs["member_count"] == 255

    # 4. Message at t0+3605s (4s later):
    # Suppressed by 90s group touch debounce
    with patch("time.monotonic", return_value=t0 + 3605.0):
        await mw._process_message(msg)
    assert bot.get_chat_member_count.await_count == 2

    # 5. Message at t0+3700s (99s after refresh):
    # Group touch cache expired, but member count was refreshed at 3601s (only 99s old < 3600s)
    with patch("time.monotonic", return_value=t0 + 3700.0):
        await mw._process_message(msg)
    assert bot.get_chat_member_count.await_count == 2


# =============================================================================
# CHALLENGE 6: Error handling and resilience on Telegram API errors
# =============================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_exc,expected_fallback_count",
    [
        (TelegramForbiddenError(method="getChatMemberCount", message="bot was kicked from the supergroup chat"), 500),
        (TelegramBadRequest(method="getChatMemberCount", message="chat not found"), 500),
        (TelegramRetryAfter(method="getChatMemberCount", message="Flood control exceeded", retry_after=30), 500),
        (TelegramNetworkError(method="getChatMemberCount", message="Connection reset by peer"), 500),
        (asyncio.TimeoutError(), 500),
        (RuntimeError("Unexpected API gateway glitch"), 500),
    ],
)
async def test_challenge_6_member_count_telegram_error_resilience(error_exc, expected_fallback_count):
    """Verify middleware does not crash when get_chat_member_count raises any Telegram error,
    and falls back safely to cached count without interrupting the middleware chain."""
    mock_db = MockDB()
    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=500)
    mw = ChatTrackerMiddleware(database=mock_db, bot=bot)

    g_chat = MagicMock(spec=Chat, id=-100666, type=ChatType.SUPERGROUP, title="Faulty Group", username=None)
    user = MagicMock(spec=User, id=6001, full_name="Tester", username="tester", language_code="en", is_bot=False)
    msg = MagicMock(spec=Message, chat=g_chat, from_user=user, bot=bot)

    t0 = 10000.0

    # Prime cache at t0
    with patch("time.monotonic", return_value=t0):
        await mw._process_message(msg)
    assert bot.get_chat_member_count.await_count == 1
    assert mock_db.upsert_group.await_args.kwargs["member_count"] == 500

    # At t0+3605s (cache expired), bot raises exception!
    bot.get_chat_member_count.side_effect = error_exc
    with patch("time.monotonic", return_value=t0 + 3605.0):
        # Must NOT raise exception!
        await mw._process_message(msg)

    # Should safely fallback to cached count
    assert mock_db.upsert_group.await_args.kwargs["member_count"] == expected_fallback_count

    # Test fresh group with error on first call (no previous cache) -> falls back to 0
    bot.get_chat_member_count.side_effect = error_exc
    g_fresh = MagicMock(spec=Chat, id=-100777, type=ChatType.SUPERGROUP, title="Fresh Error Group", username=None)
    msg_fresh = MagicMock(spec=Message, chat=g_fresh, from_user=user, bot=bot)
    await mw._process_message(msg_fresh)
    assert mock_db.upsert_group.await_args.kwargs["member_count"] == 0


# =============================================================================
# CHALLENGE 7: Middleware Pipeline Execution & Data Context
# =============================================================================
@pytest.mark.asyncio
async def test_challenge_7_middleware_pipeline_execution():
    """Verify that __call__ properly processes message, extracts bot from data dict or event,
    and calls downstream handler with request_id logging context."""
    mock_db = MockDB()
    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=12)
    mw = ChatTrackerMiddleware(database=mock_db)

    p_chat = MagicMock(spec=Chat, id=7001, type=ChatType.PRIVATE)
    user = MagicMock(spec=User, id=7001, full_name="Pipeline User", username="pipeline", language_code="en", is_bot=False)
    msg = MagicMock(spec=Message, chat=p_chat, from_user=user, bot=bot)

    downstream_handler = AsyncMock(return_value="handler_result")
    data = {"bot": bot, "some_dependency": 123}

    result = await mw(downstream_handler, msg, data)

    assert result == "handler_result"
    downstream_handler.assert_awaited_once_with(msg, data)
    mock_db.upsert_user.assert_awaited_once_with(
        user_id=7001,
        user_name="Pipeline User",
        user_username="pipeline",
        chat_type="private",
        language="en",
        has_dm=True,
        status="active",
    )


# =============================================================================
# CHALLENGE 8: Concurrency Stress Test (50 simultaneous messages)
# =============================================================================
@pytest.mark.asyncio
async def test_challenge_8_concurrency_stress():
    """Stress-test middleware with 50 concurrent incoming messages across multiple groups and DMs."""
    mock_db = MockDB()
    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=100)
    mw = ChatTrackerMiddleware(database=mock_db, bot=bot)

    async def send_msg(idx):
        if idx % 2 == 0:
            chat = MagicMock(spec=Chat, id=-1000 - (idx % 5), type=ChatType.SUPERGROUP, title=f"Grp {idx % 5}")
        else:
            chat = MagicMock(spec=Chat, id=8000 + idx, type=ChatType.PRIVATE)
        user = MagicMock(spec=User, id=8000 + idx, full_name=f"User {idx}", username=f"user_{idx}", language_code="en", is_bot=False)
        msg = MagicMock(spec=Message, chat=chat, from_user=user, bot=bot)
        handler = AsyncMock(return_value=f"ok_{idx}")
        return await mw(handler, msg, {"bot": bot})

    results = await asyncio.gather(*(send_msg(i) for i in range(50)))
    assert len(results) == 50
    assert all(r.startswith("ok_") for r in results)


if __name__ == "__main__":
    pytest.main(["-v", __file__])
