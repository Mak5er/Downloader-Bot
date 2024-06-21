from typing import Any, Awaitable, Callable, Dict, Union

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import Message, CallbackQuery
from cachetools import TTLCache

import database as db

class AntifloodMiddleware(BaseMiddleware):
    caches = {
        "another_flag": TTLCache(maxsize=10_000, ttl=2),
        "default": TTLCache(maxsize=10_000, ttl=1)
    }

    async def __call__(
            self,
            handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
            event: Message,
            data: Dict[str, Any],
    ) -> Any:
        await db.users.add(event.from_user.id)
        throttling_key = get_flag(handler=data, name="throttling_key", default="default")
        if throttling_key is not None and throttling_key in self.caches:
            chat_id = event.from_user.id
            if chat_id in self.caches[throttling_key]:
                return
            else:
                self.caches[throttling_key][chat_id] = None
        return await handler(event, data)
