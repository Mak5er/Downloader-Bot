from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import handlers
from handlers import user


class DummyMessage:
    def __init__(self, user_id=1, chat_type="private"):
        self.from_user = SimpleNamespace(
            id=user_id,
            full_name="Tester",
            username="tester",
            language_code="uk",
        )
        self.chat = SimpleNamespace(id=100, type=chat_type)
        self.text = "payload"
        self.caption = None
        self._replies = []
        self.reply = AsyncMock(side_effect=self._collect_reply)
        self.answer = AsyncMock(side_effect=self._collect_reply)
        self.answer_photo = AsyncMock(side_effect=self._collect_reply)

    def _collect_reply(self, *args, **kwargs):
        self._replies.append((args, kwargs))


class DummyCallbackMessage:
    def __init__(self):
        self.chat = SimpleNamespace(id=100, type="private")
        self.answer_photo = AsyncMock()
        self.edit_media = AsyncMock()
        self.edit_text = AsyncMock()
        self.edit_reply_markup = AsyncMock()
        self.delete = AsyncMock()


class DummyCallback:
    def __init__(self, data="stats:Week:total"):
        self.data = data
        self.message = DummyCallbackMessage()
        self.from_user = SimpleNamespace(id=1)
        self.answer = AsyncMock()


@pytest.mark.asyncio
async def test_update_info_adds_new_user(monkeypatch):
    fake_db = SimpleNamespace(
        upsert_chat=AsyncMock(),
    )

    monkeypatch.setattr(user, "db", fake_db)

    message = DummyMessage(user_id=42)
    await user.update_info(message)

    fake_db.upsert_chat.assert_awaited_once()
    _, kwargs = fake_db.upsert_chat.await_args
    assert kwargs["user_id"] == 42
    assert kwargs["user_name"] == "Tester"
    assert kwargs["user_username"] == "tester"
    assert kwargs["chat_type"] == "private"
    assert kwargs["status"] == "active"


@pytest.mark.asyncio
async def test_send_welcome(monkeypatch):
    fake_send_analytics = AsyncMock()
    fake_update_info = AsyncMock()

    monkeypatch.setattr(user, "send_analytics", fake_send_analytics)
    monkeypatch.setattr(user, "update_info", fake_update_info)

    message = DummyMessage()
    await user.send_welcome(message)

    fake_send_analytics.assert_awaited_once()
    message.reply.assert_awaited_once()
    fake_update_info.assert_awaited_once_with(message)


@pytest.mark.asyncio
async def test_send_welcome_deeplink_skips_default_welcome(monkeypatch):
    fake_send_analytics = AsyncMock()
    fake_update_info = AsyncMock()
    fake_process_deeplink = AsyncMock(return_value=True)

    monkeypatch.setattr(user, "send_analytics", fake_send_analytics)
    monkeypatch.setattr(user, "update_info", fake_update_info)
    monkeypatch.setattr(user, "_extract_start_payload", lambda _text: "album_token")
    monkeypatch.setattr(user, "_process_inline_album_deeplink", fake_process_deeplink)

    message = DummyMessage()
    message.text = "/start album_token"
    await user.send_welcome(message)

    fake_send_analytics.assert_awaited_once()
    fake_update_info.assert_awaited_once_with(message)
    fake_process_deeplink.assert_awaited_once_with(message, "album_token")
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_welcome_replays_pending_request_in_private_context(monkeypatch):
    fake_send_analytics = AsyncMock()
    fake_update_info = AsyncMock()
    fake_process_pending = AsyncMock()
    fake_bot = SimpleNamespace(delete_message=AsyncMock())

    monkeypatch.setattr(user, "send_analytics", fake_send_analytics)
    monkeypatch.setattr(user, "update_info", fake_update_info)
    monkeypatch.setattr(user, "bot", fake_bot)
    monkeypatch.setattr(user, "_process_pending_message", fake_process_pending)
    monkeypatch.setattr(
        user,
        "pop_pending",
        lambda _user_id: SimpleNamespace(
            text="https://youtu.be/demo",
            notice_chat_id=-100,
            notice_message_id=555,
        ),
    )

    message = DummyMessage()
    message.text = "/start"
    await user.send_welcome(message)

    fake_bot.delete_message.assert_awaited_once_with(-100, 555)
    fake_process_pending.assert_awaited_once()
    replayed_message = fake_process_pending.await_args.args[0]
    assert replayed_message.chat.id == message.chat.id
    assert replayed_message.chat.type == message.chat.type
    assert replayed_message.text == "https://youtu.be/demo"


@pytest.mark.asyncio
async def test_settings_menu_private(monkeypatch):
    fake_send_analytics = AsyncMock()
    fake_db = SimpleNamespace(
        user_settings=AsyncMock(return_value={"captions": "off"})
    )
    monkeypatch.setattr(user, "send_analytics", fake_send_analytics)
    monkeypatch.setattr(user, "db", fake_db)
    monkeypatch.setattr(user.kb, "return_settings_keyboard", lambda: "keyboard")

    message = DummyMessage(chat_type="private")
    await user.settings_menu(message)

    fake_send_analytics.assert_awaited_once()
    message.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_settings_menu_group(monkeypatch):
    fake_send_analytics = AsyncMock()
    fake_db = SimpleNamespace(user_settings=AsyncMock())
    monkeypatch.setattr(user, "send_analytics", fake_send_analytics)
    monkeypatch.setattr(user, "db", fake_db)

    message = DummyMessage(chat_type="group")
    await user.settings_menu(message)

    message.reply.assert_awaited_once()
    fake_db.user_settings.assert_not_called()


@pytest.mark.asyncio
async def test_stats_command_sends_photo(monkeypatch):
    monkeypatch.setattr(user, "_render_stats", AsyncMock(return_value=(b"chart", "<b>Caption</b>")))
    monkeypatch.setattr(user.kb, "stats_keyboard", lambda period, mode: ("keyboard", period, mode))

    message = DummyMessage()
    await user.stats_command(message)

    message.answer_photo.assert_awaited_once()
    _, kwargs = message.answer_photo.await_args
    assert kwargs["caption"] == "<b>Caption</b>"
    assert kwargs["reply_markup"] == ("keyboard", "Week", "total")


@pytest.mark.asyncio
async def test_switch_stats_edits_existing_message(monkeypatch):
    monkeypatch.setattr(user, "_render_stats", AsyncMock(return_value=(b"chart", "<b>Caption</b>")))
    monkeypatch.setattr(user.kb, "stats_keyboard", lambda period, mode: ("keyboard", period, mode))

    call = DummyCallback("stats:Month:split")
    await user.switch_stats(call)

    call.message.edit_media.assert_awaited_once()
    _, kwargs = call.message.edit_media.await_args
    assert kwargs["reply_markup"] == ("keyboard", "Month", "split")
    media = kwargs["media"]
    assert media.caption == "<b>Caption</b>"
    call.answer.assert_awaited_once_with()
    call.message.answer_photo.assert_not_awaited()
    call.message.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_stats_falls_back_when_edit_media_fails(monkeypatch):
    class FakeTelegramError(Exception):
        pass

    monkeypatch.setattr(user, "TelegramBadRequest", FakeTelegramError)
    monkeypatch.setattr(user, "TelegramAPIError", FakeTelegramError)
    monkeypatch.setattr(user, "_render_stats", AsyncMock(return_value=(b"chart", "<b>Caption</b>")))
    monkeypatch.setattr(user.kb, "stats_keyboard", lambda period, mode: ("keyboard", period, mode))

    call = DummyCallback("stats:Year:total")
    call.message.edit_media.side_effect = FakeTelegramError("broken")

    await user.switch_stats(call)

    call.message.answer_photo.assert_awaited_once()
    call.message.delete.assert_awaited_once()
    call.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_switch_period_uses_total_mode(monkeypatch):
    monkeypatch.setattr(user, "_render_stats", AsyncMock(return_value=(b"chart", "<b>Caption</b>")))
    monkeypatch.setattr(user.kb, "stats_keyboard", lambda period, mode: ("keyboard", period, mode))

    call = DummyCallback("date_Month")
    await user.switch_period(call)

    _, kwargs = call.message.edit_media.await_args
    assert kwargs["reply_markup"] == ("keyboard", "Month", "total")


@pytest.mark.asyncio
async def test_open_setting_rejects_invalid_callback_payload(monkeypatch):
    call = DummyCallback("settings:captions:extra")

    await user.open_setting(call)

    call.answer.assert_awaited_once()
    _, kwargs = call.answer.await_args
    assert kwargs["show_alert"] is True
    call.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_setting_rejects_invalid_callback_payload(monkeypatch):
    call = DummyCallback("setting:captions")
    fake_db = SimpleNamespace(set_user_setting=AsyncMock(), get_user_setting=AsyncMock())
    monkeypatch.setattr(user, "db", fake_db)

    await user.change_setting(call)

    call.answer.assert_awaited_once()
    _, kwargs = call.answer.await_args
    assert kwargs["show_alert"] is True
    fake_db.set_user_setting.assert_not_awaited()
    call.message.edit_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_setting_handles_invalid_db_setting_value(monkeypatch):
    call = DummyCallback("setting:captions:on")
    fake_db = SimpleNamespace(
        set_user_setting=AsyncMock(side_effect=ValueError("bad value")),
        get_user_setting=AsyncMock(),
    )
    monkeypatch.setattr(user, "db", fake_db)

    await user.change_setting(call)

    call.answer.assert_awaited_once()
    _, kwargs = call.answer.await_args
    assert kwargs["show_alert"] is True
    call.message.edit_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_pending_message_dispatches_soundcloud(monkeypatch):
    message = DummyMessage()
    message.text = "https://soundcloud.com/artist/track"
    process_soundcloud_url = AsyncMock()

    monkeypatch.setattr(handlers.soundcloud, "process_soundcloud_url", process_soundcloud_url)

    await user._process_pending_message(message)

    process_soundcloud_url.assert_awaited_once_with(message)


@pytest.mark.asyncio
async def test_process_pending_message_dispatches_pinterest(monkeypatch):
    message = DummyMessage()
    message.text = "https://pin.it/demo123"
    process_pinterest_url = AsyncMock()

    monkeypatch.setattr(handlers.pinterest, "process_pinterest_url", process_pinterest_url)

    await user._process_pending_message(message)

    process_pinterest_url.assert_awaited_once_with(message)
