import datetime
import os
from pytube import YouTube

from aiogram import types, Router, F
from aiogram.types import FSInputFile
from moviepy.editor import VideoFileClip

from config import OUTPUT_DIR
from main import bot
from handlers.user import update_info

MAX_FILE_SIZE = 50 * 1024

router = Router()


# Download video
@router.message(F.text.regexp(r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+'))
async def download_video(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")
    bot_url = f"t.me/{(await bot.get_me()).username}"

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

        size = video.filesize_kb

        if size < MAX_FILE_SIZE:

            video_file_path = os.path.join(OUTPUT_DIR, name)
            video.download(output_path=OUTPUT_DIR, filename=name)

            # Check file size using moviepy

            if yt.title is not None:
                caption = f'{yt.title}\n\n<a href="{bot_url}">💻Powered by MaxLoad</a>'
            else:
                caption = f'<a href="{bot_url}">💻Powered by MaxLoad</a>'

            video_clip = VideoFileClip(video_file_path)

            width, height = video_clip.size
            # Send video file
            await bot.send_video(chat_id=message.chat.id, video=FSInputFile(video_file_path), width=width,
                                 height=height,
                                 caption=caption, parse_mode="HTMl")
            os.remove(video_file_path)

        else:
            react = types.ReactionTypeEmoji(emoji="👎")
            await message.react([react])
            await message.reply("The video is too large.")

    except Exception as e:
        print(e)
        react = types.ReactionTypeEmoji(emoji="👎")
        await message.react([react])
        await message.reply("The URL does not seem to be a valid YouTube video link.")

    await update_info(message)


@router.message(F.text.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+'))
async def download_music(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")
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

        audio_file_path = os.path.join(OUTPUT_DIR, name)
        audio.download(output_path=OUTPUT_DIR, filename=name)

        # Check file size
        file_size = audio.filesize_kb

        if file_size > MAX_FILE_SIZE:
            os.remove(audio_file_path)
            await message.reply("The audio file is too large.")
            return

            # Send audio file
        await bot.send_audio(chat_id=message.chat.id, audio=FSInputFile(audio_file_path), title=yt.title,
                             performer=yt.author, caption=f'<a href="{bot_url}">💻Powered by MaxLoad</a>',
                             parse_mode="HTML")

        os.remove(audio_file_path)
    except Exception as e:
        print(e)
        react = types.ReactionTypeEmoji(emoji="👎")
        await message.react([react])
        await message.reply("The URL does not seem to be a valid YouTube music link.")

    await update_info(message)
