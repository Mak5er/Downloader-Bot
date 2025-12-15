from datetime import datetime, timedelta
import os

import pytest
import pytest_asyncio

from services import db as db_module


@pytest_asyncio.fixture
async def database(monkeypatch):
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL env not set for tests.")

    async def noop_migration():
        return None

    monkeypatch.setattr(db_module, "run_alembic_migration", noop_migration)

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
    }

    await database.set_user_setting(1, "captions", "on")
    updated = await database.user_settings(1)
    assert updated["captions"] == "on"


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

    week_counts = await database.get_downloaded_files_count("Week")
    year_counts = await database.get_downloaded_files_count("Year")

    assert sum(week_counts.values()) == 1
    assert sum(year_counts.values()) == 2
