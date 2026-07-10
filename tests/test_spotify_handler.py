from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from handlers import spotify
from utils.download_manager import DownloadMetrics


@pytest.mark.asyncio
async def test_process_spotify_downloads_matching_track_with_metadata(monkeypatch, tmp_path):
    audio_path = tmp_path / "spotify.mp3"
    audio_path.write_bytes(b"audio")
    metrics = DownloadMetrics(
        url="https://youtube.com/watch?v=abcdefghijk",
        path=str(audio_path),
        size=audio_path.stat().st_size,
        elapsed=0.1,
        used_multipart=False,
        resumed=False,
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=7, username="tester", full_name="Tester"),
        business_connection_id=None,
        chat=SimpleNamespace(id=99, type="private"),
        answer=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
        reply_audio=AsyncMock(
            return_value=SimpleNamespace(audio=SimpleNamespace(file_id="telegram-audio-id"))
        ),
        reply=AsyncMock(),
    )
    track = {
        "spotify_id": "abc123",
        "title": "Track Name",
        "artists": "Artist Name",
        "thumbnail": "https://i.scdn.co/image/cover",
        "duration": 201,
        "source_url": "https://open.spotify.com/track/abc123",
    }

    monkeypatch.setattr(spotify, "should_skip_duplicate_business_message", AsyncMock(return_value=False))
    monkeypatch.setattr(spotify, "react_to_message", AsyncMock())
    monkeypatch.setattr(spotify, "send_analytics", AsyncMock())
    monkeypatch.setattr(spotify, "load_user_settings", AsyncMock(return_value={"captions": "off", "delete_message": "off"}))
    monkeypatch.setattr(spotify, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(spotify, "get_bot_avatar_thumbnail", AsyncMock(return_value=None))
    monkeypatch.setattr(spotify.db, "get_file_id", AsyncMock(return_value=None))
    monkeypatch.setattr(spotify.db, "add_file", AsyncMock())
    monkeypatch.setattr(spotify, "get_spotify_track", AsyncMock(return_value=track))
    monkeypatch.setattr(spotify, "search_youtube_track", lambda _query: {"webpage_url": metrics.url})
    monkeypatch.setattr(spotify, "download_mp3_with_ytdlp_metrics", AsyncMock(return_value=metrics))
    prepared_metadata = SimpleNamespace(thumbnail_path=None, cleanup=Mock())
    monkeypatch.setattr(
        spotify,
        "prepare_mp3_metadata",
        AsyncMock(return_value=prepared_metadata),
    )
    monkeypatch.setattr(spotify, "send_chat_action_if_needed", AsyncMock())
    monkeypatch.setattr(spotify, "safe_edit_text", AsyncMock(return_value=True))
    monkeypatch.setattr(spotify, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(spotify, "remove_file", AsyncMock())
    monkeypatch.setattr(spotify, "update_info", AsyncMock())

    await spotify.process_spotify(
        message, direct_url="https://open.spotify.com/track/abc123?si=demo"
    )

    spotify.download_mp3_with_ytdlp_metrics.assert_awaited_once()
    spotify.prepare_mp3_metadata.assert_awaited_once_with(str(audio_path), track)
    prepared_metadata.cleanup.assert_called_once_with()
    kwargs = message.reply_audio.await_args.kwargs
    assert kwargs["audio"].filename == "Track Name.mp3"
    assert kwargs["title"] == "Track Name"
    assert kwargs["performer"] == "Artist Name"
    assert kwargs["duration"] == 201
    spotify.db.add_file.assert_awaited_once()
