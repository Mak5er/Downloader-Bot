import os
from types import SimpleNamespace

import pytest

from handlers import twitter


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

    def iter_content(self, chunk_size=8192):
        for chunk in self._iter_chunks:
            yield chunk


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
    monkeypatch.setattr(
        twitter.requests,
        "get",
        lambda url: DummyResponse(json_data=sample_json),
    )
    result = twitter.scrape_media("1")
    assert result == sample_json


def test_scrape_media_parses_html_error(monkeypatch):
    error_html = '<meta content="Rate limit exceeded" property="og:description" />'
    json_error = twitter.requests.exceptions.JSONDecodeError("msg", "doc", 0)

    monkeypatch.setattr(
        twitter.requests,
        "get",
        lambda url: DummyResponse(json_data=json_error, text=error_html),
    )

    with pytest.raises(Exception) as exc:
        twitter.scrape_media("1")

    assert "Rate limit exceeded" in str(exc.value)


@pytest.mark.asyncio
async def test_download_media_saves_file(monkeypatch, tmp_path):
    chunks = [b"hello", b"world"]
    monkeypatch.setattr(
        twitter.requests,
        "get",
        lambda url, stream=True: DummyResponse(iter_chunks=chunks),
    )

    target = tmp_path / "media.mp4"
    await twitter.download_media("https://example.com/file", str(target))
    assert target.read_bytes() == b"helloworld"


@pytest.mark.asyncio
async def test_download_media_raises_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        twitter.requests,
        "get",
        lambda url, stream=True: DummyResponse(status_code=500),
    )

    with pytest.raises(Exception):
        await twitter.download_media("https://example.com/file", str(tmp_path / "media.mp4"))
