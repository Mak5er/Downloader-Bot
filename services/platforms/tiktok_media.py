import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse, urlunparse

import aiohttp
from fake_useragent import UserAgent

from log.logger import logger as logging
from utils.download_manager import (
    DownloadConfig,
    DownloadError,
    DownloadMetrics,
    DownloadQueueBusyError,
    DownloadRateLimitError,
    ResilientDownloader,
)

logging = logging.bind(service="tiktok_media")

TIKTOK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
TIKTOK_API_TIMEOUT = aiohttp.ClientTimeout(total=10)
SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com", "vn.tiktok.com"}
URL_EXPAND_TIMEOUT = 4
URL_EXPAND_CACHE_MAXSIZE = 2048


def strip_tiktok_tracking(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


def get_video_id_from_url(url: str) -> str:
    return url.split("/")[-1].split("?")[0]


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


class TikTokMediaService:
    DOWNLOAD_URL_TEMPLATE = "https://tikwm.com/video/media/play/{video_id}.mp4"

    def __init__(
        self,
        output_dir: str,
        *,
        get_http_session_func: Callable[[], Awaitable[object]],
        retry_async_operation_func: Callable[..., Awaitable[dict | DownloadMetrics | None]],
        user_agent_factory: Callable[[], object],
    ) -> None:
        config = DownloadConfig(
            chunk_size=1024 * 1024,
            multipart_threshold=16 * 1024 * 1024,
            max_workers=8,
            retry_backoff=0.8,
        )
        self._downloader = ResilientDownloader(output_dir, config=config, source="tiktok")
        self._get_http_session = get_http_session_func
        self._retry_async_operation = retry_async_operation_func
        self._user_agent_factory = user_agent_factory
        self._user_agent_provider: Optional[object] = None
        self._expanded_tiktok_url_cache: "OrderedDict[str, str]" = OrderedDict()
        self._expanded_tiktok_url_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_call_time = 0.0

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

    async def _expand_tiktok_url_cached_async(self, url: str) -> str:
        cached = self._expanded_tiktok_url_cache.get(url)
        if cached is not None:
            self._expanded_tiktok_url_cache.move_to_end(url)
            return cached

        session = await self._get_http_session()
        headers = {"User-Agent": self._get_user_agent()}
        async with self._expanded_tiktok_url_lock:
            cached = self._expanded_tiktok_url_cache.get(url)
            if cached is not None:
                self._expanded_tiktok_url_cache.move_to_end(url)
                return cached

            async with session.head(
                url,
                allow_redirects=True,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=URL_EXPAND_TIMEOUT),
            ) as response:
                expanded = str(response.url) or url

            self._expanded_tiktok_url_cache[url] = expanded
            self._expanded_tiktok_url_cache.move_to_end(url)
            if len(self._expanded_tiktok_url_cache) > URL_EXPAND_CACHE_MAXSIZE:
                self._expanded_tiktok_url_cache.popitem(last=False)
            return expanded

    async def process_tiktok_url_async(self, text: str) -> str:
        def extract_tiktok_url(input_text: str) -> str:
            import re

            match = re.search(r"(https?://(?:www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", input_text)
            return match.group(0) if match else input_text

        url = strip_tiktok_tracking(extract_tiktok_url(text))
        logging.debug("TikTok URL extracted: raw=%s extracted=%s", text, url)

        try:
            parsed = urlparse(url)
            host = (parsed.netloc or "").lower()
            if host in SHORT_HOSTS:
                expanded = await self._expand_tiktok_url_cached_async(url)
                logging.debug("TikTok short URL expanded: raw=%s expanded=%s", url, expanded)
                return strip_tiktok_tracking(expanded)
            return strip_tiktok_tracking(url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logging.error("Error expanding TikTok URL: url=%s error=%s", url, exc)
            return strip_tiktok_tracking(url)

    async def fetch_tiktok_data(self, video_url: str) -> dict:
        async with self._request_lock:
            now = time.monotonic()
            wait_for = max(0.0, 1.0 - (now - self._last_call_time))
            self._last_call_time = now + wait_for

        if wait_for:
            await asyncio.sleep(wait_for)

        params = {"url": video_url, "count": 12, "cursor": 0, "web": 1, "hd": 1}
        logging.debug("Fetching TikTok data: url=%s params=%s", video_url, params)
        session = await self._get_http_session()
        try:
            async with session.get(
                "https://tikwm.com/api/",
                params=params,
                timeout=TIKTOK_API_TIMEOUT,
                headers={"User-Agent": self._get_user_agent()},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, aiohttp.ContentTypeError, asyncio.TimeoutError) as exc:
            logging.error("TikTok API request failed: url=%s error=%s", video_url, exc)
            raise

        logging.debug(
            "Fetched TikTok data: url=%s has_error=%s keys=%s",
            video_url,
            data.get("error"),
            list(data.keys()),
        )
        return data

    async def fetch_tiktok_data_with_retry(self, video_url: str, *, on_retry=None) -> dict:
        async def _fetch_with_retry(target_url: str) -> dict:
            return await self._retry_async_operation(
                lambda: self.fetch_tiktok_data(target_url),
                attempts=3,
                delay_seconds=2.0,
                should_retry_result=is_invalid_tiktok_payload,
                on_retry=on_retry,
            )

        base_url = strip_tiktok_tracking(video_url)
        data = await _fetch_with_retry(base_url)
        if is_invalid_tiktok_payload(data) and urlparse(base_url).netloc.lower() in SHORT_HOSTS:
            resolved_url = await asyncio.wait_for(self.process_tiktok_url_async(base_url), timeout=6.0)
            if resolved_url != base_url:
                return await _fetch_with_retry(resolved_url)
        return data

    async def download_video(
        self,
        video_id: str,
        filename: str,
        *,
        user_id: Optional[int] = None,
        request_id: Optional[str] = None,
        size_hint: Optional[int] = None,
        on_queued=None,
        on_progress=None,
        on_retry=None,
    ) -> Optional[DownloadMetrics]:
        headers = {
            "User-Agent": self._get_user_agent(),
            "Referer": "https://www.tiktok.com/",
        }
        url = self.DOWNLOAD_URL_TEMPLATE.format(video_id=video_id)

        async def _download_once():
            return await self._downloader.download(
                url,
                filename,
                headers=headers,
                user_id=user_id,
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
            logging.error("Error downloading TikTok video: video_id=%s error=%s", video_id, exc)
            return None

    async def download_audio(
        self,
        audio_url: str,
        filename: str,
        *,
        user_id: Optional[int] = None,
        request_id: Optional[str] = None,
        size_hint: Optional[int] = None,
        on_queued=None,
        on_progress=None,
        on_retry=None,
    ) -> Optional[DownloadMetrics]:
        headers = {
            "User-Agent": self._get_user_agent(),
            "Referer": "https://www.tiktok.com/",
        }

        async def _download_once():
            return await self._downloader.download(
                audio_url,
                filename,
                headers=headers,
                user_id=user_id,
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
            logging.error("Error downloading TikTok audio: url=%s error=%s", audio_url, exc)
            return None

    async def fetch_user_info(self, username: str) -> Optional[TikTokUser]:
        max_retries = 10
        retry_delay = 1.5
        exist_data: dict | None = None
        session = await self._get_http_session()
        headers = {"User-Agent": self._get_user_agent()}
        exist_url = f"https://countik.com/api/exist/{username}"

        try:
            sec_user_id = None
            for attempt in range(max_retries):
                try:
                    async with session.get(exist_url, headers=headers, timeout=10) as exist_response:
                        exist_response.raise_for_status()
                        exist_data = await exist_response.json(content_type=None)
                    sec_user_id = exist_data.get("sec_uid") if isinstance(exist_data, dict) else None
                    if sec_user_id:
                        break
                except Exception as exc:
                    logging.warning(
                        "TikTok user lookup retry failed: attempt=%s username=%s error=%s",
                        attempt + 1,
                        username,
                        exc,
                    )
                    await asyncio.sleep(retry_delay)
            else:
                logging.error("Failed to get TikTok user data after %s attempts: username=%s", max_retries, username)
                return None

            if not sec_user_id:
                logging.error("TikTok user lookup missing sec_user_id: username=%s", username)
                return None

            api_url = f"https://countik.com/api/userinfo?sec_user_id={sec_user_id}"
            async with session.get(api_url, headers=headers, timeout=10, allow_redirects=True) as api_response:
                api_response.raise_for_status()
                data = await api_response.json(content_type=None)

            exist_data = exist_data or {}
            return TikTokUser(
                nickname=exist_data.get("nickname", "No nickname found"),
                followers=data.get("followerCount", 0),
                videos=data.get("videoCount", 0),
                likes=data.get("heartCount", 0),
                profile_pic=data.get("avatarThumb", ""),
                description=data.get("signature", ""),
            )
        except Exception as exc:
            logging.error("Error fetching TikTok user info: username=%s error=%s", username, exc)
            return None
