from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from services.runtime.state_store import load_bucket, save_bucket


@dataclass(slots=True)
class AudioRequest:
    service: str
    url: str
    request_key: str
    created_at_epoch: float = field(default_factory=lambda: time.time())


_AUDIO_REQUEST_TTL_SECONDS = 24 * 60 * 60.0
_MAX_AUDIO_REQUESTS = 2048
_PERSISTENCE_BUCKET = "audio_requests"
_requests: dict[str, AudioRequest] = {}
_tokens_by_key: dict[str, str] = {}
_loaded = False


def _serialize_request(request: AudioRequest) -> dict[str, object]:
    return {
        "service": request.service,
        "url": request.url,
        "request_key": request.request_key,
        "created_at_epoch": request.created_at_epoch,
    }


def _deserialize_request(payload: dict[str, object]) -> AudioRequest:
    return AudioRequest(
        service=str(payload.get("service") or ""),
        url=str(payload.get("url") or ""),
        request_key=str(payload.get("request_key") or ""),
        created_at_epoch=float(payload.get("created_at_epoch") or time.time()),
    )


def _rebuild_token_index() -> None:
    global _tokens_by_key
    _tokens_by_key = {
        request.request_key: token
        for token, request in _requests.items()
    }


def _ensure_loaded() -> None:
    global _loaded, _requests
    if _loaded:
        return

    payload = load_bucket(_PERSISTENCE_BUCKET, dict)
    _requests = {
        token: _deserialize_request(request_payload)
        for token, request_payload in payload.items()
        if isinstance(request_payload, dict)
    }
    _rebuild_token_index()
    _loaded = True


def _persist_requests() -> None:
    save_bucket(
        _PERSISTENCE_BUCKET,
        {
            token: _serialize_request(request)
            for token, request in _requests.items()
        },
    )


def _build_request_key(service: str, url: str) -> str:
    normalized = f"{service.strip().lower()}|{url.strip()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _drop_token(token: str) -> None:
    _ensure_loaded()
    request = _requests.pop(token, None)
    if request is None:
        return
    if _tokens_by_key.get(request.request_key) == token:
        _tokens_by_key.pop(request.request_key, None)


def _prune_requests(now: Optional[float] = None) -> None:
    _ensure_loaded()
    now = time.time() if now is None else now

    expired_tokens = [
        token
        for token, request in _requests.items()
        if now - request.created_at_epoch > _AUDIO_REQUEST_TTL_SECONDS
    ]
    for token in expired_tokens:
        _drop_token(token)

    overflow = len(_requests) - _MAX_AUDIO_REQUESTS
    if overflow <= 0:
        if expired_tokens:
            _persist_requests()
        return

    oldest_tokens = sorted(
        _requests,
        key=lambda token: _requests[token].created_at_epoch,
    )[:overflow]
    for token in oldest_tokens:
        _drop_token(token)

    if expired_tokens or overflow > 0:
        _persist_requests()


def create_audio_request(service: str, url: str) -> str:
    _prune_requests()
    key = _build_request_key(service, url)
    existing_token = _tokens_by_key.get(key)
    if existing_token and existing_token in _requests:
        return existing_token

    token = secrets.token_urlsafe(12)
    _tokens_by_key[key] = token
    _requests[token] = AudioRequest(service=service, url=url, request_key=key)
    _persist_requests()
    return token


def get_audio_request(token: str) -> Optional[AudioRequest]:
    _prune_requests()
    return _requests.get(token)
