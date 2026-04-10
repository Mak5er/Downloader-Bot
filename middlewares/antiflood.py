from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, InlineQuery, Message

from config import (
    ANTIFLOOD_CALLBACK_LIMIT,
    ANTIFLOOD_CALLBACK_WINDOW_SECONDS,
    ANTIFLOOD_COOLDOWN_SECONDS,
    ANTIFLOOD_GLOBAL_LIMIT,
    ANTIFLOOD_GLOBAL_WINDOW_SECONDS,
    ANTIFLOOD_INLINE_LIMIT,
    ANTIFLOOD_INLINE_WINDOW_SECONDS,
    ANTIFLOOD_MAX_TRACKED_USERS,
    ANTIFLOOD_MESSAGE_LIMIT,
    ANTIFLOOD_MESSAGE_WINDOW_SECONDS,
    ANTIFLOOD_USER_TTL_SECONDS,
)
from services.logger import logger as logging

logging = logging.bind(service="antiflood")

_FLOOD_MESSAGE = "Too many requests. Please slow down for a few seconds."


@dataclass(frozen=True, slots=True)
class _RateLimitRule:
    limit: int
    window_seconds: float


@dataclass(slots=True)
class _UserFloodState:
    events: Deque[tuple[float, str]] = field(default_factory=deque)
    blocked_until: float = 0.0
    last_seen: float = 0.0
    last_message_notice_at: float = -1.0


@dataclass(frozen=True, slots=True)
class _FloodScopeKey:
    user_id: int
    chat_id: Optional[int]


class AntifloodMiddleware(BaseMiddleware):
    def __init__(
        self,
        *,
        max_messages: int = ANTIFLOOD_MESSAGE_LIMIT,
        message_window_seconds: float = ANTIFLOOD_MESSAGE_WINDOW_SECONDS,
        max_callbacks: int = ANTIFLOOD_CALLBACK_LIMIT,
        callback_window_seconds: float = ANTIFLOOD_CALLBACK_WINDOW_SECONDS,
        max_inline_queries: int = ANTIFLOOD_INLINE_LIMIT,
        inline_window_seconds: float = ANTIFLOOD_INLINE_WINDOW_SECONDS,
        max_events: int = ANTIFLOOD_GLOBAL_LIMIT,
        event_window_seconds: float = ANTIFLOOD_GLOBAL_WINDOW_SECONDS,
        cooldown_seconds: float = ANTIFLOOD_COOLDOWN_SECONDS,
        user_ttl_seconds: float = ANTIFLOOD_USER_TTL_SECONDS,
        max_tracked_users: int = ANTIFLOOD_MAX_TRACKED_USERS,
        cleanup_every: int = 256,
        message_notice_cooldown_seconds: float = 5.0,
    ):
        super().__init__()
        self._rules = {
            "message": _RateLimitRule(max(1, int(max_messages)), max(0.1, float(message_window_seconds))),
            "callback": _RateLimitRule(max(1, int(max_callbacks)), max(0.1, float(callback_window_seconds))),
            "inline": _RateLimitRule(max(1, int(max_inline_queries)), max(0.1, float(inline_window_seconds))),
        }
        self._global_rule = _RateLimitRule(max(1, int(max_events)), max(0.1, float(event_window_seconds)))
        self._cooldown_seconds = max(0.1, float(cooldown_seconds))
        self._user_ttl_seconds = max(self._cooldown_seconds, float(user_ttl_seconds))
        self._max_tracked_users = max(1, int(max_tracked_users))
        self._cleanup_every = max(1, int(cleanup_every))
        self._message_notice_cooldown_seconds = max(0.0, float(message_notice_cooldown_seconds))
        self._max_window_seconds = max(
            self._global_rule.window_seconds,
            *(rule.window_seconds for rule in self._rules.values()),
        )
        self._users: "OrderedDict[_FloodScopeKey, _UserFloodState]" = OrderedDict()
        self._events_since_cleanup = 0

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        event_kind = self._resolve_event_kind(event)
        scope_key = self._resolve_scope_key(event, event_kind)
        if event_kind is None or scope_key is None:
            return await handler(event, data)

        now = time.monotonic()
        state = self._get_or_create_state(scope_key, now)
        self._prune_user_events(state, now)

        if state.blocked_until > now:
            await self._notify_flood_block(event, state, now)
            self._maybe_cleanup(now)
            return None

        if self._is_limited(state, event_kind, now):
            state.blocked_until = now + self._cooldown_seconds
            logging.warning(
                "Flood limit triggered: user_id=%s chat_id=%s kind=%s blocked_for=%.2fs tracked_events=%s",
                scope_key.user_id,
                scope_key.chat_id,
                event_kind,
                self._cooldown_seconds,
                len(state.events),
            )
            await self._notify_flood_block(event, state, now)
            self._maybe_cleanup(now)
            return None

        state.events.append((now, event_kind))
        state.last_seen = now
        self._maybe_cleanup(now)
        return await handler(event, data)

    def _get_or_create_state(self, scope_key: _FloodScopeKey, now: float) -> _UserFloodState:
        state = self._users.get(scope_key)
        if state is None:
            state = _UserFloodState(last_seen=now)
            self._users[scope_key] = state
        else:
            state.last_seen = now
            self._users.move_to_end(scope_key)

        overflow = len(self._users) - self._max_tracked_users
        while overflow > 0:
            self._users.popitem(last=False)
            overflow -= 1

        return state

    def _prune_user_events(self, state: _UserFloodState, now: float) -> None:
        cutoff = now - self._max_window_seconds
        while state.events and state.events[0][0] <= cutoff:
            state.events.popleft()

    def _is_limited(self, state: _UserFloodState, event_kind: str, now: float) -> bool:
        if self._count_events(state.events, now, self._global_rule.window_seconds) >= self._global_rule.limit:
            return True

        rule = self._rules[event_kind]
        return self._count_events(state.events, now, rule.window_seconds, event_kind=event_kind) >= rule.limit

    @staticmethod
    def _count_events(
        events: Deque[tuple[float, str]],
        now: float,
        window_seconds: float,
        *,
        event_kind: Optional[str] = None,
    ) -> int:
        cutoff = now - window_seconds
        total = 0
        for timestamp, stored_kind in reversed(events):
            if timestamp <= cutoff:
                break
            if event_kind is None or stored_kind == event_kind:
                total += 1
        return total

    def _maybe_cleanup(self, now: float) -> None:
        self._events_since_cleanup += 1
        if self._events_since_cleanup < self._cleanup_every and len(self._users) <= self._max_tracked_users:
            return

        self._events_since_cleanup = 0
        stale_cutoff = now - self._user_ttl_seconds
        stale_scope_keys = [
            scope_key
            for scope_key, state in self._users.items()
            if state.last_seen <= stale_cutoff and state.blocked_until <= now
        ]
        for scope_key in stale_scope_keys:
            self._users.pop(scope_key, None)

        overflow = len(self._users) - self._max_tracked_users
        while overflow > 0:
            self._users.popitem(last=False)
            overflow -= 1

    async def _notify_flood_block(self, event: Any, state: _UserFloodState, now: float) -> None:
        try:
            if self._resolve_event_kind(event) == "callback":
                await event.answer(_FLOOD_MESSAGE, show_alert=False)
                return

            if self._resolve_event_kind(event) == "inline":
                await event.answer([], cache_time=1, is_personal=True)
                return

            if self._resolve_event_kind(event) == "message" and self._is_private_chat(event):
                if state.last_message_notice_at >= 0 and now - state.last_message_notice_at < self._message_notice_cooldown_seconds:
                    return
                state.last_message_notice_at = now
                await event.answer(_FLOOD_MESSAGE)
        except Exception as exc:
            logging.debug("Failed to notify flood block: error=%s", exc)

    @staticmethod
    def _resolve_user_id(event: Any) -> Optional[int]:
        from_user = getattr(event, "from_user", None)
        user_id = getattr(from_user, "id", None)
        if user_id is None:
            return None
        return int(user_id)

    @classmethod
    def _resolve_scope_key(cls, event: Any, event_kind: Optional[str]) -> Optional[_FloodScopeKey]:
        if event_kind is None:
            return None

        user_id = cls._resolve_user_id(event)
        if user_id is None:
            return None

        chat_id = cls._resolve_chat_id(event) if event_kind in {"message", "callback"} else None
        return _FloodScopeKey(user_id=user_id, chat_id=chat_id)

    @staticmethod
    def _resolve_chat_id(event: Any) -> Optional[int]:
        chat = getattr(event, "chat", None)
        if chat is None:
            message = getattr(event, "message", None)
            chat = getattr(message, "chat", None)

        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            return None
        return int(chat_id)

    @staticmethod
    def _resolve_event_kind(event: Any) -> Optional[str]:
        if isinstance(event, CallbackQuery):
            return "callback"
        if hasattr(event, "data") and callable(getattr(event, "answer", None)):
            return "callback"
        if isinstance(event, InlineQuery):
            return "inline"
        if hasattr(event, "query") and callable(getattr(event, "answer", None)):
            return "inline"
        if isinstance(event, Message):
            return "message"
        if getattr(event, "chat", None) is not None:
            return "message"
        return None

    @staticmethod
    def _is_private_chat(event: Message) -> bool:
        chat_type = getattr(getattr(event, "chat", None), "type", None)
        if isinstance(chat_type, ChatType):
            return chat_type == ChatType.PRIVATE
        return str(chat_type or "").lower() == "private"
