from aiogram import types, Router, F
from aiogram.filters import Command

import keyboards as kb
import messages as bm
from filters import ChatTypeF
from main import db, send_analytics, bot

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
    await db.set_active(user_id)


@router.message(ChatTypeF('group'), F.new_chat_member)
async def send_welcome(message: types.Message):
    for user in message.new_chat_members:
        if user.is_bot and user.id == bot.id:
            chat_info = await bot.get_chat(message.chat.id)
            chat_type = "public"
            user_id = message.chat.id
            user_name = chat_info.title
            user_username = None
            language = 'uk'
            status = 'active'
            referrer_id = None

            await db.add_users(user_id, user_name, user_username, chat_type, language, status, referrer_id)

            chat_title = chat_info.title
            await bot.send_message(
                chat_id=message.chat.id,
                text=bm.join_group(chat_title),
                parse_mode="HTML")


@router.message(Command("start"))
async def send_welcome(message: types.Message):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name='start')

    await message.reply(bm.welcome_message())
    await update_info(message)


@router.message(Command("settings"))
async def settings(message: types.Message):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name='settings')

    await message.reply(
        text=bm.settings(),
        reply_markup=kb.return_settings_keyboard(),
        parse_mode="HTML")


@router.callback_query(F.data == 'back_to_settings')
async def back_to_settings(call: types.CallbackQuery):
    await call.message.edit_text(
        text=bm.settings(),
        reply_markup=kb.return_settings_keyboard())
    await call.answer()


@router.callback_query(F.data == "settings_caption")
async def captions_setting(call: types.CallbackQuery):
    user_captions = await db.get_user_captions(call.from_user.id)
    await call.message.edit_text(
        text=bm.captions_settings(),
        reply_markup=kb.return_captions_keyboard(captions=user_captions), parse_mode='HTML')
    await call.answer()


@router.callback_query(F.data.startswith('captions_'))
async def change_captions(call: types.CallbackQuery):
    captions = call.data.split('_')[1]
    await db.update_captions(captions=captions, user_id=call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=kb.return_captions_keyboard(captions))
    await call.answer()
