"""Shared MP3 delivery pipeline used by the YouTube, SoundCloud and Spotify flows.

``run_audio_flow`` mirrors ``run_single_media_flow``: cached-file lookup,
optional metadata fetch, retryable download (supplied by the caller),
size check, MP3 metadata preparation, upload, cache store, and temp-file
cleanup in ``finally``.
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from services.logger import logger as logging
from utils.download_manager import DownloadMetrics


def _default_extract_file_id(sent: Any) -> Optional[str]:
    audio = getattr(sent, "audio", None)
    return audio.file_id if audio else None


@dataclass
class AudioFlowResult:
    file_id: Optional[str]
    sent_message: Any = None
    from_cache: bool = False


async def run_audio_flow(
    *,
    cache_key: str,
    db_service: Any,
    send_cached: Callable[[str], Awaitable[Any]],
    download_audio: Callable[[], Awaitable[Optional[DownloadMetrics]]],
    on_missing_audio: Callable[[], Awaitable[None]],
    max_file_size: int,
    on_too_large: Callable[[], Awaitable[None]],
    prepare_metadata: Callable[[str], Awaitable[Any]],
    send_downloaded: Callable[[str, Any], Awaitable[Any]],
    cleanup_path: Callable[[str], Awaitable[None]],
    fetch_metadata: Optional[Callable[[], Awaitable[bool]]] = None,
    extract_file_id: Callable[[Any], Optional[str]] = _default_extract_file_id,
    cache_file_type: str = "audio",
    on_cache_store_error: Optional[Callable[[Exception], Awaitable[None]]] = None,
    on_after_send: Optional[Callable[[AudioFlowResult], Awaitable[None]]] = None,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    chat_type: Optional[str] = None,
    service: Optional[str] = None,
    url: Optional[str] = None,
    title: Optional[str] = None,
) -> Optional[AudioFlowResult]:
    """Run the shared audio pipeline and return the delivery result."""
    async def _safe_record(
        *,
        status: str,
        file_id: Optional[str] = None,
        file_size_bytes: Optional[int] = None,
        duration_seconds: Optional[float] = None,
        error_message: Optional[str] = None,
    ) -> None:
        record_fn = getattr(db_service, "record_download", None)
        if not callable(record_fn) or user_id is None:
            return
        try:
            await record_fn(
                user_id=user_id,
                chat_id=chat_id,
                chat_type=chat_type,
                service=service or "unknown",
                url=url or cache_key,
                title=title,
                file_type=cache_file_type,
                file_id=file_id,
                file_size_bytes=file_size_bytes,
                duration_seconds=duration_seconds,
                status=status,
                error_message=error_message,
            )
        except Exception as exc:
            logging.debug("Failed to record download history in audio_flow: %s", exc)

    cached_file_id = await db_service.get_file_id(cache_key)
    if cached_file_id:
        sent = await send_cached(cached_file_id)
        result = AudioFlowResult(
            file_id=cached_file_id, sent_message=sent, from_cache=True
        )
        await _safe_record(status="cached", file_id=cached_file_id)
        if user_id and service:
            logging.download_cached(
                user_id=user_id,
                service=service,
                file_type=cache_file_type,
                url=url or cache_key,
            )
        if on_after_send:
            await on_after_send(result)
        return result

    if fetch_metadata is not None and not await fetch_metadata():
        return None

    metrics = await download_audio()
    if not metrics:
        await on_missing_audio()
        return None

    try:
        if metrics.size >= max_file_size:
            await on_too_large()
            return None

        prepared_metadata = await prepare_metadata(metrics.path)
        try:
            sent = await send_downloaded(metrics.path, prepared_metadata)
            file_id = extract_file_id(sent)
            if file_id:
                try:
                    await db_service.add_file(cache_key, file_id, cache_file_type)
                except Exception as exc:
                    if on_cache_store_error is None:
                        raise
                    await on_cache_store_error(exc)
            result = AudioFlowResult(file_id=file_id, sent_message=sent)
            await _safe_record(
                status="success",
                file_id=file_id,
                file_size_bytes=metrics.size,
                duration_seconds=metrics.elapsed,
            )
            if user_id and service:
                logging.download_success(
                    user_id=user_id,
                    service=service,
                    file_type=cache_file_type,
                    size_bytes=metrics.size,
                    duration_s=metrics.elapsed,
                    url=url or cache_key,
                )
            if on_after_send:
                await on_after_send(result)
            return result
        finally:
            if prepared_metadata is not None:
                prepared_metadata.cleanup()
    finally:
        await cleanup_path(metrics.path)
