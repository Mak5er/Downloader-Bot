import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.client_exceptions import ClientResponseError

from handlers import twitter
from services.inline_album_links import get_inline_album_request
from utils.download_manager import DownloadMetrics


class DummyResponse:
    def __init__(self, *, url=None, status_code=200, text=""):
        self.url = url
        self.status_code = status_code
        self._text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ClientResponseError(
                request_info=SimpleNamespace(real_url=self.url or "https://example.com"),
                history=(),
                status=self.status_code,
                message=f"status {self.status_code}",
                headers=None,
            )

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummySession:
    def __init__(self, handler):
        self._handler = handler

    def get(self, *args, **kwargs):
        return self._handler(*args, **kwargs)


@pytest.mark.asyncio
async def test_extract_tweet_ids_expands_short_links(monkeypatch):
    def fake_get(url, allow_redirects=True, timeout=5):
        return DummyResponse(url="https://twitter.com/user/status/1234567890")

    monkeypatch.setattr(twitter, "get_http_session", AsyncMock(return_value=DummySession(fake_get)))
    result = await twitter.extract_tweet_ids_async("Check this https://t.co/abc123")
    assert result == ["1234567890"]


@pytest.mark.asyncio
async def test_extract_tweet_ids_none(monkeypatch):
    monkeypatch.setattr(twitter, "get_http_session", AsyncMock(return_value=DummySession(lambda *a, **k: DummyResponse())))
    assert await twitter.extract_tweet_ids_async("No twitter links here") is None


@pytest.mark.asyncio
async def test_scrape_media_success(monkeypatch):
    sample_json = {"tweetURL": "https://twitter.com/user/status/1"}
    monkeypatch.setattr(
        twitter,
        "get_http_session",
        AsyncMock(return_value=DummySession(lambda url, timeout=None: DummyResponse(text=twitter.json.dumps(sample_json)))),
    )
    result = await twitter.scrape_media_async("1")
    assert result == sample_json


@pytest.mark.asyncio
async def test_collect_media_files_downloads_to_output_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(twitter, "OUTPUT_DIR", str(tmp_path))
    twitter.twitter_downloader.output_dir = str(tmp_path)

    async def fake_download(url, filename, **_kwargs):
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
        return DownloadMetrics(
            url=url,
            path=str(path),
            size=path.stat().st_size,
            elapsed=0.01,
            used_multipart=False,
            resumed=False,
        )

    monkeypatch.setattr(twitter.twitter_downloader, "download", fake_download)

    tweet_media = {
        "tweetURL": "https://x.com/user/status/42",
        "media_extended": [
            {"type": "image", "url": "https://cdn.example.com/1.jpg"},
            {"type": "video", "url": "https://cdn.example.com/2.mp4"},
        ]
    }

    photos, videos = await twitter._collect_media_files("42", tweet_media)

    assert len(photos) == 1
    assert len(videos) == 1
    assert os.path.exists(photos[0])
    assert os.path.exists(videos[0])


@pytest.mark.asyncio
async def test_reply_media_uses_cached_file_id_for_single_video(monkeypatch):
    status_message = SimpleNamespace(delete=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(id=99, type="private"),
        message_id=7,
        answer=AsyncMock(return_value=status_message),
        reply_video=AsyncMock(return_value=SimpleNamespace(video=SimpleNamespace(file_id="sent-file-id"))),
        reply_photo=AsyncMock(),
        answer_media_group=AsyncMock(),
        reply=AsyncMock(),
    )
    tweet_media = {
        "tweetURL": "https://x.com/user/status/123",
        "text": "caption",
        "likes": 1,
        "replies": 2,
        "retweets": 3,
        "media_extended": [{"type": "video", "url": "https://cdn.example.com/video.mp4"}],
    }

    monkeypatch.setattr(twitter, "send_analytics", AsyncMock())
    monkeypatch.setattr(twitter.db, "get_file_id", AsyncMock(return_value="cached-file-id"))
    monkeypatch.setattr(twitter.db, "add_file", AsyncMock())
    monkeypatch.setattr(twitter.twitter_downloader, "download", AsyncMock())
    monkeypatch.setattr(twitter, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(twitter, "safe_edit_text", AsyncMock(return_value=True))
    monkeypatch.setattr(twitter, "maybe_delete_user_message", AsyncMock())

    await twitter.reply_media(
        message,
        "123",
        tweet_media,
        "https://t.me/maxloadbot",
        None,
        {"delete_message": "off", "info_buttons": "off", "url_button": "off", "audio_button": "off"},
    )

    assert twitter.twitter_downloader.download.await_count == 0
    assert message.reply_video.await_args.kwargs["video"] == "cached-file-id"


@pytest.mark.asyncio
async def test_collect_media_entries_only_downloads_cache_misses(monkeypatch, tmp_path):
    monkeypatch.setattr(twitter, "OUTPUT_DIR", str(tmp_path))
    twitter.twitter_downloader.output_dir = str(tmp_path)
    get_file_id = AsyncMock(side_effect=["cached-photo-id", None])

    async def fake_download(url, filename, **_kwargs):
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return DownloadMetrics(
            url=url,
            path=str(path),
            size=path.stat().st_size,
            elapsed=0.01,
            used_multipart=False,
            resumed=False,
        )

    monkeypatch.setattr(twitter.db, "get_file_id", get_file_id)
    monkeypatch.setattr(twitter.twitter_downloader, "download", AsyncMock(side_effect=fake_download))

    entries = await twitter._collect_media_entries(
        "42",
        {
            "tweetURL": "https://x.com/user/status/42",
            "media_extended": [
                {"type": "image", "url": "https://cdn.example.com/1.jpg"},
                {"type": "video", "url": "https://cdn.example.com/2.mp4"},
            ],
        },
    )

    assert [entry["kind"] for entry in entries] == ["photo", "video"]
    assert entries[0]["file_id"] == "cached-photo-id"
    assert entries[1]["path"] is not None
    assert twitter.twitter_downloader.download.await_count == 1


@pytest.mark.asyncio
async def test_send_tweet_media_entries_replies_only_on_first_sent_message(monkeypatch):
    message = SimpleNamespace(
        message_id=7,
        answer_media_group=AsyncMock(return_value=[SimpleNamespace(photo=[SimpleNamespace(file_id="sent-photo-id")])]),
        answer_photo=AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="sent-last-photo-id")])),
        reply_photo=AsyncMock(),
        answer_video=AsyncMock(),
        reply_video=AsyncMock(),
    )
    entries = [
        {
            "kind": "photo",
            "cached": True,
            "file_id": "cached-photo-id",
            "path": None,
            "cache_key": "https://x.com/user/status/1#item:0:photo",
        },
        {
            "kind": "photo",
            "cached": True,
            "file_id": "cached-last-photo-id",
            "path": None,
            "cache_key": "https://x.com/user/status/1#item:1:photo",
        },
    ]

    monkeypatch.setattr(twitter.db, "add_file", AsyncMock())

    await twitter._send_tweet_media_entries(message, entries, "caption", None)

    assert message.answer_media_group.await_args.kwargs["reply_to_message_id"] == 7
    assert message.answer_photo.await_args.kwargs["photo"] == "cached-last-photo-id"
    assert message.reply_photo.await_count == 0


@pytest.mark.asyncio
async def test_inline_twitter_query_returns_album_deeplink_for_multi_video_post(monkeypatch):
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
        query="https://x.com/user/status/123456",
        answer=AsyncMock(),
    )
    tweet_media = {
        "tweetURL": "https://x.com/user/status/123456",
        "text": "multi video tweet",
        "media_extended": [
            {"type": "video", "url": "https://cdn.example.com/1.mp4"},
            {"type": "video", "url": "https://cdn.example.com/2.mp4"},
        ],
    }

    monkeypatch.setattr(twitter, "send_analytics", AsyncMock())
    monkeypatch.setattr(twitter.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(twitter, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(twitter, "_get_tweet_context", AsyncMock(return_value=("123456", tweet_media)))

    await twitter.inline_twitter_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.title == twitter.bm.inline_album_title("Twitter")
    assert result.thumbnail_url == twitter.get_inline_service_icon("twitter")
    deep_link = result.reply_markup.inline_keyboard[0][0].url
    token = deep_link.split("?start=album_", 1)[1]
    request = get_inline_album_request(token)
    assert request is not None
    assert request.service == "twitter"
    assert request.url == "https://x.com/user/status/123456"


@pytest.mark.asyncio
async def test_inline_twitter_query_prefers_media_thumbnail(monkeypatch):
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
        query="https://x.com/user/status/999",
        answer=AsyncMock(),
    )
    tweet_media = {
        "tweetURL": "https://x.com/user/status/999",
        "text": "video tweet",
        "media_extended": [
            {"type": "video", "url": "https://cdn.example.com/1.mp4", "thumbnail_url": "https://cdn.example.com/1.jpg"},
        ],
    }

    monkeypatch.setattr(twitter, "send_analytics", AsyncMock())
    monkeypatch.setattr(twitter.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(twitter, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(twitter, "_get_tweet_context", AsyncMock(return_value=("999", tweet_media)))

    await twitter.inline_twitter_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    assert results[0].thumbnail_url == "https://cdn.example.com/1.jpg"


@pytest.mark.asyncio
async def test_chosen_inline_twitter_result_edits_cached_inline_photo(monkeypatch):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    token = twitter.create_inline_video_request(
        "twitter",
        "https://x.com/user/status/999",
        42,
        settings,
    )
    result = SimpleNamespace(
        result_id=f"twitter_inline:{token}",
        inline_message_id="inline-twitter-photo",
        from_user=SimpleNamespace(full_name="Inline User"),
    )
    tweet_media = {
        "tweetURL": "https://x.com/user/status/999",
        "text": "photo tweet",
        "likes": 1,
        "replies": 2,
        "retweets": 3,
        "media_extended": [
            {"type": "image", "url": "https://cdn.example.com/1.jpg", "thumbnail_url": "https://cdn.example.com/1.jpg"},
        ],
    }

    monkeypatch.setattr(twitter, "_get_tweet_context", AsyncMock(return_value=("999", tweet_media)))
    monkeypatch.setattr(twitter.db, "get_file_id", AsyncMock(return_value="cached-photo-id"))
    monkeypatch.setattr(twitter, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(twitter, "safe_edit_inline_text", AsyncMock(return_value=True))
    monkeypatch.setattr(twitter, "safe_edit_inline_media", AsyncMock(return_value=True))

    await twitter.chosen_inline_twitter_result(result)

    media = twitter.safe_edit_inline_media.await_args.args[2]
    assert media.media == "cached-photo-id"


@pytest.mark.asyncio
async def test_inline_twitter_query_returns_open_in_bot_when_context_missing(monkeypatch):
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
        query="https://x.com/user/status/404",
        answer=AsyncMock(),
    )

    monkeypatch.setattr(twitter, "send_analytics", AsyncMock())
    monkeypatch.setattr(twitter.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(twitter, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(twitter, "_get_tweet_context", AsyncMock(return_value=None))

    await twitter.inline_twitter_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.title == "X / Twitter Post"
    assert "?start=album_" in result.reply_markup.inline_keyboard[0][0].url


@pytest.mark.asyncio
async def test_inline_twitter_query_returns_open_in_bot_when_media_list_empty(monkeypatch):
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
        query="https://x.com/user/status/777",
        answer=AsyncMock(),
    )
    tweet_media = {
        "tweetURL": "https://x.com/user/status/777",
        "text": "no structured media list",
        "media_extended": [],
    }

    monkeypatch.setattr(twitter, "send_analytics", AsyncMock())
    monkeypatch.setattr(twitter.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(twitter, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(twitter, "_get_tweet_context", AsyncMock(return_value=("777", tweet_media)))

    await twitter.inline_twitter_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.title == "X / Twitter Post"
    assert "?start=album_" in result.reply_markup.inline_keyboard[0][0].url


@pytest.mark.asyncio
async def test_inline_twitter_query_supports_multi_photo_mediaurls_payload(monkeypatch):
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
        query="https://x.com/user/status/888",
        answer=AsyncMock(),
    )
    tweet_media = {
        "tweetURL": "https://x.com/user/status/888",
        "text": "photo thread",
        "mediaURLs": [
            "https://cdn.example.com/1.jpg",
            "https://cdn.example.com/2.jpg",
        ],
    }

    monkeypatch.setattr(twitter, "send_analytics", AsyncMock())
    monkeypatch.setattr(twitter, "CHANNEL_ID", None)
    monkeypatch.setattr(twitter.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(twitter, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(twitter, "_get_tweet_context", AsyncMock(return_value=("888", tweet_media)))

    await twitter.inline_twitter_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.title == twitter.bm.inline_album_title("Twitter")
    assert result.photo_url == "https://cdn.example.com/1.jpg"
    assert result.thumbnail_url == "https://cdn.example.com/1.jpg"


@pytest.mark.asyncio
async def test_inline_twitter_query_uses_first_media_preview_for_mixed_album(monkeypatch):
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
        query="https://x.com/user/status/555",
        answer=AsyncMock(),
    )
    tweet_media = {
        "tweetURL": "https://x.com/user/status/555",
        "text": "mixed media tweet",
        "media_extended": [
            {"type": "video", "url": "https://cdn.example.com/1.mp4", "thumbnail_url": "https://cdn.example.com/1.jpg"},
            {"type": "image", "url": "https://cdn.example.com/2.jpg", "thumbnail_url": "https://cdn.example.com/2.jpg"},
        ],
    }

    monkeypatch.setattr(twitter, "send_analytics", AsyncMock())
    monkeypatch.setattr(twitter.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(twitter, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(twitter, "_get_tweet_context", AsyncMock(return_value=("555", tweet_media)))

    await twitter.inline_twitter_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert result.photo_url == "https://cdn.example.com/1.jpg"
    assert result.thumbnail_url == "https://cdn.example.com/1.jpg"
    assert result.caption == twitter.bm.captions(settings["captions"], tweet_media["text"], "https://t.me/maxloadbot")
