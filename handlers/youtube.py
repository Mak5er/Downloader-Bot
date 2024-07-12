import asyncio
import datetime
import os

from aiogram import types, Router, F
from aiogram.types import FSInputFile
from moviepy.editor import VideoFileClip
from pytubefix import YouTube

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR
from handlers.user import update_info
from main import bot, db, send_analytics

MAX_FILE_SIZE = 1 * 1024 * 1024

router = Router()


def download_youtube_video(video, name):
    video.download(output_path=OUTPUT_DIR, filename=name)


# Download video
@router.message(F.text.regexp(r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+'))
async def download_video(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")

    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="youtube_video")

    bot_url = f"t.me/{(await bot.get_me()).username}"
    file_type = "video"

    url = message.text
    try:
        react = types.ReactionTypeEmoji(emoji="👨‍💻")
        await message.react([react])
        time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{time}_youtube_video.mp4"

        yt = YouTube(url)
        video = yt.streams.filter(res="720p", file_extension='mp4', progressive=True).first()

        if not video:
            video = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
            if not video:
                await message.reply("The URL does not seem to be a valid YouTube video link.")
                return

        post_caption = yt.title

        user_captions = await db.get_user_captions(message.from_user.id)

        db_file_id = await db.get_file_id(yt.watch_url)

        if db_file_id:
            await bot.send_video(chat_id=message.chat.id, video=db_file_id[0][0],
                                 caption=bm.captions(user_captions, post_caption, bot_url),
                                 reply_markup=kb.return_audio_download_keyboard(yt.watch_url),
                                 parse_mode="HTMl")
            return

        size = video.filesize_kb

        if size < MAX_FILE_SIZE:

            video_file_path = os.path.join(OUTPUT_DIR, name)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, download_youtube_video, video, name)

            # Check file size using moviepy

            video_clip = VideoFileClip(video_file_path)

            width, height = video_clip.size

            # Send video file
            sent_message = await bot.send_video(chat_id=message.chat.id, video=FSInputFile(video_file_path),
                                                width=width,
                                                height=height,
                                                caption=bm.captions(user_captions, post_caption, bot_url),
                                                reply_markup=kb.return_audio_download_keyboard(yt.watch_url),
                                                parse_mode="HTMl")

            file_id = sent_message.video.file_id

            await db.add_file(yt.watch_url, file_id, file_type)

            os.remove(video_file_path)



        else:
            react = types.ReactionTypeEmoji(emoji="👎")
            await message.react([react])
            await message.reply("The video is too large.")

    except Exception as e:
        print(e)
        react = types.ReactionTypeEmoji(emoji="👎")
        await message.react([react])
        await message.reply(f"An error occurred during the download: {e}")

    await update_info(message)


@router.callback_query(F.data.startswith('audio_'))
async def download_audio(call: types.CallbackQuery):
    await bot.send_chat_action(call.message.chat.id, "typing")
    bot_url = f"t.me/{(await bot.get_me()).username}"

    url = call.data.split('_')[1]
    print(url)

    time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{time}_youtube_audio.mp3"

    yt = YouTube(url)
    audio = yt.streams.filter(only_audio=True, file_extension='mp4').first()

    if not audio:
        await call.message.reply("The URL does not seem to be a valid YouTube music link.")
        return

    file_size = audio.filesize_kb

    bitrate_kbps = ''.join(filter(str.isdigit, audio.abr))

    duration_seconds = round((file_size * 8) / int(bitrate_kbps))

    audio_file_path = os.path.join(OUTPUT_DIR, name)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, download_youtube_video, audio, name)

    # Check file size
    if file_size > MAX_FILE_SIZE:
        os.remove(audio_file_path)
        await call.message.reply("The audio file is too large.")
        return

    # Send audio file
    await bot.send_audio(chat_id=call.message.chat.id, audio=FSInputFile(audio_file_path), title=yt.title,
                         performer=yt.author, duration=duration_seconds,
                         caption=bm.captions(None, None, bot_url),
                         parse_mode="HTML")

    os.remove(audio_file_path)


def download_youtube_audio(audio, name):
    audio.download(output_path=OUTPUT_DIR, filename=name)


@router.message(F.text.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+'))
async def download_music(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")

    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="youtube_audio")

    bot_url = f"t.me/{(await bot.get_me()).username}"

    url = message.text
    try:
        react = types.ReactionTypeEmoji(emoji="👨‍💻")
        await message.react([react])
        time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{time}_youtube_audio.mp3"

        yt = YouTube(url)
        audio = yt.streams.filter(only_audio=True, file_extension='mp4').first()

        if not audio:
            await message.reply("The URL does not seem to be a valid YouTube music link.")
            return

        file_size = audio.filesize_kb

        bitrate_kbps = ''.join(filter(str.isdigit, audio.abr))

        duration_seconds = round((file_size * 8) / int(bitrate_kbps))

        audio_file_path = os.path.join(OUTPUT_DIR, name)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, download_youtube_video, audio, name)

        # Check file size
        if file_size > MAX_FILE_SIZE:
            os.remove(audio_file_path)
            await message.reply("The audio file is too large.")
            return

        # Send audio file
        await bot.send_audio(chat_id=message.chat.id, audio=FSInputFile(audio_file_path), title=yt.title,
                             performer=yt.author, duration=duration_seconds,
                             caption=bm.captions(None, None, bot_url),
                             parse_mode="HTML")

        os.remove(audio_file_path)
    except Exception as e:
        print(e)
        react = types.ReactionTypeEmoji(emoji="👎")
        await message.react([react])
        await message.reply(f"An error occurred during the download: {e}")

    await update_info(message)
