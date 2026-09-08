import asyncio
import time


class AsyncTokenBucket:
    """
    Token bucket rate limiter for asyncio operations.
    Allows up to `rate` tokens per second with `capacity` burst limit.
    Default: 25 tokens/second (leaving 5 msg/s headroom for standard Telegram Bot API 30 msg/s limit).
    """

    def __init__(self, rate: float = 25.0, capacity: float = 25.0):
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                if now < self._last_update:
                    wait_time = self._last_update - now
                else:
                    elapsed = now - self._last_update
                    self._last_update = now
                    self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

                    if self._tokens >= tokens:
                        self._tokens -= tokens
                        return

                    missing = tokens - self._tokens
                    wait_time = missing / self.rate

            await asyncio.sleep(wait_time)

    def penalize(self, seconds: float) -> None:
        """Pause acquisitions and drain tokens for `seconds` due to upstream backoff."""
        now = time.monotonic()
        self._tokens = 0.0
        self._last_update = max(self._last_update, now) + max(0.0, float(seconds))

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


# Global singleton instance for outgoing broadcasts and bulk checks
broadcast_limiter = AsyncTokenBucket(rate=25.0, capacity=25.0)
