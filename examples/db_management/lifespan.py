"""Management-only workers wrap the common identity resource lifespan."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from examples.identity.lifespan import global_lifespan as identity_lifespan
from examples.db_management.services.site_media_cleanup import drain_site_media_cleanup_jobs

logger = logging.getLogger(__name__)
_site_media_cleanup_interval_seconds = 30


async def _run_site_media_cleanup_worker(
    stop_event: asyncio.Event,
    database,
) -> None:
    """Periodically drain durable media-cleanup jobs outside HTTP requests."""
    failures = 0
    while not stop_event.is_set():
        try:
            async with database.session() as db:
                await drain_site_media_cleanup_jobs(db)
            failures = 0
        except Exception:
            failures += 1
            logger.warning(
                'Deferred site media cleanup worker failed',
                exc_info=True,
            )
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=min(300, 2 ** min(failures, 9))
                if failures
                else _site_media_cleanup_interval_seconds,
            )
        except TimeoutError:
            continue



@asynccontextmanager
async def global_lifespan(app):
    async with identity_lifespan(app):
        stop = asyncio.Event()
        task = asyncio.create_task(_run_site_media_cleanup_worker(stop, app.state.identity.database))
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
