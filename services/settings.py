from __future__ import annotations

from typing import Final


SETTING_ENABLED: Final[str] = "on"
SETTING_DISABLED: Final[str] = "off"
TOGGLE_SETTING_VALUES: Final[frozenset[str]] = frozenset({SETTING_ENABLED, SETTING_DISABLED})

VIDEO_QUALITY_BEST: Final[str] = "best"
VIDEO_QUALITY_BALANCED: Final[str] = "balanced"
VIDEO_QUALITY_SAVER: Final[str] = "saver"
VIDEO_QUALITY_VALUES: Final[frozenset[str]] = frozenset(
    {VIDEO_QUALITY_BEST, VIDEO_QUALITY_BALANCED, VIDEO_QUALITY_SAVER}
)

AUDIO_FORMAT_MP3: Final[str] = "mp3"
AUDIO_FORMAT_M4A: Final[str] = "m4a"
AUDIO_FORMAT_BEST: Final[str] = "best"
AUDIO_FORMAT_VALUES: Final[frozenset[str]] = frozenset(
    {AUDIO_FORMAT_MP3, AUDIO_FORMAT_M4A, AUDIO_FORMAT_BEST}
)

SETTING_VALUES: Final[frozenset[str]] = frozenset(
    TOGGLE_SETTING_VALUES | VIDEO_QUALITY_VALUES | AUDIO_FORMAT_VALUES
)

SETTING_FIELDS: Final[tuple[str, ...]] = (
    "video_quality",
    "as_document",
    "audio_format",
    "captions",
    "delete_message",
    "info_buttons",
    "url_button",
    "audio_button",
    "file_button",
)

SETTING_LABELS: Final[tuple[tuple[str, str], ...]] = (
    ("🎬 Video Quality", "video_quality"),
    ("📄 Send as File", "as_document"),
    ("🎵 Audio Format", "audio_format"),
    ("📝 Descriptions", "captions"),
    ("ℹ️ Info Buttons", "info_buttons"),
    ("🎧 MP3 Button", "audio_button"),
    ("📄 File Button", "file_button"),
    ("🔗 URL Button", "url_button"),
    ("🗑️ Delete Messages", "delete_message"),
)


def is_valid_setting_field(field: str | None) -> bool:
    return isinstance(field, str) and field in SETTING_FIELDS


def normalize_setting_value(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in SETTING_VALUES:
        return normalized
    return None


def parse_settings_view_callback(data: str | None) -> str | None:
    if not isinstance(data, str) or not data.startswith("settings:"):
        return None
    field = data.split(":", 1)[1].strip()
    if not is_valid_setting_field(field):
        return None
    return field


def parse_setting_toggle_callback(data: str | None) -> tuple[str, str] | None:
    if not isinstance(data, str) or not data.startswith("setting:"):
        return None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None

    _, field, raw_value = parts
    normalized_value = normalize_setting_value(raw_value)
    if not is_valid_setting_field(field) or normalized_value is None:
        return None
    return field, normalized_value


def resolve_video_quality_format(video_quality: str | None) -> str:
    quality = normalize_setting_value(video_quality) or "best"
    if quality == "saver":
        return "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
    if quality == "balanced":
        return "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
    return "bestvideo+bestaudio/best"


def resolve_audio_format_codec(audio_format: str | None) -> str:
    fmt = normalize_setting_value(audio_format) or "mp3"
    if fmt == "m4a":
        return "m4a"
    if fmt == "best":
        return "flac"
    return "mp3"
