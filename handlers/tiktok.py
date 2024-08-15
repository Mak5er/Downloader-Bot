import datetime
import os
import re
import time

import requests
from aiogram import types, Router, F
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder
from bs4 import BeautifulSoup
from moviepy.editor import VideoFileClip, AudioFileClip

import keyboards as kb
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

    def download_video(self, video_id):
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

    def download_audio(self, video_id):
        try:
            download_url = f"https://tikwm.com/video/music/{video_id}.mp3"
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
@router.business_message(F.text.regexp(r"(https?://(www\.)?tiktok\.com/[^\s]+|https?://vm\.tiktok\.com/[^\s]+)"))
async def process_url_tiktok(message: types.Message):
    business_id = message.business_connection_id

    bot_url = f"t.me/{(await bot.get_me()).username}"

    url_match = re.match(r"(https?://(www\.)?tiktok\.com/[^\s]+|https?://vm\.tiktok\.com/[^\s]+)", message.text)
    if url_match:
        url = url_match.group(0)
    else:
        url = message.text

    full_url = expand_tiktok_url(url)

    if business_id is None:
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
            if business_id is None:
                await bot.send_chat_action(message.chat.id, "upload_video")

            await message.answer_video(video=db_file_id[0][0],
                                       caption=bm.captions(None, None, bot_url),
                                       reply_markup=kb.return_audio_download_keyboard("tt",
                                                                                      video_id) if business_id is None else None,
                                       parse_mode="HTMl")
            return

        video_file_path = os.path.join(OUTPUT_DIR, name)
        downloader = DownloaderTikTok(OUTPUT_DIR, video_file_path)

        if downloader.download_video(video_id):
            video = FSInputFile(video_file_path)
            file_size = os.path.getsize(video_file_path)

            video_clip = VideoFileClip(video_file_path)
            width, height = video_clip.size

            if file_size < MAX_FILE_SIZE:
                if business_id is None:
                    await bot.send_chat_action(message.chat.id, "upload_video")

                sent_message = await message.reply_video(
                    video=video,
                    width=width,
                    height=height,
                    caption=bm.captions(None, None, bot_url),
                    reply_markup=kb.return_audio_download_keyboard("tt", video_id) if business_id is None else None,
                    parse_mode="HTML"
                )

                file_id = sent_message.video.file_id

                await db.add_file(full_url, file_id, file_type)

            else:
                if business_id is None:
                    react = types.ReactionTypeEmoji(emoji="👎")
                    await message.react([react])
                await message.reply("The video is too large.")

            os.remove(video_file_path)
        else:
            if business_id is None:
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

            if business_id is None:
                await bot.send_chat_action(message.chat.id, "upload_photo")

            while all_files:
                media_group = MediaGroupBuilder(caption=bm.captions(None, None, bot_url))
                for _ in range(min(10, len(all_files))):
                    file_path = all_files.pop(0)
                    media_group.add_photo(media=FSInputFile(file_path), parse_mode="HTML")

                await message.answer_media_group(media=media_group.build())

            for root, dirs, files in os.walk(download_dir):
                for file in files:
                    os.remove(os.path.join(root, file))
                os.rmdir(download_dir)
        else:
            if business_id is None:
                react = types.ReactionTypeEmoji(emoji="👎")
                await message.react([react])
            await message.reply("Failed to download photos. Please try again.")

    else:
        if business_id is None:
            react = types.ReactionTypeEmoji(emoji="👎")
            await message.react([react])
        await message.reply("The URL does not seem to be a valid TikTok video or photo link.")

    await update_info(message)


@router.callback_query(F.data.startswith('tt_audio_'))
async def download_audio(call: types.CallbackQuery):
    await bot.send_chat_action(call.message.chat.id, "upload_voice")
    bot_url = f"t.me/{(await bot.get_me()).username}"

    audio_id = call.data.split('_')[2]

    time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{time}_tiktok_audio.mp3"

    audio_file_path = os.path.join(OUTPUT_DIR, name)
    downloader = DownloaderTikTok(OUTPUT_DIR, audio_file_path)

    if downloader.download_video(audio_id):
        audio = AudioFileClip(audio_file_path)
        duration = round(audio.duration)
        file_size = os.path.getsize(audio_file_path)

        if file_size > MAX_FILE_SIZE:
            os.remove(audio_file_path)
            await call.message.reply("The audio file is too large.")
            os.remove(audio_file_path)
            return

        await call.answer()

        await call.message.answer_audio(audio=FSInputFile(audio_file_path),
                                        duration=duration,
                                        caption=bm.captions(None, None, bot_url),
                                        parse_mode="HTML")

    os.remove(audio_file_path)
