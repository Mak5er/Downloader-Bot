from __future__ import annotations


def build_media_cache_key(
    base_key: str,
    *,
    variant: str | None = None,
    item_index: int | None = None,
    item_kind: str | None = None,
) -> str:
    key = (base_key or "").strip()
    if not key:
        raise ValueError("base_key must not be empty")

    if item_index is not None:
        kind = (item_kind or "media").strip() or "media"
        key = f"{key}#item:{int(item_index)}:{kind}"

    if variant:
        key = f"{key}#{variant.strip()}"

    return key
