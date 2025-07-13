import asyncio
import os
import time
from typing import Optional, Tuple, Any

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


async def download_media(stream: dict, filename: str) -> bool:
    try:
        fmt = stream.get("format_id") or stream.get("format")
        page_url = stream.get("webpage_url")
        if not fmt or not page_url:
            logging.error("download_media: missing format_id or webpage_url")
            return False

        outtmpl = os.path.join(OUTPUT_DIR, filename)
        ydl_opts = {
            'quiet': True,
            'format': fmt,
            'outtmpl': outtmpl,
            'merge_output_format': 'mp4',
            'oauth': True,
            'oauth_verifier': custom_oauth_verifier,
        }
        # Завантажуємо через сторінку, а не прямою URL
        await asyncio.to_thread(lambda: YoutubeDL(ydl_opts).download([page_url]))
        return True

    except Exception as e:
        logging.error(f"Download error: {e}")
        return False


def get_youtube_video(url):
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'oauth': True,
            'oauth_verifier': custom_oauth_verifier,
        }
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except DownloadError as e:
        logging.error(f"Error fetching YouTube info: {e}")
        return None


def get_video_stream(yt: dict) -> dict | None:
    formats = yt.get("formats", [])
    vs = [f for f in formats
          if f.get("vcodec") != "none"
          and f.get("acodec") != "none"
          and f.get("ext") == "mp4"]
    vs.sort(key=lambda x: int(x.get("height", 0)), reverse=True)
    best = vs[0] if vs else None
    if best:
        best["webpage_url"] = yt["webpage_url"]
    return best


def get_audio_stream(yt: dict) -> dict | None:
    formats = yt.get("formats", [])
    audio_streams = [
        f for f in formats
        if f.get("vcodec") == "none" and f.get("ext") in ("m4a", "mp4")
    ]
    # Якщо abr=None, то (f.get("abr") or 0) дасть 0
    audio_streams.sort(key=lambda f: float(f.get("abr") or 0), reverse=True)
    best = audio_streams[0] if audio_streams else None
    if best:
        best["webpage_url"] = yt["webpage_url"]
    return best


async def get_clip_dimensions(file_path: str) -> tuple[None, None] | Any:
    try:
        # Offload VideoFileClip operations to thread pool
        return await asyncio.to_thread(_get_dimensions, file_path)
    except Exception as e:
        logging.error(f"Error getting video dimensions: {e}")
        return None, None


def _get_dimensions(file_path: str) -> Tuple[int, int]:
    with VideoFileClip(file_path) as clip:
        return clip.size


async def get_audio_duration(file_path: str) -> float:
    try:
        # Offload AudioFileClip operations to thread pool
        return await asyncio.to_thread(_get_audio_duration, file_path)
    except Exception as e:
        logging.error(f"Error getting audio duration: {e}")
        return 0


def _get_audio_duration(file_path: str) -> float:
    with AudioFileClip(file_path) as clip:
        return clip.duration


async def safe_remove(file_path: str):
    try:
        # Offload file removal to thread pool
        await asyncio.to_thread(os.remove, file_path)
    except Exception as e:
        logging.error(f"Error removing file {file_path}: {e}")


async def handle_download_error(message):
    await message.react([types.ReactionTypeEmoji(emoji="👎")])
    await message.reply(bm.something_went_wrong())


@router.message(F.text.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(?!@)[\S]+)"))
async def download_video(message: types.Message):
    url = message.text
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="youtube_video")
    try:
        await message.react([types.ReactionTypeEmoji(emoji="👨‍💻")])

        # Get YouTube video object - this is a heavy operation so run in thread pool
        yt = await asyncio.to_thread(get_youtube_video, url)
        video = await asyncio.to_thread(get_video_stream, yt)

        if not video:
            await message.reply(bm.nothing_found())
            return

        # Get video metadata
        views = int(yt.get('view_count', 0))
        likes = int(yt.get('like_count', 0))

        size = video.get('filesize_approx', 0) // 1024  # Convert to KB

        if size >= MAX_FILE_SIZE:
            await message.reply(bm.video_too_large())
            return

        db_file_id = await db.get_file_id(yt['webpage_url'])

        if db_file_id:
            await bot.send_chat_action(message.chat.id, "upload_video")
            await message.answer_video(
                video=db_file_id,
                caption=bm.captions(await db.get_user_captions(message.from_user.id), yt['title']
                                    ,
                                    f"t.me/{(await bot.get_me()).username}"),
                reply_markup=kb.return_video_info_keyboard(
                    views=views,
                    likes=likes,
                    comments=None,
                    shares=None,
                    music_play_url=None,
                    video_url=yt['webpage_url'],
                ),
                parse_mode="HTML"
            )
        else:
            name = f"{yt['id']}_youtube_video.mp4"
            video_file_path = os.path.join(OUTPUT_DIR, name)

            # Download video asynchronously
            if await download_media(video, name):
                # Get video dimensions asynchronously
                width, height = await get_clip_dimensions(video_file_path)

                await bot.send_chat_action(message.chat.id, "upload_video")
                sent_message = await message.answer_video(
                    video=FSInputFile(video_file_path),
                    width=width,
                    height=height,
                    caption=bm.captions(await db.get_user_captions(message.from_user.id), yt['title']
                                        ,
                                        f"t.me/{(await bot.get_me()).username}"),
                    reply_markup=kb.return_video_info_keyboard(
                        views=views,
                        likes=likes,
                        comments=None,
                        shares=None,
                        music_play_url=None,
                        video_url=yt['webpage_url'],
                    ),
                    parse_mode="HTML"
                )
                await db.add_file(yt['webpage_url'], sent_message.video.file_id, "video")

                # Clean up file asynchronously
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
    try:
        await message.react([types.ReactionTypeEmoji(emoji="👨‍💻")])

        # Get YouTube audio object - run in thread pool
        yt = await asyncio.to_thread(get_youtube_video, url)
        audio = await asyncio.to_thread(get_audio_stream, yt)

        if not audio:
            await message.reply(bm.nothing_found())
            return

        name = f"{yt['id']}_youtube_audio.mp3"
        audio_file_path = os.path.join(OUTPUT_DIR, name)

        # Download audio asynchronously
        if await download_media(audio, name):
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
        # Get YouTube video object - run in thread pool
        yt = await asyncio.to_thread(get_youtube_video, url)

        # Get video metadata
        views = int(yt.get('view_count', 0))
        likes = int(yt.get('like_count', 0))

        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_youtube_shorts")
        user_captions = await db.get_user_captions(query.from_user.id)
        bot_url = f"t.me/{(await bot.get_me()).username}"

        # Check if video exists in database first
        db_file_id = await db.get_file_id(yt['webpage_url'])
        if db_file_id:
            results = [
                InlineQueryResultVideo(
                    id=f"shorts_{yt['id']}",
                    video_url=db_file_id,
                    thumbnail_url=yt['thumbnail'],
                    description=yt['title'],
                    title="🎥 YouTube Shorts",
                    mime_type="video/mp4",
                    caption=bm.captions(user_captions, yt['title']
                                        , bot_url),
                    reply_markup=kb.return_video_info_keyboard(
                        views=views,
                        likes=likes,
                        comments=None,
                        shares=None,
                        music_play_url=None,
                        video_url=yt['webpage_url'],
                    )
                )
            ]
            await query.answer(results, cache_time=10)
            return

        if "shorts" not in url.lower():
            results = [
                InlineQueryResultArticle(
                    id="not_shorts",
                    title="❌ Not a Shorts Video",
                    description="Regular YouTube videos are not supported in inline mode due to size limitations.",
                    input_message_content=types.InputTextMessageContent(
                        message_text="❌ Regular YouTube videos are not supported in inline mode due to size limitations. Please use the bot directly for regular videos."
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

        # Download video asynchronously
        if await download_media(video, name):
            video = FSInputFile(video_file_path)
            sent_message = await bot.send_video(
                chat_id=CHANNEL_ID,
                video=video,
                caption=f"🎥 YouTube Shorts from {query.from_user.full_name}"
            )
            video_file_id = sent_message.video.file_id
            await db.add_file(yt['webpage_url'], video_file_id, "video")

            results = [
                InlineQueryResultVideo(
                    id=f"shorts_{yt['id']}",
                    video_url=video_file_id,
                    thumbnail_url=yt['thumbnail']
                    ,
                    description=yt['title']
                    ,
                    title="🎥 YouTube Shorts",
                    mime_type="video/mp4",
                    caption=bm.captions(user_captions, yt['title']
                                        , bot_url),
                    reply_markup=kb.return_video_info_keyboard(
                        views=views,
                        likes=likes,
                        comments=None,
                        shares=None,
                        music_play_url=None,
                        video_url=yt['webpage_url'],
                    )
                )
            ]

            await query.answer(results, cache_time=10)

            # Clean up file asynchronously
            await asyncio.sleep(5)
            await safe_remove(video_file_path)
        else:
            await query.answer([], cache_time=1, is_personal=True)

    except Exception as e:
        logging.error(f"Error processing inline query: {e}")
        await query.answer([], cache_time=1, is_personal=True)
