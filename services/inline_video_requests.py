from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class InlineVideoRequest:
    service: str
    source_url: str
    owner_user_id: int
    user_settings: dict[str, str]
    state: str = "pending"
    created_at_monotonic: float = field(default_factory=lambda: time.monotonic())
    updated_at_monotonic: float = field(default_factory=lambda: time.monotonic())


_PENDING_REQUEST_TTL_SECONDS = 6 * 60 * 60.0
_COMPLETED_REQUEST_TTL_SECONDS = 30 * 60.0
_MAX_INLINE_VIDEO_REQUESTS = 2048
_requests: dict[str, InlineVideoRequest] = {}


def _request_ttl(request: InlineVideoRequest) -> float:
    return _COMPLETED_REQUEST_TTL_SECONDS if request.state == "completed" else _PENDING_REQUEST_TTL_SECONDS


def _set_request_state(request: InlineVideoRequest, state: str, now: Optional[float] = None) -> None:
    request.state = state
    request.updated_at_monotonic = time.monotonic() if now is None else now


def _prune_requests(now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now

    expired_tokens = [
        token
        for token, request in _requests.items()
        if now - request.updated_at_monotonic > _request_ttl(request)
    ]
    for token in expired_tokens:
        _requests.pop(token, None)

    overflow = len(_requests) - _MAX_INLINE_VIDEO_REQUESTS
    if overflow <= 0:
        return

    oldest_tokens = sorted(
        _requests,
        key=lambda token: _requests[token].updated_at_monotonic,
    )[:overflow]
    for token in oldest_tokens:
        _requests.pop(token, None)


def create_inline_video_request(
    service: str,
    source_url: str,
    owner_user_id: int,
    user_settings: dict[str, str],
) -> str:
    _prune_requests()
    token = secrets.token_urlsafe(12)
    _requests[token] = InlineVideoRequest(
        service=service,
        source_url=source_url,
        owner_user_id=owner_user_id,
        user_settings=dict(user_settings),
    )
    return token


def get_inline_video_request(token: str) -> Optional[InlineVideoRequest]:
    _prune_requests()
    return _requests.get(token)


def claim_inline_video_request(token: str) -> Optional[InlineVideoRequest]:
    _prune_requests()
    request = _requests.get(token)
    if request is None or request.state != "pending":
        return None
    _set_request_state(request, "processing")
    return request


def claim_inline_video_request_for_send(
    token: str,
    *,
    duplicate_handler: str,
    actor_user_id: Optional[int] = None,
) -> Optional[InlineVideoRequest]:
    _prune_requests()
    request = get_inline_video_request(token)
    if request is None:
        return None

    if actor_user_id is not None and int(actor_user_id) != request.owner_user_id:
        raise PermissionError("token_owner_mismatch")

    if request.state == "pending":
        _set_request_state(request, "processing")
        return request

    if duplicate_handler == "callback":
        if request.state == "processing":
            raise ValueError("already_processing")
        if request.state == "completed":
            raise ValueError("already_completed")
    return None


def reset_inline_video_request(token: str) -> Optional[InlineVideoRequest]:
    _prune_requests()
    request = _requests.get(token)
    if request is None:
        return None
    _set_request_state(request, "pending")
    return request


def complete_inline_video_request(token: str) -> Optional[InlineVideoRequest]:
    _prune_requests()
    request = _requests.get(token)
    if request is None:
        return None
    _set_request_state(request, "completed")
    return request
