import asyncio
from collections import OrderedDict
import html
import json
import os
import re
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import aiohttp
from aiogram import Router, F, types
from aiogram.types import FSInputFile

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, CHANNEL_ID
from handlers.media_delivery import send_cached_media_entries
from handlers.utils import (
    build_inline_album_result,
    build_request_id,
    build_queue_busy_text,
    build_rate_limit_text,
    build_start_deeplink_url,
    get_bot_url,
    handle_download_error,
    get_message_text,
    load_user_settings,
    make_status_text_progress_updater,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    retry_async_operation,
    safe_delete_message,
    safe_edit_text,
    safe_edit_inline_media,
    safe_edit_inline_text,
    safe_answer_inline_query,
    send_chat_action_if_needed,
    with_callback_logging,
    with_chosen_inline_logging,
    with_inline_query_logging,
    with_inline_send_logging,
    with_message_logging,
)
from log.logger import logger as logging
from app_context import bot, db, send_analytics
from utils.download_manager import (
    DownloadConfig,
    DownloadMetrics,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    DownloadTooLargeError,
    ResilientDownloader,
    log_download_metrics,
)
from utils.media_cache import build_media_cache_key
from services.inline_service_icons import get_inline_service_icon
from services.inline_album_links import create_inline_album_request
from services.inline_video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)
from utils.http_client import get_http_session

logging = logging.bind(service="twitter")

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)  # 1.5 GB
_TWITTER_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=6, connect=3, sock_read=6)
_TWITTER_SHORT_TIMEOUT = aiohttp.ClientTimeout(total=4, connect=2, sock_read=4)
_TWITTER_LINK_REGEX = r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)"
_SHORT_LINK_TTL_SECONDS = 30 * 60
_SCRAPE_TTL_SECONDS = 2 * 60
_CACHE_MAXSIZE = 1024

router = Router()

twitter_downloader = ResilientDownloader(
    OUTPUT_DIR,
    config=DownloadConfig(
        chunk_size=1024 * 1024,
        multipart_threshold=8 * 1024 * 1024,
        max_workers=8,             # more workers for faster parallel fetch
        head_timeout=4.0,
        probe_max_retries=1,
        stream_timeout=(5.0, 45.0),
    ),
    source="twitter",
)

_expanded_short_link_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_tweet_scrape_cache: "OrderedDict[str, tuple[float, dict[str, Any]]]" = OrderedDict()
_twitter_cache_lock = asyncio.Lock()


def _cache_get(
    cache: "OrderedDict[str, tuple[float, Any]]",
    key: str,
    ttl_seconds: float,
) -> Any | None:
    cached = cache.get(key)
    if not cached:
        return None
    created_at, value = cached
    if time.monotonic() - created_at > ttl_seconds:
        cache.pop(key, None)
        return None
    cache.move_to_end(key)
    return value


def _cache_put(cache: "OrderedDict[str, tuple[float, Any]]", key: str, value: Any) -> None:
    cache[key] = (time.monotonic(), value)
    cache.move_to_end(key)
    while len(cache) > _CACHE_MAXSIZE:
        cache.popitem(last=False)


def _normalize_twitter_media_kind(media_type: str | None) -> str | None:
    if media_type in {"image", "photo"}:
        return "photo"
    if media_type in {"video", "gif"}:
        return "video"
    return None


def _infer_twitter_media_kind_from_url(url: str | None) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    probe = url.lower().split("?", 1)[0]
    if any(probe.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return "photo"
    if any(probe.endswith(ext) for ext in (".mp4", ".mov", ".m4v", ".webm")):
        return "video"
    return None


def _extract_twitter_media_url(item: Any) -> str | None:
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


def _extract_twitter_media_items(tweet_media: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    raw_items = tweet_media.get("media_extended")
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            media_url = _extract_twitter_media_url(item)
            media_kind = _normalize_twitter_media_kind(item.get("type")) or _infer_twitter_media_kind_from_url(media_url)
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
            media_url = _extract_twitter_media_url(item)
            media_kind = None
            if isinstance(item, dict):
                media_kind = (
                    _normalize_twitter_media_kind(item.get("type"))
                    or _normalize_twitter_media_kind(item.get("media_type"))
                    or _normalize_twitter_media_kind(item.get("kind"))
                )
            media_kind = media_kind or _infer_twitter_media_kind_from_url(media_url)
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


def _build_twitter_media_cache_key(post_url: str, index: int, media_kind: str, total_items: int) -> str:
    if total_items == 1 and media_kind == "video":
        return post_url
    return build_media_cache_key(post_url, item_index=index, item_kind=media_kind)


def _get_twitter_media_preview_url(media: dict[str, Any], tweet_media: dict[str, Any]) -> Optional[str]:
    media_kind = _normalize_twitter_media_kind(media.get("type"))
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
                    nested = _get_twitter_media_preview_url(item, {})
                    if nested:
                        return nested
    return None


def _build_twitter_open_in_bot_result(
    *,
    result_id: str,
    deep_link: str,
    description: str,
) -> types.InlineQueryResultArticle:
    return types.InlineQueryResultArticle(
        id=result_id,
        title="X / Twitter Post",
        description=description,
        thumbnail_url=get_inline_service_icon("twitter"),
        input_message_content=types.InputTextMessageContent(
            message_text="Open this X / Twitter post in the bot if inline preview is limited.",
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[
                types.InlineKeyboardButton(
                    text=bm.inline_open_full_album_button(),
                    url=deep_link,
                )
            ]]
        ),
    )


async def _expand_short_link_cached(link: str) -> Optional[str]:
    async with _twitter_cache_lock:
        cached = _cache_get(_expanded_short_link_cache, link, _SHORT_LINK_TTL_SECONDS)
        if cached is not None:
            return cached

    session = await get_http_session()

    async def _fetch() -> str:
        async with session.get(
            f"https://{link}",
            allow_redirects=True,
            timeout=_TWITTER_SHORT_TIMEOUT,
        ) as response:
            return str(response.url)

    try:
        expanded = await retry_async_operation(
            _fetch,
            attempts=3,
            delay_seconds=0.25,
            retry_on_exception=lambda exc: isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError)),
        )
    except Exception as exc:
        logging.error("Failed to expand t.co URL: url=%s error=%s", link, exc)
        return None

    async with _twitter_cache_lock:
        _cache_put(_expanded_short_link_cache, link, expanded)
    return expanded


async def extract_tweet_ids_async(text: str) -> Optional[list[str]]:
    direct_tweet_ids = re.findall(
        r"(?:twitter|x)\.com/.{1,15}/(?:web|status(?:es)?)/([0-9]{1,20})",
        text,
    )
    if direct_tweet_ids:
        return list(dict.fromkeys(direct_tweet_ids))

    short_links = re.findall(r't\.co\/[a-zA-Z0-9]+', text)
    if not short_links:
        return None

    expanded_results = await asyncio.gather(*(_expand_short_link_cached(link) for link in short_links))
    expanded_links = [item for item in expanded_results if item]

    combined_text = '\n'.join([text, *expanded_links]) if expanded_links else text
    tweet_ids = re.findall(
        r"(?:twitter|x)\.com/.{1,15}/(?:web|status(?:es)?)/([0-9]{1,20})",
        combined_text,
    )
    return list(dict.fromkeys(tweet_ids)) if tweet_ids else None


async def scrape_media_async(tweet_id: str) -> dict:
    async with _twitter_cache_lock:
        cached = _cache_get(_tweet_scrape_cache, tweet_id, _SCRAPE_TTL_SECONDS)
        if cached is not None:
            return dict(cached)

    session = await get_http_session()

    async def _fetch_payload() -> dict[str, Any]:
        async with session.get(
            f"https://api.vxtwitter.com/Twitter/status/{tweet_id}",
            timeout=_TWITTER_HTTP_TIMEOUT,
        ) as response:
            response.raise_for_status()
            payload = await response.text()

        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            if match := re.search(r'<meta content="(.*?)" property="og:description" />', payload):
                error_message = html.unescape(match.group(1))
                logging.error("Twitter API returned error: tweet_id=%s error=%s", tweet_id, error_message)
                raise Exception(f"API returned error: {error_message}")
            logging.error("Failed to parse Twitter API response JSON: tweet_id=%s", tweet_id)
            raise

    try:
        data = await retry_async_operation(
            _fetch_payload,
            attempts=3,
            delay_seconds=0.4,
            retry_on_exception=lambda exc: isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError)),
        )
    except Exception as exc:
        logging.error("Failed to fetch media for tweet: tweet_id=%s error=%s", tweet_id, exc)
        raise

    async with _twitter_cache_lock:
        _cache_put(_tweet_scrape_cache, tweet_id, data)
    return dict(data)


async def _get_tweet_context(source_url: str) -> tuple[str, dict[str, Any]] | None:
    tweet_ids = await extract_tweet_ids_async(source_url)
    if not tweet_ids:
        return None
    tweet_id = tweet_ids[0]
    return tweet_id, await scrape_media_async(tweet_id)


async def _extract_single_inline_tweet_media_async(source_url: str) -> tuple[str, dict, dict] | None:
    context = await _get_tweet_context(source_url)
    if not context:
        return None

    _, tweet_media = context
    items = _extract_twitter_media_items(tweet_media)
    if len(items) != 1:
        return None

    media = items[0]
    media_kind = _normalize_twitter_media_kind(media.get("type"))
    if media_kind:
        return media_kind, tweet_media, media
    return None


async def _collect_media_entries(
    tweet_id,
    tweet_media,
    *,
    user_id: Optional[int] = None,
    request_id: Optional[str] = None,
):
    entries: list[dict[str, Any] | None] = []
    download_tasks = []
    media_meta: list[tuple[int, str, str, str, str]] = []
    media_items = _extract_twitter_media_items(tweet_media)
    total_items = len(media_items)
    post_url = tweet_media.get("tweetURL") or f"https://x.com/i/status/{tweet_id}"

    for index, media in enumerate(media_items):
        media_url = media.get('url')
        media_kind = _normalize_twitter_media_kind(media.get("type"))
        if not media_url or not media_kind:
            continue

        cache_key = _build_twitter_media_cache_key(post_url, index, media_kind, total_items)
        cached_file_id = await db.get_file_id(cache_key)
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
            twitter_downloader.download(
                media_url,
                file_name,
                skip_if_exists=True,
                user_id=user_id,
                request_id=request_id,
                max_size_bytes=MAX_FILE_SIZE,
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
                os.path.join(OUTPUT_DIR, file_path),
                media_kind,
                result,
            )
            continue
        resolved_path = (
            result.path if isinstance(result, DownloadMetrics) else os.path.join(OUTPUT_DIR, file_path)
        )

        log_download_metrics("twitter_media", result if isinstance(result, DownloadMetrics) else DownloadMetrics(
            url=media_url,
            path=resolved_path,
            size=os.path.getsize(resolved_path) if os.path.exists(resolved_path) else 0,
            elapsed=0.0,
            used_multipart=isinstance(result, DownloadMetrics) and result.used_multipart,
            resumed=isinstance(result, DownloadMetrics) and result.resumed,
        ))

        entries[index] = {
            "index": index,
            "kind": media_kind,
            "cache_key": cache_key,
            "file_id": None,
            "path": resolved_path,
            "cached": False,
        }

    return [entry for entry in entries if entry is not None]


async def _collect_media_files(
    tweet_id,
    tweet_media,
    *,
    user_id: Optional[int] = None,
    request_id: Optional[str] = None,
):
    entries = await _collect_media_entries(
        tweet_id,
        tweet_media,
        user_id=user_id,
        request_id=request_id,
    )
    photos = [str(entry["path"]) for entry in entries if entry["kind"] == "photo" and entry["path"]]
    videos = [str(entry["path"]) for entry in entries if entry["kind"] == "video" and entry["path"]]
    return photos, videos


async def _send_tweet_media_entries(message, entries, caption, keyboard):
    await send_cached_media_entries(
        message,
        entries,
        db_service=db,
        caption=caption,
        reply_markup=keyboard,
    )


async def _cleanup_tweet_dir(tweet_dir):
    try:
        for root, _dirs, files in os.walk(tweet_dir):
            for file in files:
                await remove_file(os.path.join(root, file))
        await asyncio.to_thread(os.rmdir, tweet_dir)
        logging.debug("Cleaned tweet temp directory: path=%s", tweet_dir)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logging.debug("Failed to cleanup tweet directory: path=%s error=%s", tweet_dir, exc)


async def reply_media(message, tweet_id, tweet_media, bot_url, business_id, user_settings):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="twitter")
    logging.info(
        "Processing tweet media: user_id=%s tweet_id=%s",
        message.from_user.id,
        tweet_id,
    )

    tweet_dir = os.path.join(OUTPUT_DIR, str(tweet_id))
    os.makedirs(tweet_dir, exist_ok=True)
    logging.debug("Tweet temp directory ready: path=%s", tweet_dir)

    post_url = tweet_media['tweetURL']
    post_caption = tweet_media["text"]
    likes = tweet_media['likes']
    comments = tweet_media['replies']
    retweets = tweet_media['retweets']
    status_message: Optional[types.Message] = None
    try:
        if business_id is None:
            status_message = await message.answer(bm.downloading_video_status())

        media_entries = await _collect_media_entries(
            tweet_id,
            tweet_media,
            user_id=message.from_user.id,
            request_id=f"twitter:{message.chat.id}:{message.message_id}:{tweet_id}",
        )
        logging.info(
            "Tweet media fetched: tweet_id=%s photos=%s videos=%s",
            tweet_id,
            sum(1 for item in media_entries if item["kind"] == "photo"),
            sum(1 for item in media_entries if item["kind"] == "video"),
        )

        caption_media = bm.captions("on", post_caption, bot_url)
        caption_text = bm.captions("on", post_caption, bot_url, limit=4096)
        keyboard = kb.return_video_info_keyboard(None, likes, comments, retweets, None, post_url, user_settings)

        if media_entries:
            await safe_edit_text(status_message, bm.uploading_status())
            await _send_tweet_media_entries(message, media_entries, caption_media, keyboard)
        else:
            await message.answer(caption_text, reply_markup=keyboard, parse_mode="HTML")

        logging.info(
            "Tweet media delivered: user_id=%s tweet_id=%s",
            message.from_user.id,
            tweet_id,
        )
    except DownloadRateLimitError as exc:
        if business_id is None:
            await message.reply(build_rate_limit_text(exc.retry_after))
        else:
            await handle_download_error(message, business_id=business_id)
    except DownloadQueueBusyError as exc:
        if business_id is None:
            await message.reply(build_queue_busy_text(exc.position))
        else:
            await handle_download_error(message, business_id=business_id)
    except DownloadTooLargeError:
        await message.reply(bm.video_too_large())
    except Exception as e:
        logging.exception(
            "Error processing tweet media: tweet_id=%s user_id=%s error=%s",
            tweet_id,
            message.from_user.id,
            e,
        )
        await react_to_message(message, "👎", business_id=business_id)
        await message.reply(bm.something_went_wrong())
    finally:
        await safe_delete_message(status_message)
        asyncio.create_task(_cleanup_tweet_dir(tweet_dir))


async def _prefetch_tweet_payloads(tweet_ids: list[str]) -> list[tuple[str, dict[str, Any] | BaseException]]:
    semaphore = asyncio.Semaphore(3)
    results: list[tuple[int, str, dict[str, Any] | BaseException]] = []

    async def _fetch(index: int, tweet_id: str) -> None:
        async with semaphore:
            try:
                payload: dict[str, Any] | BaseException = await scrape_media_async(tweet_id)
            except Exception as exc:  # pragma: no cover - defensive path
                payload = exc
            results.append((index, tweet_id, payload))

    await asyncio.gather(*(_fetch(index, tweet_id) for index, tweet_id in enumerate(tweet_ids)))
    results.sort(key=lambda item: item[0])
    return [(tweet_id, payload) for _, tweet_id, payload in results]


@router.message(
    F.text.regexp(_TWITTER_LINK_REGEX, mode="search")
    | F.caption.regexp(_TWITTER_LINK_REGEX, mode="search")
)
@router.business_message(
    F.text.regexp(_TWITTER_LINK_REGEX, mode="search")
    | F.caption.regexp(_TWITTER_LINK_REGEX, mode="search")
)
@with_message_logging("twitter", "message")
async def handle_tweet_links(message, direct_url: Optional[str] = None):
    business_id = message.business_connection_id
    text = direct_url or get_message_text(message)

    logging.info(
        "Twitter request received: user_id=%s username=%s business_id=%s text=%s",
        message.from_user.id,
        message.from_user.username,
        business_id,
        text,
    )

    await react_to_message(message, "👾", business_id=business_id)

    bot_url = await get_bot_url(bot)
    user_settings = await load_user_settings(db, message)

    try:
        tweet_ids = await extract_tweet_ids_async(text)
        if tweet_ids:
            logging.info("Twitter links parsed: user_id=%s count=%s", message.from_user.id, len(tweet_ids))
            await send_chat_action_if_needed(bot, message.chat.id, "typing", business_id)
            prefetched_payloads = await _prefetch_tweet_payloads(tweet_ids)

            for tweet_id, media in prefetched_payloads:
                try:
                    if isinstance(media, BaseException):
                        raise media
                    await reply_media(message, tweet_id, media, bot_url, business_id, user_settings)
                    await maybe_delete_user_message(message, user_settings.get("delete_message"))
                except Exception as e:
                    logging.exception("Failed to process tweet: tweet_id=%s error=%s", tweet_id, e)
                    await message.reply(bm.something_went_wrong())
        else:
            logging.info("No tweet links found: user_id=%s", message.from_user.id)
            await react_to_message(message, "👎", business_id=business_id)
            await message.reply(bm.nothing_found())
    except Exception as e:
        logging.exception("Error handling tweet links: user_id=%s error=%s", message.from_user.id, e)
        await message.reply(bm.something_went_wrong())


@router.inline_query(F.query.regexp(_TWITTER_LINK_REGEX, mode="search"))
@with_inline_query_logging("twitter", "inline_query")
async def inline_twitter_query(query: types.InlineQuery):
    try:
        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_twitter_media")
        match = re.search(_TWITTER_LINK_REGEX, query.query or "")
        if not match:
            await query.answer([], cache_time=1, is_personal=True)
            return

        source_url = match.group(0)
        user_settings = await db.user_settings(query.from_user.id)
        bot_url = await get_bot_url(bot)
        album_token = create_inline_album_request(query.from_user.id, "twitter", source_url)
        album_deep_link = build_start_deeplink_url(bot_url, f"album_{album_token}")
        context = await _get_tweet_context(source_url)
        if not context:
            await safe_answer_inline_query(
                query,
                [
                    _build_twitter_open_in_bot_result(
                        result_id="twitter_open_in_bot",
                        deep_link=album_deep_link,
                        description="Open this post in the bot.",
                    )
                ],
                cache_time=10,
                is_personal=True,
            )
            return

        tweet_id, tweet_media = context
        media_items = _extract_twitter_media_items(tweet_media)
        if len(media_items) > 1:
            preview_file_id = None
            first_media = media_items[0]
            first_media_kind = _normalize_twitter_media_kind(first_media.get("type"))
            if first_media_kind == "photo" and CHANNEL_ID:
                preview_cache_key = _build_twitter_media_cache_key(source_url, 0, "photo", len(media_items))
                preview_file_id = await db.get_file_id(preview_cache_key)
                if not preview_file_id:
                    try:
                        sent = await bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=first_media["url"],
                            caption="X / Twitter Album Preview",
                        )
                        if sent.photo:
                            preview_file_id = sent.photo[-1].file_id
                            await db.add_file(preview_cache_key, preview_file_id, "photo")
                    except Exception as exc:
                        logging.warning(
                            "Failed to cache Twitter album preview photo: url=%s error=%s",
                            source_url,
                            exc,
                        )
            preview_url = _get_twitter_media_preview_url(media_items[0], tweet_media) or next(
                (
                    _get_twitter_media_preview_url(item, tweet_media)
                    for item in media_items
                    if _get_twitter_media_preview_url(item, tweet_media)
                ),
                None,
            )
            results = [
                build_inline_album_result(
                    result_id=f"twitter_album_{tweet_id}",
                    service_name="Twitter",
                    deep_link=album_deep_link,
                    message_text=bm.captions(
                        user_settings["captions"],
                        tweet_media.get("text"),
                        bot_url,
                    ),
                    preview_file_id=preview_file_id,
                    preview_url=preview_url,
                    thumbnail_url=preview_url or get_inline_service_icon("twitter"),
                )
            ]
            await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
            return

        if len(media_items) != 1:
            await safe_answer_inline_query(
                query,
                [
                    _build_twitter_open_in_bot_result(
                        result_id=f"twitter_open_{tweet_id}",
                        deep_link=album_deep_link,
                        description="Inline preview is limited for this post. Open it in the bot.",
                    )
                ],
                cache_time=10,
                is_personal=True,
            )
            return

        media = media_items[0]
        media_kind = _normalize_twitter_media_kind(media.get("type"))
        if not media_kind:
            await safe_answer_inline_query(
                query,
                [
                    _build_twitter_open_in_bot_result(
                        result_id=f"twitter_open_unknown_{tweet_id}",
                        deep_link=album_deep_link,
                        description="Open this post in the bot.",
                    )
                ],
                cache_time=10,
                is_personal=True,
            )
            return
        token = create_inline_video_request("twitter", source_url, query.from_user.id, user_settings)
        action_text = "Send photo inline" if media_kind == "photo" else "Send video inline"
        prompt_text = (
            bm.inline_send_video_prompt("Twitter")
            if media_kind == "video"
            else "Twitter photo is being prepared...\nIf it does not start automatically, tap the button below."
        )
        preview_url = _get_twitter_media_preview_url(media, tweet_media) or get_inline_service_icon("twitter")
        results = [
            types.InlineQueryResultArticle(
                id=f"twitter_inline:{token}",
                title="X / Twitter Post",
                description=tweet_media.get("text") or f"Press the button to send this {media_kind} inline.",
                thumbnail_url=preview_url,
                input_message_content=types.InputTextMessageContent(message_text=prompt_text),
                reply_markup=kb.inline_send_media_keyboard(action_text, f"inline:twitter:{token}"),
            )
        ]
        await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
    except Exception as exc:
        logging.exception(
            "Error processing Twitter inline query: user_id=%s query=%s error=%s",
            query.from_user.id,
            query.query,
            exc,
        )
        await query.answer([], cache_time=1, is_personal=True)


@with_inline_send_logging("twitter", "inline_send")
async def _send_inline_twitter_media(
    *,
    token: str,
    inline_message_id: str,
    actor_name: str,
    actor_user_id: int,
    request_event_id: str,
    duplicate_handler: str,
) -> None:
    request = claim_inline_video_request_for_send(
        token,
        duplicate_handler=duplicate_handler,
        actor_user_id=actor_user_id,
    )
    if request is None:
        return

    download_path: Optional[str] = None

    async def _edit_inline_status(text: str, *, with_retry_button: bool = False, media_kind: str = "video") -> None:
        button_text = "Send photo inline" if media_kind == "photo" else "Send video inline"
        reply_markup = (
            kb.inline_send_media_keyboard(button_text, f"inline:twitter:{token}")
            if with_retry_button
            else None
        )
        await safe_edit_inline_text(bot, inline_message_id, text, reply_markup=reply_markup)

    try:
        context = await _get_tweet_context(request.source_url)
        if not context:
            complete_inline_video_request(token)
            await _edit_inline_status("Only single photo or single video posts are supported inline.")
            return

        _, tweet_media = context
        media_items = _extract_twitter_media_items(tweet_media)
        if len(media_items) != 1:
            complete_inline_video_request(token)
            await _edit_inline_status("Only single photo or single video posts are supported inline.")
            return

        media = media_items[0]
        media_kind = _normalize_twitter_media_kind(media.get("type"))
        if not media_kind:
            complete_inline_video_request(token)
            await _edit_inline_status("Only single photo or single video posts are supported inline.")
            return
        post_url = tweet_media["tweetURL"]
        post_caption = tweet_media.get("text")
        likes = tweet_media.get("likes")
        comments = tweet_media.get("replies")
        retweets = tweet_media.get("retweets")

        if media_kind == "photo":
            cache_key = _build_twitter_media_cache_key(post_url, 0, "photo", 1)
            db_file_id = await db.get_file_id(cache_key)
            if not db_file_id:
                if not CHANNEL_ID:
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return

                await _edit_inline_status(bm.uploading_status(), media_kind="photo")
                sent = await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=media["url"],
                    caption=f"X / Twitter Photo from {actor_name}",
                )
                if not sent.photo:
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return
                db_file_id = sent.photo[-1].file_id
                await db.add_file(cache_key, db_file_id, "photo")
            else:
                await _edit_inline_status(bm.uploading_status(), media_kind="photo")

            edited = await safe_edit_inline_media(
                bot,
                inline_message_id,
                types.InputMediaPhoto(
                    media=db_file_id,
                    caption=bm.captions(request.user_settings["captions"], post_caption, await get_bot_url(bot)),
                    parse_mode="HTML",
                ),
                reply_markup=kb.return_video_info_keyboard(
                    None,
                    likes,
                    comments,
                    retweets,
                    None,
                    post_url,
                    request.user_settings,
                ),
            )
            if edited:
                complete_inline_video_request(token)
                return
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
            return

        cache_key = post_url
        db_file_id = await db.get_file_id(cache_key)
        if not db_file_id:
            if not CHANNEL_ID:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            file_name = os.path.join(str(tweet_media["conversationID"]), os.path.basename(urlsplit(media["url"]).path))
            await _edit_inline_status(bm.downloading_video_status())

            on_progress = make_status_text_progress_updater("X / Twitter video", _edit_inline_status)

            metrics = await twitter_downloader.download(
                media["url"],
                file_name,
                skip_if_exists=True,
                user_id=request.owner_user_id,
                request_id=f"twitter_inline:{request.owner_user_id}:{request_event_id}:{tweet_media['conversationID']}",
                max_size_bytes=MAX_FILE_SIZE,
                on_progress=on_progress,
            )
            if not metrics:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            log_download_metrics("twitter_inline", metrics)
            download_path = metrics.path
            if metrics.size >= MAX_FILE_SIZE:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.video_too_large())
                return

            await _edit_inline_status(bm.uploading_status())
            sent = await bot.send_video(
                chat_id=CHANNEL_ID,
                video=FSInputFile(download_path),
                caption=f"X / Twitter Video from {actor_name}",
            )
            db_file_id = sent.video.file_id
            await db.add_file(cache_key, db_file_id, "video")
        else:
            await _edit_inline_status(bm.uploading_status())

        edited = await safe_edit_inline_media(
            bot,
            inline_message_id,
            types.InputMediaVideo(
                media=db_file_id,
                caption=bm.captions(request.user_settings["captions"], post_caption, await get_bot_url(bot)),
                parse_mode="HTML",
            ),
            reply_markup=kb.return_video_info_keyboard(
                None,
                likes,
                comments,
                retweets,
                None,
                post_url,
                request.user_settings,
            ),
        )
        if edited:
            complete_inline_video_request(token)
            return

        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    except Exception as exc:
        logging.exception(
            "Error sending Twitter inline media: inline_message_id=%s token=%s error=%s",
            inline_message_id,
            token,
            exc,
        )
        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    finally:
        if download_path:
            await remove_file(download_path)


@router.chosen_inline_result(F.result_id.startswith("twitter_inline:"))
@with_chosen_inline_logging("twitter", "chosen_inline")
async def chosen_inline_twitter_result(result: types.ChosenInlineResult):
    if not result.inline_message_id:
        logging.warning("Chosen inline Twitter result is missing inline_message_id")
        return

    token = result.result_id.removeprefix("twitter_inline:")
    await _send_inline_twitter_media(
        token=token,
        inline_message_id=result.inline_message_id,
        actor_name=result.from_user.full_name,
        actor_user_id=getattr(result.from_user, "id", None),
        request_event_id=result.result_id,
        duplicate_handler="chosen",
    )


@router.callback_query(F.data.startswith("inline:twitter:"))
@with_callback_logging("twitter", "inline_callback")
async def send_inline_twitter_media_callback(call: types.CallbackQuery):
    if not call.inline_message_id:
        await call.answer("This button works only in inline mode.", show_alert=True)
        return

    token = call.data.removeprefix("inline:twitter:")
    await call.answer()
    try:
        await _send_inline_twitter_media(
            token=token,
            inline_message_id=call.inline_message_id,
            actor_name=call.from_user.full_name,
            actor_user_id=call.from_user.id,
            request_event_id=str(call.id),
            duplicate_handler="callback",
        )
    except PermissionError:
        await call.answer(bm.something_went_wrong(), show_alert=True)
        return
    except ValueError as exc:
        if str(exc) == "already_processing":
            await call.answer(bm.inline_video_already_processing(), show_alert=False)
            return
        if str(exc) == "already_completed":
            await call.answer(bm.inline_video_already_sent(), show_alert=False)
            return
