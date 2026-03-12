from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import soundcloud
from utils.download_manager import DownloadMetrics


def test_strip_soundcloud_url_strips_tracking():
    url = "https://soundcloud.com/artist/track-name?si=abc123&utm_source=clipboard#frag"
    assert soundcloud.strip_soundcloud_url(url) == "https://soundcloud.com/artist/track-name"


def test_parse_soundcloud_track_tunnel():
    payload = {
        "status": "tunnel",
        "url": "https://cdn.example.com/audio.mp3",
        "filename": "artist-track.mp3",
    }
    track = soundcloud.parse_soundcloud_track(payload, "https://soundcloud.com/artist/track")
    assert track is not None
    assert track.audio_url == "https://cdn.example.com/audio.mp3"
    assert track.title != ""


def test_parse_soundcloud_track_local_processing_with_cover():
    payload = {
        "status": "local-processing",
        "type": "audio",
        "tunnel": [
            "https://cdn.example.com/cover.jpg",
            "https://cdn.example.com/final.mp3",
        ],
        "output": {
            "type": "audio/mpeg",
            "filename": "final.mp3",
            "metadata": {
                "title": "Track Title",
                "artist": "Artist Name",
            },
        },
        "audio": {"cover": True},
    }
    track = soundcloud.parse_soundcloud_track(payload, "https://soundcloud.com/artist/track")
    assert track is not None
    assert track.audio_url == "https://cdn.example.com/final.mp3"
    assert track.thumbnail_url == "https://cdn.example.com/cover.jpg"
    assert track.title == "Track Title"
    assert track.artist == "Artist Name"


@pytest.mark.asyncio
async def test_soundcloud_service_fetch_track_uses_cobalt_client(monkeypatch, tmp_path):
    captured = {}
    payload = {
        "status": "tunnel",
        "url": "https://cdn.example.com/audio.mp3",
        "filename": "track.mp3",
    }

    async def fake_fetch_cobalt_data(base_url, api_key, request_payload, **kwargs):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        captured["payload"] = request_payload
        captured["kwargs"] = kwargs
        return payload

    monkeypatch.setattr(soundcloud, "COBALT_API_URL", "https://cobalt.test")
    monkeypatch.setattr(soundcloud, "COBALT_API_KEY", "test-key")
    monkeypatch.setattr(soundcloud, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = soundcloud.SoundCloudService(output_dir=str(tmp_path))
    track = await service.fetch_track("https://soundcloud.com/artist/track")

    assert track is not None
    assert captured["base_url"] == "https://cobalt.test"
    assert captured["api_key"] == "test-key"
    assert captured["payload"]["downloadMode"] == "audio"
    assert captured["kwargs"]["source"] == "soundcloud"


@pytest.mark.asyncio
async def test_soundcloud_service_download_media_success(monkeypatch, tmp_path):
    service = soundcloud.SoundCloudService(output_dir=str(tmp_path))

    async def fake_download(url, filename, **_kwargs):
        path = tmp_path / filename
        path.write_bytes(b"audio")
        return DownloadMetrics(
            url=url,
            path=str(path),
            size=path.stat().st_size,
            elapsed=0.01,
            used_multipart=False,
            resumed=False,
        )

    monkeypatch.setattr(service._downloader, "download", fake_download)
    metrics = await service.download_media("https://cdn.example.com/audio.mp3", "track.mp3")

    assert metrics is not None
    assert (tmp_path / "track.mp3").exists()


@pytest.mark.asyncio
async def test_soundcloud_service_download_media_handles_error(monkeypatch, tmp_path):
    service = soundcloud.SoundCloudService(output_dir=str(tmp_path))

    async def fake_download(*_args, **_kwargs):
        raise soundcloud.DownloadError("boom")

    monkeypatch.setattr(service._downloader, "download", fake_download)
    metrics = await service.download_media("https://cdn.example.com/audio.mp3", "track.mp3")

    assert metrics is None


@pytest.mark.asyncio
async def test_inline_soundcloud_uses_bot_avatar_thumbnail(monkeypatch, tmp_path):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    token = soundcloud.create_inline_video_request(
        "soundcloud",
        "https://soundcloud.com/artist/track",
        42,
        settings,
    )
    result = SimpleNamespace(
        result_id=f"soundcloud_inline:{token}",
        inline_message_id="inline-message-1",
        from_user=SimpleNamespace(full_name="Inline User"),
    )
    audio_path = tmp_path / "track.mp3"
    audio_path.write_bytes(b"audio")
    metrics = DownloadMetrics(
        url="https://cdn.example.com/audio.mp3",
        path=str(audio_path),
        size=audio_path.stat().st_size,
        elapsed=0.1,
        used_multipart=False,
        resumed=False,
    )
    bot_avatar = object()

    monkeypatch.setattr(
        soundcloud.soundcloud_service,
        "fetch_track",
        AsyncMock(
            return_value=soundcloud.SoundCloudTrack(
                id="track-1",
                source_url="https://soundcloud.com/artist/track",
                audio_url="https://cdn.example.com/audio.mp3",
                title="Track Title",
                artist="Artist Name",
                thumbnail_url="https://cdn.example.com/cover.jpg",
            )
        ),
    )
    monkeypatch.setattr(
        soundcloud.soundcloud_service,
        "download_media",
        AsyncMock(return_value=metrics),
    )
    monkeypatch.setattr(soundcloud.db, "get_file_id", AsyncMock(return_value=None))
    monkeypatch.setattr(soundcloud.db, "add_file", AsyncMock())
    monkeypatch.setattr(soundcloud, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(soundcloud, "get_bot_avatar_thumbnail", AsyncMock(return_value=bot_avatar))
    monkeypatch.setattr(soundcloud, "safe_edit_inline_text", AsyncMock(return_value=True))
    monkeypatch.setattr(soundcloud, "safe_edit_inline_media", AsyncMock(return_value=True))
    monkeypatch.setattr(soundcloud, "remove_file", AsyncMock())
    monkeypatch.setattr(
        soundcloud.bot,
        "send_audio",
        AsyncMock(return_value=SimpleNamespace(audio=SimpleNamespace(file_id="cached-file-id"))),
    )

    await soundcloud.chosen_inline_soundcloud_result(result)

    send_kwargs = soundcloud.bot.send_audio.await_args.kwargs
    assert send_kwargs["thumbnail"] is bot_avatar
    assert soundcloud.soundcloud_service.download_media.await_count == 1
