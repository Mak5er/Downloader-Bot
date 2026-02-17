import asyncio
import datetime
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse

from aiogram import F, Router, types
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder

import keyboards as kb
import messages as bm
from config import CHANNEL_ID, COBALT_API_KEY, COBALT_API_URL, OUTPUT_DIR
from handlers.user import update_info
from handlers.utils import (
    build_progress_status,
    build_queue_busy_text,
    build_rate_limit_text,
    build_start_deeplink_url,
    get_bot_url,
    get_message_text,
    handle_download_error,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    retry_async_operation,
    safe_delete_message,
    safe_edit_text,
    send_chat_action_if_needed,
    resolve_settings_target_id,
)
from log.logger import logger as logging
from main import bot, db, send_analytics
from utils.cobalt_client import fetch_cobalt_data
from utils.cobalt_media import classify_cobalt_media_type
from utils.download_manager import (
    DownloadConfig,
    DownloadError,
    DownloadMetrics,
    DownloadProgress,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    ResilientDownloader,
    log_download_metrics,
)
from services.inline_album_links import create_inline_album_request

router = Router()

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)
PINTEREST_URL_REGEX = r"(https?://(?:[\w-]+\.)?pinterest\.[\w.]+/\S+|https?://pin\.it/\S+)"


def strip_pinterest_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


def _derive_description(data: dict) -> str:
    output = data.get("output")
    if isinstance(output, dict):
        metadata = output.get("metadata")
        if isinstance(metadata, dict):
            title = metadata.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()
    description = data.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    return ""


@dataclass
class PinterestMedia:
    url: str
    type: str
    thumb: Optional[str] = None


@dataclass
class PinterestPost:
    id: str
    description: str
    media_list: list[PinterestMedia]


def parse_pinterest_post(data: dict) -> Optional[PinterestPost]:
    if not isinstance(data, dict):
        return None

    status = data.get("status")
    if status == "error":
        error_obj = data.get("error") or {}
        logging.error(
            "Cobalt Pinterest API error: code=%s context=%s",
            error_obj.get("code") if isinstance(error_obj, dict) else None,
            error_obj.get("context") if isinstance(error_obj, dict) else None,
        )
        return None

    if not status:
        if "url" in data:
            status = "tunnel"
        elif "picker" in data:
            status = "picker"

    media_list: list[PinterestMedia] = []

    if status in {"tunnel", "redirect"}:
        media_url = data.get("url")
        if isinstance(media_url, str) and media_url:
            media_list.append(
                PinterestMedia(
                    url=media_url,
                    type=classify_cobalt_media_type(
                        media_url,
                        filename=data.get("filename"),
                    ),
                )
            )
    elif status == "picker":
        for item in data.get("picker") or []:
            if not isinstance(item, dict):
                continue
            media_url = item.get("url")
            if not isinstance(media_url, str) or not media_url:
                continue
            media_list.append(
                PinterestMedia(
                    url=media_url,
                    type=classify_cobalt_media_type(
                        media_url,
                        declared_type=item.get("type"),
                    ),
                    thumb=item.get("thumb") if isinstance(item.get("thumb"), str) else None,
                )
            )
    elif status == "local-processing":
        tunnels = data.get("tunnel") or []
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        for media_url in tunnels:
            if not isinstance(media_url, str) or not media_url:
                continue
            media_list.append(
                PinterestMedia(
                    url=media_url,
                    type=classify_cobalt_media_type(
                        media_url,
                        declared_type=data.get("type"),
                        filename=output.get("filename"),
                        mime_type=output.get("type"),
                    ),
                )
            )
    else:
        logging.error("Unsupported Cobalt Pinterest status: status=%s payload=%s", status, data)
        return None

    if not media_list:
        logging.error("Cobalt Pinterest response has no media items: payload=%s", data)
        return None

    return PinterestPost(
        id=str(int(datetime.datetime.now().timestamp())),
        description=_derive_description(data),
        media_list=media_list,
    )


async def get_user_settings(message: types.Message):
    return await db.user_settings(resolve_settings_target_id(message))


class PinterestService:
    def __init__(self, output_dir: str) -> None:
        config = DownloadConfig(
            chunk_size=1024 * 1024,
            multipart_threshold=16 * 1024 * 1024,
            max_workers=6,
            max_concurrent_downloads=3,
            retry_backoff=0.8,
        )
        self._downloader = ResilientDownloader(output_dir, config=config, source="pinterest")

    async def fetch_post(self, url: str) -> Optional[PinterestPost]:
        payload = {
            "url": url,
            "downloadMode": "auto",
            "videoQuality": "1080",
            "alwaysProxy": True,
            "localProcessing": "disabled",
        }
        data = await fetch_cobalt_data(
            COBALT_API_URL,
            COBALT_API_KEY,
            payload,
            source="pinterest",
            timeout=20,
            attempts=3,
            retry_delay=0.0,
        )
        if not data:
            return None
        return parse_pinterest_post(data)

    async def download_media(
        self,
        url: str,
        filename: str,
        *,
        user_id: Optional[int] = None,
        request_id: Optional[str] = None,
        size_hint: Optional[int] = None,
        on_queued=None,
        on_progress=None,
        on_retry=None,
    ) -> Optional[DownloadMetrics]:
        async def _download_once():
            return await self._downloader.download(
                url,
                filename,
                user_id=user_id,
                request_id=request_id,
                size_hint=size_hint,
                on_queued=on_queued,
                on_progress=on_progress,
            )

        try:
            return await retry_async_operation(
                _download_once,
                attempts=3,
                delay_seconds=2.0,
                retry_on_exception=lambda exc: not isinstance(exc, (DownloadRateLimitError, DownloadQueueBusyError)),
                on_retry=on_retry,
            )
        except (DownloadRateLimitError, DownloadQueueBusyError):
            raise
        except DownloadError as exc:
            logging.error("Error downloading Pinterest media: url=%s error=%s", url, exc)
            return None


pinterest_service = PinterestService(OUTPUT_DIR)


@router.message(
    F.text.regexp(PINTEREST_URL_REGEX, mode="search") | F.caption.regexp(PINTEREST_URL_REGEX, mode="search")
)
@router.business_message(
    F.text.regexp(PINTEREST_URL_REGEX, mode="search") | F.caption.regexp(PINTEREST_URL_REGEX, mode="search")
)
async def process_pinterest(message: types.Message, direct_url: Optional[str] = None):
    try:
        business_id = message.business_connection_id
        text = get_message_text(message)
        bot_url = await get_bot_url(bot)
        if direct_url:
            source_url = strip_pinterest_url(direct_url)
        else:
            url_match = re.search(PINTEREST_URL_REGEX, text or "")
            if not url_match:
                return
            source_url = strip_pinterest_url(url_match.group(0))

        logging.info("Pinterest request: user_id=%s url=%s", message.from_user.id, source_url)
        await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="pinterest_media")
        await react_to_message(message, "\U0001F47E", business_id=business_id)
        user_settings = await get_user_settings(message)

        post = await pinterest_service.fetch_post(source_url)
        if not post or not post.media_list:
            await handle_download_error(message, business_id=business_id)
            return

        first = post.media_list[0]
        if len(post.media_list) == 1 and first.type == "video":
            await process_pinterest_single_video(message, post, source_url, bot_url, user_settings, business_id)
            return
        if len(post.media_list) == 1 and first.type == "photo":
            await process_pinterest_single_photo(message, post, source_url, bot_url, user_settings, business_id)
            return
        await process_pinterest_media_group(message, post, source_url, bot_url, user_settings, business_id)
    except Exception as exc:
        logging.exception(
            "Error processing Pinterest message: user_id=%s text=%s error=%s",
            message.from_user.id,
            get_message_text(message),
            exc,
        )
        await handle_download_error(message)
    finally:
        await update_info(message)


async def process_pinterest_url(message: types.Message, url: Optional[str] = None):
    await process_pinterest(message, direct_url=url)


async def process_pinterest_single_video(
    message: types.Message,
    post: PinterestPost,
    source_url: str,
    bot_url: str,
    user_settings: dict,
    business_id: Optional[int],
):
    media = post.media_list[0]
    db_file_id = await db.get_file_id(source_url)
    if db_file_id:
        await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
        await message.answer_video(
            video=db_file_id,
            caption=bm.captions(user_settings["captions"], post.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                None, None, None, None, None, source_url, user_settings,
                audio_callback_data=None,
            ),
            parse_mode="HTML",
        )
        await maybe_delete_user_message(message, user_settings["delete_message"])
        return

    status_message: Optional[types.Message] = None
    if business_id is None:
        status_message = await message.answer(bm.downloading_video_status())
    progress_state = {"last": 0.0}
    download_path: Optional[str] = None
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    download_name = f"{post.id}_{timestamp}_pinterest_video.mp4"

    try:
        async def on_progress(progress: DownloadProgress):
            now = time.monotonic()
            if not progress.done and now - progress_state["last"] < 1.0:
                return
            progress_state["last"] = now
            await safe_edit_text(status_message, build_progress_status("Pinterest video", progress))

        async def on_retry(failed_attempt: int, total_attempts: int, _error):
            if business_id is None and failed_attempt >= 2:
                await safe_edit_text(
                    status_message,
                    bm.retrying_again_status(failed_attempt + 1, total_attempts),
                )

        metrics = await pinterest_service.download_media(
            media.url,
            download_name,
            user_id=message.from_user.id,
            on_progress=on_progress,
            on_retry=on_retry,
        )
        if not metrics:
            await handle_download_error(message, business_id=business_id)
            return
        log_download_metrics("pinterest_video", metrics)
        download_path = metrics.path
        if metrics.size >= MAX_FILE_SIZE:
            await handle_download_error(message, business_id=business_id, text=bm.video_too_large())
            return

        await safe_edit_text(status_message, bm.uploading_status())
        await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
        sent = await message.answer_video(
            video=FSInputFile(download_path),
            caption=bm.captions(user_settings["captions"], post.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                None, None, None, None, None, source_url, user_settings,
                audio_callback_data=None,
            ),
            parse_mode="HTML",
        )
        await maybe_delete_user_message(message, user_settings["delete_message"])
        await db.add_file(source_url, sent.video.file_id, "video")
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
    finally:
        if download_path:
            await remove_file(download_path)
        await safe_delete_message(status_message)


async def process_pinterest_single_photo(
    message: types.Message,
    post: PinterestPost,
    source_url: str,
    bot_url: str,
    user_settings: dict,
    business_id: Optional[int],
):
    media = post.media_list[0]
    cache_key = f"{source_url}#photo"
    db_file_id = await db.get_file_id(cache_key)
    if db_file_id:
        await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)
        await message.answer_photo(
            photo=db_file_id,
            caption=bm.captions(user_settings["captions"], post.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                None, None, None, None, None, source_url, user_settings,
                audio_callback_data=None,
            ),
            parse_mode="HTML",
        )
        await maybe_delete_user_message(message, user_settings["delete_message"])
        return

    ext = "jpg"
    low_url = media.url.lower().split("?", 1)[0]
    if low_url.endswith(".png"):
        ext = "png"
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"{post.id}_{timestamp}_pinterest_photo.{ext}"
    metrics = await pinterest_service.download_media(
        media.url,
        filename,
        user_id=message.from_user.id,
        request_id=f"pinterest_photo:{message.chat.id}:{message.message_id}:{post.id}",
    )
    if not metrics:
        await handle_download_error(message, business_id=business_id)
        return

    log_download_metrics("pinterest_photo", metrics)
    try:
        await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)
        sent = await message.answer_photo(
            photo=FSInputFile(metrics.path),
            caption=bm.captions(user_settings["captions"], post.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                None, None, None, None, None, source_url, user_settings,
                audio_callback_data=None,
            ),
            parse_mode="HTML",
        )
        await maybe_delete_user_message(message, user_settings["delete_message"])
        if sent.photo:
            await db.add_file(cache_key, sent.photo[-1].file_id, "photo")
    finally:
        await remove_file(metrics.path)


async def process_pinterest_media_group(
    message: types.Message,
    post: PinterestPost,
    source_url: str,
    bot_url: str,
    user_settings: dict,
    business_id: Optional[int],
):
    await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)
    downloaded_paths: list[str] = []
    media_items: list[dict[str, str]] = []
    request_id = f"pinterest_group:{message.chat.id}:{message.message_id}:{post.id}"

    async def _download_item(index: int, item: PinterestMedia):
        ext = "mp4" if item.type == "video" else "jpg"
        filename = f"pin_{post.id}_{index}.{ext}"
        metrics = await pinterest_service.download_media(
            item.url,
            filename,
            user_id=message.from_user.id,
            request_id=request_id,
        )
        if not metrics:
            return None
        log_download_metrics("pinterest_group", metrics)
        return index, item.type, metrics.path

    tasks = [
        asyncio.create_task(_download_item(index, item))
        for index, item in enumerate(post.media_list[:10])
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in sorted(
        (r for r in results if not isinstance(r, Exception) and r is not None),
        key=lambda value: value[0],
    ):
        _, media_type, media_path = result
        downloaded_paths.append(media_path)
        media_items.append({"path": media_path, "type": media_type})

    for result in results:
        if isinstance(result, Exception):
            logging.error("Pinterest media download task failed: error=%s", result)

    if not media_items:
        await handle_download_error(message, business_id=business_id)
        return

    try:
        if len(media_items) > 1:
            media_group = MediaGroupBuilder()
            for item in media_items[:-1]:
                if item["type"] == "video":
                    media_group.add_video(media=FSInputFile(item["path"]))
                else:
                    media_group.add_photo(media=FSInputFile(item["path"]))
            await message.answer_media_group(media=media_group.build())

        last_item = media_items[-1]
        caption = bm.captions(user_settings["captions"], post.description, bot_url)
        keyboard = kb.return_video_info_keyboard(
            None, None, None, None, None, source_url, user_settings,
            audio_callback_data=None,
        )
        if last_item["type"] == "video":
            await message.answer_video(
                video=FSInputFile(last_item["path"]),
                caption=caption,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            await message.answer_photo(
                photo=FSInputFile(last_item["path"]),
                caption=caption,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        await maybe_delete_user_message(message, user_settings["delete_message"])
    finally:
        for path in downloaded_paths:
            await remove_file(path)


@router.inline_query(F.query.regexp(PINTEREST_URL_REGEX, mode="search"))
async def inline_pinterest_query(query: types.InlineQuery):
    try:
        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_pinterest_video")
        match = re.search(PINTEREST_URL_REGEX, query.query or "")
        if not match:
            await query.answer([], cache_time=1, is_personal=True)
            return
        if not CHANNEL_ID:
            logging.error("CHANNEL_ID is not configured; Pinterest inline is disabled")
            await query.answer([], cache_time=1, is_personal=True)
            return

        source_url = strip_pinterest_url(match.group(0))
        user_settings = await db.user_settings(query.from_user.id)
        bot_url = await get_bot_url(bot)

        post = await pinterest_service.fetch_post(source_url)
        if not post or not post.media_list:
            await query.answer([], cache_time=1, is_personal=True)
            return
        photo_items = [item for item in post.media_list if item.type == "photo"]
        first_photo = photo_items[0] if photo_items else None

        if len(post.media_list) == 1 and first_photo:
            preview_url = first_photo.thumb or first_photo.url
            results = [
                types.InlineQueryResultPhoto(
                    id=f"pinterest_photo_{post.id}",
                    photo_url=first_photo.url,
                    thumbnail_url=preview_url,
                    title=bm.inline_photo_title("Pinterest"),
                    description=post.description or bm.inline_photo_description(),
                    caption=bm.captions(user_settings["captions"], post.description, bot_url),
                    reply_markup=kb.return_video_info_keyboard(
                        None, None, None, None, None, source_url, user_settings,
                        audio_callback_data=None,
                    ),
                    parse_mode="HTML",
                )
            ]
            await query.answer(results, cache_time=10, is_personal=True)
            return

        if len(post.media_list) != 1 or post.media_list[0].type != "video":
            if first_photo and len(photo_items) > 1:
                token = create_inline_album_request(query.from_user.id, "pinterest", source_url)
                deep_link = build_start_deeplink_url(bot_url, f"album_{token}")
                preview_url = first_photo.thumb or first_photo.url
                results = [
                    types.InlineQueryResultPhoto(
                        id=f"pinterest_album_{post.id}",
                        photo_url=first_photo.url,
                        thumbnail_url=preview_url,
                        title=bm.inline_album_title("Pinterest"),
                        description=bm.inline_album_description(),
                        caption=bm.captions(user_settings["captions"], post.description, bot_url),
                        reply_markup=types.InlineKeyboardMarkup(
                            inline_keyboard=[[
                                types.InlineKeyboardButton(text=bm.inline_open_full_album_button(), url=deep_link)
                            ]]
                        ),
                        parse_mode="HTML",
                    )
                ]
                await query.answer(results, cache_time=10, is_personal=True)
                return
            if first_photo:
                preview_url = first_photo.thumb or first_photo.url
                results = [
                    types.InlineQueryResultPhoto(
                        id=f"pinterest_photo_{post.id}",
                        photo_url=first_photo.url,
                        thumbnail_url=preview_url,
                        title=bm.inline_photo_title("Pinterest"),
                        description=post.description or bm.inline_photo_description(),
                        caption=bm.captions(user_settings["captions"], post.description, bot_url),
                        reply_markup=kb.return_video_info_keyboard(
                            None, None, None, None, None, source_url, user_settings,
                            audio_callback_data=None,
                        ),
                        parse_mode="HTML",
                    )
                ]
                await query.answer(results, cache_time=10, is_personal=True)
                return

            results = [
                types.InlineQueryResultArticle(
                    id="unsupported_pinterest_content",
                    title="Pinterest Content",
                    description="Only single videos are supported inline.",
                    input_message_content=types.InputTextMessageContent(
                        message_text="Only single Pinterest videos are supported inline.",
                    ),
                )
            ]
            await query.answer(results, cache_time=10, is_personal=True)
            return

        db_id = await db.get_file_id(source_url)
        metrics: Optional[DownloadMetrics] = None
        if not db_id:
            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            filename = f"{post.id}_{timestamp}_pinterest_inline.mp4"
            metrics = await pinterest_service.download_media(
                post.media_list[0].url,
                filename,
                user_id=query.from_user.id,
                request_id=f"pinterest_inline:{query.from_user.id}:{query.id}:{post.id}",
            )
            if metrics and metrics.size < MAX_FILE_SIZE:
                log_download_metrics("pinterest_inline", metrics)
                sent = await bot.send_video(
                    chat_id=CHANNEL_ID,
                    video=FSInputFile(metrics.path),
                    caption=f"📌 Pinterest Video from {query.from_user.full_name}",
                )
                db_id = sent.video.file_id
                await db.add_file(source_url, db_id, "video")
            if metrics:
                await remove_file(metrics.path)

        if not db_id:
            await query.answer([], cache_time=1, is_personal=True)
            return

        thumb_url = post.media_list[0].thumb or "https://www.pinterest.com/favicon.ico"
        results = [
            types.InlineQueryResultVideo(
                id=f"pinterest_{post.id}",
                video_url=db_id,
                thumbnail_url=thumb_url,
                title="📌 Pinterest Video",
                description=post.description or "Pinterest Video",
                mime_type="video/mp4",
                caption=bm.captions(user_settings["captions"], post.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    None, None, None, None, None, source_url, user_settings,
                    audio_callback_data=None,
                ),
                parse_mode="HTML",
            )
        ]
        await query.answer(results, cache_time=10, is_personal=True)
    except Exception as exc:
        logging.exception(
            "Error processing Pinterest inline query: user_id=%s query=%s error=%s",
            query.from_user.id,
            query.query,
            exc,
        )
        await query.answer([], cache_time=1, is_personal=True)
