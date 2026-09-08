from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from aiogram.exceptions import TelegramForbiddenError

from handlers import admin
import keyboards.inline_keyboards as inline_kb
import messages.admin_messages as admin_bm


@pytest.mark.asyncio
async def test_admin_panel_displays_segmented_dm_and_group_stats():
    panel = admin_bm.admin_panel(
        dm_total=1050,
        dm_active=920,
        dm_inactive=120,
        dm_banned=10,
        groups_total=15,
        groups_active=12,
        groups_inactive=3,
        total_reach=25400,
        tracked_group_members=850,
    )
    assert "Private Chats (DM):" in panel
    assert "1,050" in panel or "1050" in panel
    assert "920" in panel
    assert "120" in panel
    assert "10" in panel
    assert "Groups:" in panel
    assert "15" in panel
    assert "12" in panel
    assert "3" in panel
    assert "25,400" in panel
    assert "850" in panel


@pytest.mark.asyncio
async def test_check_active_users_ignores_group_only_users_and_groups(monkeypatch):
    run_bounded = AsyncMock(return_value=[True])
    mock_users_to_check = [
        SimpleNamespace(user_id=101, status="active", has_dm=True),
    ]
    fake_db = SimpleNamespace(
        get_users_for_reachability_check=AsyncMock(return_value=mock_users_to_check)
    )
    fake_bot = SimpleNamespace(send_chat_action=AsyncMock())
    status_message = SimpleNamespace(edit_text=AsyncMock())
    call = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            edit_text=AsyncMock(return_value=status_message),
        ),
    )

    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "db", fake_db)
    monkeypatch.setattr(admin, "bot", fake_bot)
    monkeypatch.setattr(admin, "_run_bounded", run_bounded)
    monkeypatch.setattr(admin.bm, "active_users_check_started", lambda total: f"started:{total}")
    monkeypatch.setattr(admin.bm, "active_users_check_completed", lambda total, ok, bad: f"done:{total}:{ok}:{bad}")
    monkeypatch.setattr(admin.kb, "return_back_to_admin_keyboard", lambda: "back")

    await admin.check_active_users(call)

    fake_db.get_users_for_reachability_check.assert_awaited_once()
    run_bounded.assert_awaited_once()
    checked_users = run_bounded.await_args[0][0]
    assert len(checked_users) == 1
    assert checked_users[0].user_id == 101


@pytest.mark.asyncio
async def test_check_active_groups_updates_member_counts_and_kicked_status(monkeypatch):
    fake_groups = [
        SimpleNamespace(id=-1001, title="Active Group"),
        SimpleNamespace(id=-1002, title="Kicked Group"),
    ]
    fake_db = SimpleNamespace(
        get_active_groups=AsyncMock(return_value=fake_groups),
        update_group_member_count=AsyncMock(),
        update_group_status=AsyncMock(),
    )

    async def fake_get_chat_member_count(chat_id):
        if chat_id == -1001:
            return 350
        raise TelegramForbiddenError(method="getChatMemberCount", message="bot was kicked from supergroup")

    fake_bot = SimpleNamespace(
        send_chat_action=AsyncMock(),
        get_chat_member_count=AsyncMock(side_effect=fake_get_chat_member_count),
    )
    status_message = SimpleNamespace(edit_text=AsyncMock())
    call = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            edit_text=AsyncMock(return_value=status_message),
        ),
    )

    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "db", fake_db)
    monkeypatch.setattr(admin, "bot", fake_bot)
    monkeypatch.setattr(admin, "_ADMIN_THROTTLE_SECONDS", 0.0)

    await admin.check_active_groups(call)

    fake_db.get_active_groups.assert_awaited_once()
    fake_db.update_group_member_count.assert_awaited_once_with(-1001, 350)
    fake_db.update_group_status.assert_any_await(-1001, "active")
    fake_db.update_group_status.assert_any_await(-1002, "kicked")
    status_message.edit_text.assert_awaited_once()
    completed_text = status_message.edit_text.await_args[0][0]
    assert "Group check finished" in completed_text
    assert "Reachable (bot member): <b>1</b>" in completed_text
    assert "Unreachable (kicked/left): <b>1</b>" in completed_text
    assert "350" in completed_text


@pytest.mark.asyncio
async def test_admin_keyboard_includes_check_groups_button():
    kb = inline_kb.admin_keyboard()
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "check_active_users" in callbacks
    assert "check_active_groups" in callbacks
