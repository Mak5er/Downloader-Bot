from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import tiktok
from services.inline_album_links import get_inline_album_request
from services.inline_video_requests import create_inline_video_request, get_inline_video_request


@pytest.fixture(autouse=True)
def stub_user_agent(monkeypatch):
    monkeypatch.setattr(tiktok, "UserAgent", lambda: SimpleNamespace(random="Agent"))
    tiktok._expanded_tiktok_url_cache.clear()


class DummyResponse:
    def __init__(self, url=None):
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummySession:
    def __init__(self, *, head_handler=None):
        self._head_handler = head_handler

    def head(self, *args, **kwargs):
        assert self._head_handler is not None
        return self._head_handler(*args, **kwargs)


@pytest.mark.asyncio
async def test_process_tiktok_url_expands_short_links(monkeypatch):
    expected_url = "https://www.tiktok.com/@user/video/123456"

    def fake_head(url, allow_redirects, headers, timeout=None):
        assert allow_redirects is True
        assert "User-Agent" in headers
        return DummyResponse(url=expected_url)

    monkeypatch.setattr(tiktok, "get_http_session", AsyncMock(return_value=DummySession(head_handler=fake_head)))
    result = await tiktok.process_tiktok_url_async("https://vm.tiktok.com/ABC123/")
    assert result == expected_url


@pytest.mark.asyncio
async def test_process_tiktok_url_returns_original_on_error(monkeypatch):
    original_url = "https://vm.tiktok.com/ABC123/"

    def boom(*_a, **_k):
        raise tiktok.aiohttp.ClientError("fail")

    monkeypatch.setattr(tiktok, "get_http_session", AsyncMock(return_value=DummySession(head_handler=boom)))
    result = await tiktok.process_tiktok_url_async(original_url)
    assert result == original_url


@pytest.mark.asyncio
async def test_process_tiktok_url_strips_query(monkeypatch):
    expanded_url = "https://www.tiktok.com/@user/video/123456"

    def fake_head(url, allow_redirects, headers, timeout=None):
        return DummyResponse(url=f"{expanded_url}?is_from_webapp=1&sender_device=pc")

    monkeypatch.setattr(tiktok, "get_http_session", AsyncMock(return_value=DummySession(head_handler=fake_head)))
    result = await tiktok.process_tiktok_url_async(f"{expanded_url}?is_from_webapp=1&sender_device=pc")
    assert result == expanded_url


def test_get_video_id_from_url():
    url = "https://www.tiktok.com/@user/video/1234567890?lang=en"
    assert tiktok.get_video_id_from_url(url) == "1234567890"


@pytest.mark.asyncio
async def test_video_info_returns_dataclass():
    data = {
        "error": None,
        "code": 0,
        "data": {
            "id": "123",
            "title": "Funny video",
            "cover": "https://example.com/cover.jpg",
            "play_count": 1000,
            "digg_count": 100,
            "comment_count": 25,
            "share_count": 10,
            "music_info": {"play": "https://example.com/music.mp3"},
            "author": {"unique_id": "creator"},
        },
    }
    info = await tiktok.video_info(data)
    assert info is not None
    assert info.id == "123"
    assert info.author == "creator"


@pytest.mark.asyncio
async def test_video_info_none_on_error():
    assert await tiktok.video_info({"error": "quota"}) is None
    assert await tiktok.video_info({"error": None, "code": 1, "message": "bad"}) is None


@pytest.mark.asyncio
async def test_process_tiktok_photos_replies_only_on_first_sent_message(monkeypatch):
    status_message = SimpleNamespace(delete=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(id=99, type="private"),
        message_id=7,
        answer=AsyncMock(return_value=status_message),
        answer_media_group=AsyncMock(return_value=[SimpleNamespace(photo=[SimpleNamespace(file_id="sent-photo-id")])]),
        answer_photo=AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="sent-last-photo-id")])),
        reply_photo=AsyncMock(),
    )
    info = tiktok.TikTokVideo(
        id="123",
        description="caption",
        cover="https://cdn.example.com/cover.jpg",
        author="creator",
        views=1,
        likes=2,
        comments=3,
        shares=4,
        music_play_url="https://cdn.example.com/audio.mp3",
    )

    monkeypatch.setattr(tiktok, "send_analytics", AsyncMock())
    monkeypatch.setattr(tiktok, "video_info", AsyncMock(return_value=info))
    monkeypatch.setattr(tiktok, "send_chat_action_if_needed", AsyncMock())
    monkeypatch.setattr(tiktok, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(tiktok, "maybe_delete_user_message", AsyncMock())
    monkeypatch.setattr(tiktok.db, "get_file_id", AsyncMock(return_value=None))
    monkeypatch.setattr(tiktok.db, "add_file", AsyncMock())

    await tiktok.process_tiktok_photos(
        message,
        {"data": {"id": "123"}},
        "https://t.me/maxloadbot",
        {"captions": "on", "delete_message": "off", "info_buttons": "off", "url_button": "off", "audio_button": "off"},
        None,
        ["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.jpg"],
    )

    assert message.answer_media_group.await_args.kwargs["reply_to_message_id"] == 7
    assert message.answer_photo.await_args.kwargs["photo"] == "https://cdn.example.com/2.jpg"
    assert message.reply_photo.await_count == 0
    assert tiktok.db.add_file.await_count == 2


@pytest.mark.asyncio
async def test_inline_tiktok_query_returns_send_button(monkeypatch):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat_type="inline",
        query="https://www.tiktok.com/@creator/video/123",
        answer=AsyncMock(),
    )

    monkeypatch.setattr(tiktok, "CHANNEL_ID", None)
    monkeypatch.setattr(tiktok, "send_analytics", AsyncMock())
    monkeypatch.setattr(tiktok.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(tiktok.db, "get_file_id", AsyncMock(return_value="cached-file-id"))
    monkeypatch.setattr(tiktok, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(
        tiktok,
        "fetch_tiktok_data_with_retry",
        AsyncMock(
            return_value={
                "error": None,
                "code": 0,
                "data": {
                    "id": "123",
                    "title": "Funny video",
                    "cover": "https://example.com/cover.jpg",
                    "play_count": 1000,
                    "digg_count": 100,
                    "comment_count": 25,
                    "share_count": 10,
                    "music_info": {"play": "https://example.com/music.mp3"},
                    "author": {"unique_id": "creator"},
                },
            }
        ),
    )

    await tiktok.inline_tiktok_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.title == "TikTok Video"
    assert result.reply_markup.inline_keyboard[0][0].text == "Send video inline"
    assert result.reply_markup.inline_keyboard[0][0].callback_data == f"inline:tiktok:{result.id.removeprefix('tiktok_inline:')}"
    assert result.thumbnail_url == "https://example.com/cover.jpg"
    token = result.id.removeprefix("tiktok_inline:")
    request = get_inline_video_request(token)
    assert request is not None
    assert request.source_url == "https://www.tiktok.com/@creator/video/123"
    assert request.user_settings == settings


@pytest.mark.asyncio
async def test_inline_tiktok_query_sanitizes_invalid_preview_url(monkeypatch):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat_type="inline",
        query="https://www.tiktok.com/@creator/video/123",
        answer=AsyncMock(),
    )

    monkeypatch.setattr(tiktok, "CHANNEL_ID", 12345)
    monkeypatch.setattr(tiktok, "send_analytics", AsyncMock())
    monkeypatch.setattr(tiktok.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(tiktok.db, "get_file_id", AsyncMock(return_value="cached-file-id"))
    monkeypatch.setattr(tiktok, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(
        tiktok,
        "fetch_tiktok_data_with_retry",
        AsyncMock(
            return_value={
                "error": None,
                "code": 0,
                "data": {
                    "id": "123",
                    "title": "Funny video",
                    "cover": "https://example.com/cover.webp",
                    "play_count": 1000,
                    "digg_count": 100,
                    "comment_count": 25,
                    "share_count": 10,
                    "music_info": {"play": "https://example.com/music.mp3"},
                    "author": {"unique_id": "creator"},
                },
            }
        ),
    )

    await tiktok.inline_tiktok_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    assert results[0].thumbnail_url is None


@pytest.mark.asyncio
async def test_inline_tiktok_query_returns_send_button_for_single_photo(monkeypatch):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat_type="inline",
        query="https://www.tiktok.com/@creator/video/123",
        answer=AsyncMock(),
    )

    monkeypatch.setattr(tiktok, "CHANNEL_ID", 12345)
    monkeypatch.setattr(tiktok, "send_analytics", AsyncMock())
    monkeypatch.setattr(tiktok.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(tiktok.db, "get_file_id", AsyncMock(return_value=None))
    monkeypatch.setattr(tiktok, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(
        tiktok,
        "fetch_tiktok_data_with_retry",
        AsyncMock(
            return_value={
                "error": None,
                "code": 0,
                "data": {
                    "id": "123",
                    "title": "Funny photo",
                    "cover": "https://example.com/cover.jpg",
                    "play_count": 1000,
                    "digg_count": 100,
                    "comment_count": 25,
                    "share_count": 10,
                    "music_info": {"play": "https://example.com/music.mp3"},
                    "author": {"unique_id": "creator"},
                    "images": ["https://example.com/photo.jpg"],
                },
            }
        ),
    )

    await tiktok.inline_tiktok_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.title == "TikTok Photo"
    assert result.reply_markup.inline_keyboard[0][0].text == "Send photo inline"
    assert result.thumbnail_url == "https://example.com/photo.jpg"
    token = result.id.removeprefix("tiktok_inline:")
    request = get_inline_video_request(token)
    assert request is not None
    assert request.source_url == "https://www.tiktok.com/@creator/video/123"


@pytest.mark.asyncio
async def test_inline_tiktok_query_returns_album_deeplink_for_multi_photo_post(monkeypatch):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat_type="inline",
        query="https://www.tiktok.com/@creator/video/123",
        answer=AsyncMock(),
    )

    monkeypatch.setattr(tiktok, "send_analytics", AsyncMock())
    monkeypatch.setattr(tiktok.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(tiktok, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(
        tiktok,
        "fetch_tiktok_data_with_retry",
        AsyncMock(
            return_value={
                "error": None,
                "code": 0,
                "data": {
                    "id": "123",
                    "title": "Photo set",
                    "cover": "https://example.com/cover.jpg",
                    "play_count": 1000,
                    "digg_count": 100,
                    "comment_count": 25,
                    "share_count": 10,
                    "music_info": {"play": "https://example.com/music.mp3"},
                    "author": {"unique_id": "creator"},
                    "images": [
                        "https://example.com/photo-1.webp",
                        "https://example.com/photo-2.webp",
                    ],
                },
            }
        ),
    )

    await tiktok.inline_tiktok_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.title == tiktok.bm.inline_album_title("TikTok")
    assert result.thumbnail_url == "https://example.com/cover.jpg"
    deep_link = result.reply_markup.inline_keyboard[0][0].url
    token = deep_link.split("?start=album_", 1)[1]
    request = get_inline_album_request(token)
    assert request is not None
    assert request.service == "tiktok"
    assert request.url == "https://www.tiktok.com/@creator/video/123"


@pytest.mark.asyncio
async def test_chosen_inline_tiktok_result_edits_inline_message(monkeypatch):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    token = create_inline_video_request(
        "tiktok",
        "https://www.tiktok.com/@creator/video/123",
        42,
        settings,
    )
    result = SimpleNamespace(
        result_id=f"tiktok_inline:{token}",
        inline_message_id="inline-message-1",
        from_user=SimpleNamespace(full_name="Inline User"),
    )

    monkeypatch.setattr(
        tiktok,
        "fetch_tiktok_data_with_retry",
        AsyncMock(
            return_value={
                "error": None,
                "code": 0,
                "data": {
                    "id": "123",
                    "title": "Funny video",
                    "cover": "https://example.com/cover.jpg",
                    "play_count": 1000,
                    "digg_count": 100,
                    "comment_count": 25,
                    "share_count": 10,
                    "music_info": {"play": "https://example.com/music.mp3"},
                    "author": {"unique_id": "creator"},
                },
            }
        ),
    )
    monkeypatch.setattr(tiktok.db, "get_file_id", AsyncMock(return_value="cached-file-id"))
    monkeypatch.setattr(tiktok, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(tiktok, "safe_edit_inline_text", AsyncMock(return_value=True))
    monkeypatch.setattr(tiktok, "safe_edit_inline_media", AsyncMock(return_value=True))

    await tiktok.chosen_inline_tiktok_result(result)

    assert tiktok.safe_edit_inline_text.await_count == 1
    assert tiktok.safe_edit_inline_text.await_args_list[0].args[2] == tiktok.bm.uploading_status()
    media = tiktok.safe_edit_inline_media.await_args.args[2]
    assert media.media == "cached-file-id"
    assert media.caption is not None
    request = get_inline_video_request(token)
    assert request is not None
    assert request.state == "completed"


@pytest.mark.asyncio
async def test_chosen_inline_tiktok_result_edits_inline_photo(monkeypatch):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    token = create_inline_video_request(
        "tiktok",
        "https://www.tiktok.com/@creator/video/123",
        42,
        settings,
    )
    result = SimpleNamespace(
        result_id=f"tiktok_inline:{token}",
        inline_message_id="inline-message-photo",
        from_user=SimpleNamespace(full_name="Inline User"),
    )

    monkeypatch.setattr(
        tiktok,
        "fetch_tiktok_data_with_retry",
        AsyncMock(
            return_value={
                "error": None,
                "code": 0,
                "data": {
                    "id": "123",
                    "title": "Funny photo",
                    "cover": "https://example.com/cover.jpg",
                    "play_count": 1000,
                    "digg_count": 100,
                    "comment_count": 25,
                    "share_count": 10,
                    "music_info": {"play": "https://example.com/music.mp3"},
                    "author": {"unique_id": "creator"},
                    "images": ["https://example.com/photo.jpg"],
                },
            }
        ),
    )
    monkeypatch.setattr(tiktok.db, "get_file_id", AsyncMock(return_value="cached-photo-id"))
    monkeypatch.setattr(tiktok, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(tiktok, "safe_edit_inline_text", AsyncMock(return_value=True))
    monkeypatch.setattr(tiktok, "safe_edit_inline_media", AsyncMock(return_value=True))

    await tiktok.chosen_inline_tiktok_result(result)

    media = tiktok.safe_edit_inline_media.await_args.args[2]
    assert media.media == "cached-photo-id"
    request = get_inline_video_request(token)
    assert request is not None
    assert request.state == "completed"
