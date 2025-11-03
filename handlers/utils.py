import asyncio
import os
from typing import Optional

from aiogram import Bot, types
from aiogram.exceptions import TelegramAPIError

from log.logger import logger as logging

DELETE_WARNING_TEXT = (
    "I can't delete the link in this chat because I don't have enough permissions."
)


async def maybe_delete_user_message(message: types.Message, delete_flag) -> bool:
    if str(delete_flag).lower() != "on":
        return False

    try:
        await message.delete()
        return True
    except TelegramAPIError:
        await message.answer(DELETE_WARNING_TEXT)
        return False


async def get_bot_url(bot: Bot) -> str:
    bot_data = await bot.get_me()
    return f"t.me/{bot_data.username}"


async def remove_file(path: Optional[str]) -> None:
    if not path:
        return

    try:
        exists = await asyncio.to_thread(os.path.exists, path)
        if exists:
            await asyncio.to_thread(os.remove, path)
            logging.debug("Removed temporary file: path=%s", path)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logging.error("Error removing file: path=%s error=%s", path, exc)


async def send_chat_action_if_needed(bot: Bot, chat_id: int, action: str, business_id: Optional[int]) -> None:
    if business_id is None:
        await bot.send_chat_action(chat_id, action)
