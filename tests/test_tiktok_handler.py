import os
from types import SimpleNamespace

import pytest

from handlers import tiktok


class DummyResponse:
    def __init__(self, url=None, content=b"", status_code=200, json_data=None, headers=None, iter_chunks=None):
        self.url = url
        self.content = content
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}
        self._iter_chunks = iter_chunks or ([content] if content else [])

    def raise_for_status(self):
        if self.status_code >= 400:
            raise tiktok.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json_data

    def iter_content(self, chunk_size=1):
        for chunk in self._iter_chunks:
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class DummyVideoClip:
    def __init__(self, size):
        self.size = size

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture(autouse=True)
def stub_user_agent(monkeypatch):
    monkeypatch.setattr(tiktok, "UserAgent", lambda: SimpleNamespace(random="Agent"))


def test_process_tiktok_url_expands_short_links(monkeypatch):
    expected_url = "https://www.tiktok.com/@user/video/123456"

    def fake_head(url, allow_redirects, headers):
        assert allow_redirects is True
        assert "User-Agent" in headers
        return DummyResponse(url=expected_url)

    monkeypatch.setattr(tiktok.requests, "head", fake_head)
    result = tiktok.process_tiktok_url("https://vm.tiktok.com/ABC123/")
    assert result == expected_url


def test_process_tiktok_url_returns_original_on_error(monkeypatch):
    original_url = "https://vm.tiktok.com/ABC123/"
    monkeypatch.setattr(tiktok.requests, "head", lambda *_, **__: (_ for _ in ()).throw(tiktok.requests.RequestException()))
    result = tiktok.process_tiktok_url(original_url)
    assert result == original_url


def test_process_tiktok_url_strips_query(monkeypatch):
    expanded_url = "https://www.tiktok.com/@user/video/123456"

    def fake_head(url, allow_redirects, headers):
        return DummyResponse(url=f"{expanded_url}?is_from_webapp=1&sender_device=pc")

    monkeypatch.setattr(tiktok.requests, "head", fake_head)
    result = tiktok.process_tiktok_url(f"{expanded_url}?is_from_webapp=1&sender_device=pc")
    assert result == expanded_url


def test_get_video_id_from_url():
    url = "https://www.tiktok.com/@user/video/1234567890?lang=en"
    assert tiktok.get_video_id_from_url(url) == "1234567890"


@pytest.mark.asyncio
async def test_video_info_returns_dataclass():
    data = {
        "error": None,
        "code": 0,
        "data": {
            "id": "123",
            "title": "Funny video",
            "cover": "https://example.com/cover.jpg",
            "play_count": 1000,
            "digg_count": 100,
            "comment_count": 25,
            "share_count": 10,
            "music_info": {"play": "https://example.com/music.mp3"},
            "author": {"unique_id": "creator"},
        },
    }
    info = await tiktok.video_info(data)
    assert info is not None
    assert info.id == "123"
    assert info.description == "Funny video"
    assert info.likes == 100
    assert info.music_play_url == "https://example.com/music.mp3"
    assert info.author == "creator"


@pytest.mark.asyncio
async def test_video_info_none_on_error():
    data_with_error = {"error": "quota exceeded"}
    data_with_bad_code = {"error": None, "code": 1, "message": "bad request"}
    assert await tiktok.video_info(data_with_error) is None
    assert await tiktok.video_info(data_with_bad_code) is None


def test_downloader_download_video_success(monkeypatch, tmp_path):
    file_path = tmp_path / "video.mp4"

    def fake_head(url, headers=None, allow_redirects=True, timeout=10):
        return DummyResponse(headers={"Content-Length": "4", "Accept-Ranges": "bytes"})

    def fake_get(url, headers=None, stream=True, allow_redirects=True, timeout=(5, 60)):
        assert "video/media/play" in url
        return DummyResponse(content=b"data", iter_chunks=[b"data"])

    monkeypatch.setattr(tiktok.requests, "head", fake_head)
    monkeypatch.setattr(tiktok.requests, "get", fake_get)
    downloader = tiktok.DownloaderTikTok(output_dir=str(tmp_path), filename=str(file_path))

    assert downloader._download_video_sync("abc123") is True
    assert file_path.exists()
    assert file_path.read_bytes() == b"data"


def test_downloader_download_video_failure(monkeypatch, tmp_path):
    file_path = tmp_path / "video.mp4"
    monkeypatch.setattr(
        tiktok.requests,
        "head",
        lambda *args, **kwargs: DummyResponse(headers={"Accept-Ranges": "none"}),
    )
    monkeypatch.setattr(
        tiktok.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=500),
    )
    downloader = tiktok.DownloaderTikTok(output_dir=str(tmp_path), filename=str(file_path))
    assert downloader._download_video_sync("abc123") is False
    assert not file_path.exists()


def test_get_size_sync(monkeypatch):
    monkeypatch.setattr(tiktok, "VideoFileClip", lambda _: DummyVideoClip((720, 1280)))
    width, height = tiktok.DownloaderTikTok(output_dir="", filename="")._get_size_sync("file.mp4")
    assert width == 720
    assert height == 1280


def test_user_info_sync_returns_user(monkeypatch):
    requested_urls = []

    def fake_get(url, headers=None, timeout=None, allow_redirects=True):
        requested_urls.append(url)
        if "exist" in url:
            return DummyResponse(json_data={"sec_uid": "SEC123", "nickname": "Nickname"})
        if "userinfo" in url:
            return DummyResponse(
                json_data={
                    "followerCount": 111,
                    "videoCount": 7,
                    "heartCount": 999,
                    "avatarThumb": "avatar",
                    "signature": "bio",
                }
            )
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr(tiktok, "UserAgent", lambda: SimpleNamespace(random="Agent"))
    monkeypatch.setattr(tiktok.requests, "get", fake_get)
    monkeypatch.setattr(tiktok.time, "sleep", lambda *_: None)

    downloader = tiktok.DownloaderTikTok(output_dir="", filename="")
    result = downloader._user_info_sync("testuser")

    assert result is not None
    assert result.nickname == "Nickname"
    assert result.followers == 111
    assert result.videos == 7
    assert result.likes == 999
    assert requested_urls[0].endswith("exist/testuser")
    assert any("userinfo?sec_user_id=SEC123" in url for url in requested_urls)


def test_user_info_sync_returns_none_when_sec_uid_missing(monkeypatch):
    attempts = []

    def fake_get(url, headers=None, timeout=None, allow_redirects=True):
        if "exist" in url:
            attempts.append(url)
            return DummyResponse(json_data={"sec_uid": None})
        raise AssertionError("userinfo should not be requested without sec_uid")

    sleeps = []

    monkeypatch.setattr(tiktok, "UserAgent", lambda: SimpleNamespace(random="Agent"))
    monkeypatch.setattr(tiktok.requests, "get", fake_get)
    monkeypatch.setattr(tiktok.time, "sleep", lambda seconds: sleeps.append(seconds))

    downloader = tiktok.DownloaderTikTok(output_dir="", filename="")
    result = downloader._user_info_sync("missing")

    assert result is None
    assert len(attempts) == 10
    assert sleeps == [1.5] * 10
