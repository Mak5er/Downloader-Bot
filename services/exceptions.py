from typing import Optional


class DownloaderBaseException(Exception):
    """Base exception class for all Downloader-Bot domain errors."""

    user_message: str = "An error occurred while processing your request."

    def __init__(self, message: str | None = None, user_message: str | None = None):
        super().__init__(message or user_message or self.user_message)
        if user_message:
            self.user_message = user_message


class PlatformExtractError(DownloaderBaseException):
    """Raised when metadata or media extraction fails for a platform."""

    def __init__(self, platform: str, reason: Optional[str] = None):
        self.platform = platform
        self.reason = reason
        detail = f"Failed to extract content from {platform}."
        if reason:
            detail += f" {reason}"
        super().__init__(
            message=detail,
            user_message=f"Couldn't process this {platform} link right now. Please try again later.",
        )


class MediaPrivateError(PlatformExtractError):
    """Raised when media is private, deleted, or requires login."""

    def __init__(self, platform: str):
        super().__init__(
            platform=platform,
            reason="Content is private, deleted, or restricted.",
        )
        self.user_message = "This post appears to be private, deleted, or restricted."


class MediaGeoBlockedError(PlatformExtractError):
    """Raised when content is region-blocked or unavailable."""

    def __init__(self, platform: str):
        super().__init__(
            platform=platform,
            reason="Content is region-restricted.",
        )
        self.user_message = "This content is not available in the current region."


class ContentTooLargeError(DownloaderBaseException):
    """Raised when remote media file size exceeds Telegram limits."""

    def __init__(self, size_bytes: int, limit_bytes: int):
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            message=f"Media size {size_bytes} exceeds limit {limit_bytes}.",
            user_message="The media file is too large for Telegram.",
        )
