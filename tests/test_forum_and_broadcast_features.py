from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

import handlers.admin as admin
import messages.admin_messages as admin_bm
from middlewares.chat_tracker import ChatTrackerMiddleware


@pytest.mark.asyncio
async def test_chat_tracker_records_forum_last_thread_id():
    mock_db = MagicMock()
    mock_db.upsert_group = AsyncMock()
    mock_db.upsert_user = AsyncMock()
    mock_db.record_group_member = AsyncMock()

    middleware = ChatTrackerMiddleware(database=mock_db)

    # Simulated message with message_thread_id = 420
    chat = SimpleNamespace(id=-100999, type="supergroup", title="Forum Group", username="forumgrp")
    user = SimpleNamespace(id=123, is_bot=False, full_name="User", username="user", language_code="en")
    msg = SimpleNamespace(chat=chat, from_user=user, message_thread_id=420)

    await middleware._process_message(msg)

    mock_db.upsert_group.assert_awaited_once()
    kwargs = mock_db.upsert_group.await_args.kwargs
    assert kwargs["chat_id"] == -100999
    assert kwargs["last_thread_id"] == 420


@pytest.mark.asyncio
async def test_deliver_mailing_message_uses_last_thread_id_and_falls_back(monkeypatch):
    monkeypatch.setattr(admin, "_ADMIN_THROTTLE_SECONDS", 0.0)

    # 1. Delivery with thread_id succeeds on first attempt
    bot_mock = MagicMock()
    bot_mock.copy_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    monkeypatch.setattr(admin, "bot", bot_mock)

    target_with_thread = SimpleNamespace(id=-100123, status="active", last_thread_id=55)
    outcome = await admin._deliver_mailing_message(target_with_thread, sender_id=1, message_id=10)

    assert outcome == "delivered"
    bot_mock.copy_message.assert_awaited_once_with(
        chat_id=-100123,
        from_chat_id=1,
        message_id=10,
        message_thread_id=55,
    )

    # 2. Topic deleted ("thread not found") -> falls back to sending without thread_id
    calls = []

    async def fake_copy_message(**kwargs):
        calls.append(kwargs.copy())
        if "message_thread_id" in kwargs:
            raise TelegramBadRequest(method=MagicMock(), message="Bad Request: thread not found")
        return SimpleNamespace(message_id=1000)

    bot_mock.copy_message = AsyncMock(side_effect=fake_copy_message)

    outcome = await admin._deliver_mailing_message(target_with_thread, sender_id=1, message_id=10)

    assert outcome == "delivered"
    assert len(calls) == 2
    assert calls[0]["message_thread_id"] == 55
    assert "message_thread_id" not in calls[1]


@pytest.mark.asyncio
async def test_deliver_mailing_message_retry_after_backoff(monkeypatch):
    monkeypatch.setattr(admin, "_ADMIN_THROTTLE_SECONDS", 0.0)

    penalized = []
    monkeypatch.setattr(admin.broadcast_limiter, "penalize", lambda sec: penalized.append(sec))

    bot_mock = MagicMock()
    attempt = 0

    async def fake_copy_message(**kwargs):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise TelegramRetryAfter(method=MagicMock(), message="Flood control", retry_after=0.01)
        return SimpleNamespace(message_id=1001)

    bot_mock.copy_message = AsyncMock(side_effect=fake_copy_message)
    monkeypatch.setattr(admin, "bot", bot_mock)

    target = SimpleNamespace(id=-100555, status="active")
    outcome = await admin._deliver_mailing_message(target, sender_id=1, message_id=10)

    assert outcome == "delivered"
    assert penalized == [0.01]
    assert attempt == 2


@pytest.mark.asyncio
async def test_broadcast_job_detailed_summary_and_finish_mailing(monkeypatch):
    targets = [
        SimpleNamespace(id=1, status="active"),
        SimpleNamespace(id=2, status="active"),
        SimpleNamespace(id=-1001, status="active"),
        SimpleNamespace(id=-1002, status="active"),
    ]

    outcomes = ["delivered", "unreachable", "migrated", "failed"]
    call_idx = 0

    async def fake_deliver(target, sender_id, message_id):
        nonlocal call_idx
        res = outcomes[call_idx % len(outcomes)]
        call_idx += 1
        return res

    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "_deliver_mailing_message", fake_deliver)

    fake_db = MagicMock()
    fake_db.get_users_for_reachability_check = AsyncMock(return_value=targets)
    fake_db.get_active_groups = AsyncMock(return_value=[])
    monkeypatch.setattr(admin, "db", fake_db)

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()
    monkeypatch.setattr(admin, "bot", fake_bot)

    admin_msg = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=999),
        text="Hello world announcement",
        message_id=44,
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"target_audience": "dm"}),
        clear=AsyncMock(),
    )

    await admin.send_to_all_message(
        message=admin_msg,
        state=state,
    )

    assert fake_bot.send_message.await_count == 2
    sent_text = fake_bot.send_message.await_args.kwargs["text"]
    assert "Mailing is complete!" in sent_text
    assert "Total targets: <b>4</b>" in sent_text
    assert "Delivered: <b>2</b>" in sent_text  # 1 delivered + 1 migrated
    assert "Unreachable / Blocked: <b>1</b>" in sent_text
    assert "Migrated to supergroup: <b>1</b>" in sent_text
    assert "Failed: <b>1</b>" in sent_text


def test_finish_mailing_formatting():
    # Without arguments (backward compatibility)
    default_msg = admin_bm.finish_mailing()
    assert default_msg == "Mailing is complete!"

    # With arguments
    report = admin_bm.finish_mailing(total=100, delivered=95, unreachable=3, migrated=2, failed=0)
    assert "Total targets: <b>100</b>" in report
    assert "Delivered: <b>95</b>" in report
    assert "Unreachable / Blocked: <b>3</b>" in report
    assert "Migrated to supergroup: <b>2</b>" in report
    assert "Failed: <b>0</b>" in report
