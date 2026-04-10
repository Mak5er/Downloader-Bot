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
    assert "Cleanup skipped" in sent_messages[0][1]


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


@pytest.mark.asyncio
async def test_perf_metrics_reports_queue_load_and_bottleneck(monkeypatch):
    fake_queue = SimpleNamespace(
        load_snapshot=lambda: SimpleNamespace(queued_jobs=3, active_jobs=2, active_workers=4),
        metrics_snapshot=AsyncMock(
            return_value={
                "tiktok": SimpleNamespace(
                    count=5,
                    queue_wait_p50_ms=100.0,
                    queue_wait_p95_ms=900.0,
                    processing_p50_ms=120.0,
                    processing_p95_ms=300.0,
                )
            }
        ),
    )
    message = SimpleNamespace(answer=AsyncMock())

    monkeypatch.setattr(admin, "get_download_queue", lambda: fake_queue)

    await admin.perf_metrics(message)

    text = message.answer.await_args.args[0]
    assert "Queued jobs" in text
    assert "tracked sources" in text.lower()
    assert "queue-bound" in text


@pytest.mark.asyncio
async def test_admin_downloads_callback_renders_cleanup_panel(monkeypatch):
    call = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    fake_queue = SimpleNamespace(load_snapshot=lambda: SimpleNamespace(active_jobs=0, queued_jobs=0))
    fake_keyboard = object()

    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "OUTPUT_DIR", "downloads")
    monkeypatch.setattr(admin, "get_download_queue", lambda: fake_queue)
    monkeypatch.setattr(admin, "_build_directory_snapshot", lambda *_args, **_kwargs: admin._DirectorySnapshot(True, 4, 1, 2048, 60.0, 5.0))
    monkeypatch.setattr(admin.kb, "downloads_admin_keyboard", lambda can_cleanup=True: fake_keyboard)

    await admin.admin_downloads(call)

    call.answer.assert_awaited_once()
    kwargs = call.message.edit_text.await_args.kwargs
    assert "Downloads Storage" in kwargs["text"]
    assert kwargs["reply_markup"] is fake_keyboard


@pytest.mark.asyncio
async def test_admin_ops_callback_renders_combined_panel(monkeypatch):
    call = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    fake_keyboard = object()
    render_ops = AsyncMock(return_value="<b>Bot Health</b>\n\n<b>Queue Performance</b>")

    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "_render_ops_text", render_ops)
    monkeypatch.setattr(admin.kb, "admin_detail_keyboard", lambda refresh_callback: fake_keyboard)

    await admin.admin_ops(call)

    call.answer.assert_awaited_once()
    render_ops.assert_awaited_once()
    kwargs = call.message.edit_text.await_args.kwargs
    assert "Bot Health" in kwargs["text"]
    assert "Queue Performance" in kwargs["text"]
    assert kwargs["reply_markup"] is fake_keyboard


@pytest.mark.asyncio
async def test_admin_runtime_storage_callback_renders_combined_panel(monkeypatch):
    call = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    fake_queue = SimpleNamespace(load_snapshot=lambda: SimpleNamespace(active_jobs=0, queued_jobs=0))
    fake_keyboard = object()

    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "OUTPUT_DIR", "downloads")
    monkeypatch.setattr(admin, "get_download_queue", lambda: fake_queue)
    monkeypatch.setattr(
        admin,
        "get_runtime_snapshot",
        lambda: SimpleNamespace(
            uptime_seconds=120.0,
            total_downloads=5,
            total_videos=4,
            total_audio=1,
            total_other=0,
            total_bytes=2048,
            last_download_monotonic=None,
            by_source={},
        ),
    )
    monkeypatch.setattr(
        admin,
        "_build_directory_snapshot",
        lambda *_args, **_kwargs: admin._DirectorySnapshot(True, 4, 1, 2048, 60.0, 5.0),
    )
    monkeypatch.setattr(
        admin.kb,
        "downloads_admin_keyboard",
        lambda can_cleanup=True, refresh_callback="admin_downloads": fake_keyboard,
    )

    await admin.admin_runtime_storage(call)

    call.answer.assert_awaited_once()
    kwargs = call.message.edit_text.await_args.kwargs
    assert "Runtime Session Stats" in kwargs["text"]
    assert "Downloads Storage" in kwargs["text"]
    assert kwargs["reply_markup"] is fake_keyboard


@pytest.mark.asyncio
async def test_send_to_all_callback_shows_audience_preview(monkeypatch):
    state = SimpleNamespace(set_state=AsyncMock())
    call = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    fake_users = [
        SimpleNamespace(user_id=1, status="active", chat_type="private"),
        SimpleNamespace(user_id=2, status="inactive", chat_type="private"),
        SimpleNamespace(user_id=3, status="ban", chat_type="group"),
        SimpleNamespace(user_id=4, status="active", chat_type="group"),
    ]

    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(admin, "db", SimpleNamespace(get_all_users_info=AsyncMock(return_value=fake_users)))

    await admin.send_to_all_callback(call, state)

    text = call.message.edit_text.await_args.kwargs["text"]
    assert "audience preview" in text.lower()
    assert "3" in text
    assert "2" in text
    state.set_state.assert_awaited_once_with(admin.Mailing.send_to_all_message)


@pytest.mark.asyncio
async def test_admin_collect_chat_id_shows_known_chat_preview(monkeypatch):
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=10),
        text="12345",
        answer=AsyncMock(),
        reply=AsyncMock(),
    )

    monkeypatch.setattr(admin, "ADMINS_UID", [1])
    monkeypatch.setattr(
        admin,
        "db",
        SimpleNamespace(get_user_info=AsyncMock(return_value=("Known Chat", "@known", "active"))),
    )

    await admin.admin_collect_chat_id(message, state)

    text = message.answer.await_args.args[0]
    assert "Known chat target" in text
    assert "Known Chat" in text
    state.set_state.assert_awaited_once_with(admin.Admin.write_chat_text)


@pytest.mark.asyncio
async def test_admin_command_renders_button_panel(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=10, type="private"),
        answer=AsyncMock(),
    )
    fake_bot = SimpleNamespace(send_chat_action=AsyncMock())
    fake_keyboard = object()

    monkeypatch.setattr(admin, "bot", fake_bot)
    monkeypatch.setattr(
        admin,
        "_get_admin_counts",
        AsyncMock(
            return_value={
                "user_count": 10,
                "private_chat_count": 7,
                "group_chat_count": 3,
                "active_user_count": 8,
                "inactive_user_count": 2,
            }
        ),
    )
    monkeypatch.setattr(admin, "get_download_queue", lambda: SimpleNamespace(load_snapshot=lambda: SimpleNamespace(queued_jobs=1, active_jobs=2, active_workers=4)))
    monkeypatch.setattr(admin, "get_runtime_snapshot", lambda: SimpleNamespace(total_downloads=12, total_bytes=4096))
    monkeypatch.setattr(admin.kb, "admin_keyboard", lambda: fake_keyboard)

    await admin.admin(message)

    fake_bot.send_chat_action.assert_awaited_once_with(10, "typing")
    kwargs = message.answer.await_args.kwargs
    assert "Hello, this is the admin panel." in kwargs["text"]
    assert "Runtime now" in kwargs["text"]
    assert kwargs["reply_markup"] is fake_keyboard
