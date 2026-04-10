import asyncio
import logging as py_logging
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from aiogram import types, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import BufferedInputFile

import keyboards as kb
import messages as bm
from app_context import bot, db
from config import ADMINS_UID, OUTPUT_DIR
from filters import IsBotAdmin
from services.logger import ERROR_LOG, EVENT_LOG, INFO_LOG, PERF_LOG, logger as logging
from services.download.queue import get_download_queue
from services.runtime.analytics_status import get_snapshot as get_analytics_runtime_snapshot
from services.runtime.stats import get_runtime_snapshot

logging = logging.bind(service="admin")

router = Router()

_ADMIN_ACCESS_REQUIRED = "Admin access required."
_DOWNLOAD_CLEANUP_MIN_AGE_SECONDS = 6 * 60 * 60.0
_ADMIN_ACTIVE_CHECK_CONCURRENCY = 8
_ADMIN_MAILING_CONCURRENCY = 5
_ADMIN_THROTTLE_SECONDS = 0.05
_RUNTIME_LOG_FILES = (INFO_LOG, ERROR_LOG, EVENT_LOG, PERF_LOG)
_DOWNLOADABLE_LOG_FILES = (INFO_LOG, ERROR_LOG)


@dataclass(frozen=True, slots=True)
class _DirectorySnapshot:
    exists: bool
    file_count: int
    dir_count: int
    total_bytes: int
    oldest_file_age_seconds: float | None
    newest_file_age_seconds: float | None


def _is_admin_user(user_id: int | None) -> bool:
    return user_id is not None and int(user_id) in ADMINS_UID


async def _ensure_admin_callback(call: types.CallbackQuery) -> bool:
    if _is_admin_user(getattr(getattr(call, "from_user", None), "id", None)):
        return True
    await call.answer(_ADMIN_ACCESS_REQUIRED, show_alert=True)
    return False


async def _ensure_admin_message(message: types.Message) -> bool:
    if _is_admin_user(getattr(getattr(message, "from_user", None), "id", None)):
        return True
    await message.answer(_ADMIN_ACCESS_REQUIRED, reply_markup=types.ReplyKeyboardRemove())
    return False


async def _run_bounded(items, *, limit: int, worker):
    semaphore = asyncio.Semaphore(max(1, int(limit)))

    async def _wrapped(item):
        async with semaphore:
            return await worker(item)

    return await asyncio.gather(*(_wrapped(item) for item in items))


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


def _format_average_bytes(total_bytes: int, total_count: int) -> str:
    if total_count <= 0:
        return "0 B"
    return _format_bytes(int(total_bytes / total_count))


def _classify_perf_bottleneck(queue_wait_p95_ms: float, processing_p95_ms: float) -> str:
    if queue_wait_p95_ms > processing_p95_ms * 1.5:
        return "queue-bound"
    if processing_p95_ms > queue_wait_p95_ms * 1.5:
        return "download-bound"
    return "balanced"


def _build_directory_snapshot(path_str: str, *, now: float | None = None) -> _DirectorySnapshot:
    root = Path(path_str)
    if not root.exists():
        return _DirectorySnapshot(False, 0, 0, 0, None, None)

    current_time = time.time() if now is None else now
    file_count = 0
    dir_count = 0
    total_bytes = 0
    oldest_mtime: float | None = None
    newest_mtime: float | None = None

    for current_root, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current_root)
        if current_path != root:
            dir_count += 1
        for dirname in list(dirnames):
            if (current_path / dirname).is_symlink():
                dirnames.remove(dirname)
        for filename in filenames:
            file_path = current_path / filename
            try:
                stat = file_path.stat()
            except OSError:
                continue
            file_count += 1
            total_bytes += max(0, int(stat.st_size))
            if oldest_mtime is None or stat.st_mtime < oldest_mtime:
                oldest_mtime = stat.st_mtime
            if newest_mtime is None or stat.st_mtime > newest_mtime:
                newest_mtime = stat.st_mtime

    return _DirectorySnapshot(
        exists=True,
        file_count=file_count,
        dir_count=dir_count,
        total_bytes=total_bytes,
        oldest_file_age_seconds=(current_time - oldest_mtime) if oldest_mtime is not None else None,
        newest_file_age_seconds=(current_time - newest_mtime) if newest_mtime is not None else None,
    )


def _build_log_size_lines() -> list[str]:
    lines: list[str] = []
    for path_str in _RUNTIME_LOG_FILES:
        path = Path(path_str)
        size = path.stat().st_size if path.exists() else 0
        lines.append(f"{path.name}: {_format_bytes(size)}")
    return lines


async def _get_admin_counts() -> dict[str, int]:
    return {
        "user_count": await db.user_count(),
        "private_chat_count": await db.private_chat_count(),
        "group_chat_count": await db.group_chat_count(),
        "active_user_count": await db.active_user_count(),
        "inactive_user_count": await db.inactive_user_count(),
    }


def _is_path_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _cleanup_download_tree(*, now: float | None = None) -> tuple[int, int, int]:
    root = Path(OUTPUT_DIR).resolve()
    cutoff_timestamp = (time.time() if now is None else now) - _DOWNLOAD_CLEANUP_MIN_AGE_SECONDS
    removed_files = 0
    skipped_recent_files = 0
    candidate_dirs: list[Path] = []

    for current_root, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current_root).resolve()
        if not _is_path_within_root(current_path, root):
            dirnames[:] = []
            continue

        safe_dirnames: list[str] = []
        for dirname in dirnames:
            dir_path = current_path / dirname
            if dir_path.is_symlink():
                continue
            if _is_path_within_root(dir_path, root):
                safe_dirnames.append(dirname)
                candidate_dirs.append(dir_path.resolve())
        dirnames[:] = safe_dirnames

        for filename in filenames:
            file_path = current_path / filename
            if not _is_path_within_root(file_path, root):
                continue
            if file_path.stat().st_mtime > cutoff_timestamp:
                skipped_recent_files += 1
                continue
            file_path.unlink()
            removed_files += 1

    removed_dirs = 0
    for dir_path in sorted(candidate_dirs, key=lambda item: len(item.parts), reverse=True):
        if dir_path == root:
            continue
        try:
            next(dir_path.iterdir())
        except StopIteration:
            dir_path.rmdir()
            removed_dirs += 1
        except FileNotFoundError:
            continue

    return removed_files, skipped_recent_files, removed_dirs


async def _check_user_reachability(user) -> bool:
    user_id = int(user.user_id)
    user_status = user.status or "inactive"
    send_successful = False

    try:
        await bot.send_chat_action(chat_id=user_id, action="typing")
        send_successful = True
    except TelegramRetryAfter as error:
        await asyncio.sleep(error.retry_after)
        try:
            await bot.send_chat_action(chat_id=user_id, action="typing")
            send_successful = True
        except TelegramRetryAfter as retry_error:
            logging.warning("Retry-after triggered twice while checking user %s: %s", user_id, retry_error)
        except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest) as retry_error:
            logging.info("User %s unreachable on retry: %s", user_id, retry_error)
        except TelegramAPIError as retry_error:
            logging.error("API error on retry while checking user %s: %s", user_id, retry_error)
        except Exception as retry_error:
            logging.error("Unexpected retry error while checking user %s: %s", user_id, retry_error)
    except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest) as error:
        logging.info("User %s unreachable: %s", user_id, error)
    except TelegramAPIError as error:
        logging.error("API error while checking user %s: %s", user_id, error)
    except Exception as error:
        logging.error("Unexpected error while checking user %s: %s", user_id, error)

    if send_successful:
        if user_status != "active":
            await db.set_active(user_id)
    elif user_status != "ban":
        await db.set_inactive(user_id)

    await asyncio.sleep(_ADMIN_THROTTLE_SECONDS)
    return send_successful


async def _deliver_mailing_message(user, *, sender_id: int, message_id: int) -> None:
    user_id = int(user.user_id)
    user_status = user.status or "inactive"

    try:
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=sender_id,
            message_id=message_id,
        )
        if user_status == "inactive":
            await db.set_active(user_id)
    except Exception as error:
        error_text = str(error)
        if error_text == "Forbidden: bots can't send messages to bots":
            await db.delete_user(user_id)
        if "blocked" in error_text or "Chat not found" in error_text:
            await db.set_inactive(user_id)
    finally:
        await asyncio.sleep(_ADMIN_THROTTLE_SECONDS)


class Mailing(StatesGroup):
    send_to_all_message = State()


class Admin(StatesGroup):
    add_joke = State()
    control_user = State()
    ban_reason = State()
    feedback_answer = State()
    write_message = State()
    write_chat_id = State()
    write_chat_text = State()


def _format_bytes(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0, num_bytes))
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


def _render_session_text() -> str:
    snapshot = get_runtime_snapshot()
    uptime = str(timedelta(seconds=int(snapshot.uptime_seconds)))
    downloads_per_hour = (
        snapshot.total_downloads / (snapshot.uptime_seconds / 3600.0)
        if snapshot.uptime_seconds > 0 and snapshot.total_downloads > 0
        else 0.0
    )
    lines = [
        "<b>Runtime Session Stats</b>",
        f"Uptime: <b>{uptime}</b>",
        f"Total downloads: <b>{snapshot.total_downloads}</b> ({downloads_per_hour:.1f}/h)",
        f"Videos: <b>{snapshot.total_videos}</b>",
        f"Audio: <b>{snapshot.total_audio}</b>",
        f"Other: <b>{snapshot.total_other}</b>",
        f"Traffic: <b>{_format_bytes(snapshot.total_bytes)}</b>",
        f"Avg download size: <b>{_format_average_bytes(snapshot.total_bytes, snapshot.total_downloads)}</b>",
        f"Last download: <b>{_format_age((time.monotonic() - snapshot.last_download_monotonic) if snapshot.last_download_monotonic is not None else None)}</b>",
    ]

    if snapshot.by_source:
        lines.append("")
        lines.append("<b>By source:</b>")
        for source, payload in sorted(
            snapshot.by_source.items(),
            key=lambda item: (-int(item[1].get("count", 0) or 0), item[0]),
        ):
            count = int(payload.get("count", 0) or 0)
            size_bytes = int(payload.get("bytes", 0) or 0)
            share = (count / snapshot.total_downloads * 100.0) if snapshot.total_downloads else 0.0
            lines.append(
                f"{source}: {count} ({share:.0f}%) | {_format_bytes(size_bytes)} | avg {_format_average_bytes(size_bytes, count)}"
            )

    return "\n".join(lines)


async def _render_perf_text(*, include_load: bool = True) -> str:
    queue = get_download_queue()
    snapshot = await queue.metrics_snapshot()
    if not snapshot:
        return bm.no_queue_metrics_yet()

    lines = ["<b>Queue Performance</b>"]
    if include_load:
        load = queue.load_snapshot()
        lines.extend(
            [
                f"Queued jobs: <b>{load.queued_jobs}</b>",
                f"Active jobs: <b>{load.active_jobs}</b>",
                f"Workers: <b>{load.active_workers}</b>",
            ]
        )
    lines.append(f"Tracked sources: <b>{len(snapshot)}</b>")

    sorted_sources = sorted(
        snapshot.items(),
        key=lambda item: max(item[1].queue_wait_p95_ms, item[1].processing_p95_ms),
        reverse=True,
    )
    for source, item in sorted_sources:
        bottleneck = _classify_perf_bottleneck(item.queue_wait_p95_ms, item.processing_p95_ms)
        lines.append(
            (
                f"\n<b>{source}</b>\n"
                f"Jobs: {item.count}\n"
                f"Queue wait p50/p95: {item.queue_wait_p50_ms:.0f}/{item.queue_wait_p95_ms:.0f} ms\n"
                f"Processing p50/p95: {item.processing_p50_ms:.0f}/{item.processing_p95_ms:.0f} ms\n"
                f"Bottleneck: <b>{bottleneck}</b>"
            )
        )

    return "\n".join(lines)


async def _render_health_text(*, include_queue: bool = True, include_runtime_downloads: bool = True) -> str:
    queue_snapshot = get_download_queue().load_snapshot()
    runtime_snapshot = get_runtime_snapshot()
    analytics_snapshot = get_analytics_runtime_snapshot()
    uptime = str(timedelta(seconds=int(runtime_snapshot.uptime_seconds)))
    lines = [
        "<b>Bot Health</b>",
        f"Uptime: <b>{uptime}</b>",
        f"Last download: <b>{_format_age((time.monotonic() - runtime_snapshot.last_download_monotonic) if runtime_snapshot.last_download_monotonic is not None else None)}</b>",
        f"Analytics drops: <b>{analytics_snapshot.dropped_events}</b>",
        f"Last analytics drop: <b>{_format_age((time.monotonic() - analytics_snapshot.last_drop_monotonic) if analytics_snapshot.last_drop_monotonic is not None else None)}</b>",
        "",
        "<b>Logs</b>",
        *_build_log_size_lines(),
    ]
    if include_runtime_downloads:
        lines.insert(2, f"Runtime downloads: <b>{runtime_snapshot.total_downloads}</b>")
    if include_queue:
        lines.insert(
            2,
            (
                f"Queue: <b>{queue_snapshot.queued_jobs}</b> queued / "
                f"<b>{queue_snapshot.active_jobs}</b> active / "
                f"<b>{queue_snapshot.active_workers}</b> workers"
            ),
        )
    return "\n".join(lines)


def _render_downloads_text(*, footer: str | None = None, include_cleanup_load: bool = True) -> str:
    queue_snapshot = get_download_queue().load_snapshot()
    snapshot = _build_directory_snapshot(OUTPUT_DIR)
    cleanup_ready = queue_snapshot.active_jobs == 0 and queue_snapshot.queued_jobs == 0
    lines = [
        "<b>Downloads Storage</b>",
        f"Path: <code>{OUTPUT_DIR}</code>",
        f"Exists: <b>{'yes' if snapshot.exists else 'no'}</b>",
        f"Files: <b>{snapshot.file_count}</b>",
        f"Dirs: <b>{snapshot.dir_count}</b>",
        f"Total size: <b>{_format_bytes(snapshot.total_bytes)}</b>",
        f"Oldest file: <b>{_format_age(snapshot.oldest_file_age_seconds)}</b>",
        f"Newest file: <b>{_format_age(snapshot.newest_file_age_seconds)}</b>",
        "",
        "<b>Cleanup status</b>",
        f"Cleanup ready: <b>{'yes' if cleanup_ready else 'no'}</b>",
    ]
    if include_cleanup_load:
        lines.insert(
            -1,
            f"Queue load: <b>{queue_snapshot.queued_jobs}</b> queued / <b>{queue_snapshot.active_jobs}</b> active",
        )
    if footer:
        lines.extend(["", footer])
    return "\n".join(lines)


async def _render_ops_text() -> str:
    health_text = await _render_health_text(include_queue=False, include_runtime_downloads=False)
    perf_text = await _render_perf_text()
    return f"{health_text}\n\n{perf_text}"


def _render_runtime_storage_text(*, footer: str | None = None) -> str:
    session_text = _render_session_text()
    downloads_text = _render_downloads_text(footer=footer, include_cleanup_load=False)
    return f"{session_text}\n\n{downloads_text}"


async def _render_admin_panel(message: types.Message, *, edit: bool) -> None:
    counts = await _get_admin_counts()
    queue_snapshot = get_download_queue().load_snapshot()
    runtime_snapshot = get_runtime_snapshot()
    text = (
        f"{bm.admin_panel(
            counts['user_count'],
            counts['private_chat_count'],
            counts['group_chat_count'],
            counts['active_user_count'],
            counts['inactive_user_count'],
        )}\n\n"
        f"<b>Runtime now</b>\n"
        f"Queue: <b>{queue_snapshot.queued_jobs}</b> queued / <b>{queue_snapshot.active_jobs}</b> active / <b>{queue_snapshot.active_workers}</b> workers\n"
        f"Downloads this runtime: <b>{runtime_snapshot.total_downloads}</b>"
    )
    if edit:
        await message.edit_text(text=text, reply_markup=kb.admin_keyboard(), parse_mode="HTML")
    else:
        await message.answer(text=text, reply_markup=kb.admin_keyboard(), parse_mode="HTML")


async def _cleanup_downloads_once() -> str:
    queue_snapshot = get_download_queue().load_snapshot()
    if queue_snapshot.active_jobs > 0 or queue_snapshot.queued_jobs > 0:
        return bm.downloads_cleanup_blocked(queue_snapshot.active_jobs, queue_snapshot.queued_jobs)
    if not os.path.exists(OUTPUT_DIR):
        return f"The folder '{OUTPUT_DIR}' does not exist."
    removed_files, skipped_recent_files, removed_dirs = _cleanup_download_tree()
    return bm.downloads_cleanup_finished(removed_files, removed_dirs, skipped_recent_files)


@router.message(Command("admin"), IsBotAdmin())
async def admin(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")

    if message.chat.type == 'private':
        await _render_admin_panel(message, edit=False)
    else:
        await message.answer(bm.not_groups())


@router.message(Command("perf"), IsBotAdmin())
async def perf_metrics(message: types.Message):
    await message.answer(await _render_perf_text(), parse_mode="HTML")


@router.message(Command("session"), IsBotAdmin())
async def session_metrics(message: types.Message):
    await message.answer(_render_session_text(), parse_mode="HTML")


@router.callback_query(F.data == "admin_refresh")
async def admin_refresh(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return
    await call.answer()
    await _render_admin_panel(call.message, edit=True)


@router.callback_query(F.data == "admin_ops")
async def admin_ops(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return
    await call.answer()
    await call.message.edit_text(
        text=await _render_ops_text(),
        reply_markup=kb.admin_detail_keyboard("admin_ops"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_runtime_storage")
async def admin_runtime_storage(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return
    await call.answer()
    queue_snapshot = get_download_queue().load_snapshot()
    await call.message.edit_text(
        text=_render_runtime_storage_text(),
        reply_markup=kb.downloads_admin_keyboard(
            can_cleanup=queue_snapshot.active_jobs == 0 and queue_snapshot.queued_jobs == 0,
            refresh_callback="admin_runtime_storage",
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_health")
async def admin_health(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return
    await call.answer()
    await call.message.edit_text(
        text=await _render_health_text(),
        reply_markup=kb.return_back_to_admin_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_session")
async def admin_session(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return
    await call.answer()
    await call.message.edit_text(
        text=_render_session_text(),
        reply_markup=kb.return_back_to_admin_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_perf")
async def admin_perf(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return
    await call.answer()
    await call.message.edit_text(
        text=await _render_perf_text(),
        reply_markup=kb.return_back_to_admin_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_downloads")
async def admin_downloads(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return
    await call.answer()
    queue_snapshot = get_download_queue().load_snapshot()
    await call.message.edit_text(
        text=_render_downloads_text(),
        reply_markup=kb.downloads_admin_keyboard(
            can_cleanup=queue_snapshot.active_jobs == 0 and queue_snapshot.queued_jobs == 0
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_cleanup_downloads")
async def admin_cleanup_downloads(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return
    status_text = await _cleanup_downloads_once()
    await call.answer("Cleanup finished" if "finished" in status_text else "Cleanup skipped")
    queue_snapshot = get_download_queue().load_snapshot()
    await call.message.edit_text(
        text=_render_runtime_storage_text(footer=status_text),
        reply_markup=kb.downloads_admin_keyboard(
            can_cleanup=queue_snapshot.active_jobs == 0 and queue_snapshot.queued_jobs == 0,
            refresh_callback="admin_runtime_storage",
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data == 'delete_log')
async def del_log(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return

    await bot.send_chat_action(call.message.chat.id, "typing")
    py_logging.shutdown()
    for log_path in _RUNTIME_LOG_FILES:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    await call.message.reply(bm.log_deleted())
    await call.answer()


@router.callback_query(F.data == 'download_log')
async def download_log_handler(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return

    await bot.send_chat_action(call.message.chat.id, "typing")

    user_id = call.from_user.id

    await call.answer()

    for file_path in _DOWNLOADABLE_LOG_FILES:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        with path.open('rb') as file:
            await call.message.answer_document(BufferedInputFile(file.read(), filename=path.name))

    logging.info("User action: Downloaded logs (User ID: %s)", user_id)


@router.callback_query(F.data == 'check_active_users')
async def check_active_users(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return

    await call.answer()
    await bot.send_chat_action(call.message.chat.id, "typing")

    users_info = await db.get_all_users_info()
    users_to_check = [user for user in users_info if (user.status or "inactive") != "ban"]

    if not users_to_check:
        await call.message.edit_text(
            bm.active_users_check_no_targets(),
            reply_markup=kb.return_back_to_admin_keyboard()
        )
        return

    total_users = len(users_to_check)
    status_message = await call.message.edit_text(bm.active_users_check_started(total_users))
    results = await _run_bounded(
        users_to_check,
        limit=_ADMIN_ACTIVE_CHECK_CONCURRENCY,
        worker=_check_user_reachability,
    )
    reachable = sum(1 for result in results if result)
    unreachable = total_users - reachable

    await status_message.edit_text(
        bm.active_users_check_completed(total_users, reachable, unreachable),
        reply_markup=kb.return_back_to_admin_keyboard(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == 'cancel_action')
async def cancel_action(call: types.CallbackQuery, state: FSMContext):
    if not await _ensure_admin_callback(call):
        return

    current_state = await state.get_state()
    if current_state is None:
        await call.answer("Nothing to cancel")
        return

    await state.clear()
    await call.answer("Canceled")

    try:
        await call.message.edit_text(
            bm.canceled(),
            reply_markup=kb.return_back_to_admin_keyboard()
        )
    except TelegramBadRequest:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await call.message.answer(
            bm.canceled(),
            reply_markup=kb.return_back_to_admin_keyboard()
        )


@router.callback_query(F.data == 'message_chat_id')
async def message_chat_id(call: types.CallbackQuery, state: FSMContext):
    if not await _ensure_admin_callback(call):
        return

    await call.answer()
    await bot.send_chat_action(call.message.chat.id, "typing")
    await call.message.edit_text(
        bm.enter_chat_id(),
        reply_markup=kb.cancel_keyboard()
    )
    await state.set_state(Admin.write_chat_id)


@router.message(Admin.write_chat_id)
async def admin_collect_chat_id(message: types.Message, state: FSMContext):
    if not await _ensure_admin_message(message):
        await state.clear()
        return

    if message.text == bm.cancel():
        await bot.send_message(message.chat.id, bm.canceled(), reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return

    chat_id_text = message.text.strip()
    try:
        chat_id = int(chat_id_text)
    except ValueError:
        await message.reply(bm.invalid_chat_id())
        return

    await state.update_data(target_chat_id=chat_id)
    known_info = await db.get_user_info(chat_id)
    preview_lines: list[str] = []
    if known_info:
        preview_lines.append(
            bm.known_chat_target(
                chat_id,
                known_info[0],
                known_info[1],
                known_info[2],
            )
        )
    else:
        preview_lines.append(bm.unknown_chat_target(chat_id))
    preview_lines.append("")
    preview_lines.append(bm.enter_chat_message())
    await message.answer("\n".join(preview_lines), reply_markup=kb.cancel_keyboard(), parse_mode="HTML")
    await state.set_state(Admin.write_chat_text)


@router.message(Admin.write_chat_text)
async def admin_send_to_chat(message: types.Message, state: FSMContext):
    if not await _ensure_admin_message(message):
        await state.clear()
        return

    if message.text == bm.cancel():
        await bot.send_message(message.chat.id, bm.canceled(), reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return

    data = await state.get_data()
    chat_id = data.get("target_chat_id")

    if chat_id is None:
        await message.reply(bm.chat_message_failed("unknown"), reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return

    chat_text = message.text
    await state.clear()

    progress_message = await message.reply(bm.chat_message_sending(), reply_markup=types.ReplyKeyboardRemove())

    sent_message = None

    try:
        sent_message = await bot.send_message(chat_id=chat_id, text=chat_text)
    except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest) as error:
        logging.info("Failed to send message to chat %s: %s", chat_id, error)
        await bot.delete_message(message.chat.id, progress_message.message_id)
        await message.answer(bm.chat_message_failed(chat_id), reply_markup=kb.return_back_to_admin_keyboard())
        return
    except TelegramAPIError as error:
        logging.error("API error while sending message to chat %s: %s", chat_id, error)
        await bot.delete_message(message.chat.id, progress_message.message_id)
        await message.answer(bm.chat_message_failed(chat_id), reply_markup=kb.return_back_to_admin_keyboard())
        return
    except Exception as error:
        logging.error("Unexpected error while sending message to chat %s: %s", chat_id, error)
        await bot.delete_message(message.chat.id, progress_message.message_id)
        await message.answer(bm.chat_message_failed(chat_id), reply_markup=kb.return_back_to_admin_keyboard())
        return

    if sent_message:
        chat_obj = sent_message.chat
        chat_type_raw = getattr(chat_obj, "type", None)
        chat_type_str = chat_type_raw.value if hasattr(chat_type_raw, "value") else str(chat_type_raw)
        chat_type_value = "private" if chat_type_str == "private" else "public"

        chat_name = getattr(chat_obj, "title", None) or getattr(chat_obj, "full_name", None)

        if not chat_name:
            first_name = getattr(chat_obj, "first_name", None)
            last_name = getattr(chat_obj, "last_name", None)
            name_parts = [part for part in (first_name, last_name) if part]
            if name_parts:
                chat_name = " ".join(name_parts)

        if not chat_name:
            chat_name = f"Chat {chat_id}"

        chat_username = getattr(chat_obj, "username", None)
        language_code = getattr(chat_obj, "language_code", None)

        await db.upsert_chat(
            user_id=chat_id,
            user_name=chat_name,
            user_username=chat_username,
            chat_type=chat_type_value,
            language=language_code,
            status="active",
        )

    await bot.delete_message(message.chat.id, progress_message.message_id)
    await message.answer(bm.chat_message_sent(chat_id), reply_markup=kb.return_back_to_admin_keyboard())


@router.callback_query(F.data == 'back_to_admin')
async def back_to_admin(call: types.CallbackQuery):
    if not await _ensure_admin_callback(call):
        return

    await call.answer()
    await _render_admin_panel(call.message, edit=True)


@router.callback_query(F.data == 'send_to_all')
async def send_to_all_callback(call: types.CallbackQuery, state: FSMContext):
    if not await _ensure_admin_callback(call):
        return

    users = await db.get_all_users_info()
    active_users = sum(1 for user in users if (user.status or "inactive") == "active")
    banned_users = sum(1 for user in users if (user.status or "inactive") == "ban")
    private_users = sum(1 for user in users if user.chat_type == "private")
    group_users = sum(1 for user in users if user.chat_type != "private")

    await call.message.edit_text(
        text=bm.mailing_audience_preview(
            len(users) - banned_users,
            active_users,
            len(users) - active_users - banned_users,
            banned_users,
            private_users,
            group_users,
        ),
        reply_markup=kb.cancel_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(Mailing.send_to_all_message)
    await call.answer()


@router.message(Mailing.send_to_all_message)
async def send_to_all_message(message: types.Message, state: FSMContext):
    if not await _ensure_admin_message(message):
        await state.clear()
        return

    sender_id = message.from_user.id
    if message.text == bm.cancel():
        await message.answer(text=bm.canceled(), reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return

    else:
        await state.clear()

        await bot.send_message(chat_id=message.chat.id,
                               text=bm.start_mailing(),
                               reply_markup=types.ReplyKeyboardRemove())

        users = await db.get_all_users_info()
        await _run_bounded(
            users,
            limit=_ADMIN_MAILING_CONCURRENCY,
            worker=lambda user: _deliver_mailing_message(
                user,
                sender_id=sender_id,
                message_id=message.message_id,
            ),
        )

        await bot.send_message(chat_id=message.chat.id,
                               text=bm.finish_mailing(),
                               reply_markup=types.ReplyKeyboardRemove())
        return


@router.callback_query(F.data.startswith("write_"))
async def write_message_handler(call: types.CallbackQuery, state: FSMContext):
    if not await _ensure_admin_callback(call):
        return

    chat_id = call.data.split("_")[1]
    await call.message.delete_reply_markup()
    await call.message.delete()
    await call.message.answer(bm.please_type_message(), reply_markup=kb.cancel_keyboard())
    await state.set_state(Admin.write_message)
    await state.update_data(chat_id=chat_id)


@router.message(Admin.write_message)
async def write_message(message: types.Message, state: FSMContext):
    if not await _ensure_admin_message(message):
        await state.clear()
        return

    answer = message.text

    if answer == bm.cancel():
        await bot.send_message(message.chat.id, bm.canceled(), reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return
    data = await state.get_data()
    chat_id = data.get('chat_id')
    await state.clear()

    try:
        await bot.send_message(chat_id=chat_id,
                               text=answer)
        message_sent = await message.reply(bm.your_message_sent(), reply_markup=types.ReplyKeyboardRemove())

        await bot.delete_message(message.chat.id, message_sent.message_id)

        await message.answer(bm.your_message_sent(), reply_markup=kb.return_back_to_admin_keyboard())


    except Exception as e:
        logging.error(e)
        await message.reply(bm.something_went_wrong(),
                            reply_markup=kb.return_back_to_admin_keyboard())


async def clear_downloads_and_notify():
    try:
        message = await _cleanup_downloads_once()
    except Exception as e:
        message = f"An error occurred while clearing the folder: {e}"

    for admin_id in ADMINS_UID:
        try:
            await bot.send_message(chat_id=admin_id, text=message)
        except Exception as e:
            logging.error("Failed to send a message to admin %s: %s", admin_id, e)
