from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from services.logger import logger as logging
from services.storage.models import DownloadHistory

logging = logging.bind(service="db_download_history")


class DownloadHistoryRepositoryMixin:
    async def record_download(
        self,
        *,
        user_id: int,
        chat_id: Optional[int] = None,
        chat_type: Optional[str] = None,
        service: str,
        url: str,
        title: Optional[str] = None,
        file_type: Optional[str] = None,
        file_id: Optional[str] = None,
        file_size_bytes: Optional[int] = None,
        duration_seconds: Optional[float] = None,
        status: str = "success",
        error_message: Optional[str] = None,
    ) -> DownloadHistory:
        async with self.SessionLocal() as session:
            try:
                record = DownloadHistory(
                    user_id=user_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    service=service,
                    url=url,
                    title=title,
                    file_type=file_type,
                    file_id=file_id,
                    file_size_bytes=file_size_bytes,
                    duration_seconds=duration_seconds,
                    status=status,
                    error_message=error_message,
                )
                session.add(record)
                await session.commit()
                await session.refresh(record)
                return record
            except Exception as exc:
                logging.error("Failed to record download history: %s", exc)
                await session.rollback()
                raise

    async def get_download_history(
        self,
        *,
        page: int = 1,
        per_page: int = 15,
        user_id: Optional[int] = None,
        service: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Sequence[DownloadHistory]:
        page = max(1, int(page))
        per_page = max(1, min(int(per_page), 100))
        offset = (page - 1) * per_page

        async with self.SessionLocal() as session:
            try:
                stmt = select(DownloadHistory).options(selectinload(DownloadHistory.user))

                if user_id is not None:
                    stmt = stmt.where(DownloadHistory.user_id == user_id)
                if service is not None:
                    stmt = stmt.where(DownloadHistory.service == service)
                if status is not None:
                    stmt = stmt.where(DownloadHistory.status == status)

                stmt = stmt.order_by(DownloadHistory.created_at.desc(), DownloadHistory.id.desc())
                stmt = stmt.offset(offset).limit(per_page)

                result = await session.execute(stmt)
                return result.scalars().all()
            except Exception as exc:
                logging.error("Failed to get download history: %s", exc)
                return []

    async def get_download_history_count(
        self,
        *,
        user_id: Optional[int] = None,
        service: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        async with self.SessionLocal() as session:
            try:
                stmt = select(func.count(DownloadHistory.id))

                if user_id is not None:
                    stmt = stmt.where(DownloadHistory.user_id == user_id)
                if service is not None:
                    stmt = stmt.where(DownloadHistory.service == service)
                if status is not None:
                    stmt = stmt.where(DownloadHistory.status == status)

                result = await session.execute(stmt)
                return result.scalar() or 0
            except Exception as exc:
                logging.error("Failed to count download history: %s", exc)
                return 0

    async def cleanup_expired_history(self, max_age_days: int = 90) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(int(max_age_days), 1))
        batch_size = 1000
        total_deleted = 0

        while True:
            async with self.SessionLocal() as session:
                async with session.begin():
                    expired_ids_res = await session.execute(
                        select(DownloadHistory.id)
                        .where(DownloadHistory.created_at < cutoff)
                        .order_by(DownloadHistory.created_at, DownloadHistory.id)
                        .limit(batch_size)
                    )
                    ids = expired_ids_res.scalars().all()
                    if not ids:
                        break

                    result = await session.execute(
                        delete(DownloadHistory).where(DownloadHistory.id.in_(ids))
                    )
                    deleted = int(result.rowcount or 0)
                    total_deleted += deleted

            if len(ids) < batch_size:
                break

        if total_deleted > 0:
            logging.info(
                "cleanup_expired_history: deleted %d entries older than %d days",
                total_deleted,
                max_age_days,
            )
        return total_deleted
