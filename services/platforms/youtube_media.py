import asyncio
import glob
import os
import re
import time
from typing import Any, Awaitable, Callable, Optional

from services.logger import logger as logging
from utils.download_manager import (
    DownloadError,
    DownloadConfig,
    DownloadMetrics,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    DownloadTooLargeError,
    ResilientDownloader,
    log_download_metrics,
)

logging = logging.bind(service="youtube_media")

YTDLP_FORMAT_720 = (
    "best[height<=720][ext=mp4][acodec!=none][vcodec!=none]/"
    "best[height<=720][acodec!=none][vcodec!=none]/"
    "bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]/"
    "bestvideo[height<=720]+bestaudio/"
    "best[height<=720]/best"
)
YTDLP_SPEED_OPTS: dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "continuedl": True,
    "overwrites": True,
    "noplaylist": True,
    "cachedir": False,
    "socket_timeout": 15,
    "retries": 2,
    "fragment_retries": 2,
    "concurrent_fragment_downloads": 4,
}


def _read_float_env(name: str) -> Optional[float]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        logging.warning("Ignoring invalid float env var: %s=%s", name, value)
        return None


YOUTUBE_INFO_TIMEOUT_SECONDS = _read_float_env("YTDLP_YOUTUBE_INFO_TIMEOUT_SECONDS") or 45.0


def _split_env_list(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;]", value) if item.strip()]


def _parse_cookies_from_browser(value: str) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    match = re.fullmatch(
        r"(?x)(?P<name>[^+:]+)(?:\s*\+\s*(?P<keyring>[^:]+))?"
        r"(?:\s*:\s*(?!:)(?P<profile>.+?))?(?:\s*::\s*(?P<container>.+))?",
        value.strip(),
    )
    if not match:
        raise ValueError(f"invalid cookies-from-browser value: {value}")
    browser_name, keyring, profile, container = match.group("name", "keyring", "profile", "container")
    return browser_name.lower(), profile, keyring.upper() if keyring else None, container


def build_ytdlp_youtube_options(**overrides: Any) -> dict[str, Any]:
    options = {**YTDLP_SPEED_OPTS}

    sleep_requests = _read_float_env("YTDLP_YOUTUBE_SLEEP_REQUESTS_SECONDS")
    if sleep_requests is not None:
        options["sleep_interval_requests"] = sleep_requests
    sleep_interval = _read_float_env("YTDLP_YOUTUBE_SLEEP_INTERVAL_SECONDS")
    if sleep_interval is not None:
        options["sleep_interval"] = sleep_interval
    max_sleep_interval = _read_float_env("YTDLP_YOUTUBE_MAX_SLEEP_INTERVAL_SECONDS")
    if max_sleep_interval is not None:
        options["max_sleep_interval"] = max_sleep_interval

    cookies_file = os.getenv("YTDLP_YOUTUBE_COOKIES_FILE")
    if cookies_file and cookies_file.strip():
        options["cookiefile"] = cookies_file.strip()

    cookies_from_browser = os.getenv("YTDLP_YOUTUBE_COOKIES_FROM_BROWSER")
    if cookies_from_browser and cookies_from_browser.strip():
        try:
            options["cookiesfrombrowser"] = _parse_cookies_from_browser(cookies_from_browser)
        except ValueError as exc:
            logging.warning("Ignoring %s", exc)

    extractor_args: dict[str, dict[str, list[str]]] = {}
    player_client = os.getenv("YTDLP_YOUTUBE_PLAYER_CLIENT")
    if player_client and player_client.strip():
        extractor_args.setdefault("youtube", {})["player_client"] = _split_env_list(player_client)
    po_token = os.getenv("YTDLP_YOUTUBE_PO_TOKEN")
    if po_token and po_token.strip():
        extractor_args.setdefault("youtube", {})["po_token"] = _split_env_list(po_token)
    if extractor_args:
        options["extractor_args"] = extractor_args

    options.update({key: value for key, value in overrides.items() if value is not None})
    return options


def get_youtube_thumbnail_url(yt: Optional[dict[str, Any]]) -> Optional[str]:
    if not yt:
        return None
    thumbnail = yt.get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail:
        return thumbnail
    thumbnails = yt.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in reversed(thumbnails):
            if isinstance(item, dict):
                url = item.get("url")
                if isinstance(url, str) and url:
                    return url
    video_id = yt.get("id")
    if isinstance(video_id, str) and video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return None


def get_video_stream(yt: dict, max_height: int = 720) -> dict | None:
    formats = yt.get("formats", [])
    progressive = [
        item for item in formats
        if item.get("vcodec") != "none"
        and item.get("acodec") != "none"
        and item.get("ext") == "mp4"
        and int(item.get("height") or 0) <= max_height
    ]
    progressive.sort(key=lambda item: int(item.get("height", 0)), reverse=True)
    if progressive:
        best = progressive[0]
        best["webpage_url"] = yt["webpage_url"]
        return best

    return None


def get_audio_stream(yt: dict) -> dict | None:
    formats = yt.get("formats", [])
    audio_streams = [
        item for item in formats
        if item.get("vcodec") == "none" and item.get("ext") in ("m4a", "mp4")
    ]
    audio_streams.sort(key=lambda item: float(item.get("abr") or 0), reverse=True)
    best = audio_streams[0] if audio_streams else None
    if best:
        best["webpage_url"] = yt["webpage_url"]
    return best


def is_manifest_stream(stream: dict) -> bool:
    protocol = (stream.get("protocol") or "").lower()
    manifest_url = stream.get("manifest_url") or stream.get("url") or ""
    return "m3u8" in protocol or "dash" in protocol or manifest_url.endswith(".m3u8")


class YouTubeMediaService:
    def __init__(
        self,
        output_dir: str,
        *,
        retry_async_operation_func: Callable[..., Awaitable[DownloadMetrics | None]],
        youtube_dl_factory: Callable[[dict[str, Any]], Any],
    ) -> None:
        self._output_dir = output_dir
        self._retry_async_operation = retry_async_operation_func
        self._youtube_dl_factory = youtube_dl_factory
        self._downloader = ResilientDownloader(
            output_dir,
            config=DownloadConfig(
                chunk_size=2 * 1024 * 1024,
                multipart_threshold=8 * 1024 * 1024,
                max_workers=10,
                retry_backoff=0.6,
            ),
            source="youtube",
        )

    def _run_ytdlp_download(self, url: str, ydl_opts: dict[str, Any]) -> None:
        with self._youtube_dl_factory(ydl_opts) as ydl:
            ydl.download([url])

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

    def get_youtube_video(self, url: str) -> Optional[dict[str, Any]]:
        try:
            ydl_opts = build_ytdlp_youtube_options(
                skip_download=True,
                ignore_no_formats_error=True,
            )
            with self._youtube_dl_factory(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:
            logging.error("Error fetching YouTube info: %s", exc)
            return None

    async def download_stream(
        self,
        stream: dict,
        filename: str,
        source: str,
        *,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        size_hint: Optional[int] = None,
        max_size_bytes: Optional[int] = None,
        on_queued=None,
        on_progress=None,
        on_retry=None,
    ) -> Optional[DownloadMetrics]:
        url = stream.get("url")
        if not url:
            logging.error("Stream missing URL for %s", source)
            return None

        headers = stream.get("http_headers") or {}
        try:
            kwargs = {"headers": headers}
            if user_id is not None:
                kwargs["user_id"] = user_id
            if chat_id is not None:
                kwargs["chat_id"] = chat_id
            if size_hint is not None:
                kwargs["size_hint"] = size_hint
            if max_size_bytes is not None:
                kwargs["max_size_bytes"] = max_size_bytes
            if on_queued is not None:
                kwargs["on_queued"] = on_queued
            if on_progress is not None:
                kwargs["on_progress"] = on_progress

            async def _download_once():
                return await self._downloader.download(url, filename, **kwargs)

            metrics = await self._retry_async_operation(
                _download_once,
                attempts=3,
                delay_seconds=2.0,
                retry_on_exception=lambda exc: not isinstance(
                    exc,
                    (DownloadRateLimitError, DownloadQueueBusyError, DownloadTooLargeError),
                ),
                on_retry=on_retry,
            )
            if metrics:
                log_download_metrics(source, metrics)
            return metrics
        except (DownloadRateLimitError, DownloadQueueBusyError, DownloadTooLargeError):
            raise
        except Exception as exc:
            logging.error("Failed to download stream: source=%s url=%s error=%s", source, url, exc)
            return None

    async def download_with_ytdlp(self, url: str, filename: str) -> Optional[str]:
        out_path = self._downloader._resolve_target_path(filename)
        os.makedirs(os.path.dirname(out_path) or self._output_dir, exist_ok=True)
        ydl_opts = build_ytdlp_youtube_options(
            format=YTDLP_FORMAT_720,
            outtmpl=out_path,
            merge_output_format="mp4",
        )
        try:
            await asyncio.to_thread(self._run_ytdlp_download, url, ydl_opts)
            resolved_path = self._resolve_downloaded_path(out_path)
            logging.info("yt-dlp fallback succeeded: url=%s path=%s", url, resolved_path)
            return resolved_path
        except Exception as exc:
            logging.error("yt-dlp fallback failed: url=%s error=%s", url, exc)
            return None

    async def download_with_ytdlp_metrics(
        self,
        url: str,
        filename: str,
        format_selector: str,
        source: str,
        *,
        max_filesize: Optional[int] = None,
    ) -> Optional[DownloadMetrics]:
        out_path = self._downloader._resolve_target_path(filename)
        os.makedirs(os.path.dirname(out_path) or self._output_dir, exist_ok=True)
        ydl_opts = build_ytdlp_youtube_options(
            format=format_selector,
            outtmpl=out_path,
            merge_output_format="mp4",
        )
        if max_filesize is not None:
            ydl_opts["max_filesize"] = int(max_filesize)
        start = time.monotonic()
        try:
            await asyncio.to_thread(self._run_ytdlp_download, url, ydl_opts)
            resolved_path = self._resolve_downloaded_path(out_path)
            elapsed = time.monotonic() - start
            metrics = DownloadMetrics(
                url=url,
                path=resolved_path,
                size=os.path.getsize(resolved_path),
                elapsed=elapsed,
                used_multipart=False,
                resumed=False,
            )
            log_download_metrics(source, metrics)
            return metrics
        except Exception as exc:
            logging.error("yt-dlp download failed: source=%s url=%s error=%s", source, url, exc)
            return None

    async def download_mp3_with_ytdlp_metrics(
        self,
        url: str,
        base_name: str,
        source: str,
        *,
        max_filesize: Optional[int] = None,
    ) -> Optional[DownloadMetrics]:
        base_path = os.path.join(self._output_dir, base_name)
        out_template = f"{base_path}.%(ext)s"
        final_path = f"{base_path}.mp3"
        ydl_opts = build_ytdlp_youtube_options(
            format="bestaudio/best",
            outtmpl=out_template,
            postprocessors=[{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            merge_output_format="mp3",
        )
        if max_filesize is not None:
            ydl_opts["max_filesize"] = int(max_filesize)
        start = time.monotonic()
        try:
            await asyncio.to_thread(self._run_ytdlp_download, url, ydl_opts)
            elapsed = time.monotonic() - start
            resolved_path = final_path if os.path.exists(final_path) else None
            if not resolved_path:
                matches = glob.glob(f"{base_path}.*")
                resolved_path = matches[0] if matches else None
            if not resolved_path or not os.path.exists(resolved_path):
                logging.error("yt-dlp mp3 output missing: url=%s base=%s", url, base_path)
                return None
            metrics = DownloadMetrics(
                url=url,
                path=resolved_path,
                size=os.path.getsize(resolved_path),
                elapsed=elapsed,
                used_multipart=False,
                resumed=False,
            )
            log_download_metrics(source, metrics)
            return metrics
        except Exception as exc:
            logging.error("yt-dlp mp3 download failed: source=%s url=%s error=%s", source, url, exc)
            return None

    async def download_media(self, url: str, filename: str, format_candidates: list[str]) -> bool:
        del format_candidates
        metrics = await self.download_stream({"url": url}, filename, "youtube_legacy")
        return metrics is not None
