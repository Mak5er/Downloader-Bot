from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class MediaItem(BaseModel):
    """Strongly typed model representing an individual downloadable media asset."""

    url: str
    media_type: Literal["video", "audio", "photo", "document"] = "video"
    filename: Optional[str] = None
    size_bytes: Optional[int] = None
    supports_range: bool = False
    thumbnail_url: Optional[str] = None
    duration_seconds: Optional[float] = None
    headers: Dict[str, str] = Field(default_factory=dict)


class MediaExtractResult(BaseModel):
    """Strongly typed extraction payload returned by platform media resolvers."""

    platform: str
    source_url: str
    title: Optional[str] = None
    author: Optional[str] = None
    caption: Optional[str] = None
    items: List[MediaItem] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return len(self.items) == 0

    @property
    def is_album(self) -> bool:
        return len(self.items) > 1
