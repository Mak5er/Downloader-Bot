import asyncio
import datetime
import re
from typing import Optional

from aiogram import F, Router, types
from aiogram.types import FSInputFile

import keyboards as kb
import messages as bm
from config import CHANNEL_ID, COBALT_API_KEY, COBALT_API_URL, OUTPUT_DIR
from services.media.delivery import send_cached_media_entries
from services.media.resolver import resolve_cached_media_items
from services.platforms.pinterest_media import (
    PinterestMedia,
    PinterestMediaService,
    PinterestPost,
    get_pinterest_preview_url as _get_pinterest_preview_url,
    parse_pinterest_post,
    strip_pinterest_url,
)
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
from utils.cobalt_client import fetch_cobalt_data
from utils.download_manager import (
    DownloadError,
    DownloadMetrics,
    DownloadProgress,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    log_download_metrics,
)
from utils.media_cache import build_media_cache_key
from services.inline.album_links import create_inline_album_request
from services.inline.service_icons import get_inline_service_icon
from services.inline.video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)

logging = logging.bind(service="pinterest")

router = Router()

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)
PINTEREST_URL_REGEX = r"(https?://(?:[\w-]+\.)?pinterest\.[\w.]+/\S+|https?://pin\.it/\S+)"

class PinterestService(PinterestMediaService):
    def __init__(self, output_dir: str) -> None:
        super().__init__(
            output_dir,
            cobalt_api_url=COBALT_API_URL,
            cobalt_api_key=COBALT_API_KEY,
            fetch_cobalt_data_func=fetch_cobalt_data,
            retry_async_operation_func=retry_async_operation,
        )


pinterest_service = PinterestService(OUTPUT_DIR)


@router.message(
    F.text.regexp(PINTEREST_URL_REGEX, mode="search") | F.caption.regexp(PINTEREST_URL_REGEX, mode="search")
)
@router.business_message(
    F.text.regexp(PINTEREST_URL_REGEX, mode="search") | F.caption.regexp(PINTEREST_URL_REGEX, mode="search")
)
@with_message_logging("pinterest", "message")
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
        user_settings = await load_user_settings(db, message)

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
    status_message: Optional[types.Message] = None
    if business_id is None:
        status_message = await message.answer(bm.downloading_video_status())
    db_file_id = await db.get_file_id(source_url)
    download_path: Optional[str] = None
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    download_name = f"{post.id}_{timestamp}_pinterest_video.mp4"

    try:
        if db_file_id:
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
            await message.reply_video(
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

        async def _edit_status(text: str) -> None:
            await safe_edit_text(status_message, text)

        on_progress = make_status_text_progress_updater("Pinterest video", _edit_status)
        on_retry = make_retry_status_notifier(
            _edit_status,
            enabled=business_id is None,
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
        sent = await message.reply_video(
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
    status_message: Optional[types.Message] = None
    metrics: Optional[DownloadMetrics] = None
    if business_id is None:
        status_message = await message.answer(bm.downloading_video_status())
    db_file_id = await db.get_file_id(cache_key)
    try:
        if db_file_id:
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)
            await message.reply_photo(
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
        await safe_edit_text(status_message, bm.uploading_status())
        await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)
        sent = await message.reply_photo(
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
        await safe_delete_message(status_message)
        if metrics:
            await remove_file(metrics.path)


async def process_pinterest_media_group(
    message: types.Message,
    post: PinterestPost,
    source_url: str,
    bot_url: str,
    user_settings: dict,
    business_id: Optional[int],
):
    status_message: Optional[types.Message] = None
    if business_id is None:
        status_message = await message.answer(bm.downloading_video_status())
    await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)
    request_id = f"pinterest_group:{message.chat.id}:{message.message_id}:{post.id}"

    async def _download_item(index: int, item: PinterestMedia, media_kind: str):
        ext = "mp4" if media_kind == "video" else "jpg"
        filename = f"pin_{post.id}_{index}.{ext}"
        return await pinterest_service.download_media(
            item.url,
            filename,
            user_id=message.from_user.id,
            request_id=request_id,
        )

    media_items, downloaded_paths = await resolve_cached_media_items(
        post.media_list,
        db_service=db,
        kind_getter=lambda item: item.type,
        build_cache_key=lambda index, _item, media_kind: build_media_cache_key(
            source_url,
            item_index=index,
            item_kind=media_kind,
        ),
        download_item=_download_item,
        metrics_label="pinterest_group",
        error_label="Pinterest",
    )

    if not media_items:
        await handle_download_error(message, business_id=business_id)
        return

    try:
        caption = bm.captions(user_settings["captions"], post.description, bot_url)
        keyboard = kb.return_video_info_keyboard(
            None, None, None, None, None, source_url, user_settings,
            audio_callback_data=None,
        )
        await safe_edit_text(status_message, bm.uploading_status())
        await send_cached_media_entries(
            message,
            media_items,
            db_service=db,
            caption=caption,
            reply_markup=keyboard,
            parse_mode="HTML",
            kind_key="type",
        )
        await maybe_delete_user_message(message, user_settings["delete_message"])
    finally:
        await safe_delete_message(status_message)
        for path in downloaded_paths:
            await remove_file(path)


@router.inline_query(F.query.regexp(PINTEREST_URL_REGEX, mode="search"))
@with_inline_query_logging("pinterest", "inline_query")
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
        first = post.media_list[0]
        photo_items = [item for item in post.media_list if item.type == "photo"]
        first_photo = photo_items[0] if photo_items else None
        first_preview = _get_pinterest_preview_url(first) or next(
            (_get_pinterest_preview_url(item) for item in post.media_list if _get_pinterest_preview_url(item)),
            None,
        )

        if len(post.media_list) == 1 and first_photo:
            cache_key = f"{source_url}#photo"
            db_id = await db.get_file_id(cache_key)
            if not db_id and not CHANNEL_ID:
                logging.error("CHANNEL_ID is not configured; Pinterest inline photo send is disabled")
                await query.answer([], cache_time=1, is_personal=True)
                return

            token = create_inline_video_request("pinterest", source_url, query.from_user.id, user_settings)
            results = [
                types.InlineQueryResultArticle(
                    id=f"pinterest_inline:{token}",
                    title="Pinterest Photo",
                    description=post.description or "Press the button to send this photo inline.",
                    thumbnail_url=first_preview,
                    input_message_content=types.InputTextMessageContent(
                        message_text="Pinterest photo is being prepared...\nIf it does not start automatically, tap the button below.",
                    ),
                    reply_markup=kb.inline_send_media_keyboard(
                        "Send photo inline",
                        f"inline:pinterest:{token}",
                    ),
                )
            ]
            await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
            return

        if len(post.media_list) > 1:
            preview_file_id = None
            if first.type == "photo" and CHANNEL_ID:
                cache_key = build_media_cache_key(source_url, item_index=0, item_kind="photo")
                preview_file_id = await db.get_file_id(cache_key)
                if not preview_file_id:
                    try:
                        sent = await bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=first.url,
                            caption="Pinterest Album Preview",
                        )
                        if sent.photo:
                            preview_file_id = sent.photo[-1].file_id
                            await db.add_file(cache_key, preview_file_id, "photo")
                    except Exception as exc:
                        logging.warning(
                            "Failed to cache Pinterest album preview photo: url=%s error=%s",
                            source_url,
                            exc,
                        )
            token = create_inline_album_request(query.from_user.id, "pinterest", source_url)
            deep_link = build_start_deeplink_url(bot_url, f"album_{token}")
            results = [
                build_inline_album_result(
                    result_id=f"pinterest_album_{post.id}",
                    service_name="Pinterest",
                    deep_link=deep_link,
                    message_text=bm.captions(user_settings["captions"], post.description, bot_url),
                    preview_file_id=preview_file_id,
                    preview_url=first_preview,
                    thumbnail_url=first_preview or get_inline_service_icon("pinterest"),
                )
            ]
            await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
            return

        if first.type != "video":
            if first_photo:
                preview_url = _get_pinterest_preview_url(first_photo)
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
                await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
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
            await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
            return

        token = create_inline_video_request("pinterest", source_url, query.from_user.id, user_settings)
        preview_url = _get_pinterest_preview_url(first) or get_inline_service_icon("pinterest")
        results = [
            types.InlineQueryResultArticle(
                id=f"pinterest_inline:{token}",
                title="Pinterest Video",
                description=post.description or "Press the button to send this video inline.",
                thumbnail_url=preview_url,
                input_message_content=types.InputTextMessageContent(
                    message_text=bm.inline_send_video_prompt("Pinterest"),
                ),
                reply_markup=kb.inline_send_media_keyboard(
                    "Send video inline",
                    f"inline:pinterest:{token}",
                ),
            )
        ]
        await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
        return
    except Exception as exc:
        logging.exception(
            "Error processing Pinterest inline query: user_id=%s query=%s error=%s",
            query.from_user.id,
            query.query,
            exc,
        )
        await query.answer([], cache_time=1, is_personal=True)


@with_inline_send_logging("pinterest", "inline_send")
async def _send_inline_pinterest_video(
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
            kb.inline_send_media_keyboard(button_text, f"inline:pinterest:{token}")
            if with_retry_button
            else None
        )
        await safe_edit_inline_text(bot, inline_message_id, text, reply_markup=reply_markup)

    try:
        post = await pinterest_service.fetch_post(request.source_url)
        if not post or len(post.media_list) != 1:
            complete_inline_video_request(token)
            await _edit_inline_status(bm.inline_photos_not_supported("Pinterest"))
            return

        first_item = post.media_list[0]
        if first_item.type == "photo":
            cache_key = f"{request.source_url}#photo"
            db_id = await db.get_file_id(cache_key)
            if not db_id:
                if not CHANNEL_ID:
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return

                await _edit_inline_status(bm.uploading_status(), media_kind="photo")
                sent = await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=first_item.url,
                    caption=f"Pinterest Photo from {actor_name}",
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
                    caption=bm.captions(request.user_settings["captions"], post.description, bot_url),
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

        if first_item.type != "video":
            complete_inline_video_request(token)
            await _edit_inline_status(bm.inline_photos_not_supported("Pinterest"))
            return

        db_id = await db.get_file_id(request.source_url)
        if not db_id:
            if not CHANNEL_ID:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            filename = f"{post.id}_{timestamp}_pinterest_inline.mp4"
            await _edit_inline_status(bm.downloading_video_status())

            on_progress = make_status_text_progress_updater("Pinterest video", _edit_inline_status)

            metrics = await pinterest_service.download_media(
                first_item.url,
                filename,
                user_id=request.owner_user_id,
                request_id=f"pinterest_inline:{request.owner_user_id}:{request_event_id}:{post.id}",
                on_progress=on_progress,
            )
            if not metrics:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            log_download_metrics("pinterest_inline", metrics)
            download_path = metrics.path
            if metrics.size >= MAX_FILE_SIZE:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.video_too_large())
                return

            await _edit_inline_status(bm.uploading_status())
            sent = await bot.send_video(
                chat_id=CHANNEL_ID,
                video=FSInputFile(download_path),
                caption=f"Pinterest Video from {actor_name}",
            )
            db_id = sent.video.file_id
            await db.add_file(request.source_url, db_id, "video")
        else:
            await _edit_inline_status(bm.uploading_status())

        bot_url = await get_bot_url(bot)
        edited = await safe_edit_inline_media(
            bot,
            inline_message_id,
            types.InputMediaVideo(
                media=db_id,
                caption=bm.captions(request.user_settings["captions"], post.description, bot_url),
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


@router.chosen_inline_result(F.result_id.startswith("pinterest_inline:"))
@with_chosen_inline_logging("pinterest", "chosen_inline")
async def chosen_inline_pinterest_result(result: types.ChosenInlineResult):
    if not result.inline_message_id:
        logging.warning("Chosen inline Pinterest result is missing inline_message_id")
        return

    token = result.result_id.removeprefix("pinterest_inline:")
    await _send_inline_pinterest_video(
        token=token,
        inline_message_id=result.inline_message_id,
        actor_name=result.from_user.full_name,
        actor_user_id=getattr(result.from_user, "id", None),
        request_event_id=result.result_id,
        duplicate_handler="chosen",
    )


@router.callback_query(F.data.startswith("inline:pinterest:"))
@with_callback_logging("pinterest", "inline_callback")
async def send_inline_pinterest_video_callback(call: types.CallbackQuery):
    if not call.inline_message_id:
        await call.answer("This button works only in inline mode.", show_alert=True)
        return

    token = call.data.removeprefix("inline:pinterest:")
    await call.answer()
    try:
        await _send_inline_pinterest_video(
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
