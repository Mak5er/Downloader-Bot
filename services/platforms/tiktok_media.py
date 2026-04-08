import asyncio
import glob
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional
from urllib.parse import urlparse, urlunparse

import aiohttp
from yt_dlp.extractor.tiktok import TikTokIE

from log.logger import logger as logging
from services.download.queue import (
    QueueBackpressureError,
    QueueRateLimitError,
    get_download_queue,
)
from utils.download_manager import (
    DownloadError,
    DownloadProgress,
    DownloadMetrics,
    DownloadQueueBusyError,
    DownloadRateLimitError,
)

logging = logging.bind(service="tiktok_media")

TIKTOK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com", "vn.tiktok.com"}
URL_EXPAND_TIMEOUT = 4
URL_EXPAND_CACHE_MAXSIZE = 2048


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_non_empty_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def strip_tiktok_tracking(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


def get_video_id_from_url(url: str) -> str:
    path = urlparse(url).path or ""
    parts = [part for part in path.split("/") if part]
    if not parts:
        return ""
    return parts[-1].split("?")[0]


@dataclass
class TikTokVideo:
    id: str
    description: str
    cover: str
    views: int
    likes: int
    comments: int
    shares: int
    music_play_url: str
    author: str


@dataclass
class TikTokUser:
    nickname: str
    followers: int
    videos: int
    likes: int
    profile_pic: str
    description: str


async def video_info(data: dict) -> Optional[TikTokVideo]:
    if data.get("error"):
        logging.error("TikTok API error response: %s", data.get("error"))
        return None

    if data.get("code") != 0:
        logging.error(
            "TikTok API returned non-zero code: code=%s message=%s",
            data.get("code"),
            data.get("message"),
        )
        return None

    info = data.get("data", {})
    return TikTokVideo(
        id=info.get("id"),
        description=info.get("title", ""),
        cover=info.get("cover", ""),
        views=info.get("play_count", 0),
        likes=info.get("digg_count", 0),
        comments=info.get("comment_count", 0),
        shares=info.get("share_count", 0),
        music_play_url=info.get("music_info", {}).get("play", ""),
        author=info.get("author", {}).get("unique_id", ""),
    )


def is_invalid_tiktok_payload(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return True
    if payload.get("error"):
        return True
    return payload.get("code") not in (0, None)


def build_tiktok_video_url(info: TikTokVideo) -> str:
    return f"https://tiktok.com/@{info.author}/video/{info.id}"


def get_tiktok_audio_callback_data(info: TikTokVideo) -> Optional[str]:
    if info.author and info.id:
        return f"audio:tiktok:{info.author}:{info.id}"
    return None


def get_tiktok_size_hint(data: dict) -> Optional[int]:
    source_data = data.get("data", {}) if isinstance(data, dict) else {}
    for key in ("size_hd", "size", "wm_size"):
        raw = source_data.get(key)
        if isinstance(raw, (int, float)):
            return int(raw)
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
    return None


class TikTokMediaService:
    def __init__(
        self,
        output_dir: str,
        *,
        get_http_session_func: Callable[[], Awaitable[object]],
        retry_async_operation_func: Callable[..., Awaitable[dict | DownloadMetrics | None]],
        user_agent_factory: Callable[[], object],
        youtube_dl_factory: Callable[[dict[str, Any]], Any],
    ) -> None:
        self._output_dir = output_dir
        self._get_http_session = get_http_session_func
        self._retry_async_operation = retry_async_operation_func
        self._user_agent_factory = user_agent_factory
        self._youtube_dl_factory = youtube_dl_factory
        self._user_agent_provider: Optional[object] = None
        self._expanded_tiktok_url_cache: "OrderedDict[str, str]" = OrderedDict()
        self._expanded_tiktok_url_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_call_time = 0.0

    def _get_user_agent(self) -> str:
        if self._user_agent_provider is None:
            try:
                self._user_agent_provider = self._user_agent_factory()
            except Exception as exc:
                logging.debug("Failed to initialise UserAgent provider: %s", exc)
                self._user_agent_provider = None

        if self._user_agent_provider:
            try:
                return self._user_agent_provider.random
            except Exception as exc:
                logging.debug("Falling back to static User-Agent: %s", exc)
                self._user_agent_provider = None

        return TIKTOK_USER_AGENT

    async def _expand_tiktok_url_cached_async(self, url: str) -> str:
        cached = self._expanded_tiktok_url_cache.get(url)
        if cached is not None:
            self._expanded_tiktok_url_cache.move_to_end(url)
            return cached

        session = await self._get_http_session()
        headers = {"User-Agent": self._get_user_agent()}
        async with self._expanded_tiktok_url_lock:
            cached = self._expanded_tiktok_url_cache.get(url)
            if cached is not None:
                self._expanded_tiktok_url_cache.move_to_end(url)
                return cached

            async with session.head(
                url,
                allow_redirects=True,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=URL_EXPAND_TIMEOUT),
            ) as response:
                expanded = str(response.url) or url

            self._expanded_tiktok_url_cache[url] = expanded
            self._expanded_tiktok_url_cache.move_to_end(url)
            if len(self._expanded_tiktok_url_cache) > URL_EXPAND_CACHE_MAXSIZE:
                self._expanded_tiktok_url_cache.popitem(last=False)
            return expanded

    async def process_tiktok_url_async(self, text: str) -> str:
        def extract_tiktok_url(input_text: str) -> str:
            import re

            match = re.search(r"(https?://(?:www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", input_text)
            return match.group(0) if match else input_text

        url = strip_tiktok_tracking(extract_tiktok_url(text))
        logging.debug("TikTok URL extracted: raw=%s extracted=%s", text, url)

        try:
            parsed = urlparse(url)
            host = (parsed.netloc or "").lower()
            if host in SHORT_HOSTS:
                expanded = await self._expand_tiktok_url_cached_async(url)
                logging.debug("TikTok short URL expanded: raw=%s expanded=%s", url, expanded)
                return strip_tiktok_tracking(expanded)
            return strip_tiktok_tracking(url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logging.error("Error expanding TikTok URL: url=%s error=%s", url, exc)
            return strip_tiktok_tracking(url)

    def _build_ytdlp_options(self) -> dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "cachedir": False,
            "socket_timeout": 15,
            "retries": 2,
            "extractor_retries": 2,
        }

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

    def _extract_tiktok_detail_sync(self, video_url: str) -> tuple[dict[str, Any], int]:
        video_id = get_video_id_from_url(video_url)
        with self._youtube_dl_factory(self._build_ytdlp_options()) as ydl:
            extractor = TikTokIE(ydl)
            detail, status = extractor._extract_web_data_and_status(video_url, video_id, fatal=False)
        if not isinstance(detail, dict):
            detail = {}
        return detail, status

    async def _extract_tiktok_detail(self, video_url: str) -> tuple[dict[str, Any], int]:
        return await asyncio.to_thread(self._extract_tiktok_detail_sync, video_url)

    def _extract_cover_url(self, detail: dict[str, Any], image_urls: list[str]) -> str:
        image_post = detail.get("imagePost") if isinstance(detail.get("imagePost"), dict) else {}
        image_cover = image_post.get("cover") if isinstance(image_post, dict) else {}
        image_cover_url = self._extract_nested_url(image_cover)
        video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
        return _first_non_empty_str(
            image_cover_url,
            video.get("cover"),
            video.get("originCover"),
            video.get("dynamicCover"),
            image_urls[0] if image_urls else "",
        )

    @staticmethod
    def _extract_nested_url(payload: Any) -> str:
        if isinstance(payload, str):
            return payload.strip()
        if not isinstance(payload, dict):
            return ""
        image_url = payload.get("imageURL")
        if isinstance(image_url, dict):
            url_list = image_url.get("urlList")
            if isinstance(url_list, list):
                for candidate in url_list:
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate.strip()
        url_list = payload.get("UrlList")
        if isinstance(url_list, list):
            for candidate in url_list:
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        return ""

    def _extract_image_urls(self, detail: dict[str, Any]) -> list[str]:
        image_post = detail.get("imagePost")
        if not isinstance(image_post, dict):
            return []
        images = image_post.get("images")
        if not isinstance(images, list):
            return []
        image_urls: list[str] = []
        for image in images:
            if not isinstance(image, dict):
                continue
            url = self._extract_nested_url(image)
            if url:
                image_urls.append(url)
        return image_urls

    def _extract_video_play_url(self, detail: dict[str, Any]) -> str:
        video = detail.get("video")
        if not isinstance(video, dict):
            return ""

        direct_url = _first_non_empty_str(video.get("playAddr"), video.get("downloadAddr"))
        if direct_url:
            return direct_url

        play_addr_struct = video.get("PlayAddrStruct")
        if isinstance(play_addr_struct, dict):
            direct_url = self._extract_nested_url(play_addr_struct)
            if direct_url:
                return direct_url

        bitrate_info = video.get("bitrateInfo")
        if not isinstance(bitrate_info, list):
            return ""

        best_url = ""
        best_score = (-1, -1, -1)
        for entry in bitrate_info:
            if not isinstance(entry, dict):
                continue
            play_addr = entry.get("PlayAddr")
            if not isinstance(play_addr, dict):
                continue
            url = self._extract_nested_url(play_addr)
            if not url:
                continue
            score = (
                _safe_int(play_addr.get("DataSize")),
                _safe_int(play_addr.get("Height")),
                _safe_int(play_addr.get("Width")),
            )
            if score > best_score:
                best_score = score
                best_url = url
        return best_url

    def _extract_video_size(self, detail: dict[str, Any]) -> int:
        video = detail.get("video")
        if not isinstance(video, dict):
            return 0

        base_size = _safe_int(video.get("size"))
        if base_size > 0:
            return base_size

        play_addr_struct = video.get("PlayAddrStruct")
        if isinstance(play_addr_struct, dict):
            struct_size = _safe_int(play_addr_struct.get("DataSize"))
            if struct_size > 0:
                return struct_size

        bitrate_info = video.get("bitrateInfo")
        if not isinstance(bitrate_info, list):
            return 0

        max_size = 0
        for entry in bitrate_info:
            if not isinstance(entry, dict):
                continue
            play_addr = entry.get("PlayAddr")
            if not isinstance(play_addr, dict):
                continue
            max_size = max(max_size, _safe_int(play_addr.get("DataSize")))
        return max_size

    def _build_download_headers(
        self,
        *,
        referer: Optional[str],
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> dict[str, str]:
        headers = {
            "User-Agent": self._get_user_agent(),
            "Referer": referer or "https://www.tiktok.com/",
        }
        if extra_headers:
            for key, value in extra_headers.items():
                if isinstance(key, str) and isinstance(value, str) and value:
                    headers[key] = value
        return headers

    def _build_legacy_payload(self, video_url: str, detail: dict[str, Any]) -> dict[str, Any]:
        stats = detail.get("stats") if isinstance(detail.get("stats"), dict) else {}
        author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
        music = detail.get("music") if isinstance(detail.get("music"), dict) else {}
        image_urls = self._extract_image_urls(detail)
        webpage_url = strip_tiktok_tracking(video_url)
        return {
            "error": None,
            "code": 0,
            "message": "success",
            "data": {
                "id": _first_non_empty_str(detail.get("id"), get_video_id_from_url(video_url)),
                "title": _first_non_empty_str(detail.get("desc"), ""),
                "cover": self._extract_cover_url(detail, image_urls),
                "play_count": _safe_int(stats.get("playCount")),
                "digg_count": _safe_int(stats.get("diggCount")),
                "comment_count": _safe_int(stats.get("commentCount")),
                "share_count": _safe_int(stats.get("shareCount")),
                "music_info": {
                    "play": _first_non_empty_str(music.get("playUrl")),
                },
                "author": {
                    "unique_id": _first_non_empty_str(author.get("uniqueId"), author.get("id")),
                },
                "images": image_urls,
                "play": self._extract_video_play_url(detail),
                "download_headers": self._build_download_headers(referer=webpage_url),
                "audio_headers": self._build_download_headers(referer=webpage_url),
                "webpage_url": webpage_url,
                "size_hd": self._extract_video_size(detail),
                "size": self._extract_video_size(detail),
                "wm_size": self._extract_video_size(detail),
            },
        }

    async def fetch_tiktok_data(self, video_url: str) -> dict:
        async with self._request_lock:
            now = time.monotonic()
            wait_for = max(0.0, 1.0 - (now - self._last_call_time))
            self._last_call_time = now + wait_for

        if wait_for:
            await asyncio.sleep(wait_for)

        logging.debug("Fetching TikTok data via yt-dlp: url=%s", video_url)
        try:
            detail, status = await self._extract_tiktok_detail(video_url)
        except Exception as exc:
            logging.error("TikTok yt-dlp extraction failed: url=%s error=%s", video_url, exc)
            raise

        if status not in (0, None) or not detail:
            logging.warning(
                "TikTok yt-dlp returned invalid status: url=%s status=%s detail_keys=%s",
                video_url,
                status,
                list(detail.keys()) if isinstance(detail, dict) else None,
            )
            return {
                "error": f"status:{status}",
                "code": status or 1,
                "message": "TikTok post is unavailable",
                "data": {},
            }

        data = self._build_legacy_payload(video_url, detail)
        logging.debug(
            "Fetched TikTok data via yt-dlp: url=%s has_images=%s keys=%s",
            video_url,
            bool(data.get("data", {}).get("images")),
            list(data.get("data", {}).keys()),
        )
        return data

    async def fetch_tiktok_data_with_retry(self, video_url: str, *, on_retry=None) -> dict:
        async def _fetch_with_retry(target_url: str) -> dict:
            return await self._retry_async_operation(
                lambda: self.fetch_tiktok_data(target_url),
                attempts=3,
                delay_seconds=2.0,
                should_retry_result=is_invalid_tiktok_payload,
                on_retry=on_retry,
            )

        base_url = strip_tiktok_tracking(video_url)
        target_url = base_url
        if urlparse(base_url).netloc.lower() in SHORT_HOSTS:
            try:
                resolved_url = await asyncio.wait_for(self.process_tiktok_url_async(base_url), timeout=6.0)
            except Exception as exc:
                logging.warning("TikTok short URL expansion failed before fetch: url=%s error=%s", base_url, exc)
            else:
                if resolved_url:
                    target_url = resolved_url

        return await _fetch_with_retry(target_url)

    @staticmethod
    def _extract_source_data(download_data: Optional[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(download_data, dict):
            return {}
        inner_data = download_data.get("data")
        if isinstance(inner_data, dict):
            return inner_data
        return download_data

    async def _resolve_source_data(self, source_url: str, download_data: Optional[dict[str, Any]]) -> dict[str, Any]:
        source_data = self._extract_source_data(download_data)
        if source_data:
            return source_data
        payload = await self.fetch_tiktok_data_with_retry(source_url)
        return self._extract_source_data(payload)

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
    def _build_progress_bridge(
        loop: asyncio.AbstractEventLoop,
        callback,
    ):
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

    @staticmethod
    def _notify_progress(
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
                elapsed=max(0.001, time.monotonic() - started_at),
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
        started_at = time.monotonic()
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
                elapsed=time.monotonic() - started_at,
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
        started_at = time.monotonic()
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
                elapsed=time.monotonic() - started_at,
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
                retry_on_exception=lambda exc: not isinstance(
                    exc,
                    (DownloadRateLimitError, DownloadQueueBusyError),
                ),
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

    async def fetch_user_info(self, username: str) -> Optional[TikTokUser]:
        max_retries = 10
        retry_delay = 1.5
        exist_data: dict | None = None
        session = await self._get_http_session()
        headers = {"User-Agent": self._get_user_agent()}
        exist_url = f"https://countik.com/api/exist/{username}"

        try:
            sec_user_id = None
            for attempt in range(max_retries):
                try:
                    async with session.get(exist_url, headers=headers, timeout=10) as exist_response:
                        exist_response.raise_for_status()
                        exist_data = await exist_response.json(content_type=None)
                    sec_user_id = exist_data.get("sec_uid") if isinstance(exist_data, dict) else None
                    if sec_user_id:
                        break
                except Exception as exc:
                    logging.warning(
                        "TikTok user lookup retry failed: attempt=%s username=%s error=%s",
                        attempt + 1,
                        username,
                        exc,
                    )
                    await asyncio.sleep(retry_delay)
            else:
                logging.error("Failed to get TikTok user data after %s attempts: username=%s", max_retries, username)
                return None

            if not sec_user_id:
                logging.error("TikTok user lookup missing sec_user_id: username=%s", username)
                return None

            api_url = f"https://countik.com/api/userinfo?sec_user_id={sec_user_id}"
            async with session.get(api_url, headers=headers, timeout=10, allow_redirects=True) as api_response:
                api_response.raise_for_status()
                data = await api_response.json(content_type=None)

            exist_data = exist_data or {}
            return TikTokUser(
                nickname=exist_data.get("nickname", "No nickname found"),
                followers=data.get("followerCount", 0),
                videos=data.get("videoCount", 0),
                likes=data.get("heartCount", 0),
                profile_pic=data.get("avatarThumb", ""),
                description=data.get("signature", ""),
            )
        except Exception as exc:
            logging.error("Error fetching TikTok user info: username=%s error=%s", username, exc)
            return None
