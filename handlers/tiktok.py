import asyncio
import os
import re
import time
from log.logger import logger as logging
from dataclasses import dataclass
from typing import Optional

import requests
from aiogram import types, Router, F
from aiogram.types import FSInputFile, InlineQueryResultVideo, InlineQueryResultArticle
from aiogram.utils.media_group import MediaGroupBuilder
from fake_useragent import UserAgent
from moviepy import VideoFileClip

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, CHANNEL_ID
from handlers.user import update_info
from main import bot, db, send_analytics

MAX_FILE_SIZE = 500 * 1024 * 1024

router = Router()


def process_tiktok_url(text: str) -> str:
    def expand_tiktok_url(short_url: str) -> str:
        ua = UserAgent()
        try:
            response = requests.head(short_url, allow_redirects=True, headers={'User-Agent': ua.random})
            return response.url
        except requests.RequestException as e:
            print(f"Error expanding URL: {e}")
            return short_url

    def extract_tiktok_url(input_text: str) -> str:
        match = re.search(r"(https?://(?:www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", input_text)
        return match.group(0) if match else input_text

    url = extract_tiktok_url(text)
    return expand_tiktok_url(url)


def safe_remove(file_path: str):
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logging.error(f"Error removing file {file_path}: {e}")


def get_video_id_from_url(url: str) -> str:
    return url.split('/')[-1].split('?')[0]


async def send_chat_action_if_needed(chat_id: int, action: str, business_id: Optional[int]):
    if business_id is None:
        await bot.send_chat_action(chat_id, action)


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
    def __init__(self, output_dir: str, filename: str):
        self.output_dir = output_dir
        self.filename = filename

    def download_video(self, video_id: str) -> bool:
        try:
            download_url = f"https://tikwm.com/video/media/play/{video_id}.mp4"
            response = requests.get(download_url, allow_redirects=True, timeout=10)
            response.raise_for_status()
            with open(self.filename, 'wb') as f:
                f.write(response.content)
            return True
        except Exception as e:
            logging.error(f"Error downloading video {video_id}: {e}")
            return False

    @staticmethod
    def fetch_tiktok_data(video_url: str) -> dict:
        try:
            api_url = "https://tikwm.com/api/"
            payload = {
                "url": video_url,
                "count": 12,
                "cursor": 0,
                "web": 1,
                "hd": 1
            }
            response = requests.get(api_url, params=payload, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"Error fetching TikTok data for {video_url}: {e}")
            return {"error": str(e)}

    @staticmethod
    def user_info(username: str) -> Optional[TikTokUser]:
        max_retries = 10
        retry_delay = 1.5
        try:
            ua = UserAgent()
            headers = {'User-Agent': ua.random}
            exist_url = f"https://countik.com/api/exist/{username}"

            for attempt in range(max_retries):
                try:
                    exist_response = requests.get(exist_url, headers=headers, timeout=10)
                    exist_response.raise_for_status()
                    exist_data = exist_response.json()
                    sec_user_id = exist_data.get("sec_uid")
                    if sec_user_id:
                        break
                except Exception as e:
                    logging.warning(f"Attempt {attempt + 1} failed for user {username}: {e}")
                time.sleep(retry_delay)
            else:
                logging.error("Failed to get user data after 10 attempts.")
                return None

            if not sec_user_id:
                logging.error("Failed to find sec_user_id.")
                return None

            api_url = f"https://countik.com/api/userinfo?sec_user_id={sec_user_id}"
            api_response = requests.get(api_url, headers=headers, timeout=10, allow_redirects=True)
            api_response.raise_for_status()
            data = api_response.json()

            return TikTokUser(
                nickname=exist_data.get("nickname", "No nickname found"),
                followers=data.get("followerCount", 0),
                videos=data.get("videoCount", 0),
                likes=data.get("heartCount", 0),
                profile_pic=data.get("avatarThumb", ""),
                description=data.get("signature", "")
            )
        except Exception as e:
            logging.error(f"Error fetching user info for {username}: {e}")
            return None

    @staticmethod
    def video_info(full_url: str) -> Optional[TikTokVideo]:
        try:
            tiktok_data = DownloaderTikTok.fetch_tiktok_data(full_url)
            if "error" in tiktok_data:
                logging.error(f"API request error: {tiktok_data['error']}")
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
                music_play_url=data.get("music_info", {}).get("play", "")
            )
        except Exception as e:
            logging.error(f"Error fetching video info for {full_url}: {e}")
            return None


@router.message(F.text.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)"))
@router.business_message(F.text.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)"))
async def process_tiktok(message: types.Message):
    try:
        bot_url = f"t.me/{(await bot.get_me()).username}"
        user_captions = await db.get_user_captions(message.from_user.id)
        business_id = message.business_connection_id

        full_url = process_tiktok_url(message.text)

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
            await message.reply(bm.something_went_wrong())
    except Exception as e:
        logging.error(f"Error processing URL: {e}")
        await message.reply(bm.something_went_wrong())
    finally:
        await update_info(message)


async def process_tiktok_video(message, full_url, bot_url, user_captions, business_id):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_video")
    video_id = get_video_id_from_url(full_url)
    name = f"{video_id}_tiktok_video.mp4"
    video_file_path = os.path.join(OUTPUT_DIR, name)
    downloader = DownloaderTikTok(OUTPUT_DIR, video_file_path)

    video_info = DownloaderTikTok.video_info(full_url)
    if not video_info:
        await handle_download_error(message, business_id)
        return

    db_file_id = await db.get_file_id(full_url)
    if db_file_id:
        await send_chat_action_if_needed(message.chat.id, "upload_video", business_id)
        await message.answer_video(
            video=db_file_id,
            caption=bm.captions(user_captions, video_info.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                video_info.views, video_info.likes, video_info.comments,
                video_info.shares, video_info.music_play_url, full_url
            ),
            parse_mode="HTML"
        )
        return

    if downloader.download_video(video_id):
        try:
            video = FSInputFile(video_file_path)
            file_size = os.path.getsize(video_file_path)
            with VideoFileClip(video_file_path) as video_clip:
                width, height = video_clip.size
        except Exception as e:
            logging.error(f"Error processing video file: {e}")
            await handle_download_error(message, business_id)
            safe_remove(video_file_path)
            return

        if file_size < MAX_FILE_SIZE:
            await send_chat_action_if_needed(message.chat.id, "upload_video", business_id)
            sent_message = await message.reply_video(
                video=video, width=width, height=height,
                caption=bm.captions(user_captions, video_info.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    video_info.views, video_info.likes, video_info.comments,
                    video_info.shares, video_info.music_play_url, full_url
                ),
                parse_mode="HTML"
            )
            await db.add_file(full_url, sent_message.video.file_id, "video")
        else:
            await handle_large_file(message, business_id)
        await asyncio.sleep(5)
        safe_remove(video_file_path)
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

        await send_chat_action_if_needed(message.chat.id, "upload_photo", business_id)
        if len(images) > 1:
            media_group = MediaGroupBuilder()
            for img in images[:-1]:
                media_group.add_photo(media=img, parse_mode="HTML")
            await message.answer_media_group(media=media_group.build())

        last_photo = images[-1]
        await message.answer_photo(
            photo=last_photo,
            caption=bm.captions(user_captions, images_info.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                images_info.views, images_info.likes, images_info.comments,
                images_info.shares, images_info.music_play_url, full_url
            )
        )
    except Exception as e:
        logging.error(f"Error processing TikTok photos: {e}")
        await handle_download_error(message, business_id)


async def process_tiktok_profile(message, full_url, bot_url, user_captions):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_profile")
    downloader = DownloaderTikTok(OUTPUT_DIR, "")
    username = full_url.split('@')[1].split('?')[0]
    user = downloader.user_info(username)
    if not user:
        await message.reply(bm.something_went_wrong())
        return
    display_name = user.nickname.strip() if user.nickname else username
    high_res_url = user.profile_pic.replace("q:100:100", "q:750:750")
    try:
        await message.reply_photo(
            photo=high_res_url,
            caption=bm.captions(user_captions, user.description, bot_url),
            reply_markup=kb.return_user_info_keyboard(display_name, user.followers, user.videos, user.likes, full_url)
        )
    except Exception as e:
        logging.error(f"Error sending profile photo: {e}")
        tiktok_logo_url = 'https://freepnglogo.com/images/all_img/tik-tok-logo-transparent-031f.png'
        await message.reply_photo(
            photo=tiktok_logo_url,
            caption=bm.captions(user_captions, user.description, bot_url),
            reply_markup=kb.return_user_info_keyboard(display_name, user.followers, user.videos, user.likes, full_url)
        )


async def handle_large_file(message, business_id):
    if business_id is None:
        await message.react([types.ReactionTypeEmoji(emoji="👎")])
    await message.reply(bm.video_too_large())


async def handle_download_error(message, business_id):
    if business_id is None:
        await message.react([types.ReactionTypeEmoji(emoji="👎")])
    await message.reply(bm.something_went_wrong())


@router.inline_query(F.query.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)"))
async def inline_tiktok_query(query: types.InlineQuery):
    try:
        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_tiktok_video")
        user_captions = await db.get_user_captions(query.from_user.id)
        bot_url = f"t.me/{(await bot.get_me()).username}"

        url_match = re.search(r"(https?://(?:www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", query.query)
        if not url_match:
            return await query.answer([], cache_time=1, is_personal=True)
        full_url = process_tiktok_url(query.query)
        video_id = get_video_id_from_url(full_url)
        results = []

        if "video" in full_url:
            name = f"{video_id}_tiktok_video.mp4"
            video_file_path = os.path.join(OUTPUT_DIR, name)
            downloader = DownloaderTikTok(OUTPUT_DIR, video_file_path)
            video_info = DownloaderTikTok.video_info(full_url)
            if video_info:
                db_file_id = await db.get_file_id(full_url)
                if db_file_id:
                    video_file_id = db_file_id
                else:
                    if downloader.download_video(video_id):
                        video = FSInputFile(video_file_path)
                        sent_message = await bot.send_video(
                            chat_id=CHANNEL_ID,
                            video=video,
                            caption=f"🎥 TikTok Video from {query.from_user.full_name}"
                        )
                        video_file_id = sent_message.video.file_id
                        await db.add_file(full_url, video_file_id, "video")
                    else:
                        await query.answer([], cache_time=1, is_personal=True)
                        return

                results.append(
                    InlineQueryResultVideo(
                        id=f"video_{video_id}",
                        video_url=video_file_id,
                        thumbnail_url="https://freepnglogo.com/images/all_img/tik-tok-logo-transparent-031f.png",
                        description=video_info.description,
                        title="🎥 TikTok Video",
                        mime_type="video/mp4",
                        caption=bm.captions(user_captions, video_info.description, bot_url),
                        reply_markup=kb.return_video_info_keyboard(
                            video_info.views, video_info.likes, video_info.comments,
                            video_info.shares, video_info.music_play_url, full_url
                        )
                    )
                )
                await query.answer(results, cache_time=10)
                await asyncio.sleep(5)
                safe_remove(video_file_path)
                return

        elif "photo" in full_url:
            results.append(
                InlineQueryResultArticle(
                    id="unsupported_tiktok_photos",
                    title="📷 TikTok Photos",
                    description="⚠️ TikTok photos are not supported in inline mode.",
                    input_message_content=types.InputTextMessageContent(
                        message_text="⚠️ TikTok photos are not supported in inline mode."
                    )
                )
            )
            await query.answer(results, cache_time=10)
            return

        await query.answer([], cache_time=1, is_personal=True)
    except Exception as e:
        logging.error(f"Error processing inline query: {e}")
        await query.answer([], cache_time=1, is_personal=True)


@router.callback_query(lambda call: any(call.data.startswith(prefix) for prefix in
                                        ["followers_", "videos_", "likes_", "views_", "comments_", "shares_"]))
async def handle_stats_callback(call: types.CallbackQuery):
    try:
        prefix, value = call.data.split("_", 1)
        mapping = {
            "followers": ("Followers", "👥"),
            "videos": ("Videos", "🎥"),
            "likes": ("Likes", "❤️"),
            "views": ("Views", "👁️"),
            "comments": ("Comments", "💬"),
            "shares": ("Shares", "🔄")
        }
        if prefix in mapping:
            label, emoji = mapping[prefix]
            await call.answer(f"{label}: {value} {emoji}")
        else:
            await call.answer("Unknown data")
    except Exception as e:
        logging.error(f"Error handling callback query: {e}")
        await call.answer("Error processing callback")
