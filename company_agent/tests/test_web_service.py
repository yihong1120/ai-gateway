from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request

from examples.shared.runtime.jobs import application_jobs
from company_agent.api.pipe import current_turn
from company_agent.api.pipe import run_pipe
from company_agent.api.service import Service
from company_agent.api.service import ServiceConfiguration
from company_agent.api.service import WorkQueue
from company_agent.config import Root
from company_agent.config import Settings
from company_agent.runtime.models import gateway_for_verified_user
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_user_billing import response
from company_agent.tests.test_user_billing import verified


class QueueTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.queue = WorkQueue(self.private, self.base/'uploads')

    def submit(self, request='message', **kwargs):
        return self.queue.submit('alice', 'oidc-alice', 'chat', request, kwargs.get('instruction', '建立文字文件 '+request), kwargs.get('inputs', []), kwargs.get('references', []), kwargs.get('conversation', []))

    def test_web_jobs_use_configured_execution_timeout(self):
        config = ServiceConfiguration(self.private, self.queue.sources)
        settings = self.config.model_copy(update={'execution_timeout_seconds': 75})
        row = self.queue.get('alice', self.submit())
        with patch.object(config, 'configured_settings', return_value=settings):
            self.assertEqual(config.settings(row).execution_timeout_seconds, 75)

    async def test_old_graph_keeps_artifacts_but_cannot_enqueue_resume(self):
        key = self.submit()
        engine = self.store.job('alice')
        artifact = self.store.artifact('alice', engine, 'old.txt', b'preserved')
        self.store.finish('alice', engine, 'error', {'graph_version': 20, 'resumable': True, 'notices': []})
        self.queue.update(key, engine_job=engine, status='error')
        result = self.queue.public('alice', key)
        self.assertTrue(result['requires_restart'])
        self.assertFalse(result['resumable'])
        self.assertEqual(result['files'][0]['id'], artifact['id'])
        with self.assertRaisesRegex(ValueError, '另開新工作'):
            self.queue.resume('alice', 'oidc-alice', key)
        self.assertEqual(self.queue.get('alice', key)['status'], 'error')

    async def test_duplicate_is_idempotent_and_changed_payload_denied(self):
        a = self.submit(inputs=[('source.txt', b'one')])
        b = self.submit(inputs=[('source.txt', b'one')])
        self.assertEqual(a, b)
        with self.assertRaises(ValueError):
            self.submit(inputs=[('source.txt', b'two')])
        self.assertEqual(len(self.queue.list('alice')), 1)

    async def test_regenerate_reuses_owned_active_work_and_records_message_aliases(self):
        first = self.submit(instruction='查同一筆行程', inputs=[('source.txt', b'one')])
        for status, request in [('queued', 'regenerate-queued'), ('running', 'regenerate-running')]:
            self.queue.update(first, status=status)
            reused = self.submit(request, instruction='查同一筆行程', inputs=[('source.txt', b'one')])
            self.assertEqual(reused, first)
            self.assertEqual(self.submit(request, instruction='查同一筆行程', inputs=[('source.txt', b'one')]), first)
        self.assertEqual(len(self.queue.list('alice')), 1)
        self.assertEqual(self.queue.get('alice', first)['request_aliases'], ['regenerate-queued', 'regenerate-running'])
        self.assertEqual(len([p for p in self.queue.sources.iterdir() if p.name != '.pending']), 1)
        with self.assertRaises(ValueError):
            self.submit('regenerate-running', instruction='改成另外一個問題')
        for status in ['done', 'error', 'interrupted', 'cancelled']:
            self.queue.update(first, status=status)
            new = self.submit('new-' + status, instruction='查同一筆行程', inputs=[('source.txt', b'one')])
            self.assertNotEqual(new, first)
            self.queue.update(new, status='done')

    async def test_active_reuse_does_not_cross_owner_chat_identity_sources_or_binding(self):
        self.queue.eip_binding = lambda owner: 'binding-1'
        first = self.submit(instruction='same', inputs=[('source.txt', b'one')])
        other_owner = self.queue.submit('bob', 'oidc-bob', 'chat', 'other', 'same', [('source.txt', b'one')])
        other_chat = self.queue.submit('alice', 'oidc-alice', 'other-chat', 'other', 'same', [('source.txt', b'one')])
        other_identity = self.queue.submit(
            'alice', 'new-subject', 'chat',
            'new-identity', 'same', [('source.txt', b'one')],
        )
        other_content = self.submit('other-content', instruction='same', inputs=[('source.txt', b'two')])
        other_history = self.submit(
            'other-history', instruction='same', inputs=[('source.txt', b'one')], conversation=[
                {'role': 'user', 'id': 'new-turn', 'content': 'changed context'},
            ],
        )
        self.queue.eip_binding = lambda owner: 'binding-2'
        other_binding = self.submit('other-binding', instruction='same', inputs=[('source.txt', b'one')])
        self.assertEqual(
            len({
                first, other_owner, other_chat, other_identity,
                other_content, other_history, other_binding,
            }), 7,
        )

    async def test_queued_progress_reports_multiuser_scheduling_without_other_job_details(self):
        first = self.submit(instruction='PRIVATE first')
        self.queue.claim()
        second = self.submit('second', instruction='PRIVATE second')
        third = self.queue.submit('bob', 'oidc-bob', 'bob-chat', 'third', 'third', [])
        self.assertEqual(self.queue.public('alice', second)['progress'], '等待執行（多人並行排程，同一帳號依序處理）')
        result = self.queue.public('bob', third)
        self.assertEqual(result['progress'], '等待執行（多人並行排程，同一帳號依序處理）')
        self.assertNotIn('PRIVATE', str(result))
        self.queue.update(first, status='done')
        self.assertEqual(self.queue.public('alice', second)['progress'], '等待執行（多人並行排程，同一帳號依序處理）')

    async def test_cross_owner_job_and_source_parent_denied(self):
        a = self.submit()
        self.queue.update(a, status='done')
        with self.assertRaises(ValueError):
            self.queue.public('bob', a)
        with self.assertRaises(ValueError):
            self.queue.submit('bob', 'bob-sub', 'chat', 'm', 'read', [], [a])
        with self.assertRaises(ValueError):
            self.queue.submit('alice', 'oidc-alice', 'other-chat', 'm', 'read', [], [a])

    async def test_recovery_keeps_queued_and_does_not_replay_running(self):
        a = self.submit()
        b = self.submit('two')
        claimed = self.queue.claim()
        self.assertEqual(claimed['id'], a)
        restored = WorkQueue(self.private, self.base/'uploads')
        self.assertEqual(restored.get('alice', a)['status'], 'running')
        self.assertIsNone(restored.claim())  # One active job per owner across restarts.
        restored.recover_running()
        self.assertEqual(restored.get('alice', a)['status'], 'interrupted')
        self.assertEqual(restored.claim()['id'], b)
        self.assertIsNone(restored.claim())

    async def test_inputs_private_canonical_and_unsupported_stops(self):
        a = self.submit(inputs=[('../../different/source.txt', b'text')])
        path = self.queue.sources/a/'input_1.txt'
        self.assertEqual(path.read_bytes(), b'text')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            self.submit('bad', inputs=[('macro.docm', b'bad')])
        self.assertEqual(len(self.queue.list('alice')), 1)

    async def test_real_graph_queue_result_and_followup_sources(self):
        requests = []
        script = [
            lambda _:response('delegate_document', {'task': '建立文字草稿'}),
            lambda _:response('create_text', {'name': 'result.txt', 'content': 'actual content'}),
            lambda body:response(
                'complete_assignment', {
                    'outcome': 'completed', 'summary': '完成草稿', 'artifact_ids': [
                        json.loads(body['messages'][-1]['content'])['id'],
                    ],
                },
            ),
            lambda body:response(
                'complete_job', {
                    'outcome': 'completed', 'summary': '已完成', 'artifact_ids': [
                        json.loads(body['messages'][-1]['content'])['artifacts'][0]['id'],
                    ],
                },
            ),
        ]

        def handler(request):
            value = json.loads(request.content)
            requests.append(value)
            return script.pop(0)(value)

        def factory(user, model_name=None): return gateway_for_verified_user(
            user, 'https://gateway.example/v1', 'key', 'model', httpx.MockTransport(handler),
        )

        def settings(row): return Settings(
            state_dir=str(self.private), structured_completion=True, require_user_billing=True,
            roots=[Root(name='uploads', path=str(self.queue.sources/row['id']), readers=['alice'], model_access=True)],
        )
        svc = Service(self.queue, AsyncMock(return_value=verified()), factory, settings)
        key = self.submit(inputs=[('source.txt', b'original')])
        await svc.execute(self.queue.claim())
        result = self.queue.public('alice', key)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['files']), 1)
        self.assertTrue(all(r['user'] == 'oidc-alice' for r in requests))
        self.assertNotIn('key', str(self.queue.get('alice', key)['payload']))
        new = self.submit('followup', references=[key])
        script.append(
            lambda body: response(
                'complete_job', {
                    'outcome': 'needs_input', 'summary': '請提供修改方向', 'artifact_ids': [],
                },
            ),
        )
        await svc.execute(self.queue.claim())
        self.assertEqual(self.queue.public('alice', new)['status'], 'needs_input')
        self.assertFalse(list((self.queue.sources/new).iterdir()))
        self.assertEqual((self.queue.sources/key/'input_1.txt').read_bytes(), b'original')
        self.assertEqual((self.queue.sources/key/(result['files'][0]['id']+'.txt')).read_bytes(), b'actual content')
        self.assertIn('history_1', requests[-1]['messages'][-1]['content'])

    async def test_effective_job_configuration_is_audited_before_execution(self):
        model = types.SimpleNamespace(subject='oidc-alice')
        factory = Mock(return_value=model)
        key = self.submit()
        engine_job = self.store.job('alice')
        svc = Service(self.queue, AsyncMock(return_value=verified()), factory, lambda row: self.config)
        with patch('company_agent.api.service.DocumentAgent') as agent, patch('company_agent.api.service.close_model', AsyncMock()):
            agent.return_value.run = AsyncMock(return_value={'job_id': engine_job, 'status': 'done'})
            await svc.execute(self.queue.claim())
        with self.store.db() as db:
            revision = db.execute("SELECT status FROM audit WHERE job=%s AND action='configuration'", (key,)).fetchone()[0]
        import hashlib
        self.assertEqual(revision, hashlib.sha256(self.config.model_dump_json().encode()).hexdigest())
        factory.assert_called_once_with(verified(), self.config.model_name)

    async def test_identity_changed_before_claim_does_not_call_model(self):
        calls = []

        def factory(user, model_name=None): return gateway_for_verified_user(
            user, 'https://gateway.example/v1',
            'key', 'model', httpx.MockTransport(lambda r: calls.append(r)),
        )
        key = self.submit()
        svc = Service(self.queue, AsyncMock(return_value=verified('changed')), factory, lambda row: self.config)
        with self.assertRaises(ValueError):
            await svc.execute(self.queue.claim())
        self.assertEqual(self.queue.public('alice', key)['status'], 'running')
        self.assertFalse(calls)

    async def test_weekly_word_then_eip_same_chat_uses_new_task_and_safe_batch(self):
        scripts = [
            lambda _:response('delegate_document', {'task': '製作虛構週報範例'}),
            lambda _:response(
                'create_word', {
                    'name': 'weekly.docx', 'title': '虛構示例，非實際工程紀錄',
                    'paragraphs': [{'text': '示例進度', 'heading': False}],
                },
            ),
            lambda b:response(
                'complete_assignment', {
                    'outcome': 'completed', 'summary': '已製作虛構示例', 'artifact_ids': [
                        json.loads(b['messages'][-1]['content'])['id'],
                    ],
                },
            ),
            lambda b:response(
                'complete_job', {
                    'outcome': 'completed', 'summary': '已製作虛構示例', 'artifact_ids': [
                        json.loads(b['messages'][-1]['content'])['artifacts'][0]['id'],
                    ],
                },
            ),
        ]
        requests = []

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            return scripts.pop(0)(body)

        def factory(user, model_name=None): return gateway_for_verified_user(
            user, 'https://gateway.example/v1', 'key', 'model', httpx.MockTransport(handler),
        )

        def settings(row): return Settings(
            state_dir=str(self.private), structured_completion=True, require_user_billing=True,
            roots=[Root(name='uploads', path=str(self.queue.sources/row['id']), readers=['alice'], model_access=True)],
        )
        svc = Service(self.queue, AsyncMock(return_value=verified()), factory, settings)
        first = self.submit(instruction='製作虛構週報 Word 範例')
        await svc.execute(self.queue.claim())
        self.assertEqual(self.queue.public('alice', first)['status'], 'done')

        def query_batch(body):
            result = response('eip_query', {'folder': 'inbox', 'start': '2026-09-01', 'end': '2026-09-07'})
            value = result.json()
            calls = value['choices'][0]['message']['tool_calls']
            other = json.loads(json.dumps(calls[0]))
            other['id'] = 'query-two'
            other['function']['arguments'] = json.dumps({'folder': 'sent', 'start': '2026-09-01', 'end': '2026-09-07'})
            calls.append(other)
            return httpx.Response(200, json=value)
        scripts.extend([
            lambda _:response('delegate_eip', {'task': '查近期 EIP 任務，不製作文件'}), query_batch,
            lambda _:response(
                'complete_assignment', {
                    'outcome': 'completed',
                    'summary': '此查詢範圍沒有任務', 'artifact_ids': [],
                },
            ),
            lambda _:response('complete_job', {'outcome': 'completed', 'summary': '此查詢範圍沒有任務', 'artifact_ids': []}),
        ])
        second = self.submit('eip-followup', instruction='幫我查 EIP 最近任務', references=[first])
        with patch('company_agent.services.eip.client.query', new=AsyncMock(return_value={'documents': []})) as query, patch('company_agent.services.eip.client.binding', new=AsyncMock()):
            await svc.execute(self.queue.claim())
        result = self.queue.public('alice', second)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(query.await_count, 2)
        self.assertFalse(result['files'])
        self.assertFalse(scripts)
        self.assertTrue(all(r['user'] == 'oidc-alice' for r in requests))

    async def test_native_model_failure_escapes_execution_and_closes_client(self):
        failure = RuntimeError('upstream failure')
        key = self.submit()
        model = types.SimpleNamespace(subject='oidc-alice')
        svc = Service(self.queue, AsyncMock(return_value=verified()), lambda _, model_name=None: model, lambda row: self.config)
        with patch('company_agent.api.service.DocumentAgent') as Agent, patch('company_agent.api.service.close_model', new=AsyncMock()) as close:
            Agent.return_value.run = AsyncMock(side_effect=failure)
            with self.assertRaises(RuntimeError) as caught:
                await svc.execute(self.queue.claim())
            self.assertIs(caught.exception, failure)
            close.assert_awaited_once_with(model)
        result = self.queue.public('alice', key)
        self.assertEqual(result['status'], 'running')
        self.assertNotIn('errors', result)
        self.assertNotIn('upstream failure', str(result))

    async def test_rate_limit_is_owned_by_one_job_and_next_jobs_still_run(self):
        from company_agent.api import service as service_module

        requests = []
        clients = []

        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(429, json={'error': {'message': 'PRIVATE original 429'}})
            return response('complete_job', {'outcome': 'completed', 'summary': '新工作完成', 'artifact_ids': []})

        def factory(user, model_name=None):
            model = gateway_for_verified_user(
                user, 'https://gateway.example/v1',
                'key', 'model', httpx.MockTransport(handler),
            )
            clients.append(model.client)
            return model

        async def identity(owner):
            return {**verified('oidc-' + owner), 'id': owner}

        def settings(row):
            return self.config.model_copy(update={'structured_completion': True, 'require_user_billing': True})

        svc = Service(self.queue, identity, factory, settings, max_concurrent=1)
        failed = self.submit()
        # Both a new turn in this chat and another user's job must survive the 429.
        same_user = self.submit('second')
        other_user = self.queue.submit('bob', 'oidc-bob', 'other', 'first', '簡單回覆', [])
        with application_jobs(svc):
            svc.task = asyncio.create_task(svc.loop())
            try:
                async with asyncio.timeout(20):
                    while any(self.queue.get(owner, key)['status'] != 'done' for owner, key in [('bob', other_user), ('alice', same_user)]) or svc.executions:
                        await asyncio.sleep(0.01)
                self.assertFalse(svc.task.done())
                self.assertIs(service_module.service(), svc)
                self.assertEqual(len(requests), 3)  # No replay/retry of the failed call.
                self.assertEqual(svc.result('alice', same_user)['answer'], '新工作完成')
                self.assertEqual(svc.result('bob', other_user)['answer'], '新工作完成')
                interrupted = svc.result('alice', failed)
                self.assertEqual(interrupted['status'], 'error')
                self.assertIn('429', interrupted['answer'])
                self.assertNotIn('PRIVATE', str(interrupted))
                self.assertTrue(interrupted['resumable'])
                self.assertEqual(interrupted['budget']['used'], 1)
                with self.assertRaises(ValueError):
                    svc.result('bob', failed)
                public = self.queue.list('alice')
                self.assertEqual({r['status'] for r in public}, {'done', 'error'})
                self.assertNotIn('PRIVATE', str(public))
                # The failure is terminal, so a later turn may reference its artifacts.
                followup = self.submit('followup', references=[failed])
                self.assertEqual(self.queue.get('alice', followup)['payload']['references'], [failed])
                self.assertTrue(all(c.http_async_client.is_closed and c.http_client.is_closed for c in clients))
                # Only an explicit owner action resumes the saved node.
                self.queue.resume('alice', 'oidc-alice', failed)
                async with asyncio.timeout(20):
                    while any(self.queue.get('alice', key)['status'] != 'done' for key in (failed, followup)):
                        await asyncio.sleep(0.01)
                resumed = svc.result('alice', failed)
                self.assertEqual(resumed['answer'], '新工作完成')
                self.assertEqual(resumed['budget']['used'], 2)
                self.assertEqual(len(requests), 5)  # Follow-up plus explicit resume.
            finally:
                svc.task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await svc.task

    async def test_restart_interrupts_stranded_jobs_without_replaying_them(self):
        old = self.submit()
        self.queue.claim()
        engine = self.store.job('alice')
        artifact = self.store.artifact('alice', engine, 'kept.txt', b'keep')
        self.queue.update(old, engine_job=engine)
        new = self.submit('new')
        svc = Service(self.queue, None, None, None)
        executed = asyncio.Event()

        async def execute(row):
            self.assertEqual(row['id'], new)
            self.queue.update(new, status='done')
            executed.set()
        with patch.object(svc, 'execute', AsyncMock(side_effect=execute)) as run:
            svc.task = asyncio.create_task(svc.loop())
            try:
                await asyncio.wait_for(executed.wait(), timeout=5)
                result = svc.result('alice', old)
                self.assertEqual(result['status'], 'interrupted')
                self.assertEqual(result['files'][0]['id'], artifact['id'])
                self.assertEqual(self.store.read_artifact('alice', artifact['id'])[2], b'keep')
                self.assertEqual(svc.result('alice', new)['status'], 'done')
                run.assert_awaited_once()
            finally:
                svc.task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await svc.task
        # Cancellation released the single-worker lease for the next process.
        with self.queue.store.mutex('worker'):
            pass

    async def test_shutdown_cancels_active_job_without_claiming_the_next(self):
        active = self.submit()
        queued = self.submit('queued')
        svc = Service(self.queue, None, None, None)
        started, stopped = asyncio.Event(), asyncio.Event()

        async def execute(row):
            self.assertEqual(row['id'], active)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        with patch.object(svc, 'execute', AsyncMock(side_effect=execute)):
            svc.task = asyncio.create_task(svc.loop())
            await asyncio.wait_for(started.wait(), timeout=5)
            svc.task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await svc.task
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.queue.get('alice', active)['status'], 'running')
        self.assertEqual(self.queue.get('alice', queued)['status'], 'queued')
        self.assertFalse(hasattr(svc, 'failures'))
        with self.queue.store.mutex('worker'):
            pass

    async def test_stop_queued_work_is_owned_idempotent_and_not_claimed(self):
        cancelled = self.submit()
        following = self.submit('following')
        svc = Service(self.queue, None, None, None)
        with self.assertRaises(ValueError):
            await svc.cancel('bob', cancelled)
        self.assertEqual(self.queue.get('alice', cancelled)['status'], 'queued')
        await svc.cancel('alice', cancelled)
        await svc.cancel('alice', cancelled)
        self.assertEqual(svc.result('alice', cancelled)['status'], 'cancelled')
        self.assertEqual(svc.result('alice', cancelled)['progress'], '已取消')
        self.assertEqual(self.queue.claim()['id'], following)
        self.assertIsNone(self.queue.claim())
        for status in ['done', 'error', 'partial', 'awaiting_approval']:
            self.queue.update(following, status=status)
            await svc.cancel('alice', following)
            self.assertEqual(self.queue.get('alice', following)['status'], status)

    async def test_stop_running_graph_cancels_request_preserves_files_and_serves_next_user(self):
        started, cleaning, cleaned, release = (asyncio.Event() for _ in range(4))
        requests, clients = [], []

        async def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
                    cleaned.set()
            return response('complete_job', {'outcome': 'completed', 'summary': '下一位完成', 'artifact_ids': []})

        def factory(user, model_name=None):
            model = gateway_for_verified_user(
                user, 'https://gateway.example/v1',
                'key', 'model', httpx.MockTransport(handler),
            )
            clients.append(model.client)
            return model

        async def identity(owner):
            return {**verified('oidc-' + owner), 'id': owner}
        svc = Service(
            self.queue, identity, factory, lambda row: self.config.model_copy(
                update={'structured_completion': True, 'require_user_billing': True},
            ), max_concurrent=1,
        )
        active = self.submit(instruction='同一工作')
        self.assertEqual(self.submit('regenerated', instruction='同一工作'), active)
        following = self.queue.submit('bob', 'oidc-bob', 'other-chat', 'next', '下一位工作', [])
        svc.task = asyncio.create_task(svc.loop())
        stops = []
        try:
            await asyncio.wait_for(started.wait(), 10)
            with self.assertRaises(ValueError):
                await svc.cancel('bob', active)
            self.assertFalse(cleaning.is_set())
            engine = self.queue.get('alice', active)['engine_job']
            self.assertIsNotNone(engine)
            artifact = self.store.artifact('alice', engine, 'before-stop.txt', b'preserved')
            stops.append(asyncio.create_task(svc.cancel('alice', active)))
            await asyncio.wait_for(cleaning.wait(), 5)
            # Two WebUI tasks can watch one regenerated job. Stop both without
            # cancelling the native client's asynchronous cleanup a second time.
            stops.append(asyncio.create_task(svc.cancel('alice', active)))
            await asyncio.sleep(0)
            self.assertEqual(len(requests), 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(*stops), 5)
            self.assertEqual(svc.result('alice', active)['status'], 'cancelled')
            self.assertTrue(cleaned.is_set())
            async with asyncio.timeout(10):
                # The result is committed before the worker removes its task.
                # Wait for cleanup before asserting closed clients and no tasks.
                while self.queue.get('bob', following)['status'] != 'done' or svc.executions:
                    await asyncio.sleep(0.01)
            self.assertFalse(svc.task.done())
            self.assertEqual([r['user'] for r in requests], ['oidc-alice', 'oidc-bob'])
            self.assertEqual(self.store.read_artifact('alice', artifact['id'])[2], b'preserved')
            self.assertEqual(svc.result('alice', active)['files'][0]['id'], artifact['id'])
            self.assertEqual(svc.result('bob', following)['answer'], '下一位完成')
            self.assertTrue(all(c.http_async_client.is_closed and c.http_client.is_closed for c in clients))
            self.assertFalse(hasattr(svc, 'failures'))
            self.assertFalse(svc.executions)
        finally:
            release.set()
            svc.task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await svc.task
            await asyncio.gather(*stops, return_exceptions=True)

    async def test_internal_task_never_creates_work(self):
        self.assertEqual(await run_pipe({}, None, None, task='title_generation'), '公司工作助理')
        self.assertFalse(self.queue.list('alice'))

    async def test_saved_turn_only_current_attachment_not_history(self):
        chat = {
            'history': {
                'messages': {
                    'old': {'id': 'old', 'role': 'user', 'content': 'old instruction', 'files': [{'id': 'old-file'}]},
                    'new': {'id': 'new', 'role': 'user', 'content': 'new instruction', 'files': [{'id': 'new-file'}], 'parentId': 'old-answer'},
                    'answer': {'role': 'assistant', 'parentId': 'new'},
                },
            },
        }
        self.assertEqual(current_turn(chat, 'new', 'answer'), ('new instruction', ['new-file'], 'new'))
        with self.assertRaises(ValueError):
            current_turn(chat, 'old', 'answer')


class RouterTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def asyncSetUp(self):
        origin = patch.dict(os.environ, {'EIP_WEBUI_ORIGIN': 'https://testserver'})
        origin.start()
        self.addCleanup(origin.stop)
        self.queue = WorkQueue(self.private, self.base/'uploads')

        async def auth(request: Request):
            owner = request.headers.get('x-test-user')
            if not owner or owner == 'pending':
                raise HTTPException(401, 'Login required')
            return types.SimpleNamespace(id=owner, role='pending' if owner == 'pending' else 'user')
        module = types.ModuleType('open_webui.utils.auth')
        module.get_verified_user = auth
        for name in ('company_agent.api.router', 'company_agent.api.http', 'company_agent.api.identity'):
            sys.modules.pop(name, None)
        with patch.dict(sys.modules, {'open_webui.utils.auth': module}):
            self.router = importlib.import_module('company_agent.api.router')
        self.addCleanup(lambda: sys.modules.pop('company_agent.api.router', None))
        app = FastAPI()
        app.include_router(self.router.router)
        current = Service(self.queue, None, None, None)
        self.mock = patch.object(self.router, 'service', return_value=current)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        configured = patch.object(self.router, 'configured_service', return_value=current)
        configured.start()
        self.addCleanup(configured.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app, raise_app_exceptions=False,
            ), base_url='https://testserver',
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_capacity_requires_admin_before_reading_global_counters(self):
        self.assertEqual((await self.client.get('/agents/capacity')).status_code, 401)
        self.assertEqual((await self.client.get('/agents/capacity', headers={'x-test-user': 'alice'})).status_code, 403)

    async def test_login_pending_and_cross_user_download(self):
        self.assertEqual((await self.client.get('/agents')).status_code, 401)
        self.assertEqual((await self.client.get('/agents/jobs')).status_code, 401)
        self.assertEqual((await self.client.get('/agents', headers={'x-test-user': 'pending'})).status_code, 401)
        key = self.queue.submit('alice', 'oidc-alice', 'chat', 'm', 'hi', [])
        engine = self.store.job('alice')
        artifact = self.store.artifact('alice', engine, 'a.txt', b'private')
        self.queue.update(key, engine_job=engine, status='done')
        for url in ('/agents/jobs/'+key, '/agents/artifacts/'+artifact['id']):
            denied = await self.client.get(url, headers={'x-test-user': 'bob'})
            self.assertEqual(denied.status_code, 500)
        response = await self.client.get('/agents/artifacts/'+artifact['id'], headers={'x-test-user': 'alice'})
        self.assertEqual(response.content, b'private')
        self.assertEqual(response.headers['cache-control'], 'no-store')

    async def test_failed_job_does_not_break_listing_other_jobs_or_downloads(self):
        svc = Service(self.queue, None, None, None)
        self.router.service.return_value = svc
        failed = self.queue.submit('alice', 'oidc-alice', 'chat', 'failed', 'first', [])
        self.queue.update(failed, status='error', error_code='execution_failed', progress='執行未完成')
        good = self.queue.submit('alice', 'oidc-alice', 'chat', 'good', 'second', [])
        engine = self.store.job('alice')
        artifact = self.store.artifact('alice', engine, 'result.txt', b'preserved')
        self.queue.update(good, engine_job=engine, status='done')
        headers = {'x-test-user': 'alice'}
        listing = await self.client.get('/agents/jobs', headers=headers)
        self.assertEqual(listing.status_code, 200)
        self.assertNotIn('PRIVATE', listing.text)
        self.assertEqual((await self.client.get('/agents/jobs/' + failed, headers=headers)).status_code, 200)
        result = await self.client.get('/agents/jobs/' + good, headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['status'], 'done')
        download = await self.client.get('/agents/artifacts/' + artifact['id'], headers=headers)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b'preserved')

    async def test_cancel_api_is_owned_and_requires_csrf_before_recording_intent(self):
        key = self.queue.submit('alice', 'oidc-alice', 'chat', 'cancel-api', 'pending work', [])
        route = '/agents/jobs/'+key+'/cancel'
        denied = await self.client.post(route, json={}, headers={'x-test-user': 'alice', 'Origin': 'https://evil.example', 'X-Company-Action': '1'})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.queue.get('alice', key)['status'], 'queued')
        headers = {'x-test-user': 'bob', 'Origin': 'https://testserver', 'X-Company-Action': '1'}
        self.assertEqual((await self.client.post(route, json={}, headers=headers)).status_code, 500)
        self.assertEqual(self.queue.get('alice', key)['status'], 'queued')
        headers['x-test-user'] = 'alice'
        response = await self.client.post(route, json={}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'cancelled')
        self.assertEqual((await self.client.post(route, json={}, headers=headers)).json()['status'], 'cancelled')

    async def test_csrf_denied_before_any_action(self):
        response = await self.client.post('/agents/jobs/anything/resume', json={}, headers={'x-test-user': 'alice', 'Origin': 'https://evil.example', 'X-Company-Action': '1'})
        self.assertEqual(response.status_code, 403)

    async def test_removed_extension_endpoint(self):
        self.assertEqual((await self.client.post('/agents/jobs/anything/extend', json={})).status_code, 404)

    async def test_connections_page_requires_login_and_keeps_credentials_out(self):
        for url in ('/connections', '/connections/'):
            response = await self.client.get(url+'?redirect=https://evil.example')
            self.assertEqual(response.status_code, 401)
            self.assertEqual((await self.client.get(url, headers={'x-test-user': 'pending'})).status_code, 401)
        for url in ('/connections', '/connections/app.js', '/connections/app.css'):
            result = await self.client.get(url, headers={'x-test-user': 'alice'})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.headers['cache-control'], 'no-store')
        html = (await self.client.get('/connections', headers={'x-test-user': 'alice'})).text
        self.assertIn('id="eip-form"', html)
        self.assertIn('id="nas-form"', html)
        self.assertIn('id="identity-account"', html)
        self.assertIn('id="identity-visionnaire-users"', html)
        self.assertNotIn('identity-keycloak-users', html)
        self.assertNotIn('/keycloak/admin/', html)
        with patch.dict(os.environ, {'OPENID_PROVIDER_URL': 'https://sso.example/keycloak/realms/visionnaire/.well-known/openid-configuration'}):
            identity = await self.client.get('/connections/identity', headers={'x-test-user': 'alice'})
            self.assertEqual(identity.status_code, 200)
            self.assertFalse(identity.json()['admin'])
        self.assertNotIn(' checked', html)

    async def test_connection_banners_preserve_unrelated_notices(self):
        from company_agent.ops.deploy_webui import updated_banners, ENTRIES
        other = {'id': 'company-announcement', 'content': 'keep custom announcement'}
        before = [other, {'id': 'ai-gateway-eip-workspace', 'content': 'old'}]
        after = updated_banners(before)
        self.assertEqual(after[0], other)
        self.assertEqual(len(after), 4)
        self.assertEqual({b['id'] for b in after[1:]}, set(ENTRIES))
        self.assertEqual(len(updated_banners(after)), 4)

    async def test_nas_connections_work_after_worker_failure_without_hiding_job_error(self):
        from company_agent.api import service as service_module
        from company_agent.services.files.nas import NASClient
        from company_agent.tests.test_nas import FakeDSM

        dsm = FakeDSM()

        def factory(*args):
            return NASClient(*args, transport=httpx.MockTransport(dsm.handler))
        failure = ValueError('NAS Session invalid 119 from previous job')
        task = asyncio.get_running_loop().create_future()
        task.set_exception(failure)
        current = types.SimpleNamespace(queue=self.queue, task=task)
        headers = {'x-test-user': 'alice', 'Origin': 'https://testserver', 'X-Company-Action': '1'}
        with (
            application_jobs(current),
            patch.object(self.router, 'service', service_module.service),
            patch.object(self.router, 'configured_service', service_module.configured_service),
            patch.dict(
                os.environ, {
                    'COMPANY_AGENT_NAS_URL': 'https://nas.example:5001',
                    'EIP_WEBUI_ORIGIN': 'https://testserver',
                },
            ),
            patch('company_agent.services.files.nas.NASClient', factory),
        ):
            with self.assertRaises(ValueError) as raised:
                service_module.service()
            self.assertIs(raised.exception, failure)
            self.assertEqual((await self.client.get('/agents/jobs', headers=headers)).status_code, 500)
            self.assertEqual((await self.client.get('/agents/nas')).status_code, 401)
            self.assertEqual((await self.client.post('/agents/nas/test', json={}, headers={'x-test-user': 'alice'})).status_code, 403)
            self.assertFalse(dsm.calls)
            bound = await self.client.post(
                '/agents/nas/bind', json={
                    'account': 'alice', 'password': 'alice-password', 'consent': True,
                }, headers=headers,
            )
            self.assertEqual(bound.status_code, 200, bound.text)
            status = await self.client.get('/agents/nas', headers=headers)
            self.assertTrue(status.json()['bound'])
            bob = await self.client.get('/agents/nas', headers={**headers, 'x-test-user': 'bob'})
            self.assertFalse(bob.json()['bound'])
            test = await self.client.post('/agents/nas/test', json={}, headers=headers)
            self.assertEqual(test.status_code, 200, test.text)
            self.assertEqual({item['name'] for item in test.json()['shares']}, {'home', 'shared'})
            self.assertFalse(dsm.sessions)
            self.assertNotIn('alice-password', test.text)
            removed = await self.client.post('/agents/nas/unlink', json={}, headers=headers)
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertFalse(removed.json()['bound'])
            self.assertEqual((await self.client.get('/agents/jobs', headers=headers)).status_code, 500)
            self.assertIs(current.task.exception(), failure)

    async def test_nas_binding_auth_csrf_consent_and_owner_isolation(self):
        from company_agent.services.files.nas import NASClient
        from company_agent.tests.test_nas import FakeDSM
        dsm = FakeDSM()

        def factory(*a):
            return NASClient(*a, transport=httpx.MockTransport(dsm.handler))
        headers = {'x-test-user': 'alice', 'Origin': 'https://testserver', 'X-Company-Action': '1'}
        payload = {'account': 'alice', 'password': 'alice-password', 'consent': True}
        with patch.dict(os.environ, {'COMPANY_AGENT_NAS_URL': 'https://nas.example:5001', 'EIP_WEBUI_ORIGIN': 'https://testserver'}), patch('company_agent.services.files.nas.NASClient', factory):
            self.assertEqual((await self.client.get('/agents/nas')).status_code, 401)
            self.assertEqual((await self.client.post('/agents/nas/bind', json=payload, headers={'x-test-user': 'alice'})).status_code, 403)
            for bad in ({**payload, 'owner': 'bob'}, {**payload, 'consent': False}, {**payload, 'url': 'https://evil.example'}):
                self.assertEqual((await self.client.post('/agents/nas/bind', json=bad, headers=headers)).status_code, 500)
            self.assertFalse(dsm.calls)
            bound = await self.client.post('/agents/nas/bind', json=payload, headers=headers)
            self.assertEqual(bound.status_code, 200, bound.text)
            self.assertNotIn('alice-password', bound.text)
            self.assertEqual(bound.headers['cache-control'], 'no-store')
            bob = {**headers, 'x-test-user': 'bob'}
            self.assertFalse((await self.client.get('/agents/nas', headers=bob)).json()['bound'])
            self.assertEqual((await self.client.post('/agents/nas/unlink', json={}, headers=bob)).status_code, 200)
            result = await self.client.post('/agents/nas/test', json={}, headers=headers)
            self.assertEqual({x['name'] for x in result.json()['shares']}, {'home', 'shared'})
            self.assertFalse(dsm.sessions)
            self.assertEqual((await self.client.post('/agents/nas/unlink', json={}, headers=headers)).status_code, 200)
            self.assertFalse((await self.client.get('/agents/nas', headers=headers)).json()['bound'])


    async def test_multiple_nas_api_lists_updates_tests_and_removes_only_owned_binding(self):
        from company_agent.services.files.nas import NASClient
        from company_agent.tests.test_nas import FakeDSM
        dsm = FakeDSM()
        def factory(*a):
            return NASClient(*a, transport=httpx.MockTransport(dsm.handler))
        headers = {'x-test-user': 'alice', 'Origin': 'https://testserver', 'X-Company-Action': '1'}
        with patch.dict(os.environ, {'COMPANY_AGENT_NAS_URL': 'https://nas.example:5001', 'EIP_WEBUI_ORIGIN': 'https://testserver'}), patch('company_agent.services.files.nas.NASClient', factory):
            one = (await self.client.post('/agents/nas/bind', json={'account': 'alice', 'password': 'alice-password', 'consent': True}, headers=headers)).json()
            with self.store.db() as db:
                db.execute('UPDATE nas_login_limits SET next_at=0')
            response = await self.client.post('/agents/nas/bind', json={'account': 'team', 'password': 'team-password', 'consent': True, 'label': '工程部'}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            two = response.json()
            listed = await self.client.get('/agents/nas', headers=headers)
            self.assertEqual(len(listed.json()['bindings']), 2)
            self.assertNotIn('password', listed.text)
            self.assertEqual(listed.headers['cache-control'], 'no-store')
            for path in ('test', 'unlink'):
                self.assertEqual((await self.client.post('/agents/nas/'+path, json={}, headers=headers)).status_code, 500)
                count = len(dsm.calls)
                foreign = await self.client.post('/agents/nas/'+path, json={'binding_id': one['binding_id']}, headers={**headers, 'x-test-user': 'bob'})
                self.assertEqual(foreign.status_code, 500)
                self.assertEqual(len(dsm.calls), count)
            tested = await self.client.post('/agents/nas/test', json={'binding_id': two['binding_id']}, headers=headers)
            self.assertEqual(tested.status_code, 200, tested.text)
            self.assertEqual(tested.json()['revision'], two['revision'])
            self.assertFalse(dsm.sessions)
            no_csrf = await self.client.post('/agents/nas/unlink', json={'binding_id': two['binding_id']}, headers={'x-test-user': 'alice'})
            self.assertEqual(no_csrf.status_code, 403)
            removed = await self.client.post('/agents/nas/unlink', json={'binding_id': one['binding_id']}, headers=headers)
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertEqual([b['binding_id'] for b in removed.json()['bindings']], [two['binding_id']])
