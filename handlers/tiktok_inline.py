import asyncio
import datetime
import re
from typing import Optional

from aiogram import types
from aiogram.types import FSInputFile, InlineQueryResultArticle

import keyboards as kb
import messages as bm
from handlers.deps import HandlerDependencies
from handlers.utils import (
    build_inline_album_result,
    build_queue_busy_text,
    build_rate_limit_text,
    build_start_deeplink_url,
    get_bot_url,
    make_retry_status_notifier,
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
from utils.download_manager import DownloadQueueBusyError, DownloadRateLimitError, log_download_metrics
from utils.media_cache import build_media_cache_key

logging = logging.bind(service="tiktok_inline")


async def handle_tiktok_inline_query(
    query: types.InlineQuery,
    *,
    deps: HandlerDependencies,
    channel_id: Optional[int],
    strip_tiktok_tracking,
    fetch_tiktok_data_with_retry_fn,
    video_info_fn,
    build_tiktok_video_url_fn,
    get_bot_url_fn=get_bot_url,
    safe_answer_inline_query_fn=safe_answer_inline_query,
) -> None:
    try:
        await deps.send_analytics(
            user_id=query.from_user.id,
            chat_type=query.chat_type,
            action_name="inline_tiktok_video",
        )
        logging.info(
            "Inline TikTok request: user_id=%s query=%s",
            query.from_user.id,
            summarize_text_for_log(query.query),
        )
        user_settings = await deps.db.user_settings(query.from_user.id)
        bot_url = await get_bot_url_fn(deps.bot)
        match = re.search(r"(https?://(?:www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", query.query)
        if not match:
            logging.debug("Inline TikTok query pattern not matched: query=%s", summarize_text_for_log(query.query))
            await query.answer([], cache_time=1, is_personal=True)
            return

        source_url = strip_tiktok_tracking(match.group(0))
        data = await fetch_tiktok_data_with_retry_fn(source_url)
        info = await video_info_fn(data)
        images = data.get("data", {}).get("images", [])

        results = []
        if not images:
            if not info:
                await query.answer([], cache_time=1, is_personal=True)
                return

            db_video_url = build_tiktok_video_url_fn(info)
            db_id = await deps.db.get_file_id(db_video_url)
            if not db_id and not channel_id:
                logging.error("CHANNEL_ID is not configured; TikTok inline video send is disabled")
                await query.answer([], cache_time=1, is_personal=True)
                return

            token = create_inline_video_request("tiktok", source_url, query.from_user.id, user_settings)
            results.append(
                InlineQueryResultArticle(
                    id=f"tiktok_inline:{token}",
                    title="TikTok Video",
                    description=info.description or "Press the button to send this video inline.",
                    thumbnail_url=info.cover or get_inline_service_icon("tiktok"),
                    input_message_content=types.InputTextMessageContent(
                        message_text=bm.inline_send_video_prompt("TikTok"),
                    ),
                    reply_markup=kb.inline_send_video_keyboard(token),
                )
            )
            await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
            return

        first_photo = images[0] if images else None
        if first_photo and match:
            source_url = strip_tiktok_tracking(match.group(0))
            cache_key = build_media_cache_key(
                build_tiktok_video_url_fn(info) if info else source_url,
                item_index=0,
                item_kind="photo",
            )
            if len(images) == 1:
                db_id = await deps.db.get_file_id(cache_key)
                if not db_id and not channel_id:
                    logging.error("CHANNEL_ID is not configured; TikTok inline photo send is disabled")
                    await query.answer([], cache_time=1, is_personal=True)
                    return

                token = create_inline_video_request("tiktok", source_url, query.from_user.id, user_settings)
                results.append(
                    InlineQueryResultArticle(
                        id=f"tiktok_inline:{token}",
                        title="TikTok Photo",
                        description=info.description if info and info.description else "Press the button to send this photo inline.",
                        thumbnail_url=first_photo,
                        input_message_content=types.InputTextMessageContent(
                            message_text="TikTok photo is being prepared...\nIf it does not start automatically, tap the button below.",
                        ),
                        reply_markup=kb.inline_send_media_keyboard(
                            "Send photo inline",
                            f"inline:tiktok:{token}",
                        ),
                    )
                )
                await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
                return

            token = create_inline_album_request(query.from_user.id, "tiktok", source_url)
            deep_link = build_start_deeplink_url(bot_url, f"album_{token}")
            preview_file_id = None
            if channel_id:
                preview_cache_key = build_media_cache_key(
                    build_tiktok_video_url_fn(info) if info else source_url,
                    item_index=0,
                    item_kind="photo",
                )
                preview_file_id = await deps.db.get_file_id(preview_cache_key)
                if not preview_file_id:
                    try:
                        sent = await deps.bot.send_photo(
                            chat_id=channel_id,
                            photo=first_photo,
                            caption="TikTok Album Preview",
                        )
                        if sent.photo:
                            preview_file_id = sent.photo[-1].file_id
                            await deps.db.add_file(preview_cache_key, preview_file_id, "photo")
                    except Exception as exc:
                        logging.warning(
                            "Failed to cache TikTok album preview photo: url=%s error=%s",
                            summarize_url_for_log(source_url),
                            exc,
                        )
            results.append(build_inline_album_result(
                result_id=f"tiktok_album_{info.id if info else token}",
                service_name="TikTok",
                deep_link=deep_link,
                message_text=bm.captions(
                    user_settings["captions"],
                    info.description if info else None,
                    bot_url,
                ),
                preview_file_id=preview_file_id,
                preview_url=first_photo,
                thumbnail_url=(info.cover if info and info.cover else first_photo),
            ))
            await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
    except Exception as exc:
        logging.exception(
            "Error processing inline TikTok query: user_id=%s query=%s error=%s",
            query.from_user.id,
            summarize_text_for_log(query.query),
            exc,
        )
        await query.answer([], cache_time=1, is_personal=True)


async def send_inline_tiktok_media(
    *,
    token: str,
    inline_message_id: str,
    actor_name: str,
    actor_user_id: int,
    request_event_id: str,
    duplicate_handler: str,
    deps: HandlerDependencies,
    channel_id: Optional[int],
    max_file_size: int,
    fetch_tiktok_data_with_retry_fn,
    video_info_fn,
    build_tiktok_video_url_fn,
    get_tiktok_audio_callback_data_fn,
    get_tiktok_size_hint_fn,
    tiktok_service,
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

    async def _edit_inline_status(
        text: str,
        *,
        with_retry_button: bool = False,
        media_kind: str = "video",
    ) -> None:
        button_text = "Send photo inline" if media_kind == "photo" else "Send video inline"
        reply_markup = (
            kb.inline_send_media_keyboard(button_text, f"inline:tiktok:{token}")
            if with_retry_button
            else None
        )
        await safe_edit_inline_text_fn(
            deps.bot,
            inline_message_id,
            text,
            reply_markup=reply_markup,
        )

    try:
        async def _on_retry_fetch(failed_attempt: int, total_attempts: int, _error):
            if failed_attempt >= 2:
                await _edit_inline_status(bm.retrying_again_status(failed_attempt + 1, total_attempts))

        data = await fetch_tiktok_data_with_retry_fn(request.source_url, on_retry=_on_retry_fetch)
        info = await video_info_fn(data)
        images = data.get("data", {}).get("images", [])
        if not info:
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
            return
        if len(images) > 1:
            complete_inline_video_request(token)
            await _edit_inline_status(bm.inline_photos_not_supported("TikTok"))
            return
        if images:
            db_photo_url = build_tiktok_video_url_fn(info)
            cache_key = build_media_cache_key(db_photo_url, item_index=0, item_kind="photo")
            db_id = await deps.db.get_file_id(cache_key)
            if not db_id:
                if not channel_id:
                    logging.error("CHANNEL_ID is not configured; TikTok inline upload is disabled")
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return

                await _edit_inline_status(bm.uploading_status(), media_kind="photo")
                sent = await deps.bot.send_photo(
                    chat_id=channel_id,
                    photo=images[0],
                    caption=f"TikTok Photo from {actor_name}",
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
                    caption=bm.captions(request.user_settings["captions"], info.description, bot_url),
                    parse_mode="HTML",
                ),
                reply_markup=kb.return_video_info_keyboard(
                    info.views,
                    info.likes,
                    info.comments,
                    info.shares,
                    info.music_play_url,
                    db_photo_url,
                    request.user_settings,
                    audio_callback_data=get_tiktok_audio_callback_data_fn(info),
                ),
            )
            if edited:
                complete_inline_video_request(token)
                return

            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
            return

        db_video_url = build_tiktok_video_url_fn(info)
        audio_callback_data = get_tiktok_audio_callback_data_fn(info)
        db_id = await deps.db.get_file_id(db_video_url)

        if not db_id:
            if not channel_id:
                logging.error("CHANNEL_ID is not configured; TikTok inline upload is disabled")
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            download_name = f"{info.id}_{timestamp}_tiktok_video.mp4"
            request_id = f"tiktok_inline:{request.owner_user_id}:{request_event_id}:{info.id}"
            size_hint = get_tiktok_size_hint_fn(data)

            await _edit_inline_status(bm.downloading_video_status())

            on_progress = make_status_text_progress_updater("TikTok video", _edit_inline_status)
            on_retry_download = make_retry_status_notifier(_edit_inline_status)

            metrics = await asyncio.wait_for(
                tiktok_service.download_video(
                    db_video_url,
                    download_name,
                    download_data=data,
                    user_id=request.owner_user_id,
                    request_id=request_id,
                    size_hint=size_hint,
                    on_progress=on_progress,
                    on_retry=on_retry_download,
                ),
                timeout=420.0,
            )
            if not metrics:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            log_download_metrics("tiktok_inline", metrics)
            download_path = metrics.path
            if metrics.size >= max_file_size:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.video_too_large())
                return

            await _edit_inline_status(bm.uploading_status())
            sent = await deps.bot.send_video(
                chat_id=channel_id,
                video=FSInputFile(download_path),
                caption=f"TikTok Video from {actor_name}",
            )
            db_id = sent.video.file_id
            await deps.db.add_file(db_video_url, db_id, "video")
            logging.info(
                "Inline TikTok video cached: url=%s file_id=%s",
                summarize_url_for_log(db_video_url),
                db_id,
            )
        else:
            await _edit_inline_status(bm.uploading_status())

        bot_url = await get_bot_url_fn(deps.bot)
        edited = await safe_edit_inline_media_fn(
            deps.bot,
            inline_message_id,
            types.InputMediaVideo(
                media=db_id,
                caption=bm.captions(request.user_settings["captions"], info.description, bot_url),
                parse_mode="HTML",
            ),
            reply_markup=kb.return_video_info_keyboard(
                info.views,
                info.likes,
                info.comments,
                info.shares,
                info.music_play_url,
                db_video_url,
                request.user_settings,
                audio_callback_data=audio_callback_data,
            ),
        )
        if edited:
            complete_inline_video_request(token)
            logging.info(
                "Served inline TikTok video: inline_message_id=%s url=%s file_id=%s",
                inline_message_id,
                summarize_url_for_log(db_video_url),
                db_id,
            )
            return

        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    except DownloadRateLimitError as exc:
        reset_inline_video_request(token)
        await _edit_inline_status(build_rate_limit_text(exc.retry_after), with_retry_button=True)
    except DownloadQueueBusyError as exc:
        reset_inline_video_request(token)
        await _edit_inline_status(build_queue_busy_text(exc.position), with_retry_button=True)
    except asyncio.TimeoutError:
        reset_inline_video_request(token)
        await _edit_inline_status(bm.timeout_error(), with_retry_button=True)
    except Exception as exc:
        logging.exception(
            "Error sending inline TikTok video: inline_message_id=%s token=%s error=%s",
            inline_message_id,
            token,
            exc,
        )
        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    finally:
        if download_path:
            await remove_file(download_path)
