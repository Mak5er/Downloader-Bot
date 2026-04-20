import re


ALEMBIC_DATABASE_URL_PLACEHOLDER = "${DATABASE_URL}"


def to_sync_database_url(database_url: str) -> str:
    return re.sub(r"^postgresql\+asyncpg:", "postgresql:", database_url)


def to_async_database_url(database_url: str) -> str:
    sync_url = to_sync_database_url(database_url)
    return re.sub(r"^postgresql:", "postgresql+asyncpg:", sync_url)


def resolve_alembic_database_url(
    configured_url: str | None,
    env_database_url: str | None,
) -> str | None:
    candidate = configured_url
    if not candidate or candidate == ALEMBIC_DATABASE_URL_PLACEHOLDER:
        candidate = env_database_url
    if not candidate:
        return None
    return to_sync_database_url(candidate)
