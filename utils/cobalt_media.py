from typing import Optional


def classify_cobalt_media_type(
    media_url: str,
    *,
    audio_only: bool = False,
    declared_type: Optional[str] = None,
    filename: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> str:
    if audio_only:
        return "audio"

    if declared_type:
        normalized_type = declared_type.lower()
        if normalized_type in {"video", "gif", "merge", "mute", "remux"}:
            return "video"
        if normalized_type == "photo":
            return "photo"
        if normalized_type == "audio":
            return "audio"

    if mime_type:
        normalized_mime = mime_type.lower()
        if normalized_mime.startswith("video/"):
            return "video"
        if normalized_mime.startswith("image/"):
            return "photo"
        if normalized_mime.startswith("audio/"):
            return "audio"

    probe = f"{media_url} {filename or ''}".lower()
    if any(ext in probe for ext in (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".opus")):
        return "audio"
    if any(ext in probe for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif")):
        return "photo"
    return "video"
