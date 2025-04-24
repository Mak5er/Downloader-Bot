import asyncio
import datetime
import os
import re
import time

import aiohttp

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
            logging.error(f"Error expanding URL: {e}")
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
    author: str


@dataclass
class TikTokUser:
    nickname: str
    followers: int
    videos: int
    likes: int
    profile_pic: str
    description: str


_lock = asyncio.Lock()
_last_call_time = 0.0

async def fetch_tiktok_data(video_url: str) -> dict:
    global _last_call_time

    async with _lock:
        now = time.monotonic()
        elapsed = now - _last_call_time
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)

        ua = UserAgent()
        params = {"url": video_url, "count": 12, "cursor": 0, "web": 1, "hd": 1}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://tikwm.com/api/",
                params=params,
                timeout=10,
                headers={"User-Agent": ua.random}
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        _last_call_time = time.monotonic()
        return data


async def video_info(data: dict) -> Optional[TikTokVideo]:
    if data.get("error"):
        logging.error(f"API error: {data['error']}")
        return None

    elif data.get("code") != 0:
        logging.error(f"API error: {data['message']}")
        return None

    info = data.get("data", {})
    return TikTokVideo(
        id=info.get("id"),
        description=info.get("title", ""),
        cover=info.get("cover", ""),
        views=info.get("play_count", 0),
        likes=info.get("digg_count", 0),
        comments=info.get("comment_count", 0),
        shares=info.get("share_count", 0),
        music_play_url=info.get("music_info", {}).get("play", ""),
        author=info.get("author", {}).get("unique_id", "")
    )


class DownloaderTikTok:
    def __init__(self, output_dir: str, filename: str):
        self.output_dir = output_dir
        self.filename = filename

    async def download_video(self, video_id: str) -> bool:
        return await asyncio.to_thread(self._download_video_sync, video_id)

    def _download_video_sync(self, video_id: str) -> bool:
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

    async def get_video_size(self, path: str) -> tuple[int, int]:
        return await asyncio.to_thread(self._get_size_sync, path)

    def _get_size_sync(self, path: str) -> tuple[int, int]:
        with VideoFileClip(path) as clip:
            return clip.size

    async def user_info(self, username: str) -> Optional[TikTokUser]:
        return await asyncio.to_thread(self._user_info_sync, username)

    def _user_info_sync(self, username: str) -> Optional[TikTokUser]:
        max_retries = 10
        retry_delay = 1.5
        try:
            ua = UserAgent()
            headers = {'User-Agent': ua.random}
            exist_url = f"https://countik.com/api/exist/{username}"

            sec_user_id = None
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


@router.message(F.text.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)"))
@router.business_message(F.text.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)"))
async def process_tiktok(message: types.Message):
    try:
        bot_url = f"t.me/{(await bot.get_me()).username}"
        user_captions = await db.get_user_captions(message.from_user.id)
        business_id = message.business_connection_id

        data = await fetch_tiktok_data(message.text)
        images = data.get("data", {}).get("images", [])

        if business_id is None:
            await message.react([types.ReactionTypeEmoji(emoji="👨‍💻")])

        if not images:
            await process_tiktok_video(message, data, bot_url, user_captions, business_id)
        elif images:
            await process_tiktok_photos(message, data, bot_url, user_captions, business_id, images)
        elif "@" in message.text:
            await process_tiktok_profile(message, message.text, bot_url, user_captions)
        else:
            if business_id is None:
                await message.react([types.ReactionTypeEmoji(emoji="👎")])
            await message.reply(bm.something_went_wrong())
    except Exception as e:
        logging.error(f"Error processing URL: {e}")
        await message.reply(bm.something_went_wrong())
    finally:
        await update_info(message)


async def process_tiktok_video(message: types.Message, data: dict, bot_url: str, user_captions: list,
                               business_id: Optional[int]):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_video")
    info = await video_info(data)

    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    name = f"{info.id}_{timestamp}_tiktok_video.mp4"
    db_video_url = f'https://tiktok.com/@{info.author}/video/{info.id}'
    path = os.path.join(OUTPUT_DIR, name)
    downloader = DownloaderTikTok(OUTPUT_DIR, path)

    if not info:
        await handle_download_error(message, business_id)
        return

    db_file_id = await db.get_file_id(db_video_url)
    if db_file_id:
        await send_chat_action_if_needed(message.chat.id, "upload_video", business_id)
        await message.answer_video(
            video=db_file_id,
            caption=bm.captions(user_captions, info.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                info.views, info.likes, info.comments,
                info.shares, info.music_play_url, db_video_url
            ),
            parse_mode="HTML"
        )
        return

    if await downloader.download_video(info.id):
        try:
            file_size = await asyncio.to_thread(os.path.getsize, path)
            if file_size >= MAX_FILE_SIZE:
                await handle_large_file(message, business_id)
                return

            width, height = await downloader.get_video_size(path)
            await send_chat_action_if_needed(message.chat.id, "upload_video", business_id)
            sent = await message.reply_video(
                video=FSInputFile(path), width=width, height=height,
                caption=bm.captions(user_captions, info.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    info.views, info.likes, info.comments,
                    info.shares, info.music_play_url, db_video_url
                ),
                parse_mode="HTML"
            )
            try:
                await db.add_file(db_video_url, sent.video.file_id, "video")
            except Exception as e:
                logging.error(f"Error adding file to DB: {e}")

        except Exception as e:
            logging.error(f"Error processing video: {e}")
            await handle_download_error(message, business_id)
        finally:
            await asyncio.to_thread(os.remove, path)
    else:
        await handle_download_error(message, business_id)


async def process_tiktok_photos(message: types.Message, data: dict, bot_url: str, user_captions: list,
                                business_id: Optional[int], images: list):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_photos")
    info = await video_info(data)
    video_url = f'https://tiktok.com/@{info.author}/video/{info.id}'
    if not images:
        await handle_download_error(message, business_id)
        return
    await send_chat_action_if_needed(message.chat.id, "upload_photo", business_id)

    if len(images) > 1:
        photos_for_album = images[:-1]
        for i in range(0, len(photos_for_album), 10):
            group = MediaGroupBuilder()
            for url in photos_for_album[i:i + 10]:
                group.add_photo(media=url, parse_mode="HTML")
            await message.answer_media_group(media=group.build())

    last = images[-1]
    await message.answer_photo(
        photo=last,
        caption=bm.captions(user_captions, info.description, bot_url),
        reply_markup=kb.return_video_info_keyboard(
            info.views, info.likes, info.comments,
            info.shares, info.music_play_url, video_url
        )
    )


async def process_tiktok_profile(message: types.Message, full_url: str, bot_url: str, user_captions: list):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_profile")
    downloader = DownloaderTikTok(OUTPUT_DIR, "")
    username = full_url.split('@')[1].split('?')[0]
    user = await asyncio.to_thread(downloader.user_info, username)
    if not user:
        await message.reply(bm.something_went_wrong())
        return
    display = user.nickname.strip() or username
    pic = user.profile_pic.replace("q:100:100", "q:750:750")
    try:
        await message.reply_photo(
            photo=pic,
            caption=bm.captions(user_captions, user.description, bot_url),
            reply_markup=kb.return_user_info_keyboard(display, user.followers, user.videos, user.likes, full_url)
        )
    except Exception:
        logo = 'https://freepnglogo.com/images/all_img/tik-tok-logo-transparent-031f.png'
        await message.reply_photo(
            photo=logo,
            caption=bm.captions(user_captions, user.description, bot_url),
            reply_markup=kb.return_user_info_keyboard(display, user.followers, user.videos, user.likes, full_url)
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
        match = re.search(r"(https?://(?:www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", query.query)
        if not match:
            return await query.answer([], cache_time=1, is_personal=True)

        data = await fetch_tiktok_data(query.query)
        info = await video_info(data)
        images = data.get("data", {}).get("images", [])

        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        name = f"{info.id}_{timestamp}_tiktok_video.mp4"
        path = os.path.join(OUTPUT_DIR, name)
        downloader = DownloaderTikTok(OUTPUT_DIR, path)

        results = []
        if not images:

            if not info:
                return await query.answer([], cache_time=1, is_personal=True)

            db_video_url = f'https://tiktok.com/@{info.author}/video/{info.id}'

            db_id = await db.get_file_id(db_video_url)

            if not db_id and await downloader.download_video(info.id):
                sent = await bot.send_video(chat_id=CHANNEL_ID, video=FSInputFile(path),
                                            caption=f"🎥 TikTok Video from {query.from_user.full_name}")
                db_id = sent.video.file_id
                await db.add_file(db_video_url, db_id, "video")
            if db_id:
                results.append(InlineQueryResultVideo(
                    id=f"video_{info.id}",
                    video_url=db_id,
                    thumbnail_url=info.cover,
                    description=info.description,
                    title="🎥 TikTok Video",
                    mime_type="video/mp4",
                    caption=bm.captions(user_captions, info.description, bot_url),
                    reply_markup=kb.return_video_info_keyboard(
                        info.views, info.likes, info.comments, info.shares, info.music_play_url, db_video_url
                    )
                ))
                await query.answer(results, cache_time=10, is_personal=True)
                await asyncio.to_thread(os.remove, path)
                return
        elif images:
            results.append(InlineQueryResultArticle(
                id="unsupported_tiktok_photos",
                title="📷 TikTok Photos",
                description="⚠️ TikTok photos not supported inline.",
                input_message_content=types.InputTextMessageContent(
                    message_text="⚠️ TikTok photos not supported inline.")
            ))
            await query.answer(results, cache_time=10, is_personal=True)
            return
        await query.answer([], cache_time=1, is_personal=True)
    except Exception as e:
        logging.error(f"Error inline query: {e}")
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
