"""Central async dispatcher for bot event handling.

Queues incoming work so transport event loops stay responsive.
Provides per-session serialization with bounded queue + worker pool.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Coroutine, Optional, Any

from config import DISPATCHER_MAX_QUEUE, DISPATCHER_QUEUE_WARN, DISPATCHER_WORKERS

logger = logging.getLogger("tele-claude.dispatcher")


@dataclass
class DispatchItem:
    """A unit of work to run in the dispatcher."""

    name: str
    session_id: Optional[int]
    coro: Callable[[], Coroutine[Any, Any, None]]
    created_at: float = field(default_factory=time.monotonic)
    on_drop: Optional[Callable[[], Coroutine[Any, Any, None]]] = None


class EventDispatcher:
    """Central queue + worker pool with per-session serialization."""

    def __init__(
        self,
        *,
        worker_count: int = DISPATCHER_WORKERS,
        max_queue_size: int = DISPATCHER_MAX_QUEUE,
        warn_queue_size: int = DISPATCHER_QUEUE_WARN,
    ) -> None:
        self._worker_count = max(1, worker_count)
        self._max_queue_size = max(1, max_queue_size)
        self._warn_queue_size = max(1, warn_queue_size)
        self._queue: asyncio.Queue[DispatchItem] = asyncio.Queue(maxsize=self._max_queue_size)
        self._session_locks: dict[int, asyncio.Lock] = {}
        self._workers: list[asyncio.Task] = []
        self._started = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start_if_needed(self) -> None:
        """Start worker tasks if they are not already running."""
        if self._started and self._workers and all(not w.done() for w in self._workers):
            return

        loop = asyncio.get_running_loop()
        self._loop = loop
        self._workers = [
            loop.create_task(self._worker(i), name=f"dispatcher-worker-{i}")
            for i in range(self._worker_count)
        ]
        self._started = True
        logger.info(
            "Dispatcher started (workers=%d, max_queue=%d)",
            self._worker_count,
            self._max_queue_size,
        )

    def enqueue(self, item: DispatchItem) -> bool:
        """Enqueue a dispatch item. Returns False if queue is full."""
        self.start_if_needed()
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("Dispatcher queue full; dropping item name=%s session_id=%s", item.name, item.session_id)
            if item.on_drop:
                self._schedule_drop(item.on_drop)
            return False

        qsize = self._queue.qsize()
        if qsize >= self._warn_queue_size and qsize % self._warn_queue_size == 0:
            logger.warning("Dispatcher queue depth warning: %d items", qsize)
        return True

    def _schedule_drop(self, on_drop: Callable[[], Coroutine[Any, Any, None]]) -> None:
        loop = self._loop or asyncio.get_running_loop()
        loop.create_task(on_drop())

    async def _worker(self, worker_id: int) -> None:
        while True:
            item = await self._queue.get()
            lock: Optional[asyncio.Lock] = None
            if item.session_id is not None:
                lock = self._session_locks.setdefault(item.session_id, asyncio.Lock())

            try:
                if lock:
                    async with lock:
                        await self._run_item(item)
                else:
                    await self._run_item(item)
            finally:
                self._queue.task_done()

    async def _run_item(self, item: DispatchItem) -> None:
        start = time.monotonic()
        try:
            await item.coro()
        except Exception:
            logger.exception("Dispatcher item failed name=%s session_id=%s", item.name, item.session_id)
        finally:
            elapsed = time.monotonic() - start
            if elapsed >= 5.0:
                logger.warning(
                    "Dispatcher item slow name=%s session_id=%s elapsed=%.2fs",
                    item.name,
                    item.session_id,
                    elapsed,
                )


dispatcher = EventDispatcher()
