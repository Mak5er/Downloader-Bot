from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class InlineAlbumRequest:
    service: str
    url: str
    request_key: str
    created_at_monotonic: float = field(default_factory=lambda: time.monotonic())


_INLINE_ALBUM_TTL_SECONDS = 24 * 60 * 60.0
_MAX_INLINE_ALBUM_REQUESTS = 2048
_requests: dict[str, InlineAlbumRequest] = {}
_tokens_by_key: dict[str, str] = {}


def _build_request_key(service: str, url: str) -> str:
    normalized = f"{service.strip().lower()}|{url.strip()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _drop_token(token: str) -> None:
    request = _requests.pop(token, None)
    if request is None:
        return
    if _tokens_by_key.get(request.request_key) == token:
        _tokens_by_key.pop(request.request_key, None)


def _prune_requests(now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now

    expired_tokens = [
        token
        for token, request in _requests.items()
        if now - request.created_at_monotonic > _INLINE_ALBUM_TTL_SECONDS
    ]
    for token in expired_tokens:
        _drop_token(token)

    overflow = len(_requests) - _MAX_INLINE_ALBUM_REQUESTS
    if overflow <= 0:
        return

    oldest_tokens = sorted(
        _requests,
        key=lambda token: _requests[token].created_at_monotonic,
    )[:overflow]
    for token in oldest_tokens:
        _drop_token(token)


def create_inline_album_request(_user_id: int, service: str, url: str) -> str:
    _prune_requests()
    key = _build_request_key(service, url)
    existing_token = _tokens_by_key.get(key)
    if existing_token and existing_token in _requests:
        return existing_token

    token = secrets.token_urlsafe(16)
    _tokens_by_key[key] = token
    _requests[token] = InlineAlbumRequest(service=service, url=url, request_key=key)
    return token


def get_inline_album_request(token: str) -> Optional[InlineAlbumRequest]:
    _prune_requests()
    return _requests.get(token)
