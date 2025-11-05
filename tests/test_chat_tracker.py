from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType

from middlewares.chat_tracker import ChatTrackerMiddleware


class FakeDB:
    def __init__(self):
        self.user_exist = AsyncMock()
        self.user_update_name = AsyncMock()
        self.add_user = AsyncMock()
        self.set_active = AsyncMock()
        self.upsert_chat = AsyncMock()


def build_user(user_id, full_name="Test User", username="test_user", is_bot=False, language_code="en"):
    return SimpleNamespace(
        id=user_id,
        full_name=full_name,
        username=username,
        is_bot=is_bot,
        language_code=language_code,
    )


def build_chat(chat_id, chat_type, **extra):
    return SimpleNamespace(id=chat_id, type=chat_type, **extra)


@pytest.mark.asyncio
async def test_process_private_message_adds_new_user():
    fake_db = FakeDB()
    fake_db.user_exist.return_value = False

    middleware = ChatTrackerMiddleware(database=fake_db)
    message = SimpleNamespace(
        chat=build_chat(chat_id=42, chat_type=ChatType.PRIVATE),
        from_user=build_user(user_id=42),
    )

    await middleware._process_message(message)

    fake_db.user_exist.assert_awaited_once_with(42)
    fake_db.add_user.assert_awaited_once()
    args, kwargs = fake_db.add_user.await_args
    assert kwargs["chat_type"] == "private"
    assert kwargs["user_id"] == 42
    fake_db.user_update_name.assert_not_awaited()
    fake_db.set_active.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_process_group_message_tracks_chat_and_user():
    fake_db = FakeDB()
    fake_db.user_exist.return_value = False

    middleware = ChatTrackerMiddleware(database=fake_db)
    chat = build_chat(
        chat_id=-100,
        chat_type=ChatType.SUPERGROUP,
        title=None,
        full_name=None,
        first_name="Group",
        last_name="Chat",
        username="groupchat",
        language_code="uk",
    )
    user = build_user(user_id=777, full_name="Alice", username="alice")

    message = SimpleNamespace(chat=chat, from_user=user)

    await middleware._process_message(message)

    fake_db.upsert_chat.assert_awaited_once()
    _, chat_kwargs = fake_db.upsert_chat.await_args
    assert chat_kwargs["user_id"] == -100
    assert chat_kwargs["user_name"] == "Group Chat"
    assert chat_kwargs["chat_type"] == "public"

    assert fake_db.set_active.await_count == 2
    set_active_calls = [call.args[0] for call in fake_db.set_active.await_args_list]
    assert -100 in set_active_calls
    assert 777 in set_active_calls

    fake_db.add_user.assert_awaited_once()
    _, user_kwargs = fake_db.add_user.await_args
    assert user_kwargs["user_id"] == 777
    assert user_kwargs["chat_type"] == "private"


@pytest.mark.asyncio
async def test_process_private_message_updates_existing_user():
    fake_db = FakeDB()
    fake_db.user_exist.return_value = True

    middleware = ChatTrackerMiddleware(database=fake_db)
    message = SimpleNamespace(
        chat=build_chat(chat_id=10, chat_type=ChatType.PRIVATE),
        from_user=build_user(user_id=10, full_name="Updated Name", username="updated"),
    )

    await middleware._process_message(message)

    fake_db.user_exist.assert_awaited_once_with(10)
    fake_db.user_update_name.assert_awaited_once_with(10, "Updated Name", "updated")
    fake_db.add_user.assert_not_awaited()
    fake_db.set_active.assert_awaited_once_with(10)


@pytest.mark.asyncio
async def test_process_message_ignores_bot_user():
    fake_db = FakeDB()

    middleware = ChatTrackerMiddleware(database=fake_db)
    message = SimpleNamespace(
        chat=build_chat(chat_id=1, chat_type=ChatType.PRIVATE),
        from_user=build_user(user_id=1, is_bot=True),
    )

    await middleware._process_message(message)

    fake_db.user_exist.assert_not_awaited()
    fake_db.add_user.assert_not_awaited()
    fake_db.user_update_name.assert_not_awaited()
    fake_db.set_active.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_group_uses_generated_name():
    fake_db = FakeDB()

    middleware = ChatTrackerMiddleware(database=fake_db)
    chat = build_chat(
        chat_id=-500,
        chat_type=ChatType.SUPERGROUP,
        title=None,
        full_name=None,
        first_name=None,
        last_name=None,
        username=None,
        language_code=None,
    )

    await middleware._ensure_group(chat)

    fake_db.upsert_chat.assert_awaited_once()
    _, kwargs = fake_db.upsert_chat.await_args
    assert kwargs["user_id"] == -500
    assert kwargs["user_name"] == "Chat -500"
    assert kwargs["chat_type"] == "public"
    fake_db.set_active.assert_awaited_once_with(-500)
