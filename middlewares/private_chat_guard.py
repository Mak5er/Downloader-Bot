import re
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound
from aiogram.types import Message

import keyboards as kb
import messages as bm
from services.pending_requests import PendingRequest, get_pending, set_pending

SUPPORTED_LINK_RE = re.compile(
    r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+"
    r"|https?://(www\.)?(twitter|x)\.com/\S+"
    r"|https?://t\.co/\S+"
    r"|https?://(www\.)?instagram\.com/\S+"
    r"|https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)",
    re.IGNORECASE,
)

_bot_username: str | None = None


class PrivateChatGuardMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        if event.chat.type == ChatType.PRIVATE:
            return await handler(event, data)

        if not event.from_user or event.from_user.is_bot:
            return await handler(event, data)

        text = event.text or event.caption or ""
        if not text or not SUPPORTED_LINK_RE.search(text):
            return await handler(event, data)

        bot = data.get("bot")
        if not bot:
            return await handler(event, data)

        try:
            await bot.send_chat_action(chat_id=event.from_user.id, action="typing")
            return await handler(event, data)
        except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest):
            pending = get_pending(event.from_user.id)
            if pending:
                try:
                    await bot.delete_message(pending.notice_chat_id, pending.notice_message_id)
                except Exception:
                    pass

            global _bot_username
            if not _bot_username:
                bot_info = await bot.get_me()
                _bot_username = bot_info.username
            notice = await event.reply(
                bm.dm_start_required(),
                reply_markup=kb.start_private_chat_keyboard(_bot_username),
            )
            set_pending(
                event.from_user.id,
                PendingRequest(
                    message=event,
                    notice_chat_id=notice.chat.id,
                    notice_message_id=notice.message_id,
                ),
            )
            return None
