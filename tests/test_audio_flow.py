from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services.media.audio_flow import run_audio_flow
from utils.download_manager import DownloadMetrics


def _make_metrics(tmp_path, size=4):
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"a" * size)
    return DownloadMetrics(
        url="https://example.com/audio.mp3",
        path=str(audio_path),
        size=size,
        elapsed=0.1,
        used_multipart=False,
        resumed=False,
    )


def _make_db(cached_file_id=None):
    return SimpleNamespace(
        get_file_id=AsyncMock(return_value=cached_file_id),
        add_file=AsyncMock(),
    )


def _make_flow_kwargs(db, **overrides):
    kwargs = {
        "cache_key": "https://example.com/track#audio",
        "db_service": db,
        "send_cached": AsyncMock(return_value=None),
        "download_audio": AsyncMock(return_value=None),
        "on_missing_audio": AsyncMock(),
        "max_file_size": 100,
        "on_too_large": AsyncMock(),
        "prepare_metadata": AsyncMock(
            return_value=SimpleNamespace(thumbnail_path=None, cleanup=Mock())
        ),
        "send_downloaded": AsyncMock(
            return_value=SimpleNamespace(audio=SimpleNamespace(file_id="sent-file-id"))
        ),
        "cleanup_path": AsyncMock(),
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.asyncio
async def test_run_audio_flow_serves_cached_file():
    db = _make_db(cached_file_id="cached-id")
    sent_message = object()
    on_after_send = AsyncMock()
    kwargs = _make_flow_kwargs(
        db,
        send_cached=AsyncMock(return_value=sent_message),
        on_after_send=on_after_send,
    )

    result = await run_audio_flow(**kwargs)

    assert result is not None
    assert result.from_cache is True
    assert result.file_id == "cached-id"
    assert result.sent_message is sent_message
    kwargs["send_cached"].assert_awaited_once_with("cached-id")
    kwargs["download_audio"].assert_not_awaited()
    db.add_file.assert_not_awaited()
    on_after_send.assert_awaited_once_with(result)


@pytest.mark.asyncio
async def test_run_audio_flow_downloads_sends_and_caches(tmp_path):
    db = _make_db()
    metrics = _make_metrics(tmp_path)
    prepared = SimpleNamespace(thumbnail_path=None, cleanup=Mock())
    on_after_send = AsyncMock()
    kwargs = _make_flow_kwargs(
        db,
        download_audio=AsyncMock(return_value=metrics),
        prepare_metadata=AsyncMock(return_value=prepared),
        on_after_send=on_after_send,
    )

    result = await run_audio_flow(**kwargs)

    assert result is not None
    assert result.from_cache is False
    assert result.file_id == "sent-file-id"
    kwargs["prepare_metadata"].assert_awaited_once_with(metrics.path)
    kwargs["send_downloaded"].assert_awaited_once_with(metrics.path, prepared)
    db.add_file.assert_awaited_once_with(kwargs["cache_key"], "sent-file-id", "audio")
    prepared.cleanup.assert_called_once_with()
    kwargs["cleanup_path"].assert_awaited_once_with(metrics.path)
    on_after_send.assert_awaited_once_with(result)


@pytest.mark.asyncio
async def test_run_audio_flow_reports_missing_audio():
    db = _make_db()
    kwargs = _make_flow_kwargs(db, download_audio=AsyncMock(return_value=None))

    result = await run_audio_flow(**kwargs)

    assert result is None
    kwargs["on_missing_audio"].assert_awaited_once_with()
    kwargs["send_downloaded"].assert_not_awaited()
    kwargs["cleanup_path"].assert_not_awaited()


@pytest.mark.asyncio
async def test_run_audio_flow_reports_too_large_and_cleans_up(tmp_path):
    db = _make_db()
    metrics = _make_metrics(tmp_path)
    kwargs = _make_flow_kwargs(
        db,
        download_audio=AsyncMock(return_value=metrics),
        max_file_size=metrics.size,
    )

    result = await run_audio_flow(**kwargs)

    assert result is None
    kwargs["on_too_large"].assert_awaited_once_with()
    kwargs["prepare_metadata"].assert_not_awaited()
    kwargs["send_downloaded"].assert_not_awaited()
    kwargs["cleanup_path"].assert_awaited_once_with(metrics.path)


@pytest.mark.asyncio
async def test_run_audio_flow_cleans_up_when_send_fails(tmp_path):
    db = _make_db()
    metrics = _make_metrics(tmp_path)
    prepared = SimpleNamespace(thumbnail_path=None, cleanup=Mock())
    kwargs = _make_flow_kwargs(
        db,
        download_audio=AsyncMock(return_value=metrics),
        prepare_metadata=AsyncMock(return_value=prepared),
        send_downloaded=AsyncMock(side_effect=RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await run_audio_flow(**kwargs)

    prepared.cleanup.assert_called_once_with()
    kwargs["cleanup_path"].assert_awaited_once_with(metrics.path)
    db.add_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_audio_flow_aborted_fetch_metadata_skips_download():
    db = _make_db()
    kwargs = _make_flow_kwargs(db, fetch_metadata=AsyncMock(return_value=False))

    result = await run_audio_flow(**kwargs)

    assert result is None
    kwargs["download_audio"].assert_not_awaited()
    kwargs["on_missing_audio"].assert_not_awaited()


@pytest.mark.asyncio
async def test_run_audio_flow_cache_store_error_is_delegated(tmp_path):
    db = _make_db()
    db.add_file = AsyncMock(side_effect=RuntimeError("db down"))
    metrics = _make_metrics(tmp_path)
    on_cache_store_error = AsyncMock()
    on_after_send = AsyncMock()
    kwargs = _make_flow_kwargs(
        db,
        download_audio=AsyncMock(return_value=metrics),
        on_cache_store_error=on_cache_store_error,
        on_after_send=on_after_send,
    )

    result = await run_audio_flow(**kwargs)

    assert result is not None
    assert result.file_id == "sent-file-id"
    on_cache_store_error.assert_awaited_once()
    on_after_send.assert_awaited_once_with(result)


@pytest.mark.asyncio
async def test_run_audio_flow_cache_store_error_propagates_without_handler(tmp_path):
    db = _make_db()
    db.add_file = AsyncMock(side_effect=RuntimeError("db down"))
    metrics = _make_metrics(tmp_path)
    prepared = SimpleNamespace(thumbnail_path=None, cleanup=Mock())
    kwargs = _make_flow_kwargs(
        db,
        download_audio=AsyncMock(return_value=metrics),
        prepare_metadata=AsyncMock(return_value=prepared),
    )

    with pytest.raises(RuntimeError, match="db down"):
        await run_audio_flow(**kwargs)

    prepared.cleanup.assert_called_once_with()
    kwargs["cleanup_path"].assert_awaited_once_with(metrics.path)


@pytest.mark.asyncio
async def test_run_audio_flow_records_download_history(tmp_path):
    db = _make_db()
    db.record_download = AsyncMock()
    metrics = _make_metrics(tmp_path)
    kwargs = _make_flow_kwargs(
        db,
        download_audio=AsyncMock(return_value=metrics),
        user_id=777,
        chat_id=-1001,
        chat_type="supergroup",
        service="youtube",
        url="https://youtube.com/watch?v=123",
        title="Test Track",
    )

    result = await run_audio_flow(**kwargs)
    assert result is not None
    db.record_download.assert_awaited_once_with(
        user_id=777,
        chat_id=-1001,
        chat_type="supergroup",
        service="youtube",
        url="https://youtube.com/watch?v=123",
        title="Test Track",
        file_type="audio",
        file_id="sent-file-id",
        file_size_bytes=metrics.size,
        duration_seconds=metrics.elapsed,
        status="success",
        error_message=None,
    )

