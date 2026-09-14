import datetime
import re
from typing import Optional

from aiogram import F, Router, types
from aiogram.types import FSInputFile

import messages as bm
import keyboards as kb
from config import CHANNEL_ID, COBALT_API_KEY, COBALT_API_URL, OUTPUT_DIR, MAX_FILE_SIZE
from handlers.request_dedupe import claim_message_request
from handlers.user import update_info
from handlers.utils import (
    build_queue_busy_text,
    build_rate_limit_text,
    get_bot_avatar_thumbnail,
    get_bot_url,
    get_message_text,
    handle_download_backpressure_error,
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
from services.logger import (
    logger as logging,
    summarize_text_for_log,
    summarize_url_for_log,
)
from app_context import bot, db, send_analytics
from utils.cobalt_client import fetch_cobalt_data
from utils.download_manager import (
    DownloadQueueBusyError,
    DownloadRateLimitError,
    log_download_metrics,
)
from services.inline.service_icons import get_inline_service_icon
from services.inline.video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)
from services.media.delivery import (
    build_audio_cache_key,
    build_bot_audio_performer,
    coerce_audio_duration_seconds,
    send_audio_with_thumbnail,
)
from services.media.audio_flow import run_audio_flow
from services.media.audio_metadata import build_audio_filename, prepare_mp3_metadata
from services.platforms import soundcloud_media as soundcloud_platform

logging = logging.bind(service="soundcloud")

router = Router()

SOUNDCLOUD_URL_REGEX = (
    r"(https?://(?:www\.|m\.)?soundcloud\.com/\S+|https?://on\.soundcloud\.com/\S+|"
    r"https?://soundcloud\.app\.goo\.gl/\S+)"
)

SoundCloudTrack = soundcloud_platform.SoundCloudTrack
DownloadError = soundcloud_platform.DownloadError
strip_soundcloud_url = soundcloud_platform.strip_soundcloud_url
parse_soundcloud_track = soundcloud_platform.parse_soundcloud_track


class SoundCloudService(soundcloud_platform.SoundCloudMediaService):
    def __init__(self, output_dir: str) -> None:
        super().__init__(
            output_dir,
            cobalt_api_url=COBALT_API_URL,
            cobalt_api_key=COBALT_API_KEY,
            fetch_cobalt_data_func=lambda *args, **kwargs: fetch_cobalt_data(
                *args, **kwargs
            ),
            retry_async_operation_func=lambda *args, **kwargs: retry_async_operation(
                *args, **kwargs
            ),
        )


soundcloud_service = SoundCloudService(OUTPUT_DIR)


@router.message(
    F.text.regexp(SOUNDCLOUD_URL_REGEX, mode="search")
    | F.caption.regexp(SOUNDCLOUD_URL_REGEX, mode="search")
)
@router.business_message(
    F.text.regexp(SOUNDCLOUD_URL_REGEX, mode="search")
    | F.caption.regexp(SOUNDCLOUD_URL_REGEX, mode="search")
)
@with_message_logging("soundcloud", "message")
async def process_soundcloud(message: types.Message, direct_url: Optional[str] = None):
    status_message: Optional[types.Message] = None
    request_lease = None
    try:
        business_id = message.business_connection_id
        show_service_status = business_id is None
        if await should_skip_duplicate_business_message(
            message, bot, service_name="SoundCloud", logger=logging
        ):
            return

        if direct_url:
            source_url = strip_soundcloud_url(direct_url)
        else:
            text = get_message_text(message)
            match = re.search(SOUNDCLOUD_URL_REGEX, text)
            if not match:
                return
            source_url = strip_soundcloud_url(match.group(0))

        request_lease = await claim_message_request(
            message, service="soundcloud", url=source_url
        )
        if request_lease is None:
            return

        logging.info(
            "SoundCloud request: user_id=%s url=%s",
            message.from_user.id,
            summarize_url_for_log(source_url),
        )
        await send_analytics(
            user_id=message.from_user.id,
            chat_type=message.chat.type,
            action_name="soundcloud_audio",
        )
        await react_to_message(message, "\U0001f47e", business_id=business_id)
        user_settings = await load_user_settings(db, message)
        bot_url = await get_bot_url(bot)
        bot_avatar = await get_bot_avatar_thumbnail(bot)
        if show_service_status:
            status_message = await message.answer(bm.downloading_audio_status())

        cache_key = build_audio_cache_key(source_url)
        track: Optional[SoundCloudTrack] = None

        async def _edit_status(text: str) -> None:
            await safe_edit_text(status_message, text)

        async def _send_cached(file_id: str):
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(
                bot, message.chat.id, "upload_audio", business_id
            )
            return await send_audio_with_thumbnail(
                message.reply_audio,
                audio=file_id,
                caption=bm.captions(user_settings["captions"], None, bot_url),
                bot_url=bot_url,
                parse_mode="HTML",
            )

        async def _fetch_track() -> bool:
            nonlocal track
            track = await soundcloud_service.fetch_track(source_url)
            if not track:
                await handle_download_error(message, business_id=business_id)
                return False
            return True

        async def _download_audio():
            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            request_id = (
                f"soundcloud_audio:{message.chat.id}:{message.message_id}:{track.id}"
            )
            audio_name = f"{track.id}_{timestamp}_soundcloud_audio.mp3"

            on_progress = make_status_text_progress_updater(
                "SoundCloud audio", _edit_status
            )
            on_retry = make_retry_status_notifier(
                _edit_status,
                enabled=show_service_status,
            )

            audio_metrics = await soundcloud_service.download_media(
                track.audio_url,
                audio_name,
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                request_id=request_id,
                on_progress=on_progress,
                on_retry=on_retry,
            )
            if audio_metrics:
                log_download_metrics("soundcloud_audio", audio_metrics)
            return audio_metrics

        async def _prepare_metadata(path: str):
            return await prepare_mp3_metadata(
                path,
                {
                    "title": track.title,
                    "artists": track.artist,
                    "thumbnail": track.thumbnail_url,
                    "source_url": source_url,
                },
            )

        async def _send_downloaded(path: str, prepared_metadata):
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(
                bot, message.chat.id, "upload_audio", business_id
            )

            audio_thumbnail = (
                FSInputFile(str(prepared_metadata.thumbnail_path), filename="cover.jpg")
                if prepared_metadata.thumbnail_path
                else bot_avatar
            )
            return await send_audio_with_thumbnail(
                message.reply_audio,
                audio=FSInputFile(
                    path,
                    filename=build_audio_filename(track.title),
                ),
                title=track.title,
                performer=track.artist or None,
                caption=bm.captions(user_settings["captions"], None, bot_url),
                audio_path=path,
                bot_avatar=audio_thumbnail,
                bot_url=bot_url,
                duration=track.duration_seconds,
                embed_thumbnail=False,
                parse_mode="HTML",
            )

        async def _after_send(_result):
            await maybe_delete_user_message(message, user_settings["delete_message"])
            request_lease.mark_success()

        async def _on_missing_audio():
            await handle_download_error(message, business_id=business_id)

        async def _on_too_large():
            await message.reply(bm.audio_too_large())

        async def _on_cache_store_error(exc: Exception) -> None:
            logging.error(
                "Error caching SoundCloud audio: key=%s error=%s", cache_key, exc
            )

        await run_audio_flow(
            cache_key=cache_key,
            db_service=db,
            send_cached=_send_cached,
            fetch_metadata=_fetch_track,
            download_audio=_download_audio,
            on_missing_audio=_on_missing_audio,
            max_file_size=MAX_FILE_SIZE,
            on_too_large=_on_too_large,
            prepare_metadata=_prepare_metadata,
            send_downloaded=_send_downloaded,
            cleanup_path=remove_file,
            on_cache_store_error=_on_cache_store_error,
            user_id=message.from_user.id if message.from_user else None,
            chat_id=message.chat.id,
            chat_type=getattr(message.chat, "type", None),
            service="soundcloud",
            url=source_url,
            on_after_send=_after_send,
        )

    except (DownloadRateLimitError, DownloadQueueBusyError) as exc:
        show_service_status = message.business_connection_id is None
        await handle_download_backpressure_error(
            exc, message=message, show_service_status=show_service_status
        )
    except Exception as exc:
        logging.exception("Error processing SoundCloud request: error=%s", exc)
        await handle_download_error(message, business_id=message.business_connection_id)
    finally:
        if request_lease is not None:
            request_lease.finish()
        await safe_delete_message(status_message)
        await update_info(message)


async def process_soundcloud_url(message: types.Message, url: Optional[str] = None):
    """Backward-compatible entrypoint used by pending-request flow."""
    await process_soundcloud(message, direct_url=url)


@router.inline_query(F.query.regexp(SOUNDCLOUD_URL_REGEX, mode="search"))
@with_inline_query_logging("soundcloud", "inline_query")
async def inline_soundcloud_query(query: types.InlineQuery):
    try:
        await send_analytics(
            user_id=query.from_user.id,
            chat_type=query.chat_type,
            action_name="inline_soundcloud_audio",
        )

        match = re.search(SOUNDCLOUD_URL_REGEX, query.query or "")
        if not match:
            await query.answer([], cache_time=1, is_personal=True)
            return

        if not CHANNEL_ID:
            logging.error("CHANNEL_ID is not configured; SoundCloud inline is disabled")
            await query.answer([], cache_time=1, is_personal=True)
            return

        source_url = strip_soundcloud_url(match.group(0))
        user_settings = await db.user_settings(query.from_user.id)
        track = await soundcloud_service.fetch_track(source_url)
        if not track:
            await query.answer([], cache_time=1, is_personal=True)
            return

        token = create_inline_video_request(
            "soundcloud", source_url, query.from_user.id, user_settings
        )
        results = [
            types.InlineQueryResultArticle(
                id=f"soundcloud_inline:{token}",
                title="SoundCloud Audio",
                description=track.title
                or "Press the button to send this audio inline.",
                thumbnail_url=track.thumbnail_url
                or get_inline_service_icon("soundcloud"),
                input_message_content=types.InputTextMessageContent(
                    message_text=bm.inline_send_audio_prompt("SoundCloud"),
                ),
                reply_markup=kb.inline_send_media_keyboard(
                    "Send audio inline",
                    f"inline:soundcloud:{token}",
                ),
            )
        ]
        await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
        return

    except Exception as exc:
        logging.exception(
            "Error processing SoundCloud inline query: user_id=%s query=%s error=%s",
            query.from_user.id,
            summarize_text_for_log(query.query),
            exc,
        )
        await query.answer([], cache_time=1, is_personal=True)


@with_inline_send_logging("soundcloud", "inline_send")
async def _send_inline_soundcloud_audio(
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

    async def _edit_inline_status(
        text: str, *, with_retry_button: bool = False
    ) -> None:
        reply_markup = (
            kb.inline_send_media_keyboard(
                "Send audio inline", f"inline:soundcloud:{token}"
            )
            if with_retry_button
            else None
        )
        await safe_edit_inline_text(
            bot, inline_message_id, text, reply_markup=reply_markup
        )

    try:
        source_url = request.source_url
        cache_key = build_audio_cache_key(source_url)
        track = await soundcloud_service.fetch_track(source_url)
        bot_avatar = await get_bot_avatar_thumbnail(bot)
        bot_url = await get_bot_url(bot)
        if not track:
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
            return

        async def _send_cached(_file_id: str):
            await _edit_inline_status(bm.uploading_status())
            return None

        async def _download_audio():
            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            request_id = f"soundcloud_inline:{request.owner_user_id}:{request_event_id}:{track.id}"
            audio_name = f"{track.id}_{timestamp}_soundcloud_inline.mp3"

            await _edit_inline_status(bm.downloading_audio_status())

            on_progress = make_status_text_progress_updater(
                "SoundCloud audio", _edit_inline_status
            )

            return await soundcloud_service.download_media(
                track.audio_url,
                audio_name,
                user_id=request.owner_user_id,
                request_id=request_id,
                on_progress=on_progress,
            )

        async def _on_missing_audio():
            reset_inline_video_request(token)
            await _edit_inline_status(
                bm.something_went_wrong(), with_retry_button=True
            )

        async def _on_too_large():
            complete_inline_video_request(token)
            await _edit_inline_status(bm.audio_too_large())

        async def _prepare_metadata(path: str):
            return await prepare_mp3_metadata(
                path,
                {
                    "title": track.title,
                    "artists": track.artist,
                    "thumbnail": track.thumbnail_url,
                    "source_url": source_url,
                },
            )

        async def _send_downloaded(path: str, prepared_metadata):
            await _edit_inline_status(bm.uploading_status())
            audio_thumbnail = (
                FSInputFile(str(prepared_metadata.thumbnail_path), filename="cover.jpg")
                if prepared_metadata.thumbnail_path
                else bot_avatar
            )
            return await send_audio_with_thumbnail(
                bot.send_audio,
                chat_id=CHANNEL_ID,
                audio=FSInputFile(
                    path,
                    filename=build_audio_filename(track.title),
                ),
                title=track.title,
                performer=track.artist or None,
                audio_path=path,
                bot_avatar=audio_thumbnail,
                bot_url=bot_url,
                duration=track.duration_seconds,
                embed_thumbnail=False,
            )

        result = await run_audio_flow(
            cache_key=cache_key,
            db_service=db,
            send_cached=_send_cached,
            download_audio=_download_audio,
            on_missing_audio=_on_missing_audio,
            max_file_size=MAX_FILE_SIZE,
            on_too_large=_on_too_large,
            prepare_metadata=_prepare_metadata,
            send_downloaded=_send_downloaded,
            cleanup_path=remove_file,
        )
        if result is None:
            return

        edited = await safe_edit_inline_media(
            bot,
            inline_message_id,
            types.InputMediaAudio(
                media=result.file_id,
                caption=bm.captions(request.user_settings["captions"], None, bot_url),
                performer=track.artist or build_bot_audio_performer(bot_url),
                duration=coerce_audio_duration_seconds(track.duration_seconds),
                parse_mode="HTML",
            ),
        )
        if edited:
            complete_inline_video_request(token)
            return

        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    except DownloadRateLimitError as e:
        reset_inline_video_request(token)
        await _edit_inline_status(
            build_rate_limit_text(e.retry_after), with_retry_button=True
        )
    except DownloadQueueBusyError as e:
        reset_inline_video_request(token)
        await _edit_inline_status(
            build_queue_busy_text(e.position), with_retry_button=True
        )
    except Exception:
        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)


chosen_inline_soundcloud_result, send_inline_soundcloud_audio_callback = (
    register_inline_send_handlers(
        router,
        service="soundcloud",
        result_prefix="soundcloud_inline:",
        callback_prefix="inline:soundcloud:",
        send_fn=_send_inline_soundcloud_audio,
        missing_inline_message_warning=(
            "Chosen inline SoundCloud result is missing inline_message_id"
        ),
        chosen_handler_name="chosen_inline_soundcloud_result",
        callback_handler_name="send_inline_soundcloud_audio_callback",
    )
)
