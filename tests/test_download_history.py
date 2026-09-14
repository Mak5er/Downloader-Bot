from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services.storage import db as db_module
from services.storage.models import DownloadHistory


def _make_mock_session():
    session = SimpleNamespace(
        add=Mock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        refresh=AsyncMock(),
        execute=AsyncMock(),
    )

    class _SessionCtx:
        add = session.add
        commit = session.commit
        rollback = session.rollback
        refresh = session.refresh
        execute = session.execute

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    return session, _SessionCtx


@pytest.mark.asyncio
async def test_record_download(monkeypatch):
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")
    session, session_ctx = _make_mock_session()
    monkeypatch.setattr(database, "SessionLocal", lambda: session_ctx())

    record = await database.record_download(
        user_id=12345,
        chat_id=12345,
        chat_type="private",
        service="twitter",
        url="https://x.com/user/status/123",
        title="Cool Tweet Video",
        file_type="video",
        file_id="tg_file_id_abc",
        file_size_bytes=2514440,
        duration_seconds=0.55,
        status="success",
    )

    session.add.assert_called_once()
    added_obj = session.add.call_args[0][0]
    assert isinstance(added_obj, DownloadHistory)
    assert added_obj.user_id == 12345
    assert added_obj.service == "twitter"
    assert added_obj.url == "https://x.com/user/status/123"
    assert added_obj.title == "Cool Tweet Video"
    assert added_obj.status == "success"
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once_with(added_obj)
    assert record is added_obj


@pytest.mark.asyncio
async def test_get_download_history(monkeypatch):
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")
    session, session_ctx = _make_mock_session()

    fake_items = [
        DownloadHistory(id=1, user_id=10, service="tiktok", url="https://vm.tiktok.com/1", status="success"),
        DownloadHistory(id=2, user_id=10, service="youtube", url="https://youtube.com/2", status="error"),
    ]
    exec_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: fake_items))
    session.execute.return_value = exec_result
    monkeypatch.setattr(database, "SessionLocal", lambda: session_ctx())

    items = await database.get_download_history(page=2, per_page=10, user_id=10, service="tiktok", status="success")

    assert len(items) == 2
    session.execute.assert_awaited_once()
    stmt = session.execute.await_args[0][0]
    stmt_str = str(stmt)
    assert "download_history" in stmt_str
    assert "user_id" in stmt_str
    assert "service" in stmt_str
    assert "status" in stmt_str


@pytest.mark.asyncio
async def test_get_download_history_count(monkeypatch):
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")
    session, session_ctx = _make_mock_session()

    exec_result = SimpleNamespace(scalar=lambda: 42)
    session.execute.return_value = exec_result
    monkeypatch.setattr(database, "SessionLocal", lambda: session_ctx())

    count = await database.get_download_history_count(user_id=10, service="youtube")

    assert count == 42
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_expired_history(monkeypatch):
    database = db_module.DataBase("postgresql://user:pass@localhost/testdb")
    select_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [101, 102]))
    delete_result = SimpleNamespace(rowcount=2)
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[select_result, delete_result, SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))])
    )

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Session:
        execute = session.execute

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def begin(self):
            return _Transaction()

    monkeypatch.setattr(database, "SessionLocal", lambda: _Session())

    deleted = await database.cleanup_expired_history(max_age_days=90)

    assert deleted == 2
    assert session.execute.await_count >= 2
    delete_statement = session.execute.await_args_list[1].args[0]
    assert "DELETE FROM download_history" in str(delete_statement)


def test_download_history_user_relationship():
    mapper = DownloadHistory.__mapper__
    user_rel = mapper.relationships["user"]
    assert user_rel.uselist is False
    assert str(user_rel.direction) == "RelationshipDirection.MANYTOONE"

