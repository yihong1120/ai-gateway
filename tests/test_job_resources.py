"""Per-app workers, request bindings and startup failure cleanup."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock

from fastapi import FastAPI
import httpx

from company_agent.api.lifespan import ServiceLifespan
from examples.shared.runtime.jobs import application_jobs, JobServiceMiddleware, service


class JobResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_apps_bind_distinct_services_to_host_tasks_requests_and_workers(self):
        observed, cancelled = [], []
        def resource(name):
            value = SimpleNamespace(name=name, task=None)
            async def work():
                observed.append(('worker', service().name))
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(name)
            value.loop = work
            return value
        first, second = resource('one'), resource('two')
        @asynccontextmanager
        async def host(app):
            observed.append(('host', service().name))
            yield
        def app_for(current):
            app = FastAPI(lifespan=ServiceLifespan(host, lambda: current).run)
            app.add_middleware(JobServiceMiddleware, owner=app)
            @app.get('/current')
            async def current_service():
                await asyncio.sleep(0)
                return {'name': service().name}
            return app
        one, two = app_for(first), app_for(second)
        async with one.router.lifespan_context(one):
            async with two.router.lifespan_context(two):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(one), base_url='http://one') as a, \
                        httpx.AsyncClient(transport=httpx.ASGITransport(two), base_url='http://two') as b:
                    responses = await asyncio.gather(a.get('/current'), b.get('/current'), a.get('/current'))
                self.assertEqual([r.json()['name'] for r in responses], ['one', 'two', 'one'])
                self.assertIs(service(), second)
            self.assertIs(service(), first)
            self.assertTrue(second.task.cancelled())
        self.assertCountEqual(observed, [('host','one'),('host','two'),('worker','one'),('worker','two')])
        self.assertCountEqual(cancelled, ['one','two'])
        self.assertFalse(hasattr(one.state, 'job_service'))
        with self.assertRaises(RuntimeError):
            service()

    async def test_failed_host_startup_unbinds_service_without_starting_worker(self):
        current = SimpleNamespace(loop=Mock(), task=None)
        @asynccontextmanager
        async def host(app):
            self.assertIs(service(), current)
            raise ValueError('startup failed')
            yield
        app = FastAPI()
        with self.assertRaises(ValueError):
            async with ServiceLifespan(host, lambda: current).run(app):
                self.fail('startup unexpectedly completed')
        current.loop.assert_not_called()
        self.assertFalse(hasattr(app.state, 'job_service'))
        with self.assertRaises(RuntimeError):
            service()

    async def test_binding_restores_parent_after_failure(self):
        with application_jobs('parent'):
            with self.assertRaises(ValueError):
                with application_jobs('child'):
                    raise ValueError('failed')
            self.assertEqual(service(), 'parent')

    async def test_catalog_is_bound_to_the_app_repository_at_startup(self):
        from examples.official_document_workflow.application.output_receipts import MemoryOutputReceipts
        from company_agent.api.service import Service
        repository = MemoryOutputReceipts()
        current = Service(None,None,None,None)
        current.loop = AsyncMock()
        @asynccontextmanager
        async def host(app):
            app.state.workflow = SimpleNamespace(outputs=repository)
            yield
        app = FastAPI()
        async with ServiceLifespan(host,lambda:current).run(app):
            self.assertIs(current.document_outputs.__self__.repository,repository)
