from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def _load_migration_module(filename="20260408_000003_analytics_event_indexes.py"):
    migration_path = Path(__file__).resolve().parents[1] / "services" / "alembic" / "versions" / filename
    spec = spec_from_file_location(f"migration_{filename.removesuffix('.py')}", migration_path)
    module = module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_analytics_index_migration_skips_existing_indexes(monkeypatch):
    module = _load_migration_module()
    create_index = Mock()
    monkeypatch.setattr(module.op, "create_index", create_index)
    monkeypatch.setattr(module.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        module.sa,
        "inspect",
        lambda _bind: SimpleNamespace(
            get_indexes=lambda _table: [
                {"name": "ix_analytics_events_created_at"},
                {"name": "ix_analytics_events_action_name_created_at"},
            ]
        ),
    )

    module.upgrade()

    create_index.assert_not_called()


def test_analytics_index_migration_creates_only_missing_indexes(monkeypatch):
    module = _load_migration_module()
    create_index = Mock()
    monkeypatch.setattr(module.op, "create_index", create_index)
    monkeypatch.setattr(module.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        module.sa,
        "inspect",
        lambda _bind: SimpleNamespace(
            get_indexes=lambda _table: [
                {"name": "ix_analytics_events_created_at"},
            ]
        ),
    )

    module.upgrade()

    create_index.assert_called_once_with(
        "ix_analytics_events_action_name_created_at",
        "analytics_events",
        ["action_name", "created_at"],
        unique=False,
    )


def test_repository_query_index_migration_skips_existing_indexes(monkeypatch):
    module = _load_migration_module("20260525_000004_repository_query_indexes.py")
    create_index = Mock()
    monkeypatch.setattr(module.op, "create_index", create_index)
    monkeypatch.setattr(module.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        module.sa,
        "inspect",
        lambda _bind: SimpleNamespace(
            get_indexes=lambda _table: [{"name": index_name} for index_name, _table, _columns in module.INDEXES]
        ),
    )

    module.upgrade()

    create_index.assert_not_called()


def test_repository_query_index_migration_creates_only_missing_indexes(monkeypatch):
    module = _load_migration_module("20260525_000004_repository_query_indexes.py")
    create_index = Mock()
    monkeypatch.setattr(module.op, "create_index", create_index)
    monkeypatch.setattr(module.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        module.sa,
        "inspect",
        lambda _bind: SimpleNamespace(
            get_indexes=lambda table: [
                {"name": index_name}
                for index_name, table_name, _columns in module.INDEXES
                if table_name == table and index_name != "ix_users_status"
            ]
        ),
    )

    module.upgrade()

    create_index.assert_called_once_with("ix_users_status", "users", ["status"], unique=False)


def test_analytics_user_id_index_migration_skips_existing(monkeypatch):
    module = _load_migration_module("20260615_000005_analytics_user_id_index.py")
    create_index = Mock()
    monkeypatch.setattr(module.op, "create_index", create_index)
    monkeypatch.setattr(module.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        module.sa,
        "inspect",
        lambda _bind: SimpleNamespace(
            get_indexes=lambda table: [
                {"name": "ix_analytics_events_user_id"},
                {"name": "ix_downloaded_files_date_added"},
            ]
        ),
    )

    module.upgrade()

    create_index.assert_not_called()


def test_analytics_user_id_index_migration_creates_missing(monkeypatch):
    module = _load_migration_module("20260615_000005_analytics_user_id_index.py")
    create_index = Mock()
    monkeypatch.setattr(module.op, "create_index", create_index)
    monkeypatch.setattr(module.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        module.sa,
        "inspect",
        lambda _bind: SimpleNamespace(
            get_indexes=lambda table: []
        ),
    )

    module.upgrade()

    assert create_index.call_count == 2
    create_index.assert_any_call("ix_analytics_events_user_id", "analytics_events", ["user_id"], unique=False)
    create_index.assert_any_call("ix_downloaded_files_date_added", "downloaded_files", ["date_added"], unique=False)
