from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class AnalyticsRuntimeSnapshot:
    dropped_events: int
    last_drop_monotonic: float | None


_lock = threading.Lock()
_dropped_events = 0
_last_drop_monotonic: float | None = None


def record_drop(*, now: float | None = None) -> None:
    global _dropped_events, _last_drop_monotonic
    with _lock:
        _dropped_events += 1
        _last_drop_monotonic = time.monotonic() if now is None else now


def get_snapshot() -> AnalyticsRuntimeSnapshot:
    with _lock:
        return AnalyticsRuntimeSnapshot(
            dropped_events=_dropped_events,
            last_drop_monotonic=_last_drop_monotonic,
        )
