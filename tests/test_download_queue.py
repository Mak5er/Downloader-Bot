import asyncio

import pytest

from services.download.queue import AdaptiveDownloadQueue, QueueBackpressureError, QueueRateLimitError


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
async def test_queue_pending_cap_waits_for_slot():
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

    second = asyncio.create_task(queue.submit(hold, priority=20, source="test", user_id=5))
    await asyncio.sleep(0.02)
    assert not second.done()

    blocker.set()
    await asyncio.gather(first, second)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_queue_pending_cap_timeout_raises_backpressure():
    queue = AdaptiveDownloadQueue(
        min_workers=1,
        max_workers=1,
        per_user_rate_limit=20,
        per_user_max_pending=1,
        per_user_pending_timeout_seconds=0.02,
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
async def test_queue_pending_cap_isolated_per_chat():
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

    first = asyncio.create_task(queue.submit(hold, priority=20, source="test", user_id=5, chat_id=-1001))
    await asyncio.sleep(0.01)

    second_other_chat = asyncio.create_task(queue.submit(hold, priority=20, source="test", user_id=5, chat_id=-1002))
    await asyncio.sleep(0.02)
    assert not second_other_chat.done()

    third_same_first_chat = asyncio.create_task(queue.submit(hold, priority=20, source="test", user_id=5, chat_id=-1001))
    await asyncio.sleep(0.02)
    assert not third_same_first_chat.done()

    blocker.set()
    await asyncio.gather(first, second_other_chat, third_same_first_chat)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_queue_request_id_counts_as_single_user_submission():
    queue = AdaptiveDownloadQueue(
        min_workers=1,
        max_workers=1,
        per_user_rate_limit=1,
        per_user_window_seconds=30.0,
        per_user_max_pending=1,
    )
    blocker = asyncio.Event()

    async def hold():
        await blocker.wait()
        return "ok"

    first = asyncio.create_task(
        queue.submit(hold, priority=20, source="test", user_id=55, request_id="req-1")
    )
    await asyncio.sleep(0.01)
    second = asyncio.create_task(
        queue.submit(hold, priority=20, source="test", user_id=55, request_id="req-1")
    )
    await asyncio.sleep(0.02)
    assert not second.done()

    with pytest.raises(QueueRateLimitError):
        await queue.submit(hold, priority=20, source="test", user_id=55, request_id="req-2")

    blocker.set()
    await asyncio.gather(first, second)
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


@pytest.mark.asyncio
async def test_queue_submit_during_shutdown_does_not_deadlock():
    queue = AdaptiveDownloadQueue(min_workers=2, max_workers=2, per_user_rate_limit=20, max_queue_size=50)

    async def _runner():
        await asyncio.sleep(0.05)
        return "ok"

    task_a = asyncio.create_task(queue.submit(_runner, priority=10, source="test", user_id=1))
    await asyncio.sleep(0.01)
    shutdown_task = asyncio.create_task(queue.shutdown())
    task_b = asyncio.create_task(queue.submit(_runner, priority=10, source="test", user_id=2))

    results = await asyncio.gather(task_a, shutdown_task, task_b, return_exceptions=True)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_queue_per_user_slot_race():
    queue = AdaptiveDownloadQueue(min_workers=1, max_workers=1, per_user_rate_limit=20, per_user_max_pending=1, max_queue_size=50)

    async def _slow():
        await asyncio.sleep(0.1)
        return "slow"

    async def _fast():
        return "fast"

    task_a = asyncio.create_task(queue.submit(_slow, priority=10, source="test", user_id=1))
    await asyncio.sleep(0.02)
    task_b = asyncio.create_task(queue.submit(_fast, priority=10, source="test", user_id=1))

    results = await asyncio.gather(task_a, task_b, return_exceptions=True)
    await queue.shutdown()

    assert results[0] == "slow"
    assert results[1] == "fast"


@pytest.mark.asyncio
async def test_request_dedupe_concurrent_same_key():
    from services.runtime.request_dedupe import claim_request, finish_request, reset_request_tracking

    reset_request_tracking()

    async def _claim(user_id: int):
        return claim_request(user_id, None, "test_svc", "https://example.com/post/1")

    results = await asyncio.gather(
        _claim(1),
        _claim(1),
        _claim(1),
    )

    accepted = sum(1 for r in results if r == "accepted")
    active = sum(1 for r in results if r == "active")

    assert accepted == 1, f"Expected 1 accepted, got {accepted} (active={active}). Race condition detected."
    assert active == 2, f"Expected 2 active, got {active}"

    finish_request(1, None, "test_svc", "https://example.com/post/1", success=True)
    reset_request_tracking()
