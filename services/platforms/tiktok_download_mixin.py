import asyncio
import glob
import os
from typing import Any, Callable, Optional

from log.logger import logger as logging
from services.download.queue import QueueBackpressureError, QueueRateLimitError, get_download_queue
from services.platforms.tiktok_common import _safe_int
from utils.download_manager import (
    DownloadError,
    DownloadMetrics,
    DownloadProgress,
    DownloadQueueBusyError,
    DownloadRateLimitError,
)

logging = logging.bind(service="tiktok_media")


class TikTokDownloadMixin:
    def _build_ytdlp_download_options(self) -> dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "cachedir": False,
            "continuedl": True,
            "overwrites": True,
            "socket_timeout": 15,
            "retries": 2,
            "fragment_retries": 2,
            "concurrent_fragment_downloads": 4,
        }

    @staticmethod
    def _resolve_priority(size_hint: Optional[int]) -> int:
        if size_hint is None or size_hint <= 0:
            return 40
        if size_hint <= 25 * 1024 * 1024:
            return 10
        if size_hint <= 120 * 1024 * 1024:
            return 25
        return 50

    @staticmethod
    def _build_progress_bridge(loop: asyncio.AbstractEventLoop, callback):
        if callback is None:
            return None

        pending_tasks: set[asyncio.Task] = set()

        async def _emit(progress: DownloadProgress) -> None:
            try:
                maybe = callback(progress)
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception as exc:
                logging.debug("TikTok yt-dlp progress callback failed: error=%s", exc)

        def _schedule(progress: DownloadProgress) -> None:
            def _runner() -> None:
                task = asyncio.create_task(_emit(progress))
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)

            loop.call_soon_threadsafe(_runner)

        return _schedule

    def _notify_progress(
        self,
        progress_callback,
        *,
        downloaded_bytes: int,
        total_bytes: int,
        started_at: float,
        speed_bps: float,
        eta_seconds: Optional[float],
        done: bool,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(
            DownloadProgress(
                downloaded_bytes=max(0, int(downloaded_bytes)),
                total_bytes=max(0, int(total_bytes)),
                elapsed=max(0.001, self._monotonic() - started_at),
                speed_bps=max(0.0, float(speed_bps or 0.0)),
                eta_seconds=eta_seconds,
                done=done,
            )
        )

    def _build_progress_hook(self, progress_callback, started_at: float):
        def _hook(status: dict[str, Any]) -> None:
            if progress_callback is None:
                return
            state = status.get("status")
            downloaded = _safe_int(status.get("downloaded_bytes"))
            total = _safe_int(status.get("total_bytes")) or _safe_int(status.get("total_bytes_estimate"))
            speed = float(status.get("speed") or 0.0)
            eta = status.get("eta")
            eta_seconds = float(eta) if isinstance(eta, (int, float)) else None
            if state == "finished":
                total = total or downloaded
                self._notify_progress(
                    progress_callback,
                    downloaded_bytes=total,
                    total_bytes=total,
                    started_at=started_at,
                    speed_bps=speed,
                    eta_seconds=0.0,
                    done=True,
                )
                return
            if state == "downloading":
                self._notify_progress(
                    progress_callback,
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    started_at=started_at,
                    speed_bps=speed,
                    eta_seconds=eta_seconds,
                    done=False,
                )

        return _hook

    def _run_ytdlp_download(self, url: str, ydl_opts: dict[str, Any]) -> None:
        with self._youtube_dl_factory(ydl_opts) as ydl:
            ydl.download([url])

    @staticmethod
    def _cleanup_paths(*paths: str) -> None:
        seen: set[str] = set()
        for pattern in paths:
            if not pattern:
                continue
            for path in glob.glob(pattern):
                normalized = os.path.abspath(path)
                if normalized in seen:
                    continue
                seen.add(normalized)
                try:
                    if os.path.exists(normalized):
                        os.remove(normalized)
                except OSError:
                    logging.debug("Failed to clean up TikTok yt-dlp artifact: path=%s", normalized)

    @staticmethod
    def _resolve_downloaded_path(expected_path: str) -> str:
        if os.path.exists(expected_path):
            return expected_path
        stem, ext = os.path.splitext(expected_path)
        matches = sorted(glob.glob(f"{stem}*{ext}") + glob.glob(f"{stem}.*"))
        for match in matches:
            if os.path.isfile(match):
                return match
        raise DownloadError(f"yt-dlp output file missing: {expected_path}")

    def _download_video_with_ytdlp_sync(
        self,
        *,
        source_url: str,
        output_path: str,
        progress_callback=None,
    ) -> DownloadMetrics:
        started_at = self._monotonic()
        ydl_opts = {
            **self._build_ytdlp_download_options(),
            "format": "best[ext=mp4]/best",
            "outtmpl": output_path,
            "merge_output_format": "mp4",
            "progress_hooks": [self._build_progress_hook(progress_callback, started_at)],
        }
        try:
            self._run_ytdlp_download(source_url, ydl_opts)
            resolved_path = self._resolve_downloaded_path(output_path)
            size = os.path.getsize(resolved_path)
            self._notify_progress(
                progress_callback,
                downloaded_bytes=size,
                total_bytes=size,
                started_at=started_at,
                speed_bps=0.0,
                eta_seconds=0.0,
                done=True,
            )
            return DownloadMetrics(
                url=source_url,
                path=resolved_path,
                size=size,
                elapsed=self._monotonic() - started_at,
                used_multipart=False,
                resumed=False,
            )
        except Exception as exc:
            self._cleanup_paths(output_path, f"{os.path.splitext(output_path)[0]}.*")
            raise DownloadError(str(exc)) from exc

    def _download_audio_with_ytdlp_sync(
        self,
        *,
        source_url: str,
        output_path: str,
        progress_callback=None,
    ) -> DownloadMetrics:
        started_at = self._monotonic()
        base_path, _ = os.path.splitext(output_path)
        out_template = f"{base_path}.%(ext)s"
        ydl_opts = {
            **self._build_ytdlp_download_options(),
            "format": "bestaudio/best",
            "outtmpl": out_template,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "merge_output_format": "mp3",
            "progress_hooks": [self._build_progress_hook(progress_callback, started_at)],
        }
        try:
            self._run_ytdlp_download(source_url, ydl_opts)
            resolved_path = self._resolve_downloaded_path(output_path)
            size = os.path.getsize(resolved_path)
            self._notify_progress(
                progress_callback,
                downloaded_bytes=size,
                total_bytes=size,
                started_at=started_at,
                speed_bps=0.0,
                eta_seconds=0.0,
                done=True,
            )
            return DownloadMetrics(
                url=source_url,
                path=resolved_path,
                size=size,
                elapsed=self._monotonic() - started_at,
                used_multipart=False,
                resumed=False,
            )
        except Exception as exc:
            self._cleanup_paths(output_path, f"{base_path}.*")
            raise DownloadError(str(exc)) from exc

    async def _submit_queued_ytdlp_download(
        self,
        *,
        source: str,
        size_hint: Optional[int],
        user_id: Optional[int],
        request_id: Optional[str],
        on_queued,
        on_progress,
        on_retry,
        sync_download: Callable[[Any], DownloadMetrics],
    ) -> DownloadMetrics:
        loop = asyncio.get_running_loop()
        progress_bridge = self._build_progress_bridge(loop, on_progress)
        queue = get_download_queue()

        async def _runner() -> DownloadMetrics:
            return await self._retry_async_operation(
                lambda: asyncio.to_thread(sync_download, progress_bridge),
                attempts=3,
                delay_seconds=2.0,
                retry_on_exception=lambda exc: not isinstance(exc, (DownloadRateLimitError, DownloadQueueBusyError)),
                on_retry=on_retry,
            )

        try:
            return await queue.submit(
                _runner,
                priority=self._resolve_priority(size_hint),
                source=source,
                user_id=user_id,
                request_id=request_id,
                on_queued=on_queued,
            )
        except QueueRateLimitError as exc:
            raise DownloadRateLimitError(exc.retry_after) from exc
        except QueueBackpressureError as exc:
            raise DownloadQueueBusyError(exc.position) from exc

    async def download_video(
        self,
        source_url: str,
        filename: str,
        *,
        download_data: Optional[dict[str, Any]] = None,
        user_id: Optional[int] = None,
        request_id: Optional[str] = None,
        size_hint: Optional[int] = None,
        on_queued=None,
        on_progress=None,
        on_retry=None,
    ) -> Optional[DownloadMetrics]:
        source_data = await self._resolve_source_data(source_url, download_data)
        effective_size_hint = size_hint or _safe_int(source_data.get("size_hd"))
        output_path = os.path.join(self._output_dir, filename)

        try:
            return await self._submit_queued_ytdlp_download(
                source="tiktok",
                size_hint=effective_size_hint,
                user_id=user_id,
                request_id=request_id,
                on_queued=on_queued,
                on_progress=on_progress,
                on_retry=on_retry,
                sync_download=lambda progress_callback: self._download_video_with_ytdlp_sync(
                    source_url=source_url,
                    output_path=output_path,
                    progress_callback=progress_callback,
                ),
            )
        except (DownloadRateLimitError, DownloadQueueBusyError):
            raise
        except DownloadError as exc:
            logging.error("Error downloading TikTok video: source_url=%s error=%s", source_url, exc)
            return None

    async def download_audio(
        self,
        source_url: str,
        filename: str,
        *,
        download_data: Optional[dict[str, Any]] = None,
        user_id: Optional[int] = None,
        request_id: Optional[str] = None,
        size_hint: Optional[int] = None,
        on_queued=None,
        on_progress=None,
        on_retry=None,
    ) -> Optional[DownloadMetrics]:
        source_data = await self._resolve_source_data(source_url, download_data)
        effective_size_hint = size_hint or _safe_int(source_data.get("audio_size"))
        output_path = os.path.join(self._output_dir, filename)

        try:
            return await self._submit_queued_ytdlp_download(
                source="tiktok",
                size_hint=effective_size_hint,
                user_id=user_id,
                request_id=request_id,
                on_queued=on_queued,
                on_progress=on_progress,
                on_retry=on_retry,
                sync_download=lambda progress_callback: self._download_audio_with_ytdlp_sync(
                    source_url=source_url,
                    output_path=output_path,
                    progress_callback=progress_callback,
                ),
            )
        except (DownloadRateLimitError, DownloadQueueBusyError):
            raise
        except DownloadError as exc:
            logging.error("Error downloading TikTok audio: source_url=%s error=%s", source_url, exc)
            return None
