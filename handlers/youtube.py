import asyncio
import re
from typing import Optional

from aiogram import types, Router, F
from aiogram.types import FSInputFile
from yt_dlp import YoutubeDL

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, CHANNEL_ID
from handlers.deps import build_handler_dependencies
from handlers.request_dedupe import claim_message_request
from handlers.youtube_inline import (
    handle_youtube_music_inline_query,
    handle_youtube_video_inline_query,
    send_inline_youtube_music,
    send_inline_youtube_video,
)
from services.media.orchestration import handle_download_backpressure, run_single_media_flow
from services.media.delivery import AUDIO_CACHE_VARIANT, send_audio_with_thumbnail
from services.media.video_metadata import build_video_send_kwargs
from handlers.user import update_info
from handlers.utils import (
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
from services.logger import logger as logging, summarize_url_for_log
from app_context import bot, db, send_analytics
from utils.download_manager import (
    DownloadQueueBusyError,
    DownloadRateLimitError,
    DownloadTooLargeError,
    DownloadMetrics,
)
from utils.media_cache import build_media_cache_key
from services.platforms import youtube_media as youtube_platform

logging = logging.bind(service="youtube")

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)  # 1.5 GB Telegram-safe limit
YOUTUBE_VIDEO_URL_REGEX = r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(?!@)\S+)"
YOUTUBE_MUSIC_URL_REGEX = r"(https?://)?music\.(youtube|youtu|youtube-nocookie)\.(com|be)/\S+"
YOUTUBE_INFO_TIMEOUT_SECONDS = youtube_platform.YOUTUBE_INFO_TIMEOUT_SECONDS

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
    chat_id: Optional[int] = None,
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
        chat_id=chat_id,
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


async def _get_youtube_video_with_timeout(url: str):
    return await asyncio.wait_for(
        asyncio.to_thread(get_youtube_video, url),
        timeout=YOUTUBE_INFO_TIMEOUT_SECONDS,
    )


@router.message(
    F.text.regexp(YOUTUBE_VIDEO_URL_REGEX, mode="search")
    | F.caption.regexp(YOUTUBE_VIDEO_URL_REGEX, mode="search")
)
@router.business_message(
    F.text.regexp(YOUTUBE_VIDEO_URL_REGEX, mode="search")
    | F.caption.regexp(YOUTUBE_VIDEO_URL_REGEX, mode="search")
)
@with_message_logging("youtube", "video_message")
async def download_video(message: types.Message, direct_url: Optional[str] = None):
    url = direct_url or _extract_youtube_url(get_message_text(message), YOUTUBE_VIDEO_URL_REGEX)
    if not url:
        return
    logging.info(
        "Downloading YouTube video : user_id=%s username=%s url=%s",
        message.from_user.id,
        message.from_user.username,
        summarize_url_for_log(url),
    )
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="youtube_video")
    status_message: Optional[types.Message] = None
    request_lease = None
    business_id = message.business_connection_id
    show_service_status = business_id is None
    try:
        request_lease = await claim_message_request(message, service="youtube", url=url)
        if request_lease is None:
            return
        await react_to_message(message, "👾", business_id=business_id)
        if show_service_status:
            status_message = await message.answer(bm.downloading_video_status())

        user_settings = await load_user_settings(db, message)
        user_captions = user_settings["captions"]
        bot_url = await get_bot_url(bot)

        yt = await _get_youtube_video_with_timeout(url)
        if not yt:
            await safe_delete_message(status_message)
            await message.reply(bm.nothing_found())
            return
        video = await asyncio.to_thread(get_video_stream, yt)

        audio_callback_data = f"audio:youtube:{yt['id']}" if yt and yt.get("id") else None

        views = safe_int(yt.get('view_count'), None)
        likes = safe_int(yt.get('like_count'), None)

        name = f"{yt['id']}_youtube_video.mp4"
        await safe_edit_text(status_message, bm.downloading_video_status())
        size_hint_raw = (video or {}).get("filesize") or (video or {}).get("filesize_approx")
        size_hint = safe_int(size_hint_raw, 0) or None
        if size_hint and size_hint >= MAX_FILE_SIZE:
            await handle_video_too_large(message, business_id=business_id)
            return

        async def _edit_status(text: str) -> None:
            await safe_edit_text(status_message, text)

        on_progress = make_status_text_progress_updater("YouTube video", _edit_status)
        on_retry_download = make_retry_status_notifier(_edit_status)

        def _reply_markup():
            return kb.return_video_info_keyboard(
                views=views,
                likes=likes,
                comments=None,
                shares=None,
                music_play_url=None,
                video_url=yt['webpage_url'],
                user_settings=user_settings,
                audio_callback_data=audio_callback_data,
            )

        async def _download_media():
            if not video:
                return await asyncio.wait_for(
                    retry_async_operation(
                        lambda: download_with_ytdlp_metrics(
                            yt['webpage_url'],
                            name,
                            YTDLP_FORMAT_720,
                            "youtube_video_ytdlp_merged",
                            max_filesize=MAX_FILE_SIZE - 1,
                        ),
                        attempts=3,
                        delay_seconds=2.0,
                        should_retry_result=lambda result: result is None,
                        on_retry=on_retry_download,
                    ),
                    timeout=900.0,
                )
            if _is_manifest_stream(video):
                return await asyncio.wait_for(
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

            metrics = await asyncio.wait_for(
                download_stream(
                    video,
                    name,
                    "youtube_video",
                    user_id=message.from_user.id,
                    chat_id=message.chat.id,
                    size_hint=size_hint,
                    max_size_bytes=MAX_FILE_SIZE,
                    on_progress=on_progress,
                    on_retry=on_retry_download,
                ),
                timeout=540.0,
            )
            if metrics:
                return metrics
            return await asyncio.wait_for(
                retry_async_operation(
                    lambda: download_with_ytdlp_metrics(
                            yt['webpage_url'],
                            name,
                            YTDLP_FORMAT_720,
                            "youtube_video_ytdlp_merged",
                            max_filesize=MAX_FILE_SIZE - 1,
                        ),
                        attempts=3,
                    delay_seconds=2.0,
                    should_retry_result=lambda result: result is None,
                    on_retry=on_retry_download,
                ),
                timeout=900.0,
            )

        async def _send_cached(file_id: str):
            logging.info(
                "Serving cached YouTube video: url=%s file_id=%s",
                summarize_url_for_log(yt['webpage_url']),
                file_id,
            )
            return await message.reply_video(
                video=file_id,
                caption=bm.captions(user_captions, yt['title'], bot_url),
                reply_markup=_reply_markup(),
                parse_mode="HTML",
            )

        async def _send_downloaded(path: str):
            return await message.reply_video(
                video=FSInputFile(path),
                caption=bm.captions(user_captions, yt['title'], bot_url),
                reply_markup=_reply_markup(),
                parse_mode="HTML",
                **(await build_video_send_kwargs(path)),
            )

        async def _after_send():
            await maybe_delete_user_message(message, user_settings.get("delete_message"))

        async def _inspect_metrics(metrics):
            if metrics.size >= MAX_FILE_SIZE:
                await handle_video_too_large(message, business_id=business_id)
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

        sent_message = await run_single_media_flow(
            cache_key=yt['webpage_url'],
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
            on_rate_limit=_handle_backpressure,
            on_queue_busy=_handle_backpressure,
        )
        if sent_message and getattr(sent_message, "video", None):
            request_lease.mark_success()
            logging.info(
                "YouTube video cached: url=%s file_id=%s",
                summarize_url_for_log(yt['webpage_url']),
                sent_message.video.file_id,
            )
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
        if request_lease is not None:
            request_lease.finish()
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
async def download_music(message: types.Message, direct_url: Optional[str] = None):
    url = direct_url or _extract_youtube_url(get_message_text(message), YOUTUBE_MUSIC_URL_REGEX)
    if not url:
        return
    logging.info(
        "Downloading YouTube audio: user_id=%s username=%s url=%s",
        message.from_user.id,
        message.from_user.username,
        summarize_url_for_log(url),
    )
    status_message: Optional[types.Message] = None
    request_lease = None
    business_id = message.business_connection_id
    show_service_status = business_id is None
    try:
        request_lease = await claim_message_request(message, service="youtube", url=url)
        if request_lease is None:
            return
        await react_to_message(message, "👾", business_id=business_id)
        user_settings = await load_user_settings(db, message)
        bot_url = await get_bot_url(bot)
        bot_avatar = await get_bot_avatar_thumbnail(bot)
        if show_service_status:
            status_message = await message.answer(bm.downloading_audio_status())

        # Get YouTube audio object - run in thread pool
        yt = await _get_youtube_video_with_timeout(url)
        if not yt:
            await message.reply(bm.nothing_found())
            return
        audio = await asyncio.to_thread(get_audio_stream, yt)

        audio_duration = yt.get("duration")
        cache_key = build_media_cache_key(yt["webpage_url"], variant=AUDIO_CACHE_VARIANT)
        db_file_id = await db.get_file_id(cache_key)
        if db_file_id:
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(bot, message.chat.id, "upload_audio", business_id)
            await send_audio_with_thumbnail(
                message.reply_audio,
                audio=db_file_id,
                title=yt["title"],
                caption=bm.captions(None, None, bot_url),
                bot_avatar=bot_avatar,
                bot_url=bot_url,
                duration=audio_duration,
                parse_mode="HTML",
            )
            await maybe_delete_user_message(message, user_settings.get("delete_message"))
            request_lease.mark_success()
            return

        audio_ext = (audio or {}).get("ext") or "m4a"
        name = f"{yt['id']}_youtube_audio.{audio_ext}"
        size_hint_raw = (audio or {}).get("filesize") or (audio or {}).get("filesize_approx")
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
        if not metrics and audio:
            metrics = await download_stream(
                audio,
                name,
                "youtube_audio",
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                size_hint=size_hint,
                max_size_bytes=MAX_FILE_SIZE,
                on_retry=on_retry_download,
            )
        if not metrics:
            await handle_download_error(message, business_id=business_id)
            return

        await safe_edit_text(status_message, bm.uploading_status())
        await send_chat_action_if_needed(bot, message.chat.id, "upload_voice", business_id)

        sent_message = await send_audio_with_thumbnail(
            message.reply_audio,
            audio=FSInputFile(metrics.path),
            title=yt['title'],
            caption=bm.captions(None, None, bot_url),
            audio_path=metrics.path,
            bot_avatar=bot_avatar,
            bot_url=bot_url,
            duration=audio_duration,
            parse_mode="HTML"
        )
        await maybe_delete_user_message(message, user_settings.get("delete_message"))
        await db.add_file(cache_key, sent_message.audio.file_id, "audio")
        request_lease.mark_success()

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
    except asyncio.TimeoutError:
        if show_service_status:
            await safe_edit_text(status_message, bm.timeout_error())
            await handle_download_error(message, business_id=business_id, text=bm.timeout_error())
        else:
            await handle_download_error(message, business_id=business_id)
    except Exception as e:
        logging.error("Audio download error: %s", e)
        await handle_download_error(message, business_id=business_id)
    finally:
        if request_lease is not None:
            request_lease.finish()
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
        summarize_url_for_log(url),
    )

    try:
        bot_url = await get_bot_url(bot)
        bot_avatar = await get_bot_avatar_thumbnail(bot)

        yt = await _get_youtube_video_with_timeout(url)
        if not yt:
            await handle_download_error(call.message, business_id=business_id)
            return

        audio_duration = yt.get("duration")
        cache_key = build_media_cache_key(yt["webpage_url"], variant=AUDIO_CACHE_VARIANT)
        db_file_id = await db.get_file_id(cache_key)
        if db_file_id:
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(bot, call.message.chat.id, "upload_audio", business_id)
            await send_audio_with_thumbnail(
                call.message.reply_audio,
                audio=db_file_id,
                title=yt.get("title"),
                caption=bm.captions(None, None, bot_url),
                bot_avatar=bot_avatar,
                bot_url=bot_url,
                duration=audio_duration,
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
        sent_message = await send_audio_with_thumbnail(
            call.message.reply_audio,
            audio=FSInputFile(metrics.path),
            title=yt.get("title"),
            caption=bm.captions(None, None, bot_url),
            audio_path=metrics.path,
            bot_avatar=bot_avatar,
            bot_url=bot_url,
            duration=audio_duration,
            parse_mode="HTML",
        )
        await db.add_file(cache_key, sent_message.audio.file_id, "audio")

        await remove_file(metrics.path)
    except asyncio.TimeoutError:
        if show_service_status:
            await safe_edit_text(status_message, bm.timeout_error())
        await handle_download_error(call.message, business_id=business_id, text=bm.timeout_error())
    finally:
        await safe_delete_message(status_message)


@router.inline_query(F.query.regexp(YOUTUBE_MUSIC_URL_REGEX, mode="search"))
@with_inline_query_logging("youtube", "music_inline_query")
async def inline_youtube_music_query(query: types.InlineQuery):
    deps = build_handler_dependencies(bot=bot, db=db, send_analytics=send_analytics)
    await handle_youtube_music_inline_query(
        query,
        deps=deps,
        channel_id=CHANNEL_ID,
        youtube_music_url_regex=YOUTUBE_MUSIC_URL_REGEX,
        extract_youtube_url_fn=_extract_youtube_url,
        get_youtube_video_fn=get_youtube_video,
        get_youtube_thumbnail_url_fn=_get_youtube_thumbnail_url,
        get_bot_url_fn=get_bot_url,
        safe_answer_inline_query_fn=safe_answer_inline_query,
    )


@router.inline_query(F.query.regexp(YOUTUBE_VIDEO_URL_REGEX, mode="search"))
@with_inline_query_logging("youtube", "video_inline_query")
async def inline_youtube_query(query: types.InlineQuery):
    deps = build_handler_dependencies(bot=bot, db=db, send_analytics=send_analytics)
    await handle_youtube_video_inline_query(
        query,
        deps=deps,
        youtube_video_url_regex=YOUTUBE_VIDEO_URL_REGEX,
        extract_youtube_url_fn=_extract_youtube_url,
        get_youtube_video_fn=get_youtube_video,
        get_youtube_thumbnail_url_fn=_get_youtube_thumbnail_url,
        safe_answer_inline_query_fn=safe_answer_inline_query,
    )


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
    deps = build_handler_dependencies(bot=bot, db=db, send_analytics=send_analytics)
    await send_inline_youtube_music(
        token=token,
        inline_message_id=inline_message_id,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        request_event_id=request_event_id,
        duplicate_handler=duplicate_handler,
        deps=deps,
        channel_id=CHANNEL_ID,
        max_file_size=MAX_FILE_SIZE,
        get_youtube_video_fn=get_youtube_video,
        download_mp3_with_ytdlp_metrics_fn=download_mp3_with_ytdlp_metrics,
        retry_async_operation_fn=retry_async_operation,
        get_bot_avatar_thumbnail_fn=get_bot_avatar_thumbnail,
        get_bot_url_fn=get_bot_url,
        safe_edit_inline_media_fn=safe_edit_inline_media,
        safe_edit_inline_text_fn=safe_edit_inline_text,
    )


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
    deps = build_handler_dependencies(bot=bot, db=db, send_analytics=send_analytics)
    await send_inline_youtube_video(
        token=token,
        inline_message_id=inline_message_id,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        request_event_id=request_event_id,
        duplicate_handler=duplicate_handler,
        deps=deps,
        channel_id=CHANNEL_ID,
        max_file_size=MAX_FILE_SIZE,
        ytdlp_format_720=YTDLP_FORMAT_720,
        get_youtube_video_fn=get_youtube_video,
        get_video_stream_fn=get_video_stream,
        safe_int_fn=safe_int,
        is_manifest_stream_fn=_is_manifest_stream,
        download_stream_fn=download_stream,
        download_with_ytdlp_metrics_fn=download_with_ytdlp_metrics,
        get_bot_url_fn=get_bot_url,
        safe_edit_inline_media_fn=safe_edit_inline_media,
        safe_edit_inline_text_fn=safe_edit_inline_text,
    )


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
