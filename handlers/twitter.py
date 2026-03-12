import asyncio
import html
import os
import re
from typing import Optional
from urllib.parse import urlsplit

import requests
from aiogram import Router, F, types
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, CHANNEL_ID
from handlers.utils import (
    build_request_id,
    build_start_deeplink_url,
    build_progress_status,
    get_bot_url,
    get_message_text,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    safe_edit_inline_media,
    safe_edit_inline_text,
    send_chat_action_if_needed,
    resolve_settings_target_id,
    with_callback_logging,
    with_chosen_inline_logging,
    with_inline_query_logging,
    with_inline_send_logging,
    with_message_logging,
)
from log.logger import logger as logging
from main import bot, db, send_analytics
from utils.download_manager import (
    DownloadConfig,
    DownloadMetrics,
    ResilientDownloader,
    log_download_metrics,
)
from services.inline_service_icons import get_inline_service_icon
from services.inline_album_links import create_inline_album_request
from services.inline_video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)

logging = logging.bind(service="twitter")

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)  # 1.5 GB

router = Router()

twitter_downloader = ResilientDownloader(
    OUTPUT_DIR,
    config=DownloadConfig(
        chunk_size=1024 * 1024,
        multipart_threshold=8 * 1024 * 1024,
        max_workers=8,             # more workers for faster parallel fetch
        max_concurrent_downloads=3,
        stream_timeout=(5.0, 45.0),
    ),
    source="twitter",
)


def extract_tweet_ids(text):
    expanded_links: list[str] = []
    short_links = re.findall(r't\.co\/[a-zA-Z0-9]+', text)
    if short_links:
        with requests.Session() as session:
            for link in short_links:
                try:
                    response = session.get(f'https://{link}', allow_redirects=True, timeout=5)
                    expanded_links.append(response.url)
                except requests.RequestException as exc:
                    logging.error("Failed to expand t.co URL: url=%s error=%s", link, exc)

    combined_text = '\n'.join([text, *expanded_links]) if expanded_links else text
    tweet_ids = re.findall(
        r"(?:twitter|x)\.com/.{1,15}/(?:web|status(?:es)?)/([0-9]{1,20})",
        combined_text,
    )
    return list(dict.fromkeys(tweet_ids)) if tweet_ids else None


def scrape_media(tweet_id):
    r = requests.get(f'https://api.vxtwitter.com/Twitter/status/{tweet_id}')
    r.raise_for_status()
    try:
        return r.json()
    except requests.exceptions.JSONDecodeError:
        if match := re.search(r'<meta content="(.*?)" property="og:description" />', r.text):
            error_message = html.unescape(match.group(1))
            logging.error("Twitter API returned error: tweet_id=%s error=%s", tweet_id, error_message)
            raise Exception(f'API returned error: {error_message}')
        logging.error("Failed to parse Twitter API response JSON: tweet_id=%s", tweet_id)
        raise
    except Exception as e:
        logging.error("Failed to fetch media for tweet: tweet_id=%s error=%s", tweet_id, e)
        raise


def _extract_single_inline_tweet_media(source_url: str) -> tuple[str, dict, dict] | None:
    tweet_ids = extract_tweet_ids(source_url)
    if not tweet_ids:
        return None

    tweet_id = tweet_ids[0]
    tweet_media = scrape_media(tweet_id)
    items = [item for item in tweet_media.get("media_extended", []) if item.get("url") and item.get("type")]
    if len(items) != 1:
        return None

    media = items[0]
    media_type = media.get("type")
    if media_type == "image":
        return "photo", tweet_media, media
    if media_type in {"video", "gif"}:
        return "video", tweet_media, media
    return None


async def _collect_media_files(
    tweet_id,
    tweet_media,
    *,
    user_id: Optional[int] = None,
    request_id: Optional[str] = None,
):
    photos: list[str] = []
    videos: list[str] = []

    download_tasks = []
    media_meta: list[tuple[str, str, str]] = []

    for media in tweet_media.get('media_extended', []):
        media_url = media.get('url')
        media_type = media.get('type')
        if not media_url or not media_type:
            logging.debug("Skipping malformed media entry: tweet_id=%s entry=%s", tweet_id, media)
            continue

        file_name = os.path.join(str(tweet_id), os.path.basename(urlsplit(media_url).path))

        logging.debug(
            "Queueing tweet media download: tweet_id=%s type=%s url=%s",
            tweet_id,
            media_type,
            media_url,
        )
        download_tasks.append(
            twitter_downloader.download(
                media_url,
                file_name,
                skip_if_exists=True,
                user_id=user_id,
                request_id=request_id,
            )
        )
        media_meta.append((media_type, file_name, media_url))

    if not download_tasks:
        logging.debug("No downloadable media found: tweet_id=%s", tweet_id)
        return photos, videos

    results = await asyncio.gather(*download_tasks, return_exceptions=True)
    for (media_type, file_path, media_url), result in zip(media_meta, results):
        if isinstance(result, Exception):
            logging.error(
                "Failed to download tweet media chunk: tweet_id=%s path=%s type=%s error=%s",
                tweet_id,
                os.path.join(OUTPUT_DIR, file_path),
                media_type,
                result,
            )
            continue
        resolved_path = (
            result.path if isinstance(result, DownloadMetrics) else os.path.join(OUTPUT_DIR, file_path)
        )

        log_download_metrics("twitter_media", result if isinstance(result, DownloadMetrics) else DownloadMetrics(
            url=media_url,
            path=resolved_path,
            size=os.path.getsize(resolved_path) if os.path.exists(resolved_path) else 0,
            elapsed=0.0,
            used_multipart=isinstance(result, DownloadMetrics) and result.used_multipart,
            resumed=isinstance(result, DownloadMetrics) and result.resumed,
        ))

        if media_type == 'image':
            photos.append(resolved_path)
        elif media_type in ('video', 'gif'):
            videos.append(resolved_path)

    return photos, videos


async def _send_photo_responses(message, photos, caption, keyboard):
    if not photos:
        return

    if len(photos) > 1:
        album_photos = photos[:-1]
        for i in range(0, len(album_photos), 10):
            media_group = MediaGroupBuilder()
            for file_path in album_photos[i:i + 10]:
                media_group.add_photo(media=FSInputFile(file_path))
            await message.answer_media_group(media_group.build())
        photos = [photos[-1]]

    await message.answer_photo(
        photo=FSInputFile(photos[0]),
        caption=caption,
        reply_markup=keyboard,
    )


async def _send_video_responses(message, videos, caption, keyboard):
    if not videos:
        return

    if len(videos) > 1:
        album_videos = videos[:-1]
        for i in range(0, len(album_videos), 10):
            media_group = MediaGroupBuilder()
            for file_path in album_videos[i:i + 10]:
                media_group.add_video(media=FSInputFile(file_path))
            await message.answer_media_group(media_group.build())
        videos = [videos[-1]]

    await message.answer_video(
        video=FSInputFile(videos[0]),
        caption=caption,
        reply_markup=keyboard,
    )


async def _cleanup_tweet_dir(tweet_dir):
    try:
        for root, _dirs, files in os.walk(tweet_dir):
            for file in files:
                await remove_file(os.path.join(root, file))
        await asyncio.to_thread(os.rmdir, tweet_dir)
        logging.debug("Cleaned tweet temp directory: path=%s", tweet_dir)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logging.debug("Failed to cleanup tweet directory: path=%s error=%s", tweet_dir, exc)


async def reply_media(message, tweet_id, tweet_media, bot_url, business_id, user_settings):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="twitter")
    logging.info(
        "Processing tweet media: user_id=%s tweet_id=%s",
        message.from_user.id,
        tweet_id,
    )

    tweet_dir = os.path.join(OUTPUT_DIR, str(tweet_id))
    os.makedirs(tweet_dir, exist_ok=True)
    logging.debug("Tweet temp directory ready: path=%s", tweet_dir)

    post_url = tweet_media['tweetURL']
    post_caption = tweet_media["text"]
    likes = tweet_media['likes']
    comments = tweet_media['replies']
    retweets = tweet_media['retweets']
    try:
        photos, videos = await _collect_media_files(
            tweet_id,
            tweet_media,
            user_id=message.from_user.id,
            request_id=f"twitter:{message.chat.id}:{message.message_id}:{tweet_id}",
        )
        logging.info(
            "Tweet media fetched: tweet_id=%s photos=%s videos=%s",
            tweet_id,
            len(photos),
            len(videos),
        )

        caption_media = bm.captions("on", post_caption, bot_url)
        caption_text = bm.captions("on", post_caption, bot_url, limit=4096)
        keyboard = kb.return_video_info_keyboard(None, likes, comments, retweets, None, post_url, user_settings)

        await _send_photo_responses(message, photos, caption_media, keyboard)
        await _send_video_responses(message, videos, caption_media, keyboard)
        if not photos and not videos:
            await message.answer(caption_text, reply_markup=keyboard, parse_mode="HTML")

        logging.info(
            "Tweet media delivered: user_id=%s tweet_id=%s",
            message.from_user.id,
            tweet_id,
        )

    except Exception as e:
        logging.exception(
            "Error processing tweet media: tweet_id=%s user_id=%s error=%s",
            tweet_id,
            message.from_user.id,
            e,
        )
        await react_to_message(message, "👎", business_id=business_id)
        await message.reply(bm.something_went_wrong())
    finally:
        asyncio.create_task(_cleanup_tweet_dir(tweet_dir))


@router.message(
    F.text.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)", mode="search")
    | F.caption.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)", mode="search")
)
@router.business_message(
    F.text.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)", mode="search")
    | F.caption.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)", mode="search")
)
@with_message_logging("twitter", "message")
async def handle_tweet_links(message, direct_url: Optional[str] = None):
    business_id = message.business_connection_id
    text = direct_url or get_message_text(message)

    logging.info(
        "Twitter request received: user_id=%s username=%s business_id=%s text=%s",
        message.from_user.id,
        message.from_user.username,
        business_id,
        text,
    )

    await react_to_message(message, "👾", business_id=business_id)

    bot_url = await get_bot_url(bot)
    user_settings = await db.user_settings(resolve_settings_target_id(message))

    try:
        tweet_ids = await asyncio.to_thread(extract_tweet_ids, text)
        if tweet_ids:
            logging.info("Twitter links parsed: user_id=%s count=%s", message.from_user.id, len(tweet_ids))
            await send_chat_action_if_needed(bot, message.chat.id, "typing", business_id)

            for tweet_id in tweet_ids:
                try:
                    logging.info("Fetching tweet media: tweet_id=%s", tweet_id)
                    media = await asyncio.to_thread(scrape_media, tweet_id)
                    await reply_media(message, tweet_id, media, bot_url, business_id, user_settings)
                    await maybe_delete_user_message(message, user_settings.get("delete_message"))
                except Exception as e:
                    logging.exception("Failed to process tweet: tweet_id=%s error=%s", tweet_id, e)
                    await message.reply(bm.something_went_wrong())
        else:
            logging.info("No tweet links found: user_id=%s", message.from_user.id)
            await react_to_message(message, "👎", business_id=business_id)
            await message.reply(bm.nothing_found())
    except Exception as e:
        logging.exception("Error handling tweet links: user_id=%s error=%s", message.from_user.id, e)
        await message.reply(bm.something_went_wrong())


@router.inline_query(F.query.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)", mode="search"))
@with_inline_query_logging("twitter", "inline_query")
async def inline_twitter_query(query: types.InlineQuery):
    try:
        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_twitter_media")
        match = re.search(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)", query.query or "")
        if not match:
            await query.answer([], cache_time=1, is_personal=True)
            return

        source_url = match.group(0)
        user_settings = await db.user_settings(query.from_user.id)
        inline_media = await asyncio.to_thread(_extract_single_inline_tweet_media, source_url)
        if not inline_media:
            tweet_ids = await asyncio.to_thread(extract_tweet_ids, source_url)
            if tweet_ids:
                tweet_media = await asyncio.to_thread(scrape_media, tweet_ids[0])
                media_items = [
                    item for item in tweet_media.get("media_extended", [])
                    if item.get("url") and item.get("type")
                ]
                photo_items = [item for item in media_items if item.get("type") == "image"]
                if len(photo_items) > 1:
                    bot_url = await get_bot_url(bot)
                    token = create_inline_album_request(query.from_user.id, "twitter", source_url)
                    deep_link = build_start_deeplink_url(bot_url, f"album_{token}")
                    results = [
                        types.InlineQueryResultPhoto(
                            id=f"twitter_album_{tweet_ids[0]}",
                            photo_url=photo_items[0]["url"],
                            thumbnail_url=photo_items[0]["url"],
                            title=bm.inline_album_title("Twitter"),
                            description=bm.inline_album_description(),
                            caption=bm.captions(
                                user_settings["captions"],
                                tweet_media.get("text"),
                                bot_url,
                            ),
                            reply_markup=types.InlineKeyboardMarkup(
                                inline_keyboard=[[
                                    types.InlineKeyboardButton(
                                        text=bm.inline_open_full_album_button(),
                                        url=deep_link,
                                    )
                                ]]
                            ),
                            parse_mode="HTML",
                        )
                    ]
                    await query.answer(results, cache_time=10, is_personal=True)
                    return

            results = [
                types.InlineQueryResultArticle(
                    id="twitter_inline_unsupported",
                    title="X / Twitter Post",
                    description="Only single photo or single video posts are supported inline.",
                    thumbnail_url=get_inline_service_icon("twitter"),
                    input_message_content=types.InputTextMessageContent(
                        message_text="Only single photo or single video posts are supported inline. Use the bot directly for threads and albums.",
                    ),
                )
            ]
            await query.answer(results, cache_time=10, is_personal=True)
            return

        media_kind, tweet_media, media = inline_media
        token = create_inline_video_request("twitter", source_url, query.from_user.id, user_settings)
        action_text = "Send photo inline" if media_kind == "photo" else "Send video inline"
        prompt_text = (
            bm.inline_send_video_prompt("Twitter")
            if media_kind == "video"
            else "Twitter photo is being prepared...\nIf it does not start automatically, tap the button below."
        )
        results = [
            types.InlineQueryResultArticle(
                id=f"twitter_inline:{token}",
                title="X / Twitter Post",
                description=tweet_media.get("text") or f"Press the button to send this {media_kind} inline.",
                thumbnail_url=get_inline_service_icon("twitter"),
                input_message_content=types.InputTextMessageContent(message_text=prompt_text),
                reply_markup=kb.inline_send_media_keyboard(action_text, f"inline:twitter:{token}"),
            )
        ]
        await query.answer(results, cache_time=10, is_personal=True)
    except Exception as exc:
        logging.exception(
            "Error processing Twitter inline query: user_id=%s query=%s error=%s",
            query.from_user.id,
            query.query,
            exc,
        )
        await query.answer([], cache_time=1, is_personal=True)


@with_inline_send_logging("twitter", "inline_send")
async def _send_inline_twitter_media(
    *,
    token: str,
    inline_message_id: str,
    actor_name: str,
    request_event_id: str,
    duplicate_handler: str,
) -> None:
    request = claim_inline_video_request_for_send(token, duplicate_handler=duplicate_handler)
    if request is None:
        return

    download_path: Optional[str] = None

    async def _edit_inline_status(text: str, *, with_retry_button: bool = False, media_kind: str = "video") -> None:
        button_text = "Send photo inline" if media_kind == "photo" else "Send video inline"
        reply_markup = (
            kb.inline_send_media_keyboard(button_text, f"inline:twitter:{token}")
            if with_retry_button
            else None
        )
        await safe_edit_inline_text(bot, inline_message_id, text, reply_markup=reply_markup)

    try:
        await _edit_inline_status(bm.fetching_info_status())
        inline_media = await asyncio.to_thread(_extract_single_inline_tweet_media, request.source_url)
        if not inline_media:
            complete_inline_video_request(token)
            await _edit_inline_status("Only single photo or single video posts are supported inline.")
            return

        media_kind, tweet_media, media = inline_media
        post_url = tweet_media["tweetURL"]
        post_caption = tweet_media.get("text")
        likes = tweet_media.get("likes")
        comments = tweet_media.get("replies")
        retweets = tweet_media.get("retweets")

        if media_kind == "photo":
            edited = await safe_edit_inline_media(
                bot,
                inline_message_id,
                types.InputMediaPhoto(
                    media=media["url"],
                    caption=bm.captions(request.user_settings["captions"], post_caption, await get_bot_url(bot)),
                    parse_mode="HTML",
                ),
                reply_markup=kb.return_video_info_keyboard(
                    None,
                    likes,
                    comments,
                    retweets,
                    None,
                    post_url,
                    request.user_settings,
                ),
            )
            if edited:
                complete_inline_video_request(token)
                return
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
            return

        cache_key = post_url
        db_file_id = await db.get_file_id(cache_key)
        if not db_file_id:
            if not CHANNEL_ID:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            file_name = os.path.join(str(tweet_media["conversationID"]), os.path.basename(urlsplit(media["url"]).path))
            progress_state = {"last": 0.0}
            await _edit_inline_status(bm.downloading_video_status())

            async def on_progress(progress):
                now = time.monotonic()
                if not progress.done and now - progress_state["last"] < 1.0:
                    return
                progress_state["last"] = now
                await _edit_inline_status(build_progress_status("X / Twitter video", progress))

            metrics = await twitter_downloader.download(
                media["url"],
                file_name,
                skip_if_exists=True,
                user_id=request.owner_user_id,
                request_id=f"twitter_inline:{request.owner_user_id}:{request_event_id}:{tweet_media['conversationID']}",
                on_progress=on_progress,
            )
            if not metrics:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            log_download_metrics("twitter_inline", metrics)
            download_path = metrics.path
            if metrics.size >= MAX_FILE_SIZE:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.video_too_large())
                return

            await _edit_inline_status(bm.uploading_status())
            sent = await bot.send_video(
                chat_id=CHANNEL_ID,
                video=FSInputFile(download_path),
                caption=f"X / Twitter Video from {actor_name}",
            )
            db_file_id = sent.video.file_id
            await db.add_file(cache_key, db_file_id, "video")
        else:
            await _edit_inline_status(bm.uploading_status())

        edited = await safe_edit_inline_media(
            bot,
            inline_message_id,
            types.InputMediaVideo(
                media=db_file_id,
                caption=bm.captions(request.user_settings["captions"], post_caption, await get_bot_url(bot)),
                parse_mode="HTML",
            ),
            reply_markup=kb.return_video_info_keyboard(
                None,
                likes,
                comments,
                retweets,
                None,
                post_url,
                request.user_settings,
            ),
        )
        if edited:
            complete_inline_video_request(token)
            return

        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    except Exception as exc:
        logging.exception(
            "Error sending Twitter inline media: inline_message_id=%s token=%s error=%s",
            inline_message_id,
            token,
            exc,
        )
        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    finally:
        if download_path:
            await remove_file(download_path)


@router.chosen_inline_result(F.result_id.startswith("twitter_inline:"))
@with_chosen_inline_logging("twitter", "chosen_inline")
async def chosen_inline_twitter_result(result: types.ChosenInlineResult):
    if not result.inline_message_id:
        logging.warning("Chosen inline Twitter result is missing inline_message_id")
        return

    token = result.result_id.removeprefix("twitter_inline:")
    await _send_inline_twitter_media(
        token=token,
        inline_message_id=result.inline_message_id,
        actor_name=result.from_user.full_name,
        request_event_id=result.result_id,
        duplicate_handler="chosen",
    )


@router.callback_query(F.data.startswith("inline:twitter:"))
@with_callback_logging("twitter", "inline_callback")
async def send_inline_twitter_media_callback(call: types.CallbackQuery):
    if not call.inline_message_id:
        await call.answer("This button works only in inline mode.", show_alert=True)
        return

    token = call.data.removeprefix("inline:twitter:")
    await call.answer()
    try:
        await _send_inline_twitter_media(
            token=token,
            inline_message_id=call.inline_message_id,
            actor_name=call.from_user.full_name,
            request_event_id=str(call.id),
            duplicate_handler="callback",
        )
    except ValueError as exc:
        if str(exc) == "already_processing":
            await call.answer(bm.inline_video_already_processing(), show_alert=False)
            return
        if str(exc) == "already_completed":
            await call.answer(bm.inline_video_already_sent(), show_alert=False)
            return
