import datetime
import os
import time

import requests
from aiogram import types, Router, F
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder
from bs4 import BeautifulSoup
from moviepy.editor import VideoFileClip

import messages as bm
from config import OUTPUT_DIR
from handlers.user import update_info
from helper import expand_tiktok_url
from main import bot, db, send_analytics

MAX_FILE_SIZE = 500 * 1024 * 1024

router = Router()


class DownloaderTikTok:
    def __init__(self, output_dir, filename):
        self.output_dir = output_dir
        self.filename = filename

    def tikwm(self, video_id):
        try:
            download_url = f"https://tikwm.com/video/media/play/{video_id}.mp4"
            response = requests.get(download_url, allow_redirects=True)
            if response.status_code == 200:
                with open(self.filename, 'wb') as f:
                    f.write(response.content)
                return True
            return False
        except Exception as e:
            print(f"Error: {e}")
            return False

    def download_photos(self, photo_id):
        try:
            url = f"https://tikwm.com/video/{photo_id}.html"
            response = requests.get(url, allow_redirects=True)
            time.sleep(1)
            soup = BeautifulSoup(response.content, 'html.parser')
            photo_links = []
            for div in soup.find_all("div", class_=["col-lg-2", "col-md-3", "col-sm-4", "col-xs-4"]):
                a_tag = div.find("a")
                if a_tag and 'href' in a_tag.attrs:
                    photo_links.append(a_tag['href'])

            download_dir = os.path.join(self.output_dir, photo_id)
            os.makedirs(download_dir, exist_ok=True)

            for idx, photo_url in enumerate(photo_links):
                try:
                    photo_response = requests.get(photo_url)
                    if photo_response.status_code == 200:
                        photo_path = os.path.join(download_dir, f"{idx}.jpg")
                        with open(photo_path, 'wb') as f:
                            f.write(photo_response.content)
                except:
                    pass
            return True
        except Exception as e:
            print(f"Error: {e}")
            return False


@router.message(F.text.regexp(r"(https?://(www\.)?tiktok\.com/[^\s]+|https?://vm\.tiktok\.com/[^\s]+)"))
async def process_url_tiktok(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")
    bot_url = f"t.me/{(await bot.get_me()).username}"

    url = message.text

    full_url = expand_tiktok_url(url)

    react = types.ReactionTypeEmoji(emoji="👨‍💻")
    await message.react([react])

    if "video" in full_url:

        await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_video")

        file_type = "video"
        time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        video_id = full_url.split('/')[-1].split('?')[0]
        name = f"{time}_tiktok_video.mp4"

        db_file_id = await db.get_file_id(full_url)

        if db_file_id:
            await bot.send_video(chat_id=message.chat.id, video=db_file_id[0][0],
                                 caption=bm.captions(None, None, bot_url), parse_mode="HTMl")
            return

        video_file_path = os.path.join(OUTPUT_DIR, name)
        downloader = DownloaderTikTok(OUTPUT_DIR, video_file_path)

        if downloader.tikwm(video_id):
            video = FSInputFile(video_file_path)
            file_size = os.path.getsize(video_file_path)

            video_clip = VideoFileClip(video_file_path)
            width, height = video_clip.size

            if file_size < MAX_FILE_SIZE:
                sent_message = await message.reply_video(
                    video=video,
                    width=width,
                    height=height,
                    caption=bm.captions(None, None, bot_url),
                    parse_mode="HTML"
                )

                file_id = sent_message.video.file_id

                await db.add_file(full_url, file_id, file_type)

            else:
                react = types.ReactionTypeEmoji(emoji="👎")
                await message.react([react])
                await message.reply("The video is too large.")

            os.remove(video_file_path)
        else:
            react = types.ReactionTypeEmoji(emoji="👎")
            await message.react([react])
            await message.reply("Failed to download the video. Please try again.")


    elif "photo" in full_url:
        await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_photos")

        photo_id = full_url.split('/')[-1].split('?')[0]
        downloader = DownloaderTikTok(OUTPUT_DIR, "")
        download_dir = os.path.join("downloads", photo_id)

        if downloader.download_photos(photo_id):
            all_files = []
            for root, dirs, files in os.walk(download_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    if file.endswith(('.jpg', '.jpeg', '.png')):
                        all_files.append(file_path)

            all_files.sort(key=lambda x: int(os.path.basename(x).split('.')[0]))

            while all_files:
                media_group = MediaGroupBuilder(caption=bm.captions(None, None, bot_url))
                for _ in range(min(10, len(all_files))):
                    file_path = all_files.pop(0)
                    media_group.add_photo(media=FSInputFile(file_path), parse_mode="HTML")

                await bot.send_media_group(chat_id=message.chat.id, media=media_group.build())

            for root, dirs, files in os.walk(download_dir):
                for file in files:
                    os.remove(os.path.join(root, file))
                os.rmdir(download_dir)
        else:
            react = types.ReactionTypeEmoji(emoji="👎")
            await message.react([react])
            await message.reply("Failed to download photos. Please try again.")

    else:
        react = types.ReactionTypeEmoji(emoji="👎")
        await message.react([react])
        await message.reply("The URL does not seem to be a valid TikTok video or photo link.")

    await update_info(message)
