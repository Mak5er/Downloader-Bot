from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class InlineVideoRequest:
    service: str
    source_url: str
    owner_user_id: int
    user_settings: dict[str, str]
    state: str = "pending"


_requests: dict[str, InlineVideoRequest] = {}


def create_inline_video_request(
    service: str,
    source_url: str,
    owner_user_id: int,
    user_settings: dict[str, str],
) -> str:
    token = secrets.token_urlsafe(12)
    _requests[token] = InlineVideoRequest(
        service=service,
        source_url=source_url,
        owner_user_id=owner_user_id,
        user_settings=dict(user_settings),
    )
    return token


def get_inline_video_request(token: str) -> Optional[InlineVideoRequest]:
    return _requests.get(token)


def claim_inline_video_request(token: str) -> Optional[InlineVideoRequest]:
    request = _requests.get(token)
    if request is None or request.state != "pending":
        return None
    request.state = "processing"
    return request


def claim_inline_video_request_for_send(
    token: str,
    *,
    duplicate_handler: str,
) -> Optional[InlineVideoRequest]:
    request = claim_inline_video_request(token)
    if request is not None:
        return request

    if duplicate_handler == "callback":
        existing = get_inline_video_request(token)
        if existing and existing.state == "processing":
            raise ValueError("already_processing")
        if existing and existing.state == "completed":
            raise ValueError("already_completed")
    return None


def reset_inline_video_request(token: str) -> Optional[InlineVideoRequest]:
    request = _requests.get(token)
    if request is None:
        return None
    request.state = "pending"
    return request


def complete_inline_video_request(token: str) -> Optional[InlineVideoRequest]:
    request = _requests.get(token)
    if request is None:
        return None
    request.state = "completed"
    return request
