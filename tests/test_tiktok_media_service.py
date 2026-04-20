from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.platforms.tiktok_media import TikTokMediaService
from utils.download_manager import DownloadMetrics


class _FakeYoutubeDL:
    def __init__(self, options, *, ext: str = "mp4", payload: bytes = b"test-media"):
        self.options = options
        self.ext = ext
        self.payload = payload
        self.urls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def download(self, urls):
        self.urls = list(urls)
        outtmpl = self.options["outtmpl"]
        if "%(ext)s" in outtmpl:
            output_path = outtmpl.replace("%(ext)s", self.ext)
        else:
            output_path = outtmpl
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)
        for hook in self.options.get("progress_hooks", []):
            hook({
                "status": "downloading",
                "downloaded_bytes": len(self.payload),
                "total_bytes": len(self.payload),
                "speed": 1024.0,
                "eta": 0,
            })
            hook({
                "status": "finished",
                "downloaded_bytes": len(self.payload),
                "total_bytes": len(self.payload),
                "speed": 1024.0,
                "eta": 0,
            })


def _make_service(tmp_path: Path, *, youtube_dl_factory=None) -> TikTokMediaService:
    async def passthrough_retry(operation, **_kwargs):
        return await operation()

    return TikTokMediaService(
        str(tmp_path),
        get_http_session_func=AsyncMock(),
        retry_async_operation_func=passthrough_retry,
        user_agent_factory=lambda: SimpleNamespace(random="Agent"),
        youtube_dl_factory=youtube_dl_factory or (lambda options: _FakeYoutubeDL(options)),
    )


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
    assert submit_mock.await_count == 1
    assert submit_mock.await_args.kwargs["source"] == "tiktok"
    assert submit_mock.await_args.kwargs["size_hint"] == 2048
    assert submit_mock.await_args.kwargs["user_id"] == 77
    assert submit_mock.await_args.kwargs["request_id"] == "req-1"
    sync_download = submit_mock.await_args.kwargs["sync_download"]
    sync_metrics = sync_download(None)
    assert sync_metrics.path == str(tmp_path / "video.mp4")


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


def test_download_video_with_ytdlp_sync_writes_output(tmp_path):
    service = _make_service(
        tmp_path,
        youtube_dl_factory=lambda options: _FakeYoutubeDL(options, ext="mp4", payload=b"video-bytes"),
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
        return _FakeYoutubeDL(options, ext="mp4", payload=b"video-bytes")

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
        youtube_dl_factory=lambda options: _FakeYoutubeDL(options, ext="mp3", payload=b"audio-bytes"),
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
