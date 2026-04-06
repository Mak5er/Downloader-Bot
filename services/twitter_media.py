import asyncio
import os
from typing import Any, Optional
from urllib.parse import urlsplit

from log.logger import logger as logging
from utils.download_manager import (
    DownloadMetrics,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    DownloadTooLargeError,
    log_download_metrics,
)
from utils.media_cache import build_media_cache_key

logging = logging.bind(service="twitter_media")


def normalize_twitter_media_kind(media_type: str | None) -> str | None:
    if media_type in {"image", "photo"}:
        return "photo"
    if media_type in {"video", "gif"}:
        return "video"
    return None


def infer_twitter_media_kind_from_url(url: str | None) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    probe = url.lower().split("?", 1)[0]
    if any(probe.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return "photo"
    if any(probe.endswith(ext) for ext in (".mp4", ".mov", ".m4v", ".webm")):
        return "video"
    return None


def extract_twitter_media_url(item: Any) -> str | None:
    if isinstance(item, str) and item:
        return item
    if not isinstance(item, dict):
        return None
    for key in (
        "url",
        "media_url",
        "mediaUrl",
        "image",
        "image_url",
        "imageUrl",
        "video_url",
        "videoUrl",
        "src",
    ):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def extract_twitter_media_items(tweet_media: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    raw_items = tweet_media.get("media_extended")
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            media_url = extract_twitter_media_url(item)
            media_kind = normalize_twitter_media_kind(item.get("type")) or infer_twitter_media_kind_from_url(media_url)
            if media_url and media_kind:
                normalized = dict(item)
                normalized["url"] = media_url
                normalized["type"] = media_kind
                items.append(normalized)
    if items:
        return items

    candidate_lists = (
        tweet_media.get("mediaURLs"),
        tweet_media.get("media_urls"),
        tweet_media.get("images"),
        tweet_media.get("videos"),
        tweet_media.get("videoURLs"),
        tweet_media.get("video_urls"),
    )
    for candidate in candidate_lists:
        if not isinstance(candidate, list):
            continue
        normalized_items: list[dict[str, Any]] = []
        for item in candidate:
            media_url = extract_twitter_media_url(item)
            media_kind = None
            if isinstance(item, dict):
                media_kind = (
                    normalize_twitter_media_kind(item.get("type"))
                    or normalize_twitter_media_kind(item.get("media_type"))
                    or normalize_twitter_media_kind(item.get("kind"))
                )
            media_kind = media_kind or infer_twitter_media_kind_from_url(media_url)
            if not media_kind and candidate in (tweet_media.get("mediaURLs"), tweet_media.get("media_urls"), tweet_media.get("images")):
                media_kind = "photo"
            if media_url and media_kind:
                normalized_items.append(
                    item if isinstance(item, dict) and item.get("url") == media_url and item.get("type") == media_kind
                    else {
                        **(item if isinstance(item, dict) else {}),
                        "url": media_url,
                        "type": media_kind,
                    }
                )
        if normalized_items:
            return normalized_items
    return []


def build_twitter_media_cache_key(post_url: str, index: int, media_kind: str, total_items: int) -> str:
    if total_items == 1 and media_kind == "video":
        return post_url
    return build_media_cache_key(post_url, item_index=index, item_kind=media_kind)


def get_twitter_media_preview_url(media: dict[str, Any], tweet_media: dict[str, Any]) -> Optional[str]:
    media_kind = normalize_twitter_media_kind(media.get("type"))
    if media_kind == "photo":
        media_url = media.get("url")
        if isinstance(media_url, str) and media_url:
            return media_url

    for key in (
        "thumb",
        "thumbnail",
        "thumbnail_url",
        "thumbnailUrl",
        "poster",
        "poster_url",
        "posterUrl",
        "preview",
        "preview_url",
        "previewUrl",
        "image",
        "image_url",
        "imageUrl",
    ):
        value = media.get(key)
        if isinstance(value, str) and value:
            return value

    for key in ("mediaURLs", "media_urls", "images", "thumbnails"):
        value = tweet_media.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item:
                    return item
                if isinstance(item, dict):
                    nested = get_twitter_media_preview_url(item, {})
                    if nested:
                        return nested
    return None


async def collect_media_entries(
    tweet_id: str,
    tweet_media: dict[str, Any],
    *,
    db_service: Any,
    downloader: Any,
    output_dir: str,
    max_file_size: int,
    user_id: Optional[int] = None,
    request_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any] | None] = []
    download_tasks = []
    media_meta: list[tuple[int, str, str, str, str]] = []
    media_items = extract_twitter_media_items(tweet_media)
    total_items = len(media_items)
    post_url = tweet_media.get("tweetURL") or f"https://x.com/i/status/{tweet_id}"

    for index, media in enumerate(media_items):
        media_url = media.get("url")
        media_kind = normalize_twitter_media_kind(media.get("type"))
        if not media_url or not media_kind:
            continue

        cache_key = build_twitter_media_cache_key(post_url, index, media_kind, total_items)
        cached_file_id = await db_service.get_file_id(cache_key)
        if cached_file_id:
            entries.append(
                {
                    "index": index,
                    "kind": media_kind,
                    "cache_key": cache_key,
                    "file_id": cached_file_id,
                    "path": None,
                    "cached": True,
                }
            )
            continue

        file_name = os.path.join(str(tweet_id), os.path.basename(urlsplit(media_url).path))
        entries.append(None)
        logging.debug(
            "Queueing tweet media download: tweet_id=%s type=%s url=%s",
            tweet_id,
            media_kind,
            media_url,
        )
        download_tasks.append(
            downloader.download(
                media_url,
                file_name,
                skip_if_exists=True,
                user_id=user_id,
                request_id=request_id,
                max_size_bytes=max_file_size,
            )
        )
        media_meta.append((index, media_kind, file_name, media_url, cache_key))

    if not download_tasks:
        return [entry for entry in entries if entry is not None]

    results = await asyncio.gather(*download_tasks, return_exceptions=True)
    for (index, media_kind, file_path, media_url, cache_key), result in zip(media_meta, results):
        if isinstance(result, (DownloadRateLimitError, DownloadQueueBusyError, DownloadTooLargeError)):
            raise result
        if isinstance(result, Exception):
            logging.error(
                "Failed to download tweet media chunk: tweet_id=%s path=%s type=%s error=%s",
                tweet_id,
                os.path.join(output_dir, file_path),
                media_kind,
                result,
            )
            continue

        resolved_path = (
            result.path if isinstance(result, DownloadMetrics) else os.path.join(output_dir, file_path)
        )
        log_download_metrics(
            "twitter_media",
            result if isinstance(result, DownloadMetrics) else DownloadMetrics(
                url=media_url,
                path=resolved_path,
                size=os.path.getsize(resolved_path) if os.path.exists(resolved_path) else 0,
                elapsed=0.0,
                used_multipart=isinstance(result, DownloadMetrics) and result.used_multipart,
                resumed=isinstance(result, DownloadMetrics) and result.resumed,
            ),
        )
        entries[index] = {
            "index": index,
            "kind": media_kind,
            "cache_key": cache_key,
            "file_id": None,
            "path": resolved_path,
            "cached": False,
        }

    return [entry for entry in entries if entry is not None]


async def collect_media_files(
    tweet_id: str,
    tweet_media: dict[str, Any],
    *,
    db_service: Any,
    downloader: Any,
    output_dir: str,
    max_file_size: int,
    user_id: Optional[int] = None,
    request_id: Optional[str] = None,
) -> tuple[list[str], list[str]]:
    entries = await collect_media_entries(
        tweet_id,
        tweet_media,
        db_service=db_service,
        downloader=downloader,
        output_dir=output_dir,
        max_file_size=max_file_size,
        user_id=user_id,
        request_id=request_id,
    )
    photos = [str(entry["path"]) for entry in entries if entry["kind"] == "photo" and entry["path"]]
    videos = [str(entry["path"]) for entry in entries if entry["kind"] == "video" and entry["path"]]
    return photos, videos
