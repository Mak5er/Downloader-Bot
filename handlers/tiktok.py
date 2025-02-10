import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests
from aiogram import types, Router, F
from aiogram.types import FSInputFile, InlineQueryResultVideo, InputTextMessageContent, InlineQueryResultArticle
from aiogram.utils.media_group import MediaGroupBuilder
from fake_useragent import UserAgent
from moviepy import VideoFileClip

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR
from handlers.user import update_info
from helper import expand_tiktok_url
from main import bot, db, send_analytics

MAX_FILE_SIZE = 500 * 1024 * 1024

router = Router()


@dataclass
class TikTokVideo:
    id: str
    description: str
    cover: str
    views: int
    likes: int
    comments: int
    shares: int
    music_play_url: str


@dataclass
class TikTokUser:
    nickname: str
    followers: int
    videos: int
    likes: int
    profile_pic: str
    description: str


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

    @staticmethod
    def fetch_tiktok_data(video_url):
        api_url = "https://tikwm.com/api/"
        payload = {
            "url": video_url,
            "count": 12,
            "cursor": 0,
            "web": 1,
            "hd": 1
        }

        response = requests.get(api_url, params=payload)

        if response.status_code == 200:
            return response.json()
        else:
            return {"error": f"Request failed with status code {response.status_code}"}

    @staticmethod
    def user_info(username: str) -> Optional[TikTokUser]:
        max_retries = 10
        retry_delay = 1.5

        try:
            ua = UserAgent()
            headers = {'User-Agent': ua.random}
            exist_url = f"https://countik.com/api/exist/{username}"

            for attempt in range(max_retries):
                exist_response = requests.get(exist_url, headers=headers)
                if exist_response.status_code == 200:
                    break
                print(f"Attempt {attempt + 1} failed, status code: {exist_response.status_code}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
            else:
                print("Failed to get data after 10 attempts.")
                return None

            exist_data = exist_response.json()
            sec_user_id = exist_data.get("sec_uid")

            if not sec_user_id:
                print("Failed to find sec_user_id.")
                return None

            api_url = f"https://countik.com/api/userinfo?sec_user_id={sec_user_id}"
            api_response = requests.get(api_url, headers=headers, allow_redirects=True)

            if api_response.status_code != 200:
                print(f"API request error: status code {api_response.status_code}")
                return None

            data = api_response.json()

            return TikTokUser(
                nickname=exist_data.get("nickname", "No nickname found"),
                followers=data.get("followerCount", 0),
                videos=data.get("videoCount", 0),
                likes=data.get("heartCount", 0),
                profile_pic=data.get("avatarThumb", ""),
                description=data.get("signature", ""),
            )

        except Exception as e:
            print(f"Error: {e}")
            return None

    @staticmethod
    def video_info(full_url: str) -> Optional[TikTokVideo]:
        try:
            tiktok_data = DownloaderTikTok.fetch_tiktok_data(full_url)
            if "error" in tiktok_data:
                print(f"API request error: {tiktok_data['error']}")
                return None

            data = tiktok_data.get("data", {})

            return TikTokVideo(
                id=data.get("id"),
                description=data.get("title", ""),
                cover=data.get("cover", ""),
                views=data.get("play_count", 0),
                likes=data.get("digg_count", 0),
                comments=data.get("comment_count", 0),
                shares=data.get("share_count", 0),
                music_play_url=data.get("music_info", {}).get("play", ""),
            )
        except Exception as e:
            print(f"Error: {e}")
            return None


@router.message(F.text.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)"))
@router.business_message(F.text.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)"))
async def process_url_tiktok(message: types.Message):
    bot_url = f"t.me/{(await bot.get_me()).username}"
    user_captions = await db.get_user_captions(message.from_user.id)
    business_id = message.business_connection_id

    url = extract_tiktok_url(message.text)
    full_url = expand_tiktok_url(url)

    if business_id is None:
        await message.react([types.ReactionTypeEmoji(emoji="👨‍💻")])

    if "video" in full_url:
        await process_tiktok_video(message, full_url, bot_url, user_captions, business_id)
    elif "photo" in full_url:
        await process_tiktok_photos(message, full_url, bot_url, user_captions, business_id)
    elif "@" in full_url:
        await process_tiktok_profile(message, full_url, bot_url, user_captions)
    else:
        if business_id is None:
            await message.react([types.ReactionTypeEmoji(emoji="👎")])
        await message.reply("Something went wrong :(\nPlease try again later.")

    await update_info(message)


def extract_tiktok_url(text: str) -> str:
    match = re.match(r"(https?://(www\\.|vm\\.|vt\\.|vn\\.)?tiktok\\.com/\\S+)", text)
    return match.group(0) if match else text


async def process_tiktok_video(message, full_url, bot_url, user_captions, business_id):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_video")
    video_id = full_url.split('/')[-1].split('?')[0]
    name = f"{video_id}_tiktok_video.mp4"
    video_file_path = os.path.join(OUTPUT_DIR, name)
    downloader = DownloaderTikTok(OUTPUT_DIR, video_file_path)

    video_info = DownloaderTikTok.video_info(full_url)

    db_file_id = await db.get_file_id(full_url)
    if db_file_id:
        if business_id is None:
            await bot.send_chat_action(message.chat.id, "upload_video")
        await message.answer_video(video=db_file_id[0][0],
                                   caption=bm.captions(user_captions, video_info.description, bot_url),
                                   reply_markup=kb.return_video_info_keyboard(video_info.views, video_info.likes,
                                                                              video_info.comments, video_info.shares,
                                                                              video_info.music_play_url, full_url),
                                   parse_mode="HTML")
        return

    if downloader.download_video(video_id):
        video = FSInputFile(video_file_path)
        file_size = os.path.getsize(video_file_path)
        video_clip = VideoFileClip(video_file_path)
        width, height = video_clip.size

        if file_size < MAX_FILE_SIZE:
            if business_id is None:
                await bot.send_chat_action(message.chat.id, "upload_video")
            sent_message = await message.reply_video(
                video=video, width=width, height=height,
                caption=bm.captions(user_captions, video_info.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(video_info.views, video_info.likes, video_info.comments,
                                                           video_info.shares, video_info.music_play_url, full_url),
                parse_mode="HTML"
            )
            await db.add_file(full_url, sent_message.video.file_id, "video")
        else:
            await handle_large_file(message, business_id)

        await asyncio.sleep(5)
        os.remove(video_file_path)
    else:
        await handle_download_error(message, business_id)


async def process_tiktok_photos(message, full_url, bot_url, user_captions, business_id):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_photos")

    tiktok_data = DownloaderTikTok.fetch_tiktok_data(full_url)
    await asyncio.sleep(2)
    images_info = DownloaderTikTok.video_info(full_url)
    try:

        if "error" in tiktok_data:
            await handle_download_error(message, business_id)
            return

        images = tiktok_data.get("data", {}).get("images", [])

        if not images:
            await handle_download_error(message, business_id)
            return

        if business_id is None:
            await bot.send_chat_action(message.chat.id, "upload_photo")

        if len(images) > 1:
            media_group = MediaGroupBuilder()
            for img in images[:-1]:
                media_group.add_photo(media=img, parse_mode="HTML")
            await message.answer_media_group(media=media_group.build())

        last_photo = images[-1]

        await message.answer_photo(
            photo=last_photo,
            caption=bm.captions(user_captions, images_info.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(images_info.views, images_info.likes,
                                                       images_info.comments, images_info.shares,
                                                       images_info.music_play_url, full_url)
        )

    except Exception as e:
        print(e)
        await handle_download_error(message, business_id)


async def process_tiktok_profile(message, full_url, bot_url, user_captions):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_profile")

    downloader = DownloaderTikTok(OUTPUT_DIR, "")
    username = full_url.split('@')[1].split('?')[0]
    user = downloader.user_info(username)
    display_name = user.nickname.strip() if user.nickname else username
    high_res_url = user.profile_pic.replace("q:100:100", "q:750:750")

    try:
        await message.reply_photo(
            photo=high_res_url,
            caption=bm.captions(user_captions, user.description, bot_url),
            reply_markup=kb.return_user_info_keyboard(display_name, user.followers, user.videos, user.likes, full_url)
        )
    except:
        tiktok_logo_url = 'https://freepnglogo.com/images/all_img/tik-tok-logo-transparent-031f.png'
        await message.reply_photo(
            photo=tiktok_logo_url,
            caption=bm.captions(user_captions, user.description, bot_url),
            reply_markup=kb.return_user_info_keyboard(display_name, user.followers, user.videos, user.likes, full_url)
        )


async def handle_large_file(message, business_id):
    if business_id is None:
        await message.react([types.ReactionTypeEmoji(emoji="👎")])
    await message.reply("The video is too large.")


async def handle_download_error(message, business_id):
    if business_id is None:
        await message.react([types.ReactionTypeEmoji(emoji="👎")])
    await message.reply("Something went wrong :(\nPlease try again later.")


@router.inline_query(F.query.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)"))
async def inline_tiktok_query(query: types.InlineQuery):
    await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_tiktok_video")

    user_captions = await db.get_user_captions(query.from_user.id)
    bot_url = f"t.me/{(await bot.get_me()).username}"

    url_match = re.match(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", query.query)
    if not url_match:
        return await query.answer([], cache_time=1, is_personal=True)

    url = extract_tiktok_url(query.query)
    full_url = expand_tiktok_url(url)

    video_id = full_url.split('/')[-1].split('?')[0]

    results = []

    if "video" in full_url:
        video_info = DownloaderTikTok.video_info(full_url)

        results.append(
            InlineQueryResultVideo(
                id=f"video_{video_id}",
                video_url=f"https://tikwm.com/video/media/play/{video_id}.mp4",
                thumbnail_url="https://freepnglogo.com/images/all_img/tik-tok-logo-transparent-031f.png",
                description=video_info.description,
                title="🎥 TikTok Video",
                mime_type="video/mp4",
                caption=bm.captions(user_captions, video_info.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(video_info.views, video_info.likes,
                                                           video_info.comments, video_info.shares,
                                                           video_info.music_play_url, full_url)
            )
        )

    elif "photo" in full_url:
        results.append(
            InlineQueryResultArticle(
                id="unsupported_tiktok_photos",
                title="📷 TikTok Photos",
                description='⚠️ TikTok photos are not supported in inline mode.',
                input_message_content=InputTextMessageContent(
                    message_text="⚠️ TikTok photos are not supported in inline mode."
                )

            )
        )

    if results:
        await query.answer(results, cache_time=10)
    else:
        await query.answer([], cache_time=1, is_personal=True)


@router.callback_query(F.data.startswith("followers_"))
async def handle_followers(call: types.CallbackQuery):
    followers = call.data.split('_')[1]
    await call.answer(f"Followers: {followers} 👥")


@router.callback_query(F.data.startswith("videos_"))
async def handle_videos(call: types.CallbackQuery):
    videos = call.data.split('_')[1]
    await call.answer(f"Videos: {videos} 🎥")


@router.callback_query(F.data.startswith("likes_"))
async def handle_likes(call: types.CallbackQuery):
    likes = call.data.split('_')[1]
    await call.answer(f"Likes: {likes} ❤️")


@router.callback_query(F.data.startswith("views_"))
async def handle_likes(call: types.CallbackQuery):
    views = call.data.split('_')[1]
    await call.answer(f"Views: {views} 👁️")


@router.callback_query(F.data.startswith("comments_"))
async def handle_likes(call: types.CallbackQuery):
    comments = call.data.split('_')[1]
    await call.answer(f"Comments: {comments} 💬")


@router.callback_query(F.data.startswith("shares_"))
async def handle_likes(call: types.CallbackQuery):
    shares = call.data.split('_')[1]
    await call.answer(f"Shares: {shares} 🔄")
