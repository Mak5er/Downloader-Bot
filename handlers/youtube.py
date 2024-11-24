import asyncio
import datetime
import os
import time

import requests
from aiogram import types, Router, F
from aiogram.types import FSInputFile
from moviepy import VideoFileClip, AudioFileClip
from pytubefix import YouTube
from pytubefix.cli import on_progress

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, BOT_TOKEN, admin_id
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
        print("Message sent successfully.")
    else:
        print(f"Failed to send message. Status code: {response.status_code}")

    # Countdown
    for i in range(30, 0, -5):
        print(f"{i} seconds remaining")
        time.sleep(5)


def download_youtube_video(video, name):
    video.download(output_path=OUTPUT_DIR, filename=name)


# Download video
@router.message(F.text.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+)"))
@router.business_message(F.text.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+)"))
async def download_video(message: types.Message):
    business_id = message.business_connection_id

    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="youtube_video")

    bot_url = f"t.me/{(await bot.get_me()).username}"
    file_type = "video"

    url = message.text
    try:
        if business_id is None:
            react = types.ReactionTypeEmoji(emoji="👨‍💻")
            await message.react([react])

        time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{time}_youtube_video"

        yt = YouTube(url, use_oauth=True, allow_oauth_cache=True, on_progress_callback=on_progress,
                     oauth_verifier=custom_oauth_verifier)
        video = yt.streams.filter(res="1080p", file_extension='mp4', progressive=True).first()

        if not video:
            video = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
            if not video:
                await message.reply("The URL does not seem to be a valid YouTube video link.")
                return

        post_caption = yt.title

        user_captions = await db.get_user_captions(message.from_user.id)

        db_file_id = await db.get_file_id(yt.watch_url)

        if db_file_id:
            if business_id is None:
                await bot.send_chat_action(message.chat.id, "upload_video")

            await message.answer_video(video=db_file_id[0][0],
                                       caption=bm.captions(user_captions, post_caption, bot_url),
                                       reply_markup=kb.return_audio_download_keyboard("yt",
                                                                                      yt.watch_url) if business_id is None else None,
                                       parse_mode="HTMl")
            return

        size = video.filesize_kb

        if size < MAX_FILE_SIZE:
            video_file_path = os.path.join(OUTPUT_DIR, name + ".mp4")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, download_youtube_video, video, name)

            video_clip = VideoFileClip(video_file_path)

            width, height = video_clip.size

            if business_id is None:
                await bot.send_chat_action(message.chat.id, "upload_video")

            sent_message = await message.answer_video(video=FSInputFile(video_file_path),
                                                      width=width,
                                                      height=height,
                                                      caption=bm.captions(user_captions, post_caption, bot_url),
                                                      reply_markup=kb.return_audio_download_keyboard("yt",
                                                                                                     yt.watch_url) if business_id is None else None)
            file_id = sent_message.video.file_id

            await db.add_file(yt.watch_url, file_id, file_type)

            await asyncio.sleep(5)

        else:
            if business_id is None:
                react = types.ReactionTypeEmoji(emoji="👎")
                await message.react([react])

            await message.reply("The video is too large.")

    except Exception as e:
        print(e)
        if business_id is None:
            react = types.ReactionTypeEmoji(emoji="👎")
            await message.react([react])

        await message.reply("Something went wrong :(\nPlease try again later.")

    await update_info(message)


@router.callback_query(F.data.startswith('yt_audio_'))
async def download_audio(call: types.CallbackQuery):
    bot_url = f"t.me/{(await bot.get_me()).username}"

    url = call.data.split('_')[2]

    time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{time}_youtube_audio"

    yt = YouTube(url, use_oauth=True, allow_oauth_cache=True, on_progress_callback=on_progress,
                 oauth_verifier=custom_oauth_verifier)
    audio = yt.streams.filter(only_audio=True, file_extension='mp4').first()

    if not audio:
        await call.message.reply("The URL does not seem to be a valid YouTube music link.")
        return

    file_size = audio.filesize_kb

    audio_file_path = os.path.join(OUTPUT_DIR, name + '.m4a')

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, download_youtube_video, audio, name)

    # Check file size
    if file_size > MAX_FILE_SIZE:
        os.remove(audio_file_path)
        await call.message.reply("The audio file is too large.")
        return

    audio_duration = AudioFileClip(audio_file_path)
    duration = round(audio_duration.duration)

    await call.answer()

    await bot.send_chat_action(call.message.chat.id, "upload_voice")

    # Send audio file
    await call.message.answer_audio(audio=FSInputFile(audio_file_path), title=yt.title,
                                    performer=yt.author, duration=duration,
                                    caption=bm.captions(None, None, bot_url),
                                    parse_mode="HTML")

    await asyncio.sleep(5)
    os.remove(audio_file_path)


def download_youtube_audio(audio, name):
    audio.download(output_path=OUTPUT_DIR, filename=name)


@router.message(F.text.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+'))
@router.business_message(F.text.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+'))
async def download_music(message: types.Message):
    business_id = message.business_connection_id

    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="youtube_audio")

    bot_url = f"t.me/{(await bot.get_me()).username}"
    url = message.text

    if business_id is None:
        react = types.ReactionTypeEmoji(emoji="👨‍💻")
        await message.react([react])
    try:
        time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{time}_youtube_audio"

        yt = YouTube(url, use_oauth=True, allow_oauth_cache=True, on_progress_callback=on_progress,
                     oauth_verifier=custom_oauth_verifier)
        audio = yt.streams.filter(only_audio=True, file_extension='mp4').first()

        if not audio:
            await message.reply("The URL does not seem to be a valid YouTube music link.")
            return

        file_size = audio.filesize_kb

        audio_file_path = os.path.join(OUTPUT_DIR, name + '.m4a')

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, download_youtube_video, audio, name)

        if file_size > MAX_FILE_SIZE:
            os.remove(audio_file_path)
            await message.reply("The audio file is too large.")
            return

        audio_duration = AudioFileClip(audio_file_path)
        duration = round(audio_duration.duration)

        if business_id is None:
            await bot.send_chat_action(message.chat.id, "upload_voice")

        await message.answer_audio(audio=FSInputFile(audio_file_path), title=yt.title,
                                   performer=yt.author, duration=duration,
                                   caption=bm.captions(None, None, bot_url),
                                   parse_mode="HTML")

        await asyncio.sleep(5)
        os.remove(audio_file_path)
    except Exception as e:
        print(e)
        if business_id is None:
            react = types.ReactionTypeEmoji(emoji="👎")
            await message.react([react])
        await message.reply("Something went wrong :(\nPlease try again later.")

    await update_info(message)
