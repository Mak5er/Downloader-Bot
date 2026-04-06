from types import SimpleNamespace

import pytest

from services import inline_service_icons
from services import inline_video_requests
from services import pending_requests
from services import runtime_state_store
from services import runtime_stats


@pytest.fixture(autouse=True)
def isolated_runtime_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_state_store, "_STATE_FILE", tmp_path / "runtime_state.json")


def _configure_runtime_state(monkeypatch, tmp_path, *modules):
    state_file = tmp_path / "runtime_state.json"
    monkeypatch.setattr(runtime_state_store, "_STATE_FILE", state_file)
    for module in modules:
        monkeypatch.setattr(module, "_loaded", True)
    return state_file


def test_pending_requests_store_get_and_pop(monkeypatch):
    monkeypatch.setattr(pending_requests, "_pending", {})
    monkeypatch.setattr(pending_requests, "_loaded", True)
    request = pending_requests.PendingRequest(
        text="https://youtu.be/demo",
        notice_chat_id=10,
        notice_message_id=20,
        source_chat_id=-100,
        source_message_id=1,
    )

    pending_requests.set_pending(42, request)

    assert pending_requests.get_pending(42) is request
    assert pending_requests.pop_pending(42) is request
    assert pending_requests.get_pending(42) is None


def test_pending_requests_expire_after_ttl(monkeypatch):
    monkeypatch.setattr(pending_requests, "_pending", {})
    monkeypatch.setattr(pending_requests, "_loaded", True)
    now = 100.0
    monkeypatch.setattr(pending_requests.time, "time", lambda: now)

    request = pending_requests.PendingRequest(
        text="https://youtu.be/demo",
        notice_chat_id=10,
        notice_message_id=20,
    )
    pending_requests.set_pending(42, request)

    now = 100.0 + pending_requests._PENDING_TTL_SECONDS + 1.0

    assert pending_requests.get_pending(42) is None
    assert 42 not in pending_requests._pending


def test_pending_requests_reload_from_persisted_state(monkeypatch, tmp_path):
    _configure_runtime_state(monkeypatch, tmp_path, pending_requests)
    monkeypatch.setattr(pending_requests, "_pending", {})

    request = pending_requests.PendingRequest(
        text="https://youtu.be/demo",
        notice_chat_id=10,
        notice_message_id=20,
    )
    pending_requests.set_pending(42, request)

    monkeypatch.setattr(pending_requests, "_pending", {})
    monkeypatch.setattr(pending_requests, "_loaded", False)

    restored = pending_requests.get_pending(42)

    assert restored is not None
    assert restored.text == "https://youtu.be/demo"
    assert restored.notice_chat_id == 10


def test_inline_video_requests_lifecycle(monkeypatch):
    monkeypatch.setattr(inline_video_requests, "_requests", {})
    monkeypatch.setattr(inline_video_requests, "_loaded", True)
    monkeypatch.setattr(inline_video_requests.secrets, "token_urlsafe", lambda _: "token-123")

    original_settings = {"audio_button": "on"}
    token = inline_video_requests.create_inline_video_request(
        service="youtube",
        source_url="https://youtu.be/demo",
        owner_user_id=99,
        user_settings=original_settings,
    )
    original_settings["audio_button"] = "off"

    assert token == "token-123"
    created = inline_video_requests.get_inline_video_request(token)
    assert created is not None
    assert created.user_settings == {"audio_button": "on"}
    assert created.state == "pending"

    claimed = inline_video_requests.claim_inline_video_request(token)
    assert claimed is created
    assert claimed.state == "processing"
    assert inline_video_requests.claim_inline_video_request(token) is None

    with pytest.raises(ValueError, match="already_processing"):
        inline_video_requests.claim_inline_video_request_for_send(
            token,
            duplicate_handler="callback",
        )

    reset = inline_video_requests.reset_inline_video_request(token)
    assert reset is created
    assert reset.state == "pending"

    completed = inline_video_requests.complete_inline_video_request(token)
    assert completed is created
    assert completed.state == "completed"

    with pytest.raises(ValueError, match="already_completed"):
        inline_video_requests.claim_inline_video_request_for_send(
            token,
            duplicate_handler="callback",
        )

    assert (
        inline_video_requests.claim_inline_video_request_for_send(
            "missing-token",
            duplicate_handler="callback",
        )
        is None
    )


def test_inline_video_request_expires_after_completion(monkeypatch):
    monkeypatch.setattr(inline_video_requests, "_requests", {})
    monkeypatch.setattr(inline_video_requests, "_loaded", True)
    monkeypatch.setattr(inline_video_requests.secrets, "token_urlsafe", lambda _: "token-123")
    now = 200.0
    monkeypatch.setattr(inline_video_requests.time, "time", lambda: now)

    token = inline_video_requests.create_inline_video_request(
        service="youtube",
        source_url="https://youtu.be/demo",
        owner_user_id=99,
        user_settings={"captions": "on"},
    )
    inline_video_requests.complete_inline_video_request(token)

    now = 200.0 + inline_video_requests._COMPLETED_REQUEST_TTL_SECONDS + 1.0

    assert inline_video_requests.get_inline_video_request(token) is None
    assert token not in inline_video_requests._requests


def test_inline_video_request_rejects_non_owner(monkeypatch):
    monkeypatch.setattr(inline_video_requests, "_requests", {})
    monkeypatch.setattr(inline_video_requests, "_loaded", True)
    monkeypatch.setattr(inline_video_requests.secrets, "token_urlsafe", lambda _: "token-123")

    token = inline_video_requests.create_inline_video_request(
        service="youtube",
        source_url="https://youtu.be/demo",
        owner_user_id=99,
        user_settings={"captions": "on"},
    )

    with pytest.raises(PermissionError, match="token_owner_mismatch"):
        inline_video_requests.claim_inline_video_request_for_send(
            token,
            duplicate_handler="callback",
            actor_user_id=100,
        )


def test_inline_video_requests_reload_from_persisted_state(monkeypatch, tmp_path):
    _configure_runtime_state(monkeypatch, tmp_path, inline_video_requests)
    monkeypatch.setattr(inline_video_requests, "_requests", {})
    monkeypatch.setattr(inline_video_requests.secrets, "token_urlsafe", lambda _: "token-123")

    token = inline_video_requests.create_inline_video_request(
        service="youtube",
        source_url="https://youtu.be/demo",
        owner_user_id=99,
        user_settings={"captions": "on"},
    )

    monkeypatch.setattr(inline_video_requests, "_requests", {})
    monkeypatch.setattr(inline_video_requests, "_loaded", False)

    restored = inline_video_requests.get_inline_video_request(token)

    assert restored is not None
    assert restored.owner_user_id == 99
    assert restored.user_settings == {"captions": "on"}


def test_get_inline_service_icon_normalizes_service_name():
    assert "instagram" in inline_service_icons.get_inline_service_icon(" Instagram ")
    assert inline_service_icons.get_inline_service_icon("unknown") is None


def test_runtime_stats_snapshot_tracks_video_audio_and_other(monkeypatch):
    monkeypatch.setattr(runtime_stats, "_started_at", 100.0)
    monkeypatch.setattr(runtime_stats, "_total_downloads", 0)
    monkeypatch.setattr(runtime_stats, "_total_videos", 0)
    monkeypatch.setattr(runtime_stats, "_total_audio", 0)
    monkeypatch.setattr(runtime_stats, "_total_other", 0)
    monkeypatch.setattr(runtime_stats, "_total_bytes", 0)
    monkeypatch.setattr(runtime_stats, "_by_source", {})
    monkeypatch.setattr(runtime_stats.time, "monotonic", lambda: 130.0)

    runtime_stats.record_download(
        " Video API ",
        SimpleNamespace(size=128, path="clip.mp4"),
    )
    runtime_stats.record_download(
        "mp3-service",
        SimpleNamespace(size=-50, path="track"),
    )
    runtime_stats.record_download(
        "",
        SimpleNamespace(size=32, path="archive.bin"),
    )

    snapshot = runtime_stats.get_runtime_snapshot()

    assert snapshot.started_at_monotonic == 100.0
    assert snapshot.uptime_seconds == 30.0
    assert snapshot.total_downloads == 3
    assert snapshot.total_videos == 1
    assert snapshot.total_audio == 1
    assert snapshot.total_other == 1
    assert snapshot.total_bytes == 160
    assert snapshot.by_source["video api"] == {"count": 1, "bytes": 128}
    assert snapshot.by_source["mp3-service"] == {"count": 1, "bytes": 0}
    assert snapshot.by_source["unknown"] == {"count": 1, "bytes": 32}
