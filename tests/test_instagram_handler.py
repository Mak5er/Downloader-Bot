import pytest

from handlers import instagram


class DummyResponse:
    def __init__(self, status_code=200, json_data=None, content=b"data"):
        self.status_code = status_code
        self._json = json_data or {}
        self.content = content
        self._closed = False

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise instagram.requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size=1):
        yield self.content

    def close(self):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class _DummyClip:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture(autouse=True)
def stub_video_clip(monkeypatch):
    monkeypatch.setattr(instagram, "VideoFileClip", lambda *_: _DummyClip())


def test_extract_instagram_url():
    text = "https://www.instagram.com/reel/ABC123/ some caption"
    assert instagram.extract_instagram_url(text) == "https://www.instagram.com/reel/ABC123/"


def test_extract_instagram_url_returns_original():
    text = "not a link"
    assert instagram.extract_instagram_url(text) == text


def test_downloader_download_video_success(monkeypatch, tmp_path):
    target = tmp_path / "ig.mp4"
    monkeypatch.setattr(
        instagram.requests,
        "get",
        lambda url, **kwargs: DummyResponse(content=b"video"),
    )
    downloader = instagram.DownloaderInstagram(output_dir=str(tmp_path), filename=str(target))
    assert downloader.download_video("https://example.com/video.mp4") is True
    assert target.exists()
    assert target.read_bytes() == b"video"


def test_downloader_download_video_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        instagram.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=500),
    )
    downloader = instagram.DownloaderInstagram(output_dir=str(tmp_path), filename=str(tmp_path / "fail.mp4"))
    assert downloader.download_video("https://example.com/video.mp4") is False


@pytest.mark.asyncio
async def test_fetch_instagram_post_data_success(monkeypatch):
    sample_data = {
        "data": {
            "id": "1",
            "code": "CODE",
            "caption": {"text": "Caption"},
            "thumbnail_url": "https://example.com/thumb.jpg",
            "metrics": {
                "play_count": 123,
                "like_count": 45,
                "comment_count": 6,
                "share_count": 7,
            },
            "original_height": 800,
            "original_width": 600,
            "is_video": True,
            "video_url": "https://example.com/video.mp4",
        }
    }

    def fake_get(url, headers=None, params=None, timeout=None):
        assert headers["x-rapidapi-key"] in {"KEY1", "KEY2"}
        return DummyResponse(json_data=sample_data)

    monkeypatch.setattr(instagram.requests, "get", fake_get)
    monkeypatch.setattr(instagram, "RAPID_API_KEYS", ["KEY1", "KEY2"])

    result = await instagram.DownloaderInstagram.fetch_instagram_post_data("https://instagram.com/p/xyz")
    assert result is not None
    assert result.id == "1"
    assert result.video_urls == ["https://example.com/video.mp4"]
    assert result.image_urls == []
    assert result.description == "Caption"
    assert result.is_video is True


@pytest.mark.asyncio
async def test_fetch_instagram_post_data_handles_failures(monkeypatch):
    call_count = {"value": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        call_count["value"] += 1
        raise instagram.requests.RequestException("network error")

    monkeypatch.setattr(instagram.requests, "get", fake_get)
    monkeypatch.setattr(instagram, "RAPID_API_KEYS", ["KEY1"])

    result = await instagram.DownloaderInstagram.fetch_instagram_post_data("https://instagram.com/p/fail")
    assert result is None
    assert call_count["value"] == 1


@pytest.mark.asyncio
async def test_fetch_instagram_user_data_success(monkeypatch):
    sample_user = {
        "data": {
            "id": "u1",
            "page_name": "user",
            "follower_count": 1000,
            "media_count": 12,
            "hd_profile_pic_url_info": {"url": "https://example.com/pic.jpg"},
            "biography": "bio",
        }
    }

    def fake_get(url, headers=None, params=None, timeout=None):
        return DummyResponse(json_data=sample_user)

    monkeypatch.setattr(instagram.requests, "get", fake_get)
    monkeypatch.setattr(instagram, "RAPID_API_KEYS", ["KEY1"])

    result = await instagram.DownloaderInstagram.fetch_instagram_user_data("https://instagram.com/user")
    assert result is not None
    assert result.nickname == "user"
    assert result.followers == 1000
    assert result.profile_pic == "https://example.com/pic.jpg"


@pytest.mark.asyncio
async def test_fetch_instagram_user_data_handles_missing(monkeypatch):
    monkeypatch.setattr(instagram.requests, "get", lambda *args, **kwargs: DummyResponse(json_data={}))
    monkeypatch.setattr(instagram, "RAPID_API_KEYS", ["KEY1"])

    result = await instagram.DownloaderInstagram.fetch_instagram_user_data("https://instagram.com/user")
    assert result is None
