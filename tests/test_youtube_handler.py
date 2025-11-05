import builtins
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from handlers import youtube


class DummyVideoClip:
    def __init__(self, size):
        self.size = size

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class DummyAudioClip:
    def __init__(self, duration):
        self.duration = duration

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture(autouse=True)
def stub_sleep(monkeypatch):
    # Speed up retry loops inside handlers
    monkeypatch.setattr(youtube.time, "sleep", lambda *_: None, raising=False)


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


def test_get_video_stream_fallbacks_video_only():
    yt = {
        "webpage_url": "https://youtube.com/watch?v=xyz",
        "formats": [
            {"height": "1080", "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
            {"height": "480", "vcodec": "avc1", "acodec": "none", "ext": "webm"},
        ],
    }

    stream = youtube.get_video_stream(yt)

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


@pytest.mark.asyncio
async def test_get_clip_dimensions_success(monkeypatch):
    monkeypatch.setattr(youtube, "VideoFileClip", lambda _: DummyVideoClip((1920, 1080)))

    width, height = await youtube.get_clip_dimensions("dummy.mp4")

    assert width == 1920
    assert height == 1080


@pytest.mark.asyncio
async def test_get_clip_dimensions_handles_error(monkeypatch):
    def broken_clip(_):
        raise OSError("file missing")

    monkeypatch.setattr(youtube, "VideoFileClip", broken_clip)

    width, height = await youtube.get_clip_dimensions("missing.mp4")

    assert width is None and height is None


@pytest.mark.asyncio
async def test_get_audio_duration_success(monkeypatch):
    monkeypatch.setattr(youtube, "AudioFileClip", lambda _: DummyAudioClip(12.5))
    duration = await youtube.get_audio_duration("audio.mp4")
    assert duration == 12.5


@pytest.mark.asyncio
async def test_get_audio_duration_handles_error(monkeypatch):
    monkeypatch.setattr(youtube, "AudioFileClip", lambda _: (_ for _ in ()).throw(OSError("corrupt")))
    duration = await youtube.get_audio_duration("broken.mp4")
    assert duration == 0.0


@pytest.mark.asyncio
async def test_download_media_retries_formats(monkeypatch, tmp_path):
    attempts = []

    class DummyYDL:
        def __init__(self, opts):
            self.opts = opts

        def download(self, urls):
            attempts.append(self.opts["format"])
            if self.opts["format"] == "best":
                raise youtube.DownloadError("format failed")
            return 0

    monkeypatch.setattr(youtube, "YoutubeDL", lambda opts: DummyYDL(opts))

    result = await youtube.download_media(
        url="https://example.com/video",
        filename="video.mp4",
        format_candidates=["best", "bestvideo+bestaudio"],
    )

    assert result is True
    assert attempts == ["best", "bestvideo+bestaudio"]


@pytest.mark.asyncio
async def test_download_media_returns_false_when_all_formats_fail(monkeypatch, tmp_path):
    attempts = []

    class DummyYDL:
        def __init__(self, opts):
            self.opts = opts

        def download(self, urls):
            attempts.append(self.opts["format"])
            raise RuntimeError("boom")

    monkeypatch.setattr(youtube, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(youtube, "YoutubeDL", lambda opts: DummyYDL(opts))

    success = await youtube.download_media(
        url="https://example.com/video",
        filename="video.mp4",
        format_candidates=["best", "worst"],
    )

    assert success is False
    assert attempts == ["best", "worst"]


def test_get_audio_stream_returns_none_when_missing():
    yt = {
        "webpage_url": "https://youtube.com/watch?v=abc",
        "formats": [
            {"abr": None, "ext": "mp3", "vcodec": "avc1"},
        ],
    }

    assert youtube.get_audio_stream(yt) is None


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


def test_custom_oauth_verifier_sends_message(monkeypatch):
    sleep_calls = []
    request_log = {}
    info_logs = []
    error_logs = []

    class DummyResponse:
        status_code = 200

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    def fake_get(url, params):
        request_log["url"] = url
        request_log["params"] = params
        return DummyResponse()

    monkeypatch.setattr(youtube, "BOT_TOKEN", "token123")
    monkeypatch.setattr(youtube, "admin_id", 456)
    monkeypatch.setattr(youtube.time, "sleep", fake_sleep, raising=False)
    monkeypatch.setattr(youtube.requests, "get", fake_get)
    monkeypatch.setattr(youtube.logging, "info", lambda msg: info_logs.append(msg))
    monkeypatch.setattr(youtube.logging, "error", lambda msg: error_logs.append(msg))

    youtube.custom_oauth_verifier("https://verify", "CODE1")

    assert request_log["url"] == "https://api.telegram.org/bottoken123/sendMessage"
    assert request_log["params"] == {
        "chat_id": 456,
        "text": "<b>OAuth Verification</b>\n\nOpen this URL in your browser:\nhttps://verify\n\nEnter this code:\n<code>CODE1</code>",
        "parse_mode": "HTML",
    }
    assert sleep_calls == [5, 5, 5, 5, 5, 5]
    assert error_logs == []
    assert any("seconds remaining" in msg for msg in info_logs)


def test_custom_oauth_verifier_logs_error_on_failure(monkeypatch):
    sleep_calls = []
    errors = []

    class DummyResponse:
        status_code = 500

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(youtube, "BOT_TOKEN", "token321")
    monkeypatch.setattr(youtube, "admin_id", 789)
    monkeypatch.setattr(youtube.time, "sleep", fake_sleep, raising=False)
    monkeypatch.setattr(youtube.requests, "get", lambda url, params: DummyResponse())
    monkeypatch.setattr(youtube.logging, "info", lambda *_: None)
    monkeypatch.setattr(youtube.logging, "error", lambda msg: errors.append(msg))

    youtube.custom_oauth_verifier("https://verify", "CODE2")

    assert sleep_calls == [5, 5, 5, 5, 5, 5]
    assert errors == ["OAuth message failed. Status code: 500"]
