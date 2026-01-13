import asyncio
import os
from typing import Optional

from pathlib import Path

from aiogram import Bot, types
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramAPIError

import messages as bm
from log.logger import logger as logging

DELETE_WARNING_TEXT = (
    "I can't delete the link in this chat because I don't have enough permissions."
)

_bot_avatar_file_id: Optional[str] = None
_bot_avatar_path: Optional[str] = None


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


async def get_bot_avatar_file_id(bot: Bot) -> Optional[str]:
    """Return cached bot avatar file_id if available."""
    global _bot_avatar_file_id
    if _bot_avatar_file_id:
        return _bot_avatar_file_id

    try:
        bot_data = await bot.get_me()
        photos = await bot.get_user_profile_photos(bot_data.id, limit=1)
        if photos.total_count and photos.photos:
            _bot_avatar_file_id = photos.photos[0][-1].file_id
            return _bot_avatar_file_id
    except Exception as exc:
        logging.debug("Failed to fetch bot avatar: error=%s", exc)
    return None


async def get_bot_avatar_thumbnail(bot: Bot) -> Optional[FSInputFile]:
    """Return bot avatar as InputFile for thumbnail uploads."""
    global _bot_avatar_path
    if _bot_avatar_path and Path(_bot_avatar_path).exists():
        return FSInputFile(_bot_avatar_path)

    try:
        bot_data = await bot.get_me()
        photos = await bot.get_user_profile_photos(bot_data.id, limit=1)
        if not photos.total_count or not photos.photos:
            return None

        avatar_dir = Path("downloads")
        avatar_dir.mkdir(parents=True, exist_ok=True)
        avatar_path = avatar_dir / "bot_avatar.jpg"
        file_id = photos.photos[0][-1].file_id
        await bot.download(file_id, destination=avatar_path)
        _bot_avatar_path = str(avatar_path)
        return FSInputFile(_bot_avatar_path)
    except Exception as exc:
        logging.debug("Failed to fetch bot avatar thumbnail: error=%s", exc)
        return None


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
