from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}


@dataclass(frozen=True)
class RuntimeStatsSnapshot:
    started_at_monotonic: float
    uptime_seconds: float
    total_downloads: int
    total_videos: int
    total_audio: int
    total_other: int
    total_bytes: int
    by_source: dict[str, dict[str, int]]


_lock = threading.Lock()
_started_at = time.monotonic()
_total_downloads = 0
_total_videos = 0
_total_audio = 0
_total_other = 0
_total_bytes = 0
_by_source: dict[str, dict[str, int]] = {}


def record_download(source: str, metrics: Any) -> None:
    global _total_downloads, _total_videos, _total_audio, _total_other, _total_bytes

    src = (source or "unknown").strip().lower()
    size = int(getattr(metrics, "size", 0) or 0)
    path = str(getattr(metrics, "path", "") or "").lower()
    dot = path.rfind(".")
    ext = path[dot:] if dot != -1 else ""

    kind = "other"
    if ext in VIDEO_EXTENSIONS or "video" in src:
        kind = "video"
    elif ext in AUDIO_EXTENSIONS or "audio" in src or "mp3" in src:
        kind = "audio"

    with _lock:
        _total_downloads += 1
        _total_bytes += max(0, size)

        if kind == "video":
            _total_videos += 1
        elif kind == "audio":
            _total_audio += 1
        else:
            _total_other += 1

        source_bucket = _by_source.setdefault(src, {"count": 0, "bytes": 0})
        source_bucket["count"] += 1
        source_bucket["bytes"] += max(0, size)


def get_runtime_snapshot() -> RuntimeStatsSnapshot:
    with _lock:
        by_source_copy = {k: dict(v) for k, v in _by_source.items()}
        return RuntimeStatsSnapshot(
            started_at_monotonic=_started_at,
            uptime_seconds=max(0.0, time.monotonic() - _started_at),
            total_downloads=_total_downloads,
            total_videos=_total_videos,
            total_audio=_total_audio,
            total_other=_total_other,
            total_bytes=_total_bytes,
            by_source=by_source_copy,
        )
