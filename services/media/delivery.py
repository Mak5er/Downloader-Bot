import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Optional

from aiogram import types
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder

from services.logger import logger as logging
from services.media.video_metadata import build_video_send_kwargs

logging = logging.bind(service="media_delivery")

AUDIO_CACHE_VARIANT = "audio_bot_meta_v2"


def build_audio_cache_key(source_url: str) -> str:
    return f"{source_url}#{AUDIO_CACHE_VARIANT}"


def coerce_audio_duration_seconds(value: object) -> Optional[int]:
    try:
        duration = round(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return duration if duration > 0 else None


async def probe_audio_duration_seconds(path: str | None) -> Optional[int]:
    if not path:
        return None
    if not Path(path).exists():
        return None

    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logging.debug("ffprobe is not available; skipping audio duration probe")
        return None
    except Exception as exc:
        logging.debug("Failed to start ffprobe for audio %s: %s", path, exc)
        return None

    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logging.debug(
            "ffprobe returned non-zero exit code for audio %s: %s",
            path,
            stderr.decode("utf-8", errors="ignore").strip(),
        )
        return None

    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logging.debug("Failed to parse ffprobe audio payload for %s: %s", path, exc)
        return None

    return coerce_audio_duration_seconds((payload.get("format") or {}).get("duration"))


def _input_file_path(value: Any) -> str | None:
    path = getattr(value, "path", None)
    if path:
        return str(path)
    return None


def _cover_output_path(audio_path: str) -> str:
    source = Path(audio_path)
    suffix = source.suffix.lower()
    if suffix not in {".mp3", ".m4a", ".mp4", ".aac"}:
        suffix = ".mp3"
    return str(source.with_name(f"{source.stem}.cover-{uuid.uuid4().hex}{suffix}"))


async def embed_audio_cover(audio_path: str | None, cover_path: str | None) -> str | None:
    if not audio_path or not cover_path:
        return None
    if not Path(audio_path).exists() or not Path(cover_path).exists():
        return None

    output_path = _cover_output_path(audio_path)
    source_ext = Path(audio_path).suffix.lower()
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        audio_path,
        "-i",
        cover_path,
        "-map",
        "0:a:0",
        "-map",
        "1:v:0",
    ]
    if source_ext == ".mp3":
        command.extend([
            "-c:a",
            "copy",
            "-c:v",
            "mjpeg",
            "-id3v2_version",
            "3",
            "-metadata:s:v",
            "title=Album cover",
            "-metadata:s:v",
            "comment=Cover (front)",
        ])
    elif source_ext in {".m4a", ".mp4", ".aac"}:
        command.extend([
            "-c:a",
            "copy",
            "-c:v",
            "mjpeg",
            "-disposition:v:0",
            "attached_pic",
        ])
    else:
        command.extend([
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-c:v",
            "mjpeg",
            "-id3v2_version",
            "3",
            "-metadata:s:v",
            "title=Album cover",
            "-metadata:s:v",
            "comment=Cover (front)",
        ])
    command.append(output_path)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logging.debug("ffmpeg is not available; skipping embedded audio cover")
        return None
    except Exception as exc:
        logging.debug("Failed to start ffmpeg audio cover embed: path=%s error=%s", audio_path, exc)
        return None

    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logging.debug(
            "ffmpeg failed to embed audio cover: path=%s error=%s",
            audio_path,
            stderr.decode("utf-8", errors="ignore").strip(),
        )
        Path(output_path).unlink(missing_ok=True)
        return None

    return output_path if Path(output_path).exists() else None


def build_bot_audio_performer(bot_url: str | None) -> str | None:
    if not bot_url:
        return None

    value = bot_url.strip()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value.removeprefix(prefix)
            break
    if value.startswith("t.me/"):
        value = value.removeprefix("t.me/")
    value = value.split("/", 1)[0].split("?", 1)[0].strip()
    if not value:
        return None
    return value if value.startswith("@") else f"@{value}"


async def send_audio_with_thumbnail(
    send_audio,
    *,
    audio_path: str | None = None,
    bot_avatar: Any = None,
    bot_url: str | None = None,
    duration: object = None,
    performer: str | None = None,
    **kwargs: Any,
) -> types.Message:
    send_kwargs = dict(kwargs)
    embedded_audio_path = None
    audio_performer = performer or build_bot_audio_performer(bot_url)
    if audio_performer:
        send_kwargs["performer"] = audio_performer
    if "duration" not in send_kwargs:
        audio_duration = coerce_audio_duration_seconds(duration)
        if audio_duration is None:
            audio_duration = await probe_audio_duration_seconds(audio_path)
        if audio_duration is not None:
            send_kwargs["duration"] = audio_duration
    if bot_avatar:
        send_kwargs["thumbnail"] = bot_avatar

    cover_path = _input_file_path(bot_avatar)
    if audio_path and cover_path:
        embedded_audio_path = await embed_audio_cover(audio_path, cover_path)
        if embedded_audio_path:
            send_kwargs["audio"] = FSInputFile(embedded_audio_path)

    try:
        return await send_audio(**send_kwargs)
    except Exception as exc:
        if not bot_avatar:
            raise
        logging.warning("Audio thumbnail upload failed, retrying without thumbnail: error=%s", exc)
        send_kwargs.pop("thumbnail", None)
        return await send_audio(**send_kwargs)
    finally:
        if embedded_audio_path:
            Path(embedded_audio_path).unlink(missing_ok=True)


def extract_sent_file_id(sent_message: types.Message, media_kind: str) -> str | None:
    if media_kind == "video" and sent_message.video:
        return sent_message.video.file_id
    if media_kind == "photo" and sent_message.photo:
        return sent_message.photo[-1].file_id
    return None


def resolve_media_input(
    entry: dict[str, Any],
    *,
    file_id_key: str = "file_id",
    path_key: str = "path",
    url_key: str = "url",
) -> str | FSInputFile:
    file_id = entry.get(file_id_key)
    if file_id:
        return str(file_id)

    path = entry.get(path_key)
    if path:
        return FSInputFile(str(path))

    url = entry.get(url_key)
    if url:
        return str(url)

    raise ValueError("Media entry is missing file_id, path, and url")


async def _cache_sent_entry(
    db_service: Any,
    entry: dict[str, Any],
    sent_message: types.Message,
    *,
    kind_key: str,
    cache_key_key: str,
    cached_key: str,
) -> None:
    if entry.get(cached_key):
        return

    media_kind = str(entry[kind_key])
    file_id = extract_sent_file_id(sent_message, media_kind)
    if file_id:
        await db_service.add_file(str(entry[cache_key_key]), file_id, media_kind)


async def send_cached_media_entries(
    message: types.Message,
    entries: list[dict[str, Any]],
    *,
    db_service: Any,
    caption: str | None = None,
    reply_markup: Any = None,
    parse_mode: str | None = None,
    kind_key: str = "kind",
    cache_key_key: str = "cache_key",
    cached_key: str = "cached",
    file_id_key: str = "file_id",
    path_key: str = "path",
    url_key: str = "url",
) -> types.Message | None:
    if not entries:
        return None

    has_sent_media = False
    if len(entries) > 1:
        album_items = entries[:-1]
        for offset in range(0, len(album_items), 10):
            batch = album_items[offset:offset + 10]
            media_group = MediaGroupBuilder()
            for entry in batch:
                media_kind = str(entry[kind_key])
                media_ref = resolve_media_input(
                    entry,
                    file_id_key=file_id_key,
                    path_key=path_key,
                    url_key=url_key,
                )
                if media_kind == "video":
                    media_group.add_video(
                        media=media_ref,
                        **(await build_video_send_kwargs(str(entry.get(path_key)) if entry.get(path_key) else None)),
                    )
                else:
                    media_group.add_photo(media=media_ref)

            send_kwargs = {"media": media_group.build()}
            if not has_sent_media:
                send_kwargs["reply_to_message_id"] = message.message_id
            sent_group = await message.answer_media_group(**send_kwargs)
            has_sent_media = True

            for sent_message, entry in zip(sent_group, batch):
                await _cache_sent_entry(
                    db_service,
                    entry,
                    sent_message,
                    kind_key=kind_key,
                    cache_key_key=cache_key_key,
                    cached_key=cached_key,
                )

    last_entry = entries[-1]
    media_kind = str(last_entry[kind_key])
    media_ref = resolve_media_input(
        last_entry,
        file_id_key=file_id_key,
        path_key=path_key,
        url_key=url_key,
    )
    send_kwargs: dict[str, Any] = {
        "caption": caption,
        "reply_markup": reply_markup,
    }
    if parse_mode is not None:
        send_kwargs["parse_mode"] = parse_mode

    if media_kind == "video":
        send_kwargs.update(await build_video_send_kwargs(str(last_entry.get(path_key)) if last_entry.get(path_key) else None))
        send_video = message.answer_video if has_sent_media else message.reply_video
        sent_message = await send_video(video=media_ref, **send_kwargs)
    else:
        send_photo = message.answer_photo if has_sent_media else message.reply_photo
        sent_message = await send_photo(photo=media_ref, **send_kwargs)

    await _cache_sent_entry(
        db_service,
        last_entry,
        sent_message,
        kind_key=kind_key,
        cache_key_key=cache_key_key,
        cached_key=cached_key,
    )
    return sent_message
