from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound
from aiogram.types import Message

import keyboards as kb
import messages as bm
from services.links.detection import detect_supported_service
from services.runtime.pending_requests import PendingRequest, get_pending, set_pending

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
        if not detect_supported_service(text):
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
                    text=text,
                    notice_chat_id=notice.chat.id,
                    notice_message_id=notice.message_id,
                    source_chat_id=getattr(event.chat, "id", None),
                    source_message_id=getattr(event, "message_id", None),
                ),
            )
            return None
