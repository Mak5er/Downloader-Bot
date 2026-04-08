from __future__ import annotations

from dataclasses import dataclass

from aiogram import types

import messages as bm
from services.runtime.request_dedupe import claim_request, finish_request


@dataclass(slots=True)
class MessageRequestLease:
    user_id: int
    service: str
    url: str
    _successful: bool = False
    _finished: bool = False

    def mark_success(self) -> None:
        self._successful = True

    def finish(self) -> None:
        if self._finished:
            return
        finish_request(self.user_id, self.service, self.url, success=self._successful)
        self._finished = True


async def claim_message_request(
    message: types.Message,
    *,
    service: str,
    url: str,
) -> MessageRequestLease | None:
    status = claim_request(message.from_user.id, service, url)
    if status == "active":
        await message.reply(bm.duplicate_link_processing())
        return None
    if status == "recent":
        await message.reply(bm.duplicate_link_recently_processed())
        return None
    return MessageRequestLease(
        user_id=message.from_user.id,
        service=service,
        url=url,
    )
