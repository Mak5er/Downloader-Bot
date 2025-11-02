from typing import Any, Awaitable, Callable, Dict
from collections import deque
import time

from aiogram import BaseMiddleware
from aiogram.types import Message

class AntifloodMiddleware(BaseMiddleware):
    def __init__(self, max_messages: int = 10, per_seconds: int = 1):
        super().__init__()
        self.max_messages = max_messages
        self.per_seconds = per_seconds
        self.users: Dict[int, deque] = {}  # chat_id -> deque([timestamps])

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        chat_id = event.from_user.id
        now = time.time()

        if chat_id not in self.users:
            self.users[chat_id] = deque()

        timestamps = self.users[chat_id]

        while timestamps and now - timestamps[0] > self.per_seconds:
            timestamps.popleft()

        if len(timestamps) >= self.max_messages:
            return

        timestamps.append(now)

        return await handler(event, data)
