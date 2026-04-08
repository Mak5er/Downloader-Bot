from __future__ import annotations

import re


_SERVICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "tiktok",
        re.compile(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", re.IGNORECASE),
    ),
    (
        "instagram",
        re.compile(r"(https?://(www\.)?instagram\.com/\S+)", re.IGNORECASE),
    ),
    (
        "soundcloud",
        re.compile(
            r"(https?://(?:www\.|m\.)?soundcloud\.com/\S+|https?://on\.soundcloud\.com/\S+|https?://soundcloud\.app\.goo\.gl/\S+)",
            re.IGNORECASE,
        ),
    ),
    (
        "pinterest",
        re.compile(r"(https?://(?:[\w-]+\.)?pinterest\.[\w.]+/\S+|https?://pin\.it/\S+)", re.IGNORECASE),
    ),
    (
        "youtube",
        re.compile(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+)", re.IGNORECASE),
    ),
    (
        "twitter",
        re.compile(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)", re.IGNORECASE),
    ),
)


def detect_supported_service(text: str | None) -> str | None:
    if not text:
        return None

    for service, pattern in _SERVICE_PATTERNS:
        if pattern.search(text):
            return service
    return None


def extract_supported_link(text: str | None) -> tuple[str, str] | None:
    if not text:
        return None

    for service, pattern in _SERVICE_PATTERNS:
        match = pattern.search(text)
        if match:
            return service, match.group(1)
    return None
