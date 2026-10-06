"""Open WebUI lifespan extension for EIP sessions and hourly automation."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import AsyncExitStack

from .routes import JOURNAL
from official_document_workflow.application import automation
from official_document_workflow.infrastructure.eip.webui_entry import ensure_entry
from official_document_workflow.infrastructure.storage.state import STATE


class WorkflowLifespan:
    def __init__(self, original):
        self.original = original

    async def clean(self):
        while True:
            await asyncio.sleep(60)
            await STATE.sweep()

    async def shutdown(self, clean_task, automatic_task):
        clean_task.cancel()
        automatic_task.cancel()
        for job in list(STATE.tasks):
            job.cancel()
        await asyncio.gather(
            clean_task,
            automatic_task,
            *STATE.tasks,
            return_exceptions=True,
        )
        for owner in list(STATE.links):
            await STATE.unlink(owner)

    @asynccontextmanager
    async def context(self, app):
        async with AsyncExitStack() as cleanup:
            await cleanup.enter_async_context(self.original(app))
            await ensure_entry()
            clean_task = asyncio.create_task(self.clean())
            automatic_task = asyncio.create_task(automation.scheduler(JOURNAL))
            cleanup.push_async_callback(self.shutdown, clean_task, automatic_task)
            yield

    def __call__(self, app):
        return self.context(app)
