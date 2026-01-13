from datetime import datetime, timedelta
from collections import defaultdict
import re

from sqlalchemy import Column, Text, TIMESTAMP, func, select, delete, update, ForeignKey, BigInteger, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship

from config import DATABASE_URL
from log.logger import logger as logging

Base = declarative_base()


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

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    chat_type = Column(Text, nullable=True)
    action_name = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Settings(Base):
    __tablename__ = "settings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"))
    captions = Column(Text, default="off", nullable=False)
    delete_message = Column(Text, default="off", nullable=False)
    info_buttons = Column(Text, default="off", nullable=False)
    url_button = Column(Text, default="off", nullable=False)
    audio_button = Column(Text, default="off", nullable=False)

    user = relationship("User", back_populates="settings")


async def run_alembic_migration():
    import os
    import subprocess
    import shutil

    versions_dir = os.path.join(os.path.dirname(__file__), "alembic", "versions")
    os.makedirs(versions_dir, exist_ok=True)

    alembic_exe = shutil.which("alembic")
    if not alembic_exe:
        logging.warning("Alembic executable not found. Skipping migrations.")
        return

    subprocess.run([
        alembic_exe, "revision", "--autogenerate", "-m", "auto update"
    ], cwd=os.path.dirname(__file__), stdout=subprocess.DEVNULL)
    subprocess.run([alembic_exe, "upgrade", "head"], cwd=os.path.dirname(__file__))


class DataBase:
    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or DATABASE_URL
        if not self.database_url:
            raise ValueError("DATABASE_URL is not set. Configure PostgreSQL connection string.")

        # Follow Neon example: swap driver to asyncpg directly
        self.async_url = re.sub(r"^postgresql:", "postgresql+asyncpg:", self.database_url)

        self.engine = create_async_engine(
            self.async_url,
            echo=False,
            future=True,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        self.SessionLocal = async_sessionmaker(bind=self.engine, expire_on_commit=False, class_=AsyncSession)

    async def init_db(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # If using PostgreSQL, sync sequences to current max IDs to avoid PK collisions after migrations
            if self.async_url.startswith("postgresql+asyncpg"):
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
        logging.info("Database tables checked.")

    async def get_session(self):
        async with self.SessionLocal() as session:
            yield session  # Потрібно для використання у FastAPI Depends, якщо треба

    async def add_user(self, user_id, user_name, user_username, chat_type, language, status):
        async with self.SessionLocal() as session:
            async with session.begin():
                user = User(
                    user_id=user_id, user_name=user_name, user_username=user_username,
                    chat_type=chat_type, language=language, status=status
                )
                session.add(user)

    async def upsert_chat(self, user_id, user_name, user_username, chat_type, language=None, status="active"):
        async with self.SessionLocal() as session:
            async with session.begin():
                existing = await session.execute(select(User).where(User.user_id == user_id))
                record = existing.scalar_one_or_none()
                values = {
                    "user_name": user_name,
                    "user_username": user_username,
                    "chat_type": chat_type,
                    "language": language,
                    "status": status,
                }

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

    async def delete_user(self, user_id):
        async with self.SessionLocal() as session:
            async with session.begin():
                await session.execute(delete(User).where(User.user_id == user_id))

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
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(getattr(Settings, field)).where(Settings.user_id == user_id)
            )
            return result.scalar()

    async def user_settings(self, user_id):
        async with self.SessionLocal() as session:
            result = await session.execute(select(Settings).where(Settings.user_id == user_id))
            settings = result.scalar_one_or_none()
            if settings:
                return {
                    "captions": settings.captions or "off",
                    "delete_message": settings.delete_message or "off",
                    "info_buttons": settings.info_buttons or "off",
                    "url_button": settings.url_button or "off",
                    "audio_button": settings.audio_button or "off",
                }
            return {
                "captions": "off",
                "delete_message": "off",
                "info_buttons": "off",
                "url_button": "off",
                "audio_button": "off",
            }

    async def set_user_setting(self, user_id, field, value):
        async with self.SessionLocal() as session:
            existing = await session.execute(select(Settings).where(Settings.user_id == user_id))
            setting = existing.scalar_one_or_none()
            if not setting:
                setting = Settings(user_id=user_id)
                session.add(setting)
            setattr(setting, field, value)
            await session.commit()

    async def set_inactive(self, user_id):
        async with self.SessionLocal() as session:
            async with session.begin():
                user_id_int = int(user_id)
                await session.execute(update(User).where(User.user_id == user_id_int).values(status="inactive"))

    async def set_active(self, user_id):
        async with self.SessionLocal() as session:
            async with session.begin():
                user_id_int = int(user_id)
                await session.execute(update(User).where(User.user_id == user_id_int).values(status="active"))

    async def status(self, user_id):
        async with self.SessionLocal() as session:
            result = await session.execute(select(User.status).where(User.user_id == user_id))
            return result.scalar()

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

    async def add_file(self, url, file_id, file_type):
        async with self.SessionLocal() as session:
            try:
                stmt = (
                    insert(DownloadedFile)
                    .values(url=url, file_id=file_id, file_type=file_type)
                    .on_conflict_do_nothing(index_elements=[DownloadedFile.url])
                )
                await session.execute(stmt)
                await session.commit()
            except Exception as e:
                logging.error(f"Error in add_file: {e}")
                await session.rollback()

    async def get_file_id(self, url):
        async with self.SessionLocal() as session:
            try:
                result = await session.execute(select(DownloadedFile.file_id).where(DownloadedFile.url == url))
                file_id = result.scalar()
                return file_id
            except Exception as e:
                logging.error(f"Error in get_file_id: {e}")
                return None

    async def get_downloaded_files_count(self, period: str):
        async with self.SessionLocal() as session:
            start_date = datetime.now()
            if period == "Week":
                start_date -= timedelta(weeks=1)
            elif period == "Month":
                start_date -= timedelta(days=30)
            elif period == "Year":
                start_date -= timedelta(days=365)

            result = await session.execute(
                select(func.date(AnalyticsEvent.created_at), func.count())
                .where(
                    AnalyticsEvent.created_at >= start_date,
                    AnalyticsEvent.action_name.notin_(["start", "settings"])
                )
                .group_by(func.date(AnalyticsEvent.created_at))
                .order_by(func.date(AnalyticsEvent.created_at))
            )

            counts: dict[str, int] = {}
            for row in result.all():
                date_val = row[0]
                if isinstance(date_val, str):
                    normalized = datetime.strptime(date_val, "%Y-%m-%d").strftime("%Y-%m-%d")
                else:
                    normalized = date_val.strftime("%Y-%m-%d")
                counts[normalized] = row[1]

            return counts

    @staticmethod
    def _map_action_to_service(action_name: str) -> str:
        lower = (action_name or "").lower()
        if "tiktok" in lower:
            return "TikTok"
        if "instagram" in lower:
            return "Instagram"
        if "youtube" in lower:
            return "YouTube"
        if "twitter" in lower or "x_" in lower:
            return "Twitter"
        return "Other"

    async def get_downloaded_files_by_service(self, period: str) -> dict[str, dict[str, int]]:
        async with self.SessionLocal() as session:
            start_date = datetime.now()
            if period == "Week":
                start_date -= timedelta(weeks=1)
            elif period == "Month":
                start_date -= timedelta(days=30)
            elif period == "Year":
                start_date -= timedelta(days=365)

            result = await session.execute(
                select(func.date(AnalyticsEvent.created_at), AnalyticsEvent.action_name, func.count())
                .where(
                    AnalyticsEvent.created_at >= start_date,
                    AnalyticsEvent.action_name.notin_(["start", "settings"])
                )
                .group_by(func.date(AnalyticsEvent.created_at), AnalyticsEvent.action_name)
                .order_by(func.date(AnalyticsEvent.created_at))
            )

            by_service: dict[str, dict[str, int]] = defaultdict(dict)
            for date_val, action_name, count in result.all():
                if isinstance(date_val, str):
                    normalized = datetime.strptime(date_val, "%Y-%m-%d").strftime("%Y-%m-%d")
                else:
                    normalized = date_val.strftime("%Y-%m-%d")

                service = self._map_action_to_service(action_name)
                by_service[service][normalized] = by_service[service].get(normalized, 0) + count

            return dict(by_service)
