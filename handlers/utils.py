import asyncio
import os
from typing import Optional

from aiogram import Bot, types
from aiogram.exceptions import TelegramAPIError

import messages as bm
from log.logger import logger as logging

DELETE_WARNING_TEXT = (
    "I can't delete the link in this chat because I don't have enough permissions."
)


def get_message_text(message: types.Message) -> str:
    """Return the message text or caption, falling back to empty string."""
    return message.text or message.caption or ""


async def react_to_message(
        message: types.Message,
        emoji: str,
        *,
        business_id: Optional[int] = None,
        skip_if_business: bool = True,
) -> None:
    """Send a reaction to a message, optionally skipping business chats."""
    if skip_if_business:
        resolved_business_id = business_id
        if resolved_business_id is None:
            resolved_business_id = getattr(message, "business_connection_id", None)
        if resolved_business_id is not None:
            return

    try:
        await message.react([types.ReactionTypeEmoji(emoji=emoji)])
    except Exception as exc:
        logging.debug(
            "Failed to set reaction: message_id=%s emoji=%s error=%s",
            getattr(message, "message_id", None),
            emoji,
            exc,
        )


async def _send_with_reaction(
        message: types.Message,
        text: str,
        *,
        emoji: Optional[str] = None,
        business_id: Optional[int] = None,
        skip_if_business: bool = True,
        method: str = "reply",
        **kwargs,
) -> None:
    if emoji:
        await react_to_message(
            message,
            emoji,
            business_id=business_id,
            skip_if_business=skip_if_business,
        )

    responder = getattr(message, method, None)
    if not responder:
        raise AttributeError(f"Message object has no method '{method}'")

    await responder(text, **kwargs)


async def handle_download_error(
        message: types.Message,
        *,
        text: Optional[str] = None,
        emoji: str = "👎",
        business_id: Optional[int] = None,
        skip_if_business: bool = True,
        method: str = "reply",
        **kwargs,
) -> None:
    """Notify user about a failed download with a consistent reaction and message."""
    await _send_with_reaction(
        message,
        text or bm.something_went_wrong(),
        emoji=emoji,
        business_id=business_id,
        skip_if_business=skip_if_business,
        method=method,
        **kwargs,
    )


async def handle_video_too_large(
        message: types.Message,
        *,
        business_id: Optional[int] = None,
        skip_if_business: bool = True,
        method: str = "reply",
        **kwargs,
) -> None:
    """Inform the user that the requested media exceeds Telegram limits."""
    await _send_with_reaction(
        message,
        bm.video_too_large(),
        emoji="👎",
        business_id=business_id,
        skip_if_business=skip_if_business,
        method=method,
        **kwargs,
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
