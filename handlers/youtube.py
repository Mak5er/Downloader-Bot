import asyncio
import os
import re
from typing import Optional

from aiogram import types, Router, F
from aiogram.types import FSInputFile, InlineQueryResultArticle
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, CHANNEL_ID
from handlers.user import update_info
from handlers.utils import (
    build_request_id,
    build_queue_busy_text,
    build_rate_limit_text,
    get_bot_url,
    get_bot_avatar_thumbnail,
    get_message_text,
    handle_download_error,
    handle_video_too_large,
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
from utils.download_manager import (
    DownloadProgress,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    DownloadTooLargeError,
    DownloadMetrics,
)
from utils.media_cache import build_media_cache_key
from services.inline.service_icons import get_inline_service_icon
from services.inline.video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)
from services.platforms import youtube_media as youtube_platform

logging = logging.bind(service="youtube")

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)  # 1.5 GB Telegram-safe limit
YOUTUBE_VIDEO_URL_REGEX = r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(?!@)\S+)"
YOUTUBE_MUSIC_URL_REGEX = r"(https?://)?music\.(youtube|youtu|youtube-nocookie)\.(com|be)/\S+"

router = Router()

YTDLP_FORMAT_720 = youtube_platform.YTDLP_FORMAT_720


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_youtube_url(text: str, pattern: str) -> Optional[str]:
    match = re.search(pattern, text or "", re.IGNORECASE)
    if not match:
        return None
    url = match.group(0).strip()
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


_get_youtube_thumbnail_url = youtube_platform.get_youtube_thumbnail_url
get_video_stream = youtube_platform.get_video_stream
get_audio_stream = youtube_platform.get_audio_stream
_is_manifest_stream = youtube_platform.is_manifest_stream


class YouTubeMediaService(youtube_platform.YouTubeMediaService):
    def __init__(self, output_dir: str) -> None:
        super().__init__(
            output_dir,
            retry_async_operation_func=lambda *args, **kwargs: retry_async_operation(*args, **kwargs),
            youtube_dl_factory=lambda options: YoutubeDL(options),
        )


youtube_media_service = YouTubeMediaService(OUTPUT_DIR)
youtube_downloader = youtube_media_service._downloader


async def download_stream(
    stream: dict,
    filename: str,
    source: str,
    *,
    user_id: Optional[int] = None,
    size_hint: Optional[int] = None,
    max_size_bytes: Optional[int] = None,
    on_queued=None,
    on_progress=None,
    on_retry=None,
) -> Optional[DownloadMetrics]:
    return await youtube_media_service.download_stream(
        stream,
        filename,
        source,
        user_id=user_id,
        size_hint=size_hint,
        max_size_bytes=max_size_bytes,
        on_queued=on_queued,
        on_progress=on_progress,
        on_retry=on_retry,
    )


async def download_with_ytdlp(url: str, filename: str) -> Optional[str]:
    return await youtube_media_service.download_with_ytdlp(url, filename)


async def download_with_ytdlp_metrics(
    url: str,
    filename: str,
    format_selector: str,
    source: str,
    *,
    max_filesize: Optional[int] = None,
) -> Optional[DownloadMetrics]:
    return await youtube_media_service.download_with_ytdlp_metrics(
        url,
        filename,
        format_selector,
        source,
        max_filesize=max_filesize,
    )


async def download_mp3_with_ytdlp_metrics(
    url: str,
    base_name: str,
    source: str,
    *,
    max_filesize: Optional[int] = None,
) -> Optional[DownloadMetrics]:
    return await youtube_media_service.download_mp3_with_ytdlp_metrics(
        url,
        base_name,
        source,
        max_filesize=max_filesize,
    )


async def download_media(url: str, filename: str, format_candidates: list[str]) -> bool:
    return await youtube_media_service.download_media(url, filename, format_candidates)


def get_youtube_video(url):
    return youtube_media_service.get_youtube_video(url)


@router.message(
    F.text.regexp(YOUTUBE_VIDEO_URL_REGEX, mode="search")
    | F.caption.regexp(YOUTUBE_VIDEO_URL_REGEX, mode="search")
)
@router.business_message(
    F.text.regexp(YOUTUBE_VIDEO_URL_REGEX, mode="search")
    | F.caption.regexp(YOUTUBE_VIDEO_URL_REGEX, mode="search")
)
@with_message_logging("youtube", "video_message")
async def download_video(message: types.Message):
    url = _extract_youtube_url(get_message_text(message), YOUTUBE_VIDEO_URL_REGEX)
    if not url:
        return
    logging.info(
        "Downloading YouTube video : user_id=%s username=%s url=%s",
        message.from_user.id,
        message.from_user.username,
        url,
    )
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="youtube_video")
    status_message: Optional[types.Message] = None
    business_id = message.business_connection_id
    show_service_status = business_id is None
    try:
        await react_to_message(message, "👾", business_id=business_id)
        if show_service_status:
            status_message = await message.answer(bm.downloading_video_status())

        user_settings = await load_user_settings(db, message)
        user_captions = user_settings["captions"]
        bot_url = await get_bot_url(bot)

        yt = await asyncio.wait_for(asyncio.to_thread(get_youtube_video, url), timeout=45.0)
        if not yt:
            await safe_delete_message(status_message)
            await message.reply(bm.nothing_found())
            return
        video = await asyncio.to_thread(get_video_stream, yt)

        if not video:
            await safe_delete_message(status_message)
            await message.reply(bm.nothing_found())
            return

        audio_callback_data = f"audio:youtube:{yt['id']}" if yt and yt.get("id") else None

        views = safe_int(yt.get('view_count'), None)
        likes = safe_int(yt.get('like_count'), None)

        db_file_id = await db.get_file_id(yt['webpage_url'])

        if db_file_id:
            logging.info(
                "Serving cached YouTube video: url=%s file_id=%s",
                yt['webpage_url'],
                db_file_id,
            )
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
            await message.reply_video(
                video=db_file_id,
                caption=bm.captions(user_captions, yt['title'], bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    views=views,
                    likes=likes,
                    comments=None,
                    shares=None,
                    music_play_url=None,
                    video_url=yt['webpage_url'],
                    user_settings=user_settings,
                    audio_callback_data=audio_callback_data,
                ),
                parse_mode="HTML"
            )
            await maybe_delete_user_message(message, user_settings.get("delete_message"))
            return

        name = f"{yt['id']}_youtube_video.mp4"
        await safe_edit_text(status_message, bm.downloading_video_status())
        size_hint_raw = video.get("filesize") or video.get("filesize_approx")
        size_hint = safe_int(size_hint_raw, 0) or None
        if size_hint and size_hint >= MAX_FILE_SIZE:
            await handle_video_too_large(message, business_id=business_id)
            return

        async def _edit_status(text: str) -> None:
            await safe_edit_text(status_message, text)

        on_progress = make_status_text_progress_updater("YouTube video", _edit_status)
        on_retry_download = make_retry_status_notifier(_edit_status)

        # Prefer high-speed downloader when stream is direct media; fall back to yt-dlp for manifests/HLS.
        if _is_manifest_stream(video):
            metrics = await asyncio.wait_for(
                retry_async_operation(
                    lambda: download_with_ytdlp_metrics(
                        yt['webpage_url'],
                        name,
                        YTDLP_FORMAT_720,
                        "youtube_video_ytdlp_manifest",
                        max_filesize=MAX_FILE_SIZE - 1,
                    ),
                    attempts=3,
                    delay_seconds=2.0,
                    should_retry_result=lambda result: result is None,
                    on_retry=on_retry_download,
                ),
                timeout=900.0,
            )
        else:
            metrics = await asyncio.wait_for(
                download_stream(
                    video,
                    name,
                    "youtube_video",
                    user_id=message.from_user.id,
                    size_hint=size_hint,
                    max_size_bytes=MAX_FILE_SIZE,
                    on_progress=on_progress,
                    on_retry=on_retry_download,
                ),
                timeout=540.0,
            )
            if not metrics:
                metrics = await asyncio.wait_for(
                    retry_async_operation(
                        lambda: download_with_ytdlp_metrics(
                            yt['webpage_url'],
                            name,
                            YTDLP_FORMAT_720,
                            "youtube_video_ytdlp",
                            max_filesize=MAX_FILE_SIZE - 1,
                        ),
                        attempts=3,
                        delay_seconds=2.0,
                        should_retry_result=lambda result: result is None,
                        on_retry=on_retry_download,
                    ),
                    timeout=900.0,
                )
        if not metrics:
            await handle_download_error(message, business_id=business_id)
            return

        if metrics.size >= MAX_FILE_SIZE:
            await handle_video_too_large(message, business_id=business_id)
            await remove_file(metrics.path)
            return

        await safe_edit_text(status_message, bm.uploading_status())
        await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
        sent_message = await message.reply_video(
            video=FSInputFile(metrics.path),
            caption=bm.captions(user_captions, yt['title'], bot_url),
            reply_markup=kb.return_video_info_keyboard(
                views=views,
                likes=likes,
                comments=None,
                shares=None,
                music_play_url=None,
                video_url=yt['webpage_url'],
                user_settings=user_settings,
                audio_callback_data=audio_callback_data,
            ),
            parse_mode="HTML"
        )
        await maybe_delete_user_message(message, user_settings.get("delete_message"))
        await db.add_file(yt['webpage_url'], sent_message.video.file_id, "video")
        logging.info(
            "YouTube video cached: url=%s file_id=%s",
            yt['webpage_url'],
            sent_message.video.file_id,
        )

        await remove_file(metrics.path)
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
    except DownloadTooLargeError:
        await handle_video_too_large(message, business_id=business_id)
    except asyncio.TimeoutError:
        if show_service_status:
            await safe_edit_text(status_message, bm.timeout_error())
            await handle_download_error(message, business_id=business_id, text=bm.timeout_error())
        else:
            await handle_download_error(message, business_id=business_id)
    except Exception as e:
        logging.error("Video download error: %s", e)
        await handle_download_error(message, business_id=business_id)
    finally:
        await safe_delete_message(status_message)
    await update_info(message)


@router.message(
    F.text.regexp(YOUTUBE_MUSIC_URL_REGEX, mode="search")
    | F.caption.regexp(YOUTUBE_MUSIC_URL_REGEX, mode="search")
)
@router.business_message(
    F.text.regexp(YOUTUBE_MUSIC_URL_REGEX, mode="search")
    | F.caption.regexp(YOUTUBE_MUSIC_URL_REGEX, mode="search")
)
@with_message_logging("youtube", "music_message")
async def download_music(message: types.Message):
    url = _extract_youtube_url(get_message_text(message), YOUTUBE_MUSIC_URL_REGEX)
    if not url:
        return
    logging.info(
        "Downloading YouTube audio: user_id=%s username=%s url=%s",
        message.from_user.id,
        message.from_user.username,
        url,
    )
    status_message: Optional[types.Message] = None
    business_id = message.business_connection_id
    show_service_status = business_id is None
    try:
        await react_to_message(message, "👾", business_id=business_id)
        user_settings = await load_user_settings(db, message)
        bot_url = await get_bot_url(bot)
        bot_avatar = await get_bot_avatar_thumbnail(bot)
        if show_service_status:
            status_message = await message.answer(bm.downloading_audio_status())

        # Get YouTube audio object - run in thread pool
        yt = await asyncio.to_thread(get_youtube_video, url)
        if not yt:
            await message.reply(bm.nothing_found())
            return
        audio = await asyncio.to_thread(get_audio_stream, yt)

        if not audio:
            await message.reply(bm.nothing_found())
            return

        cache_key = build_media_cache_key(yt["webpage_url"], variant="audio")
        db_file_id = await db.get_file_id(cache_key)
        if db_file_id:
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(bot, message.chat.id, "upload_audio", business_id)
            await message.reply_audio(
                audio=db_file_id,
                title=yt["title"],
                caption=bm.captions(None, None, bot_url),
                thumbnail=bot_avatar,
                parse_mode="HTML",
            )
            await maybe_delete_user_message(message, user_settings.get("delete_message"))
            return

        audio_ext = audio.get("ext") or "m4a"
        name = f"{yt['id']}_youtube_audio.{audio_ext}"
        size_hint_raw = audio.get("filesize") or audio.get("filesize_approx")
        size_hint = safe_int(size_hint_raw, 0) or None
        if size_hint and size_hint >= MAX_FILE_SIZE:
            await message.reply(bm.audio_too_large())
            return

        async def on_retry_download(failed_attempt: int, total_attempts: int, _error):
            if failed_attempt >= 2:
                await safe_edit_text(
                    status_message,
                    bm.retrying_again_status(failed_attempt + 1, total_attempts),
                )

        metrics = await retry_async_operation(
            lambda: download_with_ytdlp_metrics(
                yt['webpage_url'],
                name,
                "bestaudio/best",
                "youtube_audio_ytdlp",
                max_filesize=MAX_FILE_SIZE - 1,
            ),
            attempts=3,
            delay_seconds=2.0,
            should_retry_result=lambda result: result is None,
            on_retry=on_retry_download,
        )
        if not metrics:
            metrics = await download_stream(
                audio,
                name,
                "youtube_audio",
                user_id=message.from_user.id,
                size_hint=size_hint,
                max_size_bytes=MAX_FILE_SIZE,
                on_retry=on_retry_download,
            )
        if not metrics:
            await handle_download_error(message, business_id=business_id)
            return

        await safe_edit_text(status_message, bm.uploading_status())
        await send_chat_action_if_needed(bot, message.chat.id, "upload_voice", business_id)

        sent_message = await message.reply_audio(
            audio=FSInputFile(metrics.path),
            title=yt['title'],
            caption=bm.captions(None, None, bot_url),
            thumbnail=bot_avatar,
            parse_mode="HTML"
        )
        await maybe_delete_user_message(message, user_settings.get("delete_message"))
        await db.add_file(cache_key, sent_message.audio.file_id, "audio")

        await remove_file(metrics.path)
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
    except DownloadTooLargeError:
        await message.reply(bm.audio_too_large())
    except Exception as e:
        logging.error("Audio download error: %s", e)
        await handle_download_error(message, business_id=business_id)
    finally:
        await safe_delete_message(status_message)
    await update_info(message)


@router.callback_query(F.data.startswith("audio:youtube:"))
async def download_youtube_mp3_callback(call: types.CallbackQuery):
    if not call.message:
        await call.answer("Open the bot to download MP3", show_alert=True)
        return

    await call.answer()
    business_id = call.message.business_connection_id
    show_service_status = business_id is None
    status_message: Optional[types.Message] = None
    if show_service_status:
        status_message = await call.message.answer(bm.downloading_audio_status())
    video_id = call.data.split(":", 2)[2]
    url = f"https://www.youtube.com/watch?v={video_id}"
    logging.info(
        "Downloading YouTube MP3 via button: user_id=%s url=%s",
        call.from_user.id,
        url,
    )

    try:
        bot_url = await get_bot_url(bot)
        bot_avatar = await get_bot_avatar_thumbnail(bot)

        yt = await asyncio.to_thread(get_youtube_video, url)
        if not yt:
            await handle_download_error(call.message, business_id=business_id)
            return

        cache_key = build_media_cache_key(yt["webpage_url"], variant="audio")
        db_file_id = await db.get_file_id(cache_key)
        if db_file_id:
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(bot, call.message.chat.id, "upload_audio", business_id)
            await call.message.reply_audio(
                audio=db_file_id,
                title=yt.get("title"),
                caption=bm.captions(None, None, bot_url),
                thumbnail=bot_avatar,
                parse_mode="HTML",
            )
            return

        base_name = f"{yt['id']}_youtube_audio"
        async def on_retry_download(failed_attempt: int, total_attempts: int, _error):
            if show_service_status and failed_attempt >= 2:
                await safe_edit_text(
                    status_message,
                    bm.retrying_again_status(failed_attempt + 1, total_attempts),
                )

        metrics = await retry_async_operation(
            lambda: download_mp3_with_ytdlp_metrics(
                yt['webpage_url'],
                base_name,
                "youtube_audio_mp3",
                max_filesize=MAX_FILE_SIZE - 1,
            ),
            attempts=3,
            delay_seconds=2.0,
            should_retry_result=lambda result: result is None,
            on_retry=on_retry_download,
        )
        if not metrics:
            await handle_download_error(call.message, business_id=business_id)
            return

        if metrics.size >= MAX_FILE_SIZE:
            await call.message.reply(bm.audio_too_large())
            await remove_file(metrics.path)
            return

        await send_chat_action_if_needed(bot, call.message.chat.id, "upload_audio", business_id)
        await safe_edit_text(status_message, bm.uploading_status())
        sent_message = await call.message.reply_audio(
            audio=FSInputFile(metrics.path),
            title=yt.get("title"),
            caption=bm.captions(None, None, bot_url),
            thumbnail=bot_avatar,
            parse_mode="HTML",
        )
        await db.add_file(cache_key, sent_message.audio.file_id, "audio")

        await remove_file(metrics.path)
    finally:
        await safe_delete_message(status_message)


@router.inline_query(F.query.regexp(YOUTUBE_MUSIC_URL_REGEX, mode="search"))
@with_inline_query_logging("youtube", "music_inline_query")
async def inline_youtube_music_query(query: types.InlineQuery):
    try:
        await send_analytics(
            user_id=query.from_user.id,
            chat_type=query.chat_type,
            action_name="inline_youtube_music",
        )

        url = _extract_youtube_url(query.query or "", YOUTUBE_MUSIC_URL_REGEX)
        if not url:
            await query.answer([], cache_time=1, is_personal=True)
            return
        if not CHANNEL_ID:
            logging.error("CHANNEL_ID is not configured; YouTube Music inline is disabled")
            await query.answer([], cache_time=1, is_personal=True)
            return

        yt = await asyncio.to_thread(get_youtube_video, url)
        if not yt:
            await query.answer([], cache_time=1, is_personal=True)
            return

        user_settings = await db.user_settings(query.from_user.id)
        webpage_url = yt.get("webpage_url") or url
        token = create_inline_video_request("youtube", webpage_url, query.from_user.id, user_settings)
        results = [
            types.InlineQueryResultArticle(
                id=f"ytmusic_inline:{token}",
                title="YouTube Music",
                description=yt.get("title") or "Press the button to send this audio inline.",
                thumbnail_url=_get_youtube_thumbnail_url(yt) or get_inline_service_icon("youtube"),
                input_message_content=types.InputTextMessageContent(
                    message_text=bm.inline_send_audio_prompt("YouTube"),
                ),
                reply_markup=kb.inline_send_media_keyboard(
                    "Send audio inline",
                    f"inline:ytmusic:{token}",
                ),
            )
        ]
        await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
        return

    except Exception as e:
        logging.exception(
            "Error processing YouTube Music inline query: user_id=%s query=%s error=%s",
            query.from_user.id,
            query.query,
            e,
        )
        await query.answer([], cache_time=1, is_personal=True)


@router.inline_query(F.query.regexp(YOUTUBE_VIDEO_URL_REGEX, mode="search"))
@with_inline_query_logging("youtube", "video_inline_query")
async def inline_youtube_query(query: types.InlineQuery):
    try:
        url = _extract_youtube_url(query.query, YOUTUBE_VIDEO_URL_REGEX)
        if not url:
            await query.answer([], cache_time=1, is_personal=True)
            return
        logging.info(
            "Downloading YouTube Inline: user_id=%s query=%s",
            query.from_user.id,
            url,
        )
        yt = await asyncio.to_thread(get_youtube_video, url)
        if not yt:
            await query.answer([], cache_time=1, is_personal=True)
            return

        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_youtube_video")

        user_settings = await db.user_settings(query.from_user.id)
        token = create_inline_video_request("youtube", yt["webpage_url"], query.from_user.id, user_settings)
        results = [
            InlineQueryResultArticle(
                id=f"youtube_inline:{token}",
                title="YouTube Video",
                description=yt.get("title") or "Press the button to send this video inline.",
                thumbnail_url=_get_youtube_thumbnail_url(yt) or get_inline_service_icon("youtube"),
                input_message_content=types.InputTextMessageContent(
                    message_text=bm.inline_send_video_prompt("YouTube"),
                ),
                reply_markup=kb.inline_send_media_keyboard(
                    "Send video inline",
                    f"inline:youtube:{token}",
                ),
            )
        ]
        await safe_answer_inline_query(query, results, cache_time=10)
        return
    except Exception as e:
        logging.error("Error processing inline query: %s", e)
        await query.answer([], cache_time=1, is_personal=True)


@with_inline_send_logging("youtube", "music_inline_send")
async def _send_inline_youtube_music(
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

    metrics: Optional[DownloadMetrics] = None

    async def _edit_inline_status(text: str, *, with_retry_button: bool = False) -> None:
        reply_markup = (
            kb.inline_send_media_keyboard("Send audio inline", f"inline:ytmusic:{token}")
            if with_retry_button
            else None
        )
        await safe_edit_inline_text(bot, inline_message_id, text, reply_markup=reply_markup)

    try:
        yt = await asyncio.to_thread(get_youtube_video, request.source_url)
        if not yt:
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
            return

        cache_key = build_media_cache_key(request.source_url, variant="audio")
        db_file_id = await db.get_file_id(cache_key)
        if not db_file_id:
            base_name = f"{yt.get('id', 'youtube_music')}_youtube_music_inline"
            await _edit_inline_status(bm.downloading_audio_status())
            metrics = await retry_async_operation(
                lambda: download_mp3_with_ytdlp_metrics(
                    request.source_url,
                    base_name,
                    "youtube_music_inline_mp3",
                    max_filesize=MAX_FILE_SIZE - 1,
                ),
                attempts=3,
                delay_seconds=2.0,
                should_retry_result=lambda result: result is None,
            )
            if not metrics:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return
            if metrics.size >= MAX_FILE_SIZE:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.audio_too_large())
                return

            await _edit_inline_status(bm.uploading_status())
            send_kwargs = {
                "chat_id": CHANNEL_ID,
                "audio": FSInputFile(metrics.path),
                "title": yt.get("title"),
                "caption": f"YouTube Music from {actor_name}",
            }
            bot_avatar = await get_bot_avatar_thumbnail(bot)
            if bot_avatar:
                send_kwargs["thumbnail"] = bot_avatar
            sent = await bot.send_audio(**send_kwargs)
            db_file_id = sent.audio.file_id
            await db.add_file(cache_key, db_file_id, "audio")
        else:
            await _edit_inline_status(bm.uploading_status())

        bot_url = await get_bot_url(bot)
        edited = await safe_edit_inline_media(
            bot,
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


@with_inline_send_logging("youtube", "video_inline_send")
async def _send_inline_youtube_video(
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

    metrics: Optional[DownloadMetrics] = None

    async def _edit_inline_status(text: str, *, with_retry_button: bool = False) -> None:
        reply_markup = (
            kb.inline_send_media_keyboard("Send video inline", f"inline:youtube:{token}")
            if with_retry_button
            else None
        )
        await safe_edit_inline_text(bot, inline_message_id, text, reply_markup=reply_markup)

    try:
        yt = await asyncio.to_thread(get_youtube_video, request.source_url)
        if not yt:
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
            return

        views = safe_int(yt.get("view_count"), 0)
        likes = safe_int(yt.get("like_count"), 0)
        db_file_id = await db.get_file_id(request.source_url)
        if not db_file_id:
            video = await asyncio.to_thread(get_video_stream, yt)
            if not video:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            name = f"{yt['id']}_youtube_inline.mp4"
            inline_size_hint_raw = video.get("filesize") or video.get("filesize_approx")
            inline_size_hint = safe_int(inline_size_hint_raw, 0) or None
            if inline_size_hint and inline_size_hint >= MAX_FILE_SIZE:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.video_too_large())
                return

            await _edit_inline_status(bm.downloading_video_status())
            if _is_manifest_stream(video):
                metrics = await download_with_ytdlp_metrics(
                    request.source_url,
                    name,
                    YTDLP_FORMAT_720,
                    "youtube_inline_ytdlp_manifest",
                    max_filesize=MAX_FILE_SIZE - 1,
                )
            else:
                on_progress = make_status_text_progress_updater("YouTube video", _edit_inline_status)

                metrics = await download_stream(
                    video,
                    name,
                    "youtube_inline",
                    user_id=request.owner_user_id,
                    size_hint=inline_size_hint,
                    max_size_bytes=MAX_FILE_SIZE,
                    on_progress=on_progress,
                )
                if not metrics:
                    metrics = await download_with_ytdlp_metrics(
                        request.source_url,
                        name,
                        YTDLP_FORMAT_720,
                        "youtube_inline_ytdlp",
                        max_filesize=MAX_FILE_SIZE - 1,
                    )
            if not metrics:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            await _edit_inline_status(bm.uploading_status())
            sent_message = await bot.send_video(
                chat_id=CHANNEL_ID,
                video=FSInputFile(metrics.path),
                caption=f"YouTube Video from {actor_name}",
            )
            db_file_id = sent_message.video.file_id
            await db.add_file(request.source_url, db_file_id, "video")
        else:
            await _edit_inline_status(bm.uploading_status())

        bot_url = await get_bot_url(bot)
        edited = await safe_edit_inline_media(
            bot,
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


@router.chosen_inline_result(F.result_id.startswith("ytmusic_inline:"))
@with_chosen_inline_logging("youtube", "music_chosen_inline")
async def chosen_inline_youtube_music_result(result: types.ChosenInlineResult):
    if not result.inline_message_id:
        logging.warning("Chosen inline YouTube Music result is missing inline_message_id")
        return

    token = result.result_id.removeprefix("ytmusic_inline:")
    await _send_inline_youtube_music(
        token=token,
        inline_message_id=result.inline_message_id,
        actor_name=result.from_user.full_name,
        actor_user_id=getattr(result.from_user, "id", None),
        request_event_id=result.result_id,
        duplicate_handler="chosen",
    )


@router.callback_query(F.data.startswith("inline:ytmusic:"))
@with_callback_logging("youtube", "music_inline_callback")
async def send_inline_youtube_music_callback(call: types.CallbackQuery):
    if not call.inline_message_id:
        await call.answer("This button works only in inline mode.", show_alert=True)
        return

    token = call.data.removeprefix("inline:ytmusic:")
    await call.answer()
    try:
        await _send_inline_youtube_music(
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


@router.chosen_inline_result(F.result_id.startswith("youtube_inline:"))
@with_chosen_inline_logging("youtube", "video_chosen_inline")
async def chosen_inline_youtube_result(result: types.ChosenInlineResult):
    if not result.inline_message_id:
        logging.warning("Chosen inline YouTube result is missing inline_message_id")
        return

    token = result.result_id.removeprefix("youtube_inline:")
    await _send_inline_youtube_video(
        token=token,
        inline_message_id=result.inline_message_id,
        actor_name=result.from_user.full_name,
        actor_user_id=getattr(result.from_user, "id", None),
        request_event_id=result.result_id,
        duplicate_handler="chosen",
    )


@router.callback_query(F.data.startswith("inline:youtube:"))
@with_callback_logging("youtube", "video_inline_callback")
async def send_inline_youtube_video_callback(call: types.CallbackQuery):
    if not call.inline_message_id:
        await call.answer("This button works only in inline mode.", show_alert=True)
        return

    token = call.data.removeprefix("inline:youtube:")
    await call.answer()
    try:
        await _send_inline_youtube_video(
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
