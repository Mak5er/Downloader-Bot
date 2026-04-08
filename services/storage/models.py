from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import (
    BigInteger,
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
}
APP_SCHEMA_TABLES = frozenset(
    {
        "downloaded_files",
        "users",
        "analytics_events",
        "settings",
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
