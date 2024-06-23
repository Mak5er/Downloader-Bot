from typing import Union

from aiogram.filters import BaseFilter
from aiogram.types import Message

from config import ADMINS_UID

class IsBotAdmin(BaseFilter):
    """Перевіряє чи є користувач адміном бота"""
    async def __call__(self, msg: Message) -> bool:
        return msg.from_user.id in ADMINS_UID
