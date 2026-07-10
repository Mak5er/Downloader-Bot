import pytest

from services.platforms import spotify_media


class _FakeResponse:
    def __init__(self, text: str, *, status: int = 200):
        self._text = text
        self.status = status
        self.headers = {"Content-Type": "application/json"}

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeSpotifySession:
    def get(self, url, **_kwargs):
        if url == "https://open.spotify.com/oembed":
            return _FakeResponse(
                '{"title":"Never Gonna Give You Up",'
                '"thumbnail_url":"https://i.scdn.co/image/cover"}'
            )
        return _FakeResponse(
            '<meta property="og:description" '
            'content="Rick Astley · Whenever You Need Somebody · 1987">'
        )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://open.spotify.com/track/abc123?si=demo", ("track", "abc123")),
        ("https://open.spotify.com/intl-ua/track/abc123", ("track", "abc123")),
        ("spotify:track:abc123", ("track", "abc123")),
        ("https://example.com/track/abc123", None),
    ],
)
def test_parse_spotify_url(url, expected):
    assert spotify_media.parse_spotify_url(url) == expected


def test_parse_spotify_description_extracts_track_metadata():
    assert spotify_media.parse_spotify_description(
        "Artist Name · Track Name · 2026", "Fallback"
    ) == {
        "artists": "Artist Name",
        "title": "Track Name",
        "date": "2026",
    }


def test_parse_spotify_description_deduplicates_repeated_artist():
    assert spotify_media.parse_spotify_description(
        "SUDNO, sudno, SUDNO, Sudno, SUDNO · Тону · 2023",
        "Fallback",
    )["artists"] == "SUDNO"


def test_spotify_oembed_cover_is_upgraded_to_640_pixels():
    original = "https://i.scdn.co/image/ab67616d00001e02abcdef"

    assert spotify_media.get_high_resolution_spotify_image_url(original) == (
        "https://i.scdn.co/image/ab67616d0000b273abcdef"
    )


@pytest.mark.asyncio
async def test_oembed_track_keeps_track_title_when_page_description_names_album():
    track = await spotify_media._get_oembed_track(
        _FakeSpotifySession(),
        "https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8",
        "4PTG3Z6ehGkBFwjybzWkR8",
    )

    assert track["title"] == "Never Gonna Give You Up"
    assert track["artists"] == "Rick Astley"
