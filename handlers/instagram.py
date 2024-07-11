import os

import instaloader
from aiogram import Router, F, types
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder

from main import bot, db, send_analytics
from config import OUTPUT_DIR, INST_PASS, INST_LOGIN
from handlers.user import update_info
import messages as bm

router = Router()

L = instaloader.Instaloader()

try:
    L.load_session_from_file(INST_LOGIN)

except:
    L.login(INST_LOGIN, INST_PASS)
    print("Logged in with password")
    L.save_session_to_file()


@router.message(F.text.regexp(r"(https?://(www\.)?instagram\.com/[^\s]+)"))
async def process_url_instagram(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")

    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="instagram")

    bot_url = f"t.me/{(await bot.get_me()).username}"

    url = message.text.strip()

    react = types.ReactionTypeEmoji(emoji="👨‍💻")
    await message.react([react])

    chat_id = message.chat.id

    # Get the Instagram post from URL
    try:
        post = instaloader.Post.from_shortcode(L.context, url.split("/")[-2])
        user_captions = await db.get_user_captions(message.from_user.id)
        download_dir = f"{OUTPUT_DIR}.{post.shortcode}"

        L.download_post(post, target=download_dir)

        post_caption = post.caption

        media_group = MediaGroupBuilder(caption=bm.captions(user_captions, post_caption, bot_url))

        batch_size = 10

        batch = 0
        # Create media group
        for root, _, files in os.walk(download_dir):
            for file in files:
                file_path = os.path.join(root, file)
                if file.endswith(('.jpg', '.jpeg', '.png')):
                    media_group.add_photo(media=FSInputFile(file_path), parse_mode="HTML")
                    batch += 1
                elif file.endswith('.mp4'):
                    media_group.add_video(media=FSInputFile(file_path), parse_mode="HTML")
                    batch += 1

                # Check if media group is full
                if batch == batch_size:
                    await bot.send_media_group(chat_id=chat_id, media=media_group.build())
                    media_group = MediaGroupBuilder(caption=bm.captions(user_captions, post_caption, bot_url))

        # Send remaining media if any
        if batch > 0:
            await bot.send_media_group(chat_id=chat_id, media=media_group.build())

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
