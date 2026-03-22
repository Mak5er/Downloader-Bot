import datetime
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse

from aiogram import F, Router, types
from aiogram.types import FSInputFile

import messages as bm
import keyboards as kb
from config import CHANNEL_ID, COBALT_API_KEY, COBALT_API_URL, OUTPUT_DIR
from handlers.user import update_info
from handlers.utils import (
    build_request_id,
    build_progress_status,
    build_queue_busy_text,
    build_rate_limit_text,
    get_bot_avatar_thumbnail,
    get_bot_url,
    get_message_text,
    handle_download_error,
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
    resolve_settings_target_id,
    with_callback_logging,
    with_chosen_inline_logging,
    with_inline_query_logging,
    with_inline_send_logging,
    with_message_logging,
)
from log.logger import logger as logging
from main import bot, db, send_analytics
from utils.cobalt_client import fetch_cobalt_data
from utils.download_manager import (
    DownloadConfig,
    DownloadError,
    DownloadMetrics,
    DownloadProgress,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    ResilientDownloader,
    log_download_metrics,
)
from services.inline_service_icons import get_inline_service_icon
from services.inline_video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)

logging = logging.bind(service="soundcloud")

router = Router()

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)
SOUNDCLOUD_URL_REGEX = (
    r"(https?://(?:www\.|m\.)?soundcloud\.com/\S+|https?://on\.soundcloud\.com/\S+|"
    r"https?://soundcloud\.app\.goo\.gl/\S+)"
)


@dataclass
class SoundCloudTrack:
    id: str
    source_url: str
    audio_url: str
    title: str
    artist: str
    thumbnail_url: Optional[str] = None


def strip_soundcloud_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


def _looks_like_image_url(url: str) -> bool:
    probe = (url or "").lower().split("?", 1)[0]
    return probe.endswith((".jpg", ".jpeg", ".png", ".webp", ".avif"))


def _looks_like_audio_url(url: str) -> bool:
    probe = (url or "").lower().split("?", 1)[0]
    return probe.endswith((".mp3", ".m4a", ".aac", ".ogg", ".wav", ".opus"))


def _derive_title(filename: Optional[str]) -> str:
    if not filename:
        return "SoundCloud Audio"
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stem = stem.replace("_", " ").replace("-", " ").strip()
    return stem or "SoundCloud Audio"


def parse_soundcloud_track(data: dict, source_url: str) -> Optional[SoundCloudTrack]:
    if not isinstance(data, dict):
        return None

    status = data.get("status")
    if status == "error":
        error = data.get("error") or {}
        logging.error(
            "Cobalt SoundCloud API error: code=%s context=%s",
            error.get("code") if isinstance(error, dict) else None,
            error.get("context") if isinstance(error, dict) else None,
        )
        return None

    audio_url: Optional[str] = None
    thumb_url: Optional[str] = None
    title = _derive_title(data.get("filename"))
    artist = ""

    if status in {"tunnel", "redirect"}:
        maybe_url = data.get("url")
        if isinstance(maybe_url, str) and maybe_url:
            audio_url = maybe_url

    elif status == "picker":
        maybe_audio = data.get("audio")
        if isinstance(maybe_audio, str) and maybe_audio:
            audio_url = maybe_audio
        picker_items = data.get("picker") or []
        for item in picker_items:
            if not isinstance(item, dict):
                continue
            maybe_thumb = item.get("thumb")
            if isinstance(maybe_thumb, str) and maybe_thumb and not thumb_url:
                thumb_url = maybe_thumb

    elif status == "local-processing":
        output = data.get("output") or {}
        metadata = output.get("metadata") if isinstance(output, dict) else {}
        if isinstance(metadata, dict):
            title = metadata.get("title") or title
            artist = metadata.get("artist") or ""

        tunnels = data.get("tunnel") or []
        for tunnel_url in tunnels:
            if not isinstance(tunnel_url, str) or not tunnel_url:
                continue
            if _looks_like_image_url(tunnel_url):
                if not thumb_url:
                    thumb_url = tunnel_url
                continue
            if _looks_like_audio_url(tunnel_url) and not audio_url:
                audio_url = tunnel_url
                continue
            if not audio_url:
                audio_url = tunnel_url

    else:
        logging.error("Unsupported Cobalt SoundCloud response status: status=%s payload=%s", status, data)
        return None

    if not audio_url:
        logging.error("Cobalt SoundCloud response has no audio URL: status=%s payload=%s", status, data)
        return None

    return SoundCloudTrack(
        id=str(int(datetime.datetime.now().timestamp())),
        source_url=source_url,
        audio_url=audio_url,
        title=title,
        artist=artist,
        thumbnail_url=thumb_url,
    )


async def get_user_settings(message: types.Message):
    return await db.user_settings(resolve_settings_target_id(message))


class SoundCloudService:
    def __init__(self, output_dir: str) -> None:
        config = DownloadConfig(
            chunk_size=1024 * 1024,
            multipart_threshold=16 * 1024 * 1024,
            max_workers=6,
            retry_backoff=0.8,
        )
        self._downloader = ResilientDownloader(output_dir, config=config, source="soundcloud")

    async def fetch_track(self, url: str) -> Optional[SoundCloudTrack]:
        payload = {
            "url": url,
            "downloadMode": "audio",
            "audioFormat": "mp3",
            "audioBitrate": "128",
            "alwaysProxy": True,
            "localProcessing": "preferred",
            "disableMetadata": False,
        }
        data = await fetch_cobalt_data(
            COBALT_API_URL,
            COBALT_API_KEY,
            payload,
            source="soundcloud",
            timeout=20,
            attempts=3,
            retry_delay=0.0,
        )
        if not data:
            return None
        return parse_soundcloud_track(data, url)

    async def download_media(
        self,
        url: str,
        filename: str,
        *,
        user_id: Optional[int] = None,
        request_id: Optional[str] = None,
        size_hint: Optional[int] = None,
        on_queued=None,
        on_progress=None,
        on_retry=None,
    ) -> Optional[DownloadMetrics]:
        async def _download_once():
            return await self._downloader.download(
                url,
                filename,
                user_id=user_id,
                request_id=request_id,
                size_hint=size_hint,
                on_queued=on_queued,
                on_progress=on_progress,
            )

        try:
            return await retry_async_operation(
                _download_once,
                attempts=3,
                delay_seconds=2.0,
                retry_on_exception=lambda exc: not isinstance(exc, (DownloadRateLimitError, DownloadQueueBusyError)),
                on_retry=on_retry,
            )
        except (DownloadRateLimitError, DownloadQueueBusyError):
            raise
        except DownloadError as exc:
            logging.error("Error downloading SoundCloud media: url=%s error=%s", url, exc)
            return None


soundcloud_service = SoundCloudService(OUTPUT_DIR)


@router.message(
    F.text.regexp(SOUNDCLOUD_URL_REGEX, mode="search") | F.caption.regexp(SOUNDCLOUD_URL_REGEX, mode="search")
)
@router.business_message(
    F.text.regexp(SOUNDCLOUD_URL_REGEX, mode="search") | F.caption.regexp(SOUNDCLOUD_URL_REGEX, mode="search")
)
@with_message_logging("soundcloud", "message")
async def process_soundcloud(message: types.Message):
    status_message: Optional[types.Message] = None
    audio_path: Optional[str] = None
    try:
        business_id = message.business_connection_id
        show_service_status = business_id is None
        text = get_message_text(message)
        match = re.search(SOUNDCLOUD_URL_REGEX, text)
        if not match:
            return
        source_url = strip_soundcloud_url(match.group(0))

        logging.info("SoundCloud request: user_id=%s url=%s", message.from_user.id, source_url)
        await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="soundcloud_audio")
        await react_to_message(message, "\U0001F47E", business_id=business_id)
        user_settings = await get_user_settings(message)
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
            return

        track = await soundcloud_service.fetch_track(source_url)
        if not track:
            await handle_download_error(message, business_id=business_id)
            return

        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        request_id = f"soundcloud_audio:{message.chat.id}:{message.message_id}:{track.id}"
        audio_name = f"{track.id}_{timestamp}_soundcloud_audio.mp3"
        progress_state = {"last": 0.0}

        async def on_progress(progress: DownloadProgress):
            now = time.monotonic()
            if not progress.done and now - progress_state["last"] < 1.0:
                return
            progress_state["last"] = now
            await safe_edit_text(status_message, build_progress_status("SoundCloud audio", progress))

        async def on_retry(failed_attempt: int, total_attempts: int, _error):
            if show_service_status and failed_attempt >= 2:
                await safe_edit_text(
                    status_message,
                    bm.retrying_again_status(failed_attempt + 1, total_attempts),
                )

        audio_metrics = await soundcloud_service.download_media(
            track.audio_url,
            audio_name,
            user_id=message.from_user.id,
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
        await safe_delete_message(status_message)
        if audio_path:
            await remove_file(audio_path)
        await update_info(message)


async def process_soundcloud_url(message: types.Message):
    """Backward-compatible entrypoint used by pending-request flow."""
    await process_soundcloud(message)


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
            query.query,
            exc,
        )
        await query.answer([], cache_time=1, is_personal=True)


@with_inline_send_logging("soundcloud", "inline_send")
async def _send_inline_soundcloud_audio(
    *,
    token: str,
    inline_message_id: str,
    actor_name: str,
    request_event_id: str,
    duplicate_handler: str,
) -> None:
    request = claim_inline_video_request_for_send(token, duplicate_handler=duplicate_handler)
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
            progress_state = {"last": 0.0}

            await _edit_inline_status(bm.downloading_audio_status())

            async def on_progress(progress: DownloadProgress):
                now = time.monotonic()
                if not progress.done and now - progress_state["last"] < 1.0:
                    return
                progress_state["last"] = now
                await _edit_inline_status(build_progress_status("SoundCloud audio", progress))

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
            request_event_id=str(call.id),
            duplicate_handler="callback",
        )
    except ValueError as exc:
        if str(exc) == "already_processing":
            await call.answer(bm.inline_video_already_processing(), show_alert=False)
            return
        if str(exc) == "already_completed":
            await call.answer(bm.inline_video_already_sent(), show_alert=False)
            return
