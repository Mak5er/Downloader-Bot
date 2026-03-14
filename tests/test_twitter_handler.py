import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.client_exceptions import ClientResponseError

from handlers import twitter
from utils.download_manager import DownloadMetrics


class DummyResponse:
    def __init__(self, *, url=None, status_code=200, text=""):
        self.url = url
        self.status_code = status_code
        self._text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ClientResponseError(
                request_info=SimpleNamespace(real_url=self.url or "https://example.com"),
                history=(),
                status=self.status_code,
                message=f"status {self.status_code}",
                headers=None,
            )

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummySession:
    def __init__(self, handler):
        self._handler = handler

    def get(self, *args, **kwargs):
        return self._handler(*args, **kwargs)


@pytest.mark.asyncio
async def test_extract_tweet_ids_expands_short_links(monkeypatch):
    def fake_get(url, allow_redirects=True, timeout=5):
        return DummyResponse(url="https://twitter.com/user/status/1234567890")

    monkeypatch.setattr(twitter, "get_http_session", AsyncMock(return_value=DummySession(fake_get)))
    result = await twitter.extract_tweet_ids_async("Check this https://t.co/abc123")
    assert result == ["1234567890"]


@pytest.mark.asyncio
async def test_extract_tweet_ids_none(monkeypatch):
    monkeypatch.setattr(twitter, "get_http_session", AsyncMock(return_value=DummySession(lambda *a, **k: DummyResponse())))
    assert await twitter.extract_tweet_ids_async("No twitter links here") is None


@pytest.mark.asyncio
async def test_scrape_media_success(monkeypatch):
    sample_json = {"tweetURL": "https://twitter.com/user/status/1"}
    monkeypatch.setattr(
        twitter,
        "get_http_session",
        AsyncMock(return_value=DummySession(lambda url, timeout=None: DummyResponse(text=twitter.json.dumps(sample_json)))),
    )
    result = await twitter.scrape_media_async("1")
    assert result == sample_json


@pytest.mark.asyncio
async def test_collect_media_files_downloads_to_output_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(twitter, "OUTPUT_DIR", str(tmp_path))
    twitter.twitter_downloader.output_dir = str(tmp_path)

    async def fake_download(url, filename, **_kwargs):
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
        return DownloadMetrics(
            url=url,
            path=str(path),
            size=path.stat().st_size,
            elapsed=0.01,
            used_multipart=False,
            resumed=False,
        )

    monkeypatch.setattr(twitter.twitter_downloader, "download", fake_download)

    tweet_media = {
        "media_extended": [
            {"type": "image", "url": "https://cdn.example.com/1.jpg"},
            {"type": "video", "url": "https://cdn.example.com/2.mp4"},
        ]
    }

    photos, videos = await twitter._collect_media_files("42", tweet_media)

    assert len(photos) == 1
    assert len(videos) == 1
    assert os.path.exists(photos[0])
    assert os.path.exists(videos[0])
