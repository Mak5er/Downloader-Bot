import asyncio
import datetime
import re
from typing import Optional

from aiogram import F, Router, types
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramBadRequest

import keyboards as kb
import messages as bm
from config import CHANNEL_ID, COBALT_API_KEY, COBALT_API_URL, OUTPUT_DIR, MAX_FILE_SIZE
from handlers.deps import build_handler_dependencies
from handlers.pinterest_inline import handle_pinterest_inline_query, send_inline_pinterest_media
from handlers.request_dedupe import claim_message_request
from services.media.delivery import send_cached_media_entries
from services.media.video_metadata import build_video_send_kwargs
from services.media.orchestration import (
    make_message_backpressure_handler,
    run_media_group_flow,
    run_single_media_flow,
)
from services.platforms.pinterest_media import (
    PinterestMedia,
    PinterestMediaService,
    PinterestPost,
    DownloadError,
    parse_pinterest_post,
    strip_pinterest_url,
)
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
    register_inline_send_handlers,
    retry_async_operation,
    safe_delete_message,
    safe_edit_text,
    safe_edit_inline_media,
    safe_edit_inline_text,
    safe_answer_inline_query,
    send_chat_action_if_needed,
    should_skip_duplicate_business_message,
    with_inline_query_logging,
    with_inline_send_logging,
    with_message_logging,
)
from services.logger import logger as logging, summarize_text_for_log, summarize_url_for_log
from app_context import bot, db, send_analytics
from services.inline.service_icons import get_inline_service_icon
from utils.cobalt_client import fetch_cobalt_data
from utils.download_manager import (
    DownloadMetrics,
    log_download_metrics,
)
from utils.media_cache import build_media_cache_key

logging = logging.bind(service="pinterest")

router = Router()

PINTEREST_URL_REGEX = r"(https?://(?:[\w-]+\.)?pinterest\.[\w.]+/\S+|https?://pin\.it/\S+)"

__all__ = [
    "DownloadError",
    "get_inline_service_icon",
    "parse_pinterest_post",
]

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
    request_lease = None
    try:
        business_id = message.business_connection_id
        text = get_message_text(message)
        bot_url = await get_bot_url(bot)
        if await should_skip_duplicate_business_message(message, bot, service_name="Pinterest", logger=logging):
            return

        if direct_url:
            source_url = strip_pinterest_url(direct_url)
        else:
            url_match = re.search(PINTEREST_URL_REGEX, text or "")
            if not url_match:
                return
            source_url = strip_pinterest_url(url_match.group(0))

        request_lease = await claim_message_request(message, service="pinterest", url=source_url)
        if request_lease is None:
            return

        logging.debug("Pinterest request: user_id=%s url=%s", message.from_user.id, summarize_url_for_log(source_url))
        await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="pinterest_media")
        await react_to_message(message, "\U0001F47E", business_id=business_id)
        user_settings = await load_user_settings(db, message)

        post = await pinterest_service.fetch_post(source_url)
        if not post or not post.media_list:
            await handle_download_error(message, business_id=business_id)
            return

        first = post.media_list[0]
        if len(post.media_list) == 1 and first.type in ("video", "photo"):
            if await process_pinterest_single_media(
                message, post, source_url, bot_url, user_settings, business_id, first.type
            ):
                request_lease.mark_success()
            return
        if await process_pinterest_media_group(message, post, source_url, bot_url, user_settings, business_id):
            request_lease.mark_success()
    except Exception as exc:
        logging.exception(
            "Error processing Pinterest message: user_id=%s text=%s error=%s",
            message.from_user.id,
            summarize_text_for_log(get_message_text(message)),
            exc,
        )
        await handle_download_error(message)
    finally:
        if request_lease is not None:
            request_lease.finish()
        await update_info(message)


async def process_pinterest_url(message: types.Message, url: Optional[str] = None):
    await process_pinterest(message, direct_url=url)


async def process_pinterest_single_media(
    message: types.Message,
    post: PinterestPost,
    source_url: str,
    bot_url: str,
    user_settings: dict,
    business_id: Optional[int],
    media_kind: str,
):
    media = post.media_list[0]
    status_message: Optional[types.Message] = None
    if business_id is None:
        status_message = await message.answer(bm.downloading_video_status())

    extension = "mp4"
    if media_kind == "photo":
        extension = "jpg"
        low_url = media.url.lower().split("?", 1)[0]
        if low_url.endswith(".png"):
            extension = "png"
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"{post.id}_{timestamp}_pinterest_{media_kind}.{extension}"
    cache_key = source_url if media_kind == "video" else f"{source_url}#photo"

    async def _edit_status(text: str) -> None:
        await safe_edit_text(status_message, text)

    on_progress = make_status_text_progress_updater(f"Pinterest {media_kind}", _edit_status)
    on_retry = make_retry_status_notifier(_edit_status, enabled=business_id is None)

    def _reply_markup():
        return kb.return_video_info_keyboard(
            None, None, None, None, None, source_url, user_settings,
            audio_callback_data=None,
        )

    async def _download_media():
        if media_kind == "video":
            return await asyncio.wait_for(
                pinterest_service.download_media(
                    media.url,
                    filename,
                    user_id=message.from_user.id,
                    chat_id=message.chat.id,
                    on_progress=on_progress,
                    on_retry=on_retry,
                ),
                timeout=420.0,
            )
        return await pinterest_service.download_media(
            media.url,
            filename,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            request_id=f"pinterest_photo:{message.chat.id}:{message.message_id}:{post.id}",
        )

    async def _send_cached(file_id: str):
        try:
            if media_kind == "video":
                return await message.reply_video(
                    video=file_id,
                    caption=bm.captions(user_settings["captions"], post.description, bot_url),
                    reply_markup=_reply_markup(),
                    parse_mode="HTML",
                )
            return await message.reply_photo(
                photo=file_id,
                caption=bm.captions(user_settings["captions"], post.description, bot_url),
                reply_markup=_reply_markup(),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            return None

    async def _send_downloaded(path: str):
        if media_kind == "video":
            return await message.reply_video(
                video=FSInputFile(path),
                caption=bm.captions(user_settings["captions"], post.description, bot_url),
                reply_markup=_reply_markup(),
                parse_mode="HTML",
                **(await build_video_send_kwargs(path)),
            )
        return await message.reply_photo(
            photo=FSInputFile(path),
            caption=bm.captions(user_settings["captions"], post.description, bot_url),
            reply_markup=_reply_markup(),
            parse_mode="HTML",
        )

    async def _inspect_metrics(metrics: DownloadMetrics) -> bool:
        log_download_metrics(f"pinterest_{media_kind}", metrics)
        if media_kind == "video" and metrics.size >= MAX_FILE_SIZE:
            await handle_download_error(message, business_id=business_id, text=bm.video_too_large())
            return False
        return True

    _handle_backpressure = (
        make_message_backpressure_handler(
            message,
            business_id,
            build_rate_limit_text=build_rate_limit_text,
            build_queue_busy_text=build_queue_busy_text,
            handle_download_error=handle_download_error,
        )
        if media_kind == "video"
        else None
    )

    sent_message = await run_single_media_flow(
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
        extract_file_id=lambda sent: (
            sent.video.file_id if media_kind == "video" and getattr(sent, "video", None)
            else sent.photo[-1].file_id if media_kind == "photo" and getattr(sent, "photo", None)
            else None
        ),
        cleanup_path=remove_file,
        delete_status_message=lambda: safe_delete_message(status_message),
        on_missing_media=lambda: handle_download_error(message, business_id=business_id),
        on_after_send=lambda: maybe_delete_user_message(message, user_settings["delete_message"]),
        inspect_metrics=_inspect_metrics,
        on_rate_limit=_handle_backpressure,
        on_queue_busy=_handle_backpressure,
        user_id=message.from_user.id if message.from_user else None,
        chat_id=message.chat.id,
        chat_type=getattr(message.chat, "type", None),
        service="pinterest",
        url=source_url,
    )
    return sent_message is not None


async def process_pinterest_single_video(
    message: types.Message,
    post: PinterestPost,
    source_url: str,
    bot_url: str,
    user_settings: dict,
    business_id: Optional[int],
):
    return await process_pinterest_single_media(
        message, post, source_url, bot_url, user_settings, business_id, "video"
    )


async def process_pinterest_single_photo(
    message: types.Message,
    post: PinterestPost,
    source_url: str,
    bot_url: str,
    user_settings: dict,
    business_id: Optional[int],
):
    return await process_pinterest_single_media(
        message, post, source_url, bot_url, user_settings, business_id, "photo"
    )


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
            chat_id=message.chat.id,
            request_id=request_id,
        )

    async def _send_entries(media_items: list[dict]) -> None:
        caption = bm.captions(user_settings["captions"], post.description, bot_url)
        keyboard = kb.return_video_info_keyboard(
            None, None, None, None, None, source_url, user_settings,
            audio_callback_data=None,
        )
        await send_cached_media_entries(
            message,
            media_items,
            db_service=db,
            caption=caption,
            reply_markup=keyboard,
            parse_mode="HTML",
            kind_key="type",
        )

    return await run_media_group_flow(
        media_list=post.media_list,
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
        update_status=lambda text: safe_edit_text(status_message, text),
        upload_status_text=bm.uploading_status(),
        send_entries=_send_entries,
        on_empty=lambda: handle_download_error(message, business_id=business_id),
        delete_status_message=lambda: safe_delete_message(status_message),
        cleanup_path=remove_file,
        on_after_send=lambda: maybe_delete_user_message(message, user_settings["delete_message"]),
    )


@router.inline_query(F.query.regexp(PINTEREST_URL_REGEX, mode="search"))
@with_inline_query_logging("pinterest", "inline_query")
async def inline_pinterest_query(query: types.InlineQuery):
    deps = build_handler_dependencies(bot=bot, db=db, send_analytics=send_analytics)
    await handle_pinterest_inline_query(
        query,
        deps=deps,
        pinterest_service=pinterest_service,
        pinterest_url_regex=PINTEREST_URL_REGEX,
        strip_pinterest_url=strip_pinterest_url,
        channel_id=CHANNEL_ID,
        get_bot_url_fn=get_bot_url,
        safe_answer_inline_query_fn=safe_answer_inline_query,
    )


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
    deps = build_handler_dependencies(bot=bot, db=db, send_analytics=send_analytics)
    await send_inline_pinterest_media(
        token=token,
        inline_message_id=inline_message_id,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        request_event_id=request_event_id,
        duplicate_handler=duplicate_handler,
        deps=deps,
        pinterest_service=pinterest_service,
        channel_id=CHANNEL_ID,
        max_file_size=MAX_FILE_SIZE,
        get_bot_url_fn=get_bot_url,
        safe_edit_inline_media_fn=safe_edit_inline_media,
        safe_edit_inline_text_fn=safe_edit_inline_text,
    )


chosen_inline_pinterest_result, send_inline_pinterest_video_callback = (
    register_inline_send_handlers(
        router,
        service="pinterest",
        result_prefix="pinterest_inline:",
        callback_prefix="inline:pinterest:",
        send_fn=_send_inline_pinterest_video,
        missing_inline_message_warning=(
            "Chosen inline Pinterest result is missing inline_message_id"
        ),
        chosen_handler_name="chosen_inline_pinterest_result",
        callback_handler_name="send_inline_pinterest_video_callback",
    )
)
