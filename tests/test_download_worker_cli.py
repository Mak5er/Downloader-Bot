import io
import json
from types import SimpleNamespace

import pytest

from services import download_worker_cli
from utils.download_manager import DownloadMetrics


def test_read_payload_raises_when_stdin_is_empty(monkeypatch):
    monkeypatch.setattr(download_worker_cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"")))

    with pytest.raises(ValueError, match="No payload provided"):
        download_worker_cli._read_payload()


def test_build_config_converts_stream_timeout_list_to_tuple():
    config = download_worker_cli._build_config(
        {
            "chunk_size": 123,
            "stream_timeout": [1.5, 8.0],
        }
    )

    assert config.chunk_size == 123
    assert config.stream_timeout == (1.5, 8.0)


def test_main_downloads_payload_and_writes_json(monkeypatch):
    payload = {
        "output_dir": "downloads",
        "url": "https://example.com/video",
        "filename": "video.mp4",
        "headers": {"User-Agent": "pytest"},
        "skip_if_exists": True,
        "max_size_bytes": 4096,
        "source": "pytest-worker",
        "config": {"stream_timeout": [3.0, 12.0]},
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    captured = {}

    class FakeDownloader:
        def __init__(self, *, output_dir, config, source):
            captured["output_dir"] = output_dir
            captured["config"] = config
            captured["source"] = source

        def _download_sync(
            self,
            url,
            filename,
            headers,
            skip_if_exists,
            _progress_callback,
            max_size_bytes,
        ):
            captured["call"] = {
                "url": url,
                "filename": filename,
                "headers": headers,
                "skip_if_exists": skip_if_exists,
                "max_size_bytes": max_size_bytes,
            }
            return DownloadMetrics(
                url=url,
                path=f"downloads/{filename}",
                size=2048,
                elapsed=1.25,
                used_multipart=False,
                resumed=True,
            )

    monkeypatch.setattr(
        download_worker_cli.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode("utf-8"))),
    )
    monkeypatch.setattr(download_worker_cli.sys, "stdout", stdout)
    monkeypatch.setattr(download_worker_cli.sys, "stderr", stderr)
    monkeypatch.setattr(download_worker_cli, "ResilientDownloader", FakeDownloader)

    assert download_worker_cli.main() == 0
    assert stderr.getvalue() == ""
    assert captured["output_dir"] == "downloads"
    assert captured["source"] == "pytest-worker"
    assert captured["config"].stream_timeout == (3.0, 12.0)
    assert captured["call"]["skip_if_exists"] is True
    assert captured["call"]["max_size_bytes"] == 4096
    assert json.loads(stdout.getvalue()) == {
        "url": "https://example.com/video",
        "path": "downloads/video.mp4",
        "size": 2048,
        "elapsed": 1.25,
        "used_multipart": False,
        "resumed": True,
    }


def test_main_writes_error_to_stderr(monkeypatch):
    payload = {
        "output_dir": "downloads",
        "url": "https://example.com/video",
        "filename": "video.mp4",
        "config": {},
    }
    stdout = io.StringIO()
    stderr = io.StringIO()

    class BrokenDownloader:
        def __init__(self, *, output_dir, config, source):
            pass

        def _download_sync(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        download_worker_cli.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode("utf-8"))),
    )
    monkeypatch.setattr(download_worker_cli.sys, "stdout", stdout)
    monkeypatch.setattr(download_worker_cli.sys, "stderr", stderr)
    monkeypatch.setattr(download_worker_cli, "ResilientDownloader", BrokenDownloader)

    assert download_worker_cli.main() == 1
    assert stdout.getvalue() == ""
    assert "boom" in stderr.getvalue()
