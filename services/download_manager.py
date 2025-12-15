import asyncio
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Mapping, MutableMapping, Optional

import requests

from log.logger import logger as logging


class DownloadError(Exception):
    """Raised when a download fails after exhausting all retry attempts."""


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
class DownloadConfig:
    """Configuration knobs for the resilient downloader."""

    chunk_size: int = 1024 * 1024  # 1 MiB chunks strike good throughput / memory balance
    multipart_threshold: int = 12 * 1024 * 1024  # Split downloads bigger than 12 MiB
    max_workers: int = 6  # Parallel range requests when supported
    head_timeout: float = 8.0
    stream_timeout: tuple[float, float] = (5.0, 60.0)
    max_retries: int = 3
    retry_backoff: float = 0.75
    allow_resume: bool = True
    temp_suffix: str = ".part"


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
    ) -> None:
        self.output_dir = output_dir
        self.config = config or DownloadConfig()
        self._default_headers: MutableMapping[str, str] = dict(default_headers or {})

    async def download(
        self,
        url: str,
        filename: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        skip_if_exists: bool = False,
    ) -> DownloadMetrics:
        """
        Public async entrypoint that streams the remote file to OUTPUT_DIR/filename.

        Returns metrics describing the completed download.
        """
        return await asyncio.to_thread(
            self._download_sync,
            url,
            filename,
            headers or {},
            skip_if_exists,
        )

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

        try:
            total_size, supports_range = self._probe(url, headers)
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

            if use_multipart and "Range" not in headers:
                self._download_multipart(url, temp_path, target_path, total_size, headers)
            else:
                self._download_single(url, temp_path if resumed else target_path, headers)
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

    def _download_single(self, url: str, target_path: str, headers: Mapping[str, str]) -> None:
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
                    with open(target_path, "ab" if "Range" in headers else "wb") as outfile:
                        for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                            if chunk:
                                outfile.write(chunk)
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
    ) -> None:
        """Split the download into ranged chunks and fetch them in parallel."""
        ranges = self._split_ranges(total_size)
        os.makedirs(os.path.dirname(temp_path) or self.output_dir, exist_ok=True)
        with open(temp_path, "wb") as temp_file:
            temp_file.truncate(total_size)

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

