import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType

import main


class DummySession:
    def __init__(self):
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, event):
        self.added.append(event)

    async def commit(self):
        self.committed = True


class DummyDB:
    def __init__(self):
        self.sessions = []

    def SessionLocal(self):
        session = DummySession()
        self.sessions.append(session)
        return session


class DummyAnalyticsClient:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json, timeout):
        self.calls.append(
            {
                "url": url,
                "json": json,
                "timeout": timeout,
            }
        )


@pytest.mark.asyncio
async def test_send_analytics_records_event(monkeypatch):
    dummy_db = DummyDB()
    dummy_client = DummyAnalyticsClient()

    monkeypatch.setattr(main, "db", dummy_db)
    monkeypatch.setattr(main, "BOT_TOKEN", "test-bot-token")
    monkeypatch.setattr(main, "_analytics_queue", None)
    monkeypatch.setattr(main, "_analytics_http_client", None)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *args, **kwargs: dummy_client)
    monkeypatch.setattr(main, "MEASUREMENT_ID", "G-TEST")
    monkeypatch.setattr(main, "API_SECRET", "secret")

    await main.send_analytics(user_id=123, chat_type=ChatType.PRIVATE, action_name="start")

    assert dummy_db.sessions, "No database session was created"
    session = dummy_db.sessions[0]
    assert session.committed is True
    assert len(session.added) == 1

    event = session.added[0]
    assert event.user_id == 123
    assert event.chat_type == ChatType.PRIVATE.value
    assert event.action_name == "start"

    assert len(dummy_client.calls) == 1
    call = dummy_client.calls[0]
    assert call["url"] == "https://www.google-analytics.com/mp/collect?measurement_id=G-TEST&api_secret=secret"
    assert call["timeout"] == 10
    client_id, user_identifier, session_id = main._build_analytics_identity(123)
    assert call["json"]["client_id"] == client_id
    assert call["json"]["user_id"] == user_identifier
    assert call["json"]["events"][0]["params"]["session_id"] == session_id
    assert call["json"]["client_id"] != "123"
    assert call["json"]["events"][0]["name"] == "start"
    assert call["json"]["events"][0]["params"]["chat_type"] == ChatType.PRIVATE.value


def test_build_analytics_identity_is_stable_and_pseudonymous(monkeypatch):
    monkeypatch.setattr(main, "BOT_TOKEN", "test-bot-token")
    monkeypatch.setattr(main, "MEASUREMENT_ID", "G-TEST")

    first = main._build_analytics_identity(123)
    second = main._build_analytics_identity(123)

    assert first == second
    assert all(first)
    assert "123" not in first[0]
    assert "123" not in first[1]


@pytest.mark.asyncio
async def test_flush_analytics_batch_uses_bounded_concurrency(monkeypatch):
    active = 0
    max_active = 0
    persisted = AsyncMock()

    async def fake_send(payload):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1

    monkeypatch.setattr(main, "_ANALYTICS_SEND_CONCURRENCY", 2)
    monkeypatch.setattr(main, "_send_to_google_analytics", fake_send)
    monkeypatch.setattr(main, "_persist_analytics_batch", persisted)

    batch = [
        main._AnalyticsPayload(user_id=1, chat_type="private", action_name="a"),
        main._AnalyticsPayload(user_id=2, chat_type="private", action_name="b"),
        main._AnalyticsPayload(user_id=3, chat_type="private", action_name="c"),
        main._AnalyticsPayload(user_id=4, chat_type="private", action_name="d"),
    ]

    await main._flush_analytics_batch(batch)

    assert max_active <= 2
    persisted.assert_awaited_once_with(batch)
