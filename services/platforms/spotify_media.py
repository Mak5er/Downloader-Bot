from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp

from config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_MARKET
from services.logger import logger as logging
from services.media.artist_names import normalize_artist_names

logging = logging.bind(service="spotify_media")


class SpotifyError(RuntimeError):
    pass


_token: str | None = None
_token_expires_at = 0.0


def get_high_resolution_spotify_image_url(url: str | None) -> str | None:
    if not url:
        return None
    # Spotify oEmbed returns the 300px album-art rendition. The same immutable
    # image key exposes the native 640px rendition used by the Web API.
    return url.replace("ab67616d00001e02", "ab67616d0000b273")


def parse_spotify_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() == "spotify":
        parts = [part for part in re.split(r"[:/]", parsed.path.strip("/")) if part]
    else:
        if parsed.netloc.lower() not in {
            "open.spotify.com",
            "spotify.com",
            "www.spotify.com",
        }:
            return None
        parts = [part for part in parsed.path.strip("/").split("/") if part]

    for kind in ("track", "album", "playlist"):
        if kind in parts:
            index = parts.index(kind)
            if index + 1 < len(parts) and re.fullmatch(r"[A-Za-z0-9]+", parts[index + 1]):
                return kind, parts[index + 1]
    return None


def strip_spotify_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return urlunparse(("https", parsed.netloc.lower(), parsed.path, "", "", ""))


async def _read_json_response(response: aiohttp.ClientResponse, action: str) -> dict[str, Any]:
    text = await response.text()
    if not text.strip():
        raise SpotifyError(f"Spotify returned an empty response while {action}.")
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpotifyError(f"Spotify returned an invalid response while {action}.") from exc
    if not isinstance(body, dict):
        raise SpotifyError(f"Spotify returned an unexpected response while {action}.")
    return body


async def _get_token(session: aiohttp.ClientSession) -> str:
    global _token, _token_expires_at
    if _token and time.time() < _token_expires_at:
        return _token
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise SpotifyError("Spotify API credentials are not configured.")

    credentials = base64.b64encode(
        f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
    ).decode()
    try:
        async with session.post(
            "https://accounts.spotify.com/api/token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
        ) as response:
            body = await _read_json_response(response, "authorizing")
            if response.status >= 400 or not body.get("access_token"):
                raise SpotifyError("Spotify authorization failed.")
            _token = str(body["access_token"])
            _token_expires_at = time.time() + max(
                60, int(body.get("expires_in", 3600)) - 60
            )
            return _token
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise SpotifyError("Spotify API is unavailable right now.") from exc


def parse_spotify_description(description: str, fallback_title: str) -> dict[str, str]:
    parts = [html.unescape(part.strip()) for part in description.split("·") if part.strip()]
    metadata: dict[str, str] = {}
    if len(parts) >= 2:
        metadata["artists"] = normalize_artist_names(parts[0]) or parts[0]
        metadata["title"] = parts[1]
    elif fallback_title:
        metadata["title"] = html.unescape(fallback_title)
    date = next(
        (part for part in parts if re.fullmatch(r"\d{4}(?:-\d{2}-\d{2})?", part)),
        None,
    )
    if date:
        metadata["date"] = date
    return metadata


async def _get_page_metadata(session: aiohttp.ClientSession, url: str) -> dict[str, str]:
    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as response:
            if response.status >= 400:
                return {}
            page = await response.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return {}

    patterns = (
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:description["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, page, re.IGNORECASE)
        if match:
            return parse_spotify_description(html.unescape(match.group(1)), "")
    return {}


async def _get_oembed_track(
    session: aiohttp.ClientSession,
    url: str,
    spotify_id: str,
) -> dict[str, Any]:
    try:
        async with session.get(
            "https://open.spotify.com/oembed", params={"url": url}
        ) as response:
            body = await _read_json_response(response, "loading track metadata")
            if response.status >= 400 or not body.get("title"):
                raise SpotifyError("Spotify could not find this track.")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise SpotifyError("Spotify is unavailable right now.") from exc

    page_metadata = await _get_page_metadata(session, url)
    author_name = str(body.get("author_name") or "").strip()
    artists = normalize_artist_names(page_metadata.get("artists"))
    if not artists and author_name.lower() != "spotify":
        artists = normalize_artist_names(author_name)
    return {
        "id": f"spotify:{spotify_id}",
        "spotify_id": spotify_id,
        # oEmbed is track-scoped; the page description can name the album.
        "title": body["title"],
        "artists": artists or "Unknown artist",
        "album": None,
        "album_artist": None,
        "date": page_metadata.get("date"),
        "duration": None,
        "track_number": None,
        "disc_number": None,
        "thumbnail": get_high_resolution_spotify_image_url(body.get("thumbnail_url")),
        "url": url,
        "source_url": url,
    }


async def get_spotify_track(url: str) -> dict[str, Any]:
    parsed = parse_spotify_url(url)
    if not parsed:
        raise SpotifyError("Invalid Spotify URL.")
    kind, spotify_id = parsed
    if kind != "track":
        raise SpotifyError("Send a Spotify track link, not an album or playlist link.")

    canonical_url = strip_spotify_url(url)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            return await _get_oembed_track(session, canonical_url, spotify_id)

        try:
            token = await _get_token(session)
        except SpotifyError as exc:
            logging.warning(
                "Spotify authorization failed; using oEmbed fallback: error=%s",
                exc,
            )
            return await _get_oembed_track(session, canonical_url, spotify_id)
        try:
            async with session.get(
                f"https://api.spotify.com/v1/tracks/{spotify_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"market": SPOTIFY_MARKET or "UA"},
            ) as response:
                if response.status in {401, 403, 429}:
                    logging.info(
                        "Spotify catalog metadata unavailable; continuing with oEmbed fallback: status=%s",
                        response.status,
                    )
                    return await _get_oembed_track(session, canonical_url, spotify_id)
                body = await _read_json_response(response, "loading track metadata")
                if response.status >= 400:
                    raise SpotifyError("Spotify could not find this track in the configured market.")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise SpotifyError("Spotify API is unavailable right now.") from exc

    artists = normalize_artist_names(body.get("artists"))
    album = body.get("album") or {}
    album_artists = normalize_artist_names(album.get("artists"))
    images = album.get("images") or []
    return {
        "id": f"spotify:{spotify_id}",
        "spotify_id": spotify_id,
        "title": body.get("name") or "Unknown title",
        "artists": artists or "Unknown artist",
        "album": album.get("name"),
        "album_artist": album_artists or artists,
        "date": album.get("release_date"),
        "duration": round((body.get("duration_ms") or 0) / 1000) or None,
        "track_number": body.get("track_number"),
        "disc_number": body.get("disc_number"),
        "thumbnail": images[0].get("url") if images else None,
        "url": canonical_url,
        "source_url": canonical_url,
    }
