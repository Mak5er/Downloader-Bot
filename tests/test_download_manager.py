import asyncio
import time
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
