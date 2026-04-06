import asyncio
import datetime
import re
from typing import Optional

from aiogram import types, Router, F
from aiogram.types import FSInputFile

import keyboards as kb
import messages as bm
from config import (
    CHANNEL_ID,
    OUTPUT_DIR,
    COBALT_API_URL,
    COBALT_API_KEY,
)
from handlers.media_delivery import send_cached_media_entries
from services.instagram_media import (
    InstagramMedia,
    InstagramMediaService,
    InstagramVideo,
    get_instagram_preview_url as _get_instagram_preview_url,
    strip_instagram_url,
)
from handlers.media_resolver import resolve_cached_media_items
from handlers.user import update_info
from handlers.utils import (
    build_inline_album_result,
    build_request_id,
    build_queue_busy_text,
    build_rate_limit_text,
    build_start_deeplink_url,
    get_bot_url,
    get_message_text,
    handle_download_error,
    load_user_settings,
    make_retry_status_notifier,
    make_status_text_progress_updater,
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
    with_callback_logging,
    with_chosen_inline_logging,
    with_inline_query_logging,
    with_inline_send_logging,
    with_message_logging,
)
from log.logger import logger as logging
from app_context import bot, db, send_analytics
from utils.cobalt_client import fetch_cobalt_data
from utils.download_manager import (
    DownloadError,
    DownloadProgress,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    DownloadMetrics,
    log_download_metrics,
)
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

class InstagramService(InstagramMediaService):
    def __init__(self, output_dir: str) -> None:
        super().__init__(
            output_dir,
            cobalt_api_url=COBALT_API_URL,
            cobalt_api_key=COBALT_API_KEY,
            fetch_cobalt_data_func=fetch_cobalt_data,
            retry_async_operation_func=retry_async_operation,
        )


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
        user_settings = await load_user_settings(db, message)
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

        async def _edit_status(text: str) -> None:
            await safe_edit_text(status_message, text)

        on_progress = make_status_text_progress_updater("Instagram video", _edit_status)
        on_retry = make_retry_status_notifier(
            _edit_status,
            enabled=show_service_status,
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

    request_id = f"instagram_group:{message.chat.id}:{message.message_id}:{data.id}"
    status_message: Optional[types.Message] = None
    if business_id is None:
        status_message = await message.answer(bm.downloading_video_status())

    async def _download_item(index: int, item: InstagramMedia, media_kind: str):
        ext = "mp4" if media_kind == "video" else "jpg"
        filename = f"inst_{data.id}_{index}.{ext}"
        return await inst_service.download_media(
            item.url,
            filename,
            user_id=message.from_user.id,
            request_id=request_id,
        )

    media_items, downloaded_paths = await resolve_cached_media_items(
        data.media_list,
        db_service=db,
        kind_getter=lambda item: item.type,
        build_cache_key=lambda index, _item, media_kind: build_media_cache_key(
            original_url,
            item_index=index,
            item_kind=media_kind,
        ),
        download_item=_download_item,
        metrics_label="instagram_group",
        error_label="Instagram",
    )

    if not media_items:
        await handle_download_error(message, business_id=business_id)
        return

    try:
        await safe_edit_text(status_message, bm.uploading_status())
        await send_cached_media_entries(
            message,
            media_items,
            db_service=db,
            caption=bm.captions(user_settings["captions"], data.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                None, None, None, None, "", original_url, user_settings,
                audio_callback_data=None,
            ),
            parse_mode="HTML",
            kind_key="type",
        )

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

        async def _edit_status(text: str) -> None:
            await safe_edit_text(status_message, text)

        on_progress = make_status_text_progress_updater("Instagram audio", _edit_status)
        on_retry = make_retry_status_notifier(
            _edit_status,
            enabled=show_service_status,
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

            await _edit_inline_status(bm.downloading_video_status())

            on_progress = make_status_text_progress_updater("Instagram video", _edit_inline_status)

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
