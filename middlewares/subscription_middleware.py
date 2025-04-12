import re
from aiogram import BaseMiddleware
from aiogram.types import (
    TelegramObject, User, Message, CallbackQuery, InlineQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.methods import GetChatMember
from aiogram.exceptions import TelegramAPIError
from typing import Callable, Awaitable, Dict, Any

from config import CHANNEL_USERNAME


# Допустимі URL-патерни
ALLOWED_LINKS = [
    r"(https?://(www\.)?instagram\.com/\S+)",
    r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)",
    r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)",
    r"(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+"
]


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user: User | None = None
        message: Message | None = event if isinstance(event, Message) else None

        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user
        elif isinstance(event, InlineQuery):
            user = event.from_user

        if not user:
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.data == 'check_subscription':
            if await self._is_subscribed(user.id, data):
                await event.message.edit_text("✅ You are subscribed! You may now use the bot.")
                return await handler(event, data)
            else:
                await event.answer("❌ You're still not subscribed.")
                return

        is_subscribed = await self._is_subscribed(user.id, data)

        # Якщо це група, користувач не підписаний і не відправляє дозволене посилання — ігноруємо
        if isinstance(message, Message) and message.chat.type in ("group", "supergroup") and not is_subscribed:
            if not self._contains_allowed_link(message.text or ""):
                return  # нічого не робимо
            # Якщо містить посилання — дозволяємо

        # Якщо не підписаний в особистому чаті або інші випадки — показуємо повідомлення
        if not is_subscribed:
            await self._send_subscription_prompt(event, data)
            return

        return await handler(event, data)

    def _contains_allowed_link(self, text: str) -> bool:
        return any(re.search(pattern, text) for pattern in ALLOWED_LINKS)

    async def _is_subscribed(self, user_id: int, data: Dict[str, Any]) -> bool:
        try:
            chat_member = await GetChatMember(chat_id=CHANNEL_USERNAME, user_id=user_id).as_(data['bot'])
            return chat_member.status in ["member", "administrator", "creator"]
        except TelegramAPIError:
            return False

    async def _send_subscription_prompt(self, event: TelegramObject, data: Dict[str, Any]):
        text = (
            "🚫 To use this bot, you must subscribe to our channel first.\n"
            f"👉 {CHANNEL_USERNAME}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Go to Channel", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
            [InlineKeyboardButton(text="✅ Check Subscription", callback_data='check_subscription')]
        ])

        if isinstance(event, Message):
            await event.answer(text, reply_markup=keyboard)
        elif isinstance(event, CallbackQuery):
            await event.message.answer(text, reply_markup=keyboard)
        elif isinstance(event, InlineQuery):
            await data['bot'].answer_inline_query(
                inline_query_id=event.id,
                results=[],
                switch_pm_text="Subscribe to the channel to use the bot",
                switch_pm_parameter="subscribe"
            )
