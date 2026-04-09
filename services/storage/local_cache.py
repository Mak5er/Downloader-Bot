import time
from typing import Optional


class LocalCacheMixin:
    def _prune_cache(
        self,
        cache: dict,
        *,
        now: float,
        max_entries: int,
        ttl_getter,
    ) -> None:
        expired_keys = []
        for key, (timestamp, value) in cache.items():
            ttl_seconds = ttl_getter(value)
            if ttl_seconds is not None and now - timestamp > ttl_seconds:
                expired_keys.append(key)

        for key in expired_keys:
            cache.pop(key, None)

        overflow = len(cache) - max_entries
        if overflow <= 0:
            return

        oldest_keys = sorted(cache, key=lambda item_key: cache[item_key][0])[:overflow]
        for key in oldest_keys:
            cache.pop(key, None)

    def _prune_local_caches(self, now: Optional[float] = None, *, force: bool = False) -> None:
        now = time.monotonic() if now is None else now
        if not force and now - self._last_cache_cleanup_monotonic < self._cache_cleanup_interval_seconds:
            return

        self._prune_cache(
            self._settings_cache,
            now=now,
            max_entries=self._settings_cache_max_entries,
            ttl_getter=lambda _value: self._settings_ttl_seconds,
        )
        self._prune_cache(
            self._status_cache,
            now=now,
            max_entries=self._status_cache_max_entries,
            ttl_getter=lambda _value: self._status_ttl_seconds,
        )
        self._prune_cache(
            self._file_cache,
            now=now,
            max_entries=self._file_cache_max_entries,
            ttl_getter=lambda value: self._file_cache_hit_ttl_seconds if value is not None else self._file_cache_miss_ttl_seconds,
        )
        self._last_cache_cleanup_monotonic = now
