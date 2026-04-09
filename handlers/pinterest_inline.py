import datetime
import re
from typing import Optional

from aiogram import types
from aiogram.types import FSInputFile

import keyboards as kb
import messages as bm
from handlers.deps import HandlerDependencies
from handlers.utils import (
    build_inline_album_result,
    build_inline_status_editor,
    build_queue_busy_text,
    build_rate_limit_text,
    build_start_deeplink_url,
    get_bot_url,
    make_status_text_progress_updater,
    remove_file,
    safe_answer_inline_query,
    safe_edit_inline_media,
    safe_edit_inline_text,
)
from log.logger import logger as logging, summarize_text_for_log, summarize_url_for_log
from services.inline.album_links import create_inline_album_request
from services.inline.service_icons import get_inline_service_icon
from services.inline.video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)
from services.platforms.pinterest_media import get_pinterest_preview_url as _get_pinterest_preview_url
from utils.download_manager import (
    DownloadQueueBusyError,
    DownloadRateLimitError,
    log_download_metrics,
)
from utils.media_cache import build_media_cache_key

logging = logging.bind(service="pinterest_inline")


async def handle_pinterest_inline_query(
    query: types.InlineQuery,
    *,
    deps: HandlerDependencies,
    pinterest_service,
    pinterest_url_regex: str,
    strip_pinterest_url,
    channel_id: Optional[int],
    get_bot_url_fn=get_bot_url,
    safe_answer_inline_query_fn=safe_answer_inline_query,
) -> None:
    try:
        await deps.send_analytics(
            user_id=query.from_user.id,
            chat_type=query.chat_type,
            action_name="inline_pinterest_video",
        )
        match = re.search(pinterest_url_regex, query.query or "")
        if not match:
            await query.answer([], cache_time=1, is_personal=True)
            return
        if not channel_id:
            logging.error("CHANNEL_ID is not configured; Pinterest inline is disabled")
            await query.answer([], cache_time=1, is_personal=True)
            return

        source_url = strip_pinterest_url(match.group(0))
        user_settings = await deps.db.user_settings(query.from_user.id)
        bot_url = await get_bot_url_fn(deps.bot)

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
            db_id = await deps.db.get_file_id(cache_key)
            if not db_id and not channel_id:
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
            await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
            return

        if len(post.media_list) > 1:
            preview_file_id = None
            if first.type == "photo" and channel_id:
                cache_key = build_media_cache_key(source_url, item_index=0, item_kind="photo")
                preview_file_id = await deps.db.get_file_id(cache_key)
                if not preview_file_id:
                    try:
                        sent = await deps.bot.send_photo(
                            chat_id=channel_id,
                            photo=first.url,
                            caption="Pinterest Album Preview",
                        )
                        if sent.photo:
                            preview_file_id = sent.photo[-1].file_id
                            await deps.db.add_file(cache_key, preview_file_id, "photo")
                    except Exception as exc:
                        logging.warning(
                            "Failed to cache Pinterest album preview photo: url=%s error=%s",
                            summarize_url_for_log(source_url),
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
            await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
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
                await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
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
            await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
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
        await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
    except Exception as exc:
        logging.exception(
            "Error processing Pinterest inline query: user_id=%s query=%s error=%s",
            query.from_user.id,
            summarize_text_for_log(query.query),
            exc,
        )
        await query.answer([], cache_time=1, is_personal=True)


async def send_inline_pinterest_media(
    *,
    token: str,
    inline_message_id: str,
    actor_name: str,
    actor_user_id: int,
    request_event_id: str,
    duplicate_handler: str,
    deps: HandlerDependencies,
    pinterest_service,
    channel_id: Optional[int],
    max_file_size: int,
    get_bot_url_fn=get_bot_url,
    safe_edit_inline_media_fn=safe_edit_inline_media,
    safe_edit_inline_text_fn=safe_edit_inline_text,
) -> None:
    request = claim_inline_video_request_for_send(
        token,
        duplicate_handler=duplicate_handler,
        actor_user_id=actor_user_id,
    )
    if request is None:
        return

    download_path: Optional[str] = None

    _edit_inline_status = build_inline_status_editor(
        bot=deps.bot,
        inline_message_id=inline_message_id,
        callback_data_factory=lambda _media_kind: f"inline:pinterest:{token}",
        safe_edit_inline_text_fn=safe_edit_inline_text_fn,
    )

    try:
        post = await pinterest_service.fetch_post(request.source_url)
        if not post or len(post.media_list) != 1:
            complete_inline_video_request(token)
            await _edit_inline_status(bm.inline_photos_not_supported("Pinterest"))
            return

        first_item = post.media_list[0]
        if first_item.type == "photo":
            cache_key = f"{request.source_url}#photo"
            db_id = await deps.db.get_file_id(cache_key)
            if not db_id:
                if not channel_id:
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return

                await _edit_inline_status(bm.uploading_status(), media_kind="photo")
                sent = await deps.bot.send_photo(
                    chat_id=channel_id,
                    photo=first_item.url,
                    caption=f"Pinterest Photo from {actor_name}",
                )
                if not sent.photo:
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return
                db_id = sent.photo[-1].file_id
                await deps.db.add_file(cache_key, db_id, "photo")
            else:
                await _edit_inline_status(bm.uploading_status(), media_kind="photo")

            bot_url = await get_bot_url_fn(deps.bot)
            edited = await safe_edit_inline_media_fn(
                deps.bot,
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

        db_id = await deps.db.get_file_id(request.source_url)
        if not db_id:
            if not channel_id:
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
            if metrics.size >= max_file_size:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.video_too_large())
                return

            await _edit_inline_status(bm.uploading_status())
            sent = await deps.bot.send_video(
                chat_id=channel_id,
                video=FSInputFile(download_path),
                caption=f"Pinterest Video from {actor_name}",
            )
            db_id = sent.video.file_id
            await deps.db.add_file(request.source_url, db_id, "video")
        else:
            await _edit_inline_status(bm.uploading_status())

        bot_url = await get_bot_url_fn(deps.bot)
        edited = await safe_edit_inline_media_fn(
            deps.bot,
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
    except DownloadRateLimitError as exc:
        reset_inline_video_request(token)
        await _edit_inline_status(build_rate_limit_text(exc.retry_after), with_retry_button=True)
    except DownloadQueueBusyError as exc:
        reset_inline_video_request(token)
        await _edit_inline_status(build_queue_busy_text(exc.position), with_retry_button=True)
    except Exception:
        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    finally:
        if download_path:
            await remove_file(download_path)
