import re

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DATABASE_URL, DB_MAX_OVERFLOW, DB_POOL_SIZE, DB_POOL_TIMEOUT
from services.storage.analytics_repository import AnalyticsRepositoryMixin
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

        self.sync_url = re.sub(r"^postgresql\+asyncpg:", "postgresql:", self.database_url)
        self.async_url = re.sub(r"^postgresql:", "postgresql+asyncpg:", self.sync_url)

        self.engine = create_async_engine(
            self.async_url,
            echo=False,
            future=True,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=max(1, int(DB_POOL_SIZE)),
            max_overflow=max(0, int(DB_MAX_OVERFLOW)),
            pool_timeout=max(1.0, float(DB_POOL_TIMEOUT)),
            pool_use_lifo=True,
        )
        self._dialect_name = self.engine.dialect.name
        self.SessionLocal = async_sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
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
