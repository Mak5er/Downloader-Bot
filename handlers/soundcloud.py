import datetime
import re
from typing import Optional

from aiogram import F, Router, types
from aiogram.types import FSInputFile

import messages as bm
import keyboards as kb
from config import CHANNEL_ID, COBALT_API_KEY, COBALT_API_URL, OUTPUT_DIR
from handlers.request_dedupe import claim_message_request
from handlers.user import update_info
from handlers.utils import (
    build_queue_busy_text,
    build_rate_limit_text,
    get_bot_avatar_thumbnail,
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
    safe_edit_text,
    safe_edit_inline_media,
    safe_edit_inline_text,
    safe_answer_inline_query,
    send_chat_action_if_needed,
    with_callback_logging,
    with_chosen_inline_logging,
    with_inline_query_logging,
    with_inline_send_logging,
    with_message_logging,
)
from services.logger import logger as logging, summarize_text_for_log, summarize_url_for_log
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
from services.platforms import soundcloud_media as soundcloud_platform

logging = logging.bind(service="soundcloud")

router = Router()

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)
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
            fetch_cobalt_data_func=lambda *args, **kwargs: fetch_cobalt_data(*args, **kwargs),
            retry_async_operation_func=lambda *args, **kwargs: retry_async_operation(*args, **kwargs),
        )


soundcloud_service = SoundCloudService(OUTPUT_DIR)


@router.message(
    F.text.regexp(SOUNDCLOUD_URL_REGEX, mode="search") | F.caption.regexp(SOUNDCLOUD_URL_REGEX, mode="search")
)
@router.business_message(
    F.text.regexp(SOUNDCLOUD_URL_REGEX, mode="search") | F.caption.regexp(SOUNDCLOUD_URL_REGEX, mode="search")
)
@with_message_logging("soundcloud", "message")
async def process_soundcloud(message: types.Message, direct_url: Optional[str] = None):
    status_message: Optional[types.Message] = None
    audio_path: Optional[str] = None
    request_lease = None
    try:
        business_id = message.business_connection_id
        show_service_status = business_id is None
        if direct_url:
            source_url = strip_soundcloud_url(direct_url)
        else:
            text = get_message_text(message)
            match = re.search(SOUNDCLOUD_URL_REGEX, text)
            if not match:
                return
            source_url = strip_soundcloud_url(match.group(0))

        request_lease = await claim_message_request(message, service="soundcloud", url=source_url)
        if request_lease is None:
            return

        logging.info("SoundCloud request: user_id=%s url=%s", message.from_user.id, summarize_url_for_log(source_url))
        await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="soundcloud_audio")
        await react_to_message(message, "\U0001F47E", business_id=business_id)
        user_settings = await load_user_settings(db, message)
        bot_url = await get_bot_url(bot)
        bot_avatar = await get_bot_avatar_thumbnail(bot)
        if show_service_status:
            status_message = await message.answer(bm.downloading_audio_status())

        cache_key = f"{source_url}#audio"
        db_file_id = await db.get_file_id(cache_key)
        if db_file_id:
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(bot, message.chat.id, "upload_audio", business_id)
            send_kwargs = {
                "audio": db_file_id,
                "caption": bm.captions(user_settings["captions"], None, bot_url),
                "parse_mode": "HTML",
            }
            if bot_avatar:
                send_kwargs["thumbnail"] = bot_avatar
            await message.reply_audio(
                **send_kwargs,
            )
            await maybe_delete_user_message(message, user_settings["delete_message"])
            request_lease.mark_success()
            return

        track = await soundcloud_service.fetch_track(source_url)
        if not track:
            await handle_download_error(message, business_id=business_id)
            return

        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        request_id = f"soundcloud_audio:{message.chat.id}:{message.message_id}:{track.id}"
        audio_name = f"{track.id}_{timestamp}_soundcloud_audio.mp3"

        async def _edit_status(text: str) -> None:
            await safe_edit_text(status_message, text)

        on_progress = make_status_text_progress_updater("SoundCloud audio", _edit_status)
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
        if not audio_metrics:
            await handle_download_error(message, business_id=business_id)
            return

        log_download_metrics("soundcloud_audio", audio_metrics)
        audio_path = audio_metrics.path
        if audio_metrics.size >= MAX_FILE_SIZE:
            await message.reply(bm.audio_too_large())
            return

        await safe_edit_text(status_message, bm.uploading_status())
        await send_chat_action_if_needed(bot, message.chat.id, "upload_audio", business_id)

        send_kwargs = {
            "audio": FSInputFile(audio_path),
            "title": track.title,
            "performer": track.artist or None,
            "caption": bm.captions(user_settings["captions"], None, bot_url),
            "parse_mode": "HTML",
        }
        if bot_avatar:
            send_kwargs["thumbnail"] = bot_avatar

        try:
            sent = await message.reply_audio(**send_kwargs)
        except Exception as exc:
            if bot_avatar:
                logging.warning("SoundCloud bot avatar upload failed, retrying without thumbnail: error=%s", exc)
                send_kwargs.pop("thumbnail", None)
                sent = await message.reply_audio(**send_kwargs)
            else:
                raise

        await maybe_delete_user_message(message, user_settings["delete_message"])
        request_lease.mark_success()
        try:
            await db.add_file(cache_key, sent.audio.file_id, "audio")
        except Exception as exc:
            logging.error("Error caching SoundCloud audio: key=%s error=%s", cache_key, exc)

    except DownloadRateLimitError as exc:
        if message.business_connection_id is None:
            await message.reply(build_rate_limit_text(exc.retry_after))
        else:
            await handle_download_error(message, business_id=message.business_connection_id)
    except DownloadQueueBusyError as exc:
        if message.business_connection_id is None:
            await message.reply(build_queue_busy_text(exc.position))
        else:
            await handle_download_error(message, business_id=message.business_connection_id)
    except Exception as exc:
        logging.exception("Error processing SoundCloud request: error=%s", exc)
        await handle_download_error(message, business_id=message.business_connection_id)
    finally:
        if request_lease is not None:
            request_lease.finish()
        await safe_delete_message(status_message)
        if audio_path:
            await remove_file(audio_path)
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

        token = create_inline_video_request("soundcloud", source_url, query.from_user.id, user_settings)
        results = [
            types.InlineQueryResultArticle(
                id=f"soundcloud_inline:{token}",
                title="SoundCloud Audio",
                description=track.title or "Press the button to send this audio inline.",
                thumbnail_url=track.thumbnail_url or get_inline_service_icon("soundcloud"),
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

    audio_path: Optional[str] = None

    async def _edit_inline_status(text: str, *, with_retry_button: bool = False) -> None:
        reply_markup = (
            kb.inline_send_media_keyboard("Send audio inline", f"inline:soundcloud:{token}")
            if with_retry_button
            else None
        )
        await safe_edit_inline_text(bot, inline_message_id, text, reply_markup=reply_markup)

    try:
        source_url = request.source_url
        cache_key = f"{source_url}#audio"
        track = await soundcloud_service.fetch_track(source_url)
        bot_avatar = await get_bot_avatar_thumbnail(bot)
        if not track:
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
            return

        db_file_id = await db.get_file_id(cache_key)
        if not db_file_id:
            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            request_id = f"soundcloud_inline:{request.owner_user_id}:{request_event_id}:{track.id}"
            audio_name = f"{track.id}_{timestamp}_soundcloud_inline.mp3"

            await _edit_inline_status(bm.downloading_audio_status())

            on_progress = make_status_text_progress_updater("SoundCloud audio", _edit_inline_status)

            metrics = await soundcloud_service.download_media(
                track.audio_url,
                audio_name,
                user_id=request.owner_user_id,
                request_id=request_id,
                on_progress=on_progress,
            )
            if not metrics:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            audio_path = metrics.path
            if metrics.size >= MAX_FILE_SIZE:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.audio_too_large())
                return

            await _edit_inline_status(bm.uploading_status())
            send_kwargs = {
                "chat_id": CHANNEL_ID,
                "audio": FSInputFile(audio_path),
                "title": track.title,
                "performer": track.artist or None,
            }
            if bot_avatar:
                send_kwargs["thumbnail"] = bot_avatar

            try:
                sent = await bot.send_audio(**send_kwargs)
            except Exception:
                send_kwargs.pop("thumbnail", None)
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
    except DownloadRateLimitError as e:
        reset_inline_video_request(token)
        await _edit_inline_status(build_rate_limit_text(e.retry_after), with_retry_button=True)
    except DownloadQueueBusyError as e:
        reset_inline_video_request(token)
        await _edit_inline_status(build_queue_busy_text(e.position), with_retry_button=True)
    except Exception:
        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    finally:
        if audio_path:
            await remove_file(audio_path)


@router.chosen_inline_result(F.result_id.startswith("soundcloud_inline:"))
@with_chosen_inline_logging("soundcloud", "chosen_inline")
async def chosen_inline_soundcloud_result(result: types.ChosenInlineResult):
    if not result.inline_message_id:
        logging.warning("Chosen inline SoundCloud result is missing inline_message_id")
        return

    token = result.result_id.removeprefix("soundcloud_inline:")
    await _send_inline_soundcloud_audio(
        token=token,
        inline_message_id=result.inline_message_id,
        actor_name=result.from_user.full_name,
        actor_user_id=getattr(result.from_user, "id", None),
        request_event_id=result.result_id,
        duplicate_handler="chosen",
    )


@router.callback_query(F.data.startswith("inline:soundcloud:"))
@with_callback_logging("soundcloud", "inline_callback")
async def send_inline_soundcloud_audio_callback(call: types.CallbackQuery):
    if not call.inline_message_id:
        await call.answer("This button works only in inline mode.", show_alert=True)
        return

    token = call.data.removeprefix("inline:soundcloud:")
    await call.answer()
    try:
        await _send_inline_soundcloud_audio(
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
