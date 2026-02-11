import asyncio
import json
import math
import os
import threading
import time
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, MutableMapping, Optional

import requests

from log.logger import logger as logging
from services.download_queue import (
    QueueBackpressureError,
    QueueRateLimitError,
    QueueTicket,
    get_download_queue,
)
from services.runtime_stats import record_download


class DownloadError(Exception):
    """Raised when a download fails after exhausting all retry attempts."""


class DownloadRateLimitError(DownloadError):
    """Raised when user hit per-user submission rate limit."""

    def __init__(self, retry_after: float):
        self.retry_after = max(0.0, retry_after)
        super().__init__(f"Rate limit exceeded. Retry after {self.retry_after:.1f}s")


class DownloadQueueBusyError(DownloadError):
    """Raised when queue is saturated for the current user."""

    def __init__(self, position: int):
        self.position = max(1, int(position))
        super().__init__(f"Queue is busy. Position {self.position}")


class DownloadTooLargeError(DownloadError):
    """Raised when remote file size exceeds configured max size."""

    def __init__(self, size: int, max_size: int):
        self.size = int(size)
        self.max_size = int(max_size)
        super().__init__(f"File too large: {self.size} > {self.max_size}")


@dataclass(slots=True)
class DownloadMetrics:
    """Return information about a completed download."""

    url: str
    path: str
    size: int
    elapsed: float
    used_multipart: bool
    resumed: bool


@dataclass(slots=True)
class DownloadProgress:
    """Lightweight progress snapshot for UI status updates."""

    downloaded_bytes: int
    total_bytes: int
    elapsed: float
    speed_bps: float
    eta_seconds: Optional[float]
    done: bool


ProgressCallback = Callable[[DownloadProgress], Awaitable[None] | None]


@dataclass(slots=True)
class DownloadConfig:
    """Configuration knobs for the resilient downloader."""

    chunk_size: int = 1024 * 1024  # 1 MiB chunks strike good throughput / memory balance
    multipart_threshold: int = 12 * 1024 * 1024  # Split downloads bigger than 12 MiB
    max_workers: int = 6  # Parallel range requests when supported
    max_concurrent_downloads: int = 3  # Prevent runaway thread creation under load
    head_timeout: float = 8.0
    stream_timeout: tuple[float, float] = (5.0, 60.0)
    max_retries: int = 3
    retry_backoff: float = 0.75
    allow_resume: bool = True
    temp_suffix: str = ".part"


@dataclass(slots=True)
class _ProgressState:
    total_bytes: int
    downloaded_bytes: int
    started_at: float
    last_emit_at: float
    callback: Optional[Callable[[DownloadProgress], None]]


class ResilientDownloader:
    """
    High-throughput downloader that supports HTTP range requests, retries and resume.

    The implementation keeps per-thread requests sessions so Range downloads can happen
    in parallel without recreating TCP connections on every chunk.
    """

    _thread_local: threading.local = threading.local()

    def __init__(
        self,
        output_dir: str,
        *,
        config: Optional[DownloadConfig] = None,
        default_headers: Optional[Mapping[str, str]] = None,
        source: str = "generic",
    ) -> None:
        self.output_dir = output_dir
        self.config = config or DownloadConfig()
        self._default_headers: MutableMapping[str, str] = dict(default_headers or {})
        self.source = source
        threshold_mb = os.getenv("DOWNLOAD_SUBPROCESS_THRESHOLD_MB", "0")
        try:
            threshold_mb_value = int(threshold_mb or "0")
        except ValueError:
            threshold_mb_value = 0
        self._subprocess_threshold_bytes = max(0, threshold_mb_value) * 1024 * 1024

    async def download(
        self,
        url: str,
        filename: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        skip_if_exists: bool = False,
        user_id: Optional[int] = None,
        source: Optional[str] = None,
        priority: Optional[int] = None,
        size_hint: Optional[int] = None,
        max_size_bytes: Optional[int] = None,
        on_queued: Optional[Callable[[QueueTicket], Awaitable[None] | None]] = None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> DownloadMetrics:
        """
        Public async entrypoint that streams the remote file to OUTPUT_DIR/filename.

        Returns metrics describing the completed download.
        """
        loop = asyncio.get_running_loop()
        progress_bridge = self._build_progress_bridge(loop, on_progress)
        headers_map = headers or {}
        use_subprocess = (
            self._subprocess_threshold_bytes > 0
            and (size_hint or 0) >= self._subprocess_threshold_bytes
            and on_progress is None
        )

        async def runner() -> DownloadMetrics:
            if use_subprocess:
                try:
                    return await self._download_subprocess(
                        url=url,
                        filename=filename,
                        headers=headers_map,
                        skip_if_exists=skip_if_exists,
                        max_size_bytes=max_size_bytes,
                    )
                except Exception as exc:
                    logging.warning(
                        "Subprocess download failed, falling back to thread mode: url=%s file=%s error=%s",
                        url,
                        filename,
                        exc,
                    )

            return await asyncio.to_thread(
                self._download_sync,
                url,
                filename,
                headers_map,
                skip_if_exists,
                progress_bridge,
                max_size_bytes,
            )

        queue = get_download_queue()
        queue_priority = self._resolve_priority(priority=priority, size_hint=size_hint)
        try:
            return await queue.submit(
                runner,
                priority=queue_priority,
                source=source or self.source,
                user_id=user_id,
                on_queued=on_queued,
            )
        except QueueRateLimitError as exc:
            raise DownloadRateLimitError(exc.retry_after) from exc
        except QueueBackpressureError as exc:
            raise DownloadQueueBusyError(exc.position) from exc

    @staticmethod
    def _resolve_priority(*, priority: Optional[int], size_hint: Optional[int]) -> int:
        if priority is not None:
            return int(priority)
        if size_hint is None or size_hint <= 0:
            return 40
        if size_hint <= 25 * 1024 * 1024:
            return 10
        if size_hint <= 120 * 1024 * 1024:
            return 25
        return 50

    @staticmethod
    def _build_progress_bridge(
        loop: asyncio.AbstractEventLoop,
        callback: Optional[ProgressCallback],
    ) -> Optional[Callable[[DownloadProgress], None]]:
        if callback is None:
            return None

        pending_tasks: set[asyncio.Task] = set()

        async def _emit(progress: DownloadProgress) -> None:
            try:
                maybe = callback(progress)
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception as exc:
                logging.debug("Progress callback failed: error=%s", exc)

        def _schedule(progress: DownloadProgress) -> None:
            def _runner() -> None:
                task = asyncio.create_task(_emit(progress))
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)

            loop.call_soon_threadsafe(_runner)

        return _schedule

    async def _download_subprocess(
        self,
        *,
        url: str,
        filename: str,
        headers: Mapping[str, str],
        skip_if_exists: bool,
        max_size_bytes: Optional[int] = None,
    ) -> DownloadMetrics:
        payload = {
            "url": url,
            "filename": filename,
            "headers": dict(headers or {}),
            "skip_if_exists": bool(skip_if_exists),
            "output_dir": self.output_dir,
            "source": self.source,
            "max_size_bytes": int(max_size_bytes) if max_size_bytes else 0,
            "config": {
                "chunk_size": self.config.chunk_size,
                "multipart_threshold": self.config.multipart_threshold,
                "max_workers": self.config.max_workers,
                "max_concurrent_downloads": self.config.max_concurrent_downloads,
                "head_timeout": self.config.head_timeout,
                "stream_timeout": list(self.config.stream_timeout),
                "max_retries": self.config.max_retries,
                "retry_backoff": self.config.retry_backoff,
                "allow_resume": self.config.allow_resume,
                "temp_suffix": self.config.temp_suffix,
            },
        }

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "services.download_worker_cli",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(json.dumps(payload).encode("utf-8"))
        if process.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise DownloadError(err or "download worker process failed")

        try:
            raw = json.loads((stdout or b"{}").decode("utf-8", errors="replace"))
            return DownloadMetrics(
                url=raw["url"],
                path=raw["path"],
                size=int(raw["size"]),
                elapsed=float(raw["elapsed"]),
                used_multipart=bool(raw["used_multipart"]),
                resumed=bool(raw["resumed"]),
            )
        except Exception as exc:
            raise DownloadError(f"invalid worker output: {exc}") from exc

    # ----------------------------------------------------------------------------------
    # Core synchronous implementation that is run inside a thread to avoid blocking
    # the event loop. All heavy lifting happens here.
    # ----------------------------------------------------------------------------------
    def _download_sync(
        self,
        url: str,
        filename: str,
        extra_headers: Mapping[str, str],
        skip_if_exists: bool,
        progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
        max_size_bytes: Optional[int] = None,
    ) -> DownloadMetrics:
        os.makedirs(self.output_dir, exist_ok=True)
        target_path = os.path.join(self.output_dir, filename)
        temp_path = f"{target_path}{self.config.temp_suffix}"

        if skip_if_exists and os.path.exists(target_path):
            size = os.path.getsize(target_path)
            logging.debug(
                "Download skipped because file already exists: url=%s path=%s size=%s",
                url,
                target_path,
                size,
            )
            return DownloadMetrics(
                url=url,
                path=target_path,
                size=size,
                elapsed=0.0,
                used_multipart=False,
                resumed=False,
            )

        headers = {**self._default_headers, **dict(extra_headers)}
        os.makedirs(os.path.dirname(target_path) or self.output_dir, exist_ok=True)

        start_time = time.monotonic()
        resumed = False
        existing_size = 0
        progress_state: Optional[_ProgressState] = None

        try:
            total_size, supports_range = self._probe(url, headers)
            if max_size_bytes and total_size > 0 and total_size > max_size_bytes:
                raise DownloadTooLargeError(total_size, max_size_bytes)
            use_multipart = supports_range and total_size >= self.config.multipart_threshold

            if self.config.allow_resume and os.path.exists(temp_path):
                existing_size = os.path.getsize(temp_path)
                if existing_size and supports_range:
                    headers.setdefault("Range", f"bytes={existing_size}-")
                    resumed = True
                    logging.debug(
                        "Resuming partial download: url=%s path=%s resume_from=%s total=%s",
                        url,
                        temp_path,
                        existing_size,
                        total_size,
                    )

            progress_state = _ProgressState(
                total_bytes=max(0, total_size),
                downloaded_bytes=existing_size if resumed else 0,
                started_at=start_time,
                last_emit_at=0.0,
                callback=progress_callback,
            )
            if max_size_bytes and progress_state.downloaded_bytes > max_size_bytes:
                raise DownloadTooLargeError(progress_state.downloaded_bytes, max_size_bytes)
            self._emit_progress(progress_state, force=False)

            if use_multipart and "Range" not in headers:
                self._download_multipart(
                    url,
                    temp_path,
                    target_path,
                    total_size,
                    headers,
                    progress_state=progress_state,
                )
            else:
                self._download_single(
                    url,
                    temp_path if resumed else target_path,
                    headers,
                    progress_state=progress_state,
                    max_size_bytes=max_size_bytes,
                )
                if resumed:
                    os.replace(temp_path, target_path)

            elapsed = time.monotonic() - start_time
            size = os.path.getsize(target_path)

            logging.info(
                "Download finished: url=%s path=%s size=%s elapsed=%.2fs multipart=%s resumed=%s",
                url,
                target_path,
                size,
                elapsed,
                use_multipart,
                resumed,
            )
            if progress_state:
                progress_state.downloaded_bytes = size
                progress_state.total_bytes = max(progress_state.total_bytes, size)
                self._emit_progress(progress_state, force=True, done=True)
            return DownloadMetrics(
                url=url,
                path=target_path,
                size=size,
                elapsed=elapsed,
                used_multipart=use_multipart,
                resumed=resumed,
            )
        except Exception as exc:
            logging.error("Download failed: url=%s path=%s error=%s", url, target_path, exc)
            self._cleanup_partial(temp_path, target_path)
            raise DownloadError(str(exc)) from exc

    # ----------------------------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------------------------
    def _probe(self, url: str, headers: Mapping[str, str]) -> tuple[int, bool]:
        """Issue a HEAD request to discover content length and Range support."""
        attempt = 0
        while True:
            session = self._get_session()
            try:
                response = session.head(
                    url,
                    headers=headers,
                    allow_redirects=True,
                    timeout=self.config.head_timeout,
                )
                response.raise_for_status()

                raw_size = response.headers.get("Content-Length") or "0"
                total_size = int(raw_size) if raw_size.isdigit() else 0
                supports_range = "bytes" in (response.headers.get("Accept-Ranges") or "").lower()

                logging.debug(
                    "Probe successful: url=%s size=%s supports_range=%s",
                    url,
                    total_size,
                    supports_range,
                )
                return total_size, supports_range
            except Exception as exc:
                attempt += 1
                if attempt > self.config.max_retries:
                    logging.warning(
                        "Probe failed, falling back to conservative download: url=%s error=%s",
                        url,
                        exc,
                    )
                    return 0, False
                sleep_for = self.config.retry_backoff * attempt
                logging.debug(
                    "HEAD probe retry: url=%s attempt=%s sleep=%.2fs error=%s",
                    url,
                    attempt,
                    sleep_for,
                    exc,
                )
                time.sleep(sleep_for)

    def _download_single(
        self,
        url: str,
        target_path: str,
        headers: Mapping[str, str],
        *,
        progress_state: Optional[_ProgressState] = None,
        max_size_bytes: Optional[int] = None,
    ) -> None:
        """Stream the full file sequentially."""
        backoff = self.config.retry_backoff
        for attempt in range(1, self.config.max_retries + 2):
            session = self._get_session()
            try:
                with session.get(
                    url,
                    headers=headers,
                    stream=True,
                    allow_redirects=True,
                    timeout=self.config.stream_timeout,
                ) as response:
                    response.raise_for_status()
                    if progress_state and progress_state.total_bytes <= 0:
                        content_length = response.headers.get("Content-Length") or "0"
                        if content_length.isdigit():
                            base = progress_state.downloaded_bytes if "Range" in headers else 0
                            progress_state.total_bytes = base + int(content_length)
                    if max_size_bytes and progress_state and progress_state.total_bytes > max_size_bytes > 0:
                        raise DownloadTooLargeError(progress_state.total_bytes, max_size_bytes)
                    with open(target_path, "ab" if "Range" in headers else "wb") as outfile:
                        for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                            if chunk:
                                outfile.write(chunk)
                                if progress_state:
                                    progress_state.downloaded_bytes += len(chunk)
                                    if max_size_bytes and progress_state.downloaded_bytes > max_size_bytes > 0:
                                        raise DownloadTooLargeError(progress_state.downloaded_bytes, max_size_bytes)
                                    self._emit_progress(progress_state, force=False)
                    if progress_state:
                        self._emit_progress(progress_state, force=True)
                return
            except Exception as exc:
                if attempt > self.config.max_retries:
                    raise
                sleep_for = backoff * attempt
                logging.warning(
                    "Sequential download retry: url=%s attempt=%s sleep=%.2fs error=%s",
                    url,
                    attempt,
                    sleep_for,
                    exc,
                )
                time.sleep(sleep_for)

    def _download_multipart(
        self,
        url: str,
        temp_path: str,
        target_path: str,
        total_size: int,
        headers: Mapping[str, str],
        *,
        progress_state: Optional[_ProgressState] = None,
    ) -> None:
        """Split the download into ranged chunks and fetch them in parallel."""
        ranges = self._split_ranges(total_size)
        os.makedirs(os.path.dirname(temp_path) or self.output_dir, exist_ok=True)
        with open(temp_path, "wb") as temp_file:
            temp_file.truncate(total_size)

        progress_lock = threading.Lock()

        def fetch_range(start: int, end: int) -> None:
            range_headers = dict(headers)
            range_headers["Range"] = f"bytes={start}-{end}"
            backoff = self.config.retry_backoff

            for attempt in range(1, self.config.max_retries + 2):
                session = self._get_session()
                try:
                    with session.get(
                        url,
                        headers=range_headers,
                        stream=True,
                        allow_redirects=True,
                        timeout=self.config.stream_timeout,
                    ) as response:
                        response.raise_for_status()
                        with open(temp_path, "r+b") as part_file:
                            part_file.seek(start)
                            for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                                if chunk:
                                    part_file.write(chunk)
                                    if progress_state:
                                        with progress_lock:
                                            progress_state.downloaded_bytes += len(chunk)
                                            self._emit_progress(progress_state, force=False)
                    return
                except Exception as exc:
                    if attempt > self.config.max_retries:
                        raise
                    sleep_for = backoff * attempt
                    logging.debug(
                        "Range fetch retry: url=%s range=%s-%s attempt=%s sleep=%.2fs error=%s",
                        url,
                        start,
                        end,
                        attempt,
                        sleep_for,
                        exc,
                    )
                    time.sleep(sleep_for)

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=min(self.config.max_workers, len(ranges))) as executor:
            futures = [executor.submit(fetch_range, start, end) for start, end in ranges]
            for future in as_completed(futures):
                future.result()

        os.replace(temp_path, target_path)
        if progress_state:
            self._emit_progress(progress_state, force=True)

    def _split_ranges(self, total_size: int) -> list[tuple[int, int]]:
        """Divide the download into evenly sized byte ranges."""
        if total_size <= 0:
            return [(0, 0)]

        part_size = max(
            self.config.multipart_threshold,
            math.ceil(total_size / max(1, self.config.max_workers * 2)),
        )
        ranges: list[tuple[int, int]] = []
        start = 0
        while start < total_size:
            end = min(start + part_size - 1, total_size - 1)
            ranges.append((start, end))
            start = end + 1
        return ranges

    def _emit_progress(self, state: _ProgressState, *, force: bool, done: bool = False) -> None:
        callback = state.callback
        if callback is None:
            return

        now = time.monotonic()
        if not force and now - state.last_emit_at < 0.8:
            return

        elapsed = max(0.001, now - state.started_at)
        speed = state.downloaded_bytes / elapsed
        eta: Optional[float] = None
        if state.total_bytes > 0 and speed > 0 and state.downloaded_bytes < state.total_bytes:
            eta = (state.total_bytes - state.downloaded_bytes) / speed

        callback(
            DownloadProgress(
                downloaded_bytes=max(0, state.downloaded_bytes),
                total_bytes=max(0, state.total_bytes),
                elapsed=elapsed,
                speed_bps=max(0.0, speed),
                eta_seconds=eta,
                done=done,
            )
        )
        state.last_emit_at = now

    def _cleanup_partial(self, temp_path: str, target_path: str) -> None:
        """Remove temporary files created by a failed download."""
        for path in (temp_path, target_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    logging.debug("Failed to remove partial file: path=%s", path)

    def _get_session(self) -> requests.Session:
        """Return a thread-local requests session with sane defaults."""
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.config.max_workers * 2,
                pool_maxsize=self.config.max_workers * 2,
                max_retries=0,
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            if self._default_headers:
                session.headers.update(self._default_headers)
            self._thread_local.session = session
        return session


def log_download_metrics(source: str, metrics: DownloadMetrics) -> None:
    """Log unified download stats for handlers."""
    try:
        size_mb = metrics.size / (1024 * 1024)
        logging.info(
            "Download metrics: source=%s url=%s path=%s size=%.2fMB elapsed=%.2fs multipart=%s resumed=%s",
            source,
            metrics.url,
            metrics.path,
            size_mb,
            metrics.elapsed,
            metrics.used_multipart,
            metrics.resumed,
        )
        record_download(source, metrics)
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.debug("Failed to log metrics: source=%s error=%s", source, exc)
