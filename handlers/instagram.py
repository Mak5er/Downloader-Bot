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
from handlers.deps import build_handler_dependencies
from handlers.instagram_inline import handle_instagram_inline_query, send_inline_instagram_media
from handlers.request_dedupe import claim_message_request
from handlers.user import update_info
from handlers.utils import (
    build_queue_busy_text,
    build_rate_limit_text,
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
from services.logger import logger as logging, summarize_text_for_log, summarize_url_for_log
from app_context import bot, db, send_analytics
from services.media.delivery import send_cached_media_entries
from services.media.orchestration import handle_download_backpressure, run_single_media_flow
from services.media.resolver import resolve_cached_media_items
from services.platforms.instagram_media import (
    InstagramMedia,
    InstagramMediaService,
    InstagramVideo,
    DownloadError,
    strip_instagram_url,
)
from services.inline.service_icons import get_inline_service_icon
from utils.cobalt_client import fetch_cobalt_data
from utils.download_manager import (
    DownloadQueueBusyError,
    DownloadRateLimitError,
    DownloadMetrics,
    log_download_metrics,
)
from utils.media_cache import build_media_cache_key

logging = logging.bind(service="instagram")

router = Router()

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)

__all__ = [
    "DownloadError",
    "get_inline_service_icon",
]

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
    request_lease = None
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

        request_lease = await claim_message_request(message, service="instagram", url=url)
        if request_lease is None:
            return

        logging.info("Instagram request: user_id=%s url=%s", message.from_user.id, summarize_url_for_log(url))
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
            if await process_instagram_video(message, data, url, bot_url, user_settings, business_id):
                request_lease.mark_success()
        elif has_photos or len(data.media_list) > 1:
            if await process_instagram_media_group(message, data, url, bot_url, user_settings, business_id):
                request_lease.mark_success()
        else:
            await handle_download_error(message, business_id=business_id)

    except Exception as e:
        logging.exception(
            "Error processing Instagram message: user_id=%s text=%s error=%s",
            message.from_user.id,
            summarize_text_for_log(get_message_text(message)),
            e,
        )
        await handle_download_error(message)
    finally:
        if request_lease is not None:
            request_lease.finish()
        await update_info(message)


async def process_instagram_url(message: types.Message, url: Optional[str] = None):
    """Backward-compatible entrypoint used by pending-request flow."""
    await process_instagram(message, direct_url=url)


async def process_instagram_video(message: types.Message, data: InstagramVideo, original_url: str, bot_url: str,
                                  user_settings: dict, business_id: Optional[int]):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="instagram_video")

    if not data.media_list or data.media_list[0].type != "video":
        await handle_download_error(message, business_id=business_id)
        return False

    audio_callback_data = f"audio:inst:{original_url}"
    media = data.media_list[0]

    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    download_name = f"{data.id}_{timestamp}_instagram_video.mp4"
    db_video_url = original_url
    show_service_status = business_id is None
    status_message: Optional[types.Message] = None
    if show_service_status:
        status_message = await message.answer(bm.downloading_video_status())

    async def _edit_status(text: str) -> None:
        await safe_edit_text(status_message, text)

    on_progress = make_status_text_progress_updater("Instagram video", _edit_status)
    on_retry = make_retry_status_notifier(
        _edit_status,
        enabled=show_service_status,
    )

    def _reply_markup():
        return kb.return_video_info_keyboard(
            None, None, None, None, "", db_video_url, user_settings,
            audio_callback_data=audio_callback_data,
        )

    async def _send_cached(file_id: str):
        logging.info(
            "Serving cached Instagram video: url=%s file_id=%s",
            summarize_url_for_log(db_video_url),
            file_id,
        )
        return await message.reply_video(
            video=file_id,
            caption=bm.captions(user_settings["captions"], data.description, bot_url),
            reply_markup=_reply_markup(),
            parse_mode="HTML",
        )

    async def _download_media():
        return await inst_service.download_media(
            media.url,
            download_name,
            user_id=message.from_user.id,
            on_progress=on_progress,
            on_retry=on_retry,
        )

    async def _send_downloaded(path: str):
        return await message.reply_video(
            video=FSInputFile(path),
            caption=bm.captions(user_settings["captions"], data.description, bot_url),
            reply_markup=_reply_markup(),
            parse_mode="HTML",
        )

    async def _after_send():
        await maybe_delete_user_message(message, user_settings["delete_message"])

    async def _inspect_metrics(metrics: DownloadMetrics) -> bool:
        log_download_metrics("instagram_video", metrics)
        if metrics.size >= MAX_FILE_SIZE:
            logging.warning(
                "Instagram video too large: url=%s size=%s",
                summarize_url_for_log(db_video_url),
                metrics.size,
            )
            await handle_download_error(message, business_id=business_id)
            return False
        return True

    async def _handle_backpressure(exc: Exception) -> None:
        await handle_download_backpressure(
            exc,
            business_id=business_id,
            on_rate_limit_reply=lambda retry_after: message.reply(build_rate_limit_text(retry_after)),
            on_queue_busy_reply=lambda position: message.reply(build_queue_busy_text(position)),
            on_business_error=lambda: handle_download_error(message, business_id=business_id),
        )

    async def _handle_cache_store_error(exc: Exception) -> None:
        logging.error("Error caching Instagram video: url=%s error=%s", summarize_url_for_log(db_video_url), exc)

    async def _handle_unexpected_error(exc: Exception) -> None:
        logging.exception(
            "Error processing Instagram video: url=%s error=%s",
            summarize_url_for_log(db_video_url),
            exc,
        )
        await handle_download_error(message, business_id=business_id)

    sent_message = await run_single_media_flow(
        cache_key=db_video_url,
        cache_file_type="video",
        db_service=db,
        upload_status_text=bm.uploading_status(),
        upload_action="upload_video",
        update_status=_edit_status,
        send_chat_action=lambda action: send_chat_action_if_needed(bot, message.chat.id, action, business_id),
        send_cached=_send_cached,
        download_media=_download_media,
        send_downloaded=_send_downloaded,
        extract_file_id=lambda sent: sent.video.file_id if getattr(sent, "video", None) else None,
        cleanup_path=remove_file,
        delete_status_message=lambda: safe_delete_message(status_message),
        on_missing_media=lambda: handle_download_error(message, business_id=business_id),
        on_after_send=_after_send,
        inspect_metrics=_inspect_metrics,
        on_cache_store_error=_handle_cache_store_error,
        on_rate_limit=_handle_backpressure,
        on_queue_busy=_handle_backpressure,
        on_unexpected_error=_handle_unexpected_error,
    )
    return sent_message is not None


async def process_instagram_media_group(message: types.Message, data: InstagramVideo, original_url: str, bot_url: str,
                                        user_settings: dict, business_id: Optional[int]):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="instagram_media_group")

    logging.info(
        "Sending Instagram media group: user_id=%s media_count=%s url=%s",
        message.from_user.id,
        len(data.media_list),
        summarize_url_for_log(original_url),
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
        return False

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
        return True
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
                summarize_url_for_log(original_url),
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
            logging.error("Failed to fetch Instagram audio: url=%s", summarize_url_for_log(original_url))
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
                summarize_url_for_log(original_url),
                sent_message.audio.file_id,
            )
        except Exception as e:
            logging.error("Error caching Instagram audio: url=%s error=%s", summarize_url_for_log(original_url), e)

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
            summarize_url_for_log(original_url),
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
    deps = build_handler_dependencies(bot=bot, db=db, send_analytics=send_analytics)
    await handle_instagram_inline_query(
        query,
        deps=deps,
        inst_service=inst_service,
        channel_id=CHANNEL_ID,
        get_bot_url_fn=get_bot_url,
        safe_answer_inline_query_fn=safe_answer_inline_query,
    )


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
    deps = build_handler_dependencies(bot=bot, db=db, send_analytics=send_analytics)
    await send_inline_instagram_media(
        token=token,
        inline_message_id=inline_message_id,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        request_event_id=request_event_id,
        duplicate_handler=duplicate_handler,
        deps=deps,
        inst_service=inst_service,
        channel_id=CHANNEL_ID,
        max_file_size=MAX_FILE_SIZE,
        get_bot_url_fn=get_bot_url,
        safe_edit_inline_media_fn=safe_edit_inline_media,
        safe_edit_inline_text_fn=safe_edit_inline_text,
    )


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
