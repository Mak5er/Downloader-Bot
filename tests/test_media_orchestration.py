import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.media import orchestration
from utils.download_manager import (
    DownloadMetrics,
    DownloadQueueBusyError,
    DownloadRateLimitError,
)


class _FakeDb:
    def __init__(self) -> None:
        self.file_ids: dict[str, str] = {}

    async def get_file_id(self, url: str):
        return self.file_ids.get(url)

    async def add_file(self, url: str, file_id: str, _file_type: str):
        self.file_ids[url] = file_id


@pytest.mark.asyncio
async def test_run_single_media_flow_reuses_inflight_result_for_duplicate_requests():
    orchestration.reset_single_media_flow_tracking()
    db = _FakeDb()
    cache_key = "https://tiktok.com/@demo/video/123"
    download_started = asyncio.Event()
    allow_first_download_to_finish = asyncio.Event()
    call_counts = {
        "leader_download": 0,
        "follower_download": 0,
        "leader_send_downloaded": 0,
        "follower_send_downloaded": 0,
        "leader_send_cached": 0,
        "follower_send_cached": 0,
        "cleanup": 0,
    }

    async def _run_flow(label: str):
        async def _noop(*_args, **_kwargs):
            return None

        async def _download_media():
            call_counts[f"{label}_download"] += 1
            if label == "leader":
                download_started.set()
                await allow_first_download_to_finish.wait()
                return DownloadMetrics(
                    url=cache_key,
                    path="downloads/demo.mp4",
                    size=1024,
                    elapsed=1.0,
                    used_multipart=False,
                    resumed=False,
                )
            raise AssertionError("follower must not start a second download")

        async def _send_cached(file_id: str):
            call_counts[f"{label}_send_cached"] += 1
            return {"mode": "cached", "file_id": file_id, "label": label}

        async def _send_downloaded(path: str):
            call_counts[f"{label}_send_downloaded"] += 1
            return {
                "mode": "downloaded",
                "path": path,
                "label": label,
                "video": SimpleNamespace(file_id="telegram-file-id-1"),
            }

        async def _cleanup_path(_path: str):
            call_counts["cleanup"] += 1

        return await orchestration.run_single_media_flow(
            cache_key=cache_key,
            cache_file_type="video",
            db_service=db,
            upload_status_text="Uploading...",
            upload_action="upload_video",
            update_status=_noop,
            send_chat_action=_noop,
            send_cached=_send_cached,
            download_media=_download_media,
            send_downloaded=_send_downloaded,
            extract_file_id=lambda sent: getattr(sent.get("video"), "file_id", None),
            cleanup_path=_cleanup_path,
            delete_status_message=_noop,
            on_missing_media=_noop,
        )

    leader_task = asyncio.create_task(_run_flow("leader"))
    await download_started.wait()
    follower_task = asyncio.create_task(_run_flow("follower"))
    await asyncio.sleep(0)
    allow_first_download_to_finish.set()

    leader_result, follower_result = await asyncio.gather(leader_task, follower_task)

    assert leader_result["mode"] == "downloaded"
    assert follower_result == {
        "mode": "cached",
        "file_id": "telegram-file-id-1",
        "label": "follower",
    }
    assert db.file_ids[cache_key] == "telegram-file-id-1"
    assert call_counts == {
        "leader_download": 1,
        "follower_download": 0,
        "leader_send_downloaded": 1,
        "follower_send_downloaded": 0,
        "leader_send_cached": 0,
        "follower_send_cached": 1,
        "cleanup": 1,
    }


def _make_backpressure_handler(message, business_id, handle_download_error):
    return orchestration.make_message_backpressure_handler(
        message,
        business_id,
        build_rate_limit_text=lambda retry_after: f"rate-limit:{retry_after}",
        build_queue_busy_text=lambda position: f"queue-busy:{position}",
        handle_download_error=handle_download_error,
    )


@pytest.mark.asyncio
async def test_make_message_backpressure_handler_replies_with_rate_limit_text():
    message = SimpleNamespace(reply=AsyncMock())
    handle_download_error = AsyncMock()
    handler = _make_backpressure_handler(message, None, handle_download_error)

    await handler(DownloadRateLimitError(5.0))

    message.reply.assert_awaited_once_with("rate-limit:5.0")
    handle_download_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_make_message_backpressure_handler_replies_with_queue_busy_text():
    message = SimpleNamespace(reply=AsyncMock())
    handle_download_error = AsyncMock()
    handler = _make_backpressure_handler(message, None, handle_download_error)

    await handler(DownloadQueueBusyError(3))

    message.reply.assert_awaited_once_with("queue-busy:3")
    handle_download_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_make_message_backpressure_handler_uses_error_handler_for_business_chats():
    message = SimpleNamespace(reply=AsyncMock())
    handle_download_error = AsyncMock()
    handler = _make_backpressure_handler(message, "biz-1", handle_download_error)

    await handler(DownloadRateLimitError(5.0))

    handle_download_error.assert_awaited_once_with(message, business_id="biz-1")
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_make_message_backpressure_handler_reraises_unknown_errors():
    message = SimpleNamespace(reply=AsyncMock())
    handler = _make_backpressure_handler(message, None, AsyncMock())

    with pytest.raises(RuntimeError):
        await handler(RuntimeError("boom"))
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_media_group_flow_sends_resolved_entries_and_cleans_up(tmp_path):
    db = _FakeDb()
    db.file_ids["cache:0:photo"] = "cached-photo-id"
    downloaded_file = tmp_path / "item_1.mp4"
    downloaded_file.write_bytes(b"video")
    media_list = [
        SimpleNamespace(url="https://cdn.example.com/1.jpg", type="photo"),
        SimpleNamespace(url="https://cdn.example.com/2.mp4", type="video"),
    ]
    events: list[str] = []
    sent_entries: list[list[dict]] = []
    cleaned_paths: list[str] = []

    async def _download_item(index, item, media_kind):
        events.append(f"download:{index}:{media_kind}")
        return DownloadMetrics(
            url=item.url,
            path=str(downloaded_file),
            size=downloaded_file.stat().st_size,
            elapsed=0.01,
            used_multipart=False,
            resumed=False,
        )

    async def _send_entries(entries):
        events.append("send_entries")
        sent_entries.append(entries)

    async def _cleanup_path(path):
        cleaned_paths.append(path)

    async def _update_status(text):
        events.append(f"status:{text}")

    async def _on_after_send():
        events.append("after_send")

    async def _delete_status_message():
        events.append("delete_status")

    async def _on_empty():
        events.append("empty")

    result = await orchestration.run_media_group_flow(
        media_list=media_list,
        db_service=db,
        kind_getter=lambda item: item.type,
        build_cache_key=lambda index, _item, media_kind: f"cache:{index}:{media_kind}",
        download_item=_download_item,
        metrics_label="test_group",
        error_label="Test",
        update_status=_update_status,
        upload_status_text="Uploading...",
        send_entries=_send_entries,
        on_empty=_on_empty,
        delete_status_message=_delete_status_message,
        cleanup_path=_cleanup_path,
        on_after_send=_on_after_send,
    )

    assert result is True
    assert "download:1:video" in events
    assert "download:0:photo" not in events
    assert events[-4:] == ["status:Uploading...", "send_entries", "after_send", "delete_status"]
    assert cleaned_paths == [str(downloaded_file)]
    entries = sent_entries[0]
    assert entries[0]["file_id"] == "cached-photo-id"
    assert entries[0]["cached"] is True
    assert entries[1]["path"] == str(downloaded_file)
    assert "empty" not in events


@pytest.mark.asyncio
async def test_run_media_group_flow_calls_on_empty_when_nothing_resolved():
    db = _FakeDb()
    events: list[str] = []

    async def _download_item(_index, _item, _media_kind):
        return None

    async def _on_empty():
        events.append("empty")

    async def _fail(*_args, **_kwargs):
        raise AssertionError("must not be called on the empty path")

    result = await orchestration.run_media_group_flow(
        media_list=[SimpleNamespace(url="https://cdn.example.com/1.jpg", type="photo")],
        db_service=db,
        kind_getter=lambda item: item.type,
        build_cache_key=lambda index, _item, media_kind: f"cache:{index}:{media_kind}",
        download_item=_download_item,
        metrics_label="test_group",
        error_label="Test",
        update_status=_fail,
        upload_status_text="Uploading...",
        send_entries=_fail,
        on_empty=_on_empty,
        delete_status_message=_fail,
        cleanup_path=_fail,
    )

    assert result is False
    assert events == ["empty"]


@pytest.mark.asyncio
async def test_run_single_media_flow_records_download_history():
    db = _FakeDb()
    db.record_download = AsyncMock()

    cache_key = "https://x.com/user/status/999"

    async def _noop(*_args, **_kwargs):
        return None

    async def _download_media():
        return DownloadMetrics(
            url=cache_key,
            path="downloads/video.mp4",
            size=2048,
            elapsed=0.5,
            used_multipart=False,
            resumed=False,
        )

    async def _send_downloaded(_path):
        return {"video": SimpleNamespace(file_id="vid_123")}

    result = await orchestration.run_single_media_flow(
        cache_key=cache_key,
        cache_file_type="video",
        db_service=db,
        upload_status_text="Uploading...",
        upload_action="upload_video",
        update_status=_noop,
        send_chat_action=_noop,
        send_cached=_noop,
        download_media=_download_media,
        send_downloaded=_send_downloaded,
        extract_file_id=lambda msg: msg["video"].file_id,
        cleanup_path=_noop,
        delete_status_message=_noop,
        on_missing_media=_noop,
        user_id=42,
        chat_id=100,
        chat_type="private",
        service="twitter",
        url=cache_key,
        title="Sample Video",
    )

    assert result is not None
    db.record_download.assert_awaited_once_with(
        user_id=42,
        chat_id=100,
        chat_type="private",
        service="twitter",
        url=cache_key,
        title="Sample Video",
        file_type="video",
        file_id="vid_123",
        file_size_bytes=2048,
        duration_seconds=0.5,
        status="success",
        error_message=None,
    )

