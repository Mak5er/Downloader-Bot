from __future__ import annotations

from typing import Final


SETTING_ENABLED: Final[str] = "on"
SETTING_DISABLED: Final[str] = "off"
SETTING_VALUES: Final[frozenset[str]] = frozenset({SETTING_ENABLED, SETTING_DISABLED})

SETTING_FIELDS: Final[tuple[str, ...]] = (
    "captions",
    "delete_message",
    "info_buttons",
    "url_button",
    "audio_button",
)

SETTING_LABELS: Final[tuple[tuple[str, str], ...]] = (
    ("Descriptions", "captions"),
    ("Info Buttons", "info_buttons"),
    ("MP3 Button", "audio_button"),
    ("URL Button", "url_button"),
    ("Delete Messages", "delete_message"),
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
