import os

import instaloader
from aiogram import Router, F, types
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder

from main import bot, db
from config import OUTPUT_DIR, INST_PASS, INST_LOGIN
from handlers.user import update_info
import messages as bm

router = Router()

L = instaloader.Instaloader()
L.login(INST_LOGIN, INST_PASS)


@router.message(F.text.regexp(r"(https?://(www\.)?instagram\.com/[^\s]+)"))
async def process_url_instagram(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")
    bot_url = f"t.me/{(await bot.get_me()).username}"

    url = message.text.strip()

    react = types.ReactionTypeEmoji(emoji="👨‍💻")
    await message.react([react])

    # Get the Instagram post from URL
    try:
        post = instaloader.Post.from_shortcode(L.context, url.split("/")[-2])
        user_captions = await db.get_user_captions(message.from_user.id)
        download_dir = f"{OUTPUT_DIR}.{post.shortcode}"

        L.download_post(post, target=download_dir)

        post_caption = post.caption

        # Create media group
        all_files_photo = []
        all_files_video = []

        # Iterate through the downloaded files and add them to the media group
        for root, dirs, files in os.walk(download_dir):

            for file in files:
                file_path = os.path.join(root, file)
                if file.endswith(('.jpg', '.jpeg', '.png')):
                    all_files_photo.append(file_path)
                elif file.endswith('.mp4'):
                    all_files_video.append(file_path)

        # Send the media group to the user with one caption
        while all_files_photo:
            media_group = MediaGroupBuilder(caption=bm.captions(user_captions, post_caption, bot_url))
            for _ in range(min(10, len(all_files_photo))):
                file_path = all_files_photo.pop(0)
                media_group.add_photo(media=FSInputFile(file_path), parse_mode="HTML")

            await bot.send_media_group(chat_id=message.chat.id, media=media_group.build())

        while all_files_video:
            media_group = MediaGroupBuilder(caption=bm.captions(user_captions, post_caption, bot_url))
            for _ in range(min(10, len(all_files_photo))):
                file_path = all_files_photo.pop(0)
                media_group.add_video(media=FSInputFile(file_path), parse_mode="HTML")

            await bot.send_media_group(chat_id=message.chat.id, media=media_group.build())

        # Clean up downloaded files and directory
        for root, dirs, files in os.walk(download_dir):
            for file in files:
                os.remove(os.path.join(root, file))
            os.rmdir(download_dir)

    except Exception as e:
        print(e)
        react = types.ReactionTypeEmoji(emoji="👎")
        await message.react([react])
        await message.reply(f"An error occurred during the download: {e}")

    await update_info(message)
