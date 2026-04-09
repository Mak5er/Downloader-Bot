def _favicon_url(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?sz=256&domain_url={domain}"


INLINE_SERVICE_ICONS: dict[str, str] = {
    "instagram": _favicon_url("https://www.instagram.com"),
    "pinterest": _favicon_url("https://www.pinterest.com"),
    "soundcloud": _favicon_url("https://soundcloud.com"),
    "tiktok": _favicon_url("https://www.tiktok.com"),
    "twitter": _favicon_url("https://twitter.com"),
    "youtube": _favicon_url("https://www.youtube.com"),
}


def get_inline_service_icon(service: str) -> str | None:
    return INLINE_SERVICE_ICONS.get((service or "").strip().lower())
