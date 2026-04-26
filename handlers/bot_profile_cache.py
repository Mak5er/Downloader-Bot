import asyncio
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.types import FSInputFile

from services.logger import logger as logging


_bot_avatar_file_id: Optional[str] = None
_bot_avatar_path: Optional[str] = None
_bot_username: Optional[str] = None
_bot_id: Optional[int] = None
_bot_identity_lock = asyncio.Lock()
_AUDIO_THUMB_MAX_DIMENSION = 320
_AUDIO_THUMB_MAX_BYTES = 200 * 1024
_AUDIO_THUMB_PATH = Path("downloads") / "bot_audio_thumbnail.jpg"


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
    if _bot_avatar_path and _is_usable_thumbnail_file(Path(_bot_avatar_path)):
        return FSInputFile(_bot_avatar_path)
    if _bot_avatar_path:
        Path(_bot_avatar_path).unlink(missing_ok=True)
        _bot_avatar_path = None

    try:
        bot_id = await _get_bot_id(bot)
        photos = await bot.get_user_profile_photos(bot_id, limit=1)
        if not photos.total_count or not photos.photos:
            return None

        avatar_path = _AUDIO_THUMB_PATH
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        file_id = _select_audio_thumbnail_file_id(photos.photos[0])
        if not await _download_bot_avatar_file(bot, file_id, avatar_path):
            return None
        _bot_avatar_path = str(avatar_path)
        return FSInputFile(_bot_avatar_path)
    except Exception as exc:
        logging.debug("Failed to fetch bot avatar thumbnail: error=%s", exc)
        return None


async def _download_bot_avatar_file(bot: Bot, file_id: str, final_path: Path) -> bool:
    temp_path = final_path.with_name(f"{final_path.stem}.download{final_path.suffix}")
    normalized_path = final_path.with_name(f"{final_path.stem}.normalized{final_path.suffix}")

    for path in (temp_path, normalized_path):
        path.unlink(missing_ok=True)

    try:
        with temp_path.open("wb") as destination:
            await bot.download(file_id, destination=destination)
            destination.flush()

        if not _is_usable_thumbnail_file(temp_path):
            logging.warning("Downloaded bot avatar thumbnail is empty or invalid: path=%s", temp_path)
            final_path.unlink(missing_ok=True)
            return False

        if _normalize_audio_thumbnail_file(temp_path, normalized_path):
            final_path.unlink(missing_ok=True)
            normalized_path.replace(final_path)
        else:
            final_path.unlink(missing_ok=True)
            temp_path.replace(final_path)

        return _is_usable_thumbnail_file(final_path)
    finally:
        temp_path.unlink(missing_ok=True)
        normalized_path.unlink(missing_ok=True)


def _select_audio_thumbnail_file_id(photo_sizes) -> str:
    compatible = []
    fallback = []
    for photo in photo_sizes or []:
        width = getattr(photo, "width", 0) or 0
        height = getattr(photo, "height", 0) or 0
        file_size = getattr(photo, "file_size", None)
        area = width * height
        fallback.append((area, photo.file_id))
        if width <= _AUDIO_THUMB_MAX_DIMENSION and height <= _AUDIO_THUMB_MAX_DIMENSION:
            if file_size is None or file_size <= _AUDIO_THUMB_MAX_BYTES:
                compatible.append((area, photo.file_id))

    if compatible:
        return max(compatible, key=lambda item: item[0])[1]
    if fallback:
        return min(fallback, key=lambda item: item[0])[1]
    raise ValueError("bot avatar has no photo sizes")


def _is_usable_thumbnail_file(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def _normalize_audio_thumbnail_file(source_path: Path, output_path: Path) -> bool:
    try:
        from PIL import Image
    except Exception:
        return _normalize_audio_thumbnail_file_with_ffmpeg(source_path, output_path)

    try:
        with Image.open(source_path) as image:
            image.thumbnail((_AUDIO_THUMB_MAX_DIMENSION, _AUDIO_THUMB_MAX_DIMENSION))
            converted = image.convert("RGB")
            for quality in (92, 82, 72, 62, 52):
                converted.save(output_path, "JPEG", quality=quality, optimize=True)
                if output_path.stat().st_size <= _AUDIO_THUMB_MAX_BYTES:
                    break
        return _is_usable_thumbnail_file(output_path)
    except Exception as exc:
        logging.debug("Failed to normalize bot audio thumbnail with Pillow: path=%s error=%s", source_path, exc)
        output_path.unlink(missing_ok=True)
        return _normalize_audio_thumbnail_file_with_ffmpeg(source_path, output_path)


def _normalize_audio_thumbnail_file_with_ffmpeg(source_path: Path, output_path: Path) -> bool:
    import subprocess

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source_path),
                "-vf",
                f"scale={_AUDIO_THUMB_MAX_DIMENSION}:{_AUDIO_THUMB_MAX_DIMENSION}:force_original_aspect_ratio=decrease",
                "-frames:v",
                "1",
                "-q:v",
                "4",
                str(output_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logging.debug("Failed to normalize bot audio thumbnail with ffmpeg: path=%s error=%s", source_path, exc)
        output_path.unlink(missing_ok=True)
        return False
    return _is_usable_thumbnail_file(output_path)
