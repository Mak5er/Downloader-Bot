from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Index,
    Text,
    TIMESTAMP,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import declarative_base, relationship

from services.settings import SETTING_DISABLED

Base = declarative_base()
NON_DOWNLOAD_ACTIONS = ("start", "settings")
DEFAULT_USER_SETTINGS = {
    "captions": SETTING_DISABLED,
    "delete_message": SETTING_DISABLED,
    "info_buttons": SETTING_DISABLED,
    "url_button": SETTING_DISABLED,
    "audio_button": SETTING_DISABLED,
    "file_button": SETTING_DISABLED,
    "video_quality": "best",
    "as_document": SETTING_DISABLED,
    "audio_format": "mp3",
}
APP_SCHEMA_TABLES = frozenset(
    {
        "downloaded_files",
        "users",
        "analytics_events",
        "settings",
        "groups",
        "group_members",
    }
)


@dataclass(slots=True)
class StatsSnapshot:
    totals_by_date: dict[str, int] = field(default_factory=dict)
    by_service: dict[str, dict[str, int]] = field(default_factory=dict)
    service_totals: dict[str, int] = field(default_factory=dict)
    total_downloads: int = 0


class DownloadedFile(Base):
    __tablename__ = "downloaded_files"
    __table_args__ = (
        Index("ix_downloaded_files_date_added", "date_added"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    url = Column(Text, unique=True, nullable=False)
    file_id = Column(Text, nullable=False)
    date_added = Column(TIMESTAMP(timezone=True), server_default=func.now())
    file_type = Column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_user_username", "user_username"),
        Index("ix_users_chat_type", "chat_type"),
        Index("ix_users_status", "status"),
        Index("ix_users_has_dm", "has_dm"),
    )

    user_id = Column(BigInteger, primary_key=True, autoincrement=False)
    user_name = Column(Text, nullable=True)
    user_username = Column(Text, nullable=True)
    chat_type = Column(Text, nullable=True, default="private")
    language = Column(Text, nullable=True)
    has_dm = Column(Boolean, nullable=False, default=False, server_default=sa.text("false"))
    status = Column(Text, nullable=True, default="active")
    referred_by = Column(BigInteger, nullable=True)
    source = Column(Text, nullable=True)

    settings = relationship(
        "Settings",
        back_populates="user",
        uselist=False,
        primaryjoin="User.user_id == foreign(Settings.user_id)",
    )


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (
        Index("ix_groups_status", "status"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    title = Column(Text, nullable=True)
    username = Column(Text, nullable=True)
    chat_type = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="active", server_default=sa.text("'active'"))
    member_count = Column(BigInteger, nullable=False, default=0, server_default=sa.text("0"))
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (
        Index("ix_group_members_user_id", "user_id"),
        Index("ix_group_members_group_id", "group_id"),
    )

    group_id = Column(BigInteger, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    last_seen_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class AnalyticsEvent(Base):
    __tablename__ = "analytics_events"
    __table_args__ = (
        Index("ix_analytics_events_action_name_created_at", "action_name", "created_at"),
        Index("ix_analytics_events_created_action", "created_at", "action_name"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    chat_type = Column(Text, nullable=True)
    action_name = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Settings(Base):
    __tablename__ = "settings"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_settings_user_id"),
        Index("ix_settings_user_id", "user_id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=True)
    captions = Column(Text, default=SETTING_DISABLED, nullable=False)
    delete_message = Column(Text, default=SETTING_DISABLED, nullable=False)
    info_buttons = Column(Text, default=SETTING_DISABLED, nullable=False)
    url_button = Column(Text, default=SETTING_DISABLED, nullable=False)
    audio_button = Column(Text, default=SETTING_DISABLED, nullable=False)
    file_button = Column(Text, default=SETTING_DISABLED, nullable=False)
    video_quality = Column(Text, default="best", nullable=False)
    as_document = Column(Text, default=SETTING_DISABLED, nullable=False)
    audio_format = Column(Text, default="mp3", nullable=False)

    user = relationship(
        "User",
        back_populates="settings",
        primaryjoin="User.user_id == foreign(Settings.user_id)",
    )
