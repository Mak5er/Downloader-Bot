import asyncio
import os
import time
from typing import Tuple, Any

import requests
from aiogram import types, Router, F
from aiogram.types import FSInputFile, InlineQueryResultVideo, InlineQueryResultArticle
from moviepy import VideoFileClip, AudioFileClip
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, BOT_TOKEN, admin_id, CHANNEL_ID
from handlers.user import update_info
from log.logger import logger as logging
from main import bot, db, send_analytics

MAX_FILE_SIZE = 1 * 1024 * 1024

router = Router()


def custom_oauth_verifier(verification_url, user_code):
    send_message_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    params = {
        "chat_id": admin_id,
        "text": f"<b>OAuth Verification</b>\n\nOpen this URL in your browser:\n{verification_url}\n\nEnter this code:\n<code>{user_code}</code>",
        "parse_mode": "HTML"
    }
    response = requests.get(send_message_url, params=params)
    if response.status_code == 200:
        logging.info("OAuth message sent successfully.")
    else:
        logging.error(f"OAuth message failed. Status code: {response.status_code}")
    for i in range(30, 0, -5):
        logging.info(f"{i} seconds remaining for verification")
        time.sleep(5)


async def download_media(url: str, filename: str, format_candidates: list[str]) -> bool:
    outtmpl = os.path.join(OUTPUT_DIR, filename)
    last_error: Exception | None = None

    for format_expression in format_candidates:
        try:
            ydl_opts = {
                'quiet': True,
                'format': format_expression,
                'outtmpl': outtmpl,
                'merge_output_format': 'mp4',
                'oauth': True,
                'oauth_verifier': custom_oauth_verifier,
            }
            logging.debug(
                "Attempting download: url=%s filename=%s format=%s",
                url,
                filename,
                format_expression,
            )
            await asyncio.to_thread(lambda: YoutubeDL(ydl_opts).download([url]))
            logging.info(
                "Download succeeded: url=%s filename=%s format=%s",
                url,
                filename,
                format_expression,
            )
            return True
        except Exception as e:
            last_error = e
            logging.warning(
                "Download attempt failed: url=%s format=%s error=%s",
                url,
                format_expression,
                e,
            )

    if last_error:
        logging.error(
            "Download failed after trying all formats: url=%s formats=%s error=%s",
            url,
            format_candidates,
            last_error,
        )
    return False


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


def get_video_stream(yt: dict) -> dict | None:
    formats = yt.get("formats", [])
    progressive = [
        f for f in formats
        if f.get("vcodec") != "none"
        and f.get("acodec") != "none"
        and f.get("ext") == "mp4"
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
        logging.error(f"Error getting video dimensions: {e}")
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


async def safe_remove(file_path: str):
    try:
        await asyncio.to_thread(os.remove, file_path)
    except Exception as e:
        logging.error(f"Error removing file {file_path}: {e}")


async def handle_download_error(message):
    await message.react([types.ReactionTypeEmoji(emoji="👎")])
    await message.reply(bm.something_went_wrong())

@router.message(F.text.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(?!@)[\S]+)"))
async def download_video(message: types.Message):
    url = message.text
    logging.info(
        "Downloading YouTube video : user_id=%s username=%s url=%s",
        message.from_user.id,
        message.from_user.username,
        url,
    )
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="youtube_video")
    try:
        await message.react([types.ReactionTypeEmoji(emoji="👾")])

        user_settings = await db.user_settings(message.from_user.id)
        user_captions = user_settings["captions"]
        bot_data = await bot.get_me()
        bot_url = "t.me/" + bot_data.username

        yt = await asyncio.to_thread(get_youtube_video, url)
        video = await asyncio.to_thread(get_video_stream, yt)

        if not video:
            await message.reply(bm.nothing_found())
            return

        views = int(yt.get('view_count', 0))
        likes = int(yt.get('like_count', 0))

        size = video.get('filesize_approx', 0) // 1024  # Convert to KB
        if size >= MAX_FILE_SIZE:
            await message.reply(bm.video_too_large())
            return

        db_file_id = await db.get_file_id(yt['webpage_url'])

        if db_file_id:
            logging.info(
                "Serving cached YouTube video: url=%s file_id=%s",
                yt['webpage_url'],
                db_file_id,
            )
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
                ),
                parse_mode="HTML"
            )
            return

        name = f"{yt['id']}_youtube_video.mp4"
        video_file_path = os.path.join(OUTPUT_DIR, name)

        format_candidates = [
            "best",
        ]

        if await download_media(yt['webpage_url'], name, format_candidates):
            width, height = await get_clip_dimensions(video_file_path)

            await bot.send_chat_action(message.chat.id, "upload_video")
            sent_message = await message.answer_video(
                video=FSInputFile(video_file_path),
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
                ),
                parse_mode="HTML"
            )
            await db.add_file(yt['webpage_url'], sent_message.video.file_id, "video")
            logging.info(
                "YouTube video cached: url=%s file_id=%s",
                yt['webpage_url'],
                sent_message.video.file_id,
            )

            await asyncio.sleep(5)
            await safe_remove(video_file_path)
        else:
            await handle_download_error(message)
    except Exception as e:
        logging.error(f"Video download error: {e}")
        await handle_download_error(message)
    await update_info(message)


@router.message(F.text.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+'))
async def download_music(message: types.Message):
    url = message.text
    logging.info(
        "Downloading YouTube audio: user_id=%s username=%s url=%s",
        message.from_user.id,
        message.from_user.username,
        url,
    )
    try:
        await message.react([types.ReactionTypeEmoji(emoji="👾")])

        # Get YouTube audio object - run in thread pool
        yt = await asyncio.to_thread(get_youtube_video, url)
        audio = await asyncio.to_thread(get_audio_stream, yt)

        if not audio:
            await message.reply(bm.nothing_found())
            return

        name = f"{yt['id']}_youtube_audio.mp3"
        audio_file_path = os.path.join(OUTPUT_DIR, name)

        # Download audio asynchronously
        format_candidates = [
            "bestaudio/best",
        ]

        if await download_media(yt['webpage_url'], name, format_candidates):
            # Get audio duration asynchronously
            audio_duration = await get_audio_duration(audio_file_path)

            await bot.send_chat_action(message.chat.id, "upload_voice")

            await message.answer_audio(
                audio=FSInputFile(audio_file_path),
                title=yt['title']
                ,
                duration=round(audio_duration),
                caption=bm.captions(None, None, f"t.me/{(await bot.get_me()).username}"),
                parse_mode="HTML"
            )

            # Clean up file asynchronously
            await asyncio.sleep(5)
            await safe_remove(audio_file_path)
        else:
            await handle_download_error(message)
    except Exception as e:
        logging.error(f"Audio download error: {e}")
        await handle_download_error(message)
    await update_info(message)


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

        views = int(yt.get('view_count', 0))
        likes = int(yt.get('like_count', 0))

        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_youtube_shorts")

        user_settings = await db.user_settings(query.from_user.id)
        user_captions = user_settings["captions"]
        bot_data = await bot.get_me()
        bot_url = "t.me/" + bot_data.username

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
        video_file_path = os.path.join(OUTPUT_DIR, name)

        format_candidates = [
            "best",
        ]

        if await download_media(yt['webpage_url'], name, format_candidates):
            video_file = FSInputFile(video_file_path)
            sent_message = await bot.send_video(
                chat_id=CHANNEL_ID,
                video=video_file,
                caption=f"🎬 YouTube Shorts from {query.from_user.full_name}"
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

            await asyncio.sleep(5)
            await safe_remove(video_file_path)
        else:
            await query.answer([], cache_time=1, is_personal=True)

    except Exception as e:
        logging.error(f"Error processing inline query: {e}")
        await query.answer([], cache_time=1, is_personal=True)
