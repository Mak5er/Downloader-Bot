import asyncio

from services.logger import logger as logging
from services.platforms.tiktok_common import TikTokUser

logging = logging.bind(service="tiktok_media")


class TikTokProfileMixin:
    async def fetch_user_info(self, username: str) -> TikTokUser | None:
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
