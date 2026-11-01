"""Asynchronous periodic task scheduler.

Each task runs in isolation: an exception never stops the others. After consecutive failures
a backoff is applied (up to `max_backoff`). The interval can be dynamic (a callable), which allows
e.g. polling matches every 10 s while matches are live and every 60 s otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("cs2.scheduler")


@dataclass
class Task:
    name: str
    func: Callable[[], Awaitable[None]]
    interval: float | Callable[[], float]
    initial_delay: float = 0.0
    jitter: float = 0.1
    max_backoff: float = 900.0
    enabled: bool = True
    runs: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_run: float | None = None
    last_duration: float | None = None
    last_error: str | None = None
    next_run: float = 0.0
    running: bool = False
    _handle: asyncio.Task | None = field(default=None, repr=False)

    def current_interval(self) -> float:
        return float(self.interval() if callable(self.interval) else self.interval)

    def as_dict(self) -> dict:
        return {"name": self.name, "interval": round(self.current_interval(), 1), "runs": self.runs,
                "failures": self.failures, "last_run": self.last_run, "last_duration": self.last_duration,
                "last_error": self.last_error, "next_run": self.next_run, "enabled": self.enabled}


class Scheduler:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self._stopping = asyncio.Event()

    def add(self, name: str, func: Callable[[], Awaitable[None]], interval: float | Callable[[], float],
            initial_delay: float = 0.0, enabled: bool = True, jitter: float = 0.1) -> Task:
        t = Task(name=name, func=func, interval=interval, initial_delay=initial_delay, enabled=enabled, jitter=jitter)
        self.tasks[name] = t
        return t

    async def _loop(self, t: Task) -> None:
        await self._sleep(t.initial_delay)
        while not self._stopping.is_set():
            if t.enabled:
                t.running = True
                t0 = time.monotonic()
                try:
                    await t.func()
                    t.consecutive_failures = 0
                    t.last_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # failure isolation
                    t.failures += 1
                    t.consecutive_failures += 1
                    t.last_error = f"{type(e).__name__}: {e}"[:300]
                    log.warning("Task %s failed (%d in a row): %s", t.name, t.consecutive_failures, t.last_error,
                                exc_info=t.consecutive_failures == 1)
                finally:
                    t.running = False
                    t.runs += 1
                    t.last_run = time.time()
                    t.last_duration = round(time.monotonic() - t0, 3)
            interval = t.current_interval()
            if t.consecutive_failures:
                interval = min(t.max_backoff, interval * (2 ** min(t.consecutive_failures, 6)))
            interval *= 1 + random.uniform(-t.jitter, t.jitter)
            t.next_run = time.time() + interval
            await self._sleep(interval)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass

    def start(self) -> None:
        for t in self.tasks.values():
            if t._handle is None:
                t._handle = asyncio.create_task(self._loop(t), name=f"task:{t.name}")

    async def run_now(self, name: str) -> None:
        await self.tasks[name].func()

    async def stop(self) -> None:
        self._stopping.set()
        handles = [t._handle for t in self.tasks.values() if t._handle]
        for h in handles:
            h.cancel()
        await asyncio.gather(*handles, return_exceptions=True)
