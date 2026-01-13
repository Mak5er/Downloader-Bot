from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from aiogram.types import Message


@dataclass
class PendingRequest:
    message: Message
    notice_chat_id: int
    notice_message_id: int


_pending: dict[int, PendingRequest] = {}


def set_pending(user_id: int, request: PendingRequest) -> None:
    _pending[user_id] = request


def get_pending(user_id: int) -> Optional[PendingRequest]:
    return _pending.get(user_id)


def pop_pending(user_id: int) -> Optional[PendingRequest]:
    return _pending.pop(user_id, None)
