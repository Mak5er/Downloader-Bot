import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers import instagram
from services.inline.album_links import get_inline_album_request
from services.inline.video_requests import create_inline_video_request
from utils.download_manager import DownloadMetrics


def test_strip_instagram_url_strips_tracking():
    url = "https://www.instagram.com/reel/ABC123/?utm_source=ig_web_copy_link#frag"
    assert instagram.strip_instagram_url(url) == "https://www.instagram.com/reel/ABC123/"


@pytest.mark.asyncio
async def test_instagram_service_download_media_success(monkeypatch, tmp_path):
    service = instagram.InstagramService(output_dir=str(tmp_path))

    async def fake_download(url, filename, **_kwargs):
        path = tmp_path / filename
        path.write_bytes(b"data")
        return DownloadMetrics(
            url=url,
            path=str(path),
            size=path.stat().st_size,
            elapsed=0.01,
            used_multipart=False,
            resumed=False,
        )

    monkeypatch.setattr(service._downloader, "download", fake_download)

    metrics = await service.download_media("https://example.com/v.mp4", "ig.mp4")

    assert metrics is not None
    assert (tmp_path / "ig.mp4").exists()


@pytest.mark.asyncio
async def test_instagram_service_download_media_handles_error(monkeypatch, tmp_path):
    service = instagram.InstagramService(output_dir=str(tmp_path))

    async def fake_download(*_args, **_kwargs):
        raise instagram.DownloadError("fail")

    monkeypatch.setattr(service._downloader, "download", fake_download)

    assert await service.download_media("https://example.com/v.mp4", "ig.mp4") is None


@pytest.mark.asyncio
async def test_instagram_service_fetch_data_parses_tunnel(monkeypatch, tmp_path):
    payload_response = {"status": "tunnel", "url": "https://cdn.example.com/video.mp4"}
    captured = {}

    async def fake_fetch_cobalt_data(base_url, api_key, payload, **kwargs):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return payload_response

    monkeypatch.setattr(instagram, "COBALT_API_URL", "https://cobalt.test")
    monkeypatch.setattr(instagram, "COBALT_API_KEY", "test-key")
    monkeypatch.setattr(instagram, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = instagram.InstagramService(output_dir=str(tmp_path))
    data = await service.fetch_data("https://instagram.com/reel/xyz")

    assert data is not None
    assert len(data.media_list) == 1
    assert data.media_list[0].url == "https://cdn.example.com/video.mp4"
    assert data.media_list[0].type == "video"
    assert captured["base_url"] == "https://cobalt.test"
    assert captured["api_key"] == "test-key"
    assert captured["payload"]["downloadMode"] == "auto"
    assert captured["kwargs"]["source"] == "instagram"


@pytest.mark.asyncio
async def test_instagram_service_fetch_data_parses_picker(monkeypatch, tmp_path):
    payload = {
        "status": "picker",
        "picker": [
            {"type": "photo", "url": "https://cdn.example.com/1.jpg"},
            {"type": "video", "url": "https://cdn.example.com/2.mp4"},
        ],
    }

    async def fake_fetch_cobalt_data(*_args, **_kwargs):
        return payload

    monkeypatch.setattr(instagram, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = instagram.InstagramService(output_dir=str(tmp_path))
    data = await service.fetch_data("https://instagram.com/p/abc")

    assert data is not None
    assert [m.type for m in data.media_list] == ["photo", "video"]
    assert [m.url for m in data.media_list] == [
        "https://cdn.example.com/1.jpg",
        "https://cdn.example.com/2.mp4",
    ]


@pytest.mark.asyncio
async def test_instagram_service_fetch_data_audio_uses_picker_audio(monkeypatch, tmp_path):
    payload = {
        "status": "picker",
        "audio": "https://cdn.example.com/audio.mp3",
        "picker": [
            {"type": "photo", "url": "https://cdn.example.com/1.jpg"},
        ],
    }
    captured = {}

    async def fake_fetch_cobalt_data(_base_url, _api_key, request_payload, **_kwargs):
        captured["payload"] = request_payload
        return payload

    monkeypatch.setattr(instagram, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = instagram.InstagramService(output_dir=str(tmp_path))
    data = await service.fetch_data("https://instagram.com/reel/xyz", audio_only=True)

    assert data is not None
    assert len(data.media_list) == 1
    assert data.media_list[0].type == "audio"
    assert data.media_list[0].url == "https://cdn.example.com/audio.mp3"
    assert captured["payload"]["downloadMode"] == "audio"


@pytest.mark.asyncio
async def test_instagram_service_fetch_data_returns_none_on_error_status(monkeypatch, tmp_path):
    payload = {"status": "error", "error": {"code": "api.fetch.failed"}}

    async def fake_fetch_cobalt_data(*_args, **_kwargs):
        return payload

    monkeypatch.setattr(instagram, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = instagram.InstagramService(output_dir=str(tmp_path))
    data = await service.fetch_data("https://instagram.com/reel/xyz")

    assert data is None


@pytest.mark.asyncio
async def test_instagram_service_fetch_data_returns_none_when_cobalt_client_fails(monkeypatch, tmp_path):
    async def fake_fetch_cobalt_data(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instagram, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = instagram.InstagramService(output_dir=str(tmp_path))
    data = await service.fetch_data("https://instagram.com/reel/xyz")

    assert data is None


@pytest.mark.asyncio
async def test_instagram_media_group_uses_cached_file_ids(monkeypatch):
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
    data = instagram.InstagramVideo(
        id="ig-1",
        description="caption",
        author="author",
        media_list=[
            instagram.InstagramMedia(url="https://cdn.example.com/1.jpg", type="photo"),
            instagram.InstagramMedia(url="https://cdn.example.com/2.mp4", type="video"),
        ],
    )

    monkeypatch.setattr(instagram, "send_analytics", AsyncMock())
    monkeypatch.setattr(instagram, "send_chat_action_if_needed", AsyncMock())
    monkeypatch.setattr(instagram, "safe_edit_text", AsyncMock(return_value=True))
    monkeypatch.setattr(instagram, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(instagram, "maybe_delete_user_message", AsyncMock())
    monkeypatch.setattr(instagram.db, "get_file_id", AsyncMock(side_effect=["cached-photo-id", "cached-video-id"]))
    monkeypatch.setattr(instagram.db, "add_file", AsyncMock())
    monkeypatch.setattr(instagram.inst_service, "download_media", AsyncMock())

    await instagram.process_instagram_media_group(
        message,
        data,
        "https://instagram.com/p/abc/",
        "https://t.me/maxloadbot",
        {"captions": "on", "delete_message": "off", "info_buttons": "off", "url_button": "off", "audio_button": "off"},
        None,
    )

    assert instagram.inst_service.download_media.await_count == 0
    assert message.answer_media_group.await_count == 1
    assert message.answer_media_group.await_args.kwargs["reply_to_message_id"] == 20
    assert message.answer_video.await_args.kwargs["video"] == "cached-video-id"
    assert message.reply_video.await_count == 0


@pytest.mark.asyncio
async def test_instagram_media_group_replies_only_on_first_sent_message(monkeypatch):
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
    data = instagram.InstagramVideo(
        id="ig-1",
        description="caption",
        author="author",
        media_list=[
            instagram.InstagramMedia(url="https://cdn.example.com/1.jpg", type="photo"),
            instagram.InstagramMedia(url="https://cdn.example.com/2.mp4", type="video"),
        ],
    )

    monkeypatch.setattr(instagram, "send_analytics", AsyncMock())
    monkeypatch.setattr(instagram, "send_chat_action_if_needed", AsyncMock())
    monkeypatch.setattr(instagram, "safe_edit_text", AsyncMock(return_value=True))
    monkeypatch.setattr(instagram, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(instagram, "maybe_delete_user_message", AsyncMock())
    monkeypatch.setattr(instagram.db, "get_file_id", AsyncMock(return_value=None))
    monkeypatch.setattr(instagram.db, "add_file", AsyncMock())
    monkeypatch.setattr(
        instagram.inst_service,
        "download_media",
        AsyncMock(side_effect=[SimpleNamespace(path="/tmp/1.jpg"), SimpleNamespace(path="/tmp/2.mp4")]),
    )

    await instagram.process_instagram_media_group(
        message,
        data,
        "https://instagram.com/p/abc/",
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
async def test_inline_instagram_query_returns_album_deeplink_for_multi_video_post(monkeypatch):
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
        query="https://www.instagram.com/p/abc123/",
        answer=AsyncMock(),
    )
    data = instagram.InstagramVideo(
        id="ig-inline-1",
        description="multi video post",
        author="author",
        media_list=[
            instagram.InstagramMedia(url="https://cdn.example.com/1.mp4", type="video"),
            instagram.InstagramMedia(url="https://cdn.example.com/2.mp4", type="video"),
        ],
    )

    monkeypatch.setattr(instagram, "send_analytics", AsyncMock())
    monkeypatch.setattr(instagram.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(instagram, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(instagram.inst_service, "fetch_data", AsyncMock(return_value=data))

    await instagram.inline_instagram_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.title == instagram.bm.inline_album_title("Instagram")
    assert result.thumbnail_url == instagram.get_inline_service_icon("instagram")
    deep_link = result.reply_markup.inline_keyboard[0][0].url
    token = deep_link.split("?start=album_", 1)[1]
    request = get_inline_album_request(token)
    assert request is not None
    assert request.service == "instagram"
    assert request.url.rstrip("/") == "https://www.instagram.com/p/abc123"


@pytest.mark.asyncio
async def test_inline_instagram_query_returns_send_button_for_single_photo(monkeypatch):
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
        query="https://www.instagram.com/p/photo123/",
        answer=AsyncMock(),
    )
    data = instagram.InstagramVideo(
        id="ig-photo-inline",
        description="single photo post",
        author="author",
        media_list=[instagram.InstagramMedia(url="https://cdn.example.com/photo.jpg", type="photo")],
    )

    monkeypatch.setattr(instagram, "CHANNEL_ID", 12345)
    monkeypatch.setattr(instagram, "send_analytics", AsyncMock())
    monkeypatch.setattr(instagram.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(instagram.db, "get_file_id", AsyncMock(return_value=None))
    monkeypatch.setattr(instagram, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(instagram.inst_service, "fetch_data", AsyncMock(return_value=data))

    await instagram.inline_instagram_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.title == "Instagram Photo"
    assert result.reply_markup.inline_keyboard[0][0].text == "Send photo inline"
    assert result.thumbnail_url == "https://cdn.example.com/photo.jpg"


@pytest.mark.asyncio
async def test_inline_instagram_query_prefers_video_thumb_for_album_preview(monkeypatch):
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
        query="https://www.instagram.com/p/xyz123/",
        answer=AsyncMock(),
    )
    data = instagram.InstagramVideo(
        id="ig-inline-2",
        description="multi video post",
        author="author",
        media_list=[
            instagram.InstagramMedia(url="https://cdn.example.com/1.mp4", type="video", thumb="https://cdn.example.com/1.jpg"),
            instagram.InstagramMedia(url="https://cdn.example.com/2.mp4", type="video", thumb="https://cdn.example.com/2.jpg"),
        ],
    )

    monkeypatch.setattr(instagram, "send_analytics", AsyncMock())
    monkeypatch.setattr(instagram.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(instagram, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(instagram.inst_service, "fetch_data", AsyncMock(return_value=data))

    await instagram.inline_instagram_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.photo_url == "https://cdn.example.com/1.jpg"
    assert result.thumbnail_url == "https://cdn.example.com/1.jpg"


@pytest.mark.asyncio
async def test_inline_instagram_query_uses_first_media_preview_for_mixed_album(monkeypatch):
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
        query="https://www.instagram.com/p/mixed123/",
        answer=AsyncMock(),
    )
    data = instagram.InstagramVideo(
        id="ig-inline-mixed",
        description="mixed media post",
        author="author",
        media_list=[
            instagram.InstagramMedia(url="https://cdn.example.com/1.mp4", type="video", thumb="https://cdn.example.com/1.jpg"),
            instagram.InstagramMedia(url="https://cdn.example.com/2.jpg", type="photo"),
        ],
    )

    monkeypatch.setattr(instagram, "send_analytics", AsyncMock())
    monkeypatch.setattr(instagram.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(instagram, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(instagram.inst_service, "fetch_data", AsyncMock(return_value=data))

    await instagram.inline_instagram_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.photo_url == "https://cdn.example.com/1.jpg"
    assert result.thumbnail_url == "https://cdn.example.com/1.jpg"
    assert result.caption == instagram.bm.captions(settings["captions"], data.description, "https://t.me/maxloadbot")
    assert result.caption == instagram.bm.captions(settings["captions"], data.description, "https://t.me/maxloadbot")


@pytest.mark.asyncio
async def test_chosen_inline_instagram_result_edits_inline_photo(monkeypatch):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    token = create_inline_video_request(
        "instagram",
        "https://www.instagram.com/p/photo123/",
        42,
        settings,
    )
    result = SimpleNamespace(
        result_id=f"instagram_inline:{token}",
        inline_message_id="inline-inst-photo",
        from_user=SimpleNamespace(full_name="Inline User"),
    )
    data = instagram.InstagramVideo(
        id="ig-photo-inline",
        description="single photo post",
        author="author",
        media_list=[instagram.InstagramMedia(url="https://cdn.example.com/photo.jpg", type="photo")],
    )

    monkeypatch.setattr(instagram.inst_service, "fetch_data", AsyncMock(return_value=data))
    monkeypatch.setattr(instagram.db, "get_file_id", AsyncMock(return_value="cached-photo-id"))
    monkeypatch.setattr(instagram, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(instagram, "safe_edit_inline_text", AsyncMock(return_value=True))
    monkeypatch.setattr(instagram, "safe_edit_inline_media", AsyncMock(return_value=True))

    await instagram.chosen_inline_instagram_result(result)

    media = instagram.safe_edit_inline_media.await_args.args[2]
    assert media.media == "cached-photo-id"
