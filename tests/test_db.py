from datetime import datetime, timedelta
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.dialects import postgresql

from services import db as db_module


def test_map_action_to_service_includes_soundcloud():
    assert db_module.DataBase._map_action_to_service("soundcloud_audio") == "SoundCloud"
    assert db_module.DataBase._map_action_to_service("inline_soundcloud_audio") == "SoundCloud"
    assert db_module.DataBase._map_action_to_service("pinterest_media") == "Pinterest"


def test_select_schema_migration_action_prefers_upgrade_for_empty_schema():
    assert db_module.DataBase._select_schema_migration_action(set()) == "upgrade"


def test_select_schema_migration_action_stamps_existing_legacy_schema():
    existing_tables = set(db_module.APP_SCHEMA_TABLES)
    assert db_module.DataBase._select_schema_migration_action(existing_tables) == "stamp"


def test_select_schema_migration_action_rejects_partial_legacy_schema():
    with pytest.raises(RuntimeError, match="partially initialized schema"):
        db_module.DataBase._select_schema_migration_action({"users", "settings"})


@pytest.mark.asyncio
async def test_init_db_prefers_alembic_migrations(monkeypatch):
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")
    apply_migrations = AsyncMock()
    create_schema = AsyncMock()
    sync_sequences = AsyncMock()
    monkeypatch.setattr(database, "_apply_schema_migrations", apply_migrations)
    monkeypatch.setattr(database, "_create_schema_with_metadata", create_schema)
    monkeypatch.setattr(database, "_sync_postgresql_sequences", sync_sequences)

    await database.init_db()

    apply_migrations.assert_awaited_once()
    create_schema.assert_not_awaited()
    sync_sequences.assert_awaited_once()


@pytest.mark.asyncio
async def test_init_db_can_fallback_to_metadata_bootstrap(monkeypatch):
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")
    apply_migrations = AsyncMock()
    create_schema = AsyncMock()
    sync_sequences = AsyncMock()
    monkeypatch.setattr(database, "_apply_schema_migrations", apply_migrations)
    monkeypatch.setattr(database, "_create_schema_with_metadata", create_schema)
    monkeypatch.setattr(database, "_sync_postgresql_sequences", sync_sequences)

    await database.init_db(use_migrations=False)

    apply_migrations.assert_not_awaited()
    create_schema.assert_awaited_once()
    sync_sequences.assert_awaited_once()


@pytest_asyncio.fixture
async def database(monkeypatch):
    if os.getenv("RUN_DB_TESTS") not in {"1", "true", "TRUE", "yes", "YES"}:
        pytest.skip("DB tests disabled. Set RUN_DB_TESTS=1 to enable.")

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL env not set for tests.")

    database = db_module.DataBase(db_url)
    await database.init_db()
    yield database
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_add_user_and_settings(database):
    await database.add_user(
        user_id=1,
        user_name="Alice",
        user_username="alice",
        chat_type="private",
        language="en",
        status="active",
    )

    assert await database.user_exist(1) is True

    info = await database.get_user_info(1)
    assert info[0] == "Alice"
    assert info[1] == "alice"
    assert info[2] == "active"

    settings = await database.user_settings(1)
    assert settings == {
        "captions": "off",
        "delete_message": "off",
        "info_buttons": "off",
        "url_button": "off",
        "audio_button": "off",
    }

    await database.set_user_setting(1, "captions", "on")
    updated = await database.user_settings(1)
    assert updated["captions"] == "on"


@pytest.mark.asyncio
async def test_set_user_setting_rejects_invalid_field():
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")

    with pytest.raises(ValueError, match="Unsupported user setting field"):
        await database.set_user_setting(1, "unknown_field", "on")


@pytest.mark.asyncio
async def test_set_user_setting_rejects_invalid_value():
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")

    with pytest.raises(ValueError, match="Unsupported user setting value"):
        await database.set_user_setting(1, "captions", "maybe")


@pytest.mark.asyncio
async def test_upsert_chat_creates_and_updates(database):
    await database.upsert_chat(
        user_id=555,
        user_name="Chat Alpha",
        user_username="alpha",
        chat_type="public",
        language="uk",
    )

    first = await database.get_user_info(555)
    assert first[0] == "Chat Alpha"
    assert first[2] == "active"

    await database.upsert_chat(
        user_id=555,
        user_name="Chat Beta",
        user_username="beta",
        chat_type="public",
        language="uk",
        status="inactive",
    )

    second = await database.get_user_info(555)
    assert second[0] == "Chat Beta"
    assert second[1] == "beta"
    assert second[2] == "inactive"


@pytest.mark.asyncio
async def test_user_status_transitions(database):
    await database.add_user(
        user_id=2,
        user_name="Bob",
        user_username="bob",
        chat_type="private",
        language="en",
        status="active",
    )

    await database.set_inactive(2)
    assert await database.status(2) == "inactive"

    await database.set_active(2)
    assert await database.status(2) == "active"

    await database.ban_user(2)
    assert await database.status(2) == "ban"

    await database.delete_user(2)
    assert await database.user_exist(2) is False


@pytest.mark.asyncio
async def test_downloaded_files_cache(database):
    await database.add_file("https://example.com/video", "file-id-1", "video")
    file_id = await database.get_file_id("https://example.com/video")
    assert file_id == "file-id-1"


@pytest.mark.asyncio
async def test_delete_user_removes_settings_before_user(monkeypatch):
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")
    session = SimpleNamespace(execute=AsyncMock())

    class _BeginCtx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _SessionCtx:
        execute = session.execute

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def begin(self):
            return _BeginCtx()

    monkeypatch.setattr(database, "SessionLocal", lambda: _SessionCtx())

    await database.delete_user(123)

    assert session.execute.await_count == 2
    first_stmt = session.execute.await_args_list[0].args[0]
    second_stmt = session.execute.await_args_list[1].args[0]
    assert "DELETE FROM settings" in str(first_stmt)
    assert "DELETE FROM users" in str(second_stmt)


@pytest.mark.asyncio
async def test_add_file_uses_upsert_and_refreshes_cache(monkeypatch):
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")
    session = SimpleNamespace(
        execute=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    class _SessionCtx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(database, "SessionLocal", lambda: _SessionCtx())
    monkeypatch.setattr(database, "_dialect_name", "postgresql")

    await database.add_file("https://example.com/video", "file-id-2", "video")

    stmt = session.execute.await_args.args[0]
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in compiled
    assert "DO UPDATE" in compiled
    assert database._file_cache["https://example.com/video"][1] == "file-id-2"


@pytest.mark.asyncio
async def test_get_file_id_positive_cache_has_no_ttl(monkeypatch):
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")
    database._file_cache["https://example.com/video"] = (0.0, "cached-file-id")

    class _UnusedSessionCtx:
        async def __aenter__(self):
            raise AssertionError("DB session should not be used for positive cache hit")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(database, "SessionLocal", lambda: _UnusedSessionCtx())
    monkeypatch.setattr(db_module.time, "monotonic", lambda: 10_000.0)

    assert await database.get_file_id("https://example.com/video") == "cached-file-id"


@pytest.mark.asyncio
async def test_get_file_id_negative_cache_expires_quickly(monkeypatch):
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")
    database._file_cache["https://example.com/video"] = (0.0, None)

    result = SimpleNamespace(scalar=lambda: "fresh-file-id")
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    class _SessionCtx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(database, "SessionLocal", lambda: _SessionCtx())
    monkeypatch.setattr(db_module.time, "monotonic", lambda: 16.0)

    assert await database.get_file_id("https://example.com/video") == "fresh-file-id"
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_settings_returns_defaults_when_db_lookup_fails(monkeypatch):
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")

    class _SessionCtx:
        async def __aenter__(self):
            raise TimeoutError("db timeout")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(database, "SessionLocal", lambda: _SessionCtx())

    settings = await database.user_settings(42)

    assert settings == db_module.DEFAULT_USER_SETTINGS


@pytest.mark.asyncio
async def test_user_settings_returns_stale_cache_when_db_lookup_fails(monkeypatch):
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")
    database._settings_cache[42] = (0.0, {"captions": "on", "delete_message": "off", "info_buttons": "off", "url_button": "off", "audio_button": "off"})

    class _SessionCtx:
        async def __aenter__(self):
            raise TimeoutError("db timeout")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(database, "SessionLocal", lambda: _SessionCtx())
    monkeypatch.setattr(db_module.time, "monotonic", lambda: 9999.0)

    settings = await database.user_settings(42)

    assert settings["captions"] == "on"


def test_prune_local_caches_removes_expired_and_overflow_entries():
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")
    database._settings_cache_max_entries = 2
    database._status_cache_max_entries = 2
    database._file_cache_max_entries = 2
    database._settings_cache = {
        1: (0.0, dict(db_module.DEFAULT_USER_SETTINGS)),
        2: (10.0, dict(db_module.DEFAULT_USER_SETTINGS)),
        3: (20.0, dict(db_module.DEFAULT_USER_SETTINGS)),
    }
    database._status_cache = {
        1: (0.0, "active"),
        2: (10.0, "inactive"),
        3: (20.0, "ban"),
    }
    database._file_cache = {
        "expired-miss": (0.0, None),
        "old-hit": (5.0, "file-1"),
        "fresh-hit": (20.0, "file-2"),
    }

    database._prune_local_caches(now=40.0, force=True)

    assert 1 not in database._settings_cache
    assert 1 not in database._status_cache
    assert "expired-miss" not in database._file_cache
    assert "fresh-hit" in database._file_cache


@pytest.mark.asyncio
async def test_downloaded_files_count(database):
    today = datetime.now()
    older = today - timedelta(days=10)

    async with database.SessionLocal() as session:
        async with session.begin():
            session.add(
                db_module.AnalyticsEvent(
                    user_id=1,
                    chat_type="private",
                    action_name="download_video",
                    created_at=today,
                )
            )
            session.add(
                db_module.AnalyticsEvent(
                    user_id=1,
                    chat_type="private",
                    action_name="download_video",
                    created_at=older,
                )
            )
            session.add(
                db_module.AnalyticsEvent(
                    user_id=1,
                    chat_type="private",
                    action_name="start",
                    created_at=today,
                )
            )

    snapshot = await database.get_download_stats("Year")
    week_counts = await database.get_downloaded_files_count("Week")
    year_counts = await database.get_downloaded_files_count("Year")

    assert snapshot.total_downloads == 2
    assert snapshot.totals_by_date[today.strftime("%Y-%m-%d")] == 1
    assert snapshot.totals_by_date[older.strftime("%Y-%m-%d")] == 1
    assert sum(week_counts.values()) == 1
    assert sum(year_counts.values()) == 2


@pytest.mark.asyncio
async def test_downloaded_files_by_service(database):
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")

    async with database.SessionLocal() as session:
        async with session.begin():
            session.add(
                db_module.AnalyticsEvent(
                    user_id=1,
                    chat_type="private",
                    action_name="instagram_reel",
                    created_at=today,
                )
            )
            session.add(
                db_module.AnalyticsEvent(
                    user_id=1,
                    chat_type="private",
                    action_name="tiktok_video",
                    created_at=today,
                )
            )
            session.add(
                db_module.AnalyticsEvent(
                    user_id=1,
                    chat_type="private",
                    action_name="unknown_action",
                    created_at=today,
                )
            )
            session.add(
                db_module.AnalyticsEvent(
                    user_id=1,
                    chat_type="private",
                    action_name="soundcloud_audio",
                    created_at=today,
                )
            )
            session.add(
                db_module.AnalyticsEvent(
                    user_id=1,
                    chat_type="private",
                    action_name="pinterest_media",
                    created_at=today,
                )
            )

    snapshot = await database.get_download_stats("Year")
    by_service = await database.get_downloaded_files_by_service("Year")
    assert snapshot.total_downloads == 5
    assert snapshot.service_totals["Instagram"] == 1
    assert snapshot.service_totals["TikTok"] == 1
    assert snapshot.service_totals["SoundCloud"] == 1
    assert snapshot.service_totals["Pinterest"] == 1
    assert snapshot.service_totals["Other"] == 1
    assert by_service["Instagram"][today_str] == 1
    assert by_service["TikTok"][today_str] == 1
    assert by_service["SoundCloud"][today_str] == 1
    assert by_service["Pinterest"][today_str] == 1
    assert by_service["Other"][today_str] == 1
