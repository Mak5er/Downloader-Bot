import time

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from services.logger import logger as logging
from services.settings import SETTING_DISABLED, SETTING_FIELDS, SETTING_VALUES, normalize_setting_value
from services.storage.models import DEFAULT_USER_SETTINGS, Settings, User

logging = logging.bind(service="db_users")


class UserRepositoryMixin:
    async def add_user(self, user_id, user_name, user_username, chat_type, language, status):
        async with self.SessionLocal() as session:
            async with session.begin():
                user = User(
                    user_id=user_id,
                    user_name=user_name,
                    user_username=user_username,
                    chat_type=chat_type,
                    language=language,
                    status=status,
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
                        .on_conflict_do_update(index_elements=[User.user_id], set_=values)
                    )
                    await session.execute(stmt)
                elif self._dialect_name == "sqlite":
                    stmt = (
                        sqlite_insert(User)
                        .values(user_id=user_id, **values)
                        .on_conflict_do_update(index_elements=[User.user_id], set_=values)
                    )
                    await session.execute(stmt)
                else:
                    existing = await session.execute(select(User).where(User.user_id == user_id))
                    record = existing.scalar_one_or_none()
                    if record:
                        await session.execute(update(User).where(User.user_id == user_id).values(**values))
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
            result = await session.execute(select(User.user_id))
            return len(result.scalars().all())

    async def active_user_count(self):
        async with self.SessionLocal() as session:
            result = await session.execute(select(User.user_id).where(User.status == "active"))
            return len(result.scalars().all())

    async def inactive_user_count(self):
        async with self.SessionLocal() as session:
            result = await session.execute(select(User.user_id).where(User.status != "active"))
            return len(result.scalars().all())

    async def private_chat_count(self):
        async with self.SessionLocal() as session:
            result = await session.execute(select(User.user_id).where(User.chat_type == "private"))
            return len(result.scalars().all())

    async def group_chat_count(self):
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(User.user_id).where(User.chat_type != "private", User.chat_type.isnot(None))
            )
            return len(result.scalars().all())

    async def all_users(self):
        async with self.SessionLocal() as session:
            result = await session.execute(select(User.user_id))
            return result.scalars().all()

    async def user_exist(self, user_id):
        async with self.SessionLocal() as session:
            result = await session.execute(select(User).where(User.user_id == user_id))
            return result.scalar() is not None

    async def user_update_name(self, user_id, user_name, user_username):
        async with self.SessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(User)
                    .where(User.user_id == user_id)
                    .values(user_name=user_name, user_username=user_username)
                )

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
                    select(Settings).where(Settings.user_id == user_id_int).order_by(Settings.id.desc())
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
                        select(Settings).where(Settings.user_id == user_id_int).order_by(Settings.id.desc())
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
