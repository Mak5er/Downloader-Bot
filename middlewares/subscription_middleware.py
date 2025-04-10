from aiogram import BaseMiddleware
from aiogram.types import (
    TelegramObject, User, Message, CallbackQuery, InlineQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.methods import GetChatMember
from aiogram.exceptions import TelegramAPIError
from typing import Callable, Awaitable, Dict, Any

from config import CHANNEL_USERNAME


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
            self,
            handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
            event: TelegramObject,
            data: Dict[str, Any]
    ) -> Any:
        user: User | None = None

        # Extract user from any type of interaction
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user
        elif isinstance(event, InlineQuery):
            user = event.from_user

        # If user not found, allow to pass through
        if not user:
            return await handler(event, data)

        # Special case: recheck subscription on "Check subscription" button
        if isinstance(event, CallbackQuery) and event.data == 'check_subscription':
            if await self._is_subscribed(user.id, data):
                await event.message.edit_text("✅ You are subscribed! You may now use the bot.")
                return await handler(event, data)
            else:
                await event.answer("❌ You're still not subscribed.")
                return

        # General check for all interactions
        if not await self._is_subscribed(user.id, data):
            await self._send_subscription_prompt(event, data)
            return

        return await handler(event, data)

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
