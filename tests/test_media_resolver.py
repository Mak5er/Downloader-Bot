from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services.media import resolver as media_resolver


@pytest.mark.asyncio
async def test_resolve_cached_media_items_reuses_cache_and_tracks_download_paths(monkeypatch):
    db_service = SimpleNamespace(get_file_id=AsyncMock(side_effect=["cached-photo-id", None]))
    metrics = SimpleNamespace(path="/tmp/clip.mp4")
    download_item = AsyncMock(return_value=metrics)
    log_metrics = Mock()
    monkeypatch.setattr(media_resolver, "log_download_metrics", log_metrics)

    media_items, downloaded_paths = await media_resolver.resolve_cached_media_items(
        [
            SimpleNamespace(type="photo"),
            SimpleNamespace(type="video"),
        ],
        db_service=db_service,
        kind_getter=lambda item: item.type,
        build_cache_key=lambda index, _item, kind: f"post#{index}:{kind}",
        download_item=download_item,
        metrics_label="test_media",
        error_label="Resolver",
    )

    assert [item["type"] for item in media_items] == ["photo", "video"]
    assert media_items[0]["file_id"] == "cached-photo-id"
    assert downloaded_paths == ["/tmp/clip.mp4"]
    log_metrics.assert_called_once_with("test_media", metrics)


@pytest.mark.asyncio
async def test_resolve_cached_media_items_processes_all_items_by_default(monkeypatch):
    items = [SimpleNamespace(type="photo") for _ in range(13)]
    db_service = SimpleNamespace(get_file_id=AsyncMock(return_value=None))
    log_metrics = Mock()
    monkeypatch.setattr(media_resolver, "log_download_metrics", log_metrics)

    async def download_item(index, _item, _kind):
        return SimpleNamespace(path=f"/tmp/item-{index}.jpg")

    media_items, downloaded_paths = await media_resolver.resolve_cached_media_items(
        items,
        db_service=db_service,
        kind_getter=lambda item: item.type,
        build_cache_key=lambda index, _item, kind: f"post#{index}:{kind}",
        download_item=download_item,
        metrics_label="test_media",
        error_label="Resolver",
    )

    assert len(media_items) == 13
    assert [item["cache_key"] for item in media_items] == [f"post#{index}:photo" for index in range(13)]
    assert downloaded_paths == [f"/tmp/item-{index}.jpg" for index in range(13)]
    assert log_metrics.call_count == 13
