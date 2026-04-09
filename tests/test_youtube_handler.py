from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import youtube
from services.inline.video_requests import create_inline_video_request, get_inline_video_request
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


def test_get_youtube_thumbnail_url_falls_back_to_hqdefault():
    yt = {"id": "abc123"}
    assert youtube._get_youtube_thumbnail_url(yt) == "https://i.ytimg.com/vi/abc123/hqdefault.jpg"


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


@pytest.mark.asyncio
async def test_chosen_inline_youtube_result_supports_regular_video(monkeypatch, tmp_path):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    token = create_inline_video_request(
        "youtube",
        "https://www.youtube.com/watch?v=abc123",
        42,
        settings,
    )
    result = SimpleNamespace(
        result_id=f"youtube_inline:{token}",
        inline_message_id="inline-message-1",
        from_user=SimpleNamespace(full_name="Inline User"),
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    metrics = DownloadMetrics(
        url="https://cdn.example.com/video.mp4",
        path=str(video_path),
        size=video_path.stat().st_size,
        elapsed=0.1,
        used_multipart=False,
        resumed=False,
    )

    monkeypatch.setattr(
        youtube,
        "get_youtube_video",
        lambda url: {
            "id": "abc123",
            "title": "Regular Video",
            "webpage_url": url,
            "view_count": 10,
            "like_count": 2,
        },
    )
    monkeypatch.setattr(
        youtube,
        "get_video_stream",
        lambda yt: {"url": "https://cdn.example.com/video.mp4", "filesize": 1024},
    )
    monkeypatch.setattr(youtube, "download_stream", AsyncMock(return_value=metrics))
    monkeypatch.setattr(youtube.db, "get_file_id", AsyncMock(return_value=None))
    monkeypatch.setattr(youtube.db, "add_file", AsyncMock())
    monkeypatch.setattr(youtube, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(youtube, "safe_edit_inline_text", AsyncMock(return_value=True))
    monkeypatch.setattr(youtube, "safe_edit_inline_media", AsyncMock(return_value=True))
    monkeypatch.setattr(youtube, "remove_file", AsyncMock())
    monkeypatch.setattr(
        youtube.bot,
        "send_video",
        AsyncMock(return_value=SimpleNamespace(video=SimpleNamespace(file_id="cached-file-id"))),
    )

    await youtube.chosen_inline_youtube_result(result)

    assert youtube.safe_edit_inline_text.await_count == 2
    assert youtube.safe_edit_inline_text.await_args_list[0].args[2] == youtube.bm.downloading_video_status()
    assert youtube.safe_edit_inline_text.await_args_list[1].args[2] == youtube.bm.uploading_status()
    media = youtube.safe_edit_inline_media.await_args.args[2]
    assert media.media == "cached-file-id"
    request = get_inline_video_request(token)
    assert request is not None
    assert request.state == "completed"


@pytest.mark.asyncio
async def test_download_music_uses_cached_audio_file_id(monkeypatch):
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=7, username="tester", full_name="Tester"),
        business_connection_id=None,
        chat=SimpleNamespace(id=99, type="private"),
        answer=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
        reply_audio=AsyncMock(),
        reply=AsyncMock(),
    )
    yt = {
        "id": "abc123",
        "title": "Cached Audio",
        "webpage_url": "https://youtube.com/watch?v=abc123",
    }

    monkeypatch.setattr(youtube, "get_message_text", lambda _message: "https://music.youtube.com/watch?v=abc123")
    monkeypatch.setattr(youtube, "react_to_message", AsyncMock())
    monkeypatch.setattr(youtube.db, "user_settings", AsyncMock(return_value={"delete_message": "off"}))
    monkeypatch.setattr(youtube, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(youtube, "get_bot_avatar_thumbnail", AsyncMock(return_value="thumb-id"))
    monkeypatch.setattr(youtube, "get_youtube_video", lambda _url: yt)
    monkeypatch.setattr(youtube, "get_audio_stream", lambda _yt: {"ext": "m4a"})
    monkeypatch.setattr(youtube.db, "get_file_id", AsyncMock(return_value="cached-audio-id"))
    monkeypatch.setattr(youtube, "send_chat_action_if_needed", AsyncMock())
    monkeypatch.setattr(youtube, "safe_edit_text", AsyncMock(return_value=True))
    monkeypatch.setattr(youtube, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(youtube, "download_stream", AsyncMock())
    monkeypatch.setattr(youtube, "update_info", AsyncMock())

    await youtube.download_music(message)

    assert youtube.download_stream.await_count == 0
    assert message.reply_audio.await_args.kwargs["audio"] == "cached-audio-id"


@pytest.mark.asyncio
async def test_download_music_handles_missing_youtube_metadata(monkeypatch):
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=7, username="tester", full_name="Tester"),
        business_connection_id=None,
        chat=SimpleNamespace(id=99, type="private"),
        answer=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
        reply_audio=AsyncMock(),
        reply=AsyncMock(),
    )

    monkeypatch.setattr(youtube, "get_message_text", lambda _message: "https://music.youtube.com/watch?v=abc123")
    monkeypatch.setattr(youtube, "react_to_message", AsyncMock())
    monkeypatch.setattr(youtube.db, "user_settings", AsyncMock(return_value={"delete_message": "off"}))
    monkeypatch.setattr(youtube, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(youtube, "get_bot_avatar_thumbnail", AsyncMock(return_value=None))
    monkeypatch.setattr(youtube, "get_youtube_video", lambda _url: None)
    monkeypatch.setattr(youtube, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(youtube, "update_info", AsyncMock())

    await youtube.download_music(message)

    message.reply.assert_awaited_once_with(youtube.bm.nothing_found())
