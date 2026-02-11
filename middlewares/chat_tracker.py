from typing import Any, Awaitable, Callable, Dict, Optional
import time

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Chat, Message, User

from main import db
from services.db import DataBase


class ChatTrackerMiddleware(BaseMiddleware):
    def __init__(self, database: Optional[DataBase] = None):
        super().__init__()
        self._db = database or db
        self._user_touch_cache: dict[int, tuple[float, tuple[str, Optional[str], str, Optional[str]]]] = {}
        self._group_touch_cache: dict[int, tuple[float, tuple[str, Optional[str], Optional[str]]]] = {}
        self._touch_ttl_seconds = 90.0

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            await self._process_message(event)

        return await handler(event, data)

    async def _process_message(self, message: Message) -> None:
        chat = message.chat
        user = message.from_user

        if not chat:
            return

        chat_type_value = self._resolve_chat_type(chat.type)

        if chat_type_value == "private":
            if user and not user.is_bot:
                await self._ensure_user(user, chat_type_value)
        else:
            await self._ensure_group(chat)
            if user and not user.is_bot:
                await self._ensure_user(user, "private")

    @staticmethod
    def _resolve_chat_type(chat_type: ChatType | str | None) -> str:
        if isinstance(chat_type, ChatType):
            return chat_type.value
        if chat_type:
            return str(chat_type)
        return "private"

    async def _ensure_user(self, user: User, chat_type_value: str) -> None:
        user_id = user.id
        full_name = user.full_name
        username = user.username
        language = getattr(user, "language_code", None)
        signature = (full_name, username, chat_type_value, language)

        now = time.monotonic()
        cached = self._user_touch_cache.get(user_id)
        if cached and now - cached[0] <= self._touch_ttl_seconds and cached[1] == signature:
            return

        if await self._db.user_exist(user_id):
            await self._db.user_update_name(user_id, full_name, username)
        else:
            await self._db.add_user(
                user_id=user_id,
                user_name=full_name,
                user_username=username,
                chat_type=chat_type_value,
                language=language,
                status="active",
            )

        await self._db.set_active(user_id)
        self._user_touch_cache[user_id] = (now, signature)

    async def _ensure_group(self, chat: Chat) -> None:
        chat_id = chat.id
        chat_name = chat.title or getattr(chat, "full_name", None)

        if not chat_name:
            first_name = getattr(chat, "first_name", None)
            last_name = getattr(chat, "last_name", None)
            name_parts = [part for part in (first_name, last_name) if part]
            if name_parts:
                chat_name = " ".join(name_parts)

        if not chat_name:
            chat_name = f"Chat {chat_id}"

        username = getattr(chat, "username", None)
        language = getattr(chat, "language_code", None)
        signature = (chat_name, username, language)

        now = time.monotonic()
        cached = self._group_touch_cache.get(chat_id)
        if cached and now - cached[0] <= self._touch_ttl_seconds and cached[1] == signature:
            return

        await self._db.upsert_chat(
            user_id=chat_id,
            user_name=chat_name,
            user_username=username,
            chat_type="public",
            language=language,
            status="active",
        )
        await self._db.set_active(chat_id)
        self._group_touch_cache[chat_id] = (now, signature)
