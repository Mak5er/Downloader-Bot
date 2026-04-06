import os
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
    log_dir = tmp_path / "log"
    log_dir.mkdir()
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
