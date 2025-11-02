import logging
from datetime import datetime, timedelta

from sqlalchemy import Text, TIMESTAMP, func, select, delete, update, create_engine, Integer, ForeignKey
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base, Mapped, mapped_column, relationship

Base = declarative_base()


class DownloadedFile(Base):
    __tablename__ = "downloaded_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    file_id: Mapped[str] = mapped_column(Text, nullable=False)
    date_added: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    file_type: Mapped[str] = mapped_column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_name: Mapped[str] = mapped_column(Text, nullable=True)
    user_username: Mapped[str] = mapped_column(Text, nullable=True)
    chat_type: Mapped[str] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=True)

    settings: Mapped["Settings"] = relationship("Settings", back_populates="user", uselist=False)


class AnalyticsEvent(Base):
    __tablename__ = "analytics_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_type: Mapped[str] = mapped_column(Text, nullable=True)
    action_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Settings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    captions: Mapped[str] = mapped_column(Text, default="off", nullable=False)
    delete_message: Mapped[str] = mapped_column(Text, default="off", nullable=False)
    info_buttons: Mapped[str] = mapped_column(Text, default="off", nullable=False)
    url_button: Mapped[str] = mapped_column(Text, default="off", nullable=False)

    user = relationship("User", back_populates="settings")


async def run_alembic_migration():
    import os
    import subprocess

    versions_dir = os.path.join(os.path.dirname(__file__), "alembic", "versions")
    os.makedirs(versions_dir, exist_ok=True)

    subprocess.run([
        "alembic", "revision", "--autogenerate", "-m", "auto update"
    ], cwd=os.path.dirname(__file__), stdout=subprocess.DEVNULL)
    subprocess.run(["alembic", "upgrade", "head"], cwd=os.path.dirname(__file__))


class DataBase:
    def __init__(self, sqlite_path="maxload.db"):
        # SQLite engine для бота
        self.sqlite_path = sqlite_path
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.sqlite_path}", echo=False)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=AsyncSession, expire_on_commit=False)

    async def init_db(self):
        sync_engine = create_engine(f"sqlite:///{self.sqlite_path}")
        Base.metadata.create_all(sync_engine)
        logging.info("✅ SQLite таблиці створені / підтверджено існування")

        await run_alembic_migration()

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
                }
            return {
                "captions": "off",
                "delete_message": "off",
                "info_buttons": "off",
                "url_button": "off",
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
                async with session.begin():
                    file = DownloadedFile(url=url, file_id=file_id, file_type=file_type)
                    session.add(file)
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

            return {datetime.strptime(row[0], "%Y-%m-%d").strftime("%Y-%m-%d"): row[1] for row in result.all()}
