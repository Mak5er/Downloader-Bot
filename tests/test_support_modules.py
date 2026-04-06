import asyncio
from types import SimpleNamespace

import pytest

from filters import is_bot_admin
from utils import cobalt_media
from utils import http_client
from utils import media_cache


@pytest.mark.asyncio
async def test_http_client_reuses_shared_session_and_reopens_after_close(monkeypatch):
    created_sessions = []

    class DummyConnector:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class DummySession:
        def __init__(self, *, timeout, connector):
            self.timeout = timeout
            self.connector = connector
            self.closed = False

        async def close(self):
            self.closed = True

    def build_session(*, timeout, connector):
        session = DummySession(timeout=timeout, connector=connector)
        created_sessions.append(session)
        return session

    monkeypatch.setattr(http_client.aiohttp, "TCPConnector", DummyConnector)
    monkeypatch.setattr(http_client.aiohttp, "ClientSession", build_session)
    monkeypatch.setattr(http_client, "_session", None)
    monkeypatch.setattr(http_client, "_lock", asyncio.Lock())

    first = await http_client.get_http_session()
    second = await http_client.get_http_session()

    assert first is second
    assert first.connector.kwargs["limit"] == 128
    assert first.connector.kwargs["limit_per_host"] == 32

    await http_client.close_http_session()
    assert first.closed is True

    reopened = await http_client.get_http_session()
    assert reopened is not first
    assert len(created_sessions) == 2

    await http_client.close_http_session()


def test_classify_cobalt_media_type_uses_available_hints():
    assert cobalt_media.classify_cobalt_media_type("https://example.com/file", audio_only=True) == "audio"
    assert (
        cobalt_media.classify_cobalt_media_type(
            "https://example.com/file",
            declared_type="photo",
        )
        == "photo"
    )
    assert (
        cobalt_media.classify_cobalt_media_type(
            "https://example.com/file",
            mime_type="video/mp4",
        )
        == "video"
    )
    assert cobalt_media.classify_cobalt_media_type("https://example.com/song.mp3") == "audio"
    assert (
        cobalt_media.classify_cobalt_media_type(
            "https://example.com/file",
            filename="cover.webp",
        )
        == "photo"
    )
    assert cobalt_media.classify_cobalt_media_type("https://example.com/file.bin") == "video"


def test_build_media_cache_key_formats_variants_and_items():
    assert media_cache.build_media_cache_key("media") == "media"
    assert (
        media_cache.build_media_cache_key(
            "media",
            item_index=2,
            item_kind="photo",
            variant="thumb",
        )
        == "media#item:2:photo#thumb"
    )

    with pytest.raises(ValueError, match="base_key must not be empty"):
        media_cache.build_media_cache_key("  ")


@pytest.mark.asyncio
async def test_is_bot_admin_checks_configured_admin_ids(monkeypatch):
    monkeypatch.setattr(is_bot_admin, "ADMINS_UID", [5, 10])
    guard = is_bot_admin.IsBotAdmin()

    assert await guard(SimpleNamespace(from_user=SimpleNamespace(id=10))) is True
    assert await guard(SimpleNamespace(from_user=SimpleNamespace(id=2))) is False
