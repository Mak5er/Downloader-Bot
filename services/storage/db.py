import asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class SafeAsyncSession(AsyncSession):
    async def rollback(self) -> None:
        await asyncio.shield(super().rollback())

    async def close(self) -> None:
        await asyncio.shield(super().close())


from config import DATABASE_URL, DB_MAX_OVERFLOW, DB_POOL_SIZE, DB_POOL_TIMEOUT
from services.logger import logger as logging
from services.storage.analytics_repository import AnalyticsRepositoryMixin
from services.storage.database_url import to_async_database_url, to_sync_database_url
from services.storage.file_cache_repository import FileCacheRepositoryMixin
from services.storage.local_cache import LocalCacheMixin
from services.storage.models import (
    APP_SCHEMA_TABLES,
    AnalyticsEvent,
    Base,
    DEFAULT_USER_SETTINGS,
    DownloadedFile,
    Settings,
    StatsSnapshot,
    User,
)
from services.storage.schema import SchemaManagerMixin
from services.storage.user_repository import UserRepositoryMixin

logging = logging.bind(service="db")

__all__ = [
    "APP_SCHEMA_TABLES",
    "AnalyticsEvent",
    "Base",
    "DEFAULT_USER_SETTINGS",
    "DataBase",
    "DownloadedFile",
    "Settings",
    "StatsSnapshot",
    "User",
]


_POOL_LOG_INTERVAL = 5


def _register_pool_listeners(engine) -> None:
    if engine.dialect.name != "postgresql":
        return

    sync_engine = getattr(engine, "sync_engine", engine)
    pool = getattr(sync_engine, "pool", None)
    if pool is None:
        return
    tracked_checked_out = 0

    @event.listens_for(pool, "checkout")
    def _on_checkout(dbapi_connection, connection_record, connection_proxy):
        nonlocal tracked_checked_out
        tracked_checked_out += 1
        overflow = pool.overflow()
        if overflow > 0 and tracked_checked_out % _POOL_LOG_INTERVAL == 0:
            logging.perf(
                "db_pool_checkout",
                duration_ms=0,
                pool_size=pool.size(),
                overflow=overflow,
                checkedout=pool.checkedout(),
            )

    @event.listens_for(pool, "checkin")
    def _on_checkin(dbapi_connection, connection_record):
        pass

    @event.listens_for(pool, "connect")
    def _on_connect(dbapi_connection, connection_record):
        logging.event(
            "db_pool_connect",
            pool_size=pool.size(),
            overflow=pool.overflow(),
            checkedout=pool.checkedout(),
        )

    @event.listens_for(sync_engine, "close")
    def _on_engine_close(*args):
        logging.event("db_engine_close")

    logging.event(
        "db_pool_listeners_registered",
        pool_size=pool.size(),
        max_overflow=pool._max_overflow,
    )


class DataBase(
    SchemaManagerMixin,
    LocalCacheMixin,
    UserRepositoryMixin,
    FileCacheRepositoryMixin,
    AnalyticsRepositoryMixin,
):
    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or DATABASE_URL
        if not self.database_url:
            raise ValueError("DATABASE_URL is not set. Configure PostgreSQL connection string.")

        self.sync_url = to_sync_database_url(self.database_url)
        self.async_url = to_async_database_url(self.database_url)

        self.engine = create_async_engine(
            self.async_url,
            echo=False,
            future=True,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_size=max(1, int(DB_POOL_SIZE)),
            max_overflow=max(0, int(DB_MAX_OVERFLOW)),
            pool_timeout=max(1.0, float(DB_POOL_TIMEOUT)),
            pool_use_lifo=False,
        )
        self._dialect_name = self.engine.dialect.name
        self.SessionLocal = async_sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=SafeAsyncSession,
        )
        self._settings_cache: dict[int, tuple[float, dict[str, str]]] = {}
        self._file_cache: dict[str, tuple[float, str | None]] = {}
        self._status_cache: dict[int, tuple[float, str | None]] = {}
        self._settings_ttl_seconds = 120.0
        self._file_cache_hit_ttl_seconds: float | None = 24 * 60 * 60.0
        self._file_cache_miss_ttl_seconds = 15.0
        self._status_ttl_seconds = 20.0
        self._settings_cache_max_entries = 4096
        self._file_cache_max_entries = 8192
        self._status_cache_max_entries = 4096
        self._cache_cleanup_interval_seconds = 60.0
        self._last_cache_cleanup_monotonic = 0.0
        _register_pool_listeners(self.engine)
