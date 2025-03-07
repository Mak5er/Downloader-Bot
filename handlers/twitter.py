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
            logging.error(f"Failed to expand URL {link}: {e}")

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
            logging.error(f"API returned error: {error_message}")
            raise Exception(f'API returned error: {error_message}')
        logging.error("Failed to parse API response JSON.")
        raise
    except Exception as e:
        logging.error(f"Failed to fetch media for tweet {tweet_id}: {e}")
        raise


async def download_media(media_url, file_path):
    try:
        response = requests.get(media_url, stream=True)
        response.raise_for_status()
        with open(file_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)
    except Exception as e:
        logging.error(f"Failed to download media from {media_url}: {e}")
        raise

async def reply_media(message, tweet_id, tweet_media, bot_url, business_id):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="twitter")

    tweet_dir = f"{OUTPUT_DIR}/{tweet_id}"
    post_caption = tweet_media["text"]
    user_captions = await db.get_user_captions(message.from_user.id)

    if not os.path.exists(tweet_dir):
        os.makedirs(tweet_dir)

    all_files_photo = []
    all_files_video = []

    try:
        for media in tweet_media['media_extended']:
            media_url = media['url']
            media_type = media['type']
            file_name = os.path.join(tweet_dir, os.path.basename(urlsplit(media_url).path))

            await download_media(media_url, file_name)

            if media_type == 'image':
                all_files_photo.append(file_name)
            elif media_type == 'video' or media_type == 'gif':
                all_files_video.append(file_name)

        while all_files_photo:
            media_group = MediaGroupBuilder(caption=bm.captions(user_captions, post_caption, bot_url))
            for _ in range(min(10, len(all_files_photo))):
                file_path = all_files_photo.pop(0)
                media_group.add_photo(media=FSInputFile(file_path))
            await message.answer_media_group(media_group.build())

        while all_files_video:
            media_group = MediaGroupBuilder(caption=bm.captions(user_captions, post_caption, bot_url))
            for _ in range(min(10, len(all_files_video))):
                file_path = all_files_video.pop(0)
                media_group.add_video(media=FSInputFile(file_path))
            await message.answer_media_group(media_group.build())

        await asyncio.sleep(5)

        for root, dirs, files in os.walk(tweet_dir):
            for file in files:
                os.remove(os.path.join(root, file))
        os.rmdir(tweet_dir)

    except Exception as e:
        logging.error(f"Error processing media for tweet ID {tweet_id}: {e}")
        if business_id is None:
            react = types.ReactionTypeEmoji(emoji="👎")
            await message.react([react])
        await message.reply(bm.something_went_wrong())


@router.message(F.text.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)") )
@router.business_message(F.text.regexp(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)") )
async def handle_tweet_links(message):
    business_id = message.business_connection_id

    if business_id is None:
        react = types.ReactionTypeEmoji(emoji="👨‍💻")
        await message.react([react])

    bot_url = f"t.me/{(await bot.get_me()).username}"

    try:
        tweet_ids = extract_tweet_ids(message.text)
        if tweet_ids:
            if business_id is None:
                await bot.send_chat_action(message.chat.id, "typing")

            for tweet_id in tweet_ids:
                try:
                    media = scrape_media(tweet_id)
                    await reply_media(message, tweet_id, media, bot_url, business_id)
                except Exception as e:
                    logging.error(f"Failed to process tweet {tweet_id}: {e}")
                    await message.answer(bm.something_went_wrong())
        else:
            if business_id is None:
                react = types.ReactionTypeEmoji(emoji="👎")
                await message.react([react])
            await message.answer(bm.nothing_found())
    except Exception as e:
        logging.error(f"Error handling tweet links: {e}")
        await message.answer(bm.something_went_wrong())
