from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers.media_delivery import send_cached_media_entries


@pytest.mark.asyncio
async def test_send_cached_media_entries_replies_only_on_first_batch_and_caches_results():
    message = SimpleNamespace(
        message_id=7,
        answer_media_group=AsyncMock(return_value=[SimpleNamespace(photo=[SimpleNamespace(file_id="sent-photo-id")])]),
        answer_video=AsyncMock(return_value=SimpleNamespace(video=SimpleNamespace(file_id="sent-video-id"))),
        reply_video=AsyncMock(),
        answer_photo=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    db_service = SimpleNamespace(add_file=AsyncMock())
    entries = [
        {
            "kind": "photo",
            "cache_key": "post#0",
            "file_id": None,
            "path": "/tmp/1.jpg",
            "cached": False,
        },
        {
            "kind": "video",
            "cache_key": "post#1",
            "file_id": None,
            "path": "/tmp/2.mp4",
            "cached": False,
        },
    ]

    await send_cached_media_entries(
        message,
        entries,
        db_service=db_service,
        caption="caption",
        reply_markup=None,
    )

    assert message.answer_media_group.await_args.kwargs["reply_to_message_id"] == 7
    assert message.answer_video.await_args.kwargs["video"].path == "/tmp/2.mp4"
    assert message.reply_video.await_count == 0
    assert db_service.add_file.await_count == 2


@pytest.mark.asyncio
async def test_send_cached_media_entries_supports_url_based_photo_entries():
    message = SimpleNamespace(
        message_id=11,
        answer_media_group=AsyncMock(return_value=[SimpleNamespace(photo=[SimpleNamespace(file_id="album-photo-id")])]),
        answer_photo=AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="last-photo-id")])),
        reply_photo=AsyncMock(),
        answer_video=AsyncMock(),
        reply_video=AsyncMock(),
    )
    db_service = SimpleNamespace(add_file=AsyncMock())
    entries = [
        {
            "kind": "photo",
            "cache_key": "album#0",
            "file_id": "cached-photo-id",
            "url": "https://cdn.example.com/1.jpg",
            "cached": True,
        },
        {
            "kind": "photo",
            "cache_key": "album#1",
            "file_id": None,
            "url": "https://cdn.example.com/2.jpg",
            "cached": False,
        },
    ]

    await send_cached_media_entries(
        message,
        entries,
        db_service=db_service,
        caption="caption",
        reply_markup=None,
    )

    assert message.answer_media_group.await_args.kwargs["reply_to_message_id"] == 11
    assert message.answer_photo.await_args.kwargs["photo"] == "https://cdn.example.com/2.jpg"
    db_service.add_file.assert_awaited_once_with("album#1", "last-photo-id", "photo")
