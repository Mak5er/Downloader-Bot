from log.logger import logger as logging
import asyncio
import os

from aiogram import types, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import BufferedInputFile

from main import bot, db
from filters import IsBotAdmin
import keyboards as kb
import messages as bm
from config import ADMINS_UID, OUTPUT_DIR

router = Router()


class Mailing(StatesGroup):
    send_to_all_message = State()


class Admin(StatesGroup):
    add_joke = State()
    control_user = State()
    ban_reason = State()
    feedback_answer = State()
    write_message = State()


@router.message(Command("admin"), IsBotAdmin())
async def admin(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")

    if message.chat.type == 'private':
        user_count = await db.user_count()
        active_user_count = await db.active_user_count()
        inactive_user_count = await db.inactive_user_count()

        await message.answer(
            text=bm.admin_panel(user_count, active_user_count, inactive_user_count), reply_markup=kb.admin_keyboard(),
            parse_mode='HTML')

    else:
        await message.answer(bm.not_groups())


@router.callback_query(F.data == 'delete_log')
async def del_log(call: types.CallbackQuery):
    await bot.send_chat_action(call.message.chat.id, "typing")
    logging.shutdown()
    open('log/bot_log.log', 'w').close()
    await call.message.reply(bm.log_deleted())
    await call.answer()


@router.callback_query(F.data == 'download_log')
async def download_log_handler(call: types.CallbackQuery):
    await bot.send_chat_action(call.message.chat.id, "typing")

    log_files = [
        ('log/bot_log.log', 'bot_log.log'),
        ('log/error_log.log', 'error_log.log')
    ]
    user_id = call.from_user.id

    await call.answer()

    for file_path, filename in log_files:
        with open(file_path, 'rb') as file:
            await call.message.answer_document(BufferedInputFile(file.read(), filename=filename))

    logging.info(f"User action: Downloaded logs (User ID: {user_id})")


@router.callback_query(F.data == 'back_to_admin')
async def back_to_admin(call: types.CallbackQuery):
    await bot.delete_message(call.message.chat.id, call.message.message_id)
    await admin(call.message)


@router.callback_query(F.data == 'send_to_all')
async def send_to_all_callback(call: types.CallbackQuery, state: FSMContext):
    await bot.send_message(chat_id=call.message.chat.id,
                           text=bm.mailing_message(),
                           reply_markup=kb.cancel_keyboard())
    await state.set_state(Mailing.send_to_all_message)
    await call.answer()


@router.message(Mailing.send_to_all_message)
async def send_to_all_message(message: types.Message, state: FSMContext):
    sender_id = message.from_user.id
    if message.text == bm.cancel():
        await message.answer(text=bm.canceled(), reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return

    else:
        await state.clear()

        await bot.send_message(chat_id=message.chat.id,
                               text=bm.start_mailing(),
                               reply_markup=types.ReplyKeyboardRemove())

        users = await db.all_users()
        for user in users:
            try:
                await bot.copy_message(chat_id=user,
                                       from_chat_id=sender_id,
                                       message_id=message.message_id)

                user_status = await db.status(user)

                if user_status == "inactive":
                    await db.set_active(user)

                await asyncio.sleep(
                    0.05
                )

            except Exception as e:

                if str(e) == "Forbidden: bots can't send messages to bots":
                    await db.delete_user(user)

                if "blocked" or "Chat not found" in str(e):
                    await db.set_inactive(user)
                continue

        await bot.send_message(chat_id=message.chat.id,
                               text=bm.finish_mailing(),
                               reply_markup=types.ReplyKeyboardRemove())
        return


@router.callback_query(F.data == 'control_user')
async def control_user_callback(call: types.CallbackQuery):
    await bot.delete_message(call.message.chat.id, call.message.message_id)
    await call.message.answer(text=bm.search_user_by(), reply_markup=kb.return_search_keyboard())
    await call.answer()


@router.callback_query(F.data.startswith("search_"))
async def search_user_by(call: types.CallbackQuery, state: FSMContext):
    search = call.data.split('_')[1]
    await bot.delete_message(call.message.chat.id, call.message.message_id)
    await call.message.answer(text=bm.type_user(search), reply_markup=kb.cancel_keyboard())

    await state.set_state(Admin.control_user)
    await state.update_data(search=search)
    await call.answer()


@router.message(Admin.control_user)
async def control_user(message: types.Message, state: FSMContext):
    answer = message.text
    answer = answer.replace("@", "")
    answer = answer.replace("https://t.me/", "")
    data = await state.get_data()
    search = data.get("search")

    if message.text == bm.cancel():
        await bot.send_message(message.chat.id, bm.canceled(),
                               reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        await admin(message)
        return

    else:
        await bot.send_chat_action(message.chat.id, "typing")

        clock = await bot.send_message(message.chat.id, '⏳', reply_markup=types.ReplyKeyboardRemove())

        await asyncio.sleep(2)

        await bot.delete_message(message.chat.id, clock.message_id)

        user = None

        if search == "id":
            user = await db.get_user_info(answer)

        elif search == "username":
            user = await db.get_user_info_username(answer)

        result = user

        if result is not None:
            user_name = None
            user_username = None
            status = None
            user_id = None

            if search == "id":
                user_name, user_username, status = result
                user_id = answer

            elif search == "username":
                user_name, user_id, status = result
                user_username = answer

            if user_username == "":
                user_username = "None"
            else:
                user_username = f"@{user_username}"

            user_photo = await bot.get_user_profile_photos(user_id, limit=1)

            if user_photo.total_count > 0:
                await message.reply_photo(user_photo.photos[0][-1].file_id,
                                          caption=bm.return_user_info(user_name, user_id, user_username, status),
                                          reply_markup=kb.return_control_user_keyboard(user_id, status),
                                          parse_mode="HTML")
            else:
                await bot.send_message(message.chat.id, bm.return_user_info(user_name, user_id, user_username, status),
                                       reply_markup=kb.return_control_user_keyboard(user_id, status), parse_mode="HTML")

        else:
            await bot.send_message(message.chat.id, bm.user_not_found())

        await state.clear()


@router.callback_query(F.data.startswith("ban_"))
async def message_handler(call: types.CallbackQuery, state: FSMContext):
    banned_user_id = call.data.split("_")[1]

    await call.message.delete()
    await call.message.answer(bm.enter_ban_reason(), reply_markup=kb.cancel_keyboard())
    await state.set_state(Admin.ban_reason)
    await state.update_data(banned_user_id=banned_user_id)
    await call.answer()


@router.message(Admin.ban_reason)
async def control_user(message: types.Message, state: FSMContext):
    reason = message.text
    data = await state.get_data()
    banned_user_id = data.get("banned_user_id")

    if message.text == bm.cancel():
        await bot.send_message(message.chat.id, bm.canceled(),
                               reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        await admin(message)
        return

    await db.ban_user(banned_user_id)

    await state.clear()

    await bot.send_message(chat_id=banned_user_id,
                           text=bm.ban_message(reason),
                           reply_markup=types.ReplyKeyboardRemove())

    ban_message = await message.answer(bm.successful_ban(banned_user_id),
                                       reply_markup=types.ReplyKeyboardRemove())

    await bot.delete_message(message.chat.id, ban_message.message_id)

    await message.answer(bm.successful_ban(banned_user_id), reply_markup=kb.return_back_to_admin_keyboard())


@router.callback_query(F.data.startswith("unban_"))
async def message_handler(call: types.CallbackQuery):
    unbanned_user_id = call.data.split("_")[1]

    await db.set_active(unbanned_user_id)

    await bot.send_message(chat_id=unbanned_user_id,
                           text=bm.unban_message())

    await call.message.delete()

    await call.message.answer(bm.successful_unban(unbanned_user_id),
                              reply_markup=kb.return_back_to_admin_keyboard())

    await call.answer()


@router.callback_query(F.data.startswith("write_"))
async def write_message_handler(call: types.CallbackQuery, state: FSMContext):
    chat_id = call.data.split("_")[1]
    await call.message.delete_reply_markup()
    await call.message.delete()
    await call.message.answer(bm.please_type_message(), reply_markup=kb.cancel_keyboard())
    await state.set_state(Admin.write_message)
    await state.update_data(chat_id=chat_id)


@router.message(Admin.write_message)
async def write_message(message: types.Message, state: FSMContext):
    answer = message.text

    if answer == bm.cancel():
        await bot.send_message(message.chat.id, bm.canceled(), reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return
    data = await state.get_data()
    chat_id = data.get('chat_id')
    await state.clear()

    try:
        await bot.send_message(chat_id=chat_id,
                               text=answer)
        message_sent = await message.reply(bm.your_message_sent(), reply_markup=types.ReplyKeyboardRemove())

        await bot.delete_message(message.chat.id, message_sent.message_id)

        await message.answer(bm.your_message_sent(), reply_markup=kb.return_back_to_admin_keyboard())


    except Exception as e:
        print(e)
        await message.reply(bm.something_went_wrong(),
                            reply_markup=kb.return_back_to_admin_keyboard())


async def clear_downloads_and_notify():
    try:
        if os.path.exists(OUTPUT_DIR):
            for file in os.listdir(OUTPUT_DIR):
                file_path = os.path.join(OUTPUT_DIR, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)
            message = f"The folder '{OUTPUT_DIR}' has been successfully cleared."
        else:
            message = f"The folder '{OUTPUT_DIR}' does not exist."

    except Exception as e:
        message = f"An error occurred while clearing the folder: {e}"

    for admin_id in ADMINS_UID:
        try:
            await bot.send_message(chat_id=admin_id, text=message)
        except Exception as e:
            print(f"Failed to send a message to admin {admin_id}: {e}")
