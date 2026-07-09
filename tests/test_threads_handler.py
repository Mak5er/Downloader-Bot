from unittest.mock import AsyncMock

import pytest

from handlers import threads
from services.platforms.threads_media import (
    ThreadsMediaService,
    extract_threads_post_code,
    parse_threads_post_html,
    strip_threads_url,
)


def _post_payload(code: str, *, media: dict, username: str = "author") -> str:
    return (
        '<script type="application/json" data-sjs>{"require":[{"post":'
        f'{{"code":"{code}","caption":{{"text":"Post caption"}},"user":{{"username":"{username}"}},{media}}}'
        "}]}</script>"
    )


def test_strip_threads_url_canonicalizes_domain_and_tracking():
    url = "https://threads.net/@author/post/Abc_123/?utm_source=share#fragment"
    assert strip_threads_url(url) == "https://www.threads.com/@author/post/Abc_123"
    assert extract_threads_post_code(url) == "Abc_123"


def test_parse_threads_post_html_extracts_only_target_post_and_best_variants():
    other = _post_payload(
        "other",
        media='"image_versions2":{"candidates":[{"url":"https://cdn.example/other.jpg","width":1,"height":1}]}',
    )
    target = _post_payload(
        "wanted",
        media=(
            '"carousel_media":['
            '{"image_versions2":{"candidates":[{"url":"https://cdn.example/small.jpg","width":100,"height":100},{"url":"https://cdn.example/large.jpg","width":1000,"height":900}]}},'
            '{"video_versions":[{"url":"https://cdn.example/low.mp4","width":320,"height":180},{"url":"https://cdn.example/high.mp4","width":1920,"height":1080}]}'
            ']'
        ),
        username="target_author",
    )

    post = parse_threads_post_html(other + target, "wanted")

    assert post is not None
    assert post.id == "wanted"
    assert post.author == "target_author"
    assert post.description == "Post caption"
    assert [(item.type, item.url) for item in post.media_list] == [
        ("photo", "https://cdn.example/large.jpg"),
        ("video", "https://cdn.example/high.mp4"),
    ]


def test_parse_threads_post_html_keeps_text_only_post():
    page = (
        '<script type="application/json" data-sjs>'
        '{"post":{"code":"text_only","caption":{"text":"Only text"},"user":{"username":"author"}}}'
        "</script>"
    )

    post = parse_threads_post_html(page, "text_only")

    assert post is not None
    assert post.description == "Only text"
    assert post.media_list == []


@pytest.mark.asyncio
async def test_threads_service_fetches_canonical_post_url(tmp_path):
    requested_urls: list[str] = []

    async def fetch_page(url: str) -> str:
        requested_urls.append(url)
        return _post_payload(
            "Wanted_123",
            media='"image_versions2":{"candidates":[{"url":"https://cdn.example/photo.jpg","width":100,"height":100}]}',
        )

    async def retry(operation, **_kwargs):
        return await operation()

    service = ThreadsMediaService(
        str(tmp_path),
        fetch_page_func=fetch_page,
        retry_async_operation_func=retry,
    )
    post = await service.fetch_post("https://threads.net/@author/post/Wanted_123?share=1")

    assert post is not None
    assert requested_urls == ["https://www.threads.com/@author/post/Wanted_123"]
    assert post.media_list[0].type == "photo"


@pytest.mark.asyncio
async def test_threads_text_post_replies_with_post_text(monkeypatch):
    message = type("Message", (), {})()
    message.reply = AsyncMock(return_value=object())
    post = threads.ThreadsPost(
        id="text_only",
        description="Only text",
        author="author",
        media_list=[],
    )
    settings = {"captions": "off", "delete_message": "off", "info_buttons": "off", "url_button": "off", "audio_button": "off"}
    monkeypatch.setattr(threads, "maybe_delete_user_message", AsyncMock())

    sent = await threads.process_threads_text_post(
        message,
        post,
        "https://www.threads.com/@author/post/text_only",
        "https://t.me/examplebot",
        settings,
    )

    assert sent is True
    assert "Only text" in message.reply.await_args.args[0]
    assert message.reply.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_inline_threads_text_post_returns_article_without_channel(monkeypatch):
    query = type("Query", (), {})()
    query.from_user = type("User", (), {"id": 42})()
    query.chat_type = "inline"
    query.query = "https://www.threads.com/@author/post/text_only"
    query.answer = AsyncMock()
    post = threads.ThreadsPost(
        id="text_only",
        description="Only text",
        author="author",
        media_list=[],
    )
    settings = {"captions": "off", "delete_message": "off", "info_buttons": "off", "url_button": "off", "audio_button": "off"}

    monkeypatch.setattr(threads, "CHANNEL_ID", None)
    monkeypatch.setattr(threads, "send_analytics", AsyncMock())
    monkeypatch.setattr(threads.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(threads, "get_bot_url", AsyncMock(return_value="https://t.me/examplebot"))
    monkeypatch.setattr(threads.threads_service, "fetch_post", AsyncMock(return_value=post))

    await threads.inline_threads_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    assert results[0].id == "threads_text_text_only"
    assert "Only text" in results[0].input_message_content.message_text
    assert results[0].input_message_content.parse_mode == "HTML"


@pytest.mark.asyncio
async def test_inline_threads_photo_returns_send_button(monkeypatch):
    query = type("Query", (), {})()
    query.from_user = type("User", (), {"id": 42})()
    query.chat_type = "inline"
    query.query = "https://www.threads.com/@author/post/photo_only"
    query.answer = AsyncMock()
    post = threads.ThreadsPost(
        id="photo_only",
        description="Photo post",
        author="author",
        media_list=[threads.ThreadsMedia(url="https://cdn.example/photo.jpg", type="photo")],
    )
    settings = {"captions": "on", "delete_message": "off", "info_buttons": "off", "url_button": "off", "audio_button": "off"}

    monkeypatch.setattr(threads, "CHANNEL_ID", -1001234567890)
    monkeypatch.setattr(threads, "send_analytics", AsyncMock())
    monkeypatch.setattr(threads.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(threads.db, "get_file_id", AsyncMock(return_value="cached-photo"))
    monkeypatch.setattr(threads, "get_bot_url", AsyncMock(return_value="https://t.me/examplebot"))
    monkeypatch.setattr(threads.threads_service, "fetch_post", AsyncMock(return_value=post))

    await threads.inline_threads_query(query)

    result = query.answer.await_args.args[0][0]
    assert result.id.startswith("threads_inline:")
    assert result.title == "Threads Photo"
    assert result.reply_markup.inline_keyboard[0][0].callback_data.startswith("inline:threads:")


@pytest.mark.asyncio
async def test_threads_handler_marks_success_for_single_photo(monkeypatch):
    message = type("Message", (), {})()
    message.business_connection_id = None
    message.from_user = type("User", (), {"id": 1})()
    message.chat = type("Chat", (), {"id": 2, "type": "private"})()
    message.message_id = 3
    message.text = "https://www.threads.com/@author/post/Wanted_123"
    message.caption = None
    message.answer = AsyncMock()

    lease = type("Lease", (), {"mark_success": lambda self: setattr(self, "success", True), "finish": lambda self: None})()
    lease.success = False
    post = type("Post", (), {"media_list": [type("Media", (), {"type": "photo", "url": "https://cdn.example/photo.jpg"})()]})()

    monkeypatch.setattr(threads, "claim_message_request", AsyncMock(return_value=lease))
    monkeypatch.setattr(threads, "send_analytics", AsyncMock())
    monkeypatch.setattr(threads, "react_to_message", AsyncMock())
    monkeypatch.setattr(threads, "load_user_settings", AsyncMock(return_value={"captions": "off", "delete_message": "off", "info_buttons": "off", "url_button": "off", "audio_button": "off"}))
    monkeypatch.setattr(threads, "get_bot_url", AsyncMock(return_value="https://t.me/examplebot"))
    monkeypatch.setattr(threads.threads_service, "fetch_post", AsyncMock(return_value=post))
    monkeypatch.setattr(threads, "process_threads_single_media", AsyncMock(return_value=True))
    monkeypatch.setattr(threads, "update_info", AsyncMock())

    await threads.process_threads(message)

    assert lease.success is True
    threads.process_threads_single_media.assert_awaited_once()
