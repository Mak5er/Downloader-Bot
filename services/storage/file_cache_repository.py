import time

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from services.logger import logger as logging
from services.storage.models import DownloadedFile

logging = logging.bind(service="db_files")


class FileCacheRepositoryMixin:
    async def add_file(self, url: str, file_id: str, file_type: str | None) -> None:
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
            except Exception as exc:
                logging.error("Error in add_file: %s", exc)
                await session.rollback()

    async def get_file_id(self, url: str) -> str | None:
        now = time.monotonic()
        self._prune_local_caches(now)
        cached = self._file_cache.get(url)
        if cached:
            ttl_seconds = (
                self._file_cache_hit_ttl_seconds
                if cached[1] is not None
                else self._file_cache_miss_ttl_seconds
            )
            if ttl_seconds is None or now - cached[0] <= ttl_seconds:
                return cached[1]

        async with self.SessionLocal() as session:
            try:
                result = await session.execute(select(DownloadedFile.file_id).where(DownloadedFile.url == url))
                file_id = result.scalar()
                self._file_cache[url] = (now, file_id)
                return file_id
            except Exception as exc:
                logging.error("Error in get_file_id: %s", exc)
                return None

    async def cleanup_expired_files(self, max_age_days: int = 30) -> int:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=max(int(max_age_days), 1))
        total_deleted = 0
        batch_size = 1000

        while True:
            async with self.SessionLocal() as session:
                async with session.begin():
                    result = await session.execute(
                        delete(DownloadedFile)
                        .where(DownloadedFile.date_added < cutoff)
                        .limit(batch_size)
                    )
                    deleted = result.rowcount
                    total_deleted += deleted

            if deleted < batch_size:
                break

        if total_deleted > 0:
            self._file_cache.clear()
            logging.info("cleanup_expired_files: deleted %d entries older than %d days", total_deleted, max_age_days)

        return total_deleted
