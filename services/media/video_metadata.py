import asyncio
import json
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

from services.logger import logger as logging

logging = logging.bind(service="video_metadata")

_FFPROBE_COMMAND = (
    "ffprobe",
    "-v",
    "error",
    "-select_streams",
    "v:0",
    "-show_entries",
    "stream=width,height,sample_aspect_ratio,display_aspect_ratio",
    "-of",
    "json",
)


@dataclass(slots=True)
class TelegramVideoAttrs:
    width: Optional[int] = None
    height: Optional[int] = None
    supports_streaming: bool = True


def _parse_ratio(value: object) -> Optional[float]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or raw in {"N/A", "0:1"}:
        return None
    try:
        if ":" in raw:
            numerator, denominator = raw.split(":", 1)
            numerator_value = int(numerator)
            denominator_value = int(denominator)
            if denominator_value <= 0:
                return None
            return numerator_value / denominator_value
        fraction = Fraction(raw)
        if fraction.denominator == 0:
            return None
        return float(fraction)
    except (ArithmeticError, ValueError, ZeroDivisionError):
        return None


def _coerce_dimension(value: object) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_display_dimensions(
    width: Optional[int],
    height: Optional[int],
    *,
    sample_aspect_ratio: Optional[float],
    display_aspect_ratio: Optional[float],
) -> tuple[Optional[int], Optional[int]]:
    if not width or not height:
        return width, height

    target_ratio = display_aspect_ratio
    if target_ratio is None and sample_aspect_ratio is not None:
        target_ratio = (width / height) * sample_aspect_ratio

    if (
        target_ratio is None
        or not math.isfinite(target_ratio)
        or target_ratio <= 0
        or target_ratio < 0.2
        or target_ratio > 5.0
    ):
        return width, height

    encoded_ratio = width / height
    if abs(target_ratio - encoded_ratio) / max(target_ratio, encoded_ratio) < 0.015:
        return width, height

    adjusted_width = max(1, round(height * target_ratio))
    return adjusted_width, height


async def probe_telegram_video_attrs(path: Optional[str]) -> TelegramVideoAttrs:
    if not path:
        return TelegramVideoAttrs()

    try:
        process = await asyncio.create_subprocess_exec(
            *_FFPROBE_COMMAND,
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logging.debug("ffprobe is not available; skipping video metadata probe")
        return TelegramVideoAttrs()
    except Exception as exc:
        logging.debug("Failed to start ffprobe for %s: %s", path, exc)
        return TelegramVideoAttrs()

    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logging.debug(
            "ffprobe returned non-zero exit code for %s: %s",
            path,
            stderr.decode("utf-8", errors="ignore").strip(),
        )
        return TelegramVideoAttrs()

    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logging.debug("Failed to parse ffprobe payload for %s: %s", path, exc)
        return TelegramVideoAttrs()

    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        return TelegramVideoAttrs()

    stream = streams[0] if isinstance(streams[0], dict) else {}
    width = _coerce_dimension(stream.get("width"))
    height = _coerce_dimension(stream.get("height"))
    normalized_width, normalized_height = _normalize_display_dimensions(
        width,
        height,
        sample_aspect_ratio=_parse_ratio(stream.get("sample_aspect_ratio")),
        display_aspect_ratio=_parse_ratio(stream.get("display_aspect_ratio")),
    )

    if normalized_width != width or normalized_height != height:
        logging.info(
            "Adjusted Telegram video dimensions from probed aspect metadata: path=%s width=%s height=%s adjusted_width=%s adjusted_height=%s",
            path,
            width,
            height,
            normalized_width,
            normalized_height,
        )

    return TelegramVideoAttrs(
        width=normalized_width,
        height=normalized_height,
        supports_streaming=True,
    )


async def build_video_send_kwargs(path: Optional[str] = None) -> dict[str, object]:
    attrs = await probe_telegram_video_attrs(path)
    kwargs: dict[str, object] = {"supports_streaming": attrs.supports_streaming}
    if attrs.width:
        kwargs["width"] = attrs.width
    if attrs.height:
        kwargs["height"] = attrs.height
    return kwargs
