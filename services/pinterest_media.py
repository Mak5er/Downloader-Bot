import datetime
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse, urlunparse

from log.logger import logger as logging
from utils.cobalt_media import classify_cobalt_media_type
from utils.download_manager import (
    DownloadConfig,
    DownloadError,
    DownloadMetrics,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    ResilientDownloader,
)

logging = logging.bind(service="pinterest_media")


def strip_pinterest_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


def _derive_description(data: dict) -> str:
    output = data.get("output")
    if isinstance(output, dict):
        metadata = output.get("metadata")
        if isinstance(metadata, dict):
            title = metadata.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()
    description = data.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    return ""


@dataclass
class PinterestMedia:
    url: str
    type: str
    thumb: Optional[str] = None


@dataclass
class PinterestPost:
    id: str
    description: str
    media_list: list[PinterestMedia]


def _extract_pinterest_thumb(source: dict) -> Optional[str]:
    for key in ("thumb", "thumbnail", "poster", "preview", "cover"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def get_pinterest_preview_url(media: Optional[PinterestMedia]) -> Optional[str]:
    if not media:
        return None
    if media.type == "photo":
        return media.thumb or media.url
    return media.thumb or None


def parse_pinterest_post(data: dict) -> Optional[PinterestPost]:
    if not isinstance(data, dict):
        return None

    status = data.get("status")
    if status == "error":
        error_obj = data.get("error") or {}
        logging.error(
            "Cobalt Pinterest API error: code=%s context=%s",
            error_obj.get("code") if isinstance(error_obj, dict) else None,
            error_obj.get("context") if isinstance(error_obj, dict) else None,
        )
        return None

    if not status:
        if "url" in data:
            status = "tunnel"
        elif "picker" in data:
            status = "picker"

    media_list: list[PinterestMedia] = []

    if status in {"tunnel", "redirect"}:
        media_url = data.get("url")
        if isinstance(media_url, str) and media_url:
            media_list.append(
                PinterestMedia(
                    url=media_url,
                    type=classify_cobalt_media_type(
                        media_url,
                        filename=data.get("filename"),
                    ),
                    thumb=_extract_pinterest_thumb(data),
                )
            )
    elif status == "picker":
        for item in data.get("picker") or []:
            if not isinstance(item, dict):
                continue
            media_url = item.get("url")
            if not isinstance(media_url, str) or not media_url:
                continue
            media_list.append(
                PinterestMedia(
                    url=media_url,
                    type=classify_cobalt_media_type(
                        media_url,
                        declared_type=item.get("type"),
                    ),
                    thumb=_extract_pinterest_thumb(item),
                )
            )
    elif status == "local-processing":
        tunnels = data.get("tunnel") or []
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        for media_url in tunnels:
            if not isinstance(media_url, str) or not media_url:
                continue
            media_list.append(
                PinterestMedia(
                    url=media_url,
                    type=classify_cobalt_media_type(
                        media_url,
                        declared_type=data.get("type"),
                        filename=output.get("filename"),
                        mime_type=output.get("type"),
                    ),
                    thumb=_extract_pinterest_thumb(data) or _extract_pinterest_thumb(output),
                )
            )
    else:
        logging.error("Unsupported Cobalt Pinterest status: status=%s payload=%s", status, data)
        return None

    if not media_list:
        logging.error("Cobalt Pinterest response has no media items: payload=%s", data)
        return None

    return PinterestPost(
        id=str(int(datetime.datetime.now().timestamp())),
        description=_derive_description(data),
        media_list=media_list,
    )


class PinterestMediaService:
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
        self._downloader = ResilientDownloader(output_dir, config=config, source="pinterest")
        self._cobalt_api_url = cobalt_api_url
        self._cobalt_api_key = cobalt_api_key
        self._fetch_cobalt_data = fetch_cobalt_data_func
        self._retry_async_operation = retry_async_operation_func

    async def fetch_post(self, url: str) -> Optional[PinterestPost]:
        payload = {
            "url": url,
            "downloadMode": "auto",
            "videoQuality": "1080",
            "alwaysProxy": True,
            "localProcessing": "disabled",
        }
        data = await self._fetch_cobalt_data(
            self._cobalt_api_url,
            self._cobalt_api_key,
            payload,
            source="pinterest",
            timeout=20,
            attempts=3,
            retry_delay=0.0,
        )
        if not data:
            return None
        return parse_pinterest_post(data)

    async def download_media(
        self,
        url: str,
        filename: str,
        *,
        user_id: Optional[int] = None,
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
            logging.error("Error downloading Pinterest media: url=%s error=%s", url, exc)
            return None
