import time
import pytest

from services.runtime.rate_limiter import AsyncTokenBucket


@pytest.mark.asyncio
async def test_token_bucket_burst_and_rate():
    bucket = AsyncTokenBucket(rate=10.0, capacity=5.0)

    # Initial 5 tokens should be consumed instantly
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    duration = time.monotonic() - start
    assert duration < 0.05, f"Burst took too long: {duration}"

    # Sixth token requires waiting ~0.1s
    start = time.monotonic()
    async with bucket:
        pass
    duration = time.monotonic() - start
    assert 0.05 <= duration <= 0.25, f"Wait time expected ~0.1s, got {duration}"


@pytest.mark.asyncio
async def test_token_bucket_penalize():
    bucket = AsyncTokenBucket(rate=20.0, capacity=10.0)

    # Calling penalize with 0.1s backoff
    bucket.penalize(0.1)

    start = time.monotonic()
    await bucket.acquire()
    duration = time.monotonic() - start
    assert duration >= 0.08, f"Penalty backoff was ignored: {duration}"
