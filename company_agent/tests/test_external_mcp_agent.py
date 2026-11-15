"""Run discovered, unrelated remote schemas through the real delegated graph."""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

import httpx
from openai import APIConnectionError

from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.models import close_model
from company_agent.runtime.models import gateway_for_verified_user
from company_agent.services.external_tools.client import ExternalTools
from company_agent.services.external_tools.config import MCPServerConfig
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_external_mcp_client import definition
from company_agent.tests.test_external_mcp_client import Factory


class ExternalMCPAgentTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.structured_completion = True
        self.server = MCPServerConfig(name='archive', url='https://archive.example/mcp', allowed_tools=['find_records'])
        self.config.mcp_servers = [self.server]
        self.factory = Factory()
        self.factory.tools = [
            definition(
                'find_records', {
                    'type': 'object', 'properties': {'phrase': {'type': 'string', 'minLength': 1}},
                    'required': ['phrase'], 'additionalProperties': False,
                },
            ),
        ]
        self.factory.result = {
            'structuredContent': {
                'items': ['source-owned-record'],
                'provenance': {'coverage': 'one-page', 'continuation': 'next-page', 'record_count': 1},
            }, 'isError': False,
        }
        self.tokens = AsyncMock(return_value='synthetic-personal-bearer')
        self.client = ExternalTools([self.server], self.tokens, session_factory=self.factory)
        self.model_requests = []

    def model_response(self, request):
        payload = json.loads(request.content)
        self.model_requests.append(payload)
        if not self.steps:
            raise httpx.ConnectError('synthetic-interruption', request=request)
        operation, arguments = self.steps.pop(0)
        return httpx.Response(
            200, json={
                'id': 'synthetic-completion', 'object': 'chat.completion', 'created': 1, 'model': 'test-model',
                'choices': [{
                    'index': 0, 'finish_reason': 'tool_calls', 'message': {
                        'role': 'assistant', 'content': '', 'tool_calls': [{
                            'id': 'provider-call', 'type': 'function',
                            'function': {'name': operation, 'arguments': json.dumps(arguments)},
                        }],
                    },
                }],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
            },
        )

    async def execute(self, steps):
        self.steps = list(steps)
        model = gateway_for_verified_user(
            {'id': 'alice', 'role': 'user', 'oauth': {'oidc': {'sub': 'alice-sub'}}},
            'https://models.example/v1', 'synthetic-model-key', 'test-model', httpx.MockTransport(self.model_response),
        )
        self.billed_model = model
        self.addAsyncCleanup(close_model, model)
        return await DocumentAgent(self.config, model, external_tools=self.client).run('alice', '查詢來源紀錄並說明範圍')

    async def test_dynamic_schema_is_available_only_to_knowledge_and_keeps_source_provenance(self):
        result = await self.execute([
            ('delegate_knowledge', {'task': '查詢來源紀錄並保留涵蓋範圍'}),
            ('external_archive_find_records', {'phrase': 'permit'}),
            ('complete_assignment', {'outcome': 'completed', 'summary': '來源第一頁有一筆紀錄。', 'artifact_ids': []}),
            ('complete_job', {'outcome': 'completed', 'summary': '第一頁找到一筆紀錄，尚有後續。', 'artifact_ids': []}),
        ])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(self.tokens.await_count, 2)
        self.assertTrue(all(call.args == ('alice', 'alice-sub') for call in self.tokens.await_args_list))
        self.assertTrue(all(payload['user'] == 'alice-sub' for payload in self.model_requests))
        self.factory.sessions[1].call_tool.assert_awaited_once_with(
            'find_records', {'phrase': 'permit'}, read_timeout_seconds=20,
        )
        reads = [item for item in result['read_results'] if item.get('kind') == 'external_tool']
        self.assertEqual(reads[0]['provenance']['continuation'], 'next-page')
        self.assertEqual(reads[0]['tool'], 'find_records')
        self.assertNotIn('synthetic-personal-bearer', json.dumps(result))
        self.assertNotIn('synthetic-personal-bearer', json.dumps(self.model_requests))
        self.assertTrue(any('external_reads' in json.dumps(p) for p in self.model_requests))
        self.assertFalse(any(t['function']['name'].startswith('external_') for t in self.model_requests[0]['tools']))
        remote = next(t for t in self.model_requests[1]['tools'] if t['function']['name'].startswith('external_'))
        self.assertEqual(set(remote['function']['parameters']['properties']), {'phrase'})

    async def test_remote_failure_is_a_blocker_without_retry_or_empty_success(self):
        self.factory.result = {'isError': True, 'content': [{'type': 'text', 'text': 'secret-upstream-error'}]}
        result = await self.execute([
            ('delegate_knowledge', {'task': '查詢；不可用則交回限制'}),
            ('external_archive_find_records', {'phrase': 'permit'}),
            (
                'complete_assignment', {
                    'outcome': 'blocked',
                    'summary': '來源暫時不可用。', 'blockers': ['來源拒絕查詢'], 'artifact_ids': [],
                },
            ),
            (
                'complete_job', {
                    'outcome': 'blocked', 'summary': '目前無法查詢來源。',
                    'blockers': ['來源查詢失敗'], 'artifact_ids': [],
                },
            ),
        ])
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(self.tokens.await_count, 2)
        self.assertFalse([r for r in result['read_results'] if r.get('kind') == 'external_tool'])
        self.assertNotIn('secret-upstream-error', json.dumps(result))

    async def test_schema_change_cannot_resume_previous_work(self):
        with self.assertRaises(APIConnectionError):
            await self.execute([
                ('delegate_knowledge', {'task': '查詢來源'}),
                ('external_archive_find_records', {'phrase': 'permit'}),
            ])
        with self.store.db() as database:
            job = database.execute(
                'SELECT id FROM jobs WHERE owner=%s ORDER BY created DESC LIMIT 1', ('alice',),
            ).fetchone()[0]
        model_calls = len(self.model_requests)
        tool_calls = sum(session.call_tool.await_count for session in self.factory.sessions)
        self.factory.tools[0]['description'] = 'Changed source schema contract'
        with self.assertRaisesRegex(ValueError, 'schema 已變更'):
            await DocumentAgent(self.config, self.billed_model, external_tools=self.client).resume('alice', job)
        self.assertEqual(len(self.model_requests), model_calls)
        self.assertEqual(sum(session.call_tool.await_count for session in self.factory.sessions), tool_calls)

    async def test_discovery_obeys_the_whole_job_timeout_before_model_calls(self):
        self.config.execution_timeout_seconds = 1
        self.client.discover = AsyncMock(side_effect=blocked_discovery)
        with self.assertRaises(TimeoutError):
            await self.execute([])
        self.assertFalse(self.model_requests)


async def blocked_discovery(*args):
    await asyncio.sleep(10)
