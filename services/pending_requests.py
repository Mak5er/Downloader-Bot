from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from services.runtime_state_store import load_bucket, save_bucket


@dataclass
class PendingRequest:
    text: str
    notice_chat_id: int
    notice_message_id: int
    source_chat_id: Optional[int] = None
    source_message_id: Optional[int] = None
    created_at_epoch: float = field(default_factory=lambda: time.time())


_PENDING_TTL_SECONDS = 30 * 60.0
_MAX_PENDING_REQUESTS = 1024
_PERSISTENCE_BUCKET = "pending_requests"
_pending: dict[int, PendingRequest] = {}
_loaded = False


def _serialize_pending_request(request: PendingRequest) -> dict[str, object]:
    return {
        "text": request.text,
        "notice_chat_id": request.notice_chat_id,
        "notice_message_id": request.notice_message_id,
        "source_chat_id": request.source_chat_id,
        "source_message_id": request.source_message_id,
        "created_at_epoch": request.created_at_epoch,
    }


def _deserialize_pending_request(payload: dict[str, object]) -> PendingRequest:
    return PendingRequest(
        text=str(payload.get("text") or ""),
        notice_chat_id=int(payload["notice_chat_id"]),
        notice_message_id=int(payload["notice_message_id"]),
        source_chat_id=int(payload["source_chat_id"]) if payload.get("source_chat_id") is not None else None,
        source_message_id=int(payload["source_message_id"]) if payload.get("source_message_id") is not None else None,
        created_at_epoch=float(payload.get("created_at_epoch") or time.time()),
    )


def _ensure_loaded() -> None:
    global _loaded, _pending
    if _loaded:
        return

    payload = load_bucket(_PERSISTENCE_BUCKET, dict)
    _pending = {
        int(user_id): _deserialize_pending_request(request_payload)
        for user_id, request_payload in payload.items()
        if isinstance(request_payload, dict)
    }
    _loaded = True


def _persist_pending() -> None:
    save_bucket(
        _PERSISTENCE_BUCKET,
        {
            str(user_id): _serialize_pending_request(request)
            for user_id, request in _pending.items()
        },
    )


def _prune_pending(now: Optional[float] = None) -> None:
    _ensure_loaded()
    now = time.time() if now is None else now

    expired_user_ids = [
        user_id
        for user_id, request in _pending.items()
        if now - request.created_at_epoch > _PENDING_TTL_SECONDS
    ]
    for user_id in expired_user_ids:
        _pending.pop(user_id, None)

    overflow = len(_pending) - _MAX_PENDING_REQUESTS
    if overflow <= 0:
        return

    oldest_user_ids = sorted(
        _pending,
        key=lambda user_id: _pending[user_id].created_at_epoch,
    )[:overflow]
    for user_id in oldest_user_ids:
        _pending.pop(user_id, None)

    if expired_user_ids or overflow > 0:
        _persist_pending()


def set_pending(user_id: int, request: PendingRequest) -> None:
    _prune_pending()
    _pending[user_id] = request
    _persist_pending()


def get_pending(user_id: int) -> Optional[PendingRequest]:
    _prune_pending()
    return _pending.get(user_id)


def pop_pending(user_id: int) -> Optional[PendingRequest]:
    _prune_pending()
    request = _pending.pop(user_id, None)
    if request is not None:
        _persist_pending()
    return request
