import os

import instaloader
from aiogram import Router, F, types
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder

from helper import trim_video
from main import bot
from config import OUTPUT_DIR, INST_PASS, INST_LOGIN

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

        dir = f"{OUTPUT_DIR}.{post.shortcode}"

        L.download_post(post, target=dir)

        # Create media group
        if post.caption is not None:
            media_group = MediaGroupBuilder(caption=f'{post.caption}\n\n<a href="{bot_url}">💻Powered by MaxLoad</a>')
        else:
            media_group = MediaGroupBuilder(caption=f'<a href="{bot_url}">💻Powered by MaxLoad</a>')

        # Iterate through the downloaded files and add them to the media group
        for root, dirs, files in os.walk(dir):
            for file in files:
                file_path = os.path.join(root, file)
                if file.endswith(('.jpg', '.jpeg', '.png')):
                    media_group.add_photo(media=FSInputFile(file_path), parse_mode="HTML")
                elif file.endswith('.mp4'):
                    trim_video(dir + "/" + file)
                    media_group.add_video(media=FSInputFile(file_path), parse_mode="HTML")

        # Send the media group to the user with one caption
        if media_group:
            await bot.send_media_group(chat_id=message.chat.id, media=media_group.build())

        # Clean up downloaded files and directory
        for root, dirs, files in os.walk(dir):
            for file in files:
                os.remove(os.path.join(root, file))
            os.rmdir(dir)

    except Exception as e:
        print(e)
        react = types.ReactionTypeEmoji(emoji="👎")
        await message.react([react])
        await message.reply("The URL does not seem to be a valid Instagram video or photo link.")
