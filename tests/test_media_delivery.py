from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import FSInputFile
import pytest

from services.media import delivery
from services.media.delivery import (
    AUDIO_CACHE_VARIANT,
    build_audio_cache_key,
    build_bot_audio_performer,
    send_audio_with_thumbnail,
    send_cached_media_entries,
)


def test_build_audio_cache_key_uses_metadata_version():
    assert build_audio_cache_key("https://example.com/a") == f"https://example.com/a#{AUDIO_CACHE_VARIANT}"


def test_build_bot_audio_performer_uses_bot_username_from_url():
    assert build_bot_audio_performer("https://t.me/maxloadbot") == "@maxloadbot"
    assert build_bot_audio_performer("t.me/maxloadbot") == "@maxloadbot"
    assert build_bot_audio_performer("@maxloadbot") == "@maxloadbot"


@pytest.mark.asyncio
async def test_send_audio_with_thumbnail_adds_bot_avatar_and_performer():
    send_audio = AsyncMock(return_value=SimpleNamespace(audio=SimpleNamespace(file_id="audio-id")))
    bot_avatar = object()

    sent = await send_audio_with_thumbnail(
        send_audio,
        audio="file-id",
        title="Song",
        bot_avatar=bot_avatar,
        bot_url="https://t.me/maxloadbot",
    )

    assert sent.audio.file_id == "audio-id"
    assert send_audio.await_args.kwargs["thumbnail"] is bot_avatar
    assert send_audio.await_args.kwargs["performer"] == "@maxloadbot"


@pytest.mark.asyncio
async def test_send_audio_with_thumbnail_adds_duration_from_metadata():
    send_audio = AsyncMock(return_value=SimpleNamespace(audio=SimpleNamespace(file_id="audio-id")))

    await send_audio_with_thumbnail(
        send_audio,
        audio="file-id",
        bot_url="https://t.me/maxloadbot",
        duration="123.4",
    )

    assert send_audio.await_args.kwargs["duration"] == 123


@pytest.mark.asyncio
async def test_send_audio_with_thumbnail_probes_duration_from_path(monkeypatch):
    send_audio = AsyncMock(return_value=SimpleNamespace(audio=SimpleNamespace(file_id="audio-id")))
    monkeypatch.setattr(delivery, "probe_audio_duration_seconds", AsyncMock(return_value=57))

    await send_audio_with_thumbnail(
        send_audio,
        audio="file-id",
        audio_path="/tmp/audio.mp3",
        bot_url="https://t.me/maxloadbot",
    )

    assert send_audio.await_args.kwargs["duration"] == 57


@pytest.mark.asyncio
async def test_send_audio_with_thumbnail_embeds_cover_for_local_audio(monkeypatch, tmp_path):
    audio_path = tmp_path / "audio.mp3"
    cover_path = tmp_path / "cover.jpg"
    embedded_path = tmp_path / "embedded.mp3"
    audio_path.write_bytes(b"audio")
    cover_path.write_bytes(b"cover")
    embedded_path.write_bytes(b"embedded")
    send_audio = AsyncMock(return_value=SimpleNamespace(audio=SimpleNamespace(file_id="audio-id")))
    monkeypatch.setattr(delivery, "embed_audio_cover", AsyncMock(return_value=str(embedded_path)))

    await send_audio_with_thumbnail(
        send_audio,
        audio=FSInputFile(str(audio_path)),
        audio_path=str(audio_path),
        bot_avatar=FSInputFile(str(cover_path)),
        bot_url="https://t.me/maxloadbot",
    )

    assert send_audio.await_args.kwargs["audio"].path == str(embedded_path)
    assert not embedded_path.exists()


@pytest.mark.asyncio
async def test_send_audio_with_thumbnail_retries_without_thumbnail():
    send_audio = AsyncMock(
        side_effect=[
            RuntimeError("bad thumbnail"),
            SimpleNamespace(audio=SimpleNamespace(file_id="audio-id")),
        ]
    )
    bot_avatar = object()

    await send_audio_with_thumbnail(
        send_audio,
        audio="file-id",
        bot_avatar=bot_avatar,
        bot_url="https://t.me/maxloadbot",
    )

    assert send_audio.await_count == 2
    assert send_audio.await_args_list[0].kwargs["thumbnail"] is bot_avatar
    assert "thumbnail" not in send_audio.await_args_list[1].kwargs
    assert send_audio.await_args_list[1].kwargs["performer"] == "@maxloadbot"


@pytest.mark.asyncio
async def test_send_cached_media_entries_replies_only_on_first_batch_and_caches_results(monkeypatch):
    monkeypatch.setattr(
        delivery,
        "build_video_send_kwargs",
        AsyncMock(return_value={"width": 720, "height": 1280, "supports_streaming": True}),
    )
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
    assert message.answer_video.await_args.kwargs["width"] == 720
    assert message.answer_video.await_args.kwargs["height"] == 1280
    assert message.answer_video.await_args.kwargs["supports_streaming"] is True
    assert message.reply_video.await_count == 0
    assert db_service.add_file.await_count == 2


@pytest.mark.asyncio
async def test_send_cached_media_entries_supports_url_based_photo_entries(monkeypatch):
    monkeypatch.setattr(
        delivery,
        "build_video_send_kwargs",
        AsyncMock(return_value={"supports_streaming": True}),
    )
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


@pytest.mark.asyncio
async def test_send_cached_media_entries_splits_large_albums_into_multiple_batches(monkeypatch):
    monkeypatch.setattr(
        delivery,
        "build_video_send_kwargs",
        AsyncMock(return_value={"supports_streaming": True}),
    )
    message = SimpleNamespace(
        message_id=99,
        answer_media_group=AsyncMock(
            side_effect=[
                [SimpleNamespace(photo=[SimpleNamespace(file_id=f"batch1-photo-{index}")]) for index in range(10)],
                [SimpleNamespace(photo=[SimpleNamespace(file_id=f"batch2-photo-{index}")]) for index in range(2)],
            ]
        ),
        answer_photo=AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="last-photo-id")])),
        reply_photo=AsyncMock(),
        answer_video=AsyncMock(),
        reply_video=AsyncMock(),
    )
    db_service = SimpleNamespace(add_file=AsyncMock())
    entries = [
        {
            "kind": "photo",
            "cache_key": f"album#{index}",
            "file_id": None,
            "path": f"/tmp/{index}.jpg",
            "cached": False,
        }
        for index in range(13)
    ]

    await send_cached_media_entries(
        message,
        entries,
        db_service=db_service,
        caption="caption",
        reply_markup=None,
    )

    assert message.answer_media_group.await_count == 2
    first_batch_kwargs = message.answer_media_group.await_args_list[0].kwargs
    second_batch_kwargs = message.answer_media_group.await_args_list[1].kwargs
    assert first_batch_kwargs["reply_to_message_id"] == 99
    assert "reply_to_message_id" not in second_batch_kwargs
    assert len(first_batch_kwargs["media"]) == 10
    assert len(second_batch_kwargs["media"]) == 2
    assert message.answer_photo.await_args.kwargs["photo"].path == "/tmp/12.jpg"
    assert db_service.add_file.await_count == 13
