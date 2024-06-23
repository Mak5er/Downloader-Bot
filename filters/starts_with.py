from typing import Union

from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery


class StartsWith(BaseFilter):
    def __init__(self, text: str):
        self.text = text

    async def __call__(self, msg: Union[Message, CallbackQuery]) -> bool:
        if isinstance(msg, Message):
            text = msg.text
        else:
            text = msg.data
        return text.startswith(self.text)