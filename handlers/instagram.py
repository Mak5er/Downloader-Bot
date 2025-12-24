import asyncio
import os
import re
from dataclasses import dataclass
from typing import List, Optional

import requests
from aiogram import Router, F, types
from aiogram.types import FSInputFile, InlineQueryResultVideo
from aiogram.utils.media_group import MediaGroupBuilder
from moviepy import VideoFileClip

import keyboards as kb
import messages as bm
from config import (
    OUTPUT_DIR,
    INSTAGRAM_RAPID_API_KEY1,
    INSTAGRAM_RAPID_API_KEY2,
    CHANNEL_ID,
    INSTAGRAM_RAPID_API_HOST,
)
from handlers.user import update_info
from handlers.utils import (
    get_bot_url,
    get_message_text,
    handle_download_error,
    handle_video_too_large,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    send_chat_action_if_needed,
)
from log.logger import logger as logging
from main import bot, db, send_analytics
from utils.download_manager import (
    DownloadConfig,
    DownloadError,
    DownloadMetrics,
    ResilientDownloader,
    log_download_metrics,
)

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)  # 1.5 GB

router = Router()

RAPID_API_KEYS = [INSTAGRAM_RAPID_API_KEY1, INSTAGRAM_RAPID_API_KEY2]


@dataclass
class InstagramVideo:
    id: str
    code: str
    description: str
    cover: str
    views: int
    likes: int
    comments: int
    shares: int
    video_urls: List[str]
    image_urls: List[str]
    height: int
    width: int
    is_video: bool


@dataclass
class UserData:
    id: str
    nickname: str
    followers: int
    videos: int
    profile_pic: str
    description: str


class InstagramService:
    """High-level helper that encapsulates API access and media downloads."""

    def __init__(self, output_dir: str) -> None:
        config = DownloadConfig(
            chunk_size=1024 * 1024,
            multipart_threshold=10 * 1024 * 1024,
            max_workers=6,
            retry_backoff=0.75,
        )
        self._downloader = ResilientDownloader(output_dir, config=config)

    async def download_video(self, video_url: str, filename: str) -> Optional[DownloadMetrics]:
        try:
            return await self._downloader.download(video_url, filename)
        except DownloadError as exc:
            logging.error("Error downloading Instagram video: url=%s error=%s", video_url, exc)
            return None
        except Exception as exc:  # pragma: no cover - defensive
            logging.error("Unexpected Instagram download error: url=%s error=%s", video_url, exc)
            return None

    async def fetch_post_data(self, url: str) -> Optional[InstagramVideo]:
        return await asyncio.to_thread(self._fetch_post_data_sync, url)

    def _fetch_post_data_sync(self, url: str) -> Optional[InstagramVideo]:
        try:
            api_url = f"https://{INSTAGRAM_RAPID_API_HOST}/v1/post_info"
            querystring = {
                "code_or_id_or_url": url,
                "include_insights": "true",
            }

            video_data = None

            for api_key in RAPID_API_KEYS:
                headers = {
                    "x-rapidapi-key": api_key,
                    "x-rapidapi-host": INSTAGRAM_RAPID_API_HOST,
                }

                try:
                    response = requests.get(api_url, headers=headers, params=querystring, timeout=10)
                    response.raise_for_status()
                    video_data = response.json()
                    if isinstance(video_data, dict) and "data" in video_data:
                        break
                except requests.exceptions.RequestException as exc:
                    logging.error("Instagram API error with key: key=%s error=%s", api_key, exc)

            data = video_data.get("data") if isinstance(video_data, dict) else None
            if not isinstance(data, dict):
                return None

            image_urls: list[str] = []
            video_urls: list[str] = []

            if isinstance(data.get("carousel_media"), list):
                for item in data["carousel_media"]:
                    if item.get("is_video"):
                        video_url = item.get("video_url")
                        if video_url:
                            video_urls.append(video_url)
                    else:
                        image_url = item.get("thumbnail_url")
                        if image_url:
                            image_urls.append(image_url)

            if not video_urls and not image_urls and not data.get("is_video"):
                image_url = data.get("thumbnail_url")
                if image_url:
                    image_urls.append(image_url)

            if not video_urls and data.get("is_video"):
                video_url = data.get("video_url")
                if video_url:
                    video_urls.append(video_url)

            caption = data.get("caption") or {}
            metrics = data.get("metrics") or {}

            return InstagramVideo(
                id=data.get("id", ""),
                code=data.get("code", ""),
                video_urls=video_urls,
                image_urls=image_urls,
                description=caption.get("text"),
                cover=data.get("thumbnail_url", ""),
                views=metrics.get("play_count", 0),
                likes=metrics.get("like_count", 0),
                comments=metrics.get("comment_count", 0),
                shares=metrics.get("share_count", 0),
                height=data.get("original_height", 0),
                width=data.get("original_width", 0),
                is_video=data.get("is_video", False),
            )

        except Exception as exc:  # pragma: no cover - defensive
            logging.exception("Error fetching Instagram post data: url=%s error=%s", url, exc)
            return None

    async def fetch_user_data(self, url: str) -> Optional[UserData]:
        return await asyncio.to_thread(self._fetch_user_data_sync, url)

    def _fetch_user_data_sync(self, url: str) -> Optional[UserData]:
        try:
            api_url = f"https://{INSTAGRAM_RAPID_API_HOST}/v1/info"
            querystring = {
                "username_or_id_or_url": url,
            }

            user_data = None

            for api_key in RAPID_API_KEYS:
                headers = {
                    "x-rapidapi-key": api_key,
                    "x-rapidapi-host": INSTAGRAM_RAPID_API_HOST,
                }

                try:
                    response = requests.get(api_url, headers=headers, params=querystring, timeout=10)
                    response.raise_for_status()
                    user_data = response.json()
                    if user_data:
                        break
                except requests.exceptions.RequestException as exc:
                    logging.error("Instagram user API error with key: key=%s error=%s", api_key, exc)

            if not isinstance(user_data, dict):
                return None

            data = user_data.get("data", {})
            if not isinstance(data, dict) or not data:
                return None

            return UserData(
                id=data.get("id", 0),
                nickname=data.get("page_name", "No nickname found"),
                followers=data.get("follower_count", 0),
                videos=data.get("media_count", 0),
                profile_pic=data.get("hd_profile_pic_url_info", {}).get("url", ""),
                description=data.get("biography", ""),
            )

        except Exception as exc:  # pragma: no cover - defensive
            logging.exception("Error fetching Instagram user data: url=%s error=%s", url, exc)
            return None


instagram_service = InstagramService(OUTPUT_DIR)

def _read_video_dimensions(path: str) -> tuple[int, int]:
    with VideoFileClip(path) as clip:
        return clip.size


async def get_video_dimensions(path: str) -> tuple[int | None, int | None]:
    try:
        return await asyncio.to_thread(_read_video_dimensions, path)
    except Exception as exc:
        logging.debug("Failed to read Instagram video dimensions: path=%s error=%s", path, exc)
        return None, None


@router.message(
    F.text.regexp(r"(https?://(www\.)?instagram\.com/\S+)")
    | F.caption.regexp(r"(https?://(www\.)?instagram\.com/\S+)")
)
@router.business_message(
    F.text.regexp(r"(https?://(www\.)?instagram\.com/\S+)")
    | F.caption.regexp(r"(https?://(www\.)?instagram\.com/\S+)")
)
async def process_instagram_url(message: types.Message):
    try:
        bot_url = await get_bot_url(bot)
        user_settings = await db.user_settings(message.from_user.id)
        user_captions = user_settings["captions"]
        business_id = message.business_connection_id
        text = get_message_text(message)

        logging.info(
            "Instagram request received: user_id=%s username=%s business_id=%s text=%s",
            message.from_user.id,
            message.from_user.username,
            business_id,
            text,
        )

        url = extract_instagram_url(text)

        await react_to_message(message, "??'?", business_id=business_id)

        video_info = await instagram_service.fetch_post_data(url)

        if video_info is None and ("/p/" not in url and "/reel/" not in url):
            user_info = await instagram_service.fetch_user_data(url)
            if user_info:
                await process_instagram_profile(message, user_info, bot_url, user_settings, business_id, url)
                return

        logging.debug(
            "Instagram content detected: has_video=%s has_images=%s",
            bool(video_info and video_info.video_urls),
            bool(video_info and video_info.image_urls),
        )

        if not video_info or (not video_info.video_urls and not video_info.image_urls):
            await handle_download_error(message, business_id=business_id)
            return

        if video_info.image_urls:
            await process_instagram_photos(message, video_info, bot_url, user_settings, business_id)
        elif video_info.video_urls:
            await process_instagram_video(message, video_info, bot_url, user_settings, business_id)

        await update_info(message)
    except Exception as e:
        logging.exception(
            "Error processing Instagram URL: user_id=%s text=%s error=%s",
            message.from_user.id,
            get_message_text(message),
            e,
        )
        await handle_download_error(
            message,
            business_id=message.business_connection_id,
        )

def extract_instagram_url(text: str) -> str:
    match = re.match(r"(https?://(www\.)?instagram\.com/\S+)", text)
    return match.group(0) if match else text


async def process_instagram_video(message, video_info, bot_url, user_settings, business_id):
    try:
        user_captions = user_settings["captions"]
        await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="instagram_reel")
        video_urls = video_info.video_urls
        video_id = video_info.id
        name = f"{video_id}_instagram_video.mp4"
        download_path: Optional[str] = None
        post_url = f"https://www.instagram.com/reel/{video_info.code}"

        db_file_id = await db.get_file_id(post_url)
        if db_file_id:
            logging.info(
                "Serving cached Instagram video: url=%s file_id=%s",
                post_url,
                db_file_id,
            )
            await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
            await message.answer_video(
                video=db_file_id,
                caption=bm.captions(user_captions, video_info.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    video_info.views,
                    video_info.likes,
                    video_info.comments,
                    video_info.shares,
                    None,
                    post_url,
                    user_settings
                ),
                parse_mode="HTML"
            )
            await maybe_delete_user_message(message, user_settings.get("delete_message"))
            return

        metrics: Optional[DownloadMetrics] = None
        if video_urls:
            metrics = await instagram_service.download_video(video_urls[0], name)

        if not metrics:
            await handle_download_error(message, business_id=business_id)
            return

        log_download_metrics("instagram_video", metrics)
        download_path = metrics.path
        file_size = metrics.size
        width, height = await get_video_dimensions(download_path)
        video_kwargs: dict[str, int] = {}
        if width and height:
            video_kwargs["width"] = width
            video_kwargs["height"] = height

        if file_size < MAX_FILE_SIZE:
            await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)

            sent_message = await message.reply_video(
                video=FSInputFile(download_path),
                caption=bm.captions(user_captions, video_info.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    video_info.views,
                    video_info.likes,
                    video_info.comments,
                    video_info.shares,
                    None,
                    post_url,
                    user_settings
                ),
                parse_mode="HTML",
                **video_kwargs,
            )
            await db.add_file(post_url, sent_message.video.file_id, "video")
            logging.info(
                "Instagram video cached: url=%s file_id=%s",
                post_url,
                sent_message.video.file_id,
            )
            await maybe_delete_user_message(message, user_settings.get("delete_message"))
        else:
            logging.warning(
                "Instagram video too large: url=%s size=%s",
                post_url,
                file_size,
            )
            await handle_large_file(message, business_id)

        await asyncio.sleep(5)
        await remove_file(download_path)
    except Exception as e:
        logging.exception("Error in process_instagram_video: %s", e)
        await handle_download_error(message, business_id=business_id)


async def process_instagram_photos(message, video_info, bot_url, user_settings, business_id):
    try:
        user_captions = user_settings["captions"]
        await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="instagram_post")

        images = video_info.image_urls

        if not images:
            logging.warning(
                "Instagram post has no images: user_id=%s url=%s",
                message.from_user.id,
                message.text,
            )
            await handle_download_error(message, business_id=business_id)
            return

        post_url = f"https://www.instagram.com/p/{video_info.code}"
        logging.info(
            "Sending Instagram photo post: user_id=%s url=%s image_count=%s",
            message.from_user.id,
            post_url,
            len(images),
        )

        await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)

        if len(images) > 1:
            photos_for_album = images[:-1]
            for i in range(0, len(photos_for_album), 10):
                media_group = MediaGroupBuilder()
                for img in photos_for_album[i:i + 10]:
                    media_group.add_photo(media=img, parse_mode="HTML")
                await message.answer_media_group(media=media_group.build())

        last_photo = images[-1]
        await message.answer_photo(
            photo=last_photo,
            caption=bm.captions(user_captions, video_info.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                video_info.views,
                video_info.likes,
                video_info.comments,
                video_info.shares,
                None,
                post_url,
                user_settings
            )
        )
        await maybe_delete_user_message(message, user_settings.get("delete_message"))
        logging.debug(
            "Instagram photo post delivered: user_id=%s url=%s",
            message.from_user.id,
            post_url,
        )
    except Exception as e:
        logging.exception("Error in process_instagram_photos: url=%s", video_info.code if video_info else "unknown")
        await handle_download_error(message, business_id=business_id)


async def process_instagram_profile(message, user_info, bot_url, user_settings, business_id, url):
    try:
        user_captions = user_settings["captions"]
        await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="instagram_profile")

        await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)

        username = url.split('/')[3] if len(url.split('/')) > 3 else ""
        display_name = user_info.nickname.strip() if user_info.nickname else username

        logging.info(
            "Sending Instagram profile response: user_id=%s target=%s",
            message.from_user.id,
            username,
        )
        await message.reply_photo(
            photo=user_info.profile_pic,
            caption=bm.captions(user_captions, user_info.description, bot_url),
            reply_markup=kb.return_user_info_keyboard(display_name, user_info.followers, user_info.videos, None, url)
        )
        await maybe_delete_user_message(message, user_settings.get("delete_message"))
    except Exception as e:
        logging.exception("Error in process_instagram_profile: url=%s", url)
        await handle_download_error(message, business_id=business_id)


@router.inline_query(F.query.regexp(r"(https?://(www\.)?instagram\.com/\S+)"))
async def inline_instagram_query(query: types.InlineQuery):
    try:
        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type,
                             action_name="inline_instagram_video")
        logging.info(
            "Inline Instagram request: user_id=%s query=%s",
            query.from_user.id,
            query.query,
        )

        user_settings = await db.user_settings(query.from_user.id)
        user_captions = user_settings["captions"]
        bot_url = await get_bot_url(bot)

        url_match = re.match(r"(https?://(www\.)?instagram\.com/\S+)", query.query)
        if not url_match:
            logging.debug("Inline Instagram query pattern not matched: query=%s", query.query)
            return await query.answer([], cache_time=1, is_personal=True)

        url = query.query

        results = []

        if "/reel/" in url:
            video_info = await instagram_service.fetch_post_data(url)

            if not video_info:
                await query.answer([], cache_time=1, is_personal=True)
                return

            name = f"{video_info.id}_instagram_video.mp4"
            download_path: Optional[str] = None

            db_file_id = await db.get_file_id(url)
            if db_file_id:
                video_file_id = db_file_id
                logging.info(
                    "Serving cached Instagram inline video: url=%s file_id=%s",
                    url,
                    video_file_id,
                )
            else:
                if not video_info.video_urls:
                    logging.warning("Instagram inline reel has no video URLs: url=%s", url)
                    await query.answer([], cache_time=1, is_personal=True)
                    return

                metrics = await instagram_service.download_video(video_info.video_urls[0], name)
                if not metrics:
                    logging.error("Instagram inline download failed: url=%s", url)
                    await query.answer([], cache_time=1, is_personal=True)
                    return

                log_download_metrics("instagram_inline", metrics)
                download_path = metrics.path
                sent_message = await bot.send_video(
                    chat_id=CHANNEL_ID,
                    video=FSInputFile(download_path),
                    caption=f"Instagram Reel Video from {query.from_user.full_name}",
                )
                video_file_id = sent_message.video.file_id
                await db.add_file(url, video_file_id, "video")
                logging.info(
                    "Instagram inline video cached: url=%s file_id=%s",
                    url,
                    video_file_id,
                )

            results.append(
                InlineQueryResultVideo(
                    id=f"video_{video_info.id}",
                    video_url=video_file_id,
                    thumbnail_url="https://freepnglogo.com/images/all_img/1715965947instagram-logo-png%20(1).png",
                    description=video_info.description,
                    title="Instagram Reel",
                    mime_type="video/mp4",
                    caption=bm.captions(user_captions, video_info.description, bot_url),
                    reply_markup=kb.return_video_info_keyboard(
                        video_info.views,
                        video_info.likes,
                        video_info.comments,
                        video_info.shares,
                        None,
                        url,
                        user_settings
                    )
                )
            )

            await query.answer(results, cache_time=10)

            if download_path:
                await remove_file(download_path)

            return

        elif "/p/" in url:
            results.append(
                types.InlineQueryResultArticle(
                    id="unsupported_instagram_photos",
                    title="Instagram Photos",
                    description="Instagram photos are not supported in inline mode.",
                    input_message_content=types.InputTextMessageContent(
                        message_text="Instagram photos are not supported in inline mode."
                    )
                )
            )
            logging.info(
                "Inline Instagram photos requested but unsupported: user_id=%s query=%s",
                query.from_user.id,
                query.query,
            )
            await query.answer(results, cache_time=10)
            return

        await query.answer([], cache_time=1, is_personal=True)
    except Exception as e:
        logging.exception("Error processing inline Instagram query: user_id=%s query=%s", query.from_user.id,
                          query.query)
        await query.answer([], cache_time=1, is_personal=True)

async def handle_large_file(message, business_id):
    try:
        logging.warning(
            "Instagram file too large: user_id=%s chat_id=%s",
            message.from_user.id,
            message.chat.id,
        )
        await handle_video_too_large(message, business_id=business_id)
    except Exception as e:
        logging.exception("Error in Instagram handle_large_file: user_id=%s", message.from_user.id)
