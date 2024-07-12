import asyncio

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineQuery

from main import db


class UserBannedMiddleware(BaseMiddleware):

    async def on_pre_process_message(self, message: Message, data: dict):
        try:
            user_status = await db.status(message.from_user.id)
        except:
            user_status = 'active'
        if user_status == 'ban':
            if message.chat.type == 'private':
                await message.answer(('You are banned please contact to @mak5er for more information!'),
                                     parse_mode='HTML')
            raise asyncio.CancelledError

    async def on_pre_process_callback_query(self, callback_query: CallbackQuery, data: dict):
        try:
            user_status = await db.status(callback_query.from_user.id)
        except:
            user_status = 'active'
        if user_status == 'ban':
            await callback_query.answer(('You are banned please contact to @mak5er for more information!'),
                                        show_alert=True)
            raise asyncio.CancelledError

    async def on_pre_process_inline_query(self, inline_query: InlineQuery, data: dict):
        try:
            user_status = await db.status(inline_query.from_user.id)
        except:
            user_status = 'active'
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
