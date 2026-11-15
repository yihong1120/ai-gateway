"""Native summary updates and checkpoint reuse with isolated PostgreSQL and HTTP."""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.messages import HumanMessage
from langchain_core.messages import messages_to_dict
from langgraph.graph.message import add_messages
from openai import InternalServerError

from company_agent.config import ModelRoute
from company_agent.graph import build_graph
from company_agent.runtime.compaction import ChatSummarization
from company_agent.runtime.context import Context
from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.models import close_model
from company_agent.runtime.models import gateway_for_verified_user
from company_agent.runtime.models import routed_model
from company_agent.runtime.models import WebBilledModel
from company_agent.tests.postgres_fixture import checkpointer
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_user_billing import response
from company_agent.tests.test_user_billing import verified


def transcript(size=6, width=1300):
    return [
        *[HumanMessage(content=f"SOURCE_{i}_" + '內容' * width, id=f"u{i}") for i in range(size)],
        HumanMessage(content='CURRENT_REQUEST_AND_FILE_IDS', id='current'),
    ]


class CompactionTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.requests = []
        self.events = []
        self.fail = None
        self.summary = '甲工程方案與期限 2026/09/11；待確認施工時間，未核准外部寫入。'
        self.model = self.gateway(self.handle)
        self.config.summary_trigger_tokens = 4096
        self.ctx = Context('alice', self.job, self.config, self.store, self.model)

    def gateway(self, handler, user=None):
        model = gateway_for_verified_user(
            user or verified(), 'https://gateway.example/v1', 'key', 'model', httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(close_model, model)
        return model

    def handle(self, request):
        self.requests.append(json.loads(request.content))
        if self.fail:
            return httpx.Response(self.fail, json={'error': {'message': 'PROVIDER_FAILURE'}})
        return httpx.Response(
            200, json={
                'choices': [{'message': {'role': 'assistant', 'content': self.summary}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130},
            },
        )

    async def compact(self, messages, ctx=None):
        update = await ChatSummarization(ctx or self.ctx, self.events.append).abefore_model(
            {'messages': messages}, None,
        )
        return add_messages(messages, update['messages']) if update else messages

    async def snapshot(self, agent, result):
        async with checkpointer(self.store) as saver:
            return await build_graph(agent.model, saver, self.config.structured_completion).aget_state(
                {'configurable': {'thread_id': result['job_id']}},
            )

    def history(self):
        return [{'id': f"old-{i}", 'role': 'user', 'content': 'CHAT_BACKGROUND_' + '甲' * 2400}
                for i in range(3)]

    async def test_short_conversation_needs_no_model_call(self):
        messages = transcript(1, 10)
        self.assertIs(await self.compact(messages), messages)
        self.assertFalse(self.requests)
        self.assertEqual(self.store.attempts(self.job), 0)

    async def test_official_summary_update_retains_latest_and_covers_older_input(self):
        messages = transcript()
        before = messages_to_dict(messages)
        result = await self.compact(messages)
        self.assertEqual(messages_to_dict(messages), before)
        self.assertEqual(result[-1], messages[-1])
        self.assertIn(self.summary, result[0].content)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(self.requests), 1)
        for i in range(6):
            self.assertIn(f"SOURCE_{i}_", str(self.requests))
        self.assertNotIn('CURRENT_REQUEST_AND_FILE_IDS', str(self.requests))
        self.assertEqual(self.requests[0]['user'], 'oidc-alice')
        self.assertNotIn('tools', self.requests[0])
        self.assertEqual(self.store.attempts(self.job), 1)
        self.assertEqual(self.events[0]['stage'], 'compact')

    async def test_large_old_message_is_sent_once_without_manual_chunks_or_tail_loss(self):
        text = 'BEGIN_' + '甲' * 65000 + '_END'
        messages = [HumanMessage(content=text, id='old'), HumanMessage(content='CURRENT', id='current')]
        result = await self.compact(messages)
        self.assertEqual(len(self.requests), 1)
        self.assertIn(text, self.requests[0]['messages'][0]['content'])
        self.assertEqual(result[-1].content, 'CURRENT')

    async def test_new_jobs_summarize_their_own_current_input_without_shared_cache(self):
        await self.compact(transcript(2))
        other_job = self.store.job('alice')
        messages = transcript(2)
        messages[0].content += 'EDITED_BRANCH'
        await self.compact(messages, Context('alice', other_job, self.config, self.store, self.model))
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn('EDITED_BRANCH', str(self.requests[0]))
        self.assertIn('EDITED_BRANCH', str(self.requests[1]))
        self.assertEqual(self.store.attempts(other_job), 1)

    async def test_other_owner_uses_own_billing_and_mismatched_binding_cannot_summarize(self):
        bob = self.gateway(self.handle, {**verified(), 'id': 'bob', 'oauth': {'oidc': {'sub': 'oidc-bob'}}})
        job = self.store.job('bob')
        await self.compact(transcript(2), Context('bob', job, self.config, self.store, bob))
        self.assertEqual(self.requests[-1]['user'], 'oidc-bob')
        with self.assertRaisesRegex(ValueError, '本人'):
            await self.compact(transcript(2), Context('bob', job, self.config, self.store, self.model))
        self.assertEqual(len(self.requests), 1)

    async def test_summary_uses_its_configured_model_and_counts_toward_job_limit(self):
        self.config.purpose_models = {'summary': ModelRoute(model='summary-only', max_tokens=1024)}
        self.config.max_steps = 1
        ctx = Context('alice', self.job, self.config, self.store, routed_model(self.model, self.config))
        await self.compact(transcript(2), ctx)
        self.assertEqual(self.requests[0]['model'], 'summary-only')
        with self.assertRaises(ModelCallLimitExceededError):
            await self.compact(transcript(2), ctx)
        self.assertEqual(len(self.requests), 1)

    async def test_provider_exception_propagates_without_error_summary_or_automatic_retry(self):
        self.fail = 503
        messages = transcript(2)
        before = messages_to_dict(messages)
        with self.assertRaises(InternalServerError):
            await self.compact(messages)
        self.assertEqual(messages_to_dict(messages), before)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.store.attempts(self.job), 1)

    async def test_web_identity_rechecked_before_summary_http(self):
        web = WebBilledModel(self.model.owner, self.model.subject, self.model.client)
        ctx = Context('alice', self.job, self.config, self.store, web)
        with patch('examples.identity.adapters.webui.verified_user', AsyncMock(return_value=verified('changed'))):
            with self.assertRaisesRegex(ValueError, '計費身分'):
                await self.compact(transcript(2), ctx)
        self.assertFalse(self.requests)

    async def test_full_graph_checkpoints_summary_and_counts_usage(self):
        def handler(request):
            body = json.loads(request.content)
            if body.get('tools'):
                self.assertIn('CURRENT_TASK', str(body['messages']))
                self.assertIn(self.summary, str(body['messages']))
                self.assertNotIn('CHAT_BACKGROUND_', str(body['messages']))
                return response(
                    'complete_job', {
                        'outcome': 'needs_input', 'summary': '請補充施工時間', 'artifact_ids': [],
                    },
                )
            return self.handle(request)

        self.config.structured_completion = True
        agent = DocumentAgent(self.config, self.gateway(handler))
        history = self.history()
        original = json.dumps(history)
        result = await agent.run('alice', 'CURRENT_TASK', conversation=history)
        self.assertEqual(result['status'], 'needs_input', result)
        self.assertEqual(result['usage'], {'input_tokens': 110, 'output_tokens': 35})
        self.assertEqual(result['model_steps'], 2)
        self.assertEqual(json.dumps(history), original)
        snapshot = await self.snapshot(agent, result)
        self.assertIn(self.summary, snapshot.values['messages'][0].content)
        self.assertEqual(snapshot.values['messages'][1].content, 'CURRENT_TASK')
        self.assertNotIn('CHAT_BACKGROUND_', str(snapshot.values['messages']))
        self.assertEqual(len(self.requests), 1)

    async def test_model_failure_resume_reuses_checkpointed_summary_without_rebilling(self):
        main_calls = []

        def handler(request):
            body = json.loads(request.content)
            if not body.get('tools'):
                return self.handle(request)
            main_calls.append(body)
            if len(main_calls) == 1:
                return httpx.Response(503, json={'error': {'message': 'temporary'}})
            return response('complete_job', {'outcome': 'needs_input', 'summary': '請補充資料', 'artifact_ids': []})

        self.config.structured_completion = True
        agent = DocumentAgent(self.config, self.gateway(handler))
        failed = await self.failed_job(agent.run('alice', '繼續工作', conversation=self.history()), InternalServerError)
        self.assertEqual(len(self.requests), 1)
        result = await agent.resume('alice', failed['job_id'])
        self.assertEqual(result['status'], 'needs_input', result)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(main_calls), 2)
        self.assertEqual(result['model_steps'], 3)

    async def test_approval_resume_retains_tool_receipts_without_resummarizing(self):
        script = [
            lambda _: response('delegate_document', {'task': '建立並申請存檔'}),
            lambda _: response('create_text', {'name': 'approved.txt', 'content': 'CURRENT_TOOL_RESULT'}),
            lambda body: response(
                'propose_publish', {
                    'artifact_id': json.loads(body['messages'][-1]['content'])['id'],
                    'root': 'project', 'path': 'approved.txt',
                },
            ),
            lambda _: response(content='已核准存檔'),
            lambda _: response(content='完成'),
        ]

        def handler(request):
            body = json.loads(request.content)
            return script.pop(0)(body) if body.get('tools') else self.handle(request)

        agent = DocumentAgent(self.config, self.gateway(handler))
        pending = await agent.run('alice', '建立 approved.txt 並存回', conversation=self.history())
        self.assertEqual(pending['status'], 'awaiting_approval', pending)
        self.assertFalse((self.root / 'approved.txt').exists())
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn('CURRENT_TOOL_RESULT', str(self.requests))
        done = await agent.resume(
            'alice', pending['job_id'], {
                'proposal_id': pending['proposals'][0]['proposal_id'], 'decision': 'approve',
            },
        )
        self.assertEqual(done['status'], 'done', done)
        self.assertEqual((self.root / 'approved.txt').read_text(), 'CURRENT_TOOL_RESULT')
        self.assertEqual(len(done['files']), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(script)
