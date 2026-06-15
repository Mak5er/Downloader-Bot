from abc import ABC, abstractmethod
from typing import Any, Optional

from utils.download_manager import DownloadMetrics


class BasePlatformMediaService(ABC):
    """Abstract base for platform-specific media services (Instagram, TikTok, YouTube, etc.)."""

    @abstractmethod
    async def fetch_data(self, url: str) -> Optional[Any]:
        """Fetch metadata / media info for the given URL."""

    @abstractmethod
    async def download_media(
        self,
        url: str,
        filename: str,
        *,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        request_id: Optional[str] = None,
        size_hint: Optional[int] = None,
        on_queued: Any = None,
        on_progress: Any = None,
        on_retry: Any = None,
    ) -> Optional[DownloadMetrics]:
        """Download media from the resolved URL to OUTPUT_DIR/filename."""
