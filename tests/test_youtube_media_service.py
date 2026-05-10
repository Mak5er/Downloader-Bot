from pathlib import Path

import pytest

from services.platforms.youtube_media import (
    YTDLP_FORMAT_720,
    YouTubeMediaService,
    build_ytdlp_youtube_options,
)


class _FakeYoutubeDL:
    def __init__(self, options, *, ext: str = "mp4", payload: bytes = b"youtube-media"):
        self.options = options
        self.ext = ext
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def download(self, urls):
        del urls
        outtmpl = self.options["outtmpl"]
        if "%(ext)s" in outtmpl:
            output_path = outtmpl.replace("%(ext)s", self.ext)
        else:
            stem, _ext = str(Path(outtmpl)).rsplit(".", 1)
            output_path = f"{stem}.{self.ext}"
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)

    def extract_info(self, url, download):
        return {
            "id": "abc123",
            "title": "Test Video",
            "webpage_url": url,
            "formats": [],
            "_download_arg": download,
        }


def _make_service(tmp_path: Path, *, youtube_dl_factory=None) -> YouTubeMediaService:
    async def passthrough_retry(operation, **_kwargs):
        return await operation()

    return YouTubeMediaService(
        str(tmp_path),
        retry_async_operation_func=passthrough_retry,
        youtube_dl_factory=youtube_dl_factory or (lambda options: _FakeYoutubeDL(options)),
    )


@pytest.mark.asyncio
async def test_download_with_ytdlp_resolves_postprocessed_extension(tmp_path):
    service = _make_service(
        tmp_path,
        youtube_dl_factory=lambda options: _FakeYoutubeDL(options, ext="mkv", payload=b"video-bytes"),
    )

    resolved_path = await service.download_with_ytdlp(
        "https://youtube.com/watch?v=abc123",
        "video.mp4",
    )

    assert resolved_path == str(tmp_path / "video.mkv")
    assert Path(resolved_path).read_bytes() == b"video-bytes"


@pytest.mark.asyncio
async def test_download_with_ytdlp_metrics_resolves_postprocessed_extension(tmp_path):
    service = _make_service(
        tmp_path,
        youtube_dl_factory=lambda options: _FakeYoutubeDL(options, ext="mkv", payload=b"video-bytes"),
    )

    metrics = await service.download_with_ytdlp_metrics(
        "https://youtube.com/watch?v=abc123",
        "video.mp4",
        "bestvideo+bestaudio/best",
        "youtube_video",
    )

    assert metrics is not None
    assert metrics.path == str(tmp_path / "video.mkv")
    assert metrics.size == len(b"video-bytes")


def test_ytdlp_format_720_prefers_progressive_and_falls_back_to_merge():
    assert "acodec!=none" in YTDLP_FORMAT_720
    assert "bestvideo[height<=720]" in YTDLP_FORMAT_720
    assert "bestaudio" in YTDLP_FORMAT_720


def test_get_youtube_video_uses_single_video_metadata_options(tmp_path):
    captured = {}

    class CapturingYoutubeDL(_FakeYoutubeDL):
        def extract_info(self, url, download):
            captured["options"] = self.options
            captured["download"] = download
            return super().extract_info(url, download)

    service = _make_service(tmp_path, youtube_dl_factory=lambda options: CapturingYoutubeDL(options))

    info = service.get_youtube_video("https://music.youtube.com/watch?v=abc123&list=RDAMVMdemo")

    assert info is not None
    assert captured["download"] is False
    assert captured["options"]["noplaylist"] is True
    assert captured["options"]["skip_download"] is True
    assert captured["options"]["ignore_no_formats_error"] is True


def test_build_ytdlp_youtube_options_includes_optional_access_env(monkeypatch):
    monkeypatch.setenv("YTDLP_YOUTUBE_COOKIES_FILE", "cookies.txt")
    monkeypatch.setenv("YTDLP_YOUTUBE_COOKIES_FROM_BROWSER", "firefox:Profile 1")
    monkeypatch.setenv("YTDLP_YOUTUBE_PLAYER_CLIENT", "web,android")
    monkeypatch.setenv("YTDLP_YOUTUBE_PO_TOKEN", "web.gvs+token")
    monkeypatch.setenv("YTDLP_YOUTUBE_SLEEP_REQUESTS_SECONDS", "0.25")

    options = build_ytdlp_youtube_options(skip_download=True)

    assert options["cookiefile"] == "cookies.txt"
    assert options["cookiesfrombrowser"] == ("firefox", "Profile 1", None, None)
    assert options["extractor_args"]["youtube"]["player_client"] == ["web", "android"]
    assert options["extractor_args"]["youtube"]["po_token"] == ["web.gvs+token"]
    assert options["sleep_interval_requests"] == 0.25
    assert options["skip_download"] is True
