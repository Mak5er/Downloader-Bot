import asyncio
import datetime
import re
from typing import Optional
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import aiohttp
from aiogram import types, Router, F
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, COBALT_API_URL
from handlers.user import update_info
from handlers.utils import (
    get_bot_url,
    get_message_text,
    handle_download_error,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    send_chat_action_if_needed,
    resolve_settings_target_id,
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

router = Router()

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)


def strip_instagram_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


@dataclass
class InstagramMedia:
    url: str
    type: str


@dataclass
class InstagramVideo:
    id: str
    description: str
    author: str
    media_list: list[InstagramMedia]


async def get_user_settings(message: types.Message):
    return await db.user_settings(resolve_settings_target_id(message))


class InstagramService:

    def __init__(self, output_dir: str) -> None:
        config = DownloadConfig(
            chunk_size=1024 * 1024,
            multipart_threshold=16 * 1024 * 1024,
            max_workers=4,
            retry_backoff=0.8,
        )
        self._downloader = ResilientDownloader(output_dir, config=config)

    async def fetch_data(self, url: str, audio_only: bool = False) -> Optional[InstagramVideo]:
        payload = {
            "url": url,
            "videoQuality": "720",
            "downloadMode": "audio" if audio_only else "pro",
            "alwaysProxy": True
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{COBALT_API_URL}/api/json", json=payload, headers=headers, timeout=15) as resp:
                    if resp.status != 200:
                        logging.error("Cobalt error: status %s", resp.status)
                        return None

                    data = await resp.json()
                    media_list = []

                    if "url" in data:
                        m_type = "audio" if audio_only else ("video" if "mp4" in data["url"] else "photo")
                        media_list.append(InstagramMedia(url=data["url"], type=m_type))

                    elif "picker" in data:
                        for item in data["picker"]:
                            m_type = "video" if item.get("type") == "video" else "photo"
                            media_list.append(InstagramMedia(url=item["url"], type=m_type))

                    return InstagramVideo(
                        id=str(int(datetime.datetime.now().timestamp())),
                        description="",
                        author="instagram_user",
                        media_list=media_list
                    )
        except Exception as e:
            logging.error("Instagram fetch exception: %s", e)
            return None

    async def download_media(self, url: str, filename: str) -> Optional[DownloadMetrics]:
        try:
            return await self._downloader.download(url, filename)
        except DownloadError as exc:
            logging.error("Error downloading Instagram media: url=%s error=%s", url, exc)
            return None


inst_service = InstagramService(OUTPUT_DIR)

@router.message(F.text.regexp(r"(https?://(www\.)?instagram\.com/(p|reels|reel)/[^/?#&]+)"))
@router.business_message(F.text.regexp(r"(https?://(www\.)?instagram\.com/(p|reels|reel)/[^/?#&]+)"))
async def process_instagram(message: types.Message):
    try:
        bot_url = await get_bot_url(bot)
        business_id = message.business_connection_id
        text = get_message_text(message)

        url_match = re.search(r"(https?://(www\.)?instagram\.com/(p|reels|reel)/[^/?#&]+)", text)
        if not url_match:
            return
        url = strip_instagram_url(url_match.group(0))

        logging.info("Instagram request: user_id=%s url=%s", message.from_user.id, url)
        user_settings = await get_user_settings(message)
        await react_to_message(message, "👾", business_id=business_id)

        data = await inst_service.fetch_data(url)
        if not data or not data.media_list:
            await handle_download_error(message, business_id=business_id)
            return

        has_videos = any(item.type == "video" for item in data.media_list)
        has_photos = any(item.type == "photo" for item in data.media_list)

        logging.debug(
            "Instagram content classification: has_videos=%s has_photos=%s item_count=%s",
            has_videos,
            has_photos,
            len(data.media_list),
        )

        if has_videos and len(data.media_list) == 1:
            await process_instagram_video(message, data, url, bot_url, user_settings, business_id)
        elif has_photos or len(data.media_list) > 1:
            await process_instagram_media_group(message, data, url, bot_url, user_settings, business_id)
        else:
            await handle_download_error(message, business_id=business_id)

    except Exception as e:
        logging.exception(
            "Error processing Instagram message: user_id=%s text=%s error=%s",
            message.from_user.id,
            get_message_text(message),
            e,
        )
        await handle_download_error(message)
    finally:
        await update_info(message)


async def process_instagram_video(message: types.Message, data: InstagramVideo, original_url: str, bot_url: str,
                                  user_settings: dict, business_id: Optional[int]):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="instagram_video")

    if not data.media_list or data.media_list[0].type != "video":
        await handle_download_error(message, business_id=business_id)
        return

    audio_callback_data = f"audio:inst:{original_url}"
    media = data.media_list[0]

    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    download_name = f"{data.id}_{timestamp}_instagram_video.mp4"
    db_video_url = original_url

    db_file_id = await db.get_file_id(db_video_url)
    if db_file_id:
        logging.info(
            "Serving cached Instagram video: url=%s file_id=%s",
            db_video_url,
            db_file_id,
        )
        await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
        await message.answer_video(
            video=db_file_id,
            caption=bm.captions(user_settings["captions"], data.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                None, None, None, None, "", db_video_url, user_settings,
                audio_callback_data=audio_callback_data,
            ),
            parse_mode="HTML"
        )
        await maybe_delete_user_message(message, user_settings["delete_message"])
        return

    metrics = await inst_service.download_media(media.url, download_name)
    if not metrics:
        await handle_download_error(message, business_id=business_id)
        return

    log_download_metrics("instagram_video", metrics)
    download_path = metrics.path
    file_size = metrics.size

    try:
        if file_size >= MAX_FILE_SIZE:
            logging.warning(
                "Instagram video too large: url=%s size=%s",
                db_video_url,
                file_size,
            )
            await handle_download_error(message, business_id=business_id)
            return

        await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
        sent = await message.reply_video(
            video=FSInputFile(download_path),
            caption=bm.captions(user_settings["captions"], data.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                None, None, None, None, "", db_video_url, user_settings,
                audio_callback_data=audio_callback_data,
            ),
            parse_mode="HTML"
        )
        await maybe_delete_user_message(message, user_settings["delete_message"])

        try:
            await db.add_file(db_video_url, sent.video.file_id, "video")
            logging.info(
                "Cached Instagram video: url=%s file_id=%s",
                db_video_url,
                sent.video.file_id,
            )
        except Exception as e:
            logging.error("Error caching Instagram video: url=%s error=%s", db_video_url, e)

    except Exception as e:
        logging.exception(
            "Error processing Instagram video: url=%s error=%s",
            db_video_url,
            e,
        )
        await handle_download_error(message, business_id=business_id)
    finally:
        await remove_file(download_path)
        logging.debug("Removed temporary Instagram video file: path=%s", download_path)


async def process_instagram_media_group(message: types.Message, data: InstagramVideo, original_url: str, bot_url: str,
                                        user_settings: dict, business_id: Optional[int]):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="instagram_media_group")

    logging.info(
        "Sending Instagram media group: user_id=%s media_count=%s url=%s",
        message.from_user.id,
        len(data.media_list),
        original_url,
    )

    await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)

    media_items = []
    downloaded_paths = []

    for i, item in enumerate(data.media_list[:10]):
        ext = "mp4" if item.type == "video" else "jpg"
        filename = f"inst_{data.id}_{i}.{ext}"

        metrics = await inst_service.download_media(item.url, filename)
        if metrics:
            downloaded_paths.append(metrics.path)
            media_items.append({
                "path": metrics.path,
                "type": item.type,
            })
            logging.debug("Downloaded Instagram media: type=%s index=%s", item.type, i)

    if not media_items:
        await handle_download_error(message, business_id=business_id)
        return

    try:
        if len(media_items) > 1:
            media_group = MediaGroupBuilder()
            for item in media_items[:-1]:
                if item["type"] == "video":
                    media_group.add_video(media=FSInputFile(item["path"]))
                else:
                    media_group.add_photo(media=FSInputFile(item["path"]))
            await message.answer_media_group(media=media_group.build())

        last_item = media_items[-1]
        db_video_url = original_url

        if last_item["type"] == "video":
            await message.answer_video(
                video=FSInputFile(last_item["path"]),
                caption=bm.captions(user_settings["captions"], data.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    None, None, None, None, "", db_video_url, user_settings,
                    audio_callback_data=None,
                ),
                parse_mode="HTML"
            )
        else:
            await message.answer_photo(
                photo=FSInputFile(last_item["path"]),
                caption=bm.captions(user_settings["captions"], data.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    None, None, None, None, "", db_video_url, user_settings,
                    audio_callback_data=None,
                ),
                parse_mode="HTML"
            )

        await maybe_delete_user_message(message, user_settings["delete_message"])

        logging.info(
            "Successfully sent Instagram media group: user_id=%s media_count=%s",
            message.from_user.id,
            len(media_items),
        )
    finally:
        for path in downloaded_paths:
            await remove_file(path)
            logging.debug("Removed temporary Instagram media file: path=%s", path)


@router.callback_query(F.data.startswith("audio:inst:"))
async def download_instagram_audio_callback(call: types.CallbackQuery):
    if not call.message:
        await call.answer("Open the bot to download audio", show_alert=True)
        return

    await call.answer()
    original_url = call.data.replace("audio:inst:", "")
    status_message = await call.message.answer(bm.downloading_audio_status())

    try:
        bot_url = await get_bot_url(bot)
        cache_key = f"{original_url}#audio"

        db_file_id = await db.get_file_id(cache_key)
        if db_file_id:
            logging.info(
                "Serving cached Instagram audio: url=%s file_id=%s",
                original_url,
                db_file_id,
            )
            await send_chat_action_if_needed(
                bot,
                call.message.chat.id,
                "upload_audio",
                call.message.business_connection_id,
            )
            try:
                await status_message.delete()
                status_message = None
            except Exception:
                pass
            await call.message.answer_audio(
                audio=db_file_id,
                caption=bm.captions(None, "", bot_url),
                parse_mode="HTML",
            )
            return

        data = await inst_service.fetch_data(original_url, audio_only=True)
        if not data or not data.media_list:
            await status_message.edit_text("Error fetching audio.")
            logging.error("Failed to fetch Instagram audio: url=%s", original_url)
            return

        audio_item = data.media_list[0]
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        download_name = f"{data.id}_{timestamp}_instagram_audio.mp3"

        metrics = await inst_service.download_media(audio_item.url, download_name)
        if not metrics:
            await status_message.edit_text("Error downloading audio.")
            return

        if metrics.size >= MAX_FILE_SIZE:
            await status_message.edit_text("Audio is too large for Telegram.")
            await remove_file(metrics.path)
            return

        await send_chat_action_if_needed(
            bot,
            call.message.chat.id,
            "upload_audio",
            call.message.business_connection_id,
        )
        try:
            await status_message.delete()
            status_message = None
        except Exception:
            pass

        sent_message = await call.message.answer_audio(
            audio=FSInputFile(metrics.path),
            title="Instagram Audio",
            caption=bm.captions(None, "", bot_url),
            parse_mode="HTML",
        )

        try:
            await db.add_file(cache_key, sent_message.audio.file_id, "audio")
            logging.info(
                "Cached Instagram audio: url=%s file_id=%s",
                original_url,
                sent_message.audio.file_id,
            )
        except Exception as e:
            logging.error("Error caching Instagram audio: url=%s error=%s", original_url, e)

        await asyncio.sleep(5)
        await remove_file(metrics.path)
        logging.debug("Removed temporary Instagram audio file: path=%s", metrics.path)

    except Exception as e:
        logging.exception(
            "Error downloading Instagram audio: url=%s error=%s",
            original_url,
            e,
        )
        if status_message:
            await status_message.edit_text(bm.something_went_wrong())
    finally:
        if status_message:
            try:
                await status_message.delete()
            except Exception:
                pass


@router.inline_query(F.query.regexp(r"(https?://(www\.)?instagram\.com/(p|reels|reel)/[^/?#&]+)"))
async def inline_instagram_query(query: types.InlineQuery):
    try:
        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_instagram_video")
        logging.info(
            "Inline Instagram request: user_id=%s query=%s",
            query.from_user.id,
            query.query,
        )

        user_settings = await db.user_settings(query.from_user.id)
        bot_url = await get_bot_url(bot)

        url_match = re.search(r"(https?://(www\.)?instagram\.com/(p|reels|reel)/[^/?#&]+)", query.query)
        if not url_match:
            logging.debug("Inline Instagram query pattern not matched: query=%s", query.query)
            return await query.answer([], cache_time=1, is_personal=True)

        original_url = strip_instagram_url(url_match.group(0))

        data = await inst_service.fetch_data(original_url)
        if not data or not data.media_list:
            logging.warning("Inline Instagram fetch failed: url=%s", original_url)
            return await query.answer([], cache_time=1, is_personal=True)

        if len(data.media_list) == 1 and data.media_list[0].type == "video":
            media = data.media_list[0]
            db_video_url = original_url
            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            download_name = f"{data.id}_{timestamp}_instagram_video.mp4"

            db_id = await db.get_file_id(db_video_url)

            if not db_id:
                logging.info("Downloading inline Instagram video: url=%s", original_url)
                metrics = await inst_service.download_media(media.url, download_name)
                if metrics:
                    log_download_metrics("instagram_inline", metrics)
                    download_path = metrics.path

                    # Отправляємо на канал для кешування
                    from config import CHANNEL_ID
                    sent = await bot.send_video(
                        chat_id=CHANNEL_ID,
                        video=FSInputFile(download_path),
                        caption=f"📷 Instagram Video from {query.from_user.full_name}",
                    )
                    db_id = sent.video.file_id
                    await db.add_file(db_video_url, db_id, "video")
                    logging.info(
                        "Inline Instagram video cached: url=%s file_id=%s",
                        db_video_url,
                        db_id,
                    )
                    await remove_file(download_path)

            if db_id:
                logging.info(
                    "Serving inline Instagram video: url=%s file_id=%s",
                    db_video_url,
                    db_id,
                )
                audio_callback_data = f"audio:inst:{original_url}"
                results = [
                    types.InlineQueryResultVideo(
                        id=f"video_{data.id}",
                        video_url=db_id,
                        thumbnail_url="https://instagram.com/favicon.ico",
                        description=data.description or "Instagram Video",
                        title="📷 Instagram Video",
                        mime_type="video/mp4",
                        caption=bm.captions(user_settings['captions'], data.description, bot_url),
                        reply_markup=kb.return_video_info_keyboard(
                            None, None, None, None, None, db_video_url, user_settings,
                            audio_callback_data=audio_callback_data,
                        ),
                        parse_mode="HTML",
                    )
                ]
                await query.answer(results, cache_time=10, is_personal=True)
                return

        else:
            results = [
                types.InlineQueryResultArticle(
                    id="unsupported_instagram_content",
                    title="📷 Instagram Content",
                    description="⚠️ Only single videos supported inline.",
                    input_message_content=types.InputTextMessageContent(
                        message_text="⚠️ Only single Instagram videos supported inline. Use regular chat for albums/photos."
                    )
                )
            ]
            logging.info(
                "Inline Instagram non-video content requested: user_id=%s url=%s media_count=%s",
                query.from_user.id,
                original_url,
                len(data.media_list),
            )
            await query.answer(results, cache_time=10, is_personal=True)
            return

    except Exception as e:
        logging.exception(
            "Error processing inline Instagram query: user_id=%s query=%s error=%s",
            query.from_user.id,
            query.query,
            e,
        )
        await query.answer([], cache_time=1, is_personal=True)