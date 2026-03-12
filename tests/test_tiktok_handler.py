from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import tiktok
from services.inline_video_requests import create_inline_video_request, get_inline_video_request


@pytest.fixture(autouse=True)
def stub_user_agent(monkeypatch):
    monkeypatch.setattr(tiktok, "UserAgent", lambda: SimpleNamespace(random="Agent"))
    if hasattr(tiktok, "_expand_tiktok_url_cached"):
        tiktok._expand_tiktok_url_cached.cache_clear()


class DummyResponse:
    def __init__(self, url=None):
        self.url = url


def test_process_tiktok_url_expands_short_links(monkeypatch):
    expected_url = "https://www.tiktok.com/@user/video/123456"

    def fake_head(url, allow_redirects, headers, timeout=None):
        assert allow_redirects is True
        assert "User-Agent" in headers
        return DummyResponse(url=expected_url)

    monkeypatch.setattr(tiktok.requests, "head", fake_head)
    result = tiktok.process_tiktok_url("https://vm.tiktok.com/ABC123/")
    assert result == expected_url


def test_process_tiktok_url_returns_original_on_error(monkeypatch):
    original_url = "https://vm.tiktok.com/ABC123/"

    def boom(*_a, **_k):
        raise tiktok.requests.RequestException("fail")

    monkeypatch.setattr(tiktok.requests, "head", boom)
    result = tiktok.process_tiktok_url(original_url)
    assert result == original_url


def test_process_tiktok_url_strips_query(monkeypatch):
    expanded_url = "https://www.tiktok.com/@user/video/123456"

    def fake_head(url, allow_redirects, headers):
        return DummyResponse(url=f"{expanded_url}?is_from_webapp=1&sender_device=pc")

    monkeypatch.setattr(tiktok.requests, "head", fake_head)
    result = tiktok.process_tiktok_url(f"{expanded_url}?is_from_webapp=1&sender_device=pc")
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
    token = result.id.removeprefix("tiktok_inline:")
    request = get_inline_video_request(token)
    assert request is not None
    assert request.source_url == "https://www.tiktok.com/@creator/video/123"
    assert request.user_settings == settings


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

    assert tiktok.safe_edit_inline_text.await_count == 2
    assert tiktok.safe_edit_inline_text.await_args_list[0].args[2] == tiktok.bm.fetching_info_status()
    assert tiktok.safe_edit_inline_text.await_args_list[1].args[2] == tiktok.bm.uploading_status()
    media = tiktok.safe_edit_inline_media.await_args.args[2]
    assert media.media == "cached-file-id"
    assert media.caption is not None
    request = get_inline_video_request(token)
    assert request is not None
    assert request.state == "completed"
