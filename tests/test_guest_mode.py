from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.types import (
    Chat,
    InlineQueryResultArticle,
    Message,
    Update,
    User,
)

import handlers
import middlewares
from handlers import guest
from handlers.deps import HandlerDependencies
from middlewares.antiflood import AntifloodMiddleware
from middlewares.ban_middleware import UserBannedMiddleware
from middlewares.chat_tracker import ChatTrackerMiddleware
from middlewares.private_chat_guard import PrivateChatGuardMiddleware


@pytest.fixture
def fake_deps():
    bot = Mock(spec=Bot)
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username="TestDownloaderBot"))
    bot.session = Mock()

    db = Mock()
    db.user_settings = AsyncMock(return_value={"captions": "off", "as_document": "off"})
    db.upsert_user = AsyncMock()
    db.upsert_chat = AsyncMock()
    db.status = AsyncMock(return_value="active")

    send_analytics = AsyncMock()

    return HandlerDependencies(bot=bot, db=db, send_analytics=send_analytics)


def _build_guest_message(
    text: str,
    *,
    user_id: int = 42,
    username: str = "testuser",
    chat_id: int = -100123456789,
    guest_query_id: str | None = "gq_test_123",
    reply_to_message: Message | None = None,
    quote: Any = None,
    entities: list | None = None,
    caption: str | None = None,
    caption_entities: list | None = None,
) -> Message:
    user = User(
        id=user_id,
        is_bot=False,
        first_name="Test",
        last_name="User",
        username=username,
    )
    chat = Chat(
        id=chat_id,
        type=ChatType.SUPERGROUP,
        title="Test Group",
    )
    msg = Mock(spec=Message)
    msg.message_id = 100
    msg.date = datetime.now(timezone.utc)
    msg.chat = chat
    msg.from_user = user
    msg.text = text
    msg.caption = caption
    msg.entities = entities
    msg.caption_entities = caption_entities
    msg.reply_to_message = reply_to_message
    msg.quote = quote
    msg.guest_query_id = guest_query_id
    msg.guest_bot_caller_user = user
    msg.guest_bot_caller_chat = chat
    msg.answer_guest_query = AsyncMock(
        return_value=SimpleNamespace(inline_message_id="inline_msg_abc123")
    )
    return msg


@pytest.mark.asyncio
async def test_guest_message_missing_query_id_ignored(fake_deps):
    msg = _build_guest_message(
        "@TestDownloaderBot https://vm.tiktok.com/ZM123456/", guest_query_id=None
    )
    await guest.handle_guest_message(msg, deps=fake_deps)
    msg.answer_guest_query.assert_not_awaited()
    fake_deps.send_analytics.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_message_tiktok_link(fake_deps):
    msg = _build_guest_message("@TestDownloaderBot https://vm.tiktok.com/ZM123456/")
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    call_args = msg.answer_guest_query.await_args
    result = call_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Tiktok" in result.title or "TikTok" in result.title
    assert "guest_tiktok_" in result.id

    # Check analytics
    fake_deps.send_analytics.assert_any_await(
        user_id=42,
        chat_type=ChatType.SUPERGROUP,
        action_name="guest_query",
    )
    fake_deps.send_analytics.assert_any_await(
        user_id=42,
        chat_type=ChatType.SUPERGROUP,
        action_name="guest_tiktok_download",
    )

    # Let event loop run background tasks
    await asyncio.sleep(0.01)
    mock_sender.assert_awaited_once()
    sender_kwargs = mock_sender.await_args.kwargs
    assert sender_kwargs["inline_message_id"] == "inline_msg_abc123"
    assert sender_kwargs["actor_user_id"] == 42
    assert sender_kwargs["duplicate_handler"] == "guest"


@pytest.mark.asyncio
async def test_guest_message_instagram_link(fake_deps):
    msg = _build_guest_message(
        "@TestDownloaderBot https://www.instagram.com/reel/C12345/"
    )
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Instagram" in result.title

    await asyncio.sleep(0.01)
    mock_sender.assert_awaited_once()
    assert mock_sender.await_args.kwargs["inline_message_id"] == "inline_msg_abc123"


@pytest.mark.asyncio
async def test_guest_message_youtube_link(fake_deps):
    msg = _build_guest_message("@TestDownloaderBot https://youtu.be/dQw4w9WgXcQ")
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Youtube" in result.title or "YouTube" in result.title


@pytest.mark.asyncio
async def test_guest_message_twitter_link(fake_deps):
    msg = _build_guest_message("@TestDownloaderBot https://x.com/user/status/123456")
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Twitter" in result.title


@pytest.mark.asyncio
async def test_guest_message_soundcloud_link(fake_deps):
    msg = _build_guest_message("@TestDownloaderBot https://soundcloud.com/artist/track")
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Soundcloud" in result.title or "SoundCloud" in result.title


@pytest.mark.asyncio
async def test_guest_message_threads_link(fake_deps):
    msg = _build_guest_message(
        "@TestDownloaderBot https://www.threads.net/@user/post/12345"
    )
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Threads" in result.title


@pytest.mark.asyncio
async def test_guest_message_pinterest_link(fake_deps):
    msg = _build_guest_message("@TestDownloaderBot https://pin.it/12345")
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Pinterest" in result.title


@pytest.mark.asyncio
async def test_guest_message_unsupported_sender_spotify(fake_deps):
    msg = _build_guest_message(
        "@TestDownloaderBot https://open.spotify.com/track/12345"
    )
    await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Spotify" in result.title


@pytest.mark.asyncio
async def test_guest_message_help_on_mention_without_link(fake_deps):
    msg = _build_guest_message("@TestDownloaderBot")
    await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Guest Mode" in result.title or "MaxLoad" in result.title


@pytest.mark.asyncio
async def test_guest_message_reply_to_tiktok_link(fake_deps):
    replied_msg = Mock(spec=Message)
    replied_msg.text = "https://vm.tiktok.com/ZM123456/"
    replied_msg.caption = None
    replied_msg.entities = None
    replied_msg.caption_entities = None
    replied_msg.quote = None

    msg = _build_guest_message("@TestDownloaderBot", reply_to_message=replied_msg)
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Tiktok" in result.title or "TikTok" in result.title
    assert "guest_tiktok_" in result.id

    await asyncio.sleep(0.01)
    mock_sender.assert_awaited_once()
    assert mock_sender.await_args.kwargs["inline_message_id"] == "inline_msg_abc123"


@pytest.mark.asyncio
async def test_guest_message_reply_caption_instagram_link(fake_deps):
    replied_msg = Mock(spec=Message)
    replied_msg.text = None
    replied_msg.caption = "Look at this reel: https://www.instagram.com/reel/C12345/"
    replied_msg.entities = None
    replied_msg.caption_entities = None
    replied_msg.quote = None

    msg = _build_guest_message(
        "@TestDownloaderBot download please", reply_to_message=replied_msg
    )
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Instagram" in result.title

    await asyncio.sleep(0.01)
    mock_sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_guest_message_quote_link(fake_deps):
    quote = SimpleNamespace(text="Check this out https://youtu.be/dQw4w9WgXcQ")
    msg = _build_guest_message("@TestDownloaderBot", quote=quote)
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Youtube" in result.title or "YouTube" in result.title

    await asyncio.sleep(0.01)
    mock_sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_guest_message_reply_text_link_entity(fake_deps):
    text_link_entity = SimpleNamespace(
        type="text_link", url="https://x.com/user/status/123456"
    )
    replied_msg = Mock(spec=Message)
    replied_msg.text = "Click here to see the tweet"
    replied_msg.caption = None
    replied_msg.entities = [text_link_entity]
    replied_msg.caption_entities = None
    replied_msg.quote = None

    msg = _build_guest_message("@TestDownloaderBot", reply_to_message=replied_msg)
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Twitter" in result.title

    await asyncio.sleep(0.01)
    mock_sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_guest_message_direct_link_overrides_reply_link(fake_deps):
    replied_msg = Mock(spec=Message)
    replied_msg.text = "https://www.instagram.com/reel/C12345/"
    replied_msg.caption = None
    replied_msg.entities = None
    replied_msg.caption_entities = None
    replied_msg.quote = None

    # User explicitly typed TikTok link while replying to an Instagram message
    msg = _build_guest_message(
        "@TestDownloaderBot https://vm.tiktok.com/ZM123456/",
        reply_to_message=replied_msg,
    )
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    # Direct TikTok link should have taken precedence
    assert "Tiktok" in result.title or "TikTok" in result.title


@pytest.mark.asyncio
async def test_guest_sender_error_handled_gracefully(fake_deps):
    msg = _build_guest_message("@TestDownloaderBot https://vm.tiktok.com/ZM123456/")
    mock_sender = AsyncMock(side_effect=RuntimeError("Simulated download failure"))

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    await asyncio.sleep(0.01)
    mock_sender.assert_awaited_once()



@pytest.mark.asyncio
async def test_guest_message_help_command(fake_deps):
    msg = _build_guest_message("@TestDownloaderBot /help")
    await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)


@pytest.mark.asyncio
async def test_guest_message_settings_command(fake_deps):
    msg = _build_guest_message("@TestDownloaderBot /settings")
    await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Settings" in result.title


@pytest.mark.asyncio
async def test_guest_message_stats_command(fake_deps):
    msg = _build_guest_message("@TestDownloaderBot /stats")
    await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)
    assert "Statistics" in result.title


@pytest.mark.asyncio
async def test_guest_mode_antiflood_triggers_flood_notice():
    middleware = AntifloodMiddleware(
        max_guest_queries=2, guest_window_seconds=1.0, cooldown_seconds=2.0
    )
    handler = AsyncMock(return_value="handled")

    event = _build_guest_message("@TestDownloaderBot https://vm.tiktok.com/ZM123456/")

    assert await middleware(handler, event, {}) == "handled"
    assert await middleware(handler, event, {}) == "handled"
    # Third request exceeds limit
    result = await middleware(handler, event, {})
    assert result is None
    assert handler.await_count == 2
    event.answer_guest_query.assert_awaited_once()
    call_args = event.answer_guest_query.await_args
    blocked_result = call_args.args[0] if call_args.args else call_args.kwargs["result"]
    assert blocked_result.id == "flood_limit"


@pytest.mark.asyncio
async def test_guest_mode_banned_user_blocked():
    middleware = UserBannedMiddleware()
    middleware._get_status = AsyncMock(return_value="ban")
    handler = AsyncMock()

    event = _build_guest_message("@TestDownloaderBot https://vm.tiktok.com/ZM123456/")
    data = {}

    result = await middleware(handler, event, data)
    assert result is None
    assert data.get("_skip_handler") is True
    handler.assert_not_awaited()
    event.answer_guest_query.assert_awaited_once()
    call_args = event.answer_guest_query.await_args
    ban_result = call_args.args[0] if call_args.args else call_args.kwargs["result"]
    assert ban_result.id == "banned_guest"


@pytest.mark.asyncio
async def test_guest_mode_private_chat_guard_bypassed():
    middleware = PrivateChatGuardMiddleware()
    handler = AsyncMock(return_value="passed")

    event = _build_guest_message("@TestDownloaderBot https://vm.tiktok.com/ZM123456/")
    data = {}

    result = await middleware(handler, event, data)
    assert result == "passed"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_guest_mode_chat_tracker_ensures_user_without_group_ping():
    fake_db = Mock()
    fake_db.upsert_user = AsyncMock()
    fake_db.upsert_group = AsyncMock()
    fake_db.ensure_group_member = AsyncMock()
    middleware = ChatTrackerMiddleware(database=fake_db)

    event = _build_guest_message("@TestDownloaderBot https://vm.tiktok.com/ZM123456/")

    handler = AsyncMock(return_value="done")
    result = await middleware(handler, event, {})
    assert result == "done"

    # Verified that user is upserted with has_dm=False
    fake_db.upsert_user.assert_awaited_once()
    _, kwargs = fake_db.upsert_user.await_args
    assert kwargs["user_id"] == 42
    assert kwargs["has_dm"] is False

    # Group table and members must NEVER be touched in guest mode
    fake_db.upsert_group.assert_not_awaited()
    fake_db.ensure_group_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_mode_dispatcher_integration():
    dp = Dispatcher()
    dp.include_router(handlers.router)

    for middleware_cls in middlewares.__all__:
        m = middleware_cls()
        dp.message.outer_middleware(m)
        dp.callback_query.outer_middleware(m)
        dp.inline_query.outer_middleware(m)
        dp.guest_message.outer_middleware(m)

    used_updates = dp.resolve_used_update_types()
    assert "guest_message" in used_updates

    from aiogram.types import SentGuestMessage

    bot = Bot(token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    bot.session = AsyncMock()
    bot.session.return_value = SentGuestMessage(inline_message_id="imi_integ")
    bot.get_me = AsyncMock(
        return_value=User(
            id=9999, is_bot=True, first_name="TestBot", username="TestBot"
        )
    )

    msg = Message(
        message_id=50,
        date=datetime.now(timezone.utc),
        chat=Chat(id=-100999, type=ChatType.SUPERGROUP, title="Integration Group"),
        from_user=User(id=777, is_bot=False, first_name="Integ", username="integ_user"),
        text="@TestBot /help",
        guest_query_id="gq_integ_50",
        guest_bot_caller_user=User(
            id=777, is_bot=False, first_name="Integ", username="integ_user"
        ),
        guest_bot_caller_chat=Chat(
            id=-100999, type=ChatType.SUPERGROUP, title="Integration Group"
        ),
    )

    fake_db = Mock()
    fake_db.status = AsyncMock(return_value="active")
    fake_db.upsert_user = AsyncMock()
    fake_db.user_settings = AsyncMock(return_value={})

    update = Update(update_id=999, guest_message=msg)

    with (
        patch("app_context.bot", bot),
        patch("app_context.db", fake_db),
        patch("app_context.send_analytics", AsyncMock()),
    ):
        await dp.feed_update(bot, update)

    assert bot.session.call_count >= 1
    # Check that AnswerGuestQuery was called
    called_method = bot.session.call_args_list[0][0][1]
    assert hasattr(called_method, "result")
    assert isinstance(called_method.result, InlineQueryResultArticle)


@pytest.mark.asyncio
async def test_guest_message_spotify_deeplink_stores_request_and_processes(fake_deps):
    from handlers.media_download import _process_inline_album_deeplink
    from services.inline.album_links import get_inline_album_request

    msg = _build_guest_message(
        "@TestDownloaderBot https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
    )
    await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert isinstance(result, InlineQueryResultArticle)

    # Check button URL has start=dl_
    btn_url = result.reply_markup.inline_keyboard[0][0].url
    assert "?start=dl_" in btn_url
    token = btn_url.split("?start=dl_")[1]

    # Verify request is properly stored
    stored = get_inline_album_request(token)
    assert stored is not None
    assert stored.service == "spotify"
    assert "spotify.com" in stored.url

    # Verify _process_inline_album_deeplink recognizes and processes it
    dm_msg = Mock(spec=Message)
    dm_msg.from_user = SimpleNamespace(id=42)
    dm_msg.reply = AsyncMock()

    with patch("handlers.media_download._process_supported_link", new_callable=AsyncMock) as mock_proc:
        processed = await _process_inline_album_deeplink(dm_msg, f"dl_{token}")
        assert processed is True
        mock_proc.assert_awaited_once_with(dm_msg, "spotify", stored.url)


@pytest.mark.asyncio
async def test_guest_mode_prioritizes_caller_user_over_bot_from_user(fake_deps):
    bot_user = User(id=999, is_bot=True, first_name="Bot", username="my_bot")
    human_caller = User(id=777, is_bot=False, first_name="Human", username="human_user")

    msg = _build_guest_message(
        "@TestDownloaderBot https://vm.tiktok.com/ZM123456/",
        user_id=777,
    )
    # Simulate Telegram update where from_user is bot, but caller is human
    msg.from_user = bot_user
    msg.guest_bot_caller_user = human_caller

    mock_sender = AsyncMock()
    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    fake_deps.send_analytics.assert_any_await(
        user_id=777,
        chat_type=ChatType.SUPERGROUP,
        action_name="guest_query",
    )
    await asyncio.sleep(0.01)
    assert mock_sender.await_args.kwargs["actor_user_id"] == 777


@pytest.mark.asyncio
async def test_send_welcome_handles_settings_and_stats_start_payloads():
    import handlers.commands as cmd_mod
    import handlers.user as user_mod

    dm_msg = Mock(spec=Message)
    dm_msg.chat = SimpleNamespace(type=ChatType.PRIVATE, id=100)
    dm_msg.from_user = SimpleNamespace(id=42, full_name="User", username="user")
    dm_msg.reply = AsyncMock()

    with (
        patch.object(user_mod, "send_analytics", new_callable=AsyncMock),
        patch.object(user_mod, "update_info", new_callable=AsyncMock),
        patch.object(user_mod, "settings_menu", new_callable=AsyncMock) as mock_settings,
        patch.object(user_mod, "stats_command", new_callable=AsyncMock) as mock_stats,
    ):
        # /start settings
        dm_msg.text = "/start settings"
        await cmd_mod.send_welcome(dm_msg)
        mock_settings.assert_awaited_once_with(dm_msg)
        dm_msg.reply.assert_not_awaited()

        # /start stats
        dm_msg.text = "/start stats"
        await cmd_mod.send_welcome(dm_msg)
        mock_stats.assert_awaited_once_with(dm_msg)


@pytest.mark.asyncio
async def test_guest_message_reply_to_bot_video_without_link_ignored(fake_deps):
    bot_msg = Mock(spec=Message)
    bot_msg.from_user = User(
        id=999999, is_bot=True, first_name="Bot", username="TestDownloaderBot"
    )
    bot_msg.text = None
    bot_msg.caption = "Downloaded via t.me/TestDownloaderBot"

    msg = _build_guest_message("nice video bro!", reply_to_message=bot_msg)
    await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_message_reply_to_bot_video_with_bot_mention_no_link_ignored(fake_deps):
    bot_msg = Mock(spec=Message)
    bot_msg.from_user = User(
        id=999999, is_bot=True, first_name="Bot", username="TestDownloaderBot"
    )
    bot_msg.text = None
    bot_msg.caption = "Downloaded via t.me/TestDownloaderBot"

    msg = _build_guest_message("@TestDownloaderBot thanks a lot!", reply_to_message=bot_msg)
    await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_message_reply_to_bot_video_with_social_link_downloads(fake_deps):
    bot_msg = Mock(spec=Message)
    bot_msg.from_user = User(
        id=999999, is_bot=True, first_name="Bot", username="TestDownloaderBot"
    )
    bot_msg.text = None
    bot_msg.caption = "Previous download"

    msg = _build_guest_message(
        "download this one too https://vm.tiktok.com/ZM123456/",
        reply_to_message=bot_msg,
    )
    mock_sender = AsyncMock()

    with patch("handlers.guest._get_service_sender", return_value=mock_sender):
        await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_awaited_once()
    result = msg.answer_guest_query.await_args.kwargs["result"]
    assert "guest_tiktok_" in result.id


@pytest.mark.asyncio
async def test_guest_message_reply_to_bot_video_does_not_extract_from_bot_caption(fake_deps):
    bot_msg = Mock(spec=Message)
    bot_msg.from_user = User(
        id=999999, is_bot=True, first_name="Bot", username="TestDownloaderBot"
    )
    bot_msg.text = None
    bot_msg.caption = "Source: https://vm.tiktok.com/ZM123456/"

    msg = _build_guest_message("haha lol", reply_to_message=bot_msg)
    await guest.handle_guest_message(msg, deps=fake_deps)

    msg.answer_guest_query.assert_not_awaited()

