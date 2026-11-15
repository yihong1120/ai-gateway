"""Async ownership, cancellation and device-state transaction regressions."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi import Request
from psycopg_pool import PoolClosed

from examples.company_ai_access.config import Settings
from examples.company_ai_access.runtime import runtime
from examples.company_ai_access.services.devices import challenge
from examples.company_ai_access.services.inference import InferenceProxy
from examples.company_ai_access.services.inference import stream_response
from examples.company_ai_access.storage.registry import Store
from examples.company_ai_access.storage.repository import Repository
from tests.ai_access_postgres import access_database
from tests.test_company_ai_access import Fixture


class Transactions(Fixture):
    async def test_approval_audit_failure_rolls_back_device_and_enrollment(self):
        started = await self.service.start('test', challenge(self.verifier), None, 'local')
        with patch.object(self.store, 'audit', AsyncMock(side_effect=RuntimeError('audit unavailable'))):
            with self.assertRaises(RuntimeError):
                await self.service.approve('alice', started['request_id'], started['user_code'])
        self.assertEqual(await self.store.list('device'), [])
        self.assertEqual((await self.store.get('enrollment', started['request_id']))['status'], 'pending')
        await self.service.approve('alice', started['request_id'], started['user_code'])

    async def test_ack_audit_failure_does_not_retire_old_device(self):
        old = await self.enroll()
        started = await self.approve(replace_id=old['device_id'])
        ready = await self.service.poll(started['request_id'], self.verifier)
        with patch.object(self.store, 'audit', AsyncMock(side_effect=RuntimeError('audit unavailable'))):
            with self.assertRaises(RuntimeError):
                await self.service.ack(started['request_id'], self.verifier)
        self.assertEqual((await self.store.get('device', old['device_id']))['status'], 'active')
        self.assertFalse((await self.store.get('device', ready['device_id']))['acked'])
        self.assertEqual((await self.store.get('enrollment', started['request_id']))['status'], 'delivered')
        await self.service.ack(started['request_id'], self.verifier)
        self.assertEqual((await self.store.get('device', old['device_id']))['status'], 'revoked')

    async def test_revocation_audit_failure_keeps_state_and_upstream_consistent(self):
        ready = await self.enroll()
        with patch.object(self.store, 'audit', AsyncMock(side_effect=RuntimeError('audit unavailable'))):
            with self.assertRaises(RuntimeError):
                await self.service.revoke('alice', ready['device_id'])
        self.assertEqual((await self.store.get('device', ready['device_id']))['status'], 'active')
        self.assertEqual(self.llm.blocked, [])

    async def test_membership_denial_cannot_be_overwritten_by_inflight_activation(self):
        started = await self.approve()
        entered, release = asyncio.Event(), asyncio.Event()

        async def ensure_key(*args):
            entered.set()
            await release.wait()

        with patch.object(self.llm, 'ensure_key', ensure_key):
            delivery = asyncio.create_task(self.service.poll(started['request_id'], self.verifier))
            await asyncio.wait_for(entered.wait(), 2)
            self.directory.disabled.add('alice')
            denial = asyncio.create_task(self.service.member('alice'))
            release.set()
            ready = await delivery
            await self.assert_error(403, lambda: denial)
        self.assertEqual((await self.store.get('device', ready['device_id']))['status'], 'revoked')

    async def test_cancelled_transaction_rolls_back_state_and_audit(self):
        written = asyncio.Event()

        async def write():
            async with self.store.transaction():
                await self.store.put('device', {'id': 'cancelled'})
                await self.store.audit(100, 'cancelled')
                written.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(write())
        await asyncio.wait_for(written.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(await self.store.get('device', 'cancelled'))
        async with self.store.db() as db:
            self.assertEqual(await (await db.execute('SELECT count(*) AS count FROM audit')).fetchone(), {'count': 0})

    async def test_child_task_does_not_borrow_parent_transaction(self):
        async with self.store.transaction():
            await self.store.put('device', {'id': 'parent'})
            self.assertIsNotNone(await self.store.get('device', 'parent'))
            self.assertIsNone(await asyncio.create_task(self.store.get('device', 'parent')))
        self.assertIsNotNone(await self.store.get('device', 'parent'))

    async def test_database_wait_does_not_block_event_loop(self):
        async def database_wait():
            async with self.store.db() as db:
                await db.execute('SELECT pg_sleep(0.15)')

        task = asyncio.create_task(database_wait())
        ticks = 0
        while not task.done():
            await asyncio.sleep(0.01)
            ticks += 1
        await task
        self.assertGreater(ticks, 3)


class Lifespan(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        access_database(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(
            origin='https://example.com',
            state_dir=Path(temporary.name),
            litellm_url='http://127.0.0.1:4000',
            master_key='synthetic',
        )

    async def test_shutdown_closes_pool_client_and_releases_worker(self):
        app = FastAPI()
        with patch('examples.company_ai_access.services.devices.Service.maintain', AsyncMock(return_value={})):
            async with runtime(app, self.settings) as service:
                repository = service.repository
                client = app.state.company_ai_inference.client
                self.assertFalse(client.is_closed)
        self.assertIsNone(app.state.company_ai_access)
        self.assertIsNone(app.state.company_ai_inference)
        self.assertTrue(client.is_closed)
        with self.assertRaises(PoolClosed):
            await repository.list('device')
        async with Repository(Store(self.settings.state_dir)) as next_repository:
            async with next_repository.lease():
                pass

    async def test_shutdown_exits_when_database_consumes_cancellation(self):
        entered = asyncio.Event()

        async def maintain(_service):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {}

        async def lifecycle():
            with patch('examples.company_ai_access.services.devices.Service.maintain', maintain):
                async with runtime(FastAPI(), self.settings):
                    await entered.wait()

        await asyncio.wait_for(lifecycle(), 2)

    async def test_partial_startup_failure_releases_resources(self):
        with patch('examples.company_ai_access.runtime.httpx.AsyncClient') as client:
            client.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError('startup failed'))
            with self.assertRaisesRegex(RuntimeError, 'startup failed'):
                async with runtime(FastAPI(), self.settings):
                    self.fail('startup unexpectedly succeeded')
        with patch('examples.company_ai_access.services.devices.Service.maintain', AsyncMock(return_value={})):
            async with runtime(FastAPI(), self.settings):
                pass


class Transport(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_error_body_closes_upstream(self):
        class Stream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                raise asyncio.CancelledError()
                yield b''

            async def aclose(self):
                self.closed = True

        stream = Stream()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(502, stream=stream),
            ),
        ) as client:
            settings = type('Settings', (), {'litellm_url': 'http://127.0.0.1:4000'})()
            request = Request({'type': 'http', 'headers': []})
            with self.assertRaises(asyncio.CancelledError):
                await InferenceProxy(settings, client).forward(
                    request, 'synthetic', {'subject': 'alice'}, {}, '/v1/responses',
                )
        self.assertTrue(stream.closed)

    async def test_interrupted_success_stream_closes_upstream(self):
        class Stream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b'first'
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        stream = Stream()
        response = stream_response(httpx.Response(200, stream=stream))
        self.assertEqual(await anext(response.body_iterator), b'first')
        await response.body_iterator.aclose()
        self.assertTrue(stream.closed)
