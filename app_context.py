from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from aiogram import Bot

from services.db import DataBase


AnalyticsSender = Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class AppContext:
    bot: Bot
    db: DataBase
    send_analytics: AnalyticsSender


_context: Optional[AppContext] = None


def set_app_context(*, bot: Bot, db: DataBase, send_analytics: AnalyticsSender) -> None:
    global _context
    _context = AppContext(
        bot=bot,
        db=db,
        send_analytics=send_analytics,
    )


def get_app_context() -> AppContext:
    if _context is None:
        raise RuntimeError("Application context has not been initialized yet.")
    return _context


class _ContextObjectProxy:
    def __init__(self, attribute_name: str) -> None:
        object.__setattr__(self, "_attribute_name", attribute_name)

    def _resolve(self) -> Any:
        return getattr(get_app_context(), self._attribute_name)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._resolve(), item)

    def __setattr__(self, key: str, value: Any) -> None:
        if key == "_attribute_name":
            object.__setattr__(self, key, value)
            return
        setattr(self._resolve(), key, value)

    def __delattr__(self, item: str) -> None:
        if item == "_attribute_name":
            raise AttributeError(item)
        delattr(self._resolve(), item)


class _ContextCallableProxy:
    def __init__(self, attribute_name: str) -> None:
        self._attribute_name = attribute_name

    def _resolve(self) -> AnalyticsSender:
        return getattr(get_app_context(), self._attribute_name)

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return await self._resolve()(*args, **kwargs)


bot = _ContextObjectProxy("bot")
db = _ContextObjectProxy("db")
send_analytics = _ContextCallableProxy("send_analytics")
