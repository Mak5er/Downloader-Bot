import asyncio
import os
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional, TypeVar

from aiogram import Bot, types
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError

import messages as bm
from services.logger import logger as logging
from services.download.queue import QueueTicket
from utils.download_manager import DownloadProgress

T = TypeVar("T")

_chat_action_cache: "OrderedDict[tuple[int, str], float]" = OrderedDict()
_chat_action_cache_maxsize = 2048
_chat_action_ttl_seconds = 4.0
_message_edit_cache: "OrderedDict[tuple[Any, ...], tuple[str, tuple[tuple[str, str], ...]]]" = OrderedDict()
_message_edit_cache_maxsize = 4096


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
    **kwargs: Any,
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
    **kwargs: Any,
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
    **kwargs: Any,
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


async def maybe_delete_user_message(message: types.Message, delete_flag: Any) -> bool:
    if str(delete_flag).lower() != "on":
        return False

    try:
        await message.delete()
        return True
    except TelegramAPIError:
        await message.answer(bm.delete_permission_warning())
        return False


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


async def send_chat_action_if_needed(
    bot: Bot,
    chat_id: int,
    action: str,
    business_id: Optional[int],
) -> None:
    if business_id is not None:
        return

    cache_key = (int(chat_id), str(action))
    now = time.monotonic()
    cached = _chat_action_cache.get(cache_key)
    if cached is not None and now - cached < _chat_action_ttl_seconds:
        _chat_action_cache.move_to_end(cache_key)
        return

    await bot.send_chat_action(chat_id, action)
    _chat_action_cache[cache_key] = now
    _chat_action_cache.move_to_end(cache_key)
    while len(_chat_action_cache) > _chat_action_cache_maxsize:
        _chat_action_cache.popitem(last=False)


def resolve_settings_target_id(message: types.Message) -> int:
    """Return chat id for group/supergroup, otherwise sender id."""
    if message.chat and message.chat.type != ChatType.PRIVATE:
        return message.chat.id
    return message.from_user.id


async def load_user_settings(db_service: Any, message: types.Message) -> Any:
    return await db_service.user_settings(resolve_settings_target_id(message))


def make_status_text_progress_updater(
    label: str,
    update_text: Callable[[str], Awaitable[None]],
    *,
    min_interval_seconds: float = 1.0,
) -> Callable[[DownloadProgress], Awaitable[None]]:
    state = {"last": 0.0}

    async def _on_progress(progress: DownloadProgress) -> None:
        now = time.monotonic()
        if not progress.done and now - state["last"] < min_interval_seconds:
            return
        state["last"] = now
        await update_text(build_progress_status(label, progress))

    return _on_progress


def make_retry_status_notifier(
    update_text: Callable[[str], Awaitable[None]],
    *,
    enabled: bool = True,
    min_failed_attempt: int = 2,
) -> Callable[[int, int, Any], Awaitable[None]]:
    async def _on_retry(failed_attempt: int, total_attempts: int, _error: Any) -> None:
        if not enabled or failed_attempt < min_failed_attempt:
            return
        await update_text(bm.retrying_again_status(failed_attempt + 1, total_attempts))

    return _on_retry


async def safe_edit_text(message: Optional[types.Message], text: str, **kwargs: Any) -> None:
    """Best-effort edit of a bot message (status/progress)."""
    if not message:
        return
    cache_key = _build_message_edit_cache_key(message)
    payload = (text, _normalize_edit_kwargs(kwargs))
    cached = _message_edit_cache.get(cache_key)
    if cached == payload:
        return
    try:
        await message.edit_text(text, **kwargs)
        _message_edit_cache[cache_key] = payload
        _message_edit_cache.move_to_end(cache_key)
        while len(_message_edit_cache) > _message_edit_cache_maxsize:
            _message_edit_cache.popitem(last=False)
    except Exception:
        return


async def safe_edit_inline_text(
    bot: Bot,
    inline_message_id: Optional[str],
    text: str,
    **kwargs: Any,
) -> bool:
    """Best-effort edit of an inline message text by inline_message_id."""
    if not inline_message_id:
        return False
    cache_key = ("inline", inline_message_id)
    payload = (text, _normalize_edit_kwargs(kwargs))
    cached = _message_edit_cache.get(cache_key)
    if cached == payload:
        return True
    try:
        await bot.edit_message_text(text=text, inline_message_id=inline_message_id, **kwargs)
        _message_edit_cache[cache_key] = payload
        _message_edit_cache.move_to_end(cache_key)
        while len(_message_edit_cache) > _message_edit_cache_maxsize:
            _message_edit_cache.popitem(last=False)
        return True
    except Exception:
        return False


async def safe_edit_inline_media(
    bot: Bot,
    inline_message_id: Optional[str],
    media: Any,
    **kwargs: Any,
) -> bool:
    """Best-effort edit of inline message media by inline_message_id."""
    if not inline_message_id:
        return False
    try:
        await bot.edit_message_media(inline_message_id=inline_message_id, media=media, **kwargs)
        return True
    except Exception:
        return False


async def safe_delete_message(message: Optional[types.Message]) -> None:
    """Best-effort delete of a bot message (status/progress)."""
    if not message:
        return
    try:
        await message.delete()
    except Exception:
        return


def _build_message_edit_cache_key(message: types.Message) -> tuple[Any, ...]:
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is not None and message_id is not None:
        return ("message", chat_id, message_id)
    return ("message_obj", id(message))


def _normalize_edit_kwargs(kwargs: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    normalized: list[tuple[str, str]] = []
    for key, value in sorted(kwargs.items()):
        normalized.append((str(key), repr(value)))
    return tuple(normalized)


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
