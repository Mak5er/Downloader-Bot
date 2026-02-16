import json

import pytest

from utils import cobalt_client


class _DummyResponse:
    def __init__(self, status, payload=None, text_body=None, headers=None):
        self.status = status
        self._payload = payload
        self._text_body = text_body if text_body is not None else json.dumps(payload)
        self.headers = headers or {"Content-Type": "application/json"}

    async def text(self):
        return self._text_body


class _DummyRequestCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DummySession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.post_calls = 0
        self.last_url = None
        self.last_kwargs = None

    def post(self, *args, **kwargs):
        self.post_calls += 1
        self.last_url = args[0] if args else None
        self.last_kwargs = kwargs
        idx = min(self.post_calls - 1, len(self._responses) - 1)
        return _DummyRequestCtx(self._responses[idx])


@pytest.mark.asyncio
async def test_fetch_cobalt_data_uses_endpoint_and_api_key_header(monkeypatch):
    payload = {"status": "tunnel", "url": "https://cdn.example.com/video.mp4"}
    session = _DummySession([_DummyResponse(200, payload=payload)])

    async def fake_get_http_session():
        return session

    monkeypatch.setattr(cobalt_client, "get_http_session", fake_get_http_session)

    data = await cobalt_client.fetch_cobalt_data(
        "https://cobalt.test",
        "test-key",
        {"url": "https://instagram.com/reel/xyz"},
        source="instagram",
        attempts=1,
    )

    assert data == payload
    assert session.post_calls == 1
    assert session.last_url == "https://cobalt.test/"
    assert session.last_kwargs["headers"]["Authorization"] == "Api-Key test-key"
    assert session.last_kwargs["headers"]["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_fetch_cobalt_data_returns_none_when_key_missing(monkeypatch):
    called = {"value": False}

    async def fake_get_http_session():
        called["value"] = True
        return None

    monkeypatch.setattr(cobalt_client, "get_http_session", fake_get_http_session)

    data = await cobalt_client.fetch_cobalt_data(
        "https://cobalt.test",
        None,
        {"url": "https://instagram.com/reel/xyz"},
        source="instagram",
        attempts=1,
    )

    assert data is None
    assert called["value"] is False


@pytest.mark.asyncio
async def test_fetch_cobalt_data_retries_on_502_non_json_then_succeeds(monkeypatch):
    payload = {"status": "tunnel", "url": "https://cdn.example.com/video.mp4"}
    session = _DummySession(
        [
            _DummyResponse(
                502,
                payload=None,
                text_body="<html>Bad Gateway</html>",
                headers={"Content-Type": "text/html; charset=UTF-8"},
            ),
            _DummyResponse(200, payload=payload),
        ]
    )

    async def fake_get_http_session():
        return session

    monkeypatch.setattr(cobalt_client, "get_http_session", fake_get_http_session)

    data = await cobalt_client.fetch_cobalt_data(
        "https://cobalt.test",
        "test-key",
        {"url": "https://instagram.com/reel/xyz"},
        source="instagram",
        attempts=3,
        retry_delay=0.0,
    )

    assert data == payload
    assert session.post_calls == 2


@pytest.mark.asyncio
async def test_fetch_cobalt_data_does_not_retry_on_400_non_json(monkeypatch):
    session = _DummySession(
        [
            _DummyResponse(
                400,
                payload=None,
                text_body="<html>Bad Request</html>",
                headers={"Content-Type": "text/html"},
            ),
        ]
    )

    async def fake_get_http_session():
        return session

    monkeypatch.setattr(cobalt_client, "get_http_session", fake_get_http_session)

    data = await cobalt_client.fetch_cobalt_data(
        "https://cobalt.test",
        "test-key",
        {"url": "https://instagram.com/reel/xyz"},
        source="instagram",
        attempts=3,
        retry_delay=0.0,
    )

    assert data is None
    assert session.post_calls == 1


@pytest.mark.asyncio
async def test_fetch_cobalt_data_retries_on_400_fetch_empty_then_succeeds(monkeypatch):
    payload = {"status": "tunnel", "url": "https://cdn.example.com/video.mp4"}
    session = _DummySession(
        [
            _DummyResponse(
                400,
                payload={"status": "error", "error": {"code": "error.api.fetch.empty", "context": None}},
            ),
            _DummyResponse(200, payload=payload),
        ]
    )

    async def fake_get_http_session():
        return session

    monkeypatch.setattr(cobalt_client, "get_http_session", fake_get_http_session)

    data = await cobalt_client.fetch_cobalt_data(
        "https://cobalt.test",
        "test-key",
        {"url": "https://instagram.com/reel/xyz"},
        source="instagram",
        attempts=3,
        retry_delay=0.0,
    )

    assert data == payload
    assert session.post_calls == 2


@pytest.mark.asyncio
async def test_fetch_cobalt_data_does_not_retry_on_400_auth_not_found(monkeypatch):
    session = _DummySession(
        [
            _DummyResponse(
                400,
                payload={"status": "error", "error": {"code": "error.api.auth.key.not_found", "context": None}},
            ),
        ]
    )

    async def fake_get_http_session():
        return session

    monkeypatch.setattr(cobalt_client, "get_http_session", fake_get_http_session)

    data = await cobalt_client.fetch_cobalt_data(
        "https://cobalt.test",
        "test-key",
        {"url": "https://instagram.com/reel/xyz"},
        source="instagram",
        attempts=3,
        retry_delay=0.0,
    )

    assert data is None
    assert session.post_calls == 1
