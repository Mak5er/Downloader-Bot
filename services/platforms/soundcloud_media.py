import datetime
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse, urlunparse

from services.logger import logger as logging
from utils.download_manager import (
    DownloadConfig,
    DownloadError,
    DownloadMetrics,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    ResilientDownloader,
)

logging = logging.bind(service="soundcloud_media")


@dataclass
class SoundCloudTrack:
    id: str
    source_url: str
    audio_url: str
    title: str
    artist: str
    thumbnail_url: Optional[str] = None
    duration_seconds: int = 0


def strip_soundcloud_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


def _looks_like_image_url(url: str) -> bool:
    probe = (url or "").lower().split("?", 1)[0]
    return probe.endswith((".jpg", ".jpeg", ".png", ".webp", ".avif"))


def _looks_like_audio_url(url: str) -> bool:
    probe = (url or "").lower().split("?", 1)[0]
    return probe.endswith((".mp3", ".m4a", ".aac", ".ogg", ".wav", ".opus"))


def _derive_title(filename: Optional[str]) -> str:
    if not filename:
        return "SoundCloud Audio"
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stem = stem.replace("_", " ").replace("-", " ").strip()
    return stem or "SoundCloud Audio"


def _coerce_duration_seconds(value) -> int:
    try:
        duration = round(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0
    return duration if duration > 0 else 0


def _coerce_duration_milliseconds(value) -> int:
    try:
        duration = round(float(value) / 1000)
    except (TypeError, ValueError, OverflowError):
        return 0
    return duration if duration > 0 else 0


def parse_soundcloud_track(data: dict, source_url: str) -> Optional[SoundCloudTrack]:
    if not isinstance(data, dict):
        return None

    status = data.get("status")
    if status == "error":
        error = data.get("error") or {}
        logging.error(
            "Cobalt SoundCloud API error: code=%s context=%s",
            error.get("code") if isinstance(error, dict) else None,
            error.get("context") if isinstance(error, dict) else None,
        )
        return None

    audio_url: Optional[str] = None
    thumb_url: Optional[str] = None
    title = _derive_title(data.get("filename"))
    artist = ""
    duration_seconds = 0

    if status in {"tunnel", "redirect"}:
        maybe_url = data.get("url")
        if isinstance(maybe_url, str) and maybe_url:
            audio_url = maybe_url
    elif status == "picker":
        maybe_audio = data.get("audio")
        if isinstance(maybe_audio, str) and maybe_audio:
            audio_url = maybe_audio
        for item in data.get("picker") or []:
            if not isinstance(item, dict):
                continue
            maybe_thumb = item.get("thumb")
            if isinstance(maybe_thumb, str) and maybe_thumb and not thumb_url:
                thumb_url = maybe_thumb
    elif status == "local-processing":
        output = data.get("output") or {}
        metadata = output.get("metadata") if isinstance(output, dict) else {}
        if isinstance(metadata, dict):
            title = metadata.get("title") or title
            artist = metadata.get("artist") or ""
            duration_seconds = (
                _coerce_duration_seconds(metadata.get("duration"))
                or _coerce_duration_seconds(metadata.get("length"))
                or _coerce_duration_milliseconds(metadata.get("duration_ms"))
                or _coerce_duration_milliseconds(metadata.get("durationMs"))
            )

        for tunnel_url in data.get("tunnel") or []:
            if not isinstance(tunnel_url, str) or not tunnel_url:
                continue
            if _looks_like_image_url(tunnel_url):
                if not thumb_url:
                    thumb_url = tunnel_url
                continue
            if _looks_like_audio_url(tunnel_url) and not audio_url:
                audio_url = tunnel_url
                continue
            if not audio_url:
                audio_url = tunnel_url
    else:
        logging.error("Unsupported Cobalt SoundCloud response status: status=%s payload=%s", status, data)
        return None

    if not audio_url:
        logging.error("Cobalt SoundCloud response has no audio URL: status=%s payload=%s", status, data)
        return None

    return SoundCloudTrack(
        id=str(int(datetime.datetime.now().timestamp())),
        source_url=source_url,
        audio_url=audio_url,
        title=title,
        artist=artist,
        thumbnail_url=thumb_url,
        duration_seconds=duration_seconds,
    )


class SoundCloudMediaService:
    def __init__(
        self,
        output_dir: str,
        *,
        cobalt_api_url: str,
        cobalt_api_key: str,
        fetch_cobalt_data_func: Callable[..., Awaitable[dict | None]],
        retry_async_operation_func: Callable[..., Awaitable[DownloadMetrics | None]],
    ) -> None:
        config = DownloadConfig(
            chunk_size=1024 * 1024,
            multipart_threshold=16 * 1024 * 1024,
            max_workers=6,
            retry_backoff=0.8,
        )
        self._downloader = ResilientDownloader(output_dir, config=config, source="soundcloud")
        self._cobalt_api_url = cobalt_api_url
        self._cobalt_api_key = cobalt_api_key
        self._fetch_cobalt_data = fetch_cobalt_data_func
        self._retry_async_operation = retry_async_operation_func

    async def fetch_track(self, url: str) -> Optional[SoundCloudTrack]:
        payload = {
            "url": url,
            "downloadMode": "audio",
            "audioFormat": "mp3",
            "audioBitrate": "128",
            "alwaysProxy": True,
            "localProcessing": "preferred",
            "disableMetadata": False,
        }
        data = await self._fetch_cobalt_data(
            self._cobalt_api_url,
            self._cobalt_api_key,
            payload,
            source="soundcloud",
            timeout=20,
            attempts=3,
            retry_delay=0.0,
        )
        if not data:
            return None
        return parse_soundcloud_track(data, url)

    async def download_media(
        self,
        url: str,
        filename: str,
        *,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        request_id: Optional[str] = None,
        size_hint: Optional[int] = None,
        on_queued=None,
        on_progress=None,
        on_retry=None,
    ) -> Optional[DownloadMetrics]:
        async def _download_once():
            return await self._downloader.download(
                url,
                filename,
                user_id=user_id,
                chat_id=chat_id,
                request_id=request_id,
                size_hint=size_hint,
                on_queued=on_queued,
                on_progress=on_progress,
            )

        try:
            return await self._retry_async_operation(
                _download_once,
                attempts=3,
                delay_seconds=2.0,
                retry_on_exception=lambda exc: not isinstance(exc, (DownloadRateLimitError, DownloadQueueBusyError)),
                on_retry=on_retry,
            )
        except (DownloadRateLimitError, DownloadQueueBusyError):
            raise
        except DownloadError as exc:
            logging.error("Error downloading SoundCloud media: url=%s error=%s", url, exc)
            return None
