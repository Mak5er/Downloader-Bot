import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineQuery, Message

from middlewares import antiflood
from middlewares import ban_middleware
from middlewares import private_chat_guard


@pytest.mark.asyncio
async def test_antiflood_blocks_messages_over_limit(monkeypatch):
    timestamps = iter([0.0, 0.2, 0.4])
    monkeypatch.setattr(antiflood.time, "time", lambda: next(timestamps))

    middleware = antiflood.AntifloodMiddleware(max_messages=2, per_seconds=1)
    handler = AsyncMock(return_value="handled")
    event = SimpleNamespace(from_user=SimpleNamespace(id=7))

    assert await middleware(handler, event, {}) == "handled"
    assert await middleware(handler, event, {}) == "handled"
    assert await middleware(handler, event, {}) is None
    assert handler.await_count == 2


@pytest.mark.asyncio
async def test_antiflood_expires_old_timestamps(monkeypatch):
    timestamps = iter([0.0, 2.0])
    monkeypatch.setattr(antiflood.time, "time", lambda: next(timestamps))

    middleware = antiflood.AntifloodMiddleware(max_messages=1, per_seconds=1)
    handler = AsyncMock(return_value="ok")
    event = SimpleNamespace(from_user=SimpleNamespace(id=99))

    assert await middleware(handler, event, {}) == "ok"
    assert await middleware(handler, event, {}) == "ok"
    assert handler.await_count == 2


@pytest.mark.asyncio
async def test_ban_middleware_caches_status(monkeypatch):
    status = AsyncMock(return_value="ban")
    monkeypatch.setattr(ban_middleware, "db", SimpleNamespace(status=status))
    monkeypatch.setattr(ban_middleware.time, "monotonic", lambda: 100.0)

    middleware = ban_middleware.UserBannedMiddleware(ttl_seconds=10.0)

    assert await middleware._get_status(1) == "ban"
    assert await middleware._get_status(1) == "ban"
    status.assert_awaited_once()


@pytest.mark.asyncio
async def test_ban_middleware_fails_closed_when_status_lookup_fails(monkeypatch):
    status = AsyncMock(side_effect=RuntimeError("db offline"))
    monkeypatch.setattr(ban_middleware, "db", SimpleNamespace(status=status))
    monkeypatch.setattr(ban_middleware.time, "monotonic", lambda: 200.0)

    middleware = ban_middleware.UserBannedMiddleware()

    assert await middleware._get_status(1) == "restricted"


@pytest.mark.asyncio
async def test_ban_middleware_blocks_banned_events(monkeypatch):
    middleware = ban_middleware.UserBannedMiddleware()
    monkeypatch.setattr(middleware, "_get_status", AsyncMock(return_value="ban"))

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(type="private"),
        answer=AsyncMock(),
    )
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
    )
    inline_query = SimpleNamespace(from_user=SimpleNamespace(id=1))

    with pytest.raises(asyncio.CancelledError):
        await middleware.on_pre_process_message(message, {})
    message.answer.assert_awaited_once()

    with pytest.raises(asyncio.CancelledError):
        await middleware.on_pre_process_callback_query(callback, {})
    callback.answer.assert_awaited_once()

    with pytest.raises(asyncio.CancelledError):
        await middleware.on_pre_process_inline_query(inline_query, {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "method_name"),
    [
        (Mock(spec=Message), "on_pre_process_message"),
        (Mock(spec=CallbackQuery), "on_pre_process_callback_query"),
        (Mock(spec=InlineQuery), "on_pre_process_inline_query"),
    ],
)
async def test_ban_middleware_dispatches_by_event_type(monkeypatch, event, method_name):
    middleware = ban_middleware.UserBannedMiddleware()
    handler = AsyncMock(return_value="ok")
    hook = AsyncMock()
    monkeypatch.setattr(middleware, method_name, hook)

    assert await middleware(handler, event, {}) == "ok"
    hook.assert_awaited_once_with(event, {})
    handler.assert_awaited_once_with(event, {})


@pytest.mark.asyncio
async def test_private_chat_guard_passes_through_non_message_events():
    middleware = private_chat_guard.PrivateChatGuardMiddleware()
    handler = AsyncMock(return_value="handled")

    assert await middleware(handler, object(), {}) == "handled"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_chat_guard_allows_private_messages_and_irrelevant_group_messages():
    middleware = private_chat_guard.PrivateChatGuardMiddleware()
    handler = AsyncMock(return_value="ok")

    private_event = Mock(spec=Message)
    private_event.chat = SimpleNamespace(type=ChatType.PRIVATE)
    private_event.from_user = SimpleNamespace(id=1, is_bot=False)
    private_event.text = "https://youtube.com/watch?v=demo"
    private_event.caption = None

    group_event = Mock(spec=Message)
    group_event.chat = SimpleNamespace(type=ChatType.SUPERGROUP)
    group_event.from_user = SimpleNamespace(id=1, is_bot=False)
    group_event.text = "hello there"
    group_event.caption = None

    assert await middleware(handler, private_event, {}) == "ok"
    assert await middleware(handler, group_event, {}) == "ok"
    assert handler.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "https://youtube.com/watch?v=demo",
        "https://soundcloud.com/artist/track",
        "https://pin.it/demo123",
    ],
)
async def test_private_chat_guard_sends_typing_and_calls_handler_when_dm_is_open(text):
    middleware = private_chat_guard.PrivateChatGuardMiddleware()
    handler = AsyncMock(return_value="handled")
    bot = SimpleNamespace(send_chat_action=AsyncMock())
    event = Mock(spec=Message)
    event.chat = SimpleNamespace(type=ChatType.SUPERGROUP)
    event.from_user = SimpleNamespace(id=8, is_bot=False)
    event.text = text
    event.caption = None

    result = await middleware(handler, event, {"bot": bot})

    assert result == "handled"
    bot.send_chat_action.assert_awaited_once_with(chat_id=8, action="typing")
    handler.assert_awaited_once_with(event, {"bot": bot})


@pytest.mark.asyncio
async def test_private_chat_guard_creates_pending_request_when_user_has_no_dm(monkeypatch):
    middleware = private_chat_guard.PrivateChatGuardMiddleware()
    handler = AsyncMock()
    sent_requests = []
    existing_pending = private_chat_guard.PendingRequest(
        text="https://youtu.be/old",
        notice_chat_id=-100,
        notice_message_id=555,
    )
    bot = SimpleNamespace(
        send_chat_action=AsyncMock(
            side_effect=TelegramBadRequest(
                method=SimpleNamespace(__api_method__="sendChatAction"),
                message="chat not found",
            )
        ),
        delete_message=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(username="maxloadbot")),
    )
    event = Mock(spec=Message)
    event.chat = SimpleNamespace(type=ChatType.SUPERGROUP)
    event.from_user = SimpleNamespace(id=42, is_bot=False)
    event.text = "https://tiktok.com/@demo/video/1"
    event.caption = None
    event.reply = AsyncMock(
        return_value=SimpleNamespace(
            chat=SimpleNamespace(id=-200),
            message_id=777,
        )
    )

    monkeypatch.setattr(private_chat_guard, "_bot_username", None)
    monkeypatch.setattr(private_chat_guard, "get_pending", lambda user_id: existing_pending)
    monkeypatch.setattr(private_chat_guard, "set_pending", lambda user_id, request: sent_requests.append((user_id, request)))
    monkeypatch.setattr(private_chat_guard.bm, "dm_start_required", lambda: "Open DM")
    monkeypatch.setattr(
        private_chat_guard.kb,
        "start_private_chat_keyboard",
        lambda username: f"keyboard:{username}",
    )

    result = await middleware(handler, event, {"bot": bot})

    assert result is None
    bot.delete_message.assert_awaited_once_with(-100, 555)
    bot.get_me.assert_awaited_once()
    event.reply.assert_awaited_once_with("Open DM", reply_markup="keyboard:maxloadbot")
    handler.assert_not_awaited()
    assert sent_requests[0][0] == 42
    assert sent_requests[0][1].text == "https://tiktok.com/@demo/video/1"
    assert sent_requests[0][1].notice_chat_id == -200
    assert sent_requests[0][1].notice_message_id == 777
