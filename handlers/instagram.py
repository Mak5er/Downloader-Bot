import asyncio
import datetime
import re
import time
from typing import Optional
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from aiogram import types, Router, F
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder

import keyboards as kb
import messages as bm
from config import (
    CHANNEL_ID,
    OUTPUT_DIR,
    COBALT_API_URL,
    COBALT_API_KEY,
)
from handlers.user import update_info
from handlers.utils import (
    build_inline_album_result,
    build_request_id,
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
    safe_delete_message,
    safe_edit_text,
    safe_edit_inline_media,
    safe_edit_inline_text,
    safe_answer_inline_query,
    send_chat_action_if_needed,
    retry_async_operation,
    resolve_settings_target_id,
    with_callback_logging,
    with_chosen_inline_logging,
    with_inline_query_logging,
    with_inline_send_logging,
    with_message_logging,
)
from log.logger import logger as logging
from main import bot, db, send_analytics
from utils.download_manager import (
    DownloadConfig,
    DownloadError,
    DownloadProgress,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    DownloadMetrics,
    ResilientDownloader,
    log_download_metrics,
)
from utils.cobalt_client import fetch_cobalt_data
from utils.media_cache import build_media_cache_key
from services.inline_album_links import create_inline_album_request
from services.inline_service_icons import get_inline_service_icon
from services.inline_video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)

logging = logging.bind(service="instagram")

router = Router()

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)


def strip_instagram_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


def _classify_cobalt_media_type(
    media_url: str,
    *,
    audio_only: bool = False,
    declared_type: Optional[str] = None,
    filename: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> str:
    if audio_only:
        return "audio"

    if declared_type:
        normalized_type = declared_type.lower()
        if normalized_type in {"video", "gif", "merge", "mute", "remux"}:
            return "video"
        if normalized_type == "photo":
            return "photo"
        if normalized_type == "audio":
            return "audio"

    if mime_type:
        normalized_mime = mime_type.lower()
        if normalized_mime.startswith("video/"):
            return "video"
        if normalized_mime.startswith("image/"):
            return "photo"
        if normalized_mime.startswith("audio/"):
            return "audio"

    probe = f"{media_url} {filename or ''}".lower()
    if any(ext in probe for ext in (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".opus")):
        return "audio"
    if any(ext in probe for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif")):
        return "photo"
    return "video"


@dataclass
class InstagramMedia:
    url: str
    type: str
    thumb: Optional[str] = None


@dataclass
class InstagramVideo:
    id: str
    description: str
    author: str
    media_list: list[InstagramMedia]


def _extract_instagram_thumb(source: dict) -> Optional[str]:
    for key in ("thumb", "thumbnail", "poster", "preview", "cover"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _get_instagram_preview_url(media: Optional[InstagramMedia]) -> Optional[str]:
    if not media:
        return None
    if media.type == "photo":
        return media.url
    return media.thumb or None


async def get_user_settings(message: types.Message):
    return await db.user_settings(resolve_settings_target_id(message))


class InstagramService:

    def __init__(self, output_dir: str) -> None:
        config = DownloadConfig(
            chunk_size=1024 * 1024,
            multipart_threshold=16 * 1024 * 1024,
            max_workers=6,
            retry_backoff=0.8,
        )
        self._downloader = ResilientDownloader(output_dir, config=config, source="instagram")

    async def fetch_data(self, url: str, audio_only: bool = False) -> Optional[InstagramVideo]:
        payload = {
            "url": url,
            "videoQuality": "720",
            "downloadMode": "audio" if audio_only else "auto",
            "alwaysProxy": True,
            "localProcessing": "disabled",
        }
        data = await fetch_cobalt_data(
            COBALT_API_URL,
            COBALT_API_KEY,
            payload,
            source="instagram",
            timeout=15,
            attempts=3,
            retry_delay=0.0,
        )
        if not data:
            return None

        media_list = []
        status = data.get("status")

        # Backward compatibility with old Cobalt response structure.
        if not status:
            if "url" in data:
                status = "tunnel"
            elif "picker" in data:
                status = "picker"

        if status in {"tunnel", "redirect"}:
            media_url = data.get("url")
            if isinstance(media_url, str) and media_url:
                media_list.append(
                    InstagramMedia(
                        url=media_url,
                        type=_classify_cobalt_media_type(
                            media_url,
                            audio_only=audio_only,
                            filename=data.get("filename"),
                        ),
                        thumb=_extract_instagram_thumb(data),
                    )
                )

        elif status == "picker":
            picker_audio_url = data.get("audio")
            if audio_only and isinstance(picker_audio_url, str) and picker_audio_url:
                media_list.append(InstagramMedia(url=picker_audio_url, type="audio"))
            else:
                picker_items = data.get("picker") or []
                for item in picker_items:
                    if not isinstance(item, dict):
                        continue
                    media_url = item.get("url")
                    if not isinstance(media_url, str) or not media_url:
                        continue
                    media_list.append(
                        InstagramMedia(
                            url=media_url,
                            type=_classify_cobalt_media_type(
                                media_url,
                                audio_only=audio_only,
                                declared_type=item.get("type"),
                            ),
                            thumb=_extract_instagram_thumb(item),
                        )
                    )

        elif status == "local-processing":
            tunnel_urls = data.get("tunnel") or []
            output = data.get("output") or {}
            if not isinstance(tunnel_urls, list) or not tunnel_urls:
                logging.error("Cobalt local-processing response has no tunnels: payload=%s", data)
                return None
            if not audio_only and len(tunnel_urls) > 1:
                logging.error(
                    "Unsupported Cobalt local-processing payload for Instagram: type=%s tunnel_count=%s",
                    data.get("type"),
                    len(tunnel_urls),
                )
                return None
            for media_url in tunnel_urls:
                if not isinstance(media_url, str) or not media_url:
                    continue
                media_list.append(
                    InstagramMedia(
                        url=media_url,
                        type=_classify_cobalt_media_type(
                            media_url,
                            audio_only=audio_only,
                            declared_type=data.get("type"),
                            filename=output.get("filename") if isinstance(output, dict) else None,
                            mime_type=output.get("type") if isinstance(output, dict) else None,
                        ),
                        thumb=_extract_instagram_thumb(data) or _extract_instagram_thumb(output),
                    )
                )

        elif status == "error":
            error_obj = data.get("error") or {}
            logging.error(
                "Cobalt API returned error: code=%s context=%s",
                error_obj.get("code") if isinstance(error_obj, dict) else None,
                error_obj.get("context") if isinstance(error_obj, dict) else None,
            )
            return None

        else:
            logging.error("Unsupported Cobalt response status: status=%s payload=%s", status, data)
            return None

        if not media_list:
            logging.error("Cobalt response has no media items: status=%s payload=%s", status, data)
            return None

        return InstagramVideo(
            id=str(int(datetime.datetime.now().timestamp())),
            description="",
            author="instagram_user",
            media_list=media_list
        )

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
            logging.error("Error downloading Instagram media: url=%s error=%s", url, exc)
            return None


inst_service = InstagramService(OUTPUT_DIR)

@router.message(F.text.regexp(r"(https?://(www\.)?instagram\.com/(p|reels|reel)/[^/?#&]+)", mode="search"))
@router.business_message(F.text.regexp(r"(https?://(www\.)?instagram\.com/(p|reels|reel)/[^/?#&]+)", mode="search"))
@with_message_logging("instagram", "message")
async def process_instagram(message: types.Message, direct_url: Optional[str] = None):
    try:
        bot_url = await get_bot_url(bot)
        business_id = message.business_connection_id
        text = get_message_text(message)

        if direct_url:
            url = strip_instagram_url(direct_url)
        else:
            url_match = re.search(r"(https?://(www\.)?instagram\.com/(p|reels|reel)/[^/?#&]+)", text)
            if not url_match:
                return
            url = strip_instagram_url(url_match.group(0))

        logging.info("Instagram request: user_id=%s url=%s", message.from_user.id, url)
        user_settings = await get_user_settings(message)
        await react_to_message(message, "👾", business_id=business_id)

        data = await inst_service.fetch_data(url)
        if not data or not data.media_list:
            await handle_download_error(message, business_id=business_id)
            return

        has_videos = any(item.type == "video" for item in data.media_list)
        has_photos = any(item.type == "photo" for item in data.media_list)

        logging.debug(
            "Instagram content classification: has_videos=%s has_photos=%s item_count=%s",
            has_videos,
            has_photos,
            len(data.media_list),
        )

        if has_videos and len(data.media_list) == 1:
            await process_instagram_video(message, data, url, bot_url, user_settings, business_id)
        elif has_photos or len(data.media_list) > 1:
            await process_instagram_media_group(message, data, url, bot_url, user_settings, business_id)
        else:
            await handle_download_error(message, business_id=business_id)

    except Exception as e:
        logging.exception(
            "Error processing Instagram message: user_id=%s text=%s error=%s",
            message.from_user.id,
            get_message_text(message),
            e,
        )
        await handle_download_error(message)
    finally:
        await update_info(message)


async def process_instagram_url(message: types.Message, url: Optional[str] = None):
    """Backward-compatible entrypoint used by pending-request flow."""
    await process_instagram(message, direct_url=url)


async def process_instagram_video(message: types.Message, data: InstagramVideo, original_url: str, bot_url: str,
                                  user_settings: dict, business_id: Optional[int]):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="instagram_video")

    if not data.media_list or data.media_list[0].type != "video":
        await handle_download_error(message, business_id=business_id)
        return

    audio_callback_data = f"audio:inst:{original_url}"
    media = data.media_list[0]

    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    download_name = f"{data.id}_{timestamp}_instagram_video.mp4"
    db_video_url = original_url
    show_service_status = business_id is None
    status_message: Optional[types.Message] = None
    if show_service_status:
        status_message = await message.answer(bm.downloading_video_status())

    db_file_id = await db.get_file_id(db_video_url)
    progress_state = {"last": 0.0}
    download_path: Optional[str] = None

    try:
        if db_file_id:
            logging.info(
                "Serving cached Instagram video: url=%s file_id=%s",
                db_video_url,
                db_file_id,
            )
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
            await message.reply_video(
                video=db_file_id,
                caption=bm.captions(user_settings["captions"], data.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    None, None, None, None, "", db_video_url, user_settings,
                    audio_callback_data=audio_callback_data,
                ),
                parse_mode="HTML"
            )
            await maybe_delete_user_message(message, user_settings["delete_message"])
            return

        async def on_progress(progress: DownloadProgress):
            now = time.monotonic()
            if not progress.done and now - progress_state["last"] < 1.0:
                return
            progress_state["last"] = now
            await safe_edit_text(status_message, build_progress_status("Instagram video", progress))

        async def on_retry(failed_attempt: int, total_attempts: int, _error):
            if show_service_status and failed_attempt >= 2:
                await safe_edit_text(
                    status_message,
                    bm.retrying_again_status(failed_attempt + 1, total_attempts),
                )

        metrics = await inst_service.download_media(
            media.url,
            download_name,
            user_id=message.from_user.id,
            on_progress=on_progress,
            on_retry=on_retry,
        )
        if not metrics:
            await handle_download_error(message, business_id=business_id)
            return

        log_download_metrics("instagram_video", metrics)
        download_path = metrics.path
        file_size = metrics.size

        if file_size >= MAX_FILE_SIZE:
            logging.warning(
                "Instagram video too large: url=%s size=%s",
                db_video_url,
                file_size,
            )
            await handle_download_error(message, business_id=business_id)
            return

        await safe_edit_text(status_message, bm.uploading_status())
        await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
        sent = await message.reply_video(
            video=FSInputFile(download_path),
            caption=bm.captions(user_settings["captions"], data.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                None, None, None, None, "", db_video_url, user_settings,
                audio_callback_data=audio_callback_data,
            ),
            parse_mode="HTML"
        )
        await maybe_delete_user_message(message, user_settings["delete_message"])

        try:
            await db.add_file(db_video_url, sent.video.file_id, "video")
            logging.info(
                "Cached Instagram video: url=%s file_id=%s",
                db_video_url,
                sent.video.file_id,
            )
        except Exception as e:
            logging.error("Error caching Instagram video: url=%s error=%s", db_video_url, e)

    except DownloadRateLimitError as e:
        if show_service_status:
            await message.reply(build_rate_limit_text(e.retry_after))
        else:
            await handle_download_error(message, business_id=business_id)
    except DownloadQueueBusyError as e:
        if show_service_status:
            await message.reply(build_queue_busy_text(e.position))
        else:
            await handle_download_error(message, business_id=business_id)
    except Exception as e:
        logging.exception(
            "Error processing Instagram video: url=%s error=%s",
            db_video_url,
            e,
        )
        await handle_download_error(message, business_id=business_id)
    finally:
        if download_path:
            await remove_file(download_path)
            logging.debug("Removed temporary Instagram video file: path=%s", download_path)
        await safe_delete_message(status_message)


async def process_instagram_media_group(message: types.Message, data: InstagramVideo, original_url: str, bot_url: str,
                                        user_settings: dict, business_id: Optional[int]):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="instagram_media_group")

    logging.info(
        "Sending Instagram media group: user_id=%s media_count=%s url=%s",
        message.from_user.id,
        len(data.media_list),
        original_url,
    )

    await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)

    media_items: list[dict[str, object]] = []
    downloaded_paths: list[str] = []
    request_id = f"instagram_group:{message.chat.id}:{message.message_id}:{data.id}"
    status_message: Optional[types.Message] = None
    if business_id is None:
        status_message = await message.answer(bm.downloading_video_status())

    def _extract_sent_file_id(sent_message: types.Message, media_kind: str) -> str | None:
        if media_kind == "video" and sent_message.video:
            return sent_message.video.file_id
        if media_kind == "photo" and sent_message.photo:
            return sent_message.photo[-1].file_id
        return None

    async def _resolve_item(index: int, item: InstagramMedia):
        cache_key = build_media_cache_key(original_url, item_index=index, item_kind=item.type)
        cached_file_id = await db.get_file_id(cache_key)
        if cached_file_id:
            return {
                "index": index,
                "type": item.type,
                "cache_key": cache_key,
                "file_id": cached_file_id,
                "path": None,
                "cached": True,
            }

        ext = "mp4" if item.type == "video" else "jpg"
        filename = f"inst_{data.id}_{index}.{ext}"
        metrics = await inst_service.download_media(
            item.url,
            filename,
            user_id=message.from_user.id,
            request_id=request_id,
        )
        if not metrics:
            return None
        log_download_metrics("instagram_group", metrics)
        return {
            "index": index,
            "type": item.type,
            "cache_key": cache_key,
            "file_id": None,
            "path": metrics.path,
            "cached": False,
        }

    tasks = [
        asyncio.create_task(_resolve_item(i, item))
        for i, item in enumerate(data.media_list[:10])
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in sorted(
        (r for r in results if not isinstance(r, Exception) and r is not None),
        key=lambda value: int(value["index"]),
    ):
        if result["path"]:
            downloaded_paths.append(str(result["path"]))
        media_items.append(result)

    for result in results:
        if isinstance(result, Exception):
            logging.error("Instagram media download task failed: error=%s", result)

    if not media_items:
        await handle_download_error(message, business_id=business_id)
        return

    try:
        has_sent_media = False
        if len(media_items) > 1:
            media_group = MediaGroupBuilder()
            batch = media_items[:-1]
            for item in batch:
                media_ref = item["file_id"] if item["file_id"] else FSInputFile(str(item["path"]))
                if item["type"] == "video":
                    media_group.add_video(media=media_ref)
                else:
                    media_group.add_photo(media=media_ref)
            await safe_edit_text(status_message, bm.uploading_status())
            send_kwargs = {"media": media_group.build()}
            if not has_sent_media:
                send_kwargs["reply_to_message_id"] = message.message_id
            sent_group = await message.answer_media_group(**send_kwargs)
            has_sent_media = True
            for sent_message, item in zip(sent_group, batch):
                if item["cached"]:
                    continue
                file_id = _extract_sent_file_id(sent_message, str(item["type"]))
                if file_id:
                    await db.add_file(str(item["cache_key"]), file_id, str(item["type"]))

        last_item = media_items[-1]
        db_video_url = original_url

        if last_item["type"] == "video":
            await safe_edit_text(status_message, bm.uploading_status())
            send_video = message.answer_video if has_sent_media else message.reply_video
            sent_message = await send_video(
                video=last_item["file_id"] if last_item["file_id"] else FSInputFile(str(last_item["path"])),
                caption=bm.captions(user_settings["captions"], data.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    None, None, None, None, "", db_video_url, user_settings,
                    audio_callback_data=None,
                ),
                parse_mode="HTML"
            )
        else:
            await safe_edit_text(status_message, bm.uploading_status())
            send_photo = message.answer_photo if has_sent_media else message.reply_photo
            sent_message = await send_photo(
                photo=last_item["file_id"] if last_item["file_id"] else FSInputFile(str(last_item["path"])),
                caption=bm.captions(user_settings["captions"], data.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    None, None, None, None, "", db_video_url, user_settings,
                    audio_callback_data=None,
                ),
                parse_mode="HTML"
            )
        if not last_item["cached"]:
            file_id = _extract_sent_file_id(sent_message, str(last_item["type"]))
            if file_id:
                await db.add_file(str(last_item["cache_key"]), file_id, str(last_item["type"]))

        await maybe_delete_user_message(message, user_settings["delete_message"])

        logging.info(
            "Successfully sent Instagram media group: user_id=%s media_count=%s",
            message.from_user.id,
            len(media_items),
        )
    finally:
        await safe_delete_message(status_message)
        for path in downloaded_paths:
            await remove_file(path)
            logging.debug("Removed temporary Instagram media file: path=%s", path)


@router.callback_query(F.data.startswith("audio:inst:"))
async def download_instagram_audio_callback(call: types.CallbackQuery):
    if not call.message:
        await call.answer(bm.open_bot_for_audio(), show_alert=True)
        return

    await call.answer()
    original_url = call.data.replace("audio:inst:", "")
    business_id = call.message.business_connection_id
    show_service_status = business_id is None
    status_message: Optional[types.Message] = None
    if show_service_status:
        status_message = await call.message.answer(bm.downloading_audio_status())

    try:
        bot_url = await get_bot_url(bot)
        cache_key = f"{original_url}#audio"

        db_file_id = await db.get_file_id(cache_key)
        if db_file_id:
            logging.info(
                "Serving cached Instagram audio: url=%s file_id=%s",
                original_url,
                db_file_id,
            )
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(
                bot,
                call.message.chat.id,
                "upload_audio",
                business_id,
            )
            await call.message.reply_audio(
                audio=db_file_id,
                caption=bm.captions(None, "", bot_url),
                parse_mode="HTML",
            )
            return

        data = await inst_service.fetch_data(original_url, audio_only=True)
        if not data or not data.media_list:
            if show_service_status:
                await safe_edit_text(status_message, bm.audio_fetch_failed())
            else:
                await handle_download_error(call.message, business_id=business_id)
            logging.error("Failed to fetch Instagram audio: url=%s", original_url)
            return

        audio_item = data.media_list[0]
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        download_name = f"{data.id}_{timestamp}_instagram_audio.mp3"
        progress_state = {"last": 0.0}

        async def on_progress(progress: DownloadProgress):
            now = time.monotonic()
            if not progress.done and now - progress_state["last"] < 1.0:
                return
            progress_state["last"] = now
            await safe_edit_text(status_message, build_progress_status("Instagram audio", progress))

        async def on_retry(failed_attempt: int, total_attempts: int, _error):
            if show_service_status and failed_attempt >= 2:
                await safe_edit_text(
                    status_message,
                    bm.retrying_again_status(failed_attempt + 1, total_attempts),
                )

        metrics = await inst_service.download_media(
            audio_item.url,
            download_name,
            user_id=call.from_user.id,
            on_progress=on_progress,
            on_retry=on_retry,
        )
        if not metrics:
            if show_service_status:
                await safe_edit_text(status_message, bm.audio_download_failed())
            else:
                await handle_download_error(call.message, business_id=business_id)
            return

        if metrics.size >= MAX_FILE_SIZE:
            if show_service_status:
                await safe_edit_text(status_message, bm.audio_too_large())
            else:
                await call.message.reply(bm.audio_too_large())
            await remove_file(metrics.path)
            return

        await send_chat_action_if_needed(
            bot,
            call.message.chat.id,
            "upload_audio",
            business_id,
        )
        await safe_edit_text(status_message, bm.uploading_status())
        sent_message = await call.message.reply_audio(
            audio=FSInputFile(metrics.path),
            title="Instagram Audio",
            caption=bm.captions(None, "", bot_url),
            parse_mode="HTML",
        )

        try:
            await db.add_file(cache_key, sent_message.audio.file_id, "audio")
            logging.info(
                "Cached Instagram audio: url=%s file_id=%s",
                original_url,
                sent_message.audio.file_id,
            )
        except Exception as e:
            logging.error("Error caching Instagram audio: url=%s error=%s", original_url, e)

        await remove_file(metrics.path)
        logging.debug("Removed temporary Instagram audio file: path=%s", metrics.path)

    except DownloadRateLimitError as e:
        if show_service_status:
            await call.message.reply(build_rate_limit_text(e.retry_after))
        else:
            await handle_download_error(call.message, business_id=business_id)
    except DownloadQueueBusyError as e:
        if show_service_status:
            await call.message.reply(build_queue_busy_text(e.position))
        else:
            await handle_download_error(call.message, business_id=business_id)
    except Exception as e:
        logging.exception(
            "Error downloading Instagram audio: url=%s error=%s",
            original_url,
            e,
        )
        if status_message:
            await safe_edit_text(status_message, bm.something_went_wrong())
        else:
            await handle_download_error(call.message, business_id=business_id)
    finally:
        await safe_delete_message(status_message)


@router.inline_query(F.query.regexp(r"(https?://(www\.)?instagram\.com/(p|reels|reel)/[^/?#&]+)", mode="search"))
@with_inline_query_logging("instagram", "inline_query")
async def inline_instagram_query(query: types.InlineQuery):
    try:
        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_instagram_video")
        logging.info(
            "Inline Instagram request: user_id=%s query=%s",
            query.from_user.id,
            query.query,
        )

        user_settings = await db.user_settings(query.from_user.id)
        bot_url = await get_bot_url(bot)

        url_match = re.search(r"(https?://(www\.)?instagram\.com/(p|reels|reel)/[^/?#&]+)", query.query)
        if not url_match:
            logging.debug("Inline Instagram query pattern not matched: query=%s", query.query)
            return await query.answer([], cache_time=1, is_personal=True)

        original_url = strip_instagram_url(url_match.group(0))

        data = await inst_service.fetch_data(original_url)
        if not data or not data.media_list:
            logging.warning("Inline Instagram fetch failed: url=%s", original_url)
            return await query.answer([], cache_time=1, is_personal=True)

        if len(data.media_list) == 1 and data.media_list[0].type == "video":
            db_id = await db.get_file_id(original_url)
            if not db_id and not CHANNEL_ID:
                logging.error("CHANNEL_ID is not configured; Instagram inline video send is disabled")
                return await query.answer([], cache_time=1, is_personal=True)

            preview_url = _get_instagram_preview_url(data.media_list[0]) or get_inline_service_icon("instagram")
            token = create_inline_video_request("instagram", original_url, query.from_user.id, user_settings)
            results = [
                types.InlineQueryResultArticle(
                    id=f"instagram_inline:{token}",
                    title="Instagram Video",
                    description=data.description or "Press the button to send this video inline.",
                    thumbnail_url=preview_url,
                    input_message_content=types.InputTextMessageContent(
                        message_text=bm.inline_send_video_prompt("Instagram"),
                    ),
                    reply_markup=kb.inline_send_media_keyboard(
                        "Send video inline",
                        f"inline:instagram:{token}",
                    ),
                )
            ]
            await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
            return

        first_item = data.media_list[0] if data.media_list else None
        photo_items = [item for item in data.media_list if item.type == "photo"]
        first_photo = photo_items[0] if photo_items else None
        first_preview = _get_instagram_preview_url(first_item) or next(
            (_get_instagram_preview_url(item) for item in data.media_list if _get_instagram_preview_url(item)),
            None,
        )
        if len(data.media_list) == 1 and first_photo:
            cache_key = build_media_cache_key(original_url, item_index=0, item_kind="photo")
            db_id = await db.get_file_id(cache_key)
            if not db_id and not CHANNEL_ID:
                logging.error("CHANNEL_ID is not configured; Instagram inline photo send is disabled")
                return await query.answer([], cache_time=1, is_personal=True)

            token = create_inline_video_request("instagram", original_url, query.from_user.id, user_settings)
            results = [
                types.InlineQueryResultArticle(
                    id=f"instagram_inline:{token}",
                    title="Instagram Photo",
                    description=data.description or "Press the button to send this photo inline.",
                    thumbnail_url=first_preview or first_photo.url,
                    input_message_content=types.InputTextMessageContent(
                        message_text="Instagram photo is being prepared...\nIf it does not start automatically, tap the button below.",
                    ),
                    reply_markup=kb.inline_send_media_keyboard(
                        "Send photo inline",
                        f"inline:instagram:{token}",
                    ),
                )
            ]
            await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
            return

        if len(data.media_list) > 1:
            preview_file_id = None
            if first_item and first_item.type == "photo" and CHANNEL_ID:
                cache_key = build_media_cache_key(original_url, item_index=0, item_kind="photo")
                preview_file_id = await db.get_file_id(cache_key)
                if not preview_file_id:
                    try:
                        sent = await bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=first_item.url,
                            caption="Instagram Album Preview",
                        )
                        if sent.photo:
                            preview_file_id = sent.photo[-1].file_id
                            await db.add_file(cache_key, preview_file_id, "photo")
                    except Exception as exc:
                        logging.warning(
                            "Failed to cache Instagram album preview photo: url=%s error=%s",
                            original_url,
                            exc,
                        )
            token = create_inline_album_request(query.from_user.id, "instagram", original_url)
            deep_link = build_start_deeplink_url(bot_url, f"album_{token}")
            results = [
                build_inline_album_result(
                    result_id=f"instagram_album_{data.id}",
                    service_name="Instagram",
                    deep_link=deep_link,
                    message_text=bm.captions(user_settings["captions"], data.description, bot_url),
                    preview_file_id=preview_file_id,
                    preview_url=first_preview,
                    thumbnail_url=first_preview or get_inline_service_icon("instagram"),
                )
            ]
            await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
            return

    except Exception as e:
        logging.exception(
            "Error processing inline Instagram query: user_id=%s query=%s error=%s",
            query.from_user.id,
            query.query,
            e,
        )
        await query.answer([], cache_time=1, is_personal=True)


@with_inline_send_logging("instagram", "inline_send")
async def _send_inline_instagram_video(
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

    async def _edit_inline_status(
        text: str,
        *,
        with_retry_button: bool = False,
        media_kind: str = "video",
    ) -> None:
        button_text = "Send photo inline" if media_kind == "photo" else "Send video inline"
        reply_markup = (
            kb.inline_send_media_keyboard(button_text, f"inline:instagram:{token}")
            if with_retry_button
            else None
        )
        await safe_edit_inline_text(bot, inline_message_id, text, reply_markup=reply_markup)

    try:
        data = await inst_service.fetch_data(request.source_url)
        if not data or not data.media_list:
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
            return

        if len(data.media_list) != 1:
            complete_inline_video_request(token)
            await _edit_inline_status(bm.inline_photos_not_supported("Instagram"))
            return

        media = data.media_list[0]
        if media.type == "photo":
            cache_key = build_media_cache_key(request.source_url, item_index=0, item_kind="photo")
            db_id = await db.get_file_id(cache_key)
            if not db_id:
                if not CHANNEL_ID:
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return

                await _edit_inline_status(bm.uploading_status(), media_kind="photo")
                sent = await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=media.url,
                    caption=f"Instagram Photo from {actor_name}",
                )
                if not sent.photo:
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return
                db_id = sent.photo[-1].file_id
                await db.add_file(cache_key, db_id, "photo")
            else:
                await _edit_inline_status(bm.uploading_status(), media_kind="photo")

            bot_url = await get_bot_url(bot)
            edited = await safe_edit_inline_media(
                bot,
                inline_message_id,
                types.InputMediaPhoto(
                    media=db_id,
                    caption=bm.captions(request.user_settings["captions"], data.description, bot_url),
                    parse_mode="HTML",
                ),
                reply_markup=kb.return_video_info_keyboard(
                    None,
                    None,
                    None,
                    None,
                    None,
                    request.source_url,
                    request.user_settings,
                    audio_callback_data=None,
                ),
            )
            if edited:
                complete_inline_video_request(token)
                return

            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
            return

        if media.type != "video":
            complete_inline_video_request(token)
            await _edit_inline_status(bm.inline_photos_not_supported("Instagram"))
            return

        db_video_url = request.source_url
        audio_callback_data = f"audio:inst:{request.source_url}"
        db_id = await db.get_file_id(db_video_url)
        if not db_id:
            if not CHANNEL_ID:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            download_name = f"{data.id}_{timestamp}_instagram_video.mp4"
            progress_state = {"last": 0.0}

            await _edit_inline_status(bm.downloading_video_status())

            async def on_progress(progress: DownloadProgress):
                now = time.monotonic()
                if not progress.done and now - progress_state["last"] < 1.0:
                    return
                progress_state["last"] = now
                await _edit_inline_status(build_progress_status("Instagram video", progress))

            metrics = await inst_service.download_media(
                media.url,
                download_name,
                user_id=request.owner_user_id,
                request_id=f"instagram_inline:{request.owner_user_id}:{request_event_id}:{data.id}",
                on_progress=on_progress,
            )
            if not metrics:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            log_download_metrics("instagram_inline", metrics)
            download_path = metrics.path
            if metrics.size >= MAX_FILE_SIZE:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.video_too_large())
                return

            await _edit_inline_status(bm.uploading_status())
            sent = await bot.send_video(
                chat_id=CHANNEL_ID,
                video=FSInputFile(download_path),
                caption=f"Instagram Video from {actor_name}",
            )
            db_id = sent.video.file_id
            await db.add_file(db_video_url, db_id, "video")
        else:
            await _edit_inline_status(bm.uploading_status())

        bot_url = await get_bot_url(bot)
        edited = await safe_edit_inline_media(
            bot,
            inline_message_id,
            types.InputMediaVideo(
                media=db_id,
                caption=bm.captions(request.user_settings["captions"], data.description, bot_url),
                parse_mode="HTML",
            ),
            reply_markup=kb.return_video_info_keyboard(
                None,
                None,
                None,
                None,
                None,
                db_video_url,
                request.user_settings,
                audio_callback_data=audio_callback_data,
            ),
        )
        if edited:
            complete_inline_video_request(token)
            return

        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    except DownloadRateLimitError as e:
        reset_inline_video_request(token)
        await _edit_inline_status(build_rate_limit_text(e.retry_after), with_retry_button=True)
    except DownloadQueueBusyError as e:
        reset_inline_video_request(token)
        await _edit_inline_status(build_queue_busy_text(e.position), with_retry_button=True)
    except Exception:
        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    finally:
        if download_path:
            await remove_file(download_path)


@router.chosen_inline_result(F.result_id.startswith("instagram_inline:"))
@with_chosen_inline_logging("instagram", "chosen_inline")
async def chosen_inline_instagram_result(result: types.ChosenInlineResult):
    if not result.inline_message_id:
        logging.warning("Chosen inline Instagram result is missing inline_message_id")
        return

    token = result.result_id.removeprefix("instagram_inline:")
    await _send_inline_instagram_video(
        token=token,
        inline_message_id=result.inline_message_id,
        actor_name=result.from_user.full_name,
        actor_user_id=getattr(result.from_user, "id", None),
        request_event_id=result.result_id,
        duplicate_handler="chosen",
    )


@router.callback_query(F.data.startswith("inline:instagram:"))
@with_callback_logging("instagram", "inline_callback")
async def send_inline_instagram_video_callback(call: types.CallbackQuery):
    if not call.inline_message_id:
        await call.answer("This button works only in inline mode.", show_alert=True)
        return

    token = call.data.removeprefix("inline:instagram:")
    await call.answer()
    try:
        await _send_inline_instagram_video(
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
