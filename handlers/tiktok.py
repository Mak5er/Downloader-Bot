import datetime
import os

from aiogram import types, Router, F
from aiogram.types import FSInputFile
from moviepy.editor import VideoFileClip

from services.downloader_tiktok import DownloaderTikTok
from helper import expand_tiktok_url

from main import bot
from config import OUTPUT_DIR
from handlers.user import update_info

MAX_FILE_SIZE = 50 * 1024 * 1024

router = Router()


@router.message(F.text.regexp(r"(https?://(www\.)?tiktok\.com/[^\s]+|https?://vm\.tiktok\.com/[^\s]+)"))
async def process_url_tiktok(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")
    bot_url = f"t.me/{(await bot.get_me()).username}"

    url = message.text

    full_url = expand_tiktok_url(url)

    if "video" in full_url:
        react = types.ReactionTypeEmoji(emoji="👨‍💻")
        await message.react([react])
        time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = time + "tiktok_video.mp4"
        video_file_path = os.path.join(OUTPUT_DIR, name)
        downloader = DownloaderTikTok(OUTPUT_DIR, name)
        services = [
            downloader.tiktapiocom,
            downloader.tikmatecc,
        ]
        for service in services:
            if service(full_url):
                video = FSInputFile(video_file_path)
                file_size = os.path.getsize(video_file_path)

                video_clip = VideoFileClip(video_file_path)

                width, height = video_clip.size

                if file_size < MAX_FILE_SIZE:
                    await message.reply_video(video=video, width=width,
                                              height=height,
                                              caption=f'<a href="{bot_url}">💻Powered by MaxLoad</a>', parse_mode="HTMl")

                else:
                    react = types.ReactionTypeEmoji(emoji="👎")
                    await message.react([react])
                    await message.reply("The video is too large.")

                os.remove(video_file_path)
                return
        await message.reply("Failed to download the video. Please try again.")

    elif "photo" in full_url:
        react = types.ReactionTypeEmoji(emoji="👎")
        await message.react([react])
        await message.reply("Currently, the bot does not support TikTok slideshows.")
    else:
        react = types.ReactionTypeEmoji(emoji="👎")
        await message.react([react])
        await message.reply("The URL does not seem to be a valid TikTok video or photo link.")

    await update_info(message)
