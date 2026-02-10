import os
from types import SimpleNamespace

import pytest

from handlers import twitter
from utils.download_manager import DownloadMetrics


class DummyResponse:
    def __init__(self, *, url=None, status_code=200, json_data=None, text="", iter_chunks=None):
        self.url = url
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._iter_chunks = iter_chunks or [b"data"]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise twitter.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


class DummySession:
    def __init__(self, handler):
        self._handler = handler

    def get(self, *args, **kwargs):
        return self._handler(*args, **kwargs)

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def test_extract_tweet_ids_expands_short_links(monkeypatch):
    def fake_get(url, allow_redirects=True, timeout=5):
        return DummyResponse(url="https://twitter.com/user/status/1234567890")

    monkeypatch.setattr(twitter.requests, "Session", lambda: DummySession(fake_get))
    result = twitter.extract_tweet_ids("Check this https://t.co/abc123")
    assert result == ["1234567890"]


def test_extract_tweet_ids_none(monkeypatch):
    monkeypatch.setattr(twitter.requests, "Session", lambda: DummySession(lambda *a, **k: DummyResponse()))
    assert twitter.extract_tweet_ids("No twitter links here") is None


def test_scrape_media_success(monkeypatch):
    sample_json = {"tweetURL": "https://twitter.com/user/status/1"}
    monkeypatch.setattr(twitter.requests, "get", lambda url: DummyResponse(json_data=sample_json))
    result = twitter.scrape_media("1")
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
