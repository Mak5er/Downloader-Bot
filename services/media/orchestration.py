import asyncio
from typing import Any, Awaitable, Callable, Optional, TypeVar

from utils.download_manager import (
    DownloadMetrics,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    DownloadTooLargeError,
)

T = TypeVar("T")

_single_media_inflight: dict[str, asyncio.Future[Optional[str]]] = {}
_single_media_inflight_lock: Optional[asyncio.Lock] = None


def _get_single_media_inflight_lock() -> asyncio.Lock:
    global _single_media_inflight_lock
    if _single_media_inflight_lock is None:
        _single_media_inflight_lock = asyncio.Lock()
    return _single_media_inflight_lock


async def _claim_single_media_inflight(cache_key: str) -> tuple[bool, asyncio.Future[Optional[str]]]:
    lock = _get_single_media_inflight_lock()
    async with lock:
        existing = _single_media_inflight.get(cache_key)
        if existing is None or existing.done():
            future: asyncio.Future[Optional[str]] = asyncio.get_running_loop().create_future()
            _single_media_inflight[cache_key] = future
            return True, future
        return False, existing


async def _resolve_single_media_inflight(
    cache_key: str,
    future: asyncio.Future[Optional[str]],
    file_id: Optional[str],
) -> None:
    lock = _get_single_media_inflight_lock()
    async with lock:
        current = _single_media_inflight.get(cache_key)
        if current is future:
            _single_media_inflight.pop(cache_key, None)
    if not future.done():
        future.set_result(file_id)


def reset_single_media_flow_tracking() -> None:
    _single_media_inflight.clear()


async def _maybe_await(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "__await__"):
        return await value
    return value


async def handle_download_backpressure(
    exc: Exception,
    *,
    business_id: Optional[int],
    on_rate_limit_reply: Callable[[float], Awaitable[None]],
    on_queue_busy_reply: Callable[[int], Awaitable[None]],
    on_business_error: Callable[[], Awaitable[None]],
) -> None:
    if business_id is not None:
        await on_business_error()
        return
    if isinstance(exc, DownloadRateLimitError):
        await on_rate_limit_reply(exc.retry_after)
        return
    if isinstance(exc, DownloadQueueBusyError):
        await on_queue_busy_reply(exc.position)
        return
    raise exc


async def run_single_media_flow(
    *,
    cache_key: str,
    cache_file_type: str,
    db_service: Any,
    upload_status_text: str,
    upload_action: str,
    update_status: Callable[[str], Awaitable[None]],
    send_chat_action: Callable[[str], Awaitable[None]],
    send_cached: Callable[[str], Awaitable[T]],
    download_media: Callable[[], Awaitable[DownloadMetrics | None]],
    send_downloaded: Callable[[str], Awaitable[T]],
    extract_file_id: Callable[[T], Optional[str]],
    cleanup_path: Callable[[str], Awaitable[None]],
    delete_status_message: Callable[[], Awaitable[None]],
    on_missing_media: Callable[[], Awaitable[None]],
    on_after_send: Optional[Callable[[], Awaitable[None]]] = None,
    inspect_metrics: Optional[Callable[[DownloadMetrics], Awaitable[bool] | bool]] = None,
    on_cache_store_error: Optional[Callable[[Exception], Awaitable[None] | None]] = None,
    on_rate_limit: Optional[Callable[[DownloadRateLimitError], Awaitable[None]]] = None,
    on_queue_busy: Optional[Callable[[DownloadQueueBusyError], Awaitable[None]]] = None,
    on_too_large: Optional[Callable[[DownloadTooLargeError], Awaitable[None]]] = None,
    on_unexpected_error: Optional[Callable[[Exception], Awaitable[None]]] = None,
) -> Optional[T]:
    metrics: DownloadMetrics | None = None
    inflight_future: asyncio.Future[Optional[str]] | None = None
    owns_inflight = False
    resolved_file_id: Optional[str] = None
    try:
        while True:
            cached_file_id = await db_service.get_file_id(cache_key)
            if cached_file_id:
                await update_status(upload_status_text)
                await send_chat_action(upload_action)
                sent_cached = await send_cached(cached_file_id)
                if on_after_send:
                    await on_after_send()
                return sent_cached

            owns_inflight, inflight_future = await _claim_single_media_inflight(cache_key)
            if owns_inflight:
                break

            resolved_file_id = await asyncio.shield(inflight_future)
            if resolved_file_id:
                await update_status(upload_status_text)
                await send_chat_action(upload_action)
                sent_cached = await send_cached(resolved_file_id)
                if on_after_send:
                    await on_after_send()
                return sent_cached

        metrics = await download_media()
        if not metrics:
            await on_missing_media()
            return None

        if inspect_metrics is not None:
            should_continue = await _maybe_await(inspect_metrics(metrics))
            if should_continue is False:
                return None

        await update_status(upload_status_text)
        await send_chat_action(upload_action)
        sent = await send_downloaded(metrics.path)

        resolved_file_id = extract_file_id(sent)
        if resolved_file_id:
            try:
                await db_service.add_file(cache_key, resolved_file_id, cache_file_type)
            except Exception as exc:
                if on_cache_store_error:
                    await _maybe_await(on_cache_store_error(exc))
        if on_after_send:
            await on_after_send()
        return sent
    except DownloadRateLimitError as exc:
        if on_rate_limit:
            await on_rate_limit(exc)
            return None
        raise
    except DownloadQueueBusyError as exc:
        if on_queue_busy:
            await on_queue_busy(exc)
            return None
        raise
    except DownloadTooLargeError as exc:
        if on_too_large:
            await on_too_large(exc)
            return None
        raise
    except Exception as exc:
        if on_unexpected_error:
            await on_unexpected_error(exc)
            return None
        raise
    finally:
        if owns_inflight and inflight_future is not None:
            await _resolve_single_media_inflight(cache_key, inflight_future, resolved_file_id)
        if metrics and metrics.path:
            await cleanup_path(metrics.path)
        await delete_status_message()


async def run_media_collection_flow(
    *,
    update_status: Callable[[str], Awaitable[None]],
    upload_status_text: str,
    fetch_entries: Callable[[], Awaitable[list[Any]]],
    send_entries: Callable[[list[Any]], Awaitable[None]],
    send_empty: Callable[[], Awaitable[None]],
    delete_status_message: Callable[[], Awaitable[None]],
    cleanup: Optional[Callable[[], Awaitable[None]]] = None,
    on_rate_limit: Optional[Callable[[DownloadRateLimitError], Awaitable[None]]] = None,
    on_queue_busy: Optional[Callable[[DownloadQueueBusyError], Awaitable[None]]] = None,
    on_too_large: Optional[Callable[[DownloadTooLargeError], Awaitable[None]]] = None,
    on_unexpected_error: Optional[Callable[[Exception], Awaitable[None]]] = None,
) -> None:
    try:
        entries = await fetch_entries()
        if entries:
            await update_status(upload_status_text)
            await send_entries(entries)
        else:
            await send_empty()
    except DownloadRateLimitError as exc:
        if on_rate_limit:
            await on_rate_limit(exc)
            return
        raise
    except DownloadQueueBusyError as exc:
        if on_queue_busy:
            await on_queue_busy(exc)
            return
        raise
    except DownloadTooLargeError as exc:
        if on_too_large:
            await on_too_large(exc)
            return
        raise
    except Exception as exc:
        if on_unexpected_error:
            await on_unexpected_error(exc)
            return
        raise
    finally:
        await delete_status_message()
        if cleanup:
            await cleanup()
