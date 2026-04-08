from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def _load_migration_module():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "alembic"
        / "versions"
        / "20260408_000003_analytics_event_indexes.py"
    )
    spec = spec_from_file_location("migration_20260408_000003", migration_path)
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
