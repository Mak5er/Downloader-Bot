from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app_context
from services.runtime import request_dedupe


class FakeYoutubeDL:
    """Reusable fake for yt-dlp's YoutubeDL across platform tests."""

    def __init__(
        self,
        options: dict[str, Any],
        *,
        ext: str = "mp4",
        payload: bytes = b"test-media",
        extract_info_result: Optional[dict[str, Any]] = None,
        extract_info_error: Optional[Exception] = None,
    ):
        self.options = options
        self.ext = ext
        self.payload = payload
        self._extract_info_result = extract_info_result
        self._extract_info_error = extract_info_error
        self.urls: list[Any] = []

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
            stem, _ext = str(Path(outtmpl)).rsplit(".", 1)
            output_path = f"{stem}.{self.ext}"
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

    def extract_info(self, url, download):
        if self._extract_info_error is not None:
            raise self._extract_info_error
        if self._extract_info_result is not None:
            return dict(self._extract_info_result)
        return {
            "id": "abc123",
            "title": "Test Video",
            "webpage_url": url,
            "formats": [],
            "_download_arg": download,
        }


class _AttrBag:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def __getattr__(self, name: str):
        value = AsyncMock(name=name)
        setattr(self, name, value)
        return value


@pytest.fixture(autouse=True)
def isolate_runtime_request_state():
    async def _send_analytics(*args, **kwargs):
        return None

    app_context.set_app_context(
        bot=_AttrBag(id=1, username="test_bot"),
        db=_AttrBag(
            name="test-db",
            get_file_id=AsyncMock(return_value=None),
            add_file=AsyncMock(),
            user_settings=AsyncMock(return_value={}),
            get_user_setting=AsyncMock(return_value=None),
            set_user_setting=AsyncMock(),
            upsert_chat=AsyncMock(),
            set_inactive=AsyncMock(),
            status=AsyncMock(return_value="active"),
        ),
        send_analytics=_send_analytics,
    )
    request_dedupe.reset_request_tracking()
    from middlewares import private_chat_guard
    private_chat_guard._can_dm_cache.clear()
    yield
    request_dedupe.reset_request_tracking()
    private_chat_guard._can_dm_cache.clear()
    app_context._context = None
