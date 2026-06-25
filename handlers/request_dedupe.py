from __future__ import annotations

from dataclasses import dataclass

from aiogram import types
from aiogram.exceptions import TelegramBadRequest

import messages as bm
from services.runtime.request_dedupe import claim_request, finish_request


@dataclass(slots=True)
class MessageRequestLease:
    user_id: int
    chat_id: int | None
    scope_id: str | None
    service: str
    url: str
    _successful: bool = False
    _finished: bool = False

    def mark_success(self) -> None:
        self._successful = True

    def finish(self) -> None:
        if self._finished:
            return
        finish_request(
            self.user_id,
            self.chat_id,
            self.service,
            self.url,
            success=self._successful,
            scope_id=self.scope_id,
        )
        self._finished = True


async def claim_message_request(
    message: types.Message,
    *,
    service: str,
    url: str,
) -> MessageRequestLease | None:
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    scope_id = getattr(message, "business_connection_id", None)
    status = claim_request(message.from_user.id, chat_id, service, url, scope_id=scope_id)
    if status == "active":
        try:
            await message.reply(bm.duplicate_link_processing())
        except TelegramBadRequest:
            pass
        return None
    if status == "recent":
        try:
            await message.reply(bm.duplicate_link_recently_processed())
        except TelegramBadRequest:
            pass
        return None
    return MessageRequestLease(
        user_id=message.from_user.id,
        chat_id=chat_id,
        scope_id=str(scope_id) if scope_id is not None else None,
        service=service,
        url=url,
    )
