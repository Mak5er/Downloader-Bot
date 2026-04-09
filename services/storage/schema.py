import asyncio
import re
import time
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text

from services.logger import logger as logging
from services.storage.models import APP_SCHEMA_TABLES, Base

logging = logging.bind(service="db_schema")


class SchemaManagerMixin:
    @staticmethod
    def _select_schema_migration_action(existing_tables: set[str]) -> str:
        normalized_tables = set(existing_tables)
        if "alembic_version" in normalized_tables:
            return "upgrade"

        present_app_tables = APP_SCHEMA_TABLES & normalized_tables
        if not present_app_tables:
            return "upgrade"

        if APP_SCHEMA_TABLES.issubset(normalized_tables):
            return "stamp"

        missing_tables = ", ".join(sorted(APP_SCHEMA_TABLES - normalized_tables))
        present_tables = ", ".join(sorted(present_app_tables))
        raise RuntimeError(
            "Detected partially initialized schema without alembic_version. "
            f"Present tables: {present_tables}. Missing tables: {missing_tables}. "
            "Run a manual migration/bootstrap before starting the bot."
        )

    def _build_alembic_config(self) -> AlembicConfig:
        services_dir = Path(__file__).resolve().parent.parent
        config = AlembicConfig(str(services_dir / "alembic.ini"))
        config.set_main_option("script_location", str(services_dir / "alembic"))
        config.set_main_option("sqlalchemy.url", self.sync_url)
        config.attributes["skip_logging_config"] = True
        return config

    async def _get_existing_tables(self) -> set[str]:
        async with self.engine.connect() as conn:
            return set(await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names()))

    def _run_alembic_command(self, action: str, revision: str = "head") -> None:
        alembic_config = self._build_alembic_config()
        if action == "upgrade":
            command.upgrade(alembic_config, revision)
            return
        if action == "stamp":
            command.stamp(alembic_config, revision)
            return
        raise ValueError(f"Unsupported alembic action: {action}")

    async def _apply_schema_migrations(self) -> None:
        action = self._select_schema_migration_action(await self._get_existing_tables())
        await asyncio.to_thread(self._run_alembic_command, action, "head")

    async def _create_schema_with_metadata(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _sync_postgresql_sequences(self) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                text("SELECT setval(pg_get_serial_sequence('downloaded_files','id'), COALESCE(MAX(id),0)+1, false) FROM downloaded_files")
            )
            await conn.execute(
                text("SELECT setval(pg_get_serial_sequence('analytics_events','id'), COALESCE(MAX(id),0)+1, false) FROM analytics_events")
            )
            await conn.execute(
                text("SELECT setval(pg_get_serial_sequence('settings','id'), COALESCE(MAX(id),0)+1, false) FROM settings")
            )
            await conn.execute(
                text("SELECT setval(pg_get_serial_sequence('users','user_id'), COALESCE(MAX(user_id),0)+1, false) FROM users")
            )

    async def init_db(self, *, use_migrations: bool = True):
        started_at = time.perf_counter()
        if use_migrations:
            await self._apply_schema_migrations()
        else:
            await self._create_schema_with_metadata()

        if re.sub(r"^postgresql:", "postgresql+asyncpg:", self.sync_url).startswith("postgresql+asyncpg"):
            await self._sync_postgresql_sequences()

        logging.perf(
            "db_init",
            duration_ms=(time.perf_counter() - started_at) * 1000.0,
        )
