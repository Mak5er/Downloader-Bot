import asyncio
import logging as py_logging
import os
import time
from datetime import timedelta

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

logging = logging.bind(service="admin")
from main import bot, db
from services.download_queue import get_download_queue
from services.runtime_stats import get_runtime_snapshot

router = Router()

_ADMIN_ACCESS_REQUIRED = "Admin access required."
_DOWNLOAD_CLEANUP_MIN_AGE_SECONDS = 6 * 60 * 60.0
_ADMIN_ACTIVE_CHECK_CONCURRENCY = 8
_ADMIN_MAILING_CONCURRENCY = 5
_ADMIN_THROTTLE_SECONDS = 0.05


def _is_admin_user(user_id: int | None) -> bool:
    return user_id is not None and int(user_id) in ADMINS_UID


async def _ensure_admin_callback(call: types.CallbackQuery) -> bool:
    if _is_admin_user(getattr(getattr(call, "from_user", None), "id", None)):
        return True
    await call.answer(_ADMIN_ACCESS_REQUIRED, show_alert=True)
    return False


async def _ensure_admin_message(message: types.Message) -> bool:
    if _is_admin_user(getattr(getattr(message, "from_user", None), "id", None)):
        return True
    await message.answer(_ADMIN_ACCESS_REQUIRED, reply_markup=types.ReplyKeyboardRemove())
    return False


async def _run_bounded(items, *, limit: int, worker):
    semaphore = asyncio.Semaphore(max(1, int(limit)))

    async def _wrapped(item):
        async with semaphore:
            return await worker(item)

    return await asyncio.gather(*(_wrapped(item) for item in items))


async def _check_user_reachability(user) -> bool:
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
            logging.warning("Retry-after triggered twice while checking user %s: %s", user_id, retry_error)
        except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest) as retry_error:
            logging.info("User %s unreachable on retry: %s", user_id, retry_error)
        except TelegramAPIError as retry_error:
            logging.error("API error on retry while checking user %s: %s", user_id, retry_error)
        except Exception as retry_error:
            logging.error("Unexpected retry error while checking user %s: %s", user_id, retry_error)
    except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest) as error:
        logging.info("User %s unreachable: %s", user_id, error)
    except TelegramAPIError as error:
        logging.error("API error while checking user %s: %s", user_id, error)
    except Exception as error:
        logging.error("Unexpected error while checking user %s: %s", user_id, error)

    if send_successful:
        if user_status != "active":
            await db.set_active(user_id)
    elif user_status != "ban":
        await db.set_inactive(user_id)

    await asyncio.sleep(_ADMIN_THROTTLE_SECONDS)
    return send_successful


async def _deliver_mailing_message(user, *, sender_id: int, message_id: int) -> None:
    user_id = int(user.user_id)
    user_status = user.status or "inactive"

    try:
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=sender_id,
            message_id=message_id,
        )
        if user_status == "inactive":
            await db.set_active(user_id)
    except Exception as error:
        error_text = str(error)
        if error_text == "Forbidden: bots can't send messages to bots":
            await db.delete_user(user_id)
        if "blocked" in error_text or "Chat not found" in error_text:
            await db.set_inactive(user_id)
    finally:
        await asyncio.sleep(_ADMIN_THROTTLE_SECONDS)


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


@router.message(Command("perf"), IsBotAdmin())
async def perf_metrics(message: types.Message):
    queue = get_download_queue()
    snapshot = await queue.metrics_snapshot()
    if not snapshot:
        await message.answer(bm.no_queue_metrics_yet())
        return

    lines = ["<b>Queue performance (p50/p95)</b>"]
    for source in sorted(snapshot.keys()):
        item = snapshot[source]
        lines.append(
            (
                f"\n<b>{source}</b>\n"
                f"Jobs: {item.count}\n"
                f"Queue wait: {item.queue_wait_p50_ms:.0f}/{item.queue_wait_p95_ms:.0f} ms\n"
                f"Processing: {item.processing_p50_ms:.0f}/{item.processing_p95_ms:.0f} ms"
            )
        )

    await message.answer("\n".join(lines), parse_mode="HTML")


def _format_bytes(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0, num_bytes))
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


@router.message(Command("session"), IsBotAdmin())
async def session_metrics(message: types.Message):
    snapshot = get_runtime_snapshot()
    uptime = str(timedelta(seconds=int(snapshot.uptime_seconds)))
    lines = [
        "<b>Runtime Session Stats</b>",
        f"Uptime: <b>{uptime}</b>",
        f"Total downloads: <b>{snapshot.total_downloads}</b>",
        f"Videos: <b>{snapshot.total_videos}</b>",
        f"Audio: <b>{snapshot.total_audio}</b>",
        f"Other: <b>{snapshot.total_other}</b>",
        f"Traffic: <b>{_format_bytes(snapshot.total_bytes)}</b>",
    ]

    if snapshot.by_source:
        lines.append("")
        lines.append("<b>By source:</b>")
        for source, payload in sorted(snapshot.by_source.items()):
            lines.append(
                f"{source}: {payload.get('count', 0)} | {_format_bytes(int(payload.get('bytes', 0) or 0))}"
            )

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data == 'delete_log')
async def del_log(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return

    await bot.send_chat_action(call.message.chat.id, "typing")
    py_logging.shutdown()
    for log_path in (
        "log/bot_log.log",
        "log/error_log.log",
        "log/events_log.jsonl",
        "log/perf_log.jsonl",
    ):
        open(log_path, "w", encoding="utf-8").close()
    await call.message.reply(bm.log_deleted())
    await call.answer()


@router.callback_query(F.data == 'download_log')
async def download_log_handler(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return

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

    logging.info("User action: Downloaded logs (User ID: %s)", user_id)


@router.callback_query(F.data == 'check_active_users')
async def check_active_users(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return

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
    results = await _run_bounded(
        users_to_check,
        limit=_ADMIN_ACTIVE_CHECK_CONCURRENCY,
        worker=_check_user_reachability,
    )
    reachable = sum(1 for result in results if result)
    unreachable = total_users - reachable

    await status_message.edit_text(
        bm.active_users_check_completed(total_users, reachable, unreachable),
        reply_markup=kb.return_back_to_admin_keyboard(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == 'cancel_action')
async def cancel_action(call: types.CallbackQuery, state: FSMContext):
    if not await _ensure_admin_callback(call):
        return

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
    if not await _ensure_admin_callback(call):
        return

    await call.answer()
    await bot.send_chat_action(call.message.chat.id, "typing")
    await call.message.edit_text(
        bm.enter_chat_id(),
        reply_markup=kb.cancel_keyboard()
    )
    await state.set_state(Admin.write_chat_id)


@router.message(Admin.write_chat_id)
async def admin_collect_chat_id(message: types.Message, state: FSMContext):
    if not await _ensure_admin_message(message):
        await state.clear()
        return

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
    if not await _ensure_admin_message(message):
        await state.clear()
        return

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
        logging.info("Failed to send message to chat %s: %s", chat_id, error)
        await bot.delete_message(message.chat.id, progress_message.message_id)
        await message.answer(bm.chat_message_failed(chat_id), reply_markup=kb.return_back_to_admin_keyboard())
        return
    except TelegramAPIError as error:
        logging.error("API error while sending message to chat %s: %s", chat_id, error)
        await bot.delete_message(message.chat.id, progress_message.message_id)
        await message.answer(bm.chat_message_failed(chat_id), reply_markup=kb.return_back_to_admin_keyboard())
        return
    except Exception as error:
        logging.error("Unexpected error while sending message to chat %s: %s", chat_id, error)
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
    if not await _ensure_admin_callback(call):
        return

    await bot.delete_message(call.message.chat.id, call.message.message_id)
    await admin(call.message)


@router.callback_query(F.data == 'send_to_all')
async def send_to_all_callback(call: types.CallbackQuery, state: FSMContext):
    if not await _ensure_admin_callback(call):
        return

    await call.message.edit_text(text=bm.mailing_message(),
                                 reply_markup=kb.cancel_keyboard())
    await state.set_state(Mailing.send_to_all_message)
    await call.answer()


@router.message(Mailing.send_to_all_message)
async def send_to_all_message(message: types.Message, state: FSMContext):
    if not await _ensure_admin_message(message):
        await state.clear()
        return

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

        users = await db.get_all_users_info()
        await _run_bounded(
            users,
            limit=_ADMIN_MAILING_CONCURRENCY,
            worker=lambda user: _deliver_mailing_message(
                user,
                sender_id=sender_id,
                message_id=message.message_id,
            ),
        )

        await bot.send_message(chat_id=message.chat.id,
                               text=bm.finish_mailing(),
                               reply_markup=types.ReplyKeyboardRemove())
        return


@router.callback_query(F.data.startswith("write_"))
async def write_message_handler(call: types.CallbackQuery, state: FSMContext):
    if not await _ensure_admin_callback(call):
        return

    chat_id = call.data.split("_")[1]
    await call.message.delete_reply_markup()
    await call.message.delete()
    await call.message.answer(bm.please_type_message(), reply_markup=kb.cancel_keyboard())
    await state.set_state(Admin.write_message)
    await state.update_data(chat_id=chat_id)


@router.message(Admin.write_message)
async def write_message(message: types.Message, state: FSMContext):
    if not await _ensure_admin_message(message):
        await state.clear()
        return

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
        queue_snapshot = get_download_queue().load_snapshot()
        if queue_snapshot.active_jobs > 0 or queue_snapshot.queued_jobs > 0:
            message = (
                f"Skipped clearing '{OUTPUT_DIR}': "
                f"active_jobs={queue_snapshot.active_jobs}, queued_jobs={queue_snapshot.queued_jobs}."
            )
        elif os.path.exists(OUTPUT_DIR):
            removed_files = 0
            skipped_recent_files = 0
            cutoff_timestamp = time.time() - _DOWNLOAD_CLEANUP_MIN_AGE_SECONDS
            for file in os.listdir(OUTPUT_DIR):
                file_path = os.path.join(OUTPUT_DIR, file)
                if not os.path.isfile(file_path):
                    continue
                if os.path.getmtime(file_path) > cutoff_timestamp:
                    skipped_recent_files += 1
                    continue
                os.remove(file_path)
                removed_files += 1
            message = (
                f"The folder '{OUTPUT_DIR}' has been cleaned. "
                f"Removed {removed_files} files; skipped {skipped_recent_files} recent files."
            )
        else:
            message = f"The folder '{OUTPUT_DIR}' does not exist."

    except Exception as e:
        message = f"An error occurred while clearing the folder: {e}"

    for admin_id in ADMINS_UID:
        try:
            await bot.send_message(chat_id=admin_id, text=message)
        except Exception as e:
            logging.error("Failed to send a message to admin %s: %s", admin_id, e)
