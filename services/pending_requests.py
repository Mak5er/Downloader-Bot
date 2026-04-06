from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PendingRequest:
    text: str
    notice_chat_id: int
    notice_message_id: int
    source_chat_id: Optional[int] = None
    source_message_id: Optional[int] = None
    created_at_monotonic: float = field(default_factory=lambda: time.monotonic())


_PENDING_TTL_SECONDS = 30 * 60.0
_MAX_PENDING_REQUESTS = 1024
_pending: dict[int, PendingRequest] = {}


def _prune_pending(now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now

    expired_user_ids = [
        user_id
        for user_id, request in _pending.items()
        if now - request.created_at_monotonic > _PENDING_TTL_SECONDS
    ]
    for user_id in expired_user_ids:
        _pending.pop(user_id, None)

    overflow = len(_pending) - _MAX_PENDING_REQUESTS
    if overflow <= 0:
        return

    oldest_user_ids = sorted(
        _pending,
        key=lambda user_id: _pending[user_id].created_at_monotonic,
    )[:overflow]
    for user_id in oldest_user_ids:
        _pending.pop(user_id, None)


def set_pending(user_id: int, request: PendingRequest) -> None:
    _prune_pending()
    _pending[user_id] = request


def get_pending(user_id: int) -> Optional[PendingRequest]:
    _prune_pending()
    return _pending.get(user_id)


def pop_pending(user_id: int) -> Optional[PendingRequest]:
    _prune_pending()
    return _pending.pop(user_id, None)
