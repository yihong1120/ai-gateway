"""Connectivity recovery without replaying jobs or exposing NAS credentials."""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
import psycopg

from company_agent.api.service import Service
from company_agent.api.service import WorkQueue
from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.middleware import visible_sources
from company_agent.runtime.models import close_model
from company_agent.runtime.models import gateway_for_verified_user
from company_agent.services.files.nas import NASClient
from company_agent.services.files.nas import NASUnavailable
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_user_billing import response
from company_agent.tests.test_user_billing import verified


class NASConnectivityTests(unittest.TestCase):
    def test_transport_errors_are_actionable_and_not_retried(self):
        for error in (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError):
            calls = []

            def handler(request):
                calls.append(request)
                raise error('PRIVATE credentials', request=request)
            client = NASClient(
                'https://nas.example', 'alice', 'PRIVATE password',
                transport=httpx.MockTransport(handler),
            )
            with self.assertRaises(NASUnavailable) as caught:
                with client:
                    self.fail('unreachable NAS must not authenticate')
            self.assertIn('NAS', str(caught.exception))
            self.assertIn('帳號與連線', str(caught.exception))
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertEqual(len(calls), 1)
            self.assertTrue(client.http.is_closed)
            self.assertIsNone(client.password)
            self.assertEqual(calls[0].extensions['timeout']['connect'], 5)
            self.assertEqual(calls[0].extensions['timeout']['read'], 30)

    def test_admin_route_preserves_bound_identity_and_rejects_insecure_url(self):
        with patch.dict(
            os.environ, {
                'COMPANY_AGENT_NAS_URL': 'https://old-lan.example:5001',
                'COMPANY_AGENT_NAS_CONNECT_URL': 'https://verified-relay.example',
            },
        ):
            client = NASClient('https://old-lan.example:5001', 'alice', 'secret')
            try:
                self.assertEqual(str(client.http.base_url), 'https://verified-relay.example')
            finally:
                client.close()
            other = NASClient('https://unrelated.example', 'bob', 'secret')
            try:
                self.assertEqual(str(other.http.base_url), 'https://unrelated.example')
            finally:
                other.close()
            os.environ['COMPANY_AGENT_NAS_CONNECT_URL'] = 'http://verified-relay.example'
            with self.assertRaises(ValueError):
                NASClient('https://old-lan.example:5001', 'alice', 'secret')


class QueueRecoveryTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.queue = WorkQueue(self.private, self.base / 'uploads')

    def submit(self, request):
        return self.queue.submit('alice', 'oidc-alice', 'chat', request, 'test ' + request, [])

    async def stop(self, svc):
        svc.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await svc.task

    async def test_lost_worker_lease_cancels_active_work_and_recovers_queue(self):
        old, following = self.submit('old'), self.submit('following')
        engine = self.store.job('alice')
        artifact = self.store.artifact('alice', engine, 'keep.txt', b'preserved')
        leases, calls = [], []
        started, stopped = asyncio.Event(), asyncio.Event()
        mutex = self.queue.store.mutex

        @contextmanager
        def track(key):
            with mutex(key) as lease:
                if key == 'worker':
                    leases.append(lease)
                yield lease

        async def execute(row):
            calls.append(row['id'])
            if row['id'] == old:
                self.queue.update(old, engine_job=engine)
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            else:
                self.queue.update(row['id'], status='done')
        svc = Service(self.queue, None, None, None)
        with patch.object(self.queue.store, 'mutex', track), patch.object(svc, 'execute', side_effect=execute):
            svc.task = asyncio.create_task(svc.loop())
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                # Only terminate this test's own randomly scoped lock connection.
                with self.store.db() as db:
                    db.execute('SELECT pg_terminate_backend(%s)', (leases[0].info.backend_pid,))
                async with asyncio.timeout(12):
                    while self.queue.get('alice', following)['status'] != 'done':
                        await asyncio.sleep(0.05)
                self.assertTrue(stopped.is_set())
                self.assertFalse(svc.task.done())
                self.assertEqual(calls, [old, following])
                self.assertEqual(self.queue.get('alice', old)['status'], 'interrupted')
                self.assertEqual(self.store.read_artifact('alice', artifact['id'])[2], b'preserved')
                self.assertGreaterEqual(len(leases), 2)
            finally:
                await self.stop(svc)

    async def test_transient_claim_failures_leave_queued_job_for_recovery(self):
        key = self.submit('new')
        claim, attempts = self.queue.claim, []

        def flaky_claim(excluded_owners=(), kinds=('company', 'document')):
            attempts.append(1)
            if len(attempts) < 3:
                raise psycopg.OperationalError('connection temporarily unavailable')
            return claim(excluded_owners, kinds)

        async def execute(row):
            self.queue.update(row['id'], status='done')
        svc = Service(self.queue, None, None, None)
        with patch.object(self.queue, 'claim', side_effect=flaky_claim), patch.object(svc, 'execute', new=AsyncMock(side_effect=execute)) as run:
            svc.task = asyncio.create_task(svc.loop())
            try:
                async with asyncio.timeout(10):
                    while self.queue.get('alice', key)['status'] != 'done':
                        await asyncio.sleep(0.05)
                run.assert_awaited_once()
                self.assertFalse(svc.task.done())
            finally:
                await self.stop(svc)

    async def test_nas_failure_status_survives_restart_and_keeps_next_job_running(self):
        old, following = self.submit('nas'), self.submit('next')

        async def execute(row):
            if row['id'] == old:
                raise NASUnavailable('NAS 無法連線；請在帳號與連線測試')
            self.queue.update(row['id'], status='done')
        svc = Service(self.queue, None, None, None)
        with patch.object(svc, 'execute', side_effect=execute):
            svc.task = asyncio.create_task(svc.loop())
            try:
                async with asyncio.timeout(5):
                    while self.queue.get('alice', following)['status'] != 'done':
                        await asyncio.sleep(0.05)
                before = svc.result('alice', old)
                after = Service(self.queue, None, None, None).result('alice', old)
                self.assertEqual(before, after)
                self.assertIn('NAS', after['progress'])
                with self.assertRaises(ValueError):
                    svc.result('bob', old)
            finally:
                await self.stop(svc)

    def test_dead_mutex_cleanup_preserves_original_cancellation(self):
        with self.assertRaises(asyncio.CancelledError):
            with self.store.mutex('test-dead-lease') as lease:
                with self.store.db() as db:
                    db.execute('SELECT pg_terminate_backend(%s)', (lease.info.backend_pid,))
                raise asyncio.CancelledError()
        with self.store.mutex('test-dead-lease'):
            pass


class SourceDiscoveryTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_nas_discovery_failure_keeps_local_work_and_is_cached_per_run(self):
        self.config.structured_completion = True
        self.config.require_user_billing = True
        script = [
            ('delegate_knowledge', {'task': '查詢 NAS'}),
            ('complete_assignment', {'outcome': 'blocked', 'summary': 'NAS 無法連線，未查得資料', 'artifact_ids': []}),
            ('delegate_document', {'task': '使用可用的上傳資料，說明限制'}),
            ('complete_assignment', {'outcome': 'blocked', 'summary': '目前只可使用本機資料', 'artifact_ids': []}),
            ('complete_job', {'outcome': 'blocked', 'summary': 'NAS 無法連線，請檢查網路；其他來源可使用', 'artifact_ids': []}),
        ]
        requests = []

        def model_api(request):
            value = json.loads(request.content)
            requests.append(value)
            name, args = script.pop(0)
            return response(name, args)
        discoveries = []

        def unavailable(ctx, state, role, parameters):
            if role == 'knowledge':
                discoveries.append(role)
                raise NASUnavailable('NAS 無法連線；請檢查網路')
            return visible_sources(ctx, state, role, parameters)
        model = gateway_for_verified_user(
            verified(), 'https://gateway.example/v1',
            'key', 'model', httpx.MockTransport(model_api),
        )
        try:
            with patch('company_agent.runtime.middleware.visible_sources', side_effect=unavailable):
                result = await DocumentAgent(self.config, model).run('alice', '查 NAS，並處理本機文件')
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(discoveries, ['knowledge'])
            self.assertFalse(script)
            for request in requests[1:]:
                prompt = request['messages'][0]['content']
                self.assertIn('來源連線限制', prompt)
                self.assertIn('project', prompt)
                self.assertIn('不要猜測', prompt)
        finally:
            await close_model(model)
