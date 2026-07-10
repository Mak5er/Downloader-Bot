import io

from mutagen.id3 import ID3
from PIL import Image
import pytest

from services.media import audio_metadata


def test_build_audio_filename_keeps_title_readable_and_portable():
    assert audio_metadata.build_audio_filename('  Song: Name / Live?  ') == "Song - Name - Live.mp3"


def test_build_audio_filename_limits_utf8_byte_length():
    filename = audio_metadata.build_audio_filename("пісня" * 100)
    assert filename.endswith(".mp3")
    assert len(filename.encode("utf-8")) <= 180


def test_normalize_audio_artist_deduplicates_final_id3_value():
    assert audio_metadata.normalize_audio_artist(
        {"artists": "SUDNO, sudno, SUDNO, Sudno, SUDNO"}
    ) == "SUDNO"


@pytest.mark.asyncio
async def test_embed_mp3_metadata_writes_id3_tags(monkeypatch, tmp_path):
    path = tmp_path / "track.mp3"
    path.write_bytes(b"")
    monkeypatch.setattr(audio_metadata, "_download_cover", lambda _url: _async_result((None, None)))

    written = await audio_metadata.embed_mp3_metadata(
        str(path),
        {
            "title": "Track Name",
            "artists": "Artist",
            "album": "Album",
            "date": "2026",
            "source_url": "https://open.spotify.com/track/abc123",
        },
    )

    tags = ID3(path)
    assert written is True
    assert str(tags["TIT2"]) == "Track Name"
    assert str(tags["TPE1"]) == "Artist"
    assert str(tags["TALB"]) == "Album"
    assert str(tags["TDRC"]) == "2026"


@pytest.mark.asyncio
async def test_embed_mp3_metadata_writes_one_deduplicated_artist(monkeypatch, tmp_path):
    path = tmp_path / "track.mp3"
    path.write_bytes(b"")
    monkeypatch.setattr(audio_metadata, "_download_cover", lambda _url: _async_result((None, None)))

    await audio_metadata.embed_mp3_metadata(
        str(path),
        {
            "title": "Тону",
            "artists": ["SUDNO", "sudno", "Sudno", "SUDNO"],
            "album_artist": "SUDNO, sudno, SUDNO",
        },
    )

    tags = ID3(path)
    assert str(tags["TPE1"]) == "SUDNO"
    assert str(tags["TPE2"]) == "SUDNO"


async def _async_result(value):
    return value


class _ChunkedContent:
    async def read(self, _size):
        return b"first-"

    async def iter_chunked(self, _size):
        for chunk in (b"first-", b"second"):
            yield chunk


class _CoverResponse:
    status = 200
    content_length = None
    headers = {"Content-Type": "image/webp"}
    content = _ChunkedContent()

    def raise_for_status(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _CoverSession:
    def get(self, _url):
        return _CoverResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_download_cover_reads_every_response_chunk(monkeypatch):
    monkeypatch.setattr(audio_metadata.aiohttp, "ClientSession", lambda **_kwargs: _CoverSession())

    content, mime = await audio_metadata._download_cover("https://example.com/cover.webp")

    assert content == b"first-second"
    assert mime == "image/webp"


@pytest.mark.asyncio
async def test_prepare_mp3_metadata_converts_cover_to_compatible_jpeg(monkeypatch, tmp_path):
    source = io.BytesIO()
    Image.new("RGBA", (1200, 800), (30, 60, 90, 180)).save(source, format="WEBP")
    monkeypatch.setattr(
        audio_metadata,
        "_download_cover",
        lambda _url: _async_result((source.getvalue(), "image/webp")),
    )
    path = tmp_path / "track.mp3"
    path.write_bytes(b"")

    prepared = await audio_metadata.prepare_mp3_metadata(
        str(path),
        {"title": "Track", "artists": "Artist", "thumbnail": "https://example.com/cover.webp"},
    )

    tags = ID3(path)
    cover = tags.getall("APIC")[0]
    assert prepared.tagged is True
    assert cover.mime == "image/jpeg"
    with Image.open(io.BytesIO(cover.data)) as embedded:
        assert embedded.format == "JPEG"
        assert embedded.size == (1200, 800)
    assert prepared.thumbnail_path is not None
    with Image.open(prepared.thumbnail_path) as thumbnail:
        assert thumbnail.format == "JPEG"
        assert max(thumbnail.size) <= 320
    assert prepared.thumbnail_path.stat().st_size < 200 * 1024
    prepared.cleanup()
    assert not prepared.thumbnail_path.exists()
