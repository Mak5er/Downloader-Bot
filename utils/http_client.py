import asyncio
from typing import Optional

import aiohttp

_session: Optional[aiohttp.ClientSession] = None
_session_loop: Optional[asyncio.AbstractEventLoop] = None
_lock = asyncio.Lock()

# Slightly higher limits to speed up bursty handler traffic while keeping sockets bounded
_CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5, sock_read=20)
_CONNECTOR_LIMIT = 128


async def get_http_session() -> aiohttp.ClientSession:
    """Return a lazily initialised shared aiohttp session."""
    global _session, _session_loop
    loop = asyncio.get_running_loop()
    if _session and not _session.closed and _session_loop is loop:
        return _session

    async with _lock:
        if _session and not _session.closed and _session_loop is loop:
            return _session

        previous_session = _session
        connector = aiohttp.TCPConnector(
            limit=_CONNECTOR_LIMIT,
            limit_per_host=32,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        _session = aiohttp.ClientSession(timeout=_CLIENT_TIMEOUT, connector=connector)
        _session_loop = loop
        if previous_session and not previous_session.closed:
            await previous_session.close()
    return _session


async def close_http_session() -> None:
    """Close the shared session if it was created."""
    global _session, _session_loop
    async with _lock:
        session = _session
        _session = None
        _session_loop = None
    if session and not session.closed:
        await session.close()
