import asyncio
import glob
import os
from typing import Any, Callable, Optional

from services.logger import logger as logging
from services.download.queue import QueueBackpressureError, QueueRateLimitError, get_download_queue
from services.platforms.tiktok_common import _safe_int, get_video_id_from_url
from utils.download_manager import (
    DownloadError,
    DownloadMetrics,
    DownloadProgress,
    DownloadQueueBusyError,
    DownloadRateLimitError,
)
from utils import cobalt_client
from config import COBALT_API_URL, COBALT_API_KEY

logging = logging.bind(service="tiktok_media")


class TikTokDownloadMixin:
    DOWNLOAD_URL_TEMPLATE = "https://tikwm.com/video/media/play/{video_id}.mp4"
    DOWNLOAD_SERVICE_CYCLES = 3
    DOWNLOAD_CYCLE_DELAY_SECONDS = 2.0

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
            "retries": 0,
            "fragment_retries": 0,
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
            "format": (
                "best[ext=mp4][acodec!=none][vcodec!=none]/"
                "best[acodec!=none][vcodec!=none]/"
                "best*[ext=mp4][acodec!=none][vcodec!=none]/"
                "best*[acodec!=none][vcodec!=none]/"
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo+bestaudio/best"
            ),
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

    def _build_direct_download_headers(self, source_url: str, source_data: dict[str, Any], key: str) -> dict[str, str]:
        headers = {
            "User-Agent": self._get_user_agent(),
            "Referer": source_data.get("webpage_url") or source_url or "https://www.tiktok.com/",
        }
        extra_headers = source_data.get(key)
        if isinstance(extra_headers, dict):
            for header_key, header_value in extra_headers.items():
                if isinstance(header_key, str) and isinstance(header_value, str) and header_value:
                    headers[header_key] = header_value
        return headers

    @staticmethod
    def _append_unique_url(candidates: list[str], value: Any) -> None:
        if isinstance(value, str):
            url = value.strip()
            if url and url not in candidates:
                candidates.append(url)

    def _direct_video_candidates(self, source_url: str, source_data: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        for key in ("play", "wmplay", "hdplay"):
            self._append_unique_url(candidates, source_data.get(key))
        video_id = source_data.get("id") or get_video_id_from_url(source_url)
        if isinstance(video_id, str) and video_id.strip():
            self._append_unique_url(candidates, self.DOWNLOAD_URL_TEMPLATE.format(video_id=video_id.strip()))
        return candidates

    def _direct_audio_candidates(self, source_data: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        self._append_unique_url(candidates, source_data.get("music"))
        return candidates

    async def _download_direct_candidate(
        self,
        *,
        candidate_url: str,
        filename: str,
        headers: dict[str, str],
        size_hint: Optional[int],
        user_id: Optional[int],
        chat_id: Optional[int],
        request_id: Optional[str],
        on_queued,
        on_progress,
        on_retry,
    ) -> DownloadMetrics:
        async def _download_once() -> DownloadMetrics:
            return await self._downloader.download(
                candidate_url,
                filename,
                headers=headers,
                user_id=user_id,
                chat_id=chat_id,
                source="tiktok",
                request_id=request_id,
                size_hint=size_hint,
                on_queued=on_queued,
                on_progress=on_progress,
            )

        return await _download_once()

    async def _download_direct(
        self,
        *,
        candidates: list[str],
        filename: str,
        headers: dict[str, str],
        size_hint: Optional[int],
        user_id: Optional[int],
        chat_id: Optional[int],
        request_id: Optional[str],
        on_queued,
        on_progress,
        on_retry,
    ) -> Optional[DownloadMetrics]:
        last_error: Optional[Exception] = None
        for candidate_url in candidates:
            try:
                return await self._download_direct_candidate(
                    candidate_url=candidate_url,
                    filename=filename,
                    headers=headers,
                    size_hint=size_hint,
                    user_id=user_id,
                    chat_id=chat_id,
                    request_id=request_id,
                    on_queued=on_queued,
                    on_progress=on_progress,
                    on_retry=on_retry,
                )
            except (DownloadRateLimitError, DownloadQueueBusyError):
                raise
            except DownloadError as exc:
                last_error = exc
                logging.warning("TikTok direct candidate failed: url=%s error=%s", candidate_url, exc)
        if last_error:
            raise DownloadError(str(last_error)) from last_error
        return None

    async def _notify_download_cycle_retry(self, on_retry, failed_attempt: int, total_attempts: int, error) -> None:
        if on_retry is None:
            return
        try:
            maybe = on_retry(failed_attempt, total_attempts, error)
            if asyncio.iscoroutine(maybe):
                await maybe
        except Exception as exc:
            logging.debug("TikTok download retry callback failed: error=%s", exc)

    async def _download_via_cobalt(
        self,
        *,
        source_url: str,
        filename: str,
        size_hint: Optional[int],
        user_id: Optional[int],
        chat_id: Optional[int],
        request_id: Optional[str],
        on_queued,
        on_progress,
    ) -> Optional[DownloadMetrics]:
        if not COBALT_API_URL or not COBALT_API_KEY:
            return None

        data = await cobalt_client.fetch_cobalt_data(
            COBALT_API_URL,
            COBALT_API_KEY,
            {"url": source_url, "videoQuality": "1080", "filenameStyle": "basic"},
            source="tiktok",
            attempts=2,
            retry_delay=1.0,
        )
        if not data:
            return None

        status = data.get("status")
        video_url: Optional[str] = None

        if status in ("tunnel", "redirect"):
            video_url = data.get("url")
        elif status == "stream":
            video_url = data.get("url")

        if not video_url:
            logging.debug("Cobalt returned no usable URL for TikTok: status=%s", status)
            return None

        logging.info("Cobalt resolved TikTok URL: source=%s cobalt_url=%s", source_url, video_url)

        try:
            return await self._downloader.download(
                video_url,
                filename,
                headers={"User-Agent": self._get_user_agent()},
                user_id=user_id,
                chat_id=chat_id,
                source="tiktok_cobalt",
                request_id=request_id,
                size_hint=size_hint,
                on_queued=on_queued,
                on_progress=on_progress,
            )
        except (DownloadRateLimitError, DownloadQueueBusyError):
            raise
        except DownloadError as exc:
            logging.warning("Cobalt TikTok download failed: source=%s error=%s", source_url, exc)
            return None

    async def _download_with_service_cycle(
        self,
        *,
        direct_download: Callable[[], Any],
        ytdlp_download: Callable[[], Any],
        source_url: str,
        media_kind: str,
        on_retry,
    ) -> Optional[DownloadMetrics]:
        last_error: Optional[Exception] = None
        for cycle in range(1, self.DOWNLOAD_SERVICE_CYCLES + 1):
            try:
                direct_metrics = await direct_download()
                if direct_metrics:
                    logging.info(
                        "TikTok direct %s download succeeded: source_url=%s path=%s cycle=%s",
                        media_kind,
                        source_url,
                        direct_metrics.path,
                        cycle,
                    )
                    return direct_metrics
            except (DownloadRateLimitError, DownloadQueueBusyError):
                raise
            except DownloadError as exc:
                last_error = exc
                logging.warning(
                    "TikTok direct %s download failed, trying yt-dlp fallback: source_url=%s cycle=%s error=%s",
                    media_kind,
                    source_url,
                    cycle,
                    exc,
                )

            try:
                return await ytdlp_download()
            except (DownloadRateLimitError, DownloadQueueBusyError):
                raise
            except DownloadError as exc:
                last_error = exc
                if cycle >= self.DOWNLOAD_SERVICE_CYCLES:
                    break
                logging.warning(
                    "TikTok yt-dlp %s fallback failed, retrying service cycle: source_url=%s cycle=%s error=%s",
                    media_kind,
                    source_url,
                    cycle,
                    exc,
                )
                await self._notify_download_cycle_retry(on_retry, cycle, self.DOWNLOAD_SERVICE_CYCLES, exc)
                await asyncio.sleep(self.DOWNLOAD_CYCLE_DELAY_SECONDS)

        if last_error:
            raise DownloadError(str(last_error)) from last_error
        return None

    async def _submit_queued_ytdlp_download(
        self,
        *,
        source: str,
        size_hint: Optional[int],
        user_id: Optional[int],
        chat_id: Optional[int],
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
                attempts=1,
                retry_on_exception=lambda exc: not isinstance(exc, (DownloadRateLimitError, DownloadQueueBusyError)),
                on_retry=None,
            )

        try:
            return await queue.submit(
                _runner,
                priority=self._resolve_priority(size_hint),
                source=source,
                user_id=user_id,
                chat_id=chat_id,
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
        chat_id: Optional[int] = None,
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
            cobalt_result = await self._download_via_cobalt(
                source_url=source_url,
                filename=filename,
                size_hint=effective_size_hint,
                user_id=user_id,
                chat_id=chat_id,
                request_id=request_id,
                on_queued=on_queued,
                on_progress=on_progress,
            )
            if cobalt_result:
                logging.info("TikTok video downloaded via Cobalt: source_url=%s", source_url)
                return cobalt_result

            return await self._download_with_service_cycle(
                direct_download=lambda: self._download_direct(
                    candidates=self._direct_video_candidates(source_url, source_data),
                    filename=filename,
                    headers=self._build_direct_download_headers(source_url, source_data, "download_headers"),
                    size_hint=effective_size_hint,
                    user_id=user_id,
                    chat_id=chat_id,
                    request_id=request_id,
                    on_queued=on_queued,
                    on_progress=on_progress,
                    on_retry=on_retry,
                ),
                ytdlp_download=lambda: self._submit_queued_ytdlp_download(
                    source="tiktok",
                    size_hint=effective_size_hint,
                    user_id=user_id,
                    chat_id=chat_id,
                    request_id=request_id,
                    on_queued=on_queued,
                    on_progress=on_progress,
                    on_retry=on_retry,
                    sync_download=lambda progress_callback: self._download_video_with_ytdlp_sync(
                        source_url=source_url,
                        output_path=output_path,
                        progress_callback=progress_callback,
                    ),
                ),
                source_url=source_url,
                media_kind="video",
                on_retry=on_retry,
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
        chat_id: Optional[int] = None,
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
            return await self._download_with_service_cycle(
                direct_download=lambda: self._download_direct(
                    candidates=self._direct_audio_candidates(source_data),
                    filename=filename,
                    headers=self._build_direct_download_headers(source_url, source_data, "audio_headers"),
                    size_hint=effective_size_hint,
                    user_id=user_id,
                    chat_id=chat_id,
                    request_id=request_id,
                    on_queued=on_queued,
                    on_progress=on_progress,
                    on_retry=on_retry,
                ),
                ytdlp_download=lambda: self._submit_queued_ytdlp_download(
                    source="tiktok",
                    size_hint=effective_size_hint,
                    user_id=user_id,
                    chat_id=chat_id,
                    request_id=request_id,
                    on_queued=on_queued,
                    on_progress=on_progress,
                    on_retry=on_retry,
                    sync_download=lambda progress_callback: self._download_audio_with_ytdlp_sync(
                        source_url=source_url,
                        output_path=output_path,
                        progress_callback=progress_callback,
                    ),
                ),
                source_url=source_url,
                media_kind="audio",
                on_retry=on_retry,
            )
        except (DownloadRateLimitError, DownloadQueueBusyError):
            raise
        except DownloadError as exc:
            logging.error("Error downloading TikTok audio: source_url=%s error=%s", source_url, exc)
            return None
