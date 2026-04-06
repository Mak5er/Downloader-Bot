from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import pinterest
from services.inline_album_links import get_inline_album_request
from services.inline_video_requests import create_inline_video_request, get_inline_video_request
from utils.download_manager import DownloadMetrics


def test_strip_pinterest_url_strips_tracking():
    url = "https://www.pinterest.com/pin/123456789/?utm_source=copy#frag"
    assert pinterest.strip_pinterest_url(url) == "https://www.pinterest.com/pin/123456789/"


def test_parse_pinterest_post_tunnel():
    payload = {
        "status": "tunnel",
        "url": "https://cdn.example.com/video.mp4",
        "filename": "video.mp4",
    }
    post = pinterest.parse_pinterest_post(payload)
    assert post is not None
    assert post.media_list[0].type == "video"
    assert post.media_list[0].url == "https://cdn.example.com/video.mp4"
    assert post.description == ""


def test_parse_pinterest_post_picker():
    payload = {
        "status": "picker",
        "picker": [
            {"type": "photo", "url": "https://cdn.example.com/1.jpg", "thumb": "https://cdn.example.com/t1.jpg"},
            {"type": "video", "url": "https://cdn.example.com/2.mp4"},
        ],
    }
    post = pinterest.parse_pinterest_post(payload)
    assert post is not None
    assert [m.type for m in post.media_list] == ["photo", "video"]
    assert post.media_list[0].thumb == "https://cdn.example.com/t1.jpg"


def test_parse_pinterest_post_uses_metadata_title_as_description():
    payload = {
        "status": "local-processing",
        "type": "remux",
        "tunnel": ["https://cdn.example.com/video.mp4"],
        "output": {
            "type": "video/mp4",
            "filename": "fallback-title.mp4",
            "metadata": {"title": "Real Pinterest Description"},
        },
    }
    post = pinterest.parse_pinterest_post(payload)
    assert post is not None
    assert post.description == "Real Pinterest Description"


@pytest.mark.asyncio
async def test_pinterest_service_fetch_post_uses_cobalt_client(monkeypatch, tmp_path):
    captured = {}
    payload = {"status": "tunnel", "url": "https://cdn.example.com/video.mp4", "filename": "video.mp4"}

    async def fake_fetch_cobalt_data(base_url, api_key, request_payload, **kwargs):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        captured["payload"] = request_payload
        captured["kwargs"] = kwargs
        return payload

    monkeypatch.setattr(pinterest, "COBALT_API_URL", "https://cobalt.test")
    monkeypatch.setattr(pinterest, "COBALT_API_KEY", "test-key")
    monkeypatch.setattr(pinterest, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = pinterest.PinterestService(output_dir=str(tmp_path))
    post = await service.fetch_post("https://www.pinterest.com/pin/123/")

    assert post is not None
    assert captured["base_url"] == "https://cobalt.test"
    assert captured["api_key"] == "test-key"
    assert captured["payload"]["downloadMode"] == "auto"
    assert captured["kwargs"]["source"] == "pinterest"


@pytest.mark.asyncio
async def test_pinterest_service_download_media_success(monkeypatch, tmp_path):
    service = pinterest.PinterestService(output_dir=str(tmp_path))

    async def fake_download(url, filename, **_kwargs):
        path = tmp_path / filename
        path.write_bytes(b"video")
        return DownloadMetrics(
            url=url,
            path=str(path),
            size=path.stat().st_size,
            elapsed=0.01,
            used_multipart=False,
            resumed=False,
        )

    monkeypatch.setattr(service._downloader, "download", fake_download)
    metrics = await service.download_media("https://cdn.example.com/video.mp4", "pin.mp4")

    assert metrics is not None
    assert (tmp_path / "pin.mp4").exists()


@pytest.mark.asyncio
async def test_pinterest_service_download_media_handles_error(monkeypatch, tmp_path):
    service = pinterest.PinterestService(output_dir=str(tmp_path))

    async def fake_download(*_args, **_kwargs):
        raise pinterest.DownloadError("fail")

    monkeypatch.setattr(service._downloader, "download", fake_download)

    metrics = await service.download_media("https://cdn.example.com/video.mp4", "pin.mp4")
    assert metrics is None


@pytest.mark.asyncio
async def test_inline_pinterest_query_returns_send_button(monkeypatch):
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
        query="https://www.pinterest.com/pin/123456789/",
        answer=AsyncMock(),
    )
    post = pinterest.PinterestPost(
        id="pin-123",
        description="Pin video",
        media_list=[pinterest.PinterestMedia(url="https://cdn.example.com/video.mp4", type="video")],
    )

    monkeypatch.setattr(pinterest, "CHANNEL_ID", -1001234567890)
    monkeypatch.setattr(pinterest, "send_analytics", AsyncMock())
    monkeypatch.setattr(pinterest.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(pinterest, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(pinterest.pinterest_service, "fetch_post", AsyncMock(return_value=post))

    await pinterest.inline_pinterest_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.title == "Pinterest Video"
    assert result.reply_markup.inline_keyboard[0][0].text == "Send video inline"
    assert result.thumbnail_url == pinterest.get_inline_service_icon("pinterest")
    token = result.id.removeprefix("pinterest_inline:")
    request = get_inline_video_request(token)
    assert request is not None
    assert request.source_url == "https://www.pinterest.com/pin/123456789/"


@pytest.mark.asyncio
async def test_inline_pinterest_query_prefers_video_thumb(monkeypatch):
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
        query="https://www.pinterest.com/pin/123456789/",
        answer=AsyncMock(),
    )
    post = pinterest.PinterestPost(
        id="pin-124",
        description="Pin video",
        media_list=[pinterest.PinterestMedia(url="https://cdn.example.com/video.mp4", type="video", thumb="https://cdn.example.com/video.jpg")],
    )

    monkeypatch.setattr(pinterest, "CHANNEL_ID", -1001234567890)
    monkeypatch.setattr(pinterest, "send_analytics", AsyncMock())
    monkeypatch.setattr(pinterest.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(pinterest, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(pinterest.pinterest_service, "fetch_post", AsyncMock(return_value=post))

    await pinterest.inline_pinterest_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    assert results[0].thumbnail_url == "https://cdn.example.com/video.jpg"


@pytest.mark.asyncio
async def test_pinterest_media_group_uses_cached_file_ids(monkeypatch):
    status_message = SimpleNamespace(delete=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=10, type="private"),
        message_id=20,
        answer=AsyncMock(return_value=status_message),
        answer_media_group=AsyncMock(return_value=[SimpleNamespace(photo=[SimpleNamespace(file_id="cached-photo-id")])]),
        answer_video=AsyncMock(return_value=SimpleNamespace(video=SimpleNamespace(file_id="cached-video-id"))),
        answer_photo=AsyncMock(),
        reply_video=AsyncMock(return_value=SimpleNamespace(video=SimpleNamespace(file_id="cached-video-id"))),
        reply_photo=AsyncMock(),
    )
    post = pinterest.PinterestPost(
        id="pin-1",
        description="caption",
        media_list=[
            pinterest.PinterestMedia(url="https://cdn.example.com/1.jpg", type="photo"),
            pinterest.PinterestMedia(url="https://cdn.example.com/2.mp4", type="video"),
        ],
    )

    monkeypatch.setattr(pinterest, "send_chat_action_if_needed", AsyncMock())
    monkeypatch.setattr(pinterest, "safe_edit_text", AsyncMock(return_value=True))
    monkeypatch.setattr(pinterest, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(pinterest, "maybe_delete_user_message", AsyncMock())
    monkeypatch.setattr(pinterest.db, "get_file_id", AsyncMock(side_effect=["cached-photo-id", "cached-video-id"]))
    monkeypatch.setattr(pinterest.db, "add_file", AsyncMock())
    monkeypatch.setattr(pinterest.pinterest_service, "download_media", AsyncMock())

    await pinterest.process_pinterest_media_group(
        message,
        post,
        "https://www.pinterest.com/pin/123456789/",
        "https://t.me/maxloadbot",
        {"captions": "on", "delete_message": "off", "info_buttons": "off", "url_button": "off", "audio_button": "off"},
        None,
    )

    assert pinterest.pinterest_service.download_media.await_count == 0
    assert message.answer_media_group.await_count == 1
    assert message.answer_media_group.await_args.kwargs["reply_to_message_id"] == 20
    assert message.answer_video.await_args.kwargs["video"] == "cached-video-id"
    assert message.reply_video.await_count == 0


@pytest.mark.asyncio
async def test_pinterest_media_group_replies_only_on_first_sent_message(monkeypatch):
    status_message = SimpleNamespace(delete=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=10, type="private"),
        message_id=20,
        answer=AsyncMock(return_value=status_message),
        answer_media_group=AsyncMock(return_value=[SimpleNamespace(photo=[SimpleNamespace(file_id="sent-photo-id")])]),
        answer_video=AsyncMock(return_value=SimpleNamespace(video=SimpleNamespace(file_id="sent-video-id"))),
        answer_photo=AsyncMock(),
        reply_video=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    post = pinterest.PinterestPost(
        id="pin-1",
        description="caption",
        media_list=[
            pinterest.PinterestMedia(url="https://cdn.example.com/1.jpg", type="photo"),
            pinterest.PinterestMedia(url="https://cdn.example.com/2.mp4", type="video"),
        ],
    )

    monkeypatch.setattr(pinterest, "send_chat_action_if_needed", AsyncMock())
    monkeypatch.setattr(pinterest, "safe_edit_text", AsyncMock(return_value=True))
    monkeypatch.setattr(pinterest, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(pinterest, "maybe_delete_user_message", AsyncMock())
    monkeypatch.setattr(pinterest.db, "get_file_id", AsyncMock(return_value=None))
    monkeypatch.setattr(pinterest.db, "add_file", AsyncMock())
    monkeypatch.setattr(
        pinterest.pinterest_service,
        "download_media",
        AsyncMock(side_effect=[SimpleNamespace(path="/tmp/1.jpg"), SimpleNamespace(path="/tmp/2.mp4")]),
    )

    await pinterest.process_pinterest_media_group(
        message,
        post,
        "https://www.pinterest.com/pin/123456789/",
        "https://t.me/maxloadbot",
        {"captions": "on", "delete_message": "off", "info_buttons": "off", "url_button": "off", "audio_button": "off"},
        None,
    )

    assert message.answer_media_group.await_args.kwargs["reply_to_message_id"] == 20
    assert "reply_to_message_id" not in message.answer_video.await_args.kwargs
    assert message.answer_video.await_args.kwargs["video"].path == "/tmp/2.mp4"
    assert message.reply_video.await_count == 0
    assert message.reply_photo.await_count == 0


@pytest.mark.asyncio
async def test_inline_pinterest_query_returns_album_deeplink_for_multi_video_post(monkeypatch):
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
        query="https://www.pinterest.com/pin/123456789/",
        answer=AsyncMock(),
    )
    post = pinterest.PinterestPost(
        id="pin-inline-1",
        description="multi video pin",
        media_list=[
            pinterest.PinterestMedia(url="https://cdn.example.com/1.mp4", type="video"),
            pinterest.PinterestMedia(url="https://cdn.example.com/2.mp4", type="video"),
        ],
    )

    monkeypatch.setattr(pinterest, "send_analytics", AsyncMock())
    monkeypatch.setattr(pinterest.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(pinterest, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(pinterest.pinterest_service, "fetch_post", AsyncMock(return_value=post))

    await pinterest.inline_pinterest_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.title == pinterest.bm.inline_album_title("Pinterest")
    assert result.thumbnail_url == pinterest.get_inline_service_icon("pinterest")
    deep_link = result.reply_markup.inline_keyboard[0][0].url
    token = deep_link.split("?start=album_", 1)[1]
    request = get_inline_album_request(token)
    assert request is not None
    assert request.service == "pinterest"
    assert request.url == "https://www.pinterest.com/pin/123456789/"


@pytest.mark.asyncio
async def test_inline_pinterest_query_returns_send_button_for_single_photo(monkeypatch):
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
        query="https://www.pinterest.com/pin/123456789/",
        answer=AsyncMock(),
    )
    post = pinterest.PinterestPost(
        id="pin-photo-inline",
        description="single photo pin",
        media_list=[pinterest.PinterestMedia(url="https://cdn.example.com/photo.jpg", type="photo")],
    )

    monkeypatch.setattr(pinterest, "CHANNEL_ID", 12345)
    monkeypatch.setattr(pinterest, "send_analytics", AsyncMock())
    monkeypatch.setattr(pinterest.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(pinterest.db, "get_file_id", AsyncMock(return_value=None))
    monkeypatch.setattr(pinterest, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(pinterest.pinterest_service, "fetch_post", AsyncMock(return_value=post))

    await pinterest.inline_pinterest_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.title == "Pinterest Photo"
    assert result.reply_markup.inline_keyboard[0][0].text == "Send photo inline"
    assert result.thumbnail_url == "https://cdn.example.com/photo.jpg"


@pytest.mark.asyncio
async def test_inline_pinterest_query_uses_first_media_preview_for_mixed_album(monkeypatch):
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
        query="https://www.pinterest.com/pin/987654321/",
        answer=AsyncMock(),
    )
    post = pinterest.PinterestPost(
        id="pin-inline-mixed",
        description="mixed media pin",
        media_list=[
            pinterest.PinterestMedia(url="https://cdn.example.com/1.mp4", type="video", thumb="https://cdn.example.com/1.jpg"),
            pinterest.PinterestMedia(url="https://cdn.example.com/2.jpg", type="photo"),
        ],
    )

    monkeypatch.setattr(pinterest, "send_analytics", AsyncMock())
    monkeypatch.setattr(pinterest.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(pinterest, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(pinterest.pinterest_service, "fetch_post", AsyncMock(return_value=post))

    await pinterest.inline_pinterest_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.photo_url == "https://cdn.example.com/1.jpg"
    assert result.thumbnail_url == "https://cdn.example.com/1.jpg"
    assert result.caption == pinterest.bm.captions(settings["captions"], post.description, "https://t.me/maxloadbot")


@pytest.mark.asyncio
async def test_chosen_inline_pinterest_result_edits_inline_photo(monkeypatch):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    token = create_inline_video_request(
        "pinterest",
        "https://www.pinterest.com/pin/123456789/",
        42,
        settings,
    )
    result = SimpleNamespace(
        result_id=f"pinterest_inline:{token}",
        inline_message_id="inline-pin-photo",
        from_user=SimpleNamespace(full_name="Inline User"),
    )
    post = pinterest.PinterestPost(
        id="pin-photo-inline",
        description="single photo pin",
        media_list=[pinterest.PinterestMedia(url="https://cdn.example.com/photo.jpg", type="photo")],
    )

    monkeypatch.setattr(pinterest.pinterest_service, "fetch_post", AsyncMock(return_value=post))
    monkeypatch.setattr(pinterest.db, "get_file_id", AsyncMock(return_value="cached-photo-id"))
    monkeypatch.setattr(pinterest, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(pinterest, "safe_edit_inline_text", AsyncMock(return_value=True))
    monkeypatch.setattr(pinterest, "safe_edit_inline_media", AsyncMock(return_value=True))

    await pinterest.chosen_inline_pinterest_result(result)

    media = pinterest.safe_edit_inline_media.await_args.args[2]
    assert media.media == "cached-photo-id"
