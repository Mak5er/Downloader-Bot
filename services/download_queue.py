from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Awaitable, Callable, Optional, TypeVar

from log.logger import logger as logging

T = TypeVar("T")


class QueueRateLimitError(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = max(0.0, retry_after)
        super().__init__(f"Rate limit exceeded. Retry after {self.retry_after:.1f}s.")


class QueueBackpressureError(Exception):
    def __init__(self, position: int):
        self.position = max(1, int(position))
        super().__init__(f"Queue is full. Position: {self.position}.")


@dataclass(slots=True)
class QueueTicket:
    position: int
    queue_size: int
    active_workers: int


@dataclass(slots=True)
class QueueMetricSnapshot:
    count: int
    processing_p50_ms: float
    processing_p95_ms: float
    queue_wait_p50_ms: float
    queue_wait_p95_ms: float


@dataclass(order=True)
class _QueuedJob:
    priority: int
    order: int
    created_at: float = field(compare=False)
    source: str = field(compare=False)
    user_id: Optional[int] = field(compare=False)
    runner: Optional[Callable[[], Awaitable[Any]]] = field(compare=False, default=None)
    future: Optional[asyncio.Future] = field(compare=False, default=None)
    stop_worker: bool = field(compare=False, default=False)


class AdaptiveDownloadQueue:
    """
    Shared priority queue for heavy download jobs.

    Features:
    - Prioritised scheduling (lower number = higher priority)
    - Per-user rate limiting and pending-job cap
    - Queue position feedback
    - p50/p95 runtime + queue wait metrics per source
    - Adaptive worker scaling based on real queue pressure
    """

    def __init__(
        self,
        *,
        min_workers: int = 2,
        max_workers: int = 8,
        max_queue_size: int = 300,
        per_user_rate_limit: int = 5,
        per_user_window_seconds: float = 10.0,
        per_user_max_pending: int = 2,
        metric_window: int = 300,
    ) -> None:
        if min_workers < 1:
            raise ValueError("min_workers must be >= 1")
        if max_workers < min_workers:
            raise ValueError("max_workers must be >= min_workers")

        self.min_workers = int(min_workers)
        self.max_workers = int(max_workers)
        self.max_queue_size = int(max_queue_size)
        self.per_user_rate_limit = int(per_user_rate_limit)
        self.per_user_window_seconds = float(per_user_window_seconds)
        self.per_user_max_pending = int(per_user_max_pending)

        self._queue: asyncio.PriorityQueue[_QueuedJob] = asyncio.PriorityQueue()
        self._sequence = count(1)
        self._worker_sequence = count(1)
        self._workers: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._started = False

        self._user_recent: dict[int, deque[float]] = defaultdict(deque)
        self._user_pending: dict[int, int] = defaultdict(int)

        self._processing_samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=metric_window))
        self._queue_wait_samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=metric_window))
        self._last_scale_action = 0.0
        self._scale_cooldown_seconds = 8.0
        self._idle_scale_down_seconds = 40.0
        self._last_non_empty_queue = time.monotonic()
        self._completed_jobs = 0

    @property
    def active_workers(self) -> int:
        return len(self._workers)

    async def submit(
        self,
        runner: Callable[[], Awaitable[T]],
        *,
        priority: int,
        source: str,
        user_id: Optional[int] = None,
        on_queued: Optional[Callable[[QueueTicket], Awaitable[None] | None]] = None,
    ) -> T:
        await self._ensure_started()

        if self._queue.qsize() >= self.max_queue_size:
            raise QueueBackpressureError(position=self._queue.qsize() + 1)

        if user_id is not None:
            self._enforce_rate_limit(user_id)
            pending = self._user_pending[user_id]
            if pending >= self.per_user_max_pending:
                raise QueueBackpressureError(position=pending + 1)
            self._user_pending[user_id] += 1

        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        job = _QueuedJob(
            priority=int(priority),
            order=next(self._sequence),
            created_at=time.monotonic(),
            source=source or "generic",
            user_id=user_id,
            runner=runner,
            future=future,
        )

        self._queue.put_nowait(job)
        self._last_non_empty_queue = time.monotonic()
        await self._maybe_autotune()

        if on_queued:
            ticket = QueueTicket(
                position=self._queue.qsize(),
                queue_size=self._queue.qsize(),
                active_workers=self.active_workers,
            )
            maybe = on_queued(ticket)
            if asyncio.iscoroutine(maybe):
                await maybe

        return await future

    async def metrics_snapshot(self) -> dict[str, QueueMetricSnapshot]:
        snapshot: dict[str, QueueMetricSnapshot] = {}
        for source in set(self._processing_samples.keys()) | set(self._queue_wait_samples.keys()):
            processing = list(self._processing_samples[source])
            waiting = list(self._queue_wait_samples[source])
            count = max(len(processing), len(waiting))
            snapshot[source] = QueueMetricSnapshot(
                count=count,
                processing_p50_ms=self._percentile(processing, 0.50) * 1000.0,
                processing_p95_ms=self._percentile(processing, 0.95) * 1000.0,
                queue_wait_p50_ms=self._percentile(waiting, 0.50) * 1000.0,
                queue_wait_p95_ms=self._percentile(waiting, 0.95) * 1000.0,
            )
        return snapshot

    async def shutdown(self) -> None:
        async with self._lock:
            worker_count = len(self._workers)
            for _ in range(worker_count):
                self._queue.put_nowait(
                    _QueuedJob(
                        priority=10**9,
                        order=next(self._sequence),
                        created_at=time.monotonic(),
                        source="system",
                        user_id=None,
                        stop_worker=True,
                    )
                )

        if self._workers:
            await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        self._started = False

    async def _ensure_started(self) -> None:
        if self._started:
            return

        async with self._lock:
            if self._started:
                return
            for _ in range(self.min_workers):
                self._spawn_worker_locked()
            self._started = True
            logging.info(
                "Download queue started: workers=%s max_workers=%s queue_cap=%s",
                self.min_workers,
                self.max_workers,
                self.max_queue_size,
            )

    def _spawn_worker_locked(self) -> None:
        worker_id = next(self._worker_sequence)
        task = asyncio.create_task(self._worker_loop(worker_id), name=f"download-queue-worker-{worker_id}")
        self._workers[worker_id] = task

        def _cleanup(_task: asyncio.Task, wid: int = worker_id) -> None:
            self._workers.pop(wid, None)

        task.add_done_callback(_cleanup)

    async def _worker_loop(self, worker_id: int) -> None:
        while True:
            job = await self._queue.get()
            if job.stop_worker:
                self._queue.task_done()
                return

            started = time.monotonic()
            queue_wait = max(0.0, started - job.created_at)

            try:
                assert job.runner is not None
                result = await job.runner()
            except Exception as exc:
                if job.future and not job.future.done():
                    job.future.set_exception(exc)
            else:
                if job.future and not job.future.done():
                    job.future.set_result(result)
            finally:
                self._queue.task_done()
                if job.user_id is not None:
                    self._user_pending[job.user_id] = max(0, self._user_pending[job.user_id] - 1)
                    if self._user_pending[job.user_id] == 0:
                        self._user_pending.pop(job.user_id, None)

                processing = max(0.0, time.monotonic() - started)
                self._record_metric(job.source, queue_wait, processing)
                await self._maybe_autotune()

    def _record_metric(self, source: str, queue_wait: float, processing: float) -> None:
        source_key = source or "generic"
        self._queue_wait_samples[source_key].append(queue_wait)
        self._processing_samples[source_key].append(processing)
        self._completed_jobs += 1

        if self._completed_jobs % 25 == 0:
            snap = self._build_global_snapshot()
            logging.info(
                (
                    "Queue metrics: jobs=%s workers=%s depth=%s "
                    "queue_wait_p50=%.0fms queue_wait_p95=%.0fms "
                    "processing_p50=%.0fms processing_p95=%.0fms"
                ),
                self._completed_jobs,
                self.active_workers,
                self._queue.qsize(),
                snap.queue_wait_p50_ms,
                snap.queue_wait_p95_ms,
                snap.processing_p50_ms,
                snap.processing_p95_ms,
            )

    async def _maybe_autotune(self) -> None:
        now = time.monotonic()
        if now - self._last_scale_action < self._scale_cooldown_seconds:
            return

        async with self._lock:
            now = time.monotonic()
            if now - self._last_scale_action < self._scale_cooldown_seconds:
                return

            current_workers = len(self._workers)
            if current_workers <= 0:
                self._spawn_worker_locked()
                self._last_scale_action = now
                return

            queue_depth = self._queue.qsize()
            snap = self._build_global_snapshot()
            wait_p95 = snap.queue_wait_p95_ms / 1000.0

            scale_up = (
                current_workers < self.max_workers
                and (queue_depth > current_workers * 2 or wait_p95 > 2.0)
            )
            if scale_up:
                self._spawn_worker_locked()
                self._last_scale_action = now
                logging.info(
                    "Queue auto-tune scale up: workers=%s depth=%s wait_p95=%.2fs",
                    len(self._workers),
                    queue_depth,
                    wait_p95,
                )
                return

            idle_for = now - self._last_non_empty_queue
            scale_down = (
                current_workers > self.min_workers
                and queue_depth == 0
                and wait_p95 < 0.25
                and idle_for > self._idle_scale_down_seconds
            )
            if scale_down:
                self._queue.put_nowait(
                    _QueuedJob(
                        priority=10**9,
                        order=next(self._sequence),
                        created_at=time.monotonic(),
                        source="system",
                        user_id=None,
                        stop_worker=True,
                    )
                )
                self._last_scale_action = now
                logging.info(
                    "Queue auto-tune scale down requested: workers=%s",
                    max(self.min_workers, current_workers - 1),
                )

    def _enforce_rate_limit(self, user_id: int) -> None:
        now = time.monotonic()
        bucket = self._user_recent[user_id]
        while bucket and now - bucket[0] > self.per_user_window_seconds:
            bucket.popleft()

        if len(bucket) >= self.per_user_rate_limit:
            retry_after = self.per_user_window_seconds - (now - bucket[0])
            raise QueueRateLimitError(retry_after=retry_after)

        bucket.append(now)

    def _build_global_snapshot(self) -> QueueMetricSnapshot:
        all_processing: list[float] = []
        all_waiting: list[float] = []
        for values in self._processing_samples.values():
            all_processing.extend(values)
        for values in self._queue_wait_samples.values():
            all_waiting.extend(values)

        count = max(len(all_processing), len(all_waiting))
        return QueueMetricSnapshot(
            count=count,
            processing_p50_ms=self._percentile(all_processing, 0.50) * 1000.0,
            processing_p95_ms=self._percentile(all_processing, 0.95) * 1000.0,
            queue_wait_p50_ms=self._percentile(all_waiting, 0.50) * 1000.0,
            queue_wait_p95_ms=self._percentile(all_waiting, 0.95) * 1000.0,
        )

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]

        ordered = sorted(values)
        idx = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
        return ordered[idx]


_download_queue: Optional[AdaptiveDownloadQueue] = None


def get_download_queue() -> AdaptiveDownloadQueue:
    global _download_queue
    if _download_queue is None:
        _download_queue = AdaptiveDownloadQueue()
    return _download_queue


async def shutdown_download_queue() -> None:
    global _download_queue
    if _download_queue is not None:
        await _download_queue.shutdown()
    _download_queue = None
