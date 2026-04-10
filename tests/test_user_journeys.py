from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from handlers import user
from middlewares.antiflood import AntifloodMiddleware
from middlewares.private_chat_guard import PrivateChatGuardMiddleware
from services.runtime import pending_requests


def _monotonic_sequence(*values: float):
    iterator = iter(values)
    last = values[-1]

    def _next():
        nonlocal iterator
        return next(iterator, last)

    return _next


class _JourneyMessage:
    def __init__(
        self,
        *,
        user_id: int,
        chat_id: int,
        chat_type: str | ChatType,
        text: str,
        message_id: int = 1,
        is_bot: bool = False,
    ) -> None:
        self.from_user = SimpleNamespace(
            id=user_id,
            is_bot=is_bot,
            full_name="Journey User",
            username="journey_user",
            language_code="uk",
        )
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.text = text
        self.caption = None
        self.message_id = message_id
        self.reply = AsyncMock()
        self.answer = AsyncMock()


def _guard_message(
    *,
    user_id: int,
    chat_id: int,
    chat_type: str | ChatType,
    text: str,
    message_id: int = 1,
    is_bot: bool = False,
):
    message = Mock(spec=Message)
    message.from_user = SimpleNamespace(
        id=user_id,
        is_bot=is_bot,
        full_name="Journey User",
        username="journey_user",
        language_code="uk",
    )
    message.chat = SimpleNamespace(id=chat_id, type=chat_type)
    message.text = text
    message.caption = None
    message.message_id = message_id
    message.reply = AsyncMock()
    message.answer = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_group_link_requires_dm_then_replays_after_private_start(monkeypatch):
    monkeypatch.setattr(pending_requests, "_pending", {})
    monkeypatch.setattr(pending_requests, "_loaded", True)
    monkeypatch.setattr(pending_requests, "_persist_pending", lambda: None)

    middleware = PrivateChatGuardMiddleware()
    handler = AsyncMock(return_value="handled")
    group_message = _guard_message(
        user_id=42,
        chat_id=-100500,
        chat_type=ChatType.SUPERGROUP,
        text="https://www.youtube.com/watch?v=demo123",
        message_id=55,
    )
    group_message.reply = AsyncMock(
        return_value=SimpleNamespace(
            chat=SimpleNamespace(id=-100500),
            message_id=77,
        )
    )

    bot_for_group = SimpleNamespace(
        send_chat_action=AsyncMock(
            side_effect=TelegramBadRequest(
                method=SimpleNamespace(__api_method__="sendChatAction"),
                message="chat not found",
            )
        ),
        get_me=AsyncMock(return_value=SimpleNamespace(username="maxloadbot")),
    )

    result = await middleware(handler, group_message, {"bot": bot_for_group})

    assert result is None
    handler.assert_not_awaited()
    group_message.reply.assert_awaited_once()
    pending = pending_requests.get_pending(42)
    assert pending is not None
    assert pending.service == "youtube"
    assert pending.url == "https://www.youtube.com/watch?v=demo123"
    assert pending.source_chat_id == -100500
    assert pending.source_message_id == 55

    bot_for_private = SimpleNamespace(delete_message=AsyncMock())
    process_pending = AsyncMock()
    send_analytics = AsyncMock()
    update_info = AsyncMock()
    monkeypatch.setattr(user, "bot", bot_for_private)
    monkeypatch.setattr(user, "_process_pending_message", process_pending)
    monkeypatch.setattr(user, "send_analytics", send_analytics)
    monkeypatch.setattr(user, "update_info", update_info)

    private_message = _JourneyMessage(
        user_id=42,
        chat_id=42,
        chat_type=ChatType.PRIVATE,
        text="/start",
    )

    await user.send_welcome(private_message)

    private_message.reply.assert_awaited_once()
    bot_for_private.delete_message.assert_awaited_once_with(-100500, 77)
    process_pending.assert_awaited_once()
    replayed_message = process_pending.await_args.args[0]
    assert replayed_message.chat.id == 42
    assert replayed_message.chat.type == ChatType.PRIVATE
    assert replayed_message.text == "https://www.youtube.com/watch?v=demo123"
    assert pending_requests.get_pending(42) is None


@pytest.mark.asyncio
async def test_realistic_chain_allows_same_user_in_multiple_group_chats(monkeypatch):
    from middlewares import antiflood

    monkeypatch.setattr(antiflood.time, "monotonic", _monotonic_sequence(0.0, 0.2, 0.4, 0.4))

    antiflood_middleware = AntifloodMiddleware(max_messages=1, message_window_seconds=1, cooldown_seconds=2)
    private_guard = PrivateChatGuardMiddleware()
    final_handler = AsyncMock(return_value="handled")
    shared_bot = SimpleNamespace(send_chat_action=AsyncMock())

    async def _run_chain(event):
        async def _after_antiflood(inner_event, inner_data):
            merged_data = dict(inner_data)
            merged_data["bot"] = shared_bot
            return await private_guard(final_handler, inner_event, merged_data)

        return await antiflood_middleware(_after_antiflood, event, {})

    first_group_message = _guard_message(
        user_id=88,
        chat_id=-2001,
        chat_type=ChatType.SUPERGROUP,
        text="https://www.youtube.com/watch?v=alpha",
    )
    second_group_message = _guard_message(
        user_id=88,
        chat_id=-2002,
        chat_type=ChatType.SUPERGROUP,
        text="https://www.youtube.com/watch?v=beta",
    )
    repeated_first_group_message = _guard_message(
        user_id=88,
        chat_id=-2001,
        chat_type=ChatType.SUPERGROUP,
        text="https://www.youtube.com/watch?v=gamma",
    )

    assert await _run_chain(first_group_message) == "handled"
    assert await _run_chain(second_group_message) == "handled"
    assert await _run_chain(repeated_first_group_message) is None

    assert final_handler.await_count == 2
    assert shared_bot.send_chat_action.await_count == 2
    repeated_first_group_message.answer.assert_not_awaited()
