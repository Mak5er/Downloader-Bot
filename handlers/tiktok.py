import asyncio
import datetime
import re
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from aiogram import types, Router, F
from aiogram.types import FSInputFile, InlineQueryResultArticle
from fake_useragent import UserAgent
from yt_dlp import YoutubeDL

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, CHANNEL_ID
from services.media.delivery import send_cached_media_entries
from handlers.user import update_info
from handlers.utils import (
    build_inline_album_result,
    build_queue_busy_text,
    build_rate_limit_text,
    build_start_deeplink_url,
    get_bot_url,
    get_bot_avatar_thumbnail,
    get_message_text,
    handle_download_error,
    handle_video_too_large,
    load_user_settings,
    make_retry_status_notifier,
    make_status_text_progress_updater,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    safe_delete_message,
    safe_edit_text,
    safe_edit_inline_media,
    safe_edit_inline_text,
    safe_answer_inline_query,
    send_chat_action_if_needed,
    retry_async_operation,
    with_callback_logging,
    with_chosen_inline_logging,
    with_inline_query_logging,
    with_inline_send_logging,
    with_message_logging,
)
from log.logger import logger as logging, summarize_text_for_log, summarize_url_for_log
from app_context import bot, db, send_analytics
from utils.download_manager import (
    DownloadQueueBusyError,
    DownloadRateLimitError,
    log_download_metrics,
)
from utils.http_client import get_http_session
from utils.media_cache import build_media_cache_key
from services.inline.album_links import create_inline_album_request
from services.inline.service_icons import get_inline_service_icon
from services.inline.video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)
from services.platforms import tiktok_media as tiktok_platform

logging = logging.bind(service="tiktok")

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)  # 1.5 GB
router = Router()

TikTokVideo = tiktok_platform.TikTokVideo
TikTokUser = tiktok_platform.TikTokUser
strip_tiktok_tracking = tiktok_platform.strip_tiktok_tracking
get_video_id_from_url = tiktok_platform.get_video_id_from_url
build_tiktok_video_url = tiktok_platform.build_tiktok_video_url
get_tiktok_audio_callback_data = tiktok_platform.get_tiktok_audio_callback_data
get_tiktok_size_hint = tiktok_platform.get_tiktok_size_hint
is_invalid_tiktok_payload = tiktok_platform.is_invalid_tiktok_payload


class TikTokService(tiktok_platform.TikTokMediaService):
    def __init__(self, output_dir: str) -> None:
        super().__init__(
            output_dir,
            get_http_session_func=lambda: get_http_session(),
            retry_async_operation_func=lambda *args, **kwargs: retry_async_operation(*args, **kwargs),
            user_agent_factory=lambda: UserAgent(),
            youtube_dl_factory=lambda options: YoutubeDL(options),
        )


tiktok_service = TikTokService(OUTPUT_DIR)
_expanded_tiktok_url_cache = tiktok_service._expanded_tiktok_url_cache


async def process_tiktok_url_async(text: str) -> str:
    return await tiktok_service.process_tiktok_url_async(text)


async def fetch_tiktok_data(video_url: str) -> dict:
    return await tiktok_service.fetch_tiktok_data(video_url)


async def video_info(data: dict) -> Optional[TikTokVideo]:
    return await tiktok_platform.video_info(data)


async def fetch_tiktok_data_with_retry(video_url: str, *, on_retry=None) -> dict:
    return await tiktok_service.fetch_tiktok_data_with_retry(video_url, on_retry=on_retry)


@router.message(
    F.text.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", mode="search")
    | F.caption.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", mode="search")
)
@router.business_message(
    F.text.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", mode="search")
    | F.caption.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", mode="search")
)
@with_message_logging("tiktok", "message")
async def process_tiktok(message: types.Message, direct_url: Optional[str] = None):
    try:
        bot_url = await get_bot_url(bot)
        business_id = message.business_connection_id
        show_service_status = business_id is None
        text = direct_url or get_message_text(message)

        logging.info(
            "TikTok request received: user_id=%s username=%s business_id=%s text=%s",
            message.from_user.id,
            message.from_user.username,
            business_id,
            summarize_text_for_log(text),
        )

        stripped = (text or "").strip()

        # Profile lookup: allow messages like "@username" without a URL.
        if direct_url is None and re.fullmatch(r"@[\w.]{1,32}", stripped):
            await react_to_message(message, "рџ‘ѕ", business_id=business_id)
            settings = await load_user_settings(db, message)
            await process_tiktok_profile(message, stripped, bot_url, settings["captions"])
            return

        url_match = re.search(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", stripped)
        if url_match:
            url = url_match.group(0)
        else:
            url = stripped

        await react_to_message(message, "👾", business_id=business_id)

        parsed_url = urlparse(url)
        if "/live" in (parsed_url.path or "").lower():
            await message.reply(bm.tiktok_live_not_supported())
            return

        retry_notice_sent = {"value": False}

        async def _on_retry_fetch(failed_attempt: int, total_attempts: int, _error):
            if show_service_status and failed_attempt >= 2 and not retry_notice_sent["value"]:
                retry_notice_sent["value"] = True
                await message.reply(bm.retrying_again_status(failed_attempt + 1, total_attempts))

        data = await fetch_tiktok_data_with_retry(url, on_retry=_on_retry_fetch)
        images = data.get("data", {}).get("images", [])

        user_settings = await load_user_settings(db, message)

        logging.debug(
            "TikTok content classification: has_images=%s is_profile=%s",
            bool(images),
            "@" in text,
        )

        if images:
            await process_tiktok_photos(message, data, bot_url, user_settings, business_id, images)
            return

        await process_tiktok_video(message, data, bot_url, user_settings, business_id)

    except Exception as e:
        logging.exception(
            "Error processing TikTok message: user_id=%s text=%s error=%s",
            message.from_user.id,
            summarize_text_for_log(get_message_text(message)),
            e,
        )
        await handle_download_error(message)
    finally:
        await update_info(message)


async def process_tiktok_video(message: types.Message, data: dict, bot_url: str, user_settings: dict,
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

    audio_callback_data = get_tiktok_audio_callback_data(info)

    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    download_name = f"{info.id}_{timestamp}_tiktok_video.mp4"
    db_video_url = build_tiktok_video_url(info)
    show_service_status = business_id is None
    status_message: Optional[types.Message] = None
    if show_service_status:
        status_message = await message.answer(bm.downloading_video_status())

    db_file_id = await db.get_file_id(db_video_url)
    download_path: Optional[str] = None
    request_id = f"tiktok_video:{message.chat.id}:{message.message_id}:{info.id}"
    size_hint = get_tiktok_size_hint(data)

    try:
        if db_file_id:
            logging.info(
                "Serving cached TikTok video: url=%s file_id=%s",
                summarize_url_for_log(db_video_url),
                db_file_id,
            )
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
            await message.reply_video(
                video=db_file_id,
                caption=bm.captions(user_settings["captions"], info.description, bot_url),
                reply_markup=kb.return_video_info_keyboard(
                    info.views, info.likes, info.comments,
                    info.shares, info.music_play_url, db_video_url, user_settings,
                    audio_callback_data=audio_callback_data,
                ),
                parse_mode="HTML"
            )
            await maybe_delete_user_message(message, user_settings["delete_message"])
            return

        async def _edit_status(text: str) -> None:
            await safe_edit_text(status_message, text)

        on_progress = make_status_text_progress_updater("TikTok video", _edit_status)
        on_retry_download = make_retry_status_notifier(
            _edit_status,
            enabled=show_service_status,
        )

        metrics = await asyncio.wait_for(
            tiktok_service.download_video(
                db_video_url,
                download_name,
                download_data=data,
                user_id=message.from_user.id,
                request_id=request_id,
                size_hint=size_hint,
                on_progress=on_progress,
                on_retry=on_retry_download,
            ),
            timeout=420.0,
        )
        if not metrics:
            await handle_download_error(message, business_id=business_id)
            return

        log_download_metrics("tiktok_video", metrics)
        download_path = metrics.path
        file_size = metrics.size

        if file_size >= MAX_FILE_SIZE:
            logging.warning("TikTok video too large: url=%s size=%s", summarize_url_for_log(db_video_url), file_size)
            await handle_large_file(message, business_id)
            return

        await safe_edit_text(status_message, bm.uploading_status())
        await send_chat_action_if_needed(bot, message.chat.id, "upload_video", business_id)
        sent = await message.reply_video(
            video=FSInputFile(download_path),
            caption=bm.captions(user_settings["captions"], info.description, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                info.views, info.likes, info.comments,
                info.shares, info.music_play_url, db_video_url, user_settings,
                audio_callback_data=audio_callback_data,
            ),
            parse_mode="HTML"
        )
        await maybe_delete_user_message(message, user_settings["delete_message"])

        try:
            await db.add_file(db_video_url, sent.video.file_id, "video")
            logging.info("Cached TikTok video: url=%s file_id=%s", summarize_url_for_log(db_video_url), sent.video.file_id)
        except Exception as e:
            logging.error("Error caching TikTok video: url=%s error=%s", summarize_url_for_log(db_video_url), e)

    except DownloadRateLimitError as e:
        if show_service_status:
            await message.reply(build_rate_limit_text(e.retry_after))
        else:
            await handle_download_error(message, business_id=business_id)
    except DownloadQueueBusyError as e:
        if show_service_status:
            await message.reply(build_queue_busy_text(e.position))
        else:
            await handle_download_error(message, business_id=business_id)
    except asyncio.TimeoutError:
        if show_service_status:
            await safe_edit_text(status_message, bm.timeout_error())
            await handle_download_error(message, business_id=business_id, text=bm.timeout_error())
        else:
            await handle_download_error(message, business_id=business_id)
    except Exception as e:
        logging.exception("Error processing TikTok video: url=%s error=%s", summarize_url_for_log(db_video_url), e)
        await handle_download_error(message, business_id=business_id)
    finally:
        if download_path:
            await remove_file(download_path)
            logging.debug("Removed temporary TikTok video file: path=%s", download_path)
        await safe_delete_message(status_message)


async def process_tiktok_photos(message: types.Message, data: dict, bot_url: str, user_settings: list,
                                business_id: Optional[int], images: list):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_photos")
    info = await video_info(data)
    audio_callback_data = get_tiktok_audio_callback_data(info) if info else None
    video_url = build_tiktok_video_url(info) if info else ""
    if not images:
        logging.warning(
            "TikTok photo post missing images: user_id=%s url=%s",
            message.from_user.id,
            summarize_url_for_log(video_url),
        )
        await handle_download_error(message, business_id=business_id)
        return
    logging.info(
        "Sending TikTok photo set: user_id=%s url=%s image_count=%s",
        message.from_user.id,
        summarize_url_for_log(video_url),
        len(images),
    )
    status_message: Optional[types.Message] = None
    if business_id is None:
        status_message = await message.answer(bm.uploading_status())

    try:
        await send_chat_action_if_needed(bot, message.chat.id, "upload_photo", business_id)
        media_items = []
        for index, image_url in enumerate(images):
            cache_key = build_media_cache_key(video_url or image_url, item_index=index, item_kind="photo")
            cached_file_id = await db.get_file_id(cache_key)
            media_items.append(
                {
                    "index": index,
                    "kind": "photo",
                    "cache_key": cache_key,
                    "file_id": cached_file_id,
                    "url": image_url,
                    "cached": bool(cached_file_id),
                }
            )

        await send_cached_media_entries(
            message,
            media_items,
            db_service=db,
            caption=bm.captions(user_settings['captions'], info.description if info else None, bot_url),
            reply_markup=kb.return_video_info_keyboard(
                info.views if info else None,
                info.likes if info else None,
                info.comments if info else None,
                info.shares if info else None,
                info.music_play_url if info else None,
                video_url,
                user_settings,
                audio_callback_data=audio_callback_data,
            ),
        )
        await maybe_delete_user_message(message, user_settings["delete_message"])
    finally:
        await safe_delete_message(status_message)


async def process_tiktok_profile(message: types.Message, full_url: str, bot_url: str, user_captions: list):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="tiktok_profile")
    username = full_url.split('@')[1].split('?')[0]
    logging.info(
        "Fetching TikTok profile: user_id=%s target=%s",
        message.from_user.id,
        username,
    )
    user = await tiktok_service.fetch_user_info(username)
    if not user:
        logging.error("TikTok profile lookup failed: target=%s", username)
        await message.reply(bm.something_went_wrong())
        return
    display = user.nickname.strip() or username
    pic = user.profile_pic.replace("q:100:100", "q:750:750")
    profile_cache_key = build_media_cache_key(pic, variant="profile")
    try:
        cached_file_id = await db.get_file_id(profile_cache_key)
        sent_message = await message.reply_photo(
            photo=cached_file_id or pic,
            caption=bm.captions(user_captions, user.description, bot_url),
            reply_markup=kb.return_user_info_keyboard(display, user.followers, user.videos, user.likes, full_url)
        )
        if not cached_file_id and sent_message.photo:
            await db.add_file(profile_cache_key, sent_message.photo[-1].file_id, "photo")
    except Exception:
        logo = 'https://freepnglogo.com/images/all_img/tik-tok-logo-transparent-031f.png'
        fallback_cache_key = build_media_cache_key(logo, variant="profile")
        cached_logo_id = await db.get_file_id(fallback_cache_key)
        sent_message = await message.reply_photo(
            photo=cached_logo_id or logo,
            caption=bm.captions(user_captions, user.description, bot_url),
            reply_markup=kb.return_user_info_keyboard(display, user.followers, user.videos, user.likes, full_url)
        )
        if not cached_logo_id and sent_message.photo:
            await db.add_file(fallback_cache_key, sent_message.photo[-1].file_id, "photo")


async def handle_large_file(message, business_id):
    logging.warning(
        "TikTok file too large for Telegram: user_id=%s chat_id=%s",
        message.from_user.id,
        message.chat.id,
    )
    await handle_video_too_large(message, business_id=business_id)


@router.callback_query(F.data.startswith("audio:tiktok:"))
async def download_tiktok_mp3_callback(call: types.CallbackQuery):
    if not call.message:
        await call.answer("Open the bot to download MP3", show_alert=True)
        return

    await call.answer()
    business_id = call.message.business_connection_id
    show_service_status = business_id is None
    status_message: Optional[types.Message] = None
    if show_service_status:
        status_message = await call.message.answer(bm.downloading_audio_status())
    parts = call.data.split(":", 3)
    if len(parts) != 4:
        await handle_download_error(call.message)
        return

    _, _, author, video_id = parts
    video_url = f"https://www.tiktok.com/@{author}/video/{video_id}"
    logging.info(
        "Downloading TikTok MP3 via button: user_id=%s url=%s",
        call.from_user.id,
        summarize_url_for_log(video_url),
    )

    try:
        bot_url = await get_bot_url(bot)
        bot_avatar = await get_bot_avatar_thumbnail(bot)
        cache_key = f"{video_url}#audio"
        request_id = f"tiktok_audio:{call.message.chat.id}:{call.message.message_id}:{video_id}"
        db_file_id = await db.get_file_id(cache_key)
        if db_file_id:
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(
                bot,
                call.message.chat.id,
                "upload_audio",
                business_id,
            )
            await call.message.reply_audio(
                audio=db_file_id,
                caption=bm.captions(None, None, bot_url),
                thumbnail=bot_avatar,
                parse_mode="HTML",
            )
            return

        async def _on_retry_fetch(failed_attempt: int, total_attempts: int, _error):
            if show_service_status and failed_attempt >= 2:
                await safe_edit_text(
                    status_message,
                    bm.retrying_again_status(failed_attempt + 1, total_attempts),
                )

        data = await fetch_tiktok_data_with_retry(video_url, on_retry=_on_retry_fetch)
        info = await video_info(data)
        if not info or not info.music_play_url:
            await handle_download_error(call.message)
            return

        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        download_name = f"{info.id}_{timestamp}_tiktok_audio.mp3"

        async def _edit_status(text: str) -> None:
            await safe_edit_text(status_message, text)

        on_progress = make_status_text_progress_updater("TikTok audio", _edit_status)
        on_retry_download = make_retry_status_notifier(
            _edit_status,
            enabled=show_service_status,
        )

        metrics = await tiktok_service.download_audio(
            video_url,
            download_name,
            download_data=data,
            user_id=call.from_user.id,
            request_id=request_id,
            on_progress=on_progress,
            on_retry=on_retry_download,
        )
        if not metrics:
            await handle_download_error(call.message)
            return

        if metrics.size >= MAX_FILE_SIZE:
            await call.message.reply(bm.audio_too_large())
            await remove_file(metrics.path)
            return

        await send_chat_action_if_needed(
            bot,
            call.message.chat.id,
            "upload_audio",
            business_id,
        )
        await safe_edit_text(status_message, bm.uploading_status())
        sent_message = await call.message.reply_audio(
            audio=FSInputFile(metrics.path),
            title=info.description or "TikTok audio",
            caption=bm.captions(None, None, bot_url),
            thumbnail=bot_avatar,
            parse_mode="HTML",
        )
        await db.add_file(cache_key, sent_message.audio.file_id, "audio")

        await remove_file(metrics.path)
    except DownloadRateLimitError as e:
        if show_service_status:
            await call.message.reply(build_rate_limit_text(e.retry_after))
        else:
            await handle_download_error(call.message, business_id=business_id)
    except DownloadQueueBusyError as e:
        if show_service_status:
            await call.message.reply(build_queue_busy_text(e.position))
        else:
            await handle_download_error(call.message, business_id=business_id)
    finally:
        await safe_delete_message(status_message)


@router.inline_query(F.query.regexp(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", mode="search"))
@with_inline_query_logging("tiktok", "inline_query")
async def inline_tiktok_query(query: types.InlineQuery):
    try:
        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_tiktok_video")
        logging.info(
            "Inline TikTok request: user_id=%s query=%s",
            query.from_user.id,
            summarize_text_for_log(query.query),
        )
        user_settings = await db.user_settings(query.from_user.id)
        bot_url = await get_bot_url(bot)
        match = re.search(r"(https?://(?:www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", query.query)
        if not match:
            logging.debug("Inline TikTok query pattern not matched: query=%s", summarize_text_for_log(query.query))
            return await query.answer([], cache_time=1, is_personal=True)

        source_url = strip_tiktok_tracking(match.group(0))
        data = await fetch_tiktok_data_with_retry(source_url)
        info = await video_info(data)
        images = data.get("data", {}).get("images", [])

        results = []
        if not images:

            if not info:
                return await query.answer([], cache_time=1, is_personal=True)

            db_video_url = build_tiktok_video_url(info)

            db_id = await db.get_file_id(db_video_url)
            if not db_id and not CHANNEL_ID:
                logging.error("CHANNEL_ID is not configured; TikTok inline video send is disabled")
                return await query.answer([], cache_time=1, is_personal=True)

            token = create_inline_video_request("tiktok", source_url, query.from_user.id, user_settings)
            results.append(
                InlineQueryResultArticle(
                    id=f"tiktok_inline:{token}",
                    title="TikTok Video",
                    description=info.description or "Press the button to send this video inline.",
                    thumbnail_url=info.cover or get_inline_service_icon("tiktok"),
                    input_message_content=types.InputTextMessageContent(
                        message_text=bm.inline_send_video_prompt("TikTok"),
                    ),
                    reply_markup=kb.inline_send_video_keyboard(token),
                )
            )
            await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
            return

        elif images:
            first_photo = images[0] if images else None
            if first_photo and match:
                source_url = strip_tiktok_tracking(match.group(0))
                cache_key = build_media_cache_key(
                    build_tiktok_video_url(info) if info else source_url,
                    item_index=0,
                    item_kind="photo",
                )
                if len(images) == 1:
                    db_id = await db.get_file_id(cache_key)
                    if not db_id and not CHANNEL_ID:
                        logging.error("CHANNEL_ID is not configured; TikTok inline photo send is disabled")
                        return await query.answer([], cache_time=1, is_personal=True)

                    token = create_inline_video_request("tiktok", source_url, query.from_user.id, user_settings)
                    results.append(
                        InlineQueryResultArticle(
                            id=f"tiktok_inline:{token}",
                            title="TikTok Photo",
                            description=info.description if info and info.description else "Press the button to send this photo inline.",
                            thumbnail_url=first_photo,
                            input_message_content=types.InputTextMessageContent(
                                message_text="TikTok photo is being prepared...\nIf it does not start automatically, tap the button below.",
                            ),
                            reply_markup=kb.inline_send_media_keyboard(
                                "Send photo inline",
                                f"inline:tiktok:{token}",
                            ),
                        )
                    )
                    await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
                    return

                token = create_inline_album_request(query.from_user.id, "tiktok", source_url)
                deep_link = build_start_deeplink_url(bot_url, f"album_{token}")
                preview_file_id = None
                if CHANNEL_ID:
                    preview_cache_key = build_media_cache_key(
                        build_tiktok_video_url(info) if info else source_url,
                        item_index=0,
                        item_kind="photo",
                    )
                    preview_file_id = await db.get_file_id(preview_cache_key)
                    if not preview_file_id:
                        try:
                            sent = await bot.send_photo(
                                chat_id=CHANNEL_ID,
                                photo=first_photo,
                                caption="TikTok Album Preview",
                            )
                            if sent.photo:
                                preview_file_id = sent.photo[-1].file_id
                                await db.add_file(preview_cache_key, preview_file_id, "photo")
                        except Exception as exc:
                            logging.warning(
                                "Failed to cache TikTok album preview photo: url=%s error=%s",
                                summarize_url_for_log(source_url),
                                exc,
                            )
                results.append(build_inline_album_result(
                    result_id=f"tiktok_album_{info.id if info else token}",
                    service_name="TikTok",
                    deep_link=deep_link,
                    message_text=bm.captions(
                        user_settings["captions"],
                        info.description if info else None,
                        bot_url,
                    ),
                    preview_file_id=preview_file_id,
                    preview_url=first_photo,
                    thumbnail_url=(info.cover if info and info.cover else first_photo),
                ))
                await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)
                return
            
    except Exception as e:
        logging.exception(
            "Error processing inline TikTok query: user_id=%s query=%s error=%s",
            query.from_user.id,
            summarize_text_for_log(query.query),
            e,
        )
        await query.answer([], cache_time=1, is_personal=True)


@with_inline_send_logging("tiktok", "inline_send")
async def _send_inline_tiktok_video(
    *,
    token: str,
    inline_message_id: str,
    actor_name: str,
    actor_user_id: int,
    request_event_id: str,
    duplicate_handler: str,
) -> None:
    request = claim_inline_video_request_for_send(
        token,
        duplicate_handler=duplicate_handler,
        actor_user_id=actor_user_id,
    )
    if request is None:
        return

    download_path: Optional[str] = None

    async def _edit_inline_status(
        text: str,
        *,
        with_retry_button: bool = False,
        media_kind: str = "video",
    ) -> None:
        button_text = "Send photo inline" if media_kind == "photo" else "Send video inline"
        reply_markup = (
            kb.inline_send_media_keyboard(button_text, f"inline:tiktok:{token}")
            if with_retry_button
            else None
        )
        await safe_edit_inline_text(
            bot,
            inline_message_id,
            text,
            reply_markup=reply_markup,
        )

    try:
        async def _on_retry_fetch(failed_attempt: int, total_attempts: int, _error):
            if failed_attempt >= 2:
                await _edit_inline_status(bm.retrying_again_status(failed_attempt + 1, total_attempts))

        data = await fetch_tiktok_data_with_retry(request.source_url, on_retry=_on_retry_fetch)
        info = await video_info(data)
        images = data.get("data", {}).get("images", [])
        if not info:
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
            return
        if len(images) > 1:
            complete_inline_video_request(token)
            await _edit_inline_status(bm.inline_photos_not_supported("TikTok"))
            return
        if images:
            db_photo_url = build_tiktok_video_url(info)
            cache_key = build_media_cache_key(db_photo_url, item_index=0, item_kind="photo")
            db_id = await db.get_file_id(cache_key)
            if not db_id:
                if not CHANNEL_ID:
                    logging.error("CHANNEL_ID is not configured; TikTok inline upload is disabled")
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return

                await _edit_inline_status(bm.uploading_status(), media_kind="photo")
                sent = await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=images[0],
                    caption=f"TikTok Photo from {actor_name}",
                )
                if not sent.photo:
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return
                db_id = sent.photo[-1].file_id
                await db.add_file(cache_key, db_id, "photo")
            else:
                await _edit_inline_status(bm.uploading_status(), media_kind="photo")

            bot_url = await get_bot_url(bot)
            edited = await safe_edit_inline_media(
                bot,
                inline_message_id,
                types.InputMediaPhoto(
                    media=db_id,
                    caption=bm.captions(request.user_settings["captions"], info.description, bot_url),
                    parse_mode="HTML",
                ),
                reply_markup=kb.return_video_info_keyboard(
                    info.views,
                    info.likes,
                    info.comments,
                    info.shares,
                    info.music_play_url,
                    db_photo_url,
                    request.user_settings,
                    audio_callback_data=get_tiktok_audio_callback_data(info),
                ),
            )
            if edited:
                complete_inline_video_request(token)
                return

            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
            return

        db_video_url = build_tiktok_video_url(info)
        audio_callback_data = get_tiktok_audio_callback_data(info)
        db_id = await db.get_file_id(db_video_url)

        if not db_id:
            if not CHANNEL_ID:
                logging.error("CHANNEL_ID is not configured; TikTok inline upload is disabled")
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            download_name = f"{info.id}_{timestamp}_tiktok_video.mp4"
            request_id = f"tiktok_inline:{request.owner_user_id}:{request_event_id}:{info.id}"
            size_hint = get_tiktok_size_hint(data)

            await _edit_inline_status(bm.downloading_video_status())

            on_progress = make_status_text_progress_updater("TikTok video", _edit_inline_status)
            on_retry_download = make_retry_status_notifier(_edit_inline_status)

            metrics = await asyncio.wait_for(
                tiktok_service.download_video(
                    db_video_url,
                    download_name,
                    download_data=data,
                    user_id=request.owner_user_id,
                    request_id=request_id,
                    size_hint=size_hint,
                    on_progress=on_progress,
                    on_retry=on_retry_download,
                ),
                timeout=420.0,
            )
            if not metrics:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            log_download_metrics("tiktok_inline", metrics)
            download_path = metrics.path
            if metrics.size >= MAX_FILE_SIZE:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.video_too_large())
                return

            await _edit_inline_status(bm.uploading_status())
            sent = await bot.send_video(
                chat_id=CHANNEL_ID,
                video=FSInputFile(download_path),
                caption=f"TikTok Video from {actor_name}",
            )
            db_id = sent.video.file_id
            await db.add_file(db_video_url, db_id, "video")
            logging.info(
                "Inline TikTok video cached: url=%s file_id=%s",
                summarize_url_for_log(db_video_url),
                db_id,
            )
        else:
            await _edit_inline_status(bm.uploading_status())

        bot_url = await get_bot_url(bot)
        edited = await safe_edit_inline_media(
            bot,
            inline_message_id,
            types.InputMediaVideo(
                media=db_id,
                caption=bm.captions(request.user_settings["captions"], info.description, bot_url),
                parse_mode="HTML",
            ),
            reply_markup=kb.return_video_info_keyboard(
                info.views,
                info.likes,
                info.comments,
                info.shares,
                info.music_play_url,
                db_video_url,
                request.user_settings,
                audio_callback_data=audio_callback_data,
            ),
        )
        if edited:
            complete_inline_video_request(token)
            logging.info(
                "Served inline TikTok video: inline_message_id=%s url=%s file_id=%s",
                inline_message_id,
                summarize_url_for_log(db_video_url),
                db_id,
            )
            return

        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    except DownloadRateLimitError as e:
        reset_inline_video_request(token)
        await _edit_inline_status(build_rate_limit_text(e.retry_after), with_retry_button=True)
    except DownloadQueueBusyError as e:
        reset_inline_video_request(token)
        await _edit_inline_status(build_queue_busy_text(e.position), with_retry_button=True)
    except asyncio.TimeoutError:
        reset_inline_video_request(token)
        await _edit_inline_status(bm.timeout_error(), with_retry_button=True)
    except Exception as e:
        logging.exception(
            "Error sending inline TikTok video: inline_message_id=%s token=%s error=%s",
            inline_message_id,
            token,
            e,
        )
        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    finally:
        if download_path:
            await remove_file(download_path)


@router.chosen_inline_result(F.result_id.startswith("tiktok_inline:"))
@with_chosen_inline_logging("tiktok", "chosen_inline")
async def chosen_inline_tiktok_result(result: types.ChosenInlineResult):
    inline_message_id = result.inline_message_id
    if not inline_message_id:
        logging.warning(
            "Chosen inline TikTok result is missing inline_message_id; enable inline feedback in BotFather"
        )
        return

    token = result.result_id.removeprefix("tiktok_inline:")
    await _send_inline_tiktok_video(
        token=token,
        inline_message_id=inline_message_id,
        actor_name=result.from_user.full_name,
        actor_user_id=getattr(result.from_user, "id", None),
        request_event_id=result.result_id,
        duplicate_handler="chosen",
    )


@router.callback_query(F.data.startswith("inline:tiktok:"))
@with_callback_logging("tiktok", "inline_callback")
async def send_inline_tiktok_video_callback(call: types.CallbackQuery):
    token = call.data.removeprefix("inline:tiktok:")
    inline_message_id = call.inline_message_id
    if not inline_message_id:
        await call.answer("This button works only in inline mode.", show_alert=True)
        return

    await call.answer()
    try:
        await _send_inline_tiktok_video(
            token=token,
            inline_message_id=inline_message_id,
            actor_name=call.from_user.full_name,
            actor_user_id=call.from_user.id,
            request_event_id=str(call.id),
            duplicate_handler="callback",
        )
    except PermissionError:
        await call.answer(bm.something_went_wrong(), show_alert=True)
        return
    except ValueError as exc:
        if str(exc) == "already_processing":
            await call.answer(bm.inline_video_already_processing(), show_alert=False)
            return
        if str(exc) == "already_completed":
            await call.answer(bm.inline_video_already_sent(), show_alert=False)
            return
        await call.answer(bm.something_went_wrong(), show_alert=True)


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
