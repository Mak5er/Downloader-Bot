from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import utils


@pytest.mark.asyncio
async def test_maybe_delete_user_message_success():
    message = SimpleNamespace(delete=AsyncMock(return_value=True), answer=AsyncMock())

    result = await utils.maybe_delete_user_message(message, "on")

    assert result is True
    message.delete.assert_awaited_once()
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_delete_user_message_handles_error(monkeypatch):
    class DummyTelegramError(Exception):
        pass

    message = SimpleNamespace(delete=AsyncMock(side_effect=DummyTelegramError()), answer=AsyncMock())
    monkeypatch.setattr(utils, "TelegramAPIError", DummyTelegramError)

    result = await utils.maybe_delete_user_message(message, "on")

    assert result is False
    message.answer.assert_awaited_once_with(utils.DELETE_WARNING_TEXT)


@pytest.mark.asyncio
async def test_maybe_delete_user_message_skips_when_flag_off():
    message = SimpleNamespace(delete=AsyncMock(), answer=AsyncMock())

    result = await utils.maybe_delete_user_message(message, "off")

    assert result is False
    message.delete.assert_not_awaited()
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_bot_url(monkeypatch):
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="downloader_bot"))
    )

    url = await utils.get_bot_url(bot)

    assert url == "t.me/downloader_bot"
    bot.get_me.assert_awaited_once()


@pytest.mark.asyncio
async def test_remove_file_deletes_existing(tmp_path):
    target = tmp_path / "temp.txt"
    target.write_text("data")

    await utils.remove_file(str(target))

    assert not target.exists()


@pytest.mark.asyncio
async def test_remove_file_ignores_missing(tmp_path):
    missing = tmp_path / "missing.txt"

    await utils.remove_file(str(missing))

    assert not missing.exists()


@pytest.mark.asyncio
async def test_send_chat_action_if_needed_triggers(monkeypatch):
    bot = SimpleNamespace(send_chat_action=AsyncMock())

    await utils.send_chat_action_if_needed(bot, chat_id=1, action="typing", business_id=None)

    bot.send_chat_action.assert_awaited_once_with(1, "typing")


@pytest.mark.asyncio
async def test_send_chat_action_if_needed_skips_for_business():
    bot = SimpleNamespace(send_chat_action=AsyncMock())

    await utils.send_chat_action_if_needed(bot, chat_id=1, action="typing", business_id=123)

    bot.send_chat_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_react_to_message_skips_for_business(monkeypatch):
    message = SimpleNamespace(
        business_connection_id=42,
        react=AsyncMock(),
    )
    monkeypatch.setattr(utils.types, "ReactionTypeEmoji", lambda emoji: ("emoji", emoji))

    await utils.react_to_message(message, "🔥")

    message.react.assert_not_awaited()


@pytest.mark.asyncio
async def test_react_to_message_handles_errors(monkeypatch):
    message = SimpleNamespace(
        business_connection_id=None,
        react=AsyncMock(side_effect=RuntimeError("fail")),
    )
    monkeypatch.setattr(utils.types, "ReactionTypeEmoji", lambda emoji: ("emoji", emoji))

    await utils.react_to_message(message, "🔥", skip_if_business=False)

    message.react.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_with_reaction_invokes_reply(monkeypatch):
    mock_react = AsyncMock()
    monkeypatch.setattr(utils, "react_to_message", mock_react)
    message = SimpleNamespace(reply=AsyncMock())

    await utils._send_with_reaction(message, "hello", emoji="🔥", business_id=7, skip_if_business=False)

    mock_react.assert_awaited_once_with(
        message,
        "🔥",
        business_id=7,
        skip_if_business=False,
    )
    message.reply.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_send_with_reaction_requires_method():
    with pytest.raises(AttributeError):
        await utils._send_with_reaction(SimpleNamespace(), "hello")


@pytest.mark.asyncio
async def test_handle_download_error_uses_default_text(monkeypatch):
    mock_send = AsyncMock()
    monkeypatch.setattr(utils, "_send_with_reaction", mock_send)
    monkeypatch.setattr(utils.bm, "something_went_wrong", lambda: "oops")

    message = SimpleNamespace()

    await utils.handle_download_error(message)

    await_args = mock_send.await_args
    assert await_args.args[0] is message
    assert await_args.args[1] == "oops"
    assert await_args.kwargs["emoji"] == "👎"


@pytest.mark.asyncio
async def test_handle_video_too_large_uses_predefined_text(monkeypatch):
    mock_send = AsyncMock()
    monkeypatch.setattr(utils, "_send_with_reaction", mock_send)
    monkeypatch.setattr(utils.bm, "video_too_large", lambda: "too big")

    message = SimpleNamespace()

    await utils.handle_video_too_large(message, business_id=11, skip_if_business=False)

    await_args = mock_send.await_args
    assert await_args.args[0] is message
    assert await_args.args[1] == "too big"
    assert await_args.kwargs["emoji"] == "👎"
    assert await_args.kwargs["business_id"] == 11
    assert await_args.kwargs["skip_if_business"] is False
