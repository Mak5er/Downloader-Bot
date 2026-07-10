from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

_WHITESPACE_RE = re.compile(r"\s+")
_ARTIST_SEPARATOR_RE = re.compile(r"\s*[,;|·]\s*")


def _clean_artist_name(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return _WHITESPACE_RE.sub(" ", normalized).strip(" ,;|·")


def _artist_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _deduplicate_repeated_delimited_names(value: str) -> str:
    parts = [_clean_artist_name(part) for part in _ARTIST_SEPARATOR_RE.split(value)]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return value

    unique: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = _artist_key(part)
        if key in seen:
            continue
        seen.add(key)
        unique.append(part)

    # Preserve legitimate names containing commas when no repetition exists,
    # e.g. "Tyler, The Creator" or "Earth, Wind & Fire".
    if len(unique) == len(parts):
        return value
    return ", ".join(unique)


def _iter_artist_values(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        yield value.get("name")
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_artist_values(item)
        return
    yield value


def normalize_artist_names(value: Any) -> str | None:
    """Normalize and case-insensitively deduplicate provider artist metadata."""
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in _iter_artist_values(value):
        name = _clean_artist_name(raw_name)
        if not name:
            continue
        name = _deduplicate_repeated_delimited_names(name)
        key = _artist_key(name)
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return ", ".join(names) or None
