from typing import Any

from aiogram import types
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder


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
                    media_group.add_video(media=media_ref)
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
