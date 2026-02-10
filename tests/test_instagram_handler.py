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


class _DummyResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload


class _DummyRequestCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DummySession:
    def __init__(self, response):
        self._response = response

    def post(self, *_args, **_kwargs):
        return _DummyRequestCtx(self._response)


@pytest.mark.asyncio
async def test_instagram_service_fetch_data_parses_single_url(monkeypatch, tmp_path):
    payload = {"url": "https://cdn.example.com/video.mp4"}

    async def fake_get_http_session():
        return _DummySession(_DummyResponse(200, payload))

    monkeypatch.setattr(instagram, "COBALT_API_URL", "https://cobalt.test")
    monkeypatch.setattr(instagram, "get_http_session", fake_get_http_session)

    service = instagram.InstagramService(output_dir=str(tmp_path))
    data = await service.fetch_data("https://instagram.com/reel/xyz")

    assert data is not None
    assert len(data.media_list) == 1
    assert data.media_list[0].url == "https://cdn.example.com/video.mp4"
    assert data.media_list[0].type in {"video", "photo"}


@pytest.mark.asyncio
async def test_instagram_service_fetch_data_parses_picker(monkeypatch, tmp_path):
    payload = {
        "picker": [
            {"type": "photo", "url": "https://cdn.example.com/1.jpg"},
            {"type": "video", "url": "https://cdn.example.com/2.mp4"},
        ]
    }

    async def fake_get_http_session():
        return _DummySession(_DummyResponse(200, payload))

    monkeypatch.setattr(instagram, "COBALT_API_URL", "https://cobalt.test")
    monkeypatch.setattr(instagram, "get_http_session", fake_get_http_session)

    service = instagram.InstagramService(output_dir=str(tmp_path))
    data = await service.fetch_data("https://instagram.com/p/abc")

    assert data is not None
    assert [m.type for m in data.media_list] == ["photo", "video"]
    assert [m.url for m in data.media_list] == [
        "https://cdn.example.com/1.jpg",
        "https://cdn.example.com/2.mp4",
    ]
