from types import SimpleNamespace

import pytest

from handlers import tiktok


@pytest.fixture(autouse=True)
def stub_user_agent(monkeypatch):
    monkeypatch.setattr(tiktok, "UserAgent", lambda: SimpleNamespace(random="Agent"))
    # Avoid cross-test pollution from URL expansion cache.
    if hasattr(tiktok, "_expand_tiktok_url_cached"):
        tiktok._expand_tiktok_url_cached.cache_clear()


class DummyResponse:
    def __init__(self, url=None):
        self.url = url


def test_process_tiktok_url_expands_short_links(monkeypatch):
    expected_url = "https://www.tiktok.com/@user/video/123456"

    def fake_head(url, allow_redirects, headers, timeout=None):
        assert allow_redirects is True
        assert "User-Agent" in headers
        return DummyResponse(url=expected_url)

    monkeypatch.setattr(tiktok.requests, "head", fake_head)
    result = tiktok.process_tiktok_url("https://vm.tiktok.com/ABC123/")
    assert result == expected_url


def test_process_tiktok_url_returns_original_on_error(monkeypatch):
    original_url = "https://vm.tiktok.com/ABC123/"

    def boom(*_a, **_k):
        raise tiktok.requests.RequestException("fail")

    monkeypatch.setattr(tiktok.requests, "head", boom)
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
    assert info.author == "creator"


@pytest.mark.asyncio
async def test_video_info_none_on_error():
    assert await tiktok.video_info({"error": "quota"}) is None
    assert await tiktok.video_info({"error": None, "code": 1, "message": "bad"}) is None
