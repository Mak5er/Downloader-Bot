import asyncio

import pytest

from services.download_queue import AdaptiveDownloadQueue, QueueBackpressureError, QueueRateLimitError


@pytest.mark.asyncio
async def test_queue_respects_priority_for_waiting_jobs():
    queue = AdaptiveDownloadQueue(min_workers=1, max_workers=1, per_user_rate_limit=20, max_queue_size=50)
    order: list[str] = []

    async def slow_first():
        await asyncio.sleep(0.05)
        order.append("first")
        return "first"

    async def low_priority():
        order.append("low")
        return "low"

    async def high_priority():
        order.append("high")
        return "high"

    task_first = asyncio.create_task(
        queue.submit(slow_first, priority=20, source="test", user_id=1)
    )
    await asyncio.sleep(0.01)
    task_low = asyncio.create_task(
        queue.submit(low_priority, priority=60, source="test", user_id=2)
    )
    task_high = asyncio.create_task(
        queue.submit(high_priority, priority=10, source="test", user_id=3)
    )

    await asyncio.gather(task_first, task_low, task_high)
    await queue.shutdown()

    assert order == ["first", "high", "low"]


@pytest.mark.asyncio
async def test_queue_rate_limit_per_user():
    queue = AdaptiveDownloadQueue(
        min_workers=1,
        max_workers=1,
        per_user_rate_limit=2,
        per_user_window_seconds=30.0,
        per_user_max_pending=5,
    )

    blocker = asyncio.Event()

    async def hold():
        await blocker.wait()
        return "ok"

    first = asyncio.create_task(queue.submit(hold, priority=20, source="test", user_id=99))
    second = asyncio.create_task(queue.submit(hold, priority=20, source="test", user_id=99))
    await asyncio.sleep(0.01)

    with pytest.raises(QueueRateLimitError):
        await queue.submit(hold, priority=20, source="test", user_id=99)

    blocker.set()
    await asyncio.gather(first, second)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_queue_pending_cap_per_user():
    queue = AdaptiveDownloadQueue(
        min_workers=1,
        max_workers=1,
        per_user_rate_limit=20,
        per_user_max_pending=1,
    )
    blocker = asyncio.Event()

    async def hold():
        await blocker.wait()
        return "ok"

    first = asyncio.create_task(queue.submit(hold, priority=20, source="test", user_id=5))
    await asyncio.sleep(0.01)

    with pytest.raises(QueueBackpressureError):
        await queue.submit(hold, priority=20, source="test", user_id=5)

    blocker.set()
    await first
    await queue.shutdown()


@pytest.mark.asyncio
async def test_queue_metrics_snapshot_contains_percentiles():
    queue = AdaptiveDownloadQueue(min_workers=1, max_workers=2, per_user_rate_limit=20)

    async def run(delay: float):
        await asyncio.sleep(delay)
        return delay

    await asyncio.gather(
        queue.submit(lambda: run(0.01), priority=30, source="tiktok", user_id=1),
        queue.submit(lambda: run(0.02), priority=30, source="tiktok", user_id=2),
        queue.submit(lambda: run(0.015), priority=30, source="youtube", user_id=3),
    )

    snapshot = await queue.metrics_snapshot()
    await queue.shutdown()

    assert "tiktok" in snapshot
    assert snapshot["tiktok"].count >= 1
    assert snapshot["tiktok"].processing_p95_ms >= snapshot["tiktok"].processing_p50_ms
