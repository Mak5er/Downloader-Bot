from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app_context
from services.runtime import request_dedupe


class _AttrBag:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def __getattr__(self, name: str):
        value = AsyncMock(name=name)
        setattr(self, name, value)
        return value


@pytest.fixture(autouse=True)
def isolate_runtime_request_state():
    async def _send_analytics(*args, **kwargs):
        return None

    app_context.set_app_context(
        bot=_AttrBag(id=1, username="test_bot"),
        db=_AttrBag(
            name="test-db",
            get_file_id=AsyncMock(return_value=None),
            add_file=AsyncMock(),
            user_settings=AsyncMock(return_value={}),
            get_user_setting=AsyncMock(return_value=None),
            set_user_setting=AsyncMock(),
            upsert_chat=AsyncMock(),
            set_inactive=AsyncMock(),
            status=AsyncMock(return_value="active"),
        ),
        send_analytics=_send_analytics,
    )
    request_dedupe.reset_request_tracking()
    yield
    request_dedupe.reset_request_tracking()
    app_context._context = None
