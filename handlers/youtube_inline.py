import asyncio
from typing import Optional

from aiogram import types
from aiogram.types import FSInputFile

import keyboards as kb
import messages as bm
from handlers.deps import HandlerDependencies
from handlers.utils import (
    build_inline_status_editor,
    build_queue_busy_text,
    build_rate_limit_text,
    get_bot_url,
    remove_file,
    safe_answer_inline_query,
    safe_edit_inline_media,
    safe_edit_inline_text,
)
from services.logger import logger as logging, summarize_text_for_log
from services.inline.send_flow import (
    VIDEO_TOO_LARGE,
    deliver_inline_video,
    run_inline_send_flow,
)
from services.inline.service_icons import get_inline_service_icon
from services.inline.video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)
from services.media.delivery import (
    AUDIO_CACHE_VARIANT,
    build_bot_audio_performer,
    coerce_audio_duration_seconds,
    send_audio_with_thumbnail,
)
from services.media.audio_flow import run_audio_flow
from services.media.audio_metadata import build_audio_filename, prepare_mp3_metadata
from services.platforms.youtube_media import (
    YOUTUBE_INFO_TIMEOUT_SECONDS,
    get_audio_artist,
    get_youtube_thumbnail_url,
)
from utils.download_manager import DownloadQueueBusyError, DownloadRateLimitError
from utils.media_cache import build_media_cache_key

logging = logging.bind(service="youtube_inline")


async def _get_youtube_video_with_timeout(get_youtube_video_fn, url: str):
    return await asyncio.wait_for(
        asyncio.to_thread(get_youtube_video_fn, url),
        timeout=YOUTUBE_INFO_TIMEOUT_SECONDS,
    )


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

        yt = await _get_youtube_video_with_timeout(get_youtube_video_fn, url)
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
        yt = await _get_youtube_video_with_timeout(get_youtube_video_fn, url)
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

    _edit_inline_status = build_inline_status_editor(
        bot=deps.bot,
        inline_message_id=inline_message_id,
        callback_data_factory=lambda _media_kind: f"inline:ytmusic:{token}",
        safe_edit_inline_text_fn=safe_edit_inline_text_fn,
        button_text="Send audio inline",
    )

    try:
        yt = await _get_youtube_video_with_timeout(get_youtube_video_fn, request.source_url)
        if not yt:
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
            return

        audio_duration = coerce_audio_duration_seconds(yt.get("duration"))
        audio_artist = get_audio_artist(yt)
        thumbnail_url = get_youtube_thumbnail_url(yt)
        cache_key = build_media_cache_key(request.source_url, variant=AUDIO_CACHE_VARIANT)
        bot_url = await get_bot_url_fn(deps.bot)

        async def _send_cached(_file_id: str):
            await _edit_inline_status(bm.uploading_status())
            return None

        async def _download_audio():
            base_name = f"{yt.get('id', 'youtube_music')}_youtube_music_inline"
            await _edit_inline_status(bm.downloading_audio_status())
            return await retry_async_operation_fn(
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

        async def _on_missing_audio():
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)

        async def _on_too_large():
            complete_inline_video_request(token)
            await _edit_inline_status(bm.audio_too_large())

        async def _prepare_metadata(path: str):
            return await prepare_mp3_metadata(
                path,
                {
                    **yt,
                    "artists": audio_artist,
                    "thumbnail": thumbnail_url,
                    "source_url": request.source_url,
                    "date": yt.get("release_date") or yt.get("upload_date"),
                },
            )

        async def _send_downloaded(path: str, prepared_metadata):
            await _edit_inline_status(bm.uploading_status())
            bot_avatar = await get_bot_avatar_thumbnail_fn(deps.bot)
            audio_thumbnail = (
                FSInputFile(str(prepared_metadata.thumbnail_path), filename="cover.jpg")
                if prepared_metadata.thumbnail_path
                else bot_avatar
            )
            return await send_audio_with_thumbnail(
                deps.bot.send_audio,
                chat_id=channel_id,
                audio=FSInputFile(
                    path,
                    filename=build_audio_filename(yt.get("title")),
                ),
                title=yt.get("title"),
                performer=audio_artist,
                caption=f"YouTube Music from {actor_name}",
                audio_path=path,
                bot_avatar=audio_thumbnail,
                bot_url=bot_url,
                duration=audio_duration,
                embed_thumbnail=False,
            )

        result = await run_audio_flow(
            cache_key=cache_key,
            db_service=deps.db,
            send_cached=_send_cached,
            download_audio=_download_audio,
            on_missing_audio=_on_missing_audio,
            max_file_size=max_file_size,
            on_too_large=_on_too_large,
            prepare_metadata=_prepare_metadata,
            send_downloaded=_send_downloaded,
            cleanup_path=remove_file,
            user_id=actor_user_id,
            chat_id=None,
            chat_type="inline",
            service="youtube_music",
            url=request.source_url,
            title=yt.get("title"),
        )
        if result is None:
            return

        edited = await safe_edit_inline_media_fn(
            deps.bot,
            inline_message_id,
            types.InputMediaAudio(
                media=result.file_id,
                caption=bm.captions(request.user_settings["captions"], None, bot_url),
                performer=audio_artist or build_bot_audio_performer(bot_url),
                duration=audio_duration,
                parse_mode="HTML",
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
    except asyncio.TimeoutError:
        reset_inline_video_request(token)
        await _edit_inline_status(bm.timeout_error(), with_retry_button=True)
    except Exception as exc:
        logging.exception(
            "Error sending inline YouTube Music audio: inline_message_id=%s token=%s error=%s",
            inline_message_id,
            token,
            exc,
        )
        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)


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
    async def _plan(request, edit_status, state) -> None:
        yt = await _get_youtube_video_with_timeout(get_youtube_video_fn, request.source_url)
        if not yt:
            reset_inline_video_request(token)
            await edit_status(bm.something_went_wrong(), with_retry_button=True)
            return

        views = safe_int_fn(yt.get("view_count"), 0)
        likes = safe_int_fn(yt.get("like_count"), 0)

        async def _build_caption():
            return bm.captions(
                request.user_settings["captions"],
                yt["title"],
                await get_bot_url_fn(deps.bot),
            )

        async def _download(on_progress):
            video = await asyncio.to_thread(get_video_stream_fn, yt)

            name = f"{yt['id']}_youtube_inline.mp4"
            inline_size_hint_raw = (video or {}).get("filesize") or (video or {}).get("filesize_approx")
            inline_size_hint = safe_int_fn(inline_size_hint_raw, 0) or None
            if inline_size_hint and inline_size_hint >= max_file_size:
                return VIDEO_TOO_LARGE

            if not video:
                return await download_with_ytdlp_metrics_fn(
                    request.source_url,
                    name,
                    ytdlp_format_720,
                    "youtube_inline_ytdlp_merged",
                    max_filesize=max_file_size - 1,
                )
            if is_manifest_stream_fn(video):
                return await download_with_ytdlp_metrics_fn(
                    request.source_url,
                    name,
                    ytdlp_format_720,
                    "youtube_inline_ytdlp_manifest",
                    max_filesize=max_file_size - 1,
                )
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
                    "youtube_inline_ytdlp_merged",
                    max_filesize=max_file_size - 1,
                )
            return metrics

        await deliver_inline_video(
            deps=deps,
            token=token,
            inline_message_id=inline_message_id,
            channel_id=channel_id,
            max_file_size=max_file_size,
            service_name="YouTube",
            cache_key=request.source_url,
            channel_caption=f"YouTube Video from {actor_name}",
            download_fn=_download,
            progress_label="YouTube video",
            build_caption=_build_caption,
            reply_markup=kb.return_video_info_keyboard(
                views=views,
                likes=likes,
                comments=None,
                shares=None,
                music_play_url=None,
                video_url=request.source_url,
                user_settings=request.user_settings,
            ),
            edit_status=edit_status,
            state=state,
            safe_edit_inline_media_fn=safe_edit_inline_media_fn,
            metrics_log_key=None,
            log=logging,
        )

    await run_inline_send_flow(
        token=token,
        inline_message_id=inline_message_id,
        actor_user_id=actor_user_id,
        duplicate_handler=duplicate_handler,
        deps=deps,
        service_name="YouTube",
        callback_data=f"inline:youtube:{token}",
        plan_fn=_plan,
        safe_edit_inline_text_fn=safe_edit_inline_text_fn,
        button_text="Send video inline",
        log=logging,
    )
