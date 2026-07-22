import asyncio
import time
from copy import copy
from typing import Optional

from aiogram import types
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import ChatMemberUpdated

import keyboards as kb
import messages as bm
from handlers.utils import get_bot_username, get_message_text
from services.logger import logger as logging, summarize_url_for_log
from app_context import db, send_analytics, bot
from services.runtime.pending_requests import pop_pending
from services.stats.chart import (
    _render_stats,
    _send_stats_photo,
    _handle_stats_update,
)
import handlers.user as user_mod

logging = logging.bind(service="user_commands")

_UPDATE_INFO_TTL_SECONDS = 120.0
_update_info_cache: dict[int, tuple[float, str, Optional[str]]] = {}


async def update_info(message: types.Message, referred_by: int | None = None, source: str | None = None):
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    user_username = message.from_user.username
    language = getattr(message.from_user, "language_code", None)

    now = time.monotonic()
    cached = user_mod._update_info_cache.get(user_id)
    if cached and now - cached[0] <= user_mod._UPDATE_INFO_TTL_SECONDS and referred_by is None and source is None:
        if cached[1] == user_name and cached[2] == user_username:
            return

    await user_mod.db.upsert_chat(
        user_id=user_id,
        user_name=user_name,
        user_username=user_username,
        chat_type="private",
        language=language,
        status="active",
        referred_by=referred_by,
        source=source,
    )
    user_mod._update_info_cache[user_id] = (now, user_name, user_username)


def _extract_start_payload(text: str) -> Optional[str]:
    if not text:
        return None
    parts = text.strip().split(maxsplit=1)
    if not parts or not parts[0].startswith("/start") or len(parts) < 2:
        return None
    return parts[1].strip() or None


def _build_pending_private_message(message: types.Message, pending_text: str) -> types.Message:
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"text": pending_text, "caption": None})

    replayed_message = copy(message)
    replayed_message.text = pending_text
    replayed_message.caption = None
    return replayed_message


async def send_welcome(message: types.Message):
    await user_mod.send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="start")

    referred_by = None
    source = None
    if message.chat.type == ChatType.PRIVATE:
        payload = user_mod._extract_start_payload(get_message_text(message))
        if payload:
            if payload.startswith("ref_"):
                try:
                    referred_by = int(payload[4:])
                except ValueError:
                    pass
            elif payload.startswith("src_"):
                source = payload[4:]
            else:
                if await user_mod._process_inline_album_deeplink(message, payload):
                    await user_mod.update_info(message)
                    return

    if referred_by is not None or source is not None:
        await user_mod.update_info(message, referred_by=referred_by, source=source)
    else:
        await user_mod.update_info(message)

    bot_username = await get_bot_username(user_mod.bot)
    await message.reply(
        bm.welcome_message(),
        reply_markup=kb.start_keyboard(bot_username, ref_user_id=message.from_user.id),
        parse_mode="HTML",
    )

    if message.chat.type == ChatType.PRIVATE:
        pending = user_mod.pop_pending(message.from_user.id)
        if pending:
            try:
                await user_mod.bot.delete_message(pending.notice_chat_id, pending.notice_message_id)
            except Exception:
                pass
            await user_mod._process_pending_message(_build_pending_private_message(message, pending.url))


async def send_help(message: types.Message):
    bot_username = await get_bot_username(user_mod.bot)
    await message.reply(
        bm.help_message(bot_username),
        reply_markup=kb.start_keyboard(bot_username),
        parse_mode="HTML",
    )


async def handle_bot_membership(update: ChatMemberUpdated):
    chat = update.chat
    new_status = update.new_chat_member.status
    old_status = getattr(update.old_chat_member, "status", None)

    if new_status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}:
        chat_id = chat.id
        chat_type_value = "private" if chat.type == ChatType.PRIVATE else "public"
        chat_name = chat.title or getattr(chat, "full_name", None) or f"Chat {chat_id}"
        chat_username = getattr(chat, "username", None)
        language = getattr(chat, "language_code", None)

        await user_mod.db.upsert_chat(
            user_id=chat_id,
            user_name=chat_name,
            user_username=chat_username,
            chat_type=chat_type_value,
            language=language,
            status="active",
        )

        chat_title = chat.title or chat_name

        became_member = old_status not in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
        became_admin = new_status == ChatMemberStatus.ADMINISTRATOR and old_status != ChatMemberStatus.ADMINISTRATOR

        if chat.type != ChatType.PRIVATE:
            if became_member:
                await user_mod.bot.send_message(
                    chat_id=chat_id,
                    text=bm.join_group(chat_title),
                    parse_mode="HTML",
                )

            if became_admin:
                await user_mod.bot.send_message(
                    chat_id=chat_id,
                    text=bm.admin_rights_granted(chat_title),
                    parse_mode="HTML",
                )

    elif new_status in {ChatMemberStatus.KICKED, ChatMemberStatus.LEFT, ChatMemberStatus.RESTRICTED}:
        await user_mod.db.set_inactive(update.chat.id)


async def remove_reply_keyboard(message: types.Message):
    await message.reply(text=bm.keyboard_removed(), reply_markup=types.ReplyKeyboardRemove())


async def stats_command(message: types.Message):
    period = "Week"
    mode = "total"
    try:
        chart_bytes, caption = await user_mod._render_stats(period, mode)
        await _send_stats_photo(message, period, mode, chart_bytes, caption)
    except Exception:
        await message.answer(bm.stats_temporarily_unavailable())
        logging.exception("Error handling /stats")


async def switch_stats(call: types.CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 3:
        await call.answer()
        return

    _, period, mode = parts
    await _handle_stats_update(call, period, mode)


async def switch_period(call: types.CallbackQuery):
    period = call.data.split("_")[1]
    await _handle_stats_update(call, period, "total")
