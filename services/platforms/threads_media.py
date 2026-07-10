from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable, Iterator, Optional
from urllib.parse import urlparse

from services.logger import logger as logging
from utils.download_manager import (
    DownloadConfig,
    DownloadError,
    DownloadMetrics,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    ResilientDownloader,
)
from utils.http_client import get_http_session

logging = logging.bind(service="threads_media")

THREADS_POST_URL_RE = re.compile(
    r"^https?://(?:www\.)?threads\.(?:com|net)/@(?P<username>[A-Za-z0-9._-]+)/post/(?P<code>[A-Za-z0-9_-]+)/*$",
    re.IGNORECASE,
)
THREADS_PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
}
THREADS_MEDIA_HEADERS = {
    "Referer": "https://www.threads.com/",
    "User-Agent": THREADS_PAGE_HEADERS["User-Agent"],
}


def strip_threads_url(url: str) -> str:
    """Canonicalize a public Threads post URL for dedupe and file caching."""
    candidate = (url or "").strip()
    try:
        parsed = urlparse(candidate)
    except Exception:
        return candidate

    normalized = f"{parsed.scheme.lower() or 'https'}://{parsed.netloc.lower()}{parsed.path}"
    match = THREADS_POST_URL_RE.fullmatch(normalized)
    if not match:
        return candidate
    return f"https://www.threads.com/@{match.group('username')}/post/{match.group('code')}"


def extract_threads_post_code(url: str) -> str | None:
    match = THREADS_POST_URL_RE.fullmatch(strip_threads_url(url))
    return match.group("code") if match else None


@dataclass(slots=True)
class ThreadsMedia:
    url: str
    type: str
    width: int | None = None
    height: int | None = None


@dataclass(slots=True)
class ThreadsPost:
    id: str
    description: str
    author: str
    media_list: list[ThreadsMedia]


def get_threads_preview_url(media: ThreadsMedia | None) -> str | None:
    if not media or media.type != "photo":
        return None
    return media.url


class _JsonScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._attrs: dict[str, str | None] | None = None
        self._body: list[str] | None = None
        self.scripts: list[tuple[dict[str, str | None], str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._attrs = dict(attrs)
            self._body = []

    def handle_data(self, data: str) -> None:
        if self._body is not None:
            self._body.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._body is not None:
            self.scripts.append((self._attrs or {}, "".join(self._body)))
            self._attrs = None
            self._body = None


def _iter_json_blobs(page: str) -> Iterator[dict[str, Any]]:
    parser = _JsonScriptParser()
    parser.feed(page)
    for attrs, body in parser.scripts:
        if attrs.get("type") != "application/json" or "data-sjs" not in attrs:
            continue
        if not body.lstrip().startswith("{"):
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _walk_json(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _find_post(page: str, post_code: str) -> dict[str, Any] | None:
    needle = f'"code":"{post_code}"'
    for payload in _iter_json_blobs(page):
        serialized = json.dumps(payload, separators=(",", ":"))
        if needle not in serialized:
            continue
        for node in _walk_json(payload):
            if node.get("code") == post_code:
                return node
    return None


def _has_media(node: dict[str, Any]) -> bool:
    return bool(node.get("carousel_media") or node.get("video_versions") or node.get("image_versions2"))


def _media_source(post: dict[str, Any]) -> dict[str, Any]:
    if _has_media(post):
        return post

    app_info = post.get("text_post_app_info")
    if isinstance(app_info, dict):
        linked_media = app_info.get("linked_inline_media")
        if isinstance(linked_media, dict) and _has_media(linked_media):
            return linked_media

        share_info = app_info.get("share_info")
        if isinstance(share_info, dict):
            quoted_post = share_info.get("quoted_attachment_post")
            if isinstance(quoted_post, dict) and _has_media(quoted_post):
                return quoted_post
    return post


def _best_variant(items: Any) -> dict[str, Any] | None:
    candidates = [item for item in items or [] if isinstance(item, dict) and isinstance(item.get("url"), str)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0),
    )


def _extract_media(post: dict[str, Any]) -> list[ThreadsMedia]:
    source = _media_source(post)
    items = source.get("carousel_media") or [source]
    media: list[ThreadsMedia] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        video = _best_variant(item.get("video_versions"))
        if video:
            media.append(
                ThreadsMedia(
                    url=video["url"],
                    type="video",
                    width=video.get("width"),
                    height=video.get("height"),
                )
            )
            continue

        image_versions = item.get("image_versions2")
        image = _best_variant(image_versions.get("candidates") if isinstance(image_versions, dict) else None)
        if image:
            media.append(
                ThreadsMedia(
                    url=image["url"],
                    type="photo",
                    width=image.get("width"),
                    height=image.get("height"),
                )
            )
    return media


def parse_threads_post_html(page: str, post_code: str) -> ThreadsPost | None:
    post = _find_post(page, post_code)
    if not post:
        return None

    caption = post.get("caption")
    description = caption.get("text", "") if isinstance(caption, dict) else ""
    description = description.strip() if isinstance(description, str) else ""
    media_list = _extract_media(post)
    if not media_list and not description:
        return None

    user = post.get("user")
    author = user.get("username", "threads") if isinstance(user, dict) else "threads"
    return ThreadsPost(
        id=post_code,
        description=description,
        author=author.strip() if isinstance(author, str) and author.strip() else "threads",
        media_list=media_list,
    )


async def fetch_threads_post_html(url: str) -> str:
    session = await get_http_session()
    async with session.get(url, headers=THREADS_PAGE_HEADERS, allow_redirects=True) as response:
        response.raise_for_status()
        return await response.text()


class ThreadsMediaService:
    def __init__(
        self,
        output_dir: str,
        *,
        fetch_page_func: Callable[[str], Awaitable[str]] = fetch_threads_post_html,
        retry_async_operation_func: Callable[..., Awaitable[DownloadMetrics | None]],
    ) -> None:
        self._fetch_page = fetch_page_func
        self._retry_async_operation = retry_async_operation_func
        self._downloader = ResilientDownloader(
            output_dir,
            config=DownloadConfig(
                chunk_size=1024 * 1024,
                multipart_threshold=16 * 1024 * 1024,
                max_workers=6,
                retry_backoff=0.8,
            ),
            source="threads",
        )

    async def fetch_post(self, url: str) -> ThreadsPost | None:
        source_url = strip_threads_url(url)
        post_code = extract_threads_post_code(source_url)
        if not post_code:
            logging.warning("Unsupported Threads URL: url=%s", url)
            return None
        try:
            page = await self._fetch_page(source_url)
        except Exception as exc:
            logging.warning("Threads page request failed: post=%s error=%s", post_code, exc)
            return None

        post = parse_threads_post_html(page, post_code)
        if not post:
            logging.warning("Threads post has no extractable content: post=%s", post_code)
        return post

    async def download_media(
        self,
        url: str,
        filename: str,
        *,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        request_id: Optional[str] = None,
        size_hint: Optional[int] = None,
        on_queued=None,
        on_progress=None,
        on_retry=None,
    ) -> DownloadMetrics | None:
        async def _download_once() -> DownloadMetrics:
            return await self._downloader.download(
                url,
                filename,
                headers=THREADS_MEDIA_HEADERS,
                user_id=user_id,
                chat_id=chat_id,
                request_id=request_id,
                size_hint=size_hint,
                on_queued=on_queued,
                on_progress=on_progress,
            )

        try:
            return await self._retry_async_operation(
                _download_once,
                attempts=3,
                retry_on_exception=lambda exc: not isinstance(exc, (DownloadRateLimitError, DownloadQueueBusyError)),
                on_retry=on_retry,
            )
        except (DownloadRateLimitError, DownloadQueueBusyError):
            raise
        except DownloadError as exc:
            logging.error("Threads media download failed: url=%s error=%s", url, exc)
            return None
