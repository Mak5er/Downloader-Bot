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
from services.logger import logger as logging, summarize_text_for_log, summarize_url_for_log
from services.inline.album_links import create_inline_album_request
from services.inline.service_icons import get_inline_service_icon
from services.inline.video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)
from services.platforms.instagram_media import (
    get_instagram_preview_url as _get_instagram_preview_url,
    strip_instagram_url,
)
from utils.download_manager import (
    DownloadQueueBusyError,
    DownloadRateLimitError,
    log_download_metrics,
)
from utils.media_cache import build_media_cache_key

logging = logging.bind(service="instagram_inline")


async def handle_instagram_inline_query(
    query: types.InlineQuery,
    *,
    deps: HandlerDependencies,
    inst_service,
    channel_id: Optional[int],
    get_bot_url_fn=get_bot_url,
    safe_answer_inline_query_fn=safe_answer_inline_query,
) -> None:
    try:
        await deps.send_analytics(
            user_id=query.from_user.id,
            chat_type=query.chat_type,
            action_name="inline_instagram_video",
        )
        logging.info(
            "Inline Instagram request: user_id=%s query=%s",
            query.from_user.id,
            summarize_text_for_log(query.query),
        )

        user_settings = await deps.db.user_settings(query.from_user.id)
        bot_url = await get_bot_url_fn(deps.bot)

        url_match = re.search(r"(https?://(www\.)?instagram\.com/(p|reels|reel)/[^/?#&]+)", query.query)
        if not url_match:
            logging.debug("Inline Instagram query pattern not matched: query=%s", summarize_text_for_log(query.query))
            await query.answer([], cache_time=1, is_personal=True)
            return

        original_url = strip_instagram_url(url_match.group(0))

        data = await inst_service.fetch_data(original_url)
        if not data or not data.media_list:
            logging.warning("Inline Instagram fetch failed: url=%s", summarize_url_for_log(original_url))
            await query.answer([], cache_time=1, is_personal=True)
            return

        if len(data.media_list) == 1 and data.media_list[0].type == "video":
            db_id = await deps.db.get_file_id(original_url)
            if not db_id and not channel_id:
                logging.error("CHANNEL_ID is not configured; Instagram inline video send is disabled")
                await query.answer([], cache_time=1, is_personal=True)
                return

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
            await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
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
            db_id = await deps.db.get_file_id(cache_key)
            if not db_id and not channel_id:
                logging.error("CHANNEL_ID is not configured; Instagram inline photo send is disabled")
                await query.answer([], cache_time=1, is_personal=True)
                return

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
            await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
            return

        if len(data.media_list) > 1:
            preview_file_id = None
            if first_item and first_item.type == "photo" and channel_id:
                cache_key = build_media_cache_key(original_url, item_index=0, item_kind="photo")
                preview_file_id = await deps.db.get_file_id(cache_key)
                if not preview_file_id:
                    try:
                        sent = await deps.bot.send_photo(
                            chat_id=channel_id,
                            photo=first_item.url,
                            caption="Instagram Album Preview",
                        )
                        if sent.photo:
                            preview_file_id = sent.photo[-1].file_id
                            await deps.db.add_file(cache_key, preview_file_id, "photo")
                    except Exception as exc:
                        logging.warning(
                            "Failed to cache Instagram album preview photo: url=%s error=%s",
                            summarize_url_for_log(original_url),
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
            await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
            return
    except Exception as exc:
        logging.exception(
            "Error processing inline Instagram query: user_id=%s query=%s error=%s",
            query.from_user.id,
            summarize_text_for_log(query.query),
            exc,
        )
        await query.answer([], cache_time=1, is_personal=True)


async def send_inline_instagram_media(
    *,
    token: str,
    inline_message_id: str,
    actor_name: str,
    actor_user_id: int,
    request_event_id: str,
    duplicate_handler: str,
    deps: HandlerDependencies,
    inst_service,
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
        callback_data_factory=lambda _media_kind: f"inline:instagram:{token}",
        safe_edit_inline_text_fn=safe_edit_inline_text_fn,
    )

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
            db_id = await deps.db.get_file_id(cache_key)
            if not db_id:
                if not channel_id:
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return

                await _edit_inline_status(bm.uploading_status(), media_kind="photo")
                sent = await deps.bot.send_photo(
                    chat_id=channel_id,
                    photo=media.url,
                    caption=f"Instagram Photo from {actor_name}",
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
        db_id = await deps.db.get_file_id(db_video_url)
        if not db_id:
            if not channel_id:
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
            if metrics.size >= max_file_size:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.video_too_large())
                return

            await _edit_inline_status(bm.uploading_status())
            sent = await deps.bot.send_video(
                chat_id=channel_id,
                video=FSInputFile(download_path),
                caption=f"Instagram Video from {actor_name}",
            )
            db_id = sent.video.file_id
            await deps.db.add_file(db_video_url, db_id, "video")
        else:
            await _edit_inline_status(bm.uploading_status())

        bot_url = await get_bot_url_fn(deps.bot)
        edited = await safe_edit_inline_media_fn(
            deps.bot,
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
