import asyncio
import datetime
import re
from typing import Optional

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile

import keyboards as kb
import messages as bm
from app_context import bot, db, send_analytics
from config import CHANNEL_ID, MAX_FILE_SIZE, OUTPUT_DIR
from handlers.deps import build_handler_dependencies
from handlers.pinterest_inline import handle_pinterest_inline_query, send_inline_pinterest_media
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
    retry_async_operation,
    safe_delete_message,
    safe_answer_inline_query,
    safe_edit_inline_media,
    safe_edit_inline_text,
    safe_edit_text,
    send_chat_action_if_needed,
    should_skip_duplicate_business_message,
    with_callback_logging,
    with_chosen_inline_logging,
    with_inline_query_logging,
    with_inline_send_logging,
    with_message_logging,
)
from services.logger import logger as logging, summarize_text_for_log, summarize_url_for_log
from services.media.delivery import send_cached_media_entries
from services.media.orchestration import handle_download_backpressure, run_single_media_flow
from services.media.resolver import resolve_cached_media_items
from services.media.video_metadata import build_video_send_kwargs
from services.platforms.threads_media import (
    DownloadError,
    ThreadsMedia,
    ThreadsMediaService,
    ThreadsPost,
    get_threads_preview_url,
    strip_threads_url,
)
from utils.download_manager import DownloadMetrics, log_download_metrics
from utils.media_cache import build_media_cache_key

logging = logging.bind(service="threads")

router = Router()

THREADS_URL_REGEX = r"(https?://(?:www\.)?threads\.(?:com|net)/@[A-Za-z0-9._-]+/post/[A-Za-z0-9_-]+)"

__all__ = ["DownloadError", "strip_threads_url"]


class ThreadsService(ThreadsMediaService):
    def __init__(self, output_dir: str) -> None:
        super().__init__(output_dir, retry_async_operation_func=retry_async_operation)


threads_service = ThreadsService(OUTPUT_DIR)


@router.message(F.text.regexp(THREADS_URL_REGEX, mode="search") | F.caption.regexp(THREADS_URL_REGEX, mode="search"))
@router.business_message(F.text.regexp(THREADS_URL_REGEX, mode="search") | F.caption.regexp(THREADS_URL_REGEX, mode="search"))
@with_message_logging("threads", "message")
async def process_threads(message: types.Message, direct_url: Optional[str] = None) -> None:
    request_lease = None
    try:
        business_id = message.business_connection_id
        text = get_message_text(message)
        if await should_skip_duplicate_business_message(message, bot, service_name="Threads", logger=logging):
            return

        if direct_url:
            source_url = strip_threads_url(direct_url)
        else:
            match = re.search(THREADS_URL_REGEX, text or "")
            if not match:
                return
            source_url = strip_threads_url(match.group(0))

        request_lease = await claim_message_request(message, service="threads", url=source_url)
        if request_lease is None:
            return

        logging.info("Threads request: user_id=%s url=%s", message.from_user.id, summarize_url_for_log(source_url))
        await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="threads_media")
        await react_to_message(message, "🧵", business_id=business_id)
        user_settings = await load_user_settings(db, message)
        bot_url = await get_bot_url(bot)

        post = await threads_service.fetch_post(source_url)
        if not post:
            await handle_download_error(message, business_id=business_id)
            return

        if not post.media_list:
            sent = await process_threads_text_post(message, post, source_url, bot_url, user_settings)
        elif len(post.media_list) == 1:
            sent = await process_threads_single_media(
                message, post, source_url, bot_url, user_settings, business_id
            )
        else:
            sent = await process_threads_media_group(
                message, post, source_url, bot_url, user_settings, business_id
            )
        if sent:
            request_lease.mark_success()
    except Exception as exc:
        logging.exception(
            "Error processing Threads message: user_id=%s text=%s error=%s",
            message.from_user.id,
            summarize_text_for_log(get_message_text(message)),
            exc,
        )
        await handle_download_error(message)
    finally:
        if request_lease is not None:
            request_lease.finish()
        await update_info(message)


async def process_threads_url(message: types.Message, url: Optional[str] = None) -> None:
    await process_threads(message, direct_url=url)


def _threads_reply_markup(source_url: str, user_settings: dict):
    return kb.return_video_info_keyboard(
        None, None, None, None, None, source_url, user_settings, audio_callback_data=None
    )


async def process_threads_text_post(
    message: types.Message,
    post: ThreadsPost,
    source_url: str,
    bot_url: str,
    user_settings: dict,
) -> bool:
    """Return the text itself when a Threads post has no downloadable media."""
    sent = await message.reply(
        bm.captions("on", post.description, bot_url, limit=4096),
        reply_markup=_threads_reply_markup(source_url, user_settings),
        parse_mode="HTML",
    )
    if sent is None:
        return False
    await maybe_delete_user_message(message, user_settings["delete_message"])
    return True


async def process_threads_single_media(
    message: types.Message,
    post: ThreadsPost,
    source_url: str,
    bot_url: str,
    user_settings: dict,
    business_id: Optional[int],
) -> bool:
    media = post.media_list[0]
    media_kind = media.type
    status_message: Optional[types.Message] = None
    if business_id is None:
        status_message = await message.answer(bm.downloading_video_status())

    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    extension = "mp4" if media_kind == "video" else "jpg"
    filename = f"{post.id}_{timestamp}_threads_{media_kind}.{extension}"
    cache_key = source_url if media_kind == "video" else f"{source_url}#photo"

    async def _edit_status(text: str) -> None:
        await safe_edit_text(status_message, text)

    on_progress = make_status_text_progress_updater(f"Threads {media_kind}", _edit_status)
    on_retry = make_retry_status_notifier(_edit_status, enabled=business_id is None)

    async def _download_media() -> DownloadMetrics | None:
        return await asyncio.wait_for(
            threads_service.download_media(
                media.url,
                filename,
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                request_id=f"threads:{message.chat.id}:{message.message_id}:{post.id}",
                on_progress=on_progress,
                on_retry=on_retry,
            ),
            timeout=420.0,
        )

    async def _send_cached(file_id: str):
        try:
            if media_kind == "video":
                return await message.reply_video(
                    video=file_id,
                    caption=bm.captions(user_settings["captions"], post.description, bot_url),
                    reply_markup=_threads_reply_markup(source_url, user_settings),
                    parse_mode="HTML",
                )
            return await message.reply_photo(
                photo=file_id,
                caption=bm.captions(user_settings["captions"], post.description, bot_url),
                reply_markup=_threads_reply_markup(source_url, user_settings),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            return None

    async def _send_downloaded(path: str):
        if media_kind == "video":
            return await message.reply_video(
                video=FSInputFile(path),
                caption=bm.captions(user_settings["captions"], post.description, bot_url),
                reply_markup=_threads_reply_markup(source_url, user_settings),
                parse_mode="HTML",
                **(await build_video_send_kwargs(path)),
            )
        return await message.reply_photo(
            photo=FSInputFile(path),
            caption=bm.captions(user_settings["captions"], post.description, bot_url),
            reply_markup=_threads_reply_markup(source_url, user_settings),
            parse_mode="HTML",
        )

    async def _inspect_metrics(metrics: DownloadMetrics) -> bool:
        log_download_metrics(f"threads_{media_kind}", metrics)
        if metrics.size >= MAX_FILE_SIZE:
            await handle_download_error(message, business_id=business_id, text=bm.video_too_large())
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

    sent = await run_single_media_flow(
        cache_key=cache_key,
        cache_file_type=media_kind,
        db_service=db,
        upload_status_text=bm.uploading_status(),
        upload_action="upload_video" if media_kind == "video" else "upload_photo",
        update_status=_edit_status,
        send_chat_action=lambda action: send_chat_action_if_needed(bot, message.chat.id, action, business_id),
        send_cached=_send_cached,
        download_media=_download_media,
        send_downloaded=_send_downloaded,
        extract_file_id=lambda result: (
            result.video.file_id if media_kind == "video" and getattr(result, "video", None)
            else result.photo[-1].file_id if media_kind == "photo" and getattr(result, "photo", None)
            else None
        ),
        cleanup_path=remove_file,
        delete_status_message=lambda: safe_delete_message(status_message),
        on_missing_media=lambda: handle_download_error(message, business_id=business_id),
        on_after_send=lambda: maybe_delete_user_message(message, user_settings["delete_message"]),
        inspect_metrics=_inspect_metrics,
        on_rate_limit=_handle_backpressure,
        on_queue_busy=_handle_backpressure,
    )
    return sent is not None


async def process_threads_media_group(
    message: types.Message,
    post: ThreadsPost,
    source_url: str,
    bot_url: str,
    user_settings: dict,
    business_id: Optional[int],
) -> bool:
    status_message: Optional[types.Message] = None
    if business_id is None:
        status_message = await message.answer(bm.downloading_video_status())
    await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)
    request_id = f"threads_group:{message.chat.id}:{message.message_id}:{post.id}"

    async def _download_item(index: int, item: ThreadsMedia, media_kind: str) -> DownloadMetrics | None:
        extension = "mp4" if media_kind == "video" else "jpg"
        return await threads_service.download_media(
            item.url,
            f"threads_{post.id}_{index}.{extension}",
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            request_id=request_id,
        )

    media_items, downloaded_paths = await resolve_cached_media_items(
        post.media_list,
        db_service=db,
        kind_getter=lambda item: item.type,
        build_cache_key=lambda index, _item, media_kind: build_media_cache_key(
            source_url, item_index=index, item_kind=media_kind
        ),
        download_item=_download_item,
        metrics_label="threads_group",
        error_label="Threads",
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
            caption=bm.captions(user_settings["captions"], post.description, bot_url),
            reply_markup=_threads_reply_markup(source_url, user_settings),
            parse_mode="HTML",
            kind_key="type",
        )
        await maybe_delete_user_message(message, user_settings["delete_message"])
        return True
    finally:
        await safe_delete_message(status_message)
        for path in downloaded_paths:
            await remove_file(path)


@router.inline_query(F.query.regexp(THREADS_URL_REGEX, mode="search"))
@with_inline_query_logging("threads", "inline_query")
async def inline_threads_query(query: types.InlineQuery) -> None:
    deps = build_handler_dependencies(bot=bot, db=db, send_analytics=send_analytics)
    await handle_pinterest_inline_query(
        query,
        deps=deps,
        pinterest_service=threads_service,
        pinterest_url_regex=THREADS_URL_REGEX,
        strip_pinterest_url=strip_threads_url,
        channel_id=CHANNEL_ID,
        service_key="threads",
        service_name="Threads",
        analytics_action="inline_threads_media",
        get_preview_url=get_threads_preview_url,
        allow_text_only=True,
        get_bot_url_fn=get_bot_url,
        safe_answer_inline_query_fn=safe_answer_inline_query,
    )


@with_inline_send_logging("threads", "inline_send")
async def _send_inline_threads_media(
    *,
    token: str,
    inline_message_id: str,
    actor_name: str,
    actor_user_id: int,
    request_event_id: str,
    duplicate_handler: str,
) -> None:
    deps = build_handler_dependencies(bot=bot, db=db, send_analytics=send_analytics)
    await send_inline_pinterest_media(
        token=token,
        inline_message_id=inline_message_id,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        request_event_id=request_event_id,
        duplicate_handler=duplicate_handler,
        deps=deps,
        pinterest_service=threads_service,
        channel_id=CHANNEL_ID,
        max_file_size=MAX_FILE_SIZE,
        service_key="threads",
        service_name="Threads",
        get_bot_url_fn=get_bot_url,
        safe_edit_inline_media_fn=safe_edit_inline_media,
        safe_edit_inline_text_fn=safe_edit_inline_text,
    )


@router.chosen_inline_result(F.result_id.startswith("threads_inline:"))
@with_chosen_inline_logging("threads", "chosen_inline")
async def chosen_inline_threads_result(result: types.ChosenInlineResult) -> None:
    if not result.inline_message_id:
        logging.warning("Chosen inline Threads result is missing inline_message_id")
        return

    token = result.result_id.removeprefix("threads_inline:")
    await _send_inline_threads_media(
        token=token,
        inline_message_id=result.inline_message_id,
        actor_name=result.from_user.full_name,
        actor_user_id=getattr(result.from_user, "id", None),
        request_event_id=result.result_id,
        duplicate_handler="chosen",
    )


@router.callback_query(F.data.startswith("inline:threads:"))
@with_callback_logging("threads", "inline_callback")
async def send_inline_threads_media_callback(call: types.CallbackQuery) -> None:
    if not call.inline_message_id:
        await call.answer("This button works only in inline mode.", show_alert=True)
        return

    token = call.data.removeprefix("inline:threads:")
    await call.answer()
    try:
        await _send_inline_threads_media(
            token=token,
            inline_message_id=call.inline_message_id,
            actor_name=call.from_user.full_name,
            actor_user_id=call.from_user.id,
            request_event_id=str(call.id),
            duplicate_handler="callback",
        )
    except PermissionError:
        await call.answer(bm.something_went_wrong(), show_alert=True)
    except ValueError as exc:
        if str(exc) == "already_processing":
            await call.answer(bm.inline_video_already_processing(), show_alert=False)
        elif str(exc) == "already_completed":
            await call.answer(bm.inline_video_already_sent(), show_alert=False)
