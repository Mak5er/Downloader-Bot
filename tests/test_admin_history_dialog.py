from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram_dialog import StartMode

from handlers.admin_history_dialog import (
    AdminHistorySG,
    format_history_entry,
    get_history_data,
    get_message_user_data,
    get_services_data,
    on_back_to_admin,
    on_enter_custom_chat_id,
    on_export_csv,
    on_next_page,
    on_page_info,
    on_prev_page,
    on_refresh,
    on_reset_filters,
    on_service_selected,
    on_toggle_status,
    on_user_selected,
)


def test_format_history_entry_success():
    item = SimpleNamespace(
        service="tiktok",
        user_id=123456789,
        url="https://tiktok.com/@user/video/123",
        title="Cool TikTok <video> & fun",
        status="success",
        file_size_bytes=10 * 1024 * 1024,
        duration_seconds=3.5,
        chat_type="private",
        error_message=None,
        created_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc),
        user=SimpleNamespace(user_name="John Doe", user_username="johndoe"),
    )

    formatted = format_history_entry(item)
    assert "<blockquote expandable>" in formatted
    assert "</blockquote>" in formatted
    assert "🎵 <b>Tiktok</b>" in formatted
    assert "@johndoe" in formatted
    assert "<code>123456789</code>" in formatted
    assert "✅ Success" in formatted
    assert "10.0MB" in formatted
    assert "3.5s" in formatted
    assert "&lt;video&gt;" in formatted  # HTML escaped


def test_format_history_entry_error_and_anonymous_user():
    item = SimpleNamespace(
        service="youtube",
        user_id=987654321,
        url="https://youtube.com/watch?v=abc",
        title=None,
        status="error",
        file_size_bytes=None,
        duration_seconds=None,
        chat_type="group",
        error_message="Video unavailable in country <US>",
        created_at=None,
        user=None,
    )

    formatted = format_history_entry(item)
    assert "▶️ <b>Youtube</b>" in formatted
    assert "👤 <b>User</b>" in formatted
    assert "<code>987654321</code>" in formatted
    assert "❌ Error" in formatted
    assert "group" in formatted
    assert "⚠️ <code>Video unavailable in country &lt;US&gt;</code>" in formatted


def test_format_history_entry_cached():
    item = SimpleNamespace(
        service="instagram",
        user_id=111222,
        url="https://instagram.com/p/123",
        title="Insta Reel",
        status="cached",
        file_size_bytes=5 * 1024 * 1024,
        duration_seconds=1.2,
        chat_type="private",
        error_message=None,
        created_at=datetime(2026, 9, 14, 12, 0, 0),
        user=SimpleNamespace(user_name="Alice", user_username=None),
    )

    formatted = format_history_entry(item)
    assert "📸 <b>Instagram</b>" in formatted
    assert "👤 <b>Alice</b>" in formatted
    assert "⚡ Cached" in formatted


def test_format_history_entry_username_priority_over_fullname():
    item = SimpleNamespace(
        service="tiktok",
        user_id=6013011895,
        url="https://tiktok.com/@test/1",
        title="TikTok Video",
        status="success",
        file_size_bytes=None,
        duration_seconds=None,
        chat_type="private",
        error_message=None,
        created_at=None,
        user=SimpleNamespace(user_name="Mak5er Full", user_username="mak5er"),
    )

    formatted = format_history_entry(item)
    assert "👤 <b>@mak5er</b> (<code>6013011895</code>)" in formatted


def test_format_history_entry_list_user_handling():
    item = SimpleNamespace(
        service="youtube",
        user_id=6013011895,
        url="https://youtube.com/watch?v=xyz",
        title="YouTube Video",
        status="success",
        file_size_bytes=None,
        duration_seconds=None,
        chat_type="private",
        error_message=None,
        created_at=None,
        user=[SimpleNamespace(user_name="Mak5er", user_username="@mak5er")],
    )

    formatted = format_history_entry(item)
    assert "👤 <b>@mak5er</b> (<code>6013011895</code>)" in formatted



@pytest.mark.asyncio
async def test_get_history_data():
    mock_db = SimpleNamespace(
        get_download_history_count=AsyncMock(return_value=25),
        get_download_history=AsyncMock(
            return_value=[
                SimpleNamespace(
                    service="tiktok",
                    user_id=101,
                    url="https://tiktok.com/1",
                    title="Video 1",
                    status="success",
                    file_size_bytes=1024,
                    duration_seconds=1.0,
                    chat_type="private",
                    error_message=None,
                    created_at=datetime(2026, 9, 14, 10, 0, 0),
                    user=SimpleNamespace(user_name="User1", user_username="u1"),
                ),
                SimpleNamespace(
                    service="tiktok",
                    user_id=102,
                    url="https://tiktok.com/2",
                    title="Video 2",
                    status="success",
                    file_size_bytes=2048,
                    duration_seconds=2.0,
                    chat_type="private",
                    error_message=None,
                    created_at=datetime(2026, 9, 14, 10, 5, 0),
                    user=None,
                ),
            ]
        ),
    )

    manager = MagicMock()
    manager.dialog_data = {"page": 2, "service": "tiktok", "status": None}
    manager.middleware_data = {"db": mock_db}

    data = await get_history_data(manager)

    mock_db.get_download_history_count.assert_awaited_once_with(
        service="tiktok", status=None
    )
    mock_db.get_download_history.assert_awaited_once_with(
        page=2, per_page=10, service="tiktok", status=None
    )

    assert data["total_count"] == 25
    assert data["page"] == 2
    assert data["total_pages"] == 3
    assert data["service_filter"] == "Tiktok"
    assert len(data["page_users"]) == 2
    assert (101, "@u1") in data["page_users"]
    assert (102, "User 102") in data["page_users"]


@pytest.mark.asyncio
async def test_get_services_and_message_user_data():
    manager = MagicMock()
    manager.dialog_data = {"service": "instagram"}
    svc_data = await get_services_data(manager)
    assert svc_data["current_service"] == "Instagram"
    assert len(svc_data["services"]) > 0

    # test get_message_user_data
    mock_db = SimpleNamespace(
        get_download_history_count=AsyncMock(return_value=0),
        get_download_history=AsyncMock(return_value=[]),
    )
    manager.middleware_data = {"db": mock_db}
    msg_data = await get_message_user_data(manager)
    assert msg_data["page_users"] == []


@pytest.mark.asyncio
async def test_pagination_callbacks():
    callback = SimpleNamespace(answer=AsyncMock())
    manager = MagicMock()
    manager.dialog_data = {"page": 2, "total_pages": 3}
    button = MagicMock()

    # Prev page
    await on_prev_page(callback, button, manager)
    assert manager.dialog_data["page"] == 1

    # Prev page on page 1
    await on_prev_page(callback, button, manager)
    assert manager.dialog_data["page"] == 1
    callback.answer.assert_called_with("Already on first page")

    # Next page
    await on_next_page(callback, button, manager)
    assert manager.dialog_data["page"] == 2

    # Next page to max
    manager.dialog_data["page"] = 3
    await on_next_page(callback, button, manager)
    assert manager.dialog_data["page"] == 3
    callback.answer.assert_called_with("Already on last page")

    # Page info
    await on_page_info(callback, button, manager)
    callback.answer.assert_called_with("Page 3 of 3")

    # Refresh
    await on_refresh(callback, button, manager)
    callback.answer.assert_called_with("Refreshed")


@pytest.mark.asyncio
async def test_filter_callbacks():
    callback = SimpleNamespace(answer=AsyncMock())
    manager = MagicMock()
    manager.switch_to = AsyncMock()
    manager.dialog_data = {"page": 3, "service": "tiktok", "status": None}
    button = MagicMock()

    # Toggle status to error
    await on_toggle_status(callback, button, manager)
    assert manager.dialog_data["status"] == "error"
    assert manager.dialog_data["page"] == 1

    # Toggle status back to None
    await on_toggle_status(callback, button, manager)
    assert manager.dialog_data["status"] is None
    assert manager.dialog_data["page"] == 1

    # Select service
    await on_service_selected(callback, MagicMock(), manager, "instagram")
    assert manager.dialog_data["service"] == "instagram"
    manager.switch_to.assert_called_with(AdminHistorySG.history)

    # Select "all" service
    await on_service_selected(callback, MagicMock(), manager, "all")
    assert manager.dialog_data["service"] is None

    # Reset filters
    manager.dialog_data["service"] = "youtube"
    manager.dialog_data["status"] = "error"
    await on_reset_filters(callback, button, manager)
    assert manager.dialog_data["service"] is None
    assert manager.dialog_data["status"] is None
    assert manager.dialog_data["page"] == 1


@pytest.mark.asyncio
async def test_on_user_selected():
    callback = SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=777)))
    manager = MagicMock()
    manager.done = AsyncMock()

    mock_state = MagicMock()
    mock_state.update_data = AsyncMock()
    mock_state.set_state = AsyncMock()

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    mock_db = SimpleNamespace(
        get_user_info=AsyncMock(return_value=["Active", "Alice", "@alice"])
    )

    manager.middleware_data = {
        "state": mock_state,
        "bot": mock_bot,
        "db": mock_db,
    }

    await on_user_selected(callback, MagicMock(), manager, "12345")

    manager.done.assert_awaited_once()
    mock_state.update_data.assert_awaited_once_with(target_chat_id=12345)
    mock_db.get_user_info.assert_awaited_once_with(12345)
    mock_bot.send_message.assert_awaited_once()
    assert mock_state.set_state.call_count == 1


@pytest.mark.asyncio
async def test_on_enter_custom_chat_id():
    callback = SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=777)))
    manager = MagicMock()
    manager.done = AsyncMock()

    mock_state = MagicMock()
    mock_state.set_state = AsyncMock()

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    manager.middleware_data = {
        "state": mock_state,
        "bot": mock_bot,
    }

    await on_enter_custom_chat_id(callback, MagicMock(), manager)

    manager.done.assert_awaited_once()
    mock_bot.send_message.assert_awaited_once()
    assert mock_state.set_state.call_count == 1


@pytest.mark.asyncio
async def test_on_export_csv():
    callback = SimpleNamespace(
        answer=AsyncMock(),
        message=SimpleNamespace(chat=SimpleNamespace(id=777)),
    )
    mock_db = SimpleNamespace(
        get_download_history=AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=1,
                    service="tiktok",
                    user_id=123,
                    url="https://tiktok.com/1",
                    title="Dance video",
                    status="success",
                    file_size_bytes=5000000,
                    duration_seconds=2.5,
                    chat_id=123,
                    chat_type="private",
                    error_message=None,
                    created_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc),
                    user=SimpleNamespace(user_name="Alice", user_username="alice_tg"),
                )
            ]
        )
    )
    mock_bot = MagicMock()
    mock_bot.send_document = AsyncMock()

    manager = MagicMock()
    manager.dialog_data = {"service": None, "status": None}
    manager.middleware_data = {
        "db": mock_db,
        "bot": mock_bot,
    }

    button = MagicMock()
    await on_export_csv(callback, button, manager)

    mock_db.get_download_history.assert_awaited_once()
    mock_bot.send_document.assert_awaited_once()
    sent_call = mock_bot.send_document.call_args
    assert sent_call.kwargs["chat_id"] == 777
    assert "download_history_" in sent_call.kwargs["document"].filename


@pytest.mark.asyncio
async def test_on_back_to_admin():
    callback = SimpleNamespace(message=MagicMock())
    manager = MagicMock()
    manager.done = AsyncMock()

    with patch("handlers.admin._render_admin_panel", AsyncMock()) as mock_render:
        await on_back_to_admin(callback, MagicMock(), manager)
        manager.done.assert_awaited_once()
        mock_render.assert_awaited_once_with(callback.message, edit=True)


@pytest.mark.asyncio
async def test_admin_download_history_callback():
    from handlers.admin import admin_download_history

    call = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(chat=SimpleNamespace(id=123, type="private")),
        answer=AsyncMock(),
    )
    dialog_manager = MagicMock()
    dialog_manager.start = AsyncMock()

    with patch("handlers.admin._ensure_admin_callback", AsyncMock(return_value=True)):
        await admin_download_history(call, dialog_manager)
        call.answer.assert_awaited_once()
        dialog_manager.start.assert_awaited_once_with(
            AdminHistorySG.history, mode=StartMode.RESET_STACK
        )
