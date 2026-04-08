import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import re
import time
from typing import Optional

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Column, Text, TIMESTAMP, func, select, delete, update, ForeignKey, BigInteger, inspect, text, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship

from config import DATABASE_URL
from log.logger import logger as logging
from services.settings import SETTING_DISABLED, SETTING_FIELDS, SETTING_VALUES, normalize_setting_value

logging = logging.bind(service="db")

Base = declarative_base()
NON_DOWNLOAD_ACTIONS = ("start", "settings")
DEFAULT_USER_SETTINGS = {
    "captions": SETTING_DISABLED,
    "delete_message": SETTING_DISABLED,
    "info_buttons": SETTING_DISABLED,
    "url_button": SETTING_DISABLED,
    "audio_button": SETTING_DISABLED,
}
APP_SCHEMA_TABLES = frozenset({
    "downloaded_files",
    "users",
    "analytics_events",
    "settings",
})


@dataclass(slots=True)
class StatsSnapshot:
    totals_by_date: dict[str, int] = field(default_factory=dict)
    by_service: dict[str, dict[str, int]] = field(default_factory=dict)
    service_totals: dict[str, int] = field(default_factory=dict)
    total_downloads: int = 0


class DownloadedFile(Base):
    __tablename__ = "downloaded_files"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    url = Column(Text, unique=True, nullable=False)
    file_id = Column(Text, nullable=False)
    date_added = Column(TIMESTAMP(timezone=True), server_default=func.now())
    file_type = Column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"

    user_id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_name = Column(Text, nullable=True)
    user_username = Column(Text, nullable=True)
    chat_type = Column(Text, nullable=True)
    language = Column(Text, nullable=True)
    status = Column(Text, nullable=True)

    settings = relationship("Settings", back_populates="user", uselist=False)


class AnalyticsEvent(Base):
    __tablename__ = "analytics_events"
    __table_args__ = (
        Index("ix_analytics_events_created_at", "created_at"),
        Index("ix_analytics_events_action_name_created_at", "action_name", "created_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    chat_type = Column(Text, nullable=True)
    action_name = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Settings(Base):
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("user_id", name="uq_settings_user_id"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"))
    captions = Column(Text, default=SETTING_DISABLED, nullable=False)
    delete_message = Column(Text, default=SETTING_DISABLED, nullable=False)
    info_buttons = Column(Text, default=SETTING_DISABLED, nullable=False)
    url_button = Column(Text, default=SETTING_DISABLED, nullable=False)
    audio_button = Column(Text, default=SETTING_DISABLED, nullable=False)

    user = relationship("User", back_populates="settings")


class DataBase:
    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or DATABASE_URL
        if not self.database_url:
            raise ValueError("DATABASE_URL is not set. Configure PostgreSQL connection string.")

        # Swap driver to asyncpg directly and ensure SSL for hosted Postgres providers.
        self.sync_url = re.sub(r"^postgresql\+asyncpg:", "postgresql:", self.database_url)
        self.async_url = re.sub(r"^postgresql:", "postgresql+asyncpg:", self.sync_url)

        self.engine = create_async_engine(
            self.async_url,
            echo=False,
            future=True,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        self._dialect_name = self.engine.dialect.name
        self.SessionLocal = async_sessionmaker(bind=self.engine, expire_on_commit=False, class_=AsyncSession)
        self._settings_cache: dict[int, tuple[float, dict[str, str]]] = {}
        self._file_cache: dict[str, tuple[float, Optional[str]]] = {}
        self._status_cache: dict[int, tuple[float, Optional[str]]] = {}
        self._settings_ttl_seconds = 120.0
        self._file_cache_hit_ttl_seconds: Optional[float] = 24 * 60 * 60.0
        self._file_cache_miss_ttl_seconds = 15.0
        self._status_ttl_seconds = 20.0
        self._settings_cache_max_entries = 4096
        self._file_cache_max_entries = 8192
        self._status_cache_max_entries = 4096
        self._cache_cleanup_interval_seconds = 60.0
        self._last_cache_cleanup_monotonic = 0.0

    def _prune_cache(
        self,
        cache: dict,
        *,
        now: float,
        max_entries: int,
        ttl_getter,
    ) -> None:
        expired_keys = []
        for key, (timestamp, value) in cache.items():
            ttl_seconds = ttl_getter(value)
            if ttl_seconds is not None and now - timestamp > ttl_seconds:
                expired_keys.append(key)

        for key in expired_keys:
            cache.pop(key, None)

        overflow = len(cache) - max_entries
        if overflow <= 0:
            return

        oldest_keys = sorted(cache, key=lambda item_key: cache[item_key][0])[:overflow]
        for key in oldest_keys:
            cache.pop(key, None)

    def _prune_local_caches(self, now: Optional[float] = None, *, force: bool = False) -> None:
        now = time.monotonic() if now is None else now
        if not force and now - self._last_cache_cleanup_monotonic < self._cache_cleanup_interval_seconds:
            return

        self._prune_cache(
            self._settings_cache,
            now=now,
            max_entries=self._settings_cache_max_entries,
            ttl_getter=lambda _value: self._settings_ttl_seconds,
        )
        self._prune_cache(
            self._status_cache,
            now=now,
            max_entries=self._status_cache_max_entries,
            ttl_getter=lambda _value: self._status_ttl_seconds,
        )
        self._prune_cache(
            self._file_cache,
            now=now,
            max_entries=self._file_cache_max_entries,
            ttl_getter=lambda value: self._file_cache_hit_ttl_seconds if value is not None else self._file_cache_miss_ttl_seconds,
        )
        self._last_cache_cleanup_monotonic = now

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
        # Embedded Alembic runs should preserve the application's own logging setup.
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

        if self.async_url.startswith("postgresql+asyncpg"):
            await self._sync_postgresql_sequences()

        logging.perf(
            "db_init",
            duration_ms=(time.perf_counter() - started_at) * 1000.0,
        )

    async def add_user(self, user_id, user_name, user_username, chat_type, language, status):
        async with self.SessionLocal() as session:
            async with session.begin():
                user = User(
                    user_id=user_id, user_name=user_name, user_username=user_username,
                    chat_type=chat_type, language=language, status=status
                )
                session.add(user)
        self._status_cache[int(user_id)] = (time.monotonic(), status)

    async def upsert_chat(self, user_id, user_name, user_username, chat_type, language=None, status="active"):
        values = {
            "user_name": user_name,
            "user_username": user_username,
            "chat_type": chat_type,
            "language": language,
            "status": status,
        }
        async with self.SessionLocal() as session:
            async with session.begin():
                if self._dialect_name == "postgresql":
                    stmt = (
                        pg_insert(User)
                        .values(user_id=user_id, **values)
                        .on_conflict_do_update(
                            index_elements=[User.user_id],
                            set_=values,
                        )
                    )
                    await session.execute(stmt)
                elif self._dialect_name == "sqlite":
                    stmt = (
                        sqlite_insert(User)
                        .values(user_id=user_id, **values)
                        .on_conflict_do_update(
                            index_elements=[User.user_id],
                            set_=values,
                        )
                    )
                    await session.execute(stmt)
                else:
                    existing = await session.execute(select(User).where(User.user_id == user_id))
                    record = existing.scalar_one_or_none()
                    if record:
                        await session.execute(
                            update(User)
                            .where(User.user_id == user_id)
                            .values(**values)
                        )
                    else:
                        session.add(
                            User(
                                user_id=user_id,
                                user_name=user_name,
                                user_username=user_username,
                                chat_type=chat_type,
                                language=language,
                                status=status,
                            )
                        )
        self._status_cache[int(user_id)] = (time.monotonic(), status)

    async def delete_user(self, user_id):
        async with self.SessionLocal() as session:
            async with session.begin():
                await session.execute(delete(Settings).where(Settings.user_id == user_id))
                await session.execute(delete(User).where(User.user_id == user_id))
        user_id_int = int(user_id)
        self._status_cache.pop(user_id_int, None)
        self._settings_cache.pop(user_id_int, None)

    async def user_count(self):
        async with self.SessionLocal() as session:
            result = await session.execute(select(func.count()).select_from(User))
            return result.scalar()

    async def active_user_count(self):
        async with self.SessionLocal() as session:
            result = await session.execute(select(func.count()).select_from(User).where(User.status == "active"))
            return result.scalar()

    async def inactive_user_count(self):
        async with self.SessionLocal() as session:
            result = await session.execute(select(func.count()).select_from(User).where(User.status != "active"))
            return result.scalar()

    async def private_chat_count(self):
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(func.count())
                .select_from(User)
                .where(User.chat_type == "private")
            )
            return result.scalar()

    async def group_chat_count(self):
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(func.count())
                .select_from(User)
                .where(User.chat_type != "private", User.chat_type.isnot(None))
            )
            return result.scalar()


    async def all_users(self):
        async with self.SessionLocal() as session:
            result = await session.execute(select(User.user_id))
            return result.scalars().all()

    async def user_exist(self, user_id):
        async with self.SessionLocal() as session:
            result = await session.execute(select(User).where(User.user_id == user_id))
            return result.scalar() is not None  # True / False

    async def user_update_name(self, user_id, user_name, user_username):
        async with self.SessionLocal() as session:
            async with session.begin():
                await session.execute(update(User).where(User.user_id == user_id)
                                      .values(user_name=user_name, user_username=user_username))

    async def get_user_setting(self, user_id, field):
        settings = await self.user_settings(user_id)
        return settings.get(field)

    async def user_settings(self, user_id):
        user_id_int = int(user_id)
        now = time.monotonic()
        cached = self._settings_cache.get(user_id_int)
        self._prune_local_caches(now)
        if cached and now - cached[0] <= self._settings_ttl_seconds:
            return dict(cached[1])

        try:
            async with self.SessionLocal() as session:
                result = await session.execute(
                    select(Settings)
                    .where(Settings.user_id == user_id_int)
                    .order_by(Settings.id.desc())
                )
                settings_rows = result.scalars().all()
                if len(settings_rows) > 1:
                    logging.warning(
                        "Detected duplicated settings rows; using latest entry: user_id=%s duplicates=%s",
                        user_id_int,
                        len(settings_rows),
                    )
                settings = settings_rows[0] if settings_rows else None
                if settings:
                    payload = {
                        "captions": settings.captions or SETTING_DISABLED,
                        "delete_message": settings.delete_message or SETTING_DISABLED,
                        "info_buttons": settings.info_buttons or SETTING_DISABLED,
                        "url_button": settings.url_button or SETTING_DISABLED,
                        "audio_button": settings.audio_button or SETTING_DISABLED,
                    }
                    self._settings_cache[user_id_int] = (now, payload)
                    return dict(payload)
        except Exception as exc:
            if cached:
                logging.warning(
                    "Failed to fetch user settings, using stale cache: user_id=%s error=%s",
                    user_id_int,
                    exc,
                )
                return dict(cached[1])
            logging.warning(
                "Failed to fetch user settings, using defaults: user_id=%s error=%s",
                user_id_int,
                exc,
            )

        payload = dict(DEFAULT_USER_SETTINGS)
        self._settings_cache[user_id_int] = (now, payload)
        return dict(payload)

    async def set_user_setting(self, user_id, field, value):
        user_id_int = int(user_id)
        if field not in SETTING_FIELDS:
            raise ValueError(f"Unsupported user setting field: {field}")

        normalized_value = normalize_setting_value(value)
        if normalized_value is None or normalized_value not in SETTING_VALUES:
            raise ValueError(f"Unsupported user setting value for {field}: {value}")

        async with self.SessionLocal() as session:
            async with session.begin():
                values = {"user_id": user_id_int, field: normalized_value}
                if self._dialect_name == "postgresql":
                    stmt = (
                        pg_insert(Settings)
                        .values(**values)
                        .on_conflict_do_update(
                            index_elements=[Settings.user_id],
                            set_={field: normalized_value},
                        )
                    )
                    await session.execute(stmt)
                elif self._dialect_name == "sqlite":
                    stmt = (
                        sqlite_insert(Settings)
                        .values(**values)
                        .on_conflict_do_update(
                            index_elements=[Settings.user_id],
                            set_={field: normalized_value},
                        )
                    )
                    await session.execute(stmt)
                else:
                    existing = await session.execute(
                        select(Settings)
                        .where(Settings.user_id == user_id_int)
                        .order_by(Settings.id.desc())
                    )
                    setting_rows = existing.scalars().all()
                    setting = setting_rows[0] if setting_rows else None
                    if setting is None:
                        setting = Settings(user_id=user_id_int)
                        session.add(setting)
                    setattr(setting, field, normalized_value)
                    for duplicate in setting_rows[1:]:
                        await session.delete(duplicate)
        self._settings_cache.pop(user_id_int, None)
        updated = await self.user_settings(user_id_int)
        updated[field] = normalized_value
        self._settings_cache[user_id_int] = (time.monotonic(), updated)

    async def set_inactive(self, user_id):
        async with self.SessionLocal() as session:
            async with session.begin():
                user_id_int = int(user_id)
                await session.execute(update(User).where(User.user_id == user_id_int).values(status="inactive"))
        self._status_cache[int(user_id)] = (time.monotonic(), "inactive")

    async def set_active(self, user_id):
        async with self.SessionLocal() as session:
            async with session.begin():
                user_id_int = int(user_id)
                await session.execute(update(User).where(User.user_id == user_id_int).values(status="active"))
        self._status_cache[int(user_id)] = (time.monotonic(), "active")

    async def status(self, user_id):
        user_id_int = int(user_id)
        now = time.monotonic()
        self._prune_local_caches(now)
        cached = self._status_cache.get(user_id_int)
        if cached and now - cached[0] <= self._status_ttl_seconds:
            return cached[1]

        async with self.SessionLocal() as session:
            result = await session.execute(select(User.status).where(User.user_id == user_id_int))
            value = result.scalar()
            self._status_cache[user_id_int] = (now, value)
            return value

    async def get_user_info(self, user_id):
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(User.user_name, User.user_username, User.status).where(User.user_id == user_id)
            )
            return result.first()

    async def get_user_info_username(self, user_username):
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(User.user_name, User.user_id, User.status).where(User.user_username == user_username)
            )
            return result.first()

    async def get_all_users_info(self):
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(User.user_id, User.chat_type, User.user_name, User.user_username, User.language, User.status)
            )
            return result.all()

    async def ban_user(self, user_id):
        async with self.SessionLocal() as session:
            async with session.begin():
                user_id_int = int(user_id)
                await session.execute(update(User).where(User.user_id == user_id_int).values(status="ban"))
        self._status_cache[int(user_id)] = (time.monotonic(), "ban")

    async def add_file(self, url, file_id, file_type):
        async with self.SessionLocal() as session:
            try:
                if self._dialect_name == "postgresql":
                    stmt = (
                        pg_insert(DownloadedFile)
                        .values(url=url, file_id=file_id, file_type=file_type)
                        .on_conflict_do_update(
                            index_elements=[DownloadedFile.url],
                            set_={
                                "file_id": file_id,
                                "file_type": file_type,
                                "date_added": func.now(),
                            },
                        )
                    )
                elif self._dialect_name == "sqlite":
                    stmt = (
                        sqlite_insert(DownloadedFile)
                        .values(url=url, file_id=file_id, file_type=file_type)
                        .on_conflict_do_update(
                            index_elements=[DownloadedFile.url],
                            set_={
                                "file_id": file_id,
                                "file_type": file_type,
                                "date_added": func.now(),
                            },
                        )
                    )
                else:
                    existing = await session.execute(select(DownloadedFile).where(DownloadedFile.url == url))
                    record = existing.scalar_one_or_none()
                    if record:
                        await session.execute(
                            update(DownloadedFile)
                            .where(DownloadedFile.url == url)
                            .values(
                                file_id=file_id,
                                file_type=file_type,
                                date_added=func.now(),
                            )
                        )
                        stmt = None
                    else:
                        stmt = DownloadedFile.__table__.insert().values(
                            url=url,
                            file_id=file_id,
                            file_type=file_type,
                        )
                if stmt is not None:
                    await session.execute(stmt)
                await session.commit()
                self._file_cache[url] = (time.monotonic(), file_id)
            except Exception as e:
                logging.error("Error in add_file: %s", e)
                await session.rollback()

    async def get_file_id(self, url):
        now = time.monotonic()
        self._prune_local_caches(now)
        cached = self._file_cache.get(url)
        if cached:
            ttl_seconds = self._file_cache_hit_ttl_seconds if cached[1] is not None else self._file_cache_miss_ttl_seconds
            if ttl_seconds is None or now - cached[0] <= ttl_seconds:
                return cached[1]

        async with self.SessionLocal() as session:
            try:
                result = await session.execute(select(DownloadedFile.file_id).where(DownloadedFile.url == url))
                file_id = result.scalar()
                self._file_cache[url] = (now, file_id)
                return file_id
            except Exception as e:
                logging.error("Error in get_file_id: %s", e)
                return None

    @staticmethod
    def _stats_period_start(period: str) -> datetime:
        start_date = datetime.now()
        if period == "Week":
            start_date -= timedelta(weeks=1)
        elif period == "Month":
            start_date -= timedelta(days=30)
        elif period == "Year":
            start_date -= timedelta(days=365)
        return start_date

    @staticmethod
    def _normalize_stats_date(date_val) -> str:
        if isinstance(date_val, str):
            return datetime.strptime(date_val, "%Y-%m-%d").strftime("%Y-%m-%d")
        return date_val.strftime("%Y-%m-%d")

    async def get_download_stats(self, period: str) -> StatsSnapshot:
        async with self.SessionLocal() as session:
            start_date = self._stats_period_start(period)
            result = await session.execute(
                select(func.date(AnalyticsEvent.created_at), AnalyticsEvent.action_name, func.count())
                .where(
                    AnalyticsEvent.created_at >= start_date,
                    AnalyticsEvent.action_name.notin_(NON_DOWNLOAD_ACTIONS),
                )
                .group_by(func.date(AnalyticsEvent.created_at), AnalyticsEvent.action_name)
                .order_by(func.date(AnalyticsEvent.created_at))
            )

            totals_by_date: dict[str, int] = defaultdict(int)
            by_service: dict[str, dict[str, int]] = defaultdict(dict)
            service_totals: dict[str, int] = defaultdict(int)
            total_downloads = 0

            for date_val, action_name, count in result.all():
                normalized = self._normalize_stats_date(date_val)
                service = self._map_action_to_service(action_name)

                totals_by_date[normalized] += count
                by_service[service][normalized] = by_service[service].get(normalized, 0) + count
                service_totals[service] += count
                total_downloads += count

            return StatsSnapshot(
                totals_by_date=dict(totals_by_date),
                by_service={service: dict(values) for service, values in by_service.items()},
                service_totals=dict(service_totals),
                total_downloads=total_downloads,
            )

    async def get_downloaded_files_count(self, period: str):
        snapshot = await self.get_download_stats(period)
        return snapshot.totals_by_date

    @staticmethod
    def _map_action_to_service(action_name: str) -> str:
        lower = (action_name or "").lower()
        if "tiktok" in lower:
            return "TikTok"
        if "instagram" in lower:
            return "Instagram"
        if "youtube" in lower:
            return "YouTube"
        if "soundcloud" in lower:
            return "SoundCloud"
        if "pinterest" in lower:
            return "Pinterest"
        if "twitter" in lower or "x_" in lower:
            return "Twitter"
        return "Other"

    async def get_downloaded_files_by_service(self, period: str) -> dict[str, dict[str, int]]:
        snapshot = await self.get_download_stats(period)
        return snapshot.by_service

