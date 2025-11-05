import asyncio
from typing import Optional

import aiohttp

_session: Optional[aiohttp.ClientSession] = None
_lock = asyncio.Lock()

_CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5, sock_read=20)


async def get_http_session() -> aiohttp.ClientSession:
    """Return a lazily initialised shared aiohttp session."""
    global _session
    if _session and not _session.closed:
        return _session

    async with _lock:
        if _session is None or _session.closed:
            connector = aiohttp.TCPConnector(limit=32)
            _session = aiohttp.ClientSession(timeout=_CLIENT_TIMEOUT, connector=connector)
    return _session


async def close_http_session() -> None:
    """Close the shared session if it was created."""
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None
