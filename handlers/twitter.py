import asyncio
import html
import os
import re
from urllib.parse import urlsplit

import requests
from aiogram import Router, F
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR
from handlers.utils import (
    get_bot_url,
    get_message_text,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    send_chat_action_if_needed,
)
from log.logger import logger as logging
from main import bot, db, send_analytics
from utils.download_manager import (
    DownloadConfig,
    DownloadMetrics,
    ResilientDownloader,
    log_download_metrics,
)

MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)  # 1.5 GB

router = Router()

twitter_downloader = ResilientDownloader(
    OUTPUT_DIR,
    config=DownloadConfig(
        chunk_size=1024 * 1024,
        multipart_threshold=8 * 1024 * 1024,
        max_workers=8,             # more workers for faster parallel fetch
        stream_timeout=(5.0, 45.0),
    ),
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


async def _collect_media_files(tweet_id, tweet_media):
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
            twitter_downloader.download(media_url, file_name, skip_if_exists=True)
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
        photos, videos = await _collect_media_files(tweet_id, tweet_media)
        logging.info(
            "Tweet media fetched: tweet_id=%s photos=%s videos=%s",
            tweet_id,
            len(photos),
            len(videos),
        )

        caption = bm.captions("on", post_caption, bot_url)
        keyboard = kb.return_video_info_keyboard(None, likes, comments, retweets, None, post_url, user_settings)

        await _send_photo_responses(message, photos, caption, keyboard)
        await _send_video_responses(message, videos, caption, keyboard)
        if not photos and not videos:
            await message.answer(caption, reply_markup=keyboard, parse_mode="HTML")

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
    F.text.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)")
    | F.caption.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)")
)
@router.business_message(
    F.text.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)")
    | F.caption.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)")
)
async def handle_tweet_links(message):
    business_id = message.business_connection_id
    text = get_message_text(message)

    logging.info(
        "Twitter request received: user_id=%s username=%s business_id=%s text=%s",
        message.from_user.id,
        message.from_user.username,
        business_id,
        text,
    )

    await react_to_message(message, "👾", business_id=business_id)

    bot_url = await get_bot_url(bot)
    user_settings = await db.user_settings(message.from_user.id)

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
