import asyncio
import os
import time

import requests
from log.logger import logger as logging
from aiogram import types, Router, F
from aiogram.types import FSInputFile, InlineQueryResultVideo, InlineQueryResultArticle
from moviepy import VideoFileClip, AudioFileClip
from pytubefix import YouTube
from pytubefix.cli import on_progress

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, BOT_TOKEN, admin_id, CHANNEL_ID
from handlers.user import update_info
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
        logging.info("Message sent successfully.")
    else:
        logging.error(f"Failed to send message. Status code: {response.status_code}")
    for i in range(30, 0, -5):
        logging.info(f"{i} seconds remaining")
        time.sleep(5)


def download_media(stream, filename):
    try:
        stream.download(output_path=OUTPUT_DIR, filename=filename)
    except Exception as e:
        logging.error(f"Download error: {e}")


def get_youtube_video(url):
    return YouTube(url, use_oauth=True, allow_oauth_cache=True, on_progress_callback=on_progress,
                   oauth_verifier=custom_oauth_verifier)


def get_video_stream(yt):
    return yt.streams.filter(res="1080p", file_extension='mp4', progressive=True).first() or \
        yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()


def get_audio_stream(yt):
    return yt.streams.filter(only_audio=True, file_extension='mp4').first()


async def handle_download_error(message, business_id):
    if business_id is None:
        await message.react([types.ReactionTypeEmoji(emoji="👎")])
    await message.reply(bm.something_went_wrong())


@router.message(F.text.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+)"))
@router.business_message(F.text.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+)"))
async def download_video(message: types.Message):
    url = message.text
    business_id = message.business_connection_id
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="youtube_video")
    try:
        if business_id is None:
            await message.react([types.ReactionTypeEmoji(emoji="👨‍💻")])

        yt = get_youtube_video(url)
        video = get_video_stream(yt)

        if not video:
            await message.reply(bm.nothing_found())
            return

        size = video.filesize_kb

        if size >= MAX_FILE_SIZE:
            await message.reply(bm.video_too_large())
            return

        db_file_id = await db.get_file_id(yt.watch_url)

        if db_file_id:
            await message.answer_video(
                video=db_file_id,
                caption=bm.captions(await db.get_user_captions(message.from_user.id), yt.title,
                                    f"t.me/{(await bot.get_me()).username}"),
                reply_markup=kb.return_video_info_keyboard(
                    views=None,
                    likes=None,
                    comments=None,
                    shares=None,
                    music_play_url=None,
                    video_url=yt.watch_url
                ) if not business_id else None,
                parse_mode="HTML"
            )

        else:
            name = f"{yt.video_id}_youtube_video.mp4"

            video_file_path = os.path.join(OUTPUT_DIR, name)

            await asyncio.get_event_loop().run_in_executor(None, download_media, video, name)
            video_clip = VideoFileClip(video_file_path)

            sent_message = await message.answer_video(
                video=FSInputFile(video_file_path),
                width=video_clip.w,
                height=video_clip.h,
                caption=bm.captions(await db.get_user_captions(message.from_user.id), yt.title,
                                    f"t.me/{(await bot.get_me()).username}"),
                reply_markup=kb.return_video_info_keyboard(
                    views=None,
                    likes=None,
                    comments=None,
                    shares=None,
                    music_play_url=None,
                    video_url=yt.watch_url
                ) if not business_id else None,
                parse_mode="HTML"
            )
            await db.add_file(yt.watch_url, sent_message.video.file_id, "video")

            await asyncio.sleep(5)
            os.remove(video_file_path)
    except Exception as e:
        logging.error(f"Video download error: {e}")
        await handle_download_error(message, business_id)
    await update_info(message)


@router.message(F.text.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+'))
@router.business_message(F.text.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+'))
async def download_music(message: types.Message):
    url = message.text
    business_id = message.business_connection_id
    try:
        if business_id is None:
            await message.react([types.ReactionTypeEmoji(emoji="👨‍💻")])

        yt = get_youtube_video(url)
        audio = get_audio_stream(yt)
        if not audio:
            await message.reply(bm.nothing_found())
            return

        name = f"{yt.video_id}_youtube_audio.mp3"
        audio_file_path = os.path.join(OUTPUT_DIR, name)
        await asyncio.get_event_loop().run_in_executor(None, download_media, audio, name)

        audio_duration = AudioFileClip(audio_file_path).duration
        await bot.send_chat_action(message.chat.id, "upload_voice")

        await message.answer_audio(
            audio=FSInputFile(audio_file_path),
            title=yt.title,
            duration=round(audio_duration),
            caption=bm.captions(None, None, f"t.me/{(await bot.get_me()).username}"),
            parse_mode="HTML"
        )

        await asyncio.sleep(5)
        os.remove(audio_file_path)
    except Exception as e:
        logging.error(f"Audio download error: {e}")
        await handle_download_error(message, business_id)
    await update_info(message)


@router.inline_query(F.query.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+)"))
async def inline_youtube_query(query: types.InlineQuery):
    try:
        url = query.query
        yt = get_youtube_video(url)

        if not "shorts" in url.lower():
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

        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_youtube_shorts")
        user_captions = await db.get_user_captions(query.from_user.id)
        bot_url = f"t.me/{(await bot.get_me()).username}"

        # Check if video exists in database first
        db_file_id = await db.get_file_id(yt.watch_url)
        if db_file_id:
            results = [
                InlineQueryResultVideo(
                    id=f"shorts_{yt.video_id}",
                    video_url=db_file_id,
                    thumbnail_url=yt.thumbnail_url,
                    description=yt.title,
                    title="🎥 YouTube Shorts",
                    mime_type="video/mp4",
                    caption=bm.captions(user_captions, yt.title, bot_url),
                    reply_markup=kb.return_video_info_keyboard(
                        views=None,
                        likes=None,
                        comments=None,
                        shares=None,
                        music_play_url=None,
                        video_url=yt.watch_url
                    )
                )
            ]
            await query.answer(results, cache_time=10)
            return

        video = get_video_stream(yt)
        if not video:
            await query.answer([], cache_time=1, is_personal=True)
            return

        name = f"{yt.video_id}_youtube_shorts.mp4"
        video_file_path = os.path.join(OUTPUT_DIR, name)

        await asyncio.get_event_loop().run_in_executor(None, download_media, video, name)
        video = FSInputFile(video_file_path)
        sent_message = await bot.send_video(
            chat_id=CHANNEL_ID,
            video=video,
            caption=f"🎥 YouTube Shorts from {query.from_user.full_name}"
        )
        video_file_id = sent_message.video.file_id
        await db.add_file(yt.watch_url, video_file_id, "video")

        results = [
            InlineQueryResultVideo(
                id=f"shorts_{yt.video_id}",
                video_url=video_file_id,
                thumbnail_url=yt.thumbnail_url,
                description=yt.title,
                title="🎥 YouTube Shorts",
                mime_type="video/mp4",
                caption=bm.captions(user_captions, yt.title, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    views=None,
                    likes=None,
                    comments=None,
                    shares=None,
                    music_play_url=None,
                    video_url=yt.watch_url
                )
            )
        ]

        await query.answer(results, cache_time=10)
        await asyncio.sleep(5)
        os.remove(video_file_path)

    except Exception as e:
        logging.error(f"Error processing inline query: {e}")
        await query.answer([], cache_time=1, is_personal=True)
