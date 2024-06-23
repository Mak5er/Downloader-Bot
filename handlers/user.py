from aiogram import types, Router, F
from aiogram.filters import Command
from main import db
import keyboards as kb

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


@router.message(Command("settings"))
async def settings(message: types.Message):
    await message.reply(
        text="⚙️ Settings\nUsing the buttons below, you can customize the bot's functionalities. Keep in mind that all the changes made will only apply to you.",
        reply_markup=kb.return_settings_keyboard())


@router.callback_query(F.data == 'back_to_settings')
async def back_to_settings(call: types.CallbackQuery):
    await call.message.edit_text(
        text="⚙️ Settings\nUsing the buttons below, you can customize the bot's functionalities. Keep in mind that all the changes made will only apply to you.",
        reply_markup=kb.return_settings_keyboard())
    await call.answer()


@router.callback_query(F.data == "settings_caption")
async def captions_setting(call: types.CallbackQuery):
    user_captions = await db.get_user_captions(call.from_user.id)
    await call.message.edit_text(
        text="<b>✏️Descriptions</b>\nChoose if you want to add a short description to downloaded content. Keep in mind that some extractors still don't support this feature.",
        reply_markup=kb.return_captions_keyboard(captions=user_captions), parse_mode='HTML')
    await call.answer()


@router.callback_query(F.data.startswith('captions_'))
async def change_captions(call: types.CallbackQuery):
    captions = call.data.split('_')[1]
    await db.update_captions(captions=captions, user_id=call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=kb.return_captions_keyboard(captions))
    await call.answer()
