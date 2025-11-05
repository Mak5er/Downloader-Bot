from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import user


class DummyMessage:
    def __init__(self, user_id=1, chat_type="private"):
        self.from_user = SimpleNamespace(
            id=user_id,
            full_name="Tester",
            username="tester",
        )
        self.chat = SimpleNamespace(id=100, type=chat_type)
        self.text = "payload"
        self._replies = []
        self.reply = AsyncMock(side_effect=self._collect_reply)
        self.answer = AsyncMock(side_effect=self._collect_reply)

    def _collect_reply(self, *args, **kwargs):
        self._replies.append((args, kwargs))


@pytest.mark.asyncio
async def test_update_info_adds_new_user(monkeypatch):
    fake_db = SimpleNamespace(
        user_exist=AsyncMock(return_value=False),
        add_user=AsyncMock(),
        user_update_name=AsyncMock(),
        set_active=AsyncMock(),
    )

    monkeypatch.setattr(user, "db", fake_db)

    message = DummyMessage(user_id=42)
    await user.update_info(message)

    fake_db.user_exist.assert_awaited_once_with(42)
    fake_db.add_user.assert_awaited_once()
    fake_db.user_update_name.assert_not_awaited()
    fake_db.set_active.assert_awaited_once_with(42)


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
