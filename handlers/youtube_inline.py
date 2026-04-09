import asyncio
from typing import Optional

from aiogram import types
from aiogram.types import FSInputFile

import keyboards as kb
import messages as bm
from handlers.deps import HandlerDependencies
from handlers.utils import (
    build_inline_status_editor,
    get_bot_url,
    make_status_text_progress_updater,
    remove_file,
    safe_answer_inline_query,
    safe_edit_inline_media,
    safe_edit_inline_text,
)
from log.logger import logger as logging, summarize_text_for_log
from services.inline.service_icons import get_inline_service_icon
from services.inline.video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)
from utils.media_cache import build_media_cache_key

logging = logging.bind(service="youtube_inline")


async def handle_youtube_music_inline_query(
    query: types.InlineQuery,
    *,
    deps: HandlerDependencies,
    channel_id: Optional[int],
    youtube_music_url_regex: str,
    extract_youtube_url_fn,
    get_youtube_video_fn,
    get_youtube_thumbnail_url_fn,
    get_bot_url_fn=get_bot_url,
    safe_answer_inline_query_fn=safe_answer_inline_query,
) -> None:
    try:
        await deps.send_analytics(
            user_id=query.from_user.id,
            chat_type=query.chat_type,
            action_name="inline_youtube_music",
        )

        url = extract_youtube_url_fn(query.query or "", youtube_music_url_regex)
        if not url:
            await query.answer([], cache_time=1, is_personal=True)
            return
        if not channel_id:
            logging.error("CHANNEL_ID is not configured; YouTube Music inline is disabled")
            await query.answer([], cache_time=1, is_personal=True)
            return

        yt = await asyncio.to_thread(get_youtube_video_fn, url)
        if not yt:
            await query.answer([], cache_time=1, is_personal=True)
            return

        user_settings = await deps.db.user_settings(query.from_user.id)
        webpage_url = yt.get("webpage_url") or url
        token = create_inline_video_request("youtube", webpage_url, query.from_user.id, user_settings)
        results = [
            types.InlineQueryResultArticle(
                id=f"ytmusic_inline:{token}",
                title="YouTube Music",
                description=yt.get("title") or "Press the button to send this audio inline.",
                thumbnail_url=get_youtube_thumbnail_url_fn(yt) or get_inline_service_icon("youtube"),
                input_message_content=types.InputTextMessageContent(
                    message_text=bm.inline_send_audio_prompt("YouTube"),
                ),
                reply_markup=kb.inline_send_media_keyboard(
                    "Send audio inline",
                    f"inline:ytmusic:{token}",
                ),
            )
        ]
        await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
    except Exception as exc:
        logging.exception(
            "Error processing YouTube Music inline query: user_id=%s query=%s error=%s",
            query.from_user.id,
            summarize_text_for_log(query.query),
            exc,
        )
        await query.answer([], cache_time=1, is_personal=True)


async def handle_youtube_video_inline_query(
    query: types.InlineQuery,
    *,
    deps: HandlerDependencies,
    youtube_video_url_regex: str,
    extract_youtube_url_fn,
    get_youtube_video_fn,
    get_youtube_thumbnail_url_fn,
    safe_answer_inline_query_fn=safe_answer_inline_query,
) -> None:
    try:
        url = extract_youtube_url_fn(query.query, youtube_video_url_regex)
        if not url:
            await query.answer([], cache_time=1, is_personal=True)
            return
        yt = await asyncio.to_thread(get_youtube_video_fn, url)
        if not yt:
            await query.answer([], cache_time=1, is_personal=True)
            return

        await deps.send_analytics(
            user_id=query.from_user.id,
            chat_type=query.chat_type,
            action_name="inline_youtube_video",
        )

        user_settings = await deps.db.user_settings(query.from_user.id)
        token = create_inline_video_request("youtube", yt["webpage_url"], query.from_user.id, user_settings)
        results = [
            types.InlineQueryResultArticle(
                id=f"youtube_inline:{token}",
                title="YouTube Video",
                description=yt.get("title") or "Press the button to send this video inline.",
                thumbnail_url=get_youtube_thumbnail_url_fn(yt) or get_inline_service_icon("youtube"),
                input_message_content=types.InputTextMessageContent(
                    message_text=bm.inline_send_video_prompt("YouTube"),
                ),
                reply_markup=kb.inline_send_media_keyboard(
                    "Send video inline",
                    f"inline:youtube:{token}",
                ),
            )
        ]
        await safe_answer_inline_query_fn(query, results, cache_time=10)
    except Exception as exc:
        logging.error("Error processing inline query: %s", exc)
        await query.answer([], cache_time=1, is_personal=True)


async def send_inline_youtube_music(
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
    get_youtube_video_fn,
    download_mp3_with_ytdlp_metrics_fn,
    retry_async_operation_fn,
    get_bot_avatar_thumbnail_fn,
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

    metrics = None

    _edit_inline_status = build_inline_status_editor(
        bot=deps.bot,
        inline_message_id=inline_message_id,
        callback_data_factory=lambda _media_kind: f"inline:ytmusic:{token}",
        safe_edit_inline_text_fn=safe_edit_inline_text_fn,
        button_text="Send audio inline",
    )

    try:
        yt = await asyncio.to_thread(get_youtube_video_fn, request.source_url)
        if not yt:
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
            return

        cache_key = build_media_cache_key(request.source_url, variant="audio")
        db_file_id = await deps.db.get_file_id(cache_key)
        if not db_file_id:
            base_name = f"{yt.get('id', 'youtube_music')}_youtube_music_inline"
            await _edit_inline_status(bm.downloading_audio_status())
            metrics = await retry_async_operation_fn(
                lambda: download_mp3_with_ytdlp_metrics_fn(
                    request.source_url,
                    base_name,
                    "youtube_music_inline_mp3",
                    max_filesize=max_file_size - 1,
                ),
                attempts=3,
                delay_seconds=2.0,
                should_retry_result=lambda result: result is None,
            )
            if not metrics:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return
            if metrics.size >= max_file_size:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.audio_too_large())
                return

            await _edit_inline_status(bm.uploading_status())
            send_kwargs = {
                "chat_id": channel_id,
                "audio": FSInputFile(metrics.path),
                "title": yt.get("title"),
                "caption": f"YouTube Music from {actor_name}",
            }
            bot_avatar = await get_bot_avatar_thumbnail_fn(deps.bot)
            if bot_avatar:
                send_kwargs["thumbnail"] = bot_avatar
            sent = await deps.bot.send_audio(**send_kwargs)
            db_file_id = sent.audio.file_id
            await deps.db.add_file(cache_key, db_file_id, "audio")
        else:
            await _edit_inline_status(bm.uploading_status())

        bot_url = await get_bot_url_fn(deps.bot)
        edited = await safe_edit_inline_media_fn(
            deps.bot,
            inline_message_id,
            types.InputMediaAudio(
                media=db_file_id,
                caption=bm.captions(request.user_settings["captions"], None, bot_url),
                parse_mode="HTML",
            ),
        )
        if edited:
            complete_inline_video_request(token)
            return

        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    finally:
        if metrics and metrics.path:
            await remove_file(metrics.path)


async def send_inline_youtube_video(
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
    ytdlp_format_720: str,
    get_youtube_video_fn,
    get_video_stream_fn,
    safe_int_fn,
    is_manifest_stream_fn,
    download_stream_fn,
    download_with_ytdlp_metrics_fn,
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

    metrics = None

    _edit_inline_status = build_inline_status_editor(
        bot=deps.bot,
        inline_message_id=inline_message_id,
        callback_data_factory=lambda _media_kind: f"inline:youtube:{token}",
        safe_edit_inline_text_fn=safe_edit_inline_text_fn,
        button_text="Send video inline",
    )

    try:
        yt = await asyncio.to_thread(get_youtube_video_fn, request.source_url)
        if not yt:
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
            return

        views = safe_int_fn(yt.get("view_count"), 0)
        likes = safe_int_fn(yt.get("like_count"), 0)
        db_file_id = await deps.db.get_file_id(request.source_url)
        if not db_file_id:
            video = await asyncio.to_thread(get_video_stream_fn, yt)
            if not video:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            name = f"{yt['id']}_youtube_inline.mp4"
            inline_size_hint_raw = video.get("filesize") or video.get("filesize_approx")
            inline_size_hint = safe_int_fn(inline_size_hint_raw, 0) or None
            if inline_size_hint and inline_size_hint >= max_file_size:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.video_too_large())
                return

            await _edit_inline_status(bm.downloading_video_status())
            if is_manifest_stream_fn(video):
                metrics = await download_with_ytdlp_metrics_fn(
                    request.source_url,
                    name,
                    ytdlp_format_720,
                    "youtube_inline_ytdlp_manifest",
                    max_filesize=max_file_size - 1,
                )
            else:
                on_progress = make_status_text_progress_updater("YouTube video", _edit_inline_status)
                metrics = await download_stream_fn(
                    video,
                    name,
                    "youtube_inline",
                    user_id=request.owner_user_id,
                    size_hint=inline_size_hint,
                    max_size_bytes=max_file_size,
                    on_progress=on_progress,
                )
                if not metrics:
                    metrics = await download_with_ytdlp_metrics_fn(
                        request.source_url,
                        name,
                        ytdlp_format_720,
                        "youtube_inline_ytdlp",
                        max_filesize=max_file_size - 1,
                    )
            if not metrics:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            await _edit_inline_status(bm.uploading_status())
            sent_message = await deps.bot.send_video(
                chat_id=channel_id,
                video=FSInputFile(metrics.path),
                caption=f"YouTube Video from {actor_name}",
            )
            db_file_id = sent_message.video.file_id
            await deps.db.add_file(request.source_url, db_file_id, "video")
        else:
            await _edit_inline_status(bm.uploading_status())

        bot_url = await get_bot_url_fn(deps.bot)
        edited = await safe_edit_inline_media_fn(
            deps.bot,
            inline_message_id,
            types.InputMediaVideo(
                media=db_file_id,
                caption=bm.captions(request.user_settings["captions"], yt["title"], bot_url),
                parse_mode="HTML",
            ),
            reply_markup=kb.return_video_info_keyboard(
                views=views,
                likes=likes,
                comments=None,
                shares=None,
                music_play_url=None,
                video_url=request.source_url,
                user_settings=request.user_settings,
            ),
        )
        if edited:
            complete_inline_video_request(token)
            return

        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    finally:
        if metrics and metrics.path:
            await remove_file(metrics.path)
