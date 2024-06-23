from typing import Union

from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery


class ChatTypeF(BaseFilter):
    def __init__(self, chat_type: Union[str, list]):
        self.chat_type = chat_type

    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        event_chat_type = event.chat.type if isinstance(event, Message) else event.message.chat.type
        if isinstance(self.chat_type, str):
            return event_chat_type == self.chat_type
        else:
            return event_chat_type in self.chat_type
