from __future__ import annotations

import time
from collections import OrderedDict
from typing import Hashable, Literal
from urllib.parse import parse_qs, urlparse, urlunparse

from config import (
    REQUEST_DEDUPE_ACTIVE_TTL_SECONDS,
    REQUEST_DEDUPE_COMPLETED_TTL_SECONDS,
    REQUEST_DEDUPE_MAX_ENTRIES,
)
from services.platforms.instagram_media import strip_instagram_url
from services.platforms.pinterest_media import strip_pinterest_url
from services.platforms.soundcloud_media import strip_soundcloud_url
from services.platforms.tiktok_common import strip_tiktok_tracking

RequestClaimStatus = Literal["accepted", "active", "recent"]
RequestFingerprint = tuple[int, str, str]

_ACTIVE_TTL_SECONDS = max(1.0, float(REQUEST_DEDUPE_ACTIVE_TTL_SECONDS))
_COMPLETED_TTL_SECONDS = max(0.0, float(REQUEST_DEDUPE_COMPLETED_TTL_SECONDS))
_MAX_ENTRIES = max(1, int(REQUEST_DEDUPE_MAX_ENTRIES))

_active_requests: "OrderedDict[RequestFingerprint, float]" = OrderedDict()
_completed_requests: "OrderedDict[RequestFingerprint, float]" = OrderedDict()


def normalize_request_service(service: str) -> str:
    return str(service or "").strip().lower()


def normalize_request_url(service: str, url: str) -> str:
    normalized_service = normalize_request_service(service)
    raw_url = str(url or "").strip()
    if not raw_url:
        return ""

    if normalized_service == "instagram":
        return _normalize_generic_url(strip_instagram_url(raw_url))
    if normalized_service == "pinterest":
        return _normalize_generic_url(strip_pinterest_url(raw_url))
    if normalized_service == "soundcloud":
        return _normalize_generic_url(strip_soundcloud_url(raw_url))
    if normalized_service == "tiktok":
        return _normalize_generic_url(strip_tiktok_tracking(raw_url))
    if normalized_service == "twitter":
        return _normalize_twitter_url(raw_url)
    if normalized_service.startswith("youtube"):
        return _normalize_youtube_url(raw_url)
    return _normalize_generic_url(raw_url)


def build_request_fingerprint(user_id: int, service: str, url: str) -> RequestFingerprint:
    return int(user_id), normalize_request_service(service), normalize_request_url(service, url)


def same_request(first_service: str, first_url: str, second_service: str, second_url: str) -> bool:
    return (
        normalize_request_service(first_service) == normalize_request_service(second_service)
        and normalize_request_url(first_service, first_url) == normalize_request_url(second_service, second_url)
    )


def claim_request(user_id: int, service: str, url: str) -> RequestClaimStatus:
    now = time.monotonic()
    _cleanup(now)
    fingerprint = build_request_fingerprint(user_id, service, url)
    if not fingerprint[2]:
        return "accepted"

    active_at = _active_requests.get(fingerprint)
    if active_at is not None and now - active_at <= _ACTIVE_TTL_SECONDS:
        _active_requests.move_to_end(fingerprint)
        return "active"

    completed_at = _completed_requests.get(fingerprint)
    if completed_at is not None and now - completed_at <= _COMPLETED_TTL_SECONDS:
        _completed_requests.move_to_end(fingerprint)
        return "recent"

    _active_requests[fingerprint] = now
    _active_requests.move_to_end(fingerprint)
    _trim()
    return "accepted"


def finish_request(user_id: int, service: str, url: str, *, success: bool) -> None:
    now = time.monotonic()
    fingerprint = build_request_fingerprint(user_id, service, url)
    _active_requests.pop(fingerprint, None)
    if success and fingerprint[2]:
        _completed_requests[fingerprint] = now
        _completed_requests.move_to_end(fingerprint)
    _cleanup(now)


def reset_request_tracking() -> None:
    _active_requests.clear()
    _completed_requests.clear()


def _normalize_generic_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return url.strip().lower()

    if not parsed.scheme or not parsed.netloc:
        return url.strip().lower()

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "/").rstrip("/") or "/"
    return urlunparse(("https", host, path, "", "", ""))


def _normalize_twitter_url(url: str) -> str:
    normalized = _normalize_generic_url(url)
    try:
        parsed = urlparse(normalized)
    except Exception:
        return normalized

    host = parsed.netloc.lower().replace("twitter.com", "x.com")
    path = (parsed.path or "/").rstrip("/") or "/"
    return urlunparse(("https", host, path, "", "", ""))


def _normalize_youtube_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return url.strip().lower()

    host = parsed.netloc.lower()
    path = (parsed.path or "/").strip()
    query = parse_qs(parsed.query or "")
    is_music = host.startswith("music.")
    host_prefix = "music." if is_music else ""

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = next((part for part in path.split("/") if part), "")
        if video_id:
            return f"https://{host_prefix}youtube.com/watch?v={video_id}"

    if host.startswith("www."):
        host = host[4:]

    if host.endswith("youtube.com"):
        if path == "/watch":
            video_id = (query.get("v") or [""])[0].strip()
            if video_id:
                return f"https://{host_prefix}youtube.com/watch?v={video_id}"

        path_parts = [part for part in path.split("/") if part]
        if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            return f"https://{host_prefix}youtube.com/{path_parts[0]}/{path_parts[1]}"

        clean_path = ("/" + "/".join(path_parts)).rstrip("/") or "/"
        return urlunparse(("https", f"{host_prefix}youtube.com", clean_path, "", "", ""))

    return _normalize_generic_url(url)


def _cleanup(now: float) -> None:
    _prune_expired(_active_requests, now, _ACTIVE_TTL_SECONDS)
    _prune_expired(_completed_requests, now, _COMPLETED_TTL_SECONDS)
    _trim()


def _prune_expired(store: "OrderedDict[Hashable, float]", now: float, ttl_seconds: float) -> None:
    if ttl_seconds <= 0:
        store.clear()
        return

    while store:
        key, created_at = next(iter(store.items()))
        if now - created_at <= ttl_seconds:
            break
        store.pop(key, None)


def _trim() -> None:
    overflow = (len(_active_requests) + len(_completed_requests)) - _MAX_ENTRIES
    while overflow > 0 and _completed_requests:
        _completed_requests.popitem(last=False)
        overflow -= 1
    while overflow > 0 and _active_requests:
        _active_requests.popitem(last=False)
        overflow -= 1
