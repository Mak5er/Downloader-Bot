from aiogram import types, Router
from aiogram.filters import Command
from main import db

router = Router()


async def update_info(message: types.Message):
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    user_username = message.from_user.username
    result = await db.user_exist(user_id)
    if result:
        await db.user_update_name(user_id, user_name, user_username)
    else:
        await db.add_users(user_id, user_name, user_username, "private", "uk", 'active')


@router.message(Command("start"))
async def send_welcome(message: types.Message):
    await message.reply("Welcome to MaxLoad Downloader! Send me a link to download the video.")
    await update_info(message)
