"""App-owned workflow isolation and supervised infrastructure recovery."""
from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import unittest
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
from fastapi import FastAPI

import examples.official_document_workflow.infrastructure.storage.automation_store as _automation_store_imports
from examples.official_document_workflow.runtime import current_resources
from examples.official_document_workflow.runtime import WorkflowMiddleware
from examples.official_document_workflow.runtime import WorkflowResources
from examples.official_document_workflow.runtime import WorkflowSettings
from examples.shared.runtime.io import run_sync
from examples.shared.runtime.workers import LeaseBusy
from examples.shared.runtime.workers import supervise
from examples.shared.runtime.workers import WorkerHealth


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def resources(self, label):
        return WorkflowResources(WorkflowSettings('postgresql://fake/' + label, 'eip_' + label, Path('/unused') / label))

    async def test_app_middleware_and_child_tasks_isolate_resources(self):
        one, two = self.resources('one'), self.resources('two')

        def make(resources):
            app = FastAPI()
            app.state.workflow = resources
            app.add_middleware(WorkflowMiddleware, owner=app)

            @app.get('/state')
            async def state():
                await asyncio.sleep(0)
                return {'schema': current_resources().settings.schema, 'owners': list(current_resources().state.jobs)}
            return app
        one.state.jobs['first'] = {}
        two.state.jobs['second'] = {}
        with one.bind():
            child = asyncio.create_task(run_sync(lambda: current_resources().settings.schema))
            with two.bind():
                self.assertIs(current_resources(), two)
            self.assertIs(current_resources(), one)
            self.assertEqual(await child, 'eip_one')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(make(one)), base_url='http://one') as a, \
                httpx.AsyncClient(transport=httpx.ASGITransport(make(two)), base_url='http://two') as b:
            responses = await asyncio.gather(a.get('/state'), b.get('/state'))
        self.assertEqual(
            [r.json() for r in responses], [
                {'schema': 'eip_one', 'owners': ['first']}, {'schema': 'eip_two', 'owners': ['second']},
            ],
        )
        with self.assertRaises(RuntimeError):
            current_resources()

    def test_resource_creation_has_no_database_side_effects(self):
        with patch('examples.official_document_workflow.infrastructure.storage.automation_store.AutomationStore') as backend, \
                patch('examples.official_document_workflow.infrastructure.storage.state.Journal') as journal:
            resource = self.resources('one')
            backend.assert_not_called()
            journal.assert_not_called()
            with resource.bind():
                self.assertFalse(resource.state.links)
                resource.initialize()
                self.assertIs(_automation_store_imports.store(), backend.return_value)
                resource.initialize()
            backend.assert_called_once_with(Path('/unused/one'), dsn='postgresql://fake/one', schema='eip_one')
            journal.assert_called_once()

    async def test_periodic_worker_recovers_first_failure_and_does_not_leak_error_text(self):
        health, waits = WorkerHealth(), []
        operation = AsyncMock(side_effect=[OSError('PRIVATE database address'), None])

        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                raise asyncio.CancelledError
        with patch('examples.shared.runtime.workers.asyncio.sleep', side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await supervise(operation, health, interval=60)
        self.assertEqual(waits, [1, 60])
        self.assertEqual(health.failures, 1)
        self.assertIsNotNone(health.last_success)
        self.assertEqual(health.status, 'stopped')
        self.assertNotIn('PRIVATE', str(asdict(health)))

    async def test_continuous_worker_resets_backoff_after_a_successful_heartbeat(self):
        health, waits = WorkerHealth(), []
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            if calls == 3:
                health.beat()
            raise OSError('temporary infrastructure error')

        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 4:
                raise asyncio.CancelledError
        with patch('examples.shared.runtime.workers.asyncio.sleep', side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await supervise(operation, health)
        self.assertEqual(waits, [1, 2, 1, 2])
        self.assertEqual(health.failures, 4)

    async def test_busy_lease_is_standby_and_does_not_count_as_database_failure(self):
        health, waits = WorkerHealth(), []
        operation = AsyncMock(side_effect=[LeaseBusy(), OSError()])

        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                raise asyncio.CancelledError
        with patch('examples.shared.runtime.workers.asyncio.sleep', side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await supervise(operation, health)
        self.assertEqual(waits, [5, 1])
        self.assertEqual(health.standby_count, 1)
        self.assertEqual(health.failures, 1)

    async def test_cancellation_drains_blocking_operation_before_releasing_scope(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def write():
            entered.set()
            release.wait(3)
            finished.set()
        task = asyncio.create_task(run_sync(write))
        await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())

    async def test_lifespan_retries_initial_database_failure_and_drains_its_workers(self):
        config = ModuleType('open_webui.models.config')
        config.Config = object()
        with patch.dict(sys.modules, {'open_webui.models.config': config}):
            lifecycle = importlib.import_module('examples.official_document_workflow.api.lifespan')
        resource = self.resources('supervised')
        resource._journal = object()
        initialize = unittest.mock.Mock(side_effect=[OSError('temporary database failure'), None])
        resource.initialize = initialize
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def scheduler():
            self.assertIs(current_resources(), resource)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        @asynccontextmanager
        async def host(app):
            self.assertIs(current_resources(), resource)
            yield
        resource._scheduler = SimpleNamespace(run=scheduler)
        app = SimpleNamespace(state=SimpleNamespace())
        with patch.object(lifecycle, 'ensure_entry', AsyncMock()), \
                patch.object(lifecycle.WorkflowLifespan, 'repair', AsyncMock()):
            async with lifecycle.WorkflowLifespan(host, lambda: resource).context(app):
                await asyncio.wait_for(entered.wait(), 3)
                self.assertEqual(resource.health['scheduler'].failures, 1)
                self.assertIs(app.state.workflow, resource)
        self.assertEqual(initialize.call_count, 2)
        self.assertTrue(stopped.is_set())
        self.assertFalse(hasattr(app.state, 'workflow'))
        self.assertTrue(all(health.status == 'stopped' for health in resource.health.values()))
        with self.assertRaises(RuntimeError):
            current_resources()
