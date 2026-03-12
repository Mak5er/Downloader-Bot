from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import pinterest
from services.inline_video_requests import get_inline_video_request
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
