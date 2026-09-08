from typing import Any, Awaitable, Callable, Dict, Optional
import time
import uuid

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import Chat, Message, User

from app_context import db
from services.logger import logger as logging
from services.storage.db import DataBase


class ChatTrackerMiddleware(BaseMiddleware):
    def __init__(self, database: Optional[DataBase] = None, bot: Optional[Bot] = None):
        super().__init__()
        self._db = database or db
        self._bot = bot
        self._user_touch_cache: dict[int, tuple[float, tuple[str, Optional[str], Optional[str], bool]]] = {}
        self._group_touch_cache: dict[int, tuple[float, tuple[Any, ...]]] = {}
        self._member_count_cache: dict[int, tuple[float, int]] = {}
        self._member_touch_cache: dict[tuple[int, int], float] = {}
        self._touch_ttl_seconds = 90.0
        self._member_count_ttl_seconds = 3600.0  # 1 hour

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        bot = data.get("bot") or getattr(event, "bot", None) or self._bot
        if isinstance(event, Message):
            await self._process_message(event, bot=bot)

        request_id = str(uuid.uuid4())[:12]
        user_id = getattr(getattr(event, "from_user", None), "id", None)
        with logging.context(request_id=request_id, flow="handler", user_id=user_id):
            return await handler(event, data)

    async def _process_message(
        self,
        message_or_chat: Any,
        user_or_bot: Any = None,
        bot: Optional[Bot] = None,
    ) -> None:
        if hasattr(message_or_chat, "chat"):
            chat = message_or_chat.chat
            user = getattr(message_or_chat, "from_user", None)
            resolved_bot = (
                user_or_bot
                if isinstance(user_or_bot, Bot)
                else (bot or getattr(message_or_chat, "bot", None) or self._bot)
            )
        else:
            chat = message_or_chat
            user = user_or_bot
            resolved_bot = bot or getattr(chat, "bot", None) or self._bot

        if not chat:
            return

        # Check if group was upgraded to supergroup
        migrate_to = getattr(message_or_chat, "migrate_to_chat_id", None)
        migrate_from = getattr(message_or_chat, "migrate_from_chat_id", None)
        if migrate_to:
            await self._handle_group_migration(
                old_chat_id=chat.id,
                new_chat_id=migrate_to,
                chat=chat,
                bot=resolved_bot,
            )
            return
        if migrate_from:
            await self._handle_group_migration(
                old_chat_id=migrate_from,
                new_chat_id=chat.id,
                chat=chat,
                bot=resolved_bot,
            )

        chat_type_value = self._resolve_chat_type(chat.type)

        if chat_type_value == "private":
            if user and not getattr(user, "is_bot", False):
                await self._ensure_user(user, has_dm=True)
        else:
            raw_thread_id = getattr(message_or_chat, "message_thread_id", None)
            thread_id = raw_thread_id if isinstance(raw_thread_id, int) else None
            await self._ensure_group(chat, bot=resolved_bot, thread_id=thread_id)
            if user and not getattr(user, "is_bot", False):
                await self._ensure_user(user, has_dm=False)
                await self._ensure_group_member(group_id=chat.id, user_id=user.id)

    @staticmethod
    def _resolve_chat_type(chat_type: ChatType | str | None) -> str:
        if isinstance(chat_type, ChatType):
            return chat_type.value
        if chat_type:
            return str(chat_type)
        return "private"

    async def _ensure_user(self, user: User, has_dm: bool | str = False) -> None:
        if isinstance(has_dm, str):
            has_dm_bool = (has_dm == "private")
        else:
            has_dm_bool = bool(has_dm)

        user_id = user.id
        full_name = user.full_name
        username = user.username
        language = getattr(user, "language_code", None)
        signature = (full_name, username, language, has_dm_bool)

        now = time.monotonic()
        cached = self._user_touch_cache.get(user_id)
        if cached:
            cached_time, cached_sig = cached
            cached_has_dm = cached_sig[3]
            # If already marked with has_dm=True, group interaction within TTL skips DB write
            if cached_has_dm and not has_dm_bool:
                if now - cached_time <= self._touch_ttl_seconds and cached_sig[:3] == signature[:3]:
                    return
            elif cached_sig == signature and now - cached_time <= self._touch_ttl_seconds:
                return

        upsert_user_fn = getattr(self._db, "upsert_user", None)
        if callable(upsert_user_fn):
            await upsert_user_fn(
                user_id=user_id,
                user_name=full_name,
                user_username=username,
                chat_type="private",
                language=language,
                has_dm=has_dm_bool,
                status="active" if has_dm_bool else None,
            )
        else:
            await self._db.upsert_chat(
                user_id=user_id,
                user_name=full_name,
                user_username=username,
                chat_type="private",
                language=language,
                status="active",
            )
        self._user_touch_cache[user_id] = (now, signature)

    async def _ensure_group(
        self,
        chat: Chat,
        bot: Optional[Bot] = None,
        thread_id: Optional[int] = None,
    ) -> None:
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
        chat_type_str = self._resolve_chat_type(chat.type)
        signature = (chat_name, username, chat_type_str, thread_id)

        now = time.monotonic()
        cached = self._group_touch_cache.get(chat_id)
        if cached and now - cached[0] <= self._touch_ttl_seconds:
            cached_sig = cached[1]
            if thread_id is None and cached_sig[:3] == signature[:3]:
                return
            if cached_sig == signature:
                return

        # 1-hour TTL member_count cache
        member_count: int = 0
        cached_count = self._member_count_cache.get(chat_id)
        if cached_count and (now - cached_count[0] <= self._member_count_ttl_seconds):
            member_count = cached_count[1]
        elif bot is not None and hasattr(bot, "get_chat_member_count"):
            try:
                member_count = await bot.get_chat_member_count(chat_id)
                self._member_count_cache[chat_id] = (now, member_count)
            except Exception as exc:
                logging.debug("Failed to get member count for chat %s: %s", chat_id, exc)
                if cached_count:
                    member_count = cached_count[1]
        elif cached_count:
            member_count = cached_count[1]

        upsert_group_fn = getattr(self._db, "upsert_group", None)
        if callable(upsert_group_fn):
            group_kwargs = {
                "chat_id": chat_id,
                "title": chat_name,
                "username": username,
                "chat_type": chat_type_str,
                "status": "active",
                "member_count": member_count,
            }
            if thread_id is not None:
                group_kwargs["last_thread_id"] = thread_id

            try:
                await upsert_group_fn(**group_kwargs)
            except TypeError:
                group_kwargs.pop("last_thread_id", None)
                try:
                    await upsert_group_fn(**group_kwargs)
                except TypeError:
                    await upsert_group_fn(
                        group_id=chat_id,
                        title=chat_name,
                        username=username,
                        chat_type=chat_type_str,
                        status="active",
                        member_count=member_count,
                    )
        else:
            language = getattr(chat, "language_code", None)
            await self._db.upsert_chat(
                user_id=chat_id,
                user_name=chat_name,
                user_username=username,
                chat_type="public",
                language=language,
                status="active",
            )
        self._group_touch_cache[chat_id] = (now, signature)

    async def _ensure_group_member(
        self,
        group_id: int | None = None,
        user_id: int = 0,
        chat_id: int | None = None,
    ) -> None:
        actual_group_id = group_id if group_id is not None else chat_id
        if actual_group_id is None or not user_id:
            return

        now = time.monotonic()
        key = (actual_group_id, user_id)
        cached = self._member_touch_cache.get(key)
        if cached and now - cached <= self._touch_ttl_seconds:
            return

        record_member_fn = (
            getattr(self._db, "record_group_member", None)
            or getattr(self._db, "add_group_member", None)
        )
        if callable(record_member_fn):
            await record_member_fn(group_id=actual_group_id, user_id=user_id)
        self._member_touch_cache[key] = now

    async def _handle_group_migration(
        self,
        old_chat_id: int,
        new_chat_id: int,
        chat: Chat,
        bot: Optional[Bot] = None,
    ) -> None:
        logging.info("Chat migrated from %s to %s", old_chat_id, new_chat_id)
        self._group_touch_cache.pop(old_chat_id, None)
        self._member_count_cache.pop(old_chat_id, None)
        for key in list(self._member_touch_cache.keys()):
            if key[0] == old_chat_id:
                self._member_touch_cache.pop(key, None)

        migrate_fn = getattr(self._db, "migrate_group_chat", None)
        if callable(migrate_fn):
            title = chat.title or getattr(chat, "full_name", None)
            username = getattr(chat, "username", None)
            await migrate_fn(old_chat_id, new_chat_id, new_title=title, new_username=username)

        await self._ensure_group(chat, bot=bot)
