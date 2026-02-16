import pytest

from handlers import instagram
from utils.download_manager import DownloadMetrics


def test_strip_instagram_url_strips_tracking():
    url = "https://www.instagram.com/reel/ABC123/?utm_source=ig_web_copy_link#frag"
    assert instagram.strip_instagram_url(url) == "https://www.instagram.com/reel/ABC123/"


@pytest.mark.asyncio
async def test_instagram_service_download_media_success(monkeypatch, tmp_path):
    service = instagram.InstagramService(output_dir=str(tmp_path))

    async def fake_download(url, filename, **_kwargs):
        path = tmp_path / filename
        path.write_bytes(b"data")
        return DownloadMetrics(
            url=url,
            path=str(path),
            size=path.stat().st_size,
            elapsed=0.01,
            used_multipart=False,
            resumed=False,
        )

    monkeypatch.setattr(service._downloader, "download", fake_download)

    metrics = await service.download_media("https://example.com/v.mp4", "ig.mp4")

    assert metrics is not None
    assert (tmp_path / "ig.mp4").exists()


@pytest.mark.asyncio
async def test_instagram_service_download_media_handles_error(monkeypatch, tmp_path):
    service = instagram.InstagramService(output_dir=str(tmp_path))

    async def fake_download(*_args, **_kwargs):
        raise instagram.DownloadError("fail")

    monkeypatch.setattr(service._downloader, "download", fake_download)

    assert await service.download_media("https://example.com/v.mp4", "ig.mp4") is None


@pytest.mark.asyncio
async def test_instagram_service_fetch_data_parses_tunnel(monkeypatch, tmp_path):
    payload_response = {"status": "tunnel", "url": "https://cdn.example.com/video.mp4"}
    captured = {}

    async def fake_fetch_cobalt_data(base_url, api_key, payload, **kwargs):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return payload_response

    monkeypatch.setattr(instagram, "COBALT_API_URL", "https://cobalt.test")
    monkeypatch.setattr(instagram, "COBALT_API_KEY", "test-key")
    monkeypatch.setattr(instagram, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = instagram.InstagramService(output_dir=str(tmp_path))
    data = await service.fetch_data("https://instagram.com/reel/xyz")

    assert data is not None
    assert len(data.media_list) == 1
    assert data.media_list[0].url == "https://cdn.example.com/video.mp4"
    assert data.media_list[0].type == "video"
    assert captured["base_url"] == "https://cobalt.test"
    assert captured["api_key"] == "test-key"
    assert captured["payload"]["downloadMode"] == "auto"
    assert captured["kwargs"]["source"] == "instagram"


@pytest.mark.asyncio
async def test_instagram_service_fetch_data_parses_picker(monkeypatch, tmp_path):
    payload = {
        "status": "picker",
        "picker": [
            {"type": "photo", "url": "https://cdn.example.com/1.jpg"},
            {"type": "video", "url": "https://cdn.example.com/2.mp4"},
        ],
    }

    async def fake_fetch_cobalt_data(*_args, **_kwargs):
        return payload

    monkeypatch.setattr(instagram, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = instagram.InstagramService(output_dir=str(tmp_path))
    data = await service.fetch_data("https://instagram.com/p/abc")

    assert data is not None
    assert [m.type for m in data.media_list] == ["photo", "video"]
    assert [m.url for m in data.media_list] == [
        "https://cdn.example.com/1.jpg",
        "https://cdn.example.com/2.mp4",
    ]


@pytest.mark.asyncio
async def test_instagram_service_fetch_data_audio_uses_picker_audio(monkeypatch, tmp_path):
    payload = {
        "status": "picker",
        "audio": "https://cdn.example.com/audio.mp3",
        "picker": [
            {"type": "photo", "url": "https://cdn.example.com/1.jpg"},
        ],
    }
    captured = {}

    async def fake_fetch_cobalt_data(_base_url, _api_key, request_payload, **_kwargs):
        captured["payload"] = request_payload
        return payload

    monkeypatch.setattr(instagram, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = instagram.InstagramService(output_dir=str(tmp_path))
    data = await service.fetch_data("https://instagram.com/reel/xyz", audio_only=True)

    assert data is not None
    assert len(data.media_list) == 1
    assert data.media_list[0].type == "audio"
    assert data.media_list[0].url == "https://cdn.example.com/audio.mp3"
    assert captured["payload"]["downloadMode"] == "audio"


@pytest.mark.asyncio
async def test_instagram_service_fetch_data_returns_none_on_error_status(monkeypatch, tmp_path):
    payload = {"status": "error", "error": {"code": "api.fetch.failed"}}

    async def fake_fetch_cobalt_data(*_args, **_kwargs):
        return payload

    monkeypatch.setattr(instagram, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = instagram.InstagramService(output_dir=str(tmp_path))
    data = await service.fetch_data("https://instagram.com/reel/xyz")

    assert data is None


@pytest.mark.asyncio
async def test_instagram_service_fetch_data_returns_none_when_cobalt_client_fails(monkeypatch, tmp_path):
    async def fake_fetch_cobalt_data(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instagram, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = instagram.InstagramService(output_dir=str(tmp_path))
    data = await service.fetch_data("https://instagram.com/reel/xyz")

    assert data is None
