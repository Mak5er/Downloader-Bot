import asyncio
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from yt_dlp.extractor.tiktok import TikTokIE

from log.logger import logger as logging
from services.platforms.tiktok_common import (
    SHORT_HOSTS,
    _first_non_empty_str,
    _safe_int,
    get_video_id_from_url,
    is_invalid_tiktok_payload,
    strip_tiktok_tracking,
)

logging = logging.bind(service="tiktok_media")


class TikTokMetadataMixin:
    def _build_ytdlp_options(self) -> dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "cachedir": False,
            "socket_timeout": 15,
            "retries": 2,
            "extractor_retries": 2,
        }

    def _extract_tiktok_detail_sync(self, video_url: str) -> tuple[dict[str, Any], int]:
        video_id = get_video_id_from_url(video_url)
        with self._youtube_dl_factory(self._build_ytdlp_options()) as ydl:
            extractor = TikTokIE(ydl)
            detail, status = extractor._extract_web_data_and_status(video_url, video_id, fatal=False)
        if not isinstance(detail, dict):
            detail = {}
        return detail, status

    async def _extract_tiktok_detail(self, video_url: str) -> tuple[dict[str, Any], int]:
        return await asyncio.to_thread(self._extract_tiktok_detail_sync, video_url)

    def _extract_cover_url(self, detail: dict[str, Any], image_urls: list[str]) -> str:
        image_post = detail.get("imagePost") if isinstance(detail.get("imagePost"), dict) else {}
        image_cover = image_post.get("cover") if isinstance(image_post, dict) else {}
        image_cover_url = self._extract_nested_url(image_cover)
        video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
        return _first_non_empty_str(
            image_cover_url,
            video.get("cover"),
            video.get("originCover"),
            video.get("dynamicCover"),
            image_urls[0] if image_urls else "",
        )

    @staticmethod
    def _extract_nested_url(payload: Any) -> str:
        if isinstance(payload, str):
            return payload.strip()
        if not isinstance(payload, dict):
            return ""
        image_url = payload.get("imageURL")
        if isinstance(image_url, dict):
            url_list = image_url.get("urlList")
            if isinstance(url_list, list):
                for candidate in url_list:
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate.strip()
        url_list = payload.get("UrlList")
        if isinstance(url_list, list):
            for candidate in url_list:
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        return ""

    def _extract_image_urls(self, detail: dict[str, Any]) -> list[str]:
        image_post = detail.get("imagePost")
        if not isinstance(image_post, dict):
            return []
        images = image_post.get("images")
        if not isinstance(images, list):
            return []
        image_urls: list[str] = []
        for image in images:
            if not isinstance(image, dict):
                continue
            url = self._extract_nested_url(image)
            if url:
                image_urls.append(url)
        return image_urls

    def _extract_video_play_url(self, detail: dict[str, Any]) -> str:
        video = detail.get("video")
        if not isinstance(video, dict):
            return ""

        direct_url = _first_non_empty_str(video.get("playAddr"), video.get("downloadAddr"))
        if direct_url:
            return direct_url

        play_addr_struct = video.get("PlayAddrStruct")
        if isinstance(play_addr_struct, dict):
            direct_url = self._extract_nested_url(play_addr_struct)
            if direct_url:
                return direct_url

        bitrate_info = video.get("bitrateInfo")
        if not isinstance(bitrate_info, list):
            return ""

        best_url = ""
        best_score = (-1, -1, -1)
        for entry in bitrate_info:
            if not isinstance(entry, dict):
                continue
            play_addr = entry.get("PlayAddr")
            if not isinstance(play_addr, dict):
                continue
            url = self._extract_nested_url(play_addr)
            if not url:
                continue
            score = (
                _safe_int(play_addr.get("DataSize")),
                _safe_int(play_addr.get("Height")),
                _safe_int(play_addr.get("Width")),
            )
            if score > best_score:
                best_score = score
                best_url = url
        return best_url

    def _extract_video_size(self, detail: dict[str, Any]) -> int:
        video = detail.get("video")
        if not isinstance(video, dict):
            return 0

        base_size = _safe_int(video.get("size"))
        if base_size > 0:
            return base_size

        play_addr_struct = video.get("PlayAddrStruct")
        if isinstance(play_addr_struct, dict):
            struct_size = _safe_int(play_addr_struct.get("DataSize"))
            if struct_size > 0:
                return struct_size

        bitrate_info = video.get("bitrateInfo")
        if not isinstance(bitrate_info, list):
            return 0

        max_size = 0
        for entry in bitrate_info:
            if not isinstance(entry, dict):
                continue
            play_addr = entry.get("PlayAddr")
            if not isinstance(play_addr, dict):
                continue
            max_size = max(max_size, _safe_int(play_addr.get("DataSize")))
        return max_size

    def _build_download_headers(
        self,
        *,
        referer: Optional[str],
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> dict[str, str]:
        headers = {
            "User-Agent": self._get_user_agent(),
            "Referer": referer or "https://www.tiktok.com/",
        }
        if extra_headers:
            for key, value in extra_headers.items():
                if isinstance(key, str) and isinstance(value, str) and value:
                    headers[key] = value
        return headers

    def _build_legacy_payload(self, video_url: str, detail: dict[str, Any]) -> dict[str, Any]:
        stats = detail.get("stats") if isinstance(detail.get("stats"), dict) else {}
        author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
        music = detail.get("music") if isinstance(detail.get("music"), dict) else {}
        image_urls = self._extract_image_urls(detail)
        webpage_url = strip_tiktok_tracking(video_url)
        video_size = self._extract_video_size(detail)
        return {
            "error": None,
            "code": 0,
            "message": "success",
            "data": {
                "id": _first_non_empty_str(detail.get("id"), get_video_id_from_url(video_url)),
                "title": _first_non_empty_str(detail.get("desc"), ""),
                "cover": self._extract_cover_url(detail, image_urls),
                "play_count": _safe_int(stats.get("playCount")),
                "digg_count": _safe_int(stats.get("diggCount")),
                "comment_count": _safe_int(stats.get("commentCount")),
                "share_count": _safe_int(stats.get("shareCount")),
                "music_info": {"play": _first_non_empty_str(music.get("playUrl"))},
                "author": {
                    "unique_id": _first_non_empty_str(author.get("uniqueId"), author.get("id")),
                },
                "images": image_urls,
                "play": self._extract_video_play_url(detail),
                "download_headers": self._build_download_headers(referer=webpage_url),
                "audio_headers": self._build_download_headers(referer=webpage_url),
                "webpage_url": webpage_url,
                "size_hd": video_size,
                "size": video_size,
                "wm_size": video_size,
            },
        }

    async def fetch_tiktok_data(self, video_url: str) -> dict:
        async with self._request_lock:
            now = self._monotonic()
            wait_for = max(0.0, 1.0 - (now - self._last_call_time))
            self._last_call_time = now + wait_for

        if wait_for:
            await asyncio.sleep(wait_for)

        logging.debug("Fetching TikTok data via yt-dlp: url=%s", video_url)
        detail, status = await self._extract_tiktok_detail(video_url)

        if status not in (0, None) or not detail:
            logging.warning(
                "TikTok yt-dlp returned invalid status: url=%s status=%s detail_keys=%s",
                video_url,
                status,
                list(detail.keys()) if isinstance(detail, dict) else None,
            )
            return {
                "error": f"status:{status}",
                "code": status or 1,
                "message": "TikTok post is unavailable",
                "data": {},
            }

        data = self._build_legacy_payload(video_url, detail)
        logging.debug(
            "Fetched TikTok data via yt-dlp: url=%s has_images=%s keys=%s",
            video_url,
            bool(data.get("data", {}).get("images")),
            list(data.get("data", {}).keys()),
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
        target_url = base_url
        if urlparse(base_url).netloc.lower() in SHORT_HOSTS:
            try:
                resolved_url = await asyncio.wait_for(self.process_tiktok_url_async(base_url), timeout=6.0)
            except Exception as exc:
                logging.warning("TikTok short URL expansion failed before fetch: url=%s error=%s", base_url, exc)
            else:
                if resolved_url:
                    target_url = resolved_url

        return await _fetch_with_retry(target_url)

    @staticmethod
    def _extract_source_data(download_data: Optional[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(download_data, dict):
            return {}
        inner_data = download_data.get("data")
        if isinstance(inner_data, dict):
            return inner_data
        return download_data

    async def _resolve_source_data(self, source_url: str, download_data: Optional[dict[str, Any]]) -> dict[str, Any]:
        source_data = self._extract_source_data(download_data)
        if source_data:
            return source_data
        payload = await self.fetch_tiktok_data_with_retry(source_url)
        return self._extract_source_data(payload)
