from __future__ import annotations

import asyncio
import re
from typing import Optional

from aiogram import F, Router, types
from aiogram.types import FSInputFile

import messages as bm
from app_context import bot, db, send_analytics
from config import MAX_FILE_SIZE
from handlers.request_dedupe import claim_message_request
from handlers.user import update_info
from handlers.utils import (
    get_bot_avatar_thumbnail,
    get_bot_url,
    get_message_text,
    handle_download_backpressure_error,
    handle_download_error,
    load_user_settings,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    retry_async_operation,
    safe_delete_message,
    safe_edit_text,
    send_chat_action_if_needed,
    should_skip_duplicate_business_message,
    with_message_logging,
)
from handlers.youtube import download_mp3_with_ytdlp_metrics, search_youtube_track
from services.logger import logger as logging, summarize_url_for_log
from services.media.audio_flow import run_audio_flow
from services.media.audio_metadata import (
    build_audio_filename,
    prepare_mp3_metadata,
)
from services.media.delivery import build_audio_cache_key, send_audio_with_thumbnail
from services.platforms.spotify_media import (
    SpotifyError,
    get_spotify_track,
    strip_spotify_url,
)
from utils.download_manager import (
    DownloadQueueBusyError,
    DownloadRateLimitError,
    DownloadTooLargeError,
)

logging = logging.bind(service="spotify")

SPOTIFY_URL_REGEX = r"https?://(?:(?:open|www)\.)?spotify\.com/(?:intl-[a-z]{2}/)?track/[A-Za-z0-9]+(?:\?\S*)?"

router = Router()


def _extract_spotify_url(text: str) -> str | None:
    match = re.search(SPOTIFY_URL_REGEX, text or "", re.IGNORECASE)
    if not match:
        return None
    return strip_spotify_url(match.group(0).rstrip(".,;:!?)]}>\"'"))


@router.message(
    F.text.regexp(SPOTIFY_URL_REGEX, mode="search")
    | F.caption.regexp(SPOTIFY_URL_REGEX, mode="search")
)
@router.business_message(
    F.text.regexp(SPOTIFY_URL_REGEX, mode="search")
    | F.caption.regexp(SPOTIFY_URL_REGEX, mode="search")
)
@with_message_logging("spotify", "message")
async def process_spotify(message: types.Message, direct_url: Optional[str] = None):
    source_url = strip_spotify_url(direct_url) if direct_url else _extract_spotify_url(
        get_message_text(message)
    )
    if not source_url:
        return

    business_id = message.business_connection_id
    show_service_status = business_id is None
    status_message: Optional[types.Message] = None
    request_lease = None
    try:
        if await should_skip_duplicate_business_message(
            message, bot, service_name="Spotify", logger=logging
        ):
            return
        request_lease = await claim_message_request(
            message, service="spotify", url=source_url
        )
        if request_lease is None:
            return

        logging.info(
            "Spotify track request: user_id=%s url=%s",
            message.from_user.id,
            summarize_url_for_log(source_url),
        )
        await send_analytics(
            user_id=message.from_user.id,
            chat_type=message.chat.type,
            action_name="spotify_audio",
        )
        await react_to_message(message, "👾", business_id=business_id)
        user_settings = await load_user_settings(db, message)
        bot_url = await get_bot_url(bot)
        if show_service_status:
            status_message = await message.answer(bm.downloading_audio_status())

        cache_key = build_audio_cache_key(source_url)
        track: dict | None = None
        youtube_track: dict | None = None

        async def _send_cached(file_id: str):
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(
                bot, message.chat.id, "upload_audio", business_id
            )
            return await message.reply_audio(
                audio=file_id,
                caption=bm.captions(user_settings["captions"], None, bot_url),
                parse_mode="HTML",
            )

        async def _fetch_track() -> bool:
            nonlocal track, youtube_track
            track = await get_spotify_track(source_url)
            query = " - ".join(
                value
                for value in (track.get("artists"), track.get("title"))
                if value and value != "Unknown artist"
            )
            youtube_track = await asyncio.to_thread(search_youtube_track, query)
            if not youtube_track or not youtube_track.get("webpage_url"):
                await message.reply(bm.spotify_source_not_found())
                return False
            return True

        async def _download_audio():
            base_name = f"{track['spotify_id']}_spotify_audio"
            return await retry_async_operation(
                lambda: download_mp3_with_ytdlp_metrics(
                    youtube_track["webpage_url"],
                    base_name,
                    "spotify_audio_mp3",
                    max_filesize=MAX_FILE_SIZE - 1,
                ),
                attempts=3,
                delay_seconds=2.0,
                should_retry_result=lambda result: result is None,
            )

        async def _prepare_metadata(path: str):
            return await prepare_mp3_metadata(path, track)

        async def _send_downloaded(path: str, prepared_metadata):
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(
                bot, message.chat.id, "upload_audio", business_id
            )
            thumbnail = (
                FSInputFile(str(prepared_metadata.thumbnail_path), filename="cover.jpg")
                if prepared_metadata.thumbnail_path
                else await get_bot_avatar_thumbnail(bot)
            )
            return await send_audio_with_thumbnail(
                message.reply_audio,
                audio=FSInputFile(
                    path,
                    filename=build_audio_filename(track.get("title")),
                ),
                title=track.get("title"),
                performer=track.get("artists"),
                caption=bm.captions(user_settings["captions"], None, bot_url),
                audio_path=path,
                bot_avatar=thumbnail,
                bot_url=bot_url,
                duration=track.get("duration"),
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
            on_after_send=_after_send,
            user_id=message.from_user.id if message.from_user else None,
            chat_id=message.chat.id,
            chat_type=getattr(message.chat, "type", None),
            service="spotify",
            url=source_url,
        )
    except SpotifyError as exc:
        logging.warning("Spotify metadata error: url=%s error=%s", source_url, exc)
        await message.reply(bm.spotify_metadata_failed())
    except (DownloadRateLimitError, DownloadQueueBusyError, DownloadTooLargeError) as exc:
        await handle_download_backpressure_error(
            exc,
            message=message,
            show_service_status=show_service_status,
            too_large_text=bm.audio_too_large(),
        )
    except asyncio.TimeoutError:
        await handle_download_error(
            message, business_id=business_id, text=bm.timeout_error()
        )
    except Exception as exc:
        logging.exception("Spotify download failed: error=%s", exc)
        await handle_download_error(message, business_id=business_id)
    finally:
        if request_lease is not None:
            request_lease.finish()
        await safe_delete_message(status_message)
        await update_info(message)


async def process_spotify_url(message: types.Message, url: Optional[str] = None):
    await process_spotify(message, direct_url=url)
