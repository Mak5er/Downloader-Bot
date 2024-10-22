import asyncio
import os
import re

import instaloader
from aiogram import Router, F, types
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder
from moviepy.editor import VideoFileClip

import messages as bm
from config import OUTPUT_DIR, INST_PASS, INST_LOGIN, admin_id
from handlers.user import update_info
from main import bot, db, send_analytics

router = Router()

L = instaloader.Instaloader()


# Асинхронне очікування коду двофакторної автентифікації
async def wait_for_code(admin_id):
    code_future = asyncio.Future()

    # Надсилаємо повідомлення адміну з проханням ввести код
    await bot.send_message(chat_id=admin_id, text="Введіть код двофакторної автентифікації Instagram:")

    @router.message()
    async def handle_message(message: types.Message):
        if message.from_user.id == admin_id:
            code_future.set_result(message.text)

    # Чекаємо на код
    return await code_future


# Асинхронна обробка авторизації Instaloader з двофакторною автентифікацією
async def instaloader_login(L, login, password, admin_id):
    try:
        # Спробувати завантажити сесію
        await asyncio.to_thread(L.load_session_from_file, login)
        print("Login with Session")
    except Exception as e:
        print(e)
        try:
            await asyncio.to_thread(L.close)
            await asyncio.to_thread(L.login, login, password)
            await asyncio.to_thread(L.save_session_to_file)
            print("Login Successful")
        except instaloader.exceptions.TwoFactorAuthRequiredException:
            # Отримуємо код 2FA від адміністратора
            code = str(await wait_for_code(admin_id))
            # Виконуємо двофакторний логін з кодом
            await asyncio.to_thread(L.two_factor_login, code)


@router.message(F.text.regexp(r"(https?://(www\.)?instagram\.com/\S+)"))
@router.business_message(F.text.regexp(r"(https?://(www\.)?instagram\.com/\S+)"))
async def process_url_instagram(message: types.Message):
    await instaloader_login(L, INST_LOGIN, INST_PASS, admin_id)

    business_id = message.business_connection_id

    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="instagram")

    bot_url = f"t.me/{(await bot.get_me()).username}"

    url_match = re.match(r"(https?://(www\.)?instagram\.com/\S+)", message.text)
    if url_match:
        url = url_match.group(0)
    else:
        url = message.text

    if business_id is None:
        react = types.ReactionTypeEmoji(emoji="👨‍💻")
        await message.react([react])

    # Get the Instagram post from URL
    try:
        post = instaloader.Post.from_shortcode(L.context, url.split("/")[-2])
        user_captions = await db.get_user_captions(message.from_user.id)
        download_dir = f"{OUTPUT_DIR}.{post.shortcode}"

        reels_url = "https://www.instagram.com/reel/"

        post_caption = post.caption

        db_file_id = await db.get_file_id(reels_url + post.shortcode)

        if db_file_id:
            if business_id is None:
                await bot.send_chat_action(message.chat.id, "upload_video")

            await message.answer_video(video=db_file_id[0][0],
                                       caption=bm.captions(user_captions, post_caption, bot_url),
                                       parse_mode="HTMl")
            return

        L.download_post(post, target=download_dir)

        if "/reel/" in url:
            file_type = "video"

            for root, _, files in os.walk(download_dir):
                for file in files:
                    if file.endswith('.mp4'):
                        file_path = os.path.join(root, file)

                        video_clip = VideoFileClip(file_path)
                        width, height = video_clip.size

                        if business_id is None:
                            await bot.send_chat_action(message.chat.id, "upload_video")

                        sent_message = await message.answer_video(video=FSInputFile(file_path),
                                                                  caption=bm.captions(user_captions, post_caption,
                                                                                      bot_url),
                                                                  width=width, height=height,
                                                                  parse_mode="HTML")

                        file_id = sent_message.video.file_id

                        await db.add_file(url=reels_url + post.shortcode, file_id=file_id, file_type=file_type)
                        break
        else:
            # Send all media if the URL is not for a reel
            media_group = MediaGroupBuilder(caption=bm.captions(user_captions, post_caption, bot_url))

            batch_size = 10
            batch = 0
            for root, _, files in os.walk(download_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    if file.endswith(('.jpg', '.jpeg', '.png')):
                        media_group.add_photo(media=FSInputFile(file_path), parse_mode="HTML")
                        batch += 1
                    elif file.endswith('.mp4'):
                        media_group.add_video(media=FSInputFile(file_path), parse_mode="HTML")
                        batch += 1

                    if batch == batch_size:
                        await message.answer_media_group(media=media_group.build())
                        media_group = MediaGroupBuilder(caption=bm.captions(user_captions, post_caption, bot_url))

            if batch > 0:
                await message.answer_media_group(media=media_group.build())

        await asyncio.sleep(5)

        # Clean up downloaded files and directory
        for root, dirs, files in os.walk(download_dir):
            for file in files:
                os.remove(os.path.join(root, file))
            os.rmdir(download_dir)

    except Exception as e:
        print(e)
        if business_id is None:
            react = types.ReactionTypeEmoji(emoji="👎")
            await message.react([react])
        await message.reply("Something went wrong :(\nPlease try again later.")

    await update_info(message)
