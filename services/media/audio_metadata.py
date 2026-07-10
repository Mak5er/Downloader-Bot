from __future__ import annotations

import asyncio
import io
import mimetypes
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image, ImageOps, UnidentifiedImageError
from mutagen.id3 import (
    APIC,
    COMM,
    TALB,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TRCK,
    TXXX,
    ID3,
)

from services.logger import logger as logging
from services.media.artist_names import normalize_artist_names

logging = logging.bind(service="audio_metadata")

_MAX_FILENAME_BYTES = 180
_MAX_COVER_BYTES = 10 * 1024 * 1024
_MAX_EMBEDDED_COVER_DIMENSION = 1600
_MAX_TELEGRAM_THUMBNAIL_DIMENSION = 320
_MAX_TELEGRAM_THUMBNAIL_BYTES = 190 * 1024


@dataclass(slots=True)
class PreparedAudioMetadata:
    tagged: bool
    thumbnail_path: Path | None = None

    def cleanup(self) -> None:
        if self.thumbnail_path:
            self.thumbnail_path.unlink(missing_ok=True)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def build_audio_filename(title: str | None) -> str:
    """Return a readable, portable MP3 filename suitable for Telegram."""
    clean_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " - ", title or "audio")
    clean_title = re.sub(r"\s+", " ", clean_title).strip(" .-") or "audio"
    return f"{_truncate_utf8(clean_title, _MAX_FILENAME_BYTES - 4) or 'audio'}.mp3"


def normalize_audio_artist(metadata: dict[str, Any]) -> str | None:
    artist = normalize_artist_names(metadata.get("artists"))
    if artist:
        return artist
    for key in ("artist", "creator", "uploader"):
        artist = normalize_artist_names(metadata.get(key))
        if artist:
            return artist
    return None


async def _download_cover(url: str | None) -> tuple[bytes | None, str | None]:
    if not url or not url.lower().startswith(("https://", "http://")):
        return None, None

    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                declared_size = response.content_length
                if declared_size and declared_size > _MAX_COVER_BYTES:
                    return None, None
                content = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    content.extend(chunk)
                    if len(content) > _MAX_COVER_BYTES:
                        return None, None
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if content_type and not content_type.startswith("image/"):
                    return None, None
                return bytes(content), content_type or mimetypes.guess_type(url)[0]
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logging.debug("Could not download audio cover: url=%s error=%s", url, exc)
        return None, None


def _add_text_tag(tags: ID3, frame: Any, value: Any) -> None:
    if value is not None and str(value).strip():
        tags.add(frame(encoding=3, text=[str(value)]))


def _load_compatible_rgb_image(cover: bytes) -> Image.Image:
    with Image.open(io.BytesIO(cover)) as source:
        source.load()
        image = ImageOps.exif_transpose(source)
        if image.mode in {"RGBA", "LA"}:
            background = Image.new("RGB", image.size, "white")
            alpha = image.getchannel("A")
            background.paste(image.convert("RGB"), mask=alpha)
            return background
        return image.convert("RGB")


def _encode_jpeg(image: Image.Image, *, quality: int, subsampling: int = 0) -> bytes:
    output = io.BytesIO()
    image.save(
        output,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=False,
        subsampling=subsampling,
    )
    return output.getvalue()


def _normalize_cover_images(cover: bytes | None) -> tuple[bytes | None, bytes | None]:
    if not cover:
        return None, None
    try:
        image = _load_compatible_rgb_image(cover)
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        logging.warning("Downloaded audio cover is invalid and will be skipped: error=%s", exc)
        return None, None

    embedded = image.copy()
    embedded.thumbnail(
        (_MAX_EMBEDDED_COVER_DIMENSION, _MAX_EMBEDDED_COVER_DIMENSION),
        Image.Resampling.LANCZOS,
    )
    telegram = image.copy()
    telegram.thumbnail(
        (_MAX_TELEGRAM_THUMBNAIL_DIMENSION, _MAX_TELEGRAM_THUMBNAIL_DIMENSION),
        Image.Resampling.LANCZOS,
    )
    embedded_bytes = _encode_jpeg(embedded, quality=94)
    telegram_bytes = b""
    for quality in (88, 80, 72, 64, 56):
        telegram_bytes = _encode_jpeg(telegram, quality=quality, subsampling=2)
        if len(telegram_bytes) <= _MAX_TELEGRAM_THUMBNAIL_BYTES:
            break
    return embedded_bytes, telegram_bytes


def _write_telegram_thumbnail(audio_path: str, thumbnail: bytes) -> Path:
    source = Path(audio_path)
    path = source.with_name(f".{source.stem}.telegram-cover-{uuid.uuid4().hex}.jpg")
    path.write_bytes(thumbnail)
    return path


def _write_id3_tags(path: str, metadata: dict[str, Any], cover: bytes | None, cover_mime: str | None) -> None:
    title = str(metadata.get("title") or "audio")
    artist = normalize_audio_artist(metadata)
    tags = ID3()
    _add_text_tag(tags, TIT2, title)
    _add_text_tag(tags, TPE1, artist)
    _add_text_tag(tags, TALB, metadata.get("album"))
    _add_text_tag(
        tags,
        TPE2,
        normalize_artist_names(metadata.get("album_artist")) or artist,
    )
    _add_text_tag(tags, TDRC, metadata.get("date"))
    _add_text_tag(tags, TRCK, metadata.get("track_number"))
    if metadata.get("disc_number") is not None:
        tags.add(
            TXXX(
                encoding=3,
                desc="DISCNUMBER",
                text=[str(metadata["disc_number"])],
            )
        )
    source_url = metadata.get("source_url") or metadata.get("url")
    if source_url:
        tags.add(COMM(encoding=3, lang="eng", desc="Source", text=[str(source_url)]))
        tags.add(TXXX(encoding=3, desc="SOURCE", text=[str(source_url)]))
    if cover:
        tags.add(
            APIC(
                encoding=3,
                mime=cover_mime or "image/jpeg",
                type=3,
                desc="Cover",
                data=cover,
            )
        )
    tags.save(path, v2_version=3)


async def prepare_mp3_metadata(path: str, metadata: dict[str, Any]) -> PreparedAudioMetadata:
    """Embed a compatible high-quality JPEG and prepare a Telegram thumbnail."""
    if Path(path).suffix.lower() != ".mp3" or not Path(path).exists():
        return PreparedAudioMetadata(tagged=False)
    raw_cover, _raw_cover_mime = await _download_cover(metadata.get("thumbnail"))
    cover, telegram_thumbnail = await asyncio.to_thread(_normalize_cover_images, raw_cover)
    thumbnail_path = None
    try:
        await asyncio.to_thread(_write_id3_tags, path, metadata, cover, "image/jpeg")
        if telegram_thumbnail:
            thumbnail_path = await asyncio.to_thread(
                _write_telegram_thumbnail,
                path,
                telegram_thumbnail,
            )
    except Exception as exc:
        logging.warning("Could not embed MP3 metadata: path=%s error=%s", path, exc)
        if thumbnail_path:
            thumbnail_path.unlink(missing_ok=True)
        return PreparedAudioMetadata(tagged=False)
    return PreparedAudioMetadata(tagged=True, thumbnail_path=thumbnail_path)


async def embed_mp3_metadata(path: str, metadata: dict[str, Any]) -> bool:
    """Backward-compatible metadata-only helper."""
    prepared = await prepare_mp3_metadata(path, metadata)
    prepared.cleanup()
    return prepared.tagged
