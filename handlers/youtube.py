import asyncio
import glob
import os
import time
from typing import Tuple, Any, Optional

from aiogram import types, Router, F
from aiogram.types import FSInputFile, InlineQueryResultVideo, InlineQueryResultArticle
from moviepy import VideoFileClip, AudioFileClip
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, BOT_TOKEN, admin_id, CHANNEL_ID
from handlers.user import update_info
from handlers.utils import (
    get_bot_url,
    get_bot_avatar_thumbnail,
    get_message_text,
    handle_download_error,
    handle_video_too_large,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    safe_delete_message,
    safe_edit_text,
    resolve_settings_target_id,
)
from log.logger import logger as logging
from main import bot, db, send_analytics
from utils.download_manager import (
    DownloadConfig,
    DownloadMetrics,
    ResilientDownloader,
    log_download_metrics,
)

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)  # 1.5 GB Telegram-safe limit
YTDLP_FORMAT_720 = "bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best"
YTDLP_SPEED_OPTS: dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "continuedl": True,
    "overwrites": True,
    "noplaylist": True,
    "cachedir": False,
    "socket_timeout": 15,
    "retries": 2,
    "fragment_retries": 2,
    "concurrent_fragment_downloads": 4,
}

router = Router()

youtube_downloader = ResilientDownloader(
    OUTPUT_DIR,
    config=DownloadConfig(
        chunk_size=2 * 1024 * 1024,          # Larger chunks for higher throughput
        multipart_threshold=8 * 1024 * 1024,  # Split earlier to parallelize medium files
        max_workers=10,                      # More concurrent range requests
        max_concurrent_downloads=2,          # Prevent thread explosion under multi-user load
        retry_backoff=0.6,                   # Slightly faster retry ramp-up
    ),
)


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def download_stream(stream: dict, filename: str, source: str) -> Optional[DownloadMetrics]:
    url = stream.get("url")
    if not url:
        logging.error("Stream missing URL for %s", source)
        return None

    headers = stream.get("http_headers") or {}
    try:
        metrics = await youtube_downloader.download(url, filename, headers=headers)
        log_download_metrics(source, metrics)
        return metrics
    except Exception as exc:
        logging.error("Failed to download stream: source=%s url=%s error=%s", source, url, exc)
        return None


async def download_with_ytdlp(url: str, filename: str) -> Optional[str]:
    """Fallback for cases when direct stream download produces invalid media."""
    out_path = os.path.join(OUTPUT_DIR, filename)
    os.makedirs(os.path.dirname(out_path) or OUTPUT_DIR, exist_ok=True)
    ydl_opts = {
        **YTDLP_SPEED_OPTS,
        "format": YTDLP_FORMAT_720,
        "outtmpl": out_path,
        "merge_output_format": "mp4",
    }
    try:
        await asyncio.to_thread(lambda: YoutubeDL(ydl_opts).download([url]))
        logging.info("yt-dlp fallback succeeded: url=%s path=%s", url, out_path)
        return out_path
    except Exception as exc:  # pragma: no cover - defensive
        logging.error("yt-dlp fallback failed: url=%s error=%s", url, exc)
        return None


async def download_with_ytdlp_metrics(url: str, filename: str, format_selector: str, source: str) -> Optional[DownloadMetrics]:
    """Download via yt-dlp and return DownloadMetrics for unified logging."""
    out_path = os.path.join(OUTPUT_DIR, filename)
    os.makedirs(os.path.dirname(out_path) or OUTPUT_DIR, exist_ok=True)
    ydl_opts = {
        **YTDLP_SPEED_OPTS,
        "format": format_selector,
        "outtmpl": out_path,
        "merge_output_format": "mp4",
    }
    start = time.monotonic()
    try:
        await asyncio.to_thread(lambda: YoutubeDL(ydl_opts).download([url]))
        elapsed = time.monotonic() - start
        metrics = DownloadMetrics(
            url=url,
            path=out_path,
            size=os.path.getsize(out_path),
            elapsed=elapsed,
            used_multipart=False,
            resumed=False,
        )
        log_download_metrics(source, metrics)
        return metrics
    except Exception as exc:
        logging.error("yt-dlp download failed: source=%s url=%s error=%s", source, url, exc)
        return None


async def download_mp3_with_ytdlp_metrics(url: str, base_name: str, source: str) -> Optional[DownloadMetrics]:
    """Download audio via yt-dlp and return MP3 metrics."""
    base_path = os.path.join(OUTPUT_DIR, base_name)
    out_template = f"{base_path}.%(ext)s"
    final_path = f"{base_path}.mp3"
    ydl_opts = {
        **YTDLP_SPEED_OPTS,
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "merge_output_format": "mp3",
    }
    start = time.monotonic()
    try:
        await asyncio.to_thread(lambda: YoutubeDL(ydl_opts).download([url]))
        elapsed = time.monotonic() - start
        resolved_path = final_path if os.path.exists(final_path) else None
        if not resolved_path:
            matches = glob.glob(f"{base_path}.*")
            resolved_path = matches[0] if matches else None
        if not resolved_path or not os.path.exists(resolved_path):
            logging.error("yt-dlp mp3 output missing: url=%s base=%s", url, base_path)
            return None
        metrics = DownloadMetrics(
            url=url,
            path=resolved_path,
            size=os.path.getsize(resolved_path),
            elapsed=elapsed,
            used_multipart=False,
            resumed=False,
        )
        log_download_metrics(source, metrics)
        return metrics
    except Exception as exc:
        logging.error("yt-dlp mp3 download failed: source=%s url=%s error=%s", source, url, exc)
        return None


async def download_media(url: str, filename: str, format_candidates: list[str]) -> bool:
    """
    Backward-compatible wrapper kept for tests; downloads the provided URL with the shared downloader.
    """
    metrics = await download_stream({"url": url}, filename, "youtube_legacy")
    return metrics is not None


def get_youtube_video(url):
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except DownloadError as e:
        logging.error(f"Error fetching YouTube info: {e}")
        return None


def get_video_stream(yt: dict, max_height: int = 720) -> dict | None:
    formats = yt.get("formats", [])
    progressive = [
        f for f in formats
        if f.get("vcodec") != "none"
           and f.get("acodec") != "none"
           and f.get("ext") == "mp4"
           and int(f.get("height") or 0) <= max_height
    ]
    progressive.sort(key=lambda x: int(x.get("height", 0)), reverse=True)
    if progressive:
        best = progressive[0]
        best["webpage_url"] = yt["webpage_url"]
        return best

    video_only = [
        f for f in formats
        if f.get("vcodec") != "none"
           and f.get("acodec") == "none"
           and f.get("ext") in ("mp4", "webm")
           and int(f.get("height") or 0) <= max_height
    ]
    video_only.sort(key=lambda x: int(x.get("height", 0)), reverse=True)
    if video_only:
        best = video_only[0]
        best["webpage_url"] = yt["webpage_url"]
        return best

    return None


def get_audio_stream(yt: dict) -> dict | None:
    formats = yt.get("formats", [])
    audio_streams = [
        f for f in formats
        if f.get("vcodec") == "none" and f.get("ext") in ("m4a", "mp4")
    ]
    audio_streams.sort(key=lambda f: float(f.get("abr") or 0), reverse=True)
    best = audio_streams[0] if audio_streams else None
    if best:
        best["webpage_url"] = yt["webpage_url"]
    return best


async def get_clip_dimensions(file_path: str) -> tuple[None, None] | Any:
    try:
        return await asyncio.to_thread(_get_dimensions, file_path)
    except Exception as e:
        logging.warning("Unable to read video dimensions: path=%s error=%s", file_path, e)
        return None, None


def _get_dimensions(file_path: str) -> Tuple[int, int]:
    with VideoFileClip(file_path) as clip:
        return clip.size


async def get_audio_duration(file_path: str) -> float:
    try:
        return await asyncio.to_thread(_get_audio_duration, file_path)
    except Exception as e:
        logging.error(f"Error getting audio duration: {e}")
        return 0


def _get_audio_duration(file_path: str) -> float:
    with AudioFileClip(file_path) as clip:
        return clip.duration


def _is_manifest_stream(stream: dict) -> bool:
    """Return True if stream points to HLS/DASH manifest instead of direct media."""
    protocol = (stream.get("protocol") or "").lower()
    manifest_url = stream.get("manifest_url") or stream.get("url") or ""
    return "m3u8" in protocol or "dash" in protocol or manifest_url.endswith(".m3u8")


@router.message(
    F.text.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(?!@)[\S]+)")
    | F.caption.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(?!@)[\S]+)")
)
@router.business_message(
    F.text.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(?!@)[\S]+)")
    | F.caption.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(?!@)[\S]+)")
)
async def download_video(message: types.Message):
    url = get_message_text(message)
    logging.info(
        "Downloading YouTube video : user_id=%s username=%s url=%s",
        message.from_user.id,
        message.from_user.username,
        url,
    )
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="youtube_video")
    status_message: Optional[types.Message] = None
    try:
        await react_to_message(message, "👾", skip_if_business=False)
        status_message = await message.answer(bm.fetching_info_status())

        user_settings = await db.user_settings(resolve_settings_target_id(message))
        user_captions = user_settings["captions"]
        bot_url = await get_bot_url(bot)

        yt = await asyncio.wait_for(asyncio.to_thread(get_youtube_video, url), timeout=45.0)
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
            await safe_delete_message(status_message)
            await bot.send_chat_action(message.chat.id, "upload_video")
            await message.answer_video(
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

        # Prefer high-speed downloader when stream is direct media; fall back to yt-dlp for manifests/HLS.
        if _is_manifest_stream(video):
            metrics = await asyncio.wait_for(
                download_with_ytdlp_metrics(
                    yt['webpage_url'],
                    name,
                    YTDLP_FORMAT_720,
                    "youtube_video_ytdlp_manifest",
                ),
                timeout=900.0,
            )
        else:
            metrics = await asyncio.wait_for(download_stream(video, name, "youtube_video"), timeout=540.0)
            if not metrics:
                metrics = await asyncio.wait_for(
                    download_with_ytdlp_metrics(
                        yt['webpage_url'],
                        name,
                        YTDLP_FORMAT_720,
                        "youtube_video_ytdlp",
                    ),
                    timeout=900.0,
                )
        if not metrics:
            await handle_download_error(message, skip_if_business=False)
            return

        if metrics.size >= MAX_FILE_SIZE:
            await handle_video_too_large(message, skip_if_business=False)
            await remove_file(metrics.path)
            return

        width, height = await get_clip_dimensions(metrics.path)
        if (width is None or height is None) and metrics.url != yt['webpage_url']:
            # If direct stream failed, try yt-dlp as a last resort
            fallback = await asyncio.wait_for(
                download_with_ytdlp_metrics(
                    yt['webpage_url'],
                    name,
                    YTDLP_FORMAT_720,
                    "youtube_video_ytdlp_retry",
                ),
                timeout=900.0,
            )
            if not fallback:
                await handle_download_error(message, skip_if_business=False)
                return
            metrics = fallback
            width, height = await get_clip_dimensions(metrics.path)

        await safe_edit_text(status_message, bm.uploading_status())
        await bot.send_chat_action(message.chat.id, "upload_video")
        sent_message = await message.answer_video(
            video=FSInputFile(metrics.path),
            width=width,
            height=height,
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
    except asyncio.TimeoutError:
        await safe_edit_text(status_message, bm.timeout_error())
        await handle_download_error(message, skip_if_business=False, text=bm.timeout_error())
    except Exception as e:
        logging.error(f"Video download error: {e}")
        await handle_download_error(message, skip_if_business=False)
    finally:
        await safe_delete_message(status_message)
    await update_info(message)


@router.message(
    F.text.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+')
    | F.caption.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+')
)
@router.business_message(
    F.text.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+')
    | F.caption.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+')
)
async def download_music(message: types.Message):
    url = get_message_text(message)
    logging.info(
        "Downloading YouTube audio: user_id=%s username=%s url=%s",
        message.from_user.id,
        message.from_user.username,
        url,
    )
    try:
        await react_to_message(message, "👾", skip_if_business=False)
        user_settings = await db.user_settings(resolve_settings_target_id(message))
        bot_url = await get_bot_url(bot)
        bot_avatar = await get_bot_avatar_thumbnail(bot)

        # Get YouTube audio object - run in thread pool
        yt = await asyncio.to_thread(get_youtube_video, url)
        audio = await asyncio.to_thread(get_audio_stream, yt)

        if not audio:
            await message.reply(bm.nothing_found())
            return

        audio_ext = audio.get("ext") or "m4a"
        name = f"{yt['id']}_youtube_audio.{audio_ext}"

        metrics = await download_with_ytdlp_metrics(
            yt['webpage_url'],
            name,
            "bestaudio/best",
            "youtube_audio_ytdlp",
        )
        if not metrics:
            metrics = await download_stream(audio, name, "youtube_audio")
        if not metrics:
            await handle_download_error(message, skip_if_business=False)
            return

        # Get audio duration asynchronously
        audio_duration = await get_audio_duration(metrics.path)

        await bot.send_chat_action(message.chat.id, "upload_voice")

        await message.answer_audio(
            audio=FSInputFile(metrics.path),
            title=yt['title'],
            duration=round(audio_duration),
            caption=bm.captions(None, None, bot_url),
            thumbnail=bot_avatar,
            parse_mode="HTML"
        )
        await maybe_delete_user_message(message, user_settings.get("delete_message"))

        await remove_file(metrics.path)
    except Exception as e:
        logging.error(f"Audio download error: {e}")
        await handle_download_error(message, skip_if_business=False)
    await update_info(message)


@router.callback_query(F.data.startswith("audio:youtube:"))
async def download_youtube_mp3_callback(call: types.CallbackQuery):
    if not call.message:
        await call.answer("Open the bot to download MP3", show_alert=True)
        return

    await call.answer()
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
            await handle_download_error(call.message, skip_if_business=False)
            return

        cache_key = f"{yt['webpage_url']}#audio"
        db_file_id = await db.get_file_id(cache_key)
        if db_file_id:
            await bot.send_chat_action(call.message.chat.id, "upload_audio")
            try:
                await status_message.delete()
                status_message = None
            except Exception:
                pass
            await call.message.answer_audio(
                audio=db_file_id,
                title=yt.get("title"),
                caption=bm.captions(None, None, bot_url),
                thumbnail=bot_avatar,
                parse_mode="HTML",
            )
            return

        base_name = f"{yt['id']}_youtube_audio"
        metrics = await download_mp3_with_ytdlp_metrics(
            yt['webpage_url'],
            base_name,
            "youtube_audio_mp3",
        )
        if not metrics:
            await handle_download_error(call.message, skip_if_business=False)
            return

        if metrics.size >= MAX_FILE_SIZE:
            await call.message.reply(bm.audio_too_large())
            await remove_file(metrics.path)
            return

        audio_duration = await get_audio_duration(metrics.path)
        await bot.send_chat_action(call.message.chat.id, "upload_audio")
        try:
            await status_message.delete()
            status_message = None
        except Exception:
            pass
        sent_message = await call.message.answer_audio(
            audio=FSInputFile(metrics.path),
            title=yt.get("title"),
            duration=round(audio_duration),
            caption=bm.captions(None, None, bot_url),
            thumbnail=bot_avatar,
            parse_mode="HTML",
        )
        await db.add_file(cache_key, sent_message.audio.file_id, "audio")

        await remove_file(metrics.path)
    finally:
        if status_message:
            try:
                await status_message.delete()
            except Exception:
                pass


@router.inline_query(F.query.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(?!@)[\S]+)"))
async def inline_youtube_query(query: types.InlineQuery):
    try:
        url = query.query
        logging.info(
            "Downloading YouTube Inline: user_id=%s query=%s",
            query.from_user.id,
            url,
        )
        yt = await asyncio.to_thread(get_youtube_video, url)

        views = safe_int(yt.get('view_count'), 0)
        likes = safe_int(yt.get('like_count'), 0)

        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_youtube_shorts")

        user_settings = await db.user_settings(query.from_user.id)
        user_captions = user_settings["captions"]
        bot_url = await get_bot_url(bot)

        db_file_id = await db.get_file_id(yt['webpage_url'])
        if db_file_id:
            logging.info(
                "Serving cached YouTube inline video: url=%s file_id=%s",
                yt['webpage_url'],
                db_file_id,
            )
            results = [
                InlineQueryResultVideo(
                    id=f"shorts_{yt['id']}",
                    video_url=db_file_id,
                    thumbnail_url=yt['thumbnail'],
                    description=yt['title'],
                    title="🎬 YouTube Shorts",
                    mime_type="video/mp4",
                    caption=bm.captions(user_captions, yt['title'], bot_url),
                    reply_markup=kb.return_video_info_keyboard(
                        views=views,
                        likes=likes,
                        comments=None,
                        shares=None,
                        music_play_url=None,
                        video_url=yt['webpage_url'],
                        user_settings=user_settings,
                    )
                )
            ]
            await query.answer(results, cache_time=10)
            return

        if "shorts" not in url.lower():
            results = [
                InlineQueryResultArticle(
                    id="not_shorts",
                    title="⚠️ Not a Shorts Video",
                    description="Regular YouTube videos are not supported in inline mode due to size limitations.",
                    input_message_content=types.InputTextMessageContent(
                        message_text="⚠️ Regular YouTube videos are not supported in inline mode due to size limitations. Please use the bot directly for regular videos."
                    )
                )
            ]
            await query.answer(results, cache_time=10)
            return

        video = await asyncio.to_thread(get_video_stream, yt)
        if not video:
            await query.answer([], cache_time=1, is_personal=True)
            return

        name = f"{yt['id']}_youtube_shorts.mp4"

        if _is_manifest_stream(video):
            metrics = await download_with_ytdlp_metrics(
                yt['webpage_url'],
                name,
                YTDLP_FORMAT_720,
                "youtube_inline_ytdlp_manifest",
            )
        else:
            metrics = await download_stream(video, name, "youtube_inline")
            if not metrics:
                metrics = await download_with_ytdlp_metrics(
                    yt['webpage_url'],
                    name,
                    YTDLP_FORMAT_720,
                    "youtube_inline_ytdlp",
                )
        if not metrics:
            await query.answer([], cache_time=1, is_personal=True)
            return

        video_file = FSInputFile(metrics.path)
        sent_message = await bot.send_video(
            chat_id=CHANNEL_ID,
            video=video_file,
            caption=f"?? YouTube Shorts from {query.from_user.full_name}"
        )
        video_file_id = sent_message.video.file_id
        await db.add_file(yt['webpage_url'], video_file_id, "video")
        logging.info(
            "YouTube inline video cached: url=%s file_id=%s",
            yt['webpage_url'],
            video_file_id,
        )

        results = [
            InlineQueryResultVideo(
                id=f"shorts_{yt['id']}",
                video_url=video_file_id,
                thumbnail_url=yt['thumbnail'],
                description=yt['title'],
                title="?? YouTube Shorts",
                mime_type="video/mp4",
                caption=bm.captions(user_captions, yt['title'], bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    views=views,
                    likes=likes,
                    comments=None,
                    shares=None,
                    music_play_url=None,
                    video_url=yt['webpage_url'],
                    user_settings=user_settings,
                )
            )
        ]

        await query.answer(results, cache_time=10)

        await remove_file(metrics.path)
        return

    except Exception as e:
        logging.error(f"Error processing inline query: {e}")
        await query.answer([], cache_time=1, is_personal=True)
