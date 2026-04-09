import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional

from log.logger import logger as logging
from services.platforms.tiktok_common import (
    SHORT_HOSTS,
    TIKTOK_USER_AGENT,
    TikTokUser,
    TikTokVideo,
    build_tiktok_video_url,
    get_tiktok_audio_callback_data,
    get_tiktok_size_hint,
    get_video_id_from_url,
    is_invalid_tiktok_payload,
    strip_tiktok_tracking,
    video_info,
)
from services.platforms.tiktok_download_mixin import TikTokDownloadMixin
from services.platforms.tiktok_metadata_mixin import TikTokMetadataMixin
from services.platforms.tiktok_profile_mixin import TikTokProfileMixin
from services.platforms.tiktok_url_mixin import TikTokUrlResolverMixin

logging = logging.bind(service="tiktok_media")

__all__ = [
    "SHORT_HOSTS",
    "TIKTOK_USER_AGENT",
    "TikTokMediaService",
    "TikTokUser",
    "TikTokVideo",
    "build_tiktok_video_url",
    "get_tiktok_audio_callback_data",
    "get_tiktok_size_hint",
    "get_video_id_from_url",
    "is_invalid_tiktok_payload",
    "strip_tiktok_tracking",
    "video_info",
]


class TikTokMediaService(
    TikTokUrlResolverMixin,
    TikTokMetadataMixin,
    TikTokDownloadMixin,
    TikTokProfileMixin,
):
    def __init__(
        self,
        output_dir: str,
        *,
        get_http_session_func: Callable[[], Awaitable[object]],
        retry_async_operation_func: Callable[..., Awaitable[dict | Any | None]],
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

    @staticmethod
    def _monotonic() -> float:
        return time.monotonic()

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
