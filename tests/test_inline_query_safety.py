from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import types
from aiogram.exceptions import TelegramBadRequest

from handlers.utils import safe_answer_inline_query


@pytest.mark.asyncio
async def test_safe_answer_inline_query_sanitizes_unsupported_thumbnail_url():
    query = SimpleNamespace(answer=AsyncMock())
    results = [
        types.InlineQueryResultArticle(
            id="article-1",
            title="Article",
            description="desc",
            thumbnail_url="https://example.com/thumb.webp",
            input_message_content=types.InputTextMessageContent(message_text="hello"),
        )
    ]

    await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)

    answered_results = query.answer.await_args.args[0]
    assert answered_results[0].thumbnail_url is None


@pytest.mark.asyncio
async def test_safe_answer_inline_query_keeps_photo_result_when_only_thumbnail_is_invalid():
    query = SimpleNamespace(answer=AsyncMock())
    results = [
        types.InlineQueryResultPhoto(
            id="photo-1",
            photo_url="https://example.com/photo.jpg",
            thumbnail_url="https://example.com/photo.webp",
            title="Photo",
            description="desc",
            caption="caption",
            parse_mode="HTML",
        )
    ]

    await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)

    answered_results = query.answer.await_args.args[0]
    assert isinstance(answered_results[0], types.InlineQueryResultPhoto)
    assert answered_results[0].photo_url == "https://example.com/photo.jpg"
    assert answered_results[0].thumbnail_url == "https://example.com/photo.jpg"


@pytest.mark.asyncio
async def test_safe_answer_inline_query_retries_without_web_preview_urls():
    error = TelegramBadRequest(
        method=SimpleNamespace(__api_method__="answerInlineQuery"),
        message="Telegram server says - Bad Request: WEBDOCUMENT_URL_INVALID",
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        query="inline query",
        answer=AsyncMock(side_effect=[error, None]),
    )
    results = [
        types.InlineQueryResultPhoto(
            id="photo-1",
            photo_url="https://example.com/photo.jpg",
            thumbnail_url="https://example.com/photo.jpg",
            title="Photo",
            description="desc",
            caption="caption",
            parse_mode="HTML",
        )
    ]

    await safe_answer_inline_query(query, results, cache_time=10, is_personal=True)

    assert query.answer.await_count == 2
    retried_results = query.answer.await_args_list[1].args[0]
    assert isinstance(retried_results[0], types.InlineQueryResultArticle)
    assert getattr(retried_results[0], "thumbnail_url", None) is None
