import datetime
import os

from aiogram import types, Router, F
from aiogram.types import FSInputFile
from moviepy.editor import VideoFileClip

from services.downloader_tiktok import DownloaderTikTok
from helper import expand_tiktok_url, trim_video

from main import bot
from config import OUTPUT_DIR

router = Router()


@router.message(F.text.regexp(r"(https?://(www\.)?tiktok\.com/[^\s]+|https?://vm\.tiktok\.com/[^\s]+)"))
async def process_url_tiktok(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")
    url = message.text
    full_url = expand_tiktok_url(url)

    if "video" in full_url:
        react = types.ReactionTypeEmoji(emoji="👨‍💻")
        await message.react([react])
        time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = time + "tiktok_video.mp4"
        downloader = DownloaderTikTok(OUTPUT_DIR, name)
        services = [
            downloader.tiktapiocom,
            downloader.tikmatecc,
        ]
        for service in services:
            if service(full_url):
                bot_url = f"t.me/{(await bot.get_me()).username}"
                video = FSInputFile(f"{OUTPUT_DIR}/{name}")
                trim_video(f"{OUTPUT_DIR}/{name}")
                video_clip = VideoFileClip(f"{OUTPUT_DIR}/{name}")

                width, height = video_clip.size
                await message.reply_video(video=video, height=height, width=width,
                                          caption=f'<a href="{bot_url}">💻Powered by MaxLoad</a>', parse_mode="HTMl")
                os.remove(f"{OUTPUT_DIR}/{name}")
                return
        await message.reply("Failed to download the video. Please try again.")

    elif "photo" in full_url:
        react = types.ReactionTypeEmoji(emoji="👎")
        await message.react(react)
        await message.reply("Currently, the bot does not support TikTok slideshows.")
    else:
        await message.reply("The URL does not seem to be a valid TikTok video or photo link.")
