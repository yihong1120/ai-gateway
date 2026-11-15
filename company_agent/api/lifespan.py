"""Own one worker and bind its service before host startup creates child tasks."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

from company_agent.api.service import create_service
from company_agent.services.external_tools.connection import external_tools_connection
from examples.shared.runtime.jobs import application_jobs


class ServiceLifespan:
    def __init__(self, original, factory=None):
        self.original = original
        self.factory = factory if factory is not None else create_service

    @asynccontextmanager
    async def run(self, app):
        svc = await asyncio.to_thread(self.factory)
        app.state.job_service = svc
        try:
            with application_jobs(svc):
                # Host startup tasks inherit the same bound application service.
                async with self.original(app):
                    workflow = getattr(app.state, 'workflow', None)
                    if workflow is not None and hasattr(svc, 'document_outputs') and svc.document_outputs is None:
                        from examples.official_document_workflow.application.output_catalog import OutputCatalog
                        svc.document_outputs = OutputCatalog(workflow.outputs).files_for_jobs
                    async with AsyncExitStack() as integrations:
                        # Composition-root integrations start after shared services
                        # and stop only after workers finish their cleanup.
                        for factory in getattr(app.state, 'job_integrations', ()):
                            await integrations.enter_async_context(factory(app, svc))
                        async with external_tools_connection(app, getattr(svc, 'mcp_servers', ())) as external_tools:
                            svc.external_tools = external_tools
                            svc.task = asyncio.create_task(svc.loop())
                            try:
                                yield
                            finally:
                                svc.task.cancel()
                                await asyncio.gather(svc.task, return_exceptions=True)
                                svc.external_tools = None
                                if not svc.task.cancelled():
                                    svc.task.result()
        finally:
            del app.state.job_service
