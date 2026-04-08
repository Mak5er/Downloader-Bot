import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from utils import download_manager
from utils.download_manager import DownloadConfig, DownloadMetrics, ResilientDownloader


@pytest.mark.asyncio
async def test_download_deduplicates_same_target(monkeypatch, tmp_path):
    ResilientDownloader._inflight_downloads.clear()
    downloader = ResilientDownloader(str(tmp_path))
    call_count = {"value": 0}

    async def fake_submit(runner, **_kwargs):
        return await runner()

    def fake_queue():
        return SimpleNamespace(submit=fake_submit)

    def fake_download_sync(url, filename, _headers, _skip_if_exists, _progress, _max_size_bytes):
        call_count["value"] += 1
        time.sleep(0.05)
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

    monkeypatch.setattr(download_manager, "get_download_queue", fake_queue)
    monkeypatch.setattr(downloader, "_download_sync", fake_download_sync)

    first, second = await asyncio.gather(
        downloader.download("https://cdn.example.com/video.mp4", "video.mp4"),
        downloader.download("https://cdn.example.com/video.mp4", "video.mp4"),
    )

    assert call_count["value"] == 1
    assert first.path == second.path


@pytest.mark.asyncio
async def test_download_does_not_deduplicate_different_urls_with_same_filename(monkeypatch, tmp_path):
    ResilientDownloader._inflight_downloads.clear()
    downloader = ResilientDownloader(str(tmp_path))
    call_count = {"value": 0}

    async def fake_submit(runner, **_kwargs):
        return await runner()

    def fake_queue():
        return SimpleNamespace(submit=fake_submit)

    def fake_download_sync(url, filename, _headers, _skip_if_exists, _progress, _max_size_bytes):
        call_count["value"] += 1
        path = tmp_path / filename
        path.write_text(url, encoding="utf-8")
        return DownloadMetrics(
            url=url,
            path=str(path),
            size=path.stat().st_size,
            elapsed=0.01,
            used_multipart=False,
            resumed=False,
        )

    monkeypatch.setattr(download_manager, "get_download_queue", fake_queue)
    monkeypatch.setattr(downloader, "_download_sync", fake_download_sync)

    await asyncio.gather(
        downloader.download("https://cdn.example.com/video-a.mp4", "video.mp4"),
        downloader.download("https://cdn.example.com/video-b.mp4", "video.mp4"),
    )

    assert call_count["value"] == 2


def test_probe_uses_probe_retry_limit(monkeypatch, tmp_path):
    downloader = ResilientDownloader(str(tmp_path), config=DownloadConfig(probe_max_retries=1, retry_backoff=0.0))
    attempts = {"count": 0}

    class FailingSession:
        def head(self, *args, **kwargs):
            attempts["count"] += 1
            raise RuntimeError("boom")

    monkeypatch.setattr(downloader, "_get_session", lambda: FailingSession())

    total_size, supports_range = downloader._probe("https://cdn.example.com/video.mp4", {})

    assert (total_size, supports_range) == (0, False)
    assert attempts["count"] == 2


def test_download_single_rejects_resume_when_origin_ignores_range(monkeypatch, tmp_path):
    downloader = ResilientDownloader(str(tmp_path))

    class DummyResponse:
        status_code = 200
        headers = {"Content-Length": "5"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            yield b"fresh"

    class DummySession:
        def get(self, *args, **kwargs):
            return DummyResponse()

    monkeypatch.setattr(downloader, "_get_session", lambda: DummySession())

    with pytest.raises(download_manager._ResumeNotSupportedError):
        downloader._download_single(
            "https://cdn.example.com/video.mp4",
            str(tmp_path / "video.part"),
            {"Range": "bytes=10-"},
        )


def test_download_sync_preserves_too_large_error_and_keeps_existing_target(monkeypatch, tmp_path):
    downloader = ResilientDownloader(str(tmp_path))
    target = tmp_path / "video.mp4"
    target.write_bytes(b"existing")

    monkeypatch.setattr(downloader, "_probe", lambda url, headers: (10, False))

    with pytest.raises(download_manager.DownloadTooLargeError):
        downloader._download_sync(
            "https://cdn.example.com/video.mp4",
            "video.mp4",
            {},
            False,
            None,
            5,
        )

    assert target.read_bytes() == b"existing"


@pytest.mark.asyncio
async def test_download_rejects_parent_path_traversal(tmp_path):
    downloader = ResilientDownloader(str(tmp_path))

    with pytest.raises(download_manager.DownloadError, match="escapes output directory"):
        await downloader.download("https://cdn.example.com/video.mp4", "../evil.txt")


@pytest.mark.asyncio
async def test_download_rejects_absolute_target_path(tmp_path):
    downloader = ResilientDownloader(str(tmp_path))
    outside_path = tmp_path.parent / "evil.txt"

    with pytest.raises(download_manager.DownloadError, match="escapes output directory"):
        await downloader.download("https://cdn.example.com/video.mp4", str(outside_path))


def test_download_sync_allows_nested_relative_paths(monkeypatch, tmp_path):
    downloader = ResilientDownloader(str(tmp_path))
    target = tmp_path / "tweet123" / "video.mp4"

    monkeypatch.setattr(downloader, "_probe", lambda url, headers: (4, False))

    def fake_download_single(url, target_path, headers, *, progress_state=None, max_size_bytes=None):
        Path(target_path).write_bytes(b"data")

    monkeypatch.setattr(downloader, "_download_single", fake_download_single)

    metrics = downloader._download_sync(
        "https://cdn.example.com/video.mp4",
        "tweet123/video.mp4",
        {},
        False,
        None,
        None,
    )

    assert target.exists()
    assert metrics.path == str(target)
