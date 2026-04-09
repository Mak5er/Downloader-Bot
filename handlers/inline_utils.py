from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from aiogram import types
from aiogram.client.default import Default
from aiogram.methods.base import TelegramMethod
from aiogram.exceptions import TelegramAPIError

import keyboards as kb
import messages as bm
from log.logger import logger as logging, summarize_text_for_log

_INLINE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif"}


class RawAnswerInlineQuery(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "answerInlineQuery"

    inline_query_id: str
    results: list[dict[str, Any]]
    cache_time: int | None = None
    is_personal: bool | None = None
    next_offset: str | None = None
    button: types.InlineQueryResultsButton | None = None
    switch_pm_parameter: str | None = None
    switch_pm_text: str | None = None


def _default_inline_button_text(media_kind: str) -> str:
    if media_kind == "photo":
        return "Send photo inline"
    if media_kind == "audio":
        return "Send audio inline"
    return "Send video inline"


def build_inline_status_editor(
    *,
    bot: Any,
    inline_message_id: str,
    callback_data_factory: Callable[[str], str],
    safe_edit_inline_text_fn: Callable[..., Awaitable[Any]],
    button_text: Optional[str] = None,
) -> Callable[..., Awaitable[None]]:
    async def _edit_inline_status(
        text: str,
        *,
        with_retry_button: bool = False,
        media_kind: str = "video",
    ) -> None:
        reply_markup = None
        if with_retry_button:
            reply_markup = kb.inline_send_media_keyboard(
                button_text or _default_inline_button_text(media_kind),
                callback_data_factory(media_kind),
            )
        await safe_edit_inline_text_fn(bot, inline_message_id, text, reply_markup=reply_markup)

    return _edit_inline_status


def _normalize_inline_http_url(url: Optional[str]) -> Optional[str]:
    if not isinstance(url, str):
        return None
    candidate = url.strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate


def _looks_like_supported_inline_image_url(url: Optional[str]) -> bool:
    normalized = _normalize_inline_http_url(url)
    if not normalized:
        return False

    parsed = urlparse(normalized)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if host in {"www.google.com", "google.com"} and path.startswith("/s2/favicons"):
        return True

    if any(path.endswith(ext) for ext in _INLINE_IMAGE_EXTENSIONS):
        return True

    query = (parsed.query or "").lower()
    return any(
        marker in query
        for marker in (
            "format=jpg",
            "format=jpeg",
            "format=png",
            "format=gif",
            "fm=jpg",
            "fm=jpeg",
            "fm=png",
            "fm=gif",
            "image/jpeg",
            "image/png",
            "image/gif",
        )
    )


def _clone_inline_article(
    result: types.InlineQueryResultArticle,
    *,
    thumbnail_url: Optional[str],
) -> types.InlineQueryResultArticle:
    payload = result.model_dump(exclude_none=True)
    payload.pop("type", None)
    if thumbnail_url:
        payload["thumbnail_url"] = thumbnail_url
    else:
        payload.pop("thumbnail_url", None)
    return types.InlineQueryResultArticle(**payload)


def _strip_aiogram_defaults(value: Any) -> Any:
    if isinstance(value, Default):
        return None
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            prepared = _strip_aiogram_defaults(item)
            if prepared is not None:
                cleaned[key] = prepared
        return cleaned
    if isinstance(value, list):
        return [
            prepared
            for item in value
            if (prepared := _strip_aiogram_defaults(item)) is not None
        ]
    return value


def _serialize_inline_result(result: Any) -> Any:
    if not isinstance(result, types.TelegramObject):
        return result
    payload = result.model_dump(exclude_none=True, warnings=False)
    return _strip_aiogram_defaults(payload)


async def _answer_inline_query(query: types.InlineQuery, results: list[Any], **answer_kwargs: Any) -> None:
    query_id = getattr(query, "id", None)
    bot = getattr(query, "bot", None)
    if query_id and bot is not None and getattr(bot, "session", None) is not None:
        method = RawAnswerInlineQuery(
            inline_query_id=query_id,
            results=[_serialize_inline_result(result) for result in results],
            **answer_kwargs,
        )
        await bot.session.make_request(bot, method)
        return
    await query.answer(results, **answer_kwargs)


def _build_inline_article_from_photo(
    result: types.InlineQueryResultPhoto,
    *,
    thumbnail_url: Optional[str],
) -> types.InlineQueryResultArticle:
    payload = result.model_dump(exclude_none=True)
    message_text = (
        payload.get("caption")
        or payload.get("description")
        or payload.get("title")
        or "Open this result in the bot."
    )
    parse_mode = payload.get("parse_mode")
    return types.InlineQueryResultArticle(
        id=payload["id"],
        title=payload.get("title") or "Media",
        description=payload.get("description"),
        thumbnail_url=thumbnail_url,
        input_message_content=types.InputTextMessageContent(
            message_text=message_text,
            parse_mode=parse_mode,
        ),
        reply_markup=payload.get("reply_markup"),
    )


def sanitize_inline_results(
    results: list[Any],
    *,
    force_remove_web_preview_urls: bool = False,
) -> list[Any]:
    sanitized: list[Any] = []
    for result in results:
        if isinstance(result, types.InlineQueryResultPhoto):
            thumbnail_url = None if force_remove_web_preview_urls else (
                _normalize_inline_http_url(result.thumbnail_url)
                if _looks_like_supported_inline_image_url(result.thumbnail_url)
                else None
            )
            photo_url = None if force_remove_web_preview_urls else (
                _normalize_inline_http_url(result.photo_url)
                if _looks_like_supported_inline_image_url(result.photo_url)
                else None
            )
            if photo_url:
                payload = result.model_dump(exclude_none=True)
                payload.pop("type", None)
                payload["photo_url"] = photo_url
                payload["thumbnail_url"] = thumbnail_url or photo_url
                sanitized.append(types.InlineQueryResultPhoto(**payload))
            else:
                sanitized.append(
                    _build_inline_article_from_photo(result, thumbnail_url=thumbnail_url)
                )
            continue

        if isinstance(result, types.InlineQueryResultArticle):
            thumbnail_url = None if force_remove_web_preview_urls else (
                _normalize_inline_http_url(result.thumbnail_url)
                if _looks_like_supported_inline_image_url(result.thumbnail_url)
                else None
            )
            sanitized.append(_clone_inline_article(result, thumbnail_url=thumbnail_url))
            continue

        sanitized.append(result)
    return sanitized


async def safe_answer_inline_query(
    query: types.InlineQuery,
    results: list[Any],
    **answer_kwargs: Any,
) -> None:
    sanitized_results = sanitize_inline_results(results)
    try:
        await _answer_inline_query(query, sanitized_results, **answer_kwargs)
    except TelegramAPIError as exc:
        error_text = str(exc)
        has_photo_results = any(
            isinstance(result, types.InlineQueryResultPhoto)
            for result in sanitized_results
        )
        if "WEBDOCUMENT_URL_INVALID" not in error_text and not has_photo_results:
            raise
        logging.warning(
            "Retrying inline answer with degraded previews: user_id=%s query=%s error=%s",
            getattr(getattr(query, "from_user", None), "id", None),
            summarize_text_for_log(getattr(query, "query", None)),
            exc,
        )
        degraded_results = sanitize_inline_results(
            sanitized_results,
            force_remove_web_preview_urls=True,
        )
        await _answer_inline_query(query, degraded_results, **answer_kwargs)


def build_start_deeplink_url(bot_url: str, payload: str) -> str:
    base = (bot_url or "").strip()
    if not base.startswith(("http://", "https://")):
        base = f"https://{base.lstrip('/')}"
    return f"{base}?start={payload}"


def build_inline_album_result(
    *,
    result_id: str,
    service_name: str,
    deep_link: str,
    message_text: str,
    preview_file_id: Optional[str] = None,
    preview_url: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
) -> types.InlineQueryResultCachedPhoto | types.InlineQueryResultPhoto | types.InlineQueryResultArticle:
    reply_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[
            types.InlineKeyboardButton(
                text=bm.inline_open_full_album_button(),
                url=deep_link,
            )
        ]]
    )
    if preview_file_id:
        return types.InlineQueryResultCachedPhoto(
            id=result_id,
            photo_file_id=str(preview_file_id),
            title=bm.inline_album_title(service_name),
            description=bm.inline_album_description(),
            caption=message_text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    if _looks_like_supported_inline_image_url(preview_url):
        return types.InlineQueryResultPhoto(
            id=result_id,
            photo_url=str(preview_url),
            thumbnail_url=str(thumbnail_url or preview_url),
            title=bm.inline_album_title(service_name),
            description=bm.inline_album_description(),
            caption=message_text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )

    return types.InlineQueryResultArticle(
        id=result_id,
        title=bm.inline_album_title(service_name),
        description=bm.inline_album_description(),
        thumbnail_url=thumbnail_url or preview_url,
        input_message_content=types.InputTextMessageContent(
            message_text=message_text,
            parse_mode="HTML",
        ),
        reply_markup=reply_markup,
    )
