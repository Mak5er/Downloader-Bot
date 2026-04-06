from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app_context


def test_object_proxies_resolve_latest_context():
    dummy_bot = SimpleNamespace(id=1)
    dummy_db = SimpleNamespace(name="db")
    sender = AsyncMock()
    app_context.set_app_context(bot=dummy_bot, db=dummy_db, send_analytics=sender)

    assert app_context.bot.id == 1
    assert app_context.db.name == "db"


@pytest.mark.asyncio
async def test_send_analytics_proxy_delegates_to_context_callable():
    sender = AsyncMock(return_value=None)
    app_context.set_app_context(
        bot=SimpleNamespace(),
        db=SimpleNamespace(),
        send_analytics=sender,
    )

    await app_context.send_analytics(user_id=7, chat_type="private", action_name="test")

    sender.assert_awaited_once_with(user_id=7, chat_type="private", action_name="test")
