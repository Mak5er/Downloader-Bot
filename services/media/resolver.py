import asyncio
from typing import Any, Awaitable, Callable, Sequence, TypeVar

from services.logger import logger as logging
from utils.download_manager import log_download_metrics

logging = logging.bind(service="media_resolver")

T = TypeVar("T")


async def resolve_cached_media_items(
    items: Sequence[T],
    *,
    db_service: Any,
    kind_getter: Callable[[T], str],
    build_cache_key: Callable[[int, T, str], str],
    download_item: Callable[[int, T, str], Awaitable[Any | None]],
    metrics_label: str,
    error_label: str,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    media_items: list[dict[str, Any]] = []
    downloaded_paths: list[str] = []

    async def _resolve_item(index: int, item: T) -> dict[str, Any] | None:
        media_kind = str(kind_getter(item))
        cache_key = build_cache_key(index, item, media_kind)
        cached_file_id = await db_service.get_file_id(cache_key)
        if cached_file_id:
            return {
                "index": index,
                "type": media_kind,
                "cache_key": cache_key,
                "file_id": cached_file_id,
                "path": None,
                "cached": True,
            }

        metrics = await download_item(index, item, media_kind)
        if not metrics:
            return None

        log_download_metrics(metrics_label, metrics)
        return {
            "index": index,
            "type": media_kind,
            "cache_key": cache_key,
            "file_id": None,
            "path": metrics.path,
            "cached": False,
        }

    items_to_resolve = items if limit is None else items[:limit]

    tasks = [
        asyncio.create_task(_resolve_item(index, item))
        for index, item in enumerate(items_to_resolve)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in sorted(
        (entry for entry in results if not isinstance(entry, Exception) and entry is not None),
        key=lambda value: int(value["index"]),
    ):
        if result["path"]:
            downloaded_paths.append(str(result["path"]))
        media_items.append(result)

    for result in results:
        if isinstance(result, Exception):
            logging.error("%s media download task failed: error=%s", error_label, result)

    return media_items, downloaded_paths
