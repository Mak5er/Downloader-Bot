import asyncio
import html
import os
import re
from urllib.parse import urlsplit

import requests
from aiogram import types, Router, F
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder

import messages as bm
from config import OUTPUT_DIR
from handlers.utils import get_bot_url, maybe_delete_user_message, remove_file, send_chat_action_if_needed
import keyboards as kb
from main import bot, db, send_analytics
from log.logger import logger as logging

MAX_FILE_SIZE = 500 * 1024 * 1024

router = Router()


def extract_tweet_ids(text):
    unshortened_links = ''
    for link in re.findall(r't\.co\/[a-zA-Z0-9]+', text):
        try:
            unshortened_link = requests.get('https://' + link).url
            unshortened_links += '\n' + unshortened_link
        except Exception as e:
            logging.error("Failed to expand t.co URL: url=%s error=%s", link, e)

    tweet_ids = re.findall(r"(?:twitter|x)\.com/.{1,15}/(?:web|status(?:es)?)/([0-9]{1,20})", text + unshortened_links)
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


async def download_media(media_url, file_path):
    try:
        response = requests.get(media_url, stream=True)
        response.raise_for_status()
        with open(file_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)
    except Exception as e:
        logging.error("Failed to download media: url=%s error=%s", media_url, e)
        raise


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
    user_captions = user_settings["captions"]

    all_files_photo = []
    all_files_video = []

    try:
        for media in tweet_media.get('media_extended', []):
            media_url = media['url']
            media_type = media['type']
            file_name = os.path.join(tweet_dir, os.path.basename(urlsplit(media_url).path))

            logging.debug("Downloading tweet media: tweet_id=%s type=%s url=%s", tweet_id, media_type, media_url)
            await download_media(media_url, file_name)

            if media_type == 'image':
                all_files_photo.append(file_name)
            elif media_type in ('video', 'gif'):
                all_files_video.append(file_name)

        logging.info(
            "Tweet media fetched: tweet_id=%s photos=%s videos=%s",
            tweet_id,
            len(all_files_photo),
            len(all_files_video),
        )

        if len(all_files_photo) > 1:
            media_group = MediaGroupBuilder()
            for file_path in all_files_photo[:-1]:
                media_group.add_photo(media=FSInputFile(file_path))
            await message.answer_media_group(media_group.build())
            last_photo = all_files_photo[-1]
            await message.answer_photo(
                photo=FSInputFile(last_photo),
                caption=bm.captions(user_captions, post_caption, bot_url),
                reply_markup=kb.return_video_info_keyboard(None, likes, comments, retweets, None, post_url, user_settings)
            )
        elif all_files_photo:
            await message.answer_photo(
                photo=FSInputFile(all_files_photo[0]),
                caption=bm.captions(user_captions, post_caption, bot_url),
                reply_markup=kb.return_video_info_keyboard(None, likes, comments, retweets, None, post_url, user_settings)
            )

        if len(all_files_video) > 1:
            media_group = MediaGroupBuilder()
            for file_path in all_files_video[:-1]:
                media_group.add_video(media=FSInputFile(file_path))
            await message.answer_media_group(media_group.build())
            last_video = all_files_video[-1]
            await message.answer_video(
                video=FSInputFile(last_video),
                caption=bm.captions(user_captions, post_caption, bot_url),
                reply_markup=kb.return_video_info_keyboard(None, likes, comments, retweets, None, post_url, user_settings)
            )
        elif all_files_video:
            await message.answer_video(
                video=FSInputFile(all_files_video[0]),
                caption=bm.captions(user_captions, post_caption, bot_url),
                reply_markup=kb.return_video_info_keyboard(None, likes, comments, retweets, None, post_url, user_settings)
            )

        logging.info(
            "Tweet media delivered: user_id=%s tweet_id=%s",
            message.from_user.id,
            tweet_id,
        )
        await asyncio.sleep(5)

        for root, _dirs, files in os.walk(tweet_dir):
            for file in files:
                await remove_file(os.path.join(root, file))
        await asyncio.to_thread(os.rmdir, tweet_dir)
        logging.debug("Cleaned tweet temp directory: path=%s", tweet_dir)

    except Exception as e:
        logging.exception(
            "Error processing tweet media: tweet_id=%s user_id=%s error=%s",
            tweet_id,
            message.from_user.id,
            e,
        )
        if business_id is None:
            react = types.ReactionTypeEmoji(emoji="👎")
            await message.react([react])
        await message.reply(bm.something_went_wrong())
@router.message(F.text.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)"))
@router.business_message(F.text.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)"))
async def handle_tweet_links(message):
    business_id = message.business_connection_id

    logging.info(
        "Twitter request received: user_id=%s username=%s business_id=%s text=%s",
        message.from_user.id,
        message.from_user.username,
        business_id,
        message.text,
    )

    if business_id is None:
        react = types.ReactionTypeEmoji(emoji="??")
        await message.react([react])

    bot_url = await get_bot_url(bot)
    user_settings = await db.user_settings(message.from_user.id)

    try:
        tweet_ids = extract_tweet_ids(message.text)
        if tweet_ids:
            logging.info("Twitter links parsed: user_id=%s count=%s", message.from_user.id, len(tweet_ids))
            await send_chat_action_if_needed(bot, message.chat.id, "typing", business_id)

            for tweet_id in tweet_ids:
                try:
                    logging.info("Fetching tweet media: tweet_id=%s", tweet_id)
                    media = scrape_media(tweet_id)
                    await reply_media(message, tweet_id, media, bot_url, business_id, user_settings)
                except Exception as e:
                    logging.exception("Failed to process tweet: tweet_id=%s error=%s", tweet_id, e)
                    await message.answer(bm.something_went_wrong())
        else:
            logging.info("No tweet links found: user_id=%s", message.from_user.id)
            if business_id is None:
                react = types.ReactionTypeEmoji(emoji="??")
                await message.react([react])
            await message.answer(bm.nothing_found())
    except Exception as e:
        logging.exception("Error handling tweet links: user_id=%s error=%s", message.from_user.id, e)
        await message.answer(bm.something_went_wrong())

    await maybe_delete_user_message(message, user_settings.get("delete_message"))

