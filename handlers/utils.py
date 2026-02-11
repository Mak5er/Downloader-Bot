import asyncio
import os
from typing import Any, Awaitable, Callable, Optional, TypeVar

from pathlib import Path

from aiogram import Bot, types
from aiogram.enums import ChatType
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramAPIError

import messages as bm
from log.logger import logger as logging
from services.download_queue import QueueTicket
from utils.download_manager import DownloadProgress

DELETE_WARNING_TEXT = (
    "I can't delete the link in this chat because I don't have enough permissions."
)

_bot_avatar_file_id: Optional[str] = None
_bot_avatar_path: Optional[str] = None
_bot_username: Optional[str] = None
_bot_id: Optional[int] = None

T = TypeVar("T")


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
    global _bot_username
    if _bot_username:
        return f"t.me/{_bot_username}"

    bot_data = await bot.get_me()
    _bot_username = bot_data.username or ""
    return f"t.me/{_bot_username}"


async def _get_bot_id(bot: Bot) -> int:
    global _bot_id
    if _bot_id is not None:
        return _bot_id

    bot_data = await bot.get_me()
    _bot_id = bot_data.id
    return _bot_id


async def get_bot_avatar_file_id(bot: Bot) -> Optional[str]:
    """Return cached bot avatar file_id if available."""
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
    """Return bot avatar as InputFile for thumbnail uploads."""
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


async def remove_file(path: Optional[str]) -> None:
    if not path:
        return

    try:
        await asyncio.to_thread(os.remove, path)
        logging.debug("Removed temporary file: path=%s", path)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logging.error("Error removing file: path=%s error=%s", path, exc)


async def send_chat_action_if_needed(bot: Bot, chat_id: int, action: str, business_id: Optional[int]) -> None:
    if business_id is None:
        await bot.send_chat_action(chat_id, action)


def resolve_settings_target_id(message: types.Message) -> int:
    """Return chat id for group/supergroup, otherwise sender id."""
    if message.chat and message.chat.type != ChatType.PRIVATE:
        return message.chat.id
    return message.from_user.id


async def safe_edit_text(message: Optional[types.Message], text: str, **kwargs) -> None:
    """Best-effort edit of a bot message (status/progress)."""
    if not message:
        return
    try:
        await message.edit_text(text, **kwargs)
    except Exception:
        return


async def safe_delete_message(message: Optional[types.Message]) -> None:
    """Best-effort delete of a bot message (status/progress)."""
    if not message:
        return
    try:
        await message.delete()
    except Exception:
        return


def _format_bytes(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0, num_bytes))
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def _format_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "ETA: --:--"
    total = max(0, int(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"ETA: {hours:02d}:{minutes:02d}:{sec:02d}"
    return f"ETA: {minutes:02d}:{sec:02d}"


def build_queue_status(label: str, ticket: QueueTicket) -> str:
    return (
        f"Queueing {label}...\n"
        f"Position: {ticket.position}\n"
        f"Workers: {ticket.active_workers}"
    )


def build_progress_status(label: str, progress: DownloadProgress) -> str:
    speed = _format_bytes(int(progress.speed_bps)) + "/s"
    downloaded = _format_bytes(progress.downloaded_bytes)
    if progress.total_bytes > 0:
        total = _format_bytes(progress.total_bytes)
        percent = (progress.downloaded_bytes / progress.total_bytes) * 100.0
        percent_text = f"{percent:5.1f}%"
        return (
            f"Downloading {label}... {percent_text}\n"
            f"{downloaded} / {total}\n"
            f"{speed} | {_format_eta(progress.eta_seconds)}"
        )
    return (
        f"Downloading {label}...\n"
        f"{downloaded}\n"
        f"{speed} | {_format_eta(progress.eta_seconds)}"
    )


def build_rate_limit_text(retry_after: float) -> str:
    wait = max(1, int(round(retry_after)))
    return (
        "Too many requests from your account right now.\n"
        f"Please wait {wait}s and try again."
    )


def build_queue_busy_text(position: int) -> str:
    return (
        "The download queue is busy right now.\n"
        f"Your next request position would be around #{position}."
    )


async def retry_async_operation(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    delay_seconds: float = 2.0,
    should_retry_result: Optional[Callable[[T], bool]] = None,
    retry_on_exception: Optional[Callable[[BaseException], bool]] = None,
    on_retry: Optional[Callable[[int, int, Optional[BaseException]], Awaitable[Any] | Any]] = None,
) -> T:
    """
    Retry an async operation with fixed delay.

    `on_retry` is called after a failed attempt and before sleeping.
    Signature: `(failed_attempt, total_attempts, error_or_none)`.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_error: Optional[BaseException] = None
    last_result: Optional[T] = None

    for attempt in range(1, attempts + 1):
        failed_error: Optional[BaseException] = None
        should_retry = False

        try:
            result = await operation()
            last_result = result
            if should_retry_result is not None and should_retry_result(result):
                should_retry = True
        except Exception as exc:
            if retry_on_exception is not None and not retry_on_exception(exc):
                raise
            failed_error = exc
            last_error = exc
            should_retry = True

        if not should_retry:
            # mypy: last_result is assigned when no exception and no retry request.
            return last_result  # type: ignore[return-value]

        if attempt >= attempts:
            break

        if on_retry:
            maybe = on_retry(attempt, attempts, failed_error)
            if asyncio.iscoroutine(maybe):
                await maybe

        await asyncio.sleep(max(0.0, delay_seconds))

    if last_error is not None:
        raise last_error

    return last_result  # type: ignore[return-value]
