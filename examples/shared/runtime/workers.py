"""Infrastructure supervision with heartbeat recovery and expected lease standby."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time


class LeaseBusy(RuntimeError):
    """Another healthy dispatcher owns the database; no recovery should run."""


@dataclass
class WorkerHealth:
    status: str = 'starting'
    failures: int = 0
    last_error_type: str | None = None
    last_success: float | None = None
    retry_at: float | None = None
    successes: int = 0
    standby_count: int = 0

    def beat(self):
        self.last_success, self.last_error_type = time.time(), None
        self.successes += 1


async def supervise(operation, health, *, interval=None):
    delay = 1
    try:
        while True:
            health.status, health.retry_at = 'running', None
            successes = health.successes
            try:
                await operation()
                if interval is None:
                    raise RuntimeError('Infrastructure loop exited unexpectedly.')
                health.beat()
                delay = 1
                await asyncio.sleep(interval)
            except LeaseBusy:
                health.status, health.last_error_type = 'standby', None
                health.standby_count += 1
                health.retry_at = time.time()+5
                await asyncio.sleep(5)
            except Exception as error:
                if health.successes != successes:
                    delay = 1
                health.status = 'backoff'
                health.failures += 1
                health.last_error_type = type(error).__name__
                health.retry_at = time.time()+delay
                logging.getLogger(__name__).warning('Workflow worker unavailable (%s)',type(error).__name__)
                await asyncio.sleep(delay)
                delay = min(60,delay*2)
    finally:
        health.status, health.retry_at = 'stopped', None
