import asyncio
import datetime
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse

import aiohttp
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
from handlers.utils import (
    get_bot_url,
    handle_download_error,
    handle_video_too_large,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    send_chat_action_if_needed,
)
from log.logger import logger as logging
from main import bot, db, send_analytics
from services.http_client import get_http_session

MAX_FILE_SIZE = 500 * 1024 * 1024
TIKTOK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
TIKTOK_API_TIMEOUT = aiohttp.ClientTimeout(total=10)

_user_agent_provider: Optional[UserAgent] = None


def _get_user_agent() -> str:
    global _user_agent_provider
    if _user_agent_provider is None:
        try:
            _user_agent_provider = UserAgent()
        except Exception as e:
            logging.debug("Failed to initialise UserAgent provider: %s", e)
            _user_agent_provider = None

    if _user_agent_provider:
        try:
            return _user_agent_provider.random
        except Exception as e:
            logging.debug("Falling back to static User-Agent: %s", e)
            _user_agent_provider = None

    return TIKTOK_USER_AGENT

router = Router()


def process_tiktok_url(text: str) -> str:
    def strip_tracking(url: str) -> str:
        try:
            parsed = urlparse(url)
        except Exception:
            return url
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))

    def expand_tiktok_url(short_url: str) -> str:
        headers = {'User-Agent': _get_user_agent()}
        try:
            response = requests.head(short_url, allow_redirects=True, headers=headers)
            logging.debug("TikTok short URL expanded: raw=%s expanded=%s", short_url, response.url)
            return strip_tracking(response.url or short_url)
        except requests.RequestException as e:
            logging.error("Error expanding TikTok URL: url=%s error=%s", short_url, e)
            return strip_tracking(short_url)

    def extract_tiktok_url(input_text: str) -> str:
        match = re.search(r"(https?://(?:www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", input_text)
        return match.group(0) if match else input_text

    url = extract_tiktok_url(text)
    logging.debug("TikTok URL extracted: raw=%s extracted=%s", text, url)
    return strip_tracking(expand_tiktok_url(url))


def get_video_id_from_url(url: str) -> str:
    return url.split('/')[-1].split('?')[0]


async def get_user_settings(user_id):
    return await db.user_settings(user_id)


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

        user_agent = _get_user_agent()
        params = {"url": video_url, "count": 12, "cursor": 0, "web": 1, "hd": 1}
        logging.debug("Fetching TikTok data: url=%s params=%s", video_url, params)
        session = await get_http_session()
        try:
            async with session.get(
                    "https://tikwm.com/api/",
                    params=params,
                    timeout=TIKTOK_API_TIMEOUT,
                    headers={"User-Agent": user_agent},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, aiohttp.ContentTypeError, asyncio.TimeoutError) as exc:
            logging.error("TikTok API request failed: url=%s error=%s", video_url, exc)
            raise

        _last_call_time = time.monotonic()
        logging.debug(
            "Fetched TikTok data: url=%s has_error=%s keys=%s",
            video_url,
            data.get("error"),
            list(data.keys()),
        )
        return data


async def video_info(data: dict) -> Optional[TikTokVideo]:
    if data.get("error"):
        logging.error("TikTok API error response: %s", data.get("error"))
        return None

    elif data.get("code") != 0:
        logging.error(
            "TikTok API returned non-zero code: code=%s message=%s",
            data.get("code"),
            data.get("message"),
        )
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
    STREAM_CHUNK_SIZE = 512 * 1024
    MULTIPART_MIN_SIZE = 8 * 1024 * 1024
    MAX_WORKERS = 4

    def __init__(self, output_dir: str, filename: str):
        self.output_dir = output_dir
        self.filename = filename

    async def download_video(self, video_id: str) -> bool:
        return await asyncio.to_thread(self._download_video_sync, video_id)

    def _download_video_sync(self, video_id: str) -> bool:
        if not self.filename:
            logging.error("TikTok download invoked without target filename: video_id=%s", video_id)
            return False

        try:
            download_url = f"https://tikwm.com/video/media/play/{video_id}.mp4"
            os.makedirs(os.path.dirname(self.filename) or self.output_dir, exist_ok=True)

            total_size, supports_range = self._probe_video(download_url)
            if supports_range and total_size >= self.MULTIPART_MIN_SIZE:
                logging.debug(
                    "TikTok multipart download triggered: video_id=%s size=%s",
                    video_id,
                    total_size,
                )
                return self._download_multipart(download_url, total_size)

            return self._download_single(download_url)
        except Exception as e:
            logging.error("Error downloading TikTok video: video_id=%s error=%s", video_id, e)
            return False

    @staticmethod
    def _base_headers() -> dict[str, str]:
        return {
            "User-Agent": _get_user_agent(),
            "Referer": "https://www.tiktok.com/",
        }

    def _probe_video(self, url: str) -> tuple[int, bool]:
        headers = self._base_headers()
        try:
            response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
            response.raise_for_status()
            total_size = int(response.headers.get("Content-Length", "0") or 0)
            supports_range = response.headers.get("Accept-Ranges", "").lower() == "bytes"
            return total_size, supports_range
        except Exception as e:
            logging.debug("TikTok HEAD probe failed: url=%s error=%s", url, e)
            return 0, False

    def _download_single(self, url: str) -> bool:
        headers = self._base_headers()
        try:
            with requests.get(
                url,
                headers=headers,
                stream=True,
                allow_redirects=True,
                timeout=(5, 60),
            ) as response:
                response.raise_for_status()
                with open(self.filename, "wb") as outfile:
                    for chunk in response.iter_content(chunk_size=self.STREAM_CHUNK_SIZE):
                        if chunk:
                            outfile.write(chunk)
            logging.info("TikTok video downloaded: path=%s", self.filename)
            return True
        except Exception as e:
            logging.error("TikTok single download failed: url=%s error=%s", url, e)
            return False

    def _download_multipart(self, url: str, total_size: int) -> bool:
        temp_path = f"{self.filename}.part"
        ranges = self._split_ranges(total_size)
        headers = self._base_headers()

        try:
            with open(temp_path, "wb") as temp_file:
                temp_file.truncate(total_size)

            def fetch_range(start: int, end: int) -> None:
                range_headers = {**headers, "Range": f"bytes={start}-{end}"}
                with requests.get(
                    url,
                    headers=range_headers,
                    stream=True,
                    allow_redirects=True,
                    timeout=(5, 60),
                ) as response:
                    response.raise_for_status()
                    with open(temp_path, "r+b") as part_file:
                        part_file.seek(start)
                        for chunk in response.iter_content(chunk_size=self.STREAM_CHUNK_SIZE):
                            if chunk:
                                part_file.write(chunk)

            with ThreadPoolExecutor(max_workers=min(self.MAX_WORKERS, len(ranges))) as executor:
                futures = [executor.submit(fetch_range, start, end) for start, end in ranges]
                for future in as_completed(futures):
                    future.result()

            os.replace(temp_path, self.filename)
            logging.info(
                "TikTok video downloaded with multipart: path=%s size=%s ranges=%s",
                self.filename,
                total_size,
                len(ranges),
            )
            return True
        except Exception as e:
            logging.error("TikTok multipart download failed: url=%s error=%s", url, e)
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    logging.debug("Failed to clean temp TikTok file: path=%s", temp_path)
            return False

    def _split_ranges(self, total_size: int) -> list[tuple[int, int]]:
        if total_size <= 0:
            return [(0, 0)]

        target_part_size = max(
            self.MULTIPART_MIN_SIZE,
            total_size // (self.MAX_WORKERS * 2) or self.MULTIPART_MIN_SIZE,
        )
        ranges: list[tuple[int, int]] = []
        start = 0
        while start < total_size:
            end = min(start + target_part_size - 1, total_size - 1)
            ranges.append((start, end))
            start = end + 1
        return ranges

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
            headers = {'User-Agent': _get_user_agent()}
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
                    logging.warning(
                        "TikTok user lookup retry failed: attempt=%s username=%s error=%s",
                        attempt + 1,
                        username,
                        e,
                    )
                time.sleep(retry_delay)
            else:
                logging.error("Failed to get TikTok user data after %s attempts: username=%s", max_retries, username)
                return None

            if not sec_user_id:
                logging.error("TikTok user lookup missing sec_user_id: username=%s", username)
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
            logging.error("Error fetching TikTok user info: username=%s error=%s", username, e)
            return None


@router.message(F.text.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)"))
@router.business_message(F.text.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)"))
async def process_tiktok(message: types.Message):
    try:
        bot_url = await get_bot_url(bot)
        business_id = message.business_connection_id

        logging.info(
            "TikTok request received: user_id=%s username=%s business_id=%s text=%s",
            message.from_user.id,
            message.from_user.username,
            business_id,
            message.text,
        )

        url_match = re.match(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", message.text)

        if url_match:
            url = url_match.group(0)
        else:
            url = message.text

        data = await fetch_tiktok_data(url)
        images = data.get("data", {}).get("images", [])

        user_settings = await get_user_settings(message.from_user.id)

        await react_to_message(message, "👾", business_id=business_id)

        logging.debug(
            "TikTok content classification: has_images=%s is_profile=%s",
            bool(images),
            "@" in message.text,
        )

        if not images:
            await process_tiktok_video(message, data, bot_url, user_settings, business_id)
        elif images:
            await process_tiktok_photos(message, data, bot_url, user_settings, business_id, images)
        elif "@" in message.text:
            await process_tiktok_profile(message, message.text, bot_url, user_settings)
        else:
            await handle_download_error(message, business_id=business_id)

    except Exception as e:
        logging.exception(
            "Error processing TikTok message: user_id=%s text=%s error=%s",
            message.from_user.id,
            message.text,
            e,
        )
        await handle_download_error(message)
    finally:
        await update_info(message)


async def process_tiktok_video(message: types.Message, data: dict, bot_url: str, user_settings: list,
                               business_id: Optional[int]):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_video")
    info = await video_info(data)
    if not info:
        logging.warning(
            "TikTok video metadata missing: user_id=%s data_keys=%s",
            message.from_user.id,
            list(data.keys()),
        )
        await handle_download_error(message, business_id=business_id)
        return

    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    name = f"{info.id}_{timestamp}_tiktok_video.mp4"
    db_video_url = f'https://tiktok.com/@{info.author}/video/{info.id}'
    path = os.path.join(OUTPUT_DIR, name)
    downloader = DownloaderTikTok(OUTPUT_DIR, path)

    db_file_id = await db.get_file_id(db_video_url)
    if db_file_id:
        logging.info(
            "Serving cached TikTok video: url=%s file_id=%s",
            db_video_url,
            db_file_id,
        )
        await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
        await message.answer_video(
            video=db_file_id,
            caption=bm.captions(user_settings["captions"], info.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                info.views, info.likes, info.comments,
                info.shares, info.music_play_url, db_video_url, user_settings
            ),
            parse_mode="HTML"
        )
        await maybe_delete_user_message(message, user_settings["delete_message"])
        return

    if await downloader.download_video(info.id):
        try:
            file_size = await asyncio.to_thread(os.path.getsize, path)
            if file_size >= MAX_FILE_SIZE:
                logging.warning(
                    "TikTok video too large: url=%s size=%s",
                    db_video_url,
                    file_size,
                )
                await handle_large_file(message, business_id)
                return

            width, height = await downloader.get_video_size(path)
            await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
            sent = await message.reply_video(
                video=FSInputFile(path), width=width, height=height,
                caption=bm.captions(user_settings["captions"], info.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    info.views, info.likes, info.comments,
                    info.shares, info.music_play_url, db_video_url, user_settings
                ),
                parse_mode="HTML"
            )
            await maybe_delete_user_message(message, user_settings["delete_message"])
            try:
                await db.add_file(db_video_url, sent.video.file_id, "video")
                logging.info(
                    "Cached TikTok video: url=%s file_id=%s",
                    db_video_url,
                    sent.video.file_id,
                )
            except Exception as e:
                logging.error("Error caching TikTok video: url=%s error=%s", db_video_url, e)

        except Exception as e:
            logging.exception(
                "Error processing TikTok video: url=%s error=%s",
                db_video_url,
                e,
            )
            await handle_download_error(message, business_id=business_id)
        finally:
            await remove_file(path)
            logging.debug("Removed temporary TikTok video file: path=%s", path)
    else:
        await handle_download_error(message, business_id=business_id)


async def process_tiktok_photos(message: types.Message, data: dict, bot_url: str, user_settings: list,
                                business_id: Optional[int], images: list):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_photos")
    info = await video_info(data)
    video_url = f'https://tiktok.com/@{info.author}/video/{info.id}'
    if not images:
        logging.warning(
            "TikTok photo post missing images: user_id=%s url=%s",
            message.from_user.id,
            video_url,
        )
        await handle_download_error(message, business_id=business_id)
        return
    logging.info(
        "Sending TikTok photo set: user_id=%s url=%s image_count=%s",
        message.from_user.id,
        video_url,
        len(images),
    )
    await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)

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
        caption=bm.captions(user_settings['captions'], info.description, bot_url),
        reply_markup=kb.return_video_info_keyboard(
            info.views, info.likes, info.comments,
            info.shares, info.music_play_url, video_url, user_settings
        )
    )
    await maybe_delete_user_message(message, user_settings["delete_message"])


async def process_tiktok_profile(message: types.Message, full_url: str, bot_url: str, user_captions: list):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_profile")
    downloader = DownloaderTikTok(OUTPUT_DIR, "")
    username = full_url.split('@')[1].split('?')[0]
    logging.info(
        "Fetching TikTok profile: user_id=%s target=%s",
        message.from_user.id,
        username,
    )
    user = await asyncio.to_thread(downloader.user_info, username)
    if not user:
        logging.error("TikTok profile lookup failed: target=%s", username)
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
    logging.warning(
        "TikTok file too large for Telegram: user_id=%s chat_id=%s",
        message.from_user.id,
        message.chat.id,
    )
    await handle_video_too_large(message, business_id=business_id)


@router.inline_query(F.query.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)"))
async def inline_tiktok_query(query: types.InlineQuery):
    try:
        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_tiktok_video")
        logging.info(
            "Inline TikTok request: user_id=%s query=%s",
            query.from_user.id,
            query.query,
        )
        user_settings = await db.user_settings(query.from_user.id)
        bot_url = await get_bot_url(bot)
        match = re.search(r"(https?://(?:www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", query.query)
        if not match:
            logging.debug("Inline TikTok query pattern not matched: query=%s", query.query)
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
                logging.info(
                    "Inline TikTok video cached: url=%s file_id=%s",
                    db_video_url,
                    db_id,
                )
            if db_id:
                logging.info(
                    "Serving inline TikTok video: url=%s file_id=%s",
                    db_video_url,
                    db_id,
                )
                results.append(InlineQueryResultVideo(
                    id=f"video_{info.id}",
                    video_url=db_id,
                    thumbnail_url=info.cover,
                    description=info.description,
                    title="🎥 TikTok Video",
                    mime_type="video/mp4",
                    caption=bm.captions(user_settings['captions'], info.description, bot_url),
                    reply_markup=kb.return_video_info_keyboard(
                        info.views, info.likes, info.comments, info.shares, info.music_play_url, db_video_url,
                        user_settings
                    )
                ))
                await query.answer(results, cache_time=10, is_personal=True)
                await remove_file(path)
                logging.debug("Removed inline TikTok temp file: path=%s", path)
                return
        elif images:
            results.append(InlineQueryResultArticle(
                id="unsupported_tiktok_photos",
                title="📷 TikTok Photos",
                description="⚠️ TikTok photos not supported inline.",
                input_message_content=types.InputTextMessageContent(
                    message_text="⚠️ TikTok photos not supported inline.")
            ))
            logging.info(
                "Inline TikTok photos requested but unsupported: user_id=%s query=%s",
                query.from_user.id,
                query.query,
            )
            await query.answer(results, cache_time=10, is_personal=True)
            return
        await query.answer([], cache_time=1, is_personal=True)
    except Exception as e:
        logging.exception(
            "Error processing inline TikTok query: user_id=%s query=%s error=%s",
            query.from_user.id,
            query.query,
            e,
        )
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
        logging.exception(
            "Error handling TikTok stats callback: data=%s error=%s",
            call.data,
            e,
        )
        await call.answer("Error processing callback")
