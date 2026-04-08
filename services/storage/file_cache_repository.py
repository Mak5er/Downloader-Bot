import time

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from log.logger import logger as logging
from services.storage.models import DownloadedFile

logging = logging.bind(service="db_files")


class FileCacheRepositoryMixin:
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
            except Exception as exc:
                logging.error("Error in add_file: %s", exc)
                await session.rollback()

    async def get_file_id(self, url):
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
