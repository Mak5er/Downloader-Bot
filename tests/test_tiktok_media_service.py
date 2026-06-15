from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.platforms.tiktok_media import TikTokMediaService
from tests.conftest import FakeYoutubeDL
from utils.download_manager import DownloadError, DownloadMetrics


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def json(self, content_type=None):
        return self.payload


class _FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.get_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _FakeResponse(self.payload)


def _make_service(tmp_path: Path, *, youtube_dl_factory=None) -> TikTokMediaService:
    async def passthrough_retry(operation, **_kwargs):
        return await operation()

    return TikTokMediaService(
        str(tmp_path),
        get_http_session_func=AsyncMock(),
        retry_async_operation_func=passthrough_retry,
        user_agent_factory=lambda: SimpleNamespace(random="Agent"),
        youtube_dl_factory=youtube_dl_factory or (lambda options: FakeYoutubeDL(options)),
    )


def test_tiktok_direct_downloader_fails_fast_between_service_fallbacks(tmp_path):
    service = _make_service(tmp_path)

    assert service._downloader.config.probe_max_retries == 0
    assert service._downloader.config.max_retries == 0


def test_tiktok_ytdlp_download_options_do_not_retry_inside_service_attempt(tmp_path):
    service = _make_service(tmp_path)

    options = service._build_ytdlp_download_options()

    assert options["retries"] == 0
    assert options["fragment_retries"] == 0


@pytest.mark.asyncio
async def test_fetch_tiktok_data_builds_legacy_video_payload(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    detail = {
        "id": "123",
        "desc": "Funny video",
        "author": {"uniqueId": "creator"},
        "stats": {
            "playCount": 1000,
            "diggCount": 100,
            "commentCount": 25,
            "shareCount": 10,
        },
        "music": {"playUrl": "https://cdn.example.com/audio.mp3?mime_type=audio_mpeg"},
        "video": {
            "playAddr": "https://cdn.example.com/video.mp4",
            "cover": "https://cdn.example.com/cover.jpg",
            "size": "2048",
        },
    }

    async def fake_extract(_video_url: str):
        return detail, 0

    monkeypatch.setattr(service, "_fetch_tikwm_data", AsyncMock(return_value={"error": "bad", "code": 1, "data": {}}))
    monkeypatch.setattr(service, "_extract_tiktok_detail", fake_extract)

    payload = await service.fetch_tiktok_data("https://www.tiktok.com/@creator/video/123")

    assert payload["error"] is None
    assert payload["code"] == 0
    assert payload["message"] == "success"
    assert payload["data"] == {
        "id": "123",
        "title": "Funny video",
        "cover": "https://cdn.example.com/cover.jpg",
        "play_count": 1000,
        "digg_count": 100,
        "comment_count": 25,
        "share_count": 10,
        "music_info": {"play": "https://cdn.example.com/audio.mp3?mime_type=audio_mpeg"},
        "author": {"unique_id": "creator"},
        "images": [],
        "play": "https://cdn.example.com/video.mp4",
        "download_headers": {
            "User-Agent": "Agent",
            "Referer": "https://www.tiktok.com/@creator/video/123",
        },
        "audio_headers": {
            "User-Agent": "Agent",
            "Referer": "https://www.tiktok.com/@creator/video/123",
        },
        "webpage_url": "https://www.tiktok.com/@creator/video/123",
        "size_hd": 2048,
        "size": 2048,
        "wm_size": 2048,
    }


@pytest.mark.asyncio
async def test_fetch_tiktok_data_extracts_image_posts(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    detail = {
        "id": "321",
        "desc": "Photo set",
        "author": {"uniqueId": "creator"},
        "stats": {
            "playCount": 55,
            "diggCount": 11,
            "commentCount": 3,
            "shareCount": 1,
        },
        "music": {"playUrl": "https://cdn.example.com/slideshow.m4a?mime_type=audio_mp4"},
        "imagePost": {
            "cover": {
                "imageURL": {
                    "urlList": ["https://cdn.example.com/cover-photo.jpg"],
                }
            },
            "images": [
                {"imageURL": {"urlList": ["https://cdn.example.com/1.jpg"]}},
                {"imageURL": {"urlList": ["https://cdn.example.com/2.jpg"]}},
            ],
        },
        "video": {"size": "0"},
    }

    async def fake_extract(_video_url: str):
        return detail, 0

    monkeypatch.setattr(service, "_fetch_tikwm_data", AsyncMock(return_value={"error": "bad", "code": 1, "data": {}}))
    monkeypatch.setattr(service, "_extract_tiktok_detail", fake_extract)

    payload = await service.fetch_tiktok_data("https://www.tiktok.com/@creator/video/321")

    assert payload["data"]["images"] == [
        "https://cdn.example.com/1.jpg",
        "https://cdn.example.com/2.jpg",
    ]
    assert payload["data"]["cover"] == "https://cdn.example.com/cover-photo.jpg"
    assert payload["data"]["play"] == ""
    assert payload["data"]["music_info"]["play"].endswith("mime_type=audio_mp4")


@pytest.mark.asyncio
async def test_fetch_tiktok_data_uses_tikwm_before_ytdlp(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    tikwm_payload = {
        "code": 0,
        "msg": "success",
        "data": {
            "id": "7619361128035929376",
            "title": "Photo set",
            "cover": "/video/cover/7619361128035929376.webp",
            "play": "/video/media/play/7619361128035929376.mp4",
            "hdplay": "/video/media/hdplay/7619361128035929376.mp4",
            "music": "/video/music/7619361128035929376.mp3",
            "music_info": {"play": "/video/music/7619361128035929376.mp3"},
            "author": {"unique_id": "creator", "avatar": "/video/avatar/7619361128035929376.jpeg"},
            "images": ["/photo/1.jpeg", "https://cdn.example.com/photo-2.jpeg"],
        },
    }
    fake_session = _FakeSession(tikwm_payload)

    fake_extract = AsyncMock(return_value=({}, 0))

    monkeypatch.setattr(service, "_extract_tiktok_detail", fake_extract)
    service._get_http_session = AsyncMock(return_value=fake_session)

    payload = await service.fetch_tiktok_data("https://www.tiktok.com/@creator/photo/7619361128035929376")

    assert payload["error"] is None
    assert payload["message"] == "success"
    assert payload["data"]["cover"] == "https://tikwm.com/video/cover/7619361128035929376.webp"
    assert payload["data"]["play"] == "https://tikwm.com/video/media/play/7619361128035929376.mp4"
    assert payload["data"]["hdplay"] == "https://tikwm.com/video/media/hdplay/7619361128035929376.mp4"
    assert payload["data"]["music"] == "https://tikwm.com/video/music/7619361128035929376.mp3"
    assert payload["data"]["music_info"]["play"] == "https://tikwm.com/video/music/7619361128035929376.mp3"
    assert payload["data"]["author"]["avatar"] == "https://tikwm.com/video/avatar/7619361128035929376.jpeg"
    assert payload["data"]["images"] == [
        "https://tikwm.com/photo/1.jpeg",
        "https://cdn.example.com/photo-2.jpeg",
    ]
    assert fake_session.get_calls[0][0] == "https://tikwm.com/api/"
    assert fake_extract.await_count == 0


@pytest.mark.asyncio
async def test_fetch_tiktok_data_falls_back_to_ytdlp_when_tikwm_invalid(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    detail = {
        "id": "123",
        "desc": "Restricted video",
        "author": {"uniqueId": "creator"},
        "stats": {"playCount": 10},
        "video": {"cover": "https://cdn.example.com/cover.jpg", "size": "0"},
    }
    async def fake_extract(_video_url: str):
        return detail, 0

    monkeypatch.setattr(service, "_fetch_tikwm_data", AsyncMock(return_value={"error": "bad", "code": 1, "data": {}}))
    monkeypatch.setattr(service, "_extract_tiktok_detail", fake_extract)

    payload = await service.fetch_tiktok_data("https://www.tiktok.com/@creator/video/123")

    assert payload["data"]["id"] == "123"
    assert payload["data"]["cover"] == "https://cdn.example.com/cover.jpg"
    assert payload["data"]["play"] == ""


@pytest.mark.asyncio
async def test_download_video_submits_ytdlp_job(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    expected_metrics = DownloadMetrics(
        url="https://www.tiktok.com/@creator/video/123",
        path=str(tmp_path / "downloaded.mp4"),
        size=2048,
        elapsed=1.5,
        used_multipart=False,
        resumed=False,
    )
    submit_mock = AsyncMock(return_value=expected_metrics)
    direct_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(service, "_download_direct", direct_mock)
    monkeypatch.setattr(service, "_submit_queued_ytdlp_download", submit_mock)

    payload = {
        "data": {
            "play": "https://cdn.example.com/video.mp4",
            "download_headers": {"Referer": "https://www.tiktok.com/@creator/video/123"},
            "webpage_url": "https://www.tiktok.com/@creator/video/123",
            "size_hd": 2048,
        }
    }

    metrics = await service.download_video(
        "https://www.tiktok.com/@creator/video/123",
        "video.mp4",
        download_data=payload,
        user_id=77,
        request_id="req-1",
    )

    assert metrics == expected_metrics
    assert direct_mock.await_count == 1
    assert submit_mock.await_count == 1
    assert submit_mock.await_args.kwargs["source"] == "tiktok"
    assert submit_mock.await_args.kwargs["size_hint"] == 2048
    assert submit_mock.await_args.kwargs["user_id"] == 77
    assert submit_mock.await_args.kwargs["request_id"] == "req-1"
    sync_download = submit_mock.await_args.kwargs["sync_download"]
    sync_metrics = sync_download(None)
    assert sync_metrics.path == str(tmp_path / "video.mp4")


@pytest.mark.asyncio
async def test_download_video_uses_direct_before_ytdlp(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    expected_metrics = DownloadMetrics(
        url="https://tikwm.com/video/media/play/123.mp4",
        path=str(tmp_path / "video.mp4"),
        size=2048,
        elapsed=1.5,
        used_multipart=False,
        resumed=False,
    )
    direct_download = AsyncMock(return_value=expected_metrics)
    ytdlp_submit = AsyncMock(side_effect=AssertionError("yt-dlp should not run when direct download succeeds"))
    cobalt_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(service, "_download_via_cobalt", cobalt_mock)
    monkeypatch.setattr(service._downloader, "download", direct_download)
    monkeypatch.setattr(service, "_submit_queued_ytdlp_download", ytdlp_submit)

    payload = {
        "data": {
            "id": "123",
            "hdplay": "https://tikwm.com/video/media/hdplay/123.mp4",
            "play": "https://tikwm.com/video/media/play/123.mp4",
            "download_headers": {"Referer": "https://www.tiktok.com/@creator/video/123"},
            "webpage_url": "https://www.tiktok.com/@creator/video/123",
            "size_hd": 2048,
        }
    }

    metrics = await service.download_video(
        "https://www.tiktok.com/@creator/video/123",
        "video.mp4",
        download_data=payload,
        user_id=77,
        chat_id=88,
        request_id="req-1",
    )

    assert metrics == expected_metrics
    assert cobalt_mock.await_count == 1
    assert direct_download.await_count == 1
    assert ytdlp_submit.await_count == 0
    assert direct_download.await_args.args[:2] == ("https://tikwm.com/video/media/play/123.mp4", "video.mp4")
    assert direct_download.await_args.kwargs["user_id"] == 77
    assert direct_download.await_args.kwargs["chat_id"] == 88
    assert direct_download.await_args.kwargs["request_id"] == "req-1"
    assert direct_download.await_args.kwargs["size_hint"] == 2048
    assert direct_download.await_args.kwargs["headers"]["Referer"] == "https://www.tiktok.com/@creator/video/123"


@pytest.mark.asyncio
async def test_download_audio_submits_ytdlp_job(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    expected_metrics = DownloadMetrics(
        url="https://www.tiktok.com/@creator/video/123",
        path=str(tmp_path / "final.mp3"),
        size=1024,
        elapsed=1.0,
        used_multipart=False,
        resumed=False,
    )
    submit_mock = AsyncMock(return_value=expected_metrics)
    monkeypatch.setattr(service, "_submit_queued_ytdlp_download", submit_mock)

    payload = {
        "data": {
            "music_info": {
                "play": "https://cdn.example.com/audio.m4a?mime_type=audio_mp4",
            },
            "audio_headers": {"Referer": "https://www.tiktok.com/@creator/video/123"},
            "webpage_url": "https://www.tiktok.com/@creator/video/123",
        }
    }

    metrics = await service.download_audio(
        "https://www.tiktok.com/@creator/video/123",
        "final.mp3",
        download_data=payload,
    )

    assert metrics == expected_metrics
    assert submit_mock.await_count == 1
    assert submit_mock.await_args.kwargs["source"] == "tiktok"
    assert submit_mock.await_args.kwargs["size_hint"] == 0


@pytest.mark.asyncio
async def test_download_audio_uses_tikwm_music_before_ytdlp(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    expected_metrics = DownloadMetrics(
        url="https://tikwm.com/video/music/123.mp3",
        path=str(tmp_path / "final.mp3"),
        size=1024,
        elapsed=1.0,
        used_multipart=False,
        resumed=False,
    )
    direct_download = AsyncMock(return_value=expected_metrics)
    ytdlp_submit = AsyncMock(side_effect=AssertionError("yt-dlp should not run when direct download succeeds"))
    monkeypatch.setattr(service._downloader, "download", direct_download)
    monkeypatch.setattr(service, "_submit_queued_ytdlp_download", ytdlp_submit)

    payload = {
        "data": {
            "music": "https://tikwm.com/video/music/123.mp3",
            "music_info": {"play": "https://cdn.example.com/audio.m4a"},
            "audio_headers": {"Referer": "https://www.tiktok.com/@creator/video/123"},
            "webpage_url": "https://www.tiktok.com/@creator/video/123",
        }
    }

    metrics = await service.download_audio(
        "https://www.tiktok.com/@creator/video/123",
        "final.mp3",
        download_data=payload,
    )

    assert metrics == expected_metrics
    assert direct_download.await_args.args[:2] == ("https://tikwm.com/video/music/123.mp3", "final.mp3")
    assert ytdlp_submit.await_count == 0


@pytest.mark.asyncio
async def test_download_audio_alternates_direct_and_ytdlp_across_retry_cycles(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    call_order = []
    retry_calls = []

    async def fail_direct(**_kwargs):
        call_order.append("direct")
        raise DownloadError("tikwm 403")

    async def fail_ytdlp(**_kwargs):
        call_order.append("ytdlp")
        raise DownloadError("yt-dlp unavailable")

    async def on_retry(failed_attempt, total_attempts, error):
        retry_calls.append((failed_attempt, total_attempts, str(error)))

    monkeypatch.setattr(service, "_download_direct", fail_direct)
    monkeypatch.setattr(service, "_submit_queued_ytdlp_download", fail_ytdlp)
    monkeypatch.setattr(service, "DOWNLOAD_CYCLE_DELAY_SECONDS", 0.0)

    payload = {
        "data": {
            "music": "https://tikwm.com/video/music/123.mp3",
            "webpage_url": "https://www.tiktok.com/@creator/video/123",
        }
    }

    metrics = await service.download_audio(
        "https://www.tiktok.com/@creator/video/123",
        "final.mp3",
        download_data=payload,
        on_retry=on_retry,
    )

    assert metrics is None
    assert call_order == ["direct", "ytdlp", "direct", "ytdlp", "direct", "ytdlp"]
    assert retry_calls == [
        (1, 3, "yt-dlp unavailable"),
        (2, 3, "yt-dlp unavailable"),
    ]


def test_download_video_with_ytdlp_sync_writes_output(tmp_path):
    service = _make_service(
        tmp_path,
        youtube_dl_factory=lambda options: FakeYoutubeDL(options, ext="mp4", payload=b"video-bytes"),
    )

    metrics = service._download_video_with_ytdlp_sync(
        source_url="https://www.tiktok.com/@creator/video/123",
        output_path=str(tmp_path / "video.mp4"),
        progress_callback=None,
    )

    assert metrics.url == "https://www.tiktok.com/@creator/video/123"
    assert metrics.path == str(tmp_path / "video.mp4")
    assert metrics.size == len(b"video-bytes")
    assert Path(metrics.path).read_bytes() == b"video-bytes"


def test_download_video_with_ytdlp_sync_prefers_progressive_formats_with_audio(tmp_path):
    captured = {}

    def youtube_dl_factory(options):
        captured["options"] = options
        return FakeYoutubeDL(options, ext="mp4", payload=b"video-bytes")

    service = _make_service(tmp_path, youtube_dl_factory=youtube_dl_factory)

    service._download_video_with_ytdlp_sync(
        source_url="https://www.tiktok.com/@creator/video/123",
        output_path=str(tmp_path / "video.mp4"),
        progress_callback=None,
    )

    format_selector = captured["options"]["format"]
    assert "acodec!=none" in format_selector
    assert "vcodec!=none" in format_selector
    assert "bestvideo+bestaudio" in format_selector
    assert captured["options"]["merge_output_format"] == "mp4"


def test_download_audio_with_ytdlp_sync_writes_mp3_output(tmp_path):
    service = _make_service(
        tmp_path,
        youtube_dl_factory=lambda options: FakeYoutubeDL(options, ext="mp3", payload=b"audio-bytes"),
    )

    metrics = service._download_audio_with_ytdlp_sync(
        source_url="https://www.tiktok.com/@creator/video/123",
        output_path=str(tmp_path / "audio.mp3"),
        progress_callback=None,
    )

    assert metrics.url == "https://www.tiktok.com/@creator/video/123"
    assert metrics.path == str(tmp_path / "audio.mp3")
    assert metrics.size == len(b"audio-bytes")
    assert Path(metrics.path).read_bytes() == b"audio-bytes"
