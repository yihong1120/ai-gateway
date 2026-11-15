"""Lifespan-owned service, worker lease, async database pool and HTTP client."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, suppress
import fcntl
import os

import httpx

from examples.company_ai_access.storage.repository import Repository
from examples.company_ai_access.services.devices import Service
from examples.company_ai_access.storage.registry import Store
from examples.company_ai_access.services.inference import InferenceProxy
from examples.company_ai_access.adapters.upstream import Directory, LiteLLM


@asynccontextmanager
async def runtime(app, settings):
    # Bootstrap/migrations are offline synchronous operations, off the event loop.
    registry = await asyncio.to_thread(Store, settings.state_dir)
    fd = os.open(
        settings.state_dir / 'service.lock',
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('One AI access worker is already using this registry.') from None
        async with AsyncExitStack() as stack:
            repository = await stack.enter_async_context(Repository(registry))
            await stack.enter_async_context(repository.lease())
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    limits=httpx.Limits(max_connections=30, max_keepalive_connections=10),
                )
            )
            instance = Service(
                settings,
                repository,
                LiteLLM(settings, client),
                Directory(settings, client),
            )
            app.state.company_ai_access = instance
            app.state.company_ai_inference = InferenceProxy(settings, client)
            stop = asyncio.Event()
            task = asyncio.create_task(instance.maintenance_loop(stop))
            try:
                yield instance
            finally:
                stop.set()
                task.cancel()
                try:
                    with suppress(asyncio.CancelledError):
                        await task
                finally:
                    app.state.company_ai_access = None
                    app.state.company_ai_inference = None
    finally:
        os.close(fd)
