import datetime
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse

from aiogram import F, Router, types
from aiogram.types import FSInputFile

import messages as bm
from config import CHANNEL_ID, COBALT_API_KEY, COBALT_API_URL, OUTPUT_DIR
from handlers.user import update_info
from handlers.utils import (
    build_progress_status,
    build_queue_busy_text,
    build_rate_limit_text,
    get_bot_url,
    get_message_text,
    handle_download_error,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    retry_async_operation,
    safe_delete_message,
    safe_edit_text,
    send_chat_action_if_needed,
    resolve_settings_target_id,
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
            max_concurrent_downloads=3,
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
async def process_soundcloud(message: types.Message):
    status_message: Optional[types.Message] = None
    audio_path: Optional[str] = None
    thumb_path: Optional[str] = None
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

        cache_key = f"{source_url}#audio"
        db_file_id = await db.get_file_id(cache_key)
        if db_file_id:
            await send_chat_action_if_needed(bot, message.chat.id, "upload_audio", business_id)
            await message.answer_audio(
                audio=db_file_id,
                caption=bm.captions(user_settings["captions"], None, bot_url),
                parse_mode="HTML",
            )
            await maybe_delete_user_message(message, user_settings["delete_message"])
            return

        if show_service_status:
            status_message = await message.answer(bm.downloading_audio_status())

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

        if track.thumbnail_url:
            thumb_name = f"{track.id}_{timestamp}_soundcloud_cover.jpg"
            thumb_metrics = await soundcloud_service.download_media(
                track.thumbnail_url,
                thumb_name,
                user_id=message.from_user.id,
                request_id=request_id,
            )
            if thumb_metrics:
                thumb_path = thumb_metrics.path

        await safe_edit_text(status_message, bm.uploading_status())
        await send_chat_action_if_needed(bot, message.chat.id, "upload_audio", business_id)

        send_kwargs = {
            "audio": FSInputFile(audio_path),
            "title": track.title,
            "performer": track.artist or None,
            "caption": bm.captions(user_settings["captions"], None, bot_url),
            "parse_mode": "HTML",
        }
        if thumb_path:
            send_kwargs["thumbnail"] = FSInputFile(thumb_path)

        try:
            sent = await message.answer_audio(**send_kwargs)
        except Exception as exc:
            if thumb_path:
                logging.warning("SoundCloud thumbnail upload failed, retrying without it: error=%s", exc)
                send_kwargs.pop("thumbnail", None)
                sent = await message.answer_audio(**send_kwargs)
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
        if thumb_path:
            await remove_file(thumb_path)
        await update_info(message)


async def process_soundcloud_url(message: types.Message):
    """Backward-compatible entrypoint used by pending-request flow."""
    await process_soundcloud(message)


@router.inline_query(F.query.regexp(SOUNDCLOUD_URL_REGEX, mode="search"))
async def inline_soundcloud_query(query: types.InlineQuery):
    audio_path: Optional[str] = None
    thumb_path: Optional[str] = None
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
        bot_url = await get_bot_url(bot)

        cache_key = f"{source_url}#audio"
        db_file_id = await db.get_file_id(cache_key)
        track: Optional[SoundCloudTrack] = None

        if not db_file_id:
            track = await soundcloud_service.fetch_track(source_url)
            if not track:
                await query.answer([], cache_time=1, is_personal=True)
                return

            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            request_id = f"soundcloud_inline:{query.from_user.id}:{query.id}:{track.id}"
            audio_name = f"{track.id}_{timestamp}_soundcloud_inline.mp3"
            metrics = await soundcloud_service.download_media(
                track.audio_url,
                audio_name,
                user_id=query.from_user.id,
                request_id=request_id,
            )
            if not metrics:
                await query.answer([], cache_time=1, is_personal=True)
                return

            audio_path = metrics.path
            if metrics.size >= MAX_FILE_SIZE:
                await remove_file(audio_path)
                audio_path = None
                await query.answer([], cache_time=1, is_personal=True)
                return

            if track.thumbnail_url:
                thumb_name = f"{track.id}_{timestamp}_soundcloud_inline_cover.jpg"
                thumb_metrics = await soundcloud_service.download_media(
                    track.thumbnail_url,
                    thumb_name,
                    user_id=query.from_user.id,
                    request_id=request_id,
                )
                if thumb_metrics:
                    thumb_path = thumb_metrics.path

            send_kwargs = {
                "chat_id": CHANNEL_ID,
                "audio": FSInputFile(audio_path),
                "title": track.title,
                "performer": track.artist or None,
            }
            if thumb_path:
                send_kwargs["thumbnail"] = FSInputFile(thumb_path)

            try:
                sent = await bot.send_audio(**send_kwargs)
            except Exception as exc:
                if thumb_path:
                    logging.warning("SoundCloud inline thumbnail upload failed, retrying without it: error=%s", exc)
                    send_kwargs.pop("thumbnail", None)
                    sent = await bot.send_audio(**send_kwargs)
                else:
                    raise

            db_file_id = sent.audio.file_id
            await db.add_file(cache_key, db_file_id, "audio")

        if not db_file_id:
            await query.answer([], cache_time=1, is_personal=True)
            return

        result_id = hashlib.md5(source_url.encode("utf-8")).hexdigest()[:32]
        results = [
            types.InlineQueryResultCachedAudio(
                id=f"soundcloud_{result_id}",
                audio_file_id=db_file_id,
                caption=bm.captions(user_settings["captions"], None, bot_url),
                parse_mode="HTML",
            )
        ]
        await query.answer(results, cache_time=10, is_personal=True)
    except Exception as exc:
        logging.exception(
            "Error processing SoundCloud inline query: user_id=%s query=%s error=%s",
            query.from_user.id,
            query.query,
            exc,
        )
        await query.answer([], cache_time=1, is_personal=True)
    finally:
        if audio_path:
            await remove_file(audio_path)
        if thumb_path:
            await remove_file(thumb_path)
