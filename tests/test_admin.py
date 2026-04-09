import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import admin


@pytest.mark.asyncio
async def test_clear_downloads_and_notify_removes_files(tmp_path, monkeypatch):
    folder = tmp_path / "downloads"
    folder.mkdir()
    (folder / "file1.txt").write_text("data")
    (folder / "file2.txt").write_text("data")

    sent_messages = []

    async def fake_send_message(chat_id, text):
        sent_messages.append((chat_id, text))

    monkeypatch.setattr(admin, "OUTPUT_DIR", str(folder))
    monkeypatch.setattr(admin, "ADMINS_UID", [1, 2])
    monkeypatch.setattr(admin, "bot", SimpleNamespace(send_message=fake_send_message))
    monkeypatch.setattr(admin, "_DOWNLOAD_CLEANUP_MIN_AGE_SECONDS", 0.0)
    monkeypatch.setattr(
        admin,
        "get_download_queue",
        lambda: SimpleNamespace(load_snapshot=lambda: SimpleNamespace(active_jobs=0, queued_jobs=0)),
    )

    await admin.clear_downloads_and_notify()

    assert not any(path.is_file() for path in folder.iterdir())
    assert len(sent_messages) == 2
    assert "Removed 2 files" in sent_messages[0][1]
    assert "0 directories" in sent_messages[0][1]


@pytest.mark.asyncio
async def test_clear_downloads_and_notify_removes_nested_files_and_empty_dirs(tmp_path, monkeypatch):
    folder = tmp_path / "downloads"
    nested = folder / "tweet123" / "variants"
    nested.mkdir(parents=True)
    old_file = nested / "video.mp4"
    old_file.write_text("data")

    sent_messages = []

    async def fake_send_message(chat_id, text):
        sent_messages.append((chat_id, text))

    monkeypatch.setattr(admin, "OUTPUT_DIR", str(folder))
    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "bot", SimpleNamespace(send_message=fake_send_message))
    monkeypatch.setattr(admin, "_DOWNLOAD_CLEANUP_MIN_AGE_SECONDS", 0.0)
    monkeypatch.setattr(
        admin,
        "get_download_queue",
        lambda: SimpleNamespace(load_snapshot=lambda: SimpleNamespace(active_jobs=0, queued_jobs=0)),
    )

    await admin.clear_downloads_and_notify()

    assert not old_file.exists()
    assert not nested.exists()
    assert "Removed 1 files and 2 directories" in sent_messages[0][1]


@pytest.mark.asyncio
async def test_clear_downloads_and_notify_skips_when_queue_busy(tmp_path, monkeypatch):
    folder = tmp_path / "downloads"
    folder.mkdir()
    file_path = folder / "file1.txt"
    file_path.write_text("data")

    sent_messages = []

    async def fake_send_message(chat_id, text):
        sent_messages.append((chat_id, text))

    monkeypatch.setattr(admin, "OUTPUT_DIR", str(folder))
    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "bot", SimpleNamespace(send_message=fake_send_message))
    monkeypatch.setattr(
        admin,
        "get_download_queue",
        lambda: SimpleNamespace(load_snapshot=lambda: SimpleNamespace(active_jobs=1, queued_jobs=2)),
    )

    await admin.clear_downloads_and_notify()

    assert file_path.exists()
    assert len(sent_messages) == 1
    assert "Skipped clearing" in sent_messages[0][1]


@pytest.mark.asyncio
async def test_delete_log_requires_admin(monkeypatch):
    call = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        answer=AsyncMock(),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            reply=AsyncMock(),
        ),
    )
    fake_bot = SimpleNamespace(send_chat_action=AsyncMock())

    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "bot", fake_bot)

    await admin.del_log(call)

    call.answer.assert_awaited_once_with("Admin access required.", show_alert=True)
    fake_bot.send_chat_action.assert_not_awaited()
    call.message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_log_truncates_log_files(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    for name in ("bot_log.log", "error_log.log", "events_log.jsonl", "perf_log.jsonl"):
        (log_dir / name).write_text("data", encoding="utf-8")

    call = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            reply=AsyncMock(),
        ),
    )
    fake_bot = SimpleNamespace(send_chat_action=AsyncMock())

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "bot", fake_bot)
    monkeypatch.setattr(admin.py_logging, "shutdown", lambda: None)

    await admin.del_log(call)

    fake_bot.send_chat_action.assert_awaited_once_with(1, "typing")
    for name in ("bot_log.log", "error_log.log", "events_log.jsonl", "perf_log.jsonl"):
        assert (log_dir / name).read_text(encoding="utf-8") == ""
    call.message.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_bounded_limits_parallelism():
    active = 0
    max_active = 0

    async def worker(item):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return item * 2

    results = await admin._run_bounded([1, 2, 3, 4, 5], limit=2, worker=worker)

    assert results == [2, 4, 6, 8, 10]
    assert max_active <= 2


@pytest.mark.asyncio
async def test_check_active_users_uses_bounded_runner(monkeypatch):
    run_bounded = AsyncMock(return_value=[True, False])
    fake_db = SimpleNamespace(
        get_all_users_info=AsyncMock(
            return_value=[
                SimpleNamespace(user_id=1, status="active"),
                SimpleNamespace(user_id=2, status="inactive"),
            ]
        )
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

    run_bounded.assert_awaited_once()
    _, kwargs = run_bounded.await_args
    assert kwargs["limit"] == admin._ADMIN_ACTIVE_CHECK_CONCURRENCY
    status_message.edit_text.assert_awaited_once_with("done:2:1:1", reply_markup="back", parse_mode="HTML")


@pytest.mark.asyncio
async def test_send_to_all_message_uses_bounded_runner(monkeypatch):
    run_bounded = AsyncMock(return_value=[None, None])
    fake_db = SimpleNamespace(
        get_all_users_info=AsyncMock(
            return_value=[
                SimpleNamespace(user_id=1, status="active"),
                SimpleNamespace(user_id=2, status="inactive"),
            ]
        )
    )
    fake_bot = SimpleNamespace(send_message=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=99),
        text="hello all",
        message_id=321,
        answer=AsyncMock(),
    )

    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "db", fake_db)
    monkeypatch.setattr(admin, "bot", fake_bot)
    monkeypatch.setattr(admin, "_run_bounded", run_bounded)
    monkeypatch.setattr(admin.bm, "cancel", lambda: "/cancel")
    monkeypatch.setattr(admin.bm, "start_mailing", lambda: "start")
    monkeypatch.setattr(admin.bm, "finish_mailing", lambda: "finish")

    await admin.send_to_all_message(message, state)

    run_bounded.assert_awaited_once()
    _, kwargs = run_bounded.await_args
    assert kwargs["limit"] == admin._ADMIN_MAILING_CONCURRENCY
    assert fake_bot.send_message.await_count == 2
