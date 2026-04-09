from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiogram import Bot

from services.storage.db import DataBase


AnalyticsSender = Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class HandlerDependencies:
    bot: Bot
    db: DataBase
    send_analytics: AnalyticsSender


def build_handler_dependencies(*, bot: Bot, db: DataBase, send_analytics: AnalyticsSender) -> HandlerDependencies:
    return HandlerDependencies(
        bot=bot,
        db=db,
        send_analytics=send_analytics,
    )
