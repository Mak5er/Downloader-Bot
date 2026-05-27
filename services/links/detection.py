from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


_TRAILING_URL_PUNCTUATION = ".,;:!?)]}>\"'"

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
        re.compile(r"(https?://(www\.|music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+)", re.IGNORECASE),
    ),
    (
        "twitter",
        re.compile(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)", re.IGNORECASE),
    ),
)


def _clean_url(url: str) -> str:
    return url.rstrip(_TRAILING_URL_PUNCTUATION)


def canonicalize_supported_url(service: str, url: str) -> str:
    cleaned = _clean_url(url)
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return cleaned

    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path
    params = ""
    fragment = ""
    query = ""

    if service == "youtube":
        keep = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=False):
            if key in {"v", "list", "t", "start"}:
                keep.append((key, value))
        query = urlencode(keep)
    elif service == "soundcloud":
        # SoundCloud sometimes uses meaningful path slugs; tracking lives in query/fragment.
        query = ""
    else:
        query = ""

    return urlunparse((scheme, netloc, path, params, query, fragment))


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
            return service, canonicalize_supported_url(service, match.group(1))
    return None


def extract_supported_links(text: str | None) -> list[tuple[str, str]]:
    if not text:
        return []

    matches: list[tuple[int, str, str]] = []
    for service, pattern in _SERVICE_PATTERNS:
        for match in pattern.finditer(text):
            matches.append((match.start(), service, match.group(1)))

    matches.sort(key=lambda item: item[0])

    links: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for _position, service, url in matches:
        url = canonicalize_supported_url(service, url)
        key = (service, url)
        if key in seen:
            continue
        seen.add(key)
        links.append((service, url))
    return links
