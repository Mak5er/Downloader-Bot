import time
from typing import Any

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from services.logger import logger as logging
from services.settings import SETTING_DISABLED, SETTING_FIELDS, SETTING_VALUES, normalize_setting_value
from services.storage.models import (
    DEFAULT_USER_SETTINGS,
    Group,
    GroupMember,
    Settings,
    User,
)

logging = logging.bind(service="db_users")


class UserRepositoryMixin:
    def _insert(self, model):
        if self._dialect_name == "postgresql":
            return pg_insert(model)
        return sqlite_insert(model)

    async def upsert_chat(
        self,
        user_id: int,
        user_name: str | None,
        user_username: str | None,
        chat_type: str | None,
        language: str | None = None,
        status: str = "active",
        referred_by: int | None = None,
        source: str | None = None,
    ) -> None:
        chat_id_int = int(user_id)
        if chat_id_int < 0:
            await self.upsert_group(
                chat_id=chat_id_int,
                title=user_name,
                username=user_username,
                chat_type=chat_type or "group",
                status=status,
            )
            return

        has_dm = (chat_type is None or chat_type == "private")
        await self.upsert_user(
            user_id=chat_id_int,
            user_name=user_name,
            user_username=user_username,
            chat_type=chat_type or "private",
            language=language,
            has_dm=has_dm,
            status=status,
            referred_by=referred_by,
            source=source,
        )

    async def upsert_user(
        self,
        user_id: int,
        user_name: str | None = None,
        user_username: str | None = None,
        chat_type: str | None = "private",
        language: str | None = None,
        has_dm: bool = False,
        status: str | None = None,
        referred_by: int | None = None,
        source: str | None = None,
    ) -> None:
        user_id_int = int(user_id)
        initial_status = status or "active"
        values = {
            "user_id": user_id_int,
            "user_name": user_name,
            "user_username": user_username,
            "chat_type": chat_type or "private",
            "language": language,
            "has_dm": has_dm,
            "status": initial_status,
        }
        if referred_by is not None:
            values["referred_by"] = referred_by
        if source is not None:
            values["source"] = source

        async with self.SessionLocal() as session:
            async with session.begin():
                stmt = self._insert(User).values(**values)
                set_dict = {
                    "user_name": stmt.excluded.user_name,
                    "user_username": stmt.excluded.user_username,
                    "chat_type": stmt.excluded.chat_type,
                    "has_dm": User.has_dm | stmt.excluded.has_dm,
                    "status": case(
                        (User.status == "ban", "ban"),
                        (stmt.excluded.has_dm, "active"),
                        else_=(stmt.excluded.status if status is not None else User.status),
                    ),
                    "referred_by": case((User.referred_by.is_(None), stmt.excluded.referred_by), else_=User.referred_by),
                    "source": case((User.source.is_(None), stmt.excluded.source), else_=User.source),
                }
                if language is not None:
                    set_dict["language"] = stmt.excluded.language

                stmt = stmt.on_conflict_do_update(
                    index_elements=[User.user_id],
                    set_=set_dict,
                )
                stmt = stmt.returning(User.status)
                res = await session.execute(stmt)
                updated_status = res.scalar_one_or_none()

        if updated_status is not None:
            self._status_cache[user_id_int] = (time.monotonic(), updated_status)
        elif status is not None:
            self._status_cache[user_id_int] = (time.monotonic(), status)
        else:
            self._status_cache.pop(user_id_int, None)

    async def upsert_group(
        self,
        chat_id: int,
        title: str | None = None,
        username: str | None = None,
        chat_type: str | None = None,
        status: str = "active",
        member_count: int | None = None,
    ) -> None:
        chat_id_int = int(chat_id)
        values = {
            "id": chat_id_int,
            "title": title,
            "username": username,
            "chat_type": chat_type or "group",
            "status": status,
            "member_count": member_count if member_count is not None else 0,
        }
        async with self.SessionLocal() as session:
            async with session.begin():
                stmt = self._insert(Group).values(**values)
                set_dict = {
                    "title": stmt.excluded.title,
                    "username": stmt.excluded.username,
                    "chat_type": stmt.excluded.chat_type,
                    "status": case(
                        (Group.status == "ban", "ban"),
                        else_=stmt.excluded.status,
                    ),
                    "updated_at": func.now(),
                }
                if member_count is not None:
                    set_dict["member_count"] = stmt.excluded.member_count
                else:
                    set_dict["member_count"] = Group.member_count

                stmt = stmt.on_conflict_do_update(
                    index_elements=[Group.id],
                    set_=set_dict,
                )
                stmt = stmt.returning(Group.status)
                res = await session.execute(stmt)
                updated_status = res.scalar_one_or_none()

        if updated_status is not None:
            self._status_cache[chat_id_int] = (time.monotonic(), updated_status)
        else:
            self._status_cache.pop(chat_id_int, None)

    async def record_group_member(self, group_id: int, user_id: int) -> None:
        group_id_int = int(group_id)
        user_id_int = int(user_id)
        async with self.SessionLocal() as session:
            async with session.begin():
                stmt = self._insert(GroupMember).values(
                    group_id=group_id_int,
                    user_id=user_id_int,
                    last_seen_at=func.now(),
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[GroupMember.group_id, GroupMember.user_id],
                    set_={"last_seen_at": func.now()},
                )
                await session.execute(stmt)

    async def update_group_status(self, group_id: int, status: str) -> None:
        group_id_int = int(group_id)
        async with self.SessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(Group)
                    .where(Group.id == group_id_int)
                    .values(status=status, updated_at=func.now())
                )
        self._status_cache[group_id_int] = (time.monotonic(), status)

    set_group_status = update_group_status

    async def update_group_member_count(self, group_id: int, member_count: int) -> None:
        group_id_int = int(group_id)
        async with self.SessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(Group)
                    .where(Group.id == group_id_int)
                    .values(member_count=member_count, updated_at=func.now())
                )

    async def migrate_group_chat(
        self,
        old_group_id: int,
        new_group_id: int,
        *,
        new_title: str | None = None,
        new_username: str | None = None,
    ) -> None:
        old_id = int(old_group_id)
        new_id = int(new_group_id)
        if old_id == new_id:
            return

        async with self.SessionLocal() as session:
            async with session.begin():
                old_group_res = await session.execute(select(Group).where(Group.id == old_id))
                old_group = old_group_res.scalar_one_or_none()

                new_group_res = await session.execute(select(Group).where(Group.id == new_id))
                new_group = new_group_res.scalar_one_or_none()

                title = new_title or (new_group.title if new_group else None) or (old_group.title if old_group else None)
                username = new_username or (new_group.username if new_group else None) or (old_group.username if old_group else None)
                status = (new_group.status if new_group else None) or (old_group.status if old_group else "active")
                member_count = (new_group.member_count if new_group and new_group.member_count else None) or (old_group.member_count if old_group else 0)

                stmt = self._insert(Group).values(
                    id=new_id,
                    title=title,
                    username=username,
                    chat_type="supergroup",
                    status=status,
                    member_count=member_count,
                    created_at=old_group.created_at if old_group else func.now(),
                    updated_at=func.now(),
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[Group.id],
                    set_={
                        "title": func.coalesce(stmt.excluded.title, Group.title),
                        "username": func.coalesce(stmt.excluded.username, Group.username),
                        "chat_type": "supergroup",
                        "status": stmt.excluded.status,
                        "member_count": case(
                            (stmt.excluded.member_count > 0, stmt.excluded.member_count),
                            else_=Group.member_count,
                        ),
                        "updated_at": func.now(),
                    },
                )
                await session.execute(stmt)

                # Migrate group members
                old_members_res = await session.execute(
                    select(GroupMember).where(GroupMember.group_id == old_id)
                )
                old_members = old_members_res.scalars().all()
                for member in old_members:
                    m_stmt = self._insert(GroupMember).values(
                        group_id=new_id,
                        user_id=member.user_id,
                        last_seen_at=member.last_seen_at or func.now(),
                    )
                    m_stmt = m_stmt.on_conflict_do_update(
                        index_elements=[GroupMember.group_id, GroupMember.user_id],
                        set_={
                            "last_seen_at": case(
                                (m_stmt.excluded.last_seen_at > GroupMember.last_seen_at, m_stmt.excluded.last_seen_at),
                                else_=GroupMember.last_seen_at,
                            )
                        },
                    )
                    await session.execute(m_stmt)

                await session.execute(delete(GroupMember).where(GroupMember.group_id == old_id))

                # Migrate settings
                new_settings_res = await session.execute(
                    select(Settings).where(Settings.user_id == new_id)
                )
                new_settings = new_settings_res.scalar_one_or_none()
                if new_settings:
                    await session.execute(delete(Settings).where(Settings.user_id == old_id))
                else:
                    await session.execute(
                        update(Settings).where(Settings.user_id == old_id).values(user_id=new_id)
                    )

                # Delete old group record
                await session.execute(delete(Group).where(Group.id == old_id))

        self._status_cache.pop(old_id, None)

    async def get_active_groups(self) -> list[Group]:
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(Group).where(Group.status == "active").order_by(Group.id.asc())
            )
            return list(result.scalars().all())

    async def get_users_for_reachability_check(self) -> list[User]:
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(User).where(
                    User.has_dm.is_(True),
                    func.coalesce(User.status, "active") != "ban",
                ).order_by(User.user_id.asc())
            )
            return list(result.scalars().all())

    async def get_community_stats(self) -> dict[str, int]:
        async with self.SessionLocal() as session:
            user_stats = (
                await session.execute(
                    select(
                        func.count(User.user_id).label("user_count"),
                        func.count(User.user_id).filter(User.status == "active").label("active_user_count"),
                        func.count(User.user_id).filter(User.status != "active").label("inactive_user_count"),
                        func.count(User.user_id).filter(User.has_dm.is_(True)).label("dm_total"),
                        func.count(User.user_id).filter(User.has_dm.is_(True), User.status == "active").label("dm_active"),
                        func.count(User.user_id).filter(User.has_dm.is_(True), User.status == "inactive").label("dm_inactive"),
                        func.count(User.user_id).filter(User.has_dm.is_(True), User.status == "ban").label("dm_banned"),
                    )
                )
            ).one()

            group_stats = (
                await session.execute(
                    select(
                        func.count(Group.id).label("groups_total"),
                        func.count(Group.id).filter(Group.status == "active").label("groups_active"),
                        func.count(Group.id).filter(Group.status != "active").label("groups_inactive"),
                        func.coalesce(func.sum(Group.member_count).filter(Group.status == "active"), 0).label("total_reach"),
                    )
                )
            ).one()

            tracked_members = (
                await session.execute(
                    select(func.count(func.distinct(GroupMember.user_id)))
                )
            ).scalar() or 0

            return {
                "dm_total": int(user_stats.dm_total),
                "dm_active": int(user_stats.dm_active),
                "dm_inactive": int(user_stats.dm_inactive),
                "dm_banned": int(user_stats.dm_banned),
                "groups_total": int(group_stats.groups_total),
                "groups_active": int(group_stats.groups_active),
                "groups_inactive": int(group_stats.groups_inactive),
                "total_reach": int(group_stats.total_reach),
                "tracked_group_members": int(tracked_members),
                # Legacy compatibility keys
                "user_count": int(user_stats.user_count),
                "active_user_count": int(user_stats.active_user_count),
                "inactive_user_count": int(user_stats.inactive_user_count),
                "private_chat_count": int(user_stats.dm_total),
                "group_chat_count": int(group_stats.groups_total),
            }

    async def get_user_counts(self) -> dict[str, int]:
        return await self.get_community_stats()

    async def delete_user(self, user_id: int) -> None:
        user_id_int = int(user_id)
        async with self.SessionLocal() as session:
            async with session.begin():
                await session.execute(delete(Settings).where(Settings.user_id == user_id_int))
                if user_id_int < 0:
                    await session.execute(delete(Group).where(Group.id == user_id_int))
                else:
                    await session.execute(delete(User).where(User.user_id == user_id_int))
        self._status_cache.pop(user_id_int, None)
        self._settings_cache.pop(user_id_int, None)

    async def get_user_setting(self, user_id: int, field: str) -> str | None:
        settings = await self.user_settings(user_id)
        return settings.get(field)

    async def user_settings(self, user_id: int) -> dict[str, str]:
        user_id_int = int(user_id)
        now = time.monotonic()
        cached = self._settings_cache.get(user_id_int)
        self._prune_local_caches(now)
        if cached and now - cached[0] <= self._settings_ttl_seconds:
            return dict(cached[1])

        try:
            async with self.SessionLocal() as session:
                result = await session.execute(
                    select(Settings).where(Settings.user_id == user_id_int).limit(1)
                )
                settings = result.scalar_one_or_none()
                if settings:
                    payload = {
                        "captions": settings.captions or SETTING_DISABLED,
                        "delete_message": settings.delete_message or SETTING_DISABLED,
                        "info_buttons": settings.info_buttons or SETTING_DISABLED,
                        "url_button": settings.url_button or SETTING_DISABLED,
                        "audio_button": settings.audio_button or SETTING_DISABLED,
                        "file_button": getattr(settings, "file_button", None) or SETTING_DISABLED,
                        "video_quality": getattr(settings, "video_quality", None) or "best",
                        "as_document": getattr(settings, "as_document", None) or SETTING_DISABLED,
                        "audio_format": getattr(settings, "audio_format", None) or "mp3",
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

    async def set_user_setting(self, user_id: int, field: str, value: str) -> None:
        user_id_int = int(user_id)
        if field not in SETTING_FIELDS:
            raise ValueError(f"Unsupported user setting field: {field}")

        normalized_value = normalize_setting_value(value)
        if normalized_value is None or normalized_value not in SETTING_VALUES:
            raise ValueError(f"Unsupported user setting value for {field}: {value}")

        async with self.SessionLocal() as session:
            async with session.begin():
                values = {"user_id": user_id_int, field: normalized_value}
                stmt = (
                    self._insert(Settings)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[Settings.user_id],
                        set_={field: normalized_value},
                    )
                )
                await session.execute(stmt)
        self._settings_cache.pop(user_id_int, None)
        updated = await self.user_settings(user_id_int)
        updated[field] = normalized_value
        self._settings_cache[user_id_int] = (time.monotonic(), updated)

    async def set_inactive(self, user_id: int) -> None:
        user_id_int = int(user_id)
        async with self.SessionLocal() as session:
            async with session.begin():
                if user_id_int < 0:
                    await session.execute(update(Group).where(Group.id == user_id_int).values(status="inactive", updated_at=func.now()))
                else:
                    await session.execute(update(User).where(User.user_id == user_id_int).values(status="inactive"))
        self._status_cache[user_id_int] = (time.monotonic(), "inactive")

    async def set_active(self, user_id: int) -> None:
        user_id_int = int(user_id)
        async with self.SessionLocal() as session:
            async with session.begin():
                if user_id_int < 0:
                    await session.execute(update(Group).where(Group.id == user_id_int).values(status="active", updated_at=func.now()))
                else:
                    await session.execute(update(User).where(User.user_id == user_id_int).values(status="active"))
        self._status_cache[user_id_int] = (time.monotonic(), "active")

    async def status(self, user_id: int) -> str | None:
        user_id_int = int(user_id)
        now = time.monotonic()
        self._prune_local_caches(now)
        cached = self._status_cache.get(user_id_int)
        if cached and now - cached[0] <= self._status_ttl_seconds:
            return cached[1]

        async with self.SessionLocal() as session:
            if user_id_int < 0:
                result = await session.execute(select(Group.status).where(Group.id == user_id_int))
            else:
                result = await session.execute(select(User.status).where(User.user_id == user_id_int))
            value = result.scalar()
            self._status_cache[user_id_int] = (now, value)
            return value

    async def get_user_info(self, user_id: int) -> Any:
        user_id_int = int(user_id)
        async with self.SessionLocal() as session:
            if user_id_int < 0:
                result = await session.execute(
                    select(Group.title.label("user_name"), Group.username.label("user_username"), Group.status).where(Group.id == user_id_int)
                )
            else:
                result = await session.execute(
                    select(User.user_name, User.user_username, User.status).where(User.user_id == user_id_int)
                )
            return result.first()

    async def get_all_users_info(self) -> list[Any]:
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(
                    User.user_id,
                    User.chat_type,
                    User.user_name,
                    User.user_username,
                    User.language,
                    User.status,
                    User.has_dm,
                )
            )
            return result.all()

    async def ban_user(self, user_id: int) -> None:
        user_id_int = int(user_id)
        async with self.SessionLocal() as session:
            async with session.begin():
                if user_id_int < 0:
                    await session.execute(update(Group).where(Group.id == user_id_int).values(status="ban", updated_at=func.now()))
                else:
                    await session.execute(update(User).where(User.user_id == user_id_int).values(status="ban"))
        self._status_cache[user_id_int] = (time.monotonic(), "ban")
