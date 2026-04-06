import services.inline_album_links as links


def setup_function():
    links._requests.clear()
    links._tokens_by_key.clear()


def test_get_inline_album_request_is_public_and_reusable():
    token = links.create_inline_album_request(1001, "instagram", "https://instagram.com/p/abc")

    first = links.get_inline_album_request(token)
    second = links.get_inline_album_request(token)

    assert first is not None
    assert second is not None
    assert first.service == "instagram"
    assert second.url == "https://instagram.com/p/abc"


def test_create_inline_album_request_reuses_token_for_same_service_and_url():
    first = links.create_inline_album_request(1001, "pinterest", "https://pinterest.com/pin/1")
    second = links.create_inline_album_request(2002, "pinterest", "https://pinterest.com/pin/1")

    assert first == second


def test_create_inline_album_request_returns_different_tokens_for_different_urls():
    first = links.create_inline_album_request(1001, "tiktok", "https://www.tiktok.com/@a/video/1")
    second = links.create_inline_album_request(1001, "tiktok", "https://www.tiktok.com/@a/video/2")

    assert first != second


def test_inline_album_request_expires_and_reissues_token(monkeypatch):
    now = 50.0
    monkeypatch.setattr(links.time, "monotonic", lambda: now)
    monkeypatch.setattr(links.secrets, "token_urlsafe", lambda _: f"token-{int(now)}")

    first = links.create_inline_album_request(1001, "instagram", "https://instagram.com/p/abc")
    assert links.get_inline_album_request(first) is not None

    now = 50.0 + links._INLINE_ALBUM_TTL_SECONDS + 1.0

    assert links.get_inline_album_request(first) is None

    second = links.create_inline_album_request(1001, "instagram", "https://instagram.com/p/abc")
    assert second != first
