import asyncio
import time

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineQuery

from main import db


class UserBannedMiddleware(BaseMiddleware):
    def __init__(self, ttl_seconds: float = 12.0):
        super().__init__()
        self._ttl_seconds = ttl_seconds
        self._status_cache: dict[int, tuple[float, str]] = {}

    async def _get_status(self, user_id: int) -> str:
        now = time.monotonic()
        cached = self._status_cache.get(user_id)
        if cached and now - cached[0] <= self._ttl_seconds:
            return cached[1]

        try:
            user_status = await db.status(user_id)
        except Exception:
            user_status = 'active'

        status_value = user_status or "active"
        self._status_cache[user_id] = (now, status_value)
        return status_value

    async def on_pre_process_message(self, message: Message, data: dict):
        user_status = await self._get_status(message.from_user.id)
        if user_status == 'ban':
            if message.chat.type == 'private':
                await message.answer(('You are banned please contact to @mak5er for more information!'),
                                     parse_mode='HTML')
            raise asyncio.CancelledError

    async def on_pre_process_callback_query(self, callback_query: CallbackQuery, data: dict):
        user_status = await self._get_status(callback_query.from_user.id)
        if user_status == 'ban':
            await callback_query.answer(('You are banned please contact to @mak5er for more information!'),
                                        show_alert=True)
            raise asyncio.CancelledError

    async def on_pre_process_inline_query(self, inline_query: InlineQuery, data: dict):
        user_status = await self._get_status(inline_query.from_user.id)
        if user_status == 'ban':
            raise asyncio.CancelledError

    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            await self.on_pre_process_message(event, data)
        elif isinstance(event, CallbackQuery):
            await self.on_pre_process_callback_query(event, data)
        elif isinstance(event, InlineQuery):
            await self.on_pre_process_inline_query(event, data)
        return await handler(event, data)
