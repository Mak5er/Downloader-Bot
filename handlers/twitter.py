import asyncio
from collections import OrderedDict
import html
import json
import os
import re
import time
from typing import Any, Optional

import aiohttp
from aiogram import Router, F, types

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, CHANNEL_ID
from handlers.deps import build_handler_dependencies
from services.media.orchestration import handle_download_backpressure, run_media_collection_flow
from services.media.delivery import send_cached_media_entries
from handlers.twitter_inline import handle_twitter_inline_query, send_inline_twitter_media
from services.platforms.twitter_media import (
    build_twitter_media_cache_key as _build_twitter_media_cache_key,
    collect_media_entries as _collect_media_entries_impl,
    collect_media_files as _collect_media_files_impl,
    extract_twitter_media_items as _extract_twitter_media_items,
    get_twitter_media_preview_url as _get_twitter_media_preview_url,
    normalize_twitter_media_kind as _normalize_twitter_media_kind,
)
from handlers.utils import (
    build_queue_busy_text,
    build_rate_limit_text,
    get_bot_url,
    handle_download_error,
    get_message_text,
    load_user_settings,
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
from log.logger import logger as logging, summarize_text_for_log, summarize_url_for_log
from app_context import bot, db, send_analytics
from services.inline.video_requests import create_inline_video_request
from utils.download_manager import (
    DownloadConfig,
    ResilientDownloader,
)
from services.inline.service_icons import get_inline_service_icon
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

__all__ = [
    "create_inline_video_request",
    "get_inline_service_icon",
]

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
        logging.error("Failed to expand t.co URL: url=%s error=%s", summarize_url_for_log(link), exc)
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
    return await _collect_media_entries_impl(
        tweet_id,
        tweet_media,
        db_service=db,
        downloader=twitter_downloader,
        output_dir=OUTPUT_DIR,
        max_file_size=MAX_FILE_SIZE,
        user_id=user_id,
        request_id=request_id,
    )


async def _collect_media_files(
    tweet_id,
    tweet_media,
    *,
    user_id: Optional[int] = None,
    request_id: Optional[str] = None,
):
    return await _collect_media_files_impl(
        tweet_id,
        tweet_media,
        db_service=db,
        downloader=twitter_downloader,
        output_dir=OUTPUT_DIR,
        max_file_size=MAX_FILE_SIZE,
        user_id=user_id,
        request_id=request_id,
    )


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

        caption_media = bm.captions("on", post_caption, bot_url)
        caption_text = bm.captions("on", post_caption, bot_url, limit=4096)
        keyboard = kb.return_video_info_keyboard(None, likes, comments, retweets, None, post_url, user_settings)

        async def _edit_status(text: str) -> None:
            await safe_edit_text(status_message, text)

        async def _fetch_entries():
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
            return media_entries

        async def _handle_backpressure(exc: Exception) -> None:
            await handle_download_backpressure(
                exc,
                business_id=business_id,
                on_rate_limit_reply=lambda retry_after: message.reply(build_rate_limit_text(retry_after)),
                on_queue_busy_reply=lambda position: message.reply(build_queue_busy_text(position)),
                on_business_error=lambda: handle_download_error(message, business_id=business_id),
            )

        await run_media_collection_flow(
            update_status=_edit_status,
            upload_status_text=bm.uploading_status(),
            fetch_entries=_fetch_entries,
            send_entries=lambda media_entries: _send_tweet_media_entries(
                message,
                media_entries,
                caption_media,
                keyboard,
            ),
            send_empty=lambda: message.answer(caption_text, reply_markup=keyboard, parse_mode="HTML"),
            delete_status_message=lambda: safe_delete_message(status_message),
            cleanup=lambda: _cleanup_tweet_dir(tweet_dir),
            on_rate_limit=_handle_backpressure,
            on_queue_busy=_handle_backpressure,
            on_too_large=lambda _exc: message.reply(bm.video_too_large()),
        )

        logging.info(
            "Tweet media delivered: user_id=%s tweet_id=%s",
            message.from_user.id,
            tweet_id,
        )
    except Exception as e:
        logging.exception(
            "Error processing tweet media: tweet_id=%s user_id=%s error=%s",
            tweet_id,
            message.from_user.id,
            e,
        )
        await react_to_message(message, "👎", business_id=business_id)
        await message.reply(bm.something_went_wrong())
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
        summarize_text_for_log(text),
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
    deps = build_handler_dependencies(bot=bot, db=db, send_analytics=send_analytics)
    await handle_twitter_inline_query(
        query,
        deps=deps,
        twitter_link_regex=_TWITTER_LINK_REGEX,
        channel_id=CHANNEL_ID,
        get_tweet_context_fn=_get_tweet_context,
        extract_twitter_media_items_fn=_extract_twitter_media_items,
        normalize_twitter_media_kind_fn=_normalize_twitter_media_kind,
        build_twitter_media_cache_key_fn=_build_twitter_media_cache_key,
        get_twitter_media_preview_url_fn=_get_twitter_media_preview_url,
        build_twitter_open_in_bot_result_fn=_build_twitter_open_in_bot_result,
        get_bot_url_fn=get_bot_url,
        safe_answer_inline_query_fn=safe_answer_inline_query,
    )


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
    deps = build_handler_dependencies(bot=bot, db=db, send_analytics=send_analytics)
    await send_inline_twitter_media(
        token=token,
        inline_message_id=inline_message_id,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        request_event_id=request_event_id,
        duplicate_handler=duplicate_handler,
        deps=deps,
        channel_id=CHANNEL_ID,
        max_file_size=MAX_FILE_SIZE,
        twitter_downloader=twitter_downloader,
        get_tweet_context_fn=_get_tweet_context,
        extract_twitter_media_items_fn=_extract_twitter_media_items,
        normalize_twitter_media_kind_fn=_normalize_twitter_media_kind,
        build_twitter_media_cache_key_fn=_build_twitter_media_cache_key,
        get_bot_url_fn=get_bot_url,
        safe_edit_inline_media_fn=safe_edit_inline_media,
        safe_edit_inline_text_fn=safe_edit_inline_text,
    )


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
