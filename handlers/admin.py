import asyncio
import os

from aiogram import types, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import BufferedInputFile

import keyboards as kb
import messages as bm
from config import ADMINS_UID, OUTPUT_DIR
from filters import IsBotAdmin
from log.logger import logger as logging
from main import bot, db

router = Router()


class Mailing(StatesGroup):
    send_to_all_message = State()


class Admin(StatesGroup):
    add_joke = State()
    control_user = State()
    ban_reason = State()
    feedback_answer = State()
    write_message = State()
    write_chat_id = State()
    write_chat_text = State()


@router.message(Command("admin"), IsBotAdmin())
async def admin(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")

    if message.chat.type == 'private':
        user_count = await db.user_count()
        private_chat_count = await db.private_chat_count()
        group_chat_count = await db.group_chat_count()
        active_user_count = await db.active_user_count()
        inactive_user_count = await db.inactive_user_count()

        await message.answer(
            text=bm.admin_panel(
                user_count,
                private_chat_count,
                group_chat_count,
                active_user_count,
                inactive_user_count,
            ),
            reply_markup=kb.admin_keyboard(),
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


@router.callback_query(F.data == 'check_active_users')
async def check_active_users(call: types.CallbackQuery):
    await call.answer()
    await bot.send_chat_action(call.message.chat.id, "typing")

    users_info = await db.get_all_users_info()
    users_to_check = [user for user in users_info if (user.status or "inactive") != "ban"]

    if not users_to_check:
        await call.message.edit_text(
            bm.active_users_check_no_targets(),
            reply_markup=kb.return_back_to_admin_keyboard()
        )
        return

    total_users = len(users_to_check)
    status_message = await call.message.edit_text(bm.active_users_check_started(total_users))

    reachable = 0
    unreachable = 0

    for user in users_to_check:
        user_id = int(user.user_id)
        user_status = user.status or "inactive"
        send_successful = False

        try:
            await bot.send_chat_action(chat_id=user_id, action="typing")
            send_successful = True
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after)
            try:
                await bot.send_chat_action(chat_id=user_id, action="typing")
                send_successful = True
            except TelegramRetryAfter as retry_error:
                logging.warning(f"Retry-after triggered twice while checking user {user_id}: {retry_error}")
            except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest) as retry_error:
                logging.info(f"User {user_id} unreachable on retry: {retry_error}")
            except TelegramAPIError as retry_error:
                logging.error(f"API error on retry while checking user {user_id}: {retry_error}")
            except Exception as retry_error:
                logging.error(f"Unexpected retry error while checking user {user_id}: {retry_error}")
        except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest) as error:
            logging.info(f"User {user_id} unreachable: {error}")
        except TelegramAPIError as error:
            logging.error(f"API error while checking user {user_id}: {error}")
        except Exception as error:
            logging.error(f"Unexpected error while checking user {user_id}: {error}")

        if send_successful:
            reachable += 1
            if user_status != "active":
                await db.set_active(user_id)
        else:
            unreachable += 1
            if user_status != "ban":
                await db.set_inactive(user_id)

        await asyncio.sleep(0.05)

    await status_message.edit_text(
        bm.active_users_check_completed(total_users, reachable, unreachable),
        reply_markup=kb.return_back_to_admin_keyboard(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == 'cancel_action')
async def cancel_action(call: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await call.answer("Nothing to cancel")
        return

    await state.clear()
    await call.answer("Canceled")

    try:
        await call.message.edit_text(
            bm.canceled(),
            reply_markup=kb.return_back_to_admin_keyboard()
        )
    except TelegramBadRequest:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await call.message.answer(
            bm.canceled(),
            reply_markup=kb.return_back_to_admin_keyboard()
        )


@router.callback_query(F.data == 'message_chat_id')
async def message_chat_id(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await bot.send_chat_action(call.message.chat.id, "typing")
    await call.message.edit_text(
        bm.enter_chat_id(),
        reply_markup=kb.cancel_keyboard()
    )
    await state.set_state(Admin.write_chat_id)


@router.message(Admin.write_chat_id)
async def admin_collect_chat_id(message: types.Message, state: FSMContext):
    if message.text == bm.cancel():
        await bot.send_message(message.chat.id, bm.canceled(), reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return

    chat_id_text = message.text.strip()
    try:
        chat_id = int(chat_id_text)
    except ValueError:
        await message.reply(bm.invalid_chat_id())
        return

    await state.update_data(target_chat_id=chat_id)
    await message.answer(bm.enter_chat_message(), reply_markup=kb.cancel_keyboard())
    await state.set_state(Admin.write_chat_text)


@router.message(Admin.write_chat_text)
async def admin_send_to_chat(message: types.Message, state: FSMContext):
    if message.text == bm.cancel():
        await bot.send_message(message.chat.id, bm.canceled(), reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return

    data = await state.get_data()
    chat_id = data.get("target_chat_id")

    if chat_id is None:
        await message.reply(bm.chat_message_failed("unknown"), reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return

    chat_text = message.text
    await state.clear()

    progress_message = await message.reply(bm.chat_message_sending(), reply_markup=types.ReplyKeyboardRemove())

    sent_message = None

    try:
        sent_message = await bot.send_message(chat_id=chat_id, text=chat_text)
    except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest) as error:
        logging.info(f"Failed to send message to chat {chat_id}: {error}")
        await bot.delete_message(message.chat.id, progress_message.message_id)
        await message.answer(bm.chat_message_failed(chat_id), reply_markup=kb.return_back_to_admin_keyboard())
        return
    except TelegramAPIError as error:
        logging.error(f"API error while sending message to chat {chat_id}: {error}")
        await bot.delete_message(message.chat.id, progress_message.message_id)
        await message.answer(bm.chat_message_failed(chat_id), reply_markup=kb.return_back_to_admin_keyboard())
        return
    except Exception as error:
        logging.error(f"Unexpected error while sending message to chat {chat_id}: {error}")
        await bot.delete_message(message.chat.id, progress_message.message_id)
        await message.answer(bm.chat_message_failed(chat_id), reply_markup=kb.return_back_to_admin_keyboard())
        return

    if sent_message:
        chat_obj = sent_message.chat
        chat_type_raw = getattr(chat_obj, "type", None)
        chat_type_str = chat_type_raw.value if hasattr(chat_type_raw, "value") else str(chat_type_raw)
        chat_type_value = "private" if chat_type_str == "private" else "public"

        chat_name = getattr(chat_obj, "title", None) or getattr(chat_obj, "full_name", None)

        if not chat_name:
            first_name = getattr(chat_obj, "first_name", None)
            last_name = getattr(chat_obj, "last_name", None)
            name_parts = [part for part in (first_name, last_name) if part]
            if name_parts:
                chat_name = " ".join(name_parts)

        if not chat_name:
            chat_name = f"Chat {chat_id}"

        chat_username = getattr(chat_obj, "username", None)
        language_code = getattr(chat_obj, "language_code", None)

        await db.upsert_chat(
            user_id=chat_id,
            user_name=chat_name,
            user_username=chat_username,
            chat_type=chat_type_value,
            language=language_code,
            status="active",
        )

    await bot.delete_message(message.chat.id, progress_message.message_id)
    await message.answer(bm.chat_message_sent(chat_id), reply_markup=kb.return_back_to_admin_keyboard())


@router.callback_query(F.data == 'back_to_admin')
async def back_to_admin(call: types.CallbackQuery):
    await bot.delete_message(call.message.chat.id, call.message.message_id)
    await admin(call.message)


@router.callback_query(F.data == 'send_to_all')
async def send_to_all_callback(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text(text=bm.mailing_message(),
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
        logging.error(e)
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
            logging.error(f"Failed to send a message to admin {admin_id}: {e}")
