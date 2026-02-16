from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class InlineAlbumRequest:
    service: str
    url: str


_requests: dict[str, InlineAlbumRequest] = {}
_tokens_by_key: dict[str, str] = {}


def _build_request_key(service: str, url: str) -> str:
    normalized = f"{service.strip().lower()}|{url.strip()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def create_inline_album_request(_user_id: int, service: str, url: str) -> str:
    key = _build_request_key(service, url)
    existing_token = _tokens_by_key.get(key)
    if existing_token and existing_token in _requests:
        return existing_token

    token = secrets.token_urlsafe(16)
    _tokens_by_key[key] = token
    _requests[token] = InlineAlbumRequest(service=service, url=url)
    return token


def get_inline_album_request(token: str) -> Optional[InlineAlbumRequest]:
    return _requests.get(token)
