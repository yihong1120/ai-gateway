"""Own EIP resources and supervise infrastructure without replaying writes."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import AsyncExitStack

from examples.official_document_workflow.infrastructure.eip.webui_entry import (
    ensure_entry,
)
from examples.official_document_workflow.runtime import WorkflowResources
from examples.official_document_workflow.runtime import WorkflowSettings
from examples.shared.runtime.database import application_database_pools
from examples.shared.runtime.io import run_sync
from examples.shared.runtime.modules import ModuleReference
from examples.shared.runtime.workers import supervise

_shared_jobs = ModuleReference('examples.shared.runtime.jobs')


class WorkflowLifespan:
    def __init__(self, original, factory=None):
        self.original = original
        self.factory = factory or (lambda: WorkflowResources(WorkflowSettings.from_environment()))

    async def scheduler(self, resources):
        await run_sync(resources.initialize)
        await resources.scheduler.run()

    async def repair(self, resources, publish):
        await run_sync(resources.initialize)
        await resources.output_repair(publish).run_once()

    async def shutdown(self, resources, workers):
        for task in [*workers, *resources.state.tasks]:
            task.cancel()
        await asyncio.gather(*workers, *resources.state.tasks, return_exceptions=True)
        # Attempt every session close even if one adapter fails.
        results = await asyncio.gather(
            *(resources.state.unlink(owner) for owner in list(resources.state.links)),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise BaseExceptionGroup('Workflow session cleanup failed', failures)

    @asynccontextmanager
    async def context(self, app):
        resources = self.factory()
        app.state.workflow = resources
        try:
            with resources.bind():
                async with AsyncExitStack() as cleanup:
                    app.state.database_pools = await cleanup.enter_async_context(application_database_pools())
                    await cleanup.enter_async_context(self.original(app))
                    await ensure_entry()
                    job_service = _shared_jobs.attribute('service')
                    try:
                        jobs = job_service()
                    except RuntimeError:
                        publish = None
                    else:
                        async def publish(owner, key):
                            def notify():
                                if jobs.queue.output_changed(owner, key):
                                    return True
                                # Automatic EIP task outputs have no WebUI job.
                                # A foreign app's WebUI job must stay pending.
                                with resources.store.db() as db:
                                    return db.execute(
                                        'SELECT 1 FROM tasks WHERE owner=%s AND id=%s',
                                        (owner, key),
                                    ).fetchone() is not None
                            return await run_sync(notify)
                    workers = [
                        asyncio.create_task(
                            supervise(
                                lambda: self.scheduler(
                                    resources,
                                ), resources.health['scheduler'],
                            ),
                        ),
                        asyncio.create_task(supervise(resources.state.sweep, resources.health['cleanup'], interval=60)),
                        asyncio.create_task(
                            supervise(
                                lambda: self.repair(resources, publish),
                                resources.health['outputs'], interval=5,
                            ),
                        ),
                    ]
                    cleanup.push_async_callback(self.shutdown, resources, workers)
                    yield
        finally:
            del app.state.workflow

    def __call__(self, app):
        return self.context(app)
