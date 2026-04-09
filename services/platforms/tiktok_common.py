from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

from log.logger import logger as logging

logging = logging.bind(service="tiktok_media")

TIKTOK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com", "vn.tiktok.com"}
URL_EXPAND_TIMEOUT = 4
URL_EXPAND_CACHE_MAXSIZE = 2048


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_non_empty_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def strip_tiktok_tracking(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


def get_video_id_from_url(url: str) -> str:
    path = urlparse(url).path or ""
    parts = [part for part in path.split("/") if part]
    if not parts:
        return ""
    return parts[-1].split("?")[0]


@dataclass
class TikTokVideo:
    id: str
    description: str
    cover: str
    views: int
    likes: int
    comments: int
    shares: int
    music_play_url: str
    author: str


@dataclass
class TikTokUser:
    nickname: str
    followers: int
    videos: int
    likes: int
    profile_pic: str
    description: str


async def video_info(data: dict) -> Optional[TikTokVideo]:
    if data.get("error"):
        logging.error("TikTok API error response: %s", data.get("error"))
        return None

    if data.get("code") != 0:
        logging.error(
            "TikTok API returned non-zero code: code=%s message=%s",
            data.get("code"),
            data.get("message"),
        )
        return None

    info = data.get("data", {})
    return TikTokVideo(
        id=info.get("id"),
        description=info.get("title", ""),
        cover=info.get("cover", ""),
        views=info.get("play_count", 0),
        likes=info.get("digg_count", 0),
        comments=info.get("comment_count", 0),
        shares=info.get("share_count", 0),
        music_play_url=info.get("music_info", {}).get("play", ""),
        author=info.get("author", {}).get("unique_id", ""),
    )


def is_invalid_tiktok_payload(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return True
    if payload.get("error"):
        return True
    return payload.get("code") not in (0, None)


def build_tiktok_video_url(info: TikTokVideo) -> str:
    return f"https://tiktok.com/@{info.author}/video/{info.id}"


def get_tiktok_audio_callback_data(info: TikTokVideo) -> Optional[str]:
    if info.author and info.id:
        return f"audio:tiktok:{info.author}:{info.id}"
    return None


def get_tiktok_size_hint(data: dict) -> Optional[int]:
    source_data = data.get("data", {}) if isinstance(data, dict) else {}
    for key in ("size_hd", "size", "wm_size"):
        raw = source_data.get(key)
        if isinstance(raw, (int, float)):
            return int(raw)
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
    return None
