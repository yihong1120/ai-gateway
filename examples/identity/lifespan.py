from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextlib import AsyncExitStack

from fastapi import FastAPI

from examples.identity import models
from examples.identity.cache import rate_limiter_service
from examples.identity.migrations import require_schema
from examples.identity.migrations import upgrade
from examples.identity.redis_pool import RedisClient
from examples.identity.runtime import identity_scope
from examples.identity.sessions.scheduler import start_jwt_scheduler
from examples.shared.runtime.workers import supervise
from examples.shared.runtime.workers import WorkerHealth
logger = logging.getLogger(__name__)


async def _stop_cleanup(task, stop):
    stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def global_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Own all resources, including cleanup when startup or a request fails."""
    resources = app.state.identity
    database = resources.database
    with identity_scope(resources):
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(resources.http_clients.close)
            cleanup.push_async_callback(database.close)
            database.start()
            scheduler = start_jwt_scheduler(app)
            cleanup.callback(scheduler.shutdown)
            app.state.redis_client = RedisClient(resources.redis_url)
            cleanup.push_async_callback(app.state.redis_client.close)
            redis_conn = await app.state.redis_client.connect()
            app.state.http_clients = resources.http_clients
            try:
                await rate_limiter_service.preload_script(redis_conn)
            except Exception:
                logger.warning('Rate-limit script preload unavailable', exc_info=True)
            if resources.auto_create_schema:
                async with database.engine.begin() as conn:
                    await conn.run_sync(models.Base.metadata.create_all)
                await upgrade(database.engine)
            await require_schema(database.engine)

            async def recover_accounts():
                async with database.session() as db:
                    await resources.recovery.drain_revocations(db, redis_conn)
            account_stop = asyncio.Event()
            account_worker = asyncio.create_task(supervise(recover_accounts, WorkerHealth(), interval=5))
            cleanup.push_async_callback(_stop_cleanup, account_worker, account_stop)
            yield
