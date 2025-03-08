from datetime import datetime, timedelta
from sqlalchemy import Column, BigInteger, Text, TIMESTAMP, func, select, delete, update
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
import config

Base = declarative_base()


class DownloadedFile(Base):
    __tablename__ = "downloaded_files"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    url = Column(Text, unique=True, nullable=False)
    file_id = Column(Text, nullable=False)
    date_added = Column(TIMESTAMP(timezone=True), default=func.now())
    file_type = Column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"

    user_id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_name = Column(Text, nullable=True)
    user_username = Column(Text, nullable=True)
    chat_type = Column(Text, nullable=True)
    language = Column(Text, nullable=True)
    status = Column(Text, nullable=True)
    captions = Column(Text, default="off", nullable=False)


class DataBase:
    def __init__(self):
        self.engine = create_async_engine(config.db_auth, echo=False)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=AsyncSession, expire_on_commit=False)

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

    async def update_captions(self, user_id, captions):
        async with self.SessionLocal() as session:
            async with session.begin():
                await session.execute(update(User).where(User.user_id == user_id).values(captions=captions))

    async def get_user_captions(self, user_id):
        async with self.SessionLocal() as session:
            result = await session.execute(select(User.captions).where(User.user_id == user_id))
            return result.scalar()

    async def set_inactive(self, user_id):
        async with self.SessionLocal() as session:
            async with session.begin():
                # Ensure user_id is treated as an integer
                user_id_int = int(user_id)
                await session.execute(update(User).where(User.user_id == user_id_int).values(status="inactive"))

    async def set_active(self, user_id):
        async with self.SessionLocal() as session:
            async with session.begin():
                # Ensure user_id is treated as an integer
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
                print(f"Error in add_file: {e}")
                await session.rollback()

    async def get_file_id(self, url):
        async with self.SessionLocal() as session:
            try:
                result = await session.execute(select(DownloadedFile.file_id).where(DownloadedFile.url == url))
                file_id = result.scalar()
                return file_id
            except Exception as e:
                print(f"Error in get_file_id: {e}")
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
                select(func.date(DownloadedFile.date_added), func.count())
                .where(DownloadedFile.date_added >= start_date)
                .group_by(func.date(DownloadedFile.date_added))
                .order_by(func.date(DownloadedFile.date_added))
            )

            return {row[0].strftime("%Y-%m-%d"): row[1] for row in result.all()}
