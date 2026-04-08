import asyncio
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.types import FSInputFile

from log.logger import logger as logging


_bot_avatar_file_id: Optional[str] = None
_bot_avatar_path: Optional[str] = None
_bot_username: Optional[str] = None
_bot_id: Optional[int] = None
_bot_identity_lock = asyncio.Lock()


async def get_bot_url(bot: Bot) -> str:
    global _bot_username
    if _bot_username is None:
        await _ensure_bot_identity(bot)
    return f"t.me/{_bot_username}"


async def _get_bot_id(bot: Bot) -> int:
    global _bot_id
    if _bot_id is None:
        await _ensure_bot_identity(bot)
    return _bot_id


async def _ensure_bot_identity(bot: Bot) -> None:
    global _bot_username, _bot_id
    if _bot_username is not None and _bot_id is not None:
        return

    async with _bot_identity_lock:
        if _bot_username is not None and _bot_id is not None:
            return

        bot_data = await bot.get_me()
        _bot_username = bot_data.username or ""
        _bot_id = bot_data.id


async def get_bot_avatar_file_id(bot: Bot) -> Optional[str]:
    global _bot_avatar_file_id
    if _bot_avatar_file_id:
        return _bot_avatar_file_id

    try:
        bot_id = await _get_bot_id(bot)
        photos = await bot.get_user_profile_photos(bot_id, limit=1)
        if photos.total_count and photos.photos:
            _bot_avatar_file_id = photos.photos[0][-1].file_id
            return _bot_avatar_file_id
    except Exception as exc:
        logging.debug("Failed to fetch bot avatar: error=%s", exc)
    return None


async def get_bot_avatar_thumbnail(bot: Bot) -> Optional[FSInputFile]:
    global _bot_avatar_path
    if _bot_avatar_path and Path(_bot_avatar_path).exists():
        return FSInputFile(_bot_avatar_path)

    try:
        bot_id = await _get_bot_id(bot)
        photos = await bot.get_user_profile_photos(bot_id, limit=1)
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
