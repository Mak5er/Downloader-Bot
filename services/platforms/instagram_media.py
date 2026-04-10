import datetime
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse, urlunparse

from services.logger import logger as logging
from utils.cobalt_media import classify_cobalt_media_type
from utils.download_manager import (
    DownloadConfig,
    DownloadError,
    DownloadMetrics,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    ResilientDownloader,
)

logging = logging.bind(service="instagram_media")


def strip_instagram_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


@dataclass
class InstagramMedia:
    url: str
    type: str
    thumb: Optional[str] = None


@dataclass
class InstagramVideo:
    id: str
    description: str
    author: str
    media_list: list[InstagramMedia]


def _extract_instagram_thumb(source: dict) -> Optional[str]:
    for key in ("thumb", "thumbnail", "poster", "preview", "cover"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def get_instagram_preview_url(media: Optional[InstagramMedia]) -> Optional[str]:
    if not media:
        return None
    if media.type == "photo":
        return media.url
    return media.thumb or None


class InstagramMediaService:
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
        self._downloader = ResilientDownloader(output_dir, config=config, source="instagram")
        self._cobalt_api_url = cobalt_api_url
        self._cobalt_api_key = cobalt_api_key
        self._fetch_cobalt_data = fetch_cobalt_data_func
        self._retry_async_operation = retry_async_operation_func

    async def fetch_data(self, url: str, audio_only: bool = False) -> Optional[InstagramVideo]:
        payload = {
            "url": url,
            "videoQuality": "720",
            "downloadMode": "audio" if audio_only else "auto",
            "alwaysProxy": True,
            "localProcessing": "disabled",
        }
        data = await self._fetch_cobalt_data(
            self._cobalt_api_url,
            self._cobalt_api_key,
            payload,
            source="instagram",
            timeout=15,
            attempts=3,
            retry_delay=0.0,
        )
        if not data:
            return None

        media_list = []
        status = data.get("status")

        if not status:
            if "url" in data:
                status = "tunnel"
            elif "picker" in data:
                status = "picker"

        if status in {"tunnel", "redirect"}:
            media_url = data.get("url")
            if isinstance(media_url, str) and media_url:
                media_list.append(
                    InstagramMedia(
                        url=media_url,
                        type=classify_cobalt_media_type(
                            media_url,
                            audio_only=audio_only,
                            filename=data.get("filename"),
                        ),
                        thumb=_extract_instagram_thumb(data),
                    )
                )
        elif status == "picker":
            picker_audio_url = data.get("audio")
            if audio_only and isinstance(picker_audio_url, str) and picker_audio_url:
                media_list.append(InstagramMedia(url=picker_audio_url, type="audio"))
            else:
                picker_items = data.get("picker") or []
                for item in picker_items:
                    if not isinstance(item, dict):
                        continue
                    media_url = item.get("url")
                    if not isinstance(media_url, str) or not media_url:
                        continue
                    media_list.append(
                        InstagramMedia(
                            url=media_url,
                            type=classify_cobalt_media_type(
                                media_url,
                                audio_only=audio_only,
                                declared_type=item.get("type"),
                            ),
                            thumb=_extract_instagram_thumb(item),
                        )
                    )
        elif status == "local-processing":
            tunnel_urls = data.get("tunnel") or []
            output = data.get("output") or {}
            if not isinstance(tunnel_urls, list) or not tunnel_urls:
                logging.error("Cobalt local-processing response has no tunnels: payload=%s", data)
                return None
            if not audio_only and len(tunnel_urls) > 1:
                logging.error(
                    "Unsupported Cobalt local-processing payload for Instagram: type=%s tunnel_count=%s",
                    data.get("type"),
                    len(tunnel_urls),
                )
                return None
            for media_url in tunnel_urls:
                if not isinstance(media_url, str) or not media_url:
                    continue
                media_list.append(
                    InstagramMedia(
                        url=media_url,
                        type=classify_cobalt_media_type(
                            media_url,
                            audio_only=audio_only,
                            declared_type=data.get("type"),
                            filename=output.get("filename") if isinstance(output, dict) else None,
                            mime_type=output.get("type") if isinstance(output, dict) else None,
                        ),
                        thumb=_extract_instagram_thumb(data) or _extract_instagram_thumb(output),
                    )
                )
        elif status == "error":
            error_obj = data.get("error") or {}
            logging.error(
                "Cobalt API returned error: code=%s context=%s",
                error_obj.get("code") if isinstance(error_obj, dict) else None,
                error_obj.get("context") if isinstance(error_obj, dict) else None,
            )
            return None
        else:
            logging.error("Unsupported Cobalt response status: status=%s payload=%s", status, data)
            return None

        if not media_list:
            logging.error("Cobalt response has no media items: status=%s payload=%s", status, data)
            return None

        return InstagramVideo(
            id=str(int(datetime.datetime.now().timestamp())),
            description="",
            author="instagram_user",
            media_list=media_list,
        )

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
            logging.error("Error downloading Instagram media: url=%s error=%s", url, exc)
            return None
