from types import SimpleNamespace

import pytest

from handlers import youtube
from utils.download_manager import DownloadMetrics


def test_get_video_stream_prefers_progressive():
    yt = {
        "webpage_url": "https://youtube.com/watch?v=abc",
        "formats": [
            {"height": "480", "vcodec": "avc1", "acodec": "mp4a", "ext": "mp4"},
            {"height": "720", "vcodec": "avc1", "acodec": "mp4a", "ext": "mp4"},
            {"height": "1080", "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
        ],
    }

    stream = youtube.get_video_stream(yt)

    assert stream["height"] == "720"
    assert stream["webpage_url"] == yt["webpage_url"]


def test_get_video_stream_video_only_respects_max_height():
    yt = {
        "webpage_url": "https://youtube.com/watch?v=xyz",
        "formats": [
            {"height": "1080", "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
            {"height": "480", "vcodec": "avc1", "acodec": "none", "ext": "webm"},
        ],
    }

    stream = youtube.get_video_stream(yt, max_height=1080)

    assert stream["height"] == "1080"


def test_get_video_stream_returns_none_when_missing():
    yt = {"webpage_url": "https://youtube.com/watch?v=nope", "formats": []}
    assert youtube.get_video_stream(yt) is None


def test_get_audio_stream_selects_highest_bitrate():
    yt = {
        "webpage_url": "https://youtube.com/watch?v=abc",
        "formats": [
            {"abr": "96", "ext": "m4a", "vcodec": "none"},
            {"abr": "128", "ext": "m4a", "vcodec": "none"},
        ],
    }
    stream = youtube.get_audio_stream(yt)
    assert stream["abr"] == "128"
    assert stream["webpage_url"] == yt["webpage_url"]


def test_get_audio_stream_returns_none_when_missing():
    yt = {
        "webpage_url": "https://youtube.com/watch?v=abc",
        "formats": [
            {"abr": None, "ext": "mp3", "vcodec": "avc1"},
        ],
    }

    assert youtube.get_audio_stream(yt) is None


@pytest.mark.asyncio
async def test_download_stream_calls_downloader(monkeypatch, tmp_path):
    async def fake_download(url, filename, headers=None, skip_if_exists=False):
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

    monkeypatch.setattr(youtube.youtube_downloader, "download", fake_download)

    metrics = await youtube.download_stream({"url": "https://cdn.example.com/v.mp4"}, "out.mp4", "youtube")

    assert metrics is not None
    assert (tmp_path / "out.mp4").exists()


def test_get_youtube_video_returns_none_on_error(monkeypatch):
    class DummyYDL:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download):
            raise youtube.DownloadError("boom")

    monkeypatch.setattr(youtube, "YoutubeDL", lambda opts: DummyYDL())

    result = youtube.get_youtube_video("https://example.com/video")

    assert result is None
