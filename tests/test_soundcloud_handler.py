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
